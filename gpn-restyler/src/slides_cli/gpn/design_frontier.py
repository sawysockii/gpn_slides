"""Advance each answered slide independently of unrelated pending slides.

Only model-authored, prevalidated responses are materialized. This module
does not generate content, choose a fallback concept, or relax acceptance.
The global run stage is a progress label; it is not a deck-wide barrier.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import ModelTaskKind, SlideDesignBrief, TypedFinding, VisualConcept


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def pump_design_frontier(state: Any, findings: list) -> list:
    from . import restyle_runner as runner
    from .design import generate_visual_concepts, validate_concept_fidelity
    from .models import SemanticDeck
    from .response_validation import validate_payload

    root = Path(state.run_dir)
    responses = [
        r
        for r in state.responses
        if runner._task_kind(state, r.request_id)
        in (ModelTaskKind.DESIGN_BRIEF, ModelTaskKind.VISUAL_CONCEPT_GENERATION)
    ]
    if not responses:
        return []
    deck = SemanticDeck.model_validate(_read(root / "inputs/semantic_deck.json"))
    slides = {s.source_slide_id: s for s in deck.slides}
    tasks = {t.request_id: t for t in state.task_queue}
    actions = []
    for response in responses:
        task = tasks[response.request_id]
        is_brief = task.kind == ModelTaskKind.DESIGN_BRIEF
        done = (
            state.materialized_brief_responses if is_brief else state.registered_concept_responses
        )
        if response.request_id in done:
            continue
        errors = validate_payload(root, task, response)
        if errors:
            # Old consumed responses are not retroactively made valid.
            findings.append(
                TypedFinding(
                    code="DESIGN_MATERIALIZATION_REJECTED",
                    severity="error",
                    message=f"{response.request_id}: {'; '.join(errors)}",
                )
            )
            continue
        if is_brief:
            brief = SlideDesignBrief.model_validate(response.payload)
            path = root / "design_briefs" / f"{runner._safe_name(brief.slide_id)}.json"
            runner._atomic_write(path, brief.model_dump_json(indent=1))
            concept_id = f"T-concepts-{brief.slide_id}"
            # Preserve existing immutable packets during adoption of old runs.
            if not any(t.request_id == concept_id for t in state.task_queue):
                from .owned_examples import select_owned_examples

                examples = select_owned_examples(
                    state.input_spec.project_root, slides[brief.slide_id]
                )
                concept_task = generate_visual_concepts(
                    brief,
                    approved_examples=[e["example_id"] for e in examples],
                    supported_primitives=[
                        "process",
                        "structure",
                        "comparison",
                        "table",
                        "data",
                        "typographic",
                        "evidence",
                    ],
                    run_dir=root,
                    request_id=concept_id,
                    count=min(3, state.budget.max_generations_per_slide),
                )
                runner._enqueue(state, concept_task)
            done.append(response.request_id)
            actions.append(runner.ActionRef(action="brief_materialized", detail=brief.slide_id))
            continue
        packet = _read(root / task.packet)
        sid = packet["design_brief"]["slide_id"]
        brief = SlideDesignBrief.model_validate(packet["design_brief"])
        for item in response.payload["concepts"]:
            concept = VisualConcept.model_validate(item)
            report = validate_concept_fidelity(
                concept, brief, slides[sid], runner._applicable_rules(root)
            )
            # validate_payload has already checked this, before any side effects.
            if not report.valid:
                raise ValueError(f"CONCEPT_FIDELITY_CHANGED:{sid}:{concept.concept_id}")
            key = f"{state.run_id}/{runner._safe_name(sid)}/{concept.concept_id}"
            directory = root / "scenes" / runner._safe_name(sid) / concept.concept_id
            runner._atomic_write(directory / "concept.json", concept.model_dump_json(indent=1))
            runner._atomic_write(directory / "fidelity.json", report.model_dump_json(indent=1))
            state.concept_slide[key] = sid
            state.slide_candidates.setdefault(sid, [])
            if concept.concept_id not in state.slide_candidates[sid]:
                state.slide_candidates[sid].append(concept.concept_id)
            state.candidate_states.setdefault(key, "concept_fidelity_validated")
            state.candidate_revisions.setdefault(key, 0)
            issued = any(
                t.kind in (ModelTaskKind.SCENE_GENERATION, ModelTaskKind.SCENE_REPAIR)
                and t.candidate_key is not None
                and t.candidate_key.source_slide_id == sid
                and t.candidate_key.candidate_id == concept.concept_id
                for t in state.task_queue
            )
            if not issued:
                scene_task = runner._scene_generation_task(state, key)
                if scene_task is None:
                    raise ValueError(f"SCENE_TASK_NOT_PRODUCED:{key}")
                runner._enqueue(state, scene_task)
                state.candidate_states[key] = "scene_pending"
        done.append(response.request_id)
        actions.append(runner.ActionRef(action="concept_set_materialized", detail=sid))
    return actions


def pump_current_reviews(state: Any) -> list:
    """A current visual response can unlock its own design review immediately."""
    from . import restyle_runner as runner

    root = Path(state.run_dir)
    jobs = {j["deck_key"]: j for j in state.deck_candidates}
    actions = []
    for response in state.responses:
        kind = runner._task_kind(state, response.request_id)
        if kind not in (ModelTaskKind.VISUAL_REVIEW, ModelTaskKind.DESIGN_REVIEW):
            continue
        key = response.payload.get("deck_key", "")
        candidate = root / "scenes" / key / "candidate.pptx"
        if key not in jobs or not candidate.is_file():
            continue
        if response.payload.get("candidate_sha256") != runner._sha_file(candidate):
            continue
        design = kind == ModelTaskKind.DESIGN_REVIEW
        path = candidate.parent / ("design_review.json" if design else "visual_review.json")
        existing = _read(path) if path.is_file() else {}
        if existing.get("task_id") != response.request_id:
            writer = runner._write_design_review if design else runner._write_visual_review
            writer(state, response)
            actions.append(runner.ActionRef(action="review_materialized", detail=key))
        if not design:
            task = runner._design_review_task(state, jobs[key])
            if task is not None:
                runner._enqueue(state, task)
    return actions
