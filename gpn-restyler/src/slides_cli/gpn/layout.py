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
from .text_flow import measurement_paragraphs

ALLOWED_ATTEMPTS = (
    "expand_into_free",
    "adjust_shares",
    "reflow_paragraphs",
    "reorient",
    "switch_candidate",
)

ENGINE_VERSION = "layout/3.0-constraint"
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
# 1.7: connector footprints use the declared stroke envelope (§4.2).
# 1.9: a connector with an explicit intent zone hint is authored geometry
# and moves with its group; only an unplaced connector is a source-box
# obstacle. Previously every rule stayed at its source position and shifted
# the authored composition below it (§4.1/§4.2).
# 2.2: text-less vectors never consume body slots (no stacking sums,
# no inset-driven overflow flags; geometry/obstacles/region gates kept).
# 2.1: unified search loop — measurement overflows AND residual overlaps
# drive the loop together (growth iterations followed by resolve passes,
# residual-only iterations run the resolver). Post-expand residuals used
# to skip the loop and fail unretried (§5.5).
# 2.0: interleaved local search — after each expand_into_free, run another
# overlap-resolution pass on the current boxes and re-measure, bounded by
# the same attempt budget with a no-progress stop. Growth alone and
# resolve alone each strand micro-conflicts (a +3pt overflow under a
# shifted neighbour); the old code ran overlap-batch once pre-measure and
# a single expand post-measure, then gave up (§5.5).
# 1.8: members of one authored zone that are disjoint in the source are
# placed as a scaled cluster instead of a vertical stack with a per-object
# gap. Fragmented decoration (icon group, table rules, arrow pair) must not
# consume one body slot per source object (§5.2/§10.4).
# 1.7: preserve_structure shares the recompose component builders (the
# mode branch was lost in 1.3, leaving KIND_UNMEASURED for every text
# component under preserve_structure).


def _content_box_pt(profile: Any, rules: Any) -> tuple[dict[str, float], list[str]]:
    from .frame_contract import _resolve_content_box

    box_in, _verified, _source, box_issues = _resolve_content_box(profile, rules)
    if box_in:
        return {
            "x": box_in.get("x", 0.5) * 72.0,
            "y": box_in.get("y", 0.7) * 72.0,
            "w": box_in.get("w", 9.0) * 72.0,
            "h": box_in.get("h", 4.5) * 72.0,
        }, list(box_issues)
    return {"x": 36.0, "y": 50.4, "w": 648.0, "h": 324.0}, [
        *box_issues,
        "no content box; engineering fallback box used",
    ]


def resolve_component_style(style_role: str, rules: Any, font_bundle: Any) -> dict[str, Any]:
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
    bindings = {b.role_or_face: b for b in (getattr(font_bundle, "bindings", []) or [])}
    binding = bindings.get(faces[0])
    if binding is None:
        raise ValueError(
            f"STYLE_FACE_UNBOUND: face {faces[0]!r} of role "
            f"{style_role!r} has no measurement binding"
        )
    size = getattr(role, "default_size_pt", None)
    if not size or size <= 0:
        raise ValueError(f"STYLE_SIZE_UNBOUND: role {style_role!r} has no positive size")
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
            runs.append({"id": str(getattr(run, "id", "")), "text": text, "style": {}})
        out.append(
            {
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
            }
        )
    return out


def _object_paragraphs(obj: Any) -> list[dict[str, Any]]:
    """Keep source atom identities and non-text tokens in a rich block."""
    payload = getattr(obj, "payload", None)
    paragraphs = getattr(payload, "paragraphs", None)
    if paragraphs is None:
        paragraphs = getattr(getattr(payload, "text", None), "paragraphs", []) or []
    out = []
    for para in paragraphs:
        runs = []
        carriers = any("/br" in str(getattr(r, "id", "")) for r in para.runs)
        breaks = {}
        if not carriers:
            for br in getattr(para, "breaks", []) or []:
                breaks.setdefault(br.offset, []).append(br)

        def add_breaks(offset, *, _breaks=breaks, _runs=runs, _pid=para.id):
            for index, br in enumerate(_breaks.get(offset, [])):
                _runs.append(
                    {
                        "id": f"{_pid}/br{offset}-{index}",
                        "text": "\n",
                        "kind": "break",
                        "style": {},
                        "break_kind": br.kind,
                    }
                )

        add_breaks(0)
        for index, run in enumerate(para.runs, start=1):
            raw = run.model_dump(mode="json") if hasattr(run, "model_dump") else vars(run)
            kind = "break" if "/br" in str(raw.get("id", "")) else raw.get("kind", "text")
            field = raw.get("field") or {}
            text = field.get("cached_text") if kind == "field" else raw.get("text", "")
            runs.append(
                {
                    "id": str(raw.get("id", "")),
                    "text": text or raw.get("text", "") or "",
                    "kind": kind,
                    "field": raw.get("field"),
                    "hyperlink": raw.get("hyperlink"),
                    "semantic_marks": raw.get("semantic_marks", []),
                    "style": {},
                }
            )
            add_breaks(index)
        numbering = getattr(para, "numbering", None)
        out.append(
            {
                "id": para.id,
                "runs": runs,
                "space_before_pt": float(para.space_before_pt or 0),
                "space_after_pt": float(para.space_after_pt or 0),
                "line_spacing_mode": "multiple",
                "line_spacing_value": 1.15,
                "alignment": "left",
                "level": int(para.level or 0),
                "list_kind": para.list_kind,
                "numbering": numbering.model_dump(mode="json") if numbering else None,
                "background_token": getattr(para, "background_token", ""),
                "paragraph_text_token": getattr(para, "paragraph_text_token", ""),
                "first_line_indent_pt": float(getattr(para, "source_hanging_emu", 0) or 0) / 12700,
                "left_indent_pt": float(getattr(para, "source_indent_emu", 0) or 0) / 12700,
            }
        )
    return out


def _rich_text_paragraphs(
    objects: dict[str, Any],
    refs: list[str],
    ref_roles: dict[str, str] | None = None,
    paragraph_groups: list[list[str]] | None = None,
    inline_separator: str = " ",
) -> list[dict[str, Any]]:
    from .text_flow import build_rich_paragraphs

    return build_rich_paragraphs(objects, refs, ref_roles, paragraph_groups, inline_separator)


def _source_box_pt(obj: Any, floor: float = 1.0) -> dict[str, float] | None:
    """Source local box in Pt, or None when the object has no saved box."""
    from .units import emu_to_pt

    box = getattr(obj, "local_box", None)
    if box is None:
        return None
    try:
        return {
            "x": emu_to_pt(box.x),
            "y": emu_to_pt(box.y),
            "w": max(emu_to_pt(box.w), floor),
            "h": max(emu_to_pt(box.h), floor),
        }
    except (TypeError, ValueError, AttributeError):
        return None


def _clamp_box(box: dict[str, float], content_box: dict[str, float]) -> dict[str, float]:
    """Clamp a hint box into the content box (never invents space)."""
    x1 = min(max(box["x"], content_box["x"]), content_box["x"] + content_box["w"] - 1.0)
    y1 = min(max(box["y"], content_box["y"]), content_box["y"] + content_box["h"] - 1.0)
    return {
        "x": x1,
        "y": y1,
        "w": max(min(box["w"], content_box["x"] + content_box["w"] - x1), 1.0),
        "h": max(min(box["h"], content_box["y"] + content_box["h"] - y1), 1.0),
    }


def _has_hint(node: Any) -> bool:
    """Whether the intent gave this component an explicit zone box."""
    for c in list(getattr(node, "constraints", []) or []):
        if (
            getattr(c, "kind", "") == "relative_box"
            and isinstance(getattr(c, "value", None), dict)
            and float((getattr(c, "value", None) or {}).get("w", 0) or 0) > 0
        ):
            return True
    return False


def _boxes_overlap(a: dict[str, float], b: dict[str, float], slop: float = 1.0) -> bool:
    return (
        a["x"] < b["x"] + b["w"] - slop
        and b["x"] < a["x"] + a["w"] - slop
        and a["y"] < b["y"] + b["h"] - slop
        and b["y"] < a["y"] + a["h"] - slop
    )


