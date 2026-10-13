"""Stage 5 §6: intent + semantic graph -> typed component tree/constraints.

``compile_composition`` turns an accepted LayoutIntent into a CompositionTree:
content-bearing components with exact source bindings, geometry/ordering
constraints from real operator bindings, and protected template regions.
No pixels are finalized here and no payload is rewritten.
"""

from __future__ import annotations

from typing import Any

from .models import (
    ComponentNode,
    CompositionTree,
    ContentBinding,
    LayoutConstraint,
)

SEMANTIC_ONLY_OPERATORS = frozenset(
    {"aggregate", "drill_down", "reconcile", "normalize"})


def _pct_to_pt(zone: Any, content_box_pt: dict[str, float]) -> dict[str, float]:
    return {
        "x": content_box_pt["x"] + content_box_pt["w"] * float(zone.x_pct) / 100.0,
        "y": content_box_pt["y"] + content_box_pt["h"] * float(zone.y_pct) / 100.0,
        "w": content_box_pt["w"] * float(zone.w_pct) / 100.0,
        "h": content_box_pt["h"] * float(zone.h_pct) / 100.0,
    }


def _infer_organization(zones: list[Any]) -> str:
    """Infer zone organization from pct geometry (deterministic rules)."""
    if len(zones) <= 1:
        return "leaf"
    boxes = [(float(z.x_pct), float(z.y_pct), float(z.w_pct), float(z.h_pct))
             for z in zones]
    # containment -> nest
    for i, a in enumerate(boxes):
        for j, b in enumerate(boxes):
            if i != j and (b[0] >= a[0] and b[1] >= a[1]
                           and b[0] + b[2] <= a[0] + a[2] + 1.0
                           and b[1] + b[3] <= a[1] + a[3] + 1.0):
                return "nest"
    # pairwise overlap -> overlay
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            if (a[0] < b[0] + b[2] and b[0] < a[0] + a[2]
                    and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]):
                return "overlay"
    xs = sorted(b[0] for b in boxes)
    ys = sorted(b[1] for b in boxes)
    x_spread = max(xs) - min(xs)
    y_spread = max(ys) - min(ys)
    return "h-stack" if x_spread >= y_spread else "v-stack"


def _containment_overlap_constraints(
        objects: dict[str, Any],
        components: dict[str, ComponentNode]) -> list[LayoutConstraint]:
    """Scoped container allowances from source geometry (§11.1).

    A text component fully inside an area-shape's source box is a nest
    containment, not a content-on-content occlusion: emit an exact-pair
    ``allowed_overlap`` (source origin). Anything else stays forbidden.
    """
    from .units import emu_to_pt

    boxes: dict[str, tuple[float, float, float, float]] = {}
    for cid, node in components.items():
        obj = objects.get(node.payload_ref)
        box = getattr(obj, "local_box", None) if obj is not None else None
        if box is None:
            continue
        try:
            boxes[cid] = (emu_to_pt(box.x), emu_to_pt(box.y),
                          emu_to_pt(box.w), emu_to_pt(box.h))
        except (TypeError, ValueError, AttributeError):
            continue
    out: list[LayoutConstraint] = []
    ids = sorted(boxes)
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = ids[i], ids[j]
            na, nb = components[a], components[b]
            kinds = {na.kind, nb.kind}
            if "text" not in kinds:
                continue
            shape_cid = a if na.kind == "shape" else (
                b if nb.kind == "shape" else "")
            text_cid = b if shape_cid == a else a
            if not shape_cid:
                continue
            sx, sy, sw, sh = boxes[shape_cid]
            tx, ty, tw, th = boxes[text_cid]
            if sw < 24.0 or sh < 24.0:
                continue
            if (tx >= sx - 0.75 and ty >= sy - 0.75
                    and tx + tw <= sx + sw + 0.75
                    and ty + th <= sy + sh + 0.75):
                shape_ref = components[shape_cid].payload_ref
                text_ref = components[text_cid].payload_ref
                out.append(LayoutConstraint(
                    id=f"allow-{shape_cid}-{text_cid}",
                    kind="allowed_overlap",
                    # object (payload) ids: resolved geometry is keyed by
                    # object id, and the validator matches exact pairs.
                    target_ids=[shape_ref, text_ref],
                    value={"scope": "container_containment",
                           "relation": "nest"},
                    strength="hard",
                    origin="source",
                    evidence_refs=[f"source-box:{shape_ref}"],
                ))
    return out


