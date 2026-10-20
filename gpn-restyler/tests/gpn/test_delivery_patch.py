"""Causal tests of the actual production call sites, no corporate assets."""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest

from slides_cli.gpn import restyle_runner as runner
from slides_cli.gpn.candidate_jobs import build_ready_candidates
from slides_cli.gpn.constraint_layout import solve_measured_boxes
from slides_cli.gpn.models import (
    CandidateAttemptResult,
    InputKind,
    InputSpec,
    ModelTask,
    ModelTaskKind,
    ModelTaskResponse,
    ProductScope,
    SelectionReport,
)
from slides_cli.gpn.scene_contract import lower_scene_plan, scene_token_errors


def state(tmp_path):
    return runner.RunState(
        run_id="r",
        run_dir=str(tmp_path),
        input_spec=InputSpec(
            kind=InputKind.PROMPT,
            prompt_text="fixture",
            project_root=tmp_path,
            output_path=tmp_path / "out.pptx",
            requested_scope=ProductScope.CREATE,
        ),
    )


def semantic():
    return NS(
        units=[
            NS(id="u1", source_refs=[NS(object_id="source1")]),
            NS(id="u2", source_refs=[NS(object_id="source2")]),
        ]
    )


def scene():
    return {
        "slide_id": "s",
        "candidate_id": "c1",
        "content_refs": ["u1", "u2"],
        "reading_order": ["z2", "z1"],
        "dominant_ref": "z2",
        "zones": [
            {"zone_id": "z1", "content_refs": ["u1"], "ref_roles": {"u1": "value"}},
            {"zone_id": "z2", "content_refs": ["u2"], "ref_roles": {"u2": "caption"}},
        ],
        "relations": [{"from_ref": "u1", "to_ref": "u2", "relation_kind": "sequence"}],
    }


def test_actual_materializer_preserves_roles_and_zone_order():
    plan, errors = runner._materialize_scene_plan(
        scene(), semantic(), source_object_ids={"source1", "source2"}
    )
    assert not errors
    assert plan["content_refs"] == ["source2", "source1"]
    assert plan["reading_order"] == ["source2", "source1"]
    assert plan["dominant_ref"] == "source2"
    assert plan["zones"][0]["ref_roles"] == {"source2": "caption"}


def test_relation_never_grants_overlap_permission():
    plan, errors = lower_scene_plan(scene(), semantic())
    assert not errors
    assert plan["allowed_overlaps"] == []
    assert plan["relations"][0]["from_ref"] == "source1"


def test_duplicate_source_ownership_is_not_silently_deleted():
    authored = scene()
    authored["zones"][1]["content_refs"] = ["u1", "u2"]
    _, errors = lower_scene_plan(authored, semantic())
    assert any(e.startswith("SOURCE_OWNERSHIP_DUPLICATE") for e in errors)


def test_unknown_semantic_reference_is_explicit():
    authored = scene()
    authored["zones"][0]["content_refs"] = ["missing"]
    _, errors = lower_scene_plan(authored, semantic())
    assert "missing" in errors


def test_nested_role_is_validated_before_measurement():
    rules = NS(roles={"body": {}}, colors={}, conditional_colors=[])
    errors = scene_token_errors(
        {
            "zones": [
                {
                    "zone_id": "z",
                    "role": "body",
                    "content_refs": ["u"],
                    "ref_roles": {"u": "invented_metric"},
                }
            ]
        },
        rules,
    )
    assert any("invented_metric" in e for e in errors)


def test_json_role_change_needs_no_new_python_alias():
    rules = NS(roles={"custom_from_json": {}}, colors={}, conditional_colors=[])
    assert not scene_token_errors({"zones": [{"zone_id": "z", "role": "custom_from_json"}]}, rules)


def test_rejected_response_does_not_close_pending_request(tmp_path):
    s = state(tmp_path)
    t = ModelTask(
        request_id="q",
        kind=ModelTaskKind.RULE_REVIEW,
        input_fingerprint="fp",
        response_schema_id="scene-plan/v1",
        expected_response_units=["scene_plan"],
    )
    s.task_queue = [t]
    bad = ModelTaskResponse(
        request_id="q",
        actual_model="model",
        input_fingerprint="wrong",
        schema_version_id="scene-plan/v1",
        payload={"scene_plan": {}},
    )
    assert runner.consume_response(s, bad)
    assert t.status == "pending"
    good = bad.model_copy(update={"input_fingerprint": "fp"})
    assert runner.consume_response(s, good) == []
    assert t.status == "consumed"


