"""GPN CLI: ``slides gpn doctor|import|profile`` (spec §12).

Stdout is a JSON contract; progress and diagnostics go to stderr.
Exit codes follow ``slides_cli.gpn.errors.ExitCode``:
2 invalid_input, 3 needs_assets, 4 needs_review, 6 validation_failed.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC
from pathlib import Path
from typing import Any


def build_gpn_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="slides gpn", description="GPN corporate pipeline")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--project-root", type=Path, default=None)
    sub = parser.add_subparsers(dest="gpn_command", title="gpn commands")

    d = sub.add_parser("doctor", help="Check corpus readiness, conflicts, fonts, profiles")
    d.add_argument("--run-dir", type=Path, default=None)
    d.add_argument("--require-production-assets", action="store_true",
                   help="Gate on production assets (fonts/master): exit 3 when missing")
    d.add_argument("--check-model", action="store_true",
                   help="Also verify optional model endpoint reachability")
    pf = sub.add_parser("preflight-edits",
                        help="Validate/authorize native edits on a copy (no mutation)")
    pf.add_argument("--input", type=Path, required=True)
    pf.add_argument("--edits-json", type=str, required=True)
    pf.add_argument("--run-dir", type=Path, default=None)
    ap = sub.add_parser("apply-edits",
                        help="Save a verified diagnostic native-edit candidate")
    ap.add_argument("--input", type=Path, required=True)
    ap.add_argument("--edits-json", type=str, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--run-dir", type=Path, default=None)
    ap.add_argument("--overwrite", action="store_true")

    i = sub.add_parser("import", help="Import a PPTX to SourceDeckIR + support artifacts")
    i.add_argument("source", type=Path, nargs="?", default=None)
    i.add_argument("--input", type=Path, default=None)
    i.add_argument("--run-dir", type=Path, default=None)

    p = sub.add_parser("profile", help="Build corporate profiles (rules/template/lists/fonts)")
    p.add_argument("--reference-library", type=Path, default=None)
    p.add_argument("--run-dir", type=Path, default=None)

    pp = sub.add_parser("plan-packet",
                        help="Build planning packet + schema for a slide (no model call)")
    pp.add_argument("--run-dir", type=Path, required=True)
    pp.add_argument("--slide-id", type=str, required=True)

    pl = sub.add_parser(
        "plan",
        help="Run LLM planning on a slide (local endpoint or harness provider)",
    )
    pl.add_argument("--run-dir", type=Path, required=True)
    pl.add_argument("--slide-id", type=str, default=None)
    pl.add_argument("--candidates", type=int, default=1)
    pl.add_argument("--no-plan-cache", action="store_true")
    pl.add_argument("--llm-provider", choices=("local", "harness"), default=None,
                    help="Override the configured model provider for this run")
    pl.add_argument("--harness-wait-seconds", type=float, default=None,
                    help="Bounded wait per request for the harness answer file "
                         "(provider=harness only; 0 = fail immediately when absent)")
    pl.add_argument("--diagnostic-candidate", action="store_true",
                    help="Also apply the selected intent as a diagnostic native patch")
    return parser


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _progress(msg: str) -> None:
    print(msg, file=sys.stderr)


def _load_config(args: argparse.Namespace) -> tuple[Any, Path]:
    from .config import load_config

    project_root = args.project_root
    if args.config is not None:
        config = load_config(args.config, project_root=project_root)
        return config, config.project_root
    if project_root is None:
        project_root = Path(".").resolve()
    else:
        project_root = Path(project_root).expanduser().resolve()
    default_toml = project_root / "config" / "gpn.toml"
    if default_toml.is_file():
        config = load_config(default_toml, project_root=project_root)
        return config, config.project_root
    # Minimal config-free operation: corpora resolve against project_root.
    from types import SimpleNamespace

    return SimpleNamespace(project_root=project_root, config_path=None), project_root


def _run_dir_for(config: Any, explicit: Path | None, project_root: Path) -> Path:
    if explicit is not None:
        run_dir = explicit if explicit.is_absolute() else (project_root / explicit)
    else:
        runs_dir = getattr(config, "runs_dir", project_root / "runs")
        if not isinstance(runs_dir, Path):
            runs_dir = project_root / str(runs_dir)
        from datetime import datetime

        stamp = datetime.now(UTC).strftime("%Y-%m-%d-%H%M%S")
        run_dir = runs_dir / "stage1-stage2" / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _production_assets_status(
    project_root: Path, config: Any,
) -> tuple[bool, list[dict[str, Any]]]:
    """Check production assets (fonts/master) without claiming code readiness."""
    blockers: list[dict[str, Any]] = []
    fonts_dir_raw = getattr(getattr(config, "paths", None), "fonts_dir", "assets/fonts")
    fonts_dir = project_root / str(fonts_dir_raw)
    font_files = list(fonts_dir.glob("**/*")) if fonts_dir.is_dir() else []
    font_binaries = [p for p in font_files if p.is_file()]
    if not font_binaries:
        blockers.append({
            "rule_id": "FONTS",
            "code": "GPN_FONTS_MISSING",
            "severity": "error",
            "details": f"missing GPN font binaries in {fonts_dir}",
        })
    return (len(blockers) == 0, blockers)


def cmd_doctor(args: argparse.Namespace, config: Any, project_root: Path) -> int:
    from .errors import ExitCode
    from .ontology import discover_slide_examples, resolve_ontology_sources
    from .ontology_conflicts import analyze_ontology_sources

    run_dir = _run_dir_for(config, getattr(args, "run_dir", None), project_root)
    _progress(f"[gpn doctor] project_root={project_root}")
    sources = resolve_ontology_sources(project_root)
    discovery = discover_slide_examples(project_root)
    report = analyze_ontology_sources(sources)

    blocking = [c for c in report.conflicts if c.blocking]
    assets_ready, asset_blockers = _production_assets_status(project_root, config)
    # Scoped readiness: each flag has an explicit scope, never a generic
    # "ready" that could be mistaken for production readiness.
    readiness: dict[str, Any] = {
        "code_environment_ready": True,
        "rules_compiled": False,
        "corpus_conflicts_clear": not blocking and report.ready_for_compilation,
        "source_import_complete": False,
        "source_data_verified": False,
        "template_structure_verified": False,
        "production_assets_ready": assets_ready,
        "strict_output_ready": False,
    }
    payload: dict[str, Any] = {
        "ok": True,
        "command": "doctor",
        "project_root": str(project_root),
        "run_dir": str(run_dir),
        "readiness": readiness,
        "readiness_scope": (
            "diagnostic doctor without the production gate: exit 0 means "
            "code_environment_ready=true; production_assets_ready and "
            "strict_output_ready are reported separately and stay false "
            "until the saved-output/render gate passes"),
        "production_blockers": asset_blockers,
        "ontology": {
            "primary_json": str(sources.primary_json) if sources.primary_json else None,
            "selected_version": sources.selected_version,
            "ontology_hash": sources.ontology_hash,
            "ontology_corpus_hash": sources.ontology_corpus_hash,
            "ready": sources.ready,
            "issues": [i.model_dump(mode="json") for i in sources.issues],
        },
        "conflicts": {
            "documents_differ": report.documents_differ,
            "equivalent": report.equivalent,
            "compatible_additions": report.compatible_additions,
            "compatible_refinements": report.compatible_refinements,
            "conflicts": [c.model_dump(mode="json") for c in report.conflicts],
            "unparsed_normative_sections": report.unparsed_normative_sections,
            "resolved_rule_ids": report.resolved_rule_ids,
            "ready_for_compilation": report.ready_for_compilation,
        },
        "examples": {
            "files": len(discovery.files),
            "examples_corpus_hash": discovery.examples_corpus_hash,
            "ready": discovery.ready,
            "issues": [i.model_dump(mode="json") for i in discovery.issues],
        },
    }
    if getattr(args, "check_model", False):
        model_id = getattr(getattr(config, "model", None), "model_id", "") or ""
        payload["model"] = {
            "checked": True,
            "model_id": model_id,
            "available": bool(model_id),
        }
    else:
        payload["model"] = {"checked": False}
    _emit(payload)

    from .assets import atomic_write_json

    atomic_write_json(run_dir / "ontology_manifest.json", {
        "root": str(sources.root),
        "primary_json": str(sources.primary_json) if sources.primary_json else None,
        "selected_version": sources.selected_version,
        "ontology_hash": sources.ontology_hash,
        "ontology_corpus_hash": sources.ontology_corpus_hash,
        "ready": sources.ready,
        "issues": [i.model_dump(mode="json") for i in sources.issues],
    })
    atomic_write_json(run_dir / "ontology_conflicts.json", report.model_dump(mode="json"))
    atomic_write_json(run_dir / "examples_manifest.json", discovery.model_dump(mode="json"))
    atomic_write_json(run_dir / "doctor_readiness.json", {
        "readiness": readiness,
        "production_blockers": asset_blockers,
        "blocking_conflicts": [c.model_dump(mode="json") for c in blocking],
    })

    if not sources.ready or not discovery.files:
        return int(ExitCode.NEEDS_ASSETS)
    if blocking or not report.ready_for_compilation:
        return int(ExitCode.NEEDS_REVIEW)
    if getattr(args, "require_production_assets", False) and not assets_ready:
        return int(ExitCode.NEEDS_ASSETS)
    return int(ExitCode.COMPLETED)


def cmd_import(args: argparse.Namespace, config: Any, project_root: Path) -> int:
    from .assets import AssetStore
    from .errors import ExitCode
    from .importer import import_deck
    from .package import build_manifest, read_package
    from .provenance import build_ledger

    source = args.input or args.source
    if source is None:
        _emit({"ok": False, "error": {"code": "INPUT_NOT_FOUND",
                                      "message": "import requires a source .pptx"}})
        return int(ExitCode.INVALID_INPUT)
    source = Path(source)
    if not source.is_file():
        _emit({"ok": False, "error": {"code": "INPUT_NOT_FOUND",
                                      "message": f"input file not found: {source}"}})
        return int(ExitCode.INVALID_INPUT)

    run_dir = _run_dir_for(config, getattr(args, "run_dir", None), project_root)
    _progress(f"[gpn import] source={source}")
    store = AssetStore(run_dir / "assets")
    deck, ledger_check = import_deck(source, store)
    _ = ledger_check
    graph = read_package(source, store)

    from .importer import (
        build_import_support_report,
        resolve_internal_links,
        write_import_artifacts,
    )

    links = resolve_internal_links(deck, graph)
    ledger = build_ledger(deck, decoration=[])
    support = build_import_support_report(deck, ledger, links, deck.import_issues)
    manifest = write_import_artifacts(
        run_dir=run_dir, deck=deck, ledger=ledger,
        package_manifest=build_manifest(graph), support=support, links=links,
    )
    from datetime import datetime

    from .assets import atomic_write_json

    # Run manifest (Stage 3.5 §11.2): plan/plan-packet verify their inputs
    # against these hashes instead of picking a new source/ontology.
    run_manifest_path = run_dir / "run_manifest.json"
    if not run_manifest_path.is_file():
        atomic_write_json(run_manifest_path, {
            "created_at": datetime.now(UTC).isoformat(),
            "command": "import",
            "source_path": str(source.resolve()),
            "source_sha256": deck.source_sha256,
            "slides": len(deck.slides),
        })
    _emit({
        "ok": True,
        "command": "import",
        "run_dir": str(run_dir),
        "slides": len(deck.slides),
        "objects": support.object_count,
        "source_data_verified": support.source_data_verified,
        "blocking_uncertainty_ids": support.blocking_uncertainty_ids,
        "unresolved_links": len(links.unresolved),
        "artifacts": {
            attr: str(ref.absolute_path)
            for attr in ("source_ir", "source_ledger", "source_package_manifest",
                         "support_report", "link_resolution")
            if (ref := getattr(manifest, attr, None)) is not None
        },
    })
    if support.blocking_uncertainty_ids:
        return int(ExitCode.NEEDS_REVIEW)
    return int(ExitCode.COMPLETED)


def cmd_profile(args: argparse.Namespace, config: Any, project_root: Path) -> int:
    from .pipeline import prepare_corporate_profiles

    reference = getattr(args, "reference_library", None)
    reference = Path(reference) if reference else None
    run_dir = getattr(args, "run_dir", None)
    run_dir = Path(run_dir) if run_dir else None
    _progress(f"[gpn profile] project_root={project_root}")
    result = prepare_corporate_profiles(
        project_root=project_root, config=config, run_dir=run_dir,
        reference_library=reference,
    )
    from .assets import atomic_write_json

    anchor = result.artifact_manifest.get("compiled_rules")
    if anchor is not None and "stage2_readiness" not in result.artifact_manifest:
        readiness_path = Path(anchor).parent / "stage2_readiness.json"
        atomic_write_json(readiness_path, result)
        result.artifact_manifest["stage2_readiness"] = str(readiness_path)
    _emit(result.model_dump(mode="json"))
    from .errors import ExitCode

    status_to_code = {
        "completed": ExitCode.COMPLETED,
        "needs_assets": ExitCode.NEEDS_ASSETS,
        "needs_review": ExitCode.NEEDS_REVIEW,
        "invalid_input": ExitCode.INVALID_INPUT,
    }
    return int(status_to_code.get(result.status, ExitCode.NEEDS_REVIEW))


def main(argv: list[str] | None = None) -> int:
    parser = build_gpn_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "gpn_command", None):
        parser.print_help()
        return 0
    config, project_root = _load_config(args)
    if args.gpn_command == "doctor":
        return cmd_doctor(args, config, project_root)
    if args.gpn_command == "import":
        return cmd_import(args, config, project_root)
    if args.gpn_command == "profile":
        return cmd_profile(args, config, project_root)
    if args.gpn_command == "preflight-edits":
        from .patching import cmd_preflight_edits
        return cmd_preflight_edits(args, config, project_root)
    if args.gpn_command == "apply-edits":
        from .patching import cmd_apply_edits
        return cmd_apply_edits(args, config, project_root)
    if args.gpn_command == "plan-packet":
        from .planner_cli import cmd_plan_packet
        return cmd_plan_packet(args, config, project_root)
    if args.gpn_command == "plan":
        from .planner_cli import cmd_plan
        return cmd_plan(args, config, project_root)
    parser.print_help()
    return 2
