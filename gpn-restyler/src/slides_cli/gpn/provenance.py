"""Source ledger construction (spec §7.7, §16.1).

The ledger atomizes every significant source atom so the independent
preservation gate (§16.2) can prove nothing was lost or invented.
"""

from __future__ import annotations

from .models import (
    ChartPayload,
    DecorationDecision,
    GroupPayload,
    LedgerAtom,
    NewChartPayload,
    ObjectIR,
    SourceDeckIR,
    SourceLedger,
    TablePayload,
    TextPayload,
)


def _paragraph_text(payload: TextPayload) -> str:
    parts: list[str] = []
    for para in payload.paragraphs:
        text = "".join(run.text for run in para.runs)
        parts.append(text)
    return "\n".join(parts)


def _atomize_text_payload(
    payload: TextPayload,
    obj: ObjectIR,
    atoms: list[LedgerAtom],
    order: list[str],
    kind: str = "text",
) -> None:
    for para in payload.paragraphs:
        text = "".join(run.text for run in para.runs)
        atoms.append(
            LedgerAtom(
                id=para.id,
                source_ref=para.source_ref,
                kind=kind,
                canonical_value=text,
                visibility="visible" if obj.visible else "hidden",
                required=True,
                permitted_transforms=["wrap", "style_only"],
            )
        )
        order.append(para.id)
        for run in para.runs:
            if run.hyperlink is not None:
                atom_id = f"{run.id}#hyperlink"
                atoms.append(
                    LedgerAtom(
                        id=atom_id,
                        source_ref=run.source_ref,
                        kind="hyperlink",
                        canonical_value=run.hyperlink.uri
                        or run.hyperlink.target_slide_ref or "",
                        visibility="visible" if obj.visible else "hidden",
                        required=True,
                        permitted_transforms=["style_only"],
                    )
                )
                order.append(atom_id)


def _atomize_object(obj: ObjectIR, atoms: list[LedgerAtom], order: list[str]) -> None:
    payload = obj.payload

    if isinstance(payload, TextPayload):
        _atomize_text_payload(payload, obj, atoms, order)
        return

    if isinstance(payload, TablePayload):
        for cell in payload.cells:
            text = _paragraph_text(
                TextPayload(paragraphs=cell.paragraphs)
            )
            atoms.append(
                LedgerAtom(
                    id=cell.id,
                    source_ref=cell.source_ref,
                    kind="table_cell",
                    canonical_value=text,
                    visibility="visible" if (obj.visible and cell.visible) else "hidden",
                    required=True,
                    permitted_transforms=["wrap", "style_only"],
                )
            )
            order.append(cell.id)
        return

    if isinstance(payload, (ChartPayload, NewChartPayload)):
        if isinstance(payload, ChartPayload):
            for series in payload.series:
                for point in series.points:
                    value = None
                    for candidate in (point.value, point.y, point.x):
                        if candidate is not None and candidate.display_text:
                            value = candidate.display_text
                            break
                    atom_id = point.id
                    atoms.append(
                        LedgerAtom(
                            id=atom_id,
                            source_ref=obj.source_ref,
                            kind="chart_point",
                            canonical_value=value if value is not None else "",
                            visibility="visible" if obj.visible else "hidden",
                            required=True,
                            permitted_transforms=["style_only"],
                        )
                    )
                    order.append(atom_id)
        else:
            for series in payload.data.series:
                for point in series.points:
                    atoms.append(
                        LedgerAtom(
                            id=point.id,
                            source_ref=obj.source_ref,
                            kind="chart_point",
                            canonical_value=(point.value.display_text if point.value else ""),
                            required=True,
                            permitted_transforms=["style_only"],
                        )
                    )
                    order.append(point.id)
        return

    if isinstance(payload, GroupPayload):
        for child in payload.children:
            _atomize_object(child, atoms, order)
        if payload.text is not None:
            _atomize_text_payload(payload.text, obj, atoms, order)
        return

    if obj.kind == "connector":
        text = getattr(payload, "text", None)
        if text is not None:
            for para in text.paragraphs:
                value = "".join(run.text for run in para.runs)
                atoms.append(
                    LedgerAtom(
                        id=para.id,
                        source_ref=para.source_ref,
                        kind="diagram_edge",
                        canonical_value=value,
                        required=True,
                        permitted_transforms=["style_only"],
                    )
                )
                order.append(para.id)
        else:
            atoms.append(
                LedgerAtom(
                    id=f"{obj.id}#edge",
                    source_ref=obj.source_ref,
                    kind="diagram_edge",
                    canonical_value=f"{payload.from_object_id}->{payload.to_object_id}",
                    required=True,
                    permitted_transforms=["style_only"],
                )
            )
            order.append(f"{obj.id}#edge")
        return

    if obj.kind == "image":
        atoms.append(
            LedgerAtom(
                id=f"{obj.id}#asset",
                source_ref=obj.source_ref,
                kind="asset",
                canonical_value=payload.asset.sha256,
                required=True,
                permitted_transforms=["relocate", "style_only"],
            )
        )
        order.append(f"{obj.id}#asset")
        return

    if obj.kind == "shape":
        shape_text = getattr(payload, "text", None)
        if shape_text is not None:
            _atomize_text_payload(shape_text, obj, atoms, order)
        else:
            atoms.append(
                LedgerAtom(
                    id=f"{obj.id}#shape",
                    source_ref=obj.source_ref,
                    kind="diagram_node",
                    canonical_value=payload.geometry_token or obj.name or "",
                    required=True,
                    permitted_transforms=["style_only", "relocate"],
                )
            )
            order.append(f"{obj.id}#shape")
        return

    # unknown objects: keep their extracted text and raw identity
    extracted = getattr(payload, "extracted_text", [])
    if extracted:
        for text_payload in extracted:
            _atomize_text_payload(text_payload, obj, atoms, order)
    else:
        atoms.append(
            LedgerAtom(
                id=f"{obj.id}#raw",
                source_ref=obj.source_ref,
                kind="asset",
                canonical_value=obj.name or obj.id,
                required=True,
                permitted_transforms=[],
            )
        )
        order.append(f"{obj.id}#raw")