def test_flat_c1_does_not_choose_another_slide(tmp_path):
    s = state(tmp_path)
    s.slide_candidates = {"slideA": ["c1"], "slideB": ["c1"]}
    assert runner._slide_id_for_key(s, "r/slideB/c1") == "slideB"
    with pytest.raises(runner.RunnerError):
        runner._slide_id_for_key(s, "r/unknown/c1")


def rect(x=0, y=0, w=100, h=40):
    return {"x": x, "y": y, "w": w, "h": h}


def test_solver_can_move_above_obstacle():
    result = solve_measured_boxes(
        {"text": rect(y=60), "fixed": rect(y=60)}, rect(h=100), movable=["text"]
    )
    assert result.status in ("OPTIMAL", "FEASIBLE")
    assert result.boxes["text"]["y"] + 40 <= 60


def test_solver_can_place_beside_instead_of_stack():
    result = solve_measured_boxes({"a": rect(), "b": rect()}, rect(w=200, h=40))
    assert result.status in ("OPTIMAL", "FEASIBLE")
    assert abs(result.boxes["a"]["x"] - result.boxes["b"]["x"]) >= 100


def test_measured_width_height_are_selected_together():
    result = solve_measured_boxes(
        {"a": rect(w=50, h=90)},
        rect(w=100, h=40),
        variants={"a": [{"w": 50, "h": 90}, {"w": 100, "h": 40}]},
    )
    assert result.status in ("OPTIMAL", "FEASIBLE")
    assert result.choices["a"] == 1


def test_protected_obstacles_can_overlap_each_other():
    result = solve_measured_boxes(
        {"text": rect(x=40, y=20, w=60, h=80), "left": rect(w=20, h=100), "top": rect(w=40, h=20)},
        rect(h=100),
        movable=["text"],
    )
    assert result.status in ("OPTIMAL", "FEASIBLE")


def test_underlay_and_content_remain_contained():
    hints = {"panel": rect(w=100, h=60), "text": rect(x=5, y=5, w=80, h=40)}
    result = solve_measured_boxes(
        hints,
        rect(h=100),
        allowed_pairs={frozenset(hints)},
        container_ids={"panel"},
        variants={"text": [{"w": 80, "h": 70}]},
    )
    assert result.status in ("OPTIMAL", "FEASIBLE")
    assert result.boxes["panel"]["h"] >= 70


def test_container_tight_insets_survive_quantization():
    """Insets are minima: an authored child whose fractional insets sum with
    its width to the container width must not become a 1-EMU contradiction
    when each inset and each extent is quantized independently."""
    # 278.4 - (9.6 + 86.4) leaves right = 182.4 only up to float noise, so
    # ceil-quantising left and right independently overshoots by one EMU.
    left = 9.600000000000023
    parent_w, child_w = 278.4, 86.4
    hints = {
        "container": rect(x=0.0, y=0.0, w=parent_w, h=120.0),
        "child": rect(x=left, y=10.0, w=child_w, h=40.0),
    }
    padding = {
        "l": left,
        "t": 10.0,
        "r": parent_w - left - child_w,
        "b": 120.0 - 10.0 - 40.0,
    }
    result = solve_measured_boxes(
        hints,
        rect(w=600, h=400),
        allowed_pairs={frozenset({"container", "child"})},
        container_ids={"container"},
        containments=[("container", "child", padding)],
    )
    assert result.status in ("OPTIMAL", "FEASIBLE"), result.issues
    parent, child = result.boxes["container"], result.boxes["child"]
    assert child["x"] >= parent["x"] - 1e-6
    assert child["x"] + child["w"] <= parent["x"] + parent["w"] + 1e-6


def test_true_infeasibility_has_a_finite_scope():
    result = solve_measured_boxes({"a": rect(h=60), "b": rect(h=60)}, rect(h=100))
    assert result.status == "INFEASIBLE"
    assert "only" in result.stats["scope"]


def test_global_solver_is_called_by_existing_overlap_entrypoint():
    boxes = {"a": rect(), "b": rect()}
    placed, moves = runner_layout()._resolve_hint_overlaps(boxes, list(boxes), rect(w=200, h=40))
    assert moves[0]["action"] == "constraint_layout"
    assert moves[0]["solver_calls"] == 1
    assert abs(placed["a"]["x"] - placed["b"]["x"]) >= 100


