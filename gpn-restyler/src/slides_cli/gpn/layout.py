"""Stage 5 §7: allocation, measured reflow, candidate feasibility/selection.

``resolve_layout`` takes a CompositionTree, resolves output styles from the
snapshot, measures through the injected measurer (real Chromium in production,
never a mock in acceptance paths), allocates extents with a deterministic
water-filling distributor, re-measures at assigned widths, and runs bounded
local attempts. Forbidden escapes (font shrink, fit_text/autofit, content
drop, split) are structurally unavailable: attempts come only from the
allowed list and styles are resolved once, never mutated for fit.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from .models import (
    LayoutAttempt,
    LayoutResolutionResult,
    MeasureComponent,
    MeasurementRequest,
)

ALLOWED_ATTEMPTS = (
    "expand_into_free",
    "adjust_shares",
    "reflow_paragraphs",
    "reorient",
    "switch_candidate",
)

ENGINE_VERSION = "layout/1.7"
# Cache contract (§11.2): EVERY behavior change in this module MUST bump
# ENGINE_VERSION — the layout cache key carries it, and a stale hit would
# silently resurrect pre-fix geometry (no code-change detection exists).
# 1.3: preserve_structure no longer keeps source boxes; both modes allocate
# target geometry from intent zone hints with measured fit (stage6 repair
# of the 249/327 source-box resurrection seen in Oct-11 measured output).
# 1.4: residual-overlap gate skips passthrough-decor pairs (confirmed
# text-less decor keeps its explicit source arrangement; failing on it
# would be a false infeasible).
# 1.5: measurement coverage gaps fail (missing components in a response
# are MEASUREMENT_COVERAGE_GAP, never silent 12pt defaults).
# 1.6: composition children keep reading order (sequence no_resort for
# same-zone stacking/repair priority); tree hash changes invalidate cache.
# 1.7: preserve_structure shares the recompose component builders (the
# mode branch was lost in 1.3, leaving KIND_UNMEASURED for every text
# component under preserve_structure).


def _content_box_pt(profile: Any, rules: Any) -> tuple[dict[str, float], list[str]]:
    from .planning_bridge import _resolve_content_box

    box_in, _verified, _source, box_issues = _resolve_content_box(profile, rules)
    if box_in:
        return {
            "x": box_in.get("x", 0.5) * 72.0,
            "y": box_in.get("y", 0.7) * 72.0,
            "w": box_in.get("w", 9.0) * 72.0,
            "h": box_in.get("h", 4.5) * 72.0,
        }, list(box_issues)
    return {"x": 36.0, "y": 50.4, "w": 648.0, "h": 324.0}, [
        *box_issues, "no content box; engineering fallback box used"]


def resolve_component_style(style_role: str, rules: Any,
                            font_bundle: Any) -> dict[str, Any]:
    """Role -> exact measurement style (single path, snapshot-sourced).

    Unknown role is an error (never a fallback body style). Bold comes only
    from exact faces; a required synthetic bold without the exception rule
    is refused. Returns a MeasureRunStyle dict.
    """
    roles = getattr(rules, "roles", {}) or {}
    role = roles.get(style_role)
    if role is None:
        raise ValueError(f"STYLE_ROLE_UNBOUND: {style_role!r}")
    faces = list(getattr(role, "allowed_faces", []) or [])
    if not faces:
        raise ValueError(f"STYLE_ROLE_NO_FACE: {style_role!r}")
    bindings = {b.role_or_face: b for b in (
        getattr(font_bundle, "bindings", []) or [])}
    binding = bindings.get(faces[0])
    if binding is None:
        raise ValueError(f"STYLE_FACE_UNBOUND: face {faces[0]!r} of role "
                         f"{style_role!r} has no measurement binding")
    size = getattr(role, "default_size_pt", None)
    if not size or size <= 0:
        raise ValueError(f"STYLE_SIZE_UNBOUND: role {style_role!r} has no "
                         "positive size")
    colors = dict(getattr(rules, "colors", {}) or {})
    color_tokens = list(getattr(role, "color_tokens", []) or [])
    color = ""
    if color_tokens:
        token = color_tokens[0]
        bare = token.strip().lstrip("#").upper()
        if len(bare) == 6 and all(ch in "0123456789ABCDEF" for ch in bare):
            color = bare
        elif token in colors:
            color = str(colors[token]).lstrip("#").upper()
        else:
            raise ValueError(f"STYLE_TOKEN_UNKNOWN: {token!r}")
    return {
        "face_alias": binding.browser_alias,
        "size_pt": float(size),
        "bold": False,
        "italic": False,
        "underline": "",
        "color_rgb": color,
        "language": "",
        "baseline_shift": "none",
        "uppercase": bool(getattr(role, "uppercase", False)),
    }


def extract_text_paragraphs(obj: Any) -> list[dict[str, Any]]:
    """Source text payload -> run-segmented paragraphs (text preserved byte
    for byte; field runs contribute their cached output value; soft breaks
    stay inside run text)."""
    payload = getattr(obj, "payload", None)
    paragraphs = getattr(payload, "paragraphs", None) or []
    out: list[dict[str, Any]] = []
    for para in paragraphs:
        runs = []
        for run in list(getattr(para, "runs", []) or []):
            text = getattr(run, "text", "") or ""
            if getattr(run, "kind", "text") == "field":
                meta = getattr(run, "field", None)
                cached = getattr(meta, "cached_text", "") if meta else ""
                text = cached if cached else text
            runs.append({"id": str(getattr(run, "id", "")),
                         "text": text, "style": {}})
        out.append({
            "id": str(getattr(para, "id", "")),
            "runs": runs,
            "space_before_pt": float(getattr(para, "space_before_pt", 0.0) or 0.0),
            "space_after_pt": float(getattr(para, "space_after_pt", 0.0) or 0.0),
            "line_spacing_mode": "multiple",
            "line_spacing_value": 1.15,
            "alignment": "left",
            "level": int(getattr(para, "level", 0) or 0),
            "list_kind": str(getattr(para, "list_kind", "none") or "none"),
            "first_line_indent_pt": 0.0,
            "left_indent_pt": 0.0,
        })
    return out


def _object_paragraphs(obj: Any) -> list[dict[str, Any]]:
    """Paragraphs of a text object or a text-bearing shape (same byte
    preservation contract as extract_text_paragraphs)."""
    payload = getattr(obj, "payload", None)
    paragraphs = getattr(payload, "paragraphs", None)
    if paragraphs is None:
        inner = getattr(payload, "text", None)
        paragraphs = getattr(inner, "paragraphs", None) if inner else []
    out: list[dict[str, Any]] = []
    for para in list(paragraphs or []):
        runs = []
        for run in list(getattr(para, "runs", []) or []):
            text = getattr(run, "text", "") or ""
            if getattr(run, "kind", "text") == "field":
                meta = getattr(run, "field", None)
                cached = getattr(meta, "cached_text", "") if meta else ""
                text = cached if cached else text
            runs.append({"id": str(getattr(run, "id", "")),
                         "text": text, "style": {}})
        out.append({
            "id": str(getattr(para, "id", "")),
            "runs": runs,
            "space_before_pt": float(getattr(para, "space_before_pt", 0.0) or 0.0),
            "space_after_pt": float(getattr(para, "space_after_pt", 0.0) or 0.0),
            "line_spacing_mode": "multiple",
            "line_spacing_value": 1.15,
            "alignment": "left",
            "level": int(getattr(para, "level", 0) or 0),
            "list_kind": str(getattr(para, "list_kind", "none") or "none"),
            "first_line_indent_pt": 0.0,
            "left_indent_pt": 0.0,
        })
    return out


def _source_box_pt(obj: Any, floor: float = 1.0) -> dict[str, float] | None:
    """Source local box in Pt, or None when the object has no saved box."""
    from .units import emu_to_pt

    box = getattr(obj, "local_box", None)
    if box is None:
        return None
    try:
        return {"x": emu_to_pt(box.x), "y": emu_to_pt(box.y),
                "w": max(emu_to_pt(box.w), floor),
                "h": max(emu_to_pt(box.h), floor)}
    except (TypeError, ValueError, AttributeError):
        return None


def _clamp_box(box: dict[str, float],
               content_box: dict[str, float]) -> dict[str, float]:
    """Clamp a hint box into the content box (never invents space)."""
    x1 = min(max(box["x"], content_box["x"]),
             content_box["x"] + content_box["w"] - 1.0)
    y1 = min(max(box["y"], content_box["y"]),
             content_box["y"] + content_box["h"] - 1.0)
    return {"x": x1, "y": y1,
            "w": max(min(box["w"], content_box["x"] + content_box["w"] - x1),
                     1.0),
            "h": max(min(box["h"], content_box["y"] + content_box["h"] - y1),
                     1.0)}


def _has_hint(node: Any) -> bool:
    """Whether the intent gave this component an explicit zone box."""
    for c in list(getattr(node, "constraints", []) or []):
        if (getattr(c, "kind", "") == "relative_box"
                and isinstance(getattr(c, "value", None), dict)
                and float((getattr(c, "value", None) or {}).get("w", 0) or 0) > 0):
            return True
    return False


def _boxes_overlap(a: dict[str, float], b: dict[str, float],
                   slop: float = 1.0) -> bool:
    return (a["x"] < b["x"] + b["w"] - slop
            and b["x"] < a["x"] + a["w"] - slop
            and a["y"] < b["y"] + b["h"] - slop
            and b["y"] < a["y"] + a["h"] - slop)


def _resolve_hint_overlaps(
        boxes: dict[str, dict[str, float]], order: list[str],
        content_box: dict[str, float],
        allowed: set[frozenset] | None = None,
        obstacles: dict[str, dict[str, float]] | None = None,
) -> tuple[dict[str, dict[str, float]], list[dict[str, Any]]]:
    """Deterministic overlap repair: later boxes shift below earlier ones.

    Bounded and recorded; when a shift would leave the content box the
    pair is reported, never silently stacked out of view. Exact-pair
    ``allowed`` container allowances (nest) are never moved apart.
    """
    allowed = allowed or set()
    obstacles = obstacles or {}
    moves: list[dict[str, Any]] = []
    bottom = content_box["y"] + content_box["h"]

    def _shift(cid: str, box: dict[str, float],
               blockers: list[tuple[str, dict[str, float]]]) -> dict[str, float]:
        for prev, other in blockers:
            if frozenset((cid, prev)) in allowed:
                continue
            guard = 0
            while _boxes_overlap(box, other) and guard < 50:
                guard += 1
                new_y = other["y"] + other["h"] + 7.2
                if new_y + box["h"] > bottom + 0.75:
                    moves.append({"action": "resolve_overlap",
                                  "target": cid, "obstacle": prev,
                                  "result": "no_room_below"})
                    break
                moves.append({"action": "resolve_overlap", "target": cid,
                              "obstacle": prev,
                              "from_y": round(box["y"], 2),
                              "to_y": round(new_y, 2)})
                box = {**box, "y": new_y}
        return box

    for pos, cid in enumerate(order):
        box = boxes[cid]
        box = _shift(cid, box, [(prev, boxes[prev]) for prev in order[:pos]])
        box = _shift(cid, box, sorted(obstacles.items(),
                                      key=lambda kv: kv[1]["y"]))
        boxes[cid] = box
    return boxes, moves


def distribute_extent(available: float, children: list[dict[str, Any]],
                      gaps: list[float]) -> dict[str, float] | None:
    """Weighted water-filling: minima first, rest by preferred weights with
    saturation. Returns {child_id: extent} or None when minima don't fit."""
    total_gap = sum(gaps)
    minima = {c["id"]: c["minimum"] for c in children}
    if sum(minima.values()) + total_gap > available:
        return None
    alloc = dict(minima)
    remaining = available - sum(minima.values()) - total_gap
    weights = {c["id"]: max(c.get("weight", 1.0), 0.0) for c in children}
    maxima = {c["id"]: c.get("maximum", float("inf")) for c in children}
    # saturate in weight order until the remainder is placed
    active = {cid for cid, w in weights.items() if w > 0}
    guard = 0
    while remaining > 1e-9 and active and guard < 1000:
        guard += 1
        w_sum = sum(weights[cid] for cid in active)
        if w_sum <= 0:
            break
        placed = 0.0
        for cid in sorted(active):
            share = remaining * weights[cid] / w_sum
            room = maxima[cid] - alloc[cid]
            give = min(share, max(room, 0.0))
            alloc[cid] += give
            placed += give
            if alloc[cid] >= maxima[cid] - 1e-9:
                active.discard(cid)
        remaining -= placed
        if placed <= 1e-9:
            break
    return alloc


