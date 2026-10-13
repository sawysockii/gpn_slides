"""Stage 5 §§8-10: measured geometry for tables, graphs, charts, images,
annotations and preserve-mode shapes.

Each resolver measures through the injected measurer (real Chromium) and
returns resolved geometry plus honest checked/unknown scopes. Nothing here
writes OOXML; the Stage 4 exporter consumes the resolved values (Block 5
adds the missing typed bindings).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .models import MeasureComponent, MeasurementRequest

Measurer = Callable[[MeasurementRequest], dict[str, Any]]


def _emu_to_pt(value: int) -> float:
    from .units import emu_to_pt

    return emu_to_pt(int(value))


def _cell_paragraphs(cell: Any, run_style: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for para in list(getattr(cell, "paragraphs", []) or []):
        runs = [{"id": str(getattr(r, "id", "")),
                 "text": getattr(r, "text", "") or "",
                 "style": dict(run_style)}
                for r in list(getattr(para, "runs", []) or [])]
        if not runs:
            runs = [{"id": f"{cell.id}-empty", "text": "", "style": dict(run_style)}]
        out.append({
            "id": str(getattr(para, "id", f"{cell.id}-p")),
            "runs": runs,
            "space_before_pt": 0.0, "space_after_pt": 0.0,
            "line_spacing_mode": "multiple", "line_spacing_value": 1.15,
            "alignment": "left", "level": 0, "list_kind": "none",
            "first_line_indent_pt": 0.0, "left_indent_pt": 0.0,
        })
    if not out:
        out.append({
            "id": f"{cell.id}-p0",
            "runs": [{"id": f"{cell.id}-r0", "text": "",
                      "style": dict(run_style)}],
            "space_before_pt": 0.0, "space_after_pt": 0.0,
            "line_spacing_mode": "multiple", "line_spacing_value": 1.15,
            "alignment": "left", "level": 0, "list_kind": "none",
            "first_line_indent_pt": 0.0, "left_indent_pt": 0.0,
        })
    return out


# ---------------------------------------------------------------------------
# §8 tables: measure the grid, not a text screenshot
# ---------------------------------------------------------------------------

def layout_table(payload: Any, box_pt: dict[str, float],
                 styles: dict[str, dict[str, Any]],
                 measurer: Measurer, options: dict[str, Any]) -> dict[str, Any]:
    """Resolve column widths / row heights for the full physical grid."""
    rows = int(getattr(payload, "rows", 0) or 0)
    cols = int(getattr(payload, "cols", 0) or 0)
    if rows <= 0 or cols <= 0:
        return {"ok": False, "issues": ["TABLE_EMPTY_GRID"],
                "column_widths_pt": [], "row_heights_pt": []}
    cells = list(getattr(payload, "cells", []) or [])
    by_pos: dict[tuple[int, int], Any] = {}
    for cell in cells:
        by_pos[(int(cell.row), int(cell.col))] = cell
    # grid/merge consistency
    issues: list[str] = []
    for merge in list(getattr(payload, "merges", []) or []):
        origin = by_pos.get((merge.r0, merge.c0))
        if origin is None or not getattr(origin, "is_merge_origin", False):
            issues.append(f"TABLE_MERGE_NO_ORIGIN: ({merge.r0},{merge.c0})")

    header_rows = set(getattr(payload, "header_row_ids", []) or [])
    origins: list[Any] = []
    for cell in cells:
        if getattr(cell, "is_spanned", False) and not getattr(
                cell, "is_merge_origin", False):
            continue
        origins.append(cell)

    profile_kind = str(options.get("profile_kind", "production"))
    font_assets = options.get("font_assets", [])
    allowlist = options.get("font_allowlist_roots", [])
    debug_dir = str(options.get("debug_output_dir", ""))
    components: list[MeasureComponent] = []
    for cell in origins:
        role = "body"
        if int(cell.row) in header_rows:
            role = "dense_body_or_caption"
        style = styles.get(role) or styles.get("body") or {}
        components.append(MeasureComponent(
            id=f"cell-{cell.row}-{cell.col}", kind="table_cell",
            payload_ref=getattr(cell, "id", ""),
            paragraphs=_cell_paragraphs(cell, style),
            box_pt={"x": box_pt["x"], "y": box_pt["y"],
                    "w": box_pt["w"], "h": box_pt["h"]},
            margins_pt={"l": 3.6, "r": 3.6, "t": 1.8, "b": 1.8},
            vertical_anchor="top", wrap_policy="wrap", rotation_deg=0.0,
            list_preview={}))
    if not components:
        return {"ok": False, "issues": [*issues, "TABLE_NO_VISIBLE_CELLS"],
                "column_widths_pt": [], "row_heights_pt": []}
    request = MeasurementRequest(
        schema_version="1.0", request_id=f"table-{id(payload):x}",
        canvas_pt={"w": 960.0, "h": 540.0}, css_px_per_in=96.0,
        profile_kind=profile_kind,  # type: ignore[arg-type]
        components=components, font_assets=font_assets,
        engine_options={"timeout_ms": 120000, "device_scale_factor": 1,
                        "allowlist_roots": allowlist},
        input_hashes={}, debug_output_dir=debug_dir)
    response = measurer(request)
    measured = {i["component_id"]: i for i in response.get("items", [])}

    span_of = {(c.row, c.col): (int(c.row_span or 1), int(c.col_span or 1))
               for c in origins}
    # column minima from non-spanning cells; spans constrain sums
    col_min = [0.0] * cols
    col_pref = [0.0] * cols
    span_reqs: list[tuple[int, int, float, float]] = []
    for cell in origins:
        item = measured.get(f"cell-{cell.row}-{cell.col}", {})
        rs, cs = span_of.get((cell.row, cell.col), (1, 1))
        mn = float(item.get("intrinsic_min_width_pt", 0.0) or 0.0)
        pf = float(item.get("intrinsic_preferred_width_pt", 0.0) or 0.0)
        if cs == 1:
            col_min[cell.col] = max(col_min[cell.col], mn)
            col_pref[cell.col] = max(col_pref[cell.col], pf)
        else:
            span_reqs.append((cell.col, cs, mn, pf))
    col_min = [max(v, 12.0) for v in col_min]
    for start, span, mn, pf in span_reqs:
        have_min = sum(col_min[start:start + span])
        if have_min < mn:
            deficit = mn - have_min
            for c in range(start, start + span):
                col_min[c] += deficit / span
        have_pref = sum(col_pref[start:start + span])
        if have_pref < pf:
            deficit = pf - have_pref
            for c in range(start, start + span):
                col_pref[c] += deficit / span

    from .layout import distribute_extent

    gaps: list[float] = []
    widths = distribute_extent(
        box_pt["w"],
        [{"id": str(c), "minimum": col_min[c], "preferred": col_pref[c],
          "weight": 1.0} for c in range(cols)], gaps)
    if widths is None:
        return {"ok": False,
                "issues": [*issues, "TABLE_MINIMA_EXCEED_WIDTH"],
                "column_widths_pt": [], "row_heights_pt": []}
    col_widths = [widths[str(c)] for c in range(cols)]

    # heights at assigned spanned widths
    comps2: list[MeasureComponent] = []
    for cell in origins:
        rs, cs = span_of.get((cell.row, cell.col), (1, 1))
        w = sum(col_widths[cell.col:cell.col + cs])
        role = "body"
        if int(cell.row) in header_rows:
            role = "dense_body_or_caption"
        style = styles.get(role) or styles.get("body") or {}
        comps2.append(MeasureComponent(
            id=f"cell-{cell.row}-{cell.col}", kind="table_cell",
            payload_ref=getattr(cell, "id", ""),
            paragraphs=_cell_paragraphs(cell, style),
            box_pt={"x": 0.0, "y": 0.0, "w": w, "h": box_pt["h"]},
            margins_pt={"l": 3.6, "r": 3.6, "t": 1.8, "b": 1.8},
            vertical_anchor="top", wrap_policy="wrap", rotation_deg=0.0,
            list_preview={}))
    request2 = request.model_copy(update={
        "request_id": f"table-{id(payload):x}-rows", "components": comps2})
    response2 = measurer(request2)
    measured2 = {i["component_id"]: i for i in response2.get("items", [])}

    row_heights = [0.0] * rows
    vspan_reqs: list[tuple[int, int, float]] = []
    for cell in origins:
        item = measured2.get(f"cell-{cell.row}-{cell.col}", {})
        need = float(item.get("occupied_height_pt", 0.0) or 0.0) + 3.6
        rs, cs = span_of.get((cell.row, cell.col), (1, 1))
        if item.get("overflow_x"):
            issues.append(f"TABLE_CELL_OVERFLOW_X: ({cell.row},{cell.col})")
        if rs == 1:
            row_heights[cell.row] = max(row_heights[cell.row], need)
        else:
            vspan_reqs.append((cell.row, rs, need))
    row_heights = [max(v, 12.0) for v in row_heights]
    for start, span, need in vspan_reqs:
        have = sum(row_heights[start:start + span])
        if have < need:
            deficit = need - have
            for r in range(start, start + span):
                row_heights[r] += deficit / span

    total_h = sum(row_heights)
    if total_h > box_pt["h"] + 0.75:
        issues.append(f"TABLE_HEIGHT_EXCEEDS_BOX: need {total_h:.1f}pt of "
                      f"{box_pt['h']:.1f}pt")
    return {"ok": not any("EXCEEDS" in i or "MINIMA" in i for i in issues),
            "column_widths_pt": col_widths,
            "row_heights_pt": row_heights,
            "merge_ranges": [
                {"r0": m.r0, "c0": m.c0, "r1": m.r1, "c1": m.c1}
                for m in list(getattr(payload, "merges", []) or [])],
            "total_pt": {"w": sum(col_widths), "h": total_h},
            "cell_issues": issues}


# ---------------------------------------------------------------------------
# §9 graphs: measured nodes, SCC/layers, orthogonal routes
# ---------------------------------------------------------------------------

def _strongly_connected(nodes: list[str],
                        edges: list[tuple[str, str]]) -> list[list[str]]:
    """Tarjan SCC (iterative, deterministic source-order ties)."""
    adj: dict[str, list[str]] = {n: [] for n in nodes}
    for a, b in edges:
        if a in adj and b in adj:
            adj[a].append(b)
    index_of: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    sccs: list[list[str]] = []
    counter = [0]
    for root in nodes:
        if root in index_of:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, child_i = work[-1]
            if child_i == 0 and node not in index_of:
                index_of[node] = low[node] = counter[0]
                counter[0] += 1
                stack.append(node)
                on_stack.add(node)
            advanced = False
            for i in range(child_i, len(adj[node])):
                child = adj[node][i]
                if child not in index_of:
                    work[-1] = (node, i + 1)
                    work.append((child, 0))
                    advanced = True
                    break
                elif child in on_stack:
                    low[node] = min(low[node], index_of[child])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index_of[node]:
                comp = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    comp.append(member)
                    if member == node:
                        break
                sccs.append(sorted(comp))
    return sorted(sccs, key=lambda c: c[0])


def _manhattan_route(start: tuple[float, float], end: tuple[float, float],
                     obstacles: list[dict[str, float]],
                     box_pt: dict[str, float],
                     clearance: float = 7.2) -> list[tuple[float, float]]:
    """Bounded orthogonal A* over a coarse grid (deterministic)."""
    gx, gy = 48, 36
    x0, y0, w, h = box_pt["x"], box_pt["y"], box_pt["w"], box_pt["h"]
    if w <= 0 or h <= 0:
        return [start, end]
    cw, ch = w / gx, h / gy

    def blocked(ix: int, iy: int) -> bool:
        cx = x0 + (ix + 0.5) * cw
        cy = y0 + (iy + 0.5) * ch
        for ob in obstacles:
            if (ob["x"] - clearance <= cx <= ob["x"] + ob["w"] + clearance
                    and ob["y"] - clearance <= cy <= ob["y"] + ob["h"] + clearance):
                return True
        return False

    def cell(pt: tuple[float, float]) -> tuple[int, int]:
        return (min(gx - 1, max(0, int((pt[0] - x0) / cw))),
                min(gy - 1, max(0, int((pt[1] - y0) / ch))))

    import heapq

    start_c, end_c = cell(start), cell(end)
    open_heap: list[tuple[float, int, tuple[int, int], str]] = [(0.0, 0, start_c, "")]
    came: dict[tuple[int, int], tuple[tuple[int, int], str]] = {}
    cost: dict[tuple[int, int], float] = {start_c: 0.0}
    counter = [0]
    found = False
    visited = 0
    while open_heap and visited < 20000:
        _, _, cur, cur_dir = heapq.heappop(open_heap)
        visited += 1
        if cur == end_c:
            found = True
            break
        for nxt_dir, (dx, dy) in (("E", (1, 0)), ("W", (-1, 0)),
                                  ("S", (0, 1)), ("N", (0, -1))):
            nxt = (cur[0] + dx, cur[1] + dy)
            if not (0 <= nxt[0] < gx and 0 <= nxt[1] < gy):
                continue
            if nxt != end_c and blocked(*nxt):
                continue
            step = 1.0 + (0.4 if cur_dir and nxt_dir != cur_dir else 0.0)
            new_cost = cost[cur] + step
            if nxt not in cost or new_cost < cost[nxt] - 1e-9:
                cost[nxt] = new_cost
                heuristic = abs(nxt[0] - end_c[0]) + abs(nxt[1] - end_c[1])
                counter[0] += 1
                heapq.heappush(open_heap, (new_cost + heuristic, counter[0], nxt, nxt_dir))
                came[nxt] = (cur, nxt_dir)
    if not found:
        return [start, end]
    cells = [end_c]
    while cells[-1] != start_c:
        cells.append(came[cells[-1]][0])
    cells.reverse()
    # compress straight runs, emit waypoints at direction changes
    points = [start]
    prev_dir = ""
    for i in range(1, len(cells)):
        direction = ("E" if cells[i][0] > cells[i - 1][0]
                     else "W" if cells[i][0] < cells[i - 1][0]
                     else "S" if cells[i][1] > cells[i - 1][1] else "N")
        if direction != prev_dir and i > 1:
            corner = (x0 + (cells[i - 1][0] + 0.5) * cw,
                      y0 + (cells[i - 1][1] + 0.5) * ch)
            points.append(corner)
        prev_dir = direction
    points.append(end)
    return points


def layout_graph(nodes: list[dict[str, Any]], edges: list[dict[str, Any]],
                 box_pt: dict[str, float], styles: dict[str, dict[str, Any]],
                 measurer: Measurer, options: dict[str, Any]) -> dict[str, Any]:
    """Place measured nodes (DAG layers / SCC bands) and route connectors."""
    issues: list[str] = []
    node_ids = [str(n.get("id", "")) for n in nodes]
    if not node_ids:
        return {"ok": False, "issues": ["GRAPH_NO_NODES"], "node_boxes": {},
                "routes": []}
    edge_pairs = [(str(e.get("from", "")), str(e.get("to", ""))) for e in edges]
    sccs = _strongly_connected(node_ids, edge_pairs)
    cyclic = [s for s in sccs if len(s) > 1 or
              any(a == b == s[0] for a, b in edge_pairs if a in s)]

    profile_kind = str(options.get("profile_kind", "production"))
    font_assets = options.get("font_assets", [])
    allowlist = options.get("font_allowlist_roots", [])
    debug_dir = str(options.get("debug_output_dir", ""))
    style = styles.get("body") or {}
    components: list[MeasureComponent] = []
    for node in nodes:
        text = str(node.get("label", ""))
        components.append(MeasureComponent(
            id=f"node-{node.get('id', '')}", kind="graph_label",
            payload_ref=str(node.get("id", "")),
            paragraphs=[{
                "id": f"node-{node.get('id', '')}-p0",
                "runs": [{"id": f"node-{node.get('id', '')}-r0",
                          "text": text, "style": dict(style)}],
                "space_before_pt": 0.0, "space_after_pt": 0.0,
                "line_spacing_mode": "multiple", "line_spacing_value": 1.15,
                "alignment": "center", "level": 0, "list_kind": "none",
                "first_line_indent_pt": 0.0, "left_indent_pt": 0.0,
            }],
            box_pt={"x": box_pt["x"], "y": box_pt["y"],
                    "w": box_pt["w"], "h": box_pt["h"]},
            margins_pt={"l": 7.2, "r": 7.2, "t": 7.2, "b": 7.2},
            vertical_anchor="top", wrap_policy="wrap", rotation_deg=0.0,
            list_preview={}))
    request = MeasurementRequest(
        schema_version="1.0", request_id="graph-labels",
        canvas_pt={"w": 960.0, "h": 540.0}, css_px_per_in=96.0,
        profile_kind=profile_kind,  # type: ignore[arg-type]
        components=components, font_assets=font_assets,
        engine_options={"timeout_ms": 120000, "device_scale_factor": 1,
                        "allowlist_roots": allowlist},
        input_hashes={}, debug_output_dir=debug_dir)
    response = measurer(request)
    measured = {i["component_id"]: i for i in response.get("items", [])}

    pad = 14.4
    node_size: dict[str, dict[str, float]] = {}
    for node in nodes:
        item = measured.get(f"node-{node.get('id', '')}", {})
        node_size[str(node.get("id", ""))] = {
            "w": max(float(item.get("intrinsic_preferred_width_pt", 60.0) or 60.0) + pad * 2, 60.0),
            "h": max(float(item.get("occupied_height_pt", 20.0) or 20.0) + pad * 2, 36.0),
        }

    # condensation DAG -> longest-path layers
    scc_of = {n: i for i, s in enumerate(sccs) for n in s}
    dag_edges = {(scc_of[a], scc_of[b]) for a, b in edge_pairs
                 if a in scc_of and b in scc_of and scc_of[a] != scc_of[b]}
    layer = dict.fromkeys(range(len(sccs)), 0)
    for _ in range(len(sccs) + 1):
        for a, b in sorted(dag_edges):
            if layer[b] < layer[a] + 1:
                layer[b] = layer[a] + 1
    max_layer = max(layer.values()) if layer else 0
    gap = 21.6
    band_h = (box_pt["h"] - gap * max(max_layer, 1)) / max(max_layer + 1, 1)
    node_boxes: dict[str, dict[str, float]] = {}
    for i, scc in enumerate(sccs):
        members = list(scc)
        total_w = sum(node_size[n]["w"] for n in members) + gap * (len(members) - 1)
        x = box_pt["x"] + max((box_pt["w"] - total_w) / 2.0, 0.0)
        y = box_pt["y"] + layer[i] * (band_h + gap)
        for n in members:
            node_boxes[n] = {"x": x, "y": y, "w": node_size[n]["w"],
                             "h": min(node_size[n]["h"], band_h)}
            x += node_size[n]["w"] + gap

    # routes (obstacles = all unrelated node boxes + protected regions)
    obstacles = list(node_boxes.values()) + list(
        options.get("route_obstacles", []) or [])
    routes: list[dict[str, Any]] = []
    lane = [0]
    for edge in edges:
        src, dst = str(edge.get("from", "")), str(edge.get("to", ""))
        sb, db = node_boxes.get(src), node_boxes.get(dst)
        if sb is None or db is None:
            issues.append(f"GRAPH_EDGE_DANGLING: {edge.get('id', '')}")
            continue
        if src == dst:
            routes.append({"edge_id": str(edge.get("id", "")),
                           "from": src, "to": dst, "kind": "self_loop",
                           "points": [
                               [sb["x"] + sb["w"], sb["y"] + sb["h"] / 2],
                               [sb["x"] + sb["w"] + 14.4, sb["y"] + sb["h"] / 2],
                               [sb["x"] + sb["w"] + 14.4, sb["y"] - 14.4],
                               [sb["x"] + sb["w"] / 2, sb["y"] - 14.4],
                               [sb["x"] + sb["w"] / 2, sb["y"]]]})
            continue
        start = (sb["x"] + sb["w"] / 2, sb["y"] + sb["h"] / 2)
        end = (db["x"] + db["w"] / 2, db["y"] + db["h"] / 2)
        lane[0] += 1
        pts = _manhattan_route(start, end, obstacles, box_pt)
        routes.append({"edge_id": str(edge.get("id", "")),
                       "from": src, "to": dst,
                       "kind": str(edge.get("kind", "association")),
                       "points": [[round(px, 2), round(py, 2)] for px, py in pts],
                       "lane": lane[0]})
    return {"ok": True, "node_boxes": node_boxes, "routes": routes,
            "sccs": sccs, "cyclic": cyclic,
            "node_sizes_pt": node_size, "issues": issues}


# ---------------------------------------------------------------------------
# §10.1 chart exhibits: style-only geometry, honest unknown scopes
# ---------------------------------------------------------------------------

def layout_chart_exhibit(payload: Any, box_pt: dict[str, float],
                         styles: dict[str, dict[str, Any]],
                         measurer: Measurer,
                         options: dict[str, Any]) -> dict[str, Any]:
    """Measure exhibit labels; never touch data/axes/plots.

    Category/series/title/legend strings are measured as chart_label items
    for envelope evidence. Axis auto ticks and automatic data-label placement
    decided by the native renderer stay estimated/native_render_required:
    string measurement alone never yields a whole-chart pass.
    """
    labels: list[tuple[str, str]] = []
    for key in ("categories", "series_names", "legend_entries"):
        for i, text in enumerate(list(getattr(payload, key, []) or [])):
            labels.append((f"{key}-{i}", str(text)))
    title = getattr(payload, "title", "") or ""
    if title:
        labels.append(("title", str(title)))
    style = styles.get("native_chart_label") or styles.get("body") or {}
    profile_kind = str(options.get("profile_kind", "production"))
    components = [MeasureComponent(
        id=f"chart-{key}", kind="chart_label", payload_ref=key,
        paragraphs=[{
            "id": f"chart-{key}-p0",
            "runs": [{"id": f"chart-{key}-r0", "text": text,
                      "style": dict(style)}],
            "space_before_pt": 0.0, "space_after_pt": 0.0,
            "line_spacing_mode": "multiple", "line_spacing_value": 1.15,
            "alignment": "left", "level": 0, "list_kind": "none",
            "first_line_indent_pt": 0.0, "left_indent_pt": 0.0}],
        box_pt={"x": box_pt["x"], "y": box_pt["y"],
                "w": box_pt["w"], "h": box_pt["h"]},
        margins_pt={}, vertical_anchor="top", wrap_policy="wrap",
        rotation_deg=0.0, list_preview={}) for key, text in labels]
    envelopes: dict[str, dict[str, float]] = {}
    if components:
        request = MeasurementRequest(
            schema_version="1.0", request_id="chart-labels",
            canvas_pt={"w": 960.0, "h": 540.0}, css_px_per_in=96.0,
            profile_kind=profile_kind,  # type: ignore[arg-type]
            components=components,
            font_assets=options.get("font_assets", []),
            engine_options={"timeout_ms": 120000, "device_scale_factor": 1,
                            "allowlist_roots": options.get(
                                "font_allowlist_roots", [])},
            input_hashes={},
            debug_output_dir=str(options.get("debug_output_dir", "")))
        response = measurer(request)
        for item in response.get("items", []):
            envelopes[item["component_id"]] = {
                "w": float(item.get("intrinsic_preferred_width_pt", 0.0) or 0.0),
                "h": float(item.get("occupied_height_pt", 0.0) or 0.0)}
    return {"ok": True,
            "graphic_frame_box_pt": dict(box_pt),
            "label_envelopes_pt": envelopes,
            "checked_scopes": ["label_string_sizes"],
            "unsupported_scopes": ["axis_auto_ticks",
                                   "automatic_data_label_placement",
                                   "plot_geometry"],
            "workbook_touched": False,
            "issues": []}


# ---------------------------------------------------------------------------
# §10.2 images: identity + aspect preserved, explicit fit contract
# ---------------------------------------------------------------------------

def layout_image_region(payload: Any, box_pt: dict[str, float],
                        options: dict[str, Any]) -> dict[str, Any]:
    """Keep media identity and aspect ratio; permitted crop only from policy."""
    natural = options.get("image_natural_size_pt") or {}
    nw = float(natural.get("w", 0) or 0)
    nh = float(natural.get("h", 0) or 0)
    crop = tuple(options.get("permitted_crop") or (0.0, 0.0, 0.0, 0.0))
    if len(crop) != 4:
        crop = (0.0, 0.0, 0.0, 0.0)
    if any(v < 0 or v >= 0.5 for v in crop):
        return {"ok": False, "box_pt": dict(box_pt),
                "issues": ["IMAGE_CROP_REFUSED: crop must come from policy "
                           "and stay below 50% per edge"]}
    aspect = (nw / nh) if nw > 0 and nh > 0 else 0.0
    fit = str(options.get("image_fit", "fit"))
    out_box = dict(box_pt)
    if aspect > 0 and fit == "fit":
        box_ratio = box_pt["w"] / box_pt["h"] if box_pt["h"] > 0 else 0.0
        if box_ratio > 0:
            if aspect > box_ratio:
                out_box["h"] = box_pt["w"] / aspect
            else:
                out_box["w"] = box_pt["h"] * aspect
    return {"ok": True, "box_pt": out_box,
            "aspect": aspect, "crop": list(crop), "fit": fit,
            "media_touched": False, "issues": []}


# ---------------------------------------------------------------------------
# §10.3 annotations: measured, adjacent, leader-checked
# ---------------------------------------------------------------------------

def place_annotation(annotation: dict[str, Any], target: dict[str, float],
                     box_pt: dict[str, float],
                     styles: dict[str, dict[str, Any]],
                     measurer: Measurer,
                     options: dict[str, Any]) -> dict[str, Any]:
    """Deterministic candidate placements around the target; the first
    contained, non-overlapping slot wins with a straight leader."""
    text = str(annotation.get("text", ""))
    style = styles.get("footnote") or styles.get("body") or {}
    profile_kind = str(options.get("profile_kind", "production"))
    component = MeasureComponent(
        id="annotation", kind="text", payload_ref="annotation",
        paragraphs=[{
            "id": "annotation-p0",
            "runs": [{"id": "annotation-r0", "text": text,
                      "style": dict(style)}],
            "space_before_pt": 0.0, "space_after_pt": 0.0,
            "line_spacing_mode": "multiple", "line_spacing_value": 1.15,
            "alignment": "left", "level": 0, "list_kind": "none",
            "first_line_indent_pt": 0.0, "left_indent_pt": 0.0}],
        box_pt={"x": box_pt["x"], "y": box_pt["y"], "w": box_pt["w"] / 3,
                "h": box_pt["h"]},
        margins_pt={"l": 3.6, "r": 3.6, "t": 3.6, "b": 3.6},
        vertical_anchor="top", wrap_policy="wrap", rotation_deg=0.0,
        list_preview={})
    request = MeasurementRequest(
        schema_version="1.0", request_id="annotation",
        canvas_pt={"w": 960.0, "h": 540.0}, css_px_per_in=96.0,
        profile_kind=profile_kind,  # type: ignore[arg-type]
        components=[component], font_assets=options.get("font_assets", []),
        engine_options={"timeout_ms": 120000, "device_scale_factor": 1,
                        "allowlist_roots": options.get(
                            "font_allowlist_roots", [])},
        input_hashes={},
        debug_output_dir=str(options.get("debug_output_dir", "")))
    response = measurer(request)
    items = response.get("items", [])
    if not items:
        return {"ok": False, "issues": ["ANNOTATION_UNMEASURED"]}
    item = items[0]
    need_w = max(float(item.get("intrinsic_preferred_width_pt", 0) or 0),
                 36.0) + 7.2
    need_h = max(float(item.get("occupied_height_pt", 0) or 0), 12.0) + 7.2
    obstacles = list(options.get("annotation_obstacles", []) or [])
    tx, ty, tw, th = target["x"], target["y"], target["w"], target["h"]

    def overlaps(x: float, y: float) -> bool:
        for ob in obstacles:
            if (x < ob["x"] + ob["w"] and ob["x"] < x + need_w
                    and y < ob["y"] + ob["h"] and ob["y"] < y + need_h):
                return True
        return False

    def inside(x: float, y: float) -> bool:
        return (x >= box_pt["x"] and y >= box_pt["y"]
                and x + need_w <= box_pt["x"] + box_pt["w"] + 0.75
                and y + need_h <= box_pt["y"] + box_pt["h"] + 0.75)

    candidates = [
        ("right", tx + tw + 7.2, ty),
        ("below", tx, ty + th + 7.2),
        ("left", tx - need_w - 7.2, ty),
        ("above", tx, ty - need_h - 7.2),
    ]
    for name, x, y in candidates:
        if inside(x, y) and not overlaps(x, y):
            return {"ok": True, "slot": name,
                    "box_pt": {"x": x, "y": y, "w": need_w, "h": need_h},
                    "leader": {"from": [x, y + need_h / 2],
                               "to": [tx + tw / 2, ty + th / 2]},
                    "measured": {"w": need_w, "h": need_h}, "issues": []}
    return {"ok": False,
            "issues": ["ANNOTATION_NO_SLOT: no contained non-overlapping "
                       "placement; needs_review, not a silent move"]}


# ---------------------------------------------------------------------------
# preserve-structure shapes: source box kept, text re-measured in place
# ---------------------------------------------------------------------------

def resolve_shape_preserve(node: Any, obj: Any,
                           run_style: dict[str, Any]) -> MeasureComponent:
    """A vector shape keeps its source box; its text is measured with the
    corporate role style inside that box (overflow becomes an issue, never
    a silent crop)."""
    box = getattr(obj, "local_box", None)
    if box is not None:
        box_pt = {"x": _emu_to_pt(box.x), "y": _emu_to_pt(box.y),
                  "w": max(_emu_to_pt(box.w), 1.0),
                  "h": max(_emu_to_pt(box.h), 1.0)}
    else:
        box_pt = {"x": 36.0, "y": 36.0, "w": 648.0, "h": 100.0}
    payload = getattr(obj, "payload", None)
    text_payload = getattr(payload, "text", None)
    paragraphs: list[dict[str, Any]] = []
    if text_payload is not None:
        for para in list(getattr(text_payload, "paragraphs", []) or []):
            runs = [{"id": str(getattr(r, "id", "")),
                     "text": getattr(r, "text", "") or "",
                     "style": dict(run_style)}
                    for r in list(getattr(para, "runs", []) or [])]
            if runs:
                paragraphs.append({
                    "id": str(getattr(para, "id", "")),
                    "runs": runs,
                    "space_before_pt": 0.0, "space_after_pt": 0.0,
                    "line_spacing_mode": "multiple",
                    "line_spacing_value": 1.15,
                    "alignment": "left", "level": 0, "list_kind": "none",
                    "first_line_indent_pt": 0.0, "left_indent_pt": 0.0})
    return MeasureComponent(
        id=getattr(node, "id", "shape"), kind="text",
        payload_ref=getattr(node, "payload_ref", ""),
        paragraphs=paragraphs,
        box_pt=box_pt,
        margins_pt={"l": 7.2, "r": 7.2, "t": 3.6, "b": 3.6},
        vertical_anchor="top", wrap_policy="wrap", rotation_deg=0.0,
        list_preview={})


def resolve_image_preserve(node: Any, obj: Any) -> MeasureComponent:
    """An image keeps its source box; no text to measure."""
    box = getattr(obj, "local_box", None)
    if box is not None:
        box_pt = {"x": _emu_to_pt(box.x), "y": _emu_to_pt(box.y),
                  "w": max(_emu_to_pt(box.w), 1.0),
                  "h": max(_emu_to_pt(box.h), 1.0)}
    else:
        box_pt = {"x": 36.0, "y": 36.0, "w": 200.0, "h": 150.0}
    return MeasureComponent(
        id=getattr(node, "id", "image"), kind="text",
        payload_ref=getattr(node, "payload_ref", ""),
        paragraphs=[],
        box_pt=box_pt, margins_pt={},
        vertical_anchor="top", wrap_policy="wrap", rotation_deg=0.0,
        list_preview={})
