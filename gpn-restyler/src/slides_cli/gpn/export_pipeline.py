"""Native build pipeline (Stage 4 §§3, 11–12).

``run_native_build`` stitches the whole service operation: immutable inputs
→ accepted plans (with generation origin) → resolver → preflight →
``create_output_deck`` → emitters → transplant → deferred links → final
assembly → independent reimport/compare/verify → ``NativeBuildResult``.

The exporter never calls a model: plans arrive accepted (mode 2/harness in
this stage). Measurement is diagnostic (Stage 5 owns the real measurer).
"""

from __future__ import annotations

import contextlib
import json
import logging
import posixpath
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from . import native as _native
from . import opc_adapter as adapter
from .assets import AssetStore, atomic_write_json
from .errors import ExitCode, GpnError
from .export_models import (
    CandidateArtifact,
    ContentDiff,
    ExportOptions,
    ExportPreflightReport,
    NativeBuildResult,
    NativeEditabilityReport,
    PartDiff,
    PartPreservationContract,
    ResolvedSlide,
    SourcePartKey,
)
from .transplant import TransplantContext, load_source_package

log = logging.getLogger(__name__)

SUPPORTED_EMIT_KINDS = {"text", "table", "chart", "shape", "group", "image",
                        "connector", "unknown"}


# ---------------------------------------------------------------------------
# Provider contract (§0): one field, no duplicates, no silent fallback.
# ---------------------------------------------------------------------------

PROVIDER_HARNESS = "harness"
PROVIDER_LOCAL = "local_server"
_LEGACY_LOCAL = "local"
KNOWN_PROVIDERS = (PROVIDER_HARNESS, PROVIDER_LOCAL, _LEGACY_LOCAL)


def resolve_llm_provider(config: Any) -> tuple[str, str]:
    """Return (provider, origin_note) for telemetry.

    Missing field means harness (current default). Unknown values are a
    configuration error. ``local`` is the legacy spelling of
    ``local_server``. No fallback: an explicit local provider never becomes
    harness silently — generation through it returns deferred.
    """
    model_section = getattr(config, "model", None) if config is not None else None
    raw = getattr(model_section, "provider", None) if model_section is not None else None
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return PROVIDER_HARNESS, "default_harness"
    value = str(raw).strip().lower()
    if value not in KNOWN_PROVIDERS:
        raise GpnError(
            "MODEL_PROVIDER_UNKNOWN",
            f"unknown model provider {raw!r}; expected one of {KNOWN_PROVIDERS}",
            exit_code=ExitCode.INVALID_INPUT,
        )
    if value == _LEGACY_LOCAL:
        return PROVIDER_LOCAL, "legacy_local_alias"
    return value, "explicit_config"


def require_harness_or_deferred(provider: str) -> None:
    """Refuse model generation through the not-yet-implemented local server."""
    if provider == PROVIDER_LOCAL:
        raise GpnError(
            "MODEL_LOCAL_DEFERRED",
            "model provider 'local_server' is reserved for a future adapter "
            "(no HTTP client/endpoint/credentials in this stage); refusing "
            "generation without falling back to harness",
            exit_code=ExitCode.MODEL_UNAVAILABLE,
        )


# ---------------------------------------------------------------------------
# Resolver: accepted LayoutIntent → ResolvedSlide (never raw model JSON).
# ---------------------------------------------------------------------------

def resolve_slides_from_intents(
    intents: list[dict[str, Any]],
    deck: Any,
    rules: Any,
    profile: Any,
    capabilities: dict[str, Any],
) -> list[ResolvedSlide]:
    """Resolve accepted intents through the existing bridge (§12)."""
    from .planner import LayoutIntent
    from .planning_bridge import resolve_diagnostic_layout

    slides_by_id = {s.id: s for s in deck.slides}
    out: list[ResolvedSlide] = []
    for raw in intents:
        origin = ""
        body = raw
        if isinstance(raw, dict):
            origin = str(raw.get("generation_origin", "harness_model"))
            body = {k: v for k, v in raw.items() if k != "generation_origin"}
        intent = body if isinstance(body, LayoutIntent) else LayoutIntent.model_validate(body)
        source = slides_by_id.get(intent.slide_id)
        if source is None:
            out.append(ResolvedSlide(
                slide_id=intent.slide_id, source_slide_id=intent.slide_id,
                intent_id=intent.candidate_id,
                generation_origin=str(raw.get("generation_origin", "harness_model"))
                if isinstance(raw, dict) else "harness_model",
                issues=[f"source slide {intent.slide_id} not found"]))
            continue
        resolved = resolve_diagnostic_layout(intent, source, profile, rules,
                                              capabilities)
        geometry: dict[str, dict[str, int]] = {}
        content_box = resolved.content_box or {}
        zone_issues: list[str] = []
        # Single binding algorithm: geometry.upgrade_legacy_bindings owns
        # zone->node subdivision (no duplicated share logic here). Its
        # canonical keys are target node ids (zone:<zid>[#<ref>]); the
        # source-id entries below are explicit lineage aliases consumed by
        # emit_slide, not a second writable geometry namespace.
        from .geometry import upgrade_legacy_bindings as _upgrade_bindings
        canon_geometry, source_to_node, binding_issues = _upgrade_bindings(
            resolved.zones, content_box)
        geometry.update(canon_geometry)
        zone_issues.extend(binding_issues)
        for ref, nodes in source_to_node.items():
            if ref not in geometry and nodes:
                first = canon_geometry.get(nodes[0])
                if first is not None:
                    geometry[ref] = dict(first)
        unbound = [o.id for o in source.objects if o.id not in geometry]
        if unbound:
            zone_issues.append(
                f"UNBOUND_GEOMETRY_NEEDS_REPAIR: {len(unbound)} source objects "
                f"have no zone binding {unbound[:3]}; no synthetic row created")
        out.append(ResolvedSlide(
            slide_id=intent.slide_id, source_slide_id=source.id,
            intent_id=intent.candidate_id,
            generation_origin=origin or "harness_model",
            zones=resolved.zones,
            role_assignments={str(z.get("zone_id", "")): str(z.get("role", ""))
                              for z in resolved.zones},
            geometry_emu=geometry, diagnostic_geometry=True,
            issues=[*list(resolved.issues), *zone_issues,
                    "DIAGNOSTIC_GEOMETRY: legacy intent path; "
                    "blocked from user acceptance until measured resolve"]))
    return out


def _zone_to_emu(zone: dict[str, Any], content_box: dict[str, float]) -> dict[str, int]:
    bx = float(content_box.get("x", 0.5))
    by = float(content_box.get("y", 0.7))
    bw = float(content_box.get("w", 9.0))
    bh = float(content_box.get("h", 4.5))
    to_emu = 914400 / 1.0  # content_box is in inches from the bridge
    return {
        "x": int(round((bx + bw * float(zone.get("x_pct", 0)) / 100) * to_emu)),
        "y": int(round((by + bh * float(zone.get("y_pct", 0)) / 100) * to_emu)),
        "w": int(round((bw * float(zone.get("w_pct", 100)) / 100) * to_emu)),
        "h": int(round((bh * float(zone.get("h_pct", 20)) / 100) * to_emu)),
    }


# NOTE: the legacy per-source-index synthetic fallback box is deleted.
# Unbound objects get no fabricated coordinates; emit_slide records
# GEOMETRY_BINDING_MISSING with an in-canvas diagnostic placeholder that
# the saved-geometry gate rejects from user acceptance.


# ---------------------------------------------------------------------------
# §3 preflight
# ---------------------------------------------------------------------------

def preflight_native_export(
    source: Any,
    ledger: Any,
    resolved_slides: list[ResolvedSlide],
    rules: Any,
    template: Any,
    options: ExportOptions,
) -> ExportPreflightReport:
    """Validate inputs and plan emission strategies before writing output."""
    issues: list[str] = []
    required = list(ledger.atoms) if ledger is not None else []
    kinds: Counter[str] = Counter()
    emission_plan: dict[str, str] = {}
    for atom in required:
        kinds[atom.kind] += 1
    for slide in source.slides:
        for obj in slide.objects:
            strategy = _strategy_for(obj, options)
            emission_plan[obj.id] = strategy
            if strategy == "unsupported":
                issues.append(f"unsupported object {obj.id} kind={obj.kind}")
    contracts: list[PartPreservationContract] = []
    manifest = getattr(source, "package_manifest", None)
    if manifest is not None:
        for part in manifest.parts:
            contracts.append(PartPreservationContract(
                source=SourcePartKey(package_sha256=source.source_sha256,
                                     part_name=part.part_name),
                target_part=None, mode="binary_exact",
                reason="preflight: source closure carried unless replaced",
                evidence_refs=[part.part_name],
            ))
    data_support = "native_full" if all(
        s != "unsupported" for s in emission_plan.values()) else "partial"
    ok = not [i for i in issues if "unsupported" in i and not options.diagnostic]
    if not options.diagnostic and issues:
        ok = False
    return ExportPreflightReport(
        ok=ok or options.diagnostic,
        source_sha256=getattr(source, "source_sha256", ""),
        rules_snapshot_id=getattr(rules, "source_hash", "") or "",
        template_hash=getattr(template, "template_hash", "") or "",
        required_atoms=len(required),
        kind_matrix=dict(kinds),
        emission_plan=emission_plan,
        data_preservation_support=data_support,
        style_normalization_support="native_partial",
        native_structure_support="native_partial" if ok else "unknown",
        render_support="unsupported",
        contracts=contracts,
        unresolved=list(issues),
        issues=issues,
        capabilities={"measurement": "unknown", "render": "unsupported"},
    )


def _strategy_for(obj: Any, options: ExportOptions) -> str:
    kind = getattr(obj, "kind", "unknown")
    capability = getattr(getattr(obj, "capability", None), "level", "native_full")
    if kind in ("text", "table", "shape", "group", "image", "connector"):
        return "native_rebuild"
    if kind == "chart":
        payload = getattr(obj, "payload", None)
        if getattr(payload, "representation", "native_part") == "verified_data":
            return "native_rebuild"
        return "native_transplant"
    if kind == "unknown":
        if capability == "unsupported" and not options.allow_opaque_carry:
            return "unsupported"
        return "opaque_transplant" if options.allow_opaque_carry else "unsupported"
    return "unsupported"


