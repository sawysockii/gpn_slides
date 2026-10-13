"""Stage 2 tests: font inventory (spec §8)."""

from __future__ import annotations

from pathlib import Path

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen

from slides_cli.gpn.typography import inventory_fonts, read_font_metadata


def _make_font(path: Path, family: str, subfamily: str = "Regular") -> None:
    units_per_em = 1000
    fb = FontBuilder(units_per_em, isTTF=True)
    glyph_order = [".notdef", "A", "a", "B", "b"]
    fb.setupGlyphOrder(glyph_order)
    fb.setupCharacterMap({ord("A"): "A", ord("a"): "a",
                           ord("B"): "B", ord("b"): "b"})
    glyphs = {}
    for name in glyph_order:
        pen = TTGlyphPen(None)
        pen.moveTo((0, 0))
        pen.lineTo((500, 0))
        pen.lineTo((500, 700))
        pen.closePath()
        glyphs[name] = pen.glyph()
    fb.setupGlyf(glyphs)
    metrics = dict.fromkeys(glyph_order, (600, 0))
    fb.setupHorizontalMetrics(metrics)
    fb.setupHorizontalHeader(ascent=800, descent=200)
    fb.setupNameTable({
        "familyName": family,
        "styleName": subfamily,
        "fullName": f"{family} {subfamily}",
        "uniqueFontIdentifier": f"{family}-{subfamily}",
        "psName": f"{family.replace(' ', '')}-{subfamily}",
    })
    fb.setupOS2()
    fb.setupPost()
    fb.save(str(path))


def test_font_inventory_exact_names(tmp_path: Path) -> None:
    fonts_dir = tmp_path / "fonts"
    fonts_dir.mkdir()
    _make_font(fonts_dir / "renamed.ttf", "TestFamily", "Regular")
    inv = inventory_fonts(fonts_dir, {"TestFamily Regular"})
    # matched by internal name, not filename
    assert "TestFamily Regular" in inv.faces
    asset = inv.faces["TestFamily Regular"]
    assert asset.family == "TestFamily"
    assert asset.embedding_rights != "unknown"


def test_wrong_filename_does_not_create_face(tmp_path: Path) -> None:
    fonts_dir = tmp_path / "fonts"
    fonts_dir.mkdir()
    _make_font(fonts_dir / "GPN_DIN.ttf", "LiberationSans", "Regular")
    inv = inventory_fonts(fonts_dir, {"GPN_DIN Regular"})
    assert "GPN_DIN Regular" not in inv.faces
    assert "GPN_DIN Regular" in inv.missing_faces


def test_missing_gpn_binary_is_missing(tmp_path: Path) -> None:
    fonts_dir = tmp_path / "fonts"
    fonts_dir.mkdir()
    inv = inventory_fonts(fonts_dir, {"GPN_DIN Regular", "GPN_DIN Condensed Bold"})
    assert set(inv.missing_faces) == {"GPN_DIN Regular", "GPN_DIN Condensed Bold"}
    assert inv.unverified_faces


def test_missing_fonts_dir_is_error(tmp_path: Path) -> None:
    inv = inventory_fonts(tmp_path / "nope", {"GPN_DIN Regular"})
    assert inv.missing_faces == ["GPN_DIN Regular"]
    assert any(i.severity == "error" for i in inv.issues)


def test_read_font_metadata_fields(tmp_path: Path) -> None:
    path = tmp_path / "f.ttf"
    _make_font(path, "MetaFamily", "Bold")
    asset = read_font_metadata(path)
    assert asset.family == "MetaFamily"
    assert asset.subfamily == "Bold"
    assert asset.typeface


# ---------------------------------------------------------------------------
# Stage 5 §4: system-wide discovery, glyph coverage, duplicates
# ---------------------------------------------------------------------------

def test_local_font_discovery_outside_fonts_dir(tmp_path: Path) -> None:
    """A face installed outside the configured fonts_dir is discovered by
    internal name (filename alone is never sufficient)."""
    from slides_cli.gpn.typography import discover_local_fonts

    system_root = tmp_path / "system_fonts"
    system_root.mkdir()
    _make_font(system_root / "renamed.ttf", "SysFamily", "Regular")
    report = discover_local_fonts(
        None, platform="linux", extra_roots=(system_root,))
    assert report.search_roots == [str(system_root)]
    assert report.faces_by_internal_name.get("SysFamily Regular") == [
        str(system_root / "renamed.ttf")]
    assert report.errors == []


