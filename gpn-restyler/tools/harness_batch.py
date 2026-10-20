#!/usr/bin/env python3
"""Fair work orders for the CURRENT harness model. No model impersonation.

Run inside the project environment:
  uv run python tools/harness_batch.py --batch-root <batch> next
  uv run python tools/harness_batch.py --batch-root <batch> submit <response.json>
  uv run python tools/harness_batch.py --batch-root <batch> finish

The model reads the returned work order and images, supplies a real answer,
then submits it. This driver never executes run-local author_* scripts.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".dispatch-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def digest(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def discover_runs(batch: Path) -> list[Path]:
    if (batch / "run_state.json").is_file():
        return [batch]
    return sorted(
        {
            p.parent.resolve()
            for pattern in ("*/run/run_state.json", "*/run_state.json")
            for p in batch.glob(pattern)
        }
    )


def packet_path(run: Path, task: dict) -> Path:
    path = (run / task["packet"]).resolve()
    if not path.is_relative_to(run.resolve()):
        raise ValueError("REQUEST_PACKET_OUTSIDE_RUN")
    return path


def _queue(batch: Path) -> tuple[list[dict], list[dict]]:
    candidates, problems = [], []
    for run in discover_runs(batch):
        state = read(run / "run_state.json")
        if state.get("stage") == "accepted":
            continue
        pending = [t for t in state.get("task_queue", []) if t.get("status") == "pending"]
        for index, task in enumerate(pending):
            try:
                path = packet_path(run, task)
                if not path.is_file():
                    raise ValueError(f"REQUEST_PACKET_MISSING:{path}")
                packet = read(path)
            except (OSError, ValueError, KeyError) as error:
                problems.append(
                    {"run": str(run), "request_id": task.get("request_id"), "error": str(error)}
                )
                continue
            candidates.append(
                {
                    "run": run,
                    "state": state,
                    "task": task,
                    "packet": packet,
                    "packet_path": path,
                    "queue_index": index,
                }
            )
    return candidates, problems


def _refresh_stale_reviews(batch: Path) -> None:
    """Pending reviews must describe current pixels, not a prior render env.

    ``next`` used to serve a queued review packet even after the renderer or
    font environment changed. Refresh deterministic renders first; stale
    pending reviews are marked superseded and regenerated on the next stage
    tick instead of being answered against an obsolete PNG.
    """
    from slides_cli.gpn.restyle_runner import (
        RunState,
        _render_all_candidates,
        save_state,
    )

    review_kinds = ("visual_review", "design_review")
    for run in discover_runs(batch):
        path = run / "run_state.json"
        if not path.is_file():
            continue
        raw = read(path)
        if raw.get("stage") == "accepted":
            continue
        if not any(
            t.get("status") == "pending" and t.get("kind") in review_kinds
            for t in raw.get("task_queue", [])
        ):
            continue
        state = RunState.model_validate(raw)
        changed = _render_all_candidates(state)
        if changed or any(t.status == "superseded" for t in state.task_queue):
            save_state(state)


def next_work(batch: Path) -> dict:
    journal_path = batch / ".harness_dispatch.json"
    journal = read(journal_path) if journal_path.is_file() else {"sequence": 0, "served": {}}
    _refresh_stale_reviews(batch)
    candidates, problems = _queue(batch)
    active = journal.get("outstanding")
    chosen = next(
        (
            c
            for c in candidates
            if active
            and str(c["run"]) == active["run"]
            and c["task"]["request_id"] == active["request_id"]
            and c["task"].get("input_fingerprint") == active["input_fingerprint"]
        ),
        None,
    )
    if chosen is None and candidates:
        # First service the semantic inputs the previous loop never answered.
        def priority(c):
            untouched = c["state"].get("tasks_used", 0) == 0 and c["task"]["kind"] in (
                "semantic_analysis",
                "narrative_plan",
            )
            return (
                0 if untouched else 1,
                journal["served"].get(str(c["run"]), -1),
                # Within a case, finish ready downstream work before asking
                # for every brief in the deck. Across cases, fairness wins.
                {
                    "candidate_selection": 0,
                    "design_review": 1,
                    "visual_review": 2,
                    "scene_repair": 3,
                    "scene_generation": 4,
                    "visual_concept_generation": 5,
                    "design_brief": 6,
                }.get(c["task"]["kind"], 7),
                c["queue_index"],
                str(c["run"]),
            )

        chosen = min(candidates, key=priority)
    if chosen is None:
        return {
            "status": "no_model_task_ready",
            "product_complete": batch_complete(batch),
            "packet_problems": problems,
            "next_action": "advance deterministic work or fix named missing inputs",
        }
    run, task = chosen["run"], chosen["task"]
    rules_path = run / "compiled_rules.json"
    rules = read(rules_path) if rules_path.is_file() else {}
    images = []
    for raw in task.get("required_images", []):
        p = Path(raw)
        images.append(str(p if p.is_absolute() else run / p))
    # Old immutable scene packets used only slide-01.png, while pdftoppm
    # emitted slide-1.png for NTC. Add current source context to the work
    # order without changing the issued task, its ID, or its fingerprint.
    sid = chosen["packet"].get("slide_id")
    sid = sid or chosen["packet"].get("semantic_slide", {}).get("source_slide_id")
    sid = sid or chosen["packet"].get("design_brief", {}).get("slide_id")
    if sid:
        from slides_cli.gpn.source_images import source_slide_images

        images.extend(source_slide_images(run, sid))
    images = list(dict.fromkeys(images))
    identity = {
        "run": str(run),
        "request_id": task["request_id"],
        "input_fingerprint": task.get("input_fingerprint", ""),
    }
    order_id = digest(identity)
    order_path = batch / "harness_work" / f"{order_id}.json"
    response_path = run / "model_responses" / f"{digest(task['request_id'])}.json"
    from slides_cli.gpn import models
    from slides_cli.gpn.frame_contract import model_frame_context
    from slides_cli.gpn.planner import LayoutIntent

    reference_context = {"references": [], "required_images": [], "issues": []}
    if sid and task["kind"] in (
        "design_brief",
        "visual_concept_generation",
        "scene_generation",
        "scene_repair",
    ):
        from slides_cli.gpn.owned_examples import render_owned_examples, select_owned_examples

        semantic = models.SemanticDeck.model_validate(read(run / "inputs/semantic_deck.json"))
        slide = next(s for s in semantic.slides if s.source_slide_id == sid)
        project = Path(chosen["state"]["input_spec"]["project_root"])
        examples = select_owned_examples(project, slide)
        if examples:
            reference_context = render_owned_examples(
                project, examples, font_inventory=run / "font_inventory.json"
            )
            images = list(dict.fromkeys(images + reference_context["required_images"]))

    schemas = {
        "semantic_analysis": models.SemanticDeck,
        "design_brief": models.SlideDesignBrief,
        "visual_concept_generation": models.VisualConcept,
        "scene_generation": LayoutIntent,
        "scene_repair": LayoutIntent,
        "candidate_selection": models.SelectionReport,
    }
    model = schemas.get(task["kind"])
    if task["kind"] in ("scene_generation", "scene_repair", "visual_concept_generation"):
        from pydantic import ConfigDict, create_model

        # The payload is a wrapper, not a bare LayoutIntent/VisualConcept.
        field = "concepts" if task["kind"] == "visual_concept_generation" else "scene_plan"
        field_type = list[models.VisualConcept] if field == "concepts" else LayoutIntent
        model = create_model(
            "ConceptSetPayload" if field == "concepts" else "ScenePayload",
            __config__=ConfigDict(extra="forbid"),
            **{field: (field_type, ...)},
        )
    work = {
        "status": "model_work_required",
        "product_complete": False,
        "work_order_id": order_id,
        "run_dir": str(run),
        "task": task,
        "packet_path": str(chosen["packet_path"]),
        "packet": chosen["packet"],
        "payload_schema": model.model_json_schema() if model else {},
        "required_images": images,
        "missing_images": [p for p in images if not Path(p).is_file()],
        "source_slide_id": sid,
        "loaded_role_styles": rules.get("roles", {}),
        "loaded_colors": rules.get("colors", {}),
        "layout_frame": model_frame_context(run),
        "owned_reference_context": reference_context,
        "response_path": str(response_path),
        "response_envelope": {
            "schema_version": "model-task-response/v1",
            "request_id": task["request_id"],
            "input_fingerprint": task.get("input_fingerprint", ""),
            "schema_version_id": task.get("response_schema_id", ""),
            "actual_model": "<current harness model; fill honestly>",
            "payload": {},
        },
        "instructions": [
            "Read the packet and required images, then AUTHOR the response as the current model.",
            "Do not run archival semantic_author/concepts_author/scenes_author scripts.",
            "Use exact ontology tokens; semantic roles are not automatically style roles.",
            "Group related source spans into logical native blocks; no per-source-object gap tax.",
            "Write the full response at response_path; never re-sign old answers for new tasks.",
            "Submit it with this tool, then request next. awaiting_harness is work for YOU.",
            "Continue all cases. finish is successful only when every case is accepted.",
        ],
    }
    write(order_path, work)
    journal["outstanding"] = identity
    write(journal_path, journal)
    return {
        "status": "model_work_required",
        "work_order": str(order_path),
        "run_dir": str(run),
        "request_id": task["request_id"],
        "kind": task["kind"],
        "response_path": str(response_path),
        "required_images": images,
        "missing_images": work["missing_images"],
        "product_complete": False,
        "next_action": "Read work_order, author response, submit, next; do not end the task here.",
    }


def audit_progress(batch: Path) -> dict:
    """Read-only counters: historical queue length is not spent model budget."""
    from collections import Counter

    cases = []
    for run in discover_runs(batch):
        state = read(run / "run_state.json")
        tasks = state.get("task_queue", [])
        pending = [t for t in tasks if t.get("status") == "pending"]
        spent = state.get("tasks_used", 0)
        limit = state.get("budget", {}).get("max_model_tasks", 0)
        candidates = []
        rendered = []
        for job in state.get("deck_candidates", []):
            raw = job.get("candidate.pptx")
            path = run / raw if raw else None
            if (
                path
                and path.is_file()
                and hashlib.sha256(path.read_bytes()).hexdigest() == job.get("sha256")
            ):
                candidates.append(job["deck_key"])
                manifest_path = run / str(job.get("render_manifest", "__missing__"))
                if manifest_path.is_file():
                    manifest = read(manifest_path)
                    if (
                        manifest.get("candidate_sha256") == job["sha256"]
                        and manifest.get("pages")
                        and all(
                            (manifest_path.parent / p["image"]).is_file()
                            and hashlib.sha256(
                                (manifest_path.parent / p["image"]).read_bytes()
                            ).hexdigest()
                            == p["sha256"]
                            for p in manifest["pages"]
                        )
                    ):
                        rendered.append(job["deck_key"])
        cases.append(
            {
                "run": str(run),
                "stage": state["stage"],
                "historical_tasks": len(tasks),
                "task_statuses": dict(Counter(t.get("status") for t in tasks)),
                "pending_by_kind": dict(Counter(t["kind"] for t in pending)),
                "model_responses_consumed": spent,
                "max_model_tasks": limit,
                "pending_reservations": len(pending),
                "unreserved_model_capacity": limit - spent - len(pending),
                "budget_counts_rejected_history": False,
                "counter_consistent": spent == len(state.get("consumed_request_ids", [])),
                "native_candidates_on_disk": candidates,
                "current_renders_on_disk": rendered,
                "accepted_product": state.get("accepted_product"),
            }
        )
    return {
        "schema_version": "delivery-progress/v2",
        "cases": cases,
        "product_complete": batch_complete(batch),
    }


def validate_response(run: Path, task: dict, response: dict) -> None:
    from slides_cli.gpn.models import ModelTask, ModelTaskResponse

    typed_task, typed_response = (
        ModelTask.model_validate(task),
        ModelTaskResponse.model_validate(response),
    )
    for name, expected in (
        ("request_id", typed_task.request_id),
        ("input_fingerprint", typed_task.input_fingerprint),
        ("schema_version_id", typed_task.response_schema_id),
    ):
        if getattr(typed_response, name) != expected:
            raise ValueError(f"RESPONSE_IDENTITY:{name}:expected={expected!r}")
    if not typed_response.actual_model or typed_response.actual_model.startswith("<"):
        raise ValueError("ACTUAL_MODEL_REQUIRED")
    missing = set(typed_task.expected_response_units) - set(typed_response.payload)
    if missing:
        raise ValueError(f"RESPONSE_FIELDS_MISSING:{sorted(missing)}")
    from slides_cli.gpn.response_validation import validate_payload

    errors = validate_payload(run, typed_task, typed_response)
    if errors:
        raise ValueError(json.dumps(errors, ensure_ascii=False))


def submit(batch: Path, response_path: Path, repo: Path) -> dict:
    journal_path = batch / ".harness_dispatch.json"
    journal = read(journal_path)
    outstanding = journal.get("outstanding")
    if not outstanding:
        raise ValueError("NO_OUTSTANDING_WORK_ORDER")
    run = Path(outstanding["run"])
    state = read(run / "run_state.json")
    task = next(
        t
        for t in state["task_queue"]
        if t["request_id"] == outstanding["request_id"] and t["status"] == "pending"
    )
    response = read(response_path)
    try:
        validate_response(run, task, response)
    except (ValueError, KeyError, StopIteration, OSError) as error:
        report = {
            "status": "response_repair_required",
            "request_id": task["request_id"],
            "error": str(error),
            "response_path": str(response_path),
            "next_action": "Correct this response; the same request remains pending.",
        }
        write(batch / "harness_work" / f"rejected-{digest(response)}.json", report)
        return report
    command = [
        sys.executable,
        "-m",
        "slides_cli.cli",
        "gpn",
        "resume",
        str(run),
        "--response",
        str(response_path.resolve()),
    ]
    result = subprocess.run(command, cwd=repo, capture_output=True, text=True, timeout=1800)
    try:
        output = json.loads(result.stdout)
    except ValueError:
        output = {"status": "cli_output_invalid", "stdout": result.stdout}
    after = read(run / "run_state.json")
    accepted_transport = task["request_id"] in after.get("consumed_request_ids", [])
    receipt = {
        "command": command,
        "exit_code": result.returncode,
        "runner": output,
        "stderr": result.stderr,
        "request_consumed": accepted_transport,
        "response_sha256": digest(response),
    }
    write(batch / "harness_work" / f"receipt-{digest(outstanding)}.json", receipt)
    if accepted_transport:
        journal["sequence"] += 1
        journal["served"][str(run)] = journal["sequence"]
        journal.pop("outstanding", None)
        write(journal_path, journal)
    return {
        "status": "submitted" if accepted_transport else "runner_rejected",
        "receipt": receipt,
        "product_complete": batch_complete(batch),
        "next_action": "Run next and continue the existing assignment.",
    }


def batch_complete(batch: Path) -> bool:
    runs = discover_runs(batch)
    if not runs:
        return False
    expectation = batch / "delivery_batch_manifest.json"
    if expectation.is_file():
        expected = {str(Path(p).resolve()) for p in read(expectation)["expected_runs"]}
        if expected != {str(p.resolve()) for p in runs}:
            return False
    for run in runs:
        state = read(run / "run_state.json")
        product = state.get("accepted_product") or {}
        output = Path(product.get("output_path", "/__missing_product__"))
        acceptance = run / "product_acceptance.json"
        if state.get("stage") != "accepted" or not output.is_file() or not acceptance.is_file():
            return False
        actual = hashlib.sha256(output.read_bytes()).hexdigest()
        report = read(acceptance)
        if (
            actual != product.get("candidate_sha256")
            or report.get("accepted") is not True
            or report.get("publication_eligible") is not True
            or int(report.get("required_scope_count", 0)) <= 0
            or report.get("checked_scope_count") != report.get("required_scope_count")
            or report.get("subject_candidate_sha256") != actual
        ):
            return False
    return True


def bundle_review(batch: Path, repo: Path) -> dict:
    files: dict[str, Path] = {}
    for run in discover_runs(batch):
        case = run.parent.name if run.name == "run" else run.name
        for name in (
            "run_state.json",
            "source_ir.json",
            "source_ledger.json",
            "compiled_rules.json",
            "inputs/semantic_deck.json",
            "model_rule_digest.md",
            "font_inventory.json",
            "template_profile.json",
            "profiles/base_template.pptx",
            "classification_and_transformation_ledger.json",
            "part_preservation_diff.json",
            "saved_authoring_rule_report.json",
            "required_visibility_report.json",
            "saved_frame_report.json",
            "delivery_patch_activation.json",
            "candidate_attempts.json",
            "legacy_deck_candidates.json",
            "comparison_coverage.json",
            "selection_report.json",
            "product_acceptance.json",
            "runner_transition_log.jsonl",
            "publication_manifest.json",
            "frame_report.json",
            "content_diff.json",
            "saved_geometry_report.json",
            "saved_measurement_report.json",
            "ontology_rule_coverage.json",
            "native_editability_report.json",
            "visual_review.json",
            "final/design_review.json",
            "gate_evidence.json",
        ):
            path = run / name
            if path.is_file():
                files[f"cases/{case}/{name}"] = path
        for pattern in (
            "render/*.png",
            "render/*.pdf",
            "render/*manifest*.json",
            "scenes/*/render/*manifest*.json",
            "scenes/*/*review.json",
            "scenes/*/*/migration_findings.json",
            "gate_reports/*.json",
            "scenes/*/candidate_evaluations.json",
            "scenes/*/runtime_counters.json",
            "render/render_*report.json",
        ):
            for path in run.glob(pattern):
                files[f"cases/{case}/{path.relative_to(run)}"] = path
        state = read(run / "run_state.json")
        source_input = Path(state.get("input_spec", {}).get("path") or "/__missing_input__")
        if source_input.is_file():
            files[f"cases/{case}/input/{source_input.name}"] = source_input
        product = state.get("accepted_product") or {}
        output = Path(product.get("output_path", "/__missing_product__"))
        if output.is_file():
            files[f"cases/{case}/{output.name}"] = output
        elif (run / "candidate.pptx").is_file():
            files[f"cases/{case}/unaccepted_candidate.pptx"] = run / "candidate.pptx"
        failure_signatures = set()
        successful_examples = 0
        for job in state.get("deck_candidates", []):
            if job.get("eligible"):
                # A gate archive must contain successful candidate bytes
                # and PNGs as well as the rejected diagnostic examples.
                if successful_examples >= 2:
                    continue
                successful_examples += 1
            else:
                signature = tuple(sorted(job.get("attempt", {}).get("finding_codes", [])))
                if signature in failure_signatures:
                    continue
                failure_signatures.add(signature)
            folder = run / "scenes" / job["deck_key"]
            for pattern in (
                "candidate.pptx",
                "candidate_evaluations.json",
                "runtime_counters.json",
                "content_diff.json",
                "part_preservation_diff.json",
                "saved_*report.json",
                "required_visibility_report.json",
                "native_editability_report.json",
                "render/*.png",
                "render/*.pdf",
                "render/*.json",
                "layout_attempts/*.json",
            ):
                for path in folder.glob(pattern):
                    if path.is_file():
                        files[f"cases/{case}/{path.relative_to(run)}"] = path
        for task in state.get("task_queue", []):
            if task.get("status") == "pending" or (
                task.get("status") == "consumed"
                and task.get("kind") in ("design_brief", "visual_concept_generation")
            ):
                path = packet_path(run, task)
                if path.is_file():
                    try:
                        rel = path.resolve().relative_to(run.resolve())
                    except ValueError as error:
                        raise ValueError(f"REQUEST_PACKET_OUTSIDE_RUN:{path}") from error
                    files[f"cases/{case}/{rel}"] = path
    progress = batch / "delivery_progress.json"
    write(progress, audit_progress(batch))
    files["delivery_progress.json"] = progress
    for name in ("delivery_batch_manifest.json", ".harness_dispatch.json"):
        if (batch / name).is_file():
            files[f"dispatcher/{name}"] = batch / name
    for path in (batch / "delivery_checks").rglob("*"):
        if path.is_file():
            files[f"checks/{path.relative_to(batch / 'delivery_checks')}"] = path
    for path in (batch / "harness_work").glob("*.json"):
        if path.name.startswith(("receipt-", "rejected-")):
            files[f"dispatcher/{path.name}"] = path
    for name in (
        "restyle_runner.py",
        "scene_contract.py",
        "scene_jobs.py",
        "response_validation.py",
        "design_frontier.py",
        "source_images.py",
        "owned_examples.py",
        "constraint_layout.py",
        "measured_placement.py",
        "candidate_jobs.py",
        "review_jobs.py",
        "text_flow.py",
        "frame_contract.py",
        "planning_bridge.py",
        "patch_activation.py",
        "composition.py",
        "layout.py",
        "measured_build.py",
        "native.py",
        "export_pipeline.py",
        "opc_adapter.py",
        "render.py",
        "models.py",
        "planner.py",
    ):
        path = repo / "src/slides_cli/gpn" / name
        if path.is_file():
            files[f"code/{name}"] = path
    for name in ("tools/harness_batch.py", "tools/layout/src/measure.js"):
        path = repo / name
        if path.is_file():
            files[f"code/{name}"] = path
    for path in (repo / "tests/gpn").glob("test_delivery_*.py"):
        files[f"code/tests/gpn/{path.name}"] = path
    # The earlier partial snapshot omitted unchanged core API modules,
    # making normal CLI imports impossible to reproduce outside the Mac.
    for subdir in ("src/slides_cli", "tests/gpn"):
        for path in (repo / subdir).rglob("*.py"):
            files[f"code/full/{path.relative_to(repo)}"] = path
    for name in (
        "pyproject.toml",
        "uv.lock",
        "tools/layout/package.json",
        "tools/layout/package-lock.json",
    ):
        if (repo / name).is_file():
            files[f"code/full/{name}"] = repo / name
    output = batch / "delivery_review.zip"
    manifest = {
        "schema_version": "delivery-review/v1",
        "product_complete": batch_complete(batch),
        "files": [
            {
                "path": name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "bytes": path.stat().st_size,
            }
            for name, path in sorted(files.items())
        ],
    }
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, path in sorted(files.items()):
            archive.write(path, name)
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return {
        "review_bundle": str(output),
        "files": len(files),
        "product_complete": manifest["product_complete"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-root", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("next")
    commands.add_parser("finish")
    commands.add_parser("advance")
    commands.add_parser("bundle")
    commands.add_parser("audit")
    register = commands.add_parser("register")
    register.add_argument("--expected-run", type=Path, action="append", required=True)
    activate = commands.add_parser("activate")
    activate.add_argument("--apply", action="store_true")
    response = commands.add_parser("submit")
    response.add_argument("response", type=Path)
    args = parser.parse_args()
    batch = args.batch_root.resolve()
    if not batch.is_dir():
        raise SystemExit("BATCH_DIRECTORY_MISSING")
    try:
        with (batch / ".harness_dispatch.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if args.action == "next":
                result = next_work(batch)
            elif args.action == "audit":
                result = audit_progress(batch)
            elif args.action == "submit":
                result = submit(batch, args.response, args.repo_root.resolve())
            elif args.action == "register":
                expected = [
                    (p if p.is_absolute() else batch / p).resolve() for p in args.expected_run
                ]
                if any(not (p / "run_state.json").is_file() for p in expected):
                    raise ValueError("EXPECTED_RUN_STATE_MISSING")
                path = batch / "delivery_batch_manifest.json"
                data = {
                    "schema_version": "delivery-batch/v1",
                    "expected_runs": [str(p) for p in expected],
                }
                if path.is_file() and read(path) != data:
                    raise ValueError("BATCH_EXPECTATION_CONFLICT:do not silently replace cases")
                write(path, data)
                result = {"status": "registered", "expected_runs": data["expected_runs"]}
            elif args.action == "activate":
                from slides_cli.gpn.patch_activation import activate_run

                # Preflight all cases before applying any migrations.
                preflight = [activate_run(p, apply=False) for p in discover_runs(batch)]
                result = {
                    "preflight": preflight,
                    "runs": [activate_run(p, apply=True) for p in discover_runs(batch)]
                    if args.apply
                    else [],
                }
            elif args.action == "bundle":
                result = bundle_review(batch, args.repo_root.resolve())
            elif args.action == "advance":
                result = {"runs": []}
                for run in discover_runs(batch):
                    proc = subprocess.run(
                        [sys.executable, "-m", "slides_cli.cli", "gpn", "resume", str(run)],
                        cwd=args.repo_root,
                        capture_output=True,
                        text=True,
                        timeout=1800,
                    )
                    result["runs"].append(
                        {
                            "run": str(run),
                            "exit_code": proc.returncode,
                            "stdout": proc.stdout,
                            "stderr": proc.stderr,
                        }
                    )
            else:
                result = {
                    "product_complete": batch_complete(batch),
                    "cases": [
                        {"run": str(p), "stage": read(p / "run_state.json")["stage"]}
                        for p in discover_runs(batch)
                    ],
                }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if args.action != "finish" or result["product_complete"] else 2
    except (ValueError, KeyError, StopIteration, OSError, subprocess.SubprocessError) as error:
        print(
            json.dumps(
                {"status": "dispatcher_error", "error": str(error), "product_complete": False},
                ensure_ascii=False,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