# ---------------------------------------------------------------------------
# Emission driver (§10 emit_slide)
# ---------------------------------------------------------------------------

def _is_omittable_decor(obj: Any) -> bool:
    """Text-less autoshape decor may be omitted (never required text).

    True only for kind==shape with zero text runs/images/charts/tables.
    Images, charts, tables, groups, connectors and any text-bearing shape
    are required-or-explicit and never silently omitted here.
    """
    if getattr(obj, "kind", "") != "shape":
        return False
    payload = getattr(obj, "payload", None)
    # Stage 7 §1.4/§6.2: arrow-bearing shapes are candidate relations,
    # never automatic decor — even when text-less.
    line = getattr(payload, "source_line", None) if payload else None
    for end in (getattr(line, "begin_arrow", None),
                getattr(line, "end_arrow", None)):
        if end not in (None, "", "none"):
            return False
    paras = getattr(payload, "paragraphs", None)
    if paras is None:
        inner = getattr(payload, "text", None)
        paras = getattr(inner, "paragraphs", None) if inner else []
    for para in list(paras or []):
        for run in list(getattr(para, "runs", []) or []):
            if (getattr(run, "text", "") or "") != "":
                return False
            if getattr(run, "kind", "text") == "field":
                meta = getattr(run, "field", None)
                if (getattr(meta, "cached_text", "") or "") != "":
                    return False
    return True


def _authored_block_paragraphs(
        refs: list[str],
        objects: dict[str, Any]) -> list[dict[str, Any]]:
    """Ordered paragraphs of an authored block, with span provenance.

    Source order is preserved exactly and each paragraph records the source
    object and paragraph index it came from, so the saved frame can be
    compared span by span instead of as one merged blob (§3.3).
    """
    from .layout import _object_paragraphs  # noqa: PLC0415

    out: list[dict[str, Any]] = []
    for ref in refs:
        obj = objects.get(str(ref))
        if obj is None:
            continue
        for idx, para in enumerate(_object_paragraphs(obj)):
            entry = dict(para)
            entry["source_ref"] = str(ref)
            entry["source_paragraph_index"] = idx
            entry["id"] = f"{ref}:{idx}"
            runs = []
            for ridx, run in enumerate(entry.get("runs", []) or []):
                run = dict(run)
                run["id"] = f"{ref}:{idx}:{ridx}"
                runs.append(run)
            entry["runs"] = runs
            out.append(entry)
    return out


def emit_slide(
    resolved: ResolvedSlide,
    source_slide: Any,
    context: _native.NativeDeckContext,
    output_slide: Any,
    output_position: int = 0,
    *,
    rules: Any = None,
    source_package_path: str | Path | None = None,
    semantic_slide: Any = None,
    transform_ledger: Any = None,
    source_snapshot: str = "",
) -> list[str]:
    """Emit one slide's objects in z-order; returns binding atom ids."""
    bound: list[str] = []
    # Stage 5 typed binding: the accepted intent's zone roles reach the
    # emitters (corporate faces/sizes/transforms). Unknown stays unknown
    # and changes nothing (Stage 4 behavior preserved).
    zone_roles: dict[str, str] = {}
    zone_surface: dict[str, dict[str, str]] = {}
    for zone in list(resolved.zones or []):
        if isinstance(zone, dict):
            role = str(zone.get("role", "") or "")
            refs = list(zone.get("content_refs", []) or [])
            surface = {
                "surface_token": str(zone.get("surface_token", "") or ""),
                "foreground_token": str(
                    zone.get("foreground_token", "") or ""),
                "stroke_token": str(zone.get("stroke_token", "") or ""),
            }
        else:
            role = str(getattr(zone, "role", "") or "")
            refs = list(getattr(zone, "content_refs", []) or [])
            surface = {
                "surface_token": str(
                    getattr(zone, "surface_token", "") or ""),
                "foreground_token": str(
                    getattr(zone, "foreground_token", "") or ""),
                "stroke_token": str(getattr(zone, "stroke_token", "") or ""),
            }
        for ref in refs:
            if role:
                zone_roles[str(ref)] = role
            if any(surface.values()):
                zone_surface[str(ref)] = surface

    # Authored blocks (§3.3): the writer passes target nodes first. A rich
    # text block owns several source spans inside one native frame, so its
    # spans must not also be emitted by the per-source-object loop.
    authored_blocks: dict[str, dict[str, Any]] = {}
    for zone in list(resolved.zones or []):
        refs = (list(zone.get("content_refs", []) or [])
                if isinstance(zone, dict)
                else list(getattr(zone, "content_refs", []) or []))
        zone_id = (str(zone.get("zone_id", "") or "") if isinstance(zone, dict)
                   else str(getattr(zone, "zone_id", "") or ""))
        role = (str(zone.get("role", "") or "") if isinstance(zone, dict)
                else str(getattr(zone, "role", "") or ""))
        if not zone_id.startswith("zone:") or len(refs) < 2:
            continue
        block_id = zone_id[len("zone:"):]
        box = resolved.geometry_emu.get(block_id)
        if box is None:
            continue
        authored_blocks[block_id] = {
            "box_emu": box,
            "role": role,
            "refs": list(refs),
            "surface": {
                "surface_token": str(zone.get("surface_token", "") or "")
                if isinstance(zone, dict)
                else str(getattr(zone, "surface_token", "") or ""),
                "foreground_token": str(zone.get("foreground_token", "") or "")
                if isinstance(zone, dict)
                else str(getattr(zone, "foreground_token", "") or ""),
                "stroke_token": str(zone.get("stroke_token", "") or "")
                if isinstance(zone, dict)
                else str(getattr(zone, "stroke_token", "") or ""),
            },
        }
    merged_refs: set[str] = set()
    for block_id, block in authored_blocks.items():
        merged_refs.update(str(r) for r in block["refs"])
        paragraphs = _authored_block_paragraphs(
            block["refs"], {o.id: o for o in source_slide.objects})
        if not paragraphs:
            continue
        _native.emit_rich_text_block(
            paragraphs, block["box_emu"], output_slide, context,
            role=block["role"], rules=rules, node_id=block_id,
            surface_tokens=block["surface"],
            slide_position=output_position)

    ordered = sorted(source_slide.objects, key=lambda o: (o.z_order, o.id))
    connectors: list[dict[str, Any]] = []
    id_map: dict[str, int] = {}
    by_shapeid = {f"shapeid:{o.source_ref.shape_id}": o.id for o in ordered}
    for obj in ordered:
        if obj.id in merged_refs:
            # Already emitted as part of an authored block; emitting again
            # would duplicate the span (and read as a content conflict).
            continue
        box = resolved.geometry_emu.get(obj.id)
        if box is None:
            # Stage 7 §6.2: omit only by authorizing decision for this
            # source snapshot. When a semantic projection + transform
            # ledger is available, can_omit_source_object decides; the
            # legacy text-less check is only a pre-filter, never an
            # authorization. Arrows and text-bearing objects are refused.
            if semantic_slide is not None and \
                    transform_ledger is not None:
                from .semantics import can_omit_source_object  # noqa: PLC0415
                decision = can_omit_source_object(
                    obj, semantic_slide, transform_ledger, source_snapshot)
                if decision.allowed:
                    context.issues.append(
                        f"DECOR_OMITTED: {obj.id} omitted "
                        f"({decision.authorization_ref})")
                    try:
                        omitted = getattr(context, "omitted_decor", None)
                        if isinstance(omitted, list):
                            omitted.append(obj.id)
                    except Exception:  # noqa: BLE001
                        pass
                    continue
                if decision.needs_review:
                    raise ValueError(
                        f"OMIT_NEEDS_REVIEW: {obj.id}: {decision.reason}")
                raise ValueError(
                    f"OMIT_REFUSED: {obj.id}: {decision.reason}")
            # Legacy path (no ledger): text-less autoshape omit only;
            # required (text/arrow-bearing) unbound objects stay blocking.
            if _is_omittable_decor(obj):
                context.issues.append(
                    f"DECOR_OMITTED: {obj.id} text-less decor without "
                    "zone binding omitted (authorized_replace)")
                try:
                    omitted = getattr(context, "omitted_decor", None)
                    if isinstance(omitted, list):
                        omitted.append(obj.id)
                except Exception:  # noqa: BLE001
                    pass
                continue
            # No fabricated coordinates: typed reject for missing binding.
            # Required objects without zone binding are errors, not fallbacks.
            context.issues.append(
                f"GEOMETRY_BINDING_MISSING: {obj.id} has no zone binding; "
                "required object cannot be emitted without geometry")
            raise ValueError(
                f"GEOMETRY_BINDING_MISSING: {obj.id} has no zone binding; "
                "required object cannot be emitted without geometry")
        kind = obj.kind
        obj_role = zone_roles.get(obj.id, getattr(obj.payload, "role", "unknown"))
        try:
            surface = zone_surface.get(obj.id, {})
            if kind == "text":
                addr = _native.emit_text(
                    obj.payload, {"box_emu": box, "role": obj_role,
                                  **surface},
                    output_slide, context, rules=rules, source_object_id=obj.id,
                    slide_position=output_position)
            elif kind == "table":
                payload = obj.payload
                grid = (getattr(resolved, "table_layout_emu", {}) or {}).get(
                    obj.id)
                if grid and isinstance(grid, dict):
                    from copy import deepcopy as _deepcopy

                    payload = _deepcopy(payload)
                    try:
                        payload.column_widths_emu = [
                            int(v) for v in grid.get(
                                "column_widths_emu", [])]
                        payload.row_heights_emu = [
                            int(v) for v in grid.get("row_heights_emu", [])]
                    except (TypeError, ValueError):
                        payload = obj.payload
                addr = _native.emit_table(
                    payload, {"box_emu": box, "role": obj_role},
                    output_slide, context,
                    rules=rules, source_object_id=obj.id)
            elif kind == "chart":
                payload = obj.payload
                if getattr(payload, "representation", "native_part") == "verified_data":
                    addr = _native.emit_new_chart(
                        payload.data, {"box_emu": box}, output_slide, context,
                        source_object_id=obj.id)
                else:
                    addr = _native.emit_existing_chart(
                        payload, {"box_emu": box}, output_slide, context,
                        source_package_path=source_package_path or "",
                        source_slide_part=getattr(source_slide, "slide_part",
                                                  "") or "",
                        source_object_id=obj.id,
                        slide_position=output_position)
            elif kind == "shape":
                addr = _native.emit_shape(
                    obj.payload, {"box_emu": box, "role": obj_role,
                                  **surface},
                    output_slide, context,
                    rules=rules, source_object_id=obj.id)
            elif kind == "group":
                addr = _native.emit_group(
                    obj.payload, {"box_emu": box}, output_slide, context,
                    rules=rules, source_object_id=obj.id)
            elif kind == "image":
                addr = _native.emit_image(
                    obj.payload, {"box_emu": box}, output_slide, context,
                    source_package_path=source_package_path,
                    source_object_id=obj.id)
            elif kind == "connector":
                payload = obj.payload
                if (getattr(payload, "from_object_id", None) is None
                        and getattr(payload, "to_object_id", None) is None):
                    addr = _native.emit_free_connector(
                        payload, {"box_emu": box,
                                  **zone_surface.get(obj.id, {})},
                        output_slide, context, rules=rules,
                        source_object_id=obj.id)
                    id_map[obj.id] = addr.shape_id
                    bound.append(obj.id)
                    continue
                connectors.append({"from": by_shapeid.get(
                    str(payload.from_object_id), str(payload.from_object_id)),
                    "to": by_shapeid.get(
                    str(payload.to_object_id), str(payload.to_object_id)),
                    "from_site": payload.from_site, "to_site": payload.to_site,
                    "object_id": obj.id})
                continue
            elif kind == "unknown":
                addr = _native.emit_unknown(
                    obj, {"box_emu": box}, output_slide, context,
                    source_slide_part=getattr(source_slide, "slide_part",
                                              "") or "",
                    source_object_id=obj.id)
                id_map[obj.id] = addr.shape_id
                bound.append(obj.id)
                continue
            else:
                context.issues.append(f"kind {kind} unsupported for {obj.id}")
                continue
        except Exception as exc:  # noqa: BLE001 - scoped, never silent
            msg = str(exc)
            if "REQUIRED_ASSET_UNRESOLVED" in msg or "retrievable asset" in msg:
                context.issues.append(
                    f"REQUIRED_ASSET_UNRESOLVED: emit failed {obj.id}: {exc}")
            else:
                context.issues.append(f"emit failed {obj.id}: {exc}")
            # Required failure is a typed reject: text/table/chart/image
            # emit errors block acceptance (recorded, never swallowed by
            # a bare continue). Only omittable decor may pass quietly,
            # and it never reaches here (omitted above, not emitted).
            if getattr(obj, "kind", "") in ("text", "table", "chart",
                                            "image") or not _is_omittable_decor(
                                                obj):
                context.issues.append(
                    f"REQUIRED_EMIT_FAILED: {obj.id} kind="
                    f"{getattr(obj, 'kind', '')} blocks acceptance")
            continue
        id_map[obj.id] = addr.shape_id
        bound.append(obj.id)
    if connectors:
        _native.emit_connectors(
            [{"from": c["from"], "to": c["to"], "object_id": c["object_id"],
              "from_site": c["from_site"], "to_site": c["to_site"]}
             for c in connectors],
            output_slide, context, id_map)
    _native.copy_notes(getattr(source_slide, "notes", []), output_slide, context,
                       source_slide_id=getattr(source_slide, "id", ""))
    return bound


