"""Stage 7 §8/§12.1: durable runner + ingest regressions."""

from __future__ import annotations

from pathlib import Path

import pytest

from slides_cli.gpn.document_ingest import (
    IngestError,
    detect_kind,
    extract_prompt,
    ingest_input,
    validate_semantic_response,
)
from slides_cli.gpn.models import (
    InputKind,
    InputSpec,
    ModelTaskResponse,
    ProductScope,
)
from slides_cli.gpn.restyle_runner import (
    RunnerError,
    advance_run,
    load_state,
    publish_accepted_product,
    resume_run,
    run_status,
    save_state,
    schedule_repairs,
    start_run,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _spec(tmp_path: Path, kind=InputKind.PROMPT, text="Brief text 400 FTE"):
    # Profile grounds on the real ontology/examples/fonts (§3.1); a bare
    # tmp root has no corpora, so advancing tests use PROJECT_ROOT.
    return InputSpec(
        kind=kind,
        prompt_text=text,
        project_root=PROJECT_ROOT,
        output_path=tmp_path / "out" / "deck.pptx",
        requested_scope=ProductScope.CREATE,
    )


@pytest.fixture(autouse=True)
def isolated_runner_profile(monkeypatch):
    # This module tests orchestration, not corporate corpus/font discovery.
    # Real profile integration remains in test_pipeline/test_fonts.
    monkeypatch.setattr(
        "slides_cli.gpn.restyle_runner._profile_stage",
        lambda state: (True, "test-only profile boundary"),
    )


def test_start_run_creates_durable_state(tmp_path):
    state = start_run(_spec(tmp_path), config=None, run_dir=tmp_path / "run1")
    assert (tmp_path / "run1" / "run_state.json").is_file()
    assert (tmp_path / "run1" / "runner_transition_log.jsonl").is_file()
    assert state.snapshots.input_sha256
    reloaded = load_state(tmp_path / "run1")
    assert reloaded.run_id == state.run_id


def test_start_run_emits_semantic_task_and_waits(tmp_path):
    state = start_run(_spec(tmp_path), config=None, run_dir=tmp_path / "run2")
    result = advance_run(state, [])
    assert result.status == "awaiting_harness"
    assert len(result.pending_tasks) == 1
    assert result.pending_tasks[0].kind in ("semantic_analysis", "narrative_plan")


def test_stale_response_rejected_without_changing_the_question(tmp_path):
    state = start_run(_spec(tmp_path), config=None, run_dir=tmp_path / "run3")
    result = advance_run(state, [])
    task_id = result.pending_tasks[0].request_id
    bad = ModelTaskResponse(
        request_id=task_id,
        input_fingerprint="wrong",
        actual_model="test-model",
        schema_version_id="x",
        payload={},
    )
    result2 = advance_run(state, [bad])
    assert any(f.code == "RESPONSE_REJECTED" for f in result2.findings)
    assert any(t.status == "pending" and t.request_id == task_id for t in state.task_queue)
    assert len(state.task_queue) == 1


def test_double_consume_rejected(tmp_path):
    state = start_run(_spec(tmp_path), config=None, run_dir=tmp_path / "run4")
    result = advance_run(state, [])
    task = next(t for t in state.task_queue if t.request_id == result.pending_tasks[0].request_id)
    good = ModelTaskResponse(
        request_id=task.request_id,
        input_fingerprint=task.input_fingerprint,
        actual_model="test-model",
        schema_version_id=task.response_schema_id,
        payload=dict.fromkeys(task.expected_response_units, "v"),
    )
    advance_run(state, [good])
    result2 = advance_run(state, [good])
    assert any(f.code == "RESPONSE_REJECTED" for f in result2.findings)


def test_resume_idempotent(tmp_path):
    state = start_run(_spec(tmp_path), config=None, run_dir=tmp_path / "run5")
    advance_run(state, [])
    before = state.revision
    result = resume_run(tmp_path / "run5", config=None, responses=[])
    assert result.status in ("awaiting_harness", "running")
    reloaded = load_state(tmp_path / "run5")
    assert reloaded.revision >= before


def test_budget_exhaustion_terminal(tmp_path):
    state = start_run(_spec(tmp_path), config=None, run_dir=tmp_path / "run6")
    state.budget.max_model_tasks = 0
    save_state(state)
    result = advance_run(state, [])
    assert result.status == "budget_exhausted"


def test_publication_guarded_without_acceptance(tmp_path):
    from slides_cli.gpn.models import ProductAcceptance

    state = start_run(_spec(tmp_path), config=None, run_dir=tmp_path / "run7")
    acc = ProductAcceptance(accepted=False, subject_candidate_sha256="x" * 64)
    with pytest.raises(RunnerError):
        publish_accepted_product(state, acc)


def test_schedule_repairs_rejects_spent_candidate(tmp_path):
    from slides_cli.gpn.models import CandidateKey, RepairFinding

    state = start_run(_spec(tmp_path), config=None, run_dir=tmp_path / "run8")
    key = CandidateKey(run_id="r", source_slide_id="s", candidate_id="c")
    state.budget.max_repairs_per_candidate = 1
    state.candidate_states[str(key)] = "rendered"
    state.candidate_revisions[str(key)] = 1
    finding = RepairFinding(
        finding_id="f1",
        subject_candidate_sha256="a" * 64,
        candidate_key=key,
        defect="overlap",
        severity="error",
    )
    tasks = schedule_repairs(state, [finding])
    assert tasks == []
    assert state.candidate_states[str(key)] == "rejected"


def test_run_status_shape(tmp_path):
    state = start_run(_spec(tmp_path), config=None, run_dir=tmp_path / "run9")
    status = run_status(tmp_path / "run9")
    assert status["run_id"] == state.run_id
    assert "pending_tasks" in status and "budget" in status


# --- ingest ---------------------------------------------------------------


def test_detect_kind():
    assert detect_kind(Path("a.pptx")) is InputKind.PPTX
    assert detect_kind(Path("a.pdf")) is InputKind.PDF
    assert detect_kind(Path("a.docx")) is InputKind.DOCX
    with pytest.raises(IngestError):
        detect_kind(Path("a.xlsx"))


def test_extract_prompt_blocks():
    doc = extract_prompt("Title line\n\n- item one\n- item two 400 FTE")
    assert doc.blocks
    assert doc.content_sha256


def test_ingest_prompt_writes_manifest(tmp_path):
    doc = ingest_input(_spec(tmp_path), tmp_path / "run10")
    assert (tmp_path / "run10" / "inputs" / "source_manifest.json").is_file()
    assert doc.input_kind is InputKind.PROMPT


def test_ingest_missing_file_typed_error(tmp_path):
    spec = InputSpec(
        kind=InputKind.PDF,
        path=Path("nope.pdf"),
        project_root=tmp_path,
        output_path=tmp_path / "o.pptx",
        requested_scope=ProductScope.CREATE,
    )
    with pytest.raises(IngestError):
        ingest_input(spec, tmp_path / "run11")


def test_narrative_missing_facts_flagged(tmp_path):
    from slides_cli.gpn.document_ingest import SourceBlock, SourceDocument

    source = SourceDocument(
        input_kind=InputKind.TXT,
        content_sha256="a" * 64,
        source_sha256="a" * 64,
        blocks=[SourceBlock(block_id="b1", text="Budget 400 FTE in 2026")],
    )
    response = ModelTaskResponse(
        request_id="t",
        input_fingerprint="x",
        actual_model="m",
        schema_version_id="s",
        payload={"slides": [{"title": "hi"}]},
    )
    errors = validate_semantic_response(response, source)
    assert any("NARRATIVE_REQUIRED_FACT_MISSING" in e for e in errors)
