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

SEMANTIC_ONLY_OPERATORS = frozenset({"aggregate", "drill_down", "reconcile", "normalize"})


def _pct_to_pt(zone: Any, content_box_pt: dict[str, float]) -> dict[str, float]:
    return {
        "x": content_box_pt["x"] + content_box_pt["w"] * float(zone.x_pct) / 100.0,
        "y": content_box_pt["y"] + content_box_pt["h"] * float(zone.y_pct) / 100.0,
        "w": content_box_pt["w"] * float(zone.w_pct) / 100.0,
        "h": content_box_pt["h"] * float(zone.h_pct) / 100.0,
    }


def _object_paragraphs(obj: Any) -> list[Any]:
    """Paragraphs of a text object or text-bearing shape, or [].

    Mirrors the reader's payload layout (``payload.paragraphs`` for text,
    ``payload.text.paragraphs`` for shapes) so rich-text grouping never
    guesses about a payload that carries no text.
    """
    payload = getattr(obj, "payload", None)
    if payload is None:
        return []
    paragraphs = getattr(payload, "paragraphs", None)
    if paragraphs is None:
        inner = getattr(payload, "text", None)
        paragraphs = getattr(inner, "paragraphs", None) if inner else None
    return list(paragraphs or [])


def _stroke_width_pt(obj: Any) -> float:
    """Saved stroke width in Pt; 1.0 when the source declares none."""
    payload = getattr(obj, "payload", None)
    line = getattr(payload, "source_line", None) if payload is not None else None
    emu = getattr(line, "width_emu", None) if line is not None else None
    if emu:
        try:
            return max(float(emu) / 12700.0, 0.1)
        except (TypeError, ValueError):
            pass
    return 1.0


def _is_stroked_path(obj: Any) -> bool:
    """True when the source object is a stroked path, not a filled area.

    Evidence from the saved IR, not a guess from the kind: a preset
    ``line`` geometry, or a text-less shape whose source box has zero
    height but real width (a horizontal rule). Such an object paints only
    its stroke envelope and must never reserve a body-text slot (§4.2).
    """
    payload = getattr(obj, "payload", None)
    if payload is None or _object_paragraphs(obj):
        return False
    token = str(getattr(payload, "geometry_token", "") or "").lower()
    if token in ("line", "straightconnector1", "bentconnector"):
        return True
    if token:
        return False
    box = getattr(obj, "local_box", None)
    if box is None:
        return False
    try:
        height = float(getattr(box, "h", 0) or 0)
        width = float(getattr(box, "w", 0) or 0)
    except (TypeError, ValueError):
        return False
    return height <= 1.0 and width > 0.0


def _infer_organization(zones: list[Any]) -> str:
    """Infer zone organization from pct geometry (deterministic rules)."""
    if len(zones) <= 1:
        return "leaf"
    boxes = [(float(z.x_pct), float(z.y_pct), float(z.w_pct), float(z.h_pct)) for z in zones]
    # containment -> nest
    for i, a in enumerate(boxes):
        for j, b in enumerate(boxes):
            if i != j and (
                b[0] >= a[0]
                and b[1] >= a[1]
                and b[0] + b[2] <= a[0] + a[2] + 1.0
                and b[1] + b[3] <= a[1] + a[3] + 1.0
            ):
                return "nest"
    # pairwise overlap -> overlay
    for i, a in enumerate(boxes):
        for b in boxes[i + 1 :]:
            if (
                a[0] < b[0] + b[2]
                and b[0] < a[0] + a[2]
                and a[1] < b[1] + b[3]
                and b[1] < a[1] + a[3]
            ):
                return "overlay"
    xs = sorted(b[0] for b in boxes)
    ys = sorted(b[1] for b in boxes)
    x_spread = max(xs) - min(xs)
    y_spread = max(ys) - min(ys)
    return "h-stack" if x_spread >= y_spread else "v-stack"


