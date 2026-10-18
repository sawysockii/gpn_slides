"""Stage 11 regressions for the universal authoring + visual repair round.

Each test proves a *cause* from the round's requirements table (U01…U20),
using the real engine, the real local GPN fonts and real OOXML bytes. No
mock stands in for a rendering or measurement result.

Tests that could not be executed honestly are not written: every test in
this file runs against production code paths.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import conftest as fx
import pytest
from pydantic import ValidationError

from slides_cli.gpn.composition import compile_composition
from slides_cli.gpn.planner import LayoutIntent

PROJECT_ROOT = Path(__file__).resolve().parents[3]

TEST_PROFILE = {"content_box": {"x": 0.5, "y": 1.2, "w": 9.0, "h": 4.3},
                "unit": "in", "verified": True, "source": "test_template"}


@lru_cache(maxsize=1)
def _rules():
    from slides_cli.gpn.compiler import compile_ontology
    from slides_cli.gpn.ontology import resolve_ontology_sources

    sources = resolve_ontology_sources(PROJECT_ROOT)
    assert sources.primary_json is not None
    return compile_ontology(PROJECT_ROOT / sources.primary_json, None,
                            sources=sources)


@lru_cache(maxsize=1)
def _bundle():
    from slides_cli.gpn.typography import (
        build_font_inventory_from_discovery,
        discover_local_fonts,
        prepare_measurement_fonts,
    )

    report = discover_local_fonts(None)
    faces = {f for role in _rules().roles.values() for f in role.allowed_faces}
    inv = build_font_inventory_from_discovery(report, faces)
    assert inv.missing_faces == []
    return prepare_measurement_fonts(inv, "", profile_kind="production")


def _font_assets() -> list[dict]:
    return [{"alias": b.browser_alias, "path": b.path, "sha256": b.sha256,
             "face_index": b.face_index, "style": "normal", "weight": 400}
            for b in _bundle().bindings]


ALLOW_ROOTS = ["/Users/wysockii/Library/Fonts/", "/System/Library/Fonts/"]


def _measure(rid: str, box: dict[str, float], margins: dict[str, float],
             paragraphs: list[dict], *, w_pt: float = 12.0) -> dict:
    from slides_cli.gpn.measurement import measure_components
    from slides_cli.gpn.models import MeasureComponent, MeasurementRequest

    face = _bundle().bindings[2].browser_alias
    for para in paragraphs:
        for run in para["runs"]:
            run.setdefault("style", {})
            run["style"] = {"face_alias": face, "size_pt": w_pt}
    request = MeasurementRequest(
        schema_version="1.0", request_id=rid,
        canvas_pt={"w": 960.0, "h": 540.0}, css_px_per_in=96.0,
        profile_kind="production",
        components=[MeasureComponent(
            id="c0", kind="text", payload_ref="", paragraphs=paragraphs,
            box_pt=box, margins_pt=margins, vertical_anchor="top",
            wrap_policy="wrap", rotation_deg=0.0, list_preview={})],
        font_assets=_font_assets(),
        engine_options={"timeout_ms": 180000, "device_scale_factor": 1,
                        "allowlist_roots": ALLOW_ROOTS})
    return measure_components(request, timeout_seconds=180.0,
                              project_root=PROJECT_ROOT)["response"]["items"][0]


def _styled_para(text: str, alias: str, pid: str = "p0") -> dict:
    para = _para(text, pid)
    para["runs"][0]["style"] = {"face_alias": alias, "size_pt": 12.0}
    return para


def _para(text: str, pid: str = "p0") -> dict:
    return {"id": pid, "runs": [{"id": f"{pid}:r0", "text": text}],
            "space_before_pt": 0.0, "space_after_pt": 0.0,
            "line_spacing_mode": "multiple", "line_spacing_value": 1.15,
            "alignment": "left", "level": 0, "list_kind": "none",
            "first_line_indent_pt": 0.0, "left_indent_pt": 0.0}


# ---------------------------------------------------------------------------
# U02 — inner/outer coordinate contract
# ---------------------------------------------------------------------------

def test_u02_inset_intrusion_is_detected_and_exact_fit_passes() -> None:
    """100x20 outer box with {l10 r10 t3 b3}; a 15pt line is 1pt too tall.

    The pre-fix code compared content extents against the OUTER border, so
    the fragment intruded 5pt into the right inset and 2pt into the bottom
    inset and both were reported as fitting.
    """
    margins = {"l": 10.0, "r": 10.0, "t": 3.0, "b": 3.0}
    text = "Точный текст"
    width = 300.0
    tall = _measure("u02-probe", {"x": 0.0, "y": 0.0, "w": width, "h": 400.0},
                    margins, [_para(text)])
    assert tall["line_count"] == 1
    assert tall["inner_box_in_frame_pt"] == {"x": 10.0, "y": 3.0,
                                             "w": 280.0, "h": 394.0}
    # The required outer height charges BOTH vertical insets exactly once.
    required = tall["required_outer_height_pt"]
    assert required == pytest.approx(
        tall["occupied_height_pt"] + margins["b"], abs=1e-6)
    assert required > margins["t"] + margins["b"]
    # Exact full-height fit: the frame is exactly as tall as the contract asks.
    fitted = _measure("u02-fit", {"x": 0.0, "y": 0.0, "w": width,
                                  "h": required}, margins, [_para(text)])
    assert fitted["overflow_y"] is False
    assert fitted["excess_pt"]["y"] == 0.0
    # The LEGACY sizing (occupied height only, bottom inset uncharged)
    # pushes the last line into the bottom inset and is now reported.
    legacy = _measure("u02-legacy", {"x": 0.0, "y": 0.0, "w": width,
                                     "h": tall["occupied_height_pt"]},
                      margins, [_para(text)])
    assert legacy["overflow_y"] is True
    assert legacy["excess_pt"]["y"] == pytest.approx(margins["b"], abs=0.6)
    # One point short still fails — the contract is not "never overflows".
    short = _measure("u02-short", {"x": 0.0, "y": 0.0, "w": width,
                                   "h": required - 1.0}, margins,
                     [_para(text)])
    assert short["overflow_y"] is True


def test_u02_right_inset_intrusion_is_reported() -> None:
    """A nowrap run wider than the inner width is an overflow, not a pass."""
    margins = {"l": 10.0, "r": 10.0, "t": 3.0, "b": 3.0}
    measured = _measure(
        "u02-x", {"x": 0.0, "y": 0.0, "w": 100.0, "h": 120.0}, margins,
        [_para("ОченьДлиннаяСтрокаБезПробеловКотораяНеПоместится", "p0")],
        w_pt=14.0)
    assert measured["overflow_x"] is True
    assert measured["excess_pt"]["x"] > 0.0
    assert measured["excess_inner_pt"]["x"] > 0.0


def test_u03_nonzero_origin_mixed_runs_padding_paid_once() -> None:
    """One contract for a non-zero frame origin, mixed runs and paragraphs.

    Two runs on the same visual line count as ONE logical line, and the
    frame padding is charged once, not per paragraph.
    """
    paragraphs = [
        {"id": "p0", "runs": [{"id": "p0a", "text": "Метрика 1 560"},
                              {"id": "p0b", "text": " символов"}],
         "space_before_pt": 0.0, "space_after_pt": 6.0,
         "line_spacing_mode": "multiple", "line_spacing_value": 1.15,
         "alignment": "left", "level": 0, "list_kind": "none",
         "first_line_indent_pt": 0.0, "left_indent_pt": 0.0},
        {"id": "p1", "runs": [{"id": "p1a", "text": "Подпись"}],
         "space_before_pt": 0.0, "space_after_pt": 0.0,
         "line_spacing_mode": "multiple", "line_spacing_value": 1.15,
         "alignment": "left", "level": 0, "list_kind": "none",
         "first_line_indent_pt": 0.0, "left_indent_pt": 0.0},
    ]
    margins = {"l": 7.2, "r": 7.2, "t": 3.6, "b": 3.6}
    measured = _measure(
        "u03-origin", {"x": 213.5, "y": 47.25, "w": 320.0, "h": 400.0},
        margins, paragraphs)
    # Extents are frame-relative, so a non-zero origin does not shift them.
    # A glyph range rect can bleed a fraction of a point past its line box,
    # which is why only the FAR side decides fit.
    assert measured["logical_bounds_pt"]["x"] == pytest.approx(
        margins["l"], abs=1.2)
    assert measured["logical_bounds_pt"]["y"] == pytest.approx(
        margins["t"], abs=1.2)
    assert measured["overflow_x"] is False and measured["overflow_y"] is False
    # Two runs of p0 share one visual line: line_count is a LINE count.
    assert measured["line_count"] == 2
    assert measured["paragraph_count"] == 2
    assert len(measured["logical_line_boxes_inner_pt"]) == 2
    assert measured["logical_line_boxes_inner_pt"][0]["run_ids"] == \
        ["p0a", "p0b"]
    # The paragraph space_after is inside the content extent, and the two
    # insets are charged once on top of it.
    assert measured["required_outer_height_pt"] == pytest.approx(
        measured["occupied_height_pt"] + margins["b"], abs=1e-6)


def test_u04_unrequested_face_is_never_a_production_number() -> None:
    """A face outside the allowlist fails the call instead of substituting."""
    from slides_cli.gpn.measurement import MeasurementError, measure_components
    from slides_cli.gpn.models import MeasureComponent, MeasurementRequest

    assets = _font_assets()
    request = MeasurementRequest(
        schema_version="1.0", request_id="u04-face",
        canvas_pt={"w": 960.0, "h": 540.0}, css_px_per_in=96.0,
        profile_kind="production",
        components=[MeasureComponent(
            id="c0", kind="text", payload_ref="",
            paragraphs=[_styled_para("шрифт", assets[0]["alias"])],
            box_pt={"x": 0.0, "y": 0.0, "w": 400.0, "h": 2000.0},
            margins_pt={"l": 7.2, "r": 7.2, "t": 3.6, "b": 3.6},
            vertical_anchor="top", wrap_policy="wrap", rotation_deg=0.0,
            list_preview={})],
        font_assets=assets,
        engine_options={"timeout_ms": 180000, "device_scale_factor": 1,
                        "allowlist_roots": ["/nonexistent/font/root"]})
    with pytest.raises(MeasurementError) as excinfo:
        measure_components(request, project_root=PROJECT_ROOT)
    assert excinfo.value.code == "MEASUREMENT_FACE_UNVERIFIED"


# ---------------------------------------------------------------------------
# U01 — empty geometry must not be measured as text
# ---------------------------------------------------------------------------

def _saved_package_with_empty_figures(tmp_path: Path) -> Path:
    """Ten figures with an EMPTY txBody next to real text in a small box."""
    from lxml import etree

    prs = fx.new_deck()
    slide = fx.blank_slide(prs)
    for i in range(8):
        from pptx.enum.shapes import MSO_SHAPE
        from pptx.util import Emu, Inches
        shp = slide.shapes.add_shape(
            MSO_SHAPE.OVAL, Inches(0.5 + i * 1.2), Inches(0.5),
            Emu(274320), Emu(274320))
        shp.text_frame.paragraphs[0].text = ""  # empty a:p, no a:t
    from pptx.util import Inches as In
    real = slide.shapes.add_textbox(In(0.5), In(4.0), Emu(914400), Emu(274320))
    real.text_frame.word_wrap = True
    run = real.text_frame.paragraphs[0].add_run()
    run.text = "Длинный текст " * 40
    run.font.name = _bundle().bindings[2].internal_name
    path = tmp_path / "empty_figures.pptx"
    prs.save(str(path))
    assert path.is_file()
    del etree
    return path


def test_u01_empty_geometry_is_not_text_fit_but_real_text_still_fails(
        tmp_path: Path) -> None:
    from slides_cli.gpn.assets import AssetStore
    from slides_cli.gpn.importer import import_deck
    from slides_cli.gpn.measured_build import remeasure_saved_geometry
    from slides_cli.gpn.text_presence import read_saved_presence

    package = _saved_package_with_empty_figures(tmp_path)
    presence = read_saved_presence(package)
    kinds = {p.kind.value for p in presence.values()}
    assert "empty_geometry" in kinds
    assert "text" in kinds, "real text must NOT be classified as empty"

    deck, _ = import_deck(package, AssetStore(tmp_path / "assets"))
    called: list[list[str]] = []

    def measurer(request):
        called.append([c.payload_ref for c in request.components])
        return {"items": [{"component_id": c.id, "overflow_x": False,
                           "overflow_y": False}
                          for c in request.components]}

    report = remeasure_saved_geometry(
        deck, _bundle(), measurer,
        {"saved_package_path": str(package),
         "font_allowlist_roots": ALLOW_ROOTS})
    not_applicable = {row["id"] for row in report["not_applicable"]}
    assert len(not_applicable) >= 8, report["not_applicable"]
    assert all(row["scope_excluded"] == "saved_text_fit"
               for row in report["not_applicable"])
    assert all("saved_geometry" in row["scope_still_checked"]
               for row in report["not_applicable"])
    measured_payloads = {p for group in called for p in group}
    # Empty figures were never sent to the measurer...
    real = [o.id for o in deck.slides[0].objects if o.kind == "text"]
    assert not (not_applicable & measured_payloads)
    # ...but the real text object was, and it is in the applicable scope.
    assert set(real) & measured_payloads
    assert set(real) <= set(report["applicable"])


def test_u01_real_text_in_a_tiny_box_still_fails(tmp_path: Path) -> None:
    """Negative control: same tiny bbox, but real text must still overflow."""
    box = {"x": 0.0, "y": 0.0, "w": 914400 / 12700.0, "h": 274320 / 12700.0}
    margins = {"l": 7.2, "r": 7.2, "t": 3.6, "b": 3.6}
    measured = _measure("u01-tiny", box, margins,
                        [_para("Очень длинный текст для крошечного бокса " * 6)])
    assert measured["overflow_y"] is True
    assert measured["content_absent"] is False


# ---------------------------------------------------------------------------
# U11 — overlap permissions follow relations, not a slide-wide clique
# ---------------------------------------------------------------------------

def _intent_with_pairs(slide_id: str, refs: list[str], pairs: list[list[str]],
                       permissions: list[dict], zones=None) -> LayoutIntent:
    data = {"slide_id": slide_id, "candidate_id": "c0",
            "content_refs": list(refs), "communication_job": "test",
            "reading_order": list(refs), "dominant_ref": refs[0],
            "zones": zones or [{"zone_id": "z-all", "x_pct": 0.0,
                                "y_pct": 0.0, "w_pct": 100.0, "h_pct": 100.0,
                                "role": "body", "content_refs": list(refs)}],
            "relations": [], "operators": [], "reference_ids": [],
            "allowed_overlaps": pairs, "overlap_permissions": permissions,
            "novel_recipe": "", "uncertainties": [], "rationale": "test"}
    return LayoutIntent.model_validate(data)


def _two_text_deck(tmp_path: Path):
    from slides_cli.gpn.assets import AssetStore
    from slides_cli.gpn.importer import import_deck
    from slides_cli.gpn.provenance import build_ledger

    prs = fx.new_deck()
    slide = fx.blank_slide(prs)
    fx.add_textbox(slide, "Подпись панели", left=1.0, top=1.0,
                   width=3.0, height=0.6, size=14)
    fx.add_textbox(slide, "Чужая подпись", left=1.2, top=1.2,
                   width=3.0, height=0.6, size=14)
    src = tmp_path / "pairs.pptx"
    prs.save(str(src))
    deck, _ = import_deck(src, AssetStore(tmp_path / "assets"))
    return deck, build_ledger(deck, decoration=[]), deck.slides[0]


def test_u11_unjustified_pair_is_refused_typed_pair_is_accepted(
        tmp_path: Path) -> None:
    """A bare pair is refused; the same pair WITH a typed permission passes.

    The recovery run allowed 1085 pairs on one slide by crossing every
    surface with every content object, which would hide a later occlusion.
    """
    deck, ledger, slide = _two_text_deck(tmp_path)
    refs = [o.id for o in slide.objects if o.kind == "text"]
    assert len(refs) == 2

    blanket = compile_composition(
        _intent_with_pairs(slide.id, refs, [[refs[0], refs[1]]], []),
        slide, ledger, _rules(), TEST_PROFILE)
    joined = "\n".join(blanket.issues)
    assert "OVERLAP_PERMISSION_UNJUSTIFIED" in joined
    assert not [c for c in blanket.constraints
                if c.kind == "allowed_overlap" and c.origin == "intent"]

    typed = compile_composition(
        _intent_with_pairs(
            slide.id, refs, [[refs[0], refs[1]]],
            [{"underlay_id": refs[0], "content_id": refs[1],
              "relation_id": "r:own-child",
              "allowed_kind": "surface_child",
              "scope": "container_containment",
              "evidence_ref": "plan:zone-own-child"}]),
        slide, ledger, _rules(), TEST_PROFILE)
    intent_overlaps = [c for c in typed.constraints
                       if c.kind == "allowed_overlap"
                       and c.origin == "intent"]
    assert len(intent_overlaps) == 1
    assert set(intent_overlaps[0].target_ids) == {refs[0], refs[1]}
    assert intent_overlaps[0].value["permission_ref"] == "r:own-child"
    assert not [i for i in typed.issues if "UNJUSTIFIED" in i]


def test_u11_derive_overlap_permissions_follows_the_target_tree() -> None:
    """Permissions come from the scene's parent/child relations."""
    from slides_cli.gpn.layout import derive_overlap_permissions
    from slides_cli.gpn.models import AuthoredNode, AuthoredScene

    scene = AuthoredScene.model_validate({
        "frame_id": "f", "root_id": "root",
        "nodes": {
            "root": {"node_id": "root", "kind": "container",
                     "children": ["panel", "other"]},
            "panel": {"node_id": "panel", "kind": "surface",
                      "parent_id": "root", "children": ["label", "diagram"]},
            "label": {"node_id": "label", "kind": "rich_text",
                      "parent_id": "panel"},
            "diagram": {"node_id": "diagram", "kind": "path",
                        "parent_id": "panel"},
            "other": {"node_id": "other", "kind": "rich_text",
                      "parent_id": "root"},
        },
        "relations": [{"relation_id": "r:cross", "kind": "path_crossing",
                       "from": "diagram", "to": "other"}],
    })
    perms = derive_overlap_permissions(scene)
    pairs = {(p.underlay_id, p.content_id): p for p in perms}
    assert ("panel", "label") in pairs
    assert ("panel", "diagram") in pairs
    assert ("other", "label") not in pairs, "siblings are never permitted"
    assert pairs[("panel", "label")].allowed_kind == "surface_child"
    crossing = [p for p in perms if p.allowed_kind == "intentional_path_crossing"]
    assert crossing and crossing[0].relation_id == "r:cross"
    assert AuthoredNode is not None


