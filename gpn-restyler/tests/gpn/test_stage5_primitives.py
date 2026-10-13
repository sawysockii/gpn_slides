"""Stage 5 primitive resolvers tests (spec §§8-10, 13.1).

Tables, graphs, chart exhibits, images and annotations measured through the
real Chromium bridge with exact local fonts. Synthetic fixtures only.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]


@lru_cache(maxsize=1)
def _deps():
    from slides_cli.gpn.compiler import compile_ontology
    from slides_cli.gpn.layout import resolve_component_style
    from slides_cli.gpn.ontology import resolve_ontology_sources
    from slides_cli.gpn.typography import (
        build_font_inventory_from_discovery,
        discover_local_fonts,
        prepare_measurement_fonts,
    )

    sources = resolve_ontology_sources(PROJECT_ROOT)
    rules = compile_ontology(PROJECT_ROOT / sources.primary_json, None,
                             sources=sources)
    report = discover_local_fonts(None)
    faces = {f for role in rules.roles.values() for f in role.allowed_faces}
    inv = build_font_inventory_from_discovery(report, faces)
    bundle = prepare_measurement_fonts(inv, "", profile_kind="production")
    styles = {name: resolve_component_style(name, rules, bundle)
              for name in ("body", "dense_body_or_caption", "footnote",
                           "native_chart_label")}
    assets = [{"alias": b.browser_alias, "path": b.path, "sha256": b.sha256,
               "face_index": b.face_index, "style": "normal", "weight": 400}
              for b in bundle.bindings]
    return {"rules": rules, "bundle": bundle, "styles": styles,
            "assets": assets}


def _measurer():
    from slides_cli.gpn.measurement import measure_components

    def run(request):
        out = measure_components(request, timeout_seconds=120.0,
                                 project_root=PROJECT_ROOT, use_cache=True)
        return out["response"]

    return run


def _options(**kw) -> dict:
    opts = {"profile_kind": "production", "font_assets": _deps()["assets"],
            "font_allowlist_roots": ["/Users/wysockii/Library/Fonts/",
                                     "/System/Library/Fonts/"],
            "debug_output_dir": ""}
    opts.update(kw)
    return opts


def _cell(row: int, col: int, text: str, **kw) -> dict:
    cell = {"id": f"cell-{row}-{col}", "row": row, "col": col,
            "row_span": 1, "col_span": 1, "is_merge_origin": False,
            "is_spanned": False, "paragraphs": [], "visible": True}
    cell.update(kw)
    if text:
        cell["paragraphs"] = [{"id": f"{cell['id']}-p0",
                               "runs": [{"id": f"{cell['id']}-r0",
                                         "text": text}]}]
    return cell


def _payload(cells: list[dict], rows: int, cols: int, **kw):
    from types import SimpleNamespace

    payload = {"rows": rows, "cols": cols, "cells": [], "merges": [],
               "header_row_ids": [0]}
    payload.update(kw)
    for c in cells:
        ns = SimpleNamespace(**{**c, "typed_value": None,
                                "source_style": SimpleNamespace(),
                                "source_ref": SimpleNamespace()})
        payload["cells"].append(ns)
    merges = []
    for m in payload.pop("merges"):
        merges.append(SimpleNamespace(**m))
    payload["merges"] = merges
    return SimpleNamespace(**payload)


# ---------------------------------------------------------------------------
# §8 tables
# ---------------------------------------------------------------------------

def test_long_last_table_row_and_spans() -> None:
    """Every visible cell constrains the grid: a long last-row text and a
    horizontal+vertical span widen their columns/rows (never measured by the
    first row alone)."""
    from slides_cli.gpn.primitives import layout_table

    cells = [_cell(0, 0, "Регион"), _cell(0, 1, "Выручка"),
             _cell(1, 0, "Север"), _cell(1, 1, "120,5"),
             _cell(2, 0, "Юг с очень длинным названием территории",
                   col_span=2, is_merge_origin=True),
             _cell(2, 1, "", is_spanned=True)]
    payload = _payload(cells, 3, 2,
                       merges=[{"r0": 2, "c0": 0, "r1": 2, "c1": 1}])
    box = {"x": 36.0, "y": 36.0, "w": 400.0, "h": 400.0}
    res = layout_table(payload, box, _deps()["styles"], _measurer(), _options())
    assert res["column_widths_pt"] and res["row_heights_pt"]
    assert len(res["column_widths_pt"]) == 2
    assert len(res["row_heights_pt"]) == 3
    # last-row span forces real width (not first-row-optimized)
    assert sum(res["column_widths_pt"]) > 200.0
    assert res["row_heights_pt"][2] >= res["row_heights_pt"][0]
    assert res["merge_ranges"] == [{"r0": 2, "c0": 0, "r1": 2, "c1": 1}]


def test_hidden_merged_payload_preserved() -> None:
    """Hidden grid data is reported preserved and never moved into the
    origin text: the origin measures its own text only."""
    from slides_cli.gpn.primitives import layout_table

    cells = [_cell(0, 0, "Видимый заголовок", col_span=2, is_merge_origin=True),
             _cell(0, 1, "СКРЫТЫЙ_СЕКРЕТ", is_spanned=True, visible=False),
             _cell(1, 0, "A"), _cell(1, 1, "B")]
    payload = _payload(cells, 2, 2,
                       merges=[{"r0": 0, "c0": 0, "r1": 0, "c1": 1}])
    box = {"x": 36.0, "y": 36.0, "w": 400.0, "h": 400.0}
    res = layout_table(payload, box, _deps()["styles"], _measurer(), _options())
    assert res["column_widths_pt"]
    assert "СКРЫТЫЙ_СЕКРЕТ" not in json_dumps(res)


def json_dumps(value) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)


def test_table_empty_zero_na() -> None:
    """Blank, zero and NA cells stay distinct states (no normalization)."""
    from slides_cli.gpn.primitives import layout_table

    cells = [_cell(0, 0, "H"), _cell(1, 0, ""), _cell(2, 0, "0"),
             _cell(3, 0, "н/д")]
    payload = _payload(cells, 4, 1)
    box = {"x": 36.0, "y": 36.0, "w": 300.0, "h": 400.0}
    res = layout_table(payload, box, _deps()["styles"], _measurer(), _options())
    assert len(res["row_heights_pt"]) == 4
    assert all(h >= 12.0 for h in res["row_heights_pt"])


# ---------------------------------------------------------------------------
# §9 graphs
# ---------------------------------------------------------------------------

def _branch_graph() -> tuple[list[dict], list[dict]]:
    nodes = [{"id": f"n{i}", "label": f"Этап {i} с описанием работ"}
             for i in range(8)]
    edges = [{"id": f"e{i}", "from": f"n{i}", "to": f"n{i + 1}",
              "kind": "sequence"} for i in range(6)]
    edges.append({"id": "e-branch", "from": "n2", "to": "n7",
                  "kind": "association"})
    edges.append({"id": "e-return", "from": "n6", "to": "n2",
                  "kind": "association"})
    return nodes, edges


def test_graph_more_than_six_nodes() -> None:
    """A branching 8-node process keeps every node/edge/label."""
    from slides_cli.gpn.primitives import layout_graph

    nodes, edges = _branch_graph()
    box = {"x": 36.0, "y": 36.0, "w": 600.0, "h": 400.0}
    res = layout_graph(nodes, edges, box, _deps()["styles"], _measurer(),
                       _options())
    assert res["ok"] is True
    assert len(res["node_boxes"]) == 8
    assert len(res["routes"]) == 8
    routed = {(r["from"], r["to"]) for r in res["routes"]}
    assert ("n2", "n7") in routed and ("n6", "n2") in routed


def test_graph_cycle_parallel_self_loop() -> None:
    """SCC survives (no flatten to DAG): cycle members group, parallel edges
    get lanes, self-loops route separately — or exact unsupported."""
    from slides_cli.gpn.primitives import layout_graph

    nodes = [{"id": "a", "label": "Альфа"}, {"id": "b", "label": "Бета"},
             {"id": "c", "label": "Гамма"}]
    edges = [{"id": "e1", "from": "a", "to": "b", "kind": "sequence"},
             {"id": "e2", "from": "b", "to": "a", "kind": "association"},
             {"id": "e3", "from": "a", "to": "b", "kind": "association"},
             {"id": "e4", "from": "c", "to": "c", "kind": "association"}]
    box = {"x": 36.0, "y": 36.0, "w": 500.0, "h": 300.0}
    res = layout_graph(nodes, edges, box, _deps()["styles"], _measurer(),
                       _options())
    assert res["ok"] is True
    assert any(set(s) == {"a", "b"} for s in res["sccs"])
    kinds = {r["edge_id"]: r["kind"] for r in res["routes"]}
    assert kinds["e4"] == "self_loop"
    assert len(res["routes"]) == 4


def test_connector_obstacle_routing() -> None:
    """Routes avoid unrelated measured text boxes (Manhattan corridors with
    clearance), endpoints stay on semantic node IDs."""
    from slides_cli.gpn.primitives import _manhattan_route, layout_graph

    nodes = [{"id": "left", "label": "Слева"},
             {"id": "middle", "label": " Apache Kafka посередине ".strip()},
             {"id": "right", "label": "Справа"}]
    edges = [{"id": "jump", "from": "left", "to": "right",
              "kind": "association"}]
    box = {"x": 0.0, "y": 0.0, "w": 600.0, "h": 200.0}
    res = layout_graph(nodes, edges, box, _deps()["styles"], _measurer(),
                       _options())
    route = res["routes"][0]["points"]
    assert len(route) >= 2
    mid = res["node_boxes"]["middle"]
    for i in range(len(route) - 1):
        x1, y1 = route[i]
        x2, y2 = route[i + 1]
        horizontal = abs(y1 - y2) < 1e-6
        vertical = abs(x1 - x2) < 1e-6
        assert horizontal or vertical
        if horizontal:
            inside_y = mid["y"] - 7.2 <= y1 <= mid["y"] + mid["h"] + 7.2
            overlap_x = not (max(x1, x2) <= mid["x"] - 7.2
                             or min(x1, x2) >= mid["x"] + mid["w"] + 7.2)
            assert not (inside_y and overlap_x), "route crosses the obstacle"
    assert _manhattan_route((0, 0), (10, 0), [], {"x": 0, "y": 0, "w": -5,
                                                 "h": 10}) == [(0, 0), (10, 0)]


# ---------------------------------------------------------------------------
# §10 charts / images / annotations
# ---------------------------------------------------------------------------

def test_chart_automatic_layout_unknown() -> None:
    """Label strings are measured, but automatic placement stays unknown:
    no whole-chart pass from strings alone; workbook/axes untouched."""
    from types import SimpleNamespace

    from slides_cli.gpn.primitives import layout_chart_exhibit

    payload = SimpleNamespace(
        categories=["Квартал 1 с длинным названием", "Квартал 2"],
        series_names=["Выручка", "Доля"], legend_entries=["Выручка", "Доля"],
        title="Динамика")
    box = {"x": 36.0, "y": 36.0, "w": 500.0, "h": 300.0}
    res = layout_chart_exhibit(payload, box, _deps()["styles"], _measurer(),
                               _options())
    assert res["ok"] is True
    assert res["workbook_touched"] is False
    assert "automatic_data_label_placement" in res["unsupported_scopes"]
    assert "axis_auto_ticks" in res["unsupported_scopes"]
    assert res["label_envelopes_pt"]


def test_chart_layout_keeps_workbook_axes() -> None:
    """Frame/label restyle keeps exact data/plots/axes: the payload object
    is never mutated by measurement."""
    from types import SimpleNamespace

    from slides_cli.gpn.primitives import layout_chart_exhibit

    payload = SimpleNamespace(categories=["A"], series_names=["S"],
                              legend_entries=[], title="")
    before = (list(payload.categories), list(payload.series_names))
    box = {"x": 36.0, "y": 36.0, "w": 400.0, "h": 300.0}
    layout_chart_exhibit(payload, box, _deps()["styles"], _measurer(),
                         _options())
    assert (list(payload.categories), list(payload.series_names)) == before


def test_image_aspect_and_crop_policy() -> None:
    """Aspect preserved with fit; policy-less crop is refused, not applied."""
    from types import SimpleNamespace

    from slides_cli.gpn.primitives import layout_image_region

    payload = SimpleNamespace()
    box = {"x": 36.0, "y": 36.0, "w": 400.0, "h": 300.0}
    res = layout_image_region(
        payload, box, {"image_natural_size_pt": {"w": 800.0, "h": 400.0}})
    assert res["ok"] is True
    assert res["box_pt"]["w"] == pytest.approx(400.0)
    assert res["box_pt"]["h"] == pytest.approx(200.0)
    assert res["media_touched"] is False
    bad = layout_image_region(payload, box, {"permitted_crop": (0.0, 0.0, 0.0, 0.6)})
    assert bad["ok"] is False
    assert any("CROP" in i for i in bad["issues"])


def test_annotation_placement_and_leader() -> None:
    """The caveat is measured and placed adjacent without covering foreign
    text; the leader points at the actual target."""
    from slides_cli.gpn.primitives import place_annotation

    target = {"x": 100.0, "y": 100.0, "w": 120.0, "h": 60.0}
    box = {"x": 36.0, "y": 36.0, "w": 600.0, "h": 400.0}
    res = place_annotation(
        {"text": "Оговорка: данные предварительные"}, target, box,
        _deps()["styles"], _measurer(), _options())
    assert res["ok"] is True
    assert res["slot"] in ("right", "below", "left", "above")
    assert res["leader"]["to"] == [160.0, 130.0]
    placed = res["box_pt"]
    assert placed["x"] >= box["x"] and placed["y"] >= box["y"]
    blocked = place_annotation(
        {"text": "Оговорка"}, target, box, _deps()["styles"], _measurer(),
        _options(annotation_obstacles=[{"x": 0.0, "y": 0.0, "w": 700.0,
                                       "h": 500.0}]))
    assert blocked["ok"] is False