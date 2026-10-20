"""Validate model content BEFORE committing its immutable request.

Shared by the runner and the harness dispatcher. A bad model answer is
repairable under the same issued request; it is never a committed scene.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_payload(run: Path, task: Any, response: Any) -> list[str]:
    from .models import (
        CompiledOntology,
        SelectionReport,
        SemanticDeck,
        SlideDesignBrief,
        VisualConcept,
    )
    from .planner import validate_scene_response
    from .scene_contract import lower_scene_plan

    kind, payload = task.kind.value, response.payload
    try:
        if kind in ("scene_generation", "scene_repair"):
            key = task.candidate_key
            if key is None:
                return ["SCENE_CANDIDATE_KEY_MISSING"]
            deck = SemanticDeck.model_validate(read(run / "inputs/semantic_deck.json"))
            slide = next(s for s in deck.slides if s.source_slide_id == key.source_slide_id)
            safe = key.source_slide_id.replace("/", "_").replace(":", "_")
            concept = VisualConcept.model_validate(
                read(run / "scenes" / safe / key.candidate_id / "concept.json")
            )
            rules = CompiledOntology.model_validate(read(run / "compiled_rules.json"))
            report = validate_scene_response(response, task, slide, concept, rules)
            if not report.valid:
                return [f"SCENE_SCHEMA_INVALID:{issue}" for issue in report.hard_issues]
            source = read(run / "source_ir.json")
            known = {
                o["id"]
                for s in source["slides"]
                if s["id"] == key.source_slide_id
                for o in s["objects"]
            }
            lowered, errors = lower_scene_plan(payload["scene_plan"], slide, known)
            if errors:
                return errors
            from .export_pipeline import _load_semantic_gate_inputs
            from .models import SourceDeckIR
            from .text_flow import build_rich_paragraphs
            from .visual_norm import check_scene_visual_coverage

            # The pre-build visual coverage gate needs a fully typed source
            # IR (object kind, payload, geometry) and the transformation
            # ledger that authorizes omissions. A production run always has
            # both: the import stage writes the typed IR and the semantic
            # stage writes the ledger before any scene is requested. A run
            # without them cannot be classified, so the gate does not
            # silently reject the answer; the build stage still refuses to
            # emit such a candidate.
            try:
                typed_source = SourceDeckIR.model_validate(source)
            except ValueError:
                return []
            source_slide = next(
                s for s in typed_source.slides if s.id == key.source_slide_id
            )
            objects = {o.id: o for o in source_slide.objects}
            if any(z.get("paragraph_groups") for z in lowered.get("zones", [])):
                for zone in lowered["zones"]:
                    if zone.get("paragraph_groups"):
                        build_rich_paragraphs(
                            objects,
                            zone["content_refs"],
                            zone.get("ref_roles"),
                            zone["paragraph_groups"],
                            zone.get("inline_separator", " "),
                        )
            _, ledger, snapshot = _load_semantic_gate_inputs(run)
            if ledger is None:
                return ["SCENE_VISUAL_COVERAGE_LEDGER_MISSING"]
            coverage = check_scene_visual_coverage(
                list(source_slide.objects),
                slide,
                ledger,
                snapshot or ledger.source_snapshot,
                lowered,
                rules,
            )
            if not coverage.ok:
                return list(coverage.issues) or ["SCENE_VISUAL_COVERAGE_INCOMPLETE"]
            return []
        if kind == "semantic_analysis":
            # Transformation decisions share the response envelope, not the
            # strict SemanticDeck schema. The runner validates their ledger.
            body = {
                k: v
                for k, v in payload.items()
                if k
                in (
                    "schema_version",
                    "source_manifest_sha256",
                    "slides",
                    "units",
                    "relations",
                    "evidence",
                )
            }
            deck = SemanticDeck.model_validate(body)
            source_path = run / "source_ir.json"
            if source_path.is_file():
                source = read(source_path)
                known = {o["id"] for s in source["slides"] for o in s["objects"]}
                decisions = payload.get("transformation_decisions", [])
                ids = [d.get("source_object_id") for d in decisions]
                if set(ids) != known or len(ids) != len(known):
                    return [
                        f"SEMANTIC_DECISION_COVERAGE:missing={sorted(known - set(ids))[:12]}; "
                        f"unknown={sorted(set(ids) - known)[:12]}; "
                        f"duplicates={len(ids) - len(set(ids))}"
                    ]
                required = {
                    r.object_id
                    for s in deck.slides
                    for u in s.units
                    if u.preservation != "optional"
                    for r in u.source_refs
                    if r.object_id
                }
                from .restyle_runner import _object_is_arrow

                relation_ids = {r.id for s in deck.slides for r in s.relations}
                for decision in decisions:
                    oid, action = decision["source_object_id"], decision.get("action")
                    if action not in ("preserve", "recompose", "replace", "omit"):
                        return [f"TRANSFORM_DECISION_INVALID:{oid}:{action}"]
                    if action == "omit" and (
                        oid in required
                        or not decision.get("reason")
                        or not decision.get("evidence_refs")
                    ):
                        return [f"TRANSFORM_OMIT_UNAUTHORIZED:{oid}"]
                    targets = set(decision.get("target_relation_ids", []))
                    if targets - relation_ids:
                        return [
                            f"TRANSFORM_UNKNOWN_RELATION:{oid}:{sorted(targets - relation_ids)}"
                        ]
                    if (
                        action in ("recompose", "replace")
                        and _object_is_arrow(source, oid)
                        and not targets
                    ):
                        return [f"TRANSFORM_ARROW_WITHOUT_RELATION:{oid}"]
        elif kind == "design_brief":
            brief = SlideDesignBrief.model_validate(payload)
            packet = read(run / task.packet)
            slide = packet["semantic_slide"]
            if brief.slide_id != slide["source_slide_id"]:
                return ["BRIEF_SLIDE_IDENTITY_MISMATCH"]
            units = {u["id"] for u in slide.get("units", [])}
            required = {
                u["id"] for u in slide.get("units", []) if u.get("preservation") != "optional"
            }
            relations = {r["id"] for r in slide.get("relations", [])}
            required_relations = {
                r["id"]
                for r in slide.get("relations", [])
                if r.get("preservation", "required") == "required"
            }
            if set(brief.required_units) != required:
                return ["BRIEF_REQUIRED_UNIT_COVERAGE"]
            if set(brief.required_relations) != required_relations:
                return ["BRIEF_REQUIRED_RELATION_COVERAGE"]
            grouped = {uid for group in brief.semantic_groups for uid in group.unit_ids}
            if required - grouped or grouped - units:
                return ["BRIEF_GROUP_UNIT_COVERAGE"]
            group_ids = [g.group_id for g in brief.semantic_groups]
            if len(set(group_ids)) != len(group_ids) or brief.primary_group_id not in group_ids:
                return ["BRIEF_GROUP_IDENTITY"]
            if set(brief.required_relations) - relations:
                return ["BRIEF_UNKNOWN_RELATION"]
            if not brief.communication_job.strip() or not brief.intended_takeaway.strip():
                return ["BRIEF_MEANING_MISSING"]
        elif kind == "visual_concept_generation":
            if not payload.get("concepts"):
                return ["VISUAL_CONCEPTS_MISSING"]
            from .design import validate_concept_fidelity

            packet = read(run / task.packet)
            brief = SlideDesignBrief.model_validate(packet["design_brief"])
            deck = SemanticDeck.model_validate(read(run / "inputs/semantic_deck.json"))
            slide = next(s for s in deck.slides if s.source_slide_id == brief.slide_id)
            concepts = [VisualConcept.model_validate(c) for c in payload["concepts"]]
            ids = [c.concept_id for c in concepts]
            if len(ids) != len(set(ids)):
                return ["CONCEPT_ID_DUPLICATE"]
            if any(not cid or "/" in cid or "\\" in cid or cid in (".", "..") for cid in ids):
                return ["CONCEPT_ID_UNSAFE"]
            if len(concepts) != packet.get("requested_concept_count", 3):
                return ["CONCEPT_SET_COUNT_MISMATCH"]
            signatures = [
                json.dumps(
                    {
                        key: concept.model_dump(mode="json")[key]
                        for key in (
                            "group_topology",
                            "main_visual_encoding",
                            "reading_path",
                            "dominance",
                            "alignment_system",
                            "whitespace_structure",
                        )
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                )
                for concept in concepts
            ]
            if len(set(signatures)) != len(concepts):
                return ["CONCEPT_SET_NOT_DIVERSE"]
            known_units = {u.id for u in slide.units}
            known_relations = {r.id for r in slide.relations}
            for concept in concepts:
                if any(
                    set(m.source_unit_ids) - known_units
                    or set(m.source_relation_ids) - known_relations
                    for m in concept.source_to_visual_mapping
                ):
                    return [f"CONCEPT_MAPPING_UNKNOWN:{concept.concept_id}"]
                report = validate_concept_fidelity(
                    concept, brief, slide, brief.applicable_rule_refs
                )
                if not report.valid:
                    return [
                        f"CONCEPT_FIDELITY_INVALID:{concept.concept_id}:"
                        + ";".join(f.code for f in report.fidelity_findings)
                    ]
        elif kind == "candidate_selection":
            report = SelectionReport.model_validate(payload)
            state = read(run / "run_state.json")
            expected = set(state.get("slide_candidates", {}))
            actual = [s.source_slide_id for s in report.selections]
            if set(actual) != expected or len(actual) != len(expected):
                return ["SELECTION_COVERAGE_MISMATCH"]
            packet = read(run / task.packet)
            entries = packet.get("candidates", [])
            if entries:
                for selection in report.selections:
                    matches = [
                        c
                        for c in entries
                        if c["selection"].get(selection.source_slide_id)
                        == selection.candidate_key.candidate_id
                    ]
                    if len(matches) != 1:
                        return [f"SELECTION_NOT_ELIGIBLE:{selection.source_slide_id}"]
                    if selection.candidate_sha256 != matches[0]["candidate_sha256"]:
                        return [f"SELECTION_HASH_MISMATCH:{selection.source_slide_id}"]
                    alternatives = packet.get("evaluated_alternatives", [])
                    aliases = {}
                    for candidate in alternatives:
                        cid = candidate.get("selection", {}).get(selection.source_slide_id)
                        if cid:
                            aliases[cid] = cid
                            aliases[candidate["deck_key"]] = cid
                    considered = {aliases[v] for v in selection.considered if v in aliases}
                    if alternatives and (len(considered) < 2 or not selection.reason.strip()):
                        return [f"SELECTION_COMPARISON_INCOMPLETE:{selection.source_slide_id}"]
        elif kind in ("visual_review", "design_review"):
            packet = read(run / task.packet)
            if payload.get("verdict") not in ("accept", "repair", "reject"):
                return ["REVIEW_VERDICT_INVALID"]
            for field in ("deck_key", "candidate_sha256"):
                if payload.get(field) != packet.get(field):
                    return [f"REVIEW_ARTIFACT_IDENTITY:{field}"]
            if kind == "visual_review":
                expected = {p["page"]: p["sha256"] for p in packet.get("render_pages", [])}
                pages = payload.get("pages", [])
                actual = {p.get("page"): p.get("sha256") for p in pages}
                if expected and (actual != expected or len(actual) != len(pages)):
                    return ["REVIEW_PAGE_COVERAGE_MISMATCH"]
            if kind == "design_review":
                expected = set(packet.get("required_dimensions", []))
                dimensions = payload.get("dimensions", [])
                if expected and {d.get("dimension") for d in dimensions} != expected:
                    return ["REVIEW_DESIGN_DIMENSIONS_MISSING"]
                if payload.get("verdict") == "accept" and any(
                    d.get("result") not in ("sufficient", "not_applicable") for d in dimensions
                ):
                    return ["REVIEW_ACCEPT_WITH_FAILED_DIMENSION"]
            candidate = run / "scenes" / str(packet["deck_key"]) / "candidate.pptx"
            import hashlib

            if hashlib.sha256(candidate.read_bytes()).hexdigest() != packet["candidate_sha256"]:
                return ["REVIEW_CANDIDATE_CHANGED"]
            if payload.get("verdict") == "accept":
                from .review_jobs import current_render_identity

                manifest_path = run / str(packet.get("render_manifest", ""))
                current = current_render_identity(
                    candidate,
                    manifest_path,
                    expected_page_count=len(packet.get("selection", {}) or {}),
                )
                claimed = packet.get("render_identity") or {}
                for field in (
                    "candidate_sha256",
                    "render_manifest_sha256",
                    "pdf_sha256",
                ):
                    if not claimed.get(field) or claimed.get(field) != getattr(current, field):
                        return [f"REVIEW_RENDER_IDENTITY_STALE:{field}"]
                if claimed.get("ordered_page_hashes") != current.ordered_page_hashes:
                    return ["REVIEW_RENDER_IDENTITY_STALE:ordered_page_hashes"]
                if packet.get("native_render_result") != "pass" or current.result != "pass":
                    return ["REVIEW_ACCEPT_WITH_FAILED_NATIVE_RENDER"]
                if current.expected_page_scope != list(
                    range(1, len(current.ordered_page_hashes) + 1)
                ):
                    return ["REVIEW_RENDER_PAGE_SCOPE_INCOMPLETE"]
        return []
    except (OSError, ValueError, TypeError, KeyError, StopIteration) as error:
        return [f"PAYLOAD_INVALID:{type(error).__name__}:{error}"]
