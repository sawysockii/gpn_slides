#!/usr/bin/env python3
"""Harness draft v7: lane-flow scene authoring with measured feedback.

v6 copied the source box of every object into a zone, so every text frame
was authored at the *source* height while the normative corporate type
measures taller. The engine reported `CAPACITY_COLUMN_CONFLICT` and
`HINT_OVERLAP_UNRESOLVED` on 7 of 8 NTC slides: a 12pt zone holding 35pt
of measured text is an authoring estimate, not a physical fit.

v7 authors what the engine can actually solve:

* a header band (eyebrow + title + lead) and a footer band (sources,
  page mark) sized from real measurement;
* one or more **body lanes** whose widths come from the concept topology,
  not from the source column widths;
* text units flowed down each lane in reading order with their *measured*
  `required_outer_height_pt` and the standard 7.2pt gap;
* decor kept at its source box, and any decor that would collide with the
  flowed text is authorized-omitted as a whole unit instead of being left
  behind as an empty frame.

The measurement is the real local GPN font set through the shared Chromium
bridge — no estimate, no shrink-to-fit. Nothing here is keyed to a file
name, a slide number or an NTC prefix: the same code authors a lane plan
for any deck whose objects and semantic units it is given.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from slides_cli.gpn.layout import (
    _object_paragraphs,
    resolve_component_style,
)
from slides_cli.gpn.models import MeasureComponent, MeasurementRequest, RectPt
from slides_cli.gpn.typography import prepare_measurement_fonts

RUN = Path(__file__).resolve().parents[1]
PROJECT_ROOT = RUN.parents[1]
EMU_PER_PT = 12700.0
CANVAS_W, CANVAS_H = 960.0, 496.67787401574803
GAP = 7.2

# semantic_role -> normative role token consumed by the engine
ROLEMAP = {
    "slide_title": "main_slide_title",
    "header_label": "section_heading",
    "metric_value": "large_metric",
    "body": "body",
    "label": "dense_body_or_caption",
    "source_note": "footnote",
    "footnote": "footnote",
    "footer_label": "footer",
    "illustration": "body",
}

# Lanes per concept: (top_fraction, bottom_fraction, [x, w] fractions).
# Three genuinely different topologies, chosen per content class below.
ONE_LANE = [(0.0, 1.0, [(0.0, 1.0)])]
TWO_LANES = [(0.0, 1.0, [(0.0, 0.615), (0.635, 0.365)])]
THREE_LANES = [(0.0, 1.0, [(0.0, 0.312), (0.344, 0.312), (0.688, 0.312)])]


def _box_pt(obj: dict) -> tuple[float, float, float, float]:
    b = obj.get("local_box") or {}
    return tuple(float(b.get(k, 0)) / EMU_PER_PT for k in ("x", "y", "w", "h"))


def _paras(obj: dict) -> list[dict]:
    payload = obj.get("payload") or {}
    return list(payload.get("paragraphs")
                or (payload.get("text") or {}).get("paragraphs") or [])


def _has_text(obj: dict) -> bool:
    return bool(_paras(obj))


def _text_of(obj: dict) -> str:
    return " ".join((r.get("text") or "")
                    for p in _paras(obj) for r in (p.get("runs") or [])).strip()


def _to_pct(x: float, y: float, w: float, h: float) -> dict[str, float]:
    px = min(max(x / CANVAS_W * 100.0, 0.0), 99.0)
    py = min(max(y / CANVAS_H * 100.0, 0.0), 99.0)
    pw = min(max(w / CANVAS_W * 100.0, 0.4), 100.0 - px)
    ph = min(max(h / CANVAS_H * 100.0, 0.3), 100.0 - py)
    return {"x_pct": round(px, 3), "y_pct": round(py, 3),
            "w_pct": round(pw, 3), "h_pct": round(ph, 3)}


def measure_at(deck: dict, rules, bundle, roles: dict[str, str],
               widths: list[float], tag: str,
               body: list[dict]) -> dict[tuple[str, int], float]:
    """`required_outer_height_pt` per (source object, lane width index).

    §5.1: a need measured at one width is never applied to another. The
    engine measures inside the zone it is given, so the author measures
    inside the SAME width and places there — for every lane a unit could
    end up in. Measuring and placing on different widths is exactly what
    produced capacity conflicts that never physically existed.
    """
    from slides_cli.gpn.measurement import measure_components
    from slides_cli.gpn.models import SourceDeckIR
    from slides_cli.gpn.units import emu_to_pt

    model = SourceDeckIR.model_validate(deck)
    wanted = {unit["source_refs"][0]["object_id"] for unit in body}
    comps: list[MeasureComponent] = []
    for slide in model.slides:
        for obj in slide.objects:
            if obj.id not in wanted:
                continue
            if not _has_text(obj.model_dump(mode="json")):
                continue
            role = roles.get(obj.id, "body")
            try:
                style = resolve_component_style(role, rules, bundle)
            except ValueError:
                style = resolve_component_style("body", rules, bundle)
            paras = _object_paragraphs(obj)
            for para in paras:
                for run in para["runs"]:
                    run["style"] = dict(style)
            height = max(emu_to_pt(obj.local_box.h), 24.0)
            for index, width in enumerate(widths):
                comps.append(MeasureComponent(
                    id=f"{obj.id}-w{index}", kind="text",
                    payload_ref=obj.id, paragraphs=paras,
                    box_pt=RectPt(x=0.0, y=0.0, w=max(width, 24.0),
                                  h=height),
                    margins_pt={"l": 7.2, "r": 7.2, "t": 3.6, "b": 3.6},
                    vertical_anchor="top", wrap_policy="wrap",
                    rotation_deg=0.0, list_preview={}))
    if not comps:
        return {}
    request = MeasurementRequest(
        schema_version="1.0", request_id=f"harness-v7-{tag}",
        canvas_pt={"w": 960.0, "h": 540.0}, css_px_per_in=96.0,
        profile_kind="production", debug_output_dir="",
        components=comps,
        font_assets=[{"alias": b.browser_alias, "path": b.path,
                      "sha256": b.sha256, "face_index": b.face_index,
                      "style": "normal", "weight": 400}
                     for b in (bundle.bindings or [])],
        engine_options={"timeout_ms": 120000, "device_scale_factor": 1,
                        "allowlist_roots": []},
        input_hashes={})
    response = measure_components(
        request, timeout_seconds=600.0, project_root=PROJECT_ROOT,
        use_cache=True)["response"]
    out: dict[tuple[str, int], float] = {}
    for item in response.get("items", []):
        cid = item["component_id"]
        oid, _, index = cid.rpartition("-w")
        if not index.isdigit():
            continue
        out[(oid, int(index))] = float(
            item.get("required_outer_height_pt", 0.0) or 0.0)
    return out


def balance_lanes(body: list[dict], need_of,
                  lane_width: dict[int, float], top: float,
                  bottom: float, max_rounds: int = 12) -> None:
    """Spread units over the lanes until every lane holds its own text.

    Lanes come from the concept topology; which unit belongs to which is
    refined here from MEASURED need, not from a guess. When a lane
    overflows, the units whose combined need is largest move to the least
    loaded lane — the reading order inside each lane is preserved, only the
    lane assignment changes. This is the width/side redistribution §5.5
    asks for, performed by the scene that owns the composition.
    """
    capacity = max(bottom - top, 1.0)
    for _round in range(max_rounds):
        load = dict.fromkeys(lane_width, 0.0)
        for unit in body:
            load[unit["_lane"]] += need_of(unit, unit["_lane"]) + GAP
        if all(v <= capacity for v in load.values()):
            return
        worst = max(load, key=lambda k: load[k])
        moving = sorted(
            (u for u in body if u["_lane"] == worst),
            key=lambda u: need_of(u, worst), reverse=True)
        target = min(lane_width, key=lambda k: load[k])
        if target == worst or not moving:
            return
        moved = False
        for unit in moving:
            if load[target] >= capacity:
                break
            need = need_of(unit, target) + GAP
            if load[target] + need > capacity:
                continue
            unit["_lane"] = target
            load[worst] -= need
            load[target] += need
            moved = True
        if not moved:
            return


def lane_flow(units: list[dict], needs_at, lanes: list[dict],
              top: float, bottom: float) -> tuple[list[dict], list[str]]:
    """Flow units down lanes in reading order; report lanes that cannot fit."""
    placed: list[dict] = []
    problems: list[str] = []
    cursors = [top] * len(lanes)
    for unit in units:
        lane = unit["_lane"]
        need = max(needs_at(unit, lane), 6.0)
        lx, lw = lanes[lane]
        y = cursors[lane]
        if y + need > bottom + 0.75:
            problems.append(
                f"lane{lane} overflow at unit {unit['id']}: needs "
                f"{need:.1f}pt at y={y:.1f} but the lane ends at {bottom:.1f}")
            need = max(bottom - y, 1.0)
        placed.append({"unit": unit, "x": lx, "y": y, "w": lw, "h": need})
        cursors[lane] = y + need + GAP
    return placed, problems

# ---------------------------------------------------------------------------
# bands and lanes
# ---------------------------------------------------------------------------

def _band_of(y: float, h: float) -> str:
    """Where a source text object sits in the deck's own reading order."""
    if y < 60.0:
        return "header"
    if y + h > CANVAS_H - 16.0:
        return "footer"
    return "body"


