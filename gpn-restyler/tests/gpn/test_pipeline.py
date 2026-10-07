"""Stage 2 tests: pipeline readiness and CLI (spec §12)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from slides_cli.gpn.pipeline import prepare_corporate_profiles

PROJECT_ROOT = Path("/Users/wysockii/Documents/gpn_slides")


def _config():
    return SimpleNamespace(
        project_root=PROJECT_ROOT,
        runs_dir=PROJECT_ROOT / "runs",
        paths=SimpleNamespace(fonts_dir="assets/fonts"),
    )


def test_stage2_real_assets_absent(tmp_path: Path) -> None:
    """Code readiness and production needs_assets coexist honestly."""
    run_dir = tmp_path / "run"
    result = prepare_corporate_profiles(
        project_root=PROJECT_ROOT, config=_config(), run_dir=run_dir,
        reference_library=PROJECT_ROOT / "slide_examples" / "slide_examples.pptx",
    )
    assert result.rules_compiled is True
    assert result.structure_verified is True
    assert result.rule_coverage_complete_for_stage2 is True
    # no GPN font binaries in the project → assets not ready, strict not ready
    assert result.assets_ready is False
    assert result.strict_output_ready is False
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
