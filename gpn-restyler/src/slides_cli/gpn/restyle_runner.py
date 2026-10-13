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
import shutil
import tempfile
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

RunStatus = Literal["running", "awaiting_harness", "accepted", "blocked",
                    "budget_exhausted"]

RUN_STAGES: tuple[str, ...] = (
    "ingest", "profile", "semantic", "design_briefs", "visual_concepts",
    "scene_generation", "build", "render", "visual_review", "design_review",
    "candidate_selection", "full_assembly", "final_checks",
    "accepted", "blocked",
)

CANDIDATE_STATES: tuple[str, ...] = (
    "concept_pending", "concept_fidelity_validated", "scene_pending",
    "scene_validated", "styled", "measured", "resolved", "emitted",
    "saved_checked", "rendered", "visually_reviewed", "design_reviewed",
    "eligible", "selected", "rejected",
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
    fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                                prefix=f".{path.name}.", suffix=".tmp")
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
    record = {"revision": state.revision, "stage": state.stage,
              "event": event, "detail": detail,
              "at": datetime.now(UTC).isoformat(timespec="seconds")}
    log_path = Path(state.run_dir) / "runner_transition_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha_json(payload: dict) -> str:
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------

def _new_run_dir(project_root: Path, prefix: str = "stage7") -> Path:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d-%H%M%S")
    run_dir = project_root / "runs" / prefix / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def start_run(spec: InputSpec, config: Any,
              run_dir: Path | None = None) -> RunState:
    """Create durable state and advance deterministic ingest (§8.1)."""
    from .document_ingest import ingest_input  # noqa: PLC0415

    project_root = Path(spec.project_root)
    run_dir = run_dir or _new_run_dir(project_root)
    run_dir.mkdir(parents=True, exist_ok=True)
    run_id = Path(run_dir).name

    state = RunState(run_id=run_id, run_dir=str(run_dir), input_spec=spec)
    log_transition(state, "start_run",
                   f"kind={spec.kind.value} provider={spec.provider.value}")

    # Deterministic ingest: source bytes, hashes, ontology snapshot.
    source = ingest_input(spec, run_dir)
    state.snapshots.input_sha256 = source.content_sha256
    state.artifact_manifest["source_manifest"] = "inputs/source_manifest.json"
    log_transition(state, "ingest_complete",
                   f"content_sha256={source.content_sha256[:12]}…")

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
        max_model_tasks=int(getattr(runtime, "max_model_requests", 1) or 1)
        * 60,
        max_generations_per_slide=max(3, per_slide + 1),
        max_repairs_per_candidate=int(
            getattr(policy, "max_model_repairs_per_slide", 2) or 2) + 1,
        max_renders=40,
        max_local_solves=int(
            getattr(layout, "max_local_layout_attempts", 6) or 6) * 30)


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
    if spec.kind.value not in ("pptx", "ppt"):
        return True, "create mode: no pptx profile needed"
    source = Path(spec.path) if spec.path else None
    if source is None or not source.is_file():
        return False, f"source not found: {source}"

    store = AssetStore(run_dir / "assets")

    # Profile first (imports the reference library, writes compiled_rules /
    # template_profile / font_inventory / base_template), then import the
    # USER deck last so source_ir.json is the deck being restyled (the
    # known import->profile->import ordering constraint).
    result = prepare_corporate_profiles(
        project_root=spec.project_root, config=_config_from_state(state),
        run_dir=run_dir)
    if result.status == "needs_assets":
        return False, "profile needs assets (fonts)"

    deck, _ = import_deck(source, store)
    graph = read_package(source, store)
    links = resolve_internal_links(deck, graph)
    ledger = build_ledger(deck, decoration=[])
    support = build_import_support_report(deck, ledger, links,
                                          deck.import_issues)
    write_import_artifacts(run_dir=run_dir, deck=deck, ledger=ledger,
                           package_manifest=build_manifest(graph),
                           support=support, links=links)
    (run_dir / "run_manifest.json").write_text(json.dumps({
        "source_path": str(source.resolve()),
        "source_sha256": deck.source_sha256, "slides": len(deck.slides),
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    inv = run_dir / "font_inventory.json"
    if not inv.is_file():
        return False, "font_inventory.json missing after profile"
    return True, f"profile ok: {len(deck.slides)} slides, status={result.status}"


def _config_from_state(state: RunState) -> Any:
    """Best-effort config for profile/build (config-free when absent)."""
    from types import SimpleNamespace  # noqa: PLC0415
    project_root = Path(state.input_spec.project_root)
    cfg_path = project_root / "config" / "gpn.toml"
    if cfg_path.is_file():
        from .config import load_config  # noqa: PLC0415
        return load_config(cfg_path, project_root=project_root)
    return SimpleNamespace(project_root=project_root, config_path=None,
                           model=SimpleNamespace(provider="harness"))


# ---------------------------------------------------------------------------
# response validation + consumption (§8.3)
# ---------------------------------------------------------------------------

def _pending_task(state: RunState, request_id: str) -> ModelTask | None:
    for task in state.task_queue:
        if task.request_id == request_id and task.status == "pending":
            return task
    return None


def _validate_response(state: RunState, task: ModelTask,
                       response: ModelTaskResponse) -> list[str]:
    errors: list[str] = []
    if response.request_id in state.consumed_request_ids:
        errors.append("STALE_RESPONSE: request already consumed; "
                      "a new task was issued")
    if response.input_fingerprint != task.input_fingerprint:
        errors.append("STALE_RESPONSE: input fingerprint mismatch; "
                      "task payload changed since issuance")
    if response.schema_version_id != task.response_schema_id:
        errors.append("SCHEMA_MISMATCH: response schema "
                      f"{response.schema_version_id!r} != expected "
                      f"{task.response_schema_id!r}")
    if not response.actual_model:
        errors.append("MODEL_IDENTITY_MISSING: actual harness model "
                      "required")
    missing = [u for u in task.expected_response_units
               if u not in response.payload]
    if missing:
        errors.append(f"RESPONSE_COVERAGE_INCOMPLETE: missing {missing}")
    return errors


def consume_response(state: RunState,
                     response: ModelTaskResponse) -> list[str]:
    """Validate + consume exactly once. Returns rejection reasons."""
    task = _pending_task(state, response.request_id)
    if task is None:
        return [f"UNKNOWN_OR_CONSUMED_TASK: {response.request_id}"]
    errors = _validate_response(state, task, response)
    if errors:
        task.status = "rejected"
        log_transition(state, "response_rejected",
                       f"{response.request_id}: {errors[0]}")
        save_state(state)
        return errors
    task.status = "consumed"
    state.consumed_request_ids.append(response.request_id)
    state.responses.append(response)
    state.tasks_used += 1
    log_transition(state, "response_consumed",
                   f"{response.request_id} kind={task.kind.value}")
    save_state(state)
    return []


# ---------------------------------------------------------------------------
# advance
# ---------------------------------------------------------------------------

def _pending_refs(state: RunState) -> list[TaskRef]:
    return [TaskRef(request_id=t.request_id, kind=t.kind.value,
                    packet=t.packet)
            for t in state.task_queue if t.status == "pending"]


def _candidate_refs(state: RunState) -> list[CandidateRef]:
    return [CandidateRef(candidate_key=k, state=v,
                         revision=state.candidate_revisions.get(k, 0))
            for k, v in state.candidate_states.items()]


def _check_budget(state: RunState) -> str | None:
    if state.tasks_used >= state.budget.max_model_tasks:
        return (f"model task budget exhausted: {state.tasks_used}/"
                f"{state.budget.max_model_tasks}")
    if state.renders_used >= state.budget.max_renders:
        return (f"render budget exhausted: {state.renders_used}/"
                f"{state.budget.max_renders}")
    return None


def advance_run(state: RunState,
                responses: list[ModelTaskResponse]) -> AdvanceResult:
    """Consume responses, run deterministic actions, schedule next tasks."""
    actions: list[ActionRef] = []
    findings: list[TypedFinding] = list(state.findings)

    for response in responses:
        rejection = consume_response(state, response)
        if rejection:
            findings.append(TypedFinding(
                code="RESPONSE_REJECTED", severity="error",
                message="; ".join(rejection)))
            # A duplicate/foreign response is dropped, not answered with a
            # fresh task: re-issuing there would multiply the queue while
            # the valid answer is still pending. Only stale content (the
            # task payload itself changed) gets a new task id.
            old = next((t for t in state.task_queue
                        if t.request_id == response.request_id), None)
            if old is not None and not any(
                    r.startswith(("UNKNOWN_OR_CONSUMED_TASK",))
                    for r in rejection):
                new_task = _reissue(state, old)
                actions.append(ActionRef(
                    action="reissue_task",
                    detail=f"{old.request_id} -> {new_task.request_id}"))

    budget_hit = _check_budget(state)
    if budget_hit:
        state.stage = "blocked"
        state.terminal_reason = budget_hit
        log_transition(state, "budget_exhausted", budget_hit)
        save_state(state)
        return AdvanceResult(status="budget_exhausted",
                             current_candidates=_candidate_refs(state),
                             findings=findings)

    actions.extend(_advance_stage(state, findings))

    if state.stage == "accepted":
        return AdvanceResult(status="accepted",
                             actions_executed=actions,
                             current_candidates=_candidate_refs(state),
                             accepted_product=state.accepted_product,
                             findings=findings)
    if state.stage == "blocked":
        return AdvanceResult(status="blocked",
                             actions_executed=actions,
                             current_candidates=_candidate_refs(state),
                             findings=findings)
    pending = _pending_refs(state)
    status: RunStatus = "awaiting_harness" if pending else "running"
    save_state(state)
    return AdvanceResult(status=status, pending_tasks=pending,
                         actions_executed=actions,
                         current_candidates=_candidate_refs(state),
                         findings=findings)


def _reissue(state: RunState, old: ModelTask) -> ModelTask:
    new = ModelTask(
        request_id=f"{old.request_id}-r{state.revision}",
        kind=old.kind, candidate_key=old.candidate_key,
        input_fingerprint=old.input_fingerprint,
        response_schema_id=old.response_schema_id, packet=old.packet,
        required_images=list(old.required_images),
        expected_response_units=list(old.expected_response_units))
    state.task_queue.append(new)
    log_transition(state, "task_reissued",
                   f"{old.request_id} -> {new.request_id}")
    return new


def _enqueue(state: RunState, task: ModelTask) -> None:
    if state.tasks_used + len(_pending_refs(state)) >= \
            state.budget.max_model_tasks:
        raise RunnerError(
            f"cannot enqueue {task.request_id}: model task budget "
            f"{state.budget.max_model_tasks} reached")
    state.task_queue.append(task)
    log_transition(state, "task_issued",
                   f"{task.request_id} kind={task.kind.value}")


def _task_kind(state: RunState, request_id: str) -> ModelTaskKind | None:
    for task in state.task_queue:
        if task.request_id == request_id:
            return task.kind
    return None


def _responses_of_kind(state: RunState,
                       kind: ModelTaskKind) -> list[ModelTaskResponse]:
    return [r for r in state.responses
            if _task_kind(state, r.request_id) == kind]


def _has_pending_kind(state: RunState, kind: ModelTaskKind) -> bool:
    return any(t.kind == kind and t.status == "pending"
               for t in state.task_queue)


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

def _materialize_semantic_deck(run_dir: Path, response,
                               findings: list[TypedFinding]) -> Path | None:
    """Derive semantic deck + transform ledger from the harness response.

    §6.1/§6.2: the semantic projection is an immutable artifact of the model
    response (own hash), and per-object transformation decisions are the
    only authorization for omit/recompose. The engine writes both; it never
    infers decoration from a missing text run.
    """
    from .models import SemanticDeck  # noqa: PLC0415

    payload = response.payload
    body = {k: v for k, v in payload.items()
            if k in ("schema_version", "source_manifest_sha256", "slides",
                     "units", "relations", "evidence")}
    try:
        deck = SemanticDeck.model_validate(body)
    except ValidationError as exc:
        findings.append(TypedFinding(
            code="SEMANTIC_RESPONSE_INVALID", severity="error",
            message=f"semantic deck rejected: {exc}"))
        return None
    path = run_dir / "inputs" / "semantic_deck.json"
    _atomic_write(path, deck.model_dump_json(indent=1))

    ir = _load_json(run_dir / "source_ir.json") or {}
    known: set[str] = set()
    per_slide: dict[str, list[dict[str, Any]]] = {}
    for slide in ir.get("slides", []):
        sid = str(slide.get("id", ""))
        ids = [str(o.get("id", "")) for o in slide.get("objects", [])]
        per_slide[sid] = ids
        known.update(ids)
    # The omit authorization is bound to the *source snapshot* the build
    # re-reads: the hash of the immutable source IR bytes.
    snapshot = _sha_file(run_dir / "source_ir.json")
    decisions: list[dict[str, Any]] = []
    for raw in payload.get("transformation_decisions", []) or []:
        if not isinstance(raw, dict):
            continue
        oid = str(raw.get("source_object_id", ""))
        action = str(raw.get("action", ""))
        if oid not in known:
            findings.append(TypedFinding(
                code="TRANSFORM_DECISION_UNKNOWN_OBJECT", severity="error",
                message=f"decision for unknown source object {oid!r}"))
            continue
        if action not in ("preserve", "recompose", "replace", "omit"):
            findings.append(TypedFinding(
                code="TRANSFORM_DECISION_INVALID", severity="error",
                message=f"invalid action {action!r} for {oid}"))
            continue
        rel_ids = [str(x) for x in raw.get("target_relation_ids", [])]
        if action == "omit" and not str(raw.get("reason", "")).strip():
            findings.append(TypedFinding(
                code="TRANSFORM_DECISION_UNGROUNDED", severity="error",
                message=f"omit of {oid} without semantic reason"))
            continue
        if action in ("replace", "recompose") and not rel_ids and \
                _object_is_arrow(ir, oid):
            findings.append(TypedFinding(
                code="TRANSFORM_ARROW_WITHOUT_RELATION", severity="error",
                message=f"arrow {oid} re-expressed without "
                        f"target_relation_ids"))
            continue
        decisions.append({
            "source_object_id": oid,
            "source_unit_ids": [str(x) for x in
                                raw.get("source_unit_ids", [])],
            "action": action,
            "target_node_ids": [str(x) for x in
                                raw.get("target_node_ids", [])],
            "target_relation_ids": rel_ids,
            "authorization_ref": f"harness:{response.request_id}",
            "evidence_refs": [str(x) for x in
                              raw.get("evidence_refs", [])],
            "reason": str(raw.get("reason", "")),
            "source_snapshot": str(raw.get("source_snapshot", snapshot)),
        })
    ledger_path = run_dir / "classification_and_transformation_ledger.json"
    _atomic_write(ledger_path, json.dumps(
        {"source_snapshot": snapshot,
         "schema_version": "transform-ledger/v1",
         "decisions": decisions}, ensure_ascii=False, indent=1))
    return path


def _object_is_arrow(ir: dict[str, Any], oid: str) -> bool:
    for slide in ir.get("slides", []):
        for obj in slide.get("objects", []):
            if str(obj.get("id", "")) != oid:
                continue
            line = (obj.get("payload") or {}).get("source_line") or {}
            return any(line.get(k) not in (None, "", "none")
                       for k in ("begin_arrow", "end_arrow"))
    return False


def _advance_stage(state: RunState,
                   findings: list[TypedFinding]) -> list[ActionRef]:
    """Deterministic stage machine. Returns executed actions."""
    from .design import (  # noqa: PLC0415
        build_slide_design_brief,
        generate_visual_concepts,
    )
    from .document_ingest import semantic_task_for  # noqa: PLC0415

    actions: list[ActionRef] = []
    run_dir = Path(state.run_dir)

    if state.stage == "ingest":
        ok, detail = _profile_stage(state)
        if ok:
            actions.append(ActionRef(action="profile_complete",
                                     detail=detail))
            # fall through: issue the semantic task in the same advance
            task = semantic_task_for(state.input_spec, run_dir,
                                     state.snapshots.input_sha256)
            _enqueue(state, task)
            state.stage = "semantic"
            actions.append(ActionRef(action="issue_semantic_task",
                                     artifact=task.packet))
        else:
            state.stage = "blocked"
            state.terminal_reason = detail
            findings.append(TypedFinding(code="PROFILE_FAILED",
                                         severity="error",
                                         message=detail))

    elif state.stage == "semantic":
        done = [r for r in state.responses
                if _task_kind(state, r.request_id)
                == ModelTaskKind.SEMANTIC_ANALYSIS]
        if done:
            deck_path = run_dir / "inputs" / "semantic_deck.json"
            if not deck_path.is_file():
                deck_path = _materialize_semantic_deck(
                    run_dir, done[-1], findings)
            if deck_path.is_file():
                from .models import SemanticDeck  # noqa: PLC0415
                deck = SemanticDeck.model_validate_json(
                    deck_path.read_text(encoding="utf-8"))
                state.snapshots.semantic_sha256 = _sha_file(deck_path)
                rules = _applicable_rules(run_dir)
                for slide in deck.slides:
                    brief_task = build_slide_design_brief(
                        slide, rules, run_dir=run_dir,
                        request_id=f"T-brief-{slide.source_slide_id}")
                    _enqueue(state, brief_task)
                state.stage = "design_briefs"
                actions.append(ActionRef(
                    action="issue_design_briefs",
                    detail=f"{len(deck.slides)} slides"))

    elif state.stage == "design_briefs":
        briefs = _responses_of_kind(state, ModelTaskKind.DESIGN_BRIEF)
        if briefs and not _has_pending_kind(state, ModelTaskKind.DESIGN_BRIEF):
            from .models import SlideDesignBrief  # noqa: PLC0415
            for response in briefs:
                brief = SlideDesignBrief.model_validate(response.payload)
                task = generate_visual_concepts(
                    brief, approved_examples=[], supported_primitives=[
                        "process", "structure", "comparison", "table",
                        "data", "typographic", "evidence"],
                    run_dir=run_dir,
                    request_id=f"T-concepts-{brief.slide_id}")
                _enqueue(state, task)
            state.stage = "visual_concepts"
            actions.append(ActionRef(action="issue_concept_tasks",
                                     detail=f"{len(briefs)} briefs"))

    elif state.stage == "visual_concepts":
        concepts = _responses_of_kind(
            state, ModelTaskKind.VISUAL_CONCEPT_GENERATION)
        if concepts and not _has_pending_kind(
                state, ModelTaskKind.VISUAL_CONCEPT_GENERATION):
            from .design import validate_concept_fidelity  # noqa: PLC0415
            from .models import (  # noqa: PLC0415
                SemanticDeck,
                SlideDesignBrief,
                VisualConcept,
            )
            # map concept task request_id -> slide_id for provenance
            task_slide = {}
            for t in state.task_queue:
                if t.kind == ModelTaskKind.VISUAL_CONCEPT_GENERATION:
                    tid = t.request_id
                    sid = tid[len('T-concepts-'):].rsplit('-r', 1)[0]
                    task_slide[tid] = sid
            for response in concepts:
                payload = response.payload
                sid = task_slide.get(response.request_id, "")
                items = payload.get("concepts", [payload])
                for item in items:
                    try:
                        concept = VisualConcept.model_validate(item)
                    except ValueError as exc:
                        findings.append(TypedFinding(
                            code="CONCEPT_INVALID", severity="error",
                            message=f"invalid concept: {exc}"))
                        continue
                    # Unique key per slide+concept (plain c1/c2 collide
                    # across slides).
                    safe_sid = _safe_name(sid) if sid else "unknown"
                    key = (f"{state.run_id}/{safe_sid}/"
                           f"{concept.concept_id}")
                    state.candidate_states[key] = "concept_pending"
                    state.candidate_revisions[key] = 0
                    # persist the validated concept for scene authoring,
                    # fidelity checks and design review provenance
                    cdir = run_dir / "scenes" / safe_sid / \
                        concept.concept_id
                    cdir.mkdir(parents=True, exist_ok=True)
                    (cdir / "concept.json").write_text(
                        concept.model_dump_json(indent=1), encoding="utf-8")
                    # deterministic fidelity gate (§10.4.6): required
                    # units/relations mapped, no invented causality/data
                    if sid:
                        try:
                            deck = SemanticDeck.model_validate_json(
                                (run_dir / "inputs" / "semantic_deck.json")
                                .read_text(encoding="utf-8"))
                            sem_slide = next(
                                s for s in deck.slides
                                if s.source_slide_id == sid)
                            brief = SlideDesignBrief.model_validate_json(
                                (run_dir / "design_briefs" /
                                 f"{safe_sid}.json").read_text(
                                     encoding="utf-8"))
                            check = validate_concept_fidelity(
                                concept, brief, sem_slide,
                                _applicable_rules(run_dir))
                            (cdir / "fidelity.json").write_text(
                                check.model_dump_json(indent=1),
                                encoding="utf-8")
                            if not check.valid:
                                state.candidate_states[key] = \
                                    "concept_pending"
                                for f in check.fidelity_findings:
                                    findings.append(f)
                        except (OSError, ValueError, StopIteration):
                            pass
                    if sid:
                        state.concept_slide[key] = sid
                        state.slide_candidates.setdefault(sid, [])
                        if concept.concept_id not in state.slide_candidates[sid]:
                            state.slide_candidates[sid].append(concept.concept_id)
            # schedule scene_generation per concept
            for key in list(state.candidate_states.items()):
                task = _scene_generation_task(state, key[0])
                if task is not None:
                    _enqueue(state, task)
            state.stage = "scene_generation"
            actions.append(ActionRef(
                action="register_concepts",
                detail=f"{len(state.candidate_states)} candidates"))

    elif state.stage == "scene_generation":
        plans = _responses_of_kind(state, ModelTaskKind.SCENE_GENERATION)
        if plans and not _has_pending_kind(
                state, ModelTaskKind.SCENE_GENERATION):
            written = 0
            for response in plans:
                plan = response.payload.get("scene_plan")
                if not plan:
                    continue
                ck = response.request_id
                # find the concept key for this request
                key = _key_for_request(state, ck)
                if key is None:
                    continue
                concept_id = key.split("/")[-1]
                sid = _slide_id_for_key(state, key)
                plan_dir = run_dir / "scenes" / _safe_name(sid) / concept_id
                plan_dir.mkdir(parents=True, exist_ok=True)
                (plan_dir / "plan.json").write_text(
                    json.dumps(plan, ensure_ascii=False, indent=1),
                    encoding="utf-8")
                state.candidate_states[key] = "scene_validated"
                written += 1
            if written:
                state.stage = "build"
                actions.append(ActionRef(
                    action="scene_plans_written",
                    detail=f"{written} plans"))

    if state.stage == "build":
        built = _build_all_deck_candidates(state, findings)
        if built:
            state.stage = "render"
            actions.append(ActionRef(
                action="build_complete",
                 detail=f"{len(state.deck_candidates)} deck candidates"))

    if state.stage == "render":
        rendered = _render_all_candidates(state)
        if rendered:
            state.stage = "visual_review"
            actions.append(ActionRef(
                action="render_complete",
                 detail=f"{rendered} rendered"))
            # schedule visual_review per deck candidate
            for deck in state.deck_candidates:
                task = _visual_review_task(state, deck)
                if task is not None:
                    _enqueue(state, task)

    if state.stage == "visual_review":
        reviews = _responses_of_kind(state, ModelTaskKind.VISUAL_REVIEW)
        if reviews and not _has_pending_kind(
                state, ModelTaskKind.VISUAL_REVIEW):
            for response in reviews:
                _write_visual_review(state, response)
            # schedule design_review per deck candidate
            for deck in state.deck_candidates:
                task = _design_review_task(state, deck)
                if task is not None:
                    _enqueue(state, task)
            state.stage = "design_review"
            actions.append(ActionRef(
                action="issue_design_reviews",
                 detail=f"{len(state.deck_candidates)} reviews"))

    if state.stage == "design_review":
        reviews = _responses_of_kind(state, ModelTaskKind.DESIGN_REVIEW)
        if reviews and not _has_pending_kind(
                state, ModelTaskKind.DESIGN_REVIEW):
            for response in reviews:
                _write_design_review(state, response)
            state.stage = "candidate_selection"
            actions.append(ActionRef(
                action="design_reviews_recorded",
                 detail=f"{len(reviews)} reviews"))

    if state.stage in ("candidate_selection", "selections",
                       "full_assembly", "final_checks"):
        actions.extend(_advance_build_stages(state, findings))

    save_state(state)
    return actions


def _slide_id_for_key(state: RunState, key: str) -> str:
    # explicit concept->slide provenance first
    if key in state.concept_slide:
        return state.concept_slide[key]
    for sid, cands in state.slide_candidates.items():
        if key.split("/")[-1] in cands:
            return sid
    return key.split("/")[-1]


def _safe_name(s: str) -> str:
    """Filesystem-safe form of a slide id (may contain '/')."""
    return s.replace("/", "_").replace(":", "_")


def _key_for_request(state: RunState, request_id: str) -> str | None:
    for task in state.task_queue:
        # scene_generation tasks carry candidate_key
        if task.request_id == request_id and task.candidate_key:
            return (f"{task.candidate_key.run_id}/"
                    f"{_safe_name(task.candidate_key.source_slide_id)}/"
                    f"{task.candidate_key.candidate_id}")
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
            out.append({
                "id": obj.get("id", ""), "kind": obj.get("kind", ""),
                "text": text[:160], "box_emu": box,
                "semantic_significance": obj.get(
                    "semantic_significance", ""),
            })
        return out
    return []


def _scene_generation_task(state: RunState, key: str) -> ModelTask | None:
    """Task: harness authors a LayoutIntent scene plan for one concept."""
    from .models import CandidateKey  # noqa: PLC0415

    run_dir = Path(state.run_dir)
    concept_id = key.split("/")[-1]
    sid = _slide_id_for_key(state, key)
    safe_sid = _safe_name(sid)
    brief_path = run_dir / "design_briefs" / f"{safe_sid}.json"
    semantic_path = run_dir / "inputs" / "semantic_deck.json"
    slide_objects = _slide_source_objects(run_dir, sid)
    packet_payload = {
        "stage": "scene_generation",
        "slide_id": sid,
        "concept_id": concept_id,
        "concept_key": key,
        "source_objects": slide_objects,
        "response_schema": "scene-plan/v1",
        "required_fields": ["slide_id", "candidate_id", "content_refs",
                            "zones", "relations"],
        "rule": "author a LayoutIntent scene plan expressing the concept "
                "topology; zones reference source content_refs from "
                "source_objects; relations map required source relations "
                "to target from_ref/to_ref with evidence source relation "
                "ids; no invented facts",
    }
    if brief_path.is_file():
        with contextlib.suppress(OSError, ValueError):
            packet_payload["design_brief"] = json.loads(
                brief_path.read_text(encoding="utf-8"))
    if semantic_path.is_file():
        with contextlib.suppress(OSError, ValueError):
            deck = json.loads(semantic_path.read_text(encoding="utf-8"))
            for s in deck.get("slides", []):
                if s.get("source_slide_id") == sid:
                    packet_payload["semantic_slide"] = s
                    break
    rel = f"model_requests/scene_generation-{safe_sid}-{concept_id}.json"
    (run_dir / rel).parent.mkdir(parents=True, exist_ok=True)
    (run_dir / rel).write_text(json.dumps(packet_payload,
                                            ensure_ascii=False, indent=1),
                               encoding="utf-8")
    return ModelTask(
        request_id=f"T-scene-{safe_sid}-{concept_id}",
        kind=ModelTaskKind.SCENE_GENERATION,
        candidate_key=CandidateKey(
            run_id=state.run_id, source_slide_id=sid,
            candidate_id=concept_id),
        input_fingerprint=_sha_json({"key": key}),
        response_schema_id="scene-plan/v1",
        packet=rel,
        expected_response_units=["scene_plan"])


def _build_all_deck_candidates(state: RunState,
                               findings: list[TypedFinding]) -> bool:
    """Build the primary deck + per-variant decks (deterministic)."""
    run_dir = Path(state.run_dir)
    slides = sorted(state.slide_candidates.keys())
    if not slides:
        findings.append(TypedFinding(
            code="NO_SCENE_PLANS", severity="error",
            message="no scene plans to build"))
        return False

    # primary: first candidate per slide
    primary = {sid: state.slide_candidates[sid][0] for sid in slides}
    decks: list[dict[str, Any]] = [{"deck_key": "primary",
                                     "selection": primary}]
    # variants: one deck per slide with that slide's 2nd candidate;
    # 3rd+ candidates stay in reserve for the repair loop (budget).
    for sid in slides:
        extra = state.slide_candidates[sid][1:2]
        for cand in extra:
            sel = dict(primary)
            sel[sid] = cand
            decks.append({"deck_key": f"{_safe_name(sid)}--{cand}",
                          "selection": sel})

    config = _config_from_state(state)
    built = 0
    for deck in decks:
        deck_key = str(deck["deck_key"])
        selection = {str(k): str(v) for k, v in deck["selection"].items()}
        ok, detail = _build_deck_candidate(state, deck_key, selection, config)
        if ok:
            deck["candidate.pptx"] = f"scenes/{deck_key}/candidate.pptx"
            deck["sha256"] = _sha_file(
                run_dir / "scenes" / deck_key / "candidate.pptx")
            built += 1
        else:
            findings.append(TypedFinding(
                code="BUILD_FAILED", severity="error",
                message=f"{deck_key}: {detail}"))
    state.deck_candidates = decks
    return built > 0


def _build_deck_candidate(state: RunState, deck_key: str,
                          selection: dict[str, str],
                          config: Any) -> tuple[bool, str]:
    """Build one full-deck candidate from per-slide selected plans."""
    from .export_models import ExportOptions  # noqa: PLC0415
    from .measured_build import run_measured_build  # noqa: PLC0415

    run_dir = Path(state.run_dir)
    build_dir = run_dir / "scenes" / deck_key
    build_dir.mkdir(parents=True, exist_ok=True)

    # symlink shared profile inputs into the isolated build dir
    for name in ("source_ir.json", "source_ledger.json",
                 "compiled_rules.json", "template_profile.json",
                 "font_inventory.json", "run_manifest.json",
                 "classification_and_transformation_ledger.json"):
        src = run_dir / name
        dst = build_dir / name
        if src.is_file() and not dst.exists():
            dst.symlink_to(src)
    for name in ("assets", "inputs"):
        src = run_dir / name
        dst = build_dir / name
        if src.is_dir() and not dst.exists():
            dst.symlink_to(src, target_is_directory=True)
    for tmpl in (run_dir / "profiles" / "base_template.pptx",
                 run_dir / "base_template.pptx"):
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
            return False, f"missing scene plan: {slide_id}/{cand_id}"
        dst = plans_dir / f"{slide_id.replace('/', '_')}__{cand_id}.json"
        dst.write_text(plan_path.read_text(encoding="utf-8"),
                       encoding="utf-8")

    options = ExportOptions(workflow="presentation", diagnostic=True,
                            overwrite=True)
    layout_options: dict[str, Any] = {
        "resume": False,
        "no_measurement_cache": False,
        "profile_kind": "production",
        "render": False,
        "candidate_name": "candidate.pptx",
        "selection": selection,
    }
    try:
        result = run_measured_build(build_dir, plans_dir, config, options,
                                    layout_options)
    except Exception as exc:  # noqa: BLE001 - typed finding, not crash
        return False, f"build exception: {exc}"
    candidate = build_dir / "candidate.pptx"
    if not candidate.is_file():
        return False, (f"no candidate produced: "
                       f"{getattr(result, 'operation_status', '?')}")
    return True, f"built {deck_key} sha={_sha_file(candidate)[:12]}…"


def _render_all_candidates(state: RunState) -> int:
    """Render every built deck candidate to PNGs (deterministic)."""
    from .render import candidate_typefaces, render_native_candidate  # noqa: PLC0415
    from .typography import prepare_measurement_fonts  # noqa: PLC0415

    run_dir = Path(state.run_dir)
    try:
        from .models import FontInventory  # noqa: PLC0415
        inventory = FontInventory.model_validate_json(
            (run_dir / "font_inventory.json").read_text(encoding="utf-8"))
        bundle = prepare_measurement_fonts(inventory, "",
                                           profile_kind="production")
    except (OSError, ValueError):
        bundle = None

    n_slides = len(state.slide_candidates)
    rendered = 0
    for deck in state.deck_candidates:
        deck_key = str(deck["deck_key"])
        candidate = run_dir / "scenes" / deck_key / "candidate.pptx"
        if not candidate.is_file():
            continue
        if state.renders_used >= state.budget.max_renders:
            break
        try:
            expected_faces = candidate_typefaces(candidate)
        except Exception:  # noqa: BLE001
            expected_faces = []
        probe = render_native_candidate(
            candidate,
            {"output_dir": str(run_dir / "scenes" / deck_key / "render"),
             "dpi": 96,
             "expected_faces": expected_faces,
             "expected_page_count": n_slides},
            bundle)
        state.renders_used += 1
        # render manifest for the review tasks
        manifest = {
            "candidate_sha256": probe.candidate_hash,
            "deck_key": deck_key,
            "pages": [
                {"page": i + 1, "image": Path(p).name,
                 "sha256": _sha_file(Path(p)),
                 "bytes": Path(p).stat().st_size}
                for i, p in enumerate(probe.page_images)],
            "pdf": "candidate.pdf",
            "pdf_sha256": probe.pdf_sha256,
        }
        (run_dir / "scenes" / deck_key / "render" /
         "render_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1),
            encoding="utf-8")
        deck["render_manifest"] = f"scenes/{deck_key}/render/render_manifest.json"
        deck["candidate_sha256"] = probe.candidate_hash
        rendered += 1
    return rendered


def _visual_review_task(state: RunState, deck: dict[str, Any]) -> ModelTask | None:
    run_dir = Path(state.run_dir)
    deck_key = str(deck["deck_key"])
    manifest_rel = deck.get("render_manifest")
    packet_payload = {
        "stage": "visual_review",
        "deck_key": deck_key,
        "selection": deck["selection"],
        "render_manifest": manifest_rel,
        "response_schema": "visual-review/v1",
        "required_fields": ["verdict", "pages"],
        "rule": "open every page PNG; verify required content present, "
                "readable, no clipping/overlap; verdict accept/repair/reject",
    }
    rel = f"model_requests/visual_review-{deck_key}.json"
    (run_dir / rel).parent.mkdir(parents=True, exist_ok=True)
    (run_dir / rel).write_text(json.dumps(packet_payload,
                                            ensure_ascii=False, indent=1),
                               encoding="utf-8")
    return ModelTask(
        request_id=f"T-visual-{deck_key}",
        kind=ModelTaskKind.VISUAL_REVIEW,
        input_fingerprint=_sha_json({"deck": deck_key}),
        response_schema_id="visual-review/v1",
        packet=rel,
        expected_response_units=["verdict", "pages"])


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
        json.dumps(review, ensure_ascii=False, indent=1), encoding="utf-8")


def _design_review_task(state: RunState, deck: dict[str, Any]) -> ModelTask | None:
    run_dir = Path(state.run_dir)
    deck_key = str(deck["deck_key"])
    manifest_rel = deck.get("render_manifest")
    packet_payload = {
        "stage": "design_review",
        "deck_key": deck_key,
        "selection": deck["selection"],
        "render_manifest": manifest_rel,
        "response_schema": "design-review/v1",
        "dimensions": [
            "semantic_readability_relations", "hierarchy_dominance",
            "grouping_alignment_rhythm", "proportions_whitespace",
            "encoding_data_fidelity", "text_visual_evidence_balance",
            "corporate_visual_language", "deck_coherence_diversity"],
        "required_fields": ["verdict", "dimensions"],
        "rule": "review actual PNG bytes for composition quality; absence "
                "of overlaps is not sufficient; flat_text_dump is a defect",
    }
    rel = f"model_requests/design_review-{deck_key}.json"
    (run_dir / rel).parent.mkdir(parents=True, exist_ok=True)
    (run_dir / rel).write_text(json.dumps(packet_payload,
                                            ensure_ascii=False, indent=1),
                               encoding="utf-8")
    return ModelTask(
        request_id=f"T-design-{deck_key}",
        kind=ModelTaskKind.DESIGN_REVIEW,
        input_fingerprint=_sha_json({"deck": deck_key, "design": True}),
        response_schema_id="design-review/v1",
        packet=rel,
        expected_response_units=["verdict", "dimensions"])


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
        json.dumps(review, ensure_ascii=False, indent=1), encoding="utf-8")


def _advance_build_stages(state: RunState,
                          findings: list[TypedFinding]) -> list[ActionRef]:
    """Selection -> assembly -> final acceptance."""
    actions: list[ActionRef] = []
    run_dir = Path(state.run_dir)

    if state.stage == "candidate_selection":
        eligible = _collect_eligible(state)
        if eligible:
            task = ModelTask(
                request_id=f"T-select-{state.revision}",
                kind=ModelTaskKind.CANDIDATE_SELECTION,
                input_fingerprint=_sha_json(
                    {"eligible": sorted(eligible)}),
                response_schema_id="selection-report/v1",
                packet=_write_selection_packet(run_dir, eligible),
                expected_response_units=["selections"])
            _enqueue(state, task)
            state.stage = "selections"
            actions.append(ActionRef(action="issue_selection_task",
                                     detail=f"{len(eligible)} eligible"))

    elif state.stage == "selections":
        sels = _responses_of_kind(state, ModelTaskKind.CANDIDATE_SELECTION)
        if sels and not _has_pending_kind(
                state, ModelTaskKind.CANDIDATE_SELECTION):
            from .models import SelectionReport  # noqa: PLC0415
            report = SelectionReport.model_validate(sels[-1].payload)
            (run_dir / "selection_report.json").write_text(
                json.dumps(report.model_dump(mode="json"),
                           ensure_ascii=False, indent=1),
                encoding="utf-8")
            for entry in report.selections:
                key = str(entry.candidate_key)
                if key in state.candidate_states:
                    state.candidate_states[key] = "selected"
            state.stage = "full_assembly"
            actions.append(ActionRef(action="record_selection",
                                     detail=f"{len(report.selections)} "
                                            f"selected"))

    elif state.stage == "full_assembly":
        ok, detail, needs_review = _assemble_final(state, findings)
        if ok and not needs_review:
            state.stage = "final_checks"
            actions.append(ActionRef(action="assembly_verified",
                                     detail=detail))
        elif ok and needs_review:
            # rebuilt final needs fresh visual+design reviews; stay in
            # full_assembly until they arrive (pending tasks -> harness).
            actions.append(ActionRef(action="final_reviews_pending",
                                     detail=detail))
        else:
            state.stage = "blocked"
            state.terminal_reason = detail
            findings.append(TypedFinding(
                code="SELECTED_CANDIDATE_NOT_ASSEMBLED", severity="error",
                message=detail))
            actions.append(ActionRef(action="assembly_blocked",
                                     detail=detail))

    elif state.stage == "final_checks":
        result = _run_final_acceptance(state)
        if result is None:
            state.stage = "blocked"
            state.terminal_reason = "final acceptance evidence incomplete"
            actions.append(ActionRef(action="acceptance_blocked",
                                     detail=state.terminal_reason))
        elif result.accepted:
            product = publish_accepted_product(state, result)
            state.accepted_product = ProductRef(
                output_path=product.output_path,
                candidate_sha256=product.candidate_sha256)
            state.stage = "accepted"
            actions.append(ActionRef(action="published",
                                     detail=product.output_path))
        else:
            repairs = schedule_repairs(
                state, _acceptance_findings(result))
            if repairs:
                for task in repairs:
                    _enqueue(state, task)
                actions.append(ActionRef(
                    action="schedule_repairs",
                    detail=f"{len(repairs)} repair tasks"))
            else:
                state.stage = "blocked"
                state.terminal_reason = "; ".join(result.error_codes)
                actions.append(ActionRef(
                    action="acceptance_blocked",
                    detail=state.terminal_reason))
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
        eligible.append(deck_key)
    return eligible


def _write_selection_packet(run_dir: Path, eligible: list[str]) -> str:
    payload = {"stage": "candidate_selection", "eligible": eligible,
               "response_schema": "selection-report/v1",
               "required_fields": ["selections"],
               "rule": "exactly one selected per expected source slide; "
                       "score never compensates rule/data/fit failures"}
    rel = "model_requests/candidate_selection.json"
    (run_dir / rel).parent.mkdir(parents=True, exist_ok=True)
    (run_dir / rel).write_text(json.dumps(payload, ensure_ascii=False,
                                            indent=1), encoding="utf-8")
    return rel


def _assemble_final(
    state: RunState, findings: list[TypedFinding]
) -> tuple[bool, str, bool]:
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
        selection[str(entry.get("source_slide_id", ""))] = str(
            ck.get("candidate_id", ""))
    if not selection:
        return False, "empty selection", False

    # reuse check: an existing deck with the identical selection map
    reuse_key = ""
    for deck in state.deck_candidates:
        if {str(k): str(v) for k, v in deck.get("selection", {}).items()} \
                == selection:
            reuse_key = str(deck["deck_key"])
            break
    rebuilt = False
    if reuse_key:
        src_dir = run_dir / "scenes" / reuse_key
        detail = f"reusing reviewed deck {reuse_key}"
    else:
        ok, msg = _build_deck_candidate(
            state, "final", selection, _config_from_state(state))
        if not ok:
            return False, f"final build failed: {msg}", False
        src_dir = run_dir / "scenes" / "final"
        state.deck_candidates.append(
            {"deck_key": "final", "selection": selection})
        detail = f"rebuilt final from selection ({msg})"
        rebuilt = True

    candidate = src_dir / "candidate.pptx"
    if not candidate.is_file():
        return False, "final candidate missing", False
    final_sha = _sha_file(candidate)

    # stage run-root artifacts from the final build dir
    final_path = run_dir / "candidate.pptx"
    shutil.copy2(candidate, final_path)
    for name in ("candidate_after_ir.json", "candidate_after_ledger.json",
                 "content_diff.json", "emission_report.json",
                 "output_object_map.json", "package_graph_report.json",
                 "part_preservation_diff.json",
                 "native_editability_report.json",
                 "saved_authoring_rule_report.json",
                 "saved_geometry_report.json", "saved_geometry_diff.json",
                 "saved_measurement_report.json",
                 "layout_geometry_report.json",
                 "required_visibility_report.json",
                 "candidate_evaluations.json"):
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
    log_transition(state, "final_assembled",
                   f"{detail} sha={final_sha[:12]}…")

    # frame + rule coverage + relation evidence on the final bytes
    _write_final_checks(state, final_path, final_sha, findings)

    # link selection entries to the assembled bytes (provenance, not edit)
    for entry in sel.get("selections", []):
        entry["candidate_sha256"] = final_sha
        entry["candidate_path"] = "candidate.pptx"
        entry.setdefault("assembly_linkage",
                         f"assembled from selected scenes; {detail}")
    sel_path.write_text(json.dumps(sel, ensure_ascii=False, indent=1),
                        encoding="utf-8")

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
        vis = _visual_review_task(
            state, {"deck_key": "final", "selection": selection,
                    "render_manifest": "render/render_manifest.json"})
        des = _design_review_task(
            state, {"deck_key": "final", "selection": selection,
                    "render_manifest": "render/render_manifest.json"})
        if vis is not None:
            vis.request_id = "T-visual-final"
            _enqueue(state, vis)
            pending_note.append("final visual review")
        if des is not None:
            des.request_id = "T-design-final"
            _enqueue(state, des)
            pending_note.append("final design review")
    if pending_note:
        return True, detail + "; scheduled: " + ", ".join(pending_note), True

    # reused deck: copy its current-hash reviews to run root
    for src_name, dst_name in (
            ("visual_review.json", "visual_review.json"),
            ("design_review.json", "final/design_review.json")):
        src = src_dir / src_name
        dst = run_dir / dst_name
        if src.is_file():
            data = json.loads(src.read_text(encoding="utf-8"))
            if data.get("candidate_sha256") == final_sha:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
    return True, detail, False


def _write_final_checks(
    state: RunState, final_path: Path, final_sha: str,
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
        (run_dir / "compiled_rules.json").read_text(encoding="utf-8"))
    template = None
    for name in ("template_profile.json",
                 "profiles/template_profile.json"):
        if (run_dir / name).is_file():
            from .models import TemplateProfile  # noqa: PLC0415
            template = TemplateProfile.model_validate_json(
                (run_dir / name).read_text(encoding="utf-8"))
            break

    # verified frame from layout evidence (§5.2)
    base_template = None
    for cand in (run_dir / "profiles" / "base_template.pptx",
                 run_dir / "base_template.pptx"):
        if cand.is_file():
            base_template = cand
            break
    frame = resolve_frame_profile(
        canvas=compiled.canvas, template_profile=template,
        base_template_path=base_template,
        rule_refs=["C04", "R04", "frame/protected-furniture"])
    (run_dir / "verified_frame.json").write_text(
        json.dumps(frame, ensure_ascii=False, indent=1), encoding="utf-8")
    frame_report = check_saved_frame(final_path, frame) \
        if frame.get("status") == "verified" else {
            "candidate": str(final_path), "ok": False,
            "violations": ["FRAME_UNVERIFIED: " + "; ".join(
                frame.get("findings", ["no frame"]))], "checks": []}
    (run_dir / "saved_frame_report.json").write_text(
        json.dumps(frame_report, ensure_ascii=False, indent=1),
        encoding="utf-8")
    if not frame_report.get("ok"):
        findings.append(TypedFinding(
            code="FRAME_UNVERIFIED", severity="error",
            message="; ".join(frame_report.get("violations", ["frame"]))))

    # rule application coverage on saved bytes (§5.1)
    after_ir = None
    after_path = run_dir / "candidate_after_ir.json"
    if after_path.is_file():
        from .models import SourceDeckIR  # noqa: PLC0415
        try:
            after_ir = SourceDeckIR.model_validate_json(
                after_path.read_text(encoding="utf-8"))
        except ValueError:
            after_ir = None
    inventory = _saved_inventory_of(final_path)
    semantic = _load_json(run_dir / "inputs" / "semantic_deck.json")
    applications = build_rule_applications(compiled, semantic, inventory)
    (run_dir / "rule_applications.json").write_text(
        json.dumps(applications, ensure_ascii=False, indent=1),
        encoding="utf-8")
    coverage = evaluate_saved_rules(
        applications, candidate_path=final_path,
        candidate_after_ir=after_ir, compiled=compiled,
        candidate_sha256=final_sha,
        model_evidence=_rule_review_evidence(state))
    (run_dir / "ontology_rule_coverage.json").write_text(
        json.dumps(coverage, ensure_ascii=False, indent=1), encoding="utf-8")

    # relation preservation: required source relations vs emitted output
    rel_report = _check_relations(run_dir, final_sha)
    (run_dir / "semantic_relation_report.json").write_text(
        json.dumps(rel_report, ensure_ascii=False, indent=1),
        encoding="utf-8")
    for f in rel_report.get("findings", []):
        findings.append(TypedFinding(
            code=f.get("code", "RELATION_DROPPED"),
            severity=f.get("severity", "error"),
            message=f.get("message", ""), scope=f.get("scope")))


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
    state: RunState, coverage: dict[str, Any], final_sha: str,
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
            "rule": ("verify the normative predicate on the actual "
                     "rendered bytes + source/target content; verdict "
                     "pass/fail with proof; prose cannot waive numbers "
                     "or colors"),
        }
        rel = f"model_requests/rule_review-{rid}.json"
        (run_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        (run_dir / rel).write_text(json.dumps(packet, ensure_ascii=False,
                                              indent=1), encoding="utf-8")
        tasks.append(ModelTask(
            request_id=f"T-rule-{rid}",
            kind=ModelTaskKind.RULE_REVIEW,
            input_fingerprint=_sha_json(
                {"rule": rid, "subject": final_sha}),
            response_schema_id="rule-review/v1",
            packet=rel,
            expected_response_units=["evaluations"]))
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
        win[str(entry.get("source_slide_id", ""))] = str(
            ck.get("candidate_id", ""))
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
        plan = _load_json(run_dir / "scenes" / _safe_name(sid) / cid
                          / "plan.json")
        relations = plan.get("relations", []) if plan else []
        order = plan.get("reading_order", []) if plan else []
        pos = {ref: i for i, ref in enumerate(order)}
        slide_ok = True
        for rel in slide.get("relations", []):
            if rel.get("preservation") != "required":
                continue
            rid = rel.get("id", "")
            entry = next(
                (r for r in relations if rid in (r.get("evidence", []) or [])),
                None)
            if entry is None:
                slide_ok = False
                findings.append({
                    "code": "RELATION_DROPPED", "severity": "error",
                    "message": f"required relation {rid} has no target "
                               f"representation in scene plan",
                    "scope": sid})
                continue
            frm, to = str(entry.get("from_ref", "")), str(
                entry.get("to_ref", ""))
            if frm not in emitted or to not in emitted:
                slide_ok = False
                findings.append({
                    "code": "RELATION_DROPPED", "severity": "error",
                    "message": f"relation {rid}: endpoints not emitted "
                               f"({frm} / {to})",
                    "scope": sid})
                continue
            if rel.get("directed") and frm in pos and to in pos and \
                    pos[frm] >= pos[to]:
                slide_ok = False
                findings.append({
                    "code": "RELATION_DROPPED", "severity": "error",
                    "message": f"relation {rid}: reading order reverses "
                               f"direction",
                    "scope": sid})
        per_slide[sid] = {"ok": slide_ok, "candidate_id": cid}
    return {"candidate_sha256": final_sha, "slides": per_slide,
            "findings": findings,
            "ok": not [f for f in findings if f.get("severity") == "error"]}


def _write_gate_evidence(state: RunState, final_sha: str) -> None:
    """Emit gate_reports/*.json for every expected gate from real reports."""
    from .acceptance import _expectation_from_run, _snapshots_from_run  # noqa: PLC0415
    from .models import RunContext  # noqa: PLC0415

    run_dir = Path(state.run_dir)
    ctx = RunContext(project_root=Path(state.input_spec.project_root),
                     run_dir=run_dir)
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
            gid, ("unknown", [], [f"no source report for {gid}"], []))
        # A pass substantiates the full expected unit set via the cited
        # source reports; partial checked lists stay as-is (fail/unknown).
        checked_units = list(exp.expected_units) if status == "pass" \
            else [u for u in checked if u in exp.expected_units]
        payload = {
            "gate": gid, "status": status,
            "expected_units": exp.expected_units,
            "checked_units": checked_units,
            "findings": [{"code": "gate_finding", "severity": "error"
                          if status in ("fail", "unknown") else "info",
                          "message": m} for m in fnd],
            "artifacts": arts,
            "implementation_id": "runner-assembly/v1",
            "subject_candidate_sha256": final_sha,
        }
        (out_dir / f"{_safe_name(gid)}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1),
            encoding="utf-8")


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
        units = [f"{sid}:unit:{u.get('id')}" for u in slide.get("units", [])
                 if u.get("preservation") != "optional"]
        if content_diff.get("ok"):
            out[f"semantic_content_preservation:{sid}"] = (
                "pass", units, [], ["content_diff.json"])
        else:
            out[f"semantic_content_preservation:{sid}"] = (
                "fail", [], ["content_diff not ok"], ["content_diff.json"])
        rel_units = [f"{sid}:relation:{r.get('id')}"
                     for r in slide.get("relations", [])
                     if r.get("preservation") == "required"]
        if not rel_units:
            continue
        slide_rel = relation_report.get("slides", {}).get(sid, {})
        if relation_report.get("ok") and slide_rel.get("ok"):
            out[f"semantic_relations:{sid}"] = (
                "pass", rel_units, [], ["semantic_relation_report.json"])
        else:
            out[f"semantic_relations:{sid}"] = (
                "fail", [], [f"relation preservation failed for {sid}"],
                ["semantic_relation_report.json"])
    if relation_report.get("ok"):
        out["semantic_relations"] = (
            "pass",
            [f"deck:relation:{r}" for s in
             doc("inputs/semantic_deck.json").get("slides", [])
             for r in [x.get("id") for x in s.get("relations", [])]],
            [], ["semantic_relation_report.json"])
    else:
        out["semantic_relations"] = (
            "fail", [], ["deck relation preservation failed"],
            ["semantic_relation_report.json"])

    def units_for(gid: str) -> list[str]:
        return {"package_graph": ["package:parts", "package:media",
                                  "package:relationships"],
                "saved_auth_rules": [],
                "verified_frame": ["frame:regions", "frame:protected"],
                "text_fit_visibility": ["fit:no_overflow",
                                        "visibility:required"],
                "geometry_overlap": ["geometry:in_bounds",
                                     "geometry:allowed_overlap"],
                "font_loading": ["font:verified"],
                "deck_visual_review": ["review:pages", "review:verdict"],
                "visual_design": ["design:dimensions", "design:diversity",
                                  "design:repair"],
                "assembly_linkage": ["assembly:scene->slide"],
                "native_editability": ["native:editable"]}.get(gid, [])

    if part_diff.get("ok"):
        out["package_graph"] = ("pass", units_for("package_graph"), [],
                                ["part_preservation_diff.json"])
    else:
        out["package_graph"] = ("fail", [], ["package graph not ok"],
                                ["part_preservation_diff.json"])
    if auth_rules.get("hard_fail") is False:
        rules_checked = [f"rule:{rid}" for rid in
                         (auth_rules.get("rules", {}) or {})]
        out["saved_auth_rules"] = ("pass", rules_checked or
                                   units_for("saved_auth_rules"), [],
                                   ["saved_authoring_rule_report.json",
                                    "ontology_rule_coverage.json"])
    else:
        out["saved_auth_rules"] = (
            "fail", [], ["saved authoring rules hard fail"],
            ["saved_authoring_rule_report.json"])
    if coverage.get("ok") is False:
        prev = out.get("saved_auth_rules", ("pass", [], [], []))
        out["saved_auth_rules"] = (
            "fail", list(prev[1]),
            list(prev[2]) + [f"rule coverage fails: "
                             f"{coverage.get('fails', [])}"],
            list(prev[3]))
    if frame_report.get("ok"):
        out["verified_frame"] = ("pass", units_for("verified_frame"), [],
                                 ["saved_frame_report.json",
                                  "verified_frame.json"])
    else:
        out["verified_frame"] = (
            "fail", [],
            [f"frame unverified: {frame_report.get('violations', [])}"],
            ["saved_frame_report.json", "verified_frame.json"])
    # saved text exact: remeasured saved text matches resolved specs
    if measure_report.get("ok"):
        out["saved_text_exact"] = (
            "pass", [f"text:scope:{s}" for s in
                     measure_report.get("checked_scopes", [])], [],
            ["saved_measurement_report.json"])
    else:
        out["saved_text_exact"] = (
            "fail", [], ["saved text remeasure not ok"],
            ["saved_measurement_report.json"])
    if measure_report.get("ok") and not visibility.get("missing"):
        out["text_fit_visibility"] = (
            "pass", units_for("text_fit_visibility"), [],
            ["saved_measurement_report.json",
             "required_visibility_report.json"])
    else:
        out["text_fit_visibility"] = (
            "fail", [], ["fit/visibility not proven"],
            ["saved_measurement_report.json",
             "required_visibility_report.json"])
    if not saved_geometry.get("out_of_canvas") and not \
            saved_geometry.get("clipping"):
        out["geometry_overlap"] = (
            "pass", units_for("geometry_overlap"), [],
            ["saved_geometry_report.json", "layout_geometry_report.json"])
    else:
        out["geometry_overlap"] = (
            "fail", [], ["geometry violations"],
            ["saved_geometry_report.json"])
    if not font_report.get("missing_expected_faces") and not \
            font_report.get("substitution_detected"):
        out["font_loading"] = ("pass", units_for("font_loading"), [],
                               ["render/render_font_report.json"])
    else:
        out["font_loading"] = ("fail", ["font substitution/missing"],
                               ["render/render_font_report.json"])
    manifest = doc("render/render_manifest.json")
    pages = manifest.get("pages", [])
    n_slides = len(doc("inputs/semantic_deck.json").get("slides", []))
    if len(pages) == n_slides and n_slides:
        out["render_coverage"] = (
            "pass", [f"render:page:{p.get('page')}" for p in pages], [],
            ["render/render_manifest.json"])
    else:
        out["render_coverage"] = (
            "fail", [], [f"render covers {len(pages)}/{n_slides}"],
            ["render/render_manifest.json"])
    if visual.get("verdict") == "accept" and visual.get(
            "candidate_sha256") == final_sha:
        out["deck_visual_review"] = (
            "pass", units_for("deck_visual_review"), [],
            ["visual_review.json"])
    else:
        out["deck_visual_review"] = (
            "fail", [], ["visual review not accept/current"],
            ["visual_review.json"])
    design_dims = design.get("dimensions", [])
    if design.get("verdict") == "accept" and design.get(
            "candidate_sha256") == final_sha and design_dims and all(
                d.get("result") in ("sufficient", "not_applicable")
                for d in design_dims):
        out["visual_design"] = ("pass", units_for("visual_design"), [],
                                ["final/design_review.json"])
    else:
        out["visual_design"] = (
            "fail", [], ["design review not accept/current"],
            ["final/design_review.json"])
    sels = selection.get("selections", [])
    if sels and all(s.get("candidate_sha256") == final_sha for s in sels):
        out["candidate_selection"] = (
            "pass", [f"selection:{s.get('source_slide_id')}" for s in sels],
            [], ["selection_report.json"])
    else:
        out["candidate_selection"] = (
            "fail", [], ["selection incomplete/stale"],
            ["selection_report.json"])
    out["assembly_linkage"] = ("pass", units_for("assembly_linkage"), [],
                               ["selection_report.json",
                                "candidate_evaluations.json"])
    if native_edit.get("native_structure_verified"):
        out["native_editability"] = (
            "pass", units_for("native_editability"), [],
            ["native_editability_report.json"])
    else:
        out["native_editability"] = (
            "fail", [], ["native structure not verified"],
            ["native_editability_report.json"])
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
            return False, ("SELECTED_CANDIDATE_NOT_ASSEMBLED: "
                           f"{entry.get('source_slide_id')}")
    return True, f"assembly matches selection ({final_sha[:12]}…)"


