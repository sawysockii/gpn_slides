"""Stage 2 tests: pipeline readiness and CLI (spec §12)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from slides_cli.gpn.pipeline import prepare_corporate_profiles

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _config():
    return SimpleNamespace(
        project_root=PROJECT_ROOT,
        runs_dir=PROJECT_ROOT / "runs",
        paths=SimpleNamespace(fonts_dir="assets/fonts"),
    )


def test_stage2_assets_resolved_from_installed_fonts(tmp_path: Path) -> None:
    """Stage 5 §4: user-installed fonts are discovered system-wide, so the
    old GPN_FONTS_MISSING blocker is gone; strict output stays false."""
    run_dir = tmp_path / "run"
    result = prepare_corporate_profiles(
        project_root=PROJECT_ROOT, config=_config(), run_dir=run_dir,
        reference_library=PROJECT_ROOT / "slide_examples" / "slide_examples.pptx",
    )
    assert result.rules_compiled is True
    assert result.structure_verified is True
    assert result.rule_coverage_complete_for_stage2 is True
    assert result.assets_ready is True
    assert result.strict_output_ready is False
    assert not any("GPN_FONTS_MISSING" in i.code for i in result.blockers)


def test_stage2_missing_fonts_still_block(tmp_path: Path, monkeypatch) -> None:
    """The needs_assets path survives: with no discoverable fonts anywhere,
    GPN_FONTS_MISSING blocks again (no silent carry-over of readiness)."""
    import slides_cli.gpn.pipeline as pipeline
    import slides_cli.gpn.typography as typography
    from slides_cli.gpn.models import FontDiscoveryReport

    empty = FontDiscoveryReport(platform="test", search_roots=[],
                                discovered=[], faces_by_internal_name={},
                                errors=[])
    monkeypatch.setattr(typography, "discover_local_fonts",
                        lambda *a, **k: empty)
    run_dir = tmp_path / "run"
    result = pipeline.prepare_corporate_profiles(
        project_root=PROJECT_ROOT, config=_config(), run_dir=run_dir,
        reference_library=PROJECT_ROOT / "slide_examples" / "slide_examples.pptx",
    )
    assert result.assets_ready is False
    assert result.status == "needs_assets"
    assert any("GPN_FONTS_MISSING" in i.code for i in result.blockers)
    # artifacts exist on disk
    for key in ("compiled_rules", "rule_coverage", "template_profile",
                "template_derivation", "font_inventory", "ontology_conflicts"):
        assert key in result.artifact_manifest, key
        assert Path(result.artifact_manifest[key]).is_file(), key
    assert (run_dir / "profiles" / "base_template.pptx").is_file()


def test_gpn_cli_doctor_json(capsys) -> None:
    from slides_cli.gpn.cli import main as gpn_main

    rc = gpn_main(["--project-root", str(PROJECT_ROOT), "doctor",
                   "--run-dir", str(PROJECT_ROOT / "runs" / "stage1-stage2"
                                    / "cli-doctor-probe")])
    assert rc in (0, 3, 4)
    import json

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["command"] == "doctor"
    assert payload["ontology"]["selected_version"] == "1.2.0"
