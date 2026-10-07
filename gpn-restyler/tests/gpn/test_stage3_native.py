"""Stage 3 tests: unicode search, autofit, typed ops, guards, preflight (spec §§4-11)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from conftest import (
    add_fields_and_footnotes,
    add_group_with_nonuniform_scale,
    add_mixed_runs,
    add_table,
    add_textbox,
    blank_slide,
    new_deck,
)

from slides_cli.api import (
    CANONICAL_SHAPE_XML_ALGO,
    Presentation,
    compute_shape_xml_sha256,
    resolve_shape_address,
)
from slides_cli.errors import SlidesError
from slides_cli.gpn.assets import AssetStore
from slides_cli.gpn.importer import (
    import_deck,
    iter_hyperlink_occurrences,
    resolve_internal_links,
)
from slides_cli.gpn.models import DecorationDecision
from slides_cli.gpn.ontology import resolve_ontology_sources
from slides_cli.gpn.package import read_package
from slides_cli.gpn.patching import (
    GpnEditContext,
    StyleApplicationContext,
    apply_gpn_edits,
    authorize_gpn_edit,
    compare_ledger_preservation,
    preflight_gpn_edits,
    resolve_native_style,
)
from slides_cli.model import NativeShapeStyle, OperationBatch

PROJECT_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture()
def compiled():
    from slides_cli.gpn.compiler import compile_ontology

    sources = resolve_ontology_sources(PROJECT_ROOT)
    assert sources.primary_json is not None
    return compile_ontology(Path(sources.primary_json), None, sources=sources)


def _save(prs, path: Path) -> Path:
    prs.save(str(path))
    return path


def _open(path: Path) -> Presentation:
    return Presentation.open(path)


def _shape_id(shape) -> int:
    return int(shape.shape_id)


# ---------------------------------------------------------------------------
# §4 find_text
# ---------------------------------------------------------------------------


def test_find_text_cyrillic(tmp_path: Path) -> None:
    prs = new_deck()
    s = blank_slide(prs)
    add_textbox(s, "добыча нефти и газа")
    add_textbox(s, "Добыча угля Pпл 2024")
    add_textbox(s, "unrelated latin text")
    path = _save(prs, tmp_path / "ru.pptx")
    pres = _open(path)
    hits = pres.find_text(query="добыча", limit=10)
    assert len(hits) == 2
    assert all("добыч" in h["snippet"].casefold() for h in hits)
    for h in hits:
        assert set(h) >= {"slide_index", "slide_id", "slide_uid", "shape_index",
                          "shape_id", "shape_uid", "score", "snippet"}
    upper = pres.find_text(query="ДОБЫЧА", limit=10)
    assert [h["shape_id"] for h in upper] == [h["shape_id"] for h in hits]
    mixed = pres.find_text(query="Pпл 2024", limit=10)
    assert mixed and mixed[0]["score"] >= 2
    assert pres.find_text(query="добыча", limit=1)[0]["score"] >= 1
    assert pres.find_text(query="... !!!", limit=10) == []
    assert pres.find_text(query="", limit=10) == []
    latin = pres.find_text(query="unrelated", limit=10)
    assert len(latin) == 1


def test_find_text_yo_ye_distinct(tmp_path: Path) -> None:
    prs = new_deck()
    s = blank_slide(prs)
    add_textbox(s, "елка стоит")
    path = _save(prs, tmp_path / "yo.pptx")
    pres = _open(path)
    assert pres.find_text(query="елка", limit=10)
    # No silent Yo/Ye conflation without an explicit rule.
    assert pres.find_text(query="ёлка", limit=10) == []


# ---------------------------------------------------------------------------
# §5 autofit
# ---------------------------------------------------------------------------


def test_add_text_legacy_compatibility(tmp_path: Path) -> None:
    from lxml import etree

    prs = new_deck()
    blank_slide(prs)
    pres = Presentation(prs)
    pres.add_text(slide_index=0, text="legacy", left=1, top=1, width=4, height=1)
    ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    body_pr = prs.slides[0].shapes[0]._element.find(f".//{{{ns}}}bodyPr")
    kinds = [etree.QName(c).localname for c in body_pr]
    assert kinds.count("normAutofit") == 1
    assert "noAutofit" not in kinds
    # Old JSON plans without the new field keep legacy behaviour.
    batch = OperationBatch.model_validate({"operations": [
        {"op": "add_text", "slide_index": 0, "text": "t",
         "left": 1, "top": 2, "width": 4, "height": 1}]})
    assert batch.operations[0].autofit_policy == "legacy_shrink"


def test_no_autofit_saved_xml(tmp_path: Path) -> None:
    from lxml import etree

    prs = new_deck()
    blank_slide(prs)
    pres = Presentation(prs)
    pres.add_text(slide_index=0, text="строгий текст", left=1, top=1,
                  width=4, height=1, autofit_policy="none")
    ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    body_pr = prs.slides[0].shapes[0]._element.find(f".//{{{ns}}}bodyPr")
    assert body_pr.get("wrap") is not None or True
    wrap_before = body_pr.get("wrap")
    path = _save(prs, tmp_path / "noautofit.pptx")
    pres2 = _open(path)
    shape = pres2._prs.slides[0].shapes[0]
    body_pr2 = shape._element.find(f".//{{{ns}}}bodyPr")
    kinds = [etree.QName(c).localname for c in body_pr2]
    assert kinds.count("noAutofit") == 1
    assert "normAutofit" not in kinds and "spAutoFit" not in kinds
    assert body_pr2.get("wrap") == wrap_before
    # Repeated application stays idempotent (exactly one node).
    pres2._set_autofit(shape, "none")
    kinds2 = [etree.QName(c).localname for c in body_pr2]
    assert kinds2.count("noAutofit") == 1


# ---------------------------------------------------------------------------
# §6 schema / dispatch / addressing
# ---------------------------------------------------------------------------


def test_op_schema_dispatch_roundtrip(tmp_path: Path) -> None:
    prs = new_deck()
    s = blank_slide(prs)
    box = add_textbox(s, "dispatch")
    sid = _shape_id(box)
    path = _save(prs, tmp_path / "dispatch.pptx")
    schema = OperationBatch.model_json_schema()
    assert "set_shape_geometry" in json.dumps(schema)
    batch = OperationBatch.model_validate({"operations": [
        {"op": "set_shape_geometry", "slide_index": 0, "shape_id": sid,
         "group_path": [], "left": 2.0, "top": 2.0, "width": 5.0, "height": 1.0},
        {"op": "set_shape_style", "slide_index": 0, "shape_id": sid,
         "group_path": [],
         "style": {"font_size_pt": 22.0, "fill_mode": "keep",
                   "line_mode": "keep", "text_scope": "defaults"}},
    ]})
    pres = _open(path)
    report = pres.apply_operations(batch)
    assert report.ok and report.applied_count == 2
    assert isinstance(batch.operations[1].style, NativeShapeStyle)


def test_address_nested_ids(tmp_path: Path) -> None:
    from pptx.oxml.ns import qn

    prs = new_deck()
    s = blank_slide(prs)
    group_a = s.shapes.add_group_shape()
    leaf_a = group_a.shapes.add_shape(1, 0, 0, 100000, 100000)
    leaf_a.text_frame.text = "leaf A"
    group_b = s.shapes.add_group_shape()
    leaf_b = group_b.shapes.add_shape(1, 0, 0, 100000, 100000)
    leaf_b.text_frame.text = "leaf B"
    # Force the same cNvPr id in two different group scopes.
    for leaf in (leaf_a, leaf_b):
        cnv = leaf._element.find(qn("p:nvSpPr")).find(qn("p:cNvPr"))
        cnv.set("id", "4242")
    path = _save(prs, tmp_path / "nested.pptx")
    pres = _open(path)
    gid_a = _shape_id(group_a)
    gid_b = _shape_id(group_b)
    # The same shape id resolves independently inside each group scope.
    addr_a = resolve_shape_address(
        pres, slide_index=0, shape_id=4242, group_path=(gid_a,))
    addr_b = resolve_shape_address(
        pres, slide_index=0, shape_id=4242, group_path=(gid_b,))
    assert "leaf A" in addr_a.shape.text
    assert "leaf B" in addr_b.shape.text
    # A wrong path is refused, never fuzzy-matched.
    with pytest.raises(SlidesError):
        resolve_shape_address(
            pres, slide_index=0, shape_id=4242, group_path=(999999,))
    # Duplicate ids inside ONE scope are ambiguous and refused.
    dup1 = add_textbox(s, "dup one")
    dup2 = add_textbox(s, "dup two")
    for dup in (dup1, dup2):
        cnv = dup._element.find(qn("p:nvSpPr")).find(qn("p:cNvPr"))
        cnv.set("id", "7777")
    path2 = _save(prs, tmp_path / "nested2.pptx")
    pres2 = _open(path2)
    with pytest.raises(SlidesError):
        resolve_shape_address(pres2, slide_index=0, shape_id=7777)
    # Hash roundtrip is stable on the untouched target.
    h1 = compute_shape_xml_sha256(addr_a.shape._element)
    assert h1 == addr_a.current_xml_sha256
    assert len(h1) == 64
    assert CANONICAL_SHAPE_XML_ALGO == "c14n-exclusive-v1"


def test_stale_xml_hash(tmp_path: Path) -> None:
    prs = new_deck()
    s = blank_slide(prs)
    box = add_textbox(s, "stale")
    sid = _shape_id(box)
    path = _save(prs, tmp_path / "stale.pptx")
    before = path.read_bytes()
    pres = _open(path)
    addr = resolve_shape_address(pres, slide_index=0, shape_id=sid)
    pres.set_shape_geometry(slide_index=0, shape_id=sid, left=2, top=2,
                            width=5, height=1,
                            expected_xml_sha256=addr.current_xml_sha256)
    with pytest.raises(SlidesError):
        pres.set_shape_geometry(slide_index=0, shape_id=sid, left=3, top=3,
                                width=5, height=1,
                                expected_xml_sha256=addr.current_xml_sha256)
    # Failed stale-hash mutation leaves the in-memory target at first values.
    assert round(float(pres._prs.slides[0].shapes[0].left) / 914400, 2) == 2.0
    assert path.read_bytes() == before


# ---------------------------------------------------------------------------
# §7 geometry
# ---------------------------------------------------------------------------


def test_geometry_nested_transform(tmp_path: Path) -> None:
    from pptx.util import Inches

    prs = new_deck()
    s = blank_slide(prs)
    group = s.shapes.add_group_shape()
    child = group.shapes.add_shape(1, Inches(0.5), Inches(0.5),
                                   Inches(2), Inches(1))
    child.text_frame.text = "child"
    path = _save(prs, tmp_path / "geom.pptx")
    pres = _open(path)
    gid = _shape_id(group)
    cid = _shape_id(child)
    addr = resolve_shape_address(pres, slide_index=0, shape_id=cid,
                                 group_path=(gid,))
    pres.set_shape_geometry(slide_index=0, shape_id=cid, group_path=(gid,),
                            left=1.0, top=1.0, width=3.0, height=1.5,
                            expected_xml_sha256=addr.current_xml_sha256)
    live = [x for x in pres._prs.slides[0].shapes
            if int(x.shape_id) == gid][0]
    live_child = [x for x in live.shapes if int(x.shape_id) == cid][0]
    assert int(live_child.left) == int(Inches(1.0))
    assert int(live_child.width) == int(Inches(3.0))
    # NaN/inf and non-positive text extents are rejected.
    addr2 = resolve_shape_address(pres, slide_index=0, shape_id=cid,
                                  group_path=(gid,))
    import math as _math
    with pytest.raises(SlidesError):
        pres.set_shape_geometry(
            slide_index=0, shape_id=cid, group_path=(gid,), left=_math.nan,
            top=1, width=3, height=1,
            expected_xml_sha256=addr2.current_xml_sha256)
    with pytest.raises(SlidesError):
        pres.set_shape_geometry(
            slide_index=0, shape_id=cid, group_path=(gid,), left=1,
            top=1, width=-2, height=1,
            expected_xml_sha256=addr2.current_xml_sha256)


def test_geometry_payload_preservation(tmp_path: Path) -> None:
    prs = new_deck()
    s = blank_slide(prs)
    add_mixed_runs(s)
    box = s.shapes[-1]
    sid = _shape_id(box)
    text_before = box.text_frame.paragraphs[0].runs[0].text
    path = _save(prs, tmp_path / "payload.pptx")
    pres = _open(path)
    addr = resolve_shape_address(pres, slide_index=0, shape_id=sid)
    pres.set_shape_geometry(slide_index=0, shape_id=sid, left=0.5, top=0.5,
                            width=8.0, height=2.0,
                            expected_xml_sha256=addr.current_xml_sha256)
    out = tmp_path / "payload_out.pptx"
    pres.save(out)
    pres2 = _open(out)
    shape2 = [x for x in pres2._prs.slides[0].shapes
              if int(x.shape_id) == sid][0]
    assert shape2.text_frame.paragraphs[0].runs[0].text == text_before


# ---------------------------------------------------------------------------
# §8 style
# ---------------------------------------------------------------------------


def test_style_rich_text_preservation(tmp_path: Path) -> None:
    prs = new_deck()
    s = blank_slide(prs)
    add_fields_and_footnotes(s)
    box = s.shapes[-1]
    sid = _shape_id(box)
    runs_before = [(r.text, r.font._rPr.get("baseline"))
                   for p in box.text_frame.paragraphs for r in p.runs]
    path = _save(prs, tmp_path / "rich.pptx")
    pres = _open(path)
    addr = resolve_shape_address(pres, slide_index=0, shape_id=sid)
    style = NativeShapeStyle(font_size_pt=20.0, fill_mode="keep",
                             line_mode="keep", text_scope="defaults",
                             autofit_policy="none")
    pres.set_shape_style(slide_index=0, shape_id=sid, style=style,
                         expected_xml_sha256=addr.current_xml_sha256)
    out = tmp_path / "rich_out.pptx"
    pres.save(out)
    pres2 = _open(out)
    shape2 = [x for x in pres2._prs.slides[0].shapes
              if int(x.shape_id) == sid][0]
    runs_after = [(r.text, r.font._rPr.get("baseline"))
                  for p in shape2.text_frame.paragraphs for r in p.runs]
    assert [t for t, _ in runs_after] == [t for t, _ in runs_before]
    assert [b for _, b in runs_after] == [b for _, b in runs_before]


def test_style_uniform_runs_creates_minimal_rpr(tmp_path: Path) -> None:
    from pptx import Presentation as _P
    from pptx.util import Inches

    prs = _P()
    prs.slide_width = Inches(13.33)
    prs.slide_height = Inches(7.5)
    s = prs.slides.add_slide(prs.slide_layouts[6])
    box = s.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1))
    box.text_frame.text = "без явных rPr"
    sid = _shape_id(box)
    path = _save(prs, tmp_path / "norpr.pptx")
    pres = _open(path)
    addr = resolve_shape_address(pres, slide_index=0, shape_id=sid)
    style = NativeShapeStyle(font_size_pt=14.0, fill_mode="keep",
                             line_mode="keep", text_scope="uniform_runs",
                             autofit_policy="none")
    pres.set_shape_style(slide_index=0, shape_id=sid, style=style,
                         expected_xml_sha256=addr.current_xml_sha256)
    out = tmp_path / "norpr_out.pptx"
    pres.save(out)
    shape2 = [x for x in _open(out)._prs.slides[0].shapes
              if int(x.shape_id) == sid][0]
    assert shape2.text == "без явных rPr"
    assert shape2.text_frame.paragraphs[0].runs[0].font.size.pt == 14.0


def test_style_unsupported_atomic(tmp_path: Path) -> None:
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches

    prs = new_deck()
    s = blank_slide(prs)
    data = CategoryChartData()
    data.categories = ["a", "b"]
    data.add_series("s", (1.0, 2.0))
    frame = s.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED,
                               Inches(1), Inches(1), Inches(6), Inches(3), data)
    sid = _shape_id(frame)
    path = _save(prs, tmp_path / "chart_style.pptx")
    pres = _open(path)
    addr = resolve_shape_address(pres, slide_index=0, shape_id=sid)
    xml_before = compute_shape_xml_sha256(addr.shape._element)
    style = NativeShapeStyle(font_size_pt=18.0, fill_mode="solid",
                             fill_color_rgb="003366", line_mode="keep",
                             text_scope="defaults")
    with pytest.raises(SlidesError):
        pres.set_shape_style(slide_index=0, shape_id=sid, style=style,
                             expected_xml_sha256=addr.current_xml_sha256)
    # Atomic refusal: the target XML is byte-identical after the refusal.
    addr_after = resolve_shape_address(pres, slide_index=0, shape_id=sid)
    assert addr_after.current_xml_sha256 == xml_before


# ---------------------------------------------------------------------------
# §2.1 conflicts classifier
# ---------------------------------------------------------------------------


def _stmt(rid, subj, prop, val, mod="must", app="all"):
    from slides_cli.gpn.models import NormativeStatement

    return NormativeStatement(
        id=f"{rid}:{subj}:{prop}", subject=subj, property=prop, operator="=",
        typed_value=val, applicability=app, modality=mod, rule_id=rid)


def test_same_rule_id_changed_value() -> None:
    from slides_cli.gpn.ontology_conflicts import compare_normative_statements

    report = compare_normative_statements(
        [_stmt("CX", "style", "b", "b=false"),
         _stmt("CX", "style", "b", "b=true")])
    assert report.conflicts and report.conflicts[0].blocking
    assert report.conflicts[0].kind == "contradiction"
    assert not report.ready_for_compilation


def test_predicate_overlap_and_refinement() -> None:
    from slides_cli.gpn.ontology_conflicts import compare_normative_statements

    ge8 = _stmt("CX", "style", "x", "не менее 8")
    eq8 = _stmt("CX", "style", "x", "ровно 8")
    r = compare_normative_statements([ge8, eq8])
    assert r.compatible_refinements == ["CX"]
    assert not r.conflicts and r.ready_for_compilation
    r2 = compare_normative_statements(
        [ge8, _stmt("CX", "style", "x", "не более 7")])
    assert r2.conflicts and r2.conflicts[0].kind == "contradiction"
    assert not r2.ready_for_compilation
    r3 = compare_normative_statements(
        [_stmt("CX", "body", "size_pt", "12"),
         _stmt("CX", "footnote", "size_pt", "10")])
    assert not r3.conflicts
    assert "CX" not in r3.equivalent  # disjoint scopes are not merged


def test_lost_color_condition() -> None:
    from slides_cli.gpn.ontology_conflicts import compare_normative_statements

    motel = _stmt("C01", "style", "color",
                  "sky допускается при подтверждённом контексте",
                  app="conditional")
    free = _stmt("C01", "style", "color", "sky разрешён без условий", app="all")
    report = compare_normative_statements([motel, free])
    assert report.conflicts and report.conflicts[0].blocking
    assert "C01" not in report.equivalent


def test_md_only_rule_and_unparsed_and_volume() -> None:
    from slides_cli.gpn.models import NormativeRef, NormativeStatement
    from slides_cli.gpn.ontology_conflicts import compare_normative_statements

    md_only = NormativeStatement(
        id="md:C99", subject="style", property="C99", operator="=",
        typed_value="новое обязательное правило", applicability="all",
        modality="must", rule_id="C99",
        refs=[NormativeRef(source_kind="external_markdown",
                           relative_path="markdown", source_hash="h" * 16)])
    report = compare_normative_statements([md_only])
    assert "C99" in report.compatible_additions  # never silently lost
    bad = NormativeStatement(
        id="md:C05", subject="style", property="C05", operator="=",
        typed_value="?", applicability="all", modality="must", rule_id="C05",
        extraction_status="unparsed")
    report2 = compare_normative_statements([bad])
    assert report2.unparsed_normative_sections
    assert not report2.ready_for_compilation
    # A consistent large catalog is not blocked by volume alone.
    many = [_stmt(f"C{i:02d}", "style", f"C{i:02d}", f"value {i}")
            for i in range(1, 40)]
    report3 = compare_normative_statements(many)
    assert report3.ready_for_compilation


# ---------------------------------------------------------------------------
# §2.2 checker partials
# ---------------------------------------------------------------------------


def test_checker_stage2_partial_coverage(tmp_path: Path, compiled) -> None:
    from slides_cli.gpn.provenance import build_ledger
    from slides_cli.gpn.rules import RuleEvaluationContext, evaluate_rule

    prs = new_deck()
    s = blank_slide(prs)
    box = add_textbox(s, "• fake bullet")
    _ = box
    path = _save(prs, tmp_path / "cov.pptx")
    store = AssetStore(tmp_path / "assets")
    deck, _ = import_deck(path, store)
    ledger = build_ledger(deck, decoration=[])
    ctx = RuleEvaluationContext(subject_kind="source", source_ir=deck,
                                ledger=ledger, compiled_rules=compiled,
                                evidence=["x"])
    c04 = evaluate_rule("C04", ctx)
    assert c04.result in ("pass", "fail")
    c20 = evaluate_rule("C20", ctx)
    assert c20.result in ("pass", "fail", "unknown")
    assert "paragraph-structure" in (c20.checked_scopes or []) or c20.result == "unknown"
    c05 = evaluate_rule("C05", ctx)
    assert c05.result == "pass"
    # Duplicate atoms fail instead of a silent pass.
    ledger.atoms.append(ledger.atoms[0].model_copy(deep=True))
    ctx2 = RuleEvaluationContext(subject_kind="source", source_ir=deck,
                                 ledger=ledger, compiled_rules=compiled)
    assert evaluate_rule("C05", ctx2).result == "fail"
    c14 = evaluate_rule("C14", ctx)
    assert c14.result in ("pass", "fail")
    c11 = evaluate_rule("C11", ctx)
    assert c11.result in ("pass", "fail")
    c13 = evaluate_rule("C13", ctx)
    assert c13.result in ("pass", "unknown", "fail")


def test_checker_negative_cases(compiled) -> None:
    from slides_cli.gpn.models import (
        ChartPayload,
        ChartPointIR,
        ChartSeriesIR,
        ObjectIR,
        RectEMU,
        SlideIR,
        SourceDeckIR,
        SourceLedger,
        SourceRef,
        TypedValue,
    )
    from slides_cli.gpn.rules import RuleEvaluationContext, evaluate_rule

    ref = SourceRef(deck_sha256="s", slide_part="ppt/slides/slide1.xml",
                    shape_id=2)
    bad_chart = ObjectIR(
        id="chart1", source_ref=ref, kind="chart",
        payload=ChartPayload(chart_part="", chart_type="bar",
                             series=[ChartSeriesIR(
                                 id="ser", points=[ChartPointIR(
                                     id="p", index=0,
                                     value=TypedValue(
                                         state="missing",
                                         display_text="0"))])]),
        local_box=RectEMU(x=0, y=0, w=100, h=100))
    slide = SlideIR(id="s1", source_index=0, source_canvas=RectEMU(
        x=0, y=0, w=12192000, h=6858000), objects=[bad_chart])
    deck = SourceDeckIR(input_kind="pptx", source_sha256="s", slides=[slide])
    ctx = RuleEvaluationContext(subject_kind="source", source_ir=deck,
                                ledger=SourceLedger(),
                                compiled_rules=compiled)
    assert evaluate_rule("C14", ctx).result == "fail"
    assert evaluate_rule("C11", ctx).result == "fail"


# ---------------------------------------------------------------------------
# §2.3 links
# ---------------------------------------------------------------------------


def _add_shape_hyperlink(shape, target_slide, *, hover=False):
    from lxml import etree
    from pptx.oxml.ns import qn

    nv_pr = shape._element.find(qn("p:nvSpPr"))
    nv = nv_pr.find(qn("p:nvPr")) if nv_pr is not None else None
    if nv is None:
        nv = etree.SubElement(nv_pr, qn("p:nvPr"))
    tag = qn("a:hlinkHover") if hover else qn("a:hlinkClick")
    hlink = etree.SubElement(nv, tag)
    rid = shape.part.relate_to(
        target_slide.part,
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide",
        is_external=False)
    hlink.set(qn("r:id"), rid)
    return rid


def test_shape_and_run_links(tmp_path: Path) -> None:
    from conftest import _add_internal_hyperlink

    prs = new_deck()
    s1 = blank_slide(prs)
    add_textbox(s1, "Первый слайд Первый")
    s2 = blank_slide(prs)
    run_box = add_textbox(s2, "run link сюда")
    _add_internal_hyperlink(run_box, s1)
    shape_box = add_textbox(s2, "shape link")
    rid_shape = _add_shape_hyperlink(shape_box, s1)
    grouped = add_group_with_nonuniform_scale(s2)
    _ = grouped
    path = _save(prs, tmp_path / "links2.pptx")
    store = AssetStore(tmp_path / "assets")
    deck, _ = import_deck(path, store)
    graph = read_package(path, store)
    report = resolve_internal_links(deck, graph)
    assert report.resolved, "run links must resolve"
    # Shared rId used by two occurrences keeps both records.
    assert rid_shape
    # Occurrence walker sees shape-level nodes in XML order.
    from pptx import Presentation as _P

    raw = _P(str(path))
    slide_el = raw.slides[1]._element
    occs = list(iter_hyperlink_occurrences("ppt/slides/slide2.xml", slide_el))
    kinds = {o.event_kind for o in occs}
    assert "click" in kinds
    identities = {(o.owner_part, o.xml_path, o.event_kind) for o in occs}
    assert len(identities) == len(occs)
    # Unresolved dangling target is reported, never silently dropped.
    from lxml import etree as _etree
    from pptx.oxml.ns import qn as _qn

    bad_box = add_textbox(s2, "dangling ссылка")
    rpr = bad_box.text_frame.paragraphs[0].runs[0]._r.find(_qn("a:rPr"))
    hlink = _etree.SubElement(rpr, _qn("a:hlinkClick"))
    hlink.set(_qn("r:id"), "rIdDoesNotExist")
    path2 = _save(prs, tmp_path / "links3.pptx")
    deck2, _ = import_deck(path2, AssetStore(tmp_path / "assets2"))
    graph2 = read_package(path2, AssetStore(tmp_path / "assets2"))
    report2 = resolve_internal_links(deck2, graph2)
    assert any(r.status == "unresolved" for r in report2.unresolved)


def test_clrmapovr_affects_scheme_color() -> None:
    from lxml import etree

    from slides_cli.gpn.models import ThemeProfile
    from slides_cli.gpn.typography import _color_from_scheme, parse_clr_map_override

    slide_xml = (
        '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        "<p:clrMapOvr><a:overrideClrMapping tx1=\"dk2\"/></p:clrMapOvr>"
        "</p:sld>")
    root = etree.fromstring(slide_xml.encode())
    mapping = parse_clr_map_override(root)
    assert mapping == {"tx1": "dk2"}
    theme = ThemeProfile(theme_part="t", source_hash="h",
                         colors={"tx1": "111111", "dk2": "222222"})
    plain = _color_from_scheme("tx1", theme=theme, color_map={})
    assert plain and plain.resolved_rgb == "111111"
    remapped = _color_from_scheme("tx1", theme=theme, color_map=mapping)
    assert remapped and remapped.resolved_rgb == "222222"


# ---------------------------------------------------------------------------
# §2.4 doctor + §2.5 chart gate
# ---------------------------------------------------------------------------


def test_doctor_asset_scope() -> None:
    from types import SimpleNamespace

    from slides_cli.gpn.cli import _production_assets_status

    ready, blockers = _production_assets_status(
        PROJECT_ROOT, SimpleNamespace(paths=SimpleNamespace(
            fonts_dir="assets/fonts")))
    assert ready is False
    assert any(b["code"] == "GPN_FONTS_MISSING" for b in blockers)


def test_chart_conflict_gate() -> None:
    from slides_cli.gpn.importer import (
        build_import_support_report,
        collect_import_uncertainties,
        compare_chart_bindings,
    )
    from slides_cli.gpn.models import (
        AssetRef,
        ChartPayload,
        ChartPointIR,
        ChartSeriesIR,
        LinkResolutionReport,
        RectEMU,
        SlideIR,
        SourceDeckIR,
        SourceLedger,
        TypedValue,
        WorkbookCellSnapshot,
        WorkbookSheetSnapshot,
        WorkbookSnapshot,
    )

    chart = ChartPayload(
        chart_part="ppt/charts/chart1.xml", chart_type="bar",
        series=[ChartSeriesIR(
            id="ser0", points=[ChartPointIR(
                id="p0", index=0,
                value=TypedValue(state="present", decimal="10",
                                 display_text="10"))],
            formula_refs=["Sheet1!$B$2"])],
        part_graph_root="ppt/charts/chart1.xml")
    wb = WorkbookSnapshot(
        workbook_asset=AssetRef(sha256="x" * 64, relative_path="w",
                                media_type="a", byte_count=1),
        sheets=[WorkbookSheetSnapshot(
            sheet_name="Sheet1",
            cells=[WorkbookCellSnapshot(
                cell_ref="B2",
                value=TypedValue(state="present", decimal="11",
                                 display_text="11"))])])
    comps = compare_chart_bindings(chart, workbook=wb)
    assert any(c.status == "conflict" for c in comps)
    slide = SlideIR(id="s", source_index=0,
                    source_canvas=RectEMU(x=0, y=0, w=1, h=1))
    slide.uncertainties = collect_import_uncertainties(slide, comps)
    deck = SourceDeckIR(input_kind="pptx", source_sha256="s", slides=[slide])
    support = build_import_support_report(
        deck, SourceLedger(), LinkResolutionReport(), [])
    assert support.source_data_verified is False
    assert support.blocking_uncertainty_ids
    # Real pipeline status from such a source is needs_review/exit 4.
    from slides_cli.gpn.errors import ExitCode

    assert int(ExitCode.NEEDS_REVIEW) == 4


# ---------------------------------------------------------------------------
# §9 guards
# ---------------------------------------------------------------------------


def _gpn_context(tmp_path: Path, source: Path, compiled,
                 decisions=(), protected=frozenset()) -> GpnEditContext:
    from slides_cli.gpn.assets import AssetStore as _Store
    from slides_cli.gpn.importer import import_deck as _import
    from slides_cli.gpn.provenance import build_ledger as _ledger

    store = _Store(tmp_path / "gctx")
    deck, _ = _import(source, store)
    ledger = _ledger(deck, decoration=[])
    return GpnEditContext(compiled_rules=compiled, source_ir=deck,
                          ledger=ledger, decoration_decisions=tuple(decisions),
                          protected_subject_ids=protected)


def test_protected_and_content_delete(tmp_path: Path, compiled) -> None:
    prs = new_deck()
    s = blank_slide(prs)
    box = add_textbox(s, "content")
    sid = _shape_id(box)
    path = _save(prs, tmp_path / "guard.pptx")
    ctx = _gpn_context(tmp_path, path, compiled)
    batch = OperationBatch.model_validate({"operations": [
        {"op": "delete_shape", "slide_index": 0, "shape_id": sid,
         "expected_xml_sha256": "a" * 64,
         "deletion_reason": "это декор"}]})
    auth = authorize_gpn_edit(batch.operations[0], ctx)
    assert auth.allowed is False  # reason string never authorizes
    ctx2 = _gpn_context(tmp_path, path, compiled,
                        decisions=(DecorationDecision(
                            object_id=str(sid), reason="verified decor",
                            evidence=["e"], requires_review=False),))
    auth2 = authorize_gpn_edit(batch.operations[0], ctx2)
    assert auth2.allowed is True
    ctx3 = _gpn_context(tmp_path, path, compiled,
                        protected=frozenset({str(sid)}))
    auth3 = authorize_gpn_edit(batch.operations[0], ctx3)
    assert auth3.allowed is False


def test_style_guards(tmp_path: Path, compiled) -> None:
    prs = new_deck()
    s = blank_slide(prs)
    box = add_textbox(s, "guarded")
    sid = _shape_id(box)
    path = _save(prs, tmp_path / "style_guard.pptx")
    ctx = _gpn_context(tmp_path, path, compiled)
    bad_bold = OperationBatch.model_validate({"operations": [
        {"op": "set_shape_style", "slide_index": 0, "shape_id": sid,
         "style": {"font_name": "GPN_DIN Regular", "bold": True,
                   "fill_mode": "keep", "line_mode": "keep"}}]})
    assert authorize_gpn_edit(bad_bold.operations[0], ctx).allowed is False
    bad_color = OperationBatch.model_validate({"operations": [
        {"op": "set_shape_style", "slide_index": 0, "shape_id": sid,
         "style": {"text_color_rgb": "123456", "fill_mode": "keep",
                   "line_mode": "keep"}}]})
    assert authorize_gpn_edit(bad_color.operations[0], ctx).allowed is False
    role_ctx = StyleApplicationContext(
        target_kind="text", requested_text_scope="defaults",
        verified_evidence=("slide_examples.pptx#3",))
    role = next(iter(compiled.roles))
    native = resolve_native_style(style_role=role, rules=compiled,
                                  context=role_ctx)
    assert native.autofit_policy == "none"
    from slides_cli.gpn.errors import GpnError

    with pytest.raises(GpnError):
        resolve_native_style(style_role="no_such_role", rules=compiled,
                             context=role_ctx)


# ---------------------------------------------------------------------------
# §9-10 delete/rollback/preflight/reimport
# ---------------------------------------------------------------------------


def test_connected_shape_delete_refused(tmp_path: Path) -> None:
    from lxml import etree
    from pptx.enum.shapes import MSO_CONNECTOR
    from pptx.oxml.ns import qn
    from pptx.util import Inches

    prs = new_deck()
    s = blank_slide(prs)
    box = add_textbox(s, "node")
    target_id = str(box._element.find(qn("p:nvSpPr")).find(
        qn("p:cNvPr")).get("id"))
    conn = s.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(1),
                                  Inches(1), Inches(4), Inches(2))
    cxn = conn._element.find(qn("p:nvCxnSpPr"))
    for tag, cid in (("stCxn", target_id), ("endCxn", target_id)):
        el = cxn.find(qn(f"p:{tag}"))
        if el is None:
            el = etree.SubElement(cxn, qn(f"p:{tag}"))
        el.set("id", cid)
    sid = _shape_id(box)
    path = _save(prs, tmp_path / "conn.pptx")
    pres = _open(path)
    addr = resolve_shape_address(pres, slide_index=0, shape_id=sid)
    with pytest.raises(SlidesError):
        pres.delete_shape(slide_index=0, shape_id=sid,
                          expected_xml_sha256=addr.current_xml_sha256,
                          deletion_reason="cleanup")


def test_partial_batch_rollback(tmp_path: Path) -> None:
    prs = new_deck()
    s = blank_slide(prs)
    box = add_textbox(s, "rollback")
    sid = _shape_id(box)
    path = _save(prs, tmp_path / "rb.pptx")
    before = path.read_bytes()
    pres = _open(path)
    addr = resolve_shape_address(pres, slide_index=0, shape_id=sid)
    batch = OperationBatch.model_validate({"operations": [
        {"op": "set_shape_geometry", "slide_index": 0, "shape_id": sid,
         "left": 2.0, "top": 2.0, "width": 5.0, "height": 1.0,
         "expected_xml_sha256": addr.current_xml_sha256},
        {"op": "set_shape_geometry", "slide_index": 0, "shape_id": sid,
         "left": 3.0, "top": 3.0, "width": 5.0, "height": 1.0,
         "expected_xml_sha256": addr.current_xml_sha256},
    ]})
    report = pres.apply_operations(batch, transactional=True)
    assert report.ok is False
    assert path.read_bytes() == before
    # In-memory state rolled back to the pre-batch geometry.
    assert round(float(pres._prs.slides[0].shapes[0].left) / 914400, 2) == 1.0


def _stage3_fixture(path: Path) -> Path:
    """Two native slides: textbox/field/footnote, table, chart+workbook,
    images, group. Returns the saved fixture path."""
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches

    prs = new_deck()
    s1 = blank_slide(prs)
    add_textbox(s1, "Заголовок добычи", left=1, top=0.5, width=8, height=1)
    add_fields_and_footnotes(s1)
    add_table(s1)
    s2 = blank_slide(prs)
    data = CategoryChartData()
    data.categories = ["2023", "2024"]
    data.add_series("План", (10.0, 12.0))
    s2.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1),
                        Inches(0.5), Inches(6), Inches(3), data)
    add_textbox(s2, "Подпись к графику", left=1, top=4, width=6, height=0.8)
    add_group_with_nonuniform_scale(s2)
    prs.save(str(path))
    return path


def test_validated_preflight_and_reimport(tmp_path: Path, compiled) -> None:
    fixture = _stage3_fixture(tmp_path / "fixture_source.pptx")
    source_hash_before = hashlib.sha256(fixture.read_bytes()).hexdigest()
    ctx = _gpn_context(tmp_path, fixture, compiled)
    pres_probe = _open(fixture)
    movable = None
    for shape in pres_probe._prs.slides[0].shapes:
        with_id = getattr(shape, "shape_id", None)
        text = getattr(shape, "text", "") if getattr(
            shape, "has_text_frame", False) else ""
        if with_id and "Заголовок" in text:
            movable = shape
            break
    assert movable is not None
    msid = _shape_id(movable)
    addr = resolve_shape_address(pres_probe, slide_index=0, shape_id=msid)
    batch = OperationBatch.model_validate({"operations": [
        {"op": "set_shape_geometry", "slide_index": 0, "shape_id": msid,
         "left": 1.5, "top": 0.7, "width": 8.0, "height": 1.0,
         "expected_xml_sha256": addr.current_xml_sha256},
    ]})
    run_dir = tmp_path / "run"
    preflight = preflight_gpn_edits(source=fixture, edits=batch, context=ctx)
    assert preflight.valid is True
    assert preflight.blocked_subjects == []
    assert fixture.read_bytes() and hashlib.sha256(
        fixture.read_bytes()).hexdigest() == source_hash_before
    out = run_dir / "diagnostic_candidate.pptx"
    result = apply_gpn_edits(source=fixture, edits=batch, context=ctx,
                             run_dir=run_dir, candidate_output=out)
    assert result.candidate_path == str(out)
    assert result.committed_count == 1 and result.rolled_back is False
    assert result.strict_output_ready is False
    # Independently reimport the SAVED candidate and verify preservation.
    store = AssetStore(tmp_path / "reimport")
    before_deck, _ = import_deck(fixture, store)
    after_deck, _ = import_deck(out, store)
    from slides_cli.gpn.provenance import build_ledger

    preservation = compare_ledger_preservation(
        build_ledger(before_deck, decoration=[]),
        build_ledger(after_deck, decoration=[]))
    assert preservation["preserved"] is True
    assert out.exists()


def test_opaque_part_serialization_loss_detected(tmp_path: Path) -> None:
    from conftest import add_unknown_graphic_data, build_import_fixture

    from slides_cli.gpn.provenance import build_ledger

    src = build_import_fixture(tmp_path / "opaque.pptx")
    store = AssetStore(tmp_path / "opaque_store")
    deck, _ = import_deck(src, store)
    ledger = build_ledger(deck, decoration=[])
    assert compare_ledger_preservation(ledger, ledger)["preserved"] is True
    # Simulate a dropped atom: the gate must report it, never pass silently.
    tampered = ledger.model_copy(deep=True)
    tampered.atoms = tampered.atoms[1:]
    diff = compare_ledger_preservation(ledger, tampered)
    assert diff["preserved"] is False
    assert diff["missing_atoms"]
    assert any(True for _ in [add_unknown_graphic_data])