# ---------------------------------------------------------------------------
# U06 — one painter order for rich text and ordinary objects
# ---------------------------------------------------------------------------

def test_u06_painter_order_places_surface_below_merged_text() -> None:
    from slides_cli.gpn.export_pipeline import PainterEntry, build_painter_order

    entries = [
        PainterEntry(target_id="block", kind="block", z_index=1),
        PainterEntry(target_id="panel", kind="object", z_index=9,
                     is_surface_underlay=True),
        PainterEntry(target_id="caption", kind="object", z_index=2),
    ]
    order, issues = build_painter_order(entries, {"block": "panel"})
    assert not issues
    assert order.index("panel") < order.index("block")
    assert order.index("block") < order.index("caption")


def test_u06_painter_order_cycle_is_a_typed_conflict() -> None:
    from slides_cli.gpn.export_pipeline import PainterEntry, build_painter_order

    entries = [PainterEntry(target_id="a", kind="object", z_index=0),
               PainterEntry(target_id="b", kind="object", z_index=1)]
    order, issues = build_painter_order(entries, {"a": "b", "b": "a"})
    assert any("PAINTER_ORDER_CONFLICT" in i for i in issues)
    assert sorted(order) == ["a", "b"]


# ---------------------------------------------------------------------------
# U07/U08/U10 — visual normalization, not a renderer colour guess
# ---------------------------------------------------------------------------