def test_local_font_discovery_installed_faces() -> None:
    """Real machine: the user-installed GPN faces + Wingdings are discovered
    by exact internal name with hashes (old FONTS_MISSING is not carried)."""
    from slides_cli.gpn.typography import (
        build_font_inventory_from_discovery,
        discover_local_fonts,
    )

    report = discover_local_fonts(None)
    assert report.platform == "darwin"
    for face in ("GPN_DIN Condensed Bold", "GPN_DIN Regular",
                 "GPN_DIN Condensed Regular", "GPN_DIN Bold"):
        assert face in report.faces_by_internal_name, (
            f"installed face not discovered: {face}")
    assert "Wingdings" in report.faces_by_internal_name
    inv = build_font_inventory_from_discovery(
        report, {"GPN_DIN Condensed Bold", "GPN_DIN Regular",
                 "GPN_DIN Condensed Regular"})
    assert inv.missing_faces == [], f"missing: {inv.missing_faces}"
    assert "GPN_DIN Condensed Bold" in inv.faces
    assert inv.faces["GPN_DIN Condensed Bold"].asset is not None
    assert inv.faces["GPN_DIN Condensed Bold"].asset.sha256
    assert "Wingdings" in inv.marker_faces
    marker = inv.marker_faces["Wingdings"]
    assert marker.symbol_mapping.get("0xF0A7") == "square4"


def test_missing_glyph_detected(tmp_path: Path) -> None:
    """A present file lacking the character reports missing_glyph (False),
    never a pass; without bytes nothing is verifiable."""
    from slides_cli.gpn.typography import check_glyph_coverage

    path = tmp_path / "nocyr.ttf"
    _make_font(path, "NoCyr", "Regular")
    asset = read_font_metadata(path)
    assert check_glyph_coverage(asset, "Ж", font_path=path) == {"Ж": False}
    assert check_glyph_coverage(asset, "A", font_path=path) == {"A": True}
    assert check_glyph_coverage(asset, "A") == {"A": False}


def test_duplicate_copies_folded(tmp_path: Path) -> None:
    """Byte-identical copies under different names/roots fold to one face;
    the first path wins and the rest are recorded as duplicates."""
    import shutil

    from slides_cli.gpn.typography import discover_local_fonts

    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    _make_font(root_a / "one.ttf", "DupFamily", "Regular")
    shutil.copy(root_a / "one.ttf", root_b / "two.ttf")
    report = discover_local_fonts(
        None, platform="linux", extra_roots=(root_a, root_b))
    entries = [e for e in report.discovered
               if e.full_name == "DupFamily Regular"]
    assert len(entries) == 2
    assert entries[0].duplicate_of == ""
    assert entries[1].duplicate_of == entries[0].path
    assert report.faces_by_internal_name["DupFamily Regular"] == [
        entries[0].path]


def test_ambiguous_face_refused(tmp_path: Path) -> None:
    """Two different byte streams claiming one internal name are ambiguous:
    the face stays missing with an explicit error, never a silent pick."""
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    from slides_cli.gpn.typography import build_font_inventory_from_discovery, discover_local_fonts

    def _make_variant(path: Path, extra_glyphs: bool) -> None:
        units_per_em = 1000
        fb = FontBuilder(units_per_em, isTTF=True)
        glyph_order = [".notdef", "A", "a", "B", "b"]
        charmap = {ord("A"): "A", ord("a"): "a",
                   ord("B"): "B", ord("b"): "b"}
        if extra_glyphs:
            glyph_order.append("C")
            charmap[ord("C")] = "C"
        fb.setupGlyphOrder(glyph_order)
        fb.setupCharacterMap(charmap)
        glyphs = {}
        for name in glyph_order:
            pen = TTGlyphPen(None)
            pen.moveTo((0, 0))
            pen.lineTo((500, 0))
            pen.lineTo((500, 700))
            pen.closePath()
            glyphs[name] = pen.glyph()
        fb.setupGlyf(glyphs)
        fb.setupHorizontalMetrics(dict.fromkeys(glyph_order, (600, 0)))
        fb.setupHorizontalHeader(ascent=800, descent=200)
        fb.setupNameTable({
            "familyName": "ClashFamily",
            "styleName": "Regular",
            "fullName": "ClashFamily Regular",
            "uniqueFontIdentifier": "ClashFamily-Regular",
            "psName": "ClashFamily-Regular",
        })
        fb.setupOS2()
        fb.setupPost()
        fb.save(str(path))

    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    # same internal names, different bytes (extra glyph)
    _make_variant(root_a / "x.ttf", extra_glyphs=False)
    _make_variant(root_b / "y.ttf", extra_glyphs=True)
    report = discover_local_fonts(
        None, platform="linux", extra_roots=(root_a, root_b))
    inv = build_font_inventory_from_discovery(report, {"ClashFamily Regular"})
    assert "ClashFamily Regular" in inv.missing_faces
    assert any(i.code == "FONT_AMBIGUOUS_FACE" for i in inv.issues)


