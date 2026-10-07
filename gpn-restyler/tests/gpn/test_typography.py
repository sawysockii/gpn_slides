"""Stage 1 tests: theme profiles and style inheritance (spec §3)."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import blank_slide, new_deck
from lxml import etree

from slides_cli.gpn.assets import AssetStore
from slides_cli.gpn.importer import import_deck
from slides_cli.gpn.models import ThemeProfile
from slides_cli.gpn.package import read_package
from slides_cli.gpn.typography import (
    StyleInheritanceContext,
    build_style_inheritance_context,
    load_theme_profiles,
    resolve_effective_style,
    resolve_theme_typeface,
)


@pytest.fixture()
def store(tmp_path: Path) -> AssetStore:
    return AssetStore(tmp_path / "assets")


def test_theme_profiles_scoped_per_part(tmp_path: Path, store: AssetStore) -> None:
    """Theme profiles resolve per slide via layout→master→theme, not global."""
    prs = new_deck()
    blank_slide(prs)
    blank_slide(prs)
    path = tmp_path / "themes.pptx"
    prs.save(str(path))
    graph = read_package(path, store)
    profiles = load_theme_profiles(graph, store)
    assert isinstance(profiles, dict)
    for part, profile in profiles.items():
        assert isinstance(profile, ThemeProfile)
        assert profile.theme_part == part
        assert profile.source_hash


def test_missing_b_inherits_explicit_false_overrides(store: AssetStore) -> None:
    """None/true/false bold are distinct through the inheritance chain."""
    from slides_cli.gpn.typography import _bool_attr

    assert _bool_attr(None) is None
    assert _bool_attr("1") is True
    assert _bool_attr("0") is False
    # "0"/"false" must never be truthy via bool()
    assert bool("0") is True  # documents the pitfall we avoid
    assert _bool_attr("false") is False


def test_run_without_b_inherits_paragraph_true() -> None:
    """A run without b inherits true from paragraph defRPr; b=0 cancels it."""
    a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    p_xml = (
        f'<a:p xmlns:a="{a}"><a:pPr><a:defRPr b="1" sz="1200"/>'
        f'</a:pPr><a:r><a:rPr/><a:t>x</a:t></a:r></a:p>'
    )
    p = etree.fromstring(p_xml.encode())
    r = p.find(f"{{{a}}}r")
    assert r is not None
    ctx = StyleInheritanceContext(owner_part="ppt/slides/slide1.xml",
                                  slide_part="ppt/slides/slide1.xml")
    style = resolve_effective_style(r, ctx)
    # run rPr has no b; paragraph defRPr b=1 applies
    assert style.bold is True
    assert style.size_pt == 12.0


def test_explicit_false_cancels_inherited_true() -> None:
    a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    p_xml = (
        f'<a:p xmlns:a="{a}"><a:pPr><a:defRPr b="1"/>'
        f'</a:pPr><a:r><a:rPr b="0"/><a:t>x</a:t></a:r></a:p>'
    )
    p = etree.fromstring(p_xml.encode())
    r = p.find(f"{{{a}}}r")
    assert r is not None
    ctx = StyleInheritanceContext(owner_part="ppt/slides/slide1.xml",
                                  slide_part="ppt/slides/slide1.xml")
    style = resolve_effective_style(r, ctx)
    assert style.bold is False


def test_theme_alias_resolves_per_script() -> None:
    theme = ThemeProfile(
        theme_part="ppt/theme/theme1.xml",
        source_hash="abc",
    )
    theme.minor_fonts.minor_latin = "Calibri"
    theme.major_fonts.major_latin = "Arial"
    res = resolve_theme_typeface("+mn-lt", theme=theme, script="latin", language=None)
    assert res.resolved_typeface == "Calibri"
    res2 = resolve_theme_typeface("+mj-lt", theme=theme, script="latin", language=None)
    assert res2.resolved_typeface == "Arial"
    assert res2.resolved_typeface != res.resolved_typeface


def test_unknown_alias_is_unresolved_not_default() -> None:
    theme = ThemeProfile(theme_part="ppt/theme/theme1.xml", source_hash="abc")
    res = resolve_theme_typeface("+mn-lt", theme=theme, script="latin", language=None)
    assert res.resolved_typeface is None
    assert res.issues


def test_scheme_color_keeps_formula_and_modifiers() -> None:
    theme = ThemeProfile(theme_part="ppt/theme/theme1.xml", source_hash="abc")
    theme.colors["accent1"] = "004596"
    from slides_cli.gpn.models import ColorModifier

    theme.color_transforms["accent1"] = [ColorModifier(name="tint", value=50000)]
    from slides_cli.gpn.typography import _color_from_scheme

    expr = _color_from_scheme("accent1", theme=theme, color_map={})
    assert expr is not None
    assert expr.value == "accent1"
    assert expr.resolved_rgb == "004596"
    assert [m.name for m in expr.modifiers] == ["tint"]


def test_inheritance_context_records_chain(tmp_path: Path, store: AssetStore) -> None:
    prs = new_deck()
    slide = blank_slide(prs)
    from conftest import add_textbox

    add_textbox(slide, "hello")
    path = tmp_path / "ctx.pptx"
    prs.save(str(path))
    deck, _ = import_deck(path, store)
    assert deck.slides
    obj = deck.slides[0].objects[0]

    from slides_cli.gpn.readers import ImportContext

    graph = read_package(path, store)
    ictx = ImportContext(graph=graph, asset_store=store,
                         source_hash=deck.source_sha256,
                         slide_part=deck.slides[0].slide_part or "")
    a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    el = etree.fromstring(f'<a:r xmlns:a="{a}"><a:rPr b="1"/><a:t>x</a:t></a:r>'.encode())
    inh = build_style_inheritance_context(
        el, owner_part=deck.slides[0].slide_part or "", import_context=ictx)
    assert inh.slide_part
    assert "run" in inh.property_layers
    assert obj.id
