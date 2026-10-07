"""Runtime LLM planner: LayoutIntent schema, validation, and candidate generation (Stage 3.5 §8).

The planner sends a planning packet (slide view + rule slice + capabilities +
JSON schema) to the configured local model and receives typed LayoutIntent
candidates. It does not execute edits — that is the planning_bridge's job.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import ModelConfigurationError
from .local_llm import HarnessModelClient, LocalModelClient, ModelRequest, ResolvedModelConfig
from .models import CompiledOntology, SlideIR, SourceLedger

log = logging.getLogger(__name__)

StrictModel = ConfigDict(extra="forbid")

SYSTEM_PROMPT_PATH = Path(__file__).with_name("planner_system.md")

_UNSAFE_TOKEN_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def safe_token(value: str, *, max_len: int = 48) -> str:
    """Return a filename/JSON-token-safe form of an id.

    Real source ids look like ``slide0:ppt/slides/slide1.xml``: they must stay
    verbatim inside artifacts, but they cannot be used as path segments or
    request ids. The digest keeps distinct ids distinct after sanitising.
    """
    cleaned = _UNSAFE_TOKEN_CHARS.sub("_", value).strip("_")
    cleaned = cleaned[:max_len] or "id"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned}-{digest}"


class LayoutZone(BaseModel):
    model_config = StrictModel

    zone_id: str
    x_pct: float = Field(ge=0, le=100)
    y_pct: float = Field(ge=0, le=100)
    w_pct: float = Field(gt=0, le=100)
    h_pct: float = Field(gt=0, le=100)
    role: str = ""
    content_refs: list[str] = Field(default_factory=list)


class LayoutRelation(BaseModel):
    model_config = StrictModel

    from_ref: str
    to_ref: str
    relation_kind: str
    evidence: list[str] = Field(default_factory=list)


class LayoutIntent(BaseModel):
    """One candidate composition from the model."""

    model_config = StrictModel

    slide_id: str
    candidate_id: str = ""
    content_refs: list[str] = Field(default_factory=list)
    communication_job: str = ""
    reading_order: list[str] = Field(default_factory=list)
    dominant_ref: str = ""
    zones: list[LayoutZone] = Field(default_factory=list)
    relations: list[LayoutRelation] = Field(default_factory=list)
    operators: list[str] = Field(default_factory=list)
    reference_ids: list[str] = Field(default_factory=list)
    novel_recipe: str = ""
    uncertainties: list[str] = Field(default_factory=list)
    rationale: str = ""


@dataclass
class PlanningSlideView:
    """Compact view of one slide for the model."""

    slide_id: str
    objects: list[dict[str, Any]] = field(default_factory=list)
    semantic_slots: list[dict[str, Any]] = field(default_factory=list)
    uncertainties: list[Any] = field(default_factory=list)


@dataclass
class PlanningRuleSlice:
    """Relevant rules for the planner."""

    snapshot_id: str
    corpus_hash: str
    global_rules: list[dict[str, Any]] = field(default_factory=list)
    role_records: dict[str, Any] = field(default_factory=dict)
    color_policy: dict[str, Any] = field(default_factory=dict)
    list_policy: dict[str, Any] = field(default_factory=dict)
    extension_policy: dict[str, Any] = field(default_factory=dict)
    available_operators: list[str] = field(default_factory=list)
    unresolved_norms: list[str] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)
    omitted_sections: list[str] = field(default_factory=list)
    estimated_tokens: int = 0


@dataclass
class PlanningContext:
    """Full planning packet for one slide."""

    packet_id: str
    source_hash: str
    slide_id: str
    snapshot_id: str
    ontology_corpus_hash: str
    view: PlanningSlideView | None = None
    required_ref_groups: list[str] = field(default_factory=list)
    rule_slice: PlanningRuleSlice | None = None
    examples: list[dict[str, Any]] = field(default_factory=list)
    capabilities: dict[str, Any] = field(default_factory=dict)
    previous_signatures: list[str] = field(default_factory=list)
    preservation_policy: dict[str, Any] = field(default_factory=dict)
    budget: dict[str, Any] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)


@dataclass
class IntentCandidate:
    """A validated or rejected candidate."""

    intent: LayoutIntent | None
    origin: str
    request_id: str
    candidate_id: str
    valid: bool
    errors: list[str] = field(default_factory=list)
    diversity_signature: str = ""
    validation: IntentValidationReport | None = None


class IntentValidationReport(BaseModel):
    """Independent validation of a model intent (spec §9.1).

    ``planning_acceptable`` means "may proceed on the declared planning
    scope"; it never equals production validation.
    """

    model_config = StrictModel

    schema_valid: bool = False
    reference_valid: bool = False
    required_atoms_covered: bool = False
    unexpected_refs: list[str] = Field(default_factory=list)
    missing_refs: list[str] = Field(default_factory=list)
    duplicate_refs: list[str] = Field(default_factory=list)
    relation_issues: list[str] = Field(default_factory=list)
    role_issues: list[str] = Field(default_factory=list)
    capability_issues: list[str] = Field(default_factory=list)
    hard_issues: list[str] = Field(default_factory=list)
    unresolved_scopes: list[str] = Field(default_factory=list)
    planning_acceptable: bool = False
    production_validated: bool = False


def build_intent_schema(
    packet: PlanningContext,
    rules: CompiledOntology,
    capabilities: dict[str, Any],
) -> dict[str, Any]:
    """Build JSON schema for LayoutIntent with dynamic enums from snapshot."""
    role_ids = sorted(rules.roles.keys())
    implemented_ops = set(capabilities.get("implemented_operators", []))
    operator_ids = sorted(set(rules.composition_operators) & implemented_ops)
    ref_ids = _collect_ref_ids(packet)
    ref_spec = _ref_spec(ref_ids)

    schema = {
        "type": "object",
        "properties": {
            "slide_id": {"type": "string", "const": packet.slide_id},
            "candidate_id": {"type": "string"},
            "content_refs": {
                "type": "array",
                "items": ref_spec,
                "uniqueItems": True,
            },
            "communication_job": {"type": "string"},
            "reading_order": {
                "type": "array",
                "items": ref_spec,
            },
            "dominant_ref": _ref_spec(ref_ids, allow_empty=True),
            "zones": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "zone_id": {"type": "string"},
                        "x_pct": {"type": "number", "minimum": 0, "maximum": 100},
                        "y_pct": {"type": "number", "minimum": 0, "maximum": 100},
                        "w_pct": {"type": "number", "exclusiveMinimum": 0, "maximum": 100},
                        "h_pct": {"type": "number", "exclusiveMinimum": 0, "maximum": 100},
                        "role": (
                            {"type": "string", "enum": role_ids}
                            if role_ids else {"type": "string"}
                        ),
                        "content_refs": {"type": "array", "items": ref_spec},
                    },
                    "required": ["zone_id", "x_pct", "y_pct", "w_pct", "h_pct"],
                    "additionalProperties": False,
                },
            },
            "relations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "from_ref": {"type": "string"},
                        "to_ref": {"type": "string"},
                        "relation_kind": {"type": "string"},
                        "evidence": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["from_ref", "to_ref", "relation_kind"],
                    "additionalProperties": False,
                },
            },
            "operators": {
                "type": "array",
                "items": (
                    {"type": "string", "enum": operator_ids}
                    if operator_ids else {"type": "string"}
                ),
                "uniqueItems": True,
            },
            "reference_ids": {"type": "array", "items": {"type": "string"}},
            "novel_recipe": {"type": "string"},
            "uncertainties": {"type": "array", "items": {"type": "string"}},
            "rationale": {"type": "string"},
        },
        "required": [
            "slide_id", "content_refs", "communication_job",
            "reading_order", "zones", "operators",
        ],
        "additionalProperties": False,
    }
    # §5.3: the schema document is bound to the exact snapshot it was
    # generated from. Any corpus change yields a different schema document,
    # so schema hashes (and downstream plan caches keyed on them) invalidate
    # automatically. `x-` metadata is never a LayoutIntent field and is
    # ignored by validate_intent_against_schema, which walks known keywords.
    schema["x-ontology-corpus-hash"] = rules.ontology_corpus_hash
    schema["x-ontology-source-hash"] = rules.source_hash
    return schema


def _collect_ref_ids(packet: PlanningContext) -> list[str]:
    refs: set[str] = set()
    if packet.view:
        for obj in packet.view.objects:
            ref = obj.get("ref", "")
            if ref:
                refs.add(ref)
    return sorted(refs)


REF_ENUM_LIMIT = 400


def _ref_spec(ref_ids: list[str], *, allow_empty: bool = False) -> dict[str, Any]:
    """Reference value spec: enum over the current slide refs when small.

    Beyond REF_ENUM_LIMIT the enum is dropped and the local reference
    validator (validate_planned_intent) stays authoritative — never a weak
    "looks like an id" check.
    """
    if ref_ids and len(ref_ids) <= REF_ENUM_LIMIT:
        values = list(ref_ids)
        if allow_empty:
            values = values + [""]
        return {"type": "string", "enum": values}
    return {"type": "string"}


def validate_intent_against_schema(data: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Validate a decoded intent against the generated schema subset.

    Covers the dynamic parts JSON Schema would check for us: ``const``,
    ``enum``, ``required`` and ``additionalProperties`` for the object
    structure produced by :func:`build_intent_schema`.
    """
    errors: list[str] = []

    def walk(node: Any, spec: Any, path: str) -> None:
        if not isinstance(spec, dict):
            return
        if "const" in spec and node != spec["const"]:
            errors.append(f"{path}: value {node!r} != const {spec['const']!r}")
        if "enum" in spec and node not in spec["enum"]:
            errors.append(f"{path}: value {node!r} not in enum")
        kind = spec.get("type")
        if kind == "object" and isinstance(node, dict):
            props = spec.get("properties", {})
            for req in spec.get("required", []):
                if req not in node:
                    errors.append(f"{path}: missing required property {req!r}")
            if spec.get("additionalProperties") is False:
                for key in node:
                    if key not in props:
                        errors.append(f"{path}: unexpected property {key!r}")
            for key, sub in props.items():
                if key in node:
                    walk(node[key], sub, f"{path}.{key}")
        elif kind == "array" and isinstance(node, list):
            item_spec = spec.get("items")
            for idx, item in enumerate(node):
                walk(item, item_spec, f"{path}[{idx}]")
        elif kind in ("string", "number", "integer", "boolean") and not isinstance(
            node, _PY_TYPES.get(kind, (str,))
        ):
            errors.append(f"{path}: expected {kind}, got {type(node).__name__}")
        if kind == "number" and isinstance(node, (int, float)):
            if "minimum" in spec and node < spec["minimum"]:
                errors.append(f"{path}: {node} < minimum {spec['minimum']}")
            if "maximum" in spec and node > spec["maximum"]:
                errors.append(f"{path}: {node} > maximum {spec['maximum']}")
            if "exclusiveMinimum" in spec and node <= spec["exclusiveMinimum"]:
                errors.append(f"{path}: {node} <= exclusiveMinimum {spec['exclusiveMinimum']}")

    walk(data, schema, "$")
    return errors


