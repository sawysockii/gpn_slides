"""Stage 7 §10.4-10.8: design cycle regressions."""

from __future__ import annotations

import json
from pathlib import Path

from slides_cli.gpn.design import (
    build_slide_design_brief,
    evaluate_candidate_diversity,
    generate_visual_concepts,
    review_slide_design,
    structural_flat_text_signals,
    validate_concept_fidelity,
)
from slides_cli.gpn.models import (
    CandidateKey,
    CompositionSignature,
    ConceptTopology,
    DominanceSpec,
    EncodingProposal,
    ModelTaskKind,
    SemanticRelation,
    SemanticSlide,
    SemanticUnit,
    SlideDesignBrief,
    TypedContentPayload,
    VisualConcept,
    VisualMapping,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _slide(sid="s1"):
    return SemanticSlide(
        source_slide_id=sid, title="T",
        units=[SemanticUnit(
            id=f"{sid}-u1", kind="text",
            content=TypedContentPayload(text="400 FTE"),
            preservation="required_semantic"),
            SemanticUnit(
            id=f"{sid}-u2", kind="text",
            content=TypedContentPayload(text="target"),
            preservation="required_semantic")],
        relations=[SemanticRelation(
            id=f"{sid}-r1", kind="flow", from_unit_ids=[f"{sid}-u1"],
            to_unit_ids=[f"{sid}-u2"], directed=True)])


def test_brief_task_carries_required_units_and_rules(tmp_path):
    task = build_slide_design_brief(_slide(), rules=["C01", "C04"],
                                    run_dir=tmp_path)
    assert task.kind is ModelTaskKind.DESIGN_BRIEF
    packet = json.loads((tmp_path / task.packet).read_text(encoding="utf-8"))
    units = packet["semantic_slide"]["units"]
    assert any(u["id"] == "s1-u1" for u in units)
    assert "C01" in packet["applicable_rule_refs"]
    assert task.expected_response_units


def test_concepts_task_requests_semantic_diversity(tmp_path):
    brief = SlideDesignBrief(slide_id="s1", communication_job="j",
                             intended_takeaway="t",
                             required_units=["s1-u1", "s1-u2"],
                             required_relations=["s1-r1"])
    task = generate_visual_concepts(brief, ["ex1"], ["process", "table"],
                                     run_dir=tmp_path, count=3)
    packet = json.loads((tmp_path / task.packet).read_text(encoding="utf-8"))
    assert packet["requested_concept_count"] == 3
    assert "diversity_rule" in packet


def test_fidelity_rejects_unmapped_required_relation():
    slide = _slide()
    brief = SlideDesignBrief(slide_id="s1", communication_job="j",
                             intended_takeaway="t",
                             required_units=["s1-u1", "s1-u2"],
                             required_relations=["s1-r1"],
                             visual_encoding_candidates=[
                                 EncodingProposal(encoding_id="e1",
                                                  kind="typographic",
                                                  source_unit_ids=["s1-u1"])])
    concept = VisualConcept(
        concept_id="c1", design_brief_sha256="b",
        visual_argument="arg",
        group_topology=ConceptTopology(topology="typographic"),
        main_visual_encoding=EncodingProposal(encoding_id="e1",
                                              kind="typographic",
                                              source_unit_ids=["s1-u1"]),
        source_to_visual_mapping=[
            VisualMapping(source_unit_ids=["s1-u1", "s1-u2"],
                          visual_element="stack")],
        dominance=DominanceSpec(dominant_group_id="s1-u1"))
    v = validate_concept_fidelity(concept, brief, slide, ["C01"])
    assert not v.valid
    assert any(f.code == "CONCEPT_REQUIRED_RELATION_UNMAPPED"
               for f in v.fidelity_findings)


def test_fidelity_rejects_invented_process_causality():
    slide = _slide()
    brief = SlideDesignBrief(slide_id="s1", communication_job="j",
                             intended_takeaway="t",
                             required_units=["s1-u1", "s1-u2"],
                             required_relations=["s1-r1"])
    concept = VisualConcept(
        concept_id="c2", design_brief_sha256="b", visual_argument="arg",
        group_topology=ConceptTopology(topology="process_flow"),
        main_visual_encoding=EncodingProposal(encoding_id="e1",
                                              kind="process",
                                              source_unit_ids=["s1-u1"]),
        source_to_visual_mapping=[
            VisualMapping(source_unit_ids=["s1-u1", "s1-u2"],
                          source_relation_ids=["s1-r1"],
                          visual_element="flow")],
        dominance=DominanceSpec(dominant_group_id="s1-u1"))
    slide.relations = []  # no source flow -> invented causality
    v = validate_concept_fidelity(concept, brief, slide, ["C01"])
    assert not v.valid
    assert v.invented_causality


def test_fidelity_passes_when_all_mapped():
    slide = _slide()
    brief = SlideDesignBrief(slide_id="s1", communication_job="j",
                             intended_takeaway="t",
                             required_units=["s1-u1", "s1-u2"],
                             required_relations=["s1-r1"])
    concept = VisualConcept(
        concept_id="c3", design_brief_sha256="b", visual_argument="arg",
        group_topology=ConceptTopology(topology="process_flow"),
        main_visual_encoding=EncodingProposal(encoding_id="e1",
                                              kind="process",
                                              source_unit_ids=["s1-u1"]),
        source_to_visual_mapping=[
            VisualMapping(source_unit_ids=["s1-u1", "s1-u2"],
                          source_relation_ids=["s1-r1"],
                          visual_element="flow")],
        dominance=DominanceSpec(dominant_group_id="s1-u1"))
    v = validate_concept_fidelity(concept, brief, slide, ["C01"])
    assert v.valid


def test_diversity_requires_distinct_topology():
    ck = CandidateKey(run_id="r", source_slide_id="s1", candidate_id="c1")
    sig_a = CompositionSignature(candidate_key=ck,
                                 semantic_group_topology="process_flow",
                                 reading_path="l2r", dominant_group_id="g1")
    sig_b = CompositionSignature(candidate_key=ck,
                                 semantic_group_topology="process_flow",
                                 reading_path="l2r", dominant_group_id="g1")
    report = evaluate_candidate_diversity([sig_a, sig_b], _slide())
    assert not report.sufficient  # cosmetic duplicates are not diversity
    sig_c = CompositionSignature(candidate_key=ck,
                                 semantic_group_topology="comparison",
                                 reading_path="t2b", dominant_group_id="g2")
    report2 = evaluate_candidate_diversity([sig_a, sig_c], _slide())
    assert report2.sufficient


def test_flat_text_signal_is_structural_only():
    signals = structural_flat_text_signals(
        _slide(), ["text", "text", "container"])
    assert signals["text_only_nodes"] is True
    assert "communication_job" in signals
    # verdict itself is a model judgment over PNG + job (§10.5)


def test_design_review_task_packet(tmp_path):
    brief = SlideDesignBrief(slide_id="s1", communication_job="j",
                             intended_takeaway="t")
    concept = VisualConcept(concept_id="c1", design_brief_sha256="b",
                            visual_argument="arg")
    task = review_slide_design({"sha256": "x"}, brief, concept, _slide(),
                               ["C01"], run_dir=tmp_path)
    packet = json.loads((tmp_path / task.packet).read_text(encoding="utf-8"))
    assert "dimensions" in packet
    assert len(packet["dimensions"]) == 8
    assert task.kind is ModelTaskKind.DESIGN_REVIEW


def test_compile_concept_scene_draft_binds_semantics():
    from slides_cli.gpn.design import compile_concept_scene
    from slides_cli.gpn.models import SceneNodeSpec
    concept = VisualConcept(
        concept_id="c9", design_brief_sha256="b", visual_argument="arg",
        target_node_spec=[SceneNodeSpec(node_id="n1", kind="text",
                                        semantic_role="value",
                                        content_ref="s1-u1")],
        source_to_visual_mapping=[
            VisualMapping(source_unit_ids=["s1-u1"],
                          source_relation_ids=["s1-r1"],
                          visual_element="n1")],
        dominance=DominanceSpec(dominant_group_id="s1-u1"))
    draft = compile_concept_scene(concept, _slide(), {"frame": "f"},
                                  ["C01"])
    assert draft["schema"] == "concept-scene-draft/v1"
    assert draft["bindings"][0]["source_unit_ids"] == ["s1-u1"]
    assert draft["source_slide_id"] == "s1"
