"""Planning bridge: LayoutIntent → native patch operations (Stage 3.5 §10).

Converts a validated LayoutIntent into addressable Stage 3 operations
(SetShapeGeometryOp, SetShapeStyleOp) applied through the existing GPN
patching pipeline. Does not create/delete content, does not modify
chart/table values, does not transplant unsupported parts.

Content box policy: the box comes from the template profile (layout
content box, or a derived box from canvas + protected regions) or from an
explicitly verified test-template dictionary for diagnostic fixtures.
There are no margin constants in this module; without any box the bridge
refuses instead of inventing geometry.

Expected-hash policy: every emitted operation carries the actual
``expected_xml_sha256`` of the addressed shape taken from an in-memory
clone of the source, chained after previous operations of the same batch
(§10.3.4). The source file itself is never mutated here.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..model import OperationBatch, SetShapeGeometryOp, SetShapeStyleOp
from .errors import GpnError, UnsupportedIntentError
from .models import CompiledOntology, SlideIR, SourceLedger
from .patching import GpnEditContext, apply_gpn_edits, preflight_gpn_edits
from .planner import LayoutIntent

log = logging.getLogger(__name__)

StrictModel = ConfigDict(extra="forbid")

EMU_PER_INCH = 914400
PT_PER_INCH = 72


class DiagnosticResolvedSlide(BaseModel):
    """Resolved layout for one slide from a model intent."""

    model_config = StrictModel

    slide_id: str
    content_box: dict[str, float] = Field(default_factory=dict)
    content_box_verified: bool = False
    content_box_source: str = ""
    zones: list[dict[str, Any]] = Field(default_factory=list)
    source_bindings: list[dict[str, Any]] = Field(default_factory=list)
    resolved_styles: list[dict[str, Any]] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    readability_status: str = "unknown"


class IntentPatchProposal(BaseModel):
    """Proposed patch batch from an intent."""

    model_config = StrictModel

    operation_batch: dict[str, Any] = Field(default_factory=dict)
    intent_to_operation_map: list[dict[str, Any]] = Field(default_factory=list)
    authorizations: list[dict[str, Any]] = Field(default_factory=list)
    expected_source_hash: str = ""
    issues: list[str] = Field(default_factory=list)


class DiagnosticBuildResult(BaseModel):
    """Result of applying a diagnostic intent."""

    model_config = StrictModel

    candidate_path: str | None = None
    committed_count: int = 0
    rolled_back: bool = True
    import_diff: dict[str, Any] = Field(default_factory=dict)
    package_diff: dict[str, Any] = Field(default_factory=dict)
    statuses: dict[str, Any] = Field(default_factory=dict)
    report_paths: dict[str, str] = Field(default_factory=dict)
    test_only: bool = True
    strict_output_ready: bool = False
    visual_validated: bool = False


def resolve_diagnostic_layout(
    intent: LayoutIntent,
    source_slide: SlideIR,
    profile: Any,
    rules: CompiledOntology,
    capabilities: dict[str, Any],
) -> DiagnosticResolvedSlide:
    """Resolve an intent into concrete geometry and style assignments.

    Zones come from the intent (never a static grid); styles come from the
    compiled roles; the content box comes from the template profile.
    Readability stays "unknown" — there is no text measurer yet (Stage 5).
    """
    issues: list[str] = []

    content_box, box_verified, box_source, box_issues = _resolve_content_box(profile, rules)
    issues.extend(box_issues)
    if not content_box:
        issues.append("no content box available from template profile; bridge will refuse")

    zones: list[dict[str, Any]] = []
    source_bindings: list[dict[str, Any]] = []
    resolved_styles: list[dict[str, Any]] = []

    for zone in intent.zones:
        zones.append({
            "zone_id": zone.zone_id,
            "x_pct": zone.x_pct,
            "y_pct": zone.y_pct,
            "w_pct": zone.w_pct,
            "h_pct": zone.h_pct,
            "role": zone.role,
            "content_refs": list(zone.content_refs),
        })

        for ref in zone.content_refs:
            binding = _find_source_binding(ref, source_slide)
            if binding is None:
                issues.append(f"content ref {ref!r} not found in source slide")
                continue
            source_bindings.append({
                "ref": ref,
                "zone_id": zone.zone_id,
                "shape_id": binding.get("shape_id"),
                "group_path": list(binding.get("group_path") or []),
                "slide_index": binding.get("slide_index", 0),
            })
            if zone.role:
                style = _resolve_role_style(zone.role, rules)
                if style is None:
                    issues.append(
                        f"style_role {zone.role!r} could not be resolved for ref {ref!r}"
                    )
                else:
                    resolved_styles.append({
                        "ref": ref,
                        "role": zone.role,
                        "style": style,
                    })

    if not capabilities.get("implemented_operators"):
        issues.append("no implemented operators declared in capabilities")

    return DiagnosticResolvedSlide(
        slide_id=intent.slide_id,
        content_box=content_box,
        content_box_verified=box_verified,
        content_box_source=box_source,
        zones=zones,
        source_bindings=source_bindings,
        resolved_styles=resolved_styles,
        issues=issues,
        readability_status="unknown",
    )


def lower_intent_to_patch_batch(
    resolved: DiagnosticResolvedSlide,
    source: SlideIR,
    ledger: SourceLedger,
    edit_context: GpnEditContext,
    *,
    source_pptx: Path | None = None,
) -> IntentPatchProposal:
    """Convert resolved layout into SetShapeGeometryOp/SetShapeStyleOp operations.

    Only supported resolved properties are lowered; model-supplied primitive
    colors/sizes never appear here — styles are already resolved from the
    loaded rules. When ``source_pptx`` is given, every operation gets the
    real ``expected_xml_sha256`` of its target, chained per shape on an
    in-memory clone (the source file is read-only).
    """
    operations: list[dict[str, Any]] = []
    intent_to_op_map: list[dict[str, Any]] = []
    issues: list[str] = []

    if not resolved.content_box:
        raise UnsupportedIntentError(
            "BRIDGE/NO_CONTENT_BOX",
            "no verified content box from template profile; diagnostic bridge "
            "refuses to invent geometry",
            subject_ids=[resolved.slide_id],
        )

    cb_x = resolved.content_box["x"]
    cb_y = resolved.content_box["y"]
    cb_w = resolved.content_box["w"]
    cb_h = resolved.content_box["h"]

    for binding in resolved.source_bindings:
        shape_id = binding.get("shape_id")
        if not shape_id:
            issues.append(f"ref {binding.get('ref')!r} has no addressable shape id")
            continue

        zone_id = binding.get("zone_id", "")
        zone = next((z for z in resolved.zones if z["zone_id"] == zone_id), None)
        if zone is None:
            issues.append(f"zone {zone_id!r} missing for ref {binding.get('ref')!r}")
            continue

        x_in = cb_x + (zone["x_pct"] / 100.0) * cb_w
        y_in = cb_y + (zone["y_pct"] / 100.0) * cb_h
        w_in = (zone["w_pct"] / 100.0) * cb_w
        h_in = (zone["h_pct"] / 100.0) * cb_h

        geometry = SetShapeGeometryOp(
            op="set_shape_geometry",
            slide_index=binding.get("slide_index", 0),
            shape_id=int(shape_id),
            group_path=[int(g) for g in (binding.get("group_path") or [])],
            left=x_in,
            top=y_in,
            width=w_in,
            height=h_in,
        )
        operations.append(geometry.model_dump(mode="json"))
        intent_to_op_map.append({
            "ref": binding["ref"],
            "zone_id": zone_id,
            "op_index": len(operations) - 1,
            "op_type": "set_shape_geometry",
        })

        style_entry = next(
            (s for s in resolved.resolved_styles if s["ref"] == binding["ref"]),
            None,
        )
        if style_entry is not None:
            style_op = SetShapeStyleOp(
                op="set_shape_style",
                slide_index=binding.get("slide_index", 0),
                shape_id=int(shape_id),
                group_path=[int(g) for g in (binding.get("group_path") or [])],
                style=style_entry["style"],
            )
            operations.append(style_op.model_dump(mode="json"))
            intent_to_op_map.append({
                "ref": binding["ref"],
                "zone_id": zone_id,
                "op_index": len(operations) - 1,
                "op_type": "set_shape_style",
            })

    if not operations:
        issues.append("no operations generated from intent")

    expected_source_hash = ""
    if source_pptx is not None:
        expected_source_hash = _sha256_file(source_pptx)
        if operations:
            bound, bind_issues = _bind_expected_hashes(operations, source_pptx)
            operations = bound
            issues.extend(bind_issues)

    _ = (source, ledger)
    return IntentPatchProposal(
        operation_batch={"operations": operations},
        intent_to_operation_map=intent_to_op_map,
        authorizations=[{
            "edit_scope": edit_context.edit_scope,
            "test_only": edit_context.test_only,
            "policy": "stage3 authorize_gpn_edit applied during preflight",
        }],
        expected_source_hash=expected_source_hash,
        issues=issues,
    )


def apply_diagnostic_intent(
    proposal: IntentPatchProposal,
    edit_context: GpnEditContext,
    run_dir: Path,
    *,
    source: Path,
    candidate_output: Path,
    overwrite: bool = False,
) -> DiagnosticBuildResult:
    """Apply a diagnostic intent through the existing patching pipeline."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    try:
        batch = OperationBatch.model_validate(proposal.operation_batch)
    except Exception as exc:  # noqa: BLE001 — surfaced as a typed result
        return DiagnosticBuildResult(
            import_diff={"status": "invalid_batch", "error": str(exc)},
            statuses={"strict_output_ready": False, "production_assets_ready": False},
        )

    preflight = preflight_gpn_edits(source=source, edits=batch, context=edit_context)
    (run_dir / "preflight_report.json").write_text(
        preflight.model_dump_json(indent=2), encoding="utf-8"
    )

    if not preflight.valid:
        return DiagnosticBuildResult(
            import_diff={"status": "preflight_refused"},
            statuses={"strict_output_ready": False, "production_assets_ready": False},
            report_paths={"preflight_report": str(run_dir / "preflight_report.json")},
        )

    result = apply_gpn_edits(
        source=source,
        edits=batch,
        context=edit_context,
        run_dir=run_dir,
        candidate_output=candidate_output,
        overwrite=overwrite,
    )

    return DiagnosticBuildResult(
        candidate_path=result.candidate_path,
        committed_count=result.committed_count,
        rolled_back=result.rolled_back,
        import_diff=result.import_diff,
        package_diff=result.package_diff,
        statuses={
            "strict_output_ready": False,
            "production_assets_ready": False,
            "visual_validated": False,
            "diagnostic_native_patch": True,
        },
        report_paths=result.report_paths,
        test_only=True,
        strict_output_ready=False,
        visual_validated=False,
    )