def build_ledger(deck: SourceDeckIR, decoration: list[DecorationDecision]) -> SourceLedger:
    """Atomize all significant content of a deck (spec §16.1).

    Content atoms are ``required=True``; only explicitly recorded decoration
    decisions may exclude an object. Unknown significance stays required.
    """
    excluded = {d.object_id for d in decoration if not d.requires_review}
    atoms: list[LedgerAtom] = []
    order: list[str] = []

    for slide in deck.slides:
        for obj in slide.objects:
            if obj.id in excluded:
                continue
            _atomize_object(obj, atoms, order)
        if slide.notes:
            for para in slide.notes:
                value = "".join(run.text for run in para.runs)
                atoms.append(
                    LedgerAtom(
                        id=f"{slide.id}/{para.id}",
                        source_ref=para.source_ref,
                        kind="notes",
                        canonical_value=value,
                        visibility="notes",
                        required=True,
                        permitted_transforms=["wrap"],
                    )
                )
                order.append(f"{slide.id}/{para.id}")

    return SourceLedger(atoms=atoms, excluded_decoration=list(decoration), source_order=order)


def classify_decoration(
    objects: list[ObjectIR],
    profile: object | None = None,
    policy: object | None = None,
) -> list[DecorationDecision]:
    """Decide which objects are confirmed decoration (spec §10.7).

    Without a verified template profile no object may be declared decorative:
    facts, footnotes and captions are never decoration just because they sit at
    the bottom of a slide.
    """
    decisions: list[DecorationDecision] = []
    if profile is None:
        return decisions
    for obj in objects:
        if obj.semantic_significance == "decoration":
            decisions.append(
                DecorationDecision(
                    object_id=obj.id,
                    reason="classifier marked object as decoration",
                    evidence=obj.classification_evidence,
                    policy_rule_id="DECORATION_CONFIRMED",
                    requires_review=False,
                )
            )
    return decisions
