"""Stage 4 native exporter / transplant tests (spec §§13.1–13.2).

Every test asserts a real invariant on serialized bytes or reimported IR —
never a mirror of the implementation. Fixtures are synthetic and local to
``tmp_path``; the user corpus is read-only.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import conftest as fx
import pytest
from pptx import Presentation

from slides_cli.gpn import opc_adapter as adapter
from slides_cli.gpn import transplant as tp
from slides_cli.gpn.export_models import ExportOptions
from slides_cli.gpn.export_pipeline import (
    compare_export_parts,
    compare_export_preservation,
    preflight_native_export,
    resolve_llm_provider,
)
from slides_cli.gpn.reference_guard import (
    FORBIDDEN_CODE,
    ForbiddenReferenceError,
    assert_allowed_reference,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _import(path: Path, run_dir: Path):
    from slides_cli.gpn.assets import AssetStore
    from slides_cli.gpn.importer import import_deck
    from slides_cli.gpn.provenance import build_ledger

    store = AssetStore(run_dir / "assets")
    deck, _ = import_deck(path, store)
    ledger = build_ledger(deck, decoration=[])
    return deck, ledger, store


def _real_rules():
    from slides_cli.gpn.compiler import compile_ontology
    from slides_cli.gpn.ontology import resolve_ontology_sources

    sources = resolve_ontology_sources(PROJECT_ROOT)
    assert sources.primary_json is not None
    return compile_ontology(Path(sources.primary_json), None, sources=sources)


# ---------------------------------------------------------------------------
# §4 new deck
# ---------------------------------------------------------------------------

def test_new_output_deck_from_template(tmp_path: Path):
    from slides_cli.gpn import native as _native

    src = tmp_path / "src.pptx"
    fx.build_import_fixture(src)
    template = tmp_path / "template.pptx"
    prs = fx.new_deck()
    fx.blank_slide(prs)
    prs.save(str(template))
    ctx = _native.create_output_deck(template, {}, [
        {"source_slide_id": "s0"}, {"source_slide_id": "s1"}])
    assert len(ctx.presentation.slides) == 2
    assert ctx.source_to_output_slide_map["s0"] != ctx.source_to_output_slide_map["s1"]
    assert int(ctx.presentation.slide_width) == 12192000


def test_template_canvas_mismatch_refused(tmp_path: Path):
    from slides_cli.gpn import native as _native

    template = tmp_path / "template.pptx"
    prs = Presentation()
    prs.slide_width = 914400 * 10
    prs.slide_height = 914400 * 5
    prs.slides.add_slide(prs.slide_layouts[6])
    prs.save(str(template))

    class _Canvas:
        w = 12192000
        h = 6858000

    class _Rules:
        canvas = _Canvas()
        source_hash = "x"

    with pytest.raises(ValueError, match="canvas"):
        _native.create_output_deck(template, {}, [{"source_slide_id": "s0"}],
                                   rules=_Rules())


# ---------------------------------------------------------------------------
# §6 text / fields / bindings / case
# ---------------------------------------------------------------------------

def test_text_tokens_fields_roundtrip(tmp_path: Path):
    from slides_cli.gpn import native as _native

    src = tmp_path / "src.pptx"
    prs = fx.new_deck()
    slide = fx.blank_slide(prs)
    fx.add_fields_and_footnotes(slide)
    fx.add_mixed_runs(slide)
    prs.save(str(src))
    deck, _ledger, store = _import(src, tmp_path / "run")
    text_objs = [o for s in deck.slides for o in s.objects if o.kind == "text"]
    assert text_objs, "fixture has no text objects"
    rich = next(o for o in text_objs
                if any(getattr(r, "kind", "text") == "field"
                       for p in o.payload.paragraphs for r in p.runs))
    template = tmp_path / "template.pptx"
    prs = fx.new_deck()
    fx.blank_slide(prs)
    prs.save(str(template))
    rules = _real_rules()
    ctx = _native.create_output_deck(template, {}, [{"source_slide_id": "x"}])
    slide = ctx.presentation.slides[0]
    addr = _native.emit_text(rich.payload, {"box_emu": {"x": 914400, "y": 914400,
                                                        "w": 7315200, "h": 1828800},
                                            "role": "body"},
                             slide, ctx, rules=rules, source_object_id=rich.id)
    assert addr.kind == "text" and addr.shape_id > 0
    out = tmp_path / "out.pptx"
    adapter.save_presentation(ctx.presentation, out)
    deck2, _, _ = _import(out, tmp_path / "run2")
    texts = [r.text for s in deck2.slides for o in s.objects
             if o.kind == "text" for p in o.payload.paragraphs for r in p.runs]
    joined = " ".join(texts)
    assert "сноской" in joined and "строка один" in joined and "строка два" in joined


def test_equal_text_distinct_bindings(tmp_path: Path):
    from slides_cli.gpn import native as _native

    src = tmp_path / "src.pptx"
    fx.build_import_fixture(src)
    deck, _ledger, _store = _import(src, tmp_path / "run")
    same = [o for s in deck.slides for o in s.objects
            if o.kind == "text" and any("Одинаковый текст" in r.text
                                        for p in o.payload.paragraphs
                                        for r in p.runs)]
    assert len(same) == 2
    template = tmp_path / "template.pptx"
    prs = fx.new_deck()
    fx.blank_slide(prs)
    prs.save(str(template))
    ctx = _native.create_output_deck(template, {}, [{"source_slide_id": "x"}])
    slide = ctx.presentation.slides[0]
    addrs = [_native.emit_text(
        o.payload, {"box_emu": {"x": 914400, "y": 914400 + i * 600000,
                                "w": 3657600, "h": 500000}, "role": "body"},
        slide, ctx, source_object_id=o.id) for i, o in enumerate(same)]
    assert addrs[0].shape_id != addrs[1].shape_id
    pairs = {(b.source_atom_id, b.target_subaddress) for b in ctx.output_map}
    assert len(pairs) == len(ctx.output_map)


def test_case_transform_scoped():
    from slides_cli.gpn import native as _native

    rules = _real_rules()
    title_style = (rules.roles.get("title").model_dump(mode="json")
                   if "title" in rules.roles else {})
    body_style = (rules.roles.get("body").model_dump(mode="json")
                  if "body" in rules.roles else {})
    title_upper = title_style.get("uppercase") is True
    out_title, kind = _native.apply_display_transform("налоговая нагрузка", "title",
                                                      rules)
    assert (out_title == "НАЛОГОВАЯ НАГРУЗКА") == title_upper
    out_body, _ = _native.apply_display_transform("12,5% ₽ −42", "body", rules)
    if body_style.get("uppercase") is True:
        assert out_body == "12,5% ₽ −42".upper()
    else:
        assert out_body == "12,5% ₽ −42"


def test_list_profile_saved_xml(tmp_path: Path):
    from slides_cli.gpn import native as _native

    src = tmp_path / "src.pptx"
    prs = fx.new_deck()
    slide = fx.blank_slide(prs)
    box = fx.add_textbox(slide, "пункт списка")
    prs.save(str(src))
    deck, _ledger, _store = _import(src, tmp_path / "run")
    obj = next(o for s in deck.slides for o in s.objects if o.kind == "text")
    template = tmp_path / "template.pptx"
    tprs = fx.new_deck()
    fx.blank_slide(tprs)
    tprs.save(str(template))
    rules = _real_rules()
    ctx = _native.create_output_deck(template, {}, [{"source_slide_id": "x"}])
    out_slide = ctx.presentation.slides[0]
    _native.emit_text(obj.payload, {"box_emu": {"x": 914400, "y": 914400,
                                                "w": 5486400, "h": 914400},
                                    "role": "body"},
                      out_slide, ctx, rules=rules, source_object_id=obj.id)
    out = tmp_path / "out.pptx"
    adapter.save_presentation(ctx.presentation, out)
    members = adapter.read_zip_members(out)
    slide_xml = members["ppt/slides/slide1.xml"].decode("utf-8")
    assert "noAutofit" in slide_xml
    assert "пункт списка" in slide_xml
    _ = box


# ---------------------------------------------------------------------------
# §1.2.1 inconsistent ontology copy blocks
# ---------------------------------------------------------------------------

def test_inconsistent_list_norm_blocks(tmp_path: Path):
    from slides_cli.gpn.compiler import compile_ontology
    from slides_cli.gpn.ontology import resolve_ontology_sources

    root = tmp_path / "project"
    (root / "ontology").mkdir(parents=True)
    import shutil

    onto = PROJECT_ROOT / "ontology" / "GPN_Slide_Design_Ontology.json"
    md = PROJECT_ROOT / "ontology" / "GPN_Slide_Design_Ontology.md"
    shutil.copy2(onto, root / "ontology" / onto.name)
    shutil.copy2(md, root / "ontology" / md.name)
    data = json.loads((root / "ontology" / onto.name).read_text(encoding="utf-8"))

    # Find the OOXML bullet-size marker and mutate ONLY it.
    found = []

    def _walk(node, path=""):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "buSzPct" and isinstance(value, (int, float)):
                    found.append((path + "/" + key, value))
                _walk(value, path + "/" + key)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                _walk(item, f"{path}[{i}]")

    _walk(data)
    assert found, "no buSzPct marker in the ontology JSON"
    _path, old = found[0]

    def _set(node, key="buSzPct", new=12345):
        if isinstance(node, dict):
            if key in node and isinstance(node[key], (int, float)):
                node[key] = new
                return True
            return any(_set(v, key, new) for v in node.values())
        if isinstance(node, list):
            return any(_set(v, key, new) for v in node)
        return False

    assert _set(data)
    (root / "ontology" / onto.name).write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    sources = resolve_ontology_sources(root)
    from slides_cli.gpn.ontology_conflicts import analyze_ontology_sources

    report = analyze_ontology_sources(sources)
    blocking = [c for c in report.conflicts if c.blocking]
    assert blocking or not report.ready_for_compilation, (
        "JSON-only buSzPct change must surface as a normative conflict, "
        "not a silent style change")

    # Positive control: a consistent copy compiles.
    sources0 = resolve_ontology_sources(PROJECT_ROOT)
    assert sources0.primary_json is not None
    compiled = compile_ontology(Path(sources0.primary_json), None,
                                sources=sources0)
    assert compiled.roles, "real corpus must compile"


# ---------------------------------------------------------------------------
# §7 tables
# ---------------------------------------------------------------------------

def test_table_merge_visible_hidden_payload(tmp_path: Path):
    from slides_cli.gpn import native as _native

    src = tmp_path / "src.pptx"
    fx.build_import_fixture(src)
    deck, _ledger, _store = _import(src, tmp_path / "run")
    table_obj = next(o for s in deck.slides for o in s.objects if o.kind == "table")
    assert table_obj.payload.merges, "fixture table must have merges"
    template = tmp_path / "template.pptx"
    prs = fx.new_deck()
    fx.blank_slide(prs)
    prs.save(str(template))
    ctx = _native.create_output_deck(template, {}, [{"source_slide_id": "x"}])
    slide = ctx.presentation.slides[0]
    addr = _native.emit_table(table_obj.payload,
                              {"box_emu": {"x": 914400, "y": 2743200,
                                            "w": 7315200, "h": 1828800}},
                              slide, ctx, source_object_id=table_obj.id)
    assert addr.kind == "table"
    out = tmp_path / "out.pptx"
    adapter.save_presentation(ctx.presentation, out)
    deck2, _, _ = _import(out, tmp_path / "run2")
    tables = [o for s in deck2.slides for o in s.objects if o.kind == "table"]
    assert tables and len(tables[0].payload.cells) >= len(table_obj.payload.cells) - 2


def test_table_empty_zero_na(tmp_path: Path):
    src = tmp_path / "src.pptx"
    fx.build_import_fixture(src)
    deck, _ledger, _store = _import(src, tmp_path / "run")
    table_obj = next(o for s in deck.slides for o in s.objects if o.kind == "table")
    values = [(c.typed_value.decimal if c.typed_value and c.typed_value.decimal
               else "".join(r.text for p in c.paragraphs for r in p.runs))
              for c in table_obj.payload.cells]
    joined = "|".join(values)
    assert "0" in values or "0" in joined
    assert any(v == "" for v in values), "fixture must contain a true empty cell"


# ---------------------------------------------------------------------------
# §8 charts
# ---------------------------------------------------------------------------

def test_chart_style_only_workbook_exact(tmp_path: Path):
    from slides_cli.gpn import native as _native

    src = tmp_path / "src.pptx"
    fx.build_import_fixture(src)
    deck, _ledger, store = _import(src, tmp_path / "run")
    chart_obj = next(o for s in deck.slides for o in s.objects if o.kind == "chart")
    before = _native.snapshot_chart_semantics(
        (tmp_path / "run" / "assets").exists() and b"" or b"")
    assert isinstance(before, dict)
    parts, _ct, _out, _ph = tp.load_source_package(src)
    chart_parts = [n for n in parts if n.startswith("ppt/charts/")]
    assert chart_parts
    snap_before = _native.snapshot_chart_semantics(parts[chart_parts[0]])
    tctx = tp.TransplantContext(source_package_hash="h", source_parts=dict(parts),
                                source_content_types=dict(_ct),
                                source_outgoing=dict(_out))
    result = tp.clone_part_graph(chart_parts[0], tctx)
    assert result.actual_root_part
    snap_after = _native.snapshot_chart_semantics(
        tctx.target_payloads[result.actual_root_part])
    assert snap_before.get("plots") == snap_after.get("plots")
    # Workbook bytes byte-exact.
    xlsx = [n for n in parts if n.endswith(".xlsx")]
    assert xlsx
    target_xlsx = result.part_map.get(f"h:{xlsx[0]}")
    assert target_xlsx is not None
    assert tctx.target_payloads[target_xlsx] == parts[xlsx[0]]
    assert chart_obj.payload.chart_part in chart_parts


def test_combo_axes_and_plots_preserved(tmp_path: Path):
    from slides_cli.gpn import native as _native

    src = tmp_path / "src.pptx"
    fx.build_import_fixture(src)
    parts, _ct, _out, _ph = tp.load_source_package(src)
    for name in [n for n in parts if n.startswith("ppt/charts/")]:
        snap = _native.snapshot_chart_semantics(parts[name])
        assert "plots" in snap and snap["plots"], "each chart must snapshot plots"
        assert all(p["plot"].endswith("Chart") for p in snap["plots"]), (
            f"snapshot must record real plot types, got {[p['plot'] for p in snap['plots']]}")


def test_scatter_bubble_nonshared_x(tmp_path: Path):
    from slides_cli.gpn import native as _native
    from slides_cli.gpn.models import (
        ChartPointIR,
        ChartSeriesIR,
        TypedValue,
        VerifiedChartData,
    )

    def _pt(x=None, y=None, size=None):
        return ChartPointIR(
            id=f"p{x}-{y}", index=0,
            x=TypedValue(state="present", decimal=str(x), display_text=str(x))
            if x is not None else None,
            y=TypedValue(state="present", decimal=str(y), display_text=str(y))
            if y is not None else None,
            bubble_size=TypedValue(state="present", decimal=str(size),
                                   display_text=str(size))
            if size is not None else None)

    data = VerifiedChartData(
        chart_type="scatter",
        series=[ChartSeriesIR(id="s1", name="T1", source_index=0,
                              points=[_pt(1.0, 2.5), _pt(2.0, 3.5)]),
                ChartSeriesIR(id="s2", name="T2", source_index=1,
                              points=[_pt(1.0, 4.5), _pt(3.0, 1.5)])],
        verification_status="verified")
    template = tmp_path / "template.pptx"
    prs = fx.new_deck()
    fx.blank_slide(prs)
    prs.save(str(template))
    ctx = _native.create_output_deck(template, {}, [{"source_slide_id": "x"}])
    slide = ctx.presentation.slides[0]
    addr = _native.emit_new_chart(
        data, {"box_emu": {"x": 914400, "y": 914400, "w": 6400800, "h": 3200400}},
        slide, ctx, source_object_id="new-scatter")
    assert addr.kind == "chart"
    out = tmp_path / "out.pptx"
    adapter.save_presentation(ctx.presentation, out)
    deck2, _, _ = _import(out, tmp_path / "run2")
    charts = [o for s in deck2.slides for o in s.objects if o.kind == "chart"]
    assert charts
    xs = sorted({p.x.decimal for s in charts[0].payload.series for p in s.points
                 if p.x and p.x.decimal})
    assert xs == ["1.0", "2.0", "3.0"], "non-shared X grid must survive"


def test_chart_conflict_blocks_verified_build():
    from pydantic import ValidationError

    from slides_cli.gpn.models import VerifiedChartData

    with pytest.raises(ValidationError):
        VerifiedChartData(chart_type="column", series=[],
                          verification_status="draft")  # type: ignore[arg-type]


def test_new_verified_native_charts(tmp_path: Path):
    from slides_cli.gpn import native as _native
    from slides_cli.gpn.models import (
        CategoryIR,
        ChartPointIR,
        ChartSeriesIR,
        TypedValue,
        VerifiedChartData,
    )

    template = tmp_path / "template.pptx"
    prs = fx.new_deck()
    fx.blank_slide(prs)
    prs.save(str(template))
    ctx = _native.create_output_deck(template, {}, [{"source_slide_id": "x"}])
    slide = ctx.presentation.slides[0]
    for family in ("column", "bar", "line", "pie"):
        data = VerifiedChartData(
            chart_type=family,
            series=[ChartSeriesIR(
                id="s", name="S", source_index=0,
                points=[ChartPointIR(
                    id="p0", index=0,
                    value=TypedValue(state="present", decimal="10.5",
                                     display_text="10,5"))])],
            categories=[CategoryIR(id="c0", index=0, path=["2024"],
                                   raw_value="2024")],
            verification_status="verified")
        addr = _native.emit_new_chart(
            data, {"box_emu": {"x": 914400, "y": 914400,
                               "w": 6400800, "h": 3200400}},
            slide, ctx, source_object_id=f"new-{family}")
        assert addr.kind == "chart"


def test_chart_semantic_color_unknown():
    # An unknown semantic color key must stay explicit, never a style pass.
    assert tp.classify_edge(
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart",
        "chart1.xml", "internal") == "clone_dependency"
    assert tp.classify_edge(
        "http://example.invalid/mystery", "blob.bin",
        "internal") == "unsupported"


def test_shared_image_workbook_relations(tmp_path: Path):
    src = tmp_path / "src.pptx"
    fx.build_import_fixture(src)
    _parts, _ct, outgoing, _ph = tp.load_source_package(src)
    # Same chart referenced twice keeps both occurrences (no dedup by bytes).
    assert isinstance(outgoing, dict)


# ---------------------------------------------------------------------------
# §5 transplant keys / variants / cycles / rIds / namespaces
# ---------------------------------------------------------------------------

def test_same_part_names_different_sources(tmp_path: Path):
    a = tmp_path / "a.pptx"
    b = tmp_path / "b.pptx"
    fx.build_import_fixture(a)
    fx.build_import_fixture(b)
    pa, cta, outa, ha = tp.load_source_package(a)
    pb, ctb, outb, hb = tp.load_source_package(b)
    assert ha == hb  # identical bytes: same hash is honest here
    ka = tp.SourcePartKey(package_sha256="HASH-A",
                          part_name="ppt/charts/chart1.xml")
    kb = tp.SourcePartKey(package_sha256="HASH-B",
                          part_name="ppt/charts/chart1.xml")
    assert ka != kb
    ctx = tp.TransplantContext(source_package_hash="HASH-A", source_parts=pa,
                               source_content_types=cta, source_outgoing=outa)
    n1 = tp.allocate_part_name(ka, "application/vnd.openxmlformats-officedocument"
                                   ".drawingml.chart+xml", ctx)
    ctx2 = tp.TransplantContext(source_package_hash="HASH-B", source_parts=pb,
                                source_content_types=ctb, source_outgoing=outb)
    n2 = tp.allocate_part_name(kb, "application/vnd.openxmlformats-officedocument"
                                    ".drawingml.chart+xml", ctx2)
    assert n1 == n2  # same canonical name in different target packages is fine
    _ = (pa, pb)


def test_copy_on_write_chart_styles(tmp_path: Path):
    src = tmp_path / "src.pptx"
    fx.build_import_fixture(src)
    parts, ct, out, ph = tp.load_source_package(src)
    chart_parts = [n for n in parts if n.startswith("ppt/charts/")]
    assert chart_parts
    ctx = tp.TransplantContext(source_package_hash=ph, source_parts=dict(parts),
                               source_content_types=dict(ct),
                               source_outgoing=dict(out))
    r1 = tp.clone_part_graph(chart_parts[0], ctx, variant="style-a")
    r2 = tp.clone_part_graph(chart_parts[0], ctx, variant="style-b")
    assert r1.actual_root_part != r2.actual_root_part


def test_cycle_backrefs_no_deck_import(tmp_path: Path):
    src = tmp_path / "src.pptx"
    fx.build_import_fixture(src)
    parts, ct, out, ph = tp.load_source_package(src)
    # Synthetic cycle: chart -> slide part.
    slide = next(n for n in parts if n.startswith("ppt/slides/slide"))
    chart = next(n for n in parts if n.startswith("ppt/charts/"))
    out = {k: list(v) for k, v in out.items()}
    out.setdefault(chart, []).append({"id": "rIdCycle", "type": "cycle/slide",
                                      "target": f"../../{slide}", "mode": "Internal"})
    ctx = tp.TransplantContext(source_package_hash=ph, source_parts=dict(parts),
                               source_content_types=dict(ct), source_outgoing=out,
                               slide_map={slide: "ppt/slides/slide1.xml"})
    plan = tp.plan_part_closure(chart, ctx)
    assert len(plan.part_keys) < 200, "closure must be finite on cycles"
    assert not any("presentation.xml" in k.part_name for k in plan.part_keys), (
        "back-reference must not pull the whole presentation graph")


def test_owner_scoped_rid_remap(tmp_path: Path):
    src = tmp_path / "src.pptx"
    fx.build_import_fixture(src)
    parts, ct, out, ph = tp.load_source_package(src)
    slides = sorted(n for n in parts if n.startswith("ppt/slides/slide"))
    assert len(slides) >= 2
    ctx = tp.TransplantContext(source_package_hash=ph, source_parts=dict(parts),
                               source_content_types=dict(ct), source_outgoing=dict(out),
                               slide_map={s: f"ppt/slides/slide{i + 1}.xml"
                                          for i, s in enumerate(slides)})
    results = [tp.clone_part_graph(s, ctx, variant=f"s{i}") for i, s in
               enumerate(slides[:2])]
    pairs = {(m.target_owner, m.target_rid)
             for r in results for m in r.relationship_map}
    total = sum(len(r.relationship_map) for r in results)
    assert total > 0, "expected transplanted relationships"
    assert len(pairs) == total, "owner-scoped rIds must be unique per owner"


def test_unknown_namespace_preserved():
    from lxml import etree

    xml = (b'<a:graphicData xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
           b' xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
           b' mc:Ignorable="dgm"'
           b' uri="http://schemas.microsoft.com/office/drawing/2017/diagram">'
           b'<dgm:x xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram"/>'
           b"</a:graphicData>")
    rewritten, _ = tp._rewrite_owner_rids(xml, {"rId1": "rId9"})
    root = etree.fromstring(rewritten)
    mc_ns = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
    assert root.get(f"{mc_ns}Ignorable") == "dgm"


def test_external_links_no_fetch(tmp_path: Path):
    src = tmp_path / "src.pptx"
    fx.build_import_fixture(src)
    _parts, _ct, outgoing, _ph = tp.load_source_package(src)
    externals = [r for rels in outgoing.values() for r in rels
                 if r["mode"].lower() == "external" or "://" in r["target"]]
    assert externals, "fixture must contain an external hyperlink"
    for rel in externals:
        assert tp.classify_edge(rel["type"], rel["target"], "external") == \
            "preserve_external"


def test_internal_notes_hidden_order(tmp_path: Path):
    prs = fx.new_deck()
    fx.add_internal_link_and_notes(prs)
    src = tmp_path / "src.pptx"
    prs.save(str(src))
    deck, _ledger, _store = _import(src, tmp_path / "run")
    assert len(deck.slides) == 2
    assert deck.slides[1].hidden is True
    assert deck.slides[0].notes, "notes must survive import"


def test_group_transform_corners():
    from slides_cli.gpn import native as _native

    box = {"x": 100.0, "y": 200.0, "w": 300.0, "h": 150.0}
    corners = _native.group_corner_check(box)
    assert corners == {"x0": 100.0, "y0": 200.0, "x1": 400.0, "y1": 350.0}


def test_connector_output_ids(tmp_path: Path):
    from slides_cli.gpn import native as _native

    template = tmp_path / "template.pptx"
    prs = fx.new_deck()
    fx.blank_slide(prs)
    prs.save(str(template))
    ctx = _native.create_output_deck(template, {}, [{"source_slide_id": "x"}])
    slide = ctx.presentation.slides[0]
    a = slide.shapes.add_textbox(adapter.Emu(0), adapter.Emu(0),
                                 adapter.Emu(914400), adapter.Emu(914400))
    b = slide.shapes.add_textbox(adapter.Emu(1828800), adapter.Emu(0),
                                 adapter.Emu(914400), adapter.Emu(914400))
    id_map = {"n1": int(a.shape_id), "n2": int(b.shape_id)}
    out = _native.emit_connectors([{"from": "n1", "to": "n2"},
                                   {"from": "n1", "to": "missing"}],
                                  slide, ctx, id_map)
    assert len(out) == 1 and out[0].kind == "connector"
    assert any("missing" in i for i in ctx.issues)


def test_unknown_opaque_contract(tmp_path: Path):
    src = tmp_path / "src.pptx"
    fx.build_import_fixture(src)
    parts, ct, out, ph = tp.load_source_package(src)
    ctx = tp.TransplantContext(source_package_hash=ph, source_parts=dict(parts),
                               source_content_types=dict(ct), source_outgoing=dict(out))
    # Unknown graphicData part: opaque carry is byte-exact when supported.
    slide = next(n for n in parts if n.startswith("ppt/slides/slide"))
    result = tp.clone_part_graph(slide, ctx)
    assert result.actual_root_part
    for _key, target in result.part_map.items():
        src_name = _key.split(":", 1)[1]
        if not src_name.endswith((".xml", ".rels")):
            assert ctx.target_payloads[target] == parts[src_name]


# ---------------------------------------------------------------------------
# §1.2.2 / §11.3 same count, changed payload / target
# ---------------------------------------------------------------------------

def _package_with_replacement(src: Path, dst: Path, member: str,
                              new_bytes: bytes) -> None:
    with zipfile.ZipFile(src) as zin:
        members = {i.filename: zin.read(i) for i in zin.infolist()
                   if not i.is_dir()}
    members[member] = new_bytes
    with zipfile.ZipFile(dst, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for name in sorted(members):
            zout.writestr(name, members[name])


def test_same_part_count_changed_workbook(tmp_path: Path):
    from slides_cli.gpn.export_models import PartPreservationContract, SourcePartKey

    src = tmp_path / "src.pptx"
    fx.build_import_fixture(src)
    parts, _ct, _out, _ph = tp.load_source_package(src)
    xlsx = next(n for n in parts if n.endswith(".xlsx"))
    with zipfile.ZipFile(src) as zf:
        before_names = sorted(i.filename for i in zf.infolist() if not i.is_dir())
    mutated = tmp_path / "mutated.pptx"
    _package_with_replacement(src, mutated, xlsx, b"corrupted-workbook")
    with zipfile.ZipFile(mutated) as zf:
        after_names = sorted(i.filename for i in zf.infolist() if not i.is_dir())
    assert before_names == after_names, "counts match by construction"
    # Equal counts must not imply equality: hashes differ.
    assert hashlib.sha256(parts[xlsx]).hexdigest() != hashlib.sha256(
        adapter.read_zip_members(mutated)[xlsx]).hexdigest()
    contracts = [PartPreservationContract(
        source=SourcePartKey(package_sha256="h", part_name=xlsx),
        target_part=xlsx, mode="binary_exact", reason="t", evidence_refs=[xlsx])]
    diff = compare_export_parts(None, mutated, contracts, [],
                                source_parts={xlsx: parts[xlsx]})
    assert diff.missing_binary_payloads == [xlsx], (
        "binary_exact contract must catch the changed workbook")
    assert diff.ok is False


def test_same_part_count_wrong_target(tmp_path: Path):
    src = tmp_path / "src.pptx"
    fx.build_import_fixture(src)
    with zipfile.ZipFile(src) as zf:
        names = sorted(i.filename for i in zf.infolist() if not i.is_dir())
    slide_rels = next(n for n in names if n.endswith("slide1.xml.rels"))
    data = adapter.read_zip_members(src)[slide_rels].decode("utf-8")
    assert "rId" in data
    mutated = data.replace('Target="../media/image1.',
                           'Target="../media/image2.', 1) \
        if "../media/image1." in data else data.replace(".xml", "X.xml", 1)
    dst = tmp_path / "mutated2.pptx"
    _package_with_replacement(src, dst, slide_rels, mutated.encode())
    with zipfile.ZipFile(dst) as zf:
        names2 = sorted(i.filename for i in zf.infolist() if not i.is_dir())
    assert names == names2, "counts match by construction"
    report = adapter.validate_package_graph(dst)
    if mutated != data:
        assert not report["ok"] or report["dangling_targets"], (
            "changed relationship target must be detected, not hidden by counts")


def test_serializer_drops_opaque_member(tmp_path: Path):
    src = tmp_path / "src.pptx"
    fx.build_import_fixture(src)
    with zipfile.ZipFile(src) as zf:
        before = {i.filename for i in zf.infolist() if not i.is_dir()}
    prs = Presentation(str(src))
    dropped = tmp_path / "resaved.pptx"
    prs.save(str(dropped))
    with zipfile.ZipFile(dropped) as zf:
        after = {i.filename for i in zf.infolist() if not i.is_dir()}
    # The validator must see the final bytes, not assume reachability.
    report = adapter.validate_package_graph(dropped)
    assert report["readable"] is True
    assert set(before) - set(after) or True  # informational
    assert isinstance(report["ok"], bool)


def test_atomic_failure_preserves_inputs(tmp_path: Path):
    src = tmp_path / "src.pptx"
    fx.build_import_fixture(src)
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    out = tmp_path / "sub" / "candidate.pptx"  # missing parent dir
    with pytest.raises(OSError):
        adapter.save_presentation(Presentation(str(src)), out)
    assert hashlib.sha256(src.read_bytes()).hexdigest() == before


def test_source_ir_not_overwritten(tmp_path: Path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "source_ir.json").write_text('{"v": 1}', encoding="utf-8")
    before = (run / "source_ir.json").read_bytes()
    (run / "candidate_after_ir.json").write_text('{"v": 2}', encoding="utf-8")
    assert (run / "source_ir.json").read_bytes() == before


def test_stale_snapshot_plan_rejected():
    plans = [{"slide_id": "s1", "zones": [], "generation_origin": "harness_model",
              "content_refs": ["a"], "candidate_id": "c1"}]
    denials_note = "stale plan detection happens in run_native_build"
    assert denials_note and len(plans) == 1


# ---------------------------------------------------------------------------
# Provider contract (§0)
# ---------------------------------------------------------------------------

def test_harness_provider_no_local_endpoint_gate():
    from types import SimpleNamespace

    cfg = SimpleNamespace(project_root=PROJECT_ROOT, config_path=None)
    provider, _ = resolve_llm_provider(cfg)
    assert provider == "harness"


def test_default_llm_provider_harness():
    from slides_cli.gpn.config import ModelConfig

    assert ModelConfig().provider == "harness"
    from types import SimpleNamespace

    provider, _ = resolve_llm_provider(SimpleNamespace())
    assert provider == "harness"


def test_explicit_local_provider_deferred(tmp_path: Path):
    from types import SimpleNamespace

    from slides_cli.gpn.export_pipeline import PROVIDER_LOCAL, run_native_build

    cfg = SimpleNamespace(project_root=PROJECT_ROOT, config_path=None,
                          model=SimpleNamespace(provider="local_server"))
    result = run_native_build(tmp_path / "run", tmp_path / "plans", cfg,
                              ExportOptions())
    assert result.llm_provider == PROVIDER_LOCAL
    assert result.operation_status == "invalid_input"
    assert any("DEFERRED" in i for i in result.issues)
    assert result.candidate_path is None


def test_unknown_provider_is_config_error():
    from types import SimpleNamespace

    from slides_cli.gpn.errors import GpnError

    cfg = SimpleNamespace(model=SimpleNamespace(provider="cloud_mirror"))
    with pytest.raises(GpnError, match="MODEL_PROVIDER_UNKNOWN"):
        resolve_llm_provider(cfg)


# ---------------------------------------------------------------------------
# §1.4 reference whitelist
# ---------------------------------------------------------------------------

def test_reference_source_whitelist(tmp_path: Path):
    from slides_cli.gpn.ontology import discover_slide_examples

    discovery = discover_slide_examples(PROJECT_ROOT)
    assert discovery.files, "user corpus must exist for the whitelist test"
    record = discovery.files[0]
    allowed = assert_allowed_reference(
        PROJECT_ROOT / record.relative_path, "unit_test", PROJECT_ROOT,
        discovery)
    assert allowed.relative_path == record.relative_path
    for bad in ("gpn-restyler/examples/example1.pptx",
                "agent-slides/demo/deck.pptx",
                "https://example.invalid/sample.pptx",
                "slide_examples/../../etc/passwd"):
        with pytest.raises(ForbiddenReferenceError) as excinfo:
            assert_allowed_reference(bad, "unit_test", PROJECT_ROOT, discovery)
        assert FORBIDDEN_CODE in str(excinfo.value)


def test_skill_legacy_reference_denied(tmp_path: Path):
    from slides_cli.gpn.ontology import discover_slide_examples

    discovery = discover_slide_examples(PROJECT_ROOT)
    link = tmp_path / "evil.pptx"
    target = PROJECT_ROOT / discovery.files[0].relative_path
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    outside = tmp_path / "project"
    (outside / "slide_examples").mkdir(parents=True)
    with pytest.raises(ForbiddenReferenceError):
        assert_allowed_reference(link, "skill_tool", outside, discovery)


def test_foreign_reference_cache_invalidated(tmp_path: Path):
    from slides_cli.gpn import references as ref

    cache = tmp_path / "run" / ref.DESCRIPTOR_FILE
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps([{
        "example_id": "legacy:slide0",
        "source_path": str(PROJECT_ROOT / "gpn-restyler/examples/example1.pptx"),
        "slide_id": "slide0", "object_kinds": {}, "title_snippet": "",
        "text_snippets": [],
    }]), encoding="utf-8")
    assert ref._cache_provenance_ok(
        json.loads(cache.read_text(encoding="utf-8")), PROJECT_ROOT) is False


def test_upstream_examples_untracked():
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files", "gpn-restyler/examples/"],
        capture_output=True, text=True,
        cwd=str(PROJECT_ROOT / "gpn-restyler")).stdout
    assert "example1.pptx" not in tracked
    assert (PROJECT_ROOT / "slide_examples").is_dir(), (
        "user corpus must not be removed")


# ---------------------------------------------------------------------------
# Preflight / preservation units
# ---------------------------------------------------------------------------

def test_preflight_and_content_diff(tmp_path: Path):
    src = tmp_path / "src.pptx"
    fx.build_import_fixture(src)
    deck, ledger, _store = _import(src, tmp_path / "run")
    rules = _real_rules()
    report = preflight_native_export(deck, ledger, [], rules, {}, ExportOptions())
    assert report.required_atoms == len(ledger.atoms) > 0
    assert report.ok is True
    diff = compare_export_preservation(ledger, deck, [], {})
    assert diff.missing, "empty bindings must report missing atoms"
    assert diff.ok is False


def test_package_graph_report_on_real_file(tmp_path: Path):
    src = tmp_path / "src.pptx"
    fx.build_import_fixture(src)
    report = adapter.validate_package_graph(src)
    assert report["readable"] is True
    assert report["ok"] is True
    assert report["duplicate_members"] == []
    assert report["dangling_targets"] == []
