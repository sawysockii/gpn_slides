"""Stage 7 §10.4-10.8: mandatory design cycle.

The technical layout solver is not a designer. Before any bbox is assigned,
the harness model must design the visual explanation of the content:

    build_slide_design_brief -> generate_visual_concepts
    -> validate_concept_fidelity -> compile_concept_scene
    -> review_slide_design / compare_visual_concepts

Every function here either produces a typed ``ModelTask`` packet for the
current harness or validates a response. The engine never invents concepts
from source shape order, and a visually weak concept goes back to the
concept stage — not to another cosmetic bbox shift.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path

from .models import (
    CompositionSignature,
    ConceptValidation,
    DesignReview,
    DiversityReport,
    ModelTask,
    ModelTaskKind,
    SemanticGroup,
    SemanticSlide,
    SlideDesignBrief,
    TypedFinding,
    VisualConcept,
)

DESIGN_DIMENSION_FAMILIES: tuple[str, ...] = (
    "semantic_readability_relations",
    "hierarchy_dominance",
    "grouping_alignment_rhythm",
    "proportions_whitespace",
    "encoding_data_fidelity",
    "text_visual_evidence_balance",
    "corporate_visual_language",
    "deck_coherence_diversity",
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_packet(run_dir: Path, rel: str, payload: dict) -> Path:
    p = run_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                 encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# §10.4 stage 1-2: brief + concepts (harness tasks)
# ---------------------------------------------------------------------------

def build_slide_design_brief(
    semantic_slide: SemanticSlide,
    rules: Iterable[str],
    feedback: list[TypedFinding] | None = None,
    *,
    run_dir: Path | None = None,
    request_id: str = "",
) -> ModelTask:
    """Task: define communication job + takeaway confirmed by source."""
    brief = SlideDesignBrief(
        slide_id=semantic_slide.source_slide_id,
        communication_job="",
        intended_takeaway="",
        required_units=[u.id for u in semantic_slide.units
                        if u.preservation != "optional"],
        required_relations=[r.id for r in semantic_slide.relations
                            if r.preservation == "required"],
        semantic_groups=[SemanticGroup(
            group_id=f"g{i}", role="primary" if i == 0 else "secondary",
            unit_ids=[u.id for u in semantic_slide.units])
            for i in range(1)],
        forbidden_inferences=[],
        applicable_rule_refs=sorted(rules),
        brief_sha256="",
    )
    packet_payload = {
        "stage": "design_brief",
        "semantic_slide": semantic_slide.model_dump(mode="json"),
        "response_template": brief.model_dump(mode="json"),
        "applicable_rule_refs": sorted(rules),
        "prior_findings": [f.model_dump(mode="json")
                           for f in (feedback or [])],
        "response_schema": "slide-design-brief/v1",
        "required_fields": ["communication_job", "intended_takeaway",
                            "semantic_groups", "primary_group_id",
                            "visual_encoding_candidates",
                            "forbidden_inferences"],
    }
    packet = _write_packet(run_dir, "model_requests/design_brief.json",
                           packet_payload) if run_dir else None
    return ModelTask(
        request_id=request_id or _sha(
            f"brief:{semantic_slide.source_slide_id}"),
        kind=ModelTaskKind.DESIGN_BRIEF,
        input_fingerprint=_sha(json.dumps(
            semantic_slide.model_dump(mode="json"),
            sort_keys=True, ensure_ascii=False)),
        response_schema_id="slide-design-brief/v1",
        packet=str(packet) if packet else "model_requests/design_brief.json",
        expected_response_units=[
            "communication_job", "intended_takeaway", "semantic_groups",
            "visual_encoding_candidates"],
    )


def generate_visual_concepts(
    brief: SlideDesignBrief,
    approved_examples: Iterable[str],
    supported_primitives: Iterable[str],
    *,
    run_dir: Path | None = None,
    request_id: str = "",
    count: int = 3,
) -> ModelTask:
    """Task: N semantically different concepts (topology/reading/dominance)."""
    packet_payload = {
        "stage": "visual_concept_generation",
        "design_brief": brief.model_dump(mode="json"),
        "approved_example_refs": sorted(approved_examples),
        "supported_primitives": sorted(supported_primitives),
        "requested_concept_count": count,
        "response_schema": "visual-concept/v1",
        "required_fields": ["visual_argument", "group_topology",
                            "main_visual_encoding", "reading_path",
                            "dominance", "alignment_system",
                            "whitespace_structure",
                            "source_to_visual_mapping"],
        "diversity_rule": "concepts must differ in topology, reading "
                          "path or dominance — number/header stacking "
                          "shifts are not new concepts",
    }
    packet = _write_packet(run_dir, "model_requests/visual_concepts.json",
                           packet_payload) if run_dir else None
    return ModelTask(
        request_id=request_id or _sha(f"concepts:{brief.slide_id}"),
        kind=ModelTaskKind.VISUAL_CONCEPT_GENERATION,
        input_fingerprint=brief.brief_sha256 or _sha(
            json.dumps(brief.model_dump(mode="json"),
                       sort_keys=True, ensure_ascii=False)),
        response_schema_id="visual-concept/v1",
        packet=str(packet) if packet else
        "model_requests/visual_concepts.json",
        # One response carries the requested concept set:
        # {"concepts": [VisualConcept, ...]} (§10.1 default count=3).
        expected_response_units=["concepts"],
    )


# ---------------------------------------------------------------------------
# §10.4 stage 6: fidelity validation (deterministic part)
# ---------------------------------------------------------------------------

def validate_concept_fidelity(
    concept: VisualConcept,
    brief: SlideDesignBrief,
    semantic_slide: SemanticSlide,
    rules: Iterable[str],
) -> ConceptValidation:
    """Deterministic fidelity checks; semantic part is the harness task."""
    findings: list[TypedFinding] = []
    invented_causality: list[str] = []
    invented_data: list[str] = []

    mapped_units: set[str] = set()
    for m in concept.source_to_visual_mapping:
        mapped_units.update(m.source_unit_ids)
    for uid in brief.required_units:
        if uid not in mapped_units:
            findings.append(TypedFinding(
                code="CONCEPT_REQUIRED_UNIT_UNMAPPED", severity="error",
                message=f"required unit {uid} not mapped to any visual "
                        f"element", scope=semantic_slide.source_slide_id))

    mapped_rels: set[str] = set()
    for m in concept.source_to_visual_mapping:
        mapped_rels.update(m.source_relation_ids)
    for rid in brief.required_relations:
        if rid not in mapped_rels:
            findings.append(TypedFinding(
                code="CONCEPT_REQUIRED_RELATION_UNMAPPED", severity="error",
                message=f"required relation {rid} not expressed visually",
                scope=semantic_slide.source_slide_id))

    for enc in [concept.main_visual_encoding]:
        if enc.kind == "data" and not enc.source_unit_ids:
            invented_data.append("data encoding without source units")
            findings.append(TypedFinding(
                code="CONCEPT_INVENTED_DATA", severity="error",
                message="data encoding without source units",
                scope=semantic_slide.source_slide_id))

    if concept.group_topology.topology == "process_flow" and \
            not any(r for r in semantic_slide.relations
                    if r.kind in ("flow", "sequence", "transition")):
        invented_causality.append(
            "process flow topology without source flow relations")
        findings.append(TypedFinding(
            code="CONCEPT_INVENTED_CAUSALITY", severity="error",
            message="process flow topology without source flow relations",
            scope=semantic_slide.source_slide_id))

    valid = not findings
    return ConceptValidation(
        concept_id=concept.concept_id, valid=valid,
        fidelity_findings=findings, invented_causality=invented_causality,
        invented_data=invented_data)


# ---------------------------------------------------------------------------
# §10.4 stage 7-8: scene compile + design review tasks
# ---------------------------------------------------------------------------

def compile_concept_scene(concept, semantic_slide, frame, rules) -> dict:
    """Adapt a validated concept into a target scene draft (§6.3).

    Returns the concept's target_node_spec with semantic bindings and
    frame/reading-path constraints — the input contract for the future
    ``emit_scene`` (Stage 6 debt). Source geometry is provenance only,
    never a constraint on the composition. The legacy
    ``compile_composition`` (LayoutIntent-based) is intentionally NOT
    called: its signature cannot express concept topology.
    """
    bindings = []
    for mapping in concept.source_to_visual_mapping:
        bindings.append({
            "source_unit_ids": list(mapping.source_unit_ids),
            "source_relation_ids": list(mapping.source_relation_ids),
            "target": mapping.visual_element,
            "representation": mapping.representation or
            "semantic_representation",
        })
    return {
        "schema": "concept-scene-draft/v1",
        "concept_id": concept.concept_id,
        "design_brief_sha256": concept.design_brief_sha256,
        "concept_sha256": concept.concept_sha256,
        "target_nodes": [n.model_dump(mode="json")
                         for n in concept.target_node_spec],
        "reading_path": list(concept.reading_path),
        "bindings": bindings,
        "applicable_rule_refs": sorted(rules),
        "source_slide_id": semantic_slide.source_slide_id,
        "emitter": "emit_scene (pending); legacy LayoutIntent path "
                   "not applicable",
    }


def review_slide_design(render, brief, concept, source, rules, *,
                        run_dir: Path | None = None,
                        request_id: str = "") -> ModelTask:
    """Task: design review of actual rendered bytes (§10.7)."""
    packet_payload = {
        "stage": "design_review",
        "render_manifest_sha256": getattr(render, "sha256", ""),
        "design_brief": brief.model_dump(mode="json"),
        "concept": concept.model_dump(mode="json"),
        "source_slide": source.source_slide_id,
        "applicable_rule_refs": sorted(rules),
        "response_schema": "design-review/v1",
        "dimensions": list(DESIGN_DIMENSION_FAMILIES),
        "required_fields": ["verdict", "dimensions", "findings"],
        "note": "review actual PNG bytes; absence of overlaps is not "
                "sufficient composition quality",
    }
    packet = _write_packet(run_dir, "model_requests/design_review.json",
                           packet_payload) if run_dir else None
    return ModelTask(
        request_id=request_id or _sha(f"design-review:{brief.slide_id}"),
        kind=ModelTaskKind.DESIGN_REVIEW,
        input_fingerprint=concept.concept_sha256,
        response_schema_id="design-review/v1",
        packet=str(packet) if packet else
        "model_requests/design_review.json",
        expected_response_units=["verdict", "dimensions"],
    )


def compare_visual_concepts(eligible_rendered_concepts, brief, *,
                            run_dir: Path | None = None,
                            request_id: str = "") -> ModelTask:
    """Task: comparative design review across eligible variants."""
    packet_payload = {
        "stage": "compare_visual_concepts",
        "design_brief": brief.model_dump(mode="json"),
        "concepts": [c.model_dump(mode="json")
                     for c in eligible_rendered_concepts],
        "response_schema": "design-review/v1",
        "required_fields": ["verdict", "dimensions", "findings"],
    }
    packet = _write_packet(run_dir, "model_requests/compare_concepts.json",
                           packet_payload) if run_dir else None
    return ModelTask(
        request_id=request_id or _sha(f"compare:{brief.slide_id}"),
        kind=ModelTaskKind.DESIGN_REVIEW,
        input_fingerprint=brief.brief_sha256,
        response_schema_id="design-review/v1",
        packet=str(packet) if packet else
        "model_requests/compare_concepts.json",
        expected_response_units=["verdict", "dimensions"],
    )


# ---------------------------------------------------------------------------
# §10.1: diversity from actual topology
# ---------------------------------------------------------------------------

def evaluate_candidate_diversity(
    plans: Iterable[CompositionSignature],
    semantic_slide: SemanticSlide,
) -> DiversityReport:
    """Diversity = distinct topology/reading/dominance, not pixel distance."""
    sigs = list(plans)
    topologies = {s.semantic_group_topology for s in sigs}
    readings = {s.reading_path for s in sigs}
    dominants = {s.dominant_group_id for s in sigs}
    unique_signatures = {
        (s.semantic_group_topology, s.reading_path, s.dominant_group_id)
        for s in sigs
    }
    sufficient = len(sigs) >= 2 and len(unique_signatures) >= 2
    return DiversityReport(
        slide_id=semantic_slide.source_slide_id,
        signatures=sigs,
        distinct_topologies=len(topologies),
        sufficient=sufficient,
        reason=(f"{len(sigs)} candidates, {len(unique_signatures)} unique "
                f"signatures, {len(topologies)} topologies, "
                f"{len(readings)} reading paths, {len(dominants)} "
                f"dominant groups") if sigs else "no candidates")


def structural_flat_text_signals(semantic_slide: SemanticSlide,
                                 node_kinds: Iterable[str]) -> dict:
    """Structural signals only — the flat_text_dump verdict itself is a
    model judgment over the communication job and the actual PNG (§10.5)."""
    kinds = list(node_kinds)
    text_only = bool(kinds) and all(k in ("text", "container")
                                    for k in kinds)
    return {
        "text_only_nodes": text_only,
        "node_count": len(kinds),
        "communication_job": semantic_slide.title,
        "note": "structural signal; final verdict requires rendered "
                "evidence and communication-job judgment",
    }


def design_review_to_finding(review: DesignReview) -> list[TypedFinding]:
    out: list[TypedFinding] = []
    for f in review.findings:
        out.append(f)
    for d in review.dimensions:
        if d.result == "insufficient":
            out.append(TypedFinding(
                code=f"design:{d.dimension}", severity="error",
                message=f"{d.dimension}: {d.reason}",
                scope=review.candidate_key.source_slide_id
                if review.candidate_key else None))
    return out
