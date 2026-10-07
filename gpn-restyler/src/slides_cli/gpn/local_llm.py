"""Model client for runtime model calls (Stage 3.5 §7).

Two providers behind one generation contract:

- ``local`` (LocalModelClient): HTTP client for a configured local
  OpenAI-compatible endpoint (e.g. LM Studio, Ollama, llama.cpp):
  capability probing (GET /v1/models), chat completions with optional JSON
  schema, redacted tracing, bounded transport retries, serialized calls.
  No cloud fallback, no automatic model selection beyond configured
  model_id.
- ``harness`` (HarnessModelClient): no network at all. The harness (the
  agent tool running this session) performs the LLM call with its current
  online model and writes the answer into the run directory using the JSON
  contract below; the planner consumes that file through the same
  ``generate()`` interface. Stage 3.5 amendment: the live endpoint is not
  connected yet, but request/response contracts, validation, counters and
  traces are the same.

Harness JSON contract (all paths inside ``run_dir``):

- Request artifact ``model_requests/<request_id>.json`` written by the
  client before "calling" the model::

      {"request_id", "purpose", "provider_requested", "model",
       "packet_hash", "schema_hash", "max_output_tokens", "temperature",
       "messages": [...], "response_schema": {...} | null}

- Response artifact ``model_responses/<request_id>.json`` written by the
  harness::

      {"request_id", "actual_model", "content_text", "finish_reason",
       "usage": {...} | null, "usage_available": bool, "latency_ms": int,
       "provider": "harness", "packet_hash": str, "schema_hash": str}

``content_text`` is the raw model answer (JSON text for structured plans).
Missing response file, empty content or ``finish_reason="length"`` surface
exactly like HTTP failures of the local client.

``packet_hash``/``schema_hash`` are echoed back by the harness; when present
they must equal the hashes of the request that is being answered, so a stale
answer written for an earlier packet or schema is refused instead of silently
becoming this run's plan.

While waiting for the answer the client polls a bounded number of seconds
(``harness_wait_seconds``) and reports progress on stderr; a re-run of the
same command consumes answers already present in ``model_responses/``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
DEFAULT_TIMEOUT = 600.0
DEFAULT_MAX_RETRIES = 1
DEFAULT_TEMPERATURE = 0.25
DEFAULT_MAX_OUTPUT_TOKENS = 4096
DEFAULT_CONTEXT_BUDGET_TOKENS = 12000
DEFAULT_HARNESS_WAIT_SECONDS = 900.0
HARNESS_POLL_INTERVAL_SECONDS = 0.25
HARNESS_PROGRESS_EVERY_SECONDS = 15.0

PROVIDERS = ("local", "harness")
HARNESS_DEFAULT_MODEL_ID = "harness-online-model"


@dataclass
class ResolvedModelConfig:
    """Resolved model configuration from config + environment."""

    base_url: str
    model_id: str
    provider: str = "local"
    temperature: float = DEFAULT_TEMPERATURE
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    timeout_seconds: float = DEFAULT_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES
    concurrency: int = 1
    use_json_schema: str = "auto"
    context_budget_tokens: int = DEFAULT_CONTEXT_BUDGET_TOKENS
    harness_wait_seconds: float = DEFAULT_HARNESS_WAIT_SECONDS
    api_key: str | None = None
    issues: list[str] = field(default_factory=list)


@dataclass
class ModelCapabilities:
    """Probed model capabilities."""

    reachable: bool = False
    http_status: int = 0
    provider: str = "local"
    listed_model_ids: list[str] = field(default_factory=list)
    selected_model_available: bool = False
    chat_completions: bool = False
    json_object: bool = False
    json_schema: bool = False
    seed: bool = False
    streaming: bool = False
    reasoning_separate: bool = False
    context_limit: int | None = None
    confirmed_by: str = ""
    unknown_fields: list[str] = field(default_factory=list)


@dataclass
class ModelGenerationResult:
    """Result of one model generation call."""

    request_id: str
    requested_model: str
    actual_model: str = ""
    content_text: str = ""
    reasoning_present: bool = False
    finish_reason: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    usage_available: bool = False
    latency_ms: int = 0
    http_status: int = 0
    response_hash: str = ""
    schema_mode: str = ""
    retry_count: int = 0
    error: str | None = None


@dataclass
class ModelRequest:
    """One generation request."""

    purpose: str
    messages: list[dict[str, str]]
    response_schema: dict[str, Any] | None = None
    model_config: ResolvedModelConfig | None = None
    request_id: str = ""
    packet_hash: str = ""
    schema_hash: str = ""
    max_output_tokens: int = 0
    temperature: float = 0.0


def resolve_model_config(
    config: Any, environment: dict[str, str] | None = None
) -> ResolvedModelConfig:
    """Resolve model configuration from AppConfig + environment.

    Reads base_url, model_id, and parameters from the configured [model]
    section. Does not start or modify any model server.
    """
    env = environment or {}
    model_section = getattr(config, "model", None)

    base_url = DEFAULT_BASE_URL
    model_id = ""
    provider = "local"
    temperature = DEFAULT_TEMPERATURE
    max_output_tokens = DEFAULT_MAX_OUTPUT_TOKENS
    timeout_seconds = DEFAULT_TIMEOUT
    max_retries = DEFAULT_MAX_RETRIES
    concurrency = 1
    use_json_schema = "auto"
    context_budget_tokens = DEFAULT_CONTEXT_BUDGET_TOKENS
    harness_wait_seconds = DEFAULT_HARNESS_WAIT_SECONDS
    api_key = None

    if model_section is not None:
        base_url = getattr(model_section, "base_url", base_url)
        model_id = getattr(model_section, "model_id", model_id) or ""
        provider = getattr(model_section, "provider", provider) or "local"
        temperature = getattr(model_section, "temperature", temperature)
        max_output_tokens = getattr(model_section, "max_output_tokens", max_output_tokens)
        timeout_seconds = getattr(model_section, "timeout_seconds", timeout_seconds)
        max_retries = getattr(model_section, "max_retries", max_retries)
        concurrency = getattr(model_section, "concurrency", concurrency)
        use_json_schema = getattr(model_section, "use_json_schema", use_json_schema)
        context_budget_tokens = getattr(
            model_section, "context_budget_tokens", context_budget_tokens
        )
        harness_wait_seconds = getattr(
            model_section, "harness_wait_seconds", harness_wait_seconds
        )

    if provider not in PROVIDERS:
        raise ValueError(f"unknown model provider {provider!r}; expected one of {PROVIDERS}")

    api_key = env.get("GPN_MODEL_API_KEY")

    issues: list[str] = []
    if provider == "local" and not model_id:
        issues.append(
            "model_id is empty; auto-selection will be attempted if exactly one model is available"
        )
    if provider == "harness":
        # Stage 3.5 amendment 1.0: the requested/actual model identity is the
        # harness' own online model, recorded honestly and never guessed from
        # the name of a local backend.
        if not model_id:
            model_id = env.get("GPN_HARNESS_MODEL_ID") or HARNESS_DEFAULT_MODEL_ID
            issues.append(
                "model_id empty; harness provider records the harness' own "
                f"online model as {model_id!r} (set GPN_HARNESS_MODEL_ID to "
                "the exact served model id)"
            )
        issues.append(
            "provider=harness: LLM answers are supplied by the harness via "
            "model_responses/<request_id>.json; no live endpoint is contacted"
        )

    return ResolvedModelConfig(
        base_url=base_url,
        model_id=model_id,
        provider=provider,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        concurrency=concurrency,
        use_json_schema=use_json_schema,
        context_budget_tokens=context_budget_tokens,
        harness_wait_seconds=harness_wait_seconds,
        api_key=api_key,
        issues=issues,
    )


class LocalModelClient:
    """HTTP client for local OpenAI-compatible chat completions."""

    def __init__(
        self,
        config: ResolvedModelConfig,
        *,
        run_dir: Path | None = None,
        transport: httpx.Client | None = None,
    ) -> None:
        self.config = config
        self.run_dir = Path(run_dir) if run_dir else None
        self._transport = transport
        self._lock = threading.Lock()
        self._generation_count = 0
        self._retry_count = 0

    def _get_transport(self) -> httpx.Client:
        if self._transport is not None:
            return self._transport
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        self._transport = httpx.Client(
            base_url=self.config.base_url,
            headers=headers,
            timeout=self.config.timeout_seconds,
        )
        return self._transport

    def _request_dir(self) -> Path | None:
        if self.run_dir is None:
            return None
        path = self.run_dir / "model_requests"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _response_dir(self) -> Path | None:
        if self.run_dir is None:
            return None
        path = self.run_dir / "model_responses"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_request_artifact(self, request_id: str, payload: dict[str, Any]) -> None:
        """Persist the redacted request under the shared JSON contract."""
        directory = self._request_dir()
        if directory is None:
            return
        redacted = _redact_payload(payload)
        (directory / f"{request_id}.json").write_text(
            json.dumps(redacted, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def save_response_artifact(self, request_id: str, payload: dict[str, Any]) -> None:
        """Persist the response under the shared JSON contract."""
        directory = self._response_dir()
        if directory is None:
            return
        (directory / f"{request_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _save_trace(self, request_id: str, payload: dict[str, Any], direction: str) -> None:
        """Save redacted request/response trace to run_dir (legacy path)."""
        if self.run_dir is None:
            return
        trace_dir = self.run_dir / "model_traces"
        trace_dir.mkdir(parents=True, exist_ok=True)
        redacted = _redact_payload(payload)
        path = trace_dir / f"{request_id}_{direction}.json"
        path.write_text(
            json.dumps(redacted, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def probe_capabilities(self) -> ModelCapabilities:
        """Probe endpoint capabilities via GET /v1/models."""
        caps = ModelCapabilities(provider=self.config.provider)
        try:
            client = self._get_transport()
            resp = client.get("/models")
            caps.http_status = resp.status_code
            if resp.status_code == 200:
                caps.reachable = True
                caps.chat_completions = True
                data = resp.json()
                models = data.get("data", [])
                caps.listed_model_ids = [m.get("id", "") for m in models if isinstance(m, dict)]
                if self.config.model_id:
                    caps.selected_model_available = self.config.model_id in caps.listed_model_ids
                caps.confirmed_by = "GET /v1/models"
            else:
                caps.unknown_fields.append(f"status_{resp.status_code}")
        except Exception as exc:
            caps.unknown_fields.append(f"probe_error:{type(exc).__name__}")
        return caps

    def generate(
        self,
        request: ModelRequest,
        *,
        cancel_token: threading.Event | None = None,
    ) -> ModelGenerationResult:
        """Execute one chat completions request.

        Serializes concurrent calls via threading.Lock. Saves redacted trace.
        Retries up to config.max_retries on transport errors only.
        """
        request_id = request.request_id or _new_request_id()
        cfg = request.model_config or self.config

        payload = {
            "model": cfg.model_id,
            "messages": request.messages,
            "temperature": request.temperature or cfg.temperature,
            "max_tokens": request.max_output_tokens or cfg.max_output_tokens,
        }
        if request.response_schema and cfg.use_json_schema != "never":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "layout_intent",
                    "strict": True,
                    "schema": request.response_schema,
                },
            }

        self.save_request_artifact(request_id, {
            "request_id": request_id,
            "purpose": request.purpose,
            "provider_requested": cfg.provider,
            "model": cfg.model_id,
            "packet_hash": request.packet_hash,
            "schema_hash": request.schema_hash,
            "max_output_tokens": request.max_output_tokens or cfg.max_output_tokens,
            "temperature": request.temperature or cfg.temperature,
            "messages": request.messages,
            "response_schema": request.response_schema,
        })
        self._save_trace(request_id, payload, "request")

        with self._lock:
            self._generation_count += 1
            retry_count = 0
            schema_fallback_used = False
            last_error = None

            for _ in range(cfg.max_retries + 1):
                if cancel_token and cancel_token.is_set():
                    return ModelGenerationResult(
                        request_id=request_id,
                        requested_model=cfg.model_id,
                        error="cancelled",
                    )
                start = time.monotonic()
                try:
                    client = self._get_transport()
                    resp = client.post("/chat/completions", json=payload)
                    latency_ms = int((time.monotonic() - start) * 1000)

                    if resp.status_code != 200:
                        last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                        if resp.status_code in (401, 403, 404):
                            break
                        if (
                            not schema_fallback_used
                            and request.response_schema is not None
                            and "response_format" in payload
                            and resp.status_code in (400, 422)
                            and _mentions_response_format(resp.text)
                        ):
                            # §7.3: exactly one bounded fallback to a plain
                            # JSON-in-prompt request. Auth/context/timeout
                            # errors never reach this branch.
                            payload.pop("response_format", None)
                            schema_fallback_used = True
                            continue
                        retry_count += 1
                        self._retry_count += 1
                        continue

                    data = resp.json()
                    choices = data.get("choices", [])
                    if not choices:
                        return ModelGenerationResult(
                            request_id=request_id,
                            requested_model=cfg.model_id,
                            http_status=resp.status_code,
                            latency_ms=latency_ms,
                            error="empty choices",
                        )

                    choice = choices[0]
                    message = choice.get("message", {})
                    content = message.get("content", "")
                    reasoning = message.get("reasoning") or message.get("reasoning_content", "")

                    if not content:
                        return ModelGenerationResult(
                            request_id=request_id,
                            requested_model=cfg.model_id,
                            http_status=resp.status_code,
                            latency_ms=latency_ms,
                            error="empty content",
                        )

                    if choice.get("finish_reason") == "length":
                        return ModelGenerationResult(
                            request_id=request_id,
                            requested_model=cfg.model_id,
                            content_text=content,
                            reasoning_present=bool(reasoning),
                            finish_reason="length",
                            http_status=resp.status_code,
                            latency_ms=latency_ms,
                            error="truncated: finish_reason=length",
                        )

                    usage = data.get("usage", {})
                    actual_model = data.get("model", cfg.model_id)
                    response_hash = hashlib.sha256(
                        json.dumps(data, ensure_ascii=False).encode()
                    ).hexdigest()

                    if schema_fallback_used:
                        schema_mode = "prompt_json_fallback"
                    elif request.response_schema:
                        schema_mode = "json_schema"
                    else:
                        schema_mode = "none"

                    self._save_trace(request_id, data, "response")
                    self.save_response_artifact(request_id, {
                        "request_id": request_id,
                        "provider": "local",
                        "requested_model": cfg.model_id,
                        "actual_model": actual_model,
                        "content_text": content,
                        "finish_reason": choice.get("finish_reason", ""),
                        "usage": usage,
                        "usage_available": bool(usage),
                        "latency_ms": latency_ms,
                        "http_status": resp.status_code,
                        "response_hash": response_hash,
                        "schema_mode": schema_mode,
                    })

                    return ModelGenerationResult(
                        request_id=request_id,
                        requested_model=cfg.model_id,
                        actual_model=actual_model,
                        content_text=content,
                        reasoning_present=bool(reasoning),
                        finish_reason=choice.get("finish_reason", ""),
                        usage=usage,
                        usage_available=bool(usage),
                        latency_ms=latency_ms,
                        http_status=resp.status_code,
                        response_hash=response_hash,
                        schema_mode=schema_mode,
                        retry_count=retry_count,
                    )

                except httpx.TimeoutException as exc:
                    last_error = f"timeout: {exc}"
                    retry_count += 1
                    self._retry_count += 1
                except httpx.TransportError as exc:
                    last_error = f"transport: {exc}"
                    retry_count += 1
                    self._retry_count += 1
                except Exception as exc:
                    last_error = f"error: {type(exc).__name__}: {exc}"
                    break

            return ModelGenerationResult(
                request_id=request_id,
                requested_model=cfg.model_id,
                error=last_error or "unknown error",
                retry_count=retry_count,
            )

    @property
    def generation_count(self) -> int:
        return self._generation_count

    @property
    def retry_count(self) -> int:
        return self._retry_count


class HarnessModelClient:
    """Generation client whose answers come from the harness JSON contract.

    No network. ``generate()`` writes the request artifact and then reads
    ``model_responses/<request_id>.json`` produced by the harness (its
    current online model). The interface mirrors LocalModelClient so the
    planner, counters and traces are unchanged.

    Honesty rules of amendment 1.0:

    - a generation request is counted only when a non-empty answer was
      actually consumed from the contract file;
    - a response whose ``request_id`` does not match the request is refused
      (it belongs to another packet/schema, not to this call);
    - ``finish_reason="length"`` is a truncated answer, never a valid plan;
    - ``reachable`` is not claimed: no endpoint was probed. Capabilities are
      reported as contract facts only, with the unconfirmed fields listed.
    """

    def __init__(
        self,
        config: ResolvedModelConfig,
        *,
        run_dir: Path | None = None,
        harness_model_id: str | None = None,
        wait_seconds: float | None = None,
    ) -> None:
        self.config = config
        self.run_dir = Path(run_dir) if run_dir else None
        self.harness_model_id = harness_model_id or os.environ.get(
            "GPN_HARNESS_MODEL_ID", HARNESS_DEFAULT_MODEL_ID
        )
        self.wait_seconds = (
            float(wait_seconds)
            if wait_seconds is not None
            else float(getattr(config, "harness_wait_seconds", DEFAULT_HARNESS_WAIT_SECONDS))
        )
        self._lock = threading.Lock()
        self._generation_count = 0
        self._retry_count = 0
        self._consumed_request_ids: list[str] = []

    @property
    def generation_count(self) -> int:
        return self._generation_count

    @property
    def retry_count(self) -> int:
        return self._retry_count

    @property
    def consumed_request_ids(self) -> list[str]:
        return list(self._consumed_request_ids)

    def probe_capabilities(self) -> ModelCapabilities:
        """Report the harness contract without pretending an endpoint exists."""
        return ModelCapabilities(
            reachable=False,
            provider="harness",
            chat_completions=False,
            json_schema=False,
            json_object=False,
            selected_model_available=True,
            listed_model_ids=[self.harness_model_id],
            context_limit=None,
            confirmed_by=(
                "harness JSON contract only: no live endpoint probed, so no "
                "http_status, chat-completions or structured-output support "
                "is claimed"
            ),
            unknown_fields=[
                "endpoint_not_probed",
                "context_limit_from_harness",
                "usage_from_harness",
                "latency_measured_by_harness",
            ],
        )

    def save_request_artifact(self, request_id: str, payload: dict[str, Any]) -> None:
        if self.run_dir is None:
            return
        directory = self.run_dir / "model_requests"
        directory.mkdir(parents=True, exist_ok=True)
        redacted = _redact_payload(payload)
        (directory / f"{request_id}.json").write_text(
            json.dumps(redacted, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _await_response_file(
        self,
        response_path: Path,
        request_id: str,
        cancel_token: threading.Event | None,
    ) -> bool:
        """Wait a bounded time for the harness answer; cancellable.

        Progress goes to stderr so stdout stays a single JSON result. The
        wait never loops hot: the response file is polled at a fixed interval.
        """
        if response_path.is_file():
            return True
        started = time.monotonic()
        last_progress = started
        while True:
            if cancel_token is not None and cancel_token.is_set():
                return False
            elapsed = time.monotonic() - started
            if elapsed >= self.wait_seconds:
                return False
            if time.monotonic() - last_progress >= HARNESS_PROGRESS_EVERY_SECONDS:
                last_progress = time.monotonic()
                print(
                    f"[harness] waiting for model answer {request_id} "
                    f"({int(elapsed)}s/{int(self.wait_seconds)}s): "
                    f"write it to {response_path}",
                    file=sys.stderr,
                    flush=True,
                )
            time.sleep(HARNESS_POLL_INTERVAL_SECONDS)
            if response_path.is_file():
                return True

    def generate(
        self,
        request: ModelRequest,
        *,
        cancel_token: threading.Event | None = None,
    ) -> ModelGenerationResult:
        request_id = request.request_id or _new_request_id()
        cfg = request.model_config or self.config

        if cancel_token and cancel_token.is_set():
            return ModelGenerationResult(
                request_id=request_id, requested_model=cfg.model_id, error="cancelled"
            )

        self.save_request_artifact(request_id, {
            "request_id": request_id,
            "purpose": request.purpose,
            "provider_requested": "harness",
            "model": cfg.model_id or self.harness_model_id,
            "packet_hash": request.packet_hash,
            "schema_hash": request.schema_hash,
            "max_output_tokens": request.max_output_tokens or cfg.max_output_tokens,
            "temperature": request.temperature or cfg.temperature,
            "messages": request.messages,
            "response_schema": request.response_schema,
        })

        if self.run_dir is None:
            return ModelGenerationResult(
                request_id=request_id,
                requested_model=cfg.model_id,
                error="harness provider requires run_dir to read model_responses",
            )

        response_path = self.run_dir / "model_responses" / f"{request_id}.json"
        if not self._await_response_file(response_path, request_id, cancel_token):
            if cancel_token is not None and cancel_token.is_set():
                return ModelGenerationResult(
                    request_id=request_id,
                    requested_model=cfg.model_id,
                    error="cancelled",
                )
            return ModelGenerationResult(
                request_id=request_id,
                requested_model=cfg.model_id,
                error=(
                    f"harness response missing: {response_path} "
                    f"(waited {int(self.wait_seconds)}s; write the model answer "
                    f"to model_responses/{request_id}.json)"
                ),
            )

        with self._lock:
            try:
                raw_bytes = response_path.read_bytes()
                data = json.loads(raw_bytes.decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                return ModelGenerationResult(
                    request_id=request_id,
                    requested_model=cfg.model_id,
                    error=f"harness response unreadable: {exc}",
                )
            if not isinstance(data, dict):
                return ModelGenerationResult(
                    request_id=request_id,
                    requested_model=cfg.model_id,
                    error="harness response must be a JSON object",
                )

            returned_id = str(data.get("request_id") or "")
            if returned_id and returned_id != request_id:
                return ModelGenerationResult(
                    request_id=request_id,
                    requested_model=cfg.model_id,
                    error=(
                        f"harness response request_id mismatch: file declares "
                        f"{returned_id!r}, this call is {request_id!r}"
                    ),
                )

            # A stale answer written for another packet/schema must never
            # become this run's plan: the harness echoes what it answered.
            for field, expected in (
                ("packet_hash", request.packet_hash),
                ("schema_hash", request.schema_hash),
            ):
                echoed = str(data.get(field) or "")
                if echoed and expected and echoed != expected:
                    return ModelGenerationResult(
                        request_id=request_id,
                        requested_model=cfg.model_id,
                        error=(
                            f"harness response {field} mismatch: answer declares "
                            f"{echoed[:16]}…, this request is {expected[:16]}…"
                        ),
                    )

            content = data.get("content_text", "")
        if isinstance(content, (dict, list)):
            content = json.dumps(content, ensure_ascii=False)
        if not isinstance(content, str) or not content.strip():
            return ModelGenerationResult(
                request_id=request_id,
                requested_model=cfg.model_id,
                error="harness response has empty content_text",
            )

        finish_reason = str(data.get("finish_reason", "stop")) or "stop"
        if finish_reason == "length":
            return ModelGenerationResult(
                request_id=request_id,
                requested_model=cfg.model_id,
                content_text=content,
                finish_reason="length",
                error="truncated: finish_reason=length",
            )

        usage = data.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        actual_model = str(data.get("actual_model") or self.harness_model_id)
        latency_ms = int(data.get("latency_ms") or 0)
        response_hash = hashlib.sha256(raw_bytes).hexdigest()
        with self._lock:
            self._generation_count += 1
            self._consumed_request_ids.append(request_id)
        return ModelGenerationResult(
            request_id=request_id,
            requested_model=cfg.model_id or self.harness_model_id,
            actual_model=actual_model,
            content_text=content,
            reasoning_present=bool(data.get("reasoning_present")),
            finish_reason=finish_reason,
            usage=usage,
            usage_available=bool(usage),
            latency_ms=latency_ms,
            http_status=int(data.get("http_status") or 0),
            response_hash=response_hash,
            schema_mode=str(data.get("schema_mode") or "harness_json_contract"),
        )


def make_model_client(
    config: ResolvedModelConfig,
    *,
    run_dir: Path | None = None,
    provider: str | None = None,
    transport: httpx.Client | None = None,
    wait_seconds: float | None = None,
) -> LocalModelClient | HarnessModelClient:
    """Build the configured generation client (local HTTP or harness files)."""
    chosen = (provider or config.provider or "local").lower()
    if chosen == "harness":
        return HarnessModelClient(config, run_dir=run_dir, wait_seconds=wait_seconds)
    if chosen != "local":
        raise ValueError(f"unknown model provider {chosen!r}")
    return LocalModelClient(config, run_dir=run_dir, transport=transport)


def _mentions_response_format(body: str) -> bool:
    lowered = body.lower()
    return any(
        marker in lowered
        for marker in ("response_format", "json_schema", "structured output", "structured_outputs")
    )


def _new_request_id() -> str:
    return f"req_{os.getpid()}_{threading.current_thread().ident}_{int(time.time() * 1000)}"


def _redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove sensitive fields from trace payload."""
    redacted = json.loads(json.dumps(payload))
    if "headers" in redacted:
        headers = redacted["headers"]
        for key in ("Authorization", "api-key", "X-API-Key"):
            if key in headers:
                headers[key] = "[REDACTED]"
    return redacted
