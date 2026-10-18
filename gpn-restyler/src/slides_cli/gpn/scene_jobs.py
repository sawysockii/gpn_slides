"""Issue evidence-bound repairs without mutable packets or ID suffixes."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from .models import ModelTaskKind


def request_scene_repair(
    state: Any, sid: str, cid: str, evidence: list[Any], *, reason: str
) -> Any:
    from . import restyle_runner as runner

    key = f"{state.run_id}/{runner._safe_name(sid)}/{cid}"
    active = [
        t
        for t in state.task_queue
        if t.status == "pending"
        and t.kind in (ModelTaskKind.SCENE_GENERATION, ModelTaskKind.SCENE_REPAIR)
        and t.candidate_key is not None
        and t.candidate_key.source_slide_id == sid
        and t.candidate_key.candidate_id == cid
    ]
    if active:
        return None
    if not evidence:
        return None
    revision = state.candidate_revisions.get(key, 0)
    if revision >= state.budget.max_repairs_per_candidate:
        state.candidate_states[key] = "rejected"
        return None
    root = Path(state.run_dir)
    base = runner._scene_generation_task(state, key)
    if base is None:
        return None
    packet = json.loads((root / base.packet).read_text())
    packet["stage"] = "scene_repair"
    packet["repair_reason"] = reason
    packet["repair_evidence"] = evidence
    packet["attempt"] = revision + 1
    authored = root / "scenes" / runner._safe_name(sid) / cid / "authored_plan.json"
    if authored.is_file():
        packet["previous_scene"] = json.loads(authored.read_text())
    images = []
    for item in evidence:
        if isinstance(item, dict):
            images.extend(str(p) for p in item.get("required_images", []))
    raw = json.dumps(packet, sort_keys=True, ensure_ascii=False, indent=2)
    fingerprint = hashlib.sha256(raw.encode()).hexdigest()
    rid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"scene-repair:{key}:{fingerprint}"))
    path = root / "model_requests" / rid / "packet.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text() != raw:
        raise ValueError("IMMUTABLE_SCENE_PACKET_CONFLICT")
    path.write_text(raw)
    task = base.model_copy(
        update={
            "request_id": rid,
            "kind": ModelTaskKind.SCENE_REPAIR,
            "packet": str(path.relative_to(root)),
            "input_fingerprint": fingerprint,
            "required_images": list(dict.fromkeys(images)),
        }
    )
    state.candidate_revisions[key] = revision + 1
    state.candidate_states[key] = "scene_pending"
    runner._enqueue(state, task)
    runner._invalidate_downstream(state, key)
    runner.log_transition(state, "scene_repair_issued", f"{key}:{reason}:{rid}")
    return task


def repair_failed_candidates(state: Any) -> list[Any]:
    """Use each isolated candidate's own failure and current-hash reviews."""
    root = Path(state.run_dir)
    tasks = []
    for job in state.deck_candidates:
        if job.get("scope_kind") != "slide_candidate":
            continue
        evidence = []
        if not job.get("eligible"):
            evidence.append({"stage": "build", "attempt": job.get("attempt", {})})
        folder = root / "scenes" / job["deck_key"]
        for kind in ("visual_review", "design_review"):
            path = folder / f"{kind}.json"
            if not path.is_file():
                continue
            review = json.loads(path.read_text())
            if review.get("candidate_sha256") == job.get("sha256") and review.get("verdict") in (
                "repair",
                "reject",
            ):
                manifest_path = folder / "render/render_manifest.json"
                images = []
                if manifest_path.is_file():
                    manifest = json.loads(manifest_path.read_text())
                    images = [
                        str(manifest_path.parent / p["image"]) for p in manifest.get("pages", [])
                    ]
                evidence.append({"stage": kind, "review": review, "required_images": images})
        if not evidence:
            continue
        cause = hashlib.sha256(
            json.dumps(evidence, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        if job.get("repair_issued_for") == cause:
            continue
        for sid, cid in job.get("selection", {}).items():
            task = request_scene_repair(state, sid, cid, evidence, reason="candidate_failed")
            if task is not None:
                tasks.append(task)
                job["repair_issued_for"] = cause
    return tasks


def comparison_coverage(state: Any, eligible: list[str]) -> tuple[bool, dict]:
    """Two distinct rendered/reviewed variants, at least one eligible per slide.

    This is a search policy, not a corporate style constant. Invalid
    alternatives remain visible as rejected evidence, never selectable.
    """
    groups: dict[str, list[str]] = {sid: [] for sid in state.slide_candidates}
    signatures: dict[str, set[str]] = {sid: set() for sid in groups}
    root = Path(state.run_dir)
    accepted_slides = {
        sid
        for job in state.deck_candidates
        if job.get("deck_key") in eligible
        for sid in job.get("selection", {})
    }
    for job in state.deck_candidates:
        if job.get("scope_kind") != "slide_candidate" or not job.get("render_valid"):
            continue
        folder = root / "scenes" / job["deck_key"]
        reviews = [folder / f"{name}.json" for name in ("visual_review", "design_review")]
        if not all(p.is_file() for p in reviews):
            continue
        if any(
            json.loads(p.read_text()).get("candidate_sha256") != job.get("sha256") for p in reviews
        ):
            continue
        for sid, cid in job.get("selection", {}).items():
            path = root / "scenes" / sid.replace("/", "_").replace(":", "_") / cid / "concept.json"
            concept = json.loads(path.read_text()) if path.is_file() else {}
            # The model's declared topology is checked again during review.
            signature = {
                k: concept.get(k)
                for k in (
                    "group_topology",
                    "main_visual_encoding",
                    "reading_path",
                    "dominance",
                    "alignment_system",
                    "whitespace_structure",
                )
                if concept.get(k) is not None
            }
            if not signature:
                signature = concept
            signatures[sid].add(
                hashlib.sha256(
                    json.dumps(signature, sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest()
            )
            groups.setdefault(sid, []).append(cid)
    missing = {
        sid: {"eligible_variants": ids, "distinct_concepts": len(signatures[sid])}
        for sid, ids in groups.items()
        if len(ids) < 2 or len(signatures[sid]) < 2 or sid not in accepted_slides
    }
    return not missing, missing


def repair_final_reviews(state: Any) -> list[Any]:
    """Route rejected final pages back to their selected authored scenes."""
    root = Path(state.run_dir)
    path = root / "scenes/final/candidate.pptx"
    if not path.is_file():
        return []
    candidate_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    selection_path = root / "selection_report.json"
    if not selection_path.is_file():
        return []
    report = json.loads(selection_path.read_text())
    selection = {
        e["source_slide_id"]: e["candidate_key"]["candidate_id"]
        for e in report.get("selections", [])
    }
    evidence = []
    targets = set()
    for kind in ("visual_review", "design_review"):
        review_path = path.parent / f"{kind}.json"
        if not review_path.is_file():
            continue
        review = json.loads(review_path.read_text())
        if review.get("candidate_sha256") != candidate_sha or review.get("verdict") == "accept":
            continue
        for finding in review.get("findings", []):
            scope = finding.get("scope")
            if scope in selection:
                targets.add(scope)
        evidence.append(
            {
                "stage": f"final_{kind}",
                "review": review,
                "required_images": [str(p) for p in sorted((path.parent / "render").glob("*.png"))],
            }
        )
    if not evidence:
        return []
    # An unscoped composition issue affects the deck; it is never assigned
    # to an arbitrary first slide by a request-id substring.
    if not targets:
        targets = set(selection)
    tasks = []
    for sid in sorted(targets):
        task = request_scene_repair(state, sid, selection[sid], evidence, reason="final_review")
        if task is not None:
            tasks.append(task)
    return tasks