def _containment_overlap_constraints(
    objects: dict[str, Any], components: dict[str, ComponentNode]
) -> list[LayoutConstraint]:
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
            boxes[cid] = (emu_to_pt(box.x), emu_to_pt(box.y), emu_to_pt(box.w), emu_to_pt(box.h))
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
            shape_cid = a if na.kind == "shape" else (b if nb.kind == "shape" else "")
            text_cid = b if shape_cid == a else a
            if not shape_cid:
                continue
            sx, sy, sw, sh = boxes[shape_cid]
            tx, ty, tw, th = boxes[text_cid]
            if sw < 24.0 or sh < 24.0:
                continue
            if (
                tx >= sx - 0.75
                and ty >= sy - 0.75
                and tx + tw <= sx + sw + 0.75
                and ty + th <= sy + sh + 0.75
            ):
                shape_ref = components[shape_cid].payload_ref
                text_ref = components[text_cid].payload_ref
                out.append(
                    LayoutConstraint(
                        id=f"allow-{shape_cid}-{text_cid}",
                        kind="allowed_overlap",
                        # object (payload) ids: resolved geometry is keyed by
                        # object id, and the validator matches exact pairs.
                        target_ids=[shape_ref, text_ref],
                        value={"scope": "container_containment", "relation": "nest"},
                        strength="hard",
                        origin="source",
                        evidence_refs=[f"source-box:{shape_ref}"],
                    )
                )
    return out


def _intent_overlap_constraints(
    intent: Any, objects: dict[str, Any]
) -> tuple[list[LayoutConstraint], list[str]]:
    """Overlap permissions of the accepted intent (§5.4/§11.1).

    A pair is permitted only when a typed ``OverlapPermission`` names the
    surface, the content, the relation kind, the scope and the evidence.
    A bare ``allowed_overlaps`` pair with no such permission is refused with
    ``OVERLAP_PERMISSION_UNJUSTIFIED``: the recovery run shipped 1085 such
    pairs on one slide, produced by crossing every surface of the slide
    with every content object on it, which would silently permit an
    unrelated caption to be moved under a neighbouring opaque panel later.
    """
    from .models import OverlapPermission  # noqa: PLC0415

    out: list[LayoutConstraint] = []
    issues: list[str] = []
    permissions: dict[tuple[str, str], Any] = {}
    for raw in list(getattr(intent, "overlap_permissions", []) or []):
        perm = raw if isinstance(raw, OverlapPermission) else OverlapPermission.model_validate(raw)
        refs = (str(perm.underlay_id), str(perm.content_id))
        if not all(refs) or refs[0] == refs[1]:
            issues.append(f"OVERLAP_PERMISSION_MALFORMED: {perm!r}")
            continue
        if any(r not in objects for r in refs):
            issues.append(f"OVERLAP_PERMISSION_UNKNOWN: {refs!r} not in source")
            continue
        if not perm.evidence_ref:
            issues.append(f"OVERLAP_PERMISSION_NO_EVIDENCE: {refs!r} declares no evidence_ref")
            continue
        key = tuple(sorted(refs))
        if key in permissions:
            issues.append(f"OVERLAP_PERMISSION_DUPLICATE: {refs!r} declared twice")
            continue
        permissions[key] = perm
        out.append(
            LayoutConstraint(
                id=f"allow-intent-{refs[0][:8]}-{refs[1][:8]}-{perm.allowed_kind}",
                kind="allowed_overlap",
                target_ids=list(refs),
                value={
                    "scope": perm.scope,
                    "relation": perm.allowed_kind,
                    "permission_ref": perm.relation_id,
                },
                strength="hard",
                origin="intent",
                evidence_refs=[perm.evidence_ref, f"permission:{perm.relation_id}"],
            )
        )
    for pair in list(getattr(intent, "allowed_overlaps", []) or []):
        refs = [str(r) for r in list(pair or [])]
        if len(refs) != 2:
            issues.append(f"ALLOWED_OVERLAP_MALFORMED: {refs!r} is not a pair")
            continue
        if any(r not in objects for r in refs):
            issues.append(f"ALLOWED_OVERLAP_UNKNOWN: {refs!r} not in source")
            continue
        if tuple(sorted(refs)) in permissions:
            continue
        issues.append(
            f"OVERLAP_PERMISSION_UNJUSTIFIED: pair {refs!r} has no typed "
            "permission (underlay/content/relation/scope/evidence); refused "
            "instead of silently allowing an unrelated occlusion"
        )
    return out, issues


def _apply_operator(tree: CompositionTree, op: str, intent: Any, allowed: set[str]) -> str:
    """Apply one operator binding; returns coverage status."""
    if op not in allowed:
        tree.issues.append(f"OPERATOR_NOT_ALLOWED: {op!r} not in snapshot composition_operators")
        return "needs_binding"
    if op in SEMANTIC_ONLY_OPERATORS:
        tree.issues.append(
            f"OPERATOR_SEMANTIC_ONLY: {op!r} has no verified "
            "geometric decomposition without a checked "
            "semantic contract"
        )
        return "needs_binding"
    handler = OPERATOR_BINDINGS.get(op)
    if handler is None:
        tree.issues.append(
            f"OPERATOR_NO_BINDING: {op!r} allowed by snapshot but has no implementation binding"
        )
        return "needs_binding"
    before = (len(tree.constraints), sum(len(c.constraints) for c in tree.components.values()))
    handler(tree, intent)
    after = (len(tree.constraints), sum(len(c.constraints) for c in tree.components.values()))
    if after == before:
        tree.issues.append(f"OPERATOR_NO_EFFECT: {op!r} binding added no constraint or construct")
        return "partial"
    return "implemented"