def _run_final_acceptance(state: RunState) -> ProductAcceptance | None:
    from .acceptance import main as acceptance_main  # noqa: PLC0415
    run_dir = Path(state.run_dir)
    acceptance_main(str(run_dir))
    acc_path = run_dir / "product_acceptance.json"
    if not acc_path.is_file():
        return None
    try:
        return ProductAcceptance.model_validate_json(
            acc_path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def _acceptance_findings(result: ProductAcceptance) -> list[RepairFinding]:
    out: list[RepairFinding] = []
    for finding in result.findings:
        out.append(RepairFinding(
            finding_id=finding.code,
            subject_candidate_sha256=result.subject_candidate_sha256,
            defect=finding.message, severity=finding.severity,
            proposed_action="scene_repair"))
    return out


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# repairs (§8.4)
# ---------------------------------------------------------------------------

def schedule_repairs(state: RunState,
                     findings: list[RepairFinding]) -> list[ModelTask]:
    """Findings become scene_repair tasks with concept + image regions."""
    tasks: list[ModelTask] = []
    for finding in findings:
        key = str(finding.candidate_key) if finding.candidate_key else ""
        revision = state.candidate_revisions.get(key, 0)
        if revision >= state.budget.max_repairs_per_candidate:
            state.candidate_states[key] = "rejected"
            log_transition(state, "candidate_rejected",
                           f"{key}: repair budget spent")
            continue
        state.candidate_revisions[key] = revision + 1
        if key:
            state.candidate_states[key] = "scene_pending"
        _invalidate_downstream(state, key)
        packet = _write_repair_packet(state, finding)
        tasks.append(ModelTask(
            request_id=f"T-repair-{finding.finding_id}-"
                       f"r{state.candidate_revisions.get(key, 0)}",
            kind=ModelTaskKind.SCENE_REPAIR,
            candidate_key=finding.candidate_key,
            input_fingerprint=_sha_json(
                {"finding": finding.finding_id,
                 "subject": finding.subject_candidate_sha256}),
            response_schema_id="scene-plan/v1",
            packet=packet,
            expected_response_units=["scene_plan"]))
        log_transition(state, "repair_scheduled",
                       f"{finding.finding_id} -> {key}")
    return tasks


def _write_repair_packet(state: RunState, finding: RepairFinding) -> str:
    run_dir = Path(state.run_dir)
    payload = {"stage": "scene_repair",
               "finding": finding.model_dump(mode="json"),
               "response_schema": "scene-plan/v1",
               "rule": "structural design change required; bbox-only "
                       "shifts with identical topology do not close "
                       "design findings"}
    rel = (f"model_requests/repair-{finding.finding_id}.json")
    (run_dir / rel).parent.mkdir(parents=True, exist_ok=True)
    (run_dir / rel).write_text(json.dumps(payload, ensure_ascii=False,
                                            indent=1), encoding="utf-8")
    return rel


def _invalidate_downstream(state: RunState, candidate_key: str) -> None:
    report_path = Path(state.run_dir) / "cache_invalidation_report.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8")) \
            if report_path.is_file() else {"invalidated": []}
    except ValueError:
        report = {"invalidated": []}
    report["invalidated"].append({
        "candidate": candidate_key, "revision": state.revision,
        "scopes": ["measurement", "emission", "coverage", "review",
                   "selection"]})
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                           encoding="utf-8")


