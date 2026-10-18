"""Stage 7 §8: durable orchestrator with real harness task execution.

One runner drives restyle and create through the same pipeline::

    ingest -> profile -> semantic -> design_briefs -> visual_concepts
    -> scene_generation -> build -> render -> visual_review
    -> design_review -> candidate_selection -> full_assembly
    -> final_checks -> accepted

The engine advances deterministic actions until it genuinely needs a model
response, writes the task packet, and returns ``awaiting_harness``. The
current OpenCode session reads the packet, opens every required PNG, writes
the typed response, and calls ``resume``. Responses are validated
(request id, input fingerprint, schema, single consumption); stale
responses are rejected with an actionable new task.

Per-candidate lifecycle::

    concept_pending -> concept_fidelity_validated -> scene_pending
    -> scene_validated -> styled -> measured -> resolved -> emitted
    -> saved_checked -> rendered -> visually/design_reviewed
    -> eligible -> selected

Any repair creates a new revision and recalculates affected downstream
evidence. Hard-ineligible candidates are never selected. Selection controls
the build: the assembled package IS the selected candidate's bytes.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from .models import (
    InputSpec,
    ModelTask,
    ModelTaskKind,
    ModelTaskResponse,
    ProductAcceptance,
    RepairFinding,
    SnapshotSet,
    StrictModel,
    TypedFinding,
)

RUN_STATE_SCHEMA = "run-state/v1"

RunStatus = Literal["running", "awaiting_harness", "accepted", "blocked", "budget_exhausted"]

RUN_STAGES: tuple[str, ...] = (
    "ingest",
    "profile",
    "semantic",
    "design_briefs",
    "visual_concepts",
    "scene_generation",
    "build",
    "render",
    "visual_review",
    "design_review",
    "candidate_selection",
    "full_assembly",
    "final_checks",
    "accepted",
    "blocked",
)

CANDIDATE_STATES: tuple[str, ...] = (
    "concept_pending",
    "concept_fidelity_validated",
    "scene_pending",
    "scene_validated",
    "styled",
    "measured",
    "resolved",
    "emitted",
    "saved_checked",
    "rendered",
    "visually_reviewed",
    "design_reviewed",
    "eligible",
    "selected",
    "rejected",
)


class SearchBudget(BaseModel):
    """Technical search policy (§8.4) — not a corporate norm."""

    model_config = StrictModel

    max_model_tasks: int = 60
    max_generations_per_slide: int = 4
    max_repairs_per_candidate: int = 3
    max_renders: int = 40
    max_local_solves: int = 200


class TaskRef(BaseModel):
    model_config = StrictModel
    request_id: str
    kind: str = ""
    packet: str = ""


class ActionRef(BaseModel):
    model_config = StrictModel
    action: str
    detail: str = ""
    artifact: str = ""


class CandidateRef(BaseModel):
    model_config = StrictModel
    candidate_key: str = ""
    state: str = ""
    revision: int = 0


class ProductRef(BaseModel):
    model_config = StrictModel
    output_path: str = ""
    candidate_sha256: str = ""


class RunState(BaseModel):
    """Durable run state (§8.1). Atomic JSON; resume is idempotent."""

    model_config = StrictModel

    schema_version: str = RUN_STATE_SCHEMA
    run_id: str
    run_dir: str
    input_spec: InputSpec
    snapshots: SnapshotSet = Field(default_factory=SnapshotSet)
    stage: str = "ingest"
    candidate_states: dict[str, str] = Field(default_factory=dict)
    candidate_revisions: dict[str, int] = Field(default_factory=dict)
    task_queue: list[ModelTask] = Field(default_factory=list)
    responses: list[ModelTaskResponse] = Field(default_factory=list)
    consumed_request_ids: list[str] = Field(default_factory=list)
    materialized_scene_responses: list[str] = Field(default_factory=list)
    # Transported but not (yet) successfully materialized scene responses.
    # Distinct from materialized_scene_responses: a malformed answer or a
    # missing context is processed once and recorded here, never counted
    # as an accepted plan (§5.1/§5.2).
    processed_scene_responses: list[str] = Field(default_factory=list)
    rejected_scene_reports: dict[str, dict[str, Any]] = Field(default_factory=dict)
    registered_concept_responses: list[str] = Field(default_factory=list)
    budget: SearchBudget = Field(default_factory=SearchBudget)
    tasks_used: int = 0
    renders_used: int = 0
    solves_used: int = 0
    artifact_manifest: dict[str, str] = Field(default_factory=dict)
    revision: int = 0
    accepted_product: ProductRef | None = None
    findings: list[TypedFinding] = Field(default_factory=list)
    terminal_reason: str = ""
    # slide_id -> ordered list of candidate ids (concepts) for that slide
    slide_candidates: dict[str, list[str]] = Field(default_factory=dict)
    # concept key (run_id/concept_id) -> slide_id
    concept_slide: dict[str, str] = Field(default_factory=dict)
    # deck candidates built: list of {deck_key, selection}
    deck_candidates: list[dict[str, Any]] = Field(default_factory=list)


class AdvanceResult(BaseModel):
    model_config = StrictModel

    status: RunStatus = "running"
    pending_tasks: list[TaskRef] = Field(default_factory=list)
    actions_executed: list[ActionRef] = Field(default_factory=list)
    current_candidates: list[CandidateRef] = Field(default_factory=list)
    accepted_product: ProductRef | None = None
    findings: list[TypedFinding] = Field(default_factory=list)


class PublishedProduct(BaseModel):
    model_config = StrictModel

    output_path: str
    candidate_sha256: str
    acceptance_sha256: str = ""


class RunnerError(ValueError):
    """Typed runner failure (invalid input, stale response, budget)."""


# ---------------------------------------------------------------------------
# persistence: atomic state + append-only transition log
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def save_state(state: RunState) -> Path:
    run_dir = Path(state.run_dir)
    path = run_dir / "run_state.json"
    _atomic_write(path, state.model_dump_json(indent=1))
    return path


def load_state(run_dir: Path) -> RunState:
    path = Path(run_dir) / "run_state.json"
    return RunState.model_validate_json(path.read_text(encoding="utf-8"))


def log_transition(state: RunState, event: str, detail: str = "") -> None:
    state.revision += 1
    record = {
        "revision": state.revision,
        "stage": state.stage,
        "event": event,
        "detail": detail,
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    log_path = Path(state.run_dir) / "runner_transition_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha_json(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


def _new_run_dir(project_root: Path, prefix: str = "stage7") -> Path:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d-%H%M%S")
    run_dir = project_root / "runs" / prefix / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def start_run(spec: InputSpec, config: Any, run_dir: Path | None = None) -> RunState:
    """Create durable state and advance deterministic ingest (§8.1)."""
    from .document_ingest import ingest_input  # noqa: PLC0415

    project_root = Path(spec.project_root)
    run_dir = run_dir or _new_run_dir(project_root)
    run_dir.mkdir(parents=True, exist_ok=True)
    run_id = Path(run_dir).name

    state = RunState(run_id=run_id, run_dir=str(run_dir), input_spec=spec)
    log_transition(state, "start_run", f"kind={spec.kind.value} provider={spec.provider.value}")

    # Deterministic ingest: source bytes, hashes, ontology snapshot.
    source = ingest_input(spec, run_dir)
    state.snapshots.input_sha256 = source.content_sha256
    state.artifact_manifest["source_manifest"] = "inputs/source_manifest.json"
    log_transition(state, "ingest_complete", f"content_sha256={source.content_sha256[:12]}…")

    budget = _budget_from_config(config)
    state.budget = budget
    save_state(state)
    return state


def _budget_from_config(config: Any) -> SearchBudget:
    layout = getattr(config, "layout", None)
    policy = getattr(config, "policy", None)
    runtime = getattr(config, "runtime", None)
    per_slide = int(getattr(layout, "candidate_count", 3) or 3)
    return SearchBudget(
        max_model_tasks=int(getattr(runtime, "max_model_requests", 1) or 1) * 60,
        max_generations_per_slide=max(3, per_slide + 1),
        max_repairs_per_candidate=int(getattr(policy, "max_model_repairs_per_slide", 2) or 2) + 1,
        max_renders=40,
        max_local_solves=int(getattr(layout, "max_local_layout_attempts", 6) or 6) * 30,
    )


# ---------------------------------------------------------------------------
# profile stage: import + corporate profiles into run_dir (deterministic)
# ---------------------------------------------------------------------------


def _profile_stage(state: RunState) -> tuple[bool, str]:
    """Produce source_ir/ledger/compiled_rules/template/fonts in run_dir."""
    from .assets import AssetStore  # noqa: PLC0415
    from .importer import (  # noqa: PLC0415
        build_import_support_report,
        import_deck,
        resolve_internal_links,
        write_import_artifacts,
    )
    from .package import build_manifest, read_package  # noqa: PLC0415
    from .pipeline import prepare_corporate_profiles  # noqa: PLC0415
    from .provenance import build_ledger  # noqa: PLC0415

    run_dir = Path(state.run_dir)
    spec = state.input_spec
    # Corporate context (rules/template/fonts/frame) is required by BOTH
    # modes (§3.1): create authoring grounds on the same compiled ontology
    # and verified examples as restyle. Only the source-PPT import is
    # restyle-specific. Profile first (the reference library uses its own
    # cache/profile namespace), then import the USER deck last so
    # source_ir.json is the deck being restyled (the known
    # import->profile->import ordering constraint).
    result = prepare_corporate_profiles(
        project_root=spec.project_root, config=_config_from_state(state), run_dir=run_dir
    )
    if result.status == "needs_assets":
        return False, "profile needs assets (fonts)"
    if result.status != "completed":
        # Fail closed (§3.1): needs_review/invalid_input are typed blocks,
        # not silent proceed-states. Briefs grounded on no ontology would
        # be ungrounded by construction. The blocker codes are the cause;
        # reporting only the status made every corpus problem look alike.
        causes = (
            "; ".join(f"{b.code}/{b.rule_id}: {b.details}" for b in result.blockers)
            or "no blocker detail reported"
        )
        return False, f"profile not completed: {result.status} ({causes})"
    inv = run_dir / "font_inventory.json"
    if not inv.is_file():
        return False, "font_inventory.json missing after profile"
    if spec.kind.value not in ("pptx", "ppt"):
        return True, ("create mode: corporate profiles ready, no source-PPT import")
    source = Path(spec.path) if spec.path else None
    if source is None or not source.is_file():
        return False, f"source not found: {source}"

    store = AssetStore(run_dir / "assets")

    deck, _ = import_deck(source, store)
    graph = read_package(source, store)
    links = resolve_internal_links(deck, graph)
    ledger = build_ledger(deck, decoration=[])
    support = build_import_support_report(deck, ledger, links, deck.import_issues)
    write_import_artifacts(
        run_dir=run_dir,
        deck=deck,
        ledger=ledger,
        package_manifest=build_manifest(graph),
        support=support,
        links=links,
    )
    (run_dir / "run_manifest.json").write_text(
        json.dumps(
            {
                "source_path": str(source.resolve()),
                "source_sha256": deck.source_sha256,
                "slides": len(deck.slides),
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )

    # Source render: the model must see the ACTUAL source layout, not only a
    # list of strings (§5.1). This is an input surface, not a verdict about
    # the candidate; a render failure is recorded, not fatal.
    try:
        from .render import render_source_deck  # noqa: PLC0415

        render_dir = run_dir / "source_render"
        probe = render_source_deck(source, render_dir, expected_page_count=len(deck.slides))
        (render_dir / "source_render_probe.json").write_text(
            json.dumps(probe.model_dump(mode="json"), ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        log_transition(
            state, "source_render_complete", f"result={probe.result} pages={probe.page_count}"
        )
    except Exception as exc:  # noqa: BLE001
        log_transition(state, "source_render_skipped", str(exc))

    return True, f"profile ok: {len(deck.slides)} slides, status={result.status}"


def _config_from_state(state: RunState) -> Any:
    """Best-effort config for profile/build (config-free when absent)."""
    from types import SimpleNamespace  # noqa: PLC0415

    project_root = Path(state.input_spec.project_root)
    cfg_path = project_root / "config" / "gpn.toml"
    if cfg_path.is_file():
        from .config import load_config  # noqa: PLC0415

        return load_config(cfg_path, project_root=project_root)
    return SimpleNamespace(
        project_root=project_root, config_path=None, model=SimpleNamespace(provider="harness")
    )


# ---------------------------------------------------------------------------
# response validation + consumption (§8.3)
# ---------------------------------------------------------------------------


def _pending_task(state: RunState, request_id: str) -> ModelTask | None:
    for task in state.task_queue:
        if task.request_id == request_id and task.status == "pending":
            return task
    return None


def _validate_response(state: RunState, task: ModelTask, response: ModelTaskResponse) -> list[str]:
    errors: list[str] = []
    if response.request_id in state.consumed_request_ids:
        errors.append("STALE_RESPONSE: request already consumed; a new task was issued")
    if response.input_fingerprint != task.input_fingerprint:
        errors.append(
            "STALE_RESPONSE: input fingerprint mismatch; task payload changed since issuance"
        )
    if response.schema_version_id != task.response_schema_id:
        errors.append(
            "SCHEMA_MISMATCH: response schema "
            f"{response.schema_version_id!r} != expected "
            f"{task.response_schema_id!r}"
        )
    if not response.actual_model:
        errors.append("MODEL_IDENTITY_MISSING: actual harness model required")
    missing = [u for u in task.expected_response_units if u not in response.payload]
    if missing:
        errors.append(f"RESPONSE_COVERAGE_INCOMPLETE: missing {missing}")
    return errors


def consume_response(state: RunState, response: ModelTaskResponse) -> list[str]:
    """Validate + consume exactly once. Returns rejection reasons."""
    task = _pending_task(state, response.request_id)
    if task is None:
        return [f"UNKNOWN_OR_CONSUMED_TASK: {response.request_id}"]
    errors = _validate_response(state, task, response)
    if not errors:
        from .response_validation import validate_payload

        errors = validate_payload(Path(state.run_dir), task, response)
    if errors:
        # Reject this response, keeping the immutable request answerable.
        log_transition(state, "response_rejected", f"{response.request_id}: {errors[0]}")
        save_state(state)
        return errors
    task.status = "consumed"
    state.consumed_request_ids.append(response.request_id)
    state.responses.append(response)
    state.tasks_used += 1
    log_transition(state, "response_consumed", f"{response.request_id} kind={task.kind.value}")
    save_state(state)
    return []


# ---------------------------------------------------------------------------
# advance
# ---------------------------------------------------------------------------


def _pending_refs(state: RunState) -> list[TaskRef]:
    return [
        TaskRef(request_id=t.request_id, kind=t.kind.value, packet=t.packet)
        for t in state.task_queue
        if t.status == "pending"
    ]


def _candidate_refs(state: RunState) -> list[CandidateRef]:
    return [
        CandidateRef(candidate_key=k, state=v, revision=state.candidate_revisions.get(k, 0))
        for k, v in state.candidate_states.items()
    ]


def _check_budget(state: RunState) -> str | None:
    if state.budget.max_model_tasks <= 0 or state.tasks_used > state.budget.max_model_tasks:
        return f"model task budget exhausted: {state.tasks_used}/{state.budget.max_model_tasks}"
    if state.renders_used > state.budget.max_renders:
        return f"render budget exhausted: {state.renders_used}/{state.budget.max_renders}"
    return None


def _ensure_volume_budget(
    state: RunState, findings: list[TypedFinding], n_slides: int | None = None
) -> None:
    count = n_slides if n_slides is not None else len(state.slide_candidates)
    if not count:
        return
    per_slide = max(
        state.budget.max_generations_per_slide,
        max((len(v) for v in state.slide_candidates.values()), default=0),
    )
    attempts = count * per_slide * (1 + state.budget.max_repairs_per_candidate)
    needs = {
        "max_model_tasks": 8 + 2 * count + 3 * attempts,
        "max_renders": attempts + 3,
        "max_local_solves": 6 * attempts,
    }
    for field, required in needs.items():
        if required > getattr(state.budget, field):
            previous = getattr(state.budget, field)
            setattr(state.budget, field, required)
            log_transition(state, "budget_extended", f"{field}:{previous}->{required}")
            findings.append(
                TypedFinding(
                    code="BUDGET_EXTENDED",
                    severity="info",
                    message=f"{field}:{previous}->{required}",
                )
            )


def advance_run(state: RunState, responses: list[ModelTaskResponse]) -> AdvanceResult:
    """Consume responses, run deterministic actions, schedule next tasks."""
    actions: list[ActionRef] = []
    findings: list[TypedFinding] = list(state.findings)

    for response in responses:
        rejection = consume_response(state, response)
        if rejection:
            findings.append(
                TypedFinding(
                    code="RESPONSE_REJECTED", severity="error", message="; ".join(rejection)
                )
            )
            # A stale/malformed response cannot change the issued question.
            # Upstream changes are handled explicitly by the retry scheduler.
            if _pending_task(state, response.request_id) is not None:
                findings.append(
                    TypedFinding(
                        code="RESPONSE_REPAIR_REQUIRED",
                        severity="error",
                        message=f"{response.request_id}: {rejection[0]} "
                        "(request remains pending; correct the response)",
                    )
                )

    # Volume scaling before the exhaustion verdict: a deck whose slide
    # count only became known mid-run must not wedge on a fixed cap with
    # consumed-but-unprocessed responses. Extension is upward-only,
    # logged, and keeps spent counters (§3.5).
    _ensure_volume_budget(state, findings)
    budget_hit = _check_budget(state)
    if budget_hit:
        state.stage = "blocked"
        state.terminal_reason = budget_hit
        log_transition(state, "budget_exhausted", budget_hit)
        save_state(state)
        return AdvanceResult(
            status="budget_exhausted", current_candidates=_candidate_refs(state), findings=findings
        )

    actions.extend(_advance_stage(state, findings))

    if state.stage == "accepted":
        return AdvanceResult(
            status="accepted",
            actions_executed=actions,
            current_candidates=_candidate_refs(state),
            accepted_product=state.accepted_product,
            findings=findings,
        )
    if state.stage == "blocked":
        return AdvanceResult(
            status="blocked",
            actions_executed=actions,
            current_candidates=_candidate_refs(state),
            findings=findings,
        )
    pending = _pending_refs(state)
    status: RunStatus = "awaiting_harness" if pending else "running"
    save_state(state)
    return AdvanceResult(
        status=status,
        pending_tasks=pending,
        actions_executed=actions,
        current_candidates=_candidate_refs(state),
        findings=findings,
    )


def _logical_task_id(task: ModelTask) -> str:
    """Stable logical work identity, independent of opaque request IDs.

    Request IDs are never parsed for slide/candidate provenance: the
    explicit CandidateKey (or packet file name for legacy brief/concept
    tasks) is authoritative (§3).
    """
    if task.candidate_key is not None:
        ck = task.candidate_key
        return f"{task.kind.value}:{ck.source_slide_id}:{ck.candidate_id}"
    # Brief/concept tasks carry no candidate key; their packet file name
    # is produced deterministically from the slide id.
    return f"{task.kind.value}:{task.packet}"


def _make_request_id(logical_task_id: str, input_fingerprint: str, attempt_no: int) -> str:
    """Flat opaque request id from (logical task, input basis, attempt).

    No suffix is appended to a previous request id: two calls with the
    same inputs produce the same id, and a real input/attempt change
    produces an unrelated id (§3).
    """
    basis = f"{logical_task_id}|{input_fingerprint}|{attempt_no}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "gpn/runner/request:" + basis))


def _reissue(state: RunState, old: ModelTask) -> ModelTask:
    logical = _logical_task_id(old)
    # Attempt number counts real model attempts for this logical work.
    # It is derived from consumed/consumed-and-materialized tasks, not
    # from the global event sequence.
    attempt_no = 1 + sum(1 for t in state.task_queue if _logical_task_id(t) == logical)
    new_id = _make_request_id(logical, old.input_fingerprint, attempt_no)
    # A reissue must not reuse a still-pending request for the same
    # logical subject (§4 step 2).
    if any(t.request_id == new_id for t in state.task_queue):
        new_id = _make_request_id(logical, old.input_fingerprint, attempt_no + 1)
    new = ModelTask(
        request_id=new_id,
        kind=old.kind,
        candidate_key=old.candidate_key,
        input_fingerprint=old.input_fingerprint,
        response_schema_id=old.response_schema_id,
        packet=_clone_packet_for_attempt(Path(state.run_dir), old, new_id),
        required_images=list(old.required_images),
        expected_response_units=list(old.expected_response_units),
    )
    state.task_queue.append(new)
    log_transition(state, "task_reissued", f"{old.request_id} -> {new.request_id}")
    return new


def _clone_packet_for_attempt(run_dir: Path, old: ModelTask, new_request_id: str) -> str:
    """Write a new immutable packet for a re-issued attempt.

    The original packet stays untouched; the clone carries the new
    request id and the same input basis, so historical requests remain
    verifiable (§3/§4).
    """
    src = run_dir / old.packet
    dst_dir = run_dir / "model_requests" / new_request_id
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / "packet.json"
    try:
        packet = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        packet = {}
    packet["request_id"] = new_request_id
    packet["attempt_of"] = old.request_id
    dst.write_text(json.dumps(packet, ensure_ascii=False, indent=1), encoding="utf-8")
    return str(dst.relative_to(run_dir))


def _enqueue(state: RunState, task: ModelTask) -> None:
    for existing in state.task_queue:
        if existing.request_id == task.request_id:
            if existing.input_fingerprint != task.input_fingerprint:
                raise RunnerError("REQUEST_ID_INPUT_CONFLICT")
            return
    if state.tasks_used + len(_pending_refs(state)) >= state.budget.max_model_tasks:
        raise RunnerError(
            f"cannot enqueue {task.request_id}: model task budget "
            f"{state.budget.max_model_tasks} reached"
        )
    state.task_queue.append(task)
    log_transition(state, "task_issued", f"{task.request_id} kind={task.kind.value}")


def _task_kind(state: RunState, request_id: str) -> ModelTaskKind | None:
    for task in state.task_queue:
        if task.request_id == request_id:
            return task.kind
    return None


def _responses_of_kind(state: RunState, kind: ModelTaskKind) -> list[ModelTaskResponse]:
    return [r for r in state.responses if _task_kind(state, r.request_id) == kind]


def _has_pending_kind(state: RunState, kind: ModelTaskKind) -> bool:
    return any(t.kind == kind and t.status == "pending" for t in state.task_queue)


def _applicable_rules(run_dir: Path) -> list[str]:
    compiled = run_dir / "inputs" / "compiled_rules.json"
    if not compiled.is_file():
        compiled = run_dir / "compiled_rules.json"
    if not compiled.is_file():
        return []
    try:
        data = json.loads(compiled.read_text(encoding="utf-8"))
        registry = data.get("rule_registry", {}) or {}
        rules = registry.get("rules", registry)
        if isinstance(rules, dict):
            return sorted(str(k) for k in rules)
        return sorted({str(r.get("id", "")) for r in rules if r.get("id")})
    except (OSError, ValueError):
        return []


# ---------------------------------------------------------------------------
# stage machine
# ---------------------------------------------------------------------------


def _materialize_semantic_deck(
    run_dir: Path,
    response,
    findings: list[TypedFinding],
    *,
    known_ids: set[str] | None = None,
    snapshot_path: Path | None = None,
    check_arrows: bool = True,
) -> Path | None:
    """Derive semantic deck + transform ledger from the harness response.

    §6.1/§6.2: the semantic projection is an immutable artifact of the model
    response (own hash), and per-object transformation decisions are the
    only authorization for omit/recompose. The engine writes both; it never
    infers decoration from a missing text run.

    PPT restyle authorizes against source-IR object ids; document create
    authorizes against document block/table/asset ids (no fake PPT shape
    ids) via ``known_ids``/``snapshot_path``.
    """
    from .models import SemanticDeck  # noqa: PLC0415

    payload = response.payload
    body = {
        k: v
        for k, v in payload.items()
        if k
        in ("schema_version", "source_manifest_sha256", "slides", "units", "relations", "evidence")
    }
    try:
        deck = SemanticDeck.model_validate(body)
    except ValidationError as exc:
        findings.append(
            TypedFinding(
                code="SEMANTIC_RESPONSE_INVALID",
                severity="error",
                message=f"semantic deck rejected: {exc}",
            )
        )
        return None
    path = run_dir / "inputs" / "semantic_deck.json"
    _atomic_write(path, deck.model_dump_json(indent=1))

    ir = _load_json(run_dir / "source_ir.json") or {}
    known: set[str] = set()
    for slide in ir.get("slides", []):
        known.update(str(o.get("id", "")) for o in slide.get("objects", []))
    if known_ids is not None:
        known = set(known_ids)
    # The omit authorization is bound to the *source snapshot* the build
    # re-reads: the hash of the immutable source IR bytes (or the source
    # manifest bytes for document create).
    snap_path = snapshot_path or (run_dir / "source_ir.json")
    try:
        snapshot = _sha_file(snap_path)
    except OSError:
        findings.append(
            TypedFinding(
                code="SEMANTIC_SNAPSHOT_MISSING",
                severity="error",
                message=f"source snapshot unreadable: {snap_path}",
            )
        )
        return None
    decisions: list[dict[str, Any]] = []
    for raw in payload.get("transformation_decisions", []) or []:
        if not isinstance(raw, dict):
            continue
        oid = str(raw.get("source_object_id", ""))
        action = str(raw.get("action", ""))
        if oid not in known:
            findings.append(
                TypedFinding(
                    code="TRANSFORM_DECISION_UNKNOWN_OBJECT",
                    severity="error",
                    message=f"decision for unknown source object {oid!r}",
                )
            )
            continue
        if action not in ("preserve", "recompose", "replace", "omit"):
            findings.append(
                TypedFinding(
                    code="TRANSFORM_DECISION_INVALID",
                    severity="error",
                    message=f"invalid action {action!r} for {oid}",
                )
            )
            continue
        rel_ids = [str(x) for x in raw.get("target_relation_ids", [])]
        if action == "omit" and not str(raw.get("reason", "")).strip():
            findings.append(
                TypedFinding(
                    code="TRANSFORM_DECISION_UNGROUNDED",
                    severity="error",
                    message=f"omit of {oid} without semantic reason",
                )
            )
            continue
        if (
            check_arrows
            and action in ("replace", "recompose")
            and not rel_ids
            and _object_is_arrow(ir, oid)
        ):
            findings.append(
                TypedFinding(
                    code="TRANSFORM_ARROW_WITHOUT_RELATION",
                    severity="error",
                    message=f"arrow {oid} re-expressed without target_relation_ids",
                )
            )
            continue
        decisions.append(
            {
                "source_object_id": oid,
                "source_unit_ids": [str(x) for x in raw.get("source_unit_ids", [])],
                "action": action,
                "target_node_ids": [str(x) for x in raw.get("target_node_ids", [])],
                "target_relation_ids": rel_ids,
                "authorization_ref": f"harness:{response.request_id}",
                "evidence_refs": [str(x) for x in raw.get("evidence_refs", [])],
                "reason": str(raw.get("reason", "")),
                "source_snapshot": str(raw.get("source_snapshot", snapshot)),
            }
        )
    ledger_path = run_dir / "classification_and_transformation_ledger.json"
    _atomic_write(
        ledger_path,
        json.dumps(
            {
                "source_snapshot": snapshot,
                "schema_version": "transform-ledger/v1",
                "decisions": decisions,
            },
            ensure_ascii=False,
            indent=1,
        ),
    )
    return path


def _object_is_arrow(ir: dict[str, Any], oid: str) -> bool:
    for slide in ir.get("slides", []):
        for obj in slide.get("objects", []):
            if str(obj.get("id", "")) != oid:
                continue
            line = (obj.get("payload") or {}).get("source_line") or {}
            return any(line.get(k) not in (None, "", "none") for k in ("begin_arrow", "end_arrow"))
    return False


def _materialize_narrative_deck(
    run_dir: Path, response, findings: list[TypedFinding]
) -> Path | None:
    """NARRATIVE_PLAN response -> common SemanticDeck (§3.2).

    The narrative payload plans document content (blocks/tables/assets),
    not PPT shapes. Refs stay document refs: no fake ``slide/shape`` ids
    are synthesized to satisfy the exporter signature. The downstream
    pipeline (briefs -> concepts -> scenes -> emit_scene) is identical.
    """
    from .document_ingest import (  # noqa: PLC0415
        SourceDocument,
        validate_semantic_response,
    )

    manifest_path = run_dir / "inputs" / "source_manifest.json"
    try:
        source = SourceDocument.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        findings.append(
            TypedFinding(
                code="NARRATIVE_SOURCE_MISSING",
                severity="error",
                message=f"source manifest unreadable: {exc}",
            )
        )
        return None
    errors = validate_semantic_response(response, source)
    if errors:
        findings.append(
            TypedFinding(
                code="NARRATIVE_RESPONSE_INVALID", severity="error", message="; ".join(errors[:4])
            )
        )
        return None

    manifest_sha = _sha_file(manifest_path)
    payload = response.payload
    block_index = {b.block_id: b for b in source.blocks}
    table_ids = {f"table-{i}" for i in range(len(source.tables))}
    asset_ids = {a.asset_id for a in source.assets}
    known = set(block_index) | table_ids | asset_ids

    def refs_for(ids: list[Any]) -> list[dict[str, Any]]:
        out = []
        for bid in ids:
            bid = str(bid)
            if bid in known:
                out.append(
                    {
                        "artifact_id": "source_manifest",
                        "relative_path": "inputs/source_manifest.json",
                        "sha256": manifest_sha,
                        "object_id": bid,
                    }
                )
        return out

    slides = []
    for i, raw in enumerate(payload.get("slides", []) or []):
        if not isinstance(raw, dict):
            continue
        units = []
        for u in raw.get("units", []) or []:
            if not isinstance(u, dict) or not str(u.get("id", "")):
                continue
            units.append(
                {
                    "id": str(u["id"]),
                    "kind": str(u.get("kind", "text")),
                    "content": {"text": str(u.get("text", ""))},
                    "source_refs": refs_for(list(u.get("source_block_ids", []) or [])),
                    "preservation": str(u.get("preservation", "required_semantic")),
                    "semantic_role": str(u.get("semantic_role", "")),
                }
            )
        relations = []
        for r in raw.get("relations", []) or []:
            if not isinstance(r, dict) or not str(r.get("id", "")):
                continue
            relations.append(
                {
                    "id": str(r["id"]),
                    "kind": str(r.get("kind", "relation")),
                    "from_unit_ids": [str(x) for x in r.get("from_unit_ids", []) or []],
                    "to_unit_ids": [str(x) for x in r.get("to_unit_ids", []) or []],
                    "directed": r.get("directed"),
                    "preservation": str(r.get("preservation", "required")),
                }
            )
        slides.append(
            {
                "source_slide_id": str(raw.get("source_slide_id") or f"doc-slide-{i + 1}"),
                "title": str(raw.get("title", "")),
                "units": units,
                "relations": relations,
                "hidden": False,
            }
        )
    deck_like = {
        "schema_version": "semantic-deck/v1",
        "source_manifest_sha256": source.content_sha256,
        "slides": slides,
        "transformation_decisions": payload.get("transformation_decisions", []),
    }
    adapted = ModelTaskResponseShim(response, deck_like)
    return _materialize_semantic_deck(
        run_dir, adapted, findings, known_ids=known, snapshot_path=manifest_path, check_arrows=False
    )


class ModelTaskResponseShim:
    """Minimal adapter: same request_id/payload protocol, remapped payload."""

    def __init__(self, response: Any, payload: dict[str, Any]) -> None:
        self.request_id = response.request_id
        self.payload = payload


def _advance_stage(state: RunState, findings: list[TypedFinding]) -> list[ActionRef]:
    """Deterministic stage machine. Returns executed actions."""
    from .design import (  # noqa: PLC0415
        build_slide_design_brief,
        generate_visual_concepts,
    )
    from .document_ingest import semantic_task_for  # noqa: PLC0415

    actions: list[ActionRef] = []
    run_dir = Path(state.run_dir)
    if state.stage not in (
        "ingest",
        "semantic",
        "design_briefs",
        "visual_concepts",
        "scene_generation",
        "build",
        "accepted",
    ) and _consume_scene_responses(state, findings, run_dir):
        state.stage = "build"

    if state.stage == "blocked" and "retry budget" in (state.terminal_reason or ""):
        # Consumed-but-unmaterialized scene revisions park the run as
        # blocked with work still actionable: the retry tasks were already
        # issued (budget spent at issue time), so materializing and
        # rebuilding costs nothing new. A repeated failure re-blocks
        # honestly since no unprocessed responses remain.
        unprocessed = [
            r
            for r in _responses_of_kind(state, ModelTaskKind.SCENE_GENERATION)
            if r.request_id not in state.materialized_scene_responses
        ]
        if unprocessed:
            state.stage = "build"
            state.terminal_reason = ""
            log_transition(
                state,
                "unblocked_for_materialization",
                f"{len(unprocessed)} consumed scene revisions await "
                "materialization; re-entering build",
            )
            findings.append(
                TypedFinding(
                    code="UNBLOCKED_FOR_MATERIALIZATION",
                    severity="info",
                    message=f"{len(unprocessed)} scene revisions to materialize",
                )
            )

    if state.stage == "ingest":
        ok, detail = _profile_stage(state)
        if ok:
            actions.append(ActionRef(action="profile_complete", detail=detail))
            # fall through: issue the semantic task in the same advance
            task = semantic_task_for(state.input_spec, run_dir, state.snapshots.input_sha256)
            _enqueue(state, task)
            state.stage = "semantic"
            actions.append(ActionRef(action="issue_semantic_task", artifact=task.packet))
        else:
            state.stage = "blocked"
            state.terminal_reason = detail
            findings.append(TypedFinding(code="PROFILE_FAILED", severity="error", message=detail))

    elif state.stage == "semantic":
        # Dispatch on the ACTUAL task kind (§3.2): PPT restyle consumes
        # SEMANTIC_ANALYSIS, document/prompt create consumes NARRATIVE_PLAN.
        # Either path materializes the common SemanticDeck contract.
        done_sem = [
            r
            for r in state.responses
            if _task_kind(state, r.request_id) == ModelTaskKind.SEMANTIC_ANALYSIS
        ]
        done_nar = [
            r
            for r in state.responses
            if _task_kind(state, r.request_id) == ModelTaskKind.NARRATIVE_PLAN
        ]
        done = done_sem + done_nar
        if done:
            deck_path = run_dir / "inputs" / "semantic_deck.json"
            if not deck_path.is_file():
                if done_nar and not done_sem:
                    deck_path = _materialize_narrative_deck(run_dir, done_nar[-1], findings)
                else:
                    deck_path = _materialize_semantic_deck(run_dir, done_sem[-1], findings)
            if deck_path is not None and deck_path.is_file():
                from .models import SemanticDeck  # noqa: PLC0415

                deck = SemanticDeck.model_validate_json(deck_path.read_text(encoding="utf-8"))
                state.snapshots.semantic_sha256 = _sha_file(deck_path)
                rules = _applicable_rules(run_dir)
                _ensure_volume_budget(state, findings, n_slides=len(deck.slides))
                for slide in deck.slides:
                    brief_task = build_slide_design_brief(
                        slide, rules, run_dir=run_dir, request_id=f"T-brief-{slide.source_slide_id}"
                    )
                    _enqueue(state, brief_task)
                # Slide count is known only now: scale the technical
                # budget before the brief/concept/scene fan-out (§3.5).
                _ensure_volume_budget(state, findings, n_slides=len(deck.slides))
                state.stage = "design_briefs"
                actions.append(
                    ActionRef(action="issue_design_briefs", detail=f"{len(deck.slides)} slides")
                )

    elif state.stage == "design_briefs":
        briefs = _responses_of_kind(state, ModelTaskKind.DESIGN_BRIEF)
        if briefs and not _has_pending_kind(state, ModelTaskKind.DESIGN_BRIEF):
            from .models import SlideDesignBrief  # noqa: PLC0415

            for response in briefs:
                # §3.2: a malformed model response becomes a typed finding
                # and a repair task, never a hard error that wedges the run
                # after the response has already been consumed.
                try:
                    brief = SlideDesignBrief.model_validate(response.payload)
                except ValidationError as exc:
                    findings.append(
                        TypedFinding(
                            code="DESIGN_BRIEF_INVALID",
                            severity="error",
                            message=f"{response.request_id}: {exc.errors()[0]['msg']}"
                            f" at {exc.errors()[0]['loc']}",
                        )
                    )
                    old_task = next(
                        (t for t in state.task_queue if t.request_id == response.request_id), None
                    )
                    _enqueue(state, _design_brief_retry(state, old_task, response))
                    continue
                brief_dir = run_dir / "design_briefs"
                brief_dir.mkdir(parents=True, exist_ok=True)
                safe_sid = _safe_name(brief.slide_id)
                (brief_dir / f"{safe_sid}.json").write_text(
                    brief.model_dump_json(indent=1), encoding="utf-8"
                )
                task = generate_visual_concepts(
                    brief,
                    approved_examples=[],
                    supported_primitives=[
                        "process",
                        "structure",
                        "comparison",
                        "table",
                        "data",
                        "typographic",
                        "evidence",
                    ],
                    run_dir=run_dir,
                    request_id=f"T-concepts-{brief.slide_id}",
                )
                _enqueue(state, task)
            state.stage = "visual_concepts"
            actions.append(ActionRef(action="issue_concept_tasks", detail=f"{len(briefs)} briefs"))

    elif state.stage == "visual_concepts":
        concepts = [
            r
            for r in _responses_of_kind(state, ModelTaskKind.VISUAL_CONCEPT_GENERATION)
            if r.request_id not in state.registered_concept_responses
        ]
        if concepts and not _has_pending_kind(state, ModelTaskKind.VISUAL_CONCEPT_GENERATION):
            from .design import validate_concept_fidelity  # noqa: PLC0415
            from .models import (  # noqa: PLC0415
                SemanticDeck,
                SlideDesignBrief,
                VisualConcept,
            )

            # map concept task request_id -> slide_id for provenance
            task_slide = {}
            for t in state.task_queue:
                if t.kind != ModelTaskKind.VISUAL_CONCEPT_GENERATION:
                    continue
                # Provenance comes from the immutable task packet's
                # design_brief.slide_id, never from parsing the opaque
                # request id (§3/§5.2).
                sid = ""
                packet_path = run_dir / t.packet
                try:
                    packet = json.loads(packet_path.read_text(encoding="utf-8"))
                    sid = str(packet.get("design_brief", {}).get("slide_id", ""))
                except (OSError, ValueError):
                    sid = ""
                task_slide[t.request_id] = sid
            for response in concepts:
                payload = response.payload
                sid = task_slide.get(response.request_id, "")
                items = payload.get("concepts", [payload])
                for item in items:
                    try:
                        concept = VisualConcept.model_validate(item)
                    except ValueError as exc:
                        findings.append(
                            TypedFinding(
                                code="CONCEPT_INVALID",
                                severity="error",
                                message=f"invalid concept: {exc}",
                            )
                        )
                        continue
                    # Unique key per slide+concept (plain c1/c2 collide
                    # across slides).
                    safe_sid = _safe_name(sid) if sid else "unknown"
                    key = f"{state.run_id}/{safe_sid}/{concept.concept_id}"
                    state.candidate_states[key] = "concept_pending"
                    state.candidate_revisions[key] = 0
                    # persist the validated concept for scene authoring,
                    # fidelity checks and design review provenance
                    cdir = run_dir / "scenes" / safe_sid / concept.concept_id
                    cdir.mkdir(parents=True, exist_ok=True)
                    (cdir / "concept.json").write_text(
                        concept.model_dump_json(indent=1), encoding="utf-8"
                    )
                    # deterministic fidelity gate (§10.4.6): required
                    # units/relations mapped, no invented causality/data
                    if sid:
                        try:
                            deck = SemanticDeck.model_validate_json(
                                (run_dir / "inputs" / "semantic_deck.json").read_text(
                                    encoding="utf-8"
                                )
                            )
                            sem_slide = next(s for s in deck.slides if s.source_slide_id == sid)
                            brief = SlideDesignBrief.model_validate_json(
                                (run_dir / "design_briefs" / f"{safe_sid}.json").read_text(
                                    encoding="utf-8"
                                )
                            )
                            check = validate_concept_fidelity(
                                concept, brief, sem_slide, _applicable_rules(run_dir)
                            )
                            (cdir / "fidelity.json").write_text(
                                check.model_dump_json(indent=1), encoding="utf-8"
                            )
                            if not check.valid:
                                state.candidate_states[key] = "concept_pending"
                                for f in check.fidelity_findings:
                                    findings.append(f)
                        except (OSError, ValueError, StopIteration):
                            pass
                    if sid:
                        state.concept_slide[key] = sid
                        state.slide_candidates.setdefault(sid, [])
                        if concept.concept_id not in state.slide_candidates[sid]:
                            state.slide_candidates[sid].append(concept.concept_id)
            for response in concepts:
                state.registered_concept_responses.append(response.request_id)
            # schedule scene_generation per concept, exactly once per
            # logical subject (§4 step 2).
            for key in list(state.candidate_states.items()):
                sid = _slide_id_for_key(state, key[0])
                concept_id = key[0].split("/")[-1]
                already = any(
                    t.kind in (ModelTaskKind.SCENE_GENERATION, ModelTaskKind.SCENE_REPAIR)
                    and t.candidate_key is not None
                    and _safe_name(t.candidate_key.source_slide_id) == _safe_name(sid)
                    and t.candidate_key.candidate_id == concept_id
                    for t in state.task_queue
                )
                if already:
                    continue
                task = _scene_generation_task(state, key[0])
                if task is not None:
                    _enqueue(state, task)
            state.stage = "scene_generation"
            actions.append(
                ActionRef(
                    action="register_concepts", detail=f"{len(state.candidate_states)} candidates"
                )
            )

    elif state.stage == "scene_generation":
        # Process every available scene response on every resume; a
        # global "wait for all scenes" barrier lets one slow candidate
        # starve an already-accepted scene (§6).
        written = _consume_scene_responses(state, findings, run_dir)
        if written:
            state.stage = "build"
            actions.append(ActionRef(action="scene_plans_written", detail=f"{written} plans"))

    if state.stage == "build":
        # Retry revisions arrive while the run sits in build: materialize
        # them before rebuilding, in the same advance.
        new_plans = _consume_scene_responses(state, findings, run_dir)
        if new_plans:
            actions.append(
                ActionRef(
                    action="scene_plans_updated", detail=f"{new_plans} revised plans materialized"
                )
            )
        built = _build_all_deck_candidates(state, findings)
        # A failed candidate may still be rendered for diagnosis (§7.3);
        # it never becomes eligible. Without any candidate bytes at all the
        # stage is re-entered only through an explicit repair, not in a
        # tight rebuild loop.
        produced = [d for d in state.deck_candidates if d.get("candidate.pptx")]
        if built:
            state.stage = "render"
            actions.append(
                ActionRef(
                    action="build_complete",
                    detail=f"{len(state.deck_candidates)} deck candidates, {built} eligible",
                )
            )
        elif produced:
            # Diagnostic bytes only, nothing eligible: render them for
            # review-driven diagnosis AND redesign in parallel. Reviews
            # without an eligible deck cannot select, so the run must
            # also get new scene revisions here, not only pictures.
            state.stage = "render"
            actions.append(
                ActionRef(
                    action="build_diagnostic_only",
                    detail=f"{len(produced)} diagnostic decks, 0 eligible; "
                    "rendering for review while redesigning",
                )
            )
            _ensure_volume_budget(state, findings)
            retries = _retry_unresolved_scenes(state, findings)
            if retries:
                actions.append(
                    ActionRef(
                        action="schedule_scene_retries",
                        detail=f"{len(retries)} scene tasks with layout evidence",
                    )
                )
        else:
            # No deck produced at all: every slide still needs a measured
            # design. Re-issue scene tasks with the measured layout
            # evidence attached (§3.5: measured infeasibility is a repair
            # cause with a next step, not a terminal state).
            _ensure_volume_budget(state, findings)
            retries = _retry_unresolved_scenes(state, findings)
            if retries:
                # _reissue already appends to the queue; do NOT _enqueue
                # again (that duplicates every task).
                actions.append(
                    ActionRef(
                        action="schedule_scene_retries",
                        detail=f"{len(retries)} scene tasks with layout evidence",
                    )
                )
            else:
                # Nothing to build and nothing new to schedule, but the
                # model still owes an answer: this is awaiting_harness,
                # not a blocked run. Marking the run blocked here would
                # strand its own pending tasks (§6, §12.4).
                if _has_pending_kind(state, ModelTaskKind.SCENE_GENERATION) or _has_pending_kind(
                    state, ModelTaskKind.SCENE_REPAIR
                ):
                    state.stage = "build"
                    state.terminal_reason = ""
                    actions.append(
                        ActionRef(
                            action="await_scene_responses",
                            detail="scene tasks still pending; no rebuild",
                        )
                    )
                else:
                    state.stage = "blocked"
                    state.terminal_reason = "no deck produced and no scene retry budget left"
                    findings.append(
                        TypedFinding(
                            code="BUILD_NO_CANDIDATE",
                            severity="error",
                            message=state.terminal_reason,
                        )
                    )

    if state.stage == "render":
        rendered = _render_all_candidates(state)
        if rendered:
            state.stage = "visual_review"
            actions.append(ActionRef(action="render_complete", detail=f"{rendered} rendered"))
            # Reviews only for rendered decks: a review task without
            # bytes to open is not a review, and verdicts on missing
            # renders would be fabricated.
            for deck in state.deck_candidates:
                if not deck.get("render_manifest"):
                    continue
                task = _visual_review_task(state, deck)
                if task is not None:
                    _enqueue(state, task)
        else:
            state.stage = "blocked"
            errors = [str(d.get("render_issues", [])) for d in state.deck_candidates]
            state.terminal_reason = "NATIVE_RENDER_NOT_PRODUCED:" + "; ".join(errors)
            findings.append(
                TypedFinding(
                    code="NATIVE_RENDER_NOT_PRODUCED",
                    severity="error",
                    message=state.terminal_reason,
                )
            )

    if state.stage == "visual_review":
        reviews = _responses_of_kind(state, ModelTaskKind.VISUAL_REVIEW)
        if reviews and not _has_pending_kind(state, ModelTaskKind.VISUAL_REVIEW):
            for response in reviews:
                _write_visual_review(state, response)
            # schedule design_review per RENDERED deck candidate only.
            for deck in state.deck_candidates:
                if not deck.get("render_manifest"):
                    continue
                task = _design_review_task(state, deck)
                if task is not None:
                    _enqueue(state, task)
            state.stage = "design_review"
            actions.append(
                ActionRef(
                    action="issue_design_reviews", detail=f"{len(state.deck_candidates)} reviews"
                )
            )

    if state.stage == "design_review":
        reviews = _responses_of_kind(state, ModelTaskKind.DESIGN_REVIEW)
        if reviews and not _has_pending_kind(state, ModelTaskKind.DESIGN_REVIEW):
            for response in reviews:
                _write_design_review(state, response)
            state.stage = "candidate_selection"
            actions.append(
                ActionRef(action="design_reviews_recorded", detail=f"{len(reviews)} reviews")
            )

    if state.stage in ("candidate_selection", "selections", "full_assembly", "final_checks"):
        actions.extend(_advance_build_stages(state, findings))

    save_state(state)
    return actions


def _consume_scene_responses(state: RunState, findings: list[TypedFinding], run_dir: Path) -> int:
    """Validate + materialize consumed scene responses into plans.

    Shared by the scene_generation stage and the build-stage retry path:
    re-issued scene tasks (new revisions with layout evidence) arrive while
    the run sits in ``build``. Each response is materialized exactly once;
    ``materialized_scene_responses`` records the lineage so earlier
    revisions are never re-processed over newer ones.
    """
    from .models import SemanticDeck, SemanticSlide, VisualConcept
    from .planner import validate_scene_response

    processed = set(state.processed_scene_responses)
    plans = [
        r
        for r in state.responses
        if r.request_id not in processed
        and r.request_id not in state.materialized_scene_responses
        and _task_kind(state, r.request_id)
        in (ModelTaskKind.SCENE_GENERATION, ModelTaskKind.SCENE_REPAIR)
    ]
    if not plans:
        return 0
    written = 0
    for response in plans:
        plan = response.payload.get("scene_plan")
        ck = response.request_id
        key = _key_for_request(state, ck)
        if not plan or key is None:
            # Transport receipt only: no accepted plan. Record once so a
            # repeated resume does not re-scan it as materialized.
            state.processed_scene_responses.append(response.request_id)
            if key is None:
                state.rejected_scene_reports[response.request_id] = {
                    "code": "SCENE_PROVENANCE_MISSING",
                    "error": "no candidate_key/provenance for request id",
                }
            else:
                state.rejected_scene_reports[response.request_id] = {
                    "code": "SCENE_PAYLOAD_MISSING",
                    "error": "payload missing scene_plan",
                }
            continue
        concept_id = key.split("/")[-1]
        try:
            sid = _slide_id_for_key(state, key)
        except RunnerError as exc:
            # Provenance that cannot be resolved is a refusal with a receipt,
            # not a crash of the whole resume: nothing is built, and the
            # negative history is kept for the next activation.
            state.processed_scene_responses.append(response.request_id)
            state.rejected_scene_reports[response.request_id] = {
                "code": "SCENE_PROVENANCE_MISSING",
                "key": key,
                "error": str(exc),
            }
            findings.append(
                TypedFinding(
                    code="SCENE_PROVENANCE_MISSING",
                    severity="error",
                    message=(
                        f"{key}: candidate provenance is unresolvable ({exc}); "
                        "scene is not valid and cannot be built"
                    ),
                )
            )
            continue
        plan_dir = run_dir / "scenes" / _safe_name(sid) / concept_id
        plan_dir.mkdir(parents=True, exist_ok=True)

        task = next((t for t in state.task_queue if t.request_id == response.request_id), None)
        semantic_slide = None
        sem_path = run_dir / "inputs" / "semantic_deck.json"
        if sem_path.is_file():
            with contextlib.suppress(OSError, ValueError):
                deck = json.loads(sem_path.read_text(encoding="utf-8"))
                for s in deck.get("slides", []):
                    if s.get("source_slide_id") == sid:
                        semantic_slide = SemanticSlide.model_validate(s)
                        break

        visual_concept = None
        concept_path = _concept_path(run_dir, sid, concept_id)
        if concept_path.is_file():
            with contextlib.suppress(OSError, ValueError):
                visual_concept = VisualConcept.model_validate(
                    json.loads(concept_path.read_text(encoding="utf-8"))
                )

        rules_path = run_dir / "compiled_rules.json"
        rules = None
        if rules_path.is_file():
            with contextlib.suppress(OSError, ValueError):
                from .models import CompiledOntology

                rules = CompiledOntology.model_validate(
                    json.loads(rules_path.read_text(encoding="utf-8"))
                )
        if task and semantic_slide and visual_concept:
            report = validate_scene_response(
                response, task, semantic_slide, visual_concept, rules or CompiledOntology()
            )
            if report.valid:
                # Materialize the validated scene: the model
                # addresses semantic units, the bridge binds source
                # objects. The written plan is what the build uses.
                try:
                    deck0 = SemanticDeck.model_validate_json(
                        (run_dir / "inputs" / "semantic_deck.json").read_text(encoding="utf-8")
                    )
                    sem0 = next(x for x in deck0.slides if x.source_slide_id == sid)
                except (OSError, ValueError, StopIteration):
                    sem0 = semantic_slide
                known_ids = {str(o.get("id", "")) for o in _slide_source_objects(run_dir, sid)}
                known_ids.discard("")
                build_plan, unexpanded = _materialize_scene_plan(
                    plan, sem0, findings, key, source_object_ids=known_ids
                )
                if unexpanded:
                    state.candidate_states[key] = "schema_invalid"
                    findings.append(
                        TypedFinding(
                            code="SCENE_UNRESOLVED_REF",
                            severity="error",
                            message=(
                                f"{key}: scene refs are neither source "
                                "object ids nor semantic unit ids: "
                                f"{unexpanded[:8]}; scene is not valid "
                                "and cannot be built"
                            ),
                        )
                    )
                    (plan_dir / "authored_plan.json").write_text(
                        json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8"
                    )
                    (plan_dir / "scene_validation_report.json").write_text(
                        json.dumps(
                            {"valid": False, "hard_issues": [f"unexpanded refs: {unexpanded[:8]}"]},
                            ensure_ascii=False,
                            indent=1,
                        ),
                        encoding="utf-8",
                    )
                    state.rejected_scene_reports[response.request_id] = {
                        "code": "SCENE_UNRESOLVED_REF",
                        "key": key,
                        "unexpanded": unexpanded[:8],
                    }
                    state.processed_scene_responses.append(response.request_id)
                    continue
                state.candidate_states[key] = "scene_validated"
                # Preserve the superseded revision (§12 lineage):
                # plan.r{N}.json keeps the previous attempt.
                rev = state.candidate_revisions.get(key, 0)
                prev = plan_dir / "plan.json"
                if prev.is_file():
                    with contextlib.suppress(OSError):
                        prev.rename(plan_dir / f"plan.r{rev}.json")
                (plan_dir / "plan.json").write_text(
                    json.dumps(build_plan, ensure_ascii=False, indent=1), encoding="utf-8"
                )
                (plan_dir / "authored_plan.json").write_text(
                    json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8"
                )
                state.materialized_scene_responses.append(response.request_id)
                written += 1
            else:
                state.candidate_states[key] = "schema_invalid"
                (plan_dir / "scene_validation_report.json").write_text(
                    report.model_dump_json(indent=1), encoding="utf-8"
                )
                (plan_dir / "authored_plan.json").write_text(
                    json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8"
                )
                state.rejected_scene_reports[response.request_id] = {
                    "code": "SCENE_SCHEMA_INVALID",
                    "key": key,
                    "hard_issues": report.hard_issues,
                }
                state.processed_scene_responses.append(response.request_id)
                findings.append(
                    TypedFinding(
                        code="SCENE_SCHEMA_INVALID",
                        severity="error",
                        message=f"{key}: {'; '.join(report.hard_issues[:3])}",
                    )
                )
        else:
            # Fail closed (§3.3). The pre-0.x branch marked the
            # candidate `scene_validated` whenever the task, the
            # semantic slide or the concept could not be loaded,
            # which let an unvalidated scene reach the build stage
            # and publish. The missing references are named and the
            # candidate waits for a materialization/repair action.
            missing = [
                name
                for name, value in (
                    ("model_task", task),
                    ("semantic_slide", semantic_slide),
                    ("visual_concept", visual_concept),
                    ("compiled_rules", rules_path.is_file() and (run_dir / "compiled_rules.json")),
                )
                if not value
            ]
            state.candidate_states[key] = "scene_context_missing"
            state.rejected_scene_reports[response.request_id] = {
                "code": "SCENE_CONTEXT_MISSING",
                "key": key,
                "missing": missing,
            }
            state.processed_scene_responses.append(response.request_id)
            findings.append(
                TypedFinding(
                    code="SCENE_CONTEXT_MISSING",
                    severity="error",
                    message=(
                        f"{key}: missing {', '.join(missing)}; "
                        "scene is not valid and cannot be built "
                        "until they are materialized"
                    ),
                )
            )
    return written


def _slide_id_for_key(state: RunState, key: str) -> str:
    # Explicit concept->slide provenance only. A concept id such as c1 is
    # shared by every slide, so falling back to the first slide that has
    # c1 silently mixes candidates (§3/§5.2).
    if key in state.concept_slide:
        return state.concept_slide[key]
    matches = [
        sid
        for sid, candidates in state.slide_candidates.items()
        for cid in candidates
        if key == f"{state.run_id}/{_safe_name(sid)}/{cid}"
    ]
    if len(matches) == 1:
        return matches[0]
    raise RunnerError(f"CANDIDATE_PROVENANCE_MISSING:{key}")


def _safe_name(s: str) -> str:
    """Filesystem-safe form of a slide id (may contain '/')."""
    return s.replace("/", "_").replace(":", "_")


def _key_for_request(state: RunState, request_id: str) -> str | None:
    for task in state.task_queue:
        # scene_generation tasks carry candidate_key
        if task.request_id == request_id and task.candidate_key:
            return (
                f"{task.candidate_key.run_id}/"
                f"{_safe_name(task.candidate_key.source_slide_id)}/"
                f"{task.candidate_key.candidate_id}"
            )
    return None


def _slide_source_objects(run_dir: Path, sid: str) -> list[dict[str, Any]]:
    """Compact source object inventory for scene authoring (ids + text)."""
    path = run_dir / "source_ir.json"
    try:
        ir = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    for slide in ir.get("slides", []):
        if slide.get("id") != sid:
            continue
        out: list[dict[str, Any]] = []
        for obj in slide.get("objects", []):
            payload = obj.get("payload", {}) or {}
            text = ""
            inner = payload.get("text") or {}
            parts: list[str] = []
            for para in inner.get("paragraphs", []) or []:
                for run in para.get("runs", []) or []:
                    if run.get("text"):
                        parts.append(run["text"])
            text = " ".join(parts)
            box = obj.get("local_box") or {}
            out.append(
                {
                    "id": obj.get("id", ""),
                    "kind": obj.get("kind", ""),
                    "text": text[:160],
                    "box_emu": box,
                    "semantic_significance": obj.get("semantic_significance", ""),
                }
            )
        return out
    return []


def _scene_generation_task(state: RunState, key: str) -> ModelTask | None:
    """Task: harness authors a LayoutIntent scene plan for one concept."""
    from .models import CandidateKey, VisualConcept  # noqa: PLC0415

    run_dir = Path(state.run_dir)
    concept_id = key.split("/")[-1]
    sid = _slide_id_for_key(state, key)
    safe_sid = _safe_name(sid)
    brief_path = run_dir / "design_briefs" / f"{safe_sid}.json"
    semantic_path = run_dir / "inputs" / "semantic_deck.json"
    slide_objects = _slide_source_objects(run_dir, sid)

    visual_concept: VisualConcept | None = None
    # The concept is persisted by the concept stage into the canonical
    # candidate directory (`<run>/scenes/<slide>/<candidate>/concept.json`),
    # so the scene task reads exactly that artifact. The previous
    # `visual_concepts/<slide>/<concept>.json` path never existed, which is
    # why every scene task lost its concept.
    concept_path = _concept_path(run_dir, sid, concept_id)
    if concept_path.is_file():
        with contextlib.suppress(OSError, ValueError):
            visual_concept = VisualConcept.model_validate(
                json.loads(concept_path.read_text(encoding="utf-8"))
            )

    packet_payload = {
        "stage": "scene_generation",
        "slide_id": sid,
        "concept_id": concept_id,
        "concept_key": key,
        "source_objects": slide_objects,
        "response_schema": "scene-plan/v1",
        "required_fields": ["slide_id", "candidate_id", "content_refs", "zones", "relations"],
        "authoring_contract": "semantic-zones/v3",
        "rule": "Author semantic groups, not a shape-by-shape stack. "
        "zones.content_refs and ref_roles keys use semantic unit IDs. "
        "reading_order and dominant_ref use zone IDs. ref_roles values "
        "must be exact loaded ontology role tokens, not semantic_role names. "
        "A zone with several text units emits one native rich block. "
        "Relations carry semantic unit endpoints; they do not authorize overlap. "
        "No invented facts, no font shrink, no copied archival answers.",
    }
    if visual_concept is not None:
        packet_payload["visual_concept"] = visual_concept.model_dump()
    if brief_path.is_file():
        with contextlib.suppress(OSError, ValueError):
            packet_payload["design_brief"] = json.loads(brief_path.read_text(encoding="utf-8"))
    if semantic_path.is_file():
        with contextlib.suppress(OSError, ValueError):
            deck = json.loads(semantic_path.read_text(encoding="utf-8"))
            for s in deck.get("slides", []):
                if s.get("source_slide_id") == sid:
                    packet_payload["semantic_slide"] = s
                    break
    rules_path = run_dir / "compiled_rules.json"
    if rules_path.is_file():
        loaded = json.loads(rules_path.read_text(encoding="utf-8"))
        packet_payload["allowed_role_styles"] = loaded.get("roles", {})
        packet_payload["allowed_color_tokens"] = loaded.get("colors", {})
        packet_payload["conditional_colors"] = loaded.get("conditional_colors", [])
    digest_path = run_dir / "model_rule_digest.md"
    if digest_path.is_file():
        packet_payload["model_rule_digest_md"] = digest_path.read_text(encoding="utf-8")
    required_images = []
    source_ir = _load_json(run_dir / "source_ir.json")
    for index, source_slide in enumerate(source_ir.get("slides", []), start=1):
        if source_slide.get("id") == sid:
            image = run_dir / "source_render" / f"slide-{index:02d}.png"
            if image.is_file():
                required_images.append(str(image.resolve()))
    packet_payload["required_images"] = required_images
    packet_payload["input_sha256"] = state.snapshots.input_sha256
    fingerprint = _sha_json(packet_payload)
    request_id = _make_request_id(f"scene:{key}", fingerprint, 0)
    rel = f"model_requests/{request_id}/packet.json"
    (run_dir / rel).parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(packet_payload, ensure_ascii=False, indent=1)
    path = run_dir / rel
    if path.exists() and path.read_text(encoding="utf-8") != raw:
        raise RunnerError("IMMUTABLE_SCENE_PACKET_CONFLICT")
    if not path.exists():
        path.write_text(raw, encoding="utf-8")
    return ModelTask(
        request_id=request_id,
        kind=ModelTaskKind.SCENE_GENERATION,
        candidate_key=CandidateKey(
            run_id=state.run_id, source_slide_id=sid, candidate_id=concept_id
        ),
        input_fingerprint=fingerprint,
        response_schema_id="scene-plan/v1",
        packet=rel,
        required_images=required_images,
        expected_response_units=["scene_plan"],
    )


def _concept_path(run_dir: Path, slide_id: str, concept_id: str) -> Path:
    """Canonical concept artifact: <run>/scenes/<slide>/<candidate>/concept.json.

    One helper for the writer and both readers: the earlier code had two
    different paths, so a scene task could never find the concept its own
    packet referenced.
    """
    return run_dir / "scenes" / _safe_name(slide_id) / concept_id / "concept.json"


def _design_brief_retry(
    state: RunState, old: ModelTask | None, response: ModelTaskResponse
) -> ModelTask:
    """A fresh design-brief task after a malformed response was consumed.

    The retry keeps the ORIGINAL packet path, so the replacement answer is
    judged against exactly the same request as the rejected one.
    """
    return ModelTask(
        request_id=f"{response.request_id}-retry{state.revision + 1}",
        kind=ModelTaskKind.DESIGN_BRIEF,
        input_fingerprint=response.input_fingerprint,
        response_schema_id="slide-design-brief/v1",
        packet=(old.packet if old is not None else "model_requests/design_brief.json"),
        expected_response_units=[
            "communication_job",
            "intended_takeaway",
            "semantic_groups",
            "visual_encoding_candidates",
        ],
    )


def _materialize_scene_plan(
    plan: dict[str, Any],
    semantic_slide: Any,
    findings: list[TypedFinding] | None = None,
    candidate_key: str = "",
    source_object_ids: set[str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """One checked semantic/zone/source lowering path, shared with preflight."""
    from .scene_contract import lower_scene_plan

    lowered, errors = lower_scene_plan(plan, semantic_slide, source_object_ids)
    if errors and findings is not None:
        findings.append(
            TypedFinding(
                code="SCENE_BINDING_INVALID", severity="error", message=f"{candidate_key}: {errors}"
            )
        )
    return lowered, errors


def _build_all_deck_candidates(state: RunState, findings: list[TypedFinding]) -> bool:
    """Compatibility entry point: isolated native jobs for committed scenes."""
    from .candidate_jobs import build_ready_candidates

    return build_ready_candidates(
        state, findings, builder=_build_deck_candidate, config=_config_from_state(state)
    )


# Finding codes that record legitimate, authorized operations (§8.1):
# they ride along in candidate findings but never block eligibility alone.
_INFORMATIONAL_FINDING_CODES = frozenset(
    {
        "AUTHORIZED_OMITS",
        "DECOR_FILL_NORMALIZED",
        "DECOR_LINE_NORMALIZED",
        "DECOR_CLUSTER_OMITTED",
        "DECOR_OMITTED",
        "SURFACE_APPLIED",
        "STROKE_APPLIED",
        "GEOMETRY_NORMALIZED",
        "REEXPRESSED_RELATIONS",
        "SOURCE_BOUND_TWICE",
        "BUDGET_EXTENDED",
        "ENGINE_UPDATE_REEVALUATION",
        "UNBLOCKED_FOR_MATERIALIZATION",
    }
)


def _build_deck_candidate(
    state: RunState,
    deck_key: str,
    selection: dict[str, str],
    config: Any,
    *,
    focus_slide: str = "",
    focus_candidate: str = "",
) -> Any:
    """Build one full-deck candidate; returns a typed CandidateAttemptResult.

    A candidate PPTX on disk is not a success: the measured resolve status,
    the saved checks and the render outcome decide ``eligible``. A failed
    build keeps its candidate path for diagnosis and stays ineligible.
    """
    from .export_models import ExportOptions  # noqa: PLC0415
    from .measured_build import run_measured_build  # noqa: PLC0415
    from .models import CandidateAttemptResult  # noqa: PLC0415

    run_dir = Path(state.run_dir)
    build_dir = run_dir / "scenes" / deck_key
    build_dir.mkdir(parents=True, exist_ok=True)

    # symlink shared profile inputs into the isolated build dir
    for name in (
        "source_ir.json",
        "source_ledger.json",
        "compiled_rules.json",
        "template_profile.json",
        "font_inventory.json",
        "run_manifest.json",
        "classification_and_transformation_ledger.json",
    ):
        src = run_dir / name
        dst = build_dir / name
        if src.is_file() and not dst.exists():
            dst.symlink_to(src)
    for name in ("assets", "inputs"):
        src = run_dir / name
        dst = build_dir / name
        if src.is_dir() and not dst.exists():
            dst.symlink_to(src, target_is_directory=True)
    for tmpl in (run_dir / "profiles" / "base_template.pptx", run_dir / "base_template.pptx"):
        if tmpl.is_file():
            dst = build_dir / "profiles" / "base_template.pptx"
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists():
                dst.symlink_to(tmpl)
            break

    plans_dir = build_dir / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    for slide_id, cand_id in selection.items():
        plan_path = run_dir / "scenes" / _safe_name(slide_id) / cand_id / "plan.json"
        if not plan_path.is_file():
            return CandidateAttemptResult(
                deck_key=deck_key,
                slide_id=focus_slide,
                candidate_id=focus_candidate,
                operation_status="missing_scene_plan",
                eligible=False,
                eligibility_reason=f"missing scene plan {slide_id}/{cand_id}",
                findings=[
                    TypedFinding(
                        code="SCENE_PLAN_MISSING",
                        severity="error",
                        message=f"{deck_key}: no plan.json for {slide_id}/{cand_id}",
                    )
                ],
            )
        dst = plans_dir / f"{slide_id.replace('/', '_')}__{cand_id}.json"
        dst.write_text(plan_path.read_text(encoding="utf-8"), encoding="utf-8")

    options = ExportOptions(workflow="presentation", diagnostic=True, overwrite=True)
    layout_options: dict[str, Any] = {
        "resume": False,
        "no_measurement_cache": False,
        "profile_kind": "production",
        "render": False,
        "candidate_name": "candidate.pptx",
        "selection": selection,
        "selected_slide_ids": list(selection) if focus_slide else None,
    }
    result: Any = None
    try:
        result = run_measured_build(build_dir, plans_dir, config, options, layout_options)
    except Exception as exc:  # noqa: BLE001 - typed finding, not crash
        return CandidateAttemptResult(
            deck_key=deck_key,
            slide_id=focus_slide,
            candidate_id=focus_candidate,
            operation_status="build_exception",
            eligible=False,
            eligibility_reason=f"build exception: {exc}",
            findings=[
                TypedFinding(code="BUILD_EXCEPTION", severity="error", message=f"{deck_key}: {exc}")
            ],
        )
    candidate = build_dir / "candidate.pptx"
    rel = f"scenes/{deck_key}/candidate.pptx"
    if not candidate.is_file():
        return CandidateAttemptResult(
            deck_key=deck_key,
            slide_id=focus_slide,
            candidate_id=focus_candidate,
            operation_status=str(getattr(result, "operation_status", "unknown")),
            eligible=False,
            eligibility_reason="no candidate bytes produced",
            findings=[
                TypedFinding(
                    code="BUILD_NO_CANDIDATE",
                    severity="error",
                    message=f"{deck_key}: status={getattr(result, 'operation_status', '?')}",
                )
            ],
        )
    operation = str(getattr(result, "operation_status", "") or "")
    measured = "resolved" if operation in ("completed", "needs_review") else operation
    saved_ok = bool(getattr(result, "saved_geometry_verified", False))
    layouts = _load_json(build_dir / "stage5_readiness.json") or {}
    layout_report = _load_json(build_dir / "layout_geometry_report.json") or {}
    measured_ok = str(layout_report.get("result", "")) == "pass"
    # Only well-formed finding codes count; free-prose fragments with a
    # colon (template hashes, drop counts) are not gate codes.
    codes = sorted(
        {
            head
            for head in (
                str(i).split(":", 1)[0].strip()
                for i in list(getattr(result, "issues", []) or [])
                if ":" in str(i)
            )
            if re.fullmatch(r"[A-Z][A-Z0-9_]*", head)
        }
    )
    # Informational operation records (authorized omits, applied palette
    # tokens, declared normalizations, repair warnings with deterministic
    # handling) never block eligibility by themselves; every other code
    # is a blocking finding for this candidate.
    blocking = sorted(set(codes) - _INFORMATIONAL_FINDING_CODES)
    eligible = operation == "completed" and saved_ok and measured_ok and not blocking
    reason = (
        "measured resolve, saved checks and render prerequisites all current"
        if eligible
        else f"status={operation} saved_checks={saved_ok} "
        f"measured={measured_ok} blocking={blocking[:4]}"
    )
    return CandidateAttemptResult(
        deck_key=deck_key,
        slide_id=focus_slide,
        candidate_id=focus_candidate,
        candidate_path=rel,
        candidate_sha256=_sha_file(candidate),
        operation_status=operation,
        measured_status=measured,
        saved_checks_status="pass" if saved_ok else "fail",
        attempt_count=int(layouts.get("attempts", 0) or 0),
        finding_codes=codes,
        findings=[
            TypedFinding(
                code=code,
                severity="info" if code in _INFORMATIONAL_FINDING_CODES else "error",
                message=f"{deck_key}: {code}",
            )
            for code in codes
        ],
        eligible=eligible,
        eligibility_reason=reason,
    )


def _render_all_candidates(state: RunState) -> int:
    from .models import FontInventory
    from .render import candidate_typefaces, render_native_candidate
    from .review_jobs import render_candidates
    from .typography import prepare_measurement_fonts

    path = Path(state.run_dir) / "font_inventory.json"
    bundle = None
    if path.is_file():
        inventory = FontInventory.model_validate_json(path.read_text())
        bundle = prepare_measurement_fonts(inventory, "", profile_kind="production")
    return render_candidates(
        state, renderer=render_native_candidate, typefaces=candidate_typefaces, bundle=bundle
    )


def _visual_review_task(state: RunState, deck: dict[str, Any]) -> ModelTask | None:
    from .review_jobs import make_review_task

    return make_review_task(state, deck)


def _write_visual_review(state: RunState, response: ModelTaskResponse) -> None:
    run_dir = Path(state.run_dir)
    payload = response.payload
    deck_key = str(payload.get("deck_key", ""))
    review = {
        "task_id": response.request_id,
        "deck_key": deck_key,
        "candidate_sha256": payload.get("candidate_sha256", ""),
        "verdict": payload.get("verdict", "repair"),
        "pages": payload.get("pages", []),
        "findings": payload.get("findings", []),
        "actual_model": response.actual_model,
    }
    (run_dir / "scenes" / deck_key / "visual_review.json").write_text(
        json.dumps(review, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def _design_review_task(state: RunState, deck: dict[str, Any]) -> ModelTask | None:
    from .review_jobs import make_review_task

    return make_review_task(state, deck, design=True)


def _write_design_review(state: RunState, response: ModelTaskResponse) -> None:
    run_dir = Path(state.run_dir)
    payload = response.payload
    deck_key = str(payload.get("deck_key", ""))
    review = {
        "task_id": response.request_id,
        "deck_key": deck_key,
        "candidate_sha256": payload.get("candidate_sha256", ""),
        "verdict": payload.get("verdict", "repair"),
        "dimensions": payload.get("dimensions", []),
        "findings": payload.get("findings", []),
        "actual_model": response.actual_model,
    }
    (run_dir / "scenes" / deck_key / "design_review.json").write_text(
        json.dumps(review, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def _advance_build_stages(state: RunState, findings: list[TypedFinding]) -> list[ActionRef]:
    """Selection -> assembly -> final acceptance."""
    actions: list[ActionRef] = []
    run_dir = Path(state.run_dir)

    if state.stage == "candidate_selection":
        from .scene_jobs import comparison_coverage, repair_failed_candidates

        eligible = _collect_eligible(state)
        complete, missing = comparison_coverage(state, eligible)
        pending_scenes = any(
            t.status == "pending"
            and t.kind in (ModelTaskKind.SCENE_GENERATION, ModelTaskKind.SCENE_REPAIR)
            for t in state.task_queue
        )
        if complete and not pending_scenes:
            rel = _write_selection_packet(run_dir, eligible, state)
            packet = _load_json(run_dir / rel)
            fingerprint = _sha_json(packet)
            task = ModelTask(
                request_id=_make_request_id("candidate_selection", fingerprint, 0),
                kind=ModelTaskKind.CANDIDATE_SELECTION,
                input_fingerprint=fingerprint,
                response_schema_id="selection-report/v1",
                packet=rel,
                required_images=packet.get("required_images", []),
                expected_response_units=["selections"],
            )
            _enqueue(state, task)
            state.stage = "selections"
            actions.append(
                ActionRef(
                    action="issue_selection_task", detail=f"{len(eligible)} eligible variants"
                )
            )
        else:
            retries = repair_failed_candidates(state)
            pending = any(
                t.status == "pending"
                and t.kind in (ModelTaskKind.SCENE_GENERATION, ModelTaskKind.SCENE_REPAIR)
                for t in state.task_queue
            )
            _atomic_write(
                run_dir / "comparison_coverage.json",
                json.dumps({"complete": False, "missing": missing}, ensure_ascii=False, indent=2),
            )
            if retries or pending:
                state.stage = "build"
                actions.append(
                    ActionRef(
                        action="await_comparative_variants",
                        detail=json.dumps(missing, ensure_ascii=False),
                    )
                )
            else:
                state.stage = "blocked"
                state.terminal_reason = "COMPARATIVE_VARIANTS_MISSING:" + json.dumps(
                    missing, ensure_ascii=False
                )
                findings.append(
                    TypedFinding(
                        code="COMPARATIVE_VARIANTS_MISSING",
                        severity="error",
                        message=state.terminal_reason,
                    )
                )

    elif state.stage == "selections":
        sels = _responses_of_kind(state, ModelTaskKind.CANDIDATE_SELECTION)
        if sels and not _has_pending_kind(state, ModelTaskKind.CANDIDATE_SELECTION):
            from .models import SelectionReport  # noqa: PLC0415

            report = SelectionReport.model_validate(sels[-1].payload)
            (run_dir / "selection_report.json").write_text(
                json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            for entry in report.selections:
                key = (
                    f"{state.run_id}/{_safe_name(entry.source_slide_id)}/"
                    f"{entry.candidate_key.candidate_id}"
                )
                if key in state.candidate_states:
                    state.candidate_states[key] = "selected"
            state.stage = "full_assembly"
            actions.append(
                ActionRef(action="record_selection", detail=f"{len(report.selections)} selected")
            )

    elif state.stage == "full_assembly":
        ok, detail, needs_review = _assemble_final(state, findings)
        if ok and not needs_review:
            state.stage = "final_checks"
            actions.append(ActionRef(action="assembly_verified", detail=detail))
        elif ok and needs_review:
            # rebuilt final needs fresh visual+design reviews; stay in
            # full_assembly until they arrive (pending tasks -> harness).
            actions.append(ActionRef(action="final_reviews_pending", detail=detail))
        else:
            from .scene_jobs import repair_final_reviews

            repairs = repair_final_reviews(state)
            if repairs:
                state.stage = "build"
                actions.append(
                    ActionRef(
                        action="final_review_scene_repairs",
                        detail=f"{len(repairs)} targeted repairs",
                    )
                )
            else:
                state.stage = "blocked"
                state.terminal_reason = detail
                findings.append(
                    TypedFinding(
                        code="SELECTED_CANDIDATE_NOT_ASSEMBLED", severity="error", message=detail
                    )
                )
                actions.append(ActionRef(action="assembly_blocked", detail=detail))

    elif state.stage == "final_checks":
        result = _run_final_acceptance(state)
        if result is None:
            state.stage = "blocked"
            state.terminal_reason = "final acceptance evidence incomplete"
            actions.append(ActionRef(action="acceptance_blocked", detail=state.terminal_reason))
        elif result.accepted:
            product = publish_accepted_product(state, result)
            state.accepted_product = ProductRef(
                output_path=product.output_path, candidate_sha256=product.candidate_sha256
            )
            state.stage = "accepted"
            actions.append(ActionRef(action="published", detail=product.output_path))
        else:
            repairs = schedule_repairs(state, _acceptance_findings(result))
            if repairs:
                state.stage = "build"
                actions.append(
                    ActionRef(action="schedule_repairs", detail=f"{len(repairs)} repair tasks")
                )
            else:
                state.stage = "blocked"
                state.terminal_reason = "; ".join(result.error_codes)
                actions.append(ActionRef(action="acceptance_blocked", detail=state.terminal_reason))
    return actions


def _collect_eligible(state: RunState) -> list[str]:
    """Deck candidates with current-hash accept visual + design reviews."""
    run_dir = Path(state.run_dir)
    eligible: list[str] = []
    for deck in state.deck_candidates:
        deck_key = str(deck["deck_key"])
        cand_path = run_dir / "scenes" / deck_key / "candidate.pptx"
        if not cand_path.is_file():
            continue
        cand_sha = _sha_file(cand_path)
        vis = _load_json(run_dir / "scenes" / deck_key / "visual_review.json")
        des = _load_json(run_dir / "scenes" / deck_key / "design_review.json")
        if not vis or not des:
            continue
        if vis.get("verdict") != "accept":
            continue
        if des.get("verdict") != "accept":
            continue
        if vis.get("candidate_sha256") != cand_sha:
            continue
        if des.get("candidate_sha256") != cand_sha:
            continue
        # §10.1: a candidate that failed its own measured/saved stages is
        # never eligible for selection, even when the reviews accepted it.
        if not deck.get("eligible") or deck.get("render_valid") is not True:
            continue
        eligible.append(deck_key)
    return eligible


def _write_selection_packet(run_dir: Path, eligible: list[str], state: RunState) -> str:
    candidates, images = [], []
    for job in state.deck_candidates:
        if job["deck_key"] not in eligible:
            continue
        folder = run_dir / "scenes" / job["deck_key"]
        manifest = _load_json(folder / "render/render_manifest.json")
        pngs = [str(folder / "render" / p["image"]) for p in manifest.get("pages", [])]
        images.extend(pngs)
        candidates.append(
            {
                "deck_key": job["deck_key"],
                "selection": job["selection"],
                "candidate_sha256": _sha_file(folder / "candidate.pptx"),
                "candidate_path": str((folder / "candidate.pptx").relative_to(run_dir)),
                "required_images": pngs,
                "visual_review": _load_json(folder / "visual_review.json"),
                "design_review": _load_json(folder / "design_review.json"),
            }
        )
    alternatives = []
    for job in state.deck_candidates:
        if job.get("scope_kind") != "slide_candidate":
            continue
        folder = run_dir / "scenes" / job["deck_key"]
        manifest = _load_json(folder / "render/render_manifest.json")
        pngs = [str(folder / "render" / p["image"]) for p in manifest.get("pages", [])]
        images.extend(pngs)
        alternatives.append(
            {
                "deck_key": job["deck_key"],
                "selection": job["selection"],
                "eligible": job["deck_key"] in eligible,
                "attempt": job.get("attempt", {}),
                "required_images": pngs,
                "visual_review": _load_json(folder / "visual_review.json"),
                "design_review": _load_json(folder / "design_review.json"),
            }
        )
    payload = {
        "stage": "candidate_selection",
        "eligible": eligible,
        "candidates": candidates,
        "evaluated_alternatives": alternatives,
        "expected_slide_ids": list(state.slide_candidates),
        "required_images": list(dict.fromkeys(images)),
        "response_schema": "selection-report/v1",
        "required_fields": ["selections"],
        "rule": "Exactly one eligible selection per expected slide, with actual candidate "
        "hash/path, considered alternatives and reasons. Open the PNGs. "
        "Compare visual explanation, hierarchy and grouping; clean fit alone is insufficient.",
    }
    fingerprint = _sha_json(payload)
    rel = f"model_requests/selection-{fingerprint}/packet.json"
    _atomic_write(run_dir / rel, json.dumps(payload, ensure_ascii=False, indent=2))
    return rel


def _assemble_final(state: RunState, findings: list[TypedFinding]) -> tuple[bool, str, bool]:
    """Build the final deck from the selection and stage all gate evidence.

    Returns (ok, detail, needs_final_review). Reuses a reviewed deck when
    the selection matches it exactly; otherwise rebuilds scenes/final/ and
    schedules fresh visual+design reviews of the new bytes.
    """
    run_dir = Path(state.run_dir)
    # persist any visual/design responses that arrived during assembly
    # (e.g. fresh final reviews) before the reuse check reads them.
    for response in _responses_of_kind(state, ModelTaskKind.VISUAL_REVIEW):
        _write_visual_review(state, response)
    for response in _responses_of_kind(state, ModelTaskKind.DESIGN_REVIEW):
        _write_design_review(state, response)
    sel_path = run_dir / "selection_report.json"
    if not sel_path.is_file():
        return False, "selection_report.json absent", False
    try:
        sel = json.loads(sel_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, f"selection report unreadable: {exc}", False
    selection: dict[str, str] = {}
    for entry in sel.get("selections", []):
        ck = entry.get("candidate_key") or {}
        selection[str(entry.get("source_slide_id", ""))] = str(ck.get("candidate_id", ""))
    if not selection:
        return False, "empty selection", False
    if set(selection) != set(state.slide_candidates):
        return False, "SELECTION_COVERAGE_MISMATCH", False
    if len(sel.get("selections", [])) != len(selection):
        return False, "SELECTION_DUPLICATE_SLIDE", False

    final_pending = [
        t
        for t in state.task_queue
        if t.status == "pending"
        and t.kind
        in (ModelTaskKind.VISUAL_REVIEW, ModelTaskKind.DESIGN_REVIEW, ModelTaskKind.RULE_REVIEW)
    ]
    if final_pending and (run_dir / "scenes/final/candidate.pptx").is_file():
        return True, "final artifact awaiting current reviews", True
    for name in ("visual_review", "design_review"):
        review = _load_json(run_dir / "scenes/final" / f"{name}.json")
        path = run_dir / "scenes/final/candidate.pptx"
        if (
            review
            and path.is_file()
            and review.get("candidate_sha256") == _sha_file(path)
            and review.get("verdict") in ("repair", "reject")
        ):
            return False, f"FINAL_{name.upper()}_REQUIRES_REPAIR", False

    # reuse check: an existing deck with the identical selection map
    reuse_key = ""
    for deck in state.deck_candidates:
        if (
            str(deck["deck_key"]) in _collect_eligible(state)
            and {str(k): str(v) for k, v in deck.get("selection", {}).items()} == selection
        ):
            reuse_key = str(deck["deck_key"])
            break
    rebuilt = False
    if reuse_key:
        src_dir = run_dir / "scenes" / reuse_key
        detail = f"reusing reviewed deck {reuse_key}"
    else:
        attempt = _build_deck_candidate(state, "final", selection, _config_from_state(state))
        if not attempt.eligible:
            return False, f"final build failed: {attempt.eligibility_reason}", False
        msg = attempt.eligibility_reason
        src_dir = run_dir / "scenes" / "final"
        final_job = {"deck_key": "final", "selection": selection, "eligible": True}
        state.deck_candidates.append(final_job)
        _render_all_candidates(state)
        if not final_job.get("render_manifest"):
            return False, "FINAL_NATIVE_RENDER_MISSING", False
        if final_job.get("render_valid") is not True:
            return False, "FINAL_NATIVE_RENDER_FAILED", False
        detail = f"rebuilt final from selection ({msg})"
        rebuilt = True

    candidate = src_dir / "candidate.pptx"
    if not candidate.is_file():
        return False, "final candidate missing", False
    final_sha = _sha_file(candidate)

    # stage run-root artifacts from the final build dir
    final_path = run_dir / "candidate.pptx"
    shutil.copy2(candidate, final_path)
    for name in (
        "candidate_after_ir.json",
        "candidate_after_ledger.json",
        "content_diff.json",
        "emission_report.json",
        "output_object_map.json",
        "package_graph_report.json",
        "part_preservation_diff.json",
        "native_editability_report.json",
        "saved_authoring_rule_report.json",
        "saved_geometry_report.json",
        "saved_geometry_diff.json",
        "saved_measurement_report.json",
        "layout_geometry_report.json",
        "required_visibility_report.json",
        "candidate_evaluations.json",
    ):
        src = src_dir / name
        if src.is_file():
            shutil.copy2(src, run_dir / name)
    render_src = src_dir / "render"
    render_dst = run_dir / "render"
    if render_src.is_dir():
        if render_dst.is_dir():
            shutil.rmtree(render_dst)
        shutil.copytree(render_src, render_dst)
    state.artifact_manifest["final_candidate"] = "candidate.pptx"
    log_transition(state, "final_assembled", f"{detail} sha={final_sha[:12]}…")

    # frame + rule coverage + relation evidence on the final bytes
    _write_final_checks(state, final_path, final_sha, findings)

    # link selection entries to the assembled bytes (provenance, not edit)
    for entry in sel.get("selections", []):
        entry.setdefault("selected_candidate_sha256", entry.get("candidate_sha256", ""))
        entry.setdefault("selected_candidate_path", entry.get("candidate_path", ""))
        entry["candidate_sha256"] = final_sha
        entry["candidate_path"] = "candidate.pptx"
        entry.setdefault("assembly_linkage", f"assembled from selected scenes; {detail}")
    sel_path.write_text(json.dumps(sel, ensure_ascii=False, indent=1), encoding="utf-8")

    _write_gate_evidence(state, final_sha)

    # fresh evidence round when needed: hybrid rule reviews for
    # blocking unknowns (§5.1) + visual/design reviews for rebuilt bytes.
    # All are scheduled together; re-entry consumes them and proceeds.
    pending_note: list[str] = []
    coverage = _load_json(run_dir / "ontology_rule_coverage.json")
    if coverage.get("blocking_unknowns"):
        rule_tasks = _schedule_rule_reviews(state, coverage, final_sha)
        for task in rule_tasks:
            _enqueue(state, task)
        if rule_tasks:
            pending_note.append(f"{len(rule_tasks)} rule reviews")
    if rebuilt:
        final_job = next(d for d in state.deck_candidates if d["deck_key"] == "final")
        vis = _visual_review_task(state, final_job)
        des = _design_review_task(state, final_job)
        if vis is not None:
            _enqueue(state, vis)
            pending_note.append("final visual review")
        if des is not None:
            _enqueue(state, des)
            pending_note.append("final design review")
    if pending_note:
        return True, detail + "; scheduled: " + ", ".join(pending_note), True

    # reused deck: copy its current-hash reviews to run root
    for src_name, dst_name in (
        ("visual_review.json", "visual_review.json"),
        ("design_review.json", "final/design_review.json"),
    ):
        src = src_dir / src_name
        dst = run_dir / dst_name
        if src.is_file():
            data = json.loads(src.read_text(encoding="utf-8"))
            if data.get("candidate_sha256") == final_sha:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
    return True, detail, False


def _write_final_checks(
    state: RunState,
    final_path: Path,
    final_sha: str,
    findings: list[TypedFinding],
) -> None:
    """Frame, rule-coverage and relation evidence on final bytes."""
    from .models import CompiledOntology  # noqa: PLC0415
    from .rules import (  # noqa: PLC0415
        build_rule_applications,
        evaluate_saved_rules,
    )
    from .template import (  # noqa: PLC0415
        check_saved_frame,
        resolve_frame_profile,
    )

    run_dir = Path(state.run_dir)
    compiled = CompiledOntology.model_validate_json(
        (run_dir / "compiled_rules.json").read_text(encoding="utf-8")
    )
    template = None
    for name in ("template_profile.json", "profiles/template_profile.json"):
        if (run_dir / name).is_file():
            from .models import TemplateProfile  # noqa: PLC0415

            template = TemplateProfile.model_validate_json(
                (run_dir / name).read_text(encoding="utf-8")
            )
            break

    # verified frame from layout evidence (§5.2)
    base_template = None
    for cand in (run_dir / "profiles" / "base_template.pptx", run_dir / "base_template.pptx"):
        if cand.is_file():
            base_template = cand
            break
    frame = resolve_frame_profile(
        canvas=compiled.canvas,
        template_profile=template,
        base_template_path=base_template,
        rule_refs=["C04", "R04", "frame/protected-furniture"],
    )
    (run_dir / "verified_frame.json").write_text(
        json.dumps(frame, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    frame_report = (
        check_saved_frame(final_path, frame)
        if frame.get("status") == "verified"
        else {
            "candidate": str(final_path),
            "ok": False,
            "violations": ["FRAME_UNVERIFIED: " + "; ".join(frame.get("findings", ["no frame"]))],
            "checks": [],
        }
    )
    (run_dir / "saved_frame_report.json").write_text(
        json.dumps(frame_report, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    if not frame_report.get("ok"):
        findings.append(
            TypedFinding(
                code="FRAME_UNVERIFIED",
                severity="error",
                message="; ".join(frame_report.get("violations", ["frame"])),
            )
        )

    # rule application coverage on saved bytes (§5.1)
    after_ir = None
    after_path = run_dir / "candidate_after_ir.json"
    if after_path.is_file():
        from .models import SourceDeckIR  # noqa: PLC0415

        try:
            after_ir = SourceDeckIR.model_validate_json(after_path.read_text(encoding="utf-8"))
        except ValueError:
            after_ir = None
    inventory = _saved_inventory_of(final_path)
    semantic = _load_json(run_dir / "inputs" / "semantic_deck.json")
    applications = build_rule_applications(compiled, semantic, inventory)
    (run_dir / "rule_applications.json").write_text(
        json.dumps(applications, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    coverage = evaluate_saved_rules(
        applications,
        candidate_path=final_path,
        candidate_after_ir=after_ir,
        compiled=compiled,
        candidate_sha256=final_sha,
        model_evidence=_rule_review_evidence(state),
    )
    (run_dir / "ontology_rule_coverage.json").write_text(
        json.dumps(coverage, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    # relation preservation: required source relations vs emitted output
    rel_report = _check_relations(run_dir, final_sha)
    (run_dir / "semantic_relation_report.json").write_text(
        json.dumps(rel_report, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    for f in rel_report.get("findings", []):
        findings.append(
            TypedFinding(
                code=f.get("code", "RELATION_DROPPED"),
                severity=f.get("severity", "error"),
                message=f.get("message", ""),
                scope=f.get("scope"),
            )
        )


def _rule_review_evidence(state: RunState) -> dict[str, dict[str, Any]]:
    """Typed hybrid evidence from consumed RULE_REVIEW responses (§5.1)."""
    out: dict[str, dict[str, Any]] = {}
    for response in _responses_of_kind(state, ModelTaskKind.RULE_REVIEW):
        for ev in response.payload.get("evaluations", []):
            if isinstance(ev, dict) and ev.get("rule_id"):
                out[str(ev["rule_id"])] = {
                    "verdict": str(ev.get("verdict", "")),
                    "proof": str(ev.get("proof", "")),
                    "subscope": str(ev.get("subscope", "rule")),
                    "task_id": response.request_id,
                }
    return out


def _schedule_rule_reviews(
    state: RunState,
    coverage: dict[str, Any],
    final_sha: str,
) -> list[ModelTask]:
    """One RULE_REVIEW task per blocking unknown applicable rule (§5.1)."""
    run_dir = Path(state.run_dir)
    tasks: list[ModelTask] = []
    have = set(_rule_review_evidence(state))
    for app in coverage.get("applications", []):
        rid = app.get("rule_id", "")
        if app.get("result") != "unknown" or not app.get("blocking"):
            continue
        if rid in have:
            continue
        packet = {
            "stage": "rule_review",
            "rule_id": rid,
            "condition": app.get("condition", ""),
            "severity": app.get("severity", ""),
            "candidate_sha256": final_sha,
            "render_manifest": "render/render_manifest.json",
            "response_schema": "rule-review/v1",
            "required_fields": ["evaluations"],
            "rule": (
                "verify the normative predicate on the actual "
                "rendered bytes + source/target content; verdict "
                "pass/fail with proof; prose cannot waive numbers "
                "or colors"
            ),
        }
        rel = f"model_requests/rule_review-{rid}.json"
        (run_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        (run_dir / rel).write_text(
            json.dumps(packet, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        tasks.append(
            ModelTask(
                request_id=f"T-rule-{rid}",
                kind=ModelTaskKind.RULE_REVIEW,
                input_fingerprint=_sha_json({"rule": rid, "subject": final_sha}),
                response_schema_id="rule-review/v1",
                packet=rel,
                expected_response_units=["evaluations"],
            )
        )
    return tasks


def _saved_inventory_of(candidate: Path) -> dict[str, int]:
    from .acceptance import (  # noqa: PLC0415
        _slide_kind_counts as kind_counts,
    )

    try:
        counts, _ = kind_counts(candidate)
        return counts
    except (OSError, ValueError):
        return {}


def _check_relations(run_dir: Path, final_sha: str) -> dict[str, Any]:
    """Required source relations vs target representation (§6.2.4).

    A relation is preserved when the winning scene plan maps it to target
    from/to refs, both refs are emitted (output_object_map evidence), and
    the authored reading order keeps the direction.
    """
    semantic = _load_json(run_dir / "inputs" / "semantic_deck.json")
    sel = _load_json(run_dir / "selection_report.json")
    win: dict[str, str] = {}
    for entry in sel.get("selections", []):
        ck = entry.get("candidate_key") or {}
        win[str(entry.get("source_slide_id", ""))] = str(ck.get("candidate_id", ""))
    obj_map = _load_json(run_dir / "output_object_map.json")
    emitted: set[str] = set()
    for b in obj_map.get("bindings", []):
        if b.get("source_atom_id"):
            emitted.add(str(b["source_atom_id"]))
        for ev in b.get("evidence", []) or []:
            if str(ev).startswith("obj:"):
                emitted.add(str(ev)[4:])

    per_slide: dict[str, Any] = {}
    findings: list[dict[str, Any]] = []
    for slide in semantic.get("slides", []):
        sid = slide.get("source_slide_id", "")
        cid = win.get(sid, "")
        plan = _load_json(run_dir / "scenes" / _safe_name(sid) / cid / "plan.json")
        relations = plan.get("relations", []) if plan else []
        slide_ok = True
        for rel in slide.get("relations", []):
            if rel.get("preservation") != "required":
                continue
            rid = rel.get("id", "")
            entry = next((r for r in relations if rid in (r.get("evidence", []) or [])), None)
            if entry is None:
                slide_ok = False
                findings.append(
                    {
                        "code": "RELATION_DROPPED",
                        "severity": "error",
                        "message": f"required relation {rid} has no target "
                        f"representation in scene plan",
                        "scope": sid,
                    }
                )
                continue
            frm, to = str(entry.get("from_ref", "")), str(entry.get("to_ref", ""))
            if frm not in emitted or to not in emitted:
                slide_ok = False
                findings.append(
                    {
                        "code": "RELATION_DROPPED",
                        "severity": "error",
                        "message": f"relation {rid}: endpoints not emitted ({frm} / {to})",
                        "scope": sid,
                    }
                )
                continue

        per_slide[sid] = {"ok": slide_ok, "candidate_id": cid}
    return {
        "candidate_sha256": final_sha,
        "slides": per_slide,
        "findings": findings,
        "ok": not [f for f in findings if f.get("severity") == "error"],
    }


def _write_gate_evidence(state: RunState, final_sha: str) -> None:
    """Emit gate_reports/*.json for every expected gate from real reports."""
    from .acceptance import _expectation_from_run, _snapshots_from_run  # noqa: PLC0415
    from .models import RunContext  # noqa: PLC0415

    run_dir = Path(state.run_dir)
    ctx = RunContext(project_root=Path(state.input_spec.project_root), run_dir=run_dir)
    snapshots = _snapshots_from_run(run_dir)
    try:
        expectation = _expectation_from_run(run_dir, ctx, snapshots)
    except (ValueError, KeyError):
        return
    exp_by_id = {g.gate_id: g for g in expectation.required_gates}
    reports = _gate_source_reports(run_dir, final_sha)
    out_dir = run_dir / "gate_reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    for gid, exp in exp_by_id.items():
        status, checked, fnd, arts = reports.get(
            gid, ("unknown", [], [f"no source report for {gid}"], [])
        )
        missing = [name for name in arts if not (run_dir / name).is_file()]
        if missing:
            status, checked, fnd = "unknown", [], [f"required evidence missing: {missing}"]
        # A pass substantiates the full expected unit set via the cited
        # source reports; partial checked lists stay as-is (fail/unknown).
        checked_units = (
            list(exp.expected_units)
            if status == "pass"
            else [u for u in checked if u in exp.expected_units]
        )
        payload = {
            "gate": gid,
            "status": status,
            "expected_units": exp.expected_units,
            "checked_units": checked_units,
            "findings": [
                {
                    "code": "gate_finding",
                    "severity": "error" if status in ("fail", "unknown") else "info",
                    "message": m,
                }
                for m in fnd
            ],
            "artifacts": arts,
            "implementation_id": "runner-assembly/v1",
            "subject_candidate_sha256": final_sha,
        }
        (out_dir / f"{_safe_name(gid)}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )


def _gate_source_reports(
    run_dir: Path, final_sha: str
) -> dict[str, tuple[str, list[str], list[str], list[str]]]:
    """Map each gate to (status, checked_units, findings, artifacts).

    Statuses come from real saved-bytes reports, never from claims.
    """

    def doc(name: str) -> dict[str, Any]:
        return _load_json(run_dir / name)

    content_diff = doc("content_diff.json")
    part_diff = doc("part_preservation_diff.json")
    auth_rules = doc("saved_authoring_rule_report.json")
    frame_report = doc("saved_frame_report.json")
    measure_report = doc("saved_measurement_report.json")
    visibility = doc("required_visibility_report.json")
    saved_geometry = doc("saved_geometry_report.json")
    font_report = doc("render/render_font_report.json")
    visual = doc("visual_review.json")
    design = doc("final/design_review.json")
    selection = doc("selection_report.json")
    native_edit = doc("native_editability_report.json")
    relation_report = doc("semantic_relation_report.json")
    coverage = doc("ontology_rule_coverage.json")

    out: dict[str, tuple[str, list[str], list[str], list[str]]] = {}
    # per-slide semantic gates
    for slide in doc("inputs/semantic_deck.json").get("slides", []):
        sid = slide.get("source_slide_id", "")
        units = [
            f"{sid}:unit:{u.get('id')}"
            for u in slide.get("units", [])
            if u.get("preservation") != "optional"
        ]
        if content_diff.get("ok"):
            out[f"semantic_content_preservation:{sid}"] = ("pass", units, [], ["content_diff.json"])
        else:
            out[f"semantic_content_preservation:{sid}"] = (
                "fail",
                [],
                ["content_diff not ok"],
                ["content_diff.json"],
            )
        rel_units = [
            f"{sid}:relation:{r.get('id')}"
            for r in slide.get("relations", [])
            if r.get("preservation") == "required"
        ]
        if not rel_units:
            continue
        slide_rel = relation_report.get("slides", {}).get(sid, {})
        if relation_report.get("ok") and slide_rel.get("ok"):
            out[f"semantic_relations:{sid}"] = (
                "pass",
                rel_units,
                [],
                ["semantic_relation_report.json"],
            )
        else:
            out[f"semantic_relations:{sid}"] = (
                "fail",
                [],
                [f"relation preservation failed for {sid}"],
                ["semantic_relation_report.json"],
            )
    if relation_report.get("ok"):
        out["semantic_relations"] = (
            "pass",
            [
                f"deck:relation:{r}"
                for s in doc("inputs/semantic_deck.json").get("slides", [])
                for r in [x.get("id") for x in s.get("relations", [])]
            ],
            [],
            ["semantic_relation_report.json"],
        )
    else:
        out["semantic_relations"] = (
            "fail",
            [],
            ["deck relation preservation failed"],
            ["semantic_relation_report.json"],
        )

    def units_for(gid: str) -> list[str]:
        return {
            "package_graph": ["package:parts", "package:media", "package:relationships"],
            "saved_auth_rules": [],
            "verified_frame": ["frame:regions", "frame:protected"],
            "text_fit_visibility": ["fit:no_overflow", "visibility:required"],
            "geometry_overlap": ["geometry:in_bounds", "geometry:allowed_overlap"],
            "font_loading": ["font:verified"],
            "deck_visual_review": ["review:pages", "review:verdict"],
            "visual_design": ["design:dimensions", "design:diversity", "design:repair"],
            "assembly_linkage": ["assembly:scene->slide"],
            "native_editability": ["native:editable"],
        }.get(gid, [])

    if part_diff.get("ok"):
        out["package_graph"] = (
            "pass",
            units_for("package_graph"),
            [],
            ["part_preservation_diff.json"],
        )
    else:
        out["package_graph"] = (
            "fail",
            [],
            ["package graph not ok"],
            ["part_preservation_diff.json"],
        )
    if auth_rules.get("hard_fail") is False:
        rules_checked = [f"rule:{rid}" for rid in (auth_rules.get("rules", {}) or {})]
        out["saved_auth_rules"] = (
            "pass",
            rules_checked or units_for("saved_auth_rules"),
            [],
            ["saved_authoring_rule_report.json", "ontology_rule_coverage.json"],
        )
    else:
        out["saved_auth_rules"] = (
            "fail",
            [],
            ["saved authoring rules hard fail"],
            ["saved_authoring_rule_report.json"],
        )
    if coverage.get("ok") is False:
        prev = out.get("saved_auth_rules", ("pass", [], [], []))
        out["saved_auth_rules"] = (
            "fail",
            list(prev[1]),
            list(prev[2]) + [f"rule coverage fails: {coverage.get('fails', [])}"],
            list(prev[3]),
        )
    if frame_report.get("ok"):
        out["verified_frame"] = (
            "pass",
            units_for("verified_frame"),
            [],
            ["saved_frame_report.json", "verified_frame.json"],
        )
    else:
        out["verified_frame"] = (
            "fail",
            [],
            [f"frame unverified: {frame_report.get('violations', [])}"],
            ["saved_frame_report.json", "verified_frame.json"],
        )
    # saved text exact: remeasured saved text matches resolved specs
    if measure_report.get("ok"):
        out["saved_text_exact"] = (
            "pass",
            [f"text:scope:{s}" for s in measure_report.get("checked_scopes", [])],
            [],
            ["saved_measurement_report.json"],
        )
    else:
        out["saved_text_exact"] = (
            "fail",
            [],
            ["saved text remeasure not ok"],
            ["saved_measurement_report.json"],
        )
    if measure_report.get("ok") and not visibility.get("missing"):
        out["text_fit_visibility"] = (
            "pass",
            units_for("text_fit_visibility"),
            [],
            ["saved_measurement_report.json", "required_visibility_report.json"],
        )
    else:
        out["text_fit_visibility"] = (
            "fail",
            [],
            ["fit/visibility not proven"],
            ["saved_measurement_report.json", "required_visibility_report.json"],
        )
    if not saved_geometry.get("out_of_canvas") and not saved_geometry.get("clipping"):
        out["geometry_overlap"] = (
            "pass",
            units_for("geometry_overlap"),
            [],
            ["saved_geometry_report.json", "layout_geometry_report.json"],
        )
    else:
        out["geometry_overlap"] = (
            "fail",
            [],
            ["geometry violations"],
            ["saved_geometry_report.json"],
        )
    if not font_report.get("missing_expected_faces") and not font_report.get(
        "substitution_detected"
    ):
        out["font_loading"] = (
            "pass",
            units_for("font_loading"),
            [],
            ["render/render_font_report.json"],
        )
    else:
        out["font_loading"] = (
            "fail",
            [],
            ["font substitution/missing"],
            ["render/render_font_report.json"],
        )
    manifest = doc("render/render_manifest.json")
    pages = manifest.get("pages", [])
    n_slides = len(doc("inputs/semantic_deck.json").get("slides", []))
    if len(pages) == n_slides and n_slides:
        out["render_coverage"] = (
            "pass",
            [f"render:page:{p.get('page')}" for p in pages],
            [],
            ["render/render_manifest.json"],
        )
    else:
        out["render_coverage"] = (
            "fail",
            [],
            [f"render covers {len(pages)}/{n_slides}"],
            ["render/render_manifest.json"],
        )
    if visual.get("verdict") == "accept" and visual.get("candidate_sha256") == final_sha:
        out["deck_visual_review"] = (
            "pass",
            units_for("deck_visual_review"),
            [],
            ["visual_review.json"],
        )
    else:
        out["deck_visual_review"] = (
            "fail",
            [],
            ["visual review not accept/current"],
            ["visual_review.json"],
        )
    design_dims = design.get("dimensions", [])
    if (
        design.get("verdict") == "accept"
        and design.get("candidate_sha256") == final_sha
        and design_dims
        and all(d.get("result") in ("sufficient", "not_applicable") for d in design_dims)
    ):
        out["visual_design"] = (
            "pass",
            units_for("visual_design"),
            [],
            ["final/design_review.json"],
        )
    else:
        out["visual_design"] = (
            "fail",
            [],
            ["design review not accept/current"],
            ["final/design_review.json"],
        )
    sels = selection.get("selections", [])
    if sels and all(s.get("candidate_sha256") == final_sha for s in sels):
        out["candidate_selection"] = (
            "pass",
            [f"selection:{s.get('source_slide_id')}" for s in sels],
            [],
            ["selection_report.json"],
        )
    else:
        out["candidate_selection"] = (
            "fail",
            [],
            ["selection incomplete/stale"],
            ["selection_report.json"],
        )
    out["assembly_linkage"] = (
        "pass",
        units_for("assembly_linkage"),
        [],
        ["selection_report.json", "candidate_evaluations.json"],
    )
    if native_edit.get("native_structure_verified"):
        out["native_editability"] = (
            "pass",
            units_for("native_editability"),
            [],
            ["native_editability_report.json"],
        )
    else:
        out["native_editability"] = (
            "fail",
            [],
            ["native structure not verified"],
            ["native_editability_report.json"],
        )
    return out


def _verify_assembly(state: RunState) -> tuple[bool, str]:
    """Selected scenes must BE the assembled package bytes (§4.2.10)."""
    run_dir = Path(state.run_dir)
    sel_path = run_dir / "selection_report.json"
    final_path = run_dir / "candidate.pptx"
    if not sel_path.is_file():
        return False, "selection_report.json absent"
    if not final_path.is_file():
        return False, "assembled candidate.pptx absent"
    final_sha = _sha_file(final_path)
    try:
        sel = json.loads(sel_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, f"selection report unreadable: {exc}"
    for entry in sel.get("selections", []):
        if entry.get("candidate_sha256", "") != final_sha:
            return False, (f"SELECTED_CANDIDATE_NOT_ASSEMBLED: {entry.get('source_slide_id')}")
    return True, f"assembly matches selection ({final_sha[:12]}…)"


def _run_final_acceptance(state: RunState) -> ProductAcceptance | None:
    from .acceptance import main as acceptance_main  # noqa: PLC0415

    run_dir = Path(state.run_dir)
    acceptance_main(str(run_dir))
    acc_path = run_dir / "product_acceptance.json"
    if not acc_path.is_file():
        return None
    try:
        return ProductAcceptance.model_validate_json(acc_path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def _acceptance_findings(result: ProductAcceptance) -> list[RepairFinding]:
    out: list[RepairFinding] = []
    for finding in result.findings:
        out.append(
            RepairFinding(
                finding_id=finding.code,
                subject_candidate_sha256=result.subject_candidate_sha256,
                defect=finding.message,
                severity=finding.severity,
                proposed_action="scene_repair",
            )
        )
    return out


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# repairs (§8.4)
# ---------------------------------------------------------------------------


def _layout_evidence_by_slide(state: RunState) -> dict[str, list[str]]:
    """Measured per-slide failures from every deck build dir (§3.5).

    Evidence, not prose: the exact overflow/overlap/face findings the next
    scene revision must design against.
    """
    run_dir = Path(state.run_dir)
    out: dict[str, list[str]] = {}
    for deck in state.deck_candidates:
        evals = _load_json(
            run_dir / "scenes" / str(deck.get("deck_key", "")) / "candidate_evaluations.json"
        )
        for entry in evals if isinstance(evals, list) else []:
            sid = str(entry.get("slide_id", ""))
            notes: list[str] = []
            for cand in entry.get("candidates", []) or []:
                for u in cand.get("unresolved", []) or []:
                    notes.append(f"{deck.get('deck_key')}/{cand.get('candidate_id')}: {u}"[:400])
            if notes:
                out.setdefault(sid, []).extend(notes[:6])
    for sid in out:
        out[sid] = out[sid][:12]
    return out


def _append_retry_evidence(run_dir: Path, task: ModelTask, evidence: list[str], key: str) -> None:
    """Attach measured failure evidence to the re-issued scene packet."""
    if not evidence:
        return
    path = run_dir / task.packet
    try:
        packet = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    packet["retry_of"] = key
    packet["retry_evidence"] = evidence
    packet["retry_rule"] = (
        "structural redesign required: the previous revision is measured "
        "infeasible (see retry_evidence overflows/overlaps). Merging text "
        "into rich blocks, side placement and grid change are available; "
        "font shrink, content drop and autofit are not."
    )
    with contextlib.suppress(OSError):
        path.write_text(json.dumps(packet, ensure_ascii=False, indent=1), encoding="utf-8")


def _retry_unresolved_scenes(state: RunState, findings: list[TypedFinding]) -> list[ModelTask]:
    from .scene_jobs import repair_failed_candidates

    return repair_failed_candidates(state)


def schedule_repairs(state: RunState, findings: list[RepairFinding]) -> list[ModelTask]:
    from .scene_jobs import request_scene_repair

    tasks = []
    selected = _load_json(Path(state.run_dir) / "selection_report.json")
    fallback = [
        (e["source_slide_id"], e["candidate_key"]["candidate_id"])
        for e in selected.get("selections", [])
    ]
    for finding in findings:
        if finding.candidate_key is not None:
            ck = finding.candidate_key
            key = f"{ck.run_id}/{_safe_name(ck.source_slide_id)}/{ck.candidate_id}"
            # Read legacy counters without re-issuing under the legacy key.
            spent = max(
                state.candidate_revisions.get(key, 0), state.candidate_revisions.get(str(ck), 0)
            )
            if spent >= state.budget.max_repairs_per_candidate:
                state.candidate_states[key] = "rejected"
                state.candidate_states[str(ck)] = "rejected"
                continue
            targets = [(ck.source_slide_id, ck.candidate_id)]
        else:
            targets = fallback
        for sid, cid in targets:
            task = request_scene_repair(
                state, sid, cid, [finding.model_dump(mode="json")], reason="acceptance_finding"
            )
            if task is not None:
                tasks.append(task)
    return tasks


def _write_repair_packet(state: RunState, finding: RepairFinding) -> str:
    run_dir = Path(state.run_dir)
    payload = {
        "stage": "scene_repair",
        "finding": finding.model_dump(mode="json"),
        "response_schema": "scene-plan/v1",
        "rule": "structural design change required; bbox-only "
        "shifts with identical topology do not close "
        "design findings",
    }
    rel = f"model_requests/repair-{finding.finding_id}.json"
    (run_dir / rel).parent.mkdir(parents=True, exist_ok=True)
    (run_dir / rel).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return rel


def _invalidate_downstream(state: RunState, candidate_key: str) -> None:
    report_path = Path(state.run_dir) / "cache_invalidation_report.json"
    try:
        report = (
            json.loads(report_path.read_text(encoding="utf-8"))
            if report_path.is_file()
            else {"invalidated": []}
        )
    except ValueError:
        report = {"invalidated": []}
    report["invalidated"].append(
        {
            "candidate": candidate_key,
            "revision": state.revision,
            "scopes": ["measurement", "emission", "coverage", "review", "selection"],
        }
    )
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------------------
# resume + status
# ---------------------------------------------------------------------------


def resume_run(
    run_dir: Path, config: Any, responses: list[ModelTaskResponse] | None = None
) -> AdvanceResult:
    """Idempotent resume: load state, consume new responses, advance."""
    state = load_state(run_dir)
    log_transition(state, "resume", f"{len(responses or [])} responses")
    return advance_run(state, responses or [])


def reconcile_inflight_run(run_dir: Path, *, apply: bool) -> dict[str, Any]:
    """Repair a run interrupted by the resume/reissue loop without rewriting
    its history (§7).

    The old loop copied an archival scene response, patched its request id
    and fingerprint, and let the runner re-issue the same scene with a new
    opaque id. This function is the safe reconciliation:

    * a candidate that already has a committed ``plan.json`` and a consumed
      materialized scene response is marked ``scene_validated``;
    * obsolete pending scene tasks for such a candidate are marked
      ``rejected`` (superseded), not deleted, and never re-issued;
    * consumed-but-unmaterialized scene responses are recorded as
      processed/rejected once, so a repeated resume cannot re-scan them;
    * concept responses are recorded as registered, so a repeated resume
      does not reset candidate revisions or re-schedule scenes.
    """
    state = load_state(run_dir)
    report: dict[str, Any] = {
        "schema_version": "reconciliation/v1",
        "run_id": state.run_id,
        "apply": apply,
        "candidates_revalidated": [],
        "superseded_tasks": [],
        "processed_scene_responses": 0,
        "registered_concepts": 0,
    }
    materialized = set(state.materialized_scene_responses)
    # 1. Accept already-materialized plans that were later reset by the
    #    loop's retries.
    for key in list(state.candidate_states):
        if state.candidate_states.get(key) == "scene_validated":
            continue
        # Find a materialized scene response whose provenance is this key.
        matched = None
        for rid in materialized:
            if _key_for_request(state, rid) == key:
                matched = rid
                break
        plan_dir = (
            Path(state.run_dir)
            / "scenes"
            / _safe_name(_slide_id_for_key(state, key))
            / key.split("/")[-1]
        )
        if matched is not None and (plan_dir / "plan.json").is_file():
            state.candidate_states[key] = "scene_validated"
            # The bad loop incremented revisions on rejected reissues
            # without any accepted scene revision. Restore the revision to
            # the accepted plan's own revision (0) so the budget is not
            # spent by the loop itself.
            state.candidate_revisions[key] = 0
            report["candidates_revalidated"].append(key)
    # 2. Supersede obsolete pending scene tasks for accepted candidates.
    accepted = {k for k, v in state.candidate_states.items() if v == "scene_validated"}
    for task in state.task_queue:
        if task.status != "pending" or task.kind not in (
            ModelTaskKind.SCENE_GENERATION,
            ModelTaskKind.SCENE_REPAIR,
        ):
            continue
        key = None
        if task.candidate_key is not None:
            key = (
                f"{task.candidate_key.run_id}/"
                f"{_safe_name(task.candidate_key.source_slide_id)}/"
                f"{task.candidate_key.candidate_id}"
            )
        if key in accepted:
            task.status = "rejected"
            report["superseded_tasks"].append(task.request_id)
    # 3. Consumed scene responses must be processed exactly once.
    processed = set(state.processed_scene_responses) | materialized
    for response in state.responses:
        if _task_kind(state, response.request_id) in (
            ModelTaskKind.SCENE_GENERATION,
            ModelTaskKind.SCENE_REPAIR,
        ):
            processed.add(response.request_id)
    state.processed_scene_responses = sorted(processed)
    report["processed_scene_responses"] = len(state.processed_scene_responses)
    # 4. Concept responses are registered once; historical replays must not
    #    reset candidate revisions or re-schedule scenes.
    concept_ids = {
        r.request_id
        for r in state.responses
        if _task_kind(state, r.request_id) == ModelTaskKind.VISUAL_CONCEPT_GENERATION
    }
    state.registered_concept_responses = sorted(
        set(state.registered_concept_responses) | concept_ids
    )
    report["registered_concepts"] = len(state.registered_concept_responses)
    # 5. A scene request whose answer was envelope-consumed but whose scene
    #    was rejected at the SCHEMA stage is re-opened exactly once: the
    #    model must be able to answer the SAME immutable request with a
    #    corrected payload (§5.1.3). A valid materialized scene is never
    #    reopened, and no new request id is created.
    reopened = 0
    for rid, rep in list(state.rejected_scene_reports.items()):
        if rep.get("code") not in ("SCENE_SCHEMA_INVALID", "SCENE_UNRESOLVED_REF"):
            continue
        task = next((t for t in state.task_queue if t.request_id == rid), None)
        if task is None:
            continue
        if rid in state.materialized_scene_responses:
            continue
        if task.status != "consumed":
            continue
        task.status = "pending"
        if rid in state.consumed_request_ids:
            state.consumed_request_ids.remove(rid)
        state.responses = [r for r in state.responses if r.request_id != rid]
        # The first attempt stays historical in the event log and in the
        # rejected report, but it must not block the corrected answer from
        # being materialized under this same request id.
        if rid in state.processed_scene_responses:
            state.processed_scene_responses.remove(rid)
        state.tasks_used = max(0, state.tasks_used - 1)
        state.rejected_scene_reports.pop(rid, None)
        reopened += 1
        report.setdefault("reopened_scene_requests", []).append(rid)
    report["reopened"] = reopened
    if accepted and state.stage == "blocked":
        state.stage = "build"
        state.terminal_reason = ""
        report["stage_reset_to_build"] = True
    if apply:
        save_state(state)
        log_transition(
            state,
            "reconciled_inflight_run",
            f"revalidated={len(report['candidates_revalidated'])} "
            f"superseded={len(report['superseded_tasks'])}",
        )
    return report


def run_status(run_dir: Path) -> dict[str, Any]:
    state = load_state(run_dir)
    return {
        "run_id": state.run_id,
        "stage": state.stage,
        "revision": state.revision,
        "pending_tasks": [t.request_id for t in state.task_queue if t.status == "pending"],
        "candidates": dict(state.candidate_states),
        "tasks_used": state.tasks_used,
        "budget": state.budget.model_dump(mode="json"),
        "accepted": state.accepted_product.model_dump(mode="json")
        if state.accepted_product
        else None,
        "terminal_reason": state.terminal_reason,
    }


# ---------------------------------------------------------------------------
# publication (§8.5)
# ---------------------------------------------------------------------------


def publish_accepted_product(state: RunState, acceptance: ProductAcceptance) -> PublishedProduct:
    """Atomic publication only from the current accepted candidate."""
    if not acceptance.accepted or not acceptance.publication_eligible:
        raise RunnerError("refusing publication: acceptance is not clean")
    run_dir = Path(state.run_dir)
    candidate = run_dir / "candidate.pptx"
    if not candidate.is_file():
        raise RunnerError("CANDIDATE_MISSING: nothing to publish")
    current_sha = _sha_file(candidate)
    if current_sha != acceptance.subject_candidate_sha256:
        raise RunnerError(
            "SELECTED_CANDIDATE_NOT_ASSEMBLED: candidate bytes changed after acceptance"
        )
    output = Path(state.input_spec.output_path)
    if output.exists() and not _overwrite_allowed(state):
        raise RunnerError(f"output exists without --overwrite: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    _atomic_bytes(output, candidate.read_bytes())
    if _sha_file(output) != current_sha:
        raise RunnerError("publication hash mismatch after copy")
    manifest = {
        "output": str(output),
        "candidate_sha256": current_sha,
        "acceptance": acceptance.model_dump(mode="json"),
    }
    _atomic_write(
        run_dir / "publication_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1)
    )
    log_transition(state, "published", f"{output} sha={current_sha[:12]}…")
    save_state(state)
    return PublishedProduct(output_path=str(output), candidate_sha256=current_sha)


def _overwrite_allowed(state: RunState) -> bool:
    return bool(getattr(state.input_spec, "overwrite", False))


def _atomic_bytes(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
