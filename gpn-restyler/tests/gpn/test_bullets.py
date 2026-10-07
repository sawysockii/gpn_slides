"""Stage 2 tests: LS01 profile and OOXML writer (spec §10)."""

from __future__ import annotations

from pathlib import Path

from lxml import etree

from slides_cli.gpn.bullets import (
    apply_ls01_paragraph,
    build_list_profile,
    resolve_list_level,
)
from slides_cli.gpn.compiler import compile_ontology
from slides_cli.gpn.models import TemplateProfile
from slides_cli.gpn.ontology import resolve_ontology_sources

PROJECT_ROOT = Path(__file__).resolve().parents[3]
A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _compiled():
    sources = resolve_ontology_sources(PROJECT_ROOT)
    assert sources.primary_json is not None
    return compile_ontology(Path(sources.primary_json), None, sources=sources)


def _profile():
    return build_list_profile(_compiled(), TemplateProfile(), [])


def test_ls01_xml_single_branch() -> None:
    profile = _profile()
    resolved = resolve_list_level(level=0, parent_size_pt=12.0,
                                  background_token="white",
                                  paragraph_text_token="black",
                                  profile=profile)
    assert resolved.status == "ok"
    assert resolved.marker_color == "004596"
    assert resolved.marker_color_mode == "fixed"

    p = etree.fromstring(
        f'<a:p xmlns:a="{A}"><a:pPr><a:buNone/></a:pPr>'
        f"<a:r><a:t>item</a:t></a:r></a:p>".encode())
    apply_ls01_paragraph(p, resolved)
    ppr = p.find(f"{{{A}}}pPr")
    assert ppr is not None
    assert ppr.find(f"{{{A}}}buChar") is not None
    assert ppr.find(f"{{{A}}}buChar").get("char") == "§"
    bu_font = ppr.find(f"{{{A}}}buFont")
    assert bu_font.get("typeface") == "Wingdings"
    assert bu_font.get("pitchFamily") == "2"
    assert bu_font.get("charset") == "2"
    assert ppr.find(f"{{{A}}}buSzPct").get("val") == "80000"
    # exactly one color branch: fixed buClr, no buClrTx
    assert ppr.find(f"{{{A}}}buClr") is not None
    assert ppr.find(f"{{{A}}}buClrTx") is None
    assert ppr.find(f"{{{A}}}buNone") is None
    assert ppr.find(f"{{{A}}}buAutoNum") is None


def test_color_truth_table_orange_text_gets_buclrtx() -> None:
    profile = _profile()
    resolved = resolve_list_level(level=0, parent_size_pt=12.0,
                                  background_token="white",
                                  paragraph_text_token="orange",
                                  profile=profile)
    assert resolved.marker_color_mode == "follow_paragraph_text"
    p = etree.fromstring(
        f'<a:p xmlns:a="{A}"><a:pPr/><a:r><a:t>item</a:t></a:r></a:p>'.encode())
    apply_ls01_paragraph(p, resolved)
    ppr = p.find(f"{{{A}}}pPr")
    assert ppr.find(f"{{{A}}}buClrTx") is not None
    assert ppr.find(f"{{{A}}}buClr") is None


def test_nested_level_uses_buclrtx() -> None:
    profile = _profile()
    resolved = resolve_list_level(level=2, parent_size_pt=12.0,
                                  background_token="white",
                                  paragraph_text_token="black",
                                  profile=profile)
    assert resolved.marker_color_mode == "follow_paragraph_text"
    assert resolved.text_size_pt == 10.0  # 12 - 2


def test_nested_minimum_sequence() -> None:
    profile = _profile()
    sizes = []
    parent: float | None = 14.0
    for level in range(4):
        r = resolve_list_level(level=level, parent_size_pt=parent,
                               background_token="white",
                               paragraph_text_token="black", profile=profile)
        assert r.status == "ok", f"level {level}: {r.issues}"
        assert r.text_size_pt is not None
        sizes.append(r.text_size_pt)
        parent = r.text_size_pt
    assert sizes == [12.0, 10.0, 9.0, 8.0]
    # a child of a parent at minimum 8 is infeasible, never clamped
    bad = resolve_list_level(level=4, parent_size_pt=8.0,
                             background_token="white",
                             paragraph_text_token="black", profile=profile)
    assert bad.status == "infeasible"


def test_idempotent_apply() -> None:
    profile = _profile()
    resolved = resolve_list_level(level=0, parent_size_pt=12.0,
                                  background_token="white",
                                  paragraph_text_token="black",
                                  profile=profile)
    p = etree.fromstring(
        f'<a:p xmlns:a="{A}"><a:pPr/><a:r><a:t>item</a:t></a:r></a:p>'.encode())
    apply_ls01_paragraph(p, resolved)
    before = etree.tostring(p)
    apply_ls01_paragraph(p, resolved)
    assert etree.tostring(p) == before
    ppr = p.find(f"{{{A}}}pPr")
    assert len(ppr.findall(f"{{{A}}}buChar")) == 1
    assert len(ppr.findall(f"{{{A}}}buFont")) == 1
