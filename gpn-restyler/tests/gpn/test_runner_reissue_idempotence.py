"""Runner reissue/idempotence regressions (GPN_FIX_RESUME_REISSUE_LOOP).

These are causal tests on the shared runner functions, not mirrors of new
branches. They cover flat request identity, no automatic reissue for
schema/model/coverage failures, single-active-task per logical subject,
no retry without measured failure evidence, and context-provenance safety.
"""

from __future__ import annotations

from pathlib import Path

from slides_cli.gpn.models import (
    CandidateKey,
    InputKind,
    InputSpec,
    ModelTask,
    ModelTaskKind,
    ModelTaskResponse,
    ProductScope,
)
from slides_cli.gpn.restyle_runner import (
    RunState,
    _logical_task_id,
    _make_request_id,
    _retry_unresolved_scenes,
    _slide_id_for_key,
    advance_run,
)


def _state(tmp_path: Path, run_id: str = "run") -> RunState:
    spec = InputSpec(
        kind=InputKind.PROMPT,
        prompt_text="x",
        project_root=tmp_path,
        output_path=tmp_path / "out.pptx",
        requested_scope=ProductScope.CREATE,
    )
    return RunState(run_id=run_id, run_dir=str(tmp_path / "run"), input_spec=spec)


def _scene_task(
    run_id: str, slide: str, cand: str, rid: str = "req-1", fp: str = "fp-1"
) -> ModelTask:
    return ModelTask(
        request_id=rid,
        kind=ModelTaskKind.SCENE_GENERATION,
        candidate_key=CandidateKey(run_id=run_id, source_slide_id=slide, candidate_id=cand),
        input_fingerprint=fp,
        response_schema_id="scene-plan/v1",
        packet=f"model_requests/{rid}.json",
        expected_response_units=["scene_plan"],
    )


def test_make_request_id_is_flat_and_deterministic():
    a = _make_request_id("scene_generation:s:slide:c1", "fp", 1)
    b = _make_request_id("scene_generation:s:slide:c1", "fp", 1)
    c = _make_request_id("scene_generation:s:slide:c1", "fp", 2)
    assert a == b
    assert a != c
    assert "-r" not in a and a != b + "r"


def test_logical_task_id_uses_candidate_key_not_request_id():
    t = _scene_task("run", "slide0:ppt/slides/slide1.xml", "c1", rid="opaque-r109-r182")
    assert _logical_task_id(t) == ("scene_generation:slide0:ppt/slides/slide1.xml:c1")


def test_schema_error_does_not_reissue(tmp_path):
    state = _state(tmp_path)
    task = _scene_task("run", "slide0:ppt/slides/slide1.xml", "c1")
    state.task_queue.append(task)
    state.stage = "scene_generation"
    bad = ModelTaskResponse(
        request_id=task.request_id,
        input_fingerprint=task.input_fingerprint,
        actual_model="",
        schema_version_id="wrong",
        payload={},
    )
    result = advance_run(state, [bad])
    # No new task was created for a schema/model/coverage failure.
    assert [t.request_id for t in state.task_queue] == [task.request_id]
    assert task.status == "pending"
    assert any(f.code == "RESPONSE_REPAIR_REQUIRED" for f in result.findings)


def test_stale_response_keeps_the_same_immutable_question(tmp_path):
    state = _state(tmp_path)
    task = _scene_task("run", "slide0:ppt/slides/slide1.xml", "c1")
    state.task_queue.append(task)
    state.stage = "scene_generation"
    stale = ModelTaskResponse(
        request_id=task.request_id,
        input_fingerprint="different",
        actual_model="model",
        schema_version_id=task.response_schema_id,
        payload={"scene_plan": {}},
    )
    advance_run(state, [stale])
    assert len(state.task_queue) == 1
    assert state.task_queue[0].request_id == task.request_id
    assert state.task_queue[0].input_fingerprint == task.input_fingerprint
    assert state.task_queue[0].status == "pending"


def test_retry_skips_candidate_without_evidence(tmp_path):
    state = _state(tmp_path)
    state.slide_candidates = {"slide0:ppt/slides/slide1.xml": ["c1"]}
    state.candidate_states = {"run/slide0_ppt_slides_slide1.xml/c1": "scene_validated"}
    state.candidate_revisions = {"run/slide0_ppt_slides_slide1.xml/c1": 0}
    consumed = _scene_task("run", "slide0:ppt/slides/slide1.xml", "c1")
    consumed.status = "consumed"
    state.task_queue.append(consumed)
    state.deck_candidates = []  # no measured evidence at all
    tasks = _retry_unresolved_scenes(state, [])
    assert tasks == []


def test_retry_skips_when_active_task_exists(tmp_path):
    state = _state(tmp_path)
    state.slide_candidates = {"slide0:ppt/slides/slide1.xml": ["c1"]}
    key = "run/slide0_ppt_slides_slide1.xml/c1"
    state.candidate_states = {key: "scene_pending"}
    state.candidate_revisions = {key: 0}
    pending = _scene_task("run", "slide0:ppt/slides/slide1.xml", "c1", rid="active")
    pending.status = "pending"
    state.task_queue.append(pending)
    state.deck_candidates = [
        {
            "deck_key": "primary",
            "candidate.pptx": "scenes/primary/candidate.pptx",
        }
    ]
    (Path(state.run_dir) / "scenes" / "primary").mkdir(parents=True, exist_ok=True)
    (Path(state.run_dir) / "scenes" / "primary" / "candidate_evaluations.json").write_text(
        '[{"slide_id": "slide0:ppt/slides/slide1.xml", '
        '"candidates": [{"candidate_id": "c1", '
        '"unresolved": ["overflow"]}]}]',
        encoding="utf-8",
    )
    tasks = _retry_unresolved_scenes(state, [])
    assert tasks == []


def test_slide_id_for_key_uses_explicit_provenance(tmp_path):
    state = _state(tmp_path)
    state.slide_candidates = {
        "slide0:ppt/slides/slide1.xml": ["c1"],
        "slide3:ppt/slides/slide4.xml": ["c1"],
    }
    state.concept_slide = {"run/slide3_ppt_slides_slide4.xml/c1": "slide3:ppt/slides/slide4.xml"}
    assert (
        _slide_id_for_key(state, "run/slide3_ppt_slides_slide4.xml/c1")
        == "slide3:ppt/slides/slide4.xml"
    )


def test_idle_resume_does_not_reissue_or_block(tmp_path):
    """Ten resumes with no new responses change nothing (§6, §12.4)."""
    state = _state(tmp_path)
    run_dir = Path(state.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    task = _scene_task("run", "slide0:ppt/slides/slide1.xml", "c1")
    state.task_queue.append(task)
    state.stage = "build"
    state.slide_candidates = {"slide0:ppt/slides/slide1.xml": ["c1"]}
    before = [t.request_id for t in state.task_queue if t.status == "pending"]
    for _ in range(10):
        result = advance_run(state, [])
        assert result.status == "awaiting_harness"
        assert state.stage != "blocked"
    after = [t.request_id for t in state.task_queue if t.status == "pending"]
    assert before == after
