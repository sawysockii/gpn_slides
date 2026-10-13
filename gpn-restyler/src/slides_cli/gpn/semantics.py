"""Stage 7 §5/§6: immutable semantic projection + evidence-backed transforms.

The source ledger is immutable; the semantic projection is derived before
any target generation and carries its own hash. Omitting a source object
requires an authorizing decision for the current source snapshot — never
the absence of text. In particular, a text-less shape with arrowheads is
a *candidate relation*, not decoration: ``can_omit_source_object`` refuses
it unless a harness-reviewed decision re-expresses the relation natively
with preserved direction, or proves it nonsemantic with image evidence.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .models import (
    OmitDecision,
    SemanticDeck,
    SemanticRelation,
    SemanticSlide,
    SemanticUnit,
    SourceEvidenceRef,
    TransformLedger,
    TypedContentPayload,
    TypedFinding,
)


def _hash_slides(slides: list[SemanticSlide]) -> str:
    canonical = json.dumps(
        [s.model_dump(mode="json") for s in slides],
        sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _iter_text(payload: Any) -> str:
    parts: list[str] = []
    inner = getattr(payload, "text", None)
    paras = getattr(payload, "paragraphs", None)
    if paras is None and inner is not None:
        paras = getattr(inner, "paragraphs", None)
    for para in list(paras or []):
        for run in list(getattr(para, "runs", []) or []):
            text = getattr(run, "text", "") or ""
            if getattr(run, "kind", "text") == "field":
                meta = getattr(run, "field", None)
                text = getattr(meta, "cached_text", "") or text
            if text:
                parts.append(text)
    return "".join(parts)


def _line_arrows(payload: Any) -> tuple[str | None, str | None]:
    line = getattr(payload, "source_line", None)
    if line is None:
        return None, None
    return (getattr(line, "begin_arrow", None),
            getattr(line, "end_arrow", None))


def _is_arrow(begin: str | None, end: str | None) -> bool:
    return any(a not in (None, "", "none") for a in (begin, end))


def _object_evidence_ref(obj: Any, slide_part: str,
                         deck_sha: str) -> SourceEvidenceRef:
    return SourceEvidenceRef(
        artifact_id=f"source-object:{getattr(obj, 'id', '')}",
        relative_path="inputs/source_ir.json",
        sha256=deck_sha,
        slide_part=slide_part,
        object_id=str(getattr(obj, "id", "")))


def build_semantic_projection(source_ir: dict[str, Any],
                              source_sha256: str) -> SemanticDeck:
    """Deterministic semantic projection (§6.1).

    Text-bearing objects become required units; arrow-bearing shapes become
    required relations with directed=True. Geometry-adjacent labels are NOT
    assigned as from/to here — causality is confirmed by harness review of
    the source render, not by nearest-textbox heuristics. Connector objects
    (p:cxnSp) keep their endpoint refs when the importer recorded them.
    """
    slides: list[SemanticSlide] = []
    units: dict[str, SemanticUnit] = {}
    relations: dict[str, SemanticRelation] = {}
    evidence: list[SourceEvidenceRef] = []

    for slide in source_ir.get("slides", []):
        slide_id = str(slide.get("id", ""))
        slide_part = slide_id.split(":", 1)[-1] \
            if ":" in slide_id else slide_id
        sem_units: list[SemanticUnit] = []
        sem_rels: list[SemanticRelation] = []
        for obj in slide.get("objects", []):
            oid = str(obj.get("id", ""))
            kind = str(obj.get("kind", ""))
            payload = obj.get("payload", {}) or {}
            text = _text_of_dict(payload)
            begin, end = _arrows_of_dict(payload)
            ev = SourceEvidenceRef(
                artifact_id=f"source-object:{oid}",
                relative_path="inputs/source_ir.json",
                sha256=source_sha256,
                slide_part=slide_part, object_id=oid)
            evidence.append(ev)
            if _is_arrow(begin, end):
                rel = SemanticRelation(
                    id=f"rel:{oid[:12]}", kind="flow",
                    directed=True,
                    conditions=["from_to_pending_harness_confirmation"],
                    source_refs=[ev], preservation="required")
                sem_rels.append(rel)
                relations[rel.id] = rel
                continue
            if text.strip():
                unit = SemanticUnit(
                    id=f"unit:{oid[:12]}", kind="text",
                    content=TypedContentPayload(text=text),
                    source_refs=[ev],
                    preservation="required_semantic")
                sem_units.append(unit)
                units[unit.id] = unit
            elif kind in ("image", "table", "chart"):
                unit = SemanticUnit(
                    id=f"unit:{oid[:12]}", kind=kind,
                    content=TypedContentPayload(
                        media_ref=oid if kind == "image" else None,
                        data_ref=oid if kind in ("table", "chart") else None),
                    source_refs=[ev],
                    preservation="required_semantic")
                sem_units.append(unit)
                units[unit.id] = unit
        slides.append(SemanticSlide(
            source_slide_id=slide_id, units=sem_units, relations=sem_rels))

    deck = SemanticDeck(
        source_manifest_sha256=source_sha256, slides=slides,
        units=units, relations=relations, evidence=evidence,
        sha256="")
    deck.sha256 = _hash_slides(slides)
    return deck


def _text_of_dict(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    inner = payload.get("text") or {}
    paras = payload.get("paragraphs")
    if paras is None:
        paras = inner.get("paragraphs", [])
    for para in list(paras or []):
        for run in list(para.get("runs", []) or []):
            text = run.get("text", "") or ""
            if run.get("kind") == "field":
                text = (run.get("field") or {}).get("cached_text", "") \
                    or text
            if text:
                parts.append(text)
    return "".join(parts)


def _arrows_of_dict(payload: dict[str, Any]
                    ) -> tuple[str | None, str | None]:
    line = payload.get("source_line") or {}
    return line.get("begin_arrow"), line.get("end_arrow")


def can_omit_source_object(obj: Any,
                           semantic_slide: SemanticSlide,
                           transform_ledger: TransformLedger,
                           source_snapshot: str) -> OmitDecision:
    """§6.2: omit only by authorizing decision for this source snapshot.

    1. No geometry binding is a reason to call something decoration.
    2. Required units/relations referencing this object refuse omit.
    3. Text or arrowheads refuse automatic omit.
    4. An explicit ledger decision for this object+snapshot authorizes.
    5. Everything else is unknown -> harness task, never a pass.
    """
    oid = str(getattr(obj, "id", ""))
    payload = getattr(obj, "payload", None)
    if payload is not None:
        begin, end = _line_arrows(payload)
        if _is_arrow(begin, end):
            # A reviewed replace/recompose decision re-expresses the
            # source geometry as a target relation; the required
            # relation itself is preserved in target form (§6.2.4).
            decision = transform_ledger.decision_for(oid)
            if decision is not None and \
                    decision.source_snapshot == source_snapshot and \
                    decision.action in ("replace", "recompose") and \
                    decision.target_relation_ids:
                auth = decision.authorization
                return OmitDecision(
                    allowed=True,
                    authorization_ref=f"{auth.authorized_by}:{auth.ref}",
                    reason="arrow re-expressed as native relation "
                    f"{decision.target_relation_ids}")
            # No re-expression: the required-relation refusal below
            # fires when the projection references this arrow. An
            # explicit harness omit decision is honored only when NO
            # required relation references the arrow (assessed
            # nonsemantic decor, §6.2.3); otherwise review.
            for rel in semantic_slide.relations:
                refs = [r for r in rel.source_refs
                        if (r.object_id or "").startswith(oid[:12])
                        or r.object_id == oid]
                if refs and rel.preservation == "required":
                    return OmitDecision(
                        allowed=False,
                        reason="required semantic relation "
                        f"{rel.id} references this object; re-express "
                        "natively before omit")
            decision = transform_ledger.decision_for(oid)
            if decision is not None and \
                    decision.source_snapshot == source_snapshot and \
                    decision.action == "omit" and \
                    decision.authorization.basis != "unknown":
                auth = decision.authorization
                return OmitDecision(
                    allowed=True,
                    authorization_ref=f"{auth.authorized_by}:{auth.ref}",
                    reason="arrow assessed nonsemantic decor with "
                    "explicit omit authorization; no required relation "
                    "references it")
            return OmitDecision(
                allowed=False,
                reason="arrow-bearing shape is a candidate relation; "
                "harness review required before omit",
                needs_review=True)

    for unit in semantic_slide.units:
        refs = [r for r in unit.source_refs
                if (r.object_id or "").startswith(oid[:12])
                or r.object_id == oid]
        if refs and unit.preservation != "optional":
            return OmitDecision(
                allowed=False, reason="required semantic unit "
                f"{unit.id} references this object")
    for rel in semantic_slide.relations:
        refs = [r for r in rel.source_refs
                if (r.object_id or "").startswith(oid[:12])
                or r.object_id == oid]
        if refs and rel.preservation == "required":
            return OmitDecision(
                allowed=False, reason="required semantic relation "
                f"{rel.id} references this object")

    if payload is not None and _iter_text(payload).strip():
        return OmitDecision(
            allowed=False, reason="text-bearing object cannot be "
            "omitted as decor")

    decision = transform_ledger.decision_for(oid)
    if decision is not None and \
            decision.source_snapshot == source_snapshot and \
            decision.action == "omit":
        auth = decision.authorization
        if auth.basis != "unknown":
            return OmitDecision(
                allowed=True,
                authorization_ref=f"{auth.authorized_by}:{auth.ref}",
                reason=decision.reason or "authorized optional decor omit")
    if getattr(obj, "kind", "") == "shape":
        return OmitDecision(
            allowed=False,
            reason="undecided text-less shape; classification task "
            "required",
            needs_review=True)
    return OmitDecision(
        allowed=False,
        reason=f"kind {getattr(obj, 'kind', '')} has no omit authorization")


def check_semantic_relations(source: SemanticDeck,
                             target_relation_ids: set[str],
                             target_units: set[str]) -> list[TypedFinding]:
    """§6.2.4: every required source relation must have target representation.

    Independent of geometry omission: a re-expressed native sequence/edge
    preserves the projection, a dropped relation is rejected.
    """
    findings: list[TypedFinding] = []
    for slide in source.slides:
        for rel in slide.relations:
            if rel.preservation != "required":
                continue
            if rel.id not in target_relation_ids:
                findings.append(TypedFinding(
                    code="RELATION_DROPPED", severity="error",
                    message=f"required relation {rel.id} ({rel.kind}) has "
                            f"no target representation",
                    scope=slide.source_slide_id))
    return findings