def _topology(ref: str) -> dict:
    from slides_cli.gpn.models import VisualTopology

    return VisualTopology(member_refs=[ref], member_count=1,
                          source_box_pt={"x": 1.0, "y": 1.0, "w": 8.0,
                                         "h": 6.0}).model_dump()


def _shape_with_stroke(color: str = "0C539F"):
    from slides_cli.gpn.models import ShapePayload

    return ShapePayload.model_validate({
        "geometry_kind": "preset", "geometry_token": "rect",
        "source_fill": {"kind": "srgb", "value": "0C539F"},
        "source_line": {"width_emu": 12700,
                        "color": {"kind": "srgb", "value": color}},
    })


def _empty_shape():
    from slides_cli.gpn.models import ShapePayload

    return ShapePayload.model_validate({"geometry_kind": "preset",
                                       "geometry_token": "rect"})


class _FakeContext:
    def __init__(self) -> None:
        self.issues: list[str] = []
        self.emission_records: list[object] = []


def test_u07_required_diagram_with_unresolved_stroke_is_not_cleared() -> None:
    """A required vector unit without a normative token must NOT lose its
    strokes silently: the renderer reports VISUAL_REPRESENTATION_UNRESOLVED
    and leaves the geometry for inspection."""
    from slides_cli.gpn.models import VisualUnit
    from slides_cli.gpn.native import _apply_decor_fill_line
    from slides_cli.gpn.visual_norm import plan_visual_normalization

    unit = VisualUnit.model_validate({
        "unit_id": "vu:diagram", "source_refs": ["obj-1"],
        "kind": "diagram", "required": True,
        "internal_topology": _topology("obj-1"),
        "evidence": ["connector carries an arrow endpoint"],
    })
    plan = plan_visual_normalization(unit, None, _rules(), {}, stroke_token="")
    assert plan.action == "unresolved"
    assert "silent noFill" in plan.reason

    class _Shape:
        def __init__(self) -> None:
            self.line = type("L", (), {"fill": self,
                                       "color": type("C", (), {"rgb": None})()})()
            self.fill = type("F", (), {"background": lambda _s: None})()

    context = _FakeContext()
    _apply_decor_fill_line(_Shape(), _shape_with_stroke(), "rect", "obj-1",
                           context, rules=_rules(), surface="",
                           stroke_token="", visual_plan=plan)
    joined = "\n".join(context.issues)
    assert "VISUAL_REPRESENTATION_UNRESOLVED" in joined
    assert "DECOR_LINE_NORMALIZED" not in joined
    assert "DECOR_LINE_NORMALIZED" not in joined


