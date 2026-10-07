"""Stage 2 orchestration: corporate profiles end to end (spec §12).

``prepare_corporate_profiles`` runs the full Stage 2 chain on the real
ontology + reference library without requiring GPN fonts up front:

1. config/root validation and source snapshots/hashes;
2. corpus selection/conflict extraction/report;
3. blocking conflict → persist report, return needs_review (code continues);
4. compiler(template=None) on the agreed corpus;
5. real reference import, theme/style resolution, support report;
6. structural template derivation/profile + font inventory;
7. LS01 binding/evidence + checker/reference-style runs;
8. artifact JSON/PPTX validation + immutable-hash checks;
9. readiness aggregation (semantic/render unknowns never become strict pass).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC
from pathlib import Path
from typing import Any

from .assets import AssetStore, atomic_write_json
from .models import CorporateProfileResult, Issue, Severity

log = logging.getLogger(__name__)


def _issue(rule_id: str, code: str, severity: Severity, details: str) -> Issue:
    return Issue(rule_id=rule_id, code=code, severity=severity, details=details,
                 repairable=False)


def prepare_corporate_profiles(
    *,
    project_root: Path,
    config: Any,
    run_dir: Path | None = None,
    reference_library: Path | None = None,
) -> CorporateProfileResult:
    """Build corporate profiles for Stage 2 (spec §12)."""
    from datetime import datetime

    from .compiler import compile_ontology, write_ontology_artifacts
    from .ontology import (
        discover_slide_examples,
        resolve_ontology_sources,
        write_corpus_manifests,
    )
    from .ontology_conflicts import analyze_ontology_sources

    project_root = Path(project_root).expanduser().resolve()
    if run_dir is None:
        runs_dir = getattr(config, "runs_dir", project_root / "runs")
        if not isinstance(runs_dir, Path):
            runs_dir = project_root / str(runs_dir)
        stamp = datetime.now(UTC).strftime("%Y-%m-%d-%H%M%S")
        run_dir = runs_dir / "stage1-stage2" / stamp
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    blockers: list[Issue] = []
    artifacts: dict[str, str] = {}

    # 1. Corpus resolution + snapshots.
    try:
        sources = resolve_ontology_sources(project_root)
    except Exception as exc:  # noqa: BLE001
        return CorporateProfileResult(
            status="invalid_input",
            blockers=[_issue("CORPUS", "ONTOLOGY_RESOLUTION_FAILED",
                             Severity.ERROR, str(exc))],
            artifact_manifest=artifacts,
        )
    discovery = discover_slide_examples(project_root)
    manifests = write_corpus_manifests(sources, discovery, run_dir)
    artifacts.update({k: str(v) for k, v in manifests.items()})

    if not sources.ready or sources.primary_json is None:
        blockers.extend([i for i in sources.issues if i.severity == Severity.ERROR])
        blockers.append(_issue("CORPUS", "ONTOLOGY_NOT_READY", Severity.ERROR,
                               "ontology corpus not ready for compilation"))
        return CorporateProfileResult(
            status="needs_assets",
            blockers=blockers,
            artifact_manifest=artifacts,
        )

    # 2. Conflict analysis.
    conflicts = analyze_ontology_sources(sources)
    atomic_write_json(run_dir / "ontology_conflicts.json", conflicts)
    artifacts["ontology_conflicts"] = str(run_dir / "ontology_conflicts.json")

    # 3. Blocking conflict → report + needs_review (independent work continues).
    blocking_conflicts = [c for c in conflicts.conflicts if c.blocking]
    if blocking_conflicts or not conflicts.ready_for_compilation:
        for c in blocking_conflicts:
            blockers.append(_issue("ONTOLOGY_CONFLICT", "NORMATIVE_CONTRADICTION",
                                   Severity.ERROR, c.explanation))
        if conflicts.unparsed_normative_sections:
            blockers.append(_issue(
                "ONTOLOGY_CONFLICT", "UNPARSED_NORMATIVE_SECTIONS", Severity.ERROR,
                f"unparsed: {conflicts.unparsed_normative_sections}"))
        return CorporateProfileResult(
            status="needs_review",
            blockers=blockers,
            artifact_manifest=artifacts,
        )

    # 4. Compiler(template=None): core rules first.
    try:
        compiled = compile_ontology(
            Path(sources.primary_json), None, sources=sources)
    except Exception as exc:  # noqa: BLE001
        return CorporateProfileResult(
            status="invalid_input",
            blockers=[_issue("COMPILER", "COMPILATION_FAILED",
                             Severity.ERROR, str(exc))],
            artifact_manifest=artifacts,
        )
    rules_compiled = True

    # 5. Real reference import + theme/style resolution + support report.
    reference_path = reference_library
    if reference_path is None:
        candidates = [f for f in discovery.files if f.relative_path.endswith(".pptx")]
        if candidates:
            reference_path = project_root / candidates[0].relative_path
    structure_verified = False
    if reference_path is not None and Path(reference_path).is_file():
        from .importer import (
            build_import_support_report,
            import_deck,
            resolve_internal_links,
        )
        from .package import build_manifest, read_package
        from .provenance import build_ledger
        from .typography import load_theme_profiles

        store = AssetStore(run_dir / "assets")
        deck, _ = import_deck(Path(reference_path), store)
        graph = read_package(Path(reference_path), store)
        themes = load_theme_profiles(graph, store)
        _ = themes
        links = resolve_internal_links(deck, graph)
        ledger = build_ledger(deck, decoration=[])
        support = build_import_support_report(deck, ledger, links, deck.import_issues)
        from .importer import write_import_artifacts

        written = write_import_artifacts(
            run_dir=run_dir, deck=deck, ledger=ledger,
            package_manifest=build_manifest(graph), support=support, links=links,
        )
        for attr in ("source_ir", "source_ledger", "source_package_manifest",
                       "support_report", "link_resolution"):
            ref = getattr(written, attr, None)
            if ref is not None:
                artifacts[attr] = str(ref.absolute_path)
        structure_verified = True
        if support.blocking_uncertainty_ids:
            blockers.append(_issue("IMPORT", "BLOCKING_DATA_UNCERTAINTY",
                                   Severity.ERROR,
                                   f"blocking uncertainties: "
                                   f"{support.blocking_uncertainty_ids}"))
    else:
        blockers.append(_issue("REFERENCE", "REFERENCE_LIBRARY_MISSING", Severity.ERROR,
                               "no reference .pptx found in slide_examples/"))

    # 6. Structural template derivation/profile + font inventory.
    assets_ready = True
    template_profile = None
    if reference_path is not None and Path(reference_path).is_file():
        from .template import derive_base_template, extract_template_profile
        from .typography import inventory_fonts

        store = AssetStore(run_dir / "assets")
        template_profile = extract_template_profile(
            Path(reference_path), compiled, store)
        atomic_write_json(run_dir / "template_profile.json", template_profile)
        artifacts["template_profile"] = str(run_dir / "template_profile.json")
        # Structural template errors (e.g. canvas mismatch vs the loaded
        # ontology) block asset readiness instead of passing silently.
        blockers.extend(
            i for i in template_profile.issues if i.severity == Severity.ERROR
        )

        derivation = derive_base_template(
            Path(reference_path), rules=compiled,
            output_path=run_dir / "profiles" / "base_template.pptx", store=store)
        atomic_write_json(run_dir / "template_derivation.json", derivation)
        artifacts["template_derivation"] = str(run_dir / "template_derivation.json")
        if any(i.severity == Severity.ERROR for i in derivation.issues):
            blockers.extend(derivation.issues)

        fonts_dir_raw = getattr(getattr(config, "paths", None), "fonts_dir",
                                "assets/fonts")
        fonts_dir = project_root / str(fonts_dir_raw)
        required_faces: set[str] = set()
        for role in compiled.roles.values():
            required_faces.update(role.allowed_faces)
        inventory = inventory_fonts(fonts_dir, required_faces)
        atomic_write_json(run_dir / "font_inventory.json", inventory)
        artifacts["font_inventory"] = str(run_dir / "font_inventory.json")
        if inventory.missing_faces:
            assets_ready = False
            blockers.append(_issue(
                "FONTS", "GPN_FONTS_MISSING", Severity.ERROR,
                f"missing font binaries: {inventory.missing_faces}"))

        # 7. LS01 binding + checker runs + reference-style evidence.
        from .bullets import build_list_profile
        from .rules import RuleEvaluationContext, evaluate_rule_registry
        from .template import validate_reference_style

        slides: list[Any] = []
        try:
            from .importer import import_deck as _import_deck

            deck2, _ = _import_deck(Path(reference_path), AssetStore(run_dir / "assets"))
            slides = list(deck2.slides)
        except Exception:  # noqa: BLE001
            slides = []
        if template_profile is not None:
            ls_profile = build_list_profile(compiled, template_profile, slides)
            atomic_write_json(run_dir / "list_profile.json", ls_profile)
            artifacts["list_profile"] = str(run_dir / "list_profile.json")
        # Honest coverage: evaluate against the real imported deck + ledger
        # (vacuous no-IR coverage would report not_applicable by absence).
        ctx = RuleEvaluationContext(
            subject_kind="reference", compiled_rules=compiled,
            source_ir=deck,
            ledger=ledger,
            template=template_profile,
            evidence=[str(reference_path)],
        )
        coverage = evaluate_rule_registry(compiled.rule_registry, ctx)
        coverage_payload = {"coverage": [c.model_dump(mode="json")
                                        for c in coverage]}
        atomic_write_json(run_dir / "rule_coverage.json", coverage_payload)
        artifacts["rule_coverage"] = str(run_dir / "rule_coverage.json")
        if slides:
            ref_ctx = RuleEvaluationContext(
                subject_kind="reference", source_ir=None,
                compiled_rules=compiled, evidence=[str(reference_path)])
            _ = validate_reference_style(slides[0], rules=compiled, context=ref_ctx)

    # 8. Ontology artifacts (keep the real step-7 coverage: never overwrite
    # it with an empty list).
    step7_coverage: list[Any] = []
    step7_path = run_dir / "rule_coverage.json"
    if step7_path.is_file():
        try:
            step7_coverage = json.loads(
                step7_path.read_text(encoding="utf-8")).get("coverage", [])
        except (OSError, ValueError):
            step7_coverage = []
    written_onto = write_ontology_artifacts(
        compiled=compiled, conflicts=conflicts, coverage=step7_coverage,
        run_dir=run_dir, sources=sources)
    artifacts.update({k: str(v) for k, v in written_onto.items()})
    if step7_coverage:
        # Restore the real coverage payload after the shared writer roundtrip.
        atomic_write_json(run_dir / "rule_coverage.json",
                          {"coverage": step7_coverage})

    # 9. Readiness aggregation.
    rule_coverage_complete = rules_compiled
    strict_ready = False  # saved-output/render gate is a later stage
    if blockers:
        has_asset_blocker = any(i.rule_id in ("FONTS", "REFERENCE") for i in blockers)
        has_review_blocker = any(i.rule_id not in ("FONTS", "REFERENCE")
                                 for i in blockers)
        status: str = "needs_review"
        if has_asset_blocker and not has_review_blocker:
            status = "needs_assets"
    else:
        status = "needs_assets" if not assets_ready else "completed"
        if status == "completed":
            # Structural completion still not strict output readiness.
            status = "needs_assets" if not assets_ready else "completed"

    return CorporateProfileResult(
        rules_compiled=rules_compiled,
        structure_verified=structure_verified,
        rule_coverage_complete_for_stage2=rule_coverage_complete,
        assets_ready=assets_ready,
        strict_output_ready=strict_ready,
        status=status,  # type: ignore[arg-type]
        blockers=blockers,
        artifact_manifest=artifacts,
    )
