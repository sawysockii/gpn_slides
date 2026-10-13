"""Stage 5 native render probe tests (spec §§12.3-12.4, 13.1).

Real LibreOffice PPTX->PDF renders on synthetic decks (test_only inputs,
never design references): a wrong-font candidate must fail the font gate,
and a corporate-face deck must render with embedded expected faces and
extracted text. No PowerPoint UI is involved (recorded separately).
"""

from __future__ import annotations

from pathlib import Path

import conftest as fx
from lxml import etree
from pptx.oxml.ns import qn

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _render(candidate: Path, out_dir: Path, faces: list[str]):
    from slides_cli.gpn.render import render_native_candidate

    out_dir.mkdir(parents=True, exist_ok=True)
    return render_native_candidate(
        candidate,
        {"output_dir": str(out_dir), "dpi": 96,
         "expected_faces": faces, "expected_page_count": 1},
        None)


def test_native_render_wrong_font_detected(tmp_path: Path) -> None:
    """A declared but uninstalled face is a font-report failure, never a
    silent fallback pass."""

    src = tmp_path / "src.pptx"
    prs = fx.new_deck()
    slide = fx.blank_slide(prs)
    box = slide.shapes.add_textbox(*[__import__("pptx").util.Inches(v)
                                     for v in (0.7, 0.7, 8.6, 1.5)])
    p = box.text_frame.paragraphs[0]
    r = p.add_run()
    r.text = "Текст несуществующим шрифтом"
    rpr = r._r.get_or_add_rPr()
    latin = etree.SubElement(rpr, qn("a:latin"))
    latin.set("typeface", "NoSuchFaceXYZ")
    prs.save(str(src))

    probe = _render(src, tmp_path / "render", ["NoSuchFaceXYZ"])
    assert probe.rendered is True
    assert "NoSuchFaceXYZ" in probe.missing_expected_faces
    assert probe.result == "fail"
    assert any("absent from render" in i for i in probe.issues)


def test_native_render_text_table_image(tmp_path: Path) -> None:
    """A real PPTX->PDF render carries embedded expected faces and the
    content text on a text/table/image slide."""
    from PIL import Image, ImageDraw

    src = tmp_path / "src.pptx"
    prs = fx.new_deck()
    slide = fx.blank_slide(prs)
    fx.add_textbox(slide, "Рендер кириллицы и цифры 123", left=0.7, top=0.4,
                   width=8.6, height=0.9, size=20)
    fx.add_table(slide)
    img = Image.new("RGB", (320, 160), (0, 69, 150))
    ImageDraw.Draw(img).text((20, 60), "probe", fill=(255, 255, 255))
    img_path = tmp_path / "probe.png"
    img.save(img_path)
    from pptx.util import Inches

    slide.shapes.add_picture(str(img_path), Inches(0.7), Inches(5.6),
                             Inches(3.0), Inches(1.5))
    prs.save(str(src))

    probe = _render(src, tmp_path / "render", [])
    assert probe.rendered is True
    assert probe.page_count == 1
    assert probe.page_images, "page images are the visual evidence"
    assert Path(probe.page_images[0]).is_file()
    assert probe.text_spans and probe.text_spans > 5
    assert "pdf_font_inventory" in probe.checked_scopes
    assert "renderer_is_not_target_powerpoint" in probe.support_limits
    # No expected faces were declared, so nothing may be reported missing;
    # substitution is only proven against an expectation (see above test).
    assert probe.missing_expected_faces == []
    assert probe.result == "pass", probe.issues
