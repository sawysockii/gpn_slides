"""Explicit, idempotent adoption of fixed authoring contracts in old runs.

Only run artifacts are migrated; source presentations, ontology and example
corpora are untouched. Old packets/responses remain immutable evidence.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .models import ModelTaskKind, ModelTaskResponse, SemanticDeck


def activation_signature() -> str:
    h = hashlib.sha256()
    for name in (
        "text_flow.py",
        "frame_contract.py",
        "scene_contract.py",
        "response_validation.py",
        "patch_activation.py",
        "layout.py",
        "composition.py",
        "restyle_runner.py",
    ):
        h.update((Path(__file__).parent / name).read_bytes())
    return h.hexdigest()


def activate_run(run: Path, *, apply: bool = False) -> dict[str, Any]:
    from . import restyle_runner as runner
    from .response_validation import validate_payload
    from .scene_contract import lower_scene_plan

    state = runner.load_state(run)
    signature = activation_signature()
    report_path = run / "delivery_patch_activation.json"
    if report_path.is_file():
        previous = json.loads(report_path.read_text())
        if previous.get("code_sha256") == signature and previous.get("applied"):
            return {
                "status": "already_active",
                "code_sha256": signature,
                "run_dir": str(run),
                "applied": False,
            }
    report: dict[str, Any] = {
        "status": "preflight",
        "run_dir": str(run),
        "code_sha256": signature,
        "applied": False,
        "plans": [],
        "superseded": [],
        "issued": [],
    }
    if state.stage == "accepted":
        report["status"] = "accepted_run_unchanged"
        return report
    semantic_path = run / "inputs/semantic_deck.json"
    if not semantic_path.is_file():
        report["status"] = "semantic_still_required"
        return report
    deck = SemanticDeck.model_validate_json(semantic_path.read_text())
    source = json.loads((run / "source_ir.json").read_text())
    code = signature[:16]
    for sid, cids in state.slide_candidates.items():
        slide = next(s for s in deck.slides if s.source_slide_id == sid)
        known = {o["id"] for s in source["slides"] if s["id"] == sid for o in s["objects"]}
        for cid in cids:
            key = f"{state.run_id}/{runner._safe_name(sid)}/{cid}"
            folder = run / "scenes" / runner._safe_name(sid) / cid
            authored = folder / "authored_plan.json"
            errors, lowered = [], None
            if authored.is_file():
                plan = json.loads(authored.read_text())
                # A dry run never creates a packet or mutates the queue.
                from .models import CandidateKey, ModelTask

                task = ModelTask(
                    request_id="activation-validation",
                    kind=ModelTaskKind.SCENE_GENERATION,
                    candidate_key=CandidateKey(
                        run_id=state.run_id, source_slide_id=sid, candidate_id=cid
                    ),
                )
                response = ModelTaskResponse(
                    request_id=task.request_id,
                    actual_model="activation-validator",
                    payload={"scene_plan": plan},
                )
                errors = validate_payload(run, task, response)
                if not errors:
                    lowered, errors = lower_scene_plan(plan, slide, known)
            report["plans"].append(
                {
                    "slide_id": sid,
                    "candidate_id": cid,
                    "action": "rebind" if lowered is not None and not errors else "request_scene",
                    "errors": errors,
                }
            )
            if not apply:
                continue
            path = folder / "plan.json"
            if path.is_file():
                backup = folder / "migration_history" / code / "plan.before.json"
                backup.parent.mkdir(parents=True, exist_ok=True)
                if backup.exists() and backup.read_bytes() != path.read_bytes():
                    raise ValueError(f"ACTIVATION_BACKUP_CONFLICT:{backup}")
                if not backup.exists():
                    backup.write_bytes(path.read_bytes())
                if lowered is None or errors:
                    path.unlink()
            active = [
                t
                for t in state.task_queue
                if t.status == "pending"
                and t.kind in (ModelTaskKind.SCENE_GENERATION, ModelTaskKind.SCENE_REPAIR)
                and t.candidate_key is not None
                and t.candidate_key.source_slide_id == sid
                and t.candidate_key.candidate_id == cid
            ]
            for task in active:
                task.status = "superseded"
                report["superseded"].append(task.request_id)
            if lowered is not None and not errors:
                runner._atomic_write(path, json.dumps(lowered, ensure_ascii=False, indent=2))
                state.candidate_states[key] = "scene_validated"
            else:
                task = runner._scene_generation_task(state, key)
                if task is None:
                    raise ValueError(f"ACTIVATION_SCENE_CONTEXT_MISSING:{key}")
                # Actual errors are separate evidence. The issued contract
                # already contains exact roles, semantics and source PNGs.
                runner._atomic_write(
                    folder / "migration_findings.json",
                    json.dumps(
                        {"errors": errors, "code_sha256": signature}, ensure_ascii=False, indent=2
                    ),
                )
                task.status = "pending"
                existing = next(
                    (t for t in state.task_queue if t.request_id == task.request_id), None
                )
                if existing is not None:
                    existing.status = "pending"
                    existing.input_fingerprint = task.input_fingerprint
                    existing.packet = task.packet
                    # A committed answer is not silently repurposed. This
                    # only reopens current-contract tasks never consumed.
                    if existing.request_id in state.consumed_request_ids:
                        raise ValueError("ACTIVATION_WOULD_REUSE_CONSUMED_ID")
                else:
                    runner._enqueue(state, task)
                state.candidate_states[key] = "scene_pending"
                report["issued"].append(task.request_id)
    if apply:
        # Re-lowered plans and quarantined rejections must not be overwritten
        # by historical consumed scenes on the next resume.
        processed = set(state.processed_scene_responses)
        processed.update(
            r.request_id
            for r in state.responses
            if runner._task_kind(state, r.request_id)
            in (ModelTaskKind.SCENE_GENERATION, ModelTaskKind.SCENE_REPAIR)
        )
        state.processed_scene_responses = sorted(processed)
        runner._ensure_volume_budget(state, [])
        state.stage = "build"
        state.terminal_reason = ""
        runner.save_state(state)
        report["applied"] = True
        report["status"] = "active"
        runner._atomic_write(report_path, json.dumps(report, ensure_ascii=False, indent=2))
    return report