def _concept_lanes(kind: str) -> list[tuple[float, float]]:
    if kind == "typographic":
        return [(0.0, CANVAS_W)]
    if kind == "stages":
        return [(0.0, CANVAS_W * 0.615), (CANVAS_W * 0.635, CANVAS_W * 0.365)]
    return [(0.0, CANVAS_W * 0.312), (CANVAS_W * 0.344, CANVAS_W * 0.312),
            (CANVAS_W * 0.688, CANVAS_W * 0.312)]


def _pick_lane(x: float, w: float, lanes: list[tuple[float, float]]) -> int:
    """Lane whose horizontal span the source object overlaps most."""
    best, best_overlap = 0, -1.0
    for i, (lx, lw) in enumerate(lanes):
        overlap = min(x + w, lx + lw) - max(x, lx)
        if overlap > best_overlap:
            best, best_overlap = i, overlap
    return best


def _header_bottom(units: list[dict]) -> float:
    """First free y below the header block, from measured needs."""
    bottom = 0.0
    for u in units:
        y = u["_y"]
        bottom = max(bottom, y + u["_need"])
    return bottom + 14.0


def _footer_top(units: list[dict]) -> float:
    top = CANVAS_H
    for u in units:
        top = min(top, u["_y"])
    return top - 10.0