def runner_layout():
    from slides_cli.gpn import layout

    return layout


def test_ready_candidate_does_not_need_other_slides(tmp_path):
    s = state(tmp_path)
    s.slide_candidates = {"slideA": ["c1", "c2"], "slideB": ["c1"]}
    plan = tmp_path / "scenes/slideA/c2/plan.json"
    plan.parent.mkdir(parents=True)
    plan.write_text("{}")
    calls = []

    def build(st, key, selection, config, **kwargs):
        calls.append(selection)
        return CandidateAttemptResult(
            deck_key=key,
            slide_id="slideA",
            candidate_id="c2",
            operation_status="infeasible",
            eligible=False,
        )

    assert not build_ready_candidates(s, [], builder=build, config=None)
    assert calls == [{"slideA": "c2"}]
    s.revision += 999
    build_ready_candidates(s, [], builder=build, config=None)
    assert len(calls) == 1
    plan.write_text('{"changed":true}')
    build_ready_candidates(s, [], builder=build, config=None)
    assert len(calls) == 2


def test_actual_build_entrypoint_uses_per_slide_jobs(tmp_path, monkeypatch):
    s = state(tmp_path)
    s.slide_candidates = {"slideA": ["c1"], "slideB": ["c1"]}
    for sid in s.slide_candidates:
        p = tmp_path / "scenes" / sid / "c1/plan.json"
        p.parent.mkdir(parents=True)
        p.write_text("{}")
    selections = []

    def build(st, key, selection, config, **kwargs):
        selections.append(selection)
        return CandidateAttemptResult(deck_key=key, eligible=False)

    monkeypatch.setattr(runner, "_build_deck_candidate", build)
    runner._build_all_deck_candidates(s, [])
    assert selections == [{"slideA": "c1"}, {"slideB": "c1"}]


def test_typed_final_failure_is_not_unpacked_as_tuple(tmp_path, monkeypatch):
    s = state(tmp_path)
    s.slide_candidates = {"slideA": ["c1"]}
    report = {
        "selections": [
            {
                "source_slide_id": "slideA",
                "candidate_key": {"run_id": "r", "source_slide_id": "slideA", "candidate_id": "c1"},
            }
        ]
    }
    (tmp_path / "selection_report.json").write_text(json.dumps(report))
    monkeypatch.setattr(
        runner,
        "_build_deck_candidate",
        lambda *a, **k: CandidateAttemptResult(
            deck_key="final", eligible=False, eligibility_reason="specific failure"
        ),
    )
    ok, message, _ = runner._assemble_final(s, [])
    assert not ok and "specific failure" in message


def test_partial_selection_cannot_publish_full_deck(tmp_path):
    s = state(tmp_path)
    s.slide_candidates = {"slideA": ["c1"], "slideB": ["c1"]}
    (tmp_path / "selection_report.json").write_text(
        json.dumps(
            {"selections": [{"source_slide_id": "slideA", "candidate_key": {"candidate_id": "c1"}}]}
        )
    )
    assert runner._assemble_final(s, [])[1] == "SELECTION_COVERAGE_MISMATCH"


def test_after_assembly_selection_remains_schema_valid():
    SelectionReport.model_validate(
        {
            "selections": [
                {
                    "source_slide_id": "s",
                    "candidate_key": {"run_id": "r", "source_slide_id": "s", "candidate_id": "c1"},
                    "selected_candidate_sha256": "original",
                    "assembly_linkage": "native assembly",
                }
            ]
        }
    )


def test_profile_blocker_uses_actual_typed_issue_details(tmp_path, monkeypatch):
    from slides_cli.gpn.models import Issue, Severity

    s = state(tmp_path)
    issue = Issue(
        rule_id="CORPUS",
        code="ONTOLOGY_MISSING",
        severity=Severity.ERROR,
        details="specific diagnostic",
    )
    monkeypatch.setattr(
        "slides_cli.gpn.pipeline.prepare_corporate_profiles",
        lambda **kwargs: NS(status="needs_review", blockers=[issue]),
    )
    ok, message = runner._profile_stage(s)
    assert not ok
    assert "specific diagnostic" in message