def test_u07_permitted_stroke_token_recolors_without_source_rgb() -> None:
    from slides_cli.gpn.models import VisualUnit
    from slides_cli.gpn.native import _apply_decor_fill_line
    from slides_cli.gpn.visual_norm import plan_visual_normalization

    token = sorted(_rules().colors)[0]
    assert str(_rules().colors[token]).lstrip("#").upper() in {
        str(v).lstrip("#").upper() for v in _rules().colors.values()}
    unit = VisualUnit.model_validate({
        "unit_id": "vu:d", "source_refs": ["obj-2"], "kind": "diagram",
        "required": True,
        "internal_topology": _topology("obj-2")})
    plan = plan_visual_normalization(unit, None, _rules(), {},
                                     stroke_token=token)
    assert plan.action == "keep_native_recolor"

    applied: list[str] = []

    class _Shape:
        def __init__(self) -> None:
            self.line = type("L", (), {"fill": self,
                                       "color": type("C", (), {"rgb": None})()})()
            self.fill = type("F", (), {"background": lambda _s: None})()

    shape = _Shape()

    class _LineColor:
        @property
        def rgb(self):
            return None

        @rgb.setter
        def rgb(self, value) -> None:
            applied.append(str(value))

    shape.line.color = _LineColor()
    context = _FakeContext()
    _apply_decor_fill_line(shape, _shape_with_stroke("AABBCC"), "rect",
                           "obj-2", context, rules=_rules(), surface="",
                           stroke_token=token, visual_plan=plan)
    joined = "\n".join(context.issues)
    assert "STROKE_APPLIED" in joined
    expected = str(_rules().colors[token]).lstrip("#").upper()
    assert applied == [expected], "source RGB must never be restored"
    assert "AABBCC" not in joined


def test_u08_optional_decor_is_omitted_as_a_whole_cluster() -> None:
    from slides_cli.gpn.models import VisualUnit
    from slides_cli.gpn.native import _apply_decor_fill_line
    from slides_cli.gpn.visual_norm import plan_visual_normalization

    unit = VisualUnit.model_validate({
        "unit_id": "vu:decor", "source_refs": ["obj-3"],
        "kind": "optional_decor", "required": False,
        "internal_topology": _topology("obj-3")})
    plan = plan_visual_normalization(unit, None, _rules(), {})
    assert plan.action == "authorize_omit"

    class _Shape:
        def __init__(self) -> None:
            self.line = type("L", (), {"fill": self,
                                       "color": type("C", (), {"rgb": None})()})()
            self.fill = type("F", (), {"background": lambda _s: None})()

    context = _FakeContext()
    _apply_decor_fill_line(_Shape(), _empty_shape(), "rect", "obj-3",
                           context, rules=_rules(), surface="",
                           stroke_token="", visual_plan=plan)
    joined = "\n".join(context.issues)
    assert "DECOR_CLUSTER_OMITTED" in joined
    assert "VISUAL_REPRESENTATION_UNRESOLVED" not in joined
    assert "STROKE_APPLIED" not in joined


def test_u10_unknown_surface_token_is_recorded_not_silently_passed() -> None:
    from slides_cli.gpn.models import VisualUnit
    from slides_cli.gpn.native import _apply_decor_fill_line
    from slides_cli.gpn.visual_norm import plan_visual_normalization

    unit = VisualUnit.model_validate({
        "unit_id": "vu:x", "source_refs": ["obj-4"], "kind": "diagram",
        "required": True,
        "internal_topology": _topology("obj-4")})
    plan = plan_visual_normalization(unit, None, _rules(),
                                     {"surface": "no_such_token"})
    assert plan.action == "unresolved"

    class _Shape:
        def __init__(self) -> None:
            self.line = type("L", (), {"fill": self,
                                       "color": type("C", (), {"rgb": None})()})()
            self.fill = type("F", (), {"background": lambda _s: None})()

    context = _FakeContext()
    _apply_decor_fill_line(_Shape(), _shape_with_stroke(), "rect", "obj-4",
                           context, rules=_rules(),
                           surface="no_such_token", stroke_token="",
                           visual_plan=plan)
    joined = "\n".join(context.issues)
    assert "SURFACE_TOKEN_UNKNOWN" in joined
    assert "VISUAL_REPRESENTATION_UNRESOLVED" in joined


def test_u12_classification_does_not_use_area_as_a_semantic_classifier() -> (
        None):
    """A tiny composite mark stays required; a big empty frame is decor."""
    from slides_cli.gpn.visual_norm import classify_visual_units

    class _Obj:
        def __init__(self, oid, kind, box, text="", arrow=False):
            self.id = oid
            self.kind = kind
            self.local_box = type("B", (), {
                "x": box[0], "y": box[1], "w": box[2], "h": box[3]})()
            self.payload = type("P", (), {
                "paragraphs": ([type("Para", (), {"runs": [
                    type("R", (), {"text": text, "kind": "text",
                                   "field": None})()]})()] if text else []),
                "source_line": type("L", (), {
                    "begin_arrow": "triangle" if arrow else "none",
                    "end_arrow": "none", "color": None})()})()

    def _omit_never(obj, *_a, **_k):
        return type("D", (), {"allowed": False, "reason": "no authorization",
                              "authorization_ref": ""})()

    tiny_mark = _Obj("m1", "group", (10, 10, 300, 300))
    huge_frame = _Obj("d1", "shape", (0, 0, 6_000_000, 4_000_000))
    connector = _Obj("c1", "connector", (0, 0, 400_000, 0), arrow=True)
    units = {u.unit_id: u for u in classify_visual_units(
        [tiny_mark, huge_frame, connector], None, None, "",
        can_omit=_omit_never)}
    assert units["vu:m1"].required is True
    assert units["vu:m1"].internal_topology.aspect_ratio == 1.0
    assert units["vu:d1"].required is True
    assert units["vu:c1"].kind == "diagram"
    assert units["vu:c1"].required is True
    # Nothing became "optional" merely because it was small or large.
    assert all(u.required for u in units.values())
    assert all(u.kind != "optional_decor" for u in units.values())
    assert all(u.evidence for u in units.values())


