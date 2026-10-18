"""Bridge real paragraph measurements to the wired constraint solver."""

from __future__ import annotations

from typing import Any

from .constraint_layout import ConstraintResult, solve_measured_boxes
from .models import RectPt

# Evidence recorded by the measured placement step. These are NOT layout
# mutations: they document which measured variants were probed and what the
# solver returned. Mutating attempts keep their own vocabulary (layout
# .ALLOWED_ATTEMPTS); nothing here may resize text, drop content or autofit.
SOLVER_EVIDENCE_ACTIONS = (
    "authored_geometry_without_content",
    "intrinsic_width_variants",
    "constraint_layout",
)


def place_measured_components(
    components: dict[str, Any],
    hints: dict[str, dict[str, float]],
    initial_items: dict[str, dict],
    request: Any,
    measurer: Any,
    frame: dict[str, float],
    *,
    fixed: set[str],
    allowed: set[frozenset],
    kinds: dict[str, str],
    constraints: list[Any] | None = None,
    source_bindings: dict[str, str] | None = None,
    seconds: float = 5.0,
) -> tuple[ConstraintResult, list[dict]]:
    """Remeasure finite widths, then solve all sibling placements together.

    The authored width is always included. Every other width has its own
    measured height/spec. Neither source-object count nor a default gap
    contributes any height. Existing paragraph spacing/insets are measured.
    """
    variants: dict[str, list[dict]] = {}
    probes, probe_map = [], {}
    contentless: list[str] = []
    containers = {
        cid
        for cid in hints
        if kinds.get(cid) == "shape"
        and cid in components
        and not components[cid].paragraphs
        and any(cid in p for p in allowed)
    }
    for cid, comp in components.items():
        if cid not in hints or cid in fixed or not comp.paragraphs:
            continue
        initial = initial_items.get(cid, {})
        allowed_confidence = {"exact_browser"}
        if getattr(request, "profile_kind", "production") == "test_only":
            allowed_confidence.add("test_only")
        if initial.get("confidence") not in allowed_confidence:
            raise ValueError(f"MEASUREMENT_ITEM_UNVERIFIED:{cid}:{initial.get('confidence')}")
        if initial.get("content_absent"):
            # An authored surface without content (panel, rule, connector) has
            # no measured text height to charge. The measurer reports this as a
            # legitimate empty geometry, not a missing measurement: the node
            # keeps its own box and is placed by the solver like any other.
            contentless.append(cid)
            continue
        need = float(initial.get("required_outer_height_pt", 0) or 0)
        if need <= 0:
            raise ValueError(f"MEASUREMENT_NEED_MISSING:{cid}")
        width = float(hints[cid]["w"])
        variants[cid] = [] if initial.get("overflow_x") else [{"w": width, "h": need}]
        # Parent widths constrain descendants; don't measure a panel child
        # as if it could claim the entire slide behind the adjacent panel.
        parent_widths = []
        for pair in allowed:
            if cid not in pair:
                continue
            for parent in pair - {cid}:
                if parent in containers and hints[parent]["w"] >= width:
                    parent_widths.append(hints[parent]["w"])
        maximum = min(parent_widths, default=frame["w"])
        widths = sorted({width, maximum, maximum / 2, maximum / 3})
        insets = comp.margins_pt or {}
        min_inner = float(insets.get("l", 0)) + float(insets.get("r", 0))
        for index, candidate_width in enumerate(widths):
            if candidate_width <= min_inner or abs(candidate_width - width) < 1 / 12700:
                continue
            pid = f"measure-{len(probe_map)}"
            # Intrinsic probe height is separate from the target box.
            probe = comp.model_copy(
                update={
                    "id": pid,
                    "box_pt": RectPt(x=0, y=0, w=candidate_width, h=max(frame["h"], need)),
                }
            )
            probe_map[pid] = (cid, candidate_width, index)
            probes.append(probe)
    evidence: list[dict] = []
    if contentless:
        evidence.append(
            {
                "action": "authored_geometry_without_content",
                "components": sorted(contentless),
                "note": "no text to measure; own authored box kept, no fabricated fit",
            }
        )
    if probes:
        response = measurer(
            request.model_copy(
                update={
                    "request_id": f"{request.request_id}-intrinsic-widths",
                    "components": probes,
                    "input_hashes": {},
                }
            )
        )
        items = {i["component_id"]: i for i in response.get("items", [])}
        missing = set(probe_map) - set(items)
        if missing:
            raise ValueError(f"MEASUREMENT_COVERAGE_GAP:{sorted(missing)}")
        for pid, (cid, width, _) in probe_map.items():
            item = items[pid]
            height = float(item.get("required_outer_height_pt", 0) or 0)
            if height <= 0 or item.get("overflow_x"):
                continue
            confidence = item.get("confidence")
            if confidence not in allowed_confidence:
                raise ValueError(f"MEASUREMENT_ITEM_UNSUPPORTED:{pid}:{confidence}")
            variants[cid].append({"w": width, "h": height})
        evidence.append(
            {
                "action": "intrinsic_width_variants",
                "requests": len(probes),
                "variants": dict(variants),
            }
        )
    for cid, rows in variants.items():
        if not rows:
            raise ValueError(f"MEASUREMENT_WIDTH_INFEASIBLE:{cid}")
    by_ref: dict[str, str] = dict(source_bindings or {})
    for cid, comp in components.items():
        for ref in getattr(comp, "content_refs", []) or []:
            by_ref[str(ref)] = cid
        if getattr(comp, "payload_ref", ""):
            by_ref[str(comp.payload_ref)] = cid
    containment = []
    for constraint in constraints or []:
        value = getattr(constraint, "value", {}) or {}
        targets = [by_ref.get(str(r), str(r)) for r in constraint.target_ids]
        if (
            constraint.kind == "allowed_overlap"
            and value.get("relation") == "surface_child"
            and value.get("scope") == "container_containment"
            and len(targets) == 2
        ):
            parent, child = targets
            if parent in hints and child in hints and parent != child:
                p, c = hints[parent], hints[child]
                containers.add(parent)
                padding = {
                    "l": max(0, c["x"] - p["x"]),
                    "t": max(0, c["y"] - p["y"]),
                    "r": max(0, p["x"] + p["w"] - c["x"] - c["w"]),
                    "b": max(0, p["y"] + p["h"] - c["y"] - c["h"]),
                }
                containment.append((parent, child, padding))
    precedence: list[tuple[str, str, str, float]] = []
    for constraint in constraints or []:
        if getattr(constraint, "kind", "") != "sequence":
            continue
        targets = list(dict.fromkeys(by_ref.get(str(r), str(r)) for r in constraint.target_ids))
        for a, b in zip(targets, targets[1:], strict=False):
            if (
                a == b
                or a not in hints
                or b not in hints
                or a in containers
                or b in containers
                or frozenset((a, b)) in allowed
            ):
                continue
            # Read the direction from authored target positions, never
            # from source XML order. A reading sequence can be a row.
            axis = "x" if hints[b]["x"] >= hints[a]["x"] + hints[a]["w"] else "y"
            precedence.append((a, b, axis, 0.0))
    result = solve_measured_boxes(
        hints,
        frame,
        movable=[cid for cid in hints if cid not in fixed],
        variants=variants,
        allowed_pairs=allowed,
        container_ids=containers,
        precedences=precedence,
        containments=containment,
        seconds=seconds,
    )
    evidence.append(
        {"action": "constraint_layout", **result.stats, "chosen_variants": result.choices}
    )
    return result, evidence
