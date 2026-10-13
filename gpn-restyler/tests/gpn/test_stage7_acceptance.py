"""Stage 7 §1.2/§12.1: acceptance negative + minimal positive regressions.

Every false-accept class from the review must now be rejected by the same
typed ``aggregate_product_acceptance`` used by CLI and runner.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from slides_cli.gpn.acceptance import (
    EvidenceRef,
    aggregate_product_acceptance,
    build_product_expectation,
    collect_saved_manifest,
    validate_evidence,
)
from slides_cli.gpn.models import (
    ApplicabilityProof,
    ArtifactStore,
    CandidateKey,
    CapabilityInventory,
    GateEvidence,
    GateExpectation,
    GateStatus,
    InputKind,
    InputSpec,
    ProductScope,
    RectEMU,
    ReviewedPage,
    RunContext,
    SavedPackageManifest,
    SavedRenderPage,
    SavedSlide,
    SelectionEntry,
    SelectionPolicy,
    SemanticDeck,
    SemanticRelation,
    SemanticSlide,
    SemanticUnit,
    SnapshotSet,
    TypedContentPayload,
    VisualReview,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _snap(**kw) -> SnapshotSet:
    base = {"input_sha256": "a" * 64, "compiled_rules_sha256": "b" * 64}
    base.update(kw)
    return SnapshotSet(**base)


def _unit(uid: str, preservation: str = "required_semantic") -> SemanticUnit:
    return SemanticUnit(id=uid, kind="text",
                        content=TypedContentPayload(text="x"),
                        preservation=preservation)


def _deck_sha(slides) -> str:
    canonical = json.dumps(
        [s.model_dump(mode="json") for s in slides],
        sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _deck(n_slides: int = 2, relations: bool = False) -> SemanticDeck:
    slides = []
    for i in range(n_slides):
        rels = [SemanticRelation(id=f"r{i}", kind="flow",
                                from_unit_ids=[f"u{i}a"],
                                to_unit_ids=[f"u{i}b"],
                                directed=True,
                                conditions=["c"]) ] if relations else []
        slides.append(SemanticSlide(
            source_slide_id=f"s{i}", title=f"S{i}",
            units=[_unit(f"u{i}a"), _unit(f"u{i}b")],
            relations=rels))
    return SemanticDeck(source_manifest_sha256="c" * 64, slides=slides,
                        sha256=_deck_sha(slides))


def _expectation(deck: SemanticDeck, canvas=None):
    from types import SimpleNamespace
    input_spec = InputSpec(
        kind=InputKind.PPTX, path=PROJECT_ROOT / "NTC_business_AI_GPN.pptx",
        project_root=PROJECT_ROOT, output_path=Path("/tmp/out.pptx"),
        requested_scope=ProductScope.RESTYLE)
    ontology = SimpleNamespace(
        canvas=canvas or RectEMU(x=0, y=0, w=12192000, h=6858000),
        rule_registry=SimpleNamespace(rules={"C01": {}, "C02": {}}))
    return build_product_expectation(
        input_spec, deck, ontology, CapabilityInventory(fonts_verified=True),
        SelectionPolicy())


def _saved(n_slides: int = 2, canvas=None, hidden=None,
           sha: str = "f" * 64) -> SavedPackageManifest:
    slides = []
    for i in range(n_slides):
        slides.append(SavedSlide(
            sld_id=str(256 + i), r_id=f"rId{i}",
            part=f"ppt/slides/slide{i + 1}.xml",
            hidden=(hidden[i] if hidden else False)))
    return SavedPackageManifest(
        candidate_path="/tmp/candidate.pptx", candidate_sha256=sha,
        canvas=canvas or RectEMU(x=0, y=0, w=12192000, h=6858000),
        slides=slides, drawing_inventory={"shape": 3}, address_count=3)


def _gate(gid: str, status: GateStatus = GateStatus.PASS,
          checked=None, subject="f" * 64, expectation=None) -> GateEvidence:
    units = [f"{gid}:u"]
    if expectation is not None:
        for g in expectation.required_gates:
            if g.gate_id == gid:
                units = g.expected_units or [f"{gid}:u"]
    ev = GateEvidence(
        gate_id=gid, status=status,
        expected_units=units,
        checked_units=checked if checked is not None else units,
        subject_candidate_sha256=subject, snapshots=_snap())
    return ev


def _all_gates(exp, subject="f" * 64, skip=(), **overrides):
    """Passing evidence for every required gate of the expectation."""
    out = []
    for g in exp.required_gates:
        if g.gate_id in skip:
            continue
        st = overrides.get(g.gate_id, GateStatus.PASS)
        out.append(_gate(g.gate_id, st, expectation=exp, subject=subject))
    return out


def _selection(sid: str, sha: str = "f" * 64) -> SelectionEntry:
    return SelectionEntry(
        source_slide_id=sid,
        candidate_key=CandidateKey(run_id="r", source_slide_id=sid,
                                   candidate_id="c1"),
        candidate_sha256=sha, candidate_path="/tmp/candidate.pptx")


def _review(sha: str = "f" * 64, pages=2,
            verdict: str = "accept") -> VisualReview:
    return VisualReview(
        task_id="t1", candidate_sha256=sha, render_manifest_sha256="",
        pages=[ReviewedPage(page=i + 1, image=f"p{i}.png",
                            sha256="a" * 64, byte_count=10) for i in
               range(pages)],
        verdict=verdict, actual_model="test")


def _run(expected, saved, gates, reviews, selections, tmp_path):
    store = ArtifactStore(tmp_path)
    return aggregate_product_acceptance(
        expected, saved, gates, reviews, selections, artifact_store=store)


# --- §1.2 probe classes must all be false ------------------------------------

def test_empty_gates_reviews_is_not_accept(tmp_path):
    exp = _expectation(_deck(2))
    sel = [_selection("s0"), _selection("s1")]
    r = _run(exp, _saved(), [], [], sel, tmp_path)
    assert not r.accepted
    assert "GATE_MISSING" in r.error_codes


def test_gate_with_unverifiable_artifact_is_not_accept(tmp_path):
    exp = _expectation(_deck(2))
    sel = [_selection("s0"), _selection("s1")]
    gate = _all_gates(exp)[0]
    assert gate.gate_id.startswith("semantic_content_preservation:")
    gate.artifacts = [EvidenceRef(
        artifact_id="missing", relative_path="nonexistent.json",
        sha256="0" * 64, byte_count=1)]
    gates = [gate] + _all_gates(exp)[1:]
    r = _run(exp, _saved(), gates, [_review()], sel, tmp_path)
    assert not r.accepted
    assert "EVIDENCE_HASH_MISMATCH" in r.error_codes


def test_slide_count_mismatch_blocks(tmp_path):
    exp = _expectation(_deck(2))
    sel = [_selection("s0"), _selection("s1")]
    gates = _all_gates(exp)
    r = _run(exp, _saved(1), gates, [_review(pages=1)], sel, tmp_path)
    assert not r.accepted
    assert "SLIDE_SET_MISMATCH" in r.error_codes


def test_review_with_empty_pages_blocks(tmp_path):
    exp = _expectation(_deck(2))
    sel = [_selection("s0"), _selection("s1")]
    gates = _all_gates(exp)
    r = _run(exp, _saved(), gates, [_review(pages=0)], sel, tmp_path)
    assert not r.accepted
    assert "REVIEW_PAGE_SET_MISMATCH" in r.error_codes


def test_empty_expected_is_invalid(tmp_path):
    deck = SemanticDeck(source_manifest_sha256="c" * 64, slides=[],
                        sha256=_deck_sha([]))
    with pytest.raises(ValueError):
        _expectation(deck)


def test_duplicate_gate_keeps_worst_and_reports(tmp_path):
    exp = _expectation(_deck(2))
    sel = [_selection("s0"), _selection("s1")]
    gates = [_gate("package_graph", GateStatus.FAIL),
             _gate("package_graph", GateStatus.PASS)]
    gates += [_gate(g) for g in (
        "semantic_content_preservation", "semantic_relations",
        "saved_auth_rules", "verified_frame", "saved_text_exact",
        "text_fit_visibility", "geometry_overlap", "font_loading",
        "render_coverage", "visual_design", "candidate_selection",
        "assembly_linkage", "native_editability", "deck_visual_review")]
    r = _run(exp, _saved(), gates, [_review()], sel, tmp_path)
    assert not r.accepted
    assert "GATE_DUPLICATE" in r.error_codes
    # blocker and summary never disagree: scope stays fail
    assert "package" in json.dumps(r.evidence_index)


def test_stale_review_hash_blocks(tmp_path):
    exp = _expectation(_deck(2))
    sel = [_selection("s0"), _selection("s1")]
    gates = _all_gates(exp)
    r = _run(exp, _saved(), gates, [_review(sha="e" * 64)], sel, tmp_path)
    assert not r.accepted
    assert "REVIEW_STALE" in r.error_codes


def test_duplicate_selection_same_slide_blocks(tmp_path):
    exp = _expectation(_deck(2))
    sel = [_selection("s0"), _selection("s0")]
    gates = _all_gates(exp)
    r = _run(exp, _saved(), gates, [_review()], sel, tmp_path)
    assert not r.accepted
    assert "SELECTION_INVALID" in r.error_codes


def test_missing_candidate_hash_blocks(tmp_path):
    exp = _expectation(_deck(2))
    sel = [_selection("s0"), _selection("s1")]
    for e in sel:
        e.candidate_sha256 = ""
    gates = _all_gates(exp)
    r = _run(exp, _saved(), gates, [_review()], sel, tmp_path)
    assert not r.accepted
    assert "SELECTION_INVALID" in r.error_codes


# --- additional §12.1 negatives ------------------------------------------------

def test_selected_candidate_not_assembled_blocks(tmp_path):
    exp = _expectation(_deck(2))
    sel = [_selection("s0", sha="9" * 64), _selection("s1", sha="9" * 64)]
    gates = _all_gates(exp)
    r = _run(exp, _saved(), gates, [_review()], sel, tmp_path)
    assert not r.accepted
    assert "SELECTED_CANDIDATE_NOT_ASSEMBLED" in r.error_codes


def test_hidden_flags_mismatch_blocks(tmp_path):
    exp = _expectation(_deck(2))
    sel = [_selection("s0"), _selection("s1")]
    gates = _all_gates(exp)
    r = _run(exp, _saved(hidden=[False, True]), gates, [_review()], sel,
             tmp_path)
    assert not r.accepted
    assert "HIDDEN_FLAGS_MISMATCH" in r.error_codes


def test_canvas_mismatch_blocks(tmp_path):
    exp = _expectation(_deck(2))
    sel = [_selection("s0"), _selection("s1")]
    gates = _all_gates(exp)
    other = RectEMU(x=0, y=0, w=10000000, h=6858000)
    r = _run(exp, _saved(canvas=other), gates, [_review()], sel, tmp_path)
    assert not r.accepted
    assert "CANVAS_MISMATCH" in r.error_codes


def test_not_applicable_without_proof_blocks(tmp_path):
    exp = _expectation(_deck(2))
    sel = [_selection("s0"), _selection("s1")]
    gate = _gate("semantic_relations", GateStatus.NOT_APPLICABLE,
                 expectation=exp)
    gate.not_applicable_evidence = None
    gates = [gate] + _all_gates(exp, skip=("semantic_relations",))
    r = _run(exp, _saved(), gates, [_review()], sel, tmp_path)
    assert not r.accepted


def test_not_applicable_with_proof_passes(tmp_path):
    exp = _expectation(_deck(2, relations=True))
    sel = [_selection("s0"), _selection("s1")]
    gate = _gate("semantic_relations", GateStatus.NOT_APPLICABLE,
                 expectation=exp)
    gate.not_applicable_evidence = ApplicabilityProof(
        reason="deck has no native charts; chart rule scope",
        inventory_evidence=["drawing_inventory: no chart parts"])
    gates = [gate] + _all_gates(exp, skip=("semantic_relations",))
    r = _run(exp, _saved(), gates, [_review()], sel, tmp_path)
    assert r.accepted, [f.message for f in r.findings]
    assert r.evidence_index["gates"]["semantic_relations"] == \
        "not_applicable"


def test_unknown_gate_status_blocks(tmp_path):
    exp = _expectation(_deck(2))
    sel = [_selection("s0"), _selection("s1")]
    gates = _all_gates(exp, saved_auth_rules=GateStatus.UNKNOWN)
    r = _run(exp, _saved(), gates, [_review()], sel, tmp_path)
    assert not r.accepted


def test_review_page_set_mismatch_blocks(tmp_path):
    exp = _expectation(_deck(3))
    sel = [_selection("s0"), _selection("s1"), _selection("s2")]
    gates = _all_gates(exp)
    saved = _saved(3)
    saved.render_pages = [SavedRenderPage(page=i, image=f"p{i}.png",
                                          sha256="a" * 64)
                          for i in (1, 2, 3)]
    rev = _review(pages=2)
    r = _run(exp, saved, gates, [rev], sel, tmp_path)
    assert not r.accepted
    assert "REVIEW_PAGE_SET_MISMATCH" in r.error_codes


# --- validate_evidence unit checks ---------------------------------------------

def test_validate_evidence_detects_hash_mismatch(tmp_path):
    exp = GateExpectation(gate_id="g", expected_units=["g:u"],
                          applies_to=["deck"])
    ev = GateEvidence(gate_id="g", status=GateStatus.PASS,
                      expected_units=["g:u"], checked_units=["g:u"],
                      subject_candidate_sha256="f" * 64,
                      snapshots=_snap(),
                      artifacts=[EvidenceRef(
                          artifact_id="a", relative_path="nope.json",
                          sha256="0" * 64, byte_count=1)])
    v = validate_evidence(exp, ev, ArtifactStore(tmp_path), _snap())
    assert not v.valid
    assert any(e.startswith("EVIDENCE_HASH_MISMATCH") for e in v.errors)


def test_validate_evidence_detects_stale_snapshot(tmp_path):
    exp = GateExpectation(gate_id="g", expected_units=["g:u"],
                          applies_to=["deck"])
    ev = GateEvidence(gate_id="g", status=GateStatus.PASS,
                      expected_units=["g:u"], checked_units=["g:u"],
                      subject_candidate_sha256="f" * 64,
                      snapshots=_snap(input_sha256="x" * 64))
    v = validate_evidence(exp, ev, ArtifactStore(tmp_path), _snap())
    assert not v.valid
    assert any(e.startswith("EVIDENCE_STALE") for e in v.errors)


# --- collect_saved_manifest on the real previous candidate ---------------------

def test_collect_saved_manifest_real_candidate():
    run = (PROJECT_ROOT / "runs" / "stage6-layout-repair"
           / "2026-10-12-140949")
    cand = run / "candidate.pptx"
    if not cand.is_file():
        pytest.skip("previous run candidate not present")
    ctx = RunContext(project_root=PROJECT_ROOT, run_dir=run)
    m = collect_saved_manifest(cand, ctx)
    assert m.candidate_sha256 == \
        "162b9684274225291d5aefe99a72e3a398dcb2a5eb941c3bedf935efc56c839e"
    assert len(m.slides) == 8
    assert m.canvas.w == 12192000 and m.canvas.h == 6858000
    assert m.drawing_inventory
    assert m.address_count > 0
    assert any("media" in p for p in m.package_parts)
    assert not any(s.hidden for s in m.slides)


def test_missing_candidate_raises(tmp_path):
    with pytest.raises(ValueError):
        collect_saved_manifest(tmp_path / "nope.pptx",
                               RunContext(project_root=tmp_path,
                                          run_dir=tmp_path))


# --- visual_design gate (§10.7/§10.8) ------------------------------------------

def test_design_repair_blocks_acceptance(tmp_path):
    from slides_cli.gpn.acceptance import _design_gate_evidence
    from slides_cli.gpn.models import SnapshotSet
    exp = _expectation(_deck(2))
    sel = [_selection("s0"), _selection("s1")]
    design = _design_gate_evidence({
        "candidate_sha256": "f" * 64, "verdict": "repair",
        "dimensions": [{"dimension": "hierarchy_dominance",
                        "result": "insufficient",
                        "reason": "flat_text_dump"}],
        "findings": []}, tmp_path, SnapshotSet())
    assert design is not None and design.status == GateStatus.FAIL
    gates = [design if g.gate_id == "visual_design" else g
             for g in _all_gates(exp)]
    r = _run(exp, _saved(), gates, [_review()], sel, tmp_path)
    assert not r.accepted


def test_design_accept_passes_gate(tmp_path):
    from slides_cli.gpn.acceptance import _design_gate_evidence
    from slides_cli.gpn.models import SnapshotSet
    exp = _expectation(_deck(2))
    sel = [_selection("s0"), _selection("s1")]
    design = _design_gate_evidence({
        "candidate_sha256": "f" * 64, "verdict": "accept",
        "dimensions": [{"dimension": "hierarchy_dominance",
                        "result": "sufficient", "reason": "clear dominant"}],
        "findings": []}, tmp_path, SnapshotSet())
    assert design is not None and design.status == GateStatus.PASS
    gates = [design if g.gate_id == "visual_design" else g
             for g in _all_gates(exp)]
    r = _run(exp, _saved(), gates, [_review()], sel, tmp_path)
    assert r.accepted, [f.message for f in r.findings]


def test_user_rejected_bytes_cannot_pass_with_new_score(tmp_path):
    """Same candidate hash rejected by user feedback stays rejected."""
    from slides_cli.gpn.acceptance import _design_gate_evidence
    from slides_cli.gpn.models import SnapshotSet
    design = _design_gate_evidence({
        "candidate_sha256": "f" * 64, "verdict": "accept",
        "dimensions": [{"dimension": "hierarchy_dominance",
                        "result": "sufficient", "reason": "9/10"}],
        "findings": [{"code": "user_feedback",
                      "severity": "error",
                      "message": "user rejected these bytes: "
                                 "flat_text_dump"}]}, tmp_path,
        SnapshotSet())
    assert design is not None
    assert design.status == GateStatus.FAIL