def _layout_cache_key(tree: Any, rules: Any, profile: Any, font_bundle: Any,
                      options: dict[str, Any]) -> dict[str, str]:
    import locale
    import platform
    import sys

    return {
        "source_hash": getattr(tree, "source_hash", ""),
        "ontology_snapshot": getattr(tree, "ontology_snapshot_hash", ""),
        "template_hash": getattr(tree, "template_hash", ""),
        "candidate_id": getattr(tree, "candidate_id", ""),
        "tree_hash": hashlib.sha256(tree.model_dump_json().encode()).hexdigest(),
        "operator_bindings": ENGINE_VERSION,
        "font_bundle": getattr(font_bundle, "bundle_hash", ""),
        "calibration_id": options.get("calibration_id", "uncalibrated-v1"),
        "options": json.dumps(
            {k: v for k, v in options.items() if k != "budget_tracker"},
            sort_keys=True, default=str),
        "platform": f"{sys.platform}-{platform.machine()}",
        "locale": locale.getlocale()[0] or "C",
    }


def _cache_dir(project_root: Any) -> Any:
    from pathlib import Path
    return Path(project_root) / "runs" / ".layout-cache"


def _hint_box(node: Any, content_box: dict[str, float]) -> dict[str, float]:
    for c in node.constraints:
        if (c.kind == "relative_box" and isinstance(c.value, dict)
                and c.value.get("w", 0) > 0):
            return {"x": float(c.value.get("x", 0)),
                    "y": float(c.value.get("y", 0)),
                    "w": float(c.value.get("w", 0)),
                    "h": float(c.value.get("h", 0))}
    return dict(content_box)