def _intent_overlap_constraints(intent: Any,
                                objects: dict[str, Any]
                                ) -> tuple[list[LayoutConstraint], list[str]]:
    """Explicit nest pairs declared by the accepted intent (§11.1).

    Only exact pairs of bound refs become ``allowed_overlap`` (hard,
    intent origin). Unknown refs are issues, never silent allowances.
    """
    out: list[LayoutConstraint] = []
    issues: list[str] = []
    for pair in list(getattr(intent, "allowed_overlaps", []) or []):
        refs = [str(r) for r in list(pair or [])]
        if len(refs) != 2:
            issues.append(f"ALLOWED_OVERLAP_MALFORMED: {refs!r} is not a pair")
            continue
        if any(r not in objects for r in refs):
            issues.append(f"ALLOWED_OVERLAP_UNKNOWN: {refs!r} not in source")
            continue
        out.append(LayoutConstraint(
            id=f"allow-intent-{refs[0][:8]}-{refs[1][:8]}",
            kind="allowed_overlap", target_ids=refs,
            value={"scope": "container_containment", "relation": "nest"},
            strength="hard", origin="intent",
            evidence_refs=[f"intent:{getattr(intent, 'candidate_id', '')}"]))
    return out, issues


def _apply_operator(tree: CompositionTree, op: str, intent: Any,
                    allowed: set[str]) -> str:
    """Apply one operator binding; returns coverage status."""
    if op not in allowed:
        tree.issues.append(f"OPERATOR_NOT_ALLOWED: {op!r} not in snapshot "
                            "composition_operators")
        return "needs_binding"
    if op in SEMANTIC_ONLY_OPERATORS:
        tree.issues.append(f"OPERATOR_SEMANTIC_ONLY: {op!r} has no verified "
                            "geometric decomposition without a checked "
                            "semantic contract")
        return "needs_binding"
    handler = OPERATOR_BINDINGS.get(op)
    if handler is None:
        tree.issues.append(f"OPERATOR_NO_BINDING: {op!r} allowed by snapshot "
                            "but has no implementation binding")
        return "needs_binding"
    before = (len(tree.constraints),
              sum(len(c.constraints) for c in tree.components.values()))
    handler(tree, intent)
    after = (len(tree.constraints),
             sum(len(c.constraints) for c in tree.components.values()))
    if after == before:
        tree.issues.append(f"OPERATOR_NO_EFFECT: {op!r} binding added no "
                            "constraint or construct")
        return "partial"
    return "implemented"


# ---------------------------------------------------------------------------
# §6.1 operator implementation bindings (real geometric effect)
# ---------------------------------------------------------------------------

def _bind_align(tree: CompositionTree, intent: Any) -> None:
    refs = list(getattr(intent, "reading_order", []) or [])
    tree.constraints.append(LayoutConstraint(
        id=f"align-{intent.candidate_id or 'x'}",
        kind="align",
        target_ids=refs,
        value={"edges": "left", "tolerance_pt": 1.0},
        strength="soft",
        origin="intent",
        evidence_refs=[f"intent:{getattr(intent, 'candidate_id', '')}"],
    ))


def _bind_juxtapose(tree: CompositionTree, intent: Any) -> None:
    zones = list(getattr(intent, "zones", []) or [])
    if len(zones) >= 2:
        tree.constraints.append(LayoutConstraint(
            id=f"juxtapose-{zones[0].zone_id}-{zones[1].zone_id}",
            kind="juxtapose",
            target_ids=[f"zone:{z.zone_id}" for z in zones[:2]],
            value={"axis": "horizontal", "shared_anchor": "top",
                   "preserve_reading_order": True},
            strength="soft",
            origin="intent",
            evidence_refs=[f"intent:{getattr(intent, 'candidate_id', '')}"],
        ))


def _bind_contrast(tree: CompositionTree, intent: Any) -> None:
    _bind_juxtapose(tree, intent)
    tree.constraints.append(LayoutConstraint(
        id=f"contrast-{getattr(intent, 'candidate_id', 'x')}",
        kind="contrast",
        target_ids=[f"zone:{z.zone_id}"
                    for z in list(getattr(intent, "zones", []) or [])[:2]],
        value={"visual_separation": "gap_and_anchor",
               "no_invented_causality": True},
        strength="soft",
        origin="intent",
        evidence_refs=[f"intent:{getattr(intent, 'candidate_id', '')}"],
    ))


def _bind_sequence(tree: CompositionTree, intent: Any) -> None:
    refs = list(getattr(intent, "reading_order", []) or [])
    if len(refs) >= 2:
        tree.constraints.append(LayoutConstraint(
            id=f"sequence-{getattr(intent, 'candidate_id', 'x')}",
            kind="sequence",
            target_ids=refs,
            value={"direction": "reading", "no_resort": True},
            strength="hard",
            origin="intent",
            evidence_refs=[f"intent:{getattr(intent, 'candidate_id', '')}"],
        ))


def _bind_nest(tree: CompositionTree, intent: Any) -> None:
    zones = list(getattr(intent, "zones", []) or [])
    if tree.root is not None and len(zones) > 1:
        tree.constraints.append(LayoutConstraint(
            id=f"nest-{getattr(intent, 'candidate_id', 'x')}",
            kind="nest",
            target_ids=[f"zone:{z.zone_id}" for z in zones],
            value={"container": "root", "padding_pt": 7.2,
                   "no_new_hierarchy": True},
            strength="hard",
            origin="intent",
            evidence_refs=[f"intent:{getattr(intent, 'candidate_id', '')}"],
        ))


def _bind_frame(tree: CompositionTree, intent: Any) -> None:
    _bind_nest(tree, intent)


def _bind_connect(tree: CompositionTree, intent: Any) -> None:
    relations = [r for r in list(getattr(intent, "relations", []) or [])
                 if "connect" in str(getattr(r, "relation_kind", "")).lower()
                 or "edge" in str(getattr(r, "relation_kind", "")).lower()]
    targets = [getattr(r, "from_ref", "") for r in relations] + \
              [getattr(r, "to_ref", "") for r in relations]
    tree.constraints.append(LayoutConstraint(
        id=f"graph-{getattr(intent, 'candidate_id', 'x')}",
        kind="graph_layout",
        target_ids=[t for t in targets if t],
        value={"delegate": "graph_layout", "keep_conditions": True},
        strength="hard",
        origin="intent",
        evidence_refs=[f"intent:{getattr(intent, 'candidate_id', '')}"],
    ))


def _bind_branch(tree: CompositionTree, intent: Any) -> None:
    _bind_connect(tree, intent)


def _bind_annotate(tree: CompositionTree, intent: Any) -> None:
    dominant = str(getattr(intent, "dominant_ref", "") or "")
    tree.constraints.append(LayoutConstraint(
        id=f"annotate-{getattr(intent, 'candidate_id', 'x')}",
        kind="annotate",
        target_ids=[dominant] if dominant else [],
        value={"proximity": "adjacent", "leader": "if_needed",
               "no_caveat_reassignment": True},
        strength="soft",
        origin="intent",
        evidence_refs=[f"intent:{getattr(intent, 'candidate_id', '')}"],
    ))


def _bind_highlight(tree: CompositionTree, intent: Any) -> None:
    dominant = str(getattr(intent, "dominant_ref", "") or "")
    tree.constraints.append(LayoutConstraint(
        id=f"highlight-{getattr(intent, 'candidate_id', 'x')}",
        kind="highlight",
        target_ids=[dominant] if dominant else [],
        value={"dominant_allocation": True, "confirmed_style_role_only": True},
        strength="soft",
        origin="intent",
        evidence_refs=[f"intent:{getattr(intent, 'candidate_id', '')}"],
    ))


def _bind_overlay(tree: CompositionTree, intent: Any) -> None:
    zones = list(getattr(intent, "zones", []) or [])
    if len(zones) >= 2:
        pair = f"{zones[0].zone_id}:{zones[1].zone_id}"
        tree.constraints.append(LayoutConstraint(
            id=f"overlay-{pair}",
            kind="allowed_overlap",
            target_ids=[f"zone:{zones[0].zone_id}", f"zone:{zones[1].zone_id}"],
            value={"pair": pair, "scope": "zone_pair",
                   "blanket_allow": False},
            strength="soft",
            origin="intent",
            evidence_refs=[f"intent:{getattr(intent, 'candidate_id', '')}"],
        ))


