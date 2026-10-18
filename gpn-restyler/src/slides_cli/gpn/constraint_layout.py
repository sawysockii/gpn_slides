"""Measured rectangle placement used by the production layout entry point.

All dimensions are measurements or authored geometry. This module contains
no typography, palette, corporate spacing constants, or content mutation.
Pairwise linear disjunctions support explicit underlay exemptions without
forbidding protected objects from overlapping each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from itertools import combinations
from math import isfinite
from typing import Any

EMU_PER_PT = 12700


def _q(value: float, *, upper: bool = False) -> int:
    if not isfinite(float(value)):
        raise ValueError("NON_FINITE_GEOMETRY")
    return int(
        (Decimal(str(value)) * EMU_PER_PT).to_integral_value(
            rounding=ROUND_CEILING if upper else ROUND_FLOOR
        )
    )


@dataclass
class ConstraintResult:
    status: str
    boxes: dict[str, dict[str, float]] = field(default_factory=dict)
    choices: dict[str, int] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)


def _contains(outer: dict, inner: dict) -> bool:
    return (
        inner["x"] >= outer["x"]
        and inner["y"] >= outer["y"]
        and inner["x"] + inner["w"] <= outer["x"] + outer["w"]
        and inner["y"] + inner["h"] <= outer["y"] + outer["h"]
    )


def solve_measured_boxes(
    hints: dict[str, dict[str, float]],
    frame: dict[str, float],
    *,
    movable: list[str] | None = None,
    variants: dict[str, list[dict[str, float]]] | None = None,
    allowed_pairs: set[frozenset] | None = None,
    container_ids: set[str] | None = None,
    precedences: list[tuple[str, str, str, float]] | None = None,
    containments: list[tuple[str, str, dict[str, float]]] | None = None,
    seconds: float = 5.0,
) -> ConstraintResult:
    """Solve positions and measured width/height choices simultaneously.

    INFEASIBLE proves only this finite model. No node is dropped, no
    measured text dimension is shrunk, and UNKNOWN is never called dense.
    Container height can change; its required content remains contained.
    """
    from ortools.sat.python import cp_model

    if seconds <= 0:
        raise ValueError("SOLVER_BUDGET_NON_POSITIVE")
    allowed = allowed_pairs or set()
    containers = container_ids or set()
    alternatives = variants or {}
    order = list(hints)
    active = set(order if movable is None else movable)
    if not active.issubset(hints):
        raise ValueError("MOVABLE_NODE_UNKNOWN")
    left, top = _q(frame["x"], upper=True), _q(frame["y"], upper=True)
    right = _q(float(Decimal(str(frame["x"])) + Decimal(str(frame["w"]))))
    bottom = _q(float(Decimal(str(frame["y"])) + Decimal(str(frame["h"]))))
    if right <= left or bottom <= top:
        raise ValueError("NON_POSITIVE_PARENT_REGION")
    model = cp_model.CpModel()
    nodes: dict[str, dict[str, Any]] = {}
    objective = []
    bound = 8 * (sum(abs(v) for v in (left, top, right, bottom)) + right - left + bottom - top + 1)
    containment: list[tuple[str, str, dict[str, float]]] = list(containments or [])
    explicit_containment = {frozenset((a, b)) for a, b, _ in containment}
    for parent, child, padding in containment:
        if parent not in hints or child not in hints or parent not in containers:
            raise ValueError("CONTAINER_BINDING_INVALID")
        if any(v < 0 for v in padding.values()):
            raise ValueError("CONTAINER_PADDING_NEGATIVE")
    for pair in allowed:
        if len(pair) != 2 or not pair.issubset(hints):
            raise ValueError(f"OVERLAP_PERMISSION_UNKNOWN:{sorted(pair)}")
        a, b = sorted(pair)
        if pair in explicit_containment:
            continue
        if a in containers and _contains(hints[a], hints[b]):
            containment.append((a, b, {}))
        elif b in containers and _contains(hints[b], hints[a]):
            containment.append((b, a, {}))

    for cid, box in hints.items():
        for key in ("x", "y", "w", "h"):
            if not isfinite(float(box[key])):
                raise ValueError(f"NON_FINITE_GEOMETRY:{cid}:{key}")
        if box["w"] < 0 or box["h"] < 0:
            raise ValueError(f"NEGATIVE_EXTENT:{cid}")
        # A zero-extent path has a zero area, never a manufactured text slot.
        if cid not in active:
            x, y = model.new_constant(_q(box["x"])), model.new_constant(_q(box["y"]))
            w = model.new_constant(_q(box["w"], upper=True))
            h = model.new_constant(_q(box["h"], upper=True))
            choice = None
        else:
            x = model.new_int_var(left, right, f"{cid}:x")
            y = model.new_int_var(top, bottom, f"{cid}:y")
            rows = alternatives.get(cid) or [{"w": box["w"], "h": box["h"]}]
            if any(r["w"] <= 0 or r["h"] <= 0 for r in rows):
                if box["w"] == 0 or box["h"] == 0:
                    rows = [{"w": box["w"], "h": box["h"]}]
                else:
                    raise ValueError(f"NON_POSITIVE_MEASURED_VARIANT:{cid}")
            widths = [_q(r["w"], upper=True) for r in rows]
            heights = [_q(r["h"], upper=True) for r in rows]
            w = model.new_int_var(min(widths), max(widths), f"{cid}:w")
            if cid in containers:
                h = model.new_int_var(1, bottom - top, f"{cid}:h")
                choice = None
                if len(set(widths)) != 1:
                    raise ValueError("CONTAINER_WIDTH_REQUIRES_CHILD_LAYOUT_VARIANTS")
                model.add(w == widths[0])
            else:
                h = model.new_int_var(min(heights), max(heights), f"{cid}:h")
                choice = model.new_int_var(0, len(rows) - 1, f"{cid}:variant")
                model.add_allowed_assignments(
                    [choice, w, h], [(i, widths[i], heights[i]) for i in range(len(rows))]
                )
            model.add(x + w <= right)
            model.add(y + h <= bottom)
            # Keep authored composition unless a change is needed to fit.
            for key, variable, weight in (("x", x, 1), ("y", y, 1), ("w", w, 8)):
                target = _q(box[key])
                deviation = model.new_int_var(0, bound + abs(target), f"{cid}:d:{key}")
                model.add_abs_equality(deviation, variable - target)
                objective.append(weight * deviation)
            if cid in containers:
                deviation = model.new_int_var(0, bound, f"{cid}:d:h")
                model.add_abs_equality(deviation, h - _q(box["h"]))
                objective.append(deviation)
        nodes[cid] = {"x": x, "y": y, "w": w, "h": h, "choice": choice}

    for parent, child, padding in containment:
        p, c = nodes[parent], nodes[child]
        model.add(c["x"] >= p["x"] + _q(padding.get("l", 0), upper=True))
        model.add(c["y"] >= p["y"] + _q(padding.get("t", 0), upper=True))
        model.add(c["x"] + c["w"] <= p["x"] + p["w"] - _q(padding.get("r", 0), upper=True))
        model.add(c["y"] + c["h"] <= p["y"] + p["h"] - _q(padding.get("b", 0), upper=True))
    for a, b in combinations(order, 2):
        if (a not in active and b not in active) or frozenset((a, b)) in allowed:
            continue
        # Degenerate (unfilled) paths are checked by existing route gates.
        if any(hints[c]["w"] == 0 or hints[c]["h"] == 0 for c in (a, b)):
            continue
        first, second = nodes[a], nodes[b]
        directions = [model.new_bool_var(f"separate:{a}:{b}:{i}") for i in range(4)]
        for flag, lhs, rhs in (
            (directions[0], first["x"] + first["w"], second["x"]),
            (directions[1], second["x"] + second["w"], first["x"]),
            (directions[2], first["y"] + first["h"], second["y"]),
            (directions[3], second["y"] + second["h"], first["y"]),
        ):
            model.add(lhs <= rhs).only_enforce_if(flag)
        model.add_bool_or(directions)
    for before, after, axis, gap in precedences or []:
        if before not in nodes or after not in nodes or axis not in ("x", "y") or gap < 0:
            raise ValueError("INVALID_PRECEDENCE")
        a, b = nodes[before], nodes[after]
        extent = "w" if axis == "x" else "h"
        model.add(b[axis] >= a[axis] + a[extent] + _q(gap, upper=True))
    if objective:
        model.minimize(sum(objective))
    validation = model.validate()
    if validation:
        return ConstraintResult("MODEL_INVALID", issues=[validation])
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = seconds
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    status = solver.solve(model)
    name = {
        cp_model.OPTIMAL: "OPTIMAL",
        cp_model.FEASIBLE: "FEASIBLE",
        cp_model.INFEASIBLE: "INFEASIBLE",
        cp_model.UNKNOWN: "UNKNOWN",
        cp_model.MODEL_INVALID: "MODEL_INVALID",
    }.get(status, "UNKNOWN")
    stats = {
        "solver_calls": 1,
        "status": name,
        "wall_seconds": solver.wall_time,
        "scope": "supplied measured variants and declared constraints only",
    }
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return ConstraintResult(name, stats=stats, issues=[f"CONSTRAINT_GEOMETRY_{name}"])
    result: dict[str, dict[str, float]] = {}
    selected: dict[str, int] = {}
    for cid, node in nodes.items():
        result[cid] = {k: solver.value(node[k]) / EMU_PER_PT for k in ("x", "y", "w", "h")}
        if node["choice"] is not None:
            selected[cid] = solver.value(node["choice"])
    # Verify the numerical result independently of CP-SAT's status.
    eps = 2 / EMU_PER_PT
    for cid in active:
        b = result[cid]
        if (
            b["x"] < frame["x"] - eps
            or b["y"] < frame["y"] - eps
            or b["x"] + b["w"] > frame["x"] + frame["w"] + eps
            or b["y"] + b["h"] > frame["y"] + frame["h"] + eps
        ):
            raise RuntimeError("SOLVER_CONTAINMENT_POSTCHECK_FAILED")
    for a, b in combinations(order, 2):
        if (a not in active and b not in active) or frozenset((a, b)) in allowed:
            continue
        first, second = result[a], result[b]
        if (
            min(first["x"] + first["w"], second["x"] + second["w"]) - max(first["x"], second["x"])
            > eps
            and min(first["y"] + first["h"], second["y"] + second["h"])
            - max(first["y"], second["y"])
            > eps
        ):
            raise RuntimeError("SOLVER_OVERLAP_POSTCHECK_FAILED")
    return ConstraintResult(name, result, selected, stats)