_PY_TYPES: dict[str, Any] = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
}


def validate_planned_intent(
    intent: LayoutIntent,
    packet: PlanningContext,
    source: SlideIR | None,
    ledger: SourceLedger | None,
    rules: CompiledOntology | None,
    capabilities: dict[str, Any],
    *,
    schema_errors: list[str] | None = None,
) -> IntentValidationReport:
    """Independently validate a planned intent against source and rules (§9.1)."""
    unresolved: list[str] = []
    hard: list[str] = []
    schema_ok = not (schema_errors or [])

    allowed: set[str] = set(_collect_ref_ids(packet))
    allowed.update(packet.required_ref_groups)
    required = set(packet.required_ref_groups)

    content_refs = list(intent.content_refs)
    zone_refs = [ref for zone in intent.zones for ref in zone.content_refs]
    all_refs = set(content_refs) | set(zone_refs)

    unexpected = sorted(all_refs - allowed) if allowed else []
    missing = sorted(required - set(content_refs)) if required else []
    seen: dict[str, int] = {}
    for ref in zone_refs:
        seen[ref] = seen.get(ref, 0) + 1
    duplicates = sorted(ref for ref, count in seen.items() if count > 1)

    refs_ok = not unexpected and not duplicates
    if missing:
        hard.append(f"required refs not covered: {', '.join(missing)}")
        refs_ok = False
    if unexpected:
        hard.append(f"unknown refs: {', '.join(unexpected)}")
    if duplicates:
        hard.append(f"refs assigned to multiple zones: {', '.join(duplicates)}")

    order_bad = [ref for ref in intent.reading_order if ref not in allowed and allowed]
    if order_bad:
        hard.append(f"reading_order refs unknown: {', '.join(order_bad)}")
        refs_ok = False

    relation_issues: list[str] = []
    for rel in intent.relations:
        if not rel.evidence:
            relation_issues.append(
                f"relation {rel.from_ref}->{rel.to_ref} "
                f"({rel.relation_kind}) has no source evidence"
            )
    if relation_issues and source is None:
        unresolved.append("relation_evidence_needs_source")

    role_issues: list[str] = []
    if rules is not None:
        known_roles = set(rules.roles)
        for zone in intent.zones:
            if zone.role and zone.role not in known_roles:
                role_issues.append(f"unknown style_role {zone.role!r} in zone {zone.zone_id}")
    else:
        unresolved.append("rules_not_available_for_role_check")

    capability_issues: list[str] = []
    allowed_operators: set[str] = set()
    if rules is not None:
        allowed_operators = set(rules.composition_operators)
    implemented = set(capabilities.get("implemented_operators", []))
    if implemented:
        allowed_operators &= implemented if allowed_operators else implemented
    for op in intent.operators:
        if allowed_operators and op not in allowed_operators:
            capability_issues.append(f"operator {op!r} not in allowed set")
    if not allowed_operators:
        unresolved.append("operator_set_unresolved")

    if intent.novel_recipe:
        unresolved.append("novel_recipe_requires_extension_review")
        proposes_style = any(
            marker in intent.novel_recipe.lower()
            for marker in ("color", "token", "font")
        )
        if (
            rules is not None
            and not rules.extension_contract.style_new_tokens_allowed
            and proposes_style
        ):
            hard.append(
                "novel_recipe proposes style tokens, forbidden by extension contract"
            )

    if source is None:
        unresolved.append("source_ir_not_available")
    if ledger is None:
        unresolved.append("ledger_not_available")
    unresolved.extend(rule_slice_unresolved(packet))

    blocking: list[str] = []
    blocking.extend(f"missing_ref: {m}" for m in missing)
    blocking.extend(f"unexpected_ref: {u}" for u in unexpected)
    blocking.extend(f"duplicate_ref: {d}" for d in duplicates)
    blocking.extend(f"reading_order_ref: {o}" for o in order_bad)
    blocking.extend(f"relation: {r}" for r in relation_issues)
    blocking.extend(f"role: {r}" for r in role_issues)
    blocking.extend(f"capability: {c}" for c in capability_issues)
    blocking.extend(hard)
    hard = blocking

    reference_valid = refs_ok and not order_bad
    acceptable = schema_ok and reference_valid and not hard

    return IntentValidationReport(
        schema_valid=schema_ok,
        reference_valid=reference_valid,
        required_atoms_covered=not missing,
        unexpected_refs=unexpected,
        missing_refs=missing,
        duplicate_refs=duplicates,
        relation_issues=relation_issues,
        role_issues=role_issues,
        capability_issues=capability_issues,
        hard_issues=hard,
        unresolved_scopes=sorted(set(unresolved)),
        planning_acceptable=acceptable,
        production_validated=False,
    )