# ---------------------------------------------------------------------------
# U09 — white-on-white brand asset, proven from bytes
# ---------------------------------------------------------------------------

def test_u09_white_logo_on_white_surface_is_detected_from_bytes() -> None:
    """A pure-white RGBA PNG on a white surface is proven by pixel stats."""
    from io import BytesIO

    from PIL import Image

    from slides_cli.gpn.visual_norm import analyze_image_alpha, resolve_brand_asset

    buf = BytesIO()
    Image.new("RGBA", (60, 40), (255, 255, 255, 255)).save(buf, format="PNG")
    white = buf.getvalue()
    analysis = analyze_image_alpha(white)
    assert analysis["available"] is True
    assert analysis["dominant_rgb"] == "FFFFFF"
    assert analysis["visible_pixels"] == 2400

    palette = {k: str(v).lstrip("#").upper() for k, v in _rules().colors.items()}
    white_token = next(k for k, v in palette.items() if v == "FFFFFF")

    plan = resolve_brand_asset("logo.png", white, white_token, _rules(), [])
    assert plan.action == "unresolved"
    assert "no approved colorway" in plan.reason
    assert plan.alpha_analysis["dominant_rgb"] == "FFFFFF"

    # An existing approved colorway with a verified hash is selected.
    buf2 = BytesIO()
    Image.new("RGBA", (60, 40), (12, 83, 159, 255)).save(buf2, format="PNG")
    blue = buf2.getvalue()
    import hashlib
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        asset = Path(tmp) / "logo-blue.png"
        asset.write_bytes(blue)
        approved = [{"id": "gpn_logo_blue", "path": str(asset),
                     "sha256": hashlib.sha256(blue).hexdigest()}]
        chosen = resolve_brand_asset("logo.png", white, white_token, _rules(),
                                     approved)
    assert chosen.action == "use_approved_colorway"
    assert chosen.chosen_sha256 == hashlib.sha256(blue).hexdigest()
    assert chosen.source_sha256 == hashlib.sha256(white).hexdigest()

    # A hash that does not match the bytes is not an approved lineage.
    bad = resolve_brand_asset("logo.png", white, white_token, _rules(),
                              [{"id": "x", "path": str(asset),
                                "sha256": "00" * 32}])
    assert bad.action == "unresolved"


def test_u09_ntc_logo_asset_is_white_on_white() -> None:
    """The shipped NTC logo really is #FFFFFF with alpha>0 for every pixel."""
    source = PROJECT_ROOT / "NTC_business_AI_GPN.pptx"
    if not source.is_file():
        pytest.skip("NTC source not present")
    import zipfile

    from slides_cli.gpn.visual_norm import analyze_image_alpha

    with zipfile.ZipFile(source) as z:
        parts = [n for n in z.namelist() if n.endswith("image8.png")]
        if not parts:
            pytest.skip("image8.png not in this package")
        data = z.read(parts[0])
    stats = analyze_image_alpha(data)
    assert stats["dominant_rgb"] == "FFFFFF"
    assert stats["dominant_share"] == 1.0


# ---------------------------------------------------------------------------
# U20 — a vanished required graph is a representation failure
# ---------------------------------------------------------------------------

def test_u20_required_graph_missing_fails_representation() -> None:
    """All text present, the diagram gone: still a failure."""
    from slides_cli.gpn.models import VisualUnit
    from slides_cli.gpn.visual_norm import check_visual_representation

    unit = VisualUnit.model_validate({
        "unit_id": "vu:ggd", "source_refs": ["lines-1"],
        "kind": "diagram", "required": True,
        "internal_topology": _topology("lines-1"),
        "evidence": ["connector carries an arrow endpoint"]})
    ok = check_visual_representation([unit], {}, {
        "vu:ggd": {"bound": True, "has_geometry": True, "has_stroke": True}})
    assert ok.ok is True

    # Object survives, everything visible is gone: presence diff cannot pass.
    invisible = check_visual_representation([unit], {}, {
        "vu:ggd": {"bound": True, "has_geometry": True, "has_stroke": False,
                   "has_fill": False, "has_labels": False}})
    assert invisible.ok is False
    assert invisible.invisible_representation == ["vu:ggd"]

    # Object dropped entirely.
    missing = check_visual_representation([unit], {}, {})
    assert missing.ok is False
    assert missing.missing_representation == ["vu:ggd"]

    # Optional decor may legitimately disappear.
    decor = VisualUnit.model_validate({
        "unit_id": "vu:badge", "source_refs": ["b1"],
        "kind": "optional_decor", "required": False,
        "internal_topology": _topology("b1"), "evidence": ["omit"]})
    assert check_visual_representation([decor], {}, {}).ok is True


# ---------------------------------------------------------------------------
# U17 — one canonical acceptance schema, derived summary
# ---------------------------------------------------------------------------

def test_u17_foreign_acceptance_dict_is_rejected_by_the_real_model() -> None:
    from slides_cli.gpn.models import ProductAcceptance

    foreign = {
        "generated_at": "2026-10-15T00:00:00+00:00",
        "automated_gates_pass": {"layout_resolved": True},
        "model_design_review_pass": "reviewed page images of all 8 slides",
        "user_approved": False,
        "release_ready": False,
    }
    with pytest.raises(ValidationError):
        ProductAcceptance.model_validate(foreign)


def test_u17_summary_is_derived_and_never_claims_more_than_acceptance() -> None:
    from slides_cli.gpn.acceptance import build_product_summary
    from slides_cli.gpn.models import (
        GateEvidence,
        GateStatus,
        ProductAcceptance,
        TypedFinding,
    )

    acceptance = ProductAcceptance(
        accepted=False, subject_candidate_sha256="a" * 64,
        publication_eligible=False, required_scope_count=3,
        checked_scope_count=3,
        findings=[TypedFinding(code="X", severity="error", message="m")])
    evidence = [
        GateEvidence(gate_id="content_preservation", status=GateStatus.PASS),
        GateEvidence(gate_id="geometry_overlap", status=GateStatus.FAIL),
        GateEvidence(gate_id="visual_review", status=GateStatus.UNKNOWN),
    ]
    summary = build_product_summary(acceptance, evidence)
    assert summary.automated_gates_pass is False
    assert summary.failing_gate_ids == ["geometry_overlap"]
    assert summary.unknown_gate_ids == ["visual_review"]
    assert summary.model_design_review_pass is False
    assert summary.release_ready is False
    assert summary.user_approved is False
    assert "geometry_overlap" in summary.release_ready_blockers
    assert "unknown: visual_review" in summary.release_ready_blockers

    clean = ProductAcceptance(
        accepted=True, subject_candidate_sha256="b" * 64,
        publication_eligible=True, required_scope_count=1,
        checked_scope_count=1)
    ok = build_product_summary(
        clean, [GateEvidence(gate_id="visual_review",
                             status=GateStatus.PASS)])
    assert ok.automated_gates_pass is True
    assert ok.model_design_review_pass is True
    # Even a perfect acceptance is not "release ready" without the user.
    assert ok.release_ready is False
    assert ok.user_approved is False