def _cluster_placement(
    members: list[str],
    objects: dict[str, Any],
    payload_refs: dict[str, str],
    zone: dict[str, float],
    slop: float = 0.75,
) -> list[tuple[str, dict[str, float], str]] | None:
    """Place disjoint source-cluster members inside one authored zone.

    A designer draws an icon, its rules and a caption as separate objects
    that occupy *different* places inside one visual group. When such a
    group arrives as several members of a single authored zone, the honest
    placement keeps their intra-group geometry (scaled into the zone)
    instead of stacking them with a per-object gap — stacking is what makes
    a fragmented decoration look like dozens of body-text slots (§5.2,
    §10.4 fragmentation invariance).

    Returns ``None`` when the members are not a disjoint cluster (they
    overlap in the source, i.e. a real surface/child nest), so the caller
    keeps the nest/stack rules.
    """
    boxes: dict[str, dict[str, float]] = {}
    for cid in members:
        obj = objects.get(str(payload_refs.get(cid, "")))
        if obj is None:
            return None
        box = _source_box_pt(obj)
        if box is None:
            return None
        boxes[cid] = box
    ids = sorted(boxes)
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            if _boxes_overlap(boxes[a], boxes[b], slop=slop):
                return None
    span_w = max(b["x"] + b["w"] for b in boxes.values()) - min(b["x"] for b in boxes.values())
    span_h = max(b["y"] + b["h"] for b in boxes.values()) - min(b["y"] for b in boxes.values())
    if span_w <= 0 or span_h <= 0:
        return None
    if span_w > zone["w"] + 1.0 or span_h > zone["h"] + 1.0:
        # The cluster does not fit its authored zone: that is a real
        # authoring defect, not a licence to stack or to overflow.
        return None
    origin_x = min(b["x"] for b in boxes.values())
    origin_y = min(b["y"] for b in boxes.values())
    scale = min(zone["w"] / span_w, zone["h"] / span_h, 1.0)
    out: list[tuple[str, dict[str, float], str]] = []
    for cid, box in boxes.items():
        out.append(
            (
                cid,
                {
                    "x": zone["x"] + (box["x"] - origin_x) * scale,
                    "y": zone["y"] + (box["y"] - origin_y) * scale,
                    "w": max(box["w"] * scale, 1.0),
                    "h": max(box["h"] * scale, 1.0),
                },
                "disjoint_source_cluster",
            )
        )
    return out


def place_side_by_side(
    box: dict[str, float],
    blockers: list[tuple[str, dict[str, float]]],
    content_box: dict[str, float],
    allowed: set[frozenset],
) -> dict[str, float] | None:
    """Legal alternative placement when nothing fits below (spec §1.3).

    The downward-only helper cannot fit two 100x100 objects in a 400x100
    region, yet a row placement trivially fits. This searches free space
    beside/under the blockers in both axes and returns the first placement
    that is inside the content box and collides with nothing. It returns
    ``None`` only when no such legal placement exists — which is a scoped
    conflict, not a statement about how much text the slide contains.
    """

    def _free(candidate: dict[str, float]) -> bool:
        if (
            candidate["x"] < content_box["x"] - 0.75
            or candidate["y"] < content_box["y"] - 0.75
            or candidate["x"] + candidate["w"] > content_box["x"] + content_box["w"] + 0.75
            or candidate["y"] + candidate["h"] > content_box["y"] + content_box["h"] + 0.75
        ):
            return False
        return not any(_boxes_overlap(candidate, other) for _, other in blockers)

    gap = 7.2
    candidates: list[dict[str, float]] = []
    # Beside each blocker (left and right edges), then aligned to its top.
    for _, other in blockers:
        candidates.append(
            {"x": other["x"] - gap - box["w"], "y": box["y"], "w": box["w"], "h": box["h"]}
        )
        candidates.append(
            {"x": other["x"] + other["w"] + gap, "y": box["y"], "w": box["w"], "h": box["h"]}
        )
        candidates.append({"x": other["x"], "y": box["y"], "w": box["w"], "h": box["h"]})
        candidates.append(
            {"x": box["x"], "y": other["y"] + other["h"] + gap, "w": box["w"], "h": box["h"]}
        )
        candidates.append(
            {"x": box["x"], "y": other["y"] - gap - box["h"], "w": box["w"], "h": box["h"]}
        )
    # Region corners: translation only, the box keeps its measured size.
    candidates.append({"x": content_box["x"], "y": box["y"], "w": box["w"], "h": box["h"]})
    candidates.append(
        {
            "x": content_box["x"] + content_box["w"] - box["w"],
            "y": box["y"],
            "w": box["w"],
            "h": box["h"],
        }
    )
    for candidate in candidates:
        if _free(candidate):
            return candidate
    return None


def _stroke_width_pt(node: Any) -> float:
    """Declared stroke width for a path node (Pt), else 1.0."""
    for c in getattr(node, "constraints", []) or []:
        if getattr(c, "kind", "") == "stroke_footprint":
            value = getattr(c, "value", None)
            if isinstance(value, dict):
                try:
                    return max(float(value.get("stroke_width_pt", 1.0)), 0.1)
                except (TypeError, ValueError):
                    return 1.0
    return 1.0


def _stroke_widths_pt(tree: Any) -> dict[str, float]:
    """Per-component stroke widths for the path nodes of a tree."""
    return {
        cid: _stroke_width_pt(node)
        for cid, node in getattr(tree, "components", {}).items()
        if getattr(node, "kind", "") == "connector"
    }


def _solver_budget(options: dict[str, Any], node_count: int) -> float:
    """Wall-clock CP-SAT budget that scales with the measured model size.

    Pairwise disjunctions grow quadratically with the number of placed
    components, so a fixed few-second budget returns OPTIMAL/UNKNOWN by
    timeout on dense slides. An explicit ``solver_seconds`` still wins.
    """
    explicit = options.get("solver_seconds")
    if explicit is not None:
        return float(explicit)
    return max(5.0, min(60.0, 0.5 * float(node_count)))


def _resolve_hint_overlaps(
    boxes: dict[str, dict[str, float]],
    order: list[str],
    content_box: dict[str, float],
    allowed: set[frozenset] | None = None,
    obstacles: dict[str, dict[str, float]] | None = None,
    node_kinds: dict[str, str] | None = None,
    stroke_widths: dict[str, float] | None = None,
) -> tuple[dict[str, dict[str, float]], list[dict[str, Any]]]:
    """Bounded global placement, including free positions above and beside."""
    from .constraint_layout import solve_measured_boxes

    full = {**(obstacles or {}), **boxes}
    result = solve_measured_boxes(
        full,
        content_box,
        movable=[cid for cid in order if cid not in (obstacles or {})],
        allowed_pairs=allowed,
        container_ids={cid for cid, kind in (node_kinds or {}).items() if kind == "shape"},
        seconds=_solver_budget({}, len(full)),
    )
    move = {"action": "constraint_layout", **result.stats}
    if result.boxes:
        return {cid: result.boxes[cid] for cid in boxes}, [move]
    return dict(boxes), [{**move, "result": "constraint_unsatisfied"}]


def _fmt_box(box: dict[str, float]) -> str:
    """Compact box evidence for a conflict message (x,y,w,h in Pt)."""
    try:
        return (
            f"{float(box['x']):.0f},{float(box['y']):.0f},"
            f"{float(box['w']):.0f}x{float(box['h']):.0f}"
        )
    except (KeyError, TypeError, ValueError):
        return "?"


def distribute_extent(
    available: float, children: list[dict[str, Any]], gaps: list[float]
) -> dict[str, float] | None:
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


def _style_size_pt(style: Any) -> float:
    """Declared run size in Pt; 0.0 when the style carries none.

    Run styles arrive either as a plain dict or as the typed
    ``MeasureRunStyle``, depending on the construction site, so the size is
    read through one accessor instead of a shape assumption.
    """
    if style is None:
        return 0.0
    for name in ("size_pt", "size"):
        value = style.get(name) if isinstance(style, dict) else getattr(style, name, None)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return 0.0


