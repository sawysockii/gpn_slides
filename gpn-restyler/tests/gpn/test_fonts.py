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