def rule_slice_unresolved(packet: PlanningContext) -> list[str]:
    if packet.rule_slice is None:
        return ["rule_slice_missing"]
    return list(packet.rule_slice.unresolved_norms)


@dataclass
class SelectedCandidate:
    """Outcome of candidate selection (spec §9.2)."""

    candidate: IntentCandidate | None
    status: str
    reason: str
    scores: dict[str, float] = field(default_factory=dict)


def select_planning_candidate(
    evaluations: list[IntentCandidate],
    previous: list[str] | None = None,
    options: dict[str, Any] | None = None,
) -> SelectedCandidate:
    """Pick the best acceptable candidate; never rescues an invalid one.

    Weights are engineering heuristics recorded in the run, not a beauty
    metric. Selection status stays ``selected_pending_measurement`` until a
    real text measurer exists (Stage 5).
    """
    opts = options or {}
    weights = {
        "coverage": float(opts.get("weight_coverage", 2.0)),
        "dominant": float(opts.get("weight_dominant", 1.0)),
        "reading_order": float(opts.get("weight_reading_order", 1.0)),
        "diversity": float(opts.get("weight_diversity", 1.0)),
    }
    previous_sigs = set(previous or [])

    acceptable = [
        c for c in evaluations
        if c.valid and c.intent is not None
        and c.validation is not None and c.validation.planning_acceptable
    ]
    if not acceptable:
        return SelectedCandidate(
            candidate=None,
            status="no_valid_candidate",
            reason="no candidate passed schema/reference/hard validation",
        )

    best: IntentCandidate | None = None
    best_scores: dict[str, float] = {}
    best_total = float("-inf")
    for cand in acceptable:
        intent = cand.intent
        assert intent is not None and cand.validation is not None
        coverage = 1.0 if cand.validation.required_atoms_covered else 0.0
        dominance = 1.0 if intent.dominant_ref else 0.0
        order = 1.0 if intent.reading_order else 0.0
        diversity = 0.0 if cand.diversity_signature in previous_sigs else 1.0
        scores = {
            "coverage": coverage * weights["coverage"],
            "dominant": dominance * weights["dominant"],
            "reading_order": order * weights["reading_order"],
            "diversity": diversity * weights["diversity"],
        }
        total = sum(scores.values())
        if total > best_total:
            best_total = total
            best = cand
            best_scores = scores

    if best is None:
        return SelectedCandidate(None, "no_valid_candidate", "selection produced no candidate")

    return SelectedCandidate(
        candidate=best,
        status="selected_pending_measurement",
        reason=(
            "accepted on planning scope; readability/overflow pending Stage 5 "
            "measurement"
        ),
        scores=best_scores,
    )