# ---------------------------------------------------------------------------
# §6.1 operator implementation bindings (real geometric effect)
# ---------------------------------------------------------------------------


def _bind_align(tree: CompositionTree, intent: Any) -> None:
    refs = list(getattr(intent, "reading_order", []) or [])
    tree.constraints.append(
        LayoutConstraint(
            id=f"align-{intent.candidate_id or 'x'}",
            kind="align",
            target_ids=refs,
            value={"edges": "left", "tolerance_pt": 1.0},
            strength="soft",
            origin="intent",
            evidence_refs=[f"intent:{getattr(intent, 'candidate_id', '')}"],
        )
    )


def _bind_juxtapose(tree: CompositionTree, intent: Any) -> None:
    zones = list(getattr(intent, "zones", []) or [])
    if len(zones) >= 2:
        tree.constraints.append(
            LayoutConstraint(
                id=f"juxtapose-{zones[0].zone_id}-{zones[1].zone_id}",
                kind="juxtapose",
                target_ids=[f"zone:{z.zone_id}" for z in zones[:2]],
                value={
                    "axis": "horizontal",
                    "shared_anchor": "top",
                    "preserve_reading_order": True,
                },
                strength="soft",
                origin="intent",
                evidence_refs=[f"intent:{getattr(intent, 'candidate_id', '')}"],
            )
        )


def _bind_contrast(tree: CompositionTree, intent: Any) -> None:
    _bind_juxtapose(tree, intent)
    tree.constraints.append(
        LayoutConstraint(
            id=f"contrast-{getattr(intent, 'candidate_id', 'x')}",
            kind="contrast",
            target_ids=[f"zone:{z.zone_id}" for z in list(getattr(intent, "zones", []) or [])[:2]],
            value={"visual_separation": "gap_and_anchor", "no_invented_causality": True},
            strength="soft",
            origin="intent",
            evidence_refs=[f"intent:{getattr(intent, 'candidate_id', '')}"],
        )
    )


def _bind_sequence(tree: CompositionTree, intent: Any) -> None:
    refs = list(getattr(intent, "reading_order", []) or [])
    if len(refs) >= 2:
        tree.constraints.append(
            LayoutConstraint(
                id=f"sequence-{getattr(intent, 'candidate_id', 'x')}",
                kind="sequence",
                target_ids=refs,
                value={"direction": "reading", "no_resort": True},
                strength="hard",
                origin="intent",
                evidence_refs=[f"intent:{getattr(intent, 'candidate_id', '')}"],
            )
        )


def _bind_nest(tree: CompositionTree, intent: Any) -> None:
    zones = list(getattr(intent, "zones", []) or [])
    nest_id = f"nest-{getattr(intent, 'candidate_id', 'x')}"
    # `frame` already binds a nest container; listing both `frame` and `nest`
    # must not append the same constraint id twice. A duplicated hard
    # constraint makes the allocator's ordering unstable (the same plan
    # reorders text blocks only when the operator is declared twice).
    if any(c.id == nest_id for c in tree.constraints):
        return
    if tree.root is not None and len(zones) > 1:
        tree.constraints.append(
            LayoutConstraint(
                id=nest_id,
                kind="nest",
                target_ids=[f"zone:{z.zone_id}" for z in zones],
                value={"container": "root", "padding_pt": 7.2, "no_new_hierarchy": True},
                strength="hard",
                origin="intent",
                evidence_refs=[f"intent:{getattr(intent, 'candidate_id', '')}"],
            )
        )


def _bind_frame(tree: CompositionTree, intent: Any) -> None:
    _bind_nest(tree, intent)