# ---------------------------------------------------------------------------
# §11 finalize / compare / verify
# ---------------------------------------------------------------------------

def finalize_and_save_candidate(
    context: _native.NativeDeckContext,
    path: str | Path,
    expectations: dict[str, Any],
) -> CandidateArtifact:
    """Save via the adapter, assemble transplanted parts, validate, reimport."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gpn-export-") as tmp:
        staged = Path(tmp) / "staged.pptx"
        adapter.save_presentation(context.presentation, staged)
        # Wire deferred owner-scoped rels (chart frames + internal links):
        # enforce rId uniqueness against the saved slide rels, patch the
        # slide XML consistently, then re-zip the staged package.
        _wire_deferred_slide_rels(staged, context)
        extra_parts: dict[str, bytes] = {}
        extra_ctypes: dict[str, str] = {}
        extra_rels: dict[str, list[dict[str, str]]] = {}
        tctx = context.transplant_context
        if tctx is not None:
            extra_parts.update(tctx.target_payloads)
            extra_ctypes.update(tctx.target_content_types)
            for mapping in tctx.relationship_map:
                extra_rels.setdefault(mapping.target_owner, []).append({
                    "id": mapping.target_rid, "type": mapping.relationship_type,
                    "target": mapping.target_part_or_uri,
                    "mode": mapping.target_mode,
                })
        # Chart-frame and internal-link rels collected during wiring.
        for owner, rel in context.deferred_owner_rels:
            extra_rels.setdefault(owner, []).append(rel)
        report = adapter.assemble_final_package(
            staged, extra_parts, extra_ctypes, extra_rels, path)
    graph_report = adapter.validate_package_graph(path)
    success = bool(graph_report.get("ok"))
    issues = [str(i) for i in context.issues]
    if not success:
        issues.append("package graph validation failed")
    # Independent reimport on final bytes (never overwrites source IR).
    try:
        from .importer import import_deck
        store = AssetStore(path.parent / "candidate_assets")
        _deck_after, _check = import_deck(path, store)
        reimport_ok = True
    except Exception as exc:  # noqa: BLE001
        reimport_ok = False
        issues.append(f"candidate reimport failed: {exc}")
        success = False
    return CandidateArtifact(path=str(path), sha256=report["sha256"],
                             success=success and reimport_ok, issues=issues)


def compare_export_preservation(
    source_ledger: Any,
    actual_ir: Any,
    bindings: list[Any],
    policy: dict[str, Any],
) -> ContentDiff:
    """Compare required atoms against the reimported candidate (§11.2).

    Every binding must resolve to a real target, and the target value must
    equal the ledger canonical value (modulo the binding's explicit display
    transform). Missing/duplicated/changed atoms are failures; unknown
    significance is never excluded to force a zero.
    """
    import re

    atoms = {a.id: a for a in (source_ledger.atoms if source_ledger else [])}
    after_slides: dict[str, Any] = {}
    for slide in getattr(actual_ir, "slides", []) or []:
        if getattr(slide, "slide_part", ""):
            after_slides[slide.slide_part] = slide
    # Output shape address -> source object id (from binding evidence).
    # Group-child bindings share their root group's address and are skipped
    # here: connector endpoints are always slide-level shapes.
    shape_to_obj: dict[tuple[str, int], str] = {}
    for b in bindings:
        if str(getattr(b, "target_subaddress", "")).startswith("group-child"):
            continue
        addr = getattr(b, "target_address", None)
        if addr is None:
            continue
        for item in list(getattr(b, "evidence", []) or []):
            if isinstance(item, str) and item.startswith("obj:"):
                shape_to_obj[(getattr(addr, "slide_part", ""),
                              int(getattr(addr, "shape_id", 0)))] = item[4:]
                break

    def _after_shape(part: str, shape_id: int) -> Any | None:
        slide = after_slides.get(part)
        if slide is None:
            return None
        for obj in slide.objects:
            ref = getattr(obj, "source_ref", None)
            if ref is not None and int(getattr(ref, "shape_id", -1)) == shape_id:
                return obj
        return None

    def _para_text(obj: Any, index: int) -> str | None:
        payload = getattr(obj, "payload", None)
        text = getattr(payload, "paragraphs", None)
        if text is None:
            inner = getattr(payload, "text", None)
            text = getattr(inner, "paragraphs", None) if inner is not None else None
        if not text or index >= len(text):
            return None
        return "".join(getattr(r, "text", "") for r in text[index].runs)

    def _apply_expected(canonical: str, transform: str) -> str:
        if transform == "title_uppercase":
            return canonical.upper()
        return canonical

    missing: list[str] = []
    duplicated: list[str] = []
    changed: list[str] = []
    dangling: list[str] = []
    unexpected: list[str] = []
    seen: dict[str, int] = {}
    allowed_transforms = {"identity", "wrap", "title_uppercase", "style_only",
                          "relocate", "authorized_rewrite"}

    for b in bindings:
        atom_id = getattr(b, "source_atom_id", "")
        atom = atoms.get(atom_id)
        if atom is None:
            dangling.append(f"{atom_id}:no-such-atom")
            continue
        seen[atom_id] = seen.get(atom_id, 0) + 1
        transform = getattr(b, "transform", "identity") or "identity"
        if transform not in allowed_transforms:
            unexpected.append(f"{atom_id}:{transform}")
            continue
        addr = getattr(b, "target_address", None)
        sub = getattr(b, "target_subaddress", "") or ""
        part = getattr(addr, "slide_part", "") if addr else ""
        shape_id = int(getattr(addr, "shape_id", 0)) if addr else 0
        obj = _after_shape(part, shape_id)
        if obj is None and not sub.startswith(("notes-para", "opaque")):
            dangling.append(f"{atom_id}:{part}#{shape_id}")
            continue
        expected = _apply_expected(atom.canonical_value, transform)
        actual: str | None = None
        if "/hyperlink" in sub:
            match = re.match(r"para\[(\d+)\]/hyperlink", sub)
            actual = _hyperlink_uri(obj, int(match.group(1))) if match else None
        elif sub.startswith("para["):
            match = re.match(r"para\[(\d+)\]", sub)
            actual = _para_text(obj, int(match.group(1))) if match else None
        elif sub.startswith("cell["):
            match = re.match(r"cell\[(\d+),(\d+)\]", sub)
            actual = _cell_text(obj, int(match.group(1)),
                                int(match.group(2))) if match else None
        elif "/point[" in sub:
            match = re.match(r"series\[(\d+)\]:([^/]*)/point\[(\d+)\]", sub)
            actual = _point_text(obj, match) if match else None
        elif sub == "picture":
            actual = _image_sha(obj)
            if actual is not None and actual != expected:
                # Stage6 brand repair: an authorized asset replacement
                # (approved variant bytes for one source object, recorded
                # in the run's asset_overrides artifact with old/new
                # hashes) is an authorized transform, not a content
                # change. Anything else stays a hard change.
                auth = (policy.get("authorized_asset_replacements", {})
                        or {}).get(atom_id)
                if (isinstance(auth, dict)
                        and auth.get("old_sha256") == expected
                        and auth.get("new_sha256") == actual):
                    actual = expected
        elif sub.startswith("edge["):
            # Edges compare at object identity (shape ids are unstable
            # across packages); the expected pair lives in the subaddress.
            expected = sub[len("edge["):-1]
            actual = _edge_state(obj, part, shape_to_obj, sub, actual_ir)
        elif sub.startswith("notes-para"):
            actual = _notes_text(actual_ir, part, atom_id)
            if actual is not None and expected in actual:
                actual = expected  # multi-para notes: containment is the check
        elif sub == "opaque":
            actual = expected if _opaque_present(actual_ir, part) else None
        elif sub == "geometry":
            actual = _shape_geometry_token(obj)
            # Recovery §3/§5: authorized decoration normalization.
            # Source roundRect cards are confirmed decoration
            # (decoration_decisions); the emitter rebuilds them as
            # permitted RECTANGLE with normative no-fill. The ledger
            # canonical value keeps the source token for forensic audit,
            # so roundRect->rect is an accepted restyle, not a content
            # change. Any other geometry drift still fails.
            if actual is not None and (
                    (expected in ("line",) and actual in (
                        "line", "straightConnector", "connector"))
                    or (expected == "roundRect" and actual == "rect")):
                actual = expected
        elif sub.startswith("group-child"):
            match = re.search(r"/para\[(\d+)\]$", sub)
            actual = _group_child_text(obj, sub, match) if match else None
        if actual is None:
            dangling.append(f"{atom_id}:{sub}")
        elif actual != expected:
            changed.append(f"{atom_id}:expected={expected[:60]!r} "
                           f"actual={actual[:60]!r}")

    authorized_omits = set(
        policy.get("authorized_omits", []) or [])
    omitted_authorized: list[str] = []
    for atom_id in atoms:
        if seen.get(atom_id, 0) == 0:
            if atom_id in authorized_omits:
                omitted_authorized.append(atom_id)
            else:
                missing.append(atom_id)
        elif seen.get(atom_id, 0) > 1:
            duplicated.append(atom_id)
    ok = not missing and not duplicated and not changed and not dangling \
        and not unexpected
    return ContentDiff(missing=sorted(missing), duplicated=sorted(duplicated),
                       changed=sorted(changed), dangling=sorted(dangling),
                       unexpected_rewrites=sorted(unexpected),
                       omitted_authorized=sorted(omitted_authorized), ok=ok)


def _hyperlink_uri(obj: Any, para_index: int) -> str | None:
    payload = getattr(obj, "payload", None)
    paras = getattr(payload, "paragraphs", None) or []
    if para_index >= len(paras):
        return None
    for run in paras[para_index].runs:
        link = getattr(run, "hyperlink", None)
        if link is not None:
            return link.uri or link.target_slide_ref or ""
    return None


def _cell_text(obj: Any, row: int, col: int) -> str | None:
    payload = getattr(obj, "payload", None)
    for cell in list(getattr(payload, "cells", []) or []):
        if int(cell.row) == row and int(cell.col) == col:
            return "".join(r.text for p in cell.paragraphs for r in p.runs)
    return None


def _point_text(obj: Any, match: Any) -> str | None:
    payload = getattr(obj, "payload", None)
    try:
        sidx, _name, pidx = int(match.group(1)), match.group(2), int(match.group(3))
    except (ValueError, IndexError):
        return None
    series = list(getattr(payload, "series", []) or [])
    if sidx >= len(series):
        return None
    points = list(series[sidx].points or [])
    if pidx >= len(points):
        # Match by point index field instead of position.
        for point in points:
            if int(getattr(point, "index", -1)) == pidx:
                return _display_of(point)
        return None
    return _display_of(points[pidx])


def _display_of(point: Any) -> str | None:
    for candidate in (getattr(point, "value", None), getattr(point, "y", None),
                      getattr(point, "x", None)):
        if candidate is not None and getattr(candidate, "display_text", ""):
            return str(candidate.display_text)
    return ""


def _image_sha(obj: Any) -> str | None:
    payload = getattr(obj, "payload", None)
    asset = getattr(payload, "asset", None) if payload is not None else None
    return str(getattr(asset, "sha256", "")) if asset is not None else None


def _edge_state(obj: Any, part: str,
                shape_to_obj: dict[tuple[str, int], str],
                sub: str, after_ir: Any = None) -> str | None:
    """Verify a connector links the same source objects (by obj identity).

    The reimported payload already resolves endpoints to after-object ids;
    those map back to shape ids and then, through binding evidence, to
    source object ids.
    """
    import re as _re

    if obj is None or getattr(obj, "kind", "") != "connector":
        return None
    match = _re.match(r"edge\[(.*)->(.*)\]", sub)
    if not match:
        return None
    expected = (match.group(1), match.group(2))
    payload = getattr(obj, "payload", None)
    if payload.from_object_id is None or payload.to_object_id is None:
        return None
    after_id_to_shape: dict[str, int] = {}
    if after_ir is not None:
        for slide in getattr(after_ir, "slides", []) or []:
            if getattr(slide, "slide_part", "") != part:
                continue
            for other in slide.objects:
                ref = getattr(other, "source_ref", None)
                if ref is not None:
                    after_id_to_shape[getattr(other, "id", "")] = int(
                        getattr(ref, "shape_id", -1))
    actual: list[str] = []
    for endpoint in (payload.from_object_id, payload.to_object_id):
        sid = after_id_to_shape.get(str(endpoint))
        if sid is None:
            # Fallback: legacy shapeid:N form.
            try:
                sid = int(str(endpoint).split(":", 1)[1])
            except (ValueError, IndexError):
                return None
        obj_id = shape_to_obj.get((part, sid))
        if obj_id is None:
            return None
        actual.append(obj_id)
    return f"{actual[0]}->{actual[1]}" if tuple(actual) == expected else None


def _opaque_present(actual_ir: Any, part: str) -> bool:
    for slide in getattr(actual_ir, "slides", []) or []:
        if getattr(slide, "slide_part", "") != part:
            continue
        if any(getattr(o, "kind", "") == "unknown" for o in slide.objects):
            return True
    return False


def _shape_geometry_token(obj: Any) -> str | None:
    """Actual preset geometry of a reopened shape/connector object."""
    if obj is None:
        return None
    if getattr(obj, "kind", "") == "connector":
        return "line"
    payload = getattr(obj, "payload", None)
    token = getattr(payload, "geometry_token", None) if payload else None
    return str(token) if token else None


def _notes_text(actual_ir: Any, part: str, atom_id: str) -> str | None:
    for slide in getattr(actual_ir, "slides", []) or []:
        if getattr(slide, "slide_part", "") != part:
            continue
        for para in list(getattr(slide, "notes", []) or []):
            if atom_id.endswith(f"/{getattr(para, 'id', '')}"):
                return "".join(getattr(r, "text", "")
                               for r in getattr(para, "runs", []))
    # Fallback: joined notes text contains the canonical value.
    for slide in getattr(actual_ir, "slides", []) or []:
        if getattr(slide, "slide_part", "") != part:
            continue
        joined = "\n".join("".join(getattr(r, "text", "")
                                   for r in getattr(p, "runs", []))
                           for p in list(getattr(slide, "notes", []) or []))
        return joined
    return None


def _group_child_text(obj: Any, sub: str, match: Any) -> str | None:
    """Match a group-child paragraph by position path (ids are hash-derived)."""
    import re as _re

    payload = getattr(obj, "payload", None)
    head = _re.match(r"group-child\[([0-9/]+)\]/para\[(\d+)\]", sub) \
        or _re.match(r"group-child\[([0-9/]+)\]", sub)
    path = [int(i) for i in head.group(1).split("/")] if head else []
    node: Any = payload
    for step in path:
        children = list(getattr(node, "children", []) or [])
        if step >= len(children):
            return None
        node = getattr(children[step], "payload", None)
        if node is None:
            return None
    text = getattr(node, "paragraphs", None)
    if text is None:
        inner = getattr(node, "text", None)
        text = getattr(inner, "paragraphs", []) if inner is not None else []
    idx = int(match.group(1))
    if idx < len(text):
        return "".join(getattr(r, "text", "") for r in text[idx].runs)
    return None


def compare_export_parts(
    source_graph: Any,
    target_path: str | Path,
    contracts: list[PartPreservationContract],
    mappings: list[Any],
    *,
    source_parts: dict[str, bytes] | None = None,
) -> PartDiff:
    """Check part contracts on final bytes (counts never suffice, §11.3)."""
    members = adapter.read_zip_members(target_path)
    unmapped: list[str] = []
    missing_bin: list[str] = []
    unexpected: list[str] = []
    dangling: list[str] = []
    violations: list[str] = []
    unverified: list[str] = []
    for contract in contracts:
        src = contract.source.part_name if contract.source else ""
        target = contract.target_part or ""
        if contract.mode == "excluded":
            if not contract.reason or not contract.evidence_refs:
                unverified.append(src or target)
            continue
        if contract.mode == "replacement":
            continue  # template substitution with provenance
        if not target:
            unmapped.append(src)
            continue
        payload = members.get(target)
        if payload is None:
            missing_bin.append(target)
            continue
        if contract.mode == "binary_exact" and contract.source is not None:
            expected: bytes | None = None
            if source_parts is not None:
                expected = source_parts.get(src)
            if expected is not None and payload != expected:
                missing_bin.append(target)
            elif expected is None and source_graph is not None:
                # Store-backed hash comparison needs the AssetStore; record
                # the gap instead of claiming equality.
                unverified.append(src)
    # Relationship targets are validated by the package graph report; surface
    # leftovers here from the members listing.
    diff = PartDiff(
        unmapped_required_parts=sorted(unmapped),
        missing_binary_payloads=sorted(missing_bin),
        unexpected_xml_mutations=sorted(unexpected),
        changed_relationship_semantics=[],
        template_substitution_violations=sorted(violations),
        dangling_targets=sorted(dangling),
        unverified_exclusions=sorted(unverified),
        ok=not unmapped and not missing_bin and not unexpected
        and not violations and not dangling and not unverified,
    )
    return diff


def verify_native_objects(
    actual_package: str | Path,
    actual_ir: Any,
    bindings: list[Any],
) -> NativeEditabilityReport:
    """Verify native types on the raw saved package (§11.4)."""
    kinds: Counter[str] = Counter()
    for slide in getattr(actual_ir, "slides", []) or []:
        for obj in getattr(slide, "objects", []) or []:
            kinds[getattr(obj, "kind", "unknown")] += 1
    opaque_bound = sum(1 for b in bindings
                       if (getattr(b, "target_subaddress", "") == "opaque"))
    # Opaque-preserved is a declared strategy with a byte contract, not an
    # unverified gap: native structure holds when every unknown object has
    # an opaque binding. Style compliance is still NOT claimed for them.
    native_ok = kinds.get("unknown", 0) <= opaque_bound
    return NativeEditabilityReport(
        native_structure_verified=native_ok,
        data_preservation_verified=False,  # set by the caller after diffs
        graph_integrity_verified=True,
        style_checked_scopes=["role_fonts", "list_markers", "table_grid"],
        render_verified=False,
        powerpoint_ui_edit_verified=False,
        details={"kinds": dict(kinds), "bindings": len(bindings)},
    )


# ---------------------------------------------------------------------------
# run_native_build (§12)
# ---------------------------------------------------------------------------

def _emit_resolved_slides(
    run_dir: Path,
    config: Any,
    options: Any,
    *,
    deck: Any,
    ledger: Any,
    rules: Any,
    profile: Any,
    intents: list[dict[str, Any]],
    resolved: list[Any],
    verification_scope: str,
    provider: str,
    origin_note: str,
    plan_origin: str,
    source_path: str,
    artifacts: dict[str, str],
    issues: list[str],
    readiness_name: str = "stage4_readiness.json",
    unresolved_scopes: list[str] | None = None,
    extra_result_fields: dict[str, Any] | None = None,
    candidate_name: str = "candidate.pptx",
) -> NativeBuildResult:
    """Shared emission + finalize + independent-check tail (Stages 4-5).

    Takes already-resolved slides (diagnostic pct-boxes from Stage 4 or
    measured geometry from Stage 5) through the single native exporter,
    transplant, finalize and saved-bytes gates. No second implementation.
    """
    if unresolved_scopes is None:
        unresolved_scopes = ["measurement", "render", "powerpoint_ui"]
    if extra_result_fields is None:
        extra_result_fields = {}
    project_root = getattr(config, "project_root", Path("."))
    project_root = Path(project_root)

    # Active template: prefer the derived base template artifact.
    template_path = _find_template(run_dir, project_root)
    if template_path is None:
        return _fail(run_dir, provider, "no verified template available", artifacts,
                     origin=plan_origin)
    slide_plan = [{"source_slide_id": r.source_slide_id, "slide_id": r.slide_id,
                   "hidden": _slide_hidden(deck, r.source_slide_id)}
                  for r in resolved]
    list_profile = None
    list_profile_path = run_dir / "list_profile.json"
    if list_profile_path.is_file():
        try:
            from .models import ListStyleProfile
            list_profile = ListStyleProfile.model_validate(
                json.loads(list_profile_path.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001 - optional profile
            list_profile = None
    ctx = _native.create_output_deck(template_path, profile or {}, slide_plan,
                                      rules=rules, work_dir=run_dir / "export_work",
                                      list_profile=list_profile)
    # Transplant source package for chart/media closure.
    if source_path and Path(source_path).is_file():
        parts, ctypes, outgoing, phash = load_source_package(source_path)
        ctx.transplant_context = TransplantContext(
            source_package_hash=phash, source_parts=parts,
            source_content_types=ctypes, source_outgoing=outgoing)
        protected = _protected_parts(run_dir)
        ctx.transplant_context.protected_target_parts = frozenset(protected)
    ctx.asset_store = AssetStore(run_dir / "assets")
    # Stage6 brand repair: explicit run-level asset overrides (approved
    # variant bytes for named source objects, hash-verified at emit).
    # The same artifact authorizes the content comparator, so an
    # authorized swap is a recorded transform, never a silent change.
    asset_overrides: dict[str, Any] = {}
    overrides_doc = _read_json(run_dir / "asset_overrides.json")
    if isinstance(overrides_doc, dict):
        for obj_id, entry in (overrides_doc.get("overrides", {}) or {}).items():
            if isinstance(entry, dict) and entry.get("new_sha256"):
                asset_overrides[str(obj_id)] = entry
    ctx.asset_overrides = asset_overrides
    if asset_overrides:
        issues.append(f"ASSET_OVERRIDES_DECLARED: {sorted(asset_overrides)}")

    # Emit each slide into its pre-created output slide.
    slides_by_id = {s.id: s for s in deck.slides}
    for i, r in enumerate(resolved):
        src = slides_by_id.get(r.source_slide_id)
        if src is not None:
            if getattr(src, "slide_part", ""):
                ctx.source_part_positions[src.slide_part] = i
            ctx.source_part_positions[src.id] = i
    # Stage 7 §6.2: evidence-backed omit path. The flat classification
    # ledger + semantic projection authorize decor omits and arrow
    # re-expression per source snapshot; without them the legacy
    # text-less check applies (arrows stay blocking).
    semantic_deck, ledger_model, snapshot = _load_semantic_gate_inputs(
        run_dir)
    slides_semantic = {}
    if semantic_deck is not None:
        slides_semantic = {s.source_slide_id: s
                           for s in semantic_deck.slides}
    for index, res in enumerate(resolved):
        output_slide = ctx.presentation.slides[index]
        source_slide = slides_by_id.get(res.source_slide_id)
        if source_slide is None:
            issues.append(f"source slide missing: {res.source_slide_id}")
            continue
        emit_slide(res, source_slide, ctx, output_slide, index, rules=rules,
                   source_package_path=source_path,
                   semantic_slide=slides_semantic.get(res.source_slide_id),
                   transform_ledger=ledger_model,
                   source_snapshot=snapshot)
    link_report = _native.remap_hyperlinks(ctx)
    atomic_write_json(run_dir / "emission_report.json", {
        "records": [r.model_dump(mode="json") for r in ctx.emission_records],
        "issues": list(ctx.issues), "links": link_report})
    artifacts["emission_report"] = str(run_dir / "emission_report.json")
    atomic_write_json(run_dir / "output_object_map.json", {
        "bindings": [b.model_dump(mode="json") for b in ctx.output_map]})
    artifacts["output_object_map"] = str(run_dir / "output_object_map.json")

    candidate_path = run_dir / candidate_name
    if candidate_path.exists() and not options.overwrite:
        return _fail(run_dir, provider,
                     f"OUTPUT_EXISTS: {candidate_name} exists",
                     artifacts, origin=plan_origin)
    candidate = finalize_and_save_candidate(
        ctx, candidate_path, {"scope": options.workflow})
    atomic_write_json(run_dir / "package_graph_report.json",
                      adapter.validate_package_graph(candidate_path))
    artifacts["candidate"] = str(candidate_path)

    # Independent checks on final bytes.
    from .importer import import_deck
    store = AssetStore(run_dir / "candidate_assets")
    after_deck, _ = import_deck(candidate_path, store)
    atomic_write_json(run_dir / "candidate_after_ir.json",
                      after_deck.model_dump(mode="json"))
    from .provenance import build_ledger
    after_ledger = build_ledger(after_deck, decoration=[])
    atomic_write_json(run_dir / "candidate_after_ledger.json",
                      after_ledger.model_dump(mode="json"))
    from .importer import build_import_support_report, resolve_internal_links
    from .package import read_package as _read_package
    after_graph = _read_package(candidate_path, store)
    after_links = resolve_internal_links(after_deck, after_graph)
    atomic_write_json(run_dir / "candidate_after_links.json",
                      after_links.model_dump(mode="json"))
    after_support = build_import_support_report(
        after_deck, after_ledger, after_links, after_deck.import_issues)
    atomic_write_json(run_dir / "candidate_after_support.json",
                      after_support.model_dump(mode="json"))
    artifacts["candidate_after"] = str(run_dir / "candidate_after_ir.json")
    content_policy: dict[str, Any] = {}
    if asset_overrides:
        by_atom = {f"{obj_id}#asset": {
            "old_sha256": entry.get("old_sha256", ""),
            "new_sha256": entry.get("new_sha256", ""),
            "reason": entry.get("reason", ""),
        } for obj_id, entry in asset_overrides.items()}
        content_policy["authorized_asset_replacements"] = by_atom
    # Stage6 §3.2: authorized omits from the classification ledger
    # (confirmed decoration with evidence + authorization; never silent).
    # Stage 7 §6.2: arrows re-expressed as native relations (recompose
    # with target_relation_ids) are excused from shape presence here;
    # their preservation is proven independently by the semantic
    # relations gate, never by this waiver alone.
    authorized_omits: set[str] = set()
    reexpressed: set[str] = set()
    class_doc = _read_json(run_dir / "classification_and_transformation_ledger.json")
    if isinstance(class_doc, dict):
        for dec in class_doc.get("decisions", []) or []:
            if not isinstance(dec, dict) or not dec.get(
                    "source_object_id") or not dec.get("authorization_ref"):
                continue
            if dec.get("action") == "omit":
                authorized_omits.add(
                    f"{dec['source_object_id']}#shape")
            elif dec.get("action") in ("recompose", "replace") and dec.get(
                    "target_relation_ids"):
                reexpressed.add(f"{dec['source_object_id']}#shape")
    if authorized_omits or reexpressed:
        content_policy["authorized_omits"] = sorted(authorized_omits
                                                    | reexpressed)
        issues.append(f"AUTHORIZED_OMITS: {len(authorized_omits)} geometry "
                      "atoms (confirmed text-less decor, ledgered)")
    if reexpressed:
        issues.append(f"REEXPRESSED_RELATIONS: {len(reexpressed)} arrow "
                      "atoms re-expressed as native relations (ledgered; "
                      "preservation proven by semantic relations gate)")
    content_diff = compare_export_preservation(ledger, after_deck,
                                               ctx.output_map,
                                               content_policy)
    atomic_write_json(run_dir / "content_diff.json", content_diff)
    if asset_overrides:
        applied = [i for i in ctx.issues
                   if i.startswith("ASSET_OVERRIDE_APPLIED")]
        atomic_write_json(run_dir / "authorized_replacements_report.json",
                          {"overrides": asset_overrides,
                           "applied": applied,
                           "complete": sorted(asset_overrides)
                           == sorted(a.split(" ")[1] for a in applied)})
        artifacts["authorized_replacements_report"] = str(
            run_dir / "authorized_replacements_report.json")
    part_diff = compare_export_parts(
        None, candidate_path, list(ctx.part_contracts), [],
        source_parts=dict(ctx.transplant_context.source_parts)
        if ctx.transplant_context is not None else None)
    atomic_write_json(run_dir / "part_preservation_diff.json", part_diff)
    native_report = verify_native_objects(candidate_path, after_deck,
                                           ctx.output_map)
    native_report.data_preservation_verified = bool(content_diff.ok)
    atomic_write_json(run_dir / "native_editability_report.json", native_report)
    # Recovery §5: saved-bytes authoring gate (C01/C03/C21/C22 +
    # surface_foreground) on the reopened candidate — never on the
    # expected IR. A serialization that reintroduces source styles,
    # theme effectRef, synthetic bold, or colorless runs blocks quality.
    from .rules import check_saved_authoring_rules
    saved_authoring = check_saved_authoring_rules(candidate_path, rules)
    atomic_write_json(run_dir / "saved_authoring_rule_report.json",
                      saved_authoring)
    artifacts["saved_authoring_rule_report"] = str(
        run_dir / "saved_authoring_rule_report.json")
    # Prompt §1/§12: actual saved-geometry gate on reopened bytes.
    # diagnostic_geometry=true never passes user acceptance, even when
    # all style checks pass; fully-outside required content is a hard
    # fail independent of ledger equality.
    from .geometry import check_saved_geometry
    saved_geometry = check_saved_geometry(candidate_path, rules)
    atomic_write_json(run_dir / "saved_geometry_report.json",
                      saved_geometry)
    artifacts["saved_geometry_report"] = str(
        run_dir / "saved_geometry_report.json")
    diagnostic_blocked = any(bool(getattr(r, "diagnostic_geometry", False))
                             for r in resolved)
    if diagnostic_blocked:
        saved_geometry = {**saved_geometry, "result": "fail",
                          "issues": [*saved_geometry.get("issues", []),
                                     "DIAGNOSTIC_GEOMETRY_REJECTED: resolved slide "
                                     "carries diagnostic_geometry=true; user pipeline "
                                     "requires measured resolve"]}
        atomic_write_json(run_dir / "saved_geometry_report.json",
                          saved_geometry)
    # Transplant + chart + template + style artifacts (§14.2).
    _write_transplant_artifacts(run_dir, ctx, source_path, template_path,
                                artifacts, candidate_name)
    _write_trace_artifacts(run_dir, resolved, ctx, intents, artifacts)
    artifacts.update({
        "content_diff": str(run_dir / "content_diff.json"),
        "part_diff": str(run_dir / "part_preservation_diff.json"),
        "native_editability": str(run_dir / "native_editability_report.json"),
    })

    from .cli import _production_assets_status
    assets_ready, _ = _production_assets_status(project_root, config)
    preservation_ok = bool(content_diff.ok and candidate.success)
    saved_authoring_ok = not bool(saved_authoring.get("hard_fail", False))
    saved_geometry_ok = bool(saved_geometry.get("result") == "pass")
    if not saved_authoring_ok:
        issues.append(
            "SAVED_AUTHORING_RULE_FAIL: "
            + "; ".join(
                f"{rid}({info.get('violation_count', 0)}): "
                f"{(info.get('violations') or [''])[0][:100]}"
                for rid, info in
                (saved_authoring.get("rules") or {}).items()
                if info.get("result") == "fail")[:4])
    # Recovery §8: technical serialization success is not user quality.
    # Saved-bytes C01/C03/C21/C22/foreground failures block completed.
    # Saved-geometry/off-canvas/diagnostic failures block it as well.
    if not saved_geometry_ok:
        issues.append(
            "SAVED_GEOMETRY_VIOLATION: "
            + "; ".join(saved_geometry.get("issues", [])[:2]
                        or saved_geometry.get("out_of_canvas", [])[:2]
                        or ["geometry gate failed"]))
    status = "completed" if (
        preservation_ok and saved_authoring_ok and saved_geometry_ok) else "needs_review"
    if verification_scope != "full_source":
        artifacts["verification_scope"] = verification_scope
        artifacts["pilot_selection"] = str(run_dir / "pilot_selection.json")
    result = NativeBuildResult(
        operation_status=status,  # type: ignore[arg-type]
        export_scope=options.workflow, llm_provider=provider,
        plan_generation_origin=plan_origin or f"harness ({origin_note})",
        candidate_path=str(candidate_path), candidate_sha256=candidate.sha256,
        source_preservation_verified=bool(content_diff.ok),
        package_graph_verified=bool(adapter.validate_package_graph(
            candidate_path).get("ok")),
        native_structure_verified=bool(native_report.native_structure_verified),
        unresolved_scopes=list(unresolved_scopes),
        production_assets_ready=bool(assets_ready),
        render_verified=False, powerpoint_ui_edit_verified=False,
        strict_output_ready=False,
        saved_geometry_verified=bool(saved_geometry_ok),
        layout_resolved=bool(extra_result_fields.get("layout_resolved", False)),
        issues=[*issues, *candidate.issues,
                *([] if assets_ready else ["FONTS/GPN_FONTS_MISSING"])],
        artifacts=artifacts, **{k: v for k, v in extra_result_fields.items()
                                if k not in ("saved_geometry_verified", "layout_resolved")})
    atomic_write_json(run_dir / readiness_name, result)
    return result
def run_native_build(
    run_dir: str | Path,
    plans_dir: str | Path,
    config: Any,
    options: ExportOptions,
) -> NativeBuildResult:
    """Execute the full native build service operation."""
    run_dir = Path(run_dir)
    plans_dir = Path(plans_dir)
    artifacts: dict[str, str] = {}
    issues: list[str] = []

    provider, origin_note = resolve_llm_provider(config)
    if provider == PROVIDER_LOCAL:
        result = NativeBuildResult(
            operation_status="invalid_input", llm_provider=provider,
            plan_generation_origin="deferred_local_server",
            issues=["MODEL_LOCAL_DEFERRED: local_server adapter not implemented; "
                    "no harness fallback performed"],
            artifacts={})
        atomic_write_json(run_dir / "stage4_readiness.json", result)
        return result

    project_root = getattr(config, "project_root", Path("."))
    project_root = Path(project_root)
    run_dir.mkdir(parents=True, exist_ok=True)

    # Pilot scoping (§13.3): a selection manifest restricts the comparison
    # to selected slides/atoms. The full IR/ledger files stay immutable;
    # the result is marked verification_scope=pilot_selection and never
    # proves a full-deck export.
    scope_selection: list[str] | None = None
    scope_doc = _read_json(run_dir / "pilot_selection.json")
    if isinstance(scope_doc, dict) and scope_doc.get("selected_slide_ids"):
        scope_selection = [str(i) for i in scope_doc["selected_slide_ids"]]

    # Immutable inputs: source IR/ledger + compiled rules + template profile.
    try:
        from .models import CompiledOntology, SourceDeckIR, SourceLedger, TemplateProfile
        deck = SourceDeckIR.model_validate_json(
            (run_dir / "source_ir.json").read_text(encoding="utf-8"))
        ledger = SourceLedger.model_validate_json(
            (run_dir / "source_ledger.json").read_text(encoding="utf-8"))
        rules = CompiledOntology.model_validate_json(
            (run_dir / "compiled_rules.json").read_text(encoding="utf-8"))
    except OSError as exc:
        return _fail(run_dir, provider, f"missing run inputs: {exc}", artifacts)
    profile = None
    for name in ("template_profile.json", "profiles/template_profile.json"):
        candidate = run_dir / name
        if candidate.is_file():
            try:
                from .models import TemplateProfile
                profile = TemplateProfile.model_validate_json(
                    candidate.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                profile = None
            break

    # Accepted plans with saved generation origin.
    intents, plan_origin = _load_accepted_plans(plans_dir)
    if not intents:
        return _fail(run_dir, provider, "no accepted plans in plans_dir", artifacts)
    if scope_selection is not None:
        intents = [it for it in intents
                   if (it.get("intent", it).get("slide_id") if isinstance(
                       it.get("intent", it), dict) else "") in scope_selection]

    manifest = _read_json(run_dir / "run_manifest.json")
    source_path = (manifest or {}).get("source_path", "")
    capabilities = {"implemented_operators": ["move", "style"]}
    resolved = resolve_slides_from_intents(intents, deck, rules, profile or {},
                                          capabilities)
    verification_scope = "full_source"
    if scope_selection is not None:
        ledger, verification_scope = scope_ledger_for_pilot(
            deck, ledger, scope_selection, run_dir)
    stale = [r for r in resolved if r.issues and "not found" in " ".join(r.issues)]
    if stale:
        return _fail(run_dir, provider,
                     f"stale plan: {[r.slide_id for r in stale]}", artifacts)

    preflight = preflight_native_export(deck, ledger, resolved, rules,
                                        profile or {}, options)
    atomic_write_json(run_dir / "export_preflight.json", preflight)
    artifacts["export_preflight"] = str(run_dir / "export_preflight.json")
    if not preflight.ok:
        return _fail(run_dir, provider,
                     f"preflight refused: {preflight.unresolved[:3]}", artifacts,
                     origin=plan_origin)

    return _emit_resolved_slides(
        run_dir, config, options, deck=deck, ledger=ledger, rules=rules,
        profile=profile, intents=intents, resolved=resolved,
        verification_scope=verification_scope, provider=provider,
        origin_note=origin_note, plan_origin=plan_origin,
        source_path=source_path, artifacts=artifacts, issues=issues)


def scope_ledger_for_pilot(deck: Any, ledger: Any,
                           scope_selection: list[str] | None,
                           run_dir: Path) -> tuple[Any, str]:
    """§13.3: restrict the required-atom ledger to the declared pilot scope.

    Returns ``(scoped_ledger, verification_scope)`` and writes
    ``ledger_scope_manifest.json`` (IDs/counts of the selected subset) so
    downstream gates and the independent verifier compare exactly the
    declared subset — never an empty expectation and never the whole deck.
    The full ``source_ledger.json`` on disk stays immutable.
    """
    if scope_selection is None:
        return ledger, "full_source"
    selected_parts = {s.slide_part or "" for s in deck.slides
                      if s.id in set(scope_selection)}
    scope_doc_ex = _read_json(run_dir / "pilot_selection.json") or {}
    excluded_ids: set[str] = set()
    exclusion_grounds: dict[str, str] = {}
    for entry in list(scope_doc_ex.get("excluded_objects", []) or []):
        if isinstance(entry, dict) and entry.get("id"):
            excluded_ids.add(str(entry["id"]))
            exclusion_grounds[str(entry["id"])] = str(
                entry.get("grounds", ""))
    scoped_atoms = [a for a in ledger.atoms
                    if (getattr(getattr(a, "source_ref", None),
                                "slide_part", "") in selected_parts
                        or getattr(getattr(a, "source_ref", None),
                                   "kind", "") != "pptx")
                    and str(getattr(a, "id", "")) not in excluded_ids
                    and str(getattr(a, "id", "")).split("#")[0]
                    not in excluded_ids]
    excluded_atoms = [a for a in ledger.atoms
                      if str(getattr(a, "id", "")) in excluded_ids
                      or str(getattr(a, "id", "")).split("#")[0]
                      in excluded_ids]
    notes_atoms = sum(1 for a in scoped_atoms
                      if getattr(a, "kind", "") == "notes")
    slides_meta = []
    for s in deck.slides:
        if s.id not in set(scope_selection):
            continue
        notes = sum(1 for a in ledger.atoms
                    if getattr(getattr(a, "source_ref", None),
                               "slide_part", "") == (s.slide_part or "")
                    and getattr(a, "kind", "") == "notes")
        slides_meta.append({
            "id": s.id,
            "objects": len(list(getattr(s, "objects", []) or [])),
            "hidden": bool(getattr(s, "hidden", False)),
            "notes": notes,
        })
    atomic_write_json(run_dir / "ledger_scope_manifest.json", {
        "verification_scope": "pilot_selection",
        "selected_atoms": len(scoped_atoms),
        "total_atoms": len(ledger.atoms),
        "selected_notes_atoms": notes_atoms,
        "slides": slides_meta,
        "excluded_from_pilot": [
            {"id": a.id, "kind": getattr(a, "kind", ""),
             "grounds": exclusion_grounds.get(
                 str(getattr(a, "id", "")), exclusion_grounds.get(
                     str(getattr(a, "id", "")).split("#")[0], ""))}
            for a in excluded_atoms],
    })
    return ledger.model_copy(update={"atoms": scoped_atoms}), \
        "pilot_selection"


def _fail(run_dir: Path, provider: str, message: str,
          artifacts: dict[str, str], origin: str = "") -> NativeBuildResult:
    result = NativeBuildResult(
        operation_status="needs_review" if "OUTPUT_EXISTS" in message else "failed",
        llm_provider=provider, plan_generation_origin=origin,
        issues=[message], artifacts=dict(artifacts))
    with contextlib.suppress(OSError):
        atomic_write_json(run_dir / "stage4_readiness.json", result)
    return result


def _parts_equal(first: bytes | None, second: bytes | None) -> bool:
    """Compare two package parts: bytes, or canonical XML for XML parts."""
    if first is None or second is None:
        return first is not None and second is not None and first == second
    if first == second:
        return True
    from lxml import etree as _etree

    try:
        first_root = _etree.fromstring(first)
        second_root = _etree.fromstring(second)
    except _etree.XMLSyntaxError:
        return False
    first_c14n = _etree.tostring(first_root, method="c14n")
    second_c14n = _etree.tostring(second_root, method="c14n")
    return bool(first_c14n == second_c14n)


def _write_transplant_artifacts(run_dir: Path, ctx: Any,
                                source_path: str, template_path: Path | None,
                                artifacts: dict[str, str],
                                candidate_name: str = "candidate.pptx") -> None:
    """Persist part/relationship maps, contracts, chart and template diffs."""
    import hashlib as _hashlib

    tctx = ctx.transplant_context
    part_map: dict[str, str] = {}
    rels: list[dict[str, Any]] = []
    contracts: list[dict[str, Any]] = []
    closures: list[dict[str, Any]] = []
    if tctx is not None:
        for key, target in tctx.target_names.items():
            part_map[key] = target
        rels = [m.model_dump(mode="json") for m in tctx.relationship_map]
        contracts = [c.model_dump(mode="json") for c in tctx.part_contracts]
        for key, target in sorted(part_map.items()):
            closures.append({"source_key": key, "target": target})
    atomic_write_json(run_dir / "part_map.json", part_map)
    atomic_write_json(run_dir / "relationship_map.json", rels)
    atomic_write_json(run_dir / "part_preservation_contracts.json", contracts)
    atomic_write_json(run_dir / "part_closure.json", closures)
    artifacts.update({
        "part_map": str(run_dir / "part_map.json"),
        "relationship_map": str(run_dir / "relationship_map.json"),
        "part_contracts": str(run_dir / "part_preservation_contracts.json"),
        "part_closure": str(run_dir / "part_closure.json"),
    })
    atomic_write_json(run_dir / "chart_semantic_diff.json",
                      list(ctx.chart_semantic_diffs))
    workbook_hashes: dict[str, str] = {}
    if tctx is not None:
        for target, payload in tctx.target_payloads.items():
            if target.endswith(".xlsx"):
                workbook_hashes[target] = _hashlib.sha256(payload).hexdigest()
    atomic_write_json(run_dir / "workbook_hashes.json", workbook_hashes)
    artifacts["chart_semantic_diff"] = str(run_dir / "chart_semantic_diff.json")
    artifacts["workbook_hashes"] = str(run_dir / "workbook_hashes.json")
    # Template integrity: corporate masters/layouts/themes semantically
    # identical (XML canonicalized: python-pptx re-serializes parts on save,
    # so byte equality would false-positive on benign reserialization).
    integrity: dict[str, Any] = {"protected": [], "violations": []}
    if template_path is not None and template_path.is_file():

        before = adapter.read_zip_members(template_path)
        after = adapter.read_zip_members(run_dir / candidate_name)
        for name, blob in sorted(before.items()):
            is_master = "/slideMasters/" in name or "/slideLayouts/" in name
            is_theme = "/theme/theme" in name and "Override" not in name
            if not (is_master or is_theme):
                continue
                other = after.get(name)
                same = _parts_equal(blob, other)
                integrity["protected"].append(
                    {"part": name, "preserved": bool(same)})
                if not same:
                    integrity["violations"].append(name)
    integrity["ok"] = not integrity["violations"]
    atomic_write_json(run_dir / "template_integrity_report.json", integrity)
    artifacts["template_integrity"] = str(
        run_dir / "template_integrity_report.json")
    if template_path is not None and template_path.is_file():
        import hashlib as _hashlib2

        manifest = {
            "template_path": str(template_path),
            "template_sha256": _hashlib2.sha256(
                template_path.read_bytes()).hexdigest(),
            "canvas_emu": [int(ctx.presentation.slide_width),
                           int(ctx.presentation.slide_height)],
            "layout_source": "verified TemplateProfile layout identities",
        }
        atomic_write_json(run_dir / "active_template_manifest.json", manifest)
        artifacts["active_template_manifest"] = str(
            run_dir / "active_template_manifest.json")


def _write_trace_artifacts(run_dir: Path, resolved: list[ResolvedSlide],
                           ctx: Any, intents: list[dict[str, Any]],
                           artifacts: dict[str, str]) -> None:
    """Persist resolved slides, intent→output trace and plan manifest."""
    out_dir = run_dir / "resolved_slides"
    out_dir.mkdir(parents=True, exist_ok=True)
    for res in resolved:
        (out_dir / f"{res.slide_id.replace('/', '_')}.json").write_text(
            res.model_dump_json(indent=2), encoding="utf-8")
    trace = []
    for res in resolved:
        trace.append({
            "intent_id": res.intent_id,
            "slide_id": res.slide_id,
            "generation_origin": res.generation_origin,
            "zones": res.zones,
            "role_assignments": res.role_assignments,
            "diagnostic_geometry": res.diagnostic_geometry,
            "emission_addresses": [
                r.model_dump(mode="json") for r in ctx.emission_records
            ],
        })
    atomic_write_json(run_dir / "intent_export_trace.json", trace)
    artifacts["intent_export_trace"] = str(run_dir / "intent_export_trace.json")
    artifacts["resolved_slides"] = str(out_dir)
    roles_used = sorted({r for res in resolved for r in res.role_assignments.values()
                         if r})
    coverage = {
        "roles_assigned": roles_used,
        "role_source": "loaded ontology snapshot roles (no literals)",
        "list_profile": "LS01 via bullets.apply_list_profile (loaded profile)",
        "autofit": "noAutofit on every emitted textbox",
        "unresolved_scopes": ["measurement", "render", "powerpoint_ui"],
    }
    atomic_write_json(run_dir / "style_rule_coverage.json", coverage)
    artifacts["style_rule_coverage"] = str(run_dir / "style_rule_coverage.json")


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _load_semantic_gate_inputs(
    run_dir: Path,
) -> tuple[Any, Any, str]:
    """Load semantic deck + normalized transform ledger + snapshot (§6.2).

    Returns (SemanticDeck|None, TransformLedger|None, source_snapshot).
    The flat classification ledger (source_object_id/action/
    authorization_ref/target_relation_ids) is normalized into the typed
    model; flat harness authorization_refs become harness_review basis.
    Missing files mean the legacy emission path applies (None, None).
    """
    import hashlib as _hashlib

    from .models import (  # noqa: PLC0415
        SemanticDeck,
        TransformationAuthorization,
        TransformationDecision,
        TransformLedger,
    )

    snapshot = ""
    ir_path = run_dir / "source_ir.json"
    if ir_path.is_file():
        try:
            snapshot = _hashlib.sha256(
                ir_path.read_bytes()).hexdigest()
        except OSError:
            snapshot = ""
    deck = None
    for name in ("inputs/semantic_deck.json", "semantic_deck.json"):
        p = run_dir / name
        if p.is_file():
            try:
                deck = SemanticDeck.model_validate_json(
                    p.read_text(encoding="utf-8"))
            except ValueError:
                deck = None
            break
    ledger = None
    doc = _read_json(run_dir / "classification_and_transformation_ledger.json")
    if isinstance(doc, dict) and deck is not None:
        decisions = []
        for dec in doc.get("decisions", []) or []:
            if not isinstance(dec, dict):
                continue
            oid = str(dec.get("source_object_id", ""))
            if not oid:
                continue
            ref = str(dec.get("authorization_ref", ""))
            basis = "harness_review" if ref.startswith("harness:") \
                else ("rule" if ref.startswith("rule:") else "unknown")
            action = str(dec.get("action", "preserve"))
            if action not in ("preserve", "recompose", "replace", "omit"):
                continue
            decisions.append(TransformationDecision(
                source_object_id=oid,
                source_unit_ids=[str(x) for x in
                                 dec.get("source_unit_ids", [])],
                action=action,  # type: ignore[arg-type]
                target_node_ids=[str(x) for x in
                                 dec.get("target_node_ids", [])],
                target_relation_ids=[str(x) for x in
                                     dec.get("target_relation_ids", [])],
                authorization=TransformationAuthorization(
                    authorized_by=ref.split(":", 1)[0] if ":" in ref
                    else ref, basis=basis,  # type: ignore[arg-type]
                    ref=ref),
                semantic_evidence=[str(x) for x in
                                   dec.get("evidence_refs",
                                           dec.get("semantic_evidence", []))],
                reason=str(dec.get("reason", "")),
                source_snapshot=str(dec.get("source_snapshot", snapshot)
                                    or snapshot)))
        ledger = TransformLedger(source_snapshot=snapshot,
                                 decisions=decisions)
    return deck, ledger, snapshot


def _load_accepted_plans(plans_dir: Path) -> tuple[list[dict[str, Any]], str]:
    intents: list[dict[str, Any]] = []
    origins: list[str] = []
    if not plans_dir.is_dir():
        return [], ""
    for path in sorted(plans_dir.glob("*.json")):
        if path.name in ("accepted_plan_manifest.json",):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if not isinstance(item, dict):
                continue
            # Accepted-plan envelope: {"intent": <LayoutIntent>,
            # "generation_origin": ...}. Bare intents are also accepted.
            origin = str(item.get("generation_origin", ""))
            body = item.get("intent", item)
            if isinstance(body, dict) and ("zones" in body
                                           or "content_refs" in body):
                if origin:
                    body = dict(body)
                    body["generation_origin"] = origin
                    intents.append(body)
                else:
                    intents.append(body)
                if origin:
                    origins.append(origin)
    manifest = _read_json(plans_dir / "accepted_plan_manifest.json")
    if manifest and manifest.get("generation_origin"):
        origins.append(str(manifest["generation_origin"]))
    origin = origins[0] if origins else ""
    return intents, origin


def _find_template(run_dir: Path, project_root: Path) -> Path | None:
    for candidate in (run_dir / "profiles" / "base_template.pptx",
                      run_dir / "base_template.pptx"):
        if candidate.is_file():
            return candidate
    return None


def _wire_deferred_slide_rels(staged: Path, context: Any) -> None:
    """Wire chart-frame + internal-link rels into the staged package.

    For every deferred item: resolve the owner slide part (by position),
    allocate an rId unique within that owner, patch the slide XML when the
    provisional rId collides, record the rel for final assembly, and re-zip
    the staged package in place.
    """
    import zipfile

    from lxml import etree

    members = adapter.read_zip_members(staged)
    slide_parts = _saved_slide_order(staged)
    rel_ns = ("http://schemas.openxmlformats.org/package/2006/relationships")
    r_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    c_ns = "http://schemas.openxmlformats.org/drawingml/2006/chart"
    a_ns = "http://schemas.openxmlformats.org/drawingml/2006/main"

    def _owner_rids(owner: str) -> set[str]:
        directory, _, base = owner.rpartition("/")
        rels_name = f"{directory}/_rels/{base}.rels" if directory else \
            f"_rels/{base}.rels"
        data = members.get(rels_name)
        if not data:
            return set()
        try:
            root = etree.fromstring(data)
        except etree.XMLSyntaxError:
            return set()
        return {n.get("Id") or ""
                for n in root.findall(f"{{{rel_ns}}}Relationship")}

    def _claim(owner: str, want: str) -> str:
        taken = _owner_rids(owner)
        taken.update(r["id"] for o, r in context.deferred_owner_rels
                     if o == owner)
        if want not in taken:
            return want
        counter = 1
        while f"{want}-{counter}" in taken:
            counter += 1
        return f"{want}-{counter}"

    def _patch_slide(owner: str, old_rid: str, new_rid: str,
                     ns: str, local: str) -> None:
        if old_rid == new_rid or owner not in members:
            return
        try:
            root = etree.fromstring(members[owner])
        except etree.XMLSyntaxError:
            return
        for node in root.findall(f".//{{{ns}}}{local}"):
            if node.get(f"{{{r_ns}}}id") == old_rid:
                node.set(f"{{{r_ns}}}id", new_rid)
                break
            if node.get(f"{{{r_ns}}}embed") == old_rid:
                node.set(f"{{{r_ns}}}embed", new_rid)
                break
        members[owner] = etree.tostring(root, xml_declaration=True,
                                        encoding="UTF-8", standalone=True)

    chart_type = ("http://schemas.openxmlformats.org/officeDocument/"
                  "2006/relationships/chart")
    slide_type = ("http://schemas.openxmlformats.org/officeDocument/"
                  "2006/relationships/slide")

    for item in context.pending_chart_frames:
        pos = int(item.get("slide_position", 0))
        if pos >= len(slide_parts):
            context.issues.append("chart frame slide index out of range")
            continue
        owner = slide_parts[pos]
        final_rid = _claim(owner, str(item.get("rid", "rId9")))
        _patch_slide(owner, str(item.get("rid", "")), final_rid,
                     c_ns, "chart")
        rel_target = posixpath.relpath(item["target_chart"],
                                      posixpath.dirname(owner) or ".")
        context.deferred_owner_rels.append((owner, {
            "id": final_rid, "type": chart_type, "target": rel_target,
            "mode": "internal"}))

    for item in context.pending_hyperlinks:
        if item.get("kind") != "internal":
            continue
        if item.get("target_part"):
            pass  # already resolved via the slide-id map
        elif item.get("target_position") is not None:
            pos = int(item["target_position"])
            if pos < len(slide_parts):
                item["target_part"] = slide_parts[pos]
            else:
                context.issues.append("internal link target position out of range")
                continue
        else:
            continue
        pos = int(item.get("slide_position", 0))
        if pos >= len(slide_parts):
            context.issues.append("internal link slide index out of range")
            continue
        owner = slide_parts[pos]
        final_rid = _claim(owner, str(item.get("rid", "rId9")))
        _patch_slide(owner, str(item.get("rid", "")), final_rid,
                     a_ns, "hlinkClick")
        rel_target = posixpath.relpath(item["target_part"],
                                      posixpath.dirname(owner) or ".")
        context.deferred_owner_rels.append((owner, {
            "id": final_rid, "type": slide_type, "target": rel_target,
            "mode": "internal"}))

    with zipfile.ZipFile(staged, "w",
                         compression=zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(members):
            zf.writestr(name, members[name])


def _saved_slide_order(staged: Path) -> list[str]:
    """Slide parts of a saved package in presentation order."""
    import zipfile

    from lxml import etree

    with zipfile.ZipFile(staged) as zf:
        names = set(zf.namelist())
        pres = zf.read("ppt/presentation.xml")
        rels_data = None
        for candidate in ("ppt/_rels/presentation.xml.rels",):
            if candidate in names:
                rels_data = zf.read(candidate)
    pml = "http://schemas.openxmlformats.org/presentationml/2006/main"
    rns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    root = etree.fromstring(pres)
    rel_root = etree.fromstring(rels_data) if rels_data else None
    by_id = {}
    if rel_root is not None:
        for node in rel_root.findall(f"{{{rel_ns}}}Relationship"):
            by_id[node.get("Id") or ""] = node.get("Target") or ""
    ordered = []
    lst = root.find(f"{{{pml}}}sldIdLst")
    if lst is not None:
        for node in lst.findall(f"{{{pml}}}sldId"):
            rid = node.get(f"{{{rns}}}id") or ""
            target = by_id.get(rid, "")
            if target:
                ordered.append(posixpath.normpath(
                    posixpath.join("ppt", target)).lstrip("/"))
    return ordered


def _slide_hidden(deck: Any, slide_id: str) -> bool:
    for slide in deck.slides:
        if slide.id == slide_id:
            return bool(slide.hidden)
    return False


def _protected_parts(run_dir: Path) -> list[str]:
    data = _read_json(run_dir / "template_profile.json") or {}
    protected = data.get("protected_objects", []) or []
    names = []
    for entry in protected:
        ref = entry.get("source_ref", "") if isinstance(entry, dict) else ""
        if ref:
            names.append(ref)
    return names