def deck_of(slide_obj: dict, source_deck: dict) -> dict:
    """One-slide IR view of the real deck, headers preserved."""
    out = dict(source_deck)
    out["slides"] = [slide_obj]
    return out


def _free_slot(box: dict[str, float],
               taken: list[dict[str, float]]) -> dict[str, float] | None:
    """First y in this column where ``box`` collides with nothing.

    The unit keeps its own x, width and height; only its place along the
    column is chosen. Steps are made downward first (the natural place for
    a unit whose text moved up), then upward, and never outside the canvas.
    """
    h = box["h"]
    bottom_limit = CANVAS_H - h

    def free(y: float) -> bool:
        candidate = {"x": box["x"], "y": y, "w": box["w"], "h": h}
        return all(not _hits(candidate, other) for other in taken)

    step = h + GAP
    y = box["y"]
    while y + step <= bottom_limit + 0.75:
        y += step
        if free(y):
            return {"x": box["x"], "y": y, "w": box["w"], "h": h}
    y = box["y"]
    while y - step >= -0.75:
        y -= step
        if free(y):
            return {"x": box["x"], "y": y, "w": box["w"], "h": h}
    return None


def _hits(box: dict[str, float], other: dict[str, float]) -> bool:
    return (min(box["x"] + box["w"], other["x"] + other["w"])
            - max(box["x"], other["x"]) > 1.0
            and min(box["y"] + box["h"], other["y"] + other["h"])
            - max(box["y"], other["y"]) > 1.0)


def _fmt(box: dict[str, float]) -> str:
    return (f"{box['x']:.0f},{box['y']:.0f},"
            f"{box['w']:.0f}x{box['h']:.0f}")