def _build_geometry_emu(tree: Any, current_boxes: dict[str, dict[str, float]],
                        content_box: dict[str, float],
                        pt_to_emu_fn: Any) -> dict[str, dict[str, int]]:
    geometry_emu: dict[str, dict[str, int]] = {}
    for cid, node in tree.components.items():
        box = current_boxes.get(cid, _hint_box(node, content_box))
        geometry_emu[node.payload_ref] = {
            "x": pt_to_emu_fn(box["x"]),
            "y": pt_to_emu_fn(box["y"]),
            "w": pt_to_emu_fn(box["w"]),
            "h": pt_to_emu_fn(box["h"]),
        }
    return geometry_emu


def _build_resolved_slide(tree: Any, options: dict[str, Any],
                           geometry_emu: dict[str, dict[str, int]],
                           flagged: bool,
                           passthrough_refs: list[str] | None = None,
                           table_layout_emu: dict[str, dict[str, list[int]]] | None = None,
                           ) -> dict[str, Any]:
    return {
        "slide_id": tree.slide_id,
        "source_slide_id": tree.slide_id,
        "intent_id": tree.candidate_id,
        "generation_origin": options.get("generation_origin",
                                          "harness_model"),
        "zones": [{"zone_id": f"zone:{cid}", "role": node.style_role,
                   "content_refs": node.content_refs}
                  for cid, node in tree.components.items()],
        "role_assignments": {f"zone:{cid}": node.style_role
                             for cid, node in tree.components.items()},
        "geometry_emu": geometry_emu,
        "diagnostic_geometry": False,
        "issues": ["preserve_overflow_flagged"] if flagged else [],
        "passthrough_refs": list(passthrough_refs or []),
        "table_layout_emu": dict(table_layout_emu or {}),
    }