def _text_frame_box(
    hint: dict[str, float], style: Any, margins: dict[str, float], content_box: dict[str, float]
) -> dict[str, float]:
    """Authored zone hint -> a frame that can physically hold one line.

    A scene authored in percent may give a text zone a height smaller than
    its own insets (a 2.2pt band, say). Measuring inside that box yields a
    non-positive inner area: unsupportable geometry, not a text fit. The
    honest reaction is to measure the frame at the smallest physically
    meaningful size and let the later reconciliation give it its real need —
    never to report a fabricated fit and never to shrink the type.
    """
    from .units import floor_text_frame  # noqa: PLC0415

    size = _style_size_pt(style)
    line = size * 1.2 if size > 0 else 12.0
    return _clamp_box(floor_text_frame(hint, margins, line), content_box)


def _authored_columns(
    boxes: dict[str, dict[str, float]],
    members: list[str],
    slop: float = 0.8,
) -> list[list[str]]:
    """Group hinted boxes into authored columns by substantial x overlap.

    Two boxes share a column when their x intervals overlap by at least
    ``slop`` of *both* widths — i.e. they are stacked or slightly offset in
    the same vertical lane. A full-width band that merely spans several
    lanes (a caption strip under a two-column composition) overlaps each
    lane by less, so it stays its own lane and each column owns the full
    content height independently.
    """
    order = sorted(members, key=lambda c: (boxes[c]["x"], boxes[c]["y"]))
    columns: list[list[str]] = []
    spans: list[list[float]] = []
    for cid in order:
        b = boxes[cid]
        x0, x1 = b["x"], b["x"] + b["w"]
        best = -1
        best_inter = 0.0
        for i, (s0, s1) in enumerate(spans):
            inter = min(x1, s1) - max(x0, s0)
            if inter / max(min(b["w"], s1 - s0), 1e-6) < slop:
                continue
            if inter / max(max(b["w"], s1 - s0), 1e-6) < slop:
                continue
            if best < 0 or inter > best_inter:
                best, best_inter = i, inter
        if best < 0:
            columns.append([cid])
            spans.append([x0, x1])
        else:
            columns[best].append(cid)
            spans[best] = [min(spans[best][0], x0), max(spans[best][1], x1)]
    return [sorted(c, key=lambda cid: (boxes[cid]["y"], boxes[cid]["x"])) for c in columns]


def containers_of_text(
    boxes: dict[str, dict[str, float]],
    text_cids: set[str],
) -> dict[str, list[str]]:
    """Map each non-text frame to the text frames authored inside it."""
    out: dict[str, list[str]] = {}
    for parent in sorted(boxes):
        if parent in text_cids:
            continue
        pbox = boxes[parent]
        kids: list[str] = []
        for child in sorted(text_cids):
            cbox = boxes[child]
            cx = cbox["x"] + cbox["w"] / 2.0
            cy = cbox["y"] + cbox["h"] / 2.0
            if (
                pbox["x"] - 1.0 <= cx <= pbox["x"] + pbox["w"] + 1.0
                and pbox["y"] - 1.0 <= cy <= pbox["y"] + pbox["h"] + 1.0
            ):
                kids.append(child)
        if kids:
            out[parent] = kids
    return out


def free_horizontal_run(
    box: dict[str, float],
    obstacles: list[dict[str, float]],
    content_box: dict[str, float],
    *,
    height_pt: float,
    gap_pt: float = 7.2,
) -> dict[str, float] | None:
    """Widest obstacle-free run at ``box``'s band that still covers ``box``.

    §5.5 width redistribution: normative corporate type is larger than the
    type the source used, so a frame authored at the source's narrow width
    cannot hold its own text. Claiming the adjacent free air at the same
    vertical band costs nothing spatially and reduces the measured line
    count. ``None`` when the run would not strictly contain ``box`` — the
    authored frame is then the one the scene composed, and it stays.
    """
    x0, x1 = box["x"], content_box["x"] + content_box["w"]
    y = box["y"]
    h = max(height_pt, 1.0)
    for ob in sorted(obstacles, key=lambda o: o["x"]):
        if ob["y"] + ob["h"] <= y + 0.75 or ob["y"] >= y + h - 0.75:
            continue
        if ob["x"] + ob["w"] <= x0 + 0.75 or ob["x"] >= x1 - 0.75:
            continue
        if ob["x"] >= box["x"] + box["w"] - 0.75:
            x1 = min(x1, ob["x"] - gap_pt)
        elif ob["x"] + ob["w"] <= box["x"] + 0.75:
            # Free air on the left would mean moving the authored frame,
            # which is a side placement, not a fit fix.
            continue
        else:
            # The obstacle sits inside the authored frame; no run contains
            # the frame without it.
            return None
    if x1 - x0 <= box["w"] + 1.0:
        return None
    return {"x": x0, "y": box["y"], "w": x1 - x0, "h": box["h"]}


def reconcile_hinted_heights(
    boxes: dict[str, dict[str, float]],
    needs: dict[str, float],
    content_box: dict[str, float],
    *,
    gap_pt: float = 7.2,
) -> tuple[dict[str, dict[str, float]], list[dict[str, Any]], list[str]]:
    """Turn authored proportional zones into physically capable frames.

    A scene authored in percent declares *order, lane and relative
    emphasis*, not physical heights: a 12pt zone whose text measures 35pt
    is an authoring estimate, and the honest solver reaction is to give the
    frame the height its own text needs — never to shrink the type (§5.5).

    Within each authored column (see :func:`_authored_columns`) every text
    member receives its measured ``required_outer_height_pt``; the column
    keeps its authored top and bottom, and the air the author left between
    frames is redistributed in proportion to the authored heights. That
    air is a design variable (§7.1), not a physical constraint — only the
    type sizes and the measured text are. Widths and x positions are the
    author's and are never touched here.

    Returns ``(boxes, moves, findings)``. A column whose measured needs
    do not fit its authored span is left untouched and reported: that is
    a real capacity conflict for the scene to resolve, not something to
    clamp away silently.
    """
    members = [cid for cid in boxes if needs.get(cid, 0.0) > 0.0]
    if not members:
        return dict(boxes), [], []
    out = dict(boxes)
    moves: list[dict[str, Any]] = []
    findings: list[str] = []
    for column in _authored_columns(out, members):
        column = [c for c in column if needs.get(c, 0.0) > 0.0]
        if len(column) < 2:
            continue
        top = min(out[c]["y"] for c in column)
        authored_bottom = max(out[c]["y"] + out[c]["h"] for c in column)
        lane_x0 = min(out[c]["x"] for c in column)
        lane_x1 = max(out[c]["x"] + out[c]["w"] for c in column)
        span_bottom = min(authored_bottom, content_box["y"] + content_box["h"])
        need_total = sum(needs[c] for c in column)
        gap_total = gap_pt * (len(column) - 1)
        slack = span_bottom - top - need_total - gap_total
        if slack < -0.75:
            # The authored span is a hint; the physical capacity of the
            # lane is the content box. When nothing else occupies the lane
            # below the authored span, the column may grow into it — that
            # is reclaimed air, not an overlap and not a shrunk type size.
            below = content_box["y"] + content_box["h"]
            blocked = any(
                other not in column
                and boxes[other]["y"] + boxes[other]["h"] > authored_bottom - 0.75
                and boxes[other]["x"] < lane_x1 - 0.75
                and boxes[other]["x"] + boxes[other]["w"] > lane_x0 + 0.75
                for other in boxes
            )
            if not blocked and below > span_bottom:
                span_bottom = below
                slack = span_bottom - top - need_total - gap_total
        if slack < -0.75:
            findings.append(
                f"CAPACITY_COLUMN_CONFLICT: column "
                f"x={out[column[0]]['x']:.0f}.."
                f"{max(out[c]['x'] + out[c]['w'] for c in column):.0f} "
                f"needs {need_total + gap_total:.1f}pt "
                f"({need_total:.1f}pt measured text + {gap_total:.1f}pt gaps) "
                f"but its authored span is {span_bottom - top:.1f}pt; "
                f"members={sorted(column)}"
            )
            continue
        weights = [max(out[c]["h"], 0.0) for c in column[:-1]]
        weight_total = sum(weights)
        extra = (
            [slack * w / weight_total for w in weights]
            if weight_total > 0
            else [slack / len(column)] * len(weights)
        )
        tail = slack - sum(extra)
        y = top
        for i, cid in enumerate(column):
            before = out[cid]
            height = needs[cid] + (tail if i + 1 == len(column) else 0.0)
            if abs(before["y"] - y) < 0.75 and abs(before["h"] - height) < 0.75:
                y += height + gap_pt + (extra[i] if i < len(extra) else 0.0)
                continue
            moves.append(
                {
                    "action": "reconcile_hinted_height",
                    "target": cid,
                    "reason": "authored zone height is a proportional hint; "
                    "measured text need is the physical requirement",
                    "from_y": round(before["y"], 2),
                    "to_y": round(y, 2),
                    "from_h": round(before["h"], 2),
                    "to_h": round(height, 2),
                    "measured_need_pt": round(needs[cid], 2),
                    "reclaimed_air_pt": round((extra[i] if i < len(extra) else 0.0) + tail, 2),
                }
            )
            out[cid] = {**before, "y": y, "h": height}
            y += height + gap_pt + (extra[i] if i < len(extra) else 0.0)
    return out, moves, findings