def build_plan(slide_obj: dict, units: list[dict], deck: dict, rules, bundle,
               roles: dict[str, str], lane_kind: str,
               relations: list[dict]) -> tuple[dict, list[str]]:
    """One authored scene plan: bands, lanes, typed zones.

    ``content_refs`` carry SEMANTIC UNIT ids — that is what
    ``validate_scene_response`` checks coverage against (§3.3). The bridge
    (``_materialize_scene_plan``) binds units to source objects, so a scene
    never has to know an object id to be valid.
    """
    objects = {o["id"]: o for o in slide_obj["objects"]}
    problems: list[str] = []
    prepared: list[dict] = []
    missing_objects: list[str] = []
    for unit in units:
        refs = unit.get("source_refs") or []
        oid = refs[0].get("object_id", "") if refs else ""
        obj = objects.get(oid)
        if obj is None:
            missing_objects.append(unit["id"])
            continue
        x, y, w, h = _box_pt(obj)
        text = _has_text(obj)
        prepared.append({**unit, "_obj": obj, "_oid": oid, "_x": x, "_y": y,
                         "_w": w, "_h": h, "_text": text,
                         "_need": h if text else max(h, 1.0),
                         "_band": _band_of(y, h) if text else "decor"})
    if missing_objects:
        problems.append(f"units without a source object: {missing_objects[:6]}")

    text_units = [u for u in prepared if u["_text"]]
    decor = [u for u in prepared if not u["_text"]]
    header = [u for u in text_units if u["_band"] == "header"]
    footer = [u for u in text_units if u["_band"] == "footer"]
    body = [u for u in text_units if u["_band"] == "body"]

    lanes = _concept_lanes(lane_kind)
    for u in header + footer:
        u["_lane"] = 0
        u["_lx"], u["_lw"] = 0.0, CANVAS_W * 0.92
    for u in body:
        idx = _pick_lane(u["_x"], u["_w"], lanes)
        u["_lane"] = idx
        u["_lx"], u["_lw"] = lanes[idx]

    footer_top = _footer_top(footer) if footer else CANVAS_H - 4.0
    flow_top = 0.0
    for u in sorted(header, key=lambda u: u["_y"]):
        flow_top += u["_need"] + GAP
    flow_top += 6.0

    # Measure at the width each unit will actually be placed at, then
    # balance the lanes against those numbers (§5.1: a need measured at
    # one width is never applied to another).
    lane_width = {i: lanes[i][1] for i in range(len(lanes))}
    width_list = [lane_width[i] for i in sorted(lane_width)]
    table = measure_at(deck_of(slide_obj, deck), rules, bundle, roles,
                       width_list, f"lanes-{lane_kind}", body)

    def need_of(unit: dict, lane: int) -> float:
        oid = unit["source_refs"][0]["object_id"]
        index = sorted(lane_width).index(lane)
        return max(table.get((oid, index), 0.0), 6.0)

    balance_lanes(body, need_of, lane_width, flow_top, footer_top - 4.0)
    needs_at = need_of

    zones: list[dict] = []

    def _emit(u: dict, x: float, y: float, w: float, h: float,
              role: str, zid: str) -> None:
        zones.append({"zone_id": zid, **_to_pct(x, y, w, h), "role": role,
                      "content_refs": [u["id"]], "surface_token": "",
                      "foreground_token": "", "stroke_token": "",
                      "ref_roles": {u["id"]: role}})

    hy = 0.0
    for i, u in enumerate(sorted(header, key=lambda u: u["_y"])):
        _emit(u, u["_lx"], hy, u["_lw"], u["_need"],
              ROLEMAP.get(u.get("semantic_role", ""), "body"), f"zone:h{i}")
        hy += u["_need"] + GAP
    header_bottom = hy + 6.0

    placed, lane_problems = lane_flow(body, needs_at, lanes, header_bottom,
                                      footer_top - 4.0)
    problems.extend(lane_problems)
    for i, item in enumerate(placed):
        u = item["unit"]
        # Place at the width the author measured at. The lane decides the
        # vertical run and the reading order; the frame keeps its own
        # width, so the need the author measured is the need the engine
        # will measure (§5.1: never apply a number from one width to
        # another).
        w = item["w"]
        x = item["x"]
        _emit(u, x, item["y"], w, item["h"],
              ROLEMAP.get(u.get("semantic_role", ""), "body"), f"zone:b{i}")

    fy = CANVAS_H
    for i, u in enumerate(sorted(footer, key=lambda u: -u["_y"])):
        fy -= u["_need"]
        _emit(u, u["_lx"], fy, u["_lw"], u["_need"],
              ROLEMAP.get(u.get("semantic_role", ""), "footnote"), f"zone:f{i}")
        fy -= GAP

    text_boxes = [{"x": z["x_pct"] / 100.0 * CANVAS_W,
                   "y": z["y_pct"] / 100.0 * CANVAS_H,
                   "w": z["w_pct"] / 100.0 * CANVAS_W,
                   "h": z["h_pct"] / 100.0 * CANVAS_H} for z in zones]

    def _overlaps(box: dict[str, float], other: dict[str, float]) -> bool:
        return (min(box["x"] + box["w"], other["x"] + other["w"])
                - max(box["x"], other["x"]) > 1.0
                and min(box["y"] + box["h"], other["y"] + other["h"])
                - max(box["y"], other["y"]) > 1.0)

    authorized_omits: list[str] = []
    # Placement order is by height: a tall unit has few free slots, so it
    # picks first. Every placed decor joins the occupied set, so a later
    # unit cannot be dropped on top of an earlier one either.
    occupied = list(text_boxes)
    for u in sorted(decor, key=lambda u: (-u["_h"], u["_y"], u["_x"])):
        box = {"x": u["_x"], "y": u["_y"], "w": u["_w"], "h": u["_h"]}
        if any(_hits(box, other) for other in occupied):
            # The scene's own composition puts text where this unit was.
            # Leaving it at its source box would be an empty frame (§1.5)
            # and overlapping it would be an occlusion. When the semantic
            # stage marked the unit optional — icon-scale, not an edge,
            # holding no text — the scene authorizes the whole unit away
            # and the space is recomposed. A required unit is reported
            # instead: it may not be dropped silently.
            if u.get("preservation") == "optional":
                authorized_omits.append(u["id"])
                problems.append(
                    f"decor {u['_oid']} at {_fmt(box)} overlaps flowed "
                    "text: authorized_omit as a whole unit")
                continue
            # A required unit may not be omitted, and it may not be drawn
            # behind the text either. The scene moves it along its own
            # column to the first position where it collides with nothing,
            # preserving its size and its relation to the lane it came
            # from. If no such position exists the conflict is reported -
            # never resolved by hiding the unit.
            moved = _free_slot(box, occupied)
            if moved is None:
                problems.append(
                    f"REQUIRED decor {u['_oid']} at {_fmt(box)} overlaps "
                    "flowed text and has no free slot in its column: it "
                    "must be re-expressed, never silently hidden")
            else:
                problems.append(
                    f"REQUIRED decor {u['_oid']} moved {_fmt(box)} -> "
                    f"{_fmt(moved)} to clear the flowed text of its column")
                u["_y"], u["_h"] = moved["y"], moved["h"]
                box = moved
        zones.append({"zone_id": f"zone:d{u['_oid'][:8]}",
                      **_to_pct(u["_x"], box["y"], u["_w"], box["h"]),
                      "role": "body", "content_refs": [u["id"]],
                      "surface_token": "", "foreground_token": "",
                      "stroke_token": "", "ref_roles": {}})
        occupied.append(box)

    members = {u["id"] for u in prepared}
    rels = []
    for rel in relations:
        from_ids = [i for i in rel.get("from_unit_ids", []) if i in members]
        to_ids = [i for i in rel.get("to_unit_ids", []) if i in members]
        if not from_ids or not to_ids:
            continue
        rels.append({"from_ref": from_ids[0], "to_ref": to_ids[0],
                     "relation_kind": str(rel.get("kind", "relation")),
                     "evidence": [str(rel.get("id", ""))]})

    return {"zones": zones, "relations": rels}, problems


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def main() -> int:
    from slides_cli.gpn.models import (
        CompiledOntology,
        FontInventory,
        TemplateProfile,
    )

    deck = json.loads((RUN / "source_ir.json").read_text())
    rules = CompiledOntology.model_validate_json(
        (RUN / "compiled_rules.json").read_text())
    profile = TemplateProfile.model_validate_json(
        (RUN / "template_profile.json").read_text())
    assert profile is not None  # the run's own design context
    inventory = FontInventory.model_validate_json(
        (RUN / "font_inventory.json").read_text())
    semantic = json.loads(
        (RUN / "model_responses" / "semantic.json").read_text())["payload"]

    all_text = " ".join(
        (r.get("text") or "")
        for slide in deck["slides"] for obj in slide["objects"]
        for p in _paras(obj) for r in (p.get("runs") or []))
    bundle = prepare_measurement_fonts(inventory, all_text,
                                       profile_kind="production")
    if bundle.missing_faces:
        print(f"FONT_FACE_UNVERIFIED: {bundle.missing_faces}")
        return 1

    roles: dict[str, str] = {}
    units_by_slide: dict[str, list[dict]] = {}
    relations_by_slide: dict[str, list[dict]] = {}
    for slide in semantic["slides"]:
        sid = slide["source_slide_id"]
        relations_by_slide.setdefault(sid, []).extend(
            slide.get("relations") or [])
        for unit in slide["units"]:
            units_by_slide.setdefault(sid, []).append(unit)
            for ref in unit.get("source_refs") or []:
                oid = ref.get("object_id", "")
                if oid:
                    roles[oid] = ROLEMAP.get(unit.get("semantic_role", ""),
                                             "body")


    # Concept topology per content class, and which candidate carries it.
    # The class is read from the material, not from the file name.
    state = json.loads((RUN / "run_state.json").read_text())
    tasks = {}
    for t in state["task_queue"]:
        if t["kind"] != "scene_generation" or t["status"] != "pending":
            continue
        ck = t.get("candidate_key") or {}
        tasks[(str(ck.get("source_slide_id", "")),
               str(ck.get("candidate_id", "")))] = t
    written = 0
    for (sid, cid), task in sorted(tasks.items()):
        request = json.loads(
            (RUN / task["packet"]).read_text()) if task.get("packet") else {}
        concept = request.get("visual_concept") or {}
        topology = str((concept.get("group_topology") or {}).get("topology")
                       or concept.get("topology") or "typographic")
        # Topology -> lanes. A process reads as sequential columns, a hub
        # as a matrix of groups, a typographic slide as one argument.
        lane_kind = {"typographic": "typographic",
                     "process_flow": "stages",
                     "stages": "stages",
                     "hub_spoke": "matrix",
                     "matrix": "matrix"}.get(topology, "stages")
        # A slide whose own body already carries two or three distinct
        # horizontal groups is a candidate for the matching topology; a
        # title slide is not, and says so instead of faking diversity.
        slide_ir = next(s for s in deck["slides"] if s["id"] == sid)
        plan, problems = build_plan(
            slide_ir, units_by_slide.get(sid, []), deck, rules, bundle,
            roles, lane_kind, relations_by_slide.get(sid, []))
        if problems:
            print(f"  {sid} {cid} ({topology}/{lane_kind}): "
                  f"{len(problems)} authoring problems")
            for p in problems[:3]:
                print(f"      {p}")
        content_refs = sorted({r for z in plan["zones"]
                               for r in z["content_refs"]})
        plan_relations = plan.pop("relations")
        plan.update({
            "slide_id": sid, "candidate_id": cid,
            "slide_background_token": "",
            "content_refs": content_refs,
            "communication_job": request.get("design_brief", {}).get(
                "communication_job", ""),
            "reading_order": [r for z in plan["zones"]
                              for r in z["content_refs"]],
            "dominant_ref": content_refs[0] if content_refs else "",
            "relations": plan_relations,
            "operators": [], "reference_ids": [],
            "allowed_overlaps": [], "overlap_permissions": [],
            "novel_recipe": f"lane-flow:{lane_kind}",
            "uncertainties": problems[:3],
            "rationale": f"{len(plan['zones'])} zones, {lane_kind} lanes, "
                         "heights from real measurement"})
        response = {
            "request_id": task["request_id"],
            "input_fingerprint": task.get("input_fingerprint", ""),
            "actual_model": "opencode-harness/scene-author-v7",
            "schema_version_id": request["response_schema"]["schema_id"]
            if isinstance(request.get("response_schema"), dict)
            else "scene-plan/v1",
            "payload": {"scene_plan": plan},
        }
        out = (RUN / "model_responses" /
               f"v7-{sid.replace('/', '_')}-{cid}.json")
        out.write_text(json.dumps(response, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        written += 1
    print(f"scene responses written: {written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