def resolve_layout(
    tree: Any,
    source_slide: Any,
    ledger: Any,
    rules: Any,
    profile: Any,
    font_bundle: Any,
    measurer: Callable[[MeasurementRequest], dict[str, Any]],
    options: dict[str, Any] | None = None,
) -> LayoutResolutionResult:
    """Allocate measured geometry for one candidate (spec §7)."""

    from .models import CompositionTree
    from .units import pt_to_emu

    options = dict(options or {})
    max_attempts = int(options.get("max_local_layout_attempts", 6))
    project_root = options.get("project_root")
    use_cache = bool(options.get("use_cache", True))
    profile_kind = str(options.get("profile_kind", "production"))
    debug_dir = str(options.get("debug_output_dir", ""))
    kind_resolvers: dict[str, Any] = options.get("kind_resolvers", {}) or {}

    if not isinstance(tree, CompositionTree):
        tree = CompositionTree.model_validate(tree)

    tracker = options.get("budget_tracker")
    if isinstance(tracker, dict):
        spent = int(tracker.get("consumed", 0))
        total = int(tracker.get("max_total", max_attempts))
        if spent >= total:
            return LayoutResolutionResult(
                status="infeasible", attempts=[],
                unresolved=[f"BUDGET_EXHAUSTED: per-slide budget "
                            f"{spent}/{total} consumed across candidates; "
                            "new candidate_id does not reset it"],
                measurement_profile=profile_kind, evidence_hashes={})

    if use_cache and project_root is not None:
        key = _layout_cache_key(tree, rules, profile, font_bundle, options)
        key_hash = hashlib.sha256(
            json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()
        entry = _cache_dir(project_root) / key_hash / "result.json"
        if entry.is_file():
            try:
                cached = LayoutResolutionResult.model_validate_json(
                    entry.read_text(encoding="utf-8"))
                if cached.evidence_hashes.get("tree") == key["tree_hash"]:
                    return cached
            except Exception:  # noqa: BLE001 - corrupt cache recomputes
                pass

    content_box, box_issues = _content_box_pt(profile, rules)
    attempts: list[LayoutAttempt] = []
    unresolved: list[str] = list(box_issues)
    addressed: list[str] = []

    # 1. style resolution (once; never mutated for fit). Text-bearing
    # components need a bound corporate role; imageless decor, connectors
    # and images carry no runs and take no text style.
    objects = {o.id: o for o in (getattr(source_slide, "objects", []) or [])}
    run_styles: dict[str, dict[str, Any]] = {}
    needs_assets = False
    for cid, node in tree.components.items():
        obj = objects.get(node.payload_ref)
        carries_text = node.kind in ("text",) or (
            node.kind == "shape" and bool(_object_paragraphs(obj))
            if obj is not None else node.kind == "shape")
        if node.kind in ("image", "connector") or (
                node.kind == "shape" and not carries_text) or (
                obj is not None and getattr(obj, "kind", "") == "unknown"):
            # Opaque objects carry no runs (extracted text is preserved
            # byte-wise by the transplant path, never re-styled here).
            continue
        try:
            run_styles[cid] = resolve_component_style(
                node.style_role, rules, font_bundle)
        except ValueError as exc:
            msg = str(exc)
            unresolved.append(f"{cid}: {msg}")
            if "UNBOUND" in msg and "FACE" in msg:
                needs_assets = True
    if any("STYLE_ROLE_UNBOUND" in u for u in unresolved):
        return LayoutResolutionResult(
            status="needs_review", attempts=attempts, unresolved=unresolved,
            measurement_profile=profile_kind,
            evidence_hashes={"tree": hashlib.sha256(
                tree.model_dump_json().encode()).hexdigest()})
    if needs_assets or getattr(font_bundle, "missing_faces", []):
        return LayoutResolutionResult(
            status="needs_assets", attempts=attempts, unresolved=unresolved,
            measurement_profile=profile_kind, evidence_hashes={})

    layout_mode = str(options.get("layout_mode", "recompose"))
    if layout_mode not in ("recompose", "preserve_structure"):
        return LayoutResolutionResult(
            status="needs_review", attempts=attempts,
            unresolved=[*unresolved,
                        f"LAYOUT_MODE_UNKNOWN: {layout_mode}"],
            measurement_profile=profile_kind, evidence_hashes={})
    if layout_mode == "recompose":
        # Source-box containment allowances describe the old arrangement;
        # after relocation only intent-declared nest pairs stay in force.
        # A stale allowance would launder a real occlusion into a pass.
        kept = [c for c in list(getattr(tree, "constraints", []) or [])
                if not (getattr(c, "kind", "") == "allowed_overlap"
                        and getattr(c, "origin", "") == "source")]
        if len(kept) != len(list(getattr(tree, "constraints", []) or [])):
            tree = tree.model_copy(update={"constraints": kept})
            addressed.append("stale source allow-pairs pruned for recompose")
    elif layout_mode == "preserve_structure":
        # preserve_structure is no longer a silent fallback to source boxes.
        # It is treated as recompose with the same target geometry rules.
        addressed.append("preserve_structure mode: using recompose geometry rules")

    def _allowed_cid_pairs() -> set[frozenset]:
        """Exact-pair nest allowances as component-id pairs (both modes)."""
        ref_of = {cid: node.payload_ref
                  for cid, node in tree.components.items()}
        pairs: set[frozenset] = set()
        for constraint in list(getattr(tree, "constraints", []) or []):
            if getattr(constraint, "kind", "") != "allowed_overlap":
                continue
            targets = list(getattr(constraint, "target_ids", []) or [])
            cid_pair = [cid for cid, ref in ref_of.items() if ref in targets]
            if len(cid_pair) == 2:
                pairs.add(frozenset(cid_pair))
        return pairs

    def _residual_overlaps(
            boxes: dict[str, dict[str, float]]) -> list[list[str]]:
        """Non-allowed pairwise overlaps in the final allocation.

        Passthrough-decor pairs are skipped: confirmed text-less decor
        keeps its source arrangement (explicit scene choice); a gate
        failing on that pre-existing arrangement would be a false
        infeasible. Mover-vs-decor pairs still fail (movers must avoid
        obstacles via repair).
        """
        allowed = _allowed_cid_pairs()
        ids = sorted(boxes)
        bad: list[list[str]] = []
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                if frozenset((ids[i], ids[j])) in allowed:
                    continue
                if ids[i] in passthrough_cids and ids[j] in passthrough_cids:
                    continue
                if _boxes_overlap(boxes[ids[i]], boxes[ids[j]], slop=0.75):
                    bad.append([ids[i], ids[j]])
        return bad

    # 2. intrinsic measurement at full content width
    components: dict[str, MeasureComponent] = {}
    preserve_boxes: dict[str, dict[str, float]] = {}
    # Recompose passthrough: decor/connector source boxes kept verbatim
    # (explicit, canvas-checked later — never reallocated for fit).
    passthrough_cids: set[str] = set()
    # §8 tables: resolved grids (EMU) keyed by source object id. Tables
    # measure cells through layout_table (own bridge calls); their boxes
    # are fixed geometry, verified by the saved-bytes grid gate.
    table_layout_emu: dict[str, dict[str, list[int]]] = {}
    font_assets = [{"alias": b.browser_alias, "path": b.path,
                    "sha256": b.sha256, "face_index": b.face_index,
                    "style": "normal", "weight": 400}
                   for b in (getattr(font_bundle, "bindings", []) or [])]

    def _table_styles() -> dict[str, dict[str, Any]]:
        styles: dict[str, dict[str, Any]] = {}
        for _other_cid, other_node in tree.components.items():
            role = getattr(other_node, "style_role", "")
            if role and role in run_styles:
                styles.setdefault(role, dict(run_styles[role]))
        for fallback in ("body", "dense_body_or_caption"):
            if fallback not in styles:
                try:
                    styles[fallback] = resolve_component_style(
                        fallback, rules, font_bundle)
                except ValueError:
                    continue
        return styles

    def _resolve_table(cid: str, node: Any, obj: Any) -> bool:
        """Measure the full grid; True when resolved dims are stored."""
        from . import primitives as _prim
        from .units import pt_to_emu as _pt_to_emu

        box = _clamp_box(_hint_box(node, content_box), content_box)
        res = _prim.layout_table(
            getattr(obj, "payload", None), box, _table_styles(), measurer, {
                "profile_kind": profile_kind, "font_assets": font_assets,
                "font_allowlist_roots": options.get("font_allowlist_roots",
                                                    []),
                "debug_output_dir": debug_dir})
        preserve_boxes[cid] = dict(box)
        if not res.get("ok"):
            unresolved.append(
                f"TABLE_UNRESOLVED: {cid}: "
                f"{'; '.join(res.get('issues', ['unknown'])[:3])}; "
                "source grid kept, flagged")
            return False
        table_layout_emu[node.payload_ref] = {
            "column_widths_emu": [int(_pt_to_emu(w))
                                  for w in res["column_widths_pt"]],
            "row_heights_emu": [int(_pt_to_emu(h))
                                for h in res["row_heights_pt"]]}
        # The resolved frame packs rows top-anchored at measured heights;
        # leftover box slack is air below, never stretched rows.
        content_h = sum(float(h) for h in res["row_heights_pt"])
        if content_h > 0:
            preserve_boxes[cid] = {**box, "h": content_h}
        return True

    for cid, node in tree.components.items():
        obj = objects.get(node.payload_ref)
        if obj is None:
            unresolved.append(f"OBJECT_GONE: {node.payload_ref}")
            continue
        kind = node.kind
        if kind == "table":
            # Grid resolves through layout_table (own measured bridge
            # calls); the box is fixed geometry in both modes.
            _resolve_table(cid, node, obj)
            continue
        if kind in kind_resolvers:
            components[cid] = kind_resolvers[kind](
                node, obj, content_box, run_styles[cid])
        elif layout_mode == "recompose" or layout_mode == "preserve_structure":
            # Hint-directed recompose: the intent zone hint is the initial
            # box (clamped into the content box); measurement decides fit.
            # preserve_structure uses the same target geometry rules
            # (see the mode note above): no silent source-box fallback.
            style = run_styles.get(cid)
            hint = _clamp_box(_hint_box(node, content_box), content_box)
            if kind == "image":
                if not _has_hint(node):
                    # Zoneless image: explicit source-box passthrough, same
                    # honesty contract as decor (canvas-checked, never fit).
                    src_box = _source_box_pt(obj)
                    if src_box is None:
                        unresolved.append(f"IMAGE_BOX_UNKNOWN: {cid}")
                        continue
                    comp = MeasureComponent(
                        id=cid, kind="text", payload_ref=node.payload_ref,
                        paragraphs=[], box_pt={**src_box,
                                               "w": max(src_box["w"], 1.0),
                                               "h": max(src_box["h"], 1.0)},
                        margins_pt={}, vertical_anchor="top",
                        wrap_policy="wrap", rotation_deg=0.0, list_preview={})
                    components[cid] = comp
                    preserve_boxes[cid] = dict(src_box)
                    passthrough_cids.add(cid)
                    continue
                natural = (options.get("image_natural_pt", {}) or {}).get(
                    node.payload_ref, {})
                from . import primitives as _prim2

                fitted = _prim2.layout_image_region(
                    getattr(obj, "payload", None), hint,
                    {"image_natural_size_pt": natural,
                     "image_fit": "fit", "permitted_crop": (0, 0, 0, 0)})
                box_pt = fitted.get("box_pt", hint)
                if not natural:
                    unresolved.append(
                        f"IMAGE_NATURAL_UNKNOWN: {cid} keeps hint box; "
                        "aspect fit unverified")
                comp = MeasureComponent(
                    id=cid, kind="text", payload_ref=node.payload_ref,
                    paragraphs=[], box_pt=box_pt, margins_pt={},
                    vertical_anchor="top", wrap_policy="wrap",
                    rotation_deg=0.0, list_preview={})
                components[cid] = comp
                preserve_boxes[cid] = dict(box_pt)
            elif kind == "shape" and not _object_paragraphs(obj):
                # Text-less shape: an explicit intent zone relocates it
                # (nest container / moved decor); without a zone it stays
                # at its source box (passthrough, canvas-checked).
                if _has_hint(node):
                    style = run_styles.get(cid)
                    hint = _clamp_box(_hint_box(node, content_box),
                                      content_box)
                    comp = MeasureComponent(
                        id=cid, kind="text", payload_ref=node.payload_ref,
                        paragraphs=[], box_pt=dict(hint), margins_pt={},
                        vertical_anchor="top", wrap_policy="wrap",
                        rotation_deg=0.0, list_preview={})
                    components[cid] = comp
                    preserve_boxes[cid] = dict(hint)
                    continue
                src_box = _source_box_pt(obj)
                if src_box is None:
                    unresolved.append(f"DECOR_BOX_UNKNOWN: {cid} has no "
                                      "source box; cannot place honestly")
                    continue
                comp = MeasureComponent(
                    id=cid, kind="text", payload_ref=node.payload_ref,
                    paragraphs=[], box_pt={**src_box,
                                           "w": max(src_box["w"], 1.0),
                                           "h": max(src_box["h"], 1.0)},
                    margins_pt={}, vertical_anchor="top",
                    wrap_policy="wrap", rotation_deg=0.0, list_preview={})
                components[cid] = comp
                preserve_boxes[cid] = dict(src_box)
                passthrough_cids.add(cid)
            elif kind == "connector":
                src_box = _source_box_pt(obj, floor=0.0)
                if src_box is None:
                    unresolved.append(f"CONNECTOR_BOX_UNKNOWN: {cid}")
                    continue
                comp = MeasureComponent(
                    id=cid, kind="text", payload_ref=node.payload_ref,
                    paragraphs=[], box_pt={**src_box,
                                           "w": max(src_box["w"], 1.0),
                                           "h": max(src_box["h"], 1.0)},
                    margins_pt={}, vertical_anchor="top",
                    wrap_policy="wrap", rotation_deg=0.0, list_preview={})
                components[cid] = comp
                preserve_boxes[cid] = dict(src_box)
                passthrough_cids.add(cid)
            elif kind in ("text", "shape"):
                if style is None:
                    unresolved.append(f"STYLE_UNRESOLVED: {cid}")
                    continue
                paras = _object_paragraphs(obj)
                for para in paras:
                    for run in para["runs"]:
                        run["style"] = dict(style)
                comp = MeasureComponent(
                    id=cid, kind="text", payload_ref=node.payload_ref,
                    paragraphs=paras, box_pt=dict(hint),
                    margins_pt={"l": 7.2, "r": 7.2, "t": 3.6, "b": 3.6},
                    vertical_anchor="top", wrap_policy="wrap",
                    rotation_deg=0.0, list_preview={})
                components[cid] = comp
                preserve_boxes[cid] = dict(hint)
            else:
                unresolved.append(
                    f"KIND_UNMEASURED: {kind} component {cid} has no "
                    "recompose resolver; needs Block 4 binding, not a "
                    "guessed box")
        else:
            unresolved.append(
                f"KIND_UNMEASURED: {kind} component {cid} has no resolver; "
                "needs Block 4 binding, not a guessed box")

    if unresolved and not components:
        return LayoutResolutionResult(
            status="needs_review", attempts=attempts, unresolved=unresolved,
            measurement_profile=profile_kind, evidence_hashes={})

    request = MeasurementRequest(
        schema_version="1.0",
        request_id=f"layout-{tree.candidate_id}",
        canvas_pt={"w": 960.0, "h": 540.0},
        css_px_per_in=96.0,
        profile_kind=profile_kind,  # type: ignore[arg-type]
        debug_output_dir=debug_dir,
        components=list(components.values()),
        font_assets=font_assets,
        engine_options={"timeout_ms": 120000, "device_scale_factor": 1,
                        "allowlist_roots": options.get("font_allowlist_roots", [])},
        input_hashes={})
    try:
        response = measurer(request)
    except Exception as exc:
        return LayoutResolutionResult(
            status="needs_review", attempts=attempts,
            unresolved=[*unresolved, f"MEASUREMENT_FAILED: {exc}"],
            measurement_profile=profile_kind, evidence_hashes={})
    items = {i["component_id"]: i for i in response.get("items", [])}
    # §10#6: a non-empty expected set with a missing/empty component
    # response is fail/unknown, never a pass with silent defaults.
    missing_items = sorted(set(components) - set(items))
    if missing_items:
        return LayoutResolutionResult(
            status="needs_review", attempts=attempts,
            unresolved=[*unresolved,
                        f"MEASUREMENT_COVERAGE_GAP: {missing_items[:4]} "
                        "requested but absent from response; no default "
                        "geometry trusted"],
            measurement_profile=profile_kind, evidence_hashes={})

    # 3. allocation: always use recompose geometry rules (target geometry
    # from intent zone hints, never source boxes).
    child_ids = list(tree.root.children)
    single_box = tree.root.organization in ("leaf", "overlay")
    alloc_boxes: dict[str, dict[str, float]] = {}
    overlap_moves: list[dict[str, Any]] = []
    if layout_mode == "recompose" or layout_mode == "preserve_structure":
        allowed_cids = _allowed_cid_pairs()
        movables = [c for c in child_ids
                    if c in components and c not in passthrough_cids]
        obstacles = {c: dict(preserve_boxes[c]) for c in child_ids
                     if c in passthrough_cids and c in preserve_boxes}
        # Fixed table boxes occupy space (own grid measurement already
        # ran); movables avoid them, never resize them here.
        tables_fixed = {c: dict(preserve_boxes[c]) for c in child_ids
                        if c not in components and c in preserve_boxes}
        obstacles.update(tables_fixed)
        alloc_boxes = {cid: dict(preserve_boxes.get(cid, content_box))
                       for cid in child_ids if cid in components}
        alloc_boxes.update(tables_fixed)
        # Same-zone siblings share one region: stack them vertically by
        # measured occupied heights (+ margins) inside the zone instead
        # of treating co-location as an occlusion.
        from collections import defaultdict as _dd

        groups: dict[tuple[float, ...], list[str]] = _dd(list)
        for c in movables:
            b = alloc_boxes[c]
            groups[(round(b["x"], 2), round(b["y"], 2),
                    round(b["w"], 2), round(b["h"], 2))].append(c)
        for key, members in groups.items():
            if len(members) < 2:
                continue
            zone_top = key[1]
            zone_bottom_limit = min(
                zone_top + key[3],
                content_box["y"] + content_box["h"] - 7.2)
            all_decor = all(
                not components[m].paragraphs and
                tree.components[m].kind == "shape"
                for m in members)
            # §6.3/§11.1: an explicitly declared nest cluster (container
            # plus the content it holds, every pair declared in the
            # accepted intent) keeps co-location. Without that contract a
            # shared box means "stack these", which is what destroys the
            # card/label composition.
            nest_cluster = all(
                frozenset((members[i], members[j])) in allowed_cids
                for i in range(len(members))
                for j in range(i + 1, len(members)))
            if all_decor or nest_cluster:
                for m in members:
                    alloc_boxes[m] = {"x": key[0], "y": zone_top,
                                      "w": key[2], "h": key[3]}
                    overlap_moves.append(
                        {"action": "decor_zone_stack", "target": m,
                         "zone_top": zone_top,
                         "relation": ("declared_nest_cluster"
                                      if nest_cluster else
                                      "decorative_overlap_allowed")})
                continue
            total_need = 0.0
            for m in members:
                margins = getattr(components[m], "margins_pt", {}) or {}
                need = float(items.get(m, {}).get(
                    "occupied_height_pt", 12.0) or 12.0)
                need += float(margins.get("t", 0.0) or 0.0) + float(
                    margins.get("b", 0.0) or 0.0)
                total_need += max(need, 1.0) + 7.2
            total_need -= 7.2
            if total_need > (zone_bottom_limit - zone_top) + 0.75:
                unresolved.append(
                    f"ZONE_OVERFLOW_REALLOC: same-zone group at "
                    f"({key[0]:.1f},{key[1]:.1f},{key[2]:.1f}x{key[3]:.1f}) "
                    f"needs {total_need:.1f}pt but only "
                    f"{zone_bottom_limit - zone_top:.1f}pt available; "
                    f"members={sorted(members)} reallocated across full "
                    "content height")
                y = zone_top
                for m in members:
                    margins = getattr(components[m], "margins_pt", {}) or {}
                    need = float(items.get(m, {}).get(
                        "occupied_height_pt", 12.0) or 12.0)
                    need += float(margins.get("t", 0.0) or 0.0) + float(
                        margins.get("b", 0.0) or 0.0)
                    need = max(need, 1.0)
                    alloc_boxes[m] = {"x": key[0], "y": y, "w": key[2],
                                      "h": need}
                    overlap_moves.append(
                        {"action": "realloc_overflow_zone", "target": m,
                         "zone_top": zone_top, "assigned_y": round(y, 2),
                         "assigned_h": round(need, 2)})
                    y += need + 7.2
                continue
            y = zone_top
            for m in members:
                margins = getattr(components[m], "margins_pt", {}) or {}
                need = float(items.get(m, {}).get(
                    "occupied_height_pt", 12.0) or 12.0)
                need += float(margins.get("t", 0.0) or 0.0) + float(
                    margins.get("b", 0.0) or 0.0)
                need = max(need, 1.0)
                available = zone_bottom_limit - y
                if need > available:
                    unresolved.append(
                        f"ZONE_OVERFLOW: {m} needs {need:.1f}pt in zone "
                        f"at y={y:.1f} but only {available:.1f}pt remains; "
                        "zone subdivision exceeds content box")
                    need = max(available, 1.0)
                alloc_boxes[m] = {"x": key[0], "y": y, "w": key[2],
                                  "h": need}
                overlap_moves.append(
                    {"action": "subdivide_zone", "target": m,
                     "zone_top": zone_top, "assigned_y": round(y, 2),
                     "assigned_h": round(need, 2)})
                y += need + 7.2
        alloc_boxes, repair_moves = _resolve_hint_overlaps(
            alloc_boxes, movables, content_box, allowed_cids, obstacles)
        overlap_moves.extend(repair_moves)
        if any(m.get("result") == "no_room_below" for m in overlap_moves):
            no_room = sorted({str(m.get("target")) for m in overlap_moves
                              if m.get("result") == "no_room_below"})
            unresolved.append(
                "HINT_OVERLAP_UNRESOLVED: "
                f"{no_room} cannot shift below obstacles inside the "
                "content box; intent hints need replan, not silent stacking")
    elif single_box or len(child_ids) <= 1:
        alloc_boxes = {cid: dict(content_box) for cid in child_ids}
    elif tree.root.organization in ("v-stack", "nest"):
        order = [c for c in child_ids if c in items]
        kids = [{"id": c,
                 "minimum": 12.0,
                 "preferred": max(items[c].get("occupied_height_pt", 12.0), 12.0),
                 "weight": 1.0} for c in order]
        gaps = [7.2] * max(len(order) - 1, 0)
        alloc = distribute_extent(content_box["h"], kids, gaps)
        if alloc is None:
            unresolved.append(
                "ALLOCATION_IMPOSSIBLE: stacked minima exceed content height")
            alloc_boxes = {}
        else:
            alloc_boxes = {}
            y = content_box["y"]
            for (i, c) in enumerate(order):
                alloc_boxes[c] = {"x": content_box["x"], "y": y,
                                  "w": content_box["w"], "h": alloc[c]}
                y += alloc[c] + (gaps[i] if i < len(gaps) else 0.0)
    elif tree.root.organization == "h-stack":
        order = [c for c in child_ids if c in items]
        kids = [{"id": c,
                 "minimum": max(items[c].get("intrinsic_min_width_pt", 24.0), 24.0),
                 "preferred": max(items[c].get("intrinsic_preferred_width_pt", 24.0), 24.0),
                 "weight": 1.0} for c in order]
        gaps = [7.2] * max(len(order) - 1, 0)
        alloc = distribute_extent(content_box["w"], kids, gaps)
        if alloc is None:
            unresolved.append(
                "ALLOCATION_IMPOSSIBLE: stacked minima exceed content width")
            alloc_boxes = {}
        else:
            alloc_boxes = {}
            x = content_box["x"]
            for (i, c) in enumerate(order):
                alloc_boxes[c] = {"x": x, "y": content_box["y"],
                                  "w": alloc[c], "h": content_box["h"]}
                x += alloc[c] + (gaps[i] if i < len(gaps) else 0.0)
    else:
        alloc_boxes = {cid: dict(content_box) for cid in child_ids}

    # 4. re-measure at assigned widths, bounded local attempts
    attempt_index = 0
    consumed = 0
    static_issues: list[str] = []
    current_boxes = dict(alloc_boxes)
    # Fixed table boxes: containment is checked here (content region —
    # tables are relocated content, never canvas passthrough).
    for cid, box in current_boxes.items():
        if cid in components:
            continue
        if (box["x"] < content_box["x"] - 0.75
                or box["y"] < content_box["y"] - 0.75
                or box["x"] + box["w"] > content_box["x"] + content_box["w"]
                + 0.75
                or box["y"] + box["h"] > content_box["y"] + content_box["h"]
                + 0.75):
            static_issues.append(f"{cid}: table box outside content box")
    hard_issues: list[str] = list(static_issues)

    def _measure_boxes(boxes: dict[str, dict[str, float]]) -> dict[str, Any]:
        from .models import RectPt

        comps = []
        for cid, box in boxes.items():
            if cid not in components:
                continue  # fixed geometry (tables): grid measured already
            base = components[cid].model_copy(
                update={"box_pt": RectPt(**box),
                        "id": f"{cid}-a{attempt_index}"})
            comps.append(base)
        retry = request.model_copy(update={
            "request_id": f"layout-{tree.candidate_id}-a{attempt_index}",
            "components": comps,
            # The retry is a new canonical request: drop the initial
            # hash so the bridge echoes this attempt's hash, never a
            # stale one (a mismatched echo must fail, not pass).
            "input_hashes": {}})
        resp = measurer(retry)
        got = {i["component_id"].rsplit("-a", 1)[0]: i
               for i in resp.get("items", [])}
        # §10#6: retry responses must cover every requested box; a
        # partial re-measure is a measurement failure, not a pass on
        # the returned subset.
        want = {cid for cid in boxes if cid in components}
        missing = sorted(want - set(got))
        if missing:
            raise ValueError(
                f"MEASUREMENT_COVERAGE_GAP: retry "
                f"{retry.request_id} "
                f"missing {missing[:4]}; no default geometry trusted")
        return got

    def _hard_issues(measured: dict[str, Any],
                     boxes: dict[str, dict[str, float]]) -> list[str]:
        found: list[str] = []
        canvas = {"x": 0.0, "y": 0.0, "w": 960.0, "h": 540.0}
        for cid, item in measured.items():
            if item.get("overflow_x"):
                found.append(f"{cid}: overflow_x "
                             f"+{item.get('excess_pt', {}).get('x', 0):.1f}pt")
            if item.get("overflow_y"):
                found.append(f"{cid}: overflow_y "
                             f"+{item.get('excess_pt', {}).get('y', 0):.1f}pt")
            box = boxes.get(cid, {})
            if box:
                # Passthrough decor lives at source geometry: permitted
                # region is the canvas, not the content box (§11.1).
                region = canvas if cid in passthrough_cids else content_box
                if box["x"] < region["x"] - 0.75 or box["y"] < region["y"] - 0.75:
                    found.append(f"{cid}: outside permitted origin")
                if box["x"] + box["w"] > region["x"] + region["w"] + 0.75:
                    found.append(f"{cid}: exceeds permitted width")
                if box["y"] + box["h"] > region["y"] + region["h"] + 0.75:
                    found.append(f"{cid}: exceeds permitted height")
        return found

    try:
        measured = _measure_boxes(current_boxes) if current_boxes else {}
    except Exception as exc:
        return LayoutResolutionResult(
            status="needs_review", attempts=attempts,
            unresolved=[*unresolved, f"MEASUREMENT_FAILED: {exc}"],
            measurement_profile=profile_kind, evidence_hashes={})
    hard_issues = _hard_issues(measured, current_boxes) + static_issues
    initial_changes: list[dict[str, Any]] = [{
        "action": "initial_allocation", "organization": tree.root.organization,
        "layout_mode": layout_mode}]
    if overlap_moves:
        initial_changes.append({"action": "resolve_overlap_batch",
                                "moves": overlap_moves})
    attempts.append(LayoutAttempt(
        candidate_id=tree.candidate_id, attempt_index=attempt_index,
        changes=initial_changes,
        measured_hash=hashlib.sha256(json.dumps(
            measured, sort_keys=True).encode()).hexdigest(),
        hard_issues=list(hard_issues),
        metrics={"overflow_count": float(len(hard_issues))},
        next_action="done" if not hard_issues else "expand_into_free",
        consumed_budget=consumed))

    while hard_issues and consumed < max_attempts:
        tall = any("exceeds content height" in h or "overflow_y" in h
                   for h in hard_issues)
        action = "expand_into_free" if tall else "adjust_shares"
        if action not in ALLOWED_ATTEMPTS:
            break
        consumed += 1
        attempt_index += 1
        grown: list[dict[str, Any]] = []
        if action == "expand_into_free" and layout_mode == "recompose":
            # Grow overflowing boxes down/right into genuinely free space
            # (no other box occupies the grown region); passthrough decor
            # counts as occupied. Nothing shrinks, nothing overlaps.
            for cid in list(current_boxes):
                if cid in passthrough_cids:
                    continue
                box = current_boxes[cid]
                need_y = any(f"{cid}: overflow_y" in h or
                             f"{cid}: exceeds" in h for h in hard_issues)
                need_x = any(f"{cid}: overflow_x" in h for h in hard_issues)
                if not (need_y or need_x):
                    continue
                others = [(o, b) for o, b in current_boxes.items()
                          if o != cid]
                if need_y:
                    limit = content_box["y"] + content_box["h"]
                    for _, ob in others:
                        if (ob["x"] < box["x"] + box["w"] - 1.0
                                and box["x"] < ob["x"] + ob["w"] - 1.0
                                and ob["y"] >= box["y"] + box["h"] - 1.0):
                            limit = min(limit, ob["y"] - 7.2)
                    if limit > box["y"] + box["h"] + 1.0:
                        grown.append({"cid": cid,
                                      "from_h": round(box["h"], 2),
                                      "to_h": round(limit - box["y"], 2)})
                        current_boxes[cid] = {**box,
                                              "h": limit - box["y"]}
                if need_x:
                    box = current_boxes[cid]
                    limit = content_box["x"] + content_box["w"]
                    for _, ob in others:
                        if (ob["y"] < box["y"] + box["h"] - 1.0
                                and box["y"] < ob["y"] + ob["h"] - 1.0
                                and ob["x"] >= box["x"] + box["w"] - 1.0):
                            limit = min(limit, ob["x"] - 7.2)
                    if limit > box["x"] + box["w"] + 1.0:
                        grown.append({"cid": cid,
                                      "from_w": round(box["w"], 2),
                                      "to_w": round(limit - box["x"], 2)})
                        current_boxes[cid] = {**box,
                                              "w": limit - box["x"]}
        # expand_into_free: no free space beyond the content box exists by
        # construction, so record the attempt and stop growing: the only
        # honest continuation is reflow/reorient/switch/infeasible.
        if action == "adjust_shares" and tree.root.organization == "h-stack":
            order = list(current_boxes)
            kids = [{"id": c, "minimum": 24.0,
                     "preferred": max(measured.get(c, {}).get(
                         "intrinsic_preferred_width_pt", 24.0), 24.0),
                     "weight": 2.0 if any(c in h for h in hard_issues) else 1.0}
                    for c in order]
            gaps = [7.2] * max(len(order) - 1, 0)
            alloc = distribute_extent(content_box["w"], kids, gaps)
            if alloc is not None:
                x = content_box["x"]
                for (i, c) in enumerate(order):
                    current_boxes[c] = {"x": x, "y": content_box["y"],
                                        "w": alloc[c], "h": content_box["h"]}
                    x += alloc[c] + (gaps[i] if i < len(gaps) else 0.0)
        try:
            measured = _measure_boxes(current_boxes) if current_boxes else {}
        except Exception as exc:
            unresolved.append(f"MEASUREMENT_FAILED: {exc}")
            break
        hard_issues = _hard_issues(measured, current_boxes) + static_issues
        change: dict[str, Any] = {"action": action}
        if grown:
            change["grown"] = grown
        attempts.append(LayoutAttempt(
            candidate_id=tree.candidate_id, attempt_index=attempt_index,
            changes=[change],
            measured_hash=hashlib.sha256(json.dumps(
                measured, sort_keys=True).encode()).hexdigest(),
            hard_issues=list(hard_issues),
            metrics={"overflow_count": float(len(hard_issues))},
            next_action="done" if not hard_issues else "switch_candidate",
            consumed_budget=consumed))
        if action == "expand_into_free":
            break

    if hard_issues:
        deficits = "; ".join(hard_issues[:6])
        return LayoutResolutionResult(
            status="infeasible", attempts=attempts,
            unresolved=[*unresolved,
                        f"INFEASIBLE: {deficits} "
                        f"(budget {consumed}/{max_attempts} exhausted; "
                        "content/styles/count unchanged)"],
            measurement_profile=profile_kind,
            evidence_hashes={"tree": hashlib.sha256(
                tree.model_dump_json().encode()).hexdigest()})

    # Final gate: residual non-allowed overlaps fail the allocation even
    # when every box measured clean — repair is best-effort, the gate is
    # exact (§11.1). Recompose refuses overlaps.
    residual = _residual_overlaps(current_boxes) if current_boxes else []
    if residual:
        pairs = "; ".join(f"{a}+{b}" for a, b in residual[:6])
        return LayoutResolutionResult(
            status="infeasible", attempts=attempts,
            unresolved=[*unresolved,
                        f"INFEASIBLE_OVERLAP: {pairs} "
                        f"(budget {consumed}/{max_attempts} exhausted; "
                        "content/styles/count unchanged)"],
            measurement_profile=profile_kind,
            evidence_hashes={"tree": hashlib.sha256(
                tree.model_dump_json().encode()).hexdigest()})

    # 5. feasible: build measured ResolvedSlide
    geometry_emu = _build_geometry_emu(
        tree, current_boxes, content_box, pt_to_emu)
    headroom = 0.0
    for cid, item in measured.items():
        box = current_boxes.get(cid, content_box)
        headroom += max(0.0, (box["y"] + box["h"]) - (
            content_box["y"] + item.get("occupied_height_pt", 0.0)))
    resolved_slide = _build_resolved_slide(
        tree, options, geometry_emu, False,
        [tree.components[c].payload_ref
         for c in passthrough_cids if c in tree.components],
        table_layout_emu)
    addressed.append(f"allocation:{tree.root.organization}")
    result = LayoutResolutionResult(
        status="resolved", resolved_slide=resolved_slide, attempts=attempts,
        addressed_issues=addressed,
        unresolved=unresolved, measurement_profile=profile_kind,
        evidence_hashes={
            "tree": hashlib.sha256(tree.model_dump_json().encode()).hexdigest(),
            "measured": attempts[-1].measured_hash if attempts else "",
            "score": f"{headroom:.2f}",
        })
    if use_cache and project_root is not None:
        key = _layout_cache_key(tree, rules, profile, font_bundle, options)
        key_hash = hashlib.sha256(
            json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()
        entry = _cache_dir(project_root) / key_hash
        entry.mkdir(parents=True, exist_ok=True)
        (entry / "manifest.json").write_text(json.dumps(
            {"key_hash": key_hash, "key": key}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        (entry / "result.json").write_text(
            result.model_dump_json(), encoding="utf-8")
    if isinstance(tracker, dict):
        tracker["consumed"] = int(tracker.get("consumed", 0)) + consumed
    return result