def _bind_repeat(tree: CompositionTree, intent: Any) -> None:
    for binding in tree.content_bindings:
        if binding.representation == "authorized_repeat":
            tree.constraints.append(LayoutConstraint(
                id=f"repeat-{binding.component_id}",
                kind="repeat",
                target_ids=[binding.component_id],
                value={"shared_scale": True,
                       "permission_ref": binding.permission_ref},
                strength="hard",
                origin="intent",
                evidence_refs=[f"intent:{getattr(intent, 'candidate_id', '')}"],
            ))


def _bind_facet(tree: CompositionTree, intent: Any) -> None:
    _bind_repeat(tree, intent)


OPERATOR_BINDINGS: dict[str, Any] = {
    "align": _bind_align,
    "juxtapose": _bind_juxtapose,
    "contrast": _bind_contrast,
    "sequence": _bind_sequence,
    "nest": _bind_nest,
    "frame": _bind_frame,
    "connect": _bind_connect,
    "branch": _bind_branch,
    "annotate": _bind_annotate,
    "highlight": _bind_highlight,
    "overlay": _bind_overlay,
    "repeat": _bind_repeat,
    "facet": _bind_facet,
}


# ---------------------------------------------------------------------------
# reference lineage gate (§0.2)
# ---------------------------------------------------------------------------

_USER_CORPUS_BASENAMES = frozenset({"slide_examples.pptx"})
_UPSTREAM_BASENAMES = frozenset(
    {"example1.pptx", "example2.pptx", "example3.pptx"})