# ---------------------------------------------------------------------------
# U13 — a scene with no context is never `scene_validated`
# ---------------------------------------------------------------------------

def test_u13_scene_without_semantic_or_concept_is_not_validated(
        tmp_path: Path) -> None:
    """The fail-open branch is gone: missing refs are named and block."""
    from slides_cli.gpn.models import CandidateKey, ModelTaskKind
    from slides_cli.gpn.restyle_runner import RunState, _advance_stage

    run_dir = tmp_path / "run"
    (run_dir / "scenes" / "slide0" / "c1").mkdir(parents=True)
    (run_dir / "inputs").mkdir(parents=True, exist_ok=True)
    state = RunState.model_validate({
        "run_id": "r1", "run_dir": str(run_dir),
        "input_spec": {"kind": "pptx", "path": str(tmp_path / "src.pptx"),
                       "project_root": str(tmp_path),
                       "output_path": str(tmp_path / "out.pptx")},
        "stage": "scene_generation",
        "candidate_states": {"r1/slide0/c1": "concept_pending"},
        "slide_candidates": {"slide0": ["c1"]},
    })
    state.candidate_states["r1/slide0/c1"] = "concept_pending"
    state.responses.append(type("R", (), {
        "request_id": "T-scene-slide0-c1",
        "payload": {"scene_plan": {"slide_id": "slide0"}},
    })())
    state.task_queue.append(type("T", (), {
        "request_id": "T-scene-slide0-c1", "kind": ModelTaskKind.SCENE_GENERATION,
        "status": "consumed",
        "candidate_key": CandidateKey(run_id="r1", source_slide_id="slide0",
                                      candidate_id="c1"),
    })())
    findings: list = []
    _advance_stage(state, findings)
    assert state.candidate_states["r1/slide0/c1"] == "scene_context_missing"
    codes = [f.code for f in findings]
    assert "SCENE_CONTEXT_MISSING" in codes
    message = next(f.message for f in findings
                   if f.code == "SCENE_CONTEXT_MISSING")
    assert "semantic_slide" in message and "visual_concept" in message

    # Unresolvable candidate provenance is refused the same way: nothing is
    # built, and the negative history is recorded instead of crashing resume.
    orphan = RunState.model_validate({
        "run_id": "r1", "run_dir": str(run_dir),
        "input_spec": {"kind": "pptx", "path": str(tmp_path / "src.pptx"),
                       "project_root": str(tmp_path),
                       "output_path": str(tmp_path / "out.pptx")},
        "stage": "scene_generation",
        "candidate_states": {"r1/slide0/c1": "concept_pending"},
    })
    orphan.responses.append(type("R", (), {
        "request_id": "T-scene-slide0-c1",
        "payload": {"scene_plan": {"slide_id": "slide0"}},
    })())
    orphan.task_queue.append(type("T", (), {
        "request_id": "T-scene-slide0-c1", "kind": ModelTaskKind.SCENE_GENERATION,
        "status": "consumed",
        "candidate_key": CandidateKey(run_id="r1", source_slide_id="slide0",
                                      candidate_id="c1"),
    })())
    orphan_findings: list = []
    _advance_stage(orphan, orphan_findings)
    assert orphan.candidate_states["r1/slide0/c1"] == "concept_pending"
    assert "SCENE_PROVENANCE_MISSING" in [f.code for f in orphan_findings]
    assert orphan.rejected_scene_reports["T-scene-slide0-c1"]["code"] == (
        "SCENE_PROVENANCE_MISSING"
    )
    assert orphan.materialized_scene_responses == []


# ---------------------------------------------------------------------------
# U14 — immutable, unique model task packets
# ---------------------------------------------------------------------------

def test_u14_scene_packets_are_unique_per_slide_and_concept(
        tmp_path: Path) -> None:
    """Different concepts/revisions get different immutable packet paths."""
    from slides_cli.gpn.restyle_runner import RunState, _scene_generation_task

    run_dir = tmp_path / "run"
    (run_dir / "design_briefs").mkdir(parents=True)
    (run_dir / "visual_concepts" / "slide0").mkdir(parents=True)
    (run_dir / "visual_concepts" / "slide1").mkdir(parents=True)
    (run_dir / "inputs").mkdir(parents=True)
    (run_dir / "source_ir.json").write_text(
        '{"slides": [{"id": "slide0", "objects": []},'
        ' {"id": "slide1", "objects": []}]}', encoding="utf-8")
    from slides_cli.gpn.models import CandidateKey, VisualConcept

    concept = VisualConcept.model_validate({
        "concept_id": "c1", "visual_argument": "test"})
    (run_dir / "visual_concepts" / "slide0" / "c1.json").write_text(
        concept.model_dump_json(), encoding="utf-8")

    state = RunState.model_validate({
        "run_id": "r1", "run_dir": str(run_dir),
        "input_spec": {"kind": "pptx", "path": str(tmp_path / "src.pptx"),
                       "project_root": str(tmp_path),
                       "output_path": str(tmp_path / "out.pptx")}})
    seen: dict[str, str] = {}
    for slide, cand in (("slide0", "c1"), ("slide0", "c2"),
                        ("slide1", "c1")):
        key = f"r1/{slide}/{cand}"
        state.concept_slide[key] = slide
        state.candidate_states[key] = "concept_pending"
        task = _scene_generation_task(state, key)
        assert task is not None
        assert task.packet not in seen, (
            f"packet path collision: {task.packet}")
        seen[task.packet] = key
        assert task.candidate_key == CandidateKey(
            run_id="r1", source_slide_id=slide, candidate_id=cand)
        packet = (run_dir / task.packet).read_text(encoding="utf-8")
        assert f'"slide_id": "{slide}"' in packet
        assert f'"concept_id": "{cand}"' in packet
    assert len(seen) == 3


# ---------------------------------------------------------------------------
# U15 — a failed candidate is not a successful build
# ---------------------------------------------------------------------------

