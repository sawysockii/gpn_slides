"""Stage 5 §5.1 + §11.2: Python boundary of the Chromium measurement bridge.

``measure_components`` validates the request, checks the measurement cache,
spawns the single Node/Chromium tool via an args list (no shell), parses and
validates the protocol JSON from stdout-file, and cleans up in ``finally``.
Timeouts, missing binaries, crashes, invalid JSON and font failures become
typed issues — never a zero-size successful measurement.
"""

from __future__ import annotations

import hashlib
import json
import locale
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .models import MeasurementRequest, MeasurementResponse

TOOL_VERSION = "0.1.0"
PROTOCOL_SCHEMA = "1.0"
CALIBRATION_ID = "uncalibrated-v1"


def _code_root() -> Path:
    # src/slides_cli/gpn/measurement.py -> gpn-restyler
    return Path(__file__).resolve().parents[3]


def node_binary() -> str | None:
    """Locate the node binary (PATH only, no shell interpolation)."""
    return shutil.which("node")


def measure_tool_path() -> Path:
    return _code_root() / "tools" / "layout" / "src" / "measure.js"


def canonical_request_hash(request: MeasurementRequest) -> str:
    """Canonical SHA-256 of the validated request (cache + integrity key)."""
    canonical = json.dumps(request.model_dump(mode="json"), sort_keys=True,
                           ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def cache_key_parts(request: MeasurementRequest,
                    calibration_id: str = CALIBRATION_ID) -> dict[str, str]:
    """Full §11.2 cache identity (request content + environment)."""
    return {
        "canonical_request": canonical_request_hash(request),
        "tool_version": TOOL_VERSION,
        "protocol_schema": PROTOCOL_SCHEMA,
        "calibration_id": calibration_id,
        "platform": f"{sys.platform}-{platform.machine()}",
        "locale": locale.getlocale()[0] or "C",
        "python": platform.python_version(),
    }


def cache_key_hash(parts: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def _cache_dir(project_root: Path | None) -> Path:
    if project_root is None:
        project_root = _code_root().parent
    return Path(project_root) / "runs" / ".measurement-cache"


def lookup_cache(key_hash: str,
                 project_root: Path | None = None) -> dict[str, Any] | None:
    """Return the cached response dict on a verified hit, else None."""
    entry = _cache_dir(project_root) / key_hash
    manifest_path = entry / "manifest.json"
    response_path = entry / "response.json"
    try:
        if not manifest_path.is_file() or not response_path.is_file():
            return None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        response = json.loads(response_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(response, dict) or not response.get("items"):
        return None
    if manifest.get("key_hash") != key_hash:
        return None
    return {"manifest": manifest, "response": response, "cache_hit": True}


def store_cache(key_hash: str, parts: dict[str, str],
                response: dict[str, Any],
                project_root: Path | None = None) -> Path:
    entry = _cache_dir(project_root) / key_hash
    entry.mkdir(parents=True, exist_ok=True)
    (entry / "manifest.json").write_text(
        json.dumps({"key_hash": key_hash, "parts": parts},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    (entry / "response.json").write_text(
        json.dumps(response, ensure_ascii=False), encoding="utf-8")
    return entry


class MeasurementError(Exception):
    """Typed measurement failure (never a silent zero-size pass)."""

    def __init__(self, code: str, details: str,
                 evidence: dict[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {details}")
        self.code = code
        self.details = details
        self.evidence = evidence or {}


def _validate_response(request: MeasurementRequest,
                       response: MeasurementResponse,
                       expected_hash: str) -> list[str]:
    problems: list[str] = []
    if response.request_id != request.request_id:
        problems.append(
            f"request id mismatch: {response.request_id!r} != "
            f"{request.request_id!r}")
    if response.request_hash != expected_hash:
        problems.append("request hash mismatch: stale or foreign response")
    ids = [i.component_id for i in response.items]
    if len(set(ids)) != len(ids):
        problems.append("duplicate item ids in response")
    wanted = {c.id for c in request.components}
    if set(ids) != wanted:
        problems.append(
            f"item coverage mismatch: missing={sorted(wanted - set(ids))} "
            f"extra={sorted(set(ids) - wanted)}")
    return problems


def _downgrade_unverified_faces(response: MeasurementResponse) -> None:
    """Exact-browser confidence requires verified exact faces.

    When the tool reports a face load failure, every item using that alias
    silently fell back in-browser: downgrade to unsupported, never trust.
    """
    loaded = {f.alias for f in response.font_load.faces
              if f.load_status == "loaded"}
    for item in response.items:
        used = set(item.font_evidence)
        if used - loaded:
            item.confidence = "unsupported"
            item.issues.append(
                "exact face not loaded in browser; fallback rendering "
                "cannot confirm geometry")


def measure_components(
    request: MeasurementRequest,
    timeout_seconds: float = 120.0,
    *,
    project_root: Path | None = None,
    use_cache: bool = True,
    calibration_id: str = CALIBRATION_ID,
    debug_output_dir: str = "",
) -> dict[str, Any]:
    """Measure components in Chromium; return response dict + manifest."""
    if not request.components:
        raise MeasurementError("MEASUREMENT_EMPTY_REQUEST",
                               "no components to measure")
    request_hash = canonical_request_hash(request)
    parts = cache_key_parts(request, calibration_id)
    key_hash = cache_key_hash(parts)
    if use_cache:
        hit = lookup_cache(key_hash, project_root)
        if hit is not None:
            return hit

    node = node_binary()
    if node is None:
        raise MeasurementError("MEASUREMENT_NO_NODE",
                               "node binary not found on PATH")
    tool = measure_tool_path()
    if not tool.is_file():
        raise MeasurementError("MEASUREMENT_NO_TOOL",
                               f"measurement tool missing: {tool}")

    if debug_output_dir:
        request = request.model_copy(
            update={"debug_output_dir": debug_output_dir})
        # request hash covers debug dir? No: hash the content request.
        request_hash = canonical_request_hash(
            request.model_copy(update={"debug_output_dir": ""}))
    request = request.model_copy(update={
        "input_hashes": {**request.input_hashes,
                         "canonical_request": request_hash}})

    with tempfile.TemporaryDirectory(prefix="gpn-measure-") as tmp:
        req_path = Path(tmp) / "request.json"
        resp_path = Path(tmp) / "response.json"
        req_path.write_text(
            request.model_dump_json(), encoding="utf-8")
        try:
            proc = subprocess.run(
                [node, str(tool), str(req_path), str(resp_path)],
                capture_output=True, text=True, timeout=timeout_seconds,
                cwd=str(tool.parent.parent))
        except subprocess.TimeoutExpired as exc:
            raise MeasurementError(
                "MEASUREMENT_TIMEOUT",
                f"tool exceeded {timeout_seconds}s; process killed, "
                "no result trusted",
                {"stderr_tail": (exc.stderr or b"")[-2000:]}) from exc
        except OSError as exc:
            raise MeasurementError("MEASUREMENT_SPAWN_FAILED", str(exc)) from exc
        stderr = proc.stderr or ""
        if proc.returncode not in (0, 2):
            raise MeasurementError(
                "MEASUREMENT_TOOL_FAILED",
                f"tool exit={proc.returncode}; no result trusted",
                {"stderr_tail": stderr[-2000:]})
        try:
            raw = json.loads(resp_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MeasurementError(
                "MEASUREMENT_BAD_RESPONSE",
                f"response unreadable: {exc}",
                {"stderr_tail": stderr[-2000:]}) from exc
        try:
            response = MeasurementResponse.model_validate(raw)
        except Exception as exc:
            raise MeasurementError(
                "MEASUREMENT_SCHEMA_REJECTED",
                f"response failed schema validation: {exc}",
                {"stderr_tail": stderr[-2000:]}) from exc
        problems = _validate_response(request, response, request_hash)
        if problems:
            raise MeasurementError("MEASUREMENT_INTEGRITY_REJECTED",
                                   "; ".join(problems),
                                   {"stderr_tail": stderr[-2000:]})
        _downgrade_unverified_faces(response)
        out = response.model_dump(mode="json")
        if use_cache:
            store_cache(key_hash, parts, out, project_root)
        return {"manifest": {"key_hash": key_hash, "parts": parts,
                             "tool_exit": proc.returncode,
                             "stderr_tail": stderr[-2000:]},
                "response": out, "cache_hit": False}


# ---------------------------------------------------------------------------
# UTF-16 <-> codepoint mapping (DOM Range offsets are UTF-16 code units)
# ---------------------------------------------------------------------------

def utf16_units(text: str) -> list[int]:
    """UTF-16 code units per Python codepoint (surrogates count double)."""
    units: list[int] = []
    for ch in text:
        units.append(2 if ord(ch) > 0xFFFF else 1)
    return units


def codepoint_to_utf16(text: str, cp_index: int) -> int:
    """Map a Python codepoint index to a DOM UTF-16 offset."""
    return sum(utf16_units(text[:cp_index]))


def utf16_to_codepoint(text: str, utf16_index: int) -> int:
    """Map a DOM UTF-16 offset back to a Python codepoint index."""
    acc = 0
    for i, ch in enumerate(text):
        w = 2 if ord(ch) > 0xFFFF else 1
        if acc + w > utf16_index:
            return i
        acc += w
    return len(text)


def split_run_by_fragments(run_text: str,
                           fragments: list[dict[str, Any]]) -> list[str]:
    """Split run text by UTF-16 fragment offsets (line wrap never mutates)."""
    pieces: list[str] = []
    for frag in fragments:
        start = utf16_to_codepoint(run_text, frag["utf16_start"])
        end = utf16_to_codepoint(run_text, frag["utf16_end"])
        pieces.append(run_text[start:end])
    return pieces
