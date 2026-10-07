"""Stage 2 tests: ontology compiler (spec §6)."""

from __future__ import annotations

from pathlib import Path

from slides_cli.gpn.compiler import compile_ontology
from slides_cli.gpn.ontology import resolve_ontology_sources

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _compiled():
    sources = resolve_ontology_sources(PROJECT_ROOT)
    assert sources.primary_json is not None
    return compile_ontology(Path(sources.primary_json), None, sources=sources), sources


def test_compiled_ontology_counts() -> None:
    compiled, _ = _compiled()
    assert (compiled.canvas.w, compiled.canvas.h) == (12192000, 6858000)
    assert len(compiled.roles) == 10
    unique_ids = set(compiled.rule_registry.specs)
    assert len(unique_ids) == 26
    assert {f"C{i:02d}" for i in range(1, 23)} <= unique_ids
    assert {"R01", "R02", "R03", "R04"} <= unique_ids
    assert "C26" not in unique_ids  # no phantom C23–C26
    assert len(compiled.catalog_records) == 325


def test_roles_match_normative_table() -> None:
    compiled, _ = _compiled()
    title = compiled.roles["main_slide_title"]
    assert title.default_size_pt == 24
    assert title.uppercase is True
    assert "GPN_DIN Condensed Bold" in title.allowed_faces
    body = compiled.roles["body"]
    assert body.default_size_pt == 12
    assert "GPN_DIN Regular" in body.allowed_faces


def test_colors_split_default_conditional() -> None:
    compiled, _ = _compiled()
    assert compiled.colors.get("blue") == "004596"
    assert "sky" in compiled.conditional_colors
    assert "cyan" in compiled.conditional_colors
    assert "sky" not in compiled.colors


def test_font_policy_preserves_exact_faces() -> None:
    compiled, _ = _compiled()
    rules = compiled.font_policy.get("rules", [])
    faces = [r.get("typeface") for r in rules]
    assert "GPN_DIN Regular" in faces
    assert "GPN_DIN Condensed Bold" in faces


def test_ls01_compiled_fully() -> None:
    compiled, _ = _compiled()
    ls01 = compiled.lists["LS01"]
    assert ls01.marker_settings.get("buChar") == "§"
    assert ls01.marker_settings.get("buFont") == "Wingdings"
    assert ls01.marker_settings.get("buSzPct") == "80000"
    assert len(ls01.level_profiles) == 9
    assert ls01.exceptions  # scope exceptions preserved


def test_extension_contract_forbids_new_tokens() -> None:
    compiled, _ = _compiled()
    assert compiled.extension_contract.style_new_tokens_allowed is False
    assert compiled.extension_contract.semantic_new_geometry_allowed is True


def test_catalog_records_are_description_only() -> None:
    compiled, _ = _compiled()
    assert all(r.implementation_status == "description_only"
               for r in compiled.catalog_records)