def _bind_connect(tree: CompositionTree, intent: Any) -> None:
    relations = [
        r
        for r in list(getattr(intent, "relations", []) or [])
        if "connect" in str(getattr(r, "relation_kind", "")).lower()
        or "edge" in str(getattr(r, "relation_kind", "")).lower()
    ]
    targets = [getattr(r, "from_ref", "") for r in relations] + [
        getattr(r, "to_ref", "") for r in relations
    ]
    tree.constraints.append(
        LayoutConstraint(
            id=f"graph-{getattr(intent, 'candidate_id', 'x')}",
            kind="graph_layout",
            target_ids=[t for t in targets if t],
            value={"delegate": "graph_layout", "keep_conditions": True},
            strength="hard",
            origin="intent",
            evidence_refs=[f"intent:{getattr(intent, 'candidate_id', '')}"],
        )
    )


def _bind_branch(tree: CompositionTree, intent: Any) -> None:
    _bind_connect(tree, intent)


def _bind_annotate(tree: CompositionTree, intent: Any) -> None:
    dominant = str(getattr(intent, "dominant_ref", "") or "")
    tree.constraints.append(
        LayoutConstraint(
            id=f"annotate-{getattr(intent, 'candidate_id', 'x')}",
            kind="annotate",
            target_ids=[dominant] if dominant else [],
            value={"proximity": "adjacent", "leader": "if_needed", "no_caveat_reassignment": True},
            strength="soft",
            origin="intent",
            evidence_refs=[f"intent:{getattr(intent, 'candidate_id', '')}"],
        )
    )


def _bind_highlight(tree: CompositionTree, intent: Any) -> None:
    dominant = str(getattr(intent, "dominant_ref", "") or "")
    tree.constraints.append(
        LayoutConstraint(
            id=f"highlight-{getattr(intent, 'candidate_id', 'x')}",
            kind="highlight",
            target_ids=[dominant] if dominant else [],
            value={"dominant_allocation": True, "confirmed_style_role_only": True},
            strength="soft",
            origin="intent",
            evidence_refs=[f"intent:{getattr(intent, 'candidate_id', '')}"],
        )
    )


def _bind_overlay(tree: CompositionTree, intent: Any) -> None:
    zones = list(getattr(intent, "zones", []) or [])
    if len(zones) >= 2:
        pair = f"{zones[0].zone_id}:{zones[1].zone_id}"
        tree.constraints.append(
            LayoutConstraint(
                id=f"overlay-{pair}",
                kind="allowed_overlap",
                target_ids=[f"zone:{zones[0].zone_id}", f"zone:{zones[1].zone_id}"],
                value={"pair": pair, "scope": "zone_pair", "blanket_allow": False},
                strength="soft",
                origin="intent",
                evidence_refs=[f"intent:{getattr(intent, 'candidate_id', '')}"],
            )
        )