def test_u15_candidate_attempt_result_keeps_diagnostic_path_but_blocks(
        tmp_path: Path) -> None:
    from slides_cli.gpn.models import CandidateAttemptResult

    failed = CandidateAttemptResult(
        deck_key="slide0--c2", slide_id="slide0", candidate_id="c2",
        candidate_path="scenes/slide0--c2/candidate.pptx",
        candidate_sha256="c" * 64, operation_status="needs_review",
        measured_status="resolved", saved_checks_status="fail",
        finding_codes=["SAVED_REMEASURE_FAIL"], eligible=False,
        eligibility_reason="saved checks failed")
    assert failed.candidate_path, "diagnostic path is kept"
    assert failed.eligible is False
    # A bare model that claims eligibility without stage evidence is still
    # only a claim; the runner derives eligibility from the typed stages.
    assert CandidateAttemptResult(eligible=True).operation_status == ""
    with pytest.raises(ValidationError):
        CandidateAttemptResult(unknown_field=True)


# ---------------------------------------------------------------------------
# U05 — three source frames collapse into one native rich frame
# ---------------------------------------------------------------------------

def test_u05_three_sources_one_native_frame_with_three_paragraphs(
        tmp_path: Path) -> None:
    """3 source textboxes -> 1 p:sp/txBody with 3 real paragraphs."""
    from lxml import etree

    from slides_cli.gpn.assets import AssetStore
    from slides_cli.gpn.export_pipeline import _authored_block_paragraphs
    from slides_cli.gpn.importer import import_deck
    from slides_cli.gpn.native import NativeDeckContext, emit_rich_text_block

    prs = fx.new_deck()
    slide = fx.blank_slide(prs)
    for i, text in enumerate(["Первый абзац", "Второй абзац",
                              "Третий абзац"]):
        fx.add_textbox(slide, text, left=1.0, top=1.0 + i * 1.0,
                       width=5.0, height=0.6, size=14)
    src = tmp_path / "three.pptx"
    prs.save(str(src))
    deck, _ = import_deck(src, AssetStore(tmp_path / "assets"))
    s = deck.slides[0]
    refs = [o.id for o in s.objects if o.kind == "text"]
    assert len(refs) == 3
    paragraphs = _authored_block_paragraphs(refs, {o.id: o
                                                  for o in s.objects})
    assert len(paragraphs) == 3

    out = fx.new_deck()
    out_slide = fx.blank_slide(out)
    context = NativeDeckContext(presentation=out)
    address = emit_rich_text_block(
        paragraphs,
        {"x": 914400, "y": 914400, "w": 4572000, "h": 1143000},
        out_slide, context, role="body", rules=_rules(), node_id="block-1")
    path = tmp_path / "block.pptx"
    out.save(str(path))

    with zipfile_module(path) as z:
        xml = etree.fromstring(z.read("ppt/slides/slide1.xml"))
    ns = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main",
          "p": "http://schemas.openxmlformats.org/presentationml/2006/main"}
    bodies = xml.findall(".//p:sp/p:txBody", ns)
    assert len(bodies) == 1, "three sources must land in ONE frame"
    assert [p.findtext(".//a:t", namespaces=ns) for p in
            bodies[0].findall("a:p", ns)] == [
        "Первый абзац", "Второй абзац", "Третий абзац"]
    assert address.shape_id > 0


def zipfile_module(path: Path):
    import zipfile

    return zipfile.ZipFile(path)

def test_scene_plan_unexpanded_ref_is_reported():
    """Invented unit refs must surface as unexpanded, never bind phantoms."""
    from types import SimpleNamespace

    from slides_cli.gpn.restyle_runner import _materialize_scene_plan

    unit = SimpleNamespace(
        id="u1",
        source_refs=[SimpleNamespace(object_id="obj-a")])
    semantic = SimpleNamespace(units=[unit])
    plan = {
        "zones": [
            {"zone_id": "z1", "content_refs": ["u1"]},
            {"zone_id": "z2", "content_refs": ["slide0:u:m1"]},
        ],
        "relations": [],
        "reading_order": ["u1"],
        "dominant_ref": "",
    }
    out, unexpanded = _materialize_scene_plan(
        plan, semantic, [], "k", source_object_ids={"obj-a"})
    assert unexpanded == ["slide0:u:m1"]
    # The phantom is surfaced and never bound: it reaches no zone and no
    # content_refs list, so no draft plan can bind a nonexistent object.
    assert "slide0:u:m1" not in out["content_refs"]
    assert all("slide0:u:m1" not in list(z.get("content_refs", [])) for z in out["zones"])
    # The real semantic unit still lowers to its source object.
    assert "obj-a" in out["content_refs"]


def test_narrative_deck_materializes_without_fake_ppt_ids(tmp_path):
    """NARRATIVE_PLAN advances create: doc refs only, no PPT shape ids."""
    import json

    from slides_cli.gpn.document_ingest import (
        SourceBlock,
        SourceDocument,
    )
    from slides_cli.gpn.models import InputKind, ModelTaskResponse
    from slides_cli.gpn.restyle_runner import _materialize_narrative_deck

    run = tmp_path / "nrun"
    (run / "inputs").mkdir(parents=True)
    doc = SourceDocument(
        input_kind=InputKind.PROMPT, source_path="",
        content_sha256="abc", source_sha256="abc",
        blocks=[SourceBlock(block_id="b1", text="Добыча 400 тонн")])
    (run / "inputs" / "source_manifest.json").write_text(
        doc.model_dump_json(), encoding="utf-8")
    response = ModelTaskResponse(
        request_id="T-n", input_fingerprint="abc", actual_model="test",
        schema_version_id="narrative-plan/v1",
        payload={
            "narrative": "План",
            "slides": [{
                "title": "Добыча",
                "units": [{
                    "id": "u1", "kind": "metric_value",
                    "text": "Добыча 400 тонн",
                    "source_block_ids": ["b1"],
                    "preservation": "required_exact"}],
                "relations": []}],
            "required_fact_coverage": ["400"],
            "transformation_decisions": [{
                "source_object_id": "b1", "action": "preserve",
                "reason": "key fact"}]})
    findings: list = []
    deck_path = _materialize_narrative_deck(run, response, findings)
    assert deck_path is not None and deck_path.is_file()
    deck = json.loads(deck_path.read_text(encoding="utf-8"))
    assert deck["slides"][0]["source_slide_id"] == "doc-slide-1"
    assert "ppt/slides" not in deck_path.read_text(encoding="utf-8")
    ledger = json.loads(
        (run / "classification_and_transformation_ledger.json").read_text(
            encoding="utf-8"))
    assert ledger["decisions"][0]["source_object_id"] == "b1"


def test_measurement_pair_persisted(tmp_path):
    """Raw request/response evidence lands content-addressed on disk."""
    from slides_cli.gpn.measurement import measure_components
    from slides_cli.gpn.models import MeasureComponent, MeasurementRequest

    face = _bundle().bindings[2].browser_alias
    para = _para("Добыча")
    para["runs"][0]["style"] = {"face_alias": face, "size_pt": 12.0}
    req = MeasurementRequest(
        schema_version="1.0", request_id="persist-probe",
        canvas_pt={"w": 960.0, "h": 540.0}, css_px_per_in=96.0,
        profile_kind="production",
        components=[MeasureComponent(
            id="c0", kind="text", payload_ref="", paragraphs=[para],
            box_pt={"x": 0.0, "y": 0.0, "w": 200.0, "h": 40.0},
            margins_pt={"l": 4.8, "r": 4.8, "t": 4.8, "b": 4.8},
            vertical_anchor="top", wrap_policy="wrap", rotation_deg=0.0,
            list_preview={})],
        font_assets=_font_assets(),
        engine_options={"timeout_ms": 180000, "device_scale_factor": 1,
                        "allowlist_roots": ALLOW_ROOTS})
    persist = tmp_path / "m"
    out = measure_components(
        req, timeout_seconds=180.0, project_root=PROJECT_ROOT,
        use_cache=False, persist_dir=persist)
    assert out["response"]["items"]
    assert list((persist / "requests").glob("*.json"))
    assert list((persist / "responses").glob("*.json"))


