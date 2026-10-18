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

TOOL_VERSION = "0.3.0"
PROTOCOL_SCHEMA = "1.0"
# 0.3.0 — §5.1 coordinate contract, one documented space:
#   * fragments/line boxes stay OUTER-origin relative (they include the top
#     inset); the legacy `occupied_height_pt` keeps that exact meaning.
#   * the fit test compares those extents against the inner content area
#     expressed in the SAME origin: left=margins.l, top=margins.t,
#     right=box.w-margins.r, bottom=box.h-margins.b. The pre-0.3.0 code
#     compared against the outer border only, so a fragment could intrude
#     into the right/bottom insets and never be reported; subtracting
#     top/left a second time would double-charge them instead.
#   * new fields `inner_box_in_frame_pt`, `content_bounds_inner_pt`,
#     `logical_line_boxes_inner_pt`, `required_outer_height_pt` (both
#     vertical insets charged once) and `excess_inner_pt`.
#   * `line_count` counts logical lines, not the sum of each run's lines.
# 0.2.0: overflow decided in one coordinate space (mixed-origin comparison
# against the inner box charged the top padding twice).
CALIBRATION_ID = "uncalibrated-v2-inner-outer"


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


def persist_measurement_pair(persist_dir: str | Path,
                             key_hash: str,
                             request: MeasurementRequest,
                             response: dict[str, Any],
                             cache_hit: bool) -> None:
    """Write raw request/response evidence (§5.1, §12 bundle closure).

    Content-addressed by key_hash: ``requests/<hash>.json`` carries the
    canonical request plus its cache identity; ``responses/<hash>.json``
    carries the raw tool response plus a cache_hit marker. A glob over
    ``measurement/requests/*.json`` with zero files is an explicit
    incomplete bundle, never a pass.
    """
    try:
        base = Path(persist_dir)
        (base / "requests").mkdir(parents=True, exist_ok=True)
        (base / "responses").mkdir(parents=True, exist_ok=True)
        (base / "requests" / f"{key_hash}.json").write_text(
            json.dumps({"key_hash": key_hash,
                        "cache_parts": cache_key_parts(request),
                        "request": request.model_dump(mode="json")},
                       ensure_ascii=False, indent=1), encoding="utf-8")
        (base / "responses" / f"{key_hash}.json").write_text(
            json.dumps({"key_hash": key_hash, "cache_hit": cache_hit,
                        "response": response}, ensure_ascii=False),
            encoding="utf-8")
    except OSError:
        pass


def measure_components(
    request: MeasurementRequest,
    timeout_seconds: float = 120.0,
    *,
    project_root: Path | None = None,
    use_cache: bool = True,
    calibration_id: str = CALIBRATION_ID,
    debug_output_dir: str = "",
    persist_dir: str | Path | None = None,
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
            if persist_dir is not None:
                persist_measurement_pair(persist_dir, key_hash, request,
                                         hit["response"], True)
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
        # A face the browser did not load and an item the measurer could
        # not support are different facts with different fixes. Folding
        # them together reported an unsupportable geometry (a non-positive
        # inner box) as a font problem and failed whole slides for it
        # (§5.2 unsupported state is honest; §5.3/U04 is about faces).
        unverified_faces = sorted(
            {alias for item in response.items
             if item.confidence == "unsupported"
             and any(issue.startswith("exact face not loaded")
                     for issue in item.issues)
             for alias in item.font_evidence}
            | {alias for item in response.items for issue in item.issues
               if issue.startswith("face not verified")
               for alias in [issue.rsplit(": ", 1)[-1]]})
        unsupported_items = sorted(
            item.component_id for item in response.items
            if item.confidence != "exact_browser"
            and item.component_id not in {i.component_id for i in response.items
                                         if any(x.startswith("face not verified")
                                                for x in i.issues)}
            and not any(issue.startswith("exact face not loaded")
                        for issue in item.issues))
        # §5.1/§12: a refused call is still evidence. Persist the raw pair
        # before refusing, so the finding can be read back instead of
        # leaving the next engineer with only a summary string.
        if persist_dir is not None and (unverified_faces or unsupported_items):
            persist_measurement_pair(persist_dir, key_hash, request,
                                     response.model_dump(mode="json"), False)
        if unverified_faces and request.profile_kind == "production":
            # §5.3: a face the browser did not load means every number in
            # this response came from a substituted face. Reporting them as
            # measurements would be a fabricated default, so the whole call
            # fails with the exact aliases instead.
            raise MeasurementError(
                "MEASUREMENT_FACE_UNVERIFIED",
                "measurement ran with faces the browser did not load: "
                + ", ".join(unverified_faces),
                {"font_allowlist_roots":
                 list((request.engine_options or {}).get(
                     "allowlist_roots", []) or [])})
        if unsupported_items and request.profile_kind == "production":
            # The geometry offered was not measurable (e.g. a non-positive
            # inner box). That is a layout authoring defect, named per
            # component, not a rendering-trust defect.
            raise MeasurementError(
                "MEASUREMENT_ITEM_UNSUPPORTED",
                "measurement could not support "
                f"{len(unsupported_items)} component(s): "
                + ", ".join(unsupported_items[:6]),
                {"font_allowlist_roots":
                 list((request.engine_options or {}).get(
                     "allowlist_roots", []) or [])})
        out = response.model_dump(mode="json")
        if use_cache:
            store_cache(key_hash, parts, out, project_root)
        if persist_dir is not None:
            persist_measurement_pair(persist_dir, key_hash, request,
                                     out, False)
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
