"""Current-byte native renders and immutable review packets."""

from __future__ import annotations

import hashlib
import json
import platform
import uuid
from pathlib import Path
from typing import Any

from .models import ModelTask, ModelTaskKind


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _cached_manifest(path: Path, candidate_sha: str, environment: str) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if data.get("candidate_sha256") != candidate_sha or data.get("environment") != environment:
        return {}
    if not data.get("pages"):
        return {}
    for page in data["pages"]:
        image = path.parent / page["image"]
        if not image.is_file() or sha(image) != page["sha256"]:
            return {}
    pdf = path.parent / data.get("pdf", "candidate.pdf")
    if not pdf.is_file() or sha(pdf) != data.get("pdf_sha256"):
        return {}
    return data


def render_candidates(state: Any, *, renderer: Any, typefaces: Any, bundle: Any) -> int:
    """Render only changed candidates; expected count is this job's scope."""
    root = Path(state.run_dir)
    inventory = root / "font_inventory.json"
    from .render import find_soffice, soffice_version

    binary = find_soffice()
    renderer_identity = soffice_version(binary) if binary else "unavailable"
    environment = hashlib.sha256(
        (Path(__file__).parent / "render.py").read_bytes()
        + (inventory.read_bytes() if inventory.is_file() else b"no-font-inventory")
        + (platform.platform() + renderer_identity).encode()
    ).hexdigest()
    ready = 0
    for job in state.deck_candidates:
        key = job["deck_key"]
        candidate = root / "scenes" / key / "candidate.pptx"
        if not candidate.is_file():
            continue
        current_sha = sha(candidate)
        for task in state.task_queue:
            if task.status != "pending" or task.kind not in (
                ModelTaskKind.VISUAL_REVIEW,
                ModelTaskKind.DESIGN_REVIEW,
            ):
                continue
            packet_path = root / task.packet
            if not packet_path.is_file():
                continue
            packet = json.loads(packet_path.read_text())
            if packet.get("deck_key") == key and packet.get("candidate_sha256") != current_sha:
                task.status = "superseded"
        target = candidate.parent / "render"
        manifest_path = target / "render_manifest.json"
        cached = _cached_manifest(manifest_path, sha(candidate), environment)
        if cached:
            job["render_manifest"] = str(manifest_path.relative_to(root))
            job["render_valid"] = cached.get("render_result") == "pass"
            ready += 1
            continue
        if state.renders_used >= state.budget.max_renders:
            continue
        target.mkdir(parents=True, exist_ok=True)
        probe = renderer(
            candidate,
            {
                "output_dir": str(target),
                "dpi": 96,
                "expected_faces": typefaces(candidate),
                "expected_page_count": len(job["selection"]),
            },
            bundle,
        )
        state.renders_used += 1
        pages = [Path(p) for p in probe.page_images]
        if not probe.rendered or not pages or any(not p.is_file() for p in pages):
            job["render_valid"] = False
            job["render_issues"] = list(probe.issues)
            continue
        manifest = {
            "candidate_sha256": sha(candidate),
            "environment": environment,
            "deck_key": key,
            "render_result": probe.result,
            "pages": [
                {"page": i + 1, "image": p.name, "sha256": sha(p), "bytes": p.stat().st_size}
                for i, p in enumerate(pages)
            ],
            "pdf": "candidate.pdf",
            "pdf_sha256": probe.pdf_sha256,
            "issues": list(probe.issues),
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        job["render_manifest"] = str(manifest_path.relative_to(root))
        job["candidate_sha256"] = sha(candidate)
        job["render_valid"] = probe.result == "pass"
        ready += 1
    return ready


def make_review_task(state: Any, job: dict, *, design: bool = False) -> ModelTask | None:
    root = Path(state.run_dir)
    manifest_path = root / str(job.get("render_manifest", ""))
    candidate = root / "scenes" / job["deck_key"] / "candidate.pptx"
    if not manifest_path.is_file() or not candidate.is_file():
        return None
    manifest = json.loads(manifest_path.read_text())
    candidate_sha = sha(candidate)
    if manifest.get("candidate_sha256") != candidate_sha or not manifest.get("pages"):
        return None
    images = [str(manifest_path.parent / page["image"]) for page in manifest["pages"]]
    if any(not Path(p).is_file() for p in images):
        return None
    stage = "design_review" if design else "visual_review"
    fields = ["deck_key", "candidate_sha256", "verdict", "dimensions" if design else "pages"]
    from .models import DESIGN_DIMENSION_FAMILIES

    payload = {
        "stage": stage,
        "required_dimensions": list(DESIGN_DIMENSION_FAMILIES) if design else [],
        "render_pages": manifest["pages"],
        "deck_key": job["deck_key"],
        "selection": job["selection"],
        "candidate_sha256": candidate_sha,
        "render_manifest": str(manifest_path.relative_to(root)),
        "native_render_result": manifest.get("render_result", "unknown"),
        "required_images": images,
        "required_fields": fields,
        "rule": "Open every actual PNG; critique message, hierarchy, relationships, "
        "grouping, readability and corporate rules. A text dump or clean fit "
        "alone is not acceptance. Return accept/repair/reject with node findings.",
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    fingerprint = hashlib.sha256(raw.encode()).hexdigest()
    rid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{stage}:{job['deck_key']}:{fingerprint}"))
    relative = f"model_requests/{rid}/packet.json"
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text() != raw:
        raise ValueError("IMMUTABLE_REVIEW_PACKET_CONFLICT")
    path.write_text(raw)
    return ModelTask(
        request_id=rid,
        kind=(ModelTaskKind.DESIGN_REVIEW if design else ModelTaskKind.VISUAL_REVIEW),
        input_fingerprint=fingerprint,
        response_schema_id=f"{'design' if design else 'visual'}-review/v1",
        packet=relative,
        required_images=images,
        expected_response_units=fields,
    )