def _resolve_content_box(
    profile: Any, rules: CompiledOntology
) -> tuple[dict[str, float], bool, str, list[str]]:
    """Resolve the content box in inches from the template profile.

    Sources, in order:
    1. explicit ``{"content_box": ..., "verified": true, "source": ...}``
       dictionary (diagnostic fixtures may declare a verified test template);
    2. ``layout_profiles[].content_box`` of a TemplateProfile (points → in);
    3. derived box from canvas + protected regions via the existing
       template algorithm (unverified unless layout evidence is present).
    """
    issues: list[str] = []

    if profile is None:
        return {}, False, "", issues

    if isinstance(profile, dict) and profile.get("content_box"):
        raw = profile["content_box"]
        unit = str(profile.get("unit", "in")).lower()
        try:
            if unit == "emu":
                box = {
                    "x": float(raw["x"]) / EMU_PER_INCH,
                    "y": float(raw["y"]) / EMU_PER_INCH,
                    "w": float(raw["w"]) / EMU_PER_INCH,
                    "h": float(raw["h"]) / EMU_PER_INCH,
                }
            elif unit == "pt":
                box = {
                    "x": float(raw["x"]) / PT_PER_INCH,
                    "y": float(raw["y"]) / PT_PER_INCH,
                    "w": float(raw["w"]) / PT_PER_INCH,
                    "h": float(raw["h"]) / PT_PER_INCH,
                }
            else:
                box = {k: float(raw[k]) for k in ("x", "y", "w", "h")}
        except (KeyError, TypeError, ValueError):
            issues.append("explicit content_box is malformed; ignored")
            return {}, False, "", issues
        verified = bool(profile.get("verified", False))
        source = str(profile.get("source", "explicit_test_template"))
        if not verified:
            issues.append("explicit content_box not marked verified")
        return box, verified, source, issues

    layouts = _layout_profiles(profile)
    for layout in layouts:
        box_raw = _get(layout, "content_box")
        if not box_raw:
            continue
        box = {
            "x": _pt_to_in(_get(box_raw, "x")),
            "y": _pt_to_in(_get(box_raw, "y")),
            "w": _pt_to_in(_get(box_raw, "w")),
            "h": _pt_to_in(_get(box_raw, "h")),
        }
        verified = bool(_get(layout, "is_verified", False))
        source = f"layout_content_box:{_get(layout, 'id', '')}"
        return box, verified, source, issues

    derived = _derive_content_box(profile, rules, issues)
    if derived is not None:
        return derived
    return {}, False, "", issues


