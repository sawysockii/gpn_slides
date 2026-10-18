"""Actual dispatcher scheduling and validation, with synthetic run inputs."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from slides_cli.gpn.models import (
    CandidateKey,
    CompiledOntology,
    ModelTask,
    ModelTaskKind,
    ModelTaskResponse,
    RoleStyle,
    SemanticDeck,
    SemanticSlide,
    SemanticUnit,
    SourceEvidenceRef,
    VisualConcept,
)
from slides_cli.gpn.patch_activation import activate_run
from slides_cli.gpn.response_validation import validate_payload
from slides_cli.gpn.restyle_runner import RunState, save_state

TOOL = Path(__file__).resolve().parents[2] / "tools/harness_batch.py"
spec = importlib.util.spec_from_file_location("delivery_dispatch", TOOL)
dispatch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dispatch)


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False))


def pending_case(batch, name, spent):
    root = batch / name / "run"
    task = {
        "request_id": f"{name}-semantic",
        "kind": "semantic_analysis",
        "status": "pending",
        "input_fingerprint": name,
        "response_schema_id": "semantic-deck/v1",
        "packet": "model_requests/packet.json",
        "expected_response_units": ["slides"],
    }
    write(root / "run_state.json", {"stage": "semantic", "tasks_used": spent, "task_queue": [task]})
    write(root / task["packet"], {"stage": "semantic_analysis", "case": name})
    return root, task


def test_untouched_cases_are_serviced_before_old_ntc_backlog(tmp_path):
    pending_case(tmp_path, "ntc", 42)
    pending_case(tmp_path, "round_table", 0)
    pending_case(tmp_path, "iii", 0)
    first = dispatch.next_work(tmp_path)
    assert Path(first["run_dir"]).parent.name in {"round_table", "iii"}
    assert dispatch.next_work(tmp_path) == first
    journal = dispatch.read(tmp_path / ".harness_dispatch.json")
    run = Path(first["run_dir"])
    state = dispatch.read(run / "run_state.json")
    state["tasks_used"] = 1
    state["task_queue"][0]["status"] = "consumed"
    write(run / "run_state.json", state)
    journal["sequence"] = 1
    journal["served"][str(run)] = 1
    journal.pop("outstanding")
    write(tmp_path / ".harness_dispatch.json", journal)
    second = dispatch.next_work(tmp_path)
    assert second["run_dir"] != first["run_dir"]
    assert Path(second["run_dir"]).parent.name in {"round_table", "iii"}


def test_missing_packet_does_not_starve_other_case(tmp_path):
    broken, task = pending_case(tmp_path, "bad", 0)
    (broken / task["packet"]).unlink()
    good, _ = pending_case(tmp_path, "good", 0)
    assert dispatch.next_work(tmp_path)["run_dir"] == str(good)


def test_finish_checks_product_bytes_not_only_accepted_state(tmp_path):
    run, _ = pending_case(tmp_path, "case", 0)
    write(
        run / "run_state.json",
        {
            "stage": "accepted",
            "accepted_product": {
                "output_path": str(tmp_path / "missing.pptx"),
                "candidate_sha256": "fake",
            },
        },
    )
    assert not dispatch.batch_complete(tmp_path)


def test_dispatcher_never_executes_archival_author_scripts():
    source = TOOL.read_text()
    assert "AUTHORS =" not in source
    assert "run / 'harness'" not in source
    assert "--response" in source


def test_semantic_response_accepts_separate_transformation_ledger(tmp_path):
    task = ModelTask(request_id="q", kind=ModelTaskKind.SEMANTIC_ANALYSIS)
    payload = {
        "slides": [{"source_slide_id": "s", "units": []}],
        "transformation_decisions": [{"source_object_id": "o", "action": "recompose"}],
    }
    response = ModelTaskResponse(request_id="q", actual_model="test", payload=payload)
    assert not validate_payload(tmp_path, task, response)


def scene_context(root):
    from slides_cli.gpn.models import InputKind, InputSpec, ProductScope

    sid = "slide0:ppt/slides/slide1.xml"
    key = CandidateKey(run_id="r", source_slide_id=sid, candidate_id="c1")
    unit = SemanticUnit(
        id="u",
        kind="text",
        source_refs=[
            SourceEvidenceRef(artifact_id="source", relative_path="source_ir.json", object_id="o")
        ],
    )
    deck = SemanticDeck(slides=[SemanticSlide(source_slide_id=sid, units=[unit])])
    write(root / "inputs/semantic_deck.json", deck.model_dump(mode="json"))
    write(root / "source_ir.json", {"slides": [{"id": sid, "objects": [{"id": "o"}]}]})
    safe = sid.replace("/", "_").replace(":", "_")
    write(
        root / "scenes" / safe / "c1/concept.json",
        VisualConcept(concept_id="c1").model_dump(mode="json"),
    )
    rules = CompiledOntology(
        roles={"body": RoleStyle(role="body", allowed_faces=["Test"], default_size_pt=12)}
    )
    write(root / "compiled_rules.json", rules.model_dump(mode="json"))
    task = ModelTask(request_id="old-scene", kind=ModelTaskKind.SCENE_GENERATION, candidate_key=key)
    state = RunState(
        run_id="r",
        run_dir=str(root),
        input_spec=InputSpec(
            kind=InputKind.PROMPT,
            prompt_text="test",
            project_root=root,
            output_path=root / "out.pptx",
            requested_scope=ProductScope.CREATE,
        ),
        slide_candidates={sid: ["c1"]},
        task_queue=[task],
    )
    save_state(state)
    plan = {
        "slide_id": sid,
        "candidate_id": "c1",
        "content_refs": ["u"],
        "zones": [
            {
                "zone_id": "z",
                "role": "body",
                "content_refs": ["u"],
                "x_pct": 0,
                "y_pct": 0,
                "w_pct": 100,
                "h_pct": 100,
            }
        ],
        "reading_order": ["z"],
        "dominant_ref": "z",
    }
    return state, task, plan


def test_nested_unknown_style_rejected_before_consumption(tmp_path):
    from slides_cli.gpn.restyle_runner import consume_response

    state, task, plan = scene_context(tmp_path)
    task.expected_response_units = ["scene_plan"]
    plan["zones"][0]["ref_roles"] = {"u": "metric_value"}
    response = ModelTaskResponse(
        request_id=task.request_id, actual_model="test", payload={"scene_plan": plan}
    )
    errors = consume_response(state, response)
    assert any("metric_value" in e for e in errors)
    assert task.status == "pending"
    assert not state.consumed_request_ids
    plan["zones"][0]["ref_roles"] = {"u": "body"}
    assert (
        consume_response(state, response.model_copy(update={"payload": {"scene_plan": plan}})) == []
    )
    assert task.status == "consumed"


def test_activation_is_explicit_and_idempotent(tmp_path):
    state, task, _ = scene_context(tmp_path)
    task.packet = "model_requests/old.json"
    write(tmp_path / task.packet, {"old_contract": True})
    save_state(state)
    before = (tmp_path / "run_state.json").read_bytes()
    preview = activate_run(tmp_path)
    assert preview["applied"] is False
    assert (tmp_path / "run_state.json").read_bytes() == before
    result = activate_run(tmp_path, apply=True)
    assert result["applied"]
    assert task.request_id in result["superseded"]
    after = (tmp_path / "run_state.json").read_bytes()
    assert activate_run(tmp_path, apply=True)["status"] == "already_active"
    assert (tmp_path / "run_state.json").read_bytes() == after


def test_scene_repair_hash_includes_actual_evidence_before_issue(tmp_path):
    from slides_cli.gpn.scene_jobs import request_scene_repair

    state, old, _ = scene_context(tmp_path)
    old.status = "consumed"
    task = request_scene_repair(
        state,
        old.candidate_key.source_slide_id,
        "c1",
        [{"measured_overflow_pt": 2.7}],
        reason="measured_failure",
    )
    packet = tmp_path / task.packet
    import hashlib

    assert task.input_fingerprint == hashlib.sha256(packet.read_bytes()).hexdigest()
    original = packet.read_bytes()
    assert (
        request_scene_repair(
            state,
            old.candidate_key.source_slide_id,
            "c1",
            [{"measured_overflow_pt": 2.7}],
            reason="measured_failure",
        )
        is None
    )
    assert packet.read_bytes() == original


@pytest.mark.parametrize("field", ["request_id", "input_fingerprint", "schema_version_id"])
def test_response_cannot_be_resigned_for_new_task(tmp_path, field):
    run, task = pending_case(tmp_path, "case", 0)
    response = {
        "request_id": task["request_id"],
        "input_fingerprint": task["input_fingerprint"],
        "schema_version_id": task["response_schema_id"],
        "actual_model": "test",
        "payload": {"slides": []},
    }
    response[field] = "archival-other-task"
    with pytest.raises(ValueError, match="RESPONSE_IDENTITY"):
        dispatch.validate_response(run, task, response)


def test_comparison_gate_requires_every_slide_and_real_concept_difference(tmp_path):
    from types import SimpleNamespace

    from slides_cli.gpn.scene_jobs import comparison_coverage

    state = SimpleNamespace(
        run_dir=str(tmp_path),
        slide_candidates={"a": ["c1", "c2"], "b": ["c1", "c2"]},
        deck_candidates=[],
    )
    for sid in ("a", "b"):
        for cid, topology in (("c1", "comparison"), ("c2", "process_flow")):
            key = sid + cid
            state.deck_candidates.append(
                {
                    "deck_key": key,
                    "scope_kind": "slide_candidate",
                    "selection": {sid: cid},
                    "render_valid": True,
                    "sha256": key,
                }
            )
            write(
                tmp_path / "scenes" / sid / cid / "concept.json",
                {"concept_id": cid, "group_topology": {"topology": topology}},
            )
            for kind in ("visual_review", "design_review"):
                write(
                    tmp_path / "scenes" / key / f"{kind}.json",
                    {"candidate_sha256": key, "verdict": "accept"},
                )
    assert comparison_coverage(state, ["ac1", "bc1"])[0]
    state.deck_candidates[-1]["render_valid"] = False
    ok, missing = comparison_coverage(state, ["ac1", "bc1"])
    assert not ok and "b" in missing
    state.deck_candidates[-1]["render_valid"] = True
    write(
        tmp_path / "scenes/b/c2/concept.json",
        {"concept_id": "different_id_only", "group_topology": {"topology": "comparison"}},
    )
    assert comparison_coverage(state, ["ac1", "bc1"])[0] is False


def test_final_review_routes_to_the_actual_selected_scene(tmp_path):
    import hashlib

    from slides_cli.gpn.scene_jobs import repair_final_reviews

    state, old, _ = scene_context(tmp_path)
    old.status = "consumed"
    sid = old.candidate_key.source_slide_id
    path = tmp_path / "scenes/final/candidate.pptx"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"test-only artifact identity")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    write(
        tmp_path / "selection_report.json",
        {"selections": [{"source_slide_id": sid, "candidate_key": {"candidate_id": "c1"}}]},
    )
    write(
        path.parent / "design_review.json",
        {
            "candidate_sha256": digest,
            "verdict": "repair",
            "findings": [{"scope": sid, "message": "hierarchy is flat"}],
        },
    )
    tasks = repair_final_reviews(state)
    assert len(tasks) == 1
    assert tasks[0].candidate_key == old.candidate_key
    packet = dispatch.read(tmp_path / tasks[0].packet)
    assert packet["repair_reason"] == "final_review"
    assert packet["repair_evidence"][0]["review"]["candidate_sha256"] == digest


def test_missing_registered_case_cannot_be_called_complete(tmp_path):
    run, _ = pending_case(tmp_path, "case", 0)
    write(
        tmp_path / "delivery_batch_manifest.json",
        {"expected_runs": [str(run), str(tmp_path / "other/run")]},
    )
    assert dispatch.batch_complete(tmp_path) is False