def test_collection_face_index_respected() -> None:
    """A .ttc face is addressed by explicit index, never silently face 0."""
    from slides_cli.gpn.typography import read_font_metadata

    helvetica = Path("/System/Library/Fonts/Helvetica.ttc")
    first = read_font_metadata(helvetica, collection_index=0)
    second = read_font_metadata(helvetica, collection_index=1)
    assert first.typeface == "Helvetica"
    assert second.typeface == "Helvetica Bold"
    assert first.typeface != second.typeface


def test_no_bold_synthesis_exact_faces() -> None:
    """The exact Bold face resolves to its own bytes; Regular is never an
    acceptable stand-in for a bold role (synthesis is forbidden)."""
    from slides_cli.gpn.typography import (
        build_font_inventory_from_discovery,
        discover_local_fonts,
    )

    report = discover_local_fonts(None)
    inv = build_font_inventory_from_discovery(
        report, {"GPN_DIN Bold", "GPN_DIN Regular"})
    assert inv.missing_faces == []
    bold = inv.faces["GPN_DIN Bold"]
    regular = inv.faces["GPN_DIN Regular"]
    assert bold.postscript_name != regular.postscript_name
    assert bold.asset.sha256 != regular.asset.sha256


# ---------------------------------------------------------------------------
# Stage 5 §4: prepare_measurement_fonts
# ---------------------------------------------------------------------------

def test_exact_font_bundle_production() -> None:
    """Production bundle binds exact bytes per face with unique aliases,
    full Cyrillic/₽/§ coverage and no issues on the real machine."""
    from slides_cli.gpn.typography import (
        build_font_inventory_from_discovery,
        discover_local_fonts,
        prepare_measurement_fonts,
    )

    report = discover_local_fonts(None)
    inv = build_font_inventory_from_discovery(
        report, {"GPN_DIN Condensed Bold", "GPN_DIN Regular",
                 "GPN_DIN Condensed Regular"})
    bundle = prepare_measurement_fonts(
        inv, "Состав поставки в 2026 году — 120,5 ₽ §", profile_kind="production")
    assert bundle.missing_faces == []
    assert bundle.missing_glyphs == []
    assert bundle.issues == []
    aliases = [b.browser_alias for b in bundle.bindings]
    assert len(set(aliases)) == len(aliases)
    assert all(a.startswith("MEASURE_FACE_") for a in aliases)
    assert bundle.bundle_hash


def test_bundle_missing_glyph_detected() -> None:
    """An emoji outside the corporate faces is reported, not measured blind."""
    from slides_cli.gpn.typography import (
        build_font_inventory_from_discovery,
        discover_local_fonts,
        prepare_measurement_fonts,
    )

    report = discover_local_fonts(None)
    inv = build_font_inventory_from_discovery(
        report, {"GPN_DIN Regular"})
    bundle = prepare_measurement_fonts(
        inv, "Текст с эмодзи \U0001F600", profile_kind="production")
    assert bundle.missing_glyphs != []
    assert any("MISSING_GLYPHS" in i for i in bundle.issues)


def test_bundle_test_only_mapping_is_separate() -> None:
    """Test-only profile maps onto a real system font and never alters the
    production ontology faces."""
    from slides_cli.gpn.typography import (
        build_font_inventory_from_discovery,
        discover_local_fonts,
        prepare_measurement_fonts,
    )

    report = discover_local_fonts(None)
    inv = build_font_inventory_from_discovery(
        report, {"GPN_DIN Regular"})
    bundle = prepare_measurement_fonts(
        inv, "Проверка", profile_kind="test_only")
    assert bundle.profile_kind == "test_only"
    assert len(bundle.bindings) == 1
    assert "GPN" not in bundle.bindings[0].internal_name
    assert bundle.bindings[0].path.endswith(".ttf")