def _derive_content_box(
    profile: Any, rules: CompiledOntology, issues: list[str]
) -> tuple[dict[str, float], bool, str, list[str]] | None:
    from .models import RectEMU
    from .template import compute_content_box

    protected: list[Any] = []
    evidence: list[str] = []
    for layout in _layout_profiles(profile):
        for region in (_get(layout, "protected_regions") or []):
            protected.append(RectEMU(
                x=int(_pt_to_emu(_get(region, "x"))),
                y=int(_pt_to_emu(_get(region, "y"))),
                w=int(_pt_to_emu(_get(region, "w"))),
                h=int(_pt_to_emu(_get(region, "h"))),
            ))
        evidence.extend(str(e) for e in (_get(layout, "evidence") or []))

    resolution = compute_content_box(rules.canvas, protected, None)
    if resolution.content_box is None:
        return None
    box = {
        "x": resolution.content_box.x / EMU_PER_INCH,
        "y": resolution.content_box.y / EMU_PER_INCH,
        "w": resolution.content_box.w / EMU_PER_INCH,
        "h": resolution.content_box.h / EMU_PER_INCH,
    }
    verified = bool(resolution.verified) and bool(evidence)
    source = "derived:canvas_minus_protected_regions"
    if not verified:
        issues.append("content box derived without layout evidence; unverified")
    return box, verified, source, issues


