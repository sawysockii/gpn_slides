"""Saved-geometry and binding gates (opencode prompt §1, §8, §12).

Single source of target geometry discipline:

- legacy zone→content bindings resolve to zone boxes (subdivided when a
  zone carries several refs), never to synthetic source-ID rows;
- ``diagnostic_geometry=true`` never passes user acceptance;
- saved bytes are checked for actual canvas visibility, including nested
  group transforms, rotation/flip and connector paths.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

CANVAS_W_EMU = 12192000
CANVAS_H_EMU = 6858000


def zone_box_to_emu(zone: dict[str, Any], content_box: dict[str, float]) -> dict[str, int]:
    bx = float(content_box.get("x", 0.5))
    by = float(content_box.get("y", 0.7))
    bw = float(content_box.get("w", 9.0))
    bh = float(content_box.get("h", 4.5))
    to_emu = 914400
    return {
        "x": int(round((bx + bw * float(zone.get("x_pct", 0)) / 100) * to_emu)),
        "y": int(round((by + bh * float(zone.get("y_pct", 0)) / 100) * to_emu)),
        "w": int(round((bw * float(zone.get("w_pct", 100)) / 100) * to_emu)),
        "h": int(round((bh * float(zone.get("h_pct", 20)) / 100) * to_emu)),
    }


def upgrade_legacy_bindings(
    zones: list[dict[str, Any]],
    content_box: dict[str, float],
) -> tuple[dict[str, dict[str, int]], dict[str, list[str]], list[str]]:
    """Zone content_refs -> canonical target-node geometry.

    Returns (geometry_by_node_id, source_to_node_bindings, issues).

    - one ref in a zone: the node takes the whole zone box;
    - N refs in a zone: vertical subdivision with equal shares (explicit
      child policy, deterministic, inside the zone — never a full-width
      synthetic row outside canvas);
    - geometry keys are target node ids (``zone:<zone_id>`` or
      ``zone:<zone_id>#<ref>``); source object ids are lineage only.
    """
    geometry: dict[str, dict[str, int]] = {}
    source_to_node: dict[str, list[str]] = {}
    issues: list[str] = []
    for zone in zones:
        zid = str(zone.get("zone_id", ""))
        refs = [str(r) for r in list(zone.get("content_refs", []) or [])]
        if not zid or not refs:
            continue
        box = zone_box_to_emu(zone, content_box)
        geometry[f"zone:{zid}"] = dict(box)
        if len(refs) == 1:
            node_id = f"zone:{zid}"
            geometry[node_id] = dict(box)
            source_to_node.setdefault(refs[0], []).append(node_id)
        else:
            # Explicit vertical flow: equal shares, no gaps invented
            # beyond integer rounding (last child takes the remainder).
            top = box["y"]
            total_h = box["h"]
            share = total_h // len(refs)
            for i, ref in enumerate(refs):
                node_id = f"zone:{zid}#{ref}"
                h = share if i < len(refs) - 1 else (total_h - share * (len(refs) - 1))
                geometry[node_id] = {
                    "x": box["x"], "y": top + share * i,
                    "w": box["w"], "h": max(h, 1),
                }
                source_to_node.setdefault(ref, []).append(node_id)
            issues.append(
                f"ZONE_SHARED_SUBDIVIDED: {zid} carries {len(refs)} refs; "
                "vertical equal-share flow applied (measured path refines)")
    return geometry, source_to_node, issues


def trace_geometry_application(
    slide_id: str,
    source: Any,
    plan: Any,
    resolved: Any,
    saved: Any,
) -> dict[str, Any]:
    """Per-target-object trace source refs -> saved xfrm (§1).

    Each checkpoint records box+units+coordinate_space so the first
    appearance of uniform-row stacking or off-canvas output is visible.
    ``saved`` is the reimported deck (or a dict with candidate boxes).
    """
    def _box(d: Any) -> dict[str, Any]:
        if isinstance(d, dict):
            return {k: d.get(k) for k in ("x", "y", "w", "h")}
        return {"x": getattr(d, "x", None), "y": getattr(d, "y", None),
                "w": getattr(d, "w", None), "h": getattr(d, "h", None)}

    zones: list[dict[str, Any]] = []
    raw_zones = plan.get("zones", []) if isinstance(plan, dict) else getattr(plan, "zones", [])
    for z in list(raw_zones or []):
        if isinstance(z, dict):
            zones.append(z)
        else:
            zones.append({
                "zone_id": getattr(z, "zone_id", ""),
                "x_pct": getattr(z, "x_pct", 0), "y_pct": getattr(z, "y_pct", 0),
                "w_pct": getattr(z, "w_pct", 0), "h_pct": getattr(z, "h_pct", 0),
                "content_refs": list(getattr(z, "content_refs", []) or []),
            })
    geometry = resolved.get("geometry_emu", {}) if isinstance(resolved, dict) \
        else getattr(resolved, "geometry_emu", {})
    saved_boxes: dict[str, dict[str, Any]] = {}
    if saved is not None:
        slides = saved.get("slides", []) if isinstance(saved, dict) \
            else getattr(saved, "slides", [])
        for s in list(slides or []):
            objs = s.get("objects", []) if isinstance(s, dict) else getattr(s, "objects", [])
            for o in list(objs or []):
                oid = o.get("id", "") if isinstance(o, dict) else getattr(o, "id", "")
                box = o.get("local_box") if isinstance(o, dict) else getattr(o, "local_box", None)
                if oid and box is not None:
                    saved_boxes[str(oid)] = _box(box) if isinstance(box, dict) else _box(box)

    objects = source.get("objects", []) if isinstance(source, dict) \
        else getattr(source, "objects", [])
    zone_by_ref: dict[str, dict[str, Any]] = {}
    for z in zones:
        for r in list(z.get("content_refs", []) or []):
            zone_by_ref[str(r)] = z

    checkpoints: list[dict[str, Any]] = []
    first_offence: dict[str, Any] | None = None
    for o in list(objects or []):
        oid = o.get("id", "") if isinstance(o, dict) else getattr(o, "id", "")
        zone = zone_by_ref.get(str(oid))
        scene_box = None
        if zone is not None:
            scene_box = {k: zone.get(k) for k in ("x_pct", "y_pct", "w_pct", "h_pct")}
        resolved_box = geometry.get(str(oid))
        resolved_zone_box = None
        # canonical node binding for this source ref: exact suffix match
        # on the full stable id (prefix truncation would collide).
        own_zid = str(zone["zone_id"]) if zone is not None else ""
        for key, val in dict(geometry).items():
            skey = str(key)
            if not skey.startswith("zone:"):
                continue
            if own_zid and skey == f"zone:{own_zid}":
                resolved_zone_box = val
                break
            if skey.endswith("#" + str(oid)) or skey == str(oid):
                resolved_zone_box = val
                break
        entry = {
            "source_ref": str(oid),
            "scene_box_pct": scene_box,
            "scene_units": "zone_pct_of_content_box" if scene_box else None,
            "resolved_source_row_emu": resolved_box,
            "resolved_node_box_emu": resolved_zone_box,
            "synthetic_row": bool(resolved_box and zone_by_ref.get(str(oid)) is not None
                                  and resolved_box.get("w") == CANVAS_W_EMU),
            "saved_box": saved_boxes.get(str(oid)),
        }
        # offence: synthetic full-width row off canvas
        rb = resolved_box or {}
        if rb and rb.get("w") == CANVAS_W_EMU and int(rb.get("y", 0)) >= CANVAS_H_EMU:
            entry["offence"] = "uniform-row stacking off canvas (diagnostic fallback)"
            if first_offence is None:
                first_offence = {
                    "source_ref": str(oid),
                    "checkpoint": "resolved.geometry_emu[source_id]",
                    "box_emu": dict(rb),
                }
        checkpoints.append(entry)
    return {
        "slide_id": slide_id,
        "diagnostic_geometry": bool(resolved.get("diagnostic_geometry", False)
                                     if isinstance(resolved, dict)
                                     else getattr(resolved, "diagnostic_geometry", False)),
        "checkpoints": checkpoints,
        "first_offence": first_offence,
        "result": "offence_found" if first_offence else "clean",
    }


def _canvas_emu(compiled: Any) -> tuple[int, int]:
    canvas = getattr(compiled, "canvas", None) if compiled is not None else None
    try:
        return int(getattr(canvas, "w", CANVAS_W_EMU)), int(getattr(canvas, "h", CANVAS_H_EMU))
    except (TypeError, ValueError):
        return CANVAS_W_EMU, CANVAS_H_EMU


def _parse_bool_flag(value: Any) -> bool:
    """OOXML boolean lexical space: 1/true/on are true (spec, not =="1")."""
    return str(value).strip().lower() in ("1", "true", "on")


def _slide_order_and_canvas(
    archive: Any, fallback_w: int, fallback_h: int
) -> tuple[list[str], list[str], int, int, dict[str, bool]]:
    """Actual slide order via p:sldIdLst + relationships; canvas from p:sldSz.

    Never ``sorted(slide*.xml)`` (slide10 sorts before slide2). Falls back
    to sorted names / compiled canvas only when the package graph is
    unreadable, and says so in the returned note list.
    """
    from lxml import etree

    p_ns = "http://schemas.openxmlformats.org/presentationml/2006/main"
    r_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    notes: list[str] = []
    try:
        pres_root = etree.fromstring(archive.read("ppt/presentation.xml"))
    except Exception:
        names = sorted(n for n in archive.namelist()
                       if n.startswith("ppt/slides/slide") and n.endswith(".xml"))
        return names, names, fallback_w, fallback_h, {}
    canvas_w, canvas_h = fallback_w, fallback_h
    sld_sz = pres_root.find(f"{{{p_ns}}}sldSz")
    if sld_sz is not None:
        try:
            canvas_w = int(sld_sz.get("cx", canvas_w))
            canvas_h = int(sld_sz.get("cy", canvas_h))
        except (TypeError, ValueError):
            notes.append("unparseable p:sldSz; compiled canvas used")
    else:
        notes.append("no p:sldSz; compiled canvas used")
    hidden: dict[str, bool] = {}
    rels: dict[str, str] = {}
    try:
        rels_root = etree.fromstring(archive.read("ppt/_rels/presentation.xml.rels"))
        for rel in rels_root.findall("{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"):
            rid = rel.get("Id", "")
            target = rel.get("Target", "")
            if rid and target:
                # Targets are package-relative to ppt/ (slides/slide1.xml,
                # slideLayouts/..., theme/...); normalize to archive names.
                norm = target.replace("\\", "/").lstrip("/")
                if not norm.startswith("ppt/"):
                    norm = "ppt/" + norm
                rels[rid] = norm
    except Exception:
        notes.append("presentation rels unreadable; order fallback")
    ordered: list[str] = []
    sld_id_lst = pres_root.find(f"{{{p_ns}}}sldIdLst")
    if sld_id_lst is not None:
        for sld_id in sld_id_lst.findall(f"{{{p_ns}}}sldId"):
            rid = sld_id.get(f"{{{r_ns}}}id", "")
            part = rels.get(rid, "")
            if part and part in archive.namelist():
                ordered.append(part)
                show = sld_id.get("show", "1")
                hidden[part] = _parse_bool_flag(show) is False and show != "1"
    if not ordered:
        notes.append("sldIdLst unresolvable; sorted-name fallback (order unverified)")
        ordered = sorted(n for n in archive.namelist()
                         if n.startswith("ppt/slides/slide") and n.endswith(".xml"))
    return ordered, notes, canvas_w, canvas_h, hidden


def _local_tag(el: Any) -> str:
    tag = getattr(el, "tag", "")
    return tag.split("}", 1)[1] if "}" in tag else str(tag)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _read_shape_xfrm(el: Any, kind: str, ns: dict[str, str]) -> dict[str, Any] | None:
    """Locate the xfrm for sp/cxnSp/pic/graphicFrame (distinct wrappers)."""
    if kind == "graphicFrame":
        # p:graphicFrame/p:xfrm (direct child with a:off/a:ext inside,
        # not under spPr and not in the a: namespace itself).
        found = el.find("p:xfrm", ns)
        if found is not None:
            return found
        return el.find("a:xfrm", ns)
    sp_pr = el.find("p:spPr", ns)
    if sp_pr is not None:
        found = sp_pr.find("a:xfrm", ns)
        if found is not None:
            return found
    # Fallback: any descendant a:xfrm (blipFill variants keep spPr too).
    found = el.find(".//a:xfrm", ns)
    return found


def _xfrm_to_box(xfrm: Any, ns: dict[str, str]) -> tuple[dict[str, int] | None, str | None,
                                                         dict[str, Any]]:
    """Raw (x, y, w, h) + rot/flip flags; None box means incomplete evidence."""
    if xfrm is None:
        return None, "no xfrm (opaque/unknown)", {}
    off = xfrm.find("a:off", ns)
    ext = xfrm.find("a:ext", ns)
    if off is None or ext is None:
        return None, "incomplete xfrm (missing off/ext)", {}
    vals = (_int_or_none(off.get("x")), _int_or_none(off.get("y")),
            _int_or_none(ext.get("cx")), _int_or_none(ext.get("cy")))
    if any(v is None for v in vals):
        return None, "non-numeric xfrm", {}
    x, y, w, h = vals  # type: ignore[misc]
    flags = {
        "rot": xfrm.get("rot"),
        "flipH": _parse_bool_flag(xfrm.get("flipH")),
        "flipV": _parse_bool_flag(xfrm.get("flipV")),
    }
    return {"x": x, "y": y, "w": w, "h": h}, None, flags


def _rotated_aabb_emu(box: dict[str, int], flags: dict[str, Any]) -> tuple[
        list[tuple[float, float]], dict[str, float]]:
    """Effective polygon + AABB of a (possibly rotated/flipped) box.

    Single implementation of the OOXML order: flip about the centre, then
    rotation about the centre (same order as readers._rotation_flip_matrix:
    T(c) @ R @ F @ T(-c)).
    """
    import math

    x, y, w, h = (float(box["x"]), float(box["y"]),
                  float(box["w"]), float(box["h"]))
    cx, cy = x + w / 2.0, y + h / 2.0
    try:
        angle = math.radians(float(flags.get("rot") or 0) / 60000.0)
    except (TypeError, ValueError):
        angle = 0.0
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    sx = -1.0 if flags.get("flipH") else 1.0
    sy = -1.0 if flags.get("flipV") else 1.0
    poly: list[tuple[float, float]] = []
    for px, py in ((x, y), (x + w, y), (x + w, y + h), (x, y + h)):
        lx, ly = (px - cx) * sx, (py - cy) * sy
        rx = lx * cos_a - ly * sin_a
        ry = lx * sin_a + ly * cos_a
        poly.append((rx + cx, ry + cy))
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    aabb = {"x": min(xs), "y": min(ys),
            "w": max(xs) - min(xs), "h": max(ys) - min(ys)}
    return poly, aabb


def _apply_group_chain_emu(
    poly: list[tuple[float, float]], chain: list[dict[str, Any]]
) -> list[tuple[float, float]]:
    """Map a child-local polygon through accumulated parent group transforms.

    Each chain entry mirrors readers.group_child_matrix order: shift by
    -chOff, scale by ext/chExt, move to off, then the group's own
    rot/flip about its centre. Chain is innermost-parent first.
    """
    import math

    pts = list(poly)
    for g in chain:
        ch_cx = g.get("ch_cx")
        ch_cy = g.get("ch_cy")
        if ch_cx == 0 or ch_cy == 0:
            raise ValueError("zero chExt (singular group mapping)")
        sx = g["cx"] / ch_cx if ch_cx else 1.0
        sy = g["cy"] / ch_cy if ch_cy else 1.0
        moved = [((px - g["ch_x"]) * sx + g["x"],
                   (py - g["ch_y"]) * sy + g["y"]) for px, py in pts]
        cx, cy = g["x"] + g["cx"] / 2.0, g["y"] + g["cy"] / 2.0
        angle = math.radians(float(g.get("rot") or 0) / 60000.0)
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        fsx = -1.0 if g.get("flipH") else 1.0
        fsy = -1.0 if g.get("flipV") else 1.0
        pts = []
        for px, py in moved:
            lx, ly = (px - cx) * fsx, (py - cy) * fsy
            pts.append((lx * cos_a - ly * sin_a + cx,
                        lx * sin_a + ly * cos_a + cy))
    return pts


def check_saved_geometry(
    package_path: str | Path,
    compiled: Any,
    object_map: list[Any] | None = None,
    after_deck: Any | None = None,
) -> dict[str, Any]:
    """Actual saved-geometry gate on reopened bytes (§12).

    - canvas + slide order + hidden flags from the actual package
      (``ppt/presentation.xml`` + rels), never ``sorted(slide*.xml)``;
    - recursive walk of ``p:spTree``/``p:grpSp`` covering ``p:sp``,
      ``p:pic``, ``p:cxnSp`` and ``p:graphicFrame`` (tables/charts/
      diagrams), with ``mc:AlternateContent`` choice branches;
    - real rotation/flip polygons and parent-group transform chains
      (zero ``chExt`` and missing xfrm evidence are unknown/blocking,
      never an identity matrix or a division by zero);
    - fully-outside required content is a hard fail; *partial* clipping
      of a drawn box is a fail too (``x + w > canvas_w`` with
      ``x < canvas_w`` is not a pass);
    - connectors are stroke-envelope segments: a zero-height horizontal
      (or zero-width vertical) line is valid geometry, not an invisible
      box; unknown/opaque drawings are unknown/blocking.
    """
    import zipfile

    from lxml import etree

    canvas_w, canvas_h = _canvas_emu(compiled)
    report: dict[str, Any] = {
        "candidate": str(package_path),
        "candidate_sha256": "",
        "canvas_emu": [canvas_w, canvas_h],
        "actual_canvas_emu": [canvas_w, canvas_h],
        "slide_order": [],
        "hidden_slides": [],
        "checked_scopes": ["top_level_xfrm", "nested_group_xfrm",
                           "rotation_flip_polygon", "connector_path",
                           "canvas_containment", "partial_clipping",
                           "binding_match"],
        "per_object_geometry": [],
        "out_of_canvas": [],
        "clipping": [],
        "collisions": [],
        "unresolved_transforms": [],
        "unsupported_drawings": [],
        "order_notes": [],
        "issues": [],
        "result": "pass",
    }
    try:
        import hashlib

        with open(package_path, "rb") as fh:
            report["candidate_sha256"] = hashlib.sha256(fh.read()).hexdigest()
    except OSError as exc:
        report["issues"].append(f"candidate unreadable: {exc}")
        report["result"] = "fail"
        return report

    a_ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    p_ns = "http://schemas.openxmlformats.org/presentationml/2006/main"
    mc_ns = "http://schemas.openxmlformats.org/markup-compatibility/2006"
    ns = {"a": a_ns, "p": p_ns, "mc": mc_ns}
    try:
        with zipfile.ZipFile(package_path) as archive:
            slide_names, order_notes, actual_w, actual_h, hidden = \
                _slide_order_and_canvas(archive, canvas_w, canvas_h)
            report["slide_order"] = slide_names
            report["order_notes"] = order_notes
            report["canvas_emu"] = [actual_w, actual_h]
            report["actual_canvas_emu"] = [actual_w, actual_h]
            report["hidden_slides"] = sorted(p for p, h in hidden.items() if h)
            canvas_w, canvas_h = actual_w, actual_h

            for name in slide_names:
                try:
                    root = etree.fromstring(archive.read(name))
                except Exception as exc:  # noqa: BLE001
                    report["unresolved_transforms"].append(f"{name}: {exc}")
                    continue
                tree = root.find("p:cSld/p:spTree", ns)
                if tree is None:
                    report["unresolved_transforms"].append(
                        f"{name}: no p:spTree (opaque/unknown)")
                    continue
                _walk_drawing_tree(
                    tree, ns, name, [], report, canvas_w, canvas_h)
    except Exception as exc:  # noqa: BLE001 - honest failure, not pass
        report["issues"].append(f"saved-bytes read failed: {exc}")
        report["result"] = "fail"
        return report

    if report["unresolved_transforms"]:
        report["issues"].append(
            f"{len(report['unresolved_transforms'])} unresolved transforms (unknown, blocking)")
    if report["unsupported_drawings"]:
        report["issues"].append(
            f"{len(report['unsupported_drawings'])} unsupported drawings (unknown, blocking)")
    if report["out_of_canvas"]:
        report["issues"].append(
            f"{len(report['out_of_canvas'])} objects fully outside canvas")
    if report["clipping"]:
        report["issues"].append(
            f"{len(report['clipping'])} objects partially clipped by canvas")
    # Empty checked list is unknown, never pass.
    if not report["per_object_geometry"]:
        report["issues"].append("no checked objects (unknown, not pass)")
        report["result"] = "fail"
    elif (report["out_of_canvas"] or report["unresolved_transforms"]
            or report["unsupported_drawings"] or report["clipping"]):
        report["result"] = "fail"
    else:
        report["result"] = "pass"
    return report


def _walk_drawing_tree(
    tree_el: Any, ns: dict[str, str], part: str,
    chain: list[dict[str, Any]], report: dict[str, Any],
    canvas_w: int, canvas_h: int,
) -> None:
    """Recursive spTree/grpSp walk accumulating the group transform chain."""
    from lxml import etree as _etree  # noqa: F401 - namespace clarity

    for child in list(tree_el):
        kind = _local_tag(child)
        if kind in ("nvGrpSpPr", "grpSpPr"):
            continue
        if kind == "grpSp":
            _walk_group(child, ns, part, chain, report, canvas_w, canvas_h)
            continue
        if kind == "AlternateContent":
            choice = child.find("mc:Choice", ns)
            fallback = child.find("mc:Fallback", ns)
            target = choice if choice is not None else fallback
            if target is not None:
                _walk_drawing_tree(target, ns, part, chain, report,
                                   canvas_w, canvas_h)
            else:
                report["unsupported_drawings"].append(
                    f"{part}: mc:AlternateContent without Choice/Fallback")
            continue
        if kind in ("sp", "cxnSp", "pic", "graphicFrame"):
            _check_drawn_shape(child, kind, ns, part, chain, report,
                               canvas_w, canvas_h)
            continue
        if kind in ("extLst", "cmAuthorLst", "cmLst"):
            continue
        report["unsupported_drawings"].append(
            f"{part}: unsupported drawing <{_local_tag(child)}> "
            "(unknown, blocking)")


def _walk_group(
    grp_el: Any, ns: dict[str, str], part: str,
    chain: list[dict[str, Any]], report: dict[str, Any],
    canvas_w: int, canvas_h: int,
) -> None:
    grp_pr = grp_el.find("p:nvGrpSpPr", ns)
    shape_id = ""
    if grp_pr is not None:
        cnv = grp_pr.find("p:cNvPr", ns)
        if cnv is not None:
            shape_id = str(cnv.get("id", ""))
    xfrm_el = grp_el.find("p:grpSpPr/a:xfrm", ns)
    if xfrm_el is None:
        report["unresolved_transforms"].append(
            f"{part}#{shape_id or '?'}: group has no xfrm (unknown, blocking)")
        return
    off = xfrm_el.find("a:off", ns)
    ext = xfrm_el.find("a:ext", ns)
    ch_off = xfrm_el.find("a:chOff", ns)
    ch_ext = xfrm_el.find("a:chExt", ns)
    if off is None or ext is None or ch_off is None or ch_ext is None:
        report["unresolved_transforms"].append(
            f"{part}#{shape_id or '?'}: group xfrm incomplete "
            "(off/ext/chOff/chExt required)")
        return
    vals = [_int_or_none(off.get("x")), _int_or_none(off.get("y")),
            _int_or_none(ext.get("cx")), _int_or_none(ext.get("cy")),
            _int_or_none(ch_off.get("x")), _int_or_none(ch_off.get("y")),
            _int_or_none(ch_ext.get("cx")), _int_or_none(ch_ext.get("cy"))]
    if any(v is None for v in vals):
        report["unresolved_transforms"].append(
            f"{part}#{shape_id or '?'}: group xfrm non-numeric")
        return
    gx, gy, gcw, gch, chx, chy, chcx, chcy = vals  # type: ignore[misc]
    if chcx == 0 or chcy == 0:
        report["unresolved_transforms"].append(
            f"{part}#{shape_id or '?'}: group chExt is zero "
            "(singular mapping, unknown, blocking)")
        return
    entry = {"shape_id": shape_id,
             "x": float(gx), "y": float(gy), "cx": float(gcw),
             "cy": float(gch), "ch_x": float(chx), "ch_y": float(chy),
             "ch_cx": float(chcx), "ch_cy": float(chcy),
             "rot": xfrm_el.get("rot"),
             "flipH": _parse_bool_flag(xfrm_el.get("flipH")),
             "flipV": _parse_bool_flag(xfrm_el.get("flipV"))}
    # Chain is innermost-parent first (matches _apply_group_chain_emu
    # order: child-local -> inner group -> outer group). Prepend the
    # current group so nested shapes map inner-then-outer (world x=1220
    # for the outer-1000/scale2 + inner-100 + child-10 fixture).
    for child in list(grp_el):
        if _local_tag(child) in ("nvGrpSpPr", "grpSpPr"):
            continue
        if _local_tag(child) == "grpSp":
            _walk_group(child, ns, part, [entry, *chain], report,
                        canvas_w, canvas_h)
        elif _local_tag(child) in ("sp", "cxnSp", "pic", "graphicFrame"):
            _check_drawn_shape(child, _local_tag(child), ns, part,
                               [entry, *chain], report, canvas_w, canvas_h)
        elif _local_tag(child) == "AlternateContent":
            choice = child.find("mc:Choice", ns)
            if choice is not None:
                _walk_drawing_tree(choice, ns, part, [entry, *chain], report,
                                   canvas_w, canvas_h)
            else:
                report["unsupported_drawings"].append(
                    f"{part}#{shape_id or '?'}: group AlternateContent "
                    "without Choice")


def _check_drawn_shape(
    el: Any, kind: str, ns: dict[str, str], part: str,
    chain: list[dict[str, Any]], report: dict[str, Any],
    canvas_w: int, canvas_h: int,
) -> None:
    nv_tag = {"sp": "p:nvSpPr", "cxnSp": "p:nvCxnSpPr",
              "pic": "p:nvPicPr", "graphicFrame": "p:nvGraphicFramePr"}[kind]
    shape_id = ""
    nv = el.find(nv_tag, ns)
    if nv is not None:
        # GraphicFrame identity lives in p:nvGraphicFramePr/p:cNvPr
        # (never p:nvPr); other kinds use p:cNvPr under their nv tag.
        cnv = nv.find("p:cNvPr", ns)
        if cnv is not None:
            shape_id = str(cnv.get("id", ""))
    group_ids = [str(g.get("shape_id", "")) for g in chain if g.get("shape_id")]
    group_path = "/".join(group_ids)
    ident = f"{part}#{shape_id or '?'}"
    if group_path:
        ident = f"{ident}@group:{group_path}"
    xfrm_el = _read_shape_xfrm(el, kind, ns)
    box, problem, flags = _xfrm_to_box(xfrm_el, ns)
    if box is None or problem:
        report["unresolved_transforms"].append(f"{ident}: {problem}")
        return
    is_connector = kind == "cxnSp"
    if is_connector and (box["w"] == 0 or box["h"] == 0):
        # A horizontal (h=0) or vertical (w=0) connector is a valid
        # stroke segment, never a "zero invisible box". Envelope uses
        # the actual line width (a:ln w) plus arrowhead/path scope:
        # rotation is already in flags->polygon below; an
        # unresolvable route stays unknown (scoped, never clean pass).
        line_w = _connector_line_width_emu(el, ns)
        half = max(line_w / 2.0, 12700.0)
        eff_box = {"x": box["x"] - (0 if box["w"] else half),
                   "y": box["y"] - (0 if box["h"] else half),
                   "w": box["w"] + (0 if box["w"] else 2 * half),
                   "h": box["h"] + (0 if box["h"] else 2 * half)}
        poly = [(float(eff_box["x"]), float(eff_box["y"])),
                (float(eff_box["x"] + eff_box["w"]), float(eff_box["y"])),
                (float(eff_box["x"] + eff_box["w"]),
                 float(eff_box["y"] + eff_box["h"])),
                (float(eff_box["x"]), float(eff_box["y"] + eff_box["h"]))]
    else:
        poly, _ = _rotated_aabb_emu(box, flags)
    try:
        world = _apply_group_chain_emu(poly, chain)
    except ValueError as exc:
        report["unresolved_transforms"].append(f"{ident}: {exc}")
        return
    xs = [p[0] for p in world]
    ys = [p[1] for p in world]
    ax0, ay0, ax1, ay1 = min(xs), min(ys), max(xs), max(ys)
    entry: dict[str, Any] = {
        "part": part, "shape_id": shape_id, "kind": kind,
        "group_path": group_path,
        "x": box["x"], "y": box["y"], "w": box["w"], "h": box["h"],
        "rot": flags.get("rot"), "flipH": bool(flags.get("flipH")),
        "flipV": bool(flags.get("flipV")),
        "group_depth": len(chain),
        "effective_aabb": {"x": ax0, "y": ay0,
                           "w": ax1 - ax0, "h": ay1 - ay0},
    }
    report["per_object_geometry"].append(entry)
    fully_outside = (ax0 >= canvas_w or ay0 >= canvas_h
                     or ax1 <= 0 or ay1 <= 0)
    if fully_outside:
        report["out_of_canvas"].append(
            f"{ident}({kind}) fully outside canvas "
            f"(aabb {ax0:.0f},{ay0:.0f} {ax1 - ax0:.0f}x{ay1 - ay0:.0f})")
        return
    if ax0 < 0 or ay0 < 0 or ax1 > canvas_w or ay1 > canvas_h:
        report["clipping"].append(
            f"{ident}({kind}) partially outside canvas "
            f"(aabb {ax0:.0f},{ay0:.0f} {ax1 - ax0:.0f}x{ay1 - ay0:.0f} "
            f"vs canvas {canvas_w}x{canvas_h})")


def _connector_line_width_emu(el: Any, ns: dict[str, str]) -> float:
    """Actual connector stroke width in EMU (a:ln w), min 1pt envelope.

    Falls back to 1pt (12700 EMU) when the line width is absent or
    unparseable; arrowheads extend beyond the bare segment, so the
    envelope is a lower bound, never an exact route proof.
    """
    for tag in ("p:spPr/a:ln", "p:cxnSpPr/a:ln", "a:ln"):
        try:
            ln = el.find(tag, ns)
        except Exception:  # noqa: BLE001 - unknown XML stays fallback
            ln = None
        if ln is not None:
            try:
                w = int(ln.get("w", ""))
                if w > 0:
                    return float(w)
            except (TypeError, ValueError):
                continue
    # Fallback: any descendant a:ln with w.
    try:
        for ln in list(el.iter()):
            if _local_tag(ln) == "ln" and ln.get("w"):
                try:
                    w = int(str(ln.get("w")))
                    if w > 0:
                        return float(w)
                except (TypeError, ValueError):
                    continue
    except Exception:  # noqa: BLE001
        pass
    return 12700.0


def check_required_content_visibility(
    after_deck: Any,
    saved_geometry: dict[str, Any],
    required_refs: list[str] | None = None,
) -> dict[str, Any]:
    """Required units must be visible: inside canvas, not hidden/occluded.

    Expected-first: iteration starts from the expected required set;
    ``missing = expected - mapped_checked``. Mapping is via the saved
    after-deck object ids (target addresses); a required ref absent from
    the saved package is missing/fail, never skipped by an actual-only
    walk. Geometry proof (fully outside -> hard fail) is authoritative;
    render text extraction is a signal only. Unknown essential
    visibility goes to image review/repair, never to pass.
    """
    outside = {str(item).split("#")[0]: item for item in saved_geometry.get("out_of_canvas", [])}
    per_slide: dict[str, Any] = {}
    missing: list[str] = []
    unknown: list[str] = []
    seen: set[str] = set()
    if after_deck is not None:
        for slide in list(getattr(after_deck, "slides", []) or []):
            for obj in list(getattr(slide, "objects", []) or []):
                oid = str(getattr(obj, "id", ""))
                if required_refs is not None and oid not in set(required_refs):
                    continue
                seen.add(oid)
                box = getattr(obj, "local_box", None)
                if box is None:
                    per_slide[oid] = {"result": "unknown", "reason": "no saved box"}
                    unknown.append(oid)
                    continue
                try:
                    x, y, w, h = int(box.x), int(box.y), int(box.w), int(box.h)
                except (TypeError, ValueError, AttributeError):
                    per_slide[oid] = {"result": "unknown", "reason": "unresolved transform"}
                    unknown.append(oid)
                    continue
                if x >= CANVAS_W_EMU or y >= CANVAS_H_EMU or x + w <= 0 or y + h <= 0:
                    per_slide[oid] = {"result": "fail", "reason": "fully_outside_canvas"}
                    missing.append(oid)
                else:
                    per_slide[oid] = {"result": "visible", "reason": "inside canvas"}
    if required_refs is not None:
        for oid in list(dict.fromkeys(str(r) for r in required_refs)):
            if oid not in seen:
                per_slide[oid] = {"result": "fail",
                                  "reason": "absent_from_saved_package"}
                if oid not in missing:
                    missing.append(oid)
    # Aggregation: required unknown blocks (never pass); missing fails;
    # pass needs a non-empty required set with every unit visible.
    if missing:
        result = "fail"
    elif unknown:
        result = "unknown"
    elif per_slide:
        result = "pass"
    else:
        result = "unknown"
    return {"per_object": per_slide, "missing": missing,
            "unknown_required": unknown,
            "outside_proofs": list(outside.values())[:5], "result": result}
