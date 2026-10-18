"""Stage 5 §12.1: measured build through the shared native exporter.

``run_measured_build`` extends the Stage 4 build path (no second exporter):
accepted intents -> composition trees -> measured resolution with exact
local fonts -> feasible-candidate selection -> the existing native exporter
-> independent saved-bytes checks (geometry + remeasurement).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from pathlib import Path
from typing import Any


def _slide_text(slide: Any) -> str:
    parts: list[str] = []
    for obj in list(getattr(slide, "objects", []) or []):
        payload = getattr(obj, "payload", None)
        for container in (
            getattr(payload, "paragraphs", None),
            getattr(getattr(payload, "text", None), "paragraphs", None),
        ):
            for para in list(container or []):
                for run in list(getattr(para, "runs", []) or []):
                    parts.append(getattr(run, "text", "") or "")
    return "".join(parts)


def _resume_if_unchanged(
    run_dir: Path, slide_id: str, candidate_id: str, tree: Any
) -> dict[str, Any] | None:
    """Resume: reuse a verified layout result when inputs are unchanged.

    The stored result is accepted only when its tree hash matches the
    current tree; changed inputs recompute instead of reusing stale
    geometry. Measurement reuse additionally flows through the verified
    measurement cache.
    """
    import hashlib as _hashlib

    safe = slide_id.replace("/", "_")
    stored = run_dir / "layout" / "resolved" / f"{safe}-{candidate_id}.json"
    if not stored.is_file():
        return None
    try:
        from .models import LayoutResolutionResult

        cached = LayoutResolutionResult.model_validate_json(stored.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - corrupt checkpoint recomputes
        return None
    current_hash = _hashlib.sha256(tree.model_dump_json().encode()).hexdigest()
    if cached.evidence_hashes.get("tree") != current_hash:
        return None
    return {
        "candidate_id": candidate_id,
        "status": cached.status,
        "origin": cached.resolved_slide.get("generation_origin", ""),
        "unresolved": list(cached.unresolved),
        "attempts": len(cached.attempts),
        "result_file": str(stored),
        "resumed": True,
        "resolved": cached.resolved_slide if cached.status in ("resolved", "needs_review") else {},
    }


def _image_natural_pt(run_dir: Path, slides: list[Any]) -> dict[str, dict[str, float]]:
    """Natural image sizes in Pt from run asset bytes (§10.2).

    Aspect preservation needs the true media size, never a guessed box.
    Unreadable assets are reported by omission (the resolver flags them).
    """
    out: dict[str, dict[str, float]] = {}
    try:
        from PIL import Image as _Image
    except ImportError:
        return out
    for slide in slides:
        for obj in list(getattr(slide, "objects", []) or []):
            if getattr(obj, "kind", "") != "image":
                continue
            payload = getattr(obj, "payload", None)
            asset = getattr(payload, "asset", None)
            rel = getattr(asset, "relative_path", "") if asset else ""
            if not rel:
                continue
            for base in (run_dir / "assets", run_dir / "candidate_assets"):
                candidate = base / rel
                if candidate.is_file():
                    break
            else:
                continue
            try:
                with _Image.open(candidate) as im:
                    w_px, h_px = im.size
                dpi = 96.0
                out[str(getattr(obj, "id", ""))] = {"w": w_px * 72.0 / dpi, "h": h_px * 72.0 / dpi}
            except Exception:  # noqa: BLE001 - omission flags the gap
                continue
    return out


def _candidate_plans_for_slide(
    intents: list[dict[str, Any]], slide_id: str
) -> list[dict[str, Any]]:
    out = []
    for raw in intents:
        body = raw.get("intent", raw) if isinstance(raw, dict) else raw
        sid = body.get("slide_id") if isinstance(body, dict) else getattr(body, "slide_id", "")
        if sid == slide_id:
            out.append(raw)
    return out


def _plan_candidate_id(raw: dict[str, Any]) -> str:
    body = raw.get("intent", raw) if isinstance(raw, dict) else raw
    if isinstance(body, dict):
        return str(body.get("candidate_id", "") or "")
    return str(getattr(body, "candidate_id", "") or "")


def run_measured_build(run_dir, plans_dir, config, options, layout_options=None):
    """Public boundary with actual solver counters, including failed builds."""
    from .assets import atomic_write_json

    runtime = {"solver_calls": 0}
    opts = {**(layout_options or {}), "runtime_counters": runtime}
    try:
        return _run_measured_build(run_dir, plans_dir, config, options, opts)
    finally:
        atomic_write_json(Path(run_dir) / "runtime_counters.json", runtime)


def _run_measured_build(
    run_dir: str | Path,
    plans_dir: str | Path,
    config: Any,
    options: Any,
    layout_options: dict[str, Any] | None = None,
) -> Any:
    """Measured build: resolve with Chromium, export natively, verify saved."""
    from .export_models import ExportOptions, NativeBuildResult, ResolvedSlide
    from .export_pipeline import (
        PROVIDER_LOCAL,
        _emit_resolved_slides,
        _fail,
        _find_template,
        _load_accepted_plans,
        _read_json,
        resolve_llm_provider,
        scope_ledger_for_pilot,
    )

    run_dir = Path(run_dir)
    plans_dir = Path(plans_dir)
    layout_options = dict(layout_options or {})
    artifacts: dict[str, str] = {}
    issues: list[str] = []

    provider, origin_note = resolve_llm_provider(config)
    if provider == PROVIDER_LOCAL:
        result = NativeBuildResult(
            operation_status="invalid_input",
            llm_provider=provider,
            plan_generation_origin="deferred_local_server",
            issues=[
                "MODEL_LOCAL_DEFERRED: local_server adapter not "
                "implemented; no harness fallback performed"
            ],
            artifacts={},
        )
        from .assets import atomic_write_json

        atomic_write_json(run_dir / "stage5_readiness.json", result)
        return result

    project_root = getattr(config, "project_root", Path("."))
    project_root = Path(project_root)
    run_dir.mkdir(parents=True, exist_ok=True)

    layout_cfg = getattr(getattr(config, "layout", None), "__dict__", {}) or {}
    if not isinstance(layout_cfg, dict):
        try:
            layout_cfg = dict(layout_cfg)
        except (TypeError, ValueError):
            layout_cfg = {}
    max_attempts = int(
        layout_options.get(
            "max_local_layout_attempts", layout_cfg.get("max_local_layout_attempts", 6)
        )
    )
    layout_mode = str(
        layout_options.get(
            "layout_mode", getattr(getattr(config, "policy", None), "mode", "recompose")
        )
    )
    use_cache = not bool(layout_options.get("no_measurement_cache", False))
    resume = bool(layout_options.get("resume", False))
    profile_kind = str(layout_options.get("profile_kind", "production"))
    candidate_name = str(layout_options.get("candidate_name", "candidate.pptx"))

    raw_scope = layout_options.get("selected_slide_ids")
    scope_selection = [str(i) for i in raw_scope] if raw_scope is not None else None
    if raw_scope is not None and (
        not isinstance(raw_scope, list) or not raw_scope or len(set(raw_scope)) != len(raw_scope)
    ):
        return _fail(run_dir, provider, "INVALID_SLIDE_SCOPE", artifacts)
    scope_doc = _read_json(run_dir / "pilot_selection.json")
    if (
        scope_selection is None
        and isinstance(scope_doc, dict)
        and scope_doc.get("selected_slide_ids")
    ):
        scope_selection = [str(i) for i in scope_doc["selected_slide_ids"]]

    try:
        from .models import (
            CompiledOntology,
            FontInventory,
            SourceDeckIR,
            SourceLedger,
            TemplateProfile,
        )

        deck = SourceDeckIR.model_validate_json(
            (run_dir / "source_ir.json").read_text(encoding="utf-8")
        )
        ledger = SourceLedger.model_validate_json(
            (run_dir / "source_ledger.json").read_text(encoding="utf-8")
        )
        rules = CompiledOntology.model_validate_json(
            (run_dir / "compiled_rules.json").read_text(encoding="utf-8")
        )
    except OSError as exc:
        return _fail(run_dir, provider, f"missing run inputs: {exc}", artifacts)
    profile = None
    for name in ("template_profile.json", "profiles/template_profile.json"):
        candidate = run_dir / name
        if candidate.is_file():
            try:
                profile = TemplateProfile.model_validate_json(candidate.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                profile = None
            break
    inventory = None
    inv_path = run_dir / "font_inventory.json"
    if inv_path.is_file():
        try:
            inventory = FontInventory.model_validate_json(inv_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            inventory = None
    if inventory is None:
        return _fail(
            run_dir, provider, "missing font_inventory.json (run profile first)", artifacts
        )
    manifest = _read_json(run_dir / "run_manifest.json")
    source_path = (manifest or {}).get("source_path", "")

    intents, plan_origin = _load_accepted_plans(plans_dir)
    if not intents:
        return _fail(run_dir, provider, "no accepted plans in plans_dir", artifacts)

    from .assets import atomic_write_json
    from .composition import compile_composition
    from .layout import resolve_layout
    from .measurement import measure_components
    from .typography import prepare_measurement_fonts

    template_path = _find_template(run_dir, project_root)
    template_hash = ""
    if template_path is not None:
        template_hash = hashlib.sha256(template_path.read_bytes()).hexdigest()[:16]

    def measurer(request):
        out = measure_components(
            request,
            timeout_seconds=180.0,
            project_root=project_root,
            use_cache=use_cache,
            debug_output_dir=str(run_dir / "measurement" / "debug"),
            persist_dir=run_dir / "measurement",
        )
        return out["response"]

    known_slide_ids = {slide.id for slide in deck.slides}
    if scope_selection is not None and not set(scope_selection).issubset(known_slide_ids):
        return _fail(run_dir, provider, "SLIDE_SCOPE_UNKNOWN_ID", artifacts)
    slides = [s for s in deck.slides if scope_selection is None or s.id in scope_selection]
    verification_scope = "full_source"
    if scope_selection is not None:
        # §13.3 pilot scoping: content_diff must compare the declared
        # subset, never the whole 7456-atom source ledger (an empty
        # expected set would be no proof; a huge unbound one is not a
        # failure either).
        ledger, verification_scope = scope_ledger_for_pilot(deck, ledger, scope_selection, run_dir)
    resolved: list[ResolvedSlide] = []
    resolved_trees: list[Any] = []
    trees_by_candidate: dict[tuple[str, str], Any] = {}
    evaluations: list[dict[str, Any]] = []
    tracker: dict[str, Any] = {"consumed": 0, "max_total": max_attempts * 3}
    selection = layout_options.get("selection") or None
    if selection is not None:
        selection = {str(k): str(v) for k, v in selection.items()}

    for slide in slides:
        slide_plans = _candidate_plans_for_slide(intents, slide.id)
        if not slide_plans:
            issues.append(f"NO_PLAN: slide {slide.id} has no accepted intent")
            continue
        # §1.6/§10.3: an explicit selection map controls which candidate is
        # built per slide — no status-sort/first-wins fallback.
        if selection is not None:
            wanted = selection.get(slide.id, "")
            slide_plans = [p for p in slide_plans if _plan_candidate_id(p) == wanted]
            if not slide_plans:
                issues.append(f"SELECTED_PLAN_MISSING:{slide.id}:{wanted}")
                evaluations.append(
                    {
                        "slide_id": slide.id,
                        "selected": None,
                        "status": "selected_plan_missing",
                        "candidates": [],
                    }
                )
                continue
        slide_results: list[dict[str, Any]] = []
        for raw in slide_plans:
            origin = ""
            body = raw
            if isinstance(raw, dict):
                origin = str(raw.get("generation_origin", "harness_model"))
                body = {k: v for k, v in raw.items() if k != "generation_origin"}
            from .planner import LayoutIntent

            intent = body if not isinstance(body, dict) else LayoutIntent.model_validate(body)
            candidate_id = getattr(intent, "candidate_id", "") or "c0"
            comp_dir = run_dir / "composition" / slide.id.replace("/", "_")
            comp_dir.mkdir(parents=True, exist_ok=True)
            lay_dir = run_dir / "layout" / "resolved"
            lay_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "layout" / "attempts").mkdir(parents=True, exist_ok=True)
            try:
                tree = compile_composition(
                    intent, slide, ledger, rules, profile, template_hash=template_hash
                )
            except ValueError as exc:
                slide_results.append(
                    {
                        "candidate_id": candidate_id,
                        "status": "refused",
                        "reason": str(exc),
                        "origin": origin,
                    }
                )
                continue
            atomic_write_json(comp_dir / f"{candidate_id}.json", tree)
            trees_by_candidate[(slide.id, candidate_id)] = tree
            if resume:
                reuse = _resume_if_unchanged(run_dir, slide.id, candidate_id, tree)
                if reuse is not None:
                    slide_results.append(reuse)
                    continue
            bundle = prepare_measurement_fonts(
                inventory, _slide_text(slide), profile_kind=profile_kind
            )
            if bundle.missing_faces and profile_kind == "production":
                slide_results.append(
                    {
                        "candidate_id": candidate_id,
                        "status": "needs_assets",
                        "reason": "; ".join(bundle.issues),
                        "origin": origin,
                    }
                )
                continue
            res = resolve_layout(
                tree,
                slide,
                ledger,
                rules,
                profile,
                bundle,
                measurer,
                {
                    "project_root": project_root,
                    "font_allowlist_roots": layout_options.get("font_allowlist_roots", []),
                    "use_cache": use_cache,
                    "profile_kind": profile_kind,
                    "max_local_layout_attempts": max_attempts,
                    "layout_mode": layout_mode,
                    "generation_origin": origin or "harness_model",
                    "budget_tracker": tracker,
                    "runtime_counters": layout_options.get("runtime_counters"),
                    "debug_output_dir": str(run_dir / "measurement" / "debug"),
                    "image_natural_pt": _image_natural_pt(run_dir, [slide]),
                },
            )
            safe_id = slide.id.replace("/", "_")
            atomic_write_json(
                run_dir / "layout" / "attempts" / f"{safe_id}-{candidate_id}.json",
                {
                    "attempts": [a.model_dump(mode="json") for a in res.attempts],
                    "unresolved": res.unresolved,
                },
            )
            atomic_write_json(lay_dir / f"{slide.id.replace('/', '_')}-{candidate_id}.json", res)
            slide_results.append(
                {
                    "candidate_id": candidate_id,
                    "status": res.status,
                    "origin": origin,
                    "unresolved": res.unresolved,
                    "attempts": len(res.attempts),
                    "result_file": str(
                        lay_dir / f"{slide.id.replace('/', '_')}-{candidate_id}.json"
                    ),
                    "resolved": res.resolved_slide
                    if res.status in ("resolved", "needs_review")
                    else {},
                }
            )
        order = {"resolved": 0, "needs_review": 1, "needs_assets": 2, "refused": 3, "infeasible": 4}
        if selection is not None:
            # Selected candidate is authoritative; still record the outcome.
            best = (
                slide_results[0]
                if slide_results
                else {
                    "candidate_id": selection.get(slide.id, ""),
                    "status": "refused",
                    "reason": "selected plan not found",
                }
            )
        else:
            slide_results.sort(key=lambda r: order.get(r["status"], 9))
            best = slide_results[0]
        evaluations.append(
            {
                "slide_id": slide.id,
                "candidates": [
                    {k: v for k, v in r.items() if k != "resolved"} for r in slide_results
                ],
                "selected": best["candidate_id"],
                "status": best["status"],
            }
        )
        if best["status"] in ("resolved", "needs_review") and best.get("resolved"):
            resolved.append(ResolvedSlide.model_validate(best["resolved"]))
            tree = trees_by_candidate.get((slide.id, best["candidate_id"]))
            if tree is not None:
                resolved_trees.append(tree)
            if best["status"] == "needs_review":
                issues.append(f"SLIDE_NEEDS_REVIEW: {slide.id}: {best.get('unresolved', [])[:2]}")
        else:
            issues.append(
                f"SLIDE_UNRESOLVED: {slide.id}: {best['status']} "
                f"{best.get('reason', best.get('unresolved', ''))}"
            )
    atomic_write_json(run_dir / "candidate_evaluations.json", evaluations)
    artifacts["candidate_evaluations"] = str(run_dir / "candidate_evaluations.json")
    if not resolved:
        return _fail(
            run_dir, provider, f"no feasible slides: {issues[:3]}", artifacts, origin=plan_origin
        )

    build_options = options
    if not isinstance(build_options, ExportOptions):
        build_options = ExportOptions(
            workflow=getattr(options, "workflow", "presentation"),
            diagnostic=True,
            overwrite=bool(getattr(options, "overwrite", False)),
        )
    result = _emit_resolved_slides(
        run_dir,
        config,
        build_options,
        deck=deck,
        ledger=ledger,
        rules=rules,
        profile=profile,
        intents=intents,
        resolved=resolved,
        verification_scope=verification_scope,
        provider=provider,
        origin_note=origin_note,
        plan_origin=plan_origin,
        source_path=source_path,
        artifacts=artifacts,
        issues=issues,
        readiness_name="stage5_readiness.json",
        candidate_name=candidate_name,
        unresolved_scopes=["render", "powerpoint_ui"],
        extra_result_fields={
            "layout_resolved": True,
            "measurement_verified_for_profile": profile_kind,
            "fonts_discovered": True,
            "fonts_browser_verified": True,
        },
    )
    if result.candidate_path and Path(result.candidate_path).is_file():
        from .layout import _content_box_pt

        content_box_pt, _ = _content_box_pt(profile, rules)
        _write_saved_checks(
            run_dir, resolved, result, layout_options, measurer, content_box_pt, resolved_trees
        )
        if layout_options.get("render"):
            _run_native_render(run_dir, result, resolved, rules, inventory, layout_options)
    return result


def _run_native_render(
    run_dir: Path,
    result: Any,
    resolved: list[Any],
    rules: Any,
    inventory: Any,
    layout_options: dict[str, Any],
) -> None:
    """§12.3 native render probe on the FINAL candidate bytes."""
    from .assets import atomic_write_json
    from .render import (
        build_calibration_fixture,
        calibrate_measurement,
        candidate_typefaces,
        measure_fixture_in_browser,
        native_text_bboxes,
        render_native_candidate,
    )
    from .typography import prepare_measurement_fonts

    bundle = prepare_measurement_fonts(
        inventory, "", profile_kind=str(layout_options.get("profile_kind", "production"))
    )
    # Expected faces come from the FINAL saved bytes (declared latin
    # faces in slide XML), never from an empty-text bundle that binds
    # every inventory face including unused symbol fonts.
    try:
        expected_faces = candidate_typefaces(result.candidate_path)
    except Exception:  # noqa: BLE001 - fall back, flagged downstream
        expected_faces = [b.internal_name for b in bundle.bindings if b.internal_name]
    probe = render_native_candidate(
        result.candidate_path,
        {
            "output_dir": str(run_dir / "render"),
            "dpi": int(layout_options.get("render_dpi", 96)),
            "expected_faces": expected_faces,
            "expected_text_probes": list(layout_options.get("render_text_probes", []) or []),
        },
        bundle,
    )
    atomic_write_json(
        run_dir / "render" / "native_render_probe.json", probe.model_dump(mode="json")
    )
    atomic_write_json(
        run_dir / "render" / "render_font_report.json",
        {
            "fonts": [f.model_dump(mode="json") for f in probe.fonts],
            "expected_faces": probe.expected_faces,
            "missing_expected_faces": probe.missing_expected_faces,
            "substitution_detected": probe.font_substitution_detected,
        },
    )
    atomic_write_json(
        run_dir / "render" / "render_geometry_report.json",
        {
            "page_count": probe.page_count,
            "expected_page_count": probe.expected_page_count,
            "clip_or_overlap_findings": probe.clip_or_overlap_findings,
            "checked_scopes": probe.checked_scopes,
            "unsupported_scopes": probe.unsupported_scopes,
        },
    )
    atomic_write_json(
        run_dir / "render" / "visual_review.json",
        {
            "status": "pending_harness_visual_review",
            "page_images": probe.page_images,
            "candidate_hash": probe.candidate_hash,
            "notes": "automated render checks ran; human/model "
            "visual review of page images is pending",
        },
    )
    result.render_verified = bool(probe.rendered)
    result.fonts_native_renderer_verified = (
        bool(probe.expected_faces)
        and not probe.missing_expected_faces
        and not probe.font_substitution_detected
    )
    if probe.result == "pass":
        result.native_render_verified_for_scope = "candidate_scope"
        result.unresolved_scopes = [s for s in result.unresolved_scopes if s != "render"]
    else:
        result.native_render_verified_for_scope = probe.result
        result.issues.append(f"native_render: {probe.issues[:3]}")
    result.visual_review_status = "pending"
    artifacts = dict(getattr(result, "artifacts", {}) or {})
    artifacts["native_render_probe"] = str(run_dir / "render" / "native_render_probe.json")
    artifacts["render_pdf"] = probe.pdf_path
    result.artifacts = artifacts

    # Calibration profile (technical fixture, test_only).
    try:
        fixture_path = run_dir / "render" / "calibration_fixture.pptx"
        cases = build_calibration_fixture(fixture_path, rules, bundle)

        def _measurer(request: Any) -> dict[str, Any]:
            from .measurement import measure_components

            return measure_components(
                request,
                timeout_seconds=120.0,
                project_root=layout_options.get("project_root"),
                use_cache=False,
            )["response"]

        browser_samples = measure_fixture_in_browser(cases, bundle, _measurer)
        fprobe = render_native_candidate(
            fixture_path,
            {"output_dir": str(run_dir / "render" / "fixture"), "expected_faces": expected_faces},
            bundle,
        )
        native_samples: list[dict[str, Any]] = []
        if fprobe.pdf_path:
            boxes = native_text_bboxes(fprobe.pdf_path)
            if boxes:
                xs0 = min(b["x0"] for b in boxes)
                ys0 = min(b["y0"] for b in boxes)
                xs1 = max(b["x1"] for b in boxes)
                ys1 = max(b["y1"] for b in boxes)
                native_samples = [
                    {
                        "sample_id": cases[0]["sample_id"],
                        "case": "fixture_aggregate",
                        "width_pt": xs1 - xs0,
                        "height_pt": ys1 - ys0,
                    }
                ]
        # Map browser aggregate for the same first case for comparison.
        b_agg = []
        if browser_samples:
            b_agg = [
                {
                    "sample_id": cases[0]["sample_id"],
                    "case": "fixture_aggregate",
                    "face": browser_samples[0].get("face", ""),
                    "width_pt": max((s.get("width_pt", 0.0) for s in browser_samples), default=0.0),
                    "height_pt": sum(s.get("height_pt", 0.0) for s in browser_samples),
                }
            ]
        cal = calibrate_measurement(
            b_agg,
            native_samples,
            bundle,
            {"native_renderer": fprobe.renderer or "libreoffice", "safety_pad_pt": 1.0},
        )
        atomic_write_json(
            run_dir / "render" / "calibration_profile.json", cal.model_dump(mode="json")
        )
        result.artifacts = {
            **result.artifacts,
            "calibration_profile": str(run_dir / "render" / "calibration_profile.json"),
        }
    except Exception as exc:  # noqa: BLE001 - calibration is additive evidence
        result.issues.append(f"calibration: {exc}")
    atomic_write_json(run_dir / "stage5_readiness.json", result)


def _write_saved_checks(
    run_dir: Path,
    resolved: list[Any],
    result: Any,
    layout_options: dict[str, Any],
    measurer: Any,
    content_box_pt: dict[str, float],
    resolved_trees: list[Any] | None = None,
) -> None:
    """Independent saved-bytes gates (§12.2): geometry diff, remeasurement,
    resolved-geometry validation. Results extend the readiness record."""
    from .assets import atomic_write_json
    from .importer import import_deck
    from .models import FontInventory
    from .typography import prepare_measurement_fonts

    after_path = Path(result.candidate_path)
    emission = json.loads((run_dir / "emission_report.json").read_text(encoding="utf-8"))
    bindings = list(emission.get("records", []))
    from .assets import AssetStore

    after_deck, _ = import_deck(after_path, AssetStore(run_dir / "saved_assets"))
    atomic_write_json(run_dir / "candidate_after_ir.json", after_deck.model_dump(mode="json"))
    resolved_dicts = [r if isinstance(r, dict) else r.model_dump(mode="json") for r in resolved]
    saved_diff = compare_saved_geometry(
        resolved_dicts, after_deck, after_path, bindings, layout_options
    )
    atomic_write_json(run_dir / "saved_geometry_diff.json", saved_diff)
    # Monotonic aggregation: earlier raw/authoring/parts failures are never
    # cleared by a later pass. Every gate ANDs into the verified flag.
    prior_verified = bool(getattr(result, "saved_geometry_verified", True))
    merged_ok = bool(saved_diff.get("ok")) and prior_verified
    result.saved_geometry_verified = merged_ok
    inventory = None
    try:
        inventory = FontInventory.model_validate_json(
            (run_dir / "font_inventory.json").read_text(encoding="utf-8")
        )
    except OSError:
        inventory = None
    if inventory is not None:
        bundle = prepare_measurement_fonts(
            inventory, "", profile_kind=str(layout_options.get("profile_kind", "production"))
        )
        from .measurement import TOOL_VERSION as _tool_version

        saved_measure = remeasure_saved_geometry(
            after_deck,
            bundle,
            measurer,
            {
                **layout_options,
                "saved_package_path": str(after_path),
                "candidate_sha256": _sha256_file(after_path),
                "measurement_tool_version": _tool_version,
            },
        )
        atomic_write_json(run_dir / "saved_measurement_report.json", saved_measure)
        if not saved_measure.get("ok"):
            result.saved_geometry_verified = False
            issues = list(getattr(result, "issues", []) or [])
            issues.append(
                "SAVED_REMEASURE_FAIL: "
                + "; ".join(
                    list(saved_measure.get("overflows", []))[:3]
                    or list(saved_measure.get("skipped", []))[:2]
                    or list(saved_measure.get("unknown_state", []))[:2]
                    or ["saved text does not fit saved boxes"]
                )
            )
            import contextlib as _ctxlib

            with _ctxlib.suppress(Exception):
                result.issues = issues
    geometry_report = validate_resolved_geometry(
        resolved_dicts,
        list(resolved_trees or []),
        content_box_pt,
        None,
        {
            **layout_options,
            "canvas_pt": {"x": 0.0, "y": 0.0, "w": 960.0, "h": 540.0},
            "passthrough_refs": sorted(
                {
                    ref
                    for res in resolved_dicts
                    for ref in (
                        res.get("passthrough_refs", [])
                        if isinstance(res, dict)
                        else list(getattr(res, "passthrough_refs", []) or [])
                    )
                }
            ),
        },
    )
    atomic_write_json(run_dir / "layout_geometry_report.json", geometry_report)
    # Expected-first required visibility (§7.2#1): the required set comes
    # from the SOURCE projection (objects carrying required atoms), mapped
    # to after-deck ids through binding evidence — never after==expected.
    # Scope honesty: this checker proves canvas containment + presence;
    # text fit is proven by saved_measurement_report, occlusions by the
    # layout gates + visual review (cross-referenced, not claimed here).
    visibility_report = check_required_visibility_for_run(run_dir, after_deck)
    atomic_write_json(run_dir / "required_visibility_report.json", visibility_report)
    artifacts = dict(getattr(result, "artifacts", {}) or {})
    artifacts["required_visibility_report"] = str(run_dir / "required_visibility_report.json")
    with contextlib.suppress(Exception):
        result.artifacts = artifacts
    if str(visibility_report.get("result", "")) not in ("pass",):
        result.saved_geometry_verified = False
        issues = list(getattr(result, "issues", []) or [])
        issues.append(
            "REQUIRED_VISIBILITY_FAIL: "
            + "; ".join(
                list(visibility_report.get("missing", []))[:3]
                or list(visibility_report.get("unknown_required", []))[:3]
                or ["required visibility gate failed"]
            )
        )
        with contextlib.suppress(Exception):
            result.issues = issues
    # Layout report participates in the same monotonic aggregation: a
    # layout-geometry fail blocks the verified flag even when the raw
    # saved-geometry diff passed.
    if str(geometry_report.get("result", "")) != "pass":
        result.saved_geometry_verified = False
    # The engine status is derived from the aggregator, never left as
    # completed поверх saved-measurement fail: any verified==False
    # downgrades to needs_review with the blocker recorded.
    if not bool(getattr(result, "saved_geometry_verified", True)):
        try:
            if str(getattr(result, "operation_status", "")) == "completed":
                result.operation_status = "needs_review"  # type: ignore[assignment]
        except Exception:  # noqa: BLE001
            pass
    atomic_write_json(run_dir / "stage5_readiness.json", result)


# ---------------------------------------------------------------------------
# §12.2 saved geometry: expected (resolved) vs actual (reopened bytes)
# ---------------------------------------------------------------------------


def _sha256_file(path: str | Path) -> str:
    import hashlib as _hashlib

    return _hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _emu_to_pt(value: int) -> float:
    from .units import emu_to_pt

    return emu_to_pt(int(value))


def check_required_visibility_for_run(run_dir: Path, after_deck: Any) -> dict[str, Any]:
    """Wire the expected-first visibility checker into the run tail.

    Expected required set: source objects carrying required ledger atoms
    (source projection). Mapping to after-deck ids goes through emission
    binding evidence (address part/shape_id), never source==after id
    equality. Unknown/missing required units block; an empty required
    set is unknown, never a pass.
    """
    from .geometry import check_required_content_visibility

    empty: dict[str, Any] = {
        "per_object": {},
        "missing": [],
        "unknown_required": [],
        "outside_proofs": [],
        "result": "unknown",
        "scope": "canvas_containment_and_presence_only; text fit is "
        "proven by saved_measurement_report, occlusions by the "
        "layout gates and visual review",
    }
    try:
        ledger_doc = json.loads((run_dir / "source_ledger.json").read_text(encoding="utf-8"))
        emission_doc = json.loads((run_dir / "emission_report.json").read_text(encoding="utf-8"))
        saved_geometry = json.loads(
            (run_dir / "saved_geometry_report.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        return {**empty, "result": "unknown", "reason": f"run inputs unreadable: {exc}"}
    required_parts: set[tuple[str, int]] = set()
    notes_atoms = 0
    for atom in ledger_doc.get("atoms", []) or []:
        if not atom.get("required"):
            continue
        if atom.get("kind") == "notes":
            # Slide notes are not canvas objects: their preservation is
            # proven by content_diff notes-para bindings, never by this
            # geometry gate. Counted explicitly, never silently dropped.
            notes_atoms += 1
            continue
        ref = atom.get("source_ref", {}) or {}
        try:
            required_parts.add((str(ref.get("slide_part", "")), int(ref.get("shape_id", -1))))
        except (TypeError, ValueError):
            continue
    # Shape ids are UNSTABLE across packages: map through emission
    # records (source object -> saved address -> after object), never by
    # source shape_id == after shape_id equality.
    source_loc = _source_part_to_object(run_dir)
    emit_addr: dict[str, tuple[str, int]] = {}
    for rec in emission_doc.get("records", []) or []:
        addr = rec.get("address", {}) or {}
        try:
            emit_addr[str(rec.get("source_object_id", "") or "")] = (
                str(addr.get("slide_part", "")),
                int(addr.get("shape_id", -1)),
            )
        except (TypeError, ValueError):
            continue
    after_by_addr: dict[tuple[str, int], str] = {}
    for slide in list(getattr(after_deck, "slides", []) or []):
        part = getattr(slide, "slide_part", "") or ""
        for obj in list(getattr(slide, "objects", []) or []):
            ref = getattr(obj, "source_ref", None)
            try:
                after_by_addr[(part, int(getattr(ref, "shape_id", -1)))] = str(
                    getattr(obj, "id", "")
                )
            except (TypeError, ValueError):
                continue
    authorized_omit_objs: set[str] = set()
    class_doc = _read_json_lenient(run_dir / "classification_and_transformation_ledger.json")
    for dec in class_doc.get("decisions", []) or []:
        if not isinstance(dec, dict) or not dec.get("authorization_ref"):
            continue
        if dec.get("action") == "omit":
            authorized_omit_objs.add(str(dec.get("source_object_id", "")))
        elif dec.get("action") in ("recompose", "replace") and dec.get("target_relation_ids"):
            # Arrow re-expressed as a native relation: excused from
            # shape presence; preservation proven by the relations gate.
            authorized_omit_objs.add(str(dec.get("source_object_id", "")))
    required_after_ids: list[str] = []
    mapped_parts: set[tuple[str, int]] = set()
    omitted_authorized_parts: list[str] = []
    truly_missing: list[str] = []
    for key in sorted(required_parts):
        oid = source_loc.get(key)
        addr = emit_addr.get(oid or "")
        after_id = after_by_addr.get(addr) if addr else None
        if after_id:
            required_after_ids.append(after_id)
            mapped_parts.add(key)
            continue
        # No saved object serves this required part: authorized omit or
        # missing/fail — never a silent pass.
        if oid in authorized_omit_objs:
            omitted_authorized_parts.append(f"{key[0]}#{key[1]}")
        else:
            truly_missing.append(f"{key[0]}#{key[1]}:{(oid or '?')[:8]}")
    if truly_missing:
        return {
            "per_object": {},
            "missing": truly_missing[:10],
            "unknown_required": [],
            "outside_proofs": [],
            "result": "fail",
            "reason": "required source parts absent from package",
            "scope": "canvas_containment_and_presence_only",
            "required_source_parts": len(required_parts),
            "required_after_ids": len(required_after_ids),
            "omitted_authorized_parts": sorted(omitted_authorized_parts),
        }
    if not required_after_ids and required_parts:
        return {
            "per_object": {},
            "missing": [],
            "unknown_required": [],
            "outside_proofs": [],
            "result": "fail",
            "reason": "no required source part maps to a saved object",
            "scope": "canvas_containment_and_presence_only",
            "required_source_parts": len(required_parts),
            "required_after_ids": 0,
            "omitted_authorized_parts": sorted(omitted_authorized_parts),
        }
    report = check_required_content_visibility(
        after_deck, saved_geometry, required_refs=required_after_ids
    )
    report["notes_atoms_checked_elsewhere"] = notes_atoms
    report["scope"] = (
        "canvas_containment_and_presence_only; text fit is proven by "
        "saved_measurement_report, occlusions by the layout gates and "
        "visual review"
    )
    report["required_source_parts"] = len(required_parts)
    report["required_after_ids"] = len(required_after_ids)
    report["omitted_authorized_parts"] = sorted(omitted_authorized_parts)
    return report


def _read_json_lenient(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _source_part_to_object(run_dir: Path) -> dict[tuple[str, int], str]:
    """(slide_part, shape_id) -> source object id from the source IR."""
    try:
        doc = json.loads((run_dir / "source_ir.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[tuple[str, int], str] = {}
    for slide in doc.get("slides", []) or []:
        part = slide.get("slide_part", "") or ""
        for obj in slide.get("objects", []) or []:
            ref = obj.get("source_ref", {}) or {}
            try:
                out[(part, int(ref.get("shape_id", -1)))] = str(obj.get("id", ""))
            except (TypeError, ValueError):
                continue
    return out


def compare_saved_geometry(
    resolved_slides: list[Any],
    after_deck: Any,
    candidate_path: str | Path,
    output_map: list[Any],
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Expected resolved geometry vs actual reopened native geometry.

    Objects are matched by (slide_part, shape_id) through the output map —
    never by trusting a saved echo JSON. Missing/changed bindings fail.
    Tolerance is format-level Pt->EMU rounding only.
    """
    options = options or {}
    tolerance_pt = float(options.get("saved_geometry_tolerance_pt", 0.1))
    by_address: dict[tuple[str, int], dict[str, float]] = {}
    plan = _load_plan_geometry(resolved_slides)
    for binding in output_map:
        # Emission records (object level) and output-map entries (atom
        # level) share the address shape; atom subaddresses are skipped —
        # geometry is an object property.
        addr = getattr(binding, "target_address", None)
        if addr is None:
            addr = getattr(binding, "address", None)
        if addr is None and isinstance(binding, dict):
            addr = binding.get("address", {})
        part = (
            addr.get("slide_part", "")
            if isinstance(addr, dict)
            else getattr(addr, "slide_part", "") or ""
        )
        shape_id = (
            addr.get("shape_id", 0) if isinstance(addr, dict) else getattr(addr, "shape_id", 0) or 0
        )
        try:
            shape_id = int(shape_id)
        except (TypeError, ValueError):
            continue
        source_id = (
            binding.get("source_object_id", "")
            if isinstance(binding, dict)
            else getattr(binding, "source_object_id", "") or ""
        )
        sub = "" if isinstance(binding, dict) else getattr(binding, "target_subaddress", "") or ""
        if sub or not source_id:
            continue
        box = plan.get(source_id)
        if box is not None:
            by_address[(part, shape_id)] = box
    checked: list[str] = []
    deviations: list[dict[str, Any]] = []
    missing: list[str] = []
    # Expected-first matching: every expected address must resolve to an
    # actual saved object. A binding that exists on paper while the shape
    # is absent from the package is missing/fail (never skipped by an
    # actual-only walk).
    actual_index: dict[tuple[str, int], Any] = {}
    for slide in list(getattr(after_deck, "slides", []) or []):
        part = getattr(slide, "slide_part", "") or ""
        for obj in list(getattr(slide, "objects", []) or []):
            shape_id = getattr(getattr(obj, "source_ref", None), "shape_id", 0) or 0
            try:
                actual_index[(part, int(shape_id or 0))] = obj
            except (TypeError, ValueError):
                continue
    for (part, shape_id), expected in sorted(
        by_address.items(), key=lambda kv: (kv[0][0], kv[0][1])
    ):
        obj = actual_index.get((part, shape_id))
        if obj is None:
            missing.append(f"{part}#{shape_id}: expected object absent from package")
            continue
        box = getattr(obj, "local_box", None)
        if box is None:
            missing.append(f"{part}#{shape_id}: no saved box")
            continue
        actual = {
            "x": _emu_to_pt(box.x),
            "y": _emu_to_pt(box.y),
            "w": _emu_to_pt(box.w),
            "h": _emu_to_pt(box.h),
        }
        diffs = {k: abs(actual[k] - expected[k]) for k in ("x", "y", "w", "h")}
        oid = getattr(obj, "id", f"{part}#{shape_id}")
        checked.append(oid)
        if any(v > tolerance_pt for v in diffs.values()):
            deviations.append(
                {"object_id": oid, "expected_pt": expected, "actual_pt": actual, "diff_pt": diffs}
            )

    def _bound_id(binding: Any) -> str:
        if isinstance(binding, dict):
            return str(binding.get("source_object_id", "") or "")
        return str(getattr(binding, "source_object_id", "") or "")

    unmatched = [sid for sid in plan if sid not in {_bound_id(b) for b in output_map}]
    # §8 grid gate: resolved column widths / row heights vs reopened
    # native grid. Exact EMU (single rounding at the edge); any drift is
    # a writer defect, never rounded away.
    expected_grids: dict[str, dict[str, list[int]]] = {}
    for res in resolved_slides:
        grids = (
            res.get("table_layout_emu", {})
            if isinstance(res, dict)
            else getattr(res, "table_layout_emu", {})
        )
        for obj_id, grid in dict(grids or {}).items():
            expected_grids[str(obj_id)] = {
                "column_widths_emu": [int(v) for v in dict(grid).get("column_widths_emu", [])],
                "row_heights_emu": [int(v) for v in dict(grid).get("row_heights_emu", [])],
            }
    source_by_address: dict[tuple[str, int], str] = {}
    for binding in output_map:
        addr = getattr(binding, "target_address", None)
        if addr is None:
            addr = getattr(binding, "address", None)
        if addr is None and isinstance(binding, dict):
            addr = binding.get("address", {})
        if not addr:
            continue
        part = (
            addr.get("slide_part", "")
            if isinstance(addr, dict)
            else getattr(addr, "slide_part", "") or ""
        )
        shape_id = (
            addr.get("shape_id", 0) if isinstance(addr, dict) else getattr(addr, "shape_id", 0) or 0
        )
        try:
            source_by_address[(part, int(shape_id))] = _bound_id(binding)
        except (TypeError, ValueError):
            continue
    grid_checked = 0
    grid_deviations: list[dict[str, Any]] = []
    for slide in list(getattr(after_deck, "slides", []) or []):
        part = getattr(slide, "slide_part", "") or ""
        for obj in list(getattr(slide, "objects", []) or []):
            if getattr(obj, "kind", "") != "table":
                continue
            shape_id = getattr(getattr(obj, "source_ref", None), "shape_id", 0) or 0
            source_id = source_by_address.get((part, int(shape_id or 0)))
            expected = expected_grids.get(str(source_id or ""))
            if expected is None:
                continue
            payload = getattr(obj, "payload", None)
            actual = {
                "column_widths_emu": [
                    int(v) for v in list(getattr(payload, "column_widths_emu", []) or [])
                ],
                "row_heights_emu": [
                    int(v) for v in list(getattr(payload, "row_heights_emu", []) or [])
                ],
            }
            grid_checked += 1
            if actual != expected:
                grid_deviations.append(
                    {"object_id": str(source_id), "expected": expected, "actual": actual}
                )
    return {
        "checked_objects": len(checked),
        "deviations": deviations,
        "missing_boxes": missing,
        "unmatched_expected": unmatched,
        "grid_checked": grid_checked,
        "grid_deviations": grid_deviations,
        "ok": not deviations and not missing and not unmatched and not grid_deviations,
        "tolerance_pt": tolerance_pt,
    }


