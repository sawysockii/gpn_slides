"""CLI handlers for plan-packet and plan commands (Stage 3.5 §11.2).

Both commands share one packet/schema/validation path. ``plan-packet``
never contacts a model; ``plan`` runs the configured provider — ``local``
(HTTP endpoint) or ``harness`` (answers supplied by the harness through
``model_responses/<request_id>.json``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import Any

from .errors import ExitCode, GpnError
from .local_llm import make_model_client, resolve_model_config
from .planner import (
    LocalPlanner,
    PlanningContext,
    PlanningRuleSlice,
    PlanningSlideView,
    build_intent_schema,
    safe_token,
    select_planning_candidate,
)

log = logging.getLogger(__name__)

EDITABLE_KINDS = ("text", "shape", "image")


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _progress(msg: str) -> None:
    print(msg, file=sys.stderr)


def _run_dir_of(args: Any, project_root: Path) -> Path:
    run_dir = Path(args.run_dir).expanduser()
    if not run_dir.is_absolute():
        run_dir = (project_root / run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _load_run_manifest(run_dir: Path) -> dict[str, Any]:
    manifest_path = run_dir / "run_manifest.json"
    if not manifest_path.is_file():
        raise GpnError(
            "RUN_MANIFEST_MISSING",
            f"run manifest not found: {manifest_path}; run `gpn import` first",
            exit_code=ExitCode.INVALID_INPUT,
        )
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _load_compiled_ontology(run_dir: Path) -> Any:
    from .models import CompiledOntology

    compiled_path = run_dir / "compiled_rules.json"
    if not compiled_path.is_file():
        raise GpnError(
            "COMPILED_RULES_MISSING",
            f"compiled rules not found: {compiled_path}; run `gpn profile` first",
            exit_code=ExitCode.INVALID_INPUT,
        )
    return CompiledOntology.model_validate_json(compiled_path.read_text(encoding="utf-8"))


def _load_deck(run_dir: Path) -> Any:
    source_ir_path = run_dir / "source_ir.json"
    if not source_ir_path.is_file():
        raise GpnError(
            "SOURCE_IR_MISSING",
            f"source IR not found: {source_ir_path}; run `gpn import` first",
            exit_code=ExitCode.INVALID_INPUT,
        )
    from .models import SourceDeckIR

    return SourceDeckIR.model_validate_json(source_ir_path.read_text(encoding="utf-8"))


def _load_ledger(run_dir: Path) -> Any | None:
    ledger_path = run_dir / "source_ledger.json"
    if not ledger_path.is_file():
        return None
    from .models import SourceLedger

    return SourceLedger.model_validate_json(ledger_path.read_text(encoding="utf-8"))


def _select_slide(deck: Any, slide_id: str | None) -> tuple[Any, str]:
    if slide_id is None:
        if not deck.slides:
            raise GpnError(
                "NO_SLIDES", "no slides in source", exit_code=ExitCode.INVALID_INPUT
            )
        slide = deck.slides[0]
        return slide, slide.id
    slide = next((s for s in deck.slides if s.id == slide_id), None)
    if slide is None:
        raise GpnError(
            "SLIDE_NOT_FOUND",
            f"slide {slide_id} not found",
            exit_code=ExitCode.INVALID_INPUT,
        )
    return slide, slide.id


def _source_pptx_from_manifest(manifest: dict[str, Any], deck: Any) -> Path | None:
    """Locate the source file and verify it still matches the imported hash."""
    raw = manifest.get("source_path")
    if not raw:
        return None
    path = Path(str(raw))
    if not path.is_file():
        return None
    current = hashlib.sha256(path.read_bytes()).hexdigest()
    if current != deck.source_sha256:
        raise GpnError(
            "RUN_MANIFEST_HASH_MISMATCH",
            f"source file changed since import: {path}",
            exit_code=ExitCode.INVALID_INPUT,
        )
    return path


def _object_entry(obj: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "ref": obj.id,
        "kind": obj.kind,
        "name": obj.name,
    }
    editable = obj.kind in EDITABLE_KINDS
    entry["editability"] = "editable_here" if editable else "frozen"
    if not editable:
        entry["editability_reason"] = (
            "diagnostic bridge supports geometry/style on native "
            f"text/shape/image only; kind={obj.kind} requires Stage 4 exporter"
        )

    payload = obj.payload
    paras = getattr(payload, "paragraphs", None)
    if paras:
        entry["text"] = ["".join(run.text for run in para.runs) for para in paras]

    if obj.kind == "table":
        cells = getattr(payload, "cells", None) or []
        entry["payload_descriptor"] = {
            "kind": "table",
            "cell_count": len(cells),
            "protected": True,
            "note": "values are protected payload; never rewritten by planning",
        }
    elif obj.kind == "chart":
        series = getattr(payload, "series", None) or []
        entry["payload_descriptor"] = {
            "kind": "chart",
            "series_count": len(series),
            "protected": True,
            "note": "points are protected payload; never rewritten by planning",
        }

    if obj.local_box is not None:
        entry["box_in"] = {
            "x": round(obj.local_box.x / 914400, 3),
            "y": round(obj.local_box.y / 914400, 3),
            "w": round(obj.local_box.w / 914400, 3),
            "h": round(obj.local_box.h / 914400, 3),
        }
    return entry


def _build_planning_view(slide: Any, slide_id: str) -> PlanningSlideView:
    objects = [_object_entry(obj) for obj in slide.objects]

    semantic_slots: list[dict[str, Any]] = []
    for obj in slide.objects:
        role = None
        payload = obj.payload
        role = getattr(payload, "role", None)
        if obj.id == getattr(slide, "title_ref", None):
            semantic_slots.append({"slot": "title", "ref": obj.id})
        elif role and role != "unknown":
            semantic_slots.append({"slot": str(role), "ref": obj.id})

    uncertainties = [
        {
            "code": getattr(u, "code", ""),
            "details": str(getattr(u, "details", ""))[:200],
        }
        for u in (getattr(slide, "uncertainties", None) or [])
    ]

    return PlanningSlideView(
        slide_id=slide_id,
        objects=objects,
        semantic_slots=semantic_slots,
        uncertainties=uncertainties,
    )


def _build_rule_slice(compiled: Any) -> PlanningRuleSlice:
    role_records = {}
    for role_id, role in compiled.roles.items():
        role_records[role_id] = {
            "allowed_faces": list(role.allowed_faces),
            "default_size_pt": role.default_size_pt,
            "color_tokens": list(role.color_tokens),
            "uppercase": role.uppercase,
            "status": role.status,
        }

    color_policy = {
        "colors": dict(compiled.colors),
        "conditional": {
            k: {"rgb": v.rgb, "allowed_context_ids": list(v.allowed_context_ids)}
            for k, v in compiled.conditional_colors.items()
        },
    }

    global_rules = []
    for rule_id, spec in compiled.rule_registry.specs.items():
        binding = compiled.rule_registry.bindings.get(rule_id)
        global_rules.append({
            "id": rule_id,
            "severity": spec.severity,
            "condition": spec.condition,
            "remedy": spec.remedy,
            "implementation_status": binding.implementation_status if binding else "unknown",
        })

    minimum = compiled.minimum_text_font_size
    list_policy = {}
    for list_id, profile in compiled.lists.items():
        list_policy[list_id] = {
            "marker": dict(profile.marker_settings),
            "nested_size_policy": (
                profile.nested_size_policy.model_dump()
                if profile.nested_size_policy else None
            ),
            "nested_text_color": profile.nested_text_color,
        }

    return PlanningRuleSlice(
        snapshot_id=compiled.ontology_corpus_hash,
        corpus_hash=compiled.ontology_corpus_hash,
        global_rules=global_rules,
        role_records=role_records,
        color_policy=color_policy,
        list_policy=list_policy,
        extension_policy={
            "style_new_tokens_allowed": compiled.extension_contract.style_new_tokens_allowed,
            "semantic_new_geometry_allowed": (
                compiled.extension_contract.semantic_new_geometry_allowed
            ),
        },
        available_operators=list(compiled.composition_operators),
        unresolved_norms=[
            f"minimum_text_font_size={minimum.value_pt}"
            if minimum else "minimum_text_font_size:unresolved"
        ],
        source_refs=[],
        omitted_sections=[
            "catalogs (only needed for Stage 6 reference retrieval)",
            "full markdown normative text (conditions/remedies inlined above)",
        ],
        estimated_tokens=0,
    )


def _make_packet(
    *,
    compiled: Any,
    deck: Any,
    view: PlanningSlideView,
    rule_slice: PlanningRuleSlice,
    capabilities: dict[str, Any],
) -> PlanningContext:
    required_refs = [obj["ref"] for obj in view.objects]
    return PlanningContext(
        packet_id=f"packet_{safe_token(view.slide_id)}",
        source_hash=deck.source_sha256,
        slide_id=view.slide_id,
        snapshot_id=compiled.ontology_corpus_hash,
        ontology_corpus_hash=compiled.ontology_corpus_hash,
        view=view,
        required_ref_groups=required_refs,
        rule_slice=rule_slice,
        capabilities=capabilities,
        preservation_policy={
            "content": "protected payload ids must be preserved",
            "text": "no rewriting, no dropping, no inventing facts",
        },
        budget={
            "context_budget_tokens": capabilities.get("context_budget_tokens", 0),
            "token_count_is_estimate": True,
            "estimator": "config value; exact tokenizer not available for provider",
            "max_output_tokens": capabilities.get("max_output_tokens", 0),
        },
    )


def _write_packet_artifacts(
    run_dir: Path, packet: PlanningContext, schema: dict[str, Any], rule_slice: PlanningRuleSlice
) -> tuple[Path, Path]:
    token = safe_token(packet.slide_id)
    packet_dir = run_dir / "packets"
    packet_dir.mkdir(parents=True, exist_ok=True)
    packet_path = packet_dir / f"{token}_packet.json"
    packet_path.write_text(
        json.dumps({
            "packet_id": packet.packet_id,
            "source_hash": packet.source_hash,
            "slide_id": packet.slide_id,
            "snapshot_id": packet.snapshot_id,
            "ontology_corpus_hash": packet.ontology_corpus_hash,
            "required_ref_groups": packet.required_ref_groups,
            "view": {
                "objects": packet.view.objects if packet.view else [],
                "semantic_slots": packet.view.semantic_slots if packet.view else [],
                "uncertainties": packet.view.uncertainties if packet.view else [],
            },
            "rule_slice": {
                "global_rules": rule_slice.global_rules,
                "role_records": rule_slice.role_records,
                "color_policy": rule_slice.color_policy,
                "list_policy": rule_slice.list_policy,
                "extension_policy": rule_slice.extension_policy,
                "available_operators": rule_slice.available_operators,
                "unresolved_norms": rule_slice.unresolved_norms,
                "omitted_sections": rule_slice.omitted_sections,
            },
            "capabilities": packet.capabilities,
            "preservation_policy": packet.preservation_policy,
            "budget": packet.budget,
            "examples": packet.examples,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    schema_dir = run_dir / "schemas"
    schema_dir.mkdir(parents=True, exist_ok=True)
    schema_path = schema_dir / f"{token}_schema.json"
    schema_path.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
    return packet_path, schema_path


def _attach_examples(project_root: Path, run_dir: Path, packet: PlanningContext) -> None:
    try:
        from .references import build_example_descriptors, retrieve_planning_examples

        descriptors = build_example_descriptors(project_root, run_dir)
        packet.examples = retrieve_planning_examples(packet.view, descriptors, k=2)
    except Exception as exc:  # noqa: BLE001 — examples are advisory, not blocking
        _progress(f"[gpn plan] examples unavailable: {exc}")
        packet.issues.append(f"examples_unavailable: {exc}")


def _resolve_capabilities(model_config: Any, compiled: Any) -> dict[str, Any]:
    implemented = [
        op for op in compiled.composition_operators
        if isinstance(op, str)
    ]
    return {
        "provider": model_config.provider,
        "implemented_operators": implemented,
        "context_budget_tokens": model_config.context_budget_tokens,
        "max_output_tokens": model_config.max_output_tokens,
        "use_json_schema": model_config.use_json_schema,
        "diagnostic_bridge": {
            "editable_kinds": list(EDITABLE_KINDS),
            "readonly_kinds": ["table", "chart", "group", "connector", "unknown"],
        },
    }


def _save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(payload, "model_dump"):
        path.write_text(payload.model_dump_json(indent=2), encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _collect_model_answers(run_dir: Path, request_ids: list[str]) -> list[dict[str, Any]]:
    """Read back the recorded answers for the given request ids.

    Only facts the provider actually reported are copied: model id, finish
    reason, usage availability and latency. Missing files stay missing.
    """
    out: list[dict[str, Any]] = []
    for request_id in request_ids:
        for folder in ("model_responses", "model_traces"):
            path = run_dir / folder / (
                f"{request_id}.json"
                if folder == "model_responses"
                else f"{request_id}_response.json"
            )
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                out.append({"request_id": request_id, "source": str(path),
                            "error": f"unreadable: {exc}"})
                continue
            if not isinstance(data, dict):
                continue
            entry = {
                "request_id": request_id,
                "source": str(path),
                "actual_model": data.get("actual_model") or data.get("model"),
                "finish_reason": data.get("finish_reason"),
                "usage_available": data.get("usage_available"),
                "usage": data.get("usage"),
                "latency_ms": data.get("latency_ms"),
                "response_hash": data.get("response_hash"),
                "schema_mode": data.get("schema_mode"),
            }
            out.append(entry)
            break
    return out


def cmd_plan_packet(args: Any, config: Any, project_root: Path) -> int:
    """Build planning packet + schema without calling the model."""
    try:
        run_dir = _run_dir_of(args, project_root)
        _progress(f"[gpn plan-packet] run_dir={run_dir} slide_id={args.slide_id}")

        manifest = _load_run_manifest(run_dir)
        compiled = _load_compiled_ontology(run_dir)
        deck = _load_deck(run_dir)
        slide, slide_id = _select_slide(deck, args.slide_id)
        source_pptx = _source_pptx_from_manifest(manifest, deck)

        model_config = resolve_model_config(config)
        view = _build_planning_view(slide, slide_id)
        rule_slice = _build_rule_slice(compiled)
        capabilities = _resolve_capabilities(model_config, compiled)
        packet = _make_packet(
            compiled=compiled, deck=deck, view=view,
            rule_slice=rule_slice, capabilities=capabilities,
        )
        _attach_examples(project_root, run_dir, packet)

        schema = build_intent_schema(packet, compiled, packet.capabilities)
        packet_path, schema_path = _write_packet_artifacts(run_dir, packet, schema, rule_slice)

        _emit({
            "ok": True,
            "command": "plan-packet",
            "slide_id": slide_id,
            "packet_path": str(packet_path),
            "schema_path": str(schema_path),
            "objects": len(view.objects),
            "required_refs": len(packet.required_ref_groups),
            "rules": len(rule_slice.global_rules),
            "operators": len(rule_slice.available_operators),
            "examples": len(packet.examples),
            "source_pptx": str(source_pptx) if source_pptx else None,
            "provider": model_config.provider,
        })
        return int(ExitCode.COMPLETED)

    except GpnError as exc:
        _emit({"ok": False, "error": {"code": exc.code, "message": exc.message}})
        return int(exc.exit_code)
    except Exception as exc:  # noqa: BLE001
        _emit({"ok": False, "error": {"code": "INTERNAL_ERROR", "message": str(exc)}})
        return int(ExitCode.INVALID_INPUT)


def _plan_cache_path(run_dir: Path) -> Path:
    return run_dir / "plan_cache.json"


def _cache_key(packet_hash: str, schema_hash: str, count: int, provider: str) -> str:
    return hashlib.sha256(
        f"{packet_hash}:{schema_hash}:{count}:{provider}".encode()
    ).hexdigest()[:32]


def _load_cache(run_dir: Path, key: str) -> list[dict[str, Any]] | None:
    path = _plan_cache_path(run_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    entry = data.get(key)
    if not isinstance(entry, dict):
        return None
    return entry.get("candidates")


def _store_cache(run_dir: Path, key: str, candidates: list[dict[str, Any]]) -> None:
    path = _plan_cache_path(run_dir)
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
    data[key] = {"candidates": candidates}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def cmd_plan(args: Any, config: Any, project_root: Path) -> int:
    """Run LLM planning on a slide via the configured provider."""
    try:
        run_dir = _run_dir_of(args, project_root)
        slide_id = getattr(args, "slide_id", None)
        candidates = int(getattr(args, "candidates", 1) or 1)
        diagnostic = bool(getattr(args, "diagnostic_candidate", False))
        no_cache = bool(getattr(args, "no_plan_cache", False))
        provider_override = getattr(args, "llm_provider", None)

        _progress(
            f"[gpn plan] run_dir={run_dir} slide_id={slide_id} "
            f"candidates={candidates} provider={provider_override or 'config'}"
        )

        manifest = _load_run_manifest(run_dir)
        compiled = _load_compiled_ontology(run_dir)
        deck = _load_deck(run_dir)
        ledger = _load_ledger(run_dir)
        slide, slide_id = _select_slide(deck, slide_id)
        source_pptx = _source_pptx_from_manifest(manifest, deck)

        model_config = resolve_model_config(config)
        if provider_override:
            model_config.provider = provider_override
        wait_override = getattr(args, "harness_wait_seconds", None)
        if wait_override is not None:
            model_config.harness_wait_seconds = max(0.0, float(wait_override))

        client = make_model_client(
            model_config, run_dir=run_dir, provider=model_config.provider,
            wait_seconds=(
                model_config.harness_wait_seconds
                if model_config.provider == "harness" else None
            ),
        )
        caps = client.probe_capabilities()

        _save_json(run_dir / "model_config_redacted.json", {
            "provider": model_config.provider,
            "base_url": model_config.base_url if model_config.provider == "local" else None,
            "model_id": model_config.model_id,
            "temperature": model_config.temperature,
            "max_output_tokens": model_config.max_output_tokens,
            "timeout_seconds": model_config.timeout_seconds,
            "max_retries": model_config.max_retries,
            "concurrency": model_config.concurrency,
            "use_json_schema": model_config.use_json_schema,
            "context_budget_tokens": model_config.context_budget_tokens,
            "harness_wait_seconds": model_config.harness_wait_seconds,
            "credentials_present": bool(model_config.api_key),
            "issues": list(model_config.issues),
        })
        _save_json(run_dir / "model_capabilities.json", {
            "provider": caps.provider,
            "reachable": caps.reachable,
            "confirmed_by": caps.confirmed_by,
            "listed_model_ids": list(caps.listed_model_ids),
            "selected_model_available": caps.selected_model_available,
            "chat_completions": caps.chat_completions,
            "json_object": caps.json_object,
            "json_schema": caps.json_schema,
            "context_limit": caps.context_limit,
            "unknown_fields": list(caps.unknown_fields),
        })

        if model_config.provider == "local" and not caps.reachable:
            _emit({
                "ok": False,
                "error": {
                    "code": "MODEL/UNAVAILABLE",
                    "message": f"endpoint not reachable: {model_config.base_url}",
                },
            })
            return int(ExitCode.MODEL_UNAVAILABLE)

        view = _build_planning_view(slide, slide_id)
        rule_slice = _build_rule_slice(compiled)
        capabilities = _resolve_capabilities(model_config, compiled)
        packet = _make_packet(
            compiled=compiled, deck=deck, view=view,
            rule_slice=rule_slice, capabilities=capabilities,
        )
        _attach_examples(project_root, run_dir, packet)

        schema = build_intent_schema(packet, compiled, packet.capabilities)
        packet_path, schema_path = _write_packet_artifacts(run_dir, packet, schema, rule_slice)

        schema_hash = hashlib.sha256(
            json.dumps(schema, sort_keys=True).encode()
        ).hexdigest()
        cache_key = _cache_key(deck.source_sha256, schema_hash, candidates,
                               model_config.provider)

        planner = LocalPlanner(client, model_config, run_dir=run_dir, rules=compiled)
        cached = None if no_cache else _load_cache(run_dir, cache_key)
        cache_hit = cached is not None

        if cache_hit:
            _progress("[gpn plan] plan cache hit; no generation requests")
            planner.register_cache_hit()
            candidates_list = _candidates_from_cache(
                cached or [], packet, compiled, model_config.provider, ledger
            )
        else:
            candidates_list = planner.plan(
                packet,
                schema,
                candidate_count=candidates,
                source=slide,
                ledger=ledger,
            )
            serialised = _candidates_to_cache(candidates_list)
            if serialised:
                _store_cache(run_dir, cache_key, serialised)

        candidates_dir = run_dir / "candidates"
        candidates_dir.mkdir(parents=True, exist_ok=True)
        validation_dir = run_dir / "intent_validation"
        validation_dir.mkdir(parents=True, exist_ok=True)

        valid_candidates = [c for c in candidates_list if c.valid and c.intent is not None]
        rejected = [c for c in candidates_list if not c.valid]

        candidate_payloads = []
        for cand in candidates_list:
            payload = {
                "candidate_id": cand.candidate_id,
                "origin": cand.origin,
                "request_id": cand.request_id,
                "diversity_signature": cand.diversity_signature,
                "valid": cand.valid,
                "errors": list(cand.errors),
                "intent": cand.intent.model_dump(mode="json") if cand.intent else None,
            }
            candidate_payloads.append(payload)
            _save_json(candidates_dir / f"{cand.candidate_id}.json", payload)
            if cand.validation is not None:
                _save_json(validation_dir / f"{cand.candidate_id}.json", cand.validation)

        selection = select_planning_candidate(
            candidates_list,
            previous=[c.diversity_signature for c in valid_candidates[:-1]],
        )

        diversity_sigs = {c.diversity_signature for c in valid_candidates}
        diversity_report = {
            "slide_id": slide_id,
            "distinct_signatures": sorted(diversity_sigs),
            "diversity_demonstrated": len(diversity_sigs) >= 2,
            "note": (
                "signatures are topology/grouping/dominant/order/operator based; "
                "id/color permutations do not count"
            ),
        }
        _save_json(run_dir / "diversity_report.json", diversity_report)

        stats = planner.stats
        consumed_ids = sorted({
            rid for rid in (c.request_id for c in candidates_list) if rid
        })
        usage = {
            "provider": model_config.provider,
            "generation_requests": stats["llm_generation_requests"],
            "transport_retries": stats["transport_retries"],
            "schema_repairs": stats["schema_repairs"],
            "cache_hits": stats["cache_hits"],
            "accepted_candidates": stats["accepted_candidates"],
            "rejected_candidates": stats["rejected_candidates"],
            "requested_model": model_config.model_id,
            "consumed_request_ids": list(
                getattr(client, "consumed_request_ids", []) or []
            ) if model_config.provider == "harness" else [],
            "candidate_request_ids": consumed_ids,
            "model_answers": _collect_model_answers(run_dir, consumed_ids),
        }
        _save_json(run_dir / "model_usage.json", usage)

        selected_payload = None
        if selection.candidate is not None and selection.candidate.intent is not None:
            selected_payload = {
                "slide_id": slide_id,
                "candidate_id": selection.candidate.candidate_id,
                "origin": selection.candidate.origin,
                "status": selection.status,
                "reason": selection.reason,
                "scores": selection.scores,
                "diversity_signature": selection.candidate.diversity_signature,
                "intent": selection.candidate.intent.model_dump(mode="json"),
                "validation": (
                    selection.candidate.validation.model_dump(mode="json")
                    if selection.candidate.validation else None
                ),
            }
            _save_json(
                run_dir / "selected_plans" / f"{safe_token(slide_id)}.json",
                selected_payload,
            )

        result_payload: dict[str, Any] = {
            "ok": bool(valid_candidates),
            "command": "plan",
            "slide_id": slide_id,
            "model": {
                "provider": model_config.provider,
                "requested": model_config.model_id,
                "endpoint": model_config.base_url if model_config.provider == "local" else None,
                "reachable": caps.reachable,
                "confirmed_by": caps.confirmed_by,
                "selected_model_available": caps.selected_model_available,
            },
            "packet_path": str(packet_path),
            "schema_path": str(schema_path),
            "candidates_total": len(candidates_list),
            "candidates_valid": len(valid_candidates),
            "candidates_rejected": len(rejected),
            "selection": {
                "status": selection.status,
                "reason": selection.reason,
                "candidate_id": (
                    selection.candidate.candidate_id if selection.candidate else None
                ),
                "scores": selection.scores,
            },
            "diversity_demonstrated": diversity_report["diversity_demonstrated"],
            "usage": usage,
            "candidates": candidate_payloads,
            "rejected": [
                {"candidate_id": c.candidate_id, "errors": list(c.errors)} for c in rejected
            ],
        }

        if diagnostic:
            result_payload["diagnostic"] = _run_diagnostic_bridge(
                run_dir=run_dir,
                compiled=compiled,
                deck=deck,
                slide=slide,
                ledger=ledger,
                packet=packet,
                selection=selection,
                source_pptx=source_pptx,
            )

        _emit(result_payload)

        if not valid_candidates:
            return int(ExitCode.VALIDATION_FAILED)
        return int(ExitCode.COMPLETED)

    except GpnError as exc:
        _emit({"ok": False, "error": {"code": exc.code, "message": exc.message}})
        return int(exc.exit_code)
    except Exception as exc:  # noqa: BLE001
        _emit({"ok": False, "error": {"code": "INTERNAL_ERROR", "message": str(exc)}})
        return int(ExitCode.INVALID_INPUT)


def _run_diagnostic_bridge(
    *,
    run_dir: Path,
    compiled: Any,
    deck: Any,
    slide: Any,
    ledger: Any,
    packet: PlanningContext,
    selection: Any,
    source_pptx: Path | None,
) -> dict[str, Any]:
    from .patching import GpnEditContext
    from .planning_bridge import (
        apply_diagnostic_intent,
        lower_intent_to_patch_batch,
        resolve_diagnostic_layout,
    )

    if selection.candidate is None or selection.candidate.intent is None:
        return {"status": "no_selected_candidate"}
    if source_pptx is None:
        return {"status": "source_pptx_missing", "issues": ["run manifest has no source_path"]}

    intent = selection.candidate.intent
    profile = _load_template_profile(run_dir)

    edit_context = GpnEditContext(
        compiled_rules=compiled,
        source_ir=deck,
        ledger=ledger,
        template_profile=profile,
        corpus_hashes={"ontology_corpus_hash": compiled.ontology_corpus_hash},
    )

    resolved = resolve_diagnostic_layout(
        intent, slide, profile, compiled, packet.capabilities
    )

    try:
        proposal = lower_intent_to_patch_batch(
            resolved, slide, ledger, edit_context, source_pptx=source_pptx
        )
    except Exception as exc:  # noqa: BLE001 — typed refusal surfaces as result
        return {
            "status": "bridge_incomplete",
            "issues": list(resolved.issues) + [str(exc)],
        }

    _save_json(run_dir / "operation_batch.json", proposal.model_dump(mode="json"))
    _save_json(run_dir / "intent_to_operation_map.json", {
        "slide_id": resolved.slide_id,
        "content_box": resolved.content_box,
        "content_box_verified": resolved.content_box_verified,
        "content_box_source": resolved.content_box_source,
        "readability_status": resolved.readability_status,
        "issues": list(resolved.issues) + list(proposal.issues),
        "intent_to_operation_map": proposal.intent_to_operation_map,
        "expected_source_hash": proposal.expected_source_hash,
    })

    if not proposal.operation_batch.get("operations"):
        return {
            "status": "no_operations",
            "issues": list(resolved.issues) + list(proposal.issues),
        }

    candidate_output = run_dir / "diagnostic_candidate.pptx"
    build_result = apply_diagnostic_intent(
        proposal, edit_context, run_dir,
        source=source_pptx,
        candidate_output=candidate_output,
        overwrite=True,
    )
    return {
        "status": "applied" if not build_result.rolled_back else "refused",
        "candidate_path": build_result.candidate_path,
        "committed_count": build_result.committed_count,
        "rolled_back": build_result.rolled_back,
        "import_diff": build_result.import_diff,
        "package_diff": build_result.package_diff,
        "statuses": build_result.statuses,
        "issues": list(resolved.issues) + list(proposal.issues),
    }


def _load_template_profile(run_dir: Path) -> Any:
    path = run_dir / "template_profile.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _progress(f"[gpn plan] template profile unreadable: {exc}")
        return None
    if isinstance(data, dict) and data.get("content_box"):
        # Explicit verified test template for diagnostic fixtures (§10.2).
        return data
    from .models import TemplateProfile

    try:
        return TemplateProfile.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        _progress(f"[gpn plan] template profile invalid: {exc}")
        return None


def _candidates_to_cache(candidates: list[Any]) -> list[dict[str, Any]]:
    serialised = []
    for cand in candidates:
        if cand.intent is None:
            continue
        serialised.append({
            "candidate_id": cand.candidate_id,
            "origin": cand.origin,
            "request_id": cand.request_id,
            "diversity_signature": cand.diversity_signature,
            "intent": cand.intent.model_dump(mode="json"),
            "validation": (
                cand.validation.model_dump(mode="json") if cand.validation else None
            ),
        })
    return serialised


def _candidates_from_cache(
    entries: list[dict[str, Any]],
    packet: PlanningContext,
    compiled: Any,
    provider: str,
    ledger: Any,
) -> list[Any]:
    """Re-validate cached candidates; origin becomes cached_* per provider."""
    from .planner import (
        IntentCandidate,
        IntentValidationReport,
        LayoutIntent,
        compute_diversity_signature,
        validate_planned_intent,
    )

    cached_origin = "cached_local_model" if provider == "local" else "cached_harness_model"
    out = []
    for entry in entries:
        try:
            intent = LayoutIntent.model_validate(entry["intent"])
        except Exception as exc:  # noqa: BLE001 — stale cache must not crash
            out.append(IntentCandidate(
                intent=None, origin=cached_origin,
                request_id=str(entry.get("request_id", "")),
                candidate_id=str(entry.get("candidate_id", "")),
                valid=False, errors=[f"cached candidate unreadable: {exc}"],
            ))
            continue
        report_data = entry.get("validation")
        if report_data:
            report = IntentValidationReport.model_validate(report_data)
        else:
            report = validate_planned_intent(
                intent, packet, None, ledger, compiled, packet.capabilities
            )
        out.append(IntentCandidate(
            intent=intent,
            origin=cached_origin,
            request_id=str(entry.get("request_id", "")),
            candidate_id=str(entry.get("candidate_id", "")),
            valid=report.planning_acceptable,
            errors=list(report.hard_issues),
            diversity_signature=entry.get("diversity_signature")
            or compute_diversity_signature(intent),
            validation=report,
        ))
    return out
