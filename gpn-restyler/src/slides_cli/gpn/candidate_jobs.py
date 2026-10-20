"""Independent, content-addressed native build jobs per slide/candidate."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any


def build_fingerprint(run_dir: Path, plan: Path) -> str:
    digest = hashlib.sha256()
    paths = [
        plan,
        *(
            run_dir / name
            for name in (
                "source_ir.json",
                "source_ledger.json",
                "compiled_rules.json",
                "template_profile.json",
                "profiles/base_template.pptx",
                "base_template.pptx",
                "asset_overrides.json",
                "font_inventory.json",
                "classification_and_transformation_ledger.json",
            )
        ),
    ]
    paths.extend(
        Path(__file__).parent / name
        for name in (
            "candidate_jobs.py",
            "restyle_runner.py",
            "planner.py",
            "scene_contract.py",
            "layout.py",
            "constraint_layout.py",
            "measured_placement.py",
            "composition.py",
            "text_flow.py",
            "frame_contract.py",
            "native.py",
            "measured_build.py",
            "visibility_scope.py",
            "export_pipeline.py",
            "opc_adapter.py",
        )
    )
    for path in paths:
        digest.update(path.name.encode())
        digest.update(path.read_bytes() if path.is_file() else b"missing")
    return digest.hexdigest()


def build_ready_candidates(state: Any, findings: list, *, builder: Any, config: Any) -> bool:
    """Build only committed plans, never choose a fallback for another slide."""
    run_dir = Path(state.run_dir)
    old = {str(d["deck_key"]): dict(d) for d in state.deck_candidates}
    records, attempts = [], []
    for sid, candidates in state.slide_candidates.items():
        safe_sid = sid.replace("/", "_").replace(":", "_")
        for candidate in candidates:
            plan = run_dir / "scenes" / safe_sid / candidate / "plan.json"
            if not plan.is_file():
                continue
            # Hash the full identity: source IDs and c1 are repeated across decks.
            job_id = hashlib.sha256(f"{sid}\0{candidate}".encode()).hexdigest()
            key = f"slide-{job_id}"
            fingerprint = build_fingerprint(run_dir, plan)
            previous = old.get(key, {})
            artifact = previous.get("candidate.pptx", "")
            artifact_ok = not artifact or (
                (run_dir / artifact).is_file()
                and hashlib.sha256((run_dir / artifact).read_bytes()).hexdigest()
                == previous.get("sha256")
            )
            if previous.get("build_fingerprint") == fingerprint and artifact_ok:
                records.append(previous)
                attempts.append(previous["attempt"])
                continue
            folder = run_dir / "scenes" / key
            if folder.is_dir() and any(folder.iterdir()):
                history_key = (
                    previous.get("build_fingerprint") or previous.get("sha256") or "legacy"
                )
                archive = run_dir / "candidate_history" / key / history_key
                if not archive.exists():
                    archive.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(folder, archive)
            attempt = builder(
                state, key, {sid: candidate}, config, focus_slide=sid, focus_candidate=candidate
            )
            result = attempt.model_dump(mode="json")
            record = {
                "deck_key": key,
                "scope_kind": "slide_candidate",
                "selection": {sid: candidate},
                "focus_slide": sid,
                "focus_candidate": candidate,
                "eligible": attempt.eligible,
                "build_fingerprint": fingerprint,
                "attempt": result,
            }
            if attempt.candidate_path:
                record.update(
                    {
                        "candidate.pptx": attempt.candidate_path,
                        "sha256": attempt.candidate_sha256,
                        "operation_status": attempt.operation_status,
                    }
                )
            counters_path = run_dir / "scenes" / key / "runtime_counters.json"
            if counters_path.is_file():
                counters = json.loads(counters_path.read_text())
                state.solves_used += int(counters.get("solver_calls", 0))
            if not attempt.eligible:
                findings.extend(attempt.findings)
            records.append(record)
            attempts.append(result)
    # Preserve historical composite artifacts as evidence, not candidates.
    historical = [d for d in state.deck_candidates if d.get("scope_kind") != "slide_candidate"]
    if historical and not (run_dir / "legacy_deck_candidates.json").exists():
        (run_dir / "legacy_deck_candidates.json").write_text(
            json.dumps(historical, ensure_ascii=False, indent=2)
        )
    state.deck_candidates = records
    (run_dir / "candidate_attempts.json").write_text(
        json.dumps(
            {"schema_version": "candidate-attempt/v1", "attempts": attempts},
            ensure_ascii=False,
            indent=2,
        )
    )
    return any(record.get("eligible", False) for record in records)