def _load_plan_geometry(resolved_slides: list[Any]) -> dict[str, dict[str, float]]:
    plan: dict[str, dict[str, float]] = {}
    for res in resolved_slides:
        geometry = (
            res.get("geometry_emu", {})
            if isinstance(res, dict)
            else getattr(res, "geometry_emu", {})
        )
        for obj_id, box in dict(geometry).items():
            plan[str(obj_id)] = {
                "x": _emu_to_pt(int(box.get("x", 0))),
                "y": _emu_to_pt(int(box.get("y", 0))),
                "w": _emu_to_pt(int(box.get("w", 0))),
                "h": _emu_to_pt(int(box.get("h", 0))),
            }
    return plan


def _read_saved_text_state(
    package_path: str | Path,
) -> dict[tuple[str, int], dict[str, Any]]:
    """Actual saved paragraph/box state from reopened OOXML bytes.

    Keyed by (slide_part, shape_id). For each text-bearing shape reads
    the effective bodyPr (insets/anchor/wrap), shape rotation (xfrm
    rot), and per-paragraph pPr (algn/marL/indent/spBefore/spAfter/
    lnSpc) with the same inheritance the reader uses (pPr >
    lstStyle > defRPr/endParaRPr). Unknown/missing state is reported
    per-shape (scoped unknown), never fabricated.
    """
    import zipfile

    from lxml import etree

    out: dict[tuple[str, int], dict[str, Any]] = {}
    try:
        archive = zipfile.ZipFile(package_path)
    except Exception:  # noqa: BLE001 - caller records unreadable package
        return out
    a_ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    p_ns = "http://schemas.openxmlformats.org/presentationml/2006/main"
    ns = {"a": a_ns, "p": p_ns}
    try:
        names = [
            n for n in archive.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")
        ]
    except Exception:  # noqa: BLE001
        return out
    for name in names:
        try:
            root = etree.fromstring(archive.read(name))
        except Exception:  # noqa: BLE001 - per-part unknown, continue
            continue
        for tag in ("p:sp", "p:cxnSp"):
            for sp in root.findall(f".//{tag}", ns):
                cnv = None
                for nv_tag in ("p:nvSpPr/p:cNvPr", "p:nvCxnSpPr/p:cNvPr"):
                    cnv = sp.find(nv_tag, ns)
                    if cnv is not None:
                        break
                if cnv is None:
                    continue
                try:
                    sid = int(cnv.get("id", ""))
                except (TypeError, ValueError):
                    continue
                tx_body = sp.find("p:txBody", ns)
                if tx_body is None:
                    continue
                body_pr = tx_body.find("a:bodyPr", ns)
                anchor = "top"
                wrap = "wrap"
                margins: dict[str, float] | None = None
                if body_pr is not None:
                    a = str(body_pr.get("anchor", "t") or "t").lower()
                    anchor = {"t": "top", "ctr": "middle", "b": "bottom"}.get(a, "top")
                    w = str(body_pr.get("wrap", "square") or "square").lower()
                    wrap = "nowrap" if w == "none" else "wrap"
                    try:
                        margins = {
                            "l": float(body_pr.get("lIns", 91440)) / 12700.0,
                            "r": float(body_pr.get("rIns", 91440)) / 12700.0,
                            "t": float(body_pr.get("tIns", 45720)) / 12700.0,
                            "b": float(body_pr.get("bIns", 45720)) / 12700.0,
                        }
                    except (TypeError, ValueError):
                        margins = None
                rot_deg = 0.0
                xfrm = None
                sp_pr = sp.find("p:spPr/a:xfrm", ns)
                if sp_pr is not None:
                    xfrm = sp_pr
                if xfrm is None:
                    xfrm = sp.find(".//a:xfrm", ns)
                if xfrm is not None and xfrm.get("rot"):
                    try:
                        rot_deg = float(xfrm.get("rot")) / 60000.0
                    except (TypeError, ValueError):
                        rot_deg = 0.0
                # Default run/para props for inheritance.
                def_rpr = tx_body.find("a:lstStyle/a:defPPr/a:defRPr", ns)
                if def_rpr is None:
                    def_rpr = tx_body.find("a:lstStyle/a:defRPr", ns)
                paras: list[dict[str, Any]] = []
                for p_el in tx_body.findall("a:p", ns):
                    ppr = p_el.find("a:pPr", ns)
                    # Alignment / indents / spacing from pPr (actual).
                    algn = str(ppr.get("algn", "") if ppr is not None else "") or ""
                    align = {
                        "l": "left",
                        "ctr": "center",
                        "r": "right",
                        "just": "justify",
                        "dist": "justify",
                        "thaiDist": "justify",
                    }.get(algn, "left")
                    try:
                        mar_l = (
                            float(ppr.get("marL", 0)) / 12700.0
                            if ppr is not None and ppr.get("marL")
                            else 0.0
                        )
                    except (TypeError, ValueError):
                        mar_l = 0.0
                    try:
                        indent = (
                            float(ppr.get("indent", 0)) / 12700.0
                            if ppr is not None and ppr.get("indent")
                            else 0.0
                        )
                    except (TypeError, ValueError):
                        indent = 0.0
                    lvl = 0
                    if ppr is not None and ppr.get("lvl"):
                        try:
                            lvl = int(ppr.get("lvl"))
                        except (TypeError, ValueError):
                            lvl = 0

                    # Spacing: spBefore/spAfter (spcPts/spcPct of size?),
                    # line spacing lnSpc (spcPts/spcPct/spcAuto).
                    def _spc(el: Any) -> tuple[str, float]:
                        if el is None:
                            return ("multiple", 1.0)
                        pts = el.find("a:spcPts", ns)
                        if pts is not None and pts.get("val"):
                            try:
                                return ("exact_pts", float(pts.get("val")) / 100.0)
                            except (TypeError, ValueError):
                                pass
                        pct = el.find("a:spcPct", ns)
                        if pct is not None and pct.get("val"):
                            try:
                                return ("multiple", float(pct.get("val")) / 100000.0)
                            except (TypeError, ValueError):
                                pass
                        auto = el.find("a:spcAuto", ns)
                        if auto is not None:
                            return ("multiple", 1.0)
                        return ("multiple", 1.0)

                    if ppr is not None:
                        before_mode, before_v = _spc(ppr.find("a:lnSpc", ns))
                        # NOTE: lnSpc is line spacing; before/after below.
                        ln_mode, ln_v = before_mode, before_v
                        b_mode, b_v = _spc(ppr.find("a:spBefore", ns))
                        a_mode, a_v = _spc(ppr.find("a:spAfter", ns))
                        before_pt = b_v if b_mode == "exact_pts" else 0.0
                        after_pt = a_v if a_mode == "exact_pts" else 0.0
                    else:
                        ln_mode, ln_v, before_pt, after_pt = "multiple", 1.0, 0.0, 0.0
                    # List kind from pPr bu* children (actual, not none).
                    list_kind = "none"
                    if ppr is not None:
                        if (
                            ppr.find("a:buChar", ns) is not None
                            or ppr.find("a:buBlip", ns) is not None
                            or ppr.find("a:buAutoNum", ns) is not None
                            and (
                                ppr.find("a:buAutoNum", ns) is not None
                                and str(ppr.find("a:buAutoNum", ns).get("type", "")).lower()
                                != "none"
                            )
                        ):
                            list_kind = "bullet"
                            if (
                                ppr.find("a:buAutoNum", ns) is not None
                                and str(ppr.find("a:buAutoNum", ns).get("type", "")).lower()
                                != "none"
                            ):
                                list_kind = "numbered"
                        if ppr.find("a:buNone", ns) is not None:
                            list_kind = "none"
                    paras.append(
                        {
                            "alignment": align,
                            "level": lvl,
                            "list_kind": list_kind,
                            "mar_l_pt": mar_l,
                            "indent_pt": indent,
                            "space_before_pt": before_pt,
                            "space_after_pt": after_pt,
                            "line_spacing_mode": ln_mode,
                            "line_spacing_value": ln_v,
                        }
                    )
                out[(name, sid)] = {
                    "anchor": anchor,
                    "wrap": wrap,
                    "margins_pt": margins,
                    "rotation_deg": rot_deg,
                    "paras": paras,
                }
    import contextlib as _ctxlib2

    with _ctxlib2.suppress(Exception):
        archive.close()
    return out


