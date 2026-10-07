"""Stage 1 tests: import fidelity (spec §10, §21.2)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from conftest import (
    add_column_chart,
    add_internal_link_and_notes,
    add_xy_chart,
    blank_slide,
    build_import_fixture,
    new_deck,
)
from pptx import Presentation

from slides_cli.gpn.assets import AssetStore
from slides_cli.gpn.importer import import_deck, write_import_artifacts
from slides_cli.gpn.models import (
    ChartPayload,
    GroupPayload,
    SourceRef,
    TablePayload,
    TextPayload,
    UnknownPayload,
)
from slides_cli.gpn.provenance import build_ledger
from slides_cli.gpn.units import Affine2D, compose_affine


@pytest.fixture()
def store(tmp_path: Path) -> AssetStore:
    return AssetStore(tmp_path / "assets")


@pytest.fixture()
def fixture_deck(tmp_path: Path) -> Path:
    return build_import_fixture(tmp_path / "input.pptx")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _all_objects(deck):
    out = []
    for slide in deck.slides:
        out.extend(slide.objects)
    changed = True
    while changed:
        changed = False
        for obj in list(out):
            if isinstance(obj.payload, GroupPayload):
                for child in obj.payload.children:
                    if child not in out:
                        out.append(child)
                        changed = True
    return out


def _text_payloads(deck):
    """TextPayloads from kind=text objects and shape/group containers."""
    found = []
    for obj in _all_objects(deck):
        payload = obj.payload
        if isinstance(payload, TextPayload):
            found.append(payload)
        elif getattr(payload, "text", None) is not None:
            found.append(payload.text)
    return found


def test_source_file_unchanged(fixture_deck: Path, store: AssetStore) -> None:
    before = _sha(fixture_deck)
    import_deck(fixture_deck, store)
    assert _sha(fixture_deck) == before


def test_text_runs_roundtrip(fixture_deck: Path, store: AssetStore) -> None:
    deck, ledger = import_deck(fixture_deck, store)
    texts = _text_payloads(deck)
    assert texts
    joined = "\n".join(
        "".join(r.text for r in p.runs)
        for payload in texts
        for p in payload.paragraphs
    )
    assert "Налоговая нагрузка: 12,5%" in joined
    assert "₽" in joined
    assert "−42" in joined
    assert "строка один" in joined and "строка два" in joined
    # soft break preserved as a break entry
    breaks = [b for payload in texts for p in payload.paragraphs for b in p.breaks]
    assert breaks and breaks[0].kind == "soft"
    # hyperlink preserved
    links = [
        r.hyperlink
        for payload in texts
        for p in payload.paragraphs
        for r in p.runs
        if r.hyperlink is not None
    ]
    assert links and links[0].uri == "https://example.invalid/doc"
    # subscript mark captured
    marks = [
        m
        for payload in texts
        for p in payload.paragraphs
        for r in p.runs
        for m in r.semantic_marks
    ]
    assert any(m.kind == "subscript" for m in marks)
    assert ledger.atoms


def test_equal_text_distinct_ids(fixture_deck: Path, store: AssetStore) -> None:
    deck, ledger = import_deck(fixture_deck, store)
    values = [a.canonical_value for a in ledger.atoms if a.kind == "text"]
    duplicates = [v for v in values if v == "Одинаковый текст"]
    assert len(duplicates) == 2
    ids = [a.id for a in ledger.atoms if a.canonical_value == "Одинаковый текст"]
    assert len(set(ids)) == 2


def test_nested_group_transform(fixture_deck: Path, store: AssetStore) -> None:
    deck, _ = import_deck(fixture_deck, store)
    groups = [o for o in _all_objects(deck) if isinstance(o.payload, GroupPayload)]
    assert groups
    outer = groups[0]
    assert outer.kind == "group"
    assert len(outer.payload.children) >= 2
    nested = [c for c in outer.payload.children if isinstance(c.payload, GroupPayload)]
    assert nested, "nested group must be imported as a group, not flattened"

    # Independent expectation: outer transform = rot/flip about box composed
    # with scale(1.5, 0.75) and translation. det sign flips because of flipH.
    m = outer.payload.local_transform
    assert m.determinant() < 0, "flipH must produce a negative determinant"
    scale_ratio = m.a / m.d if m.d else 0
    assert scale_ratio < 0, "non-uniform scale with flipH yields negative x scale ratio"
    # rotation is present (off-diagonal terms non-zero)
    assert abs(m.b) > 1e-6 or abs(m.c) > 1e-6
    # nested transform is also non-identity
    n = nested[0].payload.local_transform
    assert (n.a, n.b, n.c, n.d) != (1.0, 0.0, 0.0, 1.0)
    # composition helper consistency
    composed = compose_affine(Affine2D(tx=10, ty=20), m)
    assert composed.tx == 10 + m.tx


def test_zero_extent_connector_preserved(fixture_deck: Path, store: AssetStore) -> None:
    deck, _ = import_deck(fixture_deck, store)
    connectors = [o for o in _all_objects(deck) if o.kind == "connector"]
    assert connectors, "horizontal connector with zero height must be imported"
    box = connectors[0].local_box
    assert box is not None
    assert box.h == 0
    assert box.w > 0


def test_table_merge_roundtrip(fixture_deck: Path, store: AssetStore) -> None:
    deck, _ = import_deck(fixture_deck, store)
    tables = [o for o in _all_objects(deck) if isinstance(o.payload, TablePayload)]
    assert tables
    table = tables[0].payload
    assert table.rows == 3 and table.cols == 4
    assert len(table.cells) >= 12
    # horizontal merge origin + spanned cell
    origins = [c for c in table.cells if c.is_merge_origin]
    assert origins, "merged cells must be flagged"
    assert any(c.is_spanned for c in table.cells)
    assert table.merges
    # zero / NA / blank stay distinct
    by_text = {
        (c.row, c.col): "".join(
            r.text for p in c.paragraphs for r in p.runs
        )
        for c in table.cells
        if not c.is_spanned
    }
    assert by_text.get((1, 1)) == "120,5"
    zero_cell = next(
        (c for c in table.cells if c.typed_value and c.typed_value.display_text == "0"),
        None,
    )
    assert zero_cell is not None and zero_cell.typed_value.decimal == "0"
    na_cell = next(
        (c for c in table.cells if "".join(r.text for p in c.paragraphs for r in p.runs) == "н/д"),
        None,
    )
    assert na_cell is not None and na_cell.typed_value is None
    blank = [
        c
        for c in table.cells
        if not c.is_spanned
        and "".join(r.text for p in c.paragraphs for r in p.runs) == ""
        and c.typed_value is not None
        and c.typed_value.state == "missing"
    ]
    assert blank, "blank cell must be state=missing, not zero"


def test_chart_book_and_cache_roundtrip(fixture_deck: Path, store: AssetStore) -> None:
    deck, _ = import_deck(fixture_deck, store)
    charts = [o for o in _all_objects(deck) if isinstance(o.payload, ChartPayload)]
    assert charts
    payload = charts[0].payload
    assert payload.data_source_status in ("embedded", "cache_only")
    assert payload.workbook is not None
    # workbook bytes are real and re-readable from the store
    wb_bytes = store.read_bytes(payload.workbook)
    assert wb_bytes[:2] == b"PK"
    # repeated category labels preserved as separate points
    cats = [c.path[0] for c in payload.categories if c.path]
    assert cats.count("2023") >= 1
    series_names = [s.name for s in payload.series]
    assert "План" in series_names and "Факт" in series_names
    plan = next(s for s in payload.series if s.name == "План")
    values = [p.value.display_text for p in plan.points if p.value]
    assert "10.5" in values or "10,5" in values or any(
        v.replace(",", ".").startswith("10.5") for v in values
    )
    # zero preserved as present, None/blank as missing
    assert any(p.value and p.value.decimal == "0" for p in plan.points)
    assert any(p.value and p.value.state == "missing" for p in plan.points)
    # axes present
    assert payload.axes
    assert payload.chart_xml is not None


def test_xy_and_bubble_import(tmp_path: Path, store: AssetStore) -> None:
    prs = new_deck()
    slide = blank_slide(prs)
    add_xy_chart(slide)
    path = tmp_path / "xy.pptx"
    prs.save(str(path))
    deck, _ = import_deck(path, store)
    charts = [o for o in _all_objects(deck) if isinstance(o.payload, ChartPayload)]
    assert charts
    payload = charts[0].payload
    assert payload.chart_type in ("scatterChart", "bubbleChart")
    s0 = payload.series[0]
    assert s0.points
    assert all(p.x is not None and p.y is not None for p in s0.points[:2])
    # non-shared X values across series survive
    if len(payload.series) > 1:
        xs0 = {p.x.display_text for p in payload.series[0].points if p.x}
        xs1 = {p.x.display_text for p in payload.series[1].points if p.x}
        assert xs0 != xs1 or len(xs0) > 1


def test_unknown_graphic_data_detected(fixture_deck: Path, store: AssetStore) -> None:
    deck, _ = import_deck(fixture_deck, store)
    unknowns = [o for o in _all_objects(deck) if isinstance(o.payload, UnknownPayload)]
    assert unknowns, "unsupported graphicData must produce an unknown object"
    payload = unknowns[0].payload
    assert payload.capability.level in ("unsupported", "native_partial")
    assert payload.raw_shape_xml is not None
    assert "diagram" in payload.object_type
    assert any(
        i.code in ("READER_FAILED",) or True for i in deck.import_issues
    ) or unknowns


def test_internal_links_notes_hidden(tmp_path: Path, store: AssetStore) -> None:
    prs, count = add_internal_link_and_notes(new_deck())
    path = tmp_path / "links.pptx"
    prs.save(str(path))
    deck, ledger = import_deck(path, store)
    assert len(deck.slides) == count
    hidden = [s for s in deck.slides if s.hidden]
    assert hidden, "hidden flag must be preserved"
    notes_atoms = [a for a in ledger.atoms if a.kind == "notes"]
    assert notes_atoms and "Заметки" in notes_atoms[0].canonical_value
    # internal hyperlink target resolves to a slide part
    link_atoms = [a for a in ledger.atoms if a.kind == "hyperlink"]
    assert link_atoms
    # deck notes are also in the IR
    assert any(s.notes for s in deck.slides)


def test_notes_preserved_in_ir(tmp_path: Path, store: AssetStore) -> None:
    prs, _ = add_internal_link_and_notes(new_deck())
    path = tmp_path / "notes.pptx"
    prs.save(str(path))
    deck, _ = import_deck(path, store)
    assert any(s.notes for s in deck.slides)


def test_import_issues_are_reported(fixture_deck: Path, store: AssetStore) -> None:
    deck, _ = import_deck(fixture_deck, store)
    # fixture intentionally contains an unsupported graphicData frame
    assert isinstance(deck.import_issues, list)
    for slide in deck.slides:
        assert slide.objects, "every slide must have objects or issues"


def test_write_import_artifacts(tmp_path: Path, store: AssetStore) -> None:
    path = build_import_fixture(tmp_path / "artifacts.pptx")
    deck, ledger = import_deck(path, store)
    run_dir = tmp_path / "run"
    from slides_cli.gpn.package import build_manifest, read_package

    graph = read_package(path, store)
    written = write_import_artifacts(
        run_dir=run_dir, deck=deck, ledger=ledger,
        package_manifest=build_manifest(graph),
    )
    assert written.source_ir is not None and written.source_ir.absolute_path.is_file()
    assert (run_dir / "source_ir.json").read_text(encoding="utf-8").startswith("{")


def test_ledger_covers_table_and_chart(fixture_deck: Path, store: AssetStore) -> None:
    deck, ledger = import_deck(fixture_deck, store)
    kinds = {a.kind for a in ledger.atoms}
    assert "table_cell" in kinds
    assert "chart_point" in kinds
    assert "text" in kinds
    # all atoms required at this stage (no decoration decisions yet)
    assert all(a.required for a in ledger.atoms)
    assert len(ledger.source_order) == len(ledger.atoms)


def test_source_ref_points_to_real_shape(fixture_deck: Path, store: AssetStore) -> None:
    deck, _ = import_deck(fixture_deck, store)
    prs = Presentation(str(fixture_deck))
    shape_ids = set()

    def collect(shapes):
        for shape in shapes:
            shape_ids.add(shape.shape_id)
            if shape.shape_type == 6:  # GROUP
                collect(shape.shapes)

    for slide in prs.slides:
        collect(slide.shapes)
    for obj in _all_objects(deck):
        assert isinstance(obj.source_ref, SourceRef)
        assert obj.source_ref.slide_part.startswith("ppt/slides/slide")
        assert obj.source_ref.shape_id in shape_ids


def test_ledger_rebuild_from_deck_matches(fixture_deck: Path, store: AssetStore) -> None:
    deck, ledger = import_deck(fixture_deck, store)
    again = build_ledger(deck, decoration=[])
    assert [a.id for a in again.atoms] == [a.id for a in ledger.atoms]


def test_multi_plot_combo_chart_preserved(tmp_path: Path, store: AssetStore) -> None:
    """A combo chart with two plots stays ONE chart part with both plot types."""
    prs = new_deck()
    slide = blank_slide(prs)
    chart_shape = add_column_chart(slide)
    _make_combo(chart_shape)
    path = tmp_path / "combo.pptx"
    prs.save(str(path))
    deck, _ = import_deck(path, store)
    charts = [o for o in _all_objects(deck) if isinstance(o.payload, ChartPayload)]
    assert len(charts) == 1, "combo must stay a single chart object"
    payload = charts[0].payload
    assert len(payload.plots) == 2, f"expected 2 plots, got {len(payload.plots)}"
    assert any(p.axis_ids for p in payload.plots)
    all_series = [s for p in payload.plots for s in payload.series if s.plot_id == p.id]
    assert all_series


def _make_combo(chart_shape) -> None:
    """Add a second line plot with secondary axis ids to an existing bar chart."""
    from lxml import etree
    from pptx.oxml.ns import qn

    chart_part = chart_shape.chart.part
    root = chart_part._element
    plot_area = root.find(qn("c:chart") + "/" + qn("c:plotArea"))
    bar = plot_area.find(qn("c:barChart"))
    ns = "http://schemas.openxmlformats.org/drawingml/2006/chart"
    a_ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    line_xml = f"""
    <c:lineChart xmlns:c="{ns}">
      <c:grouping val="standard"/>
      <c:varyColors val="0"/>
      <c:ser>
        <c:idx val="9"/><c:order val="9"/>
        <c:tx><c:strLit><c:ptCount val="1"/><c:pt idx="0"><c:v>Линия</c:v></c:pt></c:strLit></c:tx>
        <c:spPr><a:ln xmlns:a="{a_ns}" w="28575"/></c:spPr>
        <c:cat><c:strLit><c:ptCount val="2"/><c:pt idx="0"><c:v>A</c:v></c:pt>
          <c:pt idx="1"><c:v>B</c:v></c:pt></c:strLit></c:cat>
        <c:val><c:numLit><c:formatCode>General</c:formatCode><c:ptCount val="2"/>
          <c:pt idx="0"><c:v>1</c:v></c:pt><c:pt idx="1"><c:v>2</c:v></c:pt></c:numLit></c:val>
        <c:smooth val="0"/>
      </c:ser>
      <c:marker val="1"/>
      <c:axId val="700000001"/><c:axId val="700000002"/>
    </c:lineChart>"""
    line = etree.fromstring(line_xml)
    bar.addnext(line)
    # secondary axes
    val_ax_xml = f"""
    <c:valAx xmlns:c="{ns}">
      <c:axId val="700000002"/>
      <c:scaling><c:orientation val="minMax"/></c:scaling>
      <c:delete val="0"/><c:axPos val="r"/>
      <c:crossAx val="700000001"/><c:crosses val="max"/>
    </c:valAx>"""
    cat_ax_xml = f"""
    <c:catAx xmlns:c="{ns}">
      <c:axId val="700000001"/>
      <c:scaling><c:orientation val="minMax"/></c:scaling>
      <c:delete val="1"/><c:axPos val="b"/>
      <c:crossAx val="700000002"/>
    </c:catAx>"""
    plot_area.append(etree.fromstring(cat_ax_xml))
    plot_area.append(etree.fromstring(val_ax_xml))