class LocalPlanner:
    """Generates LayoutIntent candidates via the configured model provider."""

    def __init__(
        self,
        client: LocalModelClient | HarnessModelClient,
        model_config: ResolvedModelConfig,
        *,
        run_dir: Path | None = None,
        system_prompt: str | None = None,
        rules: CompiledOntology | None = None,
        max_schema_repairs: int = 1,
    ) -> None:
        self.client = client
        self.model_config = model_config
        self.run_dir = Path(run_dir) if run_dir else None
        self.system_prompt = system_prompt or _default_system_prompt()
        self.rules = rules
        self.max_schema_repairs = max_schema_repairs
        self._candidates: list[IntentCandidate] = []
        self._lock = threading.Lock()
        self._schema_repairs = 0
        self._cache_hits = 0
        self._accepted = 0
        self._rejected = 0

    @property
    def stats(self) -> dict[str, int]:
        """Actual generation counters (spec §8.4)."""
        return {
            "llm_generation_requests": int(getattr(self.client, "generation_count", 0)),
            "transport_retries": int(getattr(self.client, "retry_count", 0)),
            "schema_repairs": self._schema_repairs,
            "accepted_candidates": self._accepted,
            "rejected_candidates": self._rejected,
            "cache_hits": self._cache_hits,
        }

    def register_cache_hit(self) -> None:
        self._cache_hits += 1

    def plan(
        self,
        packet: PlanningContext,
        schema: dict[str, Any],
        *,
        candidate_count: int = 1,
        cancel_token: threading.Event | None = None,
        source: SlideIR | None = None,
        ledger: SourceLedger | None = None,
        repairable: bool = True,
    ) -> list[IntentCandidate]:
        """Generate up to candidate_count LayoutIntent candidates.

        Each candidate is decoded strictly, validated against the dynamic
        schema, then independently validated against source/ledger/rules.
        A repairable schema error triggers at most ``max_schema_repairs``
        bounded repair requests per candidate with the exact errors.
        """
        candidates: list[IntentCandidate] = []
        schema_hash = hashlib.sha256(
            json.dumps(schema, sort_keys=True).encode()
        ).hexdigest()
        accepted_signatures: list[str] = list(packet.previous_signatures)

        for idx in range(candidate_count):
            if cancel_token and cancel_token.is_set():
                break

            request_id = f"plan_{packet.packet_id}_{idx}"
            messages = _build_messages(
                packet, schema, self.system_prompt, idx, accepted_signatures
            )
            candidate, attempt_ids = self._generate_and_validate(
                request_id=request_id,
                packet=packet,
                schema=schema,
                schema_hash=schema_hash,
                messages=messages,
                candidate_id=f"cand_{idx}",
                cancel_token=cancel_token,
                source=source,
                ledger=ledger,
                repairable=repairable,
            )
            if candidate.valid and candidate.intent is not None:
                accepted_signatures.append(candidate.diversity_signature)
            candidates.append(candidate)

        with self._lock:
            self._candidates.extend(candidates)
        return candidates

    def _generate_and_validate(
        self,
        *,
        request_id: str,
        packet: PlanningContext,
        schema: dict[str, Any],
        schema_hash: str,
        messages: list[dict[str, str]],
        candidate_id: str,
        cancel_token: threading.Event | None,
        source: SlideIR | None,
        ledger: SourceLedger | None,
        repairable: bool,
    ) -> tuple[IntentCandidate, list[str]]:
        attempt_ids: list[str] = []
        current_messages = list(messages)
        schema_errors: list[str] | None = None
        repair_round = 0

        while True:
            request = ModelRequest(
                purpose="layout_planning",
                messages=current_messages,
                response_schema=schema if self.model_config.use_json_schema != "never" else None,
                model_config=self.model_config,
                request_id=(
                    request_id if repair_round == 0
                    else f"{request_id}_repair{repair_round}"
                ),
                packet_hash=packet.source_hash,
                schema_hash=schema_hash,
            )
            result = self.client.generate(request, cancel_token=cancel_token)
            attempt_ids.append(request.request_id)

            if result.error or not result.content_text:
                with self._lock:
                    self._rejected += 1
                return IntentCandidate(
                    intent=None,
                    origin=_origin_of(self.client),
                    request_id=request.request_id,
                    candidate_id=candidate_id,
                    valid=False,
                    errors=[result.error or "empty response"],
                ), attempt_ids

            content = result.content_text
            if content.startswith("```"):
                content = _strip_fence(content)

            try:
                data = json.loads(content)
            except json.JSONDecodeError as exc:
                if repairable and repair_round < self.max_schema_repairs:
                    repair_round += 1
                    with self._lock:
                        self._schema_repairs += 1
                    current_messages = current_messages + [
                        _repair_message(f"invalid JSON: {exc}")
                    ]
                    continue
                with self._lock:
                    self._rejected += 1
                return IntentCandidate(
                    intent=None,
                    origin=_origin_of(self.client),
                    request_id=request.request_id,
                    candidate_id=candidate_id,
                    valid=False,
                    errors=[f"JSON parse error: {exc}"],
                ), attempt_ids

            pydantic_errors: list[str] = []
            intent: LayoutIntent | None = None
            try:
                intent = LayoutIntent.model_validate(data)
            except ValidationError as exc:
                pydantic_errors.append(f"schema validation: {exc}")

            dynamic_errors = validate_intent_against_schema(data, schema)
            schema_errors = pydantic_errors + dynamic_errors

            if schema_errors and repairable and repair_round < self.max_schema_repairs:
                repair_round += 1
                with self._lock:
                    self._schema_repairs += 1
                current_messages = current_messages + [
                    _repair_message("; ".join(schema_errors[:20]))
                ]
                continue

            if schema_errors:
                with self._lock:
                    self._rejected += 1
                return IntentCandidate(
                    intent=None,
                    origin=_origin_of(self.client),
                    request_id=request.request_id,
                    candidate_id=candidate_id,
                    valid=False,
                    errors=schema_errors,
                ), attempt_ids

            assert intent is not None
            report = validate_planned_intent(
                intent,
                packet,
                source,
                ledger,
                self.rules,
                packet.capabilities,
                schema_errors=schema_errors,
            )
            sig = compute_diversity_signature(intent)
            accepted = report.planning_acceptable
            with self._lock:
                if accepted:
                    self._accepted += 1
                else:
                    self._rejected += 1
            return IntentCandidate(
                intent=intent,
                origin=_origin_of(self.client),
                request_id=request.request_id,
                candidate_id=candidate_id,
                valid=accepted,
                errors=list(report.hard_issues),
                diversity_signature=sig,
                validation=report,
            ), attempt_ids

    @property
    def candidates(self) -> list[IntentCandidate]:
        return list(self._candidates)