def remeasure_saved_geometry(
    after_deck: Any, font_bundle: Any, measurer: Any, options: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Rebuild measurement requests from ACTUAL saved text/style/box and
    measure again: a writer that shrank a box or swapped a face is caught
    because the saved request differs from the trusted expected JSON.

    Applicability (§5.2) is decided from the reopened bytes by
    ``text_presence.inspect_saved_text_presence``. A figure whose ``txBody``
    has no ``a:t``, no field, no soft break and no bullet is
    ``empty_geometry``: it is ``not_applicable`` for TEXT FIT and is left in
    every other scope (geometry/style/path/semantic). It is never given a
    synthetic default-font string to "fail".
    """
    from .models import MeasureComponent, MeasurementRequest
    from .text_presence import is_text_fit_applicable, read_saved_presence

    options = options or {}
    profile_kind = str(options.get("profile_kind", "production"))
    by_internal = {b.internal_name: b for b in (getattr(font_bundle, "bindings", []) or [])}
    # Actual saved box/paragraph state from reopened OOXML bytes
    # (bodyPr/pPr/xfrm), keyed by (slide_part, shape_id). IR text
    # content + face/size come from the reader; box/para state never
    # falls back to fabricated 1.15/left/margins/none/zero.
    saved_state: dict[tuple[str, int], dict[str, Any]] = {}
    saved_pkg = options.get("saved_package_path", "")
    if saved_pkg:
        try:
            saved_state = _read_saved_text_state(saved_pkg)
        except Exception:  # noqa: BLE001 - unknown, flagged per-shape
            saved_state = {}
    presence: dict[tuple[str, int], Any] = {}
    if saved_pkg:
        presence = read_saved_presence(saved_pkg)
    components: list[MeasureComponent] = []
    skipped: list[str] = []
    unknown_state: list[str] = []
    not_applicable: list[dict[str, str]] = []
    for slide in list(getattr(after_deck, "slides", []) or []):
        for obj in list(getattr(slide, "objects", []) or []):
            if getattr(obj, "kind", "") not in ("text", "shape", "table"):
                continue
            payload = getattr(obj, "payload", None)
            paras = getattr(payload, "paragraphs", None)
            if paras is None:
                text_payload = getattr(payload, "text", None)
                paras = getattr(text_payload, "paragraphs", None) or []
            box = getattr(obj, "local_box", None)
            if box is None:
                skipped.append(f"{obj.id}: no saved box")
                continue
            # Shape address for actual state lookup.
            sref = getattr(obj, "source_ref", None)
            part = getattr(slide, "slide_part", "")
            if not isinstance(part, str):
                part = getattr(part, "__str__", lambda: "")()
            try:
                shape_id = int(getattr(sref, "shape_id", 0) or 0)
            except (TypeError, ValueError):
                shape_id = 0
            state = saved_state.get((part, shape_id), {})
            if saved_pkg and getattr(obj, "kind", "") != "table":
                pres = presence.get((part, shape_id))
                if pres is None:
                    unknown_state.append(
                        f"{obj.id}: no readable txBody presence for "
                        f"{part}#{shape_id} (unknown, blocking)"
                    )
                    continue
                applicable, why = is_text_fit_applicable(pres)
                if not applicable:
                    not_applicable.append(
                        {
                            "id": str(obj.id),
                            "reason": why,
                            "presence": pres.kind.value,
                            "scope_excluded": "saved_text_fit",
                            "scope_still_checked": "saved_geometry,saved_style,saved_path,"
                            "semantic_representation",
                        }
                    )
                    continue
            if not state and saved_pkg:
                unknown_state.append(
                    f"{obj.id}: no saved bodyPr/pPr state for {part}#{shape_id} (unknown, blocking)"
                )
                continue
            built_paras: list[dict[str, Any]] = []
            unknown_face = False
            table_cells: list[tuple[Any, float, float]] = []
            if getattr(obj, "kind", "") == "table":
                # Cells are measured at their SAVED spanned widths (the
                # same methodology as layout_table): measuring the whole
                # grid in one full-width box would wrap text differently
                # and manufacture a false overflow.
                widths = [
                    float(v) / 914400.0 * 72.0
                    for v in list(getattr(payload, "column_widths_emu", []) or [])
                ]
                heights = [
                    float(v) / 914400.0 * 72.0
                    for v in list(getattr(payload, "row_heights_emu", []) or [])
                ]
                for cell in list(getattr(payload, "cells", []) or []):
                    if getattr(cell, "is_spanned", False) and not getattr(
                        cell, "is_merge_origin", False
                    ):
                        continue
                    rs = int(getattr(cell, "row_span", 1) or 1)
                    cs = int(getattr(cell, "col_span", 1) or 1)
                    w = sum(widths[getattr(cell, "col", 0) : getattr(cell, "col", 0) + cs]) or 12.0
                    h = sum(heights[getattr(cell, "row", 0) : getattr(cell, "row", 0) + rs]) or 12.0
                    table_cells.append((cell, w, h))
                paras = []

            def _build_runs(
                source_paras: list[Any],
                para_states: list[dict[str, Any]],
            ) -> tuple[list[dict[str, Any]], bool]:
                out: list[dict[str, Any]] = []
                bad = False
                for idx, para in enumerate(list(source_paras or [])):
                    runs = []
                    for run in list(getattr(para, "runs", []) or []):
                        run_id = str(getattr(run, "id", ""))
                        if "/br" in run_id:
                            # Break carrier materialized by the reader:
                            # the ordered a:br token carries it, never a
                            # text run.
                            continue
                        saved = getattr(run, "source_style", None)
                        face = getattr(saved, "typeface", "") or ""
                        binding = by_internal.get(face)
                        if binding is None:
                            bad = True
                            break
                        runs.append(
                            {
                                "id": str(getattr(run, "id", "")),
                                "text": getattr(run, "text", "") or "",
                                "style": {
                                    "face_alias": binding.browser_alias,
                                    "size_pt": float(getattr(saved, "size_pt", 12.0) or 12.0),
                                    "bold": False,
                                    "italic": False,
                                    "underline": "",
                                    "color_rgb": "",
                                    "language": "",
                                    "baseline_shift": "none",
                                    "uppercase": False,
                                },
                            }
                        )
                    if bad:
                        break
                    # Actual saved paragraph state (a:pPr) by position;
                    # IR space_before/after + level/list_kind refine it.
                    # No fabricated 1.15/left/none: missing state is
                    # unknown/blocking at the shape level (see above).
                    st = para_states[idx] if idx < len(para_states) else {}
                    out.append(
                        {
                            "id": str(getattr(para, "id", "")),
                            "runs": runs,
                            "space_before_pt": float(st.get("space_before_pt", 0.0) or 0.0),
                            "space_after_pt": float(st.get("space_after_pt", 0.0) or 0.0),
                            "line_spacing_mode": str(
                                st.get("line_spacing_mode", "multiple") or "multiple"
                            ),
                            "line_spacing_value": float(st.get("line_spacing_value", 1.0) or 1.0),
                            "alignment": str(st.get("alignment", "left") or "left"),
                            "level": int(getattr(para, "level", 0) or 0),
                            "list_kind": str(
                                st.get("list_kind", getattr(para, "list_kind", "none")) or "none"
                            ),
                            "first_line_indent_pt": float(st.get("indent_pt", 0.0) or 0.0),
                            "left_indent_pt": float(st.get("mar_l_pt", 0.0) or 0.0),
                        }
                    )
                return out, bad

            if table_cells:
                for cell, w, h in table_cells:
                    cell_paras, cell_bad = _build_runs(
                        list(getattr(cell, "paragraphs", []) or []), []
                    )
                    if cell_bad or not cell_paras:
                        unknown_face = True
                        break
                    # Table cells: actual cell margins are not yet in the
                    # saved-state map (separate a:tcPr scope); record the
                    # fixed bridge margins explicitly as a scoped unknown
                    # rather than a verified pass.
                    unknown_state.append(
                        f"{obj.id}: table cell margins use bridge defaults (a:tcPr scope unchecked)"
                    )
                    components.append(
                        MeasureComponent(
                            id=f"saved-{obj.id[:8]}-"
                            f"{getattr(cell, 'row', 0)}-{getattr(cell, 'col', 0)}",
                            kind="text",
                            payload_ref=str(obj.id),
                            paragraphs=cell_paras,
                            box_pt={"x": 0.0, "y": 0.0, "w": w, "h": h},
                            margins_pt={"l": 3.6, "r": 3.6, "t": 1.8, "b": 1.8},
                            vertical_anchor="top",
                            wrap_policy="wrap",
                            rotation_deg=0.0,
                            list_preview={},
                        )
                    )
                if unknown_face or not any(c.payload_ref == str(obj.id) for c in components):
                    skipped.append(
                        f"{obj.id}: saved face not in bundle (renderer substitution suspected)"
                    )
                    continue
                continue
            built_paras, unknown_face = _build_runs(list(paras or []), list(state.get("paras", [])))
            if unknown_face or not built_paras:
                skipped.append(
                    f"{obj.id}: saved face not in bundle (renderer substitution suspected)"
                )
                continue
            margins = state.get("margins_pt") or {"l": 7.2, "r": 7.2, "t": 3.6, "b": 3.6}
            components.append(
                MeasureComponent(
                    id=f"saved-{obj.id[:8]}",
                    kind="text",
                    payload_ref=str(obj.id),
                    paragraphs=built_paras,
                    box_pt={
                        "x": _emu_to_pt(box.x),
                        "y": _emu_to_pt(box.y),
                        "w": max(_emu_to_pt(box.w), 1.0),
                        "h": max(_emu_to_pt(box.h), 1.0),
                    },
                    margins_pt=dict(margins),
                    vertical_anchor=str(state.get("anchor", "top") or "top"),
                    wrap_policy=str(state.get("wrap", "wrap") or "wrap"),
                    rotation_deg=float(state.get("rotation_deg", 0.0) or 0.0),
                    list_preview={},
                )
            )
    base_report = {
        "schema_version": "saved-text-fit/v1",
        "applicable": sorted({c.payload_ref for c in components}),
        "measured": [],
        "measured_count": 0,
        "not_applicable": not_applicable,
        "unknown_state": unknown_state,
        "overflows": [],
        "skipped": skipped,
        "input_hashes": {
            "candidate_sha256": str(options.get("candidate_sha256", "")),
            "font_bundle": str(getattr(font_bundle, "bundle_hash", "")),
            "measurement_tool_version": str(options.get("measurement_tool_version", "")),
        },
        "checked_scopes": [
            "saved_bodyPr",
            "saved_pPr",
            "saved_xfrm_rot",
            "saved_box",
            "saved_face_size",
            "saved_text_presence",
        ],
        "unsupported_scopes": ["table_tcPr_margins", "run_baseline_shift", "field_break_tokens"],
    }
    if not components:
        base_report["ok"] = not skipped and not unknown_state
        return base_report
    request = MeasurementRequest(
        schema_version="1.0",
        request_id="saved-remeasurement",
        canvas_pt={"w": 960.0, "h": 540.0},
        css_px_per_in=96.0,
        profile_kind=profile_kind,  # type: ignore[arg-type]
        components=components,
        font_assets=[
            {
                "alias": b.browser_alias,
                "path": b.path,
                "sha256": b.sha256,
                "face_index": b.face_index,
                "style": "normal",
                "weight": 400,
            }
            for b in (getattr(font_bundle, "bindings", []) or [])
        ],
        engine_options={
            "timeout_ms": 180000,
            "device_scale_factor": 1,
            "allowlist_roots": options.get("font_allowlist_roots", []),
        },
        input_hashes={},
        debug_output_dir=str(options.get("debug_output_dir", "")),
    )
    response = measurer(request)
    overflows = [
        i["component_id"]
        for i in response.get("items", [])
        if i.get("overflow_x") or i.get("overflow_y")
    ]
    base_report["measured"] = [i["component_id"] for i in response.get("items", [])]
    base_report["measured_count"] = len(response.get("items", []))
    base_report["overflows"] = overflows
    base_report["ok"] = not overflows and not skipped and not unknown_state
    return base_report


def validate_resolved_geometry(
    resolved_slides: list[Any],
    trees: list[Any],
    content_box_pt: dict[str, float],
    measured: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Broad-phase containment + overlap validation on resolved boxes.

    Checked scopes record exactly what ran: box containment, pairwise AABB
    overlap with scoped allow-pairs, overflow flags from measurement.
    Fragment-level narrow phase is a render-probe scope (Block 6), recorded
    as unsupported here rather than silently claimed.
    """
    options = options or {}
    tolerance_pt = float(options.get("geometry_tolerance_pt", 0.75))
    allowed: set[frozenset] = set()
    for tree in trees:
        for constraint in list(getattr(tree, "constraints", []) or []):
            if getattr(constraint, "kind", "") == "allowed_overlap":
                targets = list(getattr(constraint, "target_ids", []) or [])
                for i in range(len(targets)):
                    for j in range(i + 1, len(targets)):
                        allowed.add(frozenset((targets[i], targets[j])))
    # Per-slide scoping: each slide owns an independent coordinate space
    # (its own canvas instance). Cross-slide pairs never overlap and
    # cross-slide containment is meaningless; a merged global plan
    # manufactured thousands of false overlap pairs. Group by slide id.
    plans_by_slide = _load_plan_geometry_by_slide(resolved_slides)
    canvas = dict(options.get("canvas_pt", {}) or {}) or None
    passthrough = set(options.get("passthrough_refs", []) or [])
    containment: list[dict[str, Any]] = []
    overlaps: list[dict[str, Any]] = []
    all_ids: list[str] = []
    for slide_key in sorted(plans_by_slide):
        plan = plans_by_slide[slide_key]
        ids = sorted(plan)
        all_ids.extend(ids)
        for oid in ids:
            box = plan[oid]
            # Passthrough decor lives at source geometry: permitted region is
            # the canvas; relocated content stays inside the content box.
            region = dict(content_box_pt)
            region_kind = "content_box"
            if oid in passthrough and canvas:
                region = dict(canvas)
                region_kind = "canvas_passthrough"
            inside = (
                box["x"] >= region.get("x", 0) - tolerance_pt
                and box["y"] >= region.get("y", 0) - tolerance_pt
                and box["x"] + box["w"] <= region.get("x", 0) + region.get("w", 0) + tolerance_pt
                and box["y"] + box["h"] <= region.get("y", 0) + region.get("h", 0) + tolerance_pt
            )
            containment.append(
                {"object_id": oid, "slide_id": slide_key, "inside": inside, "region": region_kind}
            )
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = plan[ids[i]], plan[ids[j]]
                # Same overlap definition as the solver gate
                # (_boxes_overlap, slop 0.75pt): edge-touch is adjacency,
                # not occlusion. The exact-polygon saved-geometry gate
                # remains the strict backstop for real occlusions.
                slop = 0.75
                if (
                    a["x"] + slop < b["x"] + b["w"]
                    and b["x"] + slop < a["x"] + a["w"]
                    and a["y"] + slop < b["y"] + b["h"]
                    and b["y"] + slop < a["y"] + a["h"]
                ):
                    pair = frozenset((ids[i], ids[j]))
                    overlaps.append(
                        {"slide_id": slide_key, "pair": sorted(pair), "allowed": pair in allowed}
                    )
    overflow_hits = []
    if measured:
        for cid, item in measured.items():
            if item.get("overflow_x") or item.get("overflow_y"):
                overflow_hits.append(cid)
    bad_containment = [c["object_id"] for c in containment if not c["inside"]]
    bad_overlaps = [o for o in overlaps if not o["allowed"]]
    result = "fail" if bad_containment or bad_overlaps or overflow_hits else "pass"
    return {
        "checked_object_ids": all_ids,
        "checked_scopes": [
            "box_containment",
            "canvas_passthrough_region",
            "pairwise_aabb_overlap",
            "scoped_allow_pairs",
            "measurement_overflow_flags",
        ],
        "per_slide_scoping": True,
        "slide_ids": sorted(plans_by_slide),
        "containment": containment,
        "overlaps": overlaps,
        "overflow_hits": overflow_hits,
        "unsupported_scopes": ["fragment_narrow_phase", "connector_segment_phase"],
        "result": result,
    }


def _load_plan_geometry_by_slide(
    resolved_slides: list[Any],
) -> dict[str, dict[str, dict[str, float]]]:
    """Resolved geometry grouped by slide (each slide = own canvas space)."""
    out: dict[str, dict[str, dict[str, float]]] = {}
    for res in resolved_slides:
        if isinstance(res, dict):
            slide_key = str(res.get("slide_id", "") or res.get("source_slide_id", "unknown"))
            geometry = res.get("geometry_emu", {})
        else:
            slide_key = str(
                getattr(res, "slide_id", "") or getattr(res, "source_slide_id", "unknown")
            )
            geometry = getattr(res, "geometry_emu", {})
        bucket = out.setdefault(slide_key, {})
        for obj_id, box in dict(geometry or {}).items():
            try:
                bucket[str(obj_id)] = {
                    "x": _emu_to_pt(int(box.get("x", 0))),
                    "y": _emu_to_pt(int(box.get("y", 0))),
                    "w": _emu_to_pt(int(box.get("w", 0))),
                    "h": _emu_to_pt(int(box.get("h", 0))),
                }
            except (TypeError, ValueError, AttributeError):
                continue
    return out
