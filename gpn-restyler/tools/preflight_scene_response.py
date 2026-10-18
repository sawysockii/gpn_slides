"""Pre-flight a harness-authored scene response before `resume`.

Validates a candidate model response against the ACTUAL current contracts
(Pydantic LayoutIntent, validate_scene_response, _materialize_scene_plan)
so a schema/namespace/coverage defect is found locally instead of being
discovered through the resume -> reject -> reopen loop.

Usage:
    uv run python tools/preflight_scene_response.py <run_dir> <response.json>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from slides_cli.gpn.models import (
    CompiledOntology,
    ModelTask,
    ModelTaskResponse,
    SemanticDeck,
    VisualConcept,
)
from slides_cli.gpn.planner import validate_scene_response
from slides_cli.gpn.restyle_runner import _materialize_scene_plan


def main(argv: list[str]) -> int:
    run_dir = Path(argv[1]).resolve()
    resp_path = Path(argv[2]).resolve()
    state = json.loads((run_dir / "run_state.json").read_text(encoding="utf-8"))
    raw = json.loads(resp_path.read_text(encoding="utf-8"))
    rid = raw.get("request_id")
    task = next((t for t in state["task_queue"] if t["request_id"] == rid), None)
    if task is None:
        print(f"FAIL: unknown request_id {rid}")
        return 1
    if raw.get("input_fingerprint") != task["input_fingerprint"]:
        print("FAIL: input_fingerprint does not match the active request")
        return 1
    if not raw.get("actual_model"):
        print("FAIL: actual_model missing")
        return 1

    task_m = ModelTask.model_validate(task)
    task = task_m
    resp_m = ModelTaskResponse.model_validate(raw)

    deck = SemanticDeck.model_validate_json(
        (run_dir / "inputs" / "semantic_deck.json").read_text(encoding="utf-8"))
    sid = task.candidate_key.source_slide_id if task.candidate_key else ""
    sem = next((s for s in deck.slides if s.source_slide_id == sid), None)
    if sem is None:
        print(f"FAIL: no semantic slide for {sid}")
        return 1
    rules = CompiledOntology.model_validate_json(
        (run_dir / "compiled_rules.json").read_text(encoding="utf-8"))
    concept = None
    cpath = run_dir / "scenes" / sid.replace("/", "_").replace(":", "_") \
        / task.candidate_key.candidate_id / "concept.json"
    if cpath.is_file():
        concept = VisualConcept.model_validate_json(
            cpath.read_text(encoding="utf-8"))

    ok = True
    if concept is not None:
        report = validate_scene_response(resp_m, task_m, sem, concept, rules)
        if not report.valid:
            ok = False
            for issue in report.hard_issues:
                print(f"FAIL schema/context: {issue}")
    plan = resp_m.payload.get("scene_plan") or {}
    from slides_cli.gpn.restyle_runner import _slide_source_objects
    known = {str(o.get("id", "")) for o in _slide_source_objects(run_dir, sid)}
    known.discard("")
    _, unexpanded = _materialize_scene_plan(plan, sem, [], task.candidate_key
                                            and str(task.candidate_key) or "",
                                            source_object_ids=known)
    if unexpanded:
        ok = False
        print(f"FAIL materialization: unresolved refs {unexpanded[:8]}")
    print("OK" if ok else "NOT READY")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))