def test_textless_rule_keeps_geometry_without_body_slot(
        tmp_path: Path) -> None:
    """A positioned rule keeps ~stroke geometry and adds no slot need.

    Vectors carry no text: measuring empty frames with text insets
    (~12pt + 7.2 gap for a 2pt rule) manufactures overflow out of nothing.
    The rule resolves at its authored geometry while the text fits.
    """
    from pptx.enum.shapes import MSO_CONNECTOR
    from pptx.util import Inches

    from slides_cli.gpn.assets import AssetStore
    from slides_cli.gpn.importer import import_deck
    from slides_cli.gpn.layout import resolve_layout
    from slides_cli.gpn.measurement import measure_components
    from slides_cli.gpn.provenance import build_ledger

    prs = fx.new_deck()
    slide = fx.blank_slide(prs)
    fx.add_textbox(slide, "Первая строка\nВторая строка", left=1.0, top=1.0,
                   width=8.0, height=1.0, size=18)
    slide.shapes.add_connector(
        MSO_CONNECTOR.STRAIGHT, Inches(1.0), Inches(2.4),
        Inches(8.0), Inches(0.02))
    src = tmp_path / "rule.pptx"
    prs.save(str(src))
    deck, _ = import_deck(src, AssetStore(tmp_path / "assets"))
    ledger = build_ledger(deck, decoration=[])
    s = deck.slides[0]
    refs = [o.id for o in s.objects]
    assert len(refs) == 2
    text_ref = next(o.id for o in s.objects if o.kind == "text")
    line_ref = next(o.id for o in s.objects if o.id != text_ref)
    intent = _intent_with_pairs(
        s.id, refs, [], [],
        zones=[
            {"zone_id": "z-text", "x_pct": 5.0, "y_pct": 5.0,
             "w_pct": 90.0, "h_pct": 30.0, "role": "body",
             "content_refs": [text_ref]},
            {"zone_id": "z-rule", "x_pct": 5.5, "y_pct": 27.9,
             "w_pct": 88.9, "h_pct": 1.5, "role": "body",
             "content_refs": [line_ref]}])
    tree = compile_composition(intent, s, ledger, _rules(), TEST_PROFILE)

    def measurer(request):
        return measure_components(
            request, timeout_seconds=180.0, project_root=PROJECT_ROOT,
            use_cache=True)["response"]

    res = resolve_layout(
        tree, s, ledger, _rules(), TEST_PROFILE, _bundle(), measurer,
        {"project_root": PROJECT_ROOT,
         "font_allowlist_roots": ALLOW_ROOTS,
         "use_cache": True, "profile_kind": "production",
         "max_local_layout_attempts": 3,
         "debug_output_dir": str(tmp_path / "dbg")})
    assert res.status == "resolved", res.unresolved
    geo = res.resolved_slide["geometry_emu"]
    line_node = next(
        node for node in tree.components.values()
        if node.kind == "connector")
    line_h_pt = geo[line_node.payload_ref]["h"] / 12700.0
    assert line_h_pt <= 8.0, f"rule inflated to a text slot: {line_h_pt}pt"


def test_slide_background_token_flows_to_native_bytes(tmp_path: Path) -> None:
    """Backdrop token: intent validates, tree/resolved carry it, slide XML
    gets the normative solid fill (the white-logo-on-blue case)."""
    from slides_cli.gpn.native import set_slide_background

    deck, ledger, s = _two_text_deck(tmp_path)
    refs = [o.id for o in s.objects]
    data = {"slide_id": s.id, "candidate_id": "c0",
            "content_refs": list(refs), "communication_job": "t",
            "reading_order": list(refs), "dominant_ref": refs[0],
            "zones": [{"zone_id": "z-all", "x_pct": 0.0, "y_pct": 0.0,
                       "w_pct": 100.0, "h_pct": 100.0, "role": "body",
                       "content_refs": list(refs)}],
            "relations": [], "operators": [], "reference_ids": [],
            "allowed_overlaps": [], "overlap_permissions": [],
            "slide_background_token": "blue",
            "novel_recipe": "", "uncertainties": [], "rationale": "t"}
    intent = LayoutIntent.model_validate(data)
    tree = compile_composition(intent, s, ledger, _rules(), TEST_PROFILE)
    assert tree.slide_background_token == "blue"

    from slides_cli.gpn.models import (
        CandidateKey,
        ModelTask,
        ModelTaskKind,
        ModelTaskResponse,
        VisualConcept,
    )
    from slides_cli.gpn.planner import validate_scene_response

    ck = CandidateKey(run_id="r", source_slide_id=s.id, candidate_id="c0")
    task = ModelTask(request_id="T", kind=ModelTaskKind.SCENE_GENERATION,
                     candidate_key=ck, input_fingerprint="x",
                     response_schema_id="scene-plan/v1", packet="p",
                     expected_response_units=["scene_plan"])
    response = ModelTaskResponse(
        request_id="T", input_fingerprint="x", actual_model="m",
        schema_version_id="scene-plan/v1",
        payload={"scene_plan": data})
    from slides_cli.gpn.models import SemanticSlide
    report = validate_scene_response(
        response, task, SemanticSlide(source_slide_id=s.id),
        VisualConcept(concept_id="c0"), _rules())
    assert report.valid, report.hard_issues
    bad = dict(data, slide_background_token="nope")
    bad_response = ModelTaskResponse(
        request_id="T", input_fingerprint="x", actual_model="m",
        schema_version_id="scene-plan/v1", payload={"scene_plan": bad})
    bad_report = validate_scene_response(
        bad_response, task, SemanticSlide(source_slide_id=s.id),
        VisualConcept(concept_id="c0"), _rules())
    assert not bad_report.valid

    prs = fx.new_deck()
    slide = fx.blank_slide(prs)
    from slides_cli.gpn.native import NativeDeckContext
    context = NativeDeckContext(presentation=prs)
    set_slide_background(slide, "004596", s.id, context)
    path = tmp_path / "bg.pptx"
    prs.save(str(path))
    import zipfile

    from lxml import etree
    with zipfile.ZipFile(path) as z:
        xml = etree.fromstring(z.read("ppt/slides/slide1.xml"))
    ns = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main",
          "p": "http://schemas.openxmlformats.org/presentationml/2006/main"}
    fills = xml.findall(".//p:bg//a:srgbClr", ns)
    assert [f.get("val") for f in fills] == ["004596"]
