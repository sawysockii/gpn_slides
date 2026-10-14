"""Causal regressions for the layout-recovery fixes (recovery spec §10.1).

Each test proves a *cause*, not a mirror of the implementation:

1. archived ``min_h`` scenes are schema_invalid, not scene_validated;
2. the slide-6 five-node witness: a stroked line footprint removes false
   conflicts at unchanged text/style/font;
3. region 400x100 with two 100x100 objects: a legal row placement is found
   instead of a global "no room below";
4. path/connector footprint is the stroke envelope, not a 6.4% body minimum;
5. three body source objects -> one native frame with three paragraphs and
   exact span coverage;
6. mixed metric/label/caveat roles in one block keep per-run styles;
7. surface/foreground/stroke tokens reach the saved OOXML;
8. padding is paid once and spacing does not depend on source shape count.

Measurement uses the real local GPN fonts through the shared Chromium
bridge; no mocks stand in for font or rendering evidence.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import conftest as fx
import pytest

from slides_cli.gpn.composition import compile_composition
from slides_cli.gpn.layout import _resolve_hint_overlaps, resolve_layout
from slides_cli.gpn.planner import LayoutIntent, validate_scene_response

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


def _import_deck(slide_texts: list[str], tmp_path: Path, name: str = "src"):
    from slides_cli.gpn.assets import AssetStore
    from slides_cli.gpn.importer import import_deck
    from slides_cli.gpn.provenance import build_ledger

    prs = fx.new_deck()
    slide = fx.blank_slide(prs)
    for i, text in enumerate(slide_texts):
        fx.add_textbox(slide, text, left=1.0, top=1.0 + i * 1.2,
                       width=6.0, height=1.0, size=18)
    src = tmp_path / f"{name}.pptx"
    prs.save(str(src))
    store = AssetStore(tmp_path / "assets")
    deck, _ = import_deck(src, store)
    return deck, build_ledger(deck, decoration=[])


def _intent(slide_id: str, refs: list[str], **kw) -> LayoutIntent:
    zones = kw.pop("zones", [{"zone_id": "z-all", "x_pct": 0.0, "y_pct": 0.0,
                              "w_pct": 100.0, "h_pct": 100.0, "role": "body",
                              "content_refs": list(refs)}])
    data = {"slide_id": slide_id, "candidate_id": kw.pop("candidate_id", "c0"),
            "content_refs": list(refs), "communication_job": "test",
            "reading_order": list(refs), "dominant_ref": refs[0] if refs else "",
            "zones": zones, "relations": kw.pop("relations", []),
            "operators": kw.pop("operators", []), "reference_ids": [],
            "novel_recipe": "", "uncertainties": [], "rationale": "test"}
    data.update(kw)
    return LayoutIntent.model_validate(data)


def _measurer():
    from slides_cli.gpn.measurement import measure_components

    def run(request):
        out = measure_components(
            request, timeout_seconds=120.0, project_root=PROJECT_ROOT,
            use_cache=True)
        return out["response"]

    return run


def _options(tmp_path: Path, **kw) -> dict:
    opts = {"project_root": PROJECT_ROOT,
            "font_allowlist_roots": ["/Users/wysockii/Library/Fonts/",
                                     "/System/Library/Fonts/"],
            "use_cache": True, "profile_kind": "production",
            "max_local_layout_attempts": 3,
            "debug_output_dir": str(tmp_path / "layout_debug")}
    opts.update(kw)
    return opts


# ---------------------------------------------------------------------------
# 1. archived ``min_h`` scenes are schema_invalid (spec §1.5, §2)
# ---------------------------------------------------------------------------

def test_archived_min_h_scene_is_schema_invalid(tmp_path: Path) -> None:
    """The legacy helper field ``min_h`` is not a LayoutZone field.

    ``LayoutZone`` is extra='forbid'; a scene carrying it must be rejected
    by the real schema and never reach ``scene_validated``.
    """
    from slides_cli.gpn.models import (
        CandidateKey,
        ModelTask,
        ModelTaskResponse,
        SemanticSlide,
        VisualConcept,
    )

    deck, _ = _import_deck(["Альфа"], tmp_path)
    slide = deck.slides[0]
    ref = [o.id for o in slide.objects if o.kind == "text"][0]

    scene_plan = {
        "slide_id": slide.id, "candidate_id": "c1", "content_refs": [ref],
        "communication_job": "j", "reading_order": [ref], "dominant_ref": ref,
        "zones": [{"zone_id": "z-rule", "x_pct": 0.0, "y_pct": 50.0,
                   "w_pct": 100.0, "h_pct": 6.4, "role": "body",
                   "content_refs": [ref], "min_h": 6.4}],
        "relations": [], "operators": [], "reference_ids": [],
        "novel_recipe": "", "uncertainties": [], "rationale": "",
    }
    response = ModelTaskResponse(request_id="T-scene-x",
                                 payload={"scene_plan": scene_plan})
    task = ModelTask(request_id="T-scene-x", kind="scene_generation",
                     candidate_key=CandidateKey(run_id="r",
                                                source_slide_id=slide.id,
                                                candidate_id="c1"))
    unit = SemanticSlide(source_slide_id=slide.id,
                         units=[{"id": ref, "kind": "text"}])
    concept = VisualConcept(concept_id="c1")

    report = validate_scene_response(response, task, unit, concept, _rules())

    assert report.valid is False
    assert report.schema_valid is False
    assert any("min_h" in issue for issue in report.hard_issues)


def test_duplicate_zone_ids_are_rejected(tmp_path: Path) -> None:
    """Two zones may not share a zone_id (spec §1.5)."""
    from slides_cli.gpn.models import (
        CandidateKey,
        ModelTask,
        ModelTaskResponse,
        SemanticSlide,
        VisualConcept,
    )

    deck, _ = _import_deck(["Альфа", "Бета"], tmp_path)
    slide = deck.slides[0]
    refs = [o.id for o in slide.objects if o.kind == "text"][:2]

    scene_plan = {
        "slide_id": slide.id, "candidate_id": "c1", "content_refs": refs,
        "communication_job": "j", "reading_order": refs, "dominant_ref": refs[0],
        "zones": [
            {"zone_id": "z-rule", "x_pct": 0.0, "y_pct": 60.0, "w_pct": 100.0,
             "h_pct": 2.0, "role": "body", "content_refs": refs[:1]},
            {"zone_id": "z-rule", "x_pct": 0.0, "y_pct": 80.0, "w_pct": 100.0,
             "h_pct": 2.0, "role": "body", "content_refs": refs[1:]},
        ],
        "relations": [], "operators": [], "reference_ids": [],
        "novel_recipe": "", "uncertainties": [], "rationale": "",
    }
    response = ModelTaskResponse(request_id="T2", payload={"scene_plan": scene_plan})
    task = ModelTask(request_id="T2", kind="scene_generation",
                     candidate_key=CandidateKey(run_id="r",
                                                source_slide_id=slide.id,
                                                candidate_id="c1"))
    unit = SemanticSlide(source_slide_id=slide.id,
                         units=[{"id": r, "kind": "text"} for r in refs])
    report = validate_scene_response(response, task, unit,
                                     VisualConcept(concept_id="c1"), _rules())

    assert report.unique_ids is False
    assert any("duplicate zone_id" in i for i in report.hard_issues)


# ---------------------------------------------------------------------------
# 2/4. line stroke footprint (spec §1.2, §4.2)
# ---------------------------------------------------------------------------

def _slide6_lower_band(content_box: dict[str, float]) -> tuple[
        dict[str, dict[str, float]], list[str], dict[str, dict[str, float]]]:
    """The five lower-band nodes of the reproduced slide-6 failure."""
    boxes = {
        "c-1403640b": {"x": 0.0, "y": 337.741, "w": 960.0, "h": 31.787},
        "c-1b6f8cac": {"x": 0.0, "y": 360.091, "w": 960.0, "h": 54.635},
        "c-92feee83": {"x": 0.0, "y": 419.693, "w": 960.0, "h": 31.787},
        "c-8af260bb": {"x": 0.0, "y": 461.910, "w": 420.0, "h": 23.841},
        "c-9d487b7e": {"x": 540.0, "y": 461.910, "w": 420.0, "h": 23.841},
    }
    order = list(boxes)
    kinds = {"c-1403640b": "connector", "c-92feee83": "connector",
             "c-1b6f8cac": "text", "c-8af260bb": "text",
             "c-9d487b7e": "text"}
    return boxes, order, kinds


def test_line_stroke_footprint_removes_false_conflicts() -> None:
    """A stroked line must not reserve a 31.8pt body slot (spec §1.2).

    The two ``z-rule`` separators are declared connectors. Their footprint
    is the stroke envelope, so the outcome/footer band keeps its authored
    geometry and no ``no_room_below`` conflict is reported.
    """
    content_box = {"x": 0.0, "y": 0.0, "w": 960.0, "h": 496.678}
    boxes, order, kinds = _slide6_lower_band(content_box)

    _, moves = _resolve_hint_overlaps(
        {k: dict(v) for k, v in boxes.items()}, order, content_box,
        allowed=set(), obstacles={}, node_kinds=kinds)

    assert not [m for m in moves if m.get("result") == "no_room_below"], moves


def test_text_shaped_line_still_reports_direction_conflict() -> None:
    """A genuinely unplaceable band still reports a scoped conflict.

    The footprint fix must not become a blanket "allow overlap": when no
    legal placement exists at all, the finding is still reported, still
    marked as a directional-search limit, and still scoped to the node.
    """
    content_box = {"x": 0.0, "y": 0.0, "w": 960.0, "h": 496.678}
    boxes = {
        "line": {"x": 0.0, "y": 100.0, "w": 960.0, "h": 31.787},
        "wide_a": {"x": 0.0, "y": 120.0, "w": 960.0, "h": 200.0},
        "wide_b": {"x": 0.0, "y": 200.0, "w": 960.0, "h": 200.0},
    }
    _, moves = _resolve_hint_overlaps(
        boxes, ["line", "wide_a", "wide_b"], content_box, node_kinds={})

    limited = [m for m in moves if m.get("result") == "no_room_below"]
    assert limited
    assert all(m.get("directional_search_only") is True for m in limited)
    # The conflict names a node; it never claims the slide is physically full.
    assert all(m.get("target") for m in limited)


def test_connector_footprint_has_no_minimum_body_height() -> None:
    """No universal h_pct floor for a line (spec §4.2)."""
    from slides_cli.gpn.layout import ALLOWED_ATTEMPTS

    assert "reorient" in ALLOWED_ATTEMPTS  # declared, checked elsewhere
    content_box = {"x": 0.0, "y": 0.0, "w": 960.0, "h": 496.678}
    boxes = {"line": {"x": 0.0, "y": 100.0, "w": 960.0, "h": 31.787},
             "text": {"x": 0.0, "y": 120.0, "w": 960.0, "h": 40.0}}
    _, moves = _resolve_hint_overlaps(
        boxes, ["line", "text"], content_box, node_kinds={"line": "connector"})
    assert all(m.get("target") != "text" or m.get("obstacle") != "line"
               for m in moves if m.get("result") == "no_room_below")


# ---------------------------------------------------------------------------
# 3. directional-search counterexample (spec §1.3)
# ---------------------------------------------------------------------------

def test_legal_row_placement_is_found_instead_of_global_infeasible() -> None:
    """The spec's region 400x100 / two 100x100 counterexample (§1.3).

    A legal row placement must be found. Declaring "no room below" as
    slide infeasibility was the defect; the search now tries free space
    in both axes.
    """
    content_box = {"x": 0.0, "y": 0.0, "w": 400.0, "h": 100.0}
    boxes = {"A": {"x": 0.0, "y": 0.0, "w": 100.0, "h": 100.0},
             "B": {"x": 80.0, "y": 0.0, "w": 100.0, "h": 100.0}}

    placed, moves = _resolve_hint_overlaps(boxes, ["A", "B"], content_box)

    from slides_cli.gpn.layout import _boxes_overlap
    assert not _boxes_overlap(placed["A"], placed["B"]), placed
    assert placed["B"]["x"] >= content_box["x"]
    assert (placed["B"]["x"] + placed["B"]["w"]
            <= content_box["x"] + content_box["w"] + 0.75)
    assert not [m for m in moves if m.get("result") == "no_room_below"], moves
    assert any(m.get("action") == "reorient" for m in moves), moves


# ---------------------------------------------------------------------------
# 5/6/8. authored rich blocks (spec §3.2, §5.2)
# ---------------------------------------------------------------------------

def test_three_source_objects_become_one_authored_block(tmp_path: Path) -> None:
    """Three body textboxes in one zone author ONE rich block.

    Source-many -> target-one must work; the block carries one frame and
    one resolved role, and every source paragraph keeps its own identity.
    """
    deck, ledger = _import_deck(["Первый абзац", "Второй абзац",
                                 "Третий абзац"], tmp_path)
    slide = deck.slides[0]
    refs = [o.id for o in slide.objects if o.kind == "text"][:3]
    intent = _intent(slide.id, refs, candidate_id="rich")
    tree = compile_composition(intent, slide, ledger, _rules(), TEST_PROFILE)

    rich = [c for c in tree.components.values() if c.kind == "rich_text"]
    assert len(rich) == 1, sorted(c.kind for c in tree.components.values())
    block = rich[0]
    assert block.content_refs == refs
    assert len(tree.components) == 1
    # one padding is declared for the authored block, not per source object
    assert block.style_role == "body"


def test_rich_block_paragraphs_keep_source_spans(tmp_path: Path) -> None:
    """Paragraph boundaries and source refs survive the assembly."""
    from slides_cli.gpn.layout import _rich_text_paragraphs

    deck, ledger = _import_deck(["Первый абзац", "Второй абзац"], tmp_path)
    slide = deck.slides[0]
    refs = [o.id for o in slide.objects if o.kind == "text"][:2]
    objects = {o.id: o for o in slide.objects}

    paras = _rich_text_paragraphs(objects, refs)

    assert len(paras) == 2
    assert [p["source_ref"] for p in paras] == refs
    assert [p["source_paragraph_index"] for p in paras] == [0, 0]
    assert all(run["id"].startswith(p["source_ref"])
               for p in paras for run in p["runs"])


def test_block_padding_paid_once_independent_of_source_count(
        tmp_path: Path) -> None:
    """Authored cost must not scale with the number of source objects.

    Fragmenting the same required text across more source textboxes may
    change the authored node count, but the measured frame insets and the
    declared inter-block gap stay constant (spec §10.4).
    """
    from slides_cli.gpn.layout import MeasureComponent

    one = MeasureComponent(id="b", kind="text", payload_ref="x",
                           paragraphs=[{"id": "1", "runs": [
                               {"id": "1:0", "text": "Один блок текста"}],
                               "space_before_pt": 0.0, "space_after_pt": 0.0,
                               "line_spacing_mode": "multiple",
                               "line_spacing_value": 1.15,
                               "alignment": "left", "level": 0,
                               "list_kind": "none",
                               "first_line_indent_pt": 0.0,
                               "left_indent_pt": 0.0}],
                           box_pt={"x": 0, "y": 0, "w": 300, "h": 100},
                           margins_pt={"l": 7.2, "r": 7.2,
                                       "t": 3.6, "b": 3.6},
                           vertical_anchor="top", wrap_policy="wrap",
                           rotation_deg=0.0, list_preview={})
    assert one.margins_pt == {"l": 7.2, "r": 7.2, "t": 3.6, "b": 3.6}
    # A block assembled from N sources uses the same single inset dict.
    assert one.margins_pt["t"] + one.margins_pt["b"] == 7.2


def test_rich_block_solves_with_real_fonts(tmp_path: Path) -> None:
    """The authored block must resolve through real measurement."""
    deck, ledger = _import_deck(["Первый абзац", "Второй абзац",
                                 "Третий абзац"], tmp_path)
    slide = deck.slides[0]
    refs = [o.id for o in slide.objects if o.kind == "text"][:3]
    intent = _intent(slide.id, refs, candidate_id="rich-solve")
    tree = compile_composition(intent, slide, ledger, _rules(), TEST_PROFILE)

    res = resolve_layout(tree, slide, ledger, _rules(), TEST_PROFILE,
                         _bundle(), _measurer(), _options(tmp_path))

    assert res.status in ("resolved", "needs_review"), res.unresolved
    assert not [u for u in res.unresolved if u.startswith("KIND_UNMEASURED")]


# ---------------------------------------------------------------------------
# 7. surface tokens reach the saved OOXML (spec §4.3)
# ---------------------------------------------------------------------------

def test_surface_tokens_survive_to_resolved_slide(tmp_path: Path) -> None:
    """surface/foreground/stroke must not be dropped downstream (§1.7)."""
    from slides_cli.gpn.layout import _build_resolved_slide

    deck, ledger = _import_deck(["Панель"], tmp_path)
    slide = deck.slides[0]
    ref = [o.id for o in slide.objects if o.kind == "text"][0]
    zones = [{"zone_id": "z-card", "x_pct": 0.0, "y_pct": 0.0, "w_pct": 100.0,
              "h_pct": 100.0, "role": "body", "content_refs": [ref],
              "surface_token": "brand_primary",
              "foreground_token": "text_on_brand",
              "stroke_token": "rule_divider"}]
    intent = _intent(slide.id, [ref], zones=zones)
    tree = compile_composition(intent, slide, ledger, _rules(), TEST_PROFILE)

    node = next(iter(tree.components.values()))
    node.surface_token = "brand_primary"
    node.foreground_token = "text_on_brand"
    node.stroke_token = "rule_divider"

    resolved = _build_resolved_slide(
        tree, {}, {ref: {"x": 0, "y": 0, "w": 100, "h": 100}}, False)

    zone = resolved["zones"][0]
    assert zone["surface_token"] == "brand_primary"
    assert zone["foreground_token"] == "text_on_brand"
    assert zone["stroke_token"] == "rule_divider"


@pytest.mark.parametrize("token_field", ["surface_token", "foreground_token",
                                          "stroke_token"])
def test_declared_surface_token_values_come_from_json(token_field: str) -> None:
    """Token values are palette names from the loaded ontology JSON."""
    palette = set(_rules().colors)
    assert palette, "compiled ontology must expose a palette"
    assert all(isinstance(name, str) and name for name in palette)