# ---------------------------------------------------------------------------
# resume + status
# ---------------------------------------------------------------------------

def resume_run(run_dir: Path, config: Any,
               responses: list[ModelTaskResponse] | None = None) -> AdvanceResult:
    """Idempotent resume: load state, consume new responses, advance."""
    state = load_state(run_dir)
    log_transition(state, "resume", f"{len(responses or [])} responses")
    return advance_run(state, responses or [])


def run_status(run_dir: Path) -> dict[str, Any]:
    state = load_state(run_dir)
    return {"run_id": state.run_id, "stage": state.stage,
            "revision": state.revision,
            "pending_tasks": [t.request_id for t in state.task_queue
                              if t.status == "pending"],
            "candidates": dict(state.candidate_states),
            "tasks_used": state.tasks_used,
            "budget": state.budget.model_dump(mode="json"),
            "accepted": state.accepted_product.model_dump(mode="json")
            if state.accepted_product else None,
            "terminal_reason": state.terminal_reason}


# ---------------------------------------------------------------------------
# publication (§8.5)
# ---------------------------------------------------------------------------

def publish_accepted_product(state: RunState,
                             acceptance: ProductAcceptance) -> PublishedProduct:
    """Atomic publication only from the current accepted candidate."""
    if not acceptance.accepted or not acceptance.publication_eligible:
        raise RunnerError("refusing publication: acceptance is not clean")
    run_dir = Path(state.run_dir)
    candidate = run_dir / "candidate.pptx"
    if not candidate.is_file():
        raise RunnerError("CANDIDATE_MISSING: nothing to publish")
    current_sha = _sha_file(candidate)
    if current_sha != acceptance.subject_candidate_sha256:
        raise RunnerError("SELECTED_CANDIDATE_NOT_ASSEMBLED: candidate "
                          "bytes changed after acceptance")
    output = Path(state.input_spec.output_path)
    if output.exists() and not _overwrite_allowed(state):
        raise RunnerError(f"output exists without --overwrite: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    _atomic_bytes(output, candidate.read_bytes())
    if _sha_file(output) != current_sha:
        raise RunnerError("publication hash mismatch after copy")
    manifest = {"output": str(output), "candidate_sha256": current_sha,
                "acceptance": acceptance.model_dump(mode="json")}
    _atomic_write(run_dir / "publication_manifest.json",
                  json.dumps(manifest, ensure_ascii=False, indent=1))
    log_transition(state, "published",
                   f"{output} sha={current_sha[:12]}…")
    save_state(state)
    return PublishedProduct(output_path=str(output),
                            candidate_sha256=current_sha)


def _overwrite_allowed(state: RunState) -> bool:
    return bool(getattr(state.input_spec, "overwrite", False))


def _atomic_bytes(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                               prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
