"""Regressions for the causes fixed in the layout-recovery round 2.

Each test states a *cause* from ``opencode_layout_engine_recovery.md``, not
a mirror of the implementation:

* §5.1 measurement mixes coordinate spaces — an exactly fitting frame was
  reported as overflowing by its own top padding;
* §4.1/§4.2 a connector placed by the composition stayed at its source box
  and became an immovable obstacle that shifted the authored composition;
* §4.1 a decoration cluster was charged one body slot per source object;
* §4.3 surface tokens were carried only by authored rich-text blocks, so a
  declared panel silently rendered unfilled;
* §5.3 a measurement run with a face the browser did not load returned
  substituted metrics as if they were measurements.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import conftest as fx
import pytest

from slides_cli.gpn.composition import compile_composition
from slides_cli.gpn.layout import _cluster_placement, resolve_layout
from slides_cli.gpn.planner import LayoutIntent

PROJECT_ROOT = Path(__file__).resolve().parents[3]


@lru_cache(maxsize=1)
def _rules():
    from slides_cli.gpn.compiler import compile_ontology
    from slides_cli.gpn.ontology import resolve_ontology_sources

    sources = resolve_ontology_sources(PROJECT_ROOT)
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
    return prepare_measurement_fonts(inv, "", profile_kind="production")


def _profile():
    from slides_cli.gpn.models import TemplateProfile

    return TemplateProfile.model_validate({
        "template_hash": "test",
        "canvas": {"x": 0, "y": 0, "w": 12192000, "h": 6858000},
        "layout_profiles": [{
            "id": "layout:0", "is_verified": True,
            # TemplateProfile stores the content box in points.
            "content_box": {"x": 0.0, "y": 0.0, "w": 960.0,
                            "h": 496.677874015748},
            "evidence": ["test"]}],
    })


def _deck_with_rule_and_text(tmp_path: Path):
    """One text object and one horizontal rule, authored in two zones."""
    from slides_cli.gpn.assets import AssetStore
    from slides_cli.gpn.importer import import_deck
    from slides_cli.gpn.provenance import build_ledger

    prs = fx.new_deck()
    slide = fx.blank_slide(prs)
    box = fx.add_textbox(slide, "Проверка переноса линии", left=1.0, top=1.0,
                         width=6.0, height=1.0, size=18)
    line = slide.shapes.add_connector(1, fx.Emu(914400), fx.Emu(1828800),
                                      fx.Emu(8229600), fx.Emu(1828800))
    line.name = "rule"
    src = tmp_path / "rule_src.pptx"
    prs.save(str(src))
    deck, _ = import_deck(src, AssetStore(tmp_path / "assets"))
    assert box is not None
    return deck, build_ledger(deck, decoration=[]), box.name


def _intent(slide_id: str, refs: list[str], zones: list[dict]) -> LayoutIntent:
    return LayoutIntent.model_validate({
        "slide_id": slide_id, "candidate_id": "c0", "content_refs": refs,
        "communication_job": "rule placement",
        "reading_order": refs, "dominant_ref": refs[0],
        "zones": zones, "relations": [], "operators": [],
        "reference_ids": [], "novel_recipe": "", "uncertainties": [],
        "rationale": "regression"})


# ---------------------------------------------------------------------------
# §5.1 one coordinate space for overflow
# ---------------------------------------------------------------------------

def test_frame_sized_to_measured_text_is_not_overflowing(tmp_path: Path) -> None:
    """A frame sized to the FULL required outer height fits exactly.

    ``occupied_height_pt`` is the legacy outer-origin extent: it includes the
    top inset but NOT the bottom one. Sizing a frame to it charged only one
    of the two vertical insets, so a frame that "fits" by that number still
    pushes its last line into the bottom inset. The exact-fit contract is
    ``required_outer_height_pt`` = topInset + content extent + bottomInset.
    """
    from slides_cli.gpn.measurement import measure_components
    from slides_cli.gpn.models import (
        MeasureComponent,
        MeasurementRequest,
        RectPt,
    )

    assets = [{"alias": b.browser_alias, "path": b.path, "sha256": b.sha256,
               "face_index": b.face_index, "style": "normal", "weight": 400}
              for b in _bundle().bindings]
    style = {"face_alias": assets[0]["alias"], "size_pt": 12.0, "bold": False,
             "italic": False}
    margins = {"l": 7.2, "r": 7.2, "t": 3.6, "b": 3.6}

    def request(height: float, rid: str) -> MeasurementRequest:
        para = {"id": "p0", "runs": [{"id": "r0", "text": "Измерение",
                                       "style": style}],
                "space_before_pt": 0.0, "space_after_pt": 0.0,
                "line_spacing_mode": "multiple", "line_spacing_value": 1.15,
                "alignment": "left", "level": 0, "list_kind": "none",
                "first_line_indent_pt": 0.0, "left_indent_pt": 0.0}
        return MeasurementRequest(
            schema_version="1.0", request_id=rid,
            canvas_pt={"w": 960.0, "h": 540.0}, css_px_per_in=96.0,
            profile_kind="production",
            components=[MeasureComponent(
                id="c0", kind="text", payload_ref="", paragraphs=[para],
                box_pt=RectPt(x=0.0, y=0.0, w=400.0, h=height),
                margins_pt=margins, vertical_anchor="top", wrap_policy="wrap",
                rotation_deg=0.0, list_preview={})],
            font_assets=assets,
            engine_options={"timeout_ms": 120000, "device_scale_factor": 1,
                            "allowlist_roots": []})

    tall = measure_components(request(2000.0, "fit-tall"),
                              project_root=PROJECT_ROOT)["response"]["items"][0]
    required = float(tall["required_outer_height_pt"])
    assert required == pytest.approx(
        float(tall["occupied_height_pt"]) + margins["b"], abs=1e-6)
    fitted = measure_components(
        request(required, "fit-exact"),
        project_root=PROJECT_ROOT)["response"]["items"][0]
    assert fitted["overflow_y"] is False
    assert fitted["excess_pt"]["y"] == 0.0
    # The pre-0.3.0 sizing (legacy occupied height, one inset only) now
    # reports the real bottom-inset intrusion instead of passing.
    legacy_sized = measure_components(
        request(float(tall["occupied_height_pt"]), "fit-legacy"),
        project_root=PROJECT_ROOT)["response"]["items"][0]
    assert legacy_sized["overflow_y"] is True
    # A frame one point short of the required extent still reports the
    # real deficit — the contract is not "never overflows".
    short = measure_components(
        request(required - 1.0, "fit-short"),
        project_root=PROJECT_ROOT)["response"]["items"][0]
    assert short["overflow_y"] is True


def test_unverified_face_fails_instead_of_reporting_substituted_metrics() -> None:
    """§5.3: a face outside the allowlist must fail, not silently measure."""
    from slides_cli.gpn.measurement import MeasurementError, measure_components
    from slides_cli.gpn.models import (
        MeasureComponent,
        MeasurementRequest,
        RectPt,
    )

    assets = [{"alias": b.browser_alias, "path": b.path, "sha256": b.sha256,
               "face_index": b.face_index, "style": "normal", "weight": 400}
              for b in _bundle().bindings]
    request = MeasurementRequest(
        schema_version="1.0", request_id="face-guard",
        canvas_pt={"w": 960.0, "h": 540.0}, css_px_per_in=96.0,
        profile_kind="production",
        components=[MeasureComponent(
            id="c0", kind="text", payload_ref="",
            paragraphs=[{"id": "p0", "runs": [{"id": "r0", "text": "шрифт",
                                               "style": {
                                                   "face_alias":
                                                       assets[0]["alias"],
                                                   "size_pt": 12.0,
                                                   "bold": False,
                                                   "italic": False}}],
                         "space_before_pt": 0.0, "space_after_pt": 0.0,
                         "line_spacing_mode": "multiple",
                         "line_spacing_value": 1.15, "alignment": "left",
                         "level": 0, "list_kind": "none",
                         "first_line_indent_pt": 0.0, "left_indent_pt": 0.0}],
            box_pt=RectPt(x=0.0, y=0.0, w=400.0, h=2000.0),
            margins_pt={"l": 7.2, "r": 7.2, "t": 3.6, "b": 3.6},
            vertical_anchor="top", wrap_policy="wrap", rotation_deg=0.0,
            list_preview={})],
        font_assets=assets,
        engine_options={"timeout_ms": 120000, "device_scale_factor": 1,
                        "allowlist_roots": ["/nonexistent/face/root"]})
    with pytest.raises(MeasurementError) as excinfo:
        measure_components(request, project_root=PROJECT_ROOT)
    assert excinfo.value.code == "MEASUREMENT_FACE_UNVERIFIED"


# ---------------------------------------------------------------------------
# §4.1/§4.2 authored rules and decoration clusters
# ---------------------------------------------------------------------------

def test_placed_rule_moves_with_its_group(tmp_path: Path) -> None:
    """A rule the composition placed is authored geometry, not an obstacle."""
    from slides_cli.gpn.measurement import measure_components

    deck, ledger, text_name = _deck_with_rule_and_text(tmp_path)
    slide = deck.slides[0]
    text_id = next(o.id for o in slide.objects if o.kind == "text")
    rule_id = next(o.id for o in slide.objects if o.id != text_id)
    intent = _intent(slide.id, [text_id, rule_id], [
        {"zone_id": "z-text", "x_pct": 4.0, "y_pct": 6.0, "w_pct": 60.0,
         "h_pct": 12.0, "role": "body", "content_refs": [text_id]},
        {"zone_id": "z-rule", "x_pct": 4.0, "y_pct": 30.0, "w_pct": 60.0,
         "h_pct": 0.4, "role": "body", "content_refs": [rule_id]},
    ])
    tree = compile_composition(intent, slide, ledger, _rules(), _profile(),
                              _bundle(), template_hash="t")
    node = tree.components[f"c-{rule_id}"]
    assert any(c.kind == "relative_box" and
               abs(float(c.value.get("y", 0)) - 30.0 / 100.0 * 6.898303805774278 * 72) < 1.0
               for c in node.constraints), \
        "an authored rule must carry its zone box to the solver"

    def measurer(request):
        return measure_components(
            request, timeout_seconds=120.0, project_root=PROJECT_ROOT,
            use_cache=True)["response"]

    result = resolve_layout(
        tree, slide, ledger, _rules(), _profile(), _bundle(), measurer,
        {"project_root": PROJECT_ROOT, "use_cache": True,
         "profile_kind": "production", "max_local_layout_attempts": 2})
    assert result.status not in ("needs_recompose", "constraint_conflict"), \
        result.unresolved


def test_disjoint_cluster_members_keep_their_relative_places() -> None:
    """§4.1/§5.2: a decoration group is not one body slot per object."""
    class _Box:
        def __init__(self, x, y, w, h):
            self.x, self.y, self.w, self.h = x, y, w, h

    class _Obj:
        def __init__(self, x, y, w, h):
            self.local_box = _Box(x, y, w, h)

    emu = 12700.0
    objects = {"a": _Obj(0.0, 0.0, 10.0 * emu, 10.0 * emu),
               "b": _Obj(12.0 * emu, 0.0, 10.0 * emu, 10.0 * emu)}
    payload = {"a": "a", "b": "b"}
    zone = {"x": 0.0, "y": 0.0, "w": 44.0, "h": 10.0}
    placed = _cluster_placement(["a", "b"], objects, payload, zone)
    assert placed is not None
    boxes = {cid: box for cid, box, _origin in placed}
    assert boxes["a"]["x"] < boxes["b"]["x"], \
        "disjoint members must keep their order inside the group"
    assert boxes["a"]["h"] == pytest.approx(10.0)
    # An overlapping cluster is a surface/child nest, not a disjoint group.
    objects["b"].local_box = _Box(5.0 * emu, 0.0, 10.0 * emu, 10.0 * emu)
    assert _cluster_placement(["a", "b"], objects, payload, zone) is None


# ---------------------------------------------------------------------------
# §4.3 surface contract reaches every component of the zone
# ---------------------------------------------------------------------------

def test_surface_tokens_reach_plain_components(tmp_path: Path) -> None:
    """A declared panel surface must not be dropped for a non-rich node."""
    from slides_cli.gpn.assets import AssetStore
    from slides_cli.gpn.importer import import_deck
    from slides_cli.gpn.provenance import build_ledger

    prs = fx.new_deck()
    slide = fx.blank_slide(prs)
    panel = slide.shapes.add_shape(1, fx.Emu(914400), fx.Emu(914400),
                                   fx.Emu(3000000), fx.Emu(1200000))
    panel.name = "panel"
    src = tmp_path / "panel_src.pptx"
    prs.save(str(src))
    deck, _ = import_deck(src, AssetStore(tmp_path / "assets"))
    ledger = build_ledger(deck, decoration=[])
    source_slide = deck.slides[0]
    panel_id = next(o.id for o in source_slide.objects
                    if "panel" in str(o.source_ref.shape_id) or
                    o.id == source_slide.objects[0].id)
    intent = _intent(source_slide.id, [panel_id], [
        {"zone_id": "z-panel", "x_pct": 4.0, "y_pct": 10.0, "w_pct": 40.0,
         "h_pct": 30.0, "role": "body", "content_refs": [panel_id],
         "surface_token": "light", "foreground_token": "dark",
         "stroke_token": "line"},
    ])
    tree = compile_composition(intent, source_slide, ledger, _rules(),
                              _profile(), _bundle(), template_hash="t")
    node = tree.components[f"c-{panel_id}"]
    assert node.kind != "rich_text", "fixture must not be a rich block"
    assert node.surface_token == "light"
    assert node.foreground_token == "dark"
    assert node.stroke_token == "line"


def test_surface_tokens_must_name_loaded_palette_entries(tmp_path: Path) -> None:
    """Tokens are palette names from the loaded JSON, never literal colors."""
    from slides_cli.gpn.models import (
        CandidateKey,
        ModelTask,
        ModelTaskResponse,
        SemanticSlide,
        VisualConcept,
    )
    from slides_cli.gpn.planner import validate_scene_response

    rules = _rules()
    scene_plan = {
        "slide_id": "slide0", "candidate_id": "c1", "content_refs": ["r1"],
        "communication_job": "j", "reading_order": ["r1"],
        "dominant_ref": "r1",
        "zones": [{"zone_id": "z", "x_pct": 0.0, "y_pct": 0.0, "w_pct": 50.0,
                   "h_pct": 20.0, "role": "body", "content_refs": ["r1"],
                   "surface_token": "FF0000", "foreground_token": "",
                   "stroke_token": ""}],
        "relations": [], "operators": [], "reference_ids": [],
        "novel_recipe": "", "uncertainties": [], "rationale": "",
    }
    resp = ModelTaskResponse(request_id="T-scene-token",
                             payload={"scene_plan": scene_plan})
    task = ModelTask(request_id="T-scene-token", kind="scene_generation",
                     candidate_key=CandidateKey(run_id="r",
                                                source_slide_id="slide0",
                                                candidate_id="c1"))
    unit = SemanticSlide(source_slide_id="slide0",
                         units=[{"id": "r1", "kind": "text"}])
    report = validate_scene_response(resp, task, unit,
                                     VisualConcept(concept_id="c1"), rules)
    assert not report.valid
    assert any("FF0000" in issue or "palette" in issue.lower()
               for issue in report.hard_issues)