def _check_reference_ids(intent: Any) -> None:
    """Reject plans with foreign reference lineage (no fallback).

    Empty reference_ids (novel/proposed geometry) are legitimate. Non-empty
    IDs must resolve to the user corpus; upstream sample names, URLs and
    absolute outside paths raise REFERENCE/FORBIDDEN_SOURCE.
    """
    for ref in list(getattr(intent, "reference_ids", []) or []):
        text = str(ref)
        if not text:
            continue
        lowered = text.lower()
        if lowered.startswith(("http://", "https://", "ftp://")):
            raise ValueError(
                f"REFERENCE/FORBIDDEN_SOURCE: remote reference {text!r}")
        base = lowered.split(":")[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if base in _UPSTREAM_BASENAMES or "/examples/" in lowered.replace("\\", "/"):
            raise ValueError(
                f"REFERENCE/FORBIDDEN_SOURCE: upstream sample {text!r}")
        if base not in _USER_CORPUS_BASENAMES and "/" in text.replace("\\", "/"):
            raise ValueError(
                f"REFERENCE/FORBIDDEN_SOURCE: outside user corpus {text!r}")


# ---------------------------------------------------------------------------
# compile_composition
# ---------------------------------------------------------------------------

def compile_composition(
    intent: Any,
    source_slide: Any,
    ledger: Any,
    rules: Any,
    profile: Any,
    font_bundle: Any = None,
    *,
    template_hash: str = "",
) -> CompositionTree:
    """Intent + source + rules -> typed component tree and constraints."""
    from .planning_bridge import _resolve_content_box

    issues: list[str] = []
    _check_reference_ids(intent)
    objects = {o.id: o for o in (getattr(source_slide, "objects", []) or [])}

    content_refs = list(getattr(intent, "content_refs", []) or [])
    unknown_refs = [r for r in content_refs if r not in objects]
    if unknown_refs:
        issues.append(f"CONTENT_REF_UNKNOWN: {unknown_refs[:4]} not in source")
    intent_overlaps, overlap_issues = _intent_overlap_constraints(
        intent, objects)
    issues.extend(overlap_issues)

    order = list(getattr(intent, "reading_order", []) or [])
    if [r for r in order if r in objects] != [r for r in content_refs if r in objects]:
        issues.append("READING_ORDER_MISMATCH: reading order does not cover "
                      "exactly the bound content refs")

    roles = getattr(rules, "roles", {}) or {}
    allowed_ops = set(getattr(rules, "composition_operators", []) or [])

    box_in, box_verified, box_source, box_issues = _resolve_content_box(
        profile, rules)
    issues.extend(box_issues)
    content_box_pt = {
        "x": box_in.get("x", 0.5) * 72.0,
        "y": box_in.get("y", 0.7) * 72.0,
        "w": box_in.get("w", 9.0) * 72.0,
        "h": box_in.get("h", 4.5) * 72.0,
    } if box_in else {"x": 36.0, "y": 50.4, "w": 648.0, "h": 324.0}

    zone_by_ref: dict[str, Any] = {}
    for zone in list(getattr(intent, "zones", []) or []):
        for ref in list(getattr(zone, "content_refs", []) or []):
            zone_by_ref[ref] = zone

    components: dict[str, ComponentNode] = {}
    bindings: list[ContentBinding] = []
    seen_atoms: set[str] = set()
    repeat_allowed = any(
        op in ("repeat", "facet") for op in list(getattr(intent, "operators", []) or []))
    for ref in content_refs:
        obj = objects.get(ref)
        if obj is None:
            continue
        zone = zone_by_ref.get(ref)
        zone_role = str(getattr(zone, "role", "") or "") if zone else ""
        payload_role = str(getattr(getattr(obj, "payload", None), "role", "") or "")
        style_role = ""
        if zone_role in roles:
            style_role = zone_role
        elif payload_role in roles:
            style_role = payload_role
        else:
            issues.append(f"STYLE_ROLE_UNBOUND: no ontology role for object "
                          f"{ref} (zone={zone_role!r} payload={payload_role!r})")
        kind = str(getattr(obj, "kind", "text") or "text")
        node = ComponentNode(
            id=f"c-{ref[:8]}",
            kind=kind if kind in (
                "text", "table", "chart", "shape", "group", "image",
                "connector") else "shape",
            content_refs=[ref],
            style_role=style_role,
            children=[],
            organization="leaf",
            constraints=([LayoutConstraint(
                id=f"hint-{ref[:8]}",
                kind="relative_box",
                target_ids=[f"c-{ref[:8]}"],
                value=_pct_to_pt(zone, content_box_pt) if zone else {},
                strength="soft",
                origin="intent",
                evidence_refs=[f"zone:{getattr(zone, 'zone_id', '')}"] if zone else [],
            )] if zone else []),
            payload_ref=ref,
            renderer_id="native",
        )
        # Primary representation: one component per required occurrence.
        representation = "primary"
        if ref in seen_atoms:
            if repeat_allowed:
                representation = "authorized_repeat"
            else:
                issues.append(f"DUPLICATE_CONTENT_REF: {ref} shown twice "
                              "without repeat/facet authorization")
        seen_atoms.add(ref)
        components[node.id] = node
        bindings.append(ContentBinding(
            component_id=node.id,
            source_object_id=ref,
            representation=representation,  # type: ignore[arg-type]
            permission_ref="intent:repeat" if representation == "authorized_repeat" else "",
        ))

    organization = _infer_organization(
        list(getattr(intent, "zones", []) or []))
    # Children keep content_refs (reading) order, never sorted-id order:
    # same-zone vertical stacking and overlap-repair priority must follow
    # the accepted reading order (sequence operator: no_resort). Sorting
    # by component id silently swapped stacked sentences (stage6 review:
    # outcome pair and inputs pair rendered in wrong visual order).
    root = ComponentNode(
        id="root", kind="container", content_refs=[],
        style_role="", children=list(components),
        organization=organization, constraints=[], payload_ref="",
        renderer_id="native")

    protected: list[dict[str, Any]] = []
    for prot in list(getattr(profile, "protected_objects", []) or []):
        dump = prot.model_dump(mode="json") if hasattr(prot, "model_dump") else dict(prot)
        protected.append(dump)

    tree = CompositionTree(
        slide_id=str(getattr(intent, "slide_id", "")),
        candidate_id=str(getattr(intent, "candidate_id", "") or "c0"),
        source_hash=str(getattr(source_slide, "source_sha256", "")
                        or getattr(getattr(source_slide, "source_ref", None), "deck_sha256", "")),
        ontology_snapshot_hash=str(getattr(rules, "source_hash", "")
                                   or getattr(rules, "ontology_corpus_hash", "")),
        template_hash=template_hash or str(getattr(profile, "template_hash", "")),
        root=root,
        components=components,
        content_bindings=bindings,
        constraints=[*_containment_overlap_constraints(objects, components),
                     *intent_overlaps],
        protected_regions=protected,
        operator_coverage={},
        issues=issues,
    )
    coverage: dict[str, str] = {}
    seen_ops: set[str] = set()
    for op in list(getattr(intent, "operators", []) or []):
        if op in seen_ops:
            continue
        seen_ops.add(op)
        coverage[op] = _apply_operator(tree, op, intent, allowed_ops)
    tree.operator_coverage = coverage
    if getattr(intent, "novel_recipe", ""):
        tree.issues.append(
            "NOVEL_RECIPE: proposed/extrapolated combination, not observed")
    return tree