def containers_follow_children(
    boxes: dict[str, dict[str, float]],
    authored: dict[str, dict[str, float]],
    text_cids: set[str],
) -> tuple[dict[str, dict[str, float]], list[dict[str, Any]]]:
    """Keep a surface/accent container around the content it was composed for.

    A panel, card or accent bar is authored as an arrangement with the
    text it holds: its own top/bottom padding is the design's, so when the
    text inside it is re-heighted the container keeps that padding and
    follows the content instead of being left behind as an empty frame
    (§1.5/§6.1: a frame that no longer holds anything is an empty slot).
    Containment and padding are read from ``authored`` — the geometry the
    scene composed with — while the followed geometry comes from ``boxes``.
    Only text children drive the move; a container never grows to chase
    other decor, which would cascade through nested shapes. Only y/height
    move; the authored width and x are the container's own.
    """
    out = dict(boxes)
    moves: list[dict[str, Any]] = []
    for parent in sorted(boxes):
        if parent in text_cids:
            continue
        pbox = authored.get(parent)
        if pbox is None:
            continue
        pad_top = None
        pad_bottom = None
        tops: list[float] = []
        bottoms: list[float] = []
        for child in sorted(text_cids):
            cbox = authored.get(child)
            if cbox is None or child == parent:
                continue
            cx = cbox["x"] + cbox["w"] / 2.0
            cy = cbox["y"] + cbox["h"] / 2.0
            if not (
                pbox["x"] - 1.0 <= cx <= pbox["x"] + pbox["w"] + 1.0
                and pbox["y"] - 1.0 <= cy <= pbox["y"] + pbox["h"] + 1.0
            ):
                continue
            if pad_top is None:
                pad_top = cbox["y"] - pbox["y"]
                pad_bottom = (pbox["y"] + pbox["h"]) - (cbox["y"] + cbox["h"])
            tops.append(out[child]["y"])
            bottoms.append(out[child]["y"] + out[child]["h"])
        if not tops:
            continue
        new_y = min(tops) - max(pad_top, 0.0)
        new_bottom = max(bottoms) + max(pad_bottom, 0.0)
        new_h = max(new_bottom - new_y, 1.0)
        if abs(new_y - pbox["y"]) < 0.75 and abs(new_h - pbox["h"]) < 0.75:
            continue
        moves.append(
            {
                "action": "container_follows_content",
                "target": parent,
                "from_y": round(pbox["y"], 2),
                "to_y": round(new_y, 2),
                "from_h": round(pbox["h"], 2),
                "to_h": round(new_h, 2),
                "children": len(tops),
                "reason": "container keeps its authored padding around the "
                "content it was composed for",
            }
        )
        out[parent] = {**pbox, "y": new_y, "h": new_h}
    return out, moves


def _solver_code_hash() -> str:
    from pathlib import Path

    digest = hashlib.sha256()
    for name in (
        "layout.py",
        "text_flow.py",
        "frame_contract.py",
        "measured_placement.py",
        "constraint_layout.py",
        "composition.py",
    ):
        digest.update((Path(__file__).parent / name).read_bytes())
    return digest.hexdigest()


def _layout_cache_key(
    tree: Any, rules: Any, profile: Any, font_bundle: Any, options: dict[str, Any]
) -> dict[str, str]:
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
        "layout_code": _solver_code_hash(),
        "ortools_version": __import__("importlib.metadata", fromlist=["version"]).version(
            "ortools"
        ),
        "font_bundle": getattr(font_bundle, "bundle_hash", ""),
        "calibration_id": options.get("calibration_id", "uncalibrated-v1"),
        "options": json.dumps(
            {k: v for k, v in options.items() if k not in ("budget_tracker", "runtime_counters")},
            sort_keys=True,
            default=str,
        ),
        "platform": f"{sys.platform}-{platform.machine()}",
        "locale": locale.getlocale()[0] or "C",
    }


def _cache_dir(project_root: Any) -> Any:
    from pathlib import Path

    return Path(project_root) / "runs" / ".layout-cache"


def _hint_box(node: Any, content_box: dict[str, float]) -> dict[str, float]:
    for c in node.constraints:
        if c.kind == "relative_box" and isinstance(c.value, dict) and c.value.get("w", 0) > 0:
            return {
                "x": float(c.value.get("x", 0)),
                "y": float(c.value.get("y", 0)),
                "w": float(c.value.get("w", 0)),
                "h": float(c.value.get("h", 0)),
            }
    return dict(content_box)


def _build_geometry_emu(
    tree: Any,
    current_boxes: dict[str, dict[str, float]],
    content_box: dict[str, float],
    pt_to_emu_fn: Any,
) -> dict[str, dict[str, int]]:
    geometry_emu: dict[str, dict[str, int]] = {}
    for cid, node in tree.components.items():
        box = current_boxes.get(cid, _hint_box(node, content_box))
        # Authored blocks have no single payload ref: their geometry is
        # addressed by the authored component id and consumed by the native
        # writer (emit_scene), not by the per-source-object loop (§3.3).
        key = node.payload_ref or cid
        geometry_emu[key] = {
            "x": pt_to_emu_fn(box["x"]),
            "y": pt_to_emu_fn(box["y"]),
            "w": pt_to_emu_fn(box["w"]),
            "h": pt_to_emu_fn(box["h"]),
        }
    return geometry_emu