def _bind_repeat(tree: CompositionTree, intent: Any) -> None:
    for binding in tree.content_bindings:
        if binding.representation == "authorized_repeat":
            tree.constraints.append(
                LayoutConstraint(
                    id=f"repeat-{binding.component_id}",
                    kind="repeat",
                    target_ids=[binding.component_id],
                    value={"shared_scale": True, "permission_ref": binding.permission_ref},
                    strength="hard",
                    origin="intent",
                    evidence_refs=[f"intent:{getattr(intent, 'candidate_id', '')}"],
                )
            )


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
_UPSTREAM_BASENAMES = frozenset({"example1.pptx", "example2.pptx", "example3.pptx"})


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
            raise ValueError(f"REFERENCE/FORBIDDEN_SOURCE: remote reference {text!r}")
        base = lowered.split(":")[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if base in _UPSTREAM_BASENAMES or "/examples/" in lowered.replace("\\", "/"):
            raise ValueError(f"REFERENCE/FORBIDDEN_SOURCE: upstream sample {text!r}")
        if base not in _USER_CORPUS_BASENAMES and "/" in text.replace("\\", "/"):
            raise ValueError(f"REFERENCE/FORBIDDEN_SOURCE: outside user corpus {text!r}")


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
    from .frame_contract import _resolve_content_box

    issues: list[str] = []
    _check_reference_ids(intent)
    objects = {o.id: o for o in (getattr(source_slide, "objects", []) or [])}

    content_refs = list(getattr(intent, "content_refs", []) or [])
    unknown_refs = [r for r in content_refs if r not in objects]
    if unknown_refs:
        issues.append(f"CONTENT_REF_UNKNOWN: {unknown_refs[:4]} not in source")
    intent_overlaps, overlap_issues = _intent_overlap_constraints(intent, objects)
    issues.extend(overlap_issues)

    order = list(getattr(intent, "reading_order", []) or [])
    if [r for r in order if r in objects] != [r for r in content_refs if r in objects]:
        issues.append(
            "READING_ORDER_MISMATCH: reading order does not cover exactly the bound content refs"
        )

    roles = getattr(rules, "roles", {}) or {}
    allowed_ops = set(getattr(rules, "composition_operators", []) or [])

    box_in, box_verified, box_source, box_issues = _resolve_content_box(profile, rules)
    issues.extend(box_issues)
    content_box_pt = (
        {
            "x": box_in.get("x", 0.5) * 72.0,
            "y": box_in.get("y", 0.7) * 72.0,
            "w": box_in.get("w", 9.0) * 72.0,
            "h": box_in.get("h", 4.5) * 72.0,
        }
        if box_in
        else {"x": 36.0, "y": 50.4, "w": 648.0, "h": 324.0}
    )

    zone_by_ref: dict[str, Any] = {}
    for zone in list(getattr(intent, "zones", []) or []):
        for ref in list(getattr(zone, "content_refs", []) or []):
            zone_by_ref[ref] = zone

    components: dict[str, ComponentNode] = {}
    bindings: list[ContentBinding] = []
    seen_atoms: set[str] = set()
    repeat_allowed = any(
        op in ("repeat", "facet") for op in list(getattr(intent, "operators", []) or [])
    )
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
            issues.append(
                f"STYLE_ROLE_UNBOUND: no ontology role for object "
                f"{ref} (zone={zone_role!r} payload={payload_role!r})"
            )
        kind = str(getattr(obj, "kind", "text") or "text")
        is_connector = kind == "connector" or _is_stroked_path(obj)
        node_kind = (
            "connector"
            if is_connector
            else (
                kind
                if kind in ("text", "table", "chart", "shape", "group", "image", "connector")
                else "shape"
            )
        )
        node = ComponentNode(
            id=f"c-{ref}",
            kind=node_kind,
            content_refs=[ref],
            style_role=style_role,
            children=[],
            organization="leaf",
            constraints=(
                [
                    LayoutConstraint(
                        id=f"hint-{ref[:8]}",
                        kind="relative_box",
                        target_ids=[f"c-{ref}"],
                        value=_pct_to_pt(zone, content_box_pt) if zone else {},
                        strength="soft",
                        origin="intent",
                        evidence_refs=[f"zone:{getattr(zone, 'zone_id', '')}"] if zone else [],
                    )
                ]
                if zone
                else []
            ),
            payload_ref=ref,
            renderer_id="native",
        )
        # §4.3: the surface contract travels with every component the zone
        # produces, not only with authored rich-text blocks. A panel declared
        # by the composition must keep its palette tokens down to the native
        # writer, otherwise a declared surface silently renders unfilled.
        if zone:
            for token_field in ("surface_token", "foreground_token", "stroke_token"):
                token = getattr(zone, token_field, "")
                if token:
                    setattr(node, token_field, token)
        if is_connector:
            # Stroke envelope comes from the saved line evidence, not from
            # a guessed minimum body height (§4.2).
            node.constraints.append(
                LayoutConstraint(
                    id=f"stroke-footprint-{ref[:8]}",
                    kind="stroke_footprint",
                    target_ids=[node.id],
                    value={"stroke_width_pt": _stroke_width_pt(obj), "is_line": True},
                    strength="hard",
                    origin="source",
                    evidence_refs=[f"source_line:{ref}"],
                )
            )
        # Primary representation: one component per required occurrence.
        representation = "primary"
        if ref in seen_atoms:
            if repeat_allowed:
                representation = "authorized_repeat"
            else:
                issues.append(
                    f"DUPLICATE_CONTENT_REF: {ref} shown twice without repeat/facet authorization"
                )
        seen_atoms.add(ref)
        components[node.id] = node
        bindings.append(
            ContentBinding(
                component_id=node.id,
                source_object_id=ref,
                representation=representation,  # type: ignore[arg-type]
                permission_ref="intent:repeat" if representation == "authorized_repeat" else "",
            )
        )

    # Group text-bearing objects by zone for rich text assembly (§3.2).
    # Grouping is by accepted authored zone, never by source object count.
    zone_text_groups: dict[str, list[str]] = {}
    zone_order: list[str] = []
    for ref in content_refs:
        obj = objects.get(ref)
        if obj is None:
            continue
        kind = str(getattr(obj, "kind", "text") or "text")
        if kind not in ("text", "shape"):
            continue
        payload = getattr(obj, "payload", None)
        if payload is None or not _object_paragraphs(obj):
            continue
        zone = zone_by_ref.get(ref)
        if zone:
            zid = zone.zone_id
            if zid not in zone_text_groups:
                zone_text_groups[zid] = []
                zone_order.append(zid)
            zone_text_groups[zid].append(ref)

    # Create rich text blocks for zones with multiple text objects
    rich_text_nodes: dict[str, ComponentNode] = {}
    for zid in zone_order:
        refs = zone_text_groups[zid]
        if len(refs) <= 1:
            continue
        node_id = f"rt-{zid}"
        zone = zone_by_ref.get(refs[0])
        zone_role = str(getattr(zone, "role", "") or "") if zone else ""
        node = ComponentNode(
            id=node_id,
            kind="rich_text",
            content_refs=refs,
            style_role=zone_role,
            ref_roles=dict(getattr(zone, "ref_roles", {}) or {}) if zone is not None else {},
            paragraph_groups=list(getattr(zone, "paragraph_groups", []) or []) if zone else [],
            inline_separator=str(getattr(zone, "inline_separator", " ")) if zone else " ",
            children=[],
            organization="leaf",
            constraints=(
                [
                    LayoutConstraint(
                        id=f"hint-{zid}",
                        kind="relative_box",
                        target_ids=[node_id],
                        value=_pct_to_pt(zone, content_box_pt) if zone else {},
                        strength="soft",
                        origin="intent",
                        evidence_refs=[f"zone:{zid}"] if zone else [],
                    )
                ]
                if zone
                else []
            ),
            payload_ref="",
            renderer_id="native",
        )
        if zone:
            for token_field in ("surface_token", "foreground_token", "stroke_token"):
                token = getattr(zone, token_field, "")
                if token:
                    setattr(node, token_field, token)
        rich_text_nodes[node_id] = node
        components[node_id] = node
        # Occurrence identity survives grouping: a ref requested twice under
        # repeat/facet authorization keeps its authorized_repeat
        # representation, otherwise merging would silently revoke the
        # operator and leave the repeat with no effect at all.
        grouped: list[ContentBinding] = []
        pending = list(bindings)
        for ref in refs:
            match = None
            for index, candidate in enumerate(pending):
                if candidate.source_object_id == ref:
                    match = pending.pop(index)
                    break
            grouped.append(
                ContentBinding(
                    component_id=node_id,
                    source_object_id=ref,
                    representation=match.representation if match else "primary",
                    permission_ref=match.permission_ref if match else "",
                )
            )
        bindings = grouped

    # Remove individual text nodes that were merged into rich text blocks
    merged_refs = set()
    for node in rich_text_nodes.values():
        merged_refs.update(node.content_refs)
    for ref in list(merged_refs):
        node_id = f"c-{ref}"
        if node_id in components:
            del components[node_id]

    bindings = [b for b in bindings if b.component_id in components]

    organization = _infer_organization(list(getattr(intent, "zones", []) or []))
    # Children keep content_refs (reading) order, never sorted-id order:
    # same-zone vertical stacking and overlap-repair priority must follow
    # the accepted reading order (sequence operator: no_resort). Sorting
    # by component id silently swapped stacked sentences (stage6 review:
    # outcome pair and inputs pair rendered in wrong visual order).
    root = ComponentNode(
        id="root",
        kind="container",
        content_refs=[],
        style_role="",
        children=list(components),
        organization=organization,
        constraints=[],
        payload_ref="",
        renderer_id="native",
    )

    protected: list[dict[str, Any]] = []
    for prot in list(getattr(profile, "protected_objects", []) or []):
        dump = prot.model_dump(mode="json") if hasattr(prot, "model_dump") else dict(prot)
        protected.append(dump)

    tree = CompositionTree(
        slide_id=str(getattr(intent, "slide_id", "")),
        candidate_id=str(getattr(intent, "candidate_id", "") or "c0"),
        slide_background_token=str(getattr(intent, "slide_background_token", "") or ""),
        source_hash=str(
            getattr(source_slide, "source_sha256", "")
            or getattr(getattr(source_slide, "source_ref", None), "deck_sha256", "")
        ),
        ontology_snapshot_hash=str(
            getattr(rules, "source_hash", "") or getattr(rules, "ontology_corpus_hash", "")
        ),
        template_hash=template_hash or str(getattr(profile, "template_hash", "")),
        root=root,
        components=components,
        content_bindings=bindings,
        constraints=[*_containment_overlap_constraints(objects, components), *intent_overlaps],
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
        tree.issues.append("NOVEL_RECIPE: proposed/extrapolated combination, not observed")
    return tree