def _origin_of(client: Any) -> str:
    provider = getattr(getattr(client, "config", None), "provider", "local")
    if provider == "harness":
        return "harness_model"
    return "local_model"


def _repair_message(errors_text: str) -> dict[str, str]:
    return {
        "role": "user",
        "content": (
            "Previous answer was rejected by the validator with these exact "
            f"errors:\n{errors_text}\n"
            "Return a corrected JSON only, following the same schema, the same "
            "rule slice and the same slide content. Do not change slide facts, "
            "refs or rule values; fix only the structural/validation problems."
        ),
    }


def compute_diversity_signature(intent: LayoutIntent) -> str:
    """Compute a diversity signature based on topology, grouping, dominant, order, operators."""
    parts = [
        f"dominant:{intent.dominant_ref}",
        f"zones:{len(intent.zones)}",
        f"ops:{','.join(sorted(intent.operators))}",
        f"order:{','.join(intent.reading_order[:5])}",
    ]
    blob = "|".join(parts)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _build_messages(
    packet: PlanningContext,
    schema: dict[str, Any],
    system_prompt: str,
    candidate_index: int,
    previous_signatures: list[str] | None = None,
) -> list[dict[str, str]]:
    rule_slice = packet.rule_slice
    rules_text = ""
    if rule_slice:
        rules_text = json.dumps({
            "global_rules": rule_slice.global_rules,
            "role_records": rule_slice.role_records,
            "color_policy": rule_slice.color_policy,
            "list_policy": rule_slice.list_policy,
            "extension_policy": rule_slice.extension_policy,
            "available_operators": rule_slice.available_operators,
            "unresolved_norms": rule_slice.unresolved_norms,
        }, ensure_ascii=False, indent=1)

    view_text = ""
    if packet.view:
        view_text = json.dumps({
            "objects": packet.view.objects,
            "semantic_slots": packet.view.semantic_slots,
            "uncertainties": packet.view.uncertainties,
        }, ensure_ascii=False, indent=1)

    schema_text = json.dumps(schema, ensure_ascii=False, indent=1)

    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": (
                "## Ontology Snapshot\n\n"
                + json.dumps({
                    "snapshot_id": packet.snapshot_id,
                    "ontology_corpus_hash": packet.ontology_corpus_hash,
                    "capabilities": packet.capabilities,
                    "preservation_policy": packet.preservation_policy,
                    "budget": packet.budget,
                }, ensure_ascii=False, indent=1)
            ),
        },
        {"role": "user", "content": f"## Planning Rules\n\n{rules_text}"},
        {"role": "user", "content": f"## Slide Content\n\n{view_text}"},
        {"role": "user", "content": f"## Response Schema\n\n{schema_text}"},
    ]
    if candidate_index > 0:
        prior = ", ".join(previous_signatures or packet.previous_signatures)
        messages.append({
            "role": "user",
            "content": (
                "Previous candidate composition signatures: "
                f"[{prior}]. Propose a DIFFERENT justified composition "
                f"(candidate {candidate_index + 1}): different topology/grouping/"
                "reading order where the source semantics allow it. Do not "
                "repeat the same signature. Do not repeat the same topology "
                "just to satisfy the request — keep the composition justified "
                "by the slide material."
            ),
        })
    return messages


def _strip_fence(content: str) -> str:
    """Strip markdown fence if present."""
    lines = content.strip().split("\n")
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines)


def _default_system_prompt() -> str:
    """Load the system instruction from the separate UTF-8 file (§8.3).

    Normative values are never embedded here; they arrive per request via
    the generated PlanningRuleSlice.
    """
    if not SYSTEM_PROMPT_PATH.is_file():
        raise ModelConfigurationError(
            "PLANNER/SYSTEM_PROMPT_MISSING",
            f"system prompt file not found: {SYSTEM_PROMPT_PATH}",
        )
    return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
