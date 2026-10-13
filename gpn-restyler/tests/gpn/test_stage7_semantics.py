"""Stage 7 §6.2/§12.1: evidence-backed omit + relation preservation."""

from __future__ import annotations

from types import SimpleNamespace

from slides_cli.gpn.models import (
    SemanticRelation,
    SemanticSlide,
    SemanticUnit,
    SourceEvidenceRef,
    TransformationAuthorization,
    TransformationDecision,
    TransformLedger,
    TypedContentPayload,
)
from slides_cli.gpn.semantics import (
    build_semantic_projection,
    can_omit_source_object,
    check_semantic_relations,
)

SNAP = "snap-1"


def _line(begin=None, end=None):
    return SimpleNamespace(begin_arrow=begin, end_arrow=end)


def _obj(oid, kind="shape", text="", begin=None, end=None):
    paras = [SimpleNamespace(
        runs=[SimpleNamespace(text=text, kind="text", field=None)])] \
        if text else []
    payload = SimpleNamespace(
        paragraphs=paras, source_line=_line(begin, end))
    return SimpleNamespace(id=oid, kind=kind, payload=payload)


def _slide_with_arrow(oid="arrow1"):
    ev = SourceEvidenceRef(artifact_id="a", relative_path="p",
                           sha256="s", object_id=oid)
    return SemanticSlide(
        source_slide_id="s1",
        units=[SemanticUnit(id="u1", kind="text",
                            content=TypedContentPayload(text="A"))],
        relations=[SemanticRelation(
            id="rel1", kind="flow", from_unit_ids=["u1"],
            to_unit_ids=["u2"], directed=True, source_refs=[ev])])


def test_required_textless_arrow_cannot_auto_omit():
    obj = _obj("arrow1", begin="none", end="triangle")
    slide = _slide_with_arrow()
    ledger = TransformLedger(source_snapshot=SNAP, decisions=[])
    d = can_omit_source_object(obj, slide, ledger, SNAP)
    assert not d.allowed
    assert "required semantic relation" in d.reason


def test_unclassified_arrow_needs_review():
    obj = _obj("stray1", begin="none", end="triangle")
    slide = SemanticSlide(source_slide_id="s1")
    ledger = TransformLedger(source_snapshot=SNAP, decisions=[])
    d = can_omit_source_object(obj, slide, ledger, SNAP)
    assert not d.allowed
    assert d.needs_review


def test_arrow_with_authorized_replace_passes():
    obj = _obj("arrow1", begin="none", end="triangle")
    slide = _slide_with_arrow()
    ledger = TransformLedger(source_snapshot=SNAP, decisions=[
        TransformationDecision(
            source_object_id="arrow1", source_unit_ids=[],
            action="replace", target_relation_ids=["edge:arrow1"],
            authorization=TransformationAuthorization(
                authorized_by="harness", basis="harness_review",
                ref="arrow_assessment"),
            reason="re-expressed as native edge", source_snapshot=SNAP)])
    d = can_omit_source_object(obj, slide, ledger, SNAP)
    assert d.allowed
    assert "harness" in d.authorization_ref


def test_stale_snapshot_decision_does_not_authorize():
    obj = _obj("arrow1", begin="none", end="triangle")
    slide = _slide_with_arrow()
    ledger = TransformLedger(source_snapshot="other", decisions=[
        TransformationDecision(
            source_object_id="arrow1", action="replace",
            target_relation_ids=["edge:arrow1"],
            authorization=TransformationAuthorization(
                authorized_by="harness", basis="harness_review", ref="x"),
            source_snapshot="other")])
    d = can_omit_source_object(obj, slide, ledger, SNAP)
    assert not d.allowed


def test_text_bearing_object_never_decor():
    obj = _obj("t1", text="400 FTE")
    slide = SemanticSlide(source_slide_id="s1")
    d = can_omit_source_object(obj, slide, TransformLedger(
        source_snapshot=SNAP), SNAP)
    assert not d.allowed
    assert not d.needs_review


def test_authorized_nonsemantic_decor_omit_passes():
    obj = _obj("frame1")
    slide = SemanticSlide(source_slide_id="s1")
    ledger = TransformLedger(source_snapshot=SNAP, decisions=[
        TransformationDecision(
            source_object_id="frame1", action="omit",
            authorization=TransformationAuthorization(
                authorized_by="harness", basis="harness_review",
                ref="decor_proof"),
            reason="empty frame, image region reviewed",
            source_snapshot=SNAP)])
    d = can_omit_source_object(obj, slide, ledger, SNAP)
    assert d.allowed


def test_undecided_shape_needs_review():
    obj = _obj("mystery1")
    slide = SemanticSlide(source_slide_id="s1")
    d = can_omit_source_object(obj, slide, TransformLedger(
        source_snapshot=SNAP), SNAP)
    assert not d.allowed
    assert d.needs_review


def test_dropped_required_relation_rejected():
    slide = _slide_with_arrow()
    deck = SimpleNamespace(slides=[slide])
    findings = check_semantic_relations(deck, set(), {"u1"})
    assert any(f.code == "RELATION_DROPPED" for f in findings)


def test_reexpressed_relation_preserves_projection():
    slide = _slide_with_arrow()
    deck = SimpleNamespace(slides=[slide])
    findings = check_semantic_relations(deck, {"rel1"}, {"u1"})
    assert findings == []


def test_projection_extracts_arrows_as_required_relations():
    source_ir = {"slides": [{
        "id": "slide0:ppt/slides/slide1.xml",
        "objects": [
            {"id": "a1", "kind": "shape",
             "payload": {"paragraphs": [],
                         "source_line": {"begin_arrow": "none",
                                         "end_arrow": "triangle"}}},
            {"id": "t1", "kind": "text",
             "payload": {"paragraphs": [
                 {"runs": [{"text": "hello", "kind": "text"}]}],
                 "source_line": {}}},
        ]}]}
    deck = build_semantic_projection(source_ir, "s" * 64)
    assert len(deck.slides) == 1
    assert len(deck.slides[0].relations) == 1
    assert deck.slides[0].relations[0].preservation == "required"
    assert deck.slides[0].relations[0].directed is True
    assert len(deck.slides[0].units) == 1
    assert deck.sha256  # own hash, immutable projection


def test_projection_hash_stable():
    source_ir = {"slides": [{
        "id": "s", "objects": [
            {"id": "t1", "kind": "text",
             "payload": {"paragraphs": [
                 {"runs": [{"text": "x", "kind": "text"}]}],
                 "source_line": {}}}]}]}
    a = build_semantic_projection(source_ir, "s" * 64)
    b = build_semantic_projection(source_ir, "s" * 64)
    assert a.sha256 == b.sha256