def _build_resolved_slide(
    tree: Any,
    options: dict[str, Any],
    geometry_emu: dict[str, dict[str, int]],
    flagged: bool,
    passthrough_refs: list[str] | None = None,
    table_layout_emu: dict[str, dict[str, list[int]]] | None = None,
) -> dict[str, Any]:
    zones = []
    for cid, node in tree.components.items():
        zone: dict[str, Any] = {
            "zone_id": f"zone:{cid}",
            "role": node.style_role,
            "content_refs": node.content_refs,
            "ref_roles": dict(getattr(node, "ref_roles", {}) or {}),
            "paragraph_groups": list(getattr(node, "paragraph_groups", []) or []),
            "inline_separator": str(getattr(node, "inline_separator", " ")),
        }
        for token_field in ("surface_token", "foreground_token", "stroke_token"):
            token = getattr(node, token_field, "")
            if token:
                zone[token_field] = token
        zones.append(zone)
    return {
        "slide_id": tree.slide_id,
        "source_slide_id": tree.slide_id,
        "intent_id": tree.candidate_id,
        "slide_background_token": getattr(tree, "slide_background_token", "") or "",
        "generation_origin": options.get("generation_origin", "harness_model"),
        "zones": zones,
        "role_assignments": {
            f"zone:{cid}": node.style_role for cid, node in tree.components.items()
        },
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

    from .models import CompositionTree, RectPt
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
                status="search_exhausted",
                attempts=[],
                unresolved=[
                    f"BUDGET_EXHAUSTED: search_exhausted: "
                    f"{spent}/{total} attempts consumed across "
                    "candidates; termination=budget_spent; "
                    "new candidate_id does not reset it"
                ],
                measurement_profile=profile_kind,
                evidence_hashes={},
            )

    if use_cache and project_root is not None:
        key = _layout_cache_key(tree, rules, profile, font_bundle, options)
        key_hash = hashlib.sha256(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()
        entry = _cache_dir(project_root) / key_hash / "result.json"
        if entry.is_file():
            try:
                cached = LayoutResolutionResult.model_validate_json(
                    entry.read_text(encoding="utf-8")
                )
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
    rich_text_cids: dict[str, list[str]] = {}
    needs_assets = False
    for cid, node in tree.components.items():
        obj = objects.get(node.payload_ref)
        if node.kind == "rich_text":
            # Authored block: one frame, one resolved role. The block style
            # is resolved once from the role, never per source object (§3.2/5.2).
            rich_text_cids[cid] = list(node.content_refs)
            try:
                run_styles[cid] = resolve_component_style(node.style_role, rules, font_bundle)
            except ValueError as exc:
                msg = str(exc)
                unresolved.append(f"{cid}: {msg}")
                if "UNBOUND" in msg and "FACE" in msg:
                    needs_assets = True
            continue
        carries_text = node.kind in ("text",) or (
            node.kind == "shape" and bool(_object_paragraphs(obj))
            if obj is not None
            else node.kind == "shape"
        )
        if (
            node.kind in ("image", "connector")
            or (node.kind == "shape" and not carries_text)
            or (obj is not None and getattr(obj, "kind", "") == "unknown")
        ):
            # Opaque objects carry no runs (extracted text is preserved
            # byte-wise by the transplant path, never re-styled here).
            continue
        try:
            run_styles[cid] = resolve_component_style(node.style_role, rules, font_bundle)
        except ValueError as exc:
            msg = str(exc)
            unresolved.append(f"{cid}: {msg}")
            if "UNBOUND" in msg and "FACE" in msg:
                needs_assets = True
    if any("STYLE_ROLE_UNBOUND" in u for u in unresolved):
        return LayoutResolutionResult(
            status="needs_review",
            attempts=attempts,
            unresolved=unresolved,
            measurement_profile=profile_kind,
            evidence_hashes={"tree": hashlib.sha256(tree.model_dump_json().encode()).hexdigest()},
        )
    if needs_assets or getattr(font_bundle, "missing_faces", []):
        return LayoutResolutionResult(
            status="needs_assets",
            attempts=attempts,
            unresolved=unresolved,
            measurement_profile=profile_kind,
            evidence_hashes={},
        )

    layout_mode = str(options.get("layout_mode", "recompose"))
    if layout_mode not in ("recompose", "preserve_structure"):
        return LayoutResolutionResult(
            status="needs_review",
            attempts=attempts,
            unresolved=[*unresolved, f"LAYOUT_MODE_UNKNOWN: {layout_mode}"],
            measurement_profile=profile_kind,
            evidence_hashes={},
        )
    if layout_mode == "recompose":
        # Source-box containment allowances describe the old arrangement;
        # after relocation only intent-declared nest pairs stay in force.
        # A stale allowance would launder a real occlusion into a pass.
        kept = [
            c
            for c in list(getattr(tree, "constraints", []) or [])
            if not (
                getattr(c, "kind", "") == "allowed_overlap" and getattr(c, "origin", "") == "source"
            )
        ]
        if len(kept) != len(list(getattr(tree, "constraints", []) or [])):
            tree = tree.model_copy(update={"constraints": kept})
            addressed.append("stale source allow-pairs pruned for recompose")
    elif layout_mode == "preserve_structure":
        # preserve_structure is no longer a silent fallback to source boxes.
        # It is treated as recompose with the same target geometry rules.
        addressed.append("preserve_structure mode: using recompose geometry rules")

    def _allowed_cid_pairs() -> set[frozenset]:
        """Exact-pair nest allowances as component-id pairs (both modes)."""
        refs_of = {
            cid: set(node.content_refs) | {node.payload_ref}
            for cid, node in tree.components.items()
        }
        pairs: set[frozenset] = set()
        for constraint in list(getattr(tree, "constraints", []) or []):
            if getattr(constraint, "kind", "") != "allowed_overlap":
                continue
            targets = list(getattr(constraint, "target_ids", []) or [])
            cid_pair = [cid for cid, refs in refs_of.items() if refs.intersection(targets)]
            if len(cid_pair) == 2:
                pairs.add(frozenset(cid_pair))
        return pairs

    def _residual_overlaps(boxes: dict[str, dict[str, float]]) -> list[list[str]]:
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
    font_assets = [
        {
            "alias": b.browser_alias,
            "path": b.path,
            "sha256": b.sha256,
            "face_index": b.face_index,
            "style": "normal",
            "weight": 400,
        }
        for b in (getattr(font_bundle, "bindings", []) or [])
    ]

    def _table_styles() -> dict[str, dict[str, Any]]:
        styles: dict[str, dict[str, Any]] = {}
        for _other_cid, other_node in tree.components.items():
            role = getattr(other_node, "style_role", "")
            if role and role in run_styles:
                styles.setdefault(role, dict(run_styles[role]))
        for fallback in ("body", "dense_body_or_caption"):
            if fallback not in styles:
                try:
                    styles[fallback] = resolve_component_style(fallback, rules, font_bundle)
                except ValueError:
                    continue
        return styles

    def _resolve_table(cid: str, node: Any, obj: Any) -> bool:
        """Measure the full grid; True when resolved dims are stored."""
        from . import primitives as _prim
        from .units import pt_to_emu as _pt_to_emu

        box = _clamp_box(_hint_box(node, content_box), content_box)
        res = _prim.layout_table(
            getattr(obj, "payload", None),
            box,
            _table_styles(),
            measurer,
            {
                "profile_kind": profile_kind,
                "font_assets": font_assets,
                "font_allowlist_roots": options.get("font_allowlist_roots", []),
                "debug_output_dir": debug_dir,
            },
        )
        preserve_boxes[cid] = dict(box)
        if not res.get("ok"):
            unresolved.append(
                f"TABLE_UNRESOLVED: {cid}: "
                f"{'; '.join(res.get('issues', ['unknown'])[:3])}; "
                "source grid kept, flagged"
            )
            return False
        table_layout_emu[node.payload_ref] = {
            "column_widths_emu": [int(_pt_to_emu(w)) for w in res["column_widths_pt"]],
            "row_heights_emu": [int(_pt_to_emu(h)) for h in res["row_heights_pt"]],
        }
        # The resolved frame packs rows top-anchored at measured heights;
        # leftover box slack is air below, never stretched rows.
        content_h = sum(float(h) for h in res["row_heights_pt"])
        if content_h > 0:
            preserve_boxes[cid] = {**box, "h": content_h}
        return True

    for cid, node in tree.components.items():
        if node.kind == "rich_text":
            # Authored block: several source spans measured as one native
            # frame with real paragraph boundaries (§3.2). Frame padding is
            # paid once for the authored block, not once per source object.
            paras = _rich_text_paragraphs(
                objects,
                list(node.content_refs),
                dict(getattr(node, "ref_roles", {}) or {}),
                list(getattr(node, "paragraph_groups", []) or []),
                str(getattr(node, "inline_separator", " ")),
            )
            if not paras:
                unresolved.append(f"RICH_TEXT_EMPTY: {cid}")
                continue
            hint = _clamp_box(_hint_box(node, content_box), content_box)
            style = run_styles.get(cid)
            if style is None:
                unresolved.append(f"STYLE_UNRESOLVED: {cid}")
                continue
            for para in paras:
                if para.get("role") and para["role"] != node.style_role:
                    try:
                        resolve_component_style(str(para["role"]), rules, font_bundle)
                    except ValueError as exc:
                        unresolved.append(f"{cid}.{para['id']}: {exc}")
                for run in para["runs"]:
                    run_role = str(run.get("role", "") or para.get("role", "") or node.style_role)
                    run["style"] = resolve_component_style(run_role, rules, font_bundle)
            margins = {"l": 7.2, "r": 7.2, "t": 3.6, "b": 3.6}
            frame = _text_frame_box(hint, run_styles.get(cid), margins, content_box)
            comp = MeasureComponent(
                id=cid,
                kind="text",
                payload_ref=node.content_refs[0],
                paragraphs=measurement_paragraphs(paras),
                box_pt=dict(frame),
                margins_pt=margins,
                vertical_anchor="top",
                wrap_policy="wrap",
                rotation_deg=0.0,
                list_preview={},
            )
            components[cid] = comp
            preserve_boxes[cid] = dict(frame)
            continue
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
            components[cid] = kind_resolvers[kind](node, obj, content_box, run_styles[cid])
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
                        id=cid,
                        kind="text",
                        payload_ref=node.payload_ref,
                        paragraphs=[],
                        box_pt={
                            **src_box,
                            "w": max(src_box["w"], 1.0),
                            "h": max(src_box["h"], 1.0),
                        },
                        margins_pt={},
                        vertical_anchor="top",
                        wrap_policy="wrap",
                        rotation_deg=0.0,
                        list_preview={},
                    )
                    components[cid] = comp
                    preserve_boxes[cid] = dict(src_box)
                    passthrough_cids.add(cid)
                    continue
                natural = (options.get("image_natural_pt", {}) or {}).get(node.payload_ref, {})
                from . import primitives as _prim2

                fitted = _prim2.layout_image_region(
                    getattr(obj, "payload", None),
                    hint,
                    {
                        "image_natural_size_pt": natural,
                        "image_fit": "fit",
                        "permitted_crop": (0, 0, 0, 0),
                    },
                )
                box_pt = fitted.get("box_pt", hint)
                if not natural:
                    unresolved.append(
                        f"IMAGE_NATURAL_UNKNOWN: {cid} keeps hint box; aspect fit unverified"
                    )
                comp = MeasureComponent(
                    id=cid,
                    kind="text",
                    payload_ref=node.payload_ref,
                    paragraphs=[],
                    box_pt=box_pt,
                    margins_pt={},
                    vertical_anchor="top",
                    wrap_policy="wrap",
                    rotation_deg=0.0,
                    list_preview={},
                )
                components[cid] = comp
                preserve_boxes[cid] = dict(box_pt)
            elif kind == "shape" and not _object_paragraphs(obj):
                # Text-less shape: an explicit intent zone relocates it
                # (nest container / moved decor); without a zone it stays
                # at its source box (passthrough, canvas-checked).
                if _has_hint(node):
                    style = run_styles.get(cid)
                    hint = _clamp_box(_hint_box(node, content_box), content_box)
                    comp = MeasureComponent(
                        id=cid,
                        kind="text",
                        payload_ref=node.payload_ref,
                        paragraphs=[],
                        box_pt=dict(hint),
                        margins_pt={},
                        vertical_anchor="top",
                        wrap_policy="wrap",
                        rotation_deg=0.0,
                        list_preview={},
                    )
                    components[cid] = comp
                    preserve_boxes[cid] = dict(hint)
                    continue
                src_box = _source_box_pt(obj)
                if src_box is None:
                    unresolved.append(
                        f"DECOR_BOX_UNKNOWN: {cid} has no source box; cannot place honestly"
                    )
                    continue
                comp = MeasureComponent(
                    id=cid,
                    kind="text",
                    payload_ref=node.payload_ref,
                    paragraphs=[],
                    box_pt={**src_box, "w": max(src_box["w"], 1.0), "h": max(src_box["h"], 1.0)},
                    margins_pt={},
                    vertical_anchor="top",
                    wrap_policy="wrap",
                    rotation_deg=0.0,
                    list_preview={},
                )
                components[cid] = comp
                preserve_boxes[cid] = dict(src_box)
                passthrough_cids.add(cid)
            elif kind == "connector":
                # A rule/arrow the composition placed is authored geometry
                # and moves with its group (§4.1/§4.2); only an unplaced
                # connector stays at its source box as an obstacle.
                if _has_hint(node):
                    comp = MeasureComponent(
                        id=cid,
                        kind="text",
                        payload_ref=node.payload_ref,
                        paragraphs=[],
                        box_pt=dict(hint),
                        margins_pt={},
                        vertical_anchor="top",
                        wrap_policy="wrap",
                        rotation_deg=0.0,
                        list_preview={},
                    )
                    components[cid] = comp
                    preserve_boxes[cid] = dict(hint)
                    continue
                src_box = _source_box_pt(obj, floor=0.0)
                if src_box is None:
                    unresolved.append(f"CONNECTOR_BOX_UNKNOWN: {cid}")
                    continue
                comp = MeasureComponent(
                    id=cid,
                    kind="text",
                    payload_ref=node.payload_ref,
                    paragraphs=[],
                    box_pt={**src_box, "w": max(src_box["w"], 1.0), "h": max(src_box["h"], 1.0)},
                    margins_pt={},
                    vertical_anchor="top",
                    wrap_policy="wrap",
                    rotation_deg=0.0,
                    list_preview={},
                )
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
                margins = {"l": 7.2, "r": 7.2, "t": 3.6, "b": 3.6}
                frame = _text_frame_box(hint, style, margins, content_box)
                comp = MeasureComponent(
                    id=cid,
                    kind="text",
                    payload_ref=node.payload_ref,
                    paragraphs=measurement_paragraphs(paras),
                    box_pt=dict(frame),
                    margins_pt=margins,
                    vertical_anchor="top",
                    wrap_policy="wrap",
                    rotation_deg=0.0,
                    list_preview={},
                )
                components[cid] = comp
                preserve_boxes[cid] = dict(frame)
            else:
                unresolved.append(
                    f"KIND_UNMEASURED: {kind} component {cid} has no "
                    "recompose resolver; needs Block 4 binding, not a "
                    "guessed box"
                )
        else:
            unresolved.append(
                f"KIND_UNMEASURED: {kind} component {cid} has no resolver; "
                "needs Block 4 binding, not a guessed box"
            )

    if unresolved and not components:
        return LayoutResolutionResult(
            status="needs_review",
            attempts=attempts,
            unresolved=unresolved,
            measurement_profile=profile_kind,
            evidence_hashes={},
        )

    # One floor, applied to every text-bearing component right before the
    # call, so no construction site can offer a frame shorter than its own
    # insets (§5.1: a non-positive inner area is invalid geometry, never a
    # fabricated fit). Construction sites may still floor early for their
    # own reasons; this is the contract's single enforcement point.
    from .units import floor_text_frame  # noqa: PLC0415

    for cid, comp in list(components.items()):
        if not getattr(comp, "paragraphs", []):
            continue
        size = 0.0
        for para in comp.paragraphs:
            for run in para.runs:
                value = _style_size_pt(run.style)
                if value > 0:
                    size = max(size, value)
        line = size * 1.2 if size > 0 else 12.0
        box = floor_text_frame(dict(comp.box_pt), comp.margins_pt, line)
        components[cid] = comp.model_copy(update={"box_pt": RectPt(**box)})
        preserve_boxes[cid] = dict(box)

    request = MeasurementRequest(
        schema_version="1.0",
        request_id=f"layout-{tree.candidate_id}",
        canvas_pt={"w": rules.canvas.w / 12700.0, "h": rules.canvas.h / 12700.0},
        css_px_per_in=96.0,
        profile_kind=profile_kind,  # type: ignore[arg-type]
        debug_output_dir=debug_dir,
        components=list(components.values()),
        font_assets=font_assets,
        engine_options={
            "timeout_ms": 120000,
            "device_scale_factor": 1,
            "allowlist_roots": options.get("font_allowlist_roots", []),
        },
        input_hashes={},
    )
    try:
        response = measurer(request)
    except Exception as exc:
        return LayoutResolutionResult(
            status="needs_review",
            attempts=attempts,
            unresolved=[*unresolved, f"MEASUREMENT_FAILED: {exc}"],
            measurement_profile=profile_kind,
            evidence_hashes={},
        )
    items = {i["component_id"]: i for i in response.get("items", [])}
    # §10#6: a non-empty expected set with a missing/empty component
    # response is fail/unknown, never a pass with silent defaults.
    missing_items = sorted(set(components) - set(items))
    if missing_items:
        return LayoutResolutionResult(
            status="needs_review",
            attempts=attempts,
            unresolved=[
                *unresolved,
                f"MEASUREMENT_COVERAGE_GAP: {missing_items[:4]} "
                "requested but absent from response; no default "
                "geometry trusted",
            ],
            measurement_profile=profile_kind,
            evidence_hashes={},
        )

    # 3. Target semantic blocks are placed with measured alternatives.
    # No global x-overlap column inference, per-source stack, or gap tax.
    from .measured_placement import place_measured_components

    allowed_cids = _allowed_cid_pairs()
    node_kinds = {cid: node.kind for cid, node in tree.components.items()}
    textless_cids = {cid for cid, comp in components.items() if not comp.paragraphs}
    try:
        allocation, overlap_moves = place_measured_components(
            components,
            preserve_boxes,
            items,
            request,
            measurer,
            content_box,
            fixed=set(passthrough_cids) | (set(preserve_boxes) - set(components)),
            allowed=allowed_cids,
            kinds=node_kinds,
            constraints=list(tree.constraints),
            source_bindings={
                ref: cid for cid, node in tree.components.items() for ref in node.content_refs
            },
            seconds=_solver_budget(options, len(components)),
        )
    except (ValueError, RuntimeError, ImportError) as exc:
        return LayoutResolutionResult(
            status="needs_review",
            attempts=attempts,
            unresolved=[*unresolved, f"CONSTRAINT_LAYOUT_ERROR:{exc}"],
            measurement_profile=profile_kind,
            evidence_hashes={},
        )
    for counter in (tracker, options.get("runtime_counters")):
        if isinstance(counter, dict):
            counter["solver_calls"] = int(counter.get("solver_calls", 0)) + int(
                allocation.stats.get("solver_calls", 0)
            )
    if allocation.status not in ("OPTIMAL", "FEASIBLE"):
        # Keep the measured reason next to the solver verdict: an INFEASIBLE
        # status alone would hide which component was measured as overflowing.
        measured_overflow = [
            f"{cid}:{axis}"
            for cid, item in sorted(items.items())
            for axis in ("overflow_x", "overflow_y")
            if item.get(axis)
        ]
        attempts.append(
            LayoutAttempt(
                candidate_id=tree.candidate_id,
                attempt_index=0,
                changes=overlap_moves,
                hard_issues=[*allocation.issues, *measured_overflow],
                consumed_budget=1,
            )
        )
        return LayoutResolutionResult(
            status="infeasible" if allocation.status == "INFEASIBLE" else "needs_review",
            attempts=attempts,
            unresolved=[*unresolved, *allocation.issues],
            measurement_profile=profile_kind,
            evidence_hashes={},
        )
    alloc_boxes = allocation.boxes

    # 4. re-measure at assigned widths, bounded local attempts
    attempt_index = 0
    consumed = 1
    static_issues: list[str] = []
    current_boxes = dict(alloc_boxes)
    # Fixed table boxes: containment is checked here (content region —
    # tables are relocated content, never canvas passthrough).
    for cid, box in current_boxes.items():
        if cid in components:
            continue
        if (
            box["x"] < content_box["x"] - 0.75
            or box["y"] < content_box["y"] - 0.75
            or box["x"] + box["w"] > content_box["x"] + content_box["w"] + 0.75
            or box["y"] + box["h"] > content_box["y"] + content_box["h"] + 0.75
        ):
            static_issues.append(f"{cid}: table box outside content box")
    hard_issues: list[str] = list(static_issues)

    def _measure_boxes(boxes: dict[str, dict[str, float]]) -> dict[str, Any]:
        from .models import RectPt

        comps = []
        for cid, box in boxes.items():
            if cid not in components:
                continue  # fixed geometry (tables): grid measured already
            base = components[cid].model_copy(
                update={"box_pt": RectPt(**box), "id": f"{cid}-a{attempt_index}"}
            )
            comps.append(base)
        retry = request.model_copy(
            update={
                "request_id": f"layout-{tree.candidate_id}-a{attempt_index}",
                "components": comps,
                # The retry is a new canonical request: drop the initial
                # hash so the bridge echoes this attempt's hash, never a
                # stale one (a mismatched echo must fail, not pass).
                "input_hashes": {},
            }
        )
        resp = measurer(retry)
        got = {i["component_id"].rsplit("-a", 1)[0]: i for i in resp.get("items", [])}
        # §10#6: retry responses must cover every requested box; a
        # partial re-measure is a measurement failure, not a pass on
        # the returned subset.
        want = {cid for cid in boxes if cid in components}
        missing = sorted(want - set(got))
        if missing:
            raise ValueError(
                f"MEASUREMENT_COVERAGE_GAP: retry "
                f"{retry.request_id} "
                f"missing {missing[:4]}; no default geometry trusted"
            )
        return got

    def _hard_issues(measured: dict[str, Any], boxes: dict[str, dict[str, float]]) -> list[str]:
        found: list[str] = []
        canvas = {"x": 0.0, "y": 0.0, "w": rules.canvas.w / 12700.0, "h": rules.canvas.h / 12700.0}
        for cid, item in measured.items():
            if cid in textless_cids:
                # Vacuous text-fit on content-less geometry: insets around
                # nothing are not an overflow. Presence, region and
                # collision gates below still apply.
                pass
            else:
                if item.get("overflow_x"):
                    found.append(
                        f"{cid}: overflow_x +{item.get('excess_pt', {}).get('x', 0):.1f}pt"
                    )
                if item.get("overflow_y"):
                    found.append(
                        f"{cid}: overflow_y +{item.get('excess_pt', {}).get('y', 0):.1f}pt"
                    )
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
            status="needs_review",
            attempts=attempts,
            unresolved=[*unresolved, f"MEASUREMENT_FAILED: {exc}"],
            measurement_profile=profile_kind,
            evidence_hashes={},
        )
    hard_issues = _hard_issues(measured, current_boxes) + static_issues
    initial_changes: list[dict[str, Any]] = [
        {
            "action": "initial_allocation",
            "organization": tree.root.organization,
            "layout_mode": layout_mode,
        }
    ]
    if overlap_moves:
        initial_changes.append({"action": "resolve_overlap_batch", "moves": overlap_moves})
    attempts.append(
        LayoutAttempt(
            candidate_id=tree.candidate_id,
            attempt_index=attempt_index,
            changes=initial_changes,
            measured_hash=hashlib.sha256(json.dumps(measured, sort_keys=True).encode()).hexdigest(),
            hard_issues=list(hard_issues),
            metrics={"overflow_count": float(len(hard_issues))},
            next_action="done" if not hard_issues else "expand_into_free",
            consumed_budget=consumed,
        )
    )

    def _sig() -> list[str]:
        hs = sorted(h.split(":")[0] for h in hard_issues)
        rs = sorted(
            f"{a}+{b}" for a, b in (_residual_overlaps(current_boxes) if current_boxes else [])
        )
        return [*hs, "|", *rs]

    # Unified search: measurement overflows AND residual (non-allowed)
    # overlaps drive the loop together. Growth can create new overlaps and
    # resolution can expose new overflows; handling either in isolation
    # strands the other. Residual-only iterations skip growth and run the
    # resolver; growth iterations are followed by a resolve pass.
    residual_now = bool(_sig()[_sig().index("|") + 1 :])
    while (hard_issues or residual_now) and consumed < max_attempts:
        consumed += 1
        attempt_index += 1
        before = json.dumps(current_boxes, sort_keys=True)
        try:
            revised, changes = place_measured_components(
                components,
                current_boxes,
                measured,
                request,
                measurer,
                content_box,
                fixed=set(passthrough_cids) | (set(current_boxes) - set(components)),
                allowed=allowed_cids,
                kinds=node_kinds,
                constraints=list(tree.constraints),
                source_bindings={
                    ref: cid for cid, node in tree.components.items() for ref in node.content_refs
                },
                seconds=_solver_budget(options, len(components)),
            )
            for counter in (tracker, options.get("runtime_counters")):
                if isinstance(counter, dict):
                    counter["solver_calls"] = int(counter.get("solver_calls", 0)) + int(
                        revised.stats.get("solver_calls", 0)
                    )
            if revised.status not in ("OPTIMAL", "FEASIBLE"):
                unresolved.extend(revised.issues)
                break
            current_boxes = revised.boxes
            measured = _measure_boxes(current_boxes) if current_boxes else {}
        except (ValueError, RuntimeError) as exc:
            unresolved.append(f"MEASURED_REFINEMENT_FAILED:{exc}")
            break
        hard_issues = _hard_issues(measured, current_boxes) + static_issues
        attempts.append(
            LayoutAttempt(
                candidate_id=tree.candidate_id,
                attempt_index=attempt_index,
                changes=changes,
                hard_issues=list(hard_issues),
                consumed_budget=consumed,
                next_action="switch_candidate" if hard_issues else "done",
            )
        )
        residual_now = bool(_sig()[_sig().index("|") + 1 :])
        if before == json.dumps(current_boxes, sort_keys=True) and (hard_issues or residual_now):
            unresolved.append("NO_PROGRESS: unchanged measured constraint model; recompose scene")
            break

    if hard_issues:
        deficits = "; ".join(hard_issues[:6])
        termination = (
            f"search_exhausted: {consumed} attempts consumed, termination=measurement_failure"
            if consumed >= max_attempts
            else f"needs_recompose: {consumed} attempts consumed, termination={hard_issues[0]}"
        )
        return LayoutResolutionResult(
            status="needs_recompose" if consumed < max_attempts else "search_exhausted",
            attempts=attempts,
            unresolved=[*unresolved, f"{termination}: {deficits}"],
            measurement_profile=profile_kind,
            evidence_hashes={"tree": hashlib.sha256(tree.model_dump_json().encode()).hexdigest()},
        )

    residual = _residual_overlaps(current_boxes) if current_boxes else []
    if residual:
        pairs = "; ".join(
            f"{a}+{b}[{_fmt_box(current_boxes[a])}|{_fmt_box(current_boxes[b])}]"
            for a, b in residual[:6]
        )
        termination = (
            f"search_exhausted: {consumed} attempts consumed, termination=overlap_unresolved"
            if consumed >= max_attempts
            else f"constraint_conflict: {consumed} attempts consumed, "
            f"termination=overlap_unresolved"
        )
        return LayoutResolutionResult(
            status="constraint_conflict",
            attempts=attempts,
            unresolved=[*unresolved, f"{termination}: {pairs}"],
            measurement_profile=profile_kind,
            evidence_hashes={"tree": hashlib.sha256(tree.model_dump_json().encode()).hexdigest()},
        )

    # 5. feasible: build measured ResolvedSlide
    geometry_emu = _build_geometry_emu(tree, current_boxes, content_box, pt_to_emu)
    headroom = 0.0
    for cid, item in measured.items():
        box = current_boxes.get(cid, content_box)
        headroom += max(
            0.0, (box["y"] + box["h"]) - (content_box["y"] + item.get("occupied_height_pt", 0.0))
        )
    resolved_slide = _build_resolved_slide(
        tree,
        options,
        geometry_emu,
        False,
        [tree.components[c].payload_ref for c in passthrough_cids if c in tree.components],
        table_layout_emu,
    )
    addressed.append(f"allocation:{tree.root.organization}")
    result = LayoutResolutionResult(
        status="resolved",
        resolved_slide=resolved_slide,
        attempts=attempts,
        addressed_issues=addressed,
        unresolved=unresolved,
        measurement_profile=profile_kind,
        evidence_hashes={
            "tree": hashlib.sha256(tree.model_dump_json().encode()).hexdigest(),
            "measured": attempts[-1].measured_hash if attempts else "",
            "score": f"{headroom:.2f}",
        },
    )
    if use_cache and project_root is not None:
        key = _layout_cache_key(tree, rules, profile, font_bundle, options)
        key_hash = hashlib.sha256(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()
        entry = _cache_dir(project_root) / key_hash
        entry.mkdir(parents=True, exist_ok=True)
        (entry / "manifest.json").write_text(
            json.dumps({"key_hash": key_hash, "key": key}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (entry / "result.json").write_text(result.model_dump_json(), encoding="utf-8")
    if isinstance(tracker, dict):
        tracker["consumed"] = int(tracker.get("consumed", 0)) + consumed
    return result


# ---------------------------------------------------------------------------
# §5.4 typed overlap permissions
# ---------------------------------------------------------------------------


def derive_overlap_permissions(scene: Any) -> list[Any]:
    """Overlap permissions of an authored scene, by parent/child relation.

    The permission follows the TARGET tree, not the set of objects that
    happen to be on the slide:

    * ``surface_child`` — a drawable node inside a ``surface`` node's
      declared region. Only that declared child (or descendant) of that
      declared surface is permitted; a sibling in another group is not.
    * ``intentional_path_crossing`` — an edge relation explicitly declares
      that two ``path`` nodes cross. It does not extend to every pair of
      bounding boxes that happens to intersect.
    * ``explicit_overlay`` — an explicit overlay relation from the scene.

    Returns an empty list (never a blanket) when the scene declares no
    relation.
    """
    from .models import OverlapPermission

    nodes = dict(getattr(scene, "nodes", {}) or {})
    relations = list(getattr(scene, "relations", []) or [])

    def _children(node_id: str) -> list[str]:
        node = nodes.get(node_id)
        out = list(getattr(node, "children", []) or [])
        for other in nodes.values():
            parent = getattr(other, "parent_id", None)
            if parent == node_id and str(other.node_id) not in out:
                out.append(str(other.node_id))
        return out

    def _descendants(node_id: str) -> set[str]:
        seen: set[str] = set()
        stack = list(_children(node_id))
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(_children(current))
        return seen

    out: list[Any] = []
    seen: set[tuple[str, str, str]] = set()
    for node_id, node in sorted(nodes.items()):
        if str(getattr(node, "kind", "")) != "surface":
            continue
        family = _descendants(node_id)
        for child in sorted(family):
            key = (str(node_id), str(child), "surface_child")
            if key in seen:
                continue
            seen.add(key)
            out.append(
                OverlapPermission(
                    underlay_id=str(node_id),
                    content_id=str(child),
                    relation_id=f"surface-child:{node_id}->{child}",
                    allowed_kind="surface_child",
                    scope="container_containment",
                    evidence_ref=f"scene:node:{node_id}",
                )
            )
    for index, relation in enumerate(relations):
        kind = str(
            _relation_field(relation, "kind") or _relation_field(relation, "relation_kind") or ""
        )
        a = str(_relation_field(relation, "from") or _relation_field(relation, "from_node") or "")
        b = str(_relation_field(relation, "to") or _relation_field(relation, "to_node") or "")
        relation_id = str(_relation_field(relation, "relation_id") or f"rel:{index}")
        allowed = None
        if kind in ("intentional_path_crossing", "path_crossing"):
            allowed = "intentional_path_crossing"
        elif kind in ("explicit_overlay", "overlay"):
            allowed = "explicit_overlay"
        if allowed is None or not a or not b:
            continue
        key = tuple(sorted((a, b))) + (allowed,)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            OverlapPermission(
                underlay_id=a,
                content_id=b,
                relation_id=relation_id,
                allowed_kind=allowed,
                scope=(
                    "declared_path_crossing"
                    if allowed == "intentional_path_crossing"
                    else "declared_overlay"
                ),
                evidence_ref=f"scene:relation:{relation_id}",
            )
        )
    return out


def _relation_field(relation: Any, key: str) -> Any:
    if isinstance(relation, dict):
        return relation.get(key)
    return getattr(relation, key, None)
