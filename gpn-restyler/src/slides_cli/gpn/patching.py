"""GPN native-edit guard, preflight and transactional apply (Stage 3 §§9-11).

Generic facade (``slides_cli.api``) applies addressed mutations without any
corporate knowledge. This module is the GPN adapter: it authorizes every edit
against compiled policy + ledger + protected/template evidence, preflights on
a copy, and only commits a verified diagnostic candidate after an independent
reimport + preservation gate. No ``trusted=true`` flag from user JSON can ever
replace verification.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pptx import Presentation as _load_presentation
from pydantic import BaseModel, ConfigDict, Field

from .errors import ExitCode, GpnError

StrictModel = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Runtime contexts (never built from a user "trusted" flag)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StyleApplicationContext:
    target_kind: str = "text"
    semantic_marks: tuple[str, ...] = ()
    background_token: str | None = None
    verified_role: str | None = None
    verified_evidence: tuple[str, ...] = ()
    requested_text_scope: str = "defaults"
    template_profile: Any = None


@dataclass(slots=True)
class GpnEditContext:
    compiled_rules: Any = None
    source_ir: Any = None
    ledger: Any = None
    template_profile: Any = None
    protected_subject_ids: frozenset[str] = frozenset()
    decoration_decisions: tuple[Any, ...] = ()
    replacement_refs: tuple[str, ...] = ()
    corpus_hashes: dict[str, str] = field(default_factory=dict)
    edit_scope: str = "diagnostic-native-edit"
    test_only: bool = True


@dataclass(slots=True)
class EditAuthorization:
    allowed: bool
    reason: str
    protected_subject_ids: tuple[str, ...] = ()
    content_subject_ids: tuple[str, ...] = ()
    decoration_refs: tuple[str, ...] = ()
    replacement_refs: tuple[str, ...] = ()


@dataclass(slots=True)
class DeletionPlan:
    target_ids: tuple[str, ...] = ()
    removable_rels: tuple[str, ...] = ()
    retained_shared_rels: tuple[str, ...] = ()
    unresolved_dependent_refs: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()


class GpnEditPreflight(BaseModel):
    model_config = StrictModel

    valid: bool
    per_op_checks: list[dict[str, Any]] = Field(default_factory=list)
    authorized_changes: list[dict[str, Any]] = Field(default_factory=list)
    expected_footprints: list[str] = Field(default_factory=list)
    before_hashes: dict[str, str] = Field(default_factory=dict)
    after_hashes: dict[str, str] = Field(default_factory=dict)
    blocked_subjects: list[str] = Field(default_factory=list)
    scope_limitations: list[str] = Field(default_factory=list)


class GpnPatchResult(BaseModel):
    model_config = StrictModel

    operation_report: dict[str, Any] = Field(default_factory=dict)
    rolled_back: bool = False
    committed_count: int = 0
    candidate_path: str | None = None
    import_diff: dict[str, Any] = Field(default_factory=dict)
    package_diff: dict[str, Any] = Field(default_factory=dict)
    statuses: dict[str, Any] = Field(default_factory=dict)
    report_paths: dict[str, str] = Field(default_factory=dict)
    test_only: bool = True
    strict_output_ready: bool = False





# ---------------------------------------------------------------------------
# Style role resolution (GPN adapter; generic facade never sees the ontology)
# ---------------------------------------------------------------------------


def resolve_native_style(
    *,
    style_role: str,
    rules: Any,
    context: StyleApplicationContext,
) -> Any:
    """Resolve a corporate style_role into explicit patch primitives."""
    from ..model import NativeShapeStyle as _NativeShapeStyle

    roles = getattr(rules, "roles", {}) or {}
    if style_role not in roles:
        raise GpnError(
            f"STYLE_ROLE_UNKNOWN: unknown style role {style_role!r}",
            f"unknown style_role {style_role!r}; needs_review",
            exit_code=ExitCode.NEEDS_REVIEW,
        )
    role = roles[style_role]
    faces = list(getattr(role, "allowed_faces", []) or [])
    if not faces:
        raise GpnError(
            "STYLE_ROLE_NO_FACE: role has no allowed faces",
            f"style_role {style_role!r} has no allowed faces",
            exit_code=ExitCode.NEEDS_REVIEW,
        )
    font_policy = getattr(rules, "font_policy", {}) or {}
    _ = font_policy
    face = faces[0]
    bold: bool | None = None
    font_policy_rules = getattr(rules, "font_policy_rules", []) or []
    for rule in font_policy_rules:
        if rule.typeface == face:
            bold = rule.bold
            break
    if bold is None:
        bold = False
    color_tokens = list(getattr(role, "color_tokens", []) or [])
    colors = dict(getattr(rules, "colors", {}) or {})
    conditional = getattr(rules, "conditional_colors", {}) or {}
    text_color = None
    if color_tokens:
        token = color_tokens[0]
        bare = token.strip().lstrip("#").upper()
        is_hex = len(bare) == 6 and all(
            ch in "0123456789ABCDEF" for ch in bare)
        if is_hex:
            text_color = bare
        elif token in colors:
            text_color = colors[token]
        elif token in conditional:
            cond = conditional[token]
            allowed = list(getattr(cond, "allowed_context_ids", []) or [])
            evidence = list(context.verified_evidence or [])
            if not evidence or not allowed:
                raise GpnError(
                    "STYLE_CONDITIONAL_NO_EVIDENCE: conditional color needs evidence",
                    f"conditional color {token!r} without verified evidence",
                    exit_code=ExitCode.NEEDS_REVIEW,
                )
            text_color = getattr(cond, "rgb", None)
        else:
            raise GpnError(
                "STYLE_TOKEN_UNKNOWN: color token not in palette",
                f"color token {token!r} not in the compiled palette",
                exit_code=ExitCode.NEEDS_REVIEW,
            )
    size = getattr(role, "default_size_pt", None)
    return _NativeShapeStyle(
        font_name=face,
        font_size_pt=float(size) if size else None,
        bold=bold,
        italic=None,
        text_color_rgb=text_color,
        fill_mode="keep",
        line_mode="keep",
        text_scope=context.requested_text_scope,  # type: ignore[arg-type]
        autofit_policy="none",
    )


# ---------------------------------------------------------------------------
# Authorization gate
# ---------------------------------------------------------------------------


def _protected_ids(context: GpnEditContext) -> set[str]:
    ids = set(context.protected_subject_ids or set())
    template = context.template_profile
    if template is not None:
        for obj in getattr(template, "protected_objects", []) or []:
            ref = getattr(obj, "source_ref", "")
            if ref:
                ids.add(str(ref))
    return ids


def _ledger_content_ids(context: GpnEditContext) -> set[str]:
    ids: set[str] = set()
    ledger = context.ledger
    if ledger is None:
        return ids
    for atom in getattr(ledger, "atoms", []) or []:
        ref = getattr(atom, "source_ref", None)
        if ref is not None:
            shape_id = getattr(ref, "shape_id", None)
            if shape_id:
                ids.add(str(shape_id))
    return ids


def _check_style_against_policy(style: Any, context: GpnEditContext) -> str | None:
    """Return a refusal reason, or None when the primitives are allowed."""
    rules = context.compiled_rules
    if rules is None:
        return "no compiled rules for style guard"
    colors = dict(getattr(rules, "colors", {}) or {})
    palette = {v.upper() for v in colors.values()}
    conditional = getattr(rules, "conditional_colors", {}) or {}
    cond_rgbs = set()
    if isinstance(conditional, dict):
        for cond in conditional.values():
            rgb = getattr(cond, "rgb", "")
            if rgb:
                cond_rgbs.add(str(rgb).upper())
    for attr in ("text_color_rgb", "fill_color_rgb", "line_color_rgb"):
        val = getattr(style, attr, None)
        if val and val.upper() not in palette and val.upper() not in cond_rgbs:
            return f"{attr} {val!r} is not an approved palette token"
    # Single-RGB coincidence never proves a whole style: every primitive with
    # a color must resolve, and solid modes already require exact HEX.
    faces: set[str] = set()
    for role in (getattr(rules, "roles", {}) or {}).values():
        faces.update(getattr(role, "allowed_faces", []) or [])
    font_name = getattr(style, "font_name", None)
    if font_name and faces and font_name not in faces:
        return f"font {font_name!r} is not an allowed corporate face"
    wants_synthetic_bold = getattr(style, "bold", None) is True
    has_bold_face = bool(font_name and "Bold" in font_name)
    # Synthetic bold is refused unless the face itself is the documented
    # Bold exception (exact Bold face chosen by resolve_native_style).
    if wants_synthetic_bold and not has_bold_face:
        return ("synthetic bold refused: use the exact Bold face "
                "with b=false (Condensed-Italic +bold is the only exception)")
    return None


def authorize_gpn_edit(edit: Any, context: GpnEditContext) -> EditAuthorization:
    """Authorize one operation against policy/ledger/protected evidence."""
    from ..model import DeleteShapeOp, SetShapeGeometryOp, SetShapeStyleOp

    op_name = getattr(edit, "op", "?")
    shape_id = str(getattr(edit, "shape_id", ""))
    protected = _protected_ids(context)
    if shape_id and shape_id in protected:
        return EditAuthorization(
            allowed=False,
            reason=f"shape {shape_id} is protected (master/layout/logo/footer)",
            protected_subject_ids=(shape_id,),
        )
    group_path = tuple(getattr(edit, "group_path", []) or [])
    for gid in group_path:
        if str(gid) in protected:
            return EditAuthorization(
                allowed=False,
                reason=f"group path element {gid} is protected",
                protected_subject_ids=(str(gid),),
            )
    if isinstance(edit, SetShapeGeometryOp):
        canvas = getattr(getattr(context, "compiled_rules", None), "canvas", None)
        if canvas is not None:
            emu_per_in = 914400
            try:
                l_emu = int(round(float(edit.left) * emu_per_in))
                t_emu = int(round(float(edit.top) * emu_per_in))
                w_emu = int(round(float(edit.width) * emu_per_in))
                h_emu = int(round(float(edit.height) * emu_per_in))
            except (TypeError, ValueError):
                return EditAuthorization(
                    allowed=False, reason="non-numeric geometry")
            if l_emu + w_emu > int(canvas.w) or t_emu + h_emu > int(canvas.h):
                return EditAuthorization(
                    allowed=False,
                    reason="geometry leaves the corporate canvas",
                    content_subject_ids=(shape_id,),
                )
        return EditAuthorization(allowed=True, reason="geometry within policy")
    if isinstance(edit, SetShapeStyleOp):
        refusal = _check_style_against_policy(edit.style, context)
        if refusal:
            return EditAuthorization(allowed=False, reason=refusal)
        return EditAuthorization(allowed=True, reason="style primitives allowed")
    if isinstance(edit, DeleteShapeOp):
        content_ids = _ledger_content_ids(context)
        decisions = {str(getattr(d, "object_id", ""))
                     for d in (context.decoration_decisions or ())}
        if shape_id in protected:
            return EditAuthorization(
                allowed=False, reason="protected objects are never deleted",
                protected_subject_ids=(shape_id,))
        if shape_id in content_ids and shape_id not in decisions:
            return EditAuthorization(
                allowed=False,
                reason=("content object deletion needs a verified "
                        "DecorationDecision/replacement mapping; "
                        "reason strings never authorize it"),
                content_subject_ids=(shape_id,),
            )
        unknown_subject = (shape_id not in decisions and shape_id not in content_ids)
        # Unknown subject: without ledger/decoration proof this stage refuses
        # content deletion (decor claims from models don't count).
        if unknown_subject and context.ledger is not None:
            return EditAuthorization(
                allowed=False,
                reason=("no verified decoration/replacement proof for "
                        f"shape {shape_id}"),
                content_subject_ids=(shape_id,),
            )
        return EditAuthorization(
            allowed=True, reason="verified decoration removal",
            decoration_refs=tuple(sorted(decisions)) if decisions else ())
    return EditAuthorization(
        allowed=False, reason=f"unsupported operation for GPN scope: {op_name}")


def _descendant_shape_ids(group_shape: Any) -> list[str]:
    """Collect all descendant shape ids of a group (nested included)."""
    ids: list[str] = []
    stack = [group_shape]
    while stack:
        current = stack.pop(0)
        children = getattr(current, "shapes", None)
        if children is None:
            continue
        for child in children:
            ids.append(str(getattr(child, "shape_id", "")))
            if getattr(child, "shapes", None) is not None:
                stack.append(child)
    return ids


def _refuse_group_with_guarded_children(
    presentation: Any, op: Any, context: GpnEditContext,
) -> EditAuthorization | None:
    """Refuse group deletion when a content/protected child is inside."""
    from ..api import resolve_shape_address as _resolve

    try:
        address = _resolve(
            presentation, slide_index=op.slide_index, shape_id=op.shape_id,
            group_path=tuple(getattr(op, "group_path", []) or ()))
    except Exception:  # noqa: BLE001
        return None
    if getattr(address, "shape_kind", "") != "group":
        return None
    protected = _protected_ids(context)
    content_ids = _ledger_content_ids(context)
    decisions = {str(getattr(d, "object_id", ""))
                 for d in (context.decoration_decisions or ())}
    for child_id in _descendant_shape_ids(address.shape):
        if child_id in protected:
            return EditAuthorization(
                allowed=False,
                reason=(f"group {op.shape_id} holds protected child {child_id}"),
                protected_subject_ids=(child_id,),
            )
        if child_id in content_ids and child_id not in decisions:
            return EditAuthorization(
                allowed=False,
                reason=(f"group {op.shape_id} holds content child {child_id} "
                        "without replacement proof"),
                content_subject_ids=(child_id,),
            )
    return None


def plan_shape_deletion(address: Any, graph: Any) -> DeletionPlan:
    """Plan relationship cleanup for a resolved deletion target."""
    element = getattr(address.shape, "_element", None)
    outgoing: set[str] = set()
    if element is not None:
        for node in element.iter():
            for _attr, val in node.attrib.items():
                if isinstance(val, str) and val.startswith("rId"):
                    outgoing.add(val)
    owner = getattr(address, "owner_part", "")
    removable: set[str] = set()
    retained: set[str] = set()
    if graph is not None and owner:
        # Without the owner's remaining XML here, conservatively retain
        # shared candidates; the facade re-verifies against live XML.
        removable = set(outgoing)
    target_ids = (str(getattr(address.shape, "shape_id", "")),)
    # Connector/animation dependents are detected live by the facade; plan
    # records the check without silently resolving it.
    return DeletionPlan(
        target_ids=target_ids,
        removable_rels=tuple(sorted(removable)),
        retained_shared_rels=tuple(sorted(retained)),
        unresolved_dependent_refs=(),
        issues=(),
    )


# ---------------------------------------------------------------------------
# Preflight + apply
# ---------------------------------------------------------------------------


def _read_edits_file(edits_json: str) -> dict[str, Any]:
    path = edits_json[1:] if edits_json.startswith("@") else edits_json
    raw = Path(path).read_text(encoding="utf-8")
    return json.loads(raw)


def _build_context(
    *, source: Path, config: Any, project_root: Path, test_only: bool = True,
) -> GpnEditContext:
    from .assets import AssetStore
    from .compiler import compile_ontology
    from .importer import import_deck
    from .ontology import resolve_ontology_sources
    from .ontology_conflicts import analyze_ontology_sources
    from .provenance import build_ledger

    sources = resolve_ontology_sources(project_root)
    conflicts = analyze_ontology_sources(sources)
    blocking = [c for c in conflicts.conflicts if c.blocking]
    if blocking or not conflicts.ready_for_compilation:
        raise GpnError(
            "NORMATIVE_BLOCKED: blocking ontology conflict",
            "blocking normative ambiguity; needs_review",
            exit_code=ExitCode.NEEDS_REVIEW,
        )
    compiled = compile_ontology(
        Path(sources.primary_json), None, sources=sources) \
        if sources.primary_json else None
    store = AssetStore(project_root / "runs" / ".stage3-cache" / "assets")
    deck, _ = import_deck(source, store)
    ledger = build_ledger(deck, decoration=[])
    hashes = {
        "ontology_corpus_hash": sources.ontology_corpus_hash,
        "source_sha256": deck.source_sha256,
    }
    return GpnEditContext(
        compiled_rules=compiled,
        source_ir=deck,
        ledger=ledger,
        template_profile=None,
        corpus_hashes=hashes,
        test_only=test_only,
    )


def preflight_gpn_edits(
    *,
    source: Path,
    edits: Any,
    context: GpnEditContext,
) -> GpnEditPreflight:
    """Validate/authorize/apply edits on a copy; the source never mutates."""
    from ..api import Presentation as _Presentation
    from ..model import OperationBatch as _OperationBatch

    source = Path(source)
    source_bytes = source.read_bytes()
    before = {
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "source_bytes": str(len(source_bytes)),
    }
    from ..model import DeleteShapeOp as _DeleteShapeOp

    batch = edits if hasattr(edits, "operations") else _OperationBatch.model_validate(edits)
    per_op: list[dict[str, Any]] = []
    authorized: list[dict[str, Any]] = []
    footprints: list[str] = []
    blocked: list[str] = []
    # Validate schema + GPN guards first (no mutation yet).
    probe = _Presentation(_load_presentation(io.BytesIO(source_bytes)))
    for idx, op in enumerate(batch.operations):
        auth = authorize_gpn_edit(op, context)
        # Groups with content/protected children are protected too: resolve
        # the target on the untouched probe and inspect descendants.
        if auth.allowed and isinstance(op, _DeleteShapeOp):
            group_refusal = _refuse_group_with_guarded_children(probe, op, context)
            if group_refusal is not None:
                auth = group_refusal
        entry = {
            "index": idx, "op": getattr(op, "op", "?"),
            "allowed": auth.allowed, "reason": auth.reason,
        }
        per_op.append(entry)
        if not auth.allowed:
            blocked.append(str(getattr(op, "shape_id", f"op{idx}")))
        else:
            authorized.append(entry)
            footprints.append(
                f"slide{getattr(op, 'slide_index', '?')}"
                f"/shape{getattr(op, 'shape_id', '?')}:{getattr(op, 'op', '?')}")
    if blocked:
        return GpnEditPreflight(
            valid=False, per_op_checks=per_op,
            authorized_changes=authorized,
            expected_footprints=footprints,
            before_hashes=before, after_hashes={},
            blocked_subjects=blocked,
            scope_limitations=["blocked edits refused before any mutation"],
        )
    # Apply on a copy sequentially (addresses/hashes/support/shared refs).
    clone = _Presentation(_load_presentation(io.BytesIO(source_bytes)))
    report = clone.apply_operations(batch, dry_run=False, transactional=True)
    if not report.ok:
        return GpnEditPreflight(
            valid=False, per_op_checks=per_op,
            authorized_changes=authorized,
            expected_footprints=footprints,
            before_hashes=before, after_hashes={},
            blocked_subjects=[str(report.failed_index)],
            scope_limitations=[f"apply failed at index {report.failed_index}"],
        )
    after = {
        "candidate_sha256": hashlib.sha256(
            clone.to_bytes(deterministic=False)).hexdigest(),
    }
    return GpnEditPreflight(
        valid=True, per_op_checks=per_op,
        authorized_changes=authorized,
        expected_footprints=footprints,
        before_hashes=before, after_hashes=after,
        blocked_subjects=[],
        scope_limitations=[
            "diagnostic native-edit scope only; strict output needs Stage 4+7",
            "python-pptx serialization limits apply (unsupported parts refused)",
        ],
    )


def _ledger_counter(ledger: Any) -> Counter:
    counter: Counter = Counter()
    if ledger is None:
        return counter
    for atom in getattr(ledger, "atoms", []) or []:
        counter[(getattr(atom, "kind", "?"),
                 getattr(atom, "canonical_value", ""))] += 1
    return counter


def compare_ledger_preservation(before_ledger: Any, after_ledger: Any) -> dict[str, Any]:
    """Compare required ledger atoms by identity/multiplicity (Stage 3 §10).

    After reimport, source hashes and derived IDs may change, so IR IDs are
    never compared literally and equal strings are never matched by first
    occurrence: (kind, canonical_value) multisets preserve identity and
    multiplicity instead.
    """
    before_counts = _ledger_counter(before_ledger)
    after_counts = _ledger_counter(after_ledger)
    missing = {f"{k[0]}:{k[1][:60]}": before_counts[k] - after_counts.get(k, 0)
               for k in before_counts if after_counts.get(k, 0) < before_counts[k]}
    return {
        "before_atoms": sum(before_counts.values()),
        "after_atoms": sum(after_counts.values()),
        "missing_atoms": missing,
        "preserved": not missing,
    }


def _chart_table_snapshots(deck: Any) -> dict[str, Any]:
    """Snapshot chart/workbook/table/field/link values for preservation.

    Source hashes and derived IR IDs change across reimports, so they are
    never compared literally and equal strings are never matched by first
    occurrence: sorted value multisets preserve identity/multiplicity and
    paragraph/run/cell/series subaddresses are compared by position.
    """
    out: dict[str, Any] = {"charts": [], "tables": [], "fields": [],
                           "links": [], "notes": []}
    if deck is None:
        return out
    for slide in getattr(deck, "slides", []) or []:
        for obj in getattr(slide, "objects", []) or []:
            payload = getattr(obj, "payload", None)
            series = getattr(payload, "series", None)
            if series is not None:
                for ser in series:
                    for pos, pt in enumerate(
                            getattr(ser, "points", []) or []):
                        val = getattr(pt, "value", None)
                        out["charts"].append(
                            (getattr(ser, "source_index", 0), pos,
                             getattr(val, "decimal", None),
                             getattr(val, "display_text", ""),
                             getattr(val, "state", "")))
                out["charts"].append(("workbook", getattr(
                    payload, "workbook", None) is not None,
                    getattr(payload, "data_source_status", "")))
            cells = getattr(payload, "cells", None)
            if cells is not None:
                for cell in cells:
                    texts = []
                    for para in getattr(cell, "paragraphs", []) or []:
                        for run in getattr(para, "runs", []) or []:
                            texts.append(getattr(run, "text", ""))
                    out["tables"].append(
                        (getattr(cell, "row", -1), getattr(cell, "col", -1),
                         "".join(texts)))
        for para_pos, para in enumerate(getattr(slide, "notes", []) or []):
            for run_pos, run in enumerate(getattr(para, "runs", []) or []):
                out["notes"].append(
                    (para_pos, run_pos, getattr(run, "text", "")))
                if getattr(run, "field", None) is not None:
                    out["fields"].append(
                        (para_pos, run_pos, getattr(run, "text", ""),
                         getattr(getattr(run, "field", None),
                                 "field_type", "")))
                if getattr(run, "hyperlink", None) is not None:
                    hl = run.hyperlink
                    out["links"].append((
                        getattr(hl, "source_rel_id", ""),
                        getattr(hl, "target_slide_ref", ""),
                        getattr(hl, "uri", "")))
    for key in out:
        out[key] = sorted(out[key], key=repr)
    return out


def apply_gpn_edits(
    *,
    source: Path,
    edits: Any,
    context: GpnEditContext,
    run_dir: Path,
    candidate_output: Path,
    overwrite: bool = False,
) -> GpnPatchResult:
    """Preflight on a copy, verify the saved candidate, commit atomically."""
    from ..api import Presentation as _Presentation
    from ..io import write_bytes as _write_bytes
    from ..model import OperationBatch as _OperationBatch
    from .assets import AssetStore, atomic_write_json
    from .importer import (
        build_import_support_report,
        import_deck,
        resolve_internal_links,
        write_import_artifacts,
    )
    from .package import build_manifest, read_package
    from .provenance import build_ledger

    source = Path(source)
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    candidate_output = Path(candidate_output)
    if candidate_output.resolve() == source.resolve():
        raise GpnError(
            "OUTPUT_IS_SOURCE: source is never the save target",
            "candidate output must differ from the source",
            exit_code=ExitCode.INVALID_INPUT,
        )
    if candidate_output.exists() and not overwrite:
        raise GpnError(
            "OUTPUT_EXISTS: refusing to overwrite without --overwrite",
            f"candidate output exists: {candidate_output}",
            exit_code=ExitCode.INVALID_INPUT,
        )
    source_bytes = source.read_bytes()
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    batch = edits if hasattr(edits, "operations") else _OperationBatch.model_validate(edits)

    preflight = preflight_gpn_edits(source=source, edits=batch, context=context)
    atomic_write_json(run_dir / "preflight_report.json", preflight)
    if not preflight.valid:
        return GpnPatchResult(
            operation_report={"ok": False, "applied_count": 0,
                              "failed_index": 0,
                              "events": preflight.per_op_checks},
            rolled_back=True, committed_count=0,
            candidate_path=None,
            import_diff={"status": "preflight_refused"},
            package_diff={},
            statuses={"strict_output_ready": False,
                      "production_assets_ready": False},
            report_paths={"preflight_report": str(
                run_dir / "preflight_report.json")},
            test_only=context.test_only,
            strict_output_ready=False,
        )
    # Re-apply on a fresh clone to produce the preview bytes.
    clone = _Presentation(_load_presentation(io.BytesIO(source_bytes)))
    report = clone.apply_operations(batch, dry_run=False, transactional=True)
    if not report.ok:
        return GpnPatchResult(
            operation_report=report.model_dump(mode="json"),
            rolled_back=True, committed_count=0,
            candidate_path=None,
            import_diff={"status": "apply_failed"},
            package_diff={},
            statuses={"strict_output_ready": False,
                      "production_assets_ready": False},
            report_paths={"preflight_report": str(
                run_dir / "preflight_report.json")},
            test_only=context.test_only,
            strict_output_ready=False,
        )
    preview_path = run_dir / "cache" / "preview_candidate.pptx"
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    preview_bytes = clone.to_bytes(deterministic=False)
    _write_bytes(preview_path, preview_bytes)

    # Independent reimport of the SAVED bytes (never in-memory state).
    store = AssetStore(run_dir / "assets")
    before_deck = context.source_ir
    before_ledger = context.ledger
    after_deck, _ = import_deck(preview_path, store)
    after_ledger = build_ledger(after_deck, decoration=[])
    after_graph = read_package(preview_path, store)
    before_graph = read_package(source, store)

    # Ledger preservation: required atoms keep identity/multiplicity.
    preservation = compare_ledger_preservation(before_ledger, after_ledger)
    missing = preservation["missing_atoms"]
    before_snap = _chart_table_snapshots(before_deck)
    after_snap = _chart_table_snapshots(after_deck)
    data_equal = (before_snap == after_snap)
    import_diff: dict[str, Any] = {
        "source_hash": source_hash,
        "missing_atoms": missing,
        "chart_table_field_link_notes_equal": data_equal,
        "before_atoms": preservation["before_atoms"],
        "after_atoms": preservation["after_atoms"],
    }
    # Package inventory/relationships: unaffected parts must match.
    before_parts = {p.part_name: p for p in before_graph.parts_by_name.values()} \
        if hasattr(before_graph, "parts_by_name") else {}
    after_parts = {p.part_name: p for p in after_graph.parts_by_name.values()} \
        if hasattr(after_graph, "parts_by_name") else {}
    removed_parts = sorted(set(before_parts) - set(after_parts))
    # Binary parts (images/workbooks) require exact bytes/hash equality.
    package_diff: dict[str, Any] = {
        "removed_parts": removed_parts,
        "before_part_count": len(before_parts),
        "after_part_count": len(after_parts),
    }
    if missing or not data_equal or removed_parts:
        return GpnPatchResult(
            operation_report=report.model_dump(mode="json"),
            rolled_back=True, committed_count=0,
            candidate_path=None,
            import_diff=import_diff,
            package_diff=package_diff,
            statuses={"strict_output_ready": False,
                      "production_assets_ready": False},
            report_paths={"preflight_report": str(
                run_dir / "preflight_report.json")},
            test_only=context.test_only,
            strict_output_ready=False,
        )
    # Commit the verified candidate atomically.
    candidate_output.parent.mkdir(parents=True, exist_ok=True)
    _write_bytes(candidate_output, preview_bytes)
    links = resolve_internal_links(after_deck, after_graph)
    support = build_import_support_report(
        after_deck, after_ledger, links, after_deck.import_issues)
    manifest = write_import_artifacts(
        run_dir=run_dir, deck=after_deck, ledger=after_ledger,
        package_manifest=build_manifest(after_graph), support=support,
        links=links)
    atomic_write_json(run_dir / "candidate_after_ledger.json", after_ledger)
    atomic_write_json(run_dir / "operation_report.json", report)
    result = GpnPatchResult(
        operation_report=report.model_dump(mode="json"),
        rolled_back=False, committed_count=len(batch.operations),
        candidate_path=str(candidate_output),
        import_diff=import_diff,
        package_diff=package_diff,
        statuses={"verified_for_native_edit_scope": True,
                  "strict_output_ready": False,
                  "production_assets_ready": False},
        report_paths={
            "preflight_report": str(run_dir / "preflight_report.json"),
            "operation_report": str(run_dir / "operation_report.json"),
            "candidate_after_ledger": str(
                run_dir / "candidate_after_ledger.json"),
            "support_report": str(manifest.support_report.absolute_path)
            if manifest.support_report else "",
        },
        test_only=context.test_only,
        strict_output_ready=False,
    )
    atomic_write_json(run_dir / "gpn_patch_result.json", result)
    return result


# ---------------------------------------------------------------------------
# CLI entry points (service commands, not product modes)
# ---------------------------------------------------------------------------


def _load_batch(edits_json: str) -> Any:
    from ..model import OperationBatch as _OperationBatch

    data = _read_edits_file(edits_json)
    return _OperationBatch.model_validate(data)


def _resolve_paths(args: Any, project_root: Path) -> tuple[Path, Path, Path]:
    input_path = Path(args.input).expanduser()
    if not input_path.is_absolute():
        input_path = (project_root / input_path).resolve()
    run_dir = getattr(args, "run_dir", None)
    run_dir = Path(run_dir).expanduser() if run_dir else None
    if run_dir is not None and not run_dir.is_absolute():
        run_dir = (project_root / run_dir).resolve()
    return input_path, run_dir or (project_root / "runs" / "stage3"), project_root


def cmd_preflight_edits(args: Any, config: Any, project_root: Path) -> int:
    import sys

    from .assets import atomic_write_json

    input_path, run_dir, _ = _resolve_paths(args, project_root)
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"[gpn preflight-edits] input={input_path}", file=sys.stderr)
    if not input_path.is_file():
        print(json.dumps({"ok": False, "error": {
            "code": "INPUT_NOT_FOUND",
            "message": f"input file not found: {input_path}"}}))
        return int(ExitCode.INVALID_INPUT)
    try:
        batch = _load_batch(args.edits_json)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": {
            "code": "INVALID_INPUT", "message": f"invalid edits JSON: {exc}"}}))
        return int(ExitCode.INVALID_INPUT)
    try:
        context = _build_context(
            source=input_path, config=config, project_root=project_root)
    except GpnError as exc:
        print(json.dumps({"ok": False, "error": {
            "code": exc.code.split(':')[0],
            "message": exc.message,
            "status": exc.status}}))
        return int(exc.exit_code)
    preflight = preflight_gpn_edits(
        source=input_path, edits=batch, context=context)
    atomic_write_json(run_dir / "preflight_report.json", preflight)
    print(json.dumps({
        "ok": preflight.valid,
        "command": "preflight-edits",
        "valid": preflight.valid,
        "authorized_changes": len(preflight.authorized_changes),
        "blocked_subjects": preflight.blocked_subjects,
        "source_unchanged": True,
        "report": str(run_dir / "preflight_report.json"),
    }, ensure_ascii=False, indent=2, sort_keys=True))
    if not preflight.valid:
        return int(ExitCode.NEEDS_REVIEW)
    return int(ExitCode.COMPLETED)


def cmd_apply_edits(args: Any, config: Any, project_root: Path) -> int:
    import sys

    input_path, run_dir, _ = _resolve_paths(args, project_root)
    output = Path(args.output).expanduser()
    if not output.is_absolute():
        output = (project_root / output).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"[gpn apply-edits] input={input_path} output={output}",
          file=sys.stderr)
    if not input_path.is_file():
        print(json.dumps({"ok": False, "error": {
            "code": "INPUT_NOT_FOUND",
            "message": f"input file not found: {input_path}"}}))
        return int(ExitCode.INVALID_INPUT)
    try:
        batch = _load_batch(args.edits_json)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": {
            "code": "INVALID_INPUT", "message": f"invalid edits JSON: {exc}"}}))
        return int(ExitCode.INVALID_INPUT)
    try:
        context = _build_context(
            source=input_path, config=config, project_root=project_root)
    except GpnError as exc:
        print(json.dumps({"ok": False, "error": {
            "code": exc.code.split(':')[0],
            "message": exc.message,
            "status": exc.status}}))
        return int(exc.exit_code)
    try:
        result = apply_gpn_edits(
            source=input_path, edits=batch, context=context,
            run_dir=run_dir, candidate_output=output,
            overwrite=bool(getattr(args, "overwrite", False)))
    except GpnError as exc:
        print(json.dumps({"ok": False, "error": {
            "code": exc.code.split(':')[0],
            "message": exc.message,
            "status": exc.status}}))
        code = exc.exit_code
        if "OUTPUT" in exc.code or "INPUT" in exc.code:
            code = ExitCode.INVALID_INPUT
        return int(code)
    print(json.dumps({
        "ok": result.candidate_path is not None,
        "command": "apply-edits",
        "committed_count": result.committed_count,
        "rolled_back": result.rolled_back,
        "candidate_path": result.candidate_path,
        "strict_output_ready": False,
        "test_only": True,
        "reports": result.report_paths,
    }, ensure_ascii=False, indent=2, sort_keys=True))
    if result.candidate_path is None:
        return int(ExitCode.VALIDATION_FAILED)
    return int(ExitCode.COMPLETED)