def _layout_profiles(profile: Any) -> list[Any]:
    layouts = getattr(profile, "layout_profiles", None)
    if layouts is None and isinstance(profile, dict):
        layouts = profile.get("layout_profiles")
    return list(layouts or [])


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _pt_to_in(value: Any) -> float:
    return float(value or 0) / PT_PER_INCH


def _pt_to_emu(value: Any) -> float:
    return float(value or 0) * 12700


def _find_source_binding(ref: str, source_slide: SlideIR) -> dict[str, Any] | None:
    """Find a source object by ref (object id or paragraph id)."""
    slide_index = int(getattr(source_slide, "source_index", 0))
    for obj in source_slide.objects:
        if obj.id == ref:
            address = _object_address(obj)
            if address is not None:
                shape_id, group_path = address
                return {
                    "shape_id": shape_id,
                    "group_path": group_path,
                    "slide_index": slide_index,
                }
            continue
        payload = obj.payload
        text = getattr(payload, "text", None)
        if text is None and hasattr(payload, "paragraphs"):
            text = payload
        if text is not None:
            for para in getattr(text, "paragraphs", []):
                if para.id == ref:
                    address = _object_address(obj)
                    if address is not None:
                        shape_id, group_path = address
                        return {
                            "shape_id": shape_id,
                            "group_path": group_path,
                            "slide_index": slide_index,
                        }
    return None


def _object_address(obj: Any) -> tuple[str, list[int]] | None:
    ref = getattr(obj, "source_ref", None)
    shape_id = getattr(ref, "shape_id", None) if ref is not None else None
    if not shape_id:
        return None
    group_path = list(getattr(ref, "group_path", None) or [])
    return str(shape_id), group_path


def _resolve_role_style(role_id: str, rules: CompiledOntology) -> Any:
    """Resolve a role to NativeShapeStyle via the existing patching resolver."""
    from .patching import StyleApplicationContext, resolve_native_style

    try:
        return resolve_native_style(
            style_role=role_id,
            rules=rules,
            context=StyleApplicationContext(),
        )
    except GpnError:
        return None


def _bind_expected_hashes(
    operations: list[dict[str, Any]], source_pptx: Path
) -> tuple[list[dict[str, Any]], list[str]]:
    """Bind real expected_xml_sha256 values, chained per shape (§10.3.4).

    Runs on an in-memory clone of the source; the file on disk is never
    modified. If a shape cannot be resolved the operation keeps no hash and
    an issue is recorded — preflight still refuses unaddressable shapes.
    """
    issues: list[str] = []
    try:
        from pptx import Presentation as _load_presentation

        from ..api import Presentation as _Presentation
        from ..api import resolve_shape_address
    except Exception as exc:  # noqa: BLE001
        return operations, [f"hash binding unavailable: {exc}"]

    try:
        clone = _Presentation(_load_presentation(io.BytesIO(source_pptx.read_bytes())))
    except Exception as exc:  # noqa: BLE001
        return operations, [f"hash binding could not open source: {exc}"]

    for idx, op in enumerate(operations):
        try:
            address = resolve_shape_address(
                clone,
                slide_index=int(op.get("slide_index", 0)),
                shape_id=int(op["shape_id"]),
                group_path=tuple(op.get("group_path") or ()),
            )
            op["expected_xml_sha256"] = address.current_xml_sha256
        except Exception as exc:  # noqa: BLE001
            issues.append(f"op {idx}: cannot resolve shape for hash binding: {exc}")
            continue
        try:
            typed = SetShapeGeometryOp.model_validate(op) if op.get("op") == "set_shape_geometry" \
                else SetShapeStyleOp.model_validate(op)
            clone.apply_operations(
                OperationBatch(operations=[typed]), transactional=False
            )
        except Exception as exc:  # noqa: BLE001
            issues.append(f"op {idx}: in-memory chaining failed: {exc}")
    return operations, issues


def _sha256_file(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()
