"""Stage 9 §6: keep graphic meaning while applying the corporate style.

The recovery run shipped a renderer rule that read "source stroke is not in
the palette -> clear the line". Applied to the ГГД schematic on slide 3, to
the source->processing->table chain on slide 2 and to the logo on slide 6,
that produced XML objects whose visible strokes were gone: the object count
was preserved, the graph was not. Semantic preservation was then declared on
the strength of a text/presence diff.

This module moves the decision BEFORE emission:

``classify_visual_units``      what each drawing element means (with evidence)
``plan_visual_normalization``  one decided action per unit
``resolve_brand_asset``        colorway / surface for a brand asset
``check_visual_representation``visible encoding of every required unit

``native._apply_decor_fill_line`` executes the plan; it no longer decides
semantics from a random source colour at the end of emission.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .models import (
    BrandAssetPlan,
    RepresentationReport,
    VisualNormalizationPlan,
    VisualTopology,
    VisualUnit,
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _object_paragraphs(obj: Any) -> list[Any]:
    payload = getattr(obj, "payload", None)
    paras = getattr(payload, "paragraphs", None)
    if paras is None:
        inner = getattr(payload, "text", None)
        paras = getattr(inner, "paragraphs", None) if inner is not None else []
    return list(paras or [])


def _has_displayable_text(obj: Any) -> bool:
    for para in _object_paragraphs(obj):
        for run in list(getattr(para, "runs", []) or []):
            if (getattr(run, "text", "") or "") != "":
                return True
            if getattr(run, "kind", "text") == "field":
                meta = getattr(run, "field", None)
                if (getattr(meta, "cached_text", "") or "") != "":
                    return True
    return False


def _is_connector(obj: Any) -> bool:
    payload = getattr(obj, "payload", None)
    line = getattr(payload, "source_line", None)
    if line is None:
        return False
    return any(getattr(line, attr, None) not in (None, "", "none")
               for attr in ("begin_arrow", "end_arrow"))


def _semantic_index(semantic_slide: Any) -> dict[str, list[str]]:
    """source object id -> semantic unit ids that reference it."""
    out: dict[str, list[str]] = {}
    if semantic_slide is None:
        return out
    for unit in list(getattr(semantic_slide, "units", []) or []):
        for ref in list(getattr(unit, "source_refs", []) or []):
            out.setdefault(str(ref), []).append(
                str(getattr(unit, "id", "") or ""))
    return out


def classify_visual_units(
    objects: list[Any],
    semantic_slide: Any = None,
    transform_ledger: Any = None,
    source_snapshot: str = "",
    *,
    can_omit: Any = None,
) -> list[VisualUnit]:
    """Classify every drawing element of a slide with explicit evidence.

    ``required`` comes from the omit authorization: a unit that the ledger
    forbids omitting is required. Text-bearing objects are not visual units —
    their preservation is proven by the span comparator, not here.
    """
    from .units import emu_to_pt

    sem_index = _semantic_index(semantic_slide)
    semantic_units = list(getattr(semantic_slide, "units", []) or [])
    relations = list(getattr(semantic_slide, "relations", []) or [])

    def unit_refs(unit: Any) -> list[str]:
        return [
            str(getattr(ref, "object_id", "") or "")
            for ref in list(getattr(unit, "source_refs", []) or [])
            if str(getattr(ref, "object_id", "") or "")
        ]

    units_by_id = {
        str(getattr(unit, "id", "") or ""): unit for unit in semantic_units
    }
    required_refs: set[str] = set()
    for unit in semantic_units:
        if str(getattr(unit, "preservation", "") or "") != "optional":
            required_refs.update(unit_refs(unit))
    required_relation_ids: set[str] = set()
    relation_targets: dict[str, list[str]] = {}
    for relation in relations:
        rid = str(
            getattr(relation, "id", "")
            or getattr(relation, "relation_id", "")
        )
        refs = [
            str(getattr(ref, "object_id", "") or "")
            for ref in list(getattr(relation, "source_refs", []) or [])
            if str(getattr(ref, "object_id", "") or "")
        ]
        preservation = str(getattr(relation, "preservation", "") or "")
        if preservation == "required":
            required_relation_ids.add(rid)
            required_refs.update(refs)
        for unit_id in list(getattr(relation, "from_unit_ids", []) or []) + list(
            getattr(relation, "to_unit_ids", []) or []
        ):
            unit = units_by_id.get(str(unit_id))
            if unit is not None:
                refs.extend(unit_refs(unit))
                if preservation == "required":
                    required_refs.update(unit_refs(unit))
        for ref in dict.fromkeys(refs):
            relation_targets.setdefault(ref, []).append(rid)
    units: list[VisualUnit] = []
    for obj in objects:
        kind = str(getattr(obj, "kind", "") or "")
        if kind in ("text", "table", "chart"):
            continue
        if _has_displayable_text(obj):
            continue
        ref = str(getattr(obj, "id", "") or "")
        evidence: list[str] = [
            f"object {ref} kind={kind}",
            f"displayable text in shape: "
            f"{'yes' if _has_displayable_text(obj) else 'no'}",
        ]
        if _is_connector(obj):
            unit_kind = "diagram"
            evidence.append("connector carries an arrow endpoint: "
                            "sequence/relation encoding")
        elif kind == "image":
            unit_kind = "brand" if sem_index.get(ref) else "illustration"
            evidence.append(
                "image mapped to a semantic unit: brand asset"
                if sem_index.get(ref) else
                "unmapped image: illustration/screenshot candidate")
        elif kind == "group":
            unit_kind = "diagram"
            evidence.append("group of shapes: internal topology must survive")
        else:
            unit_kind = "diagram"
            evidence.append("text-less vector shape: meaning is decided by "
                            "relations, not by its area")
        required = True
        if can_omit is not None:
            try:
                decision = can_omit(obj, semantic_slide, transform_ledger,
                                    source_snapshot)
            except Exception:  # noqa: BLE001 - unknown stays required
                decision = None
            if decision is not None and getattr(decision, "allowed", False):
                required = False
                evidence.append(
                    f"omit authorized by "
                    f"{getattr(decision, 'authorization_ref', '?')}")
            else:
                evidence.append(
                    "omit refused: "
                    f"{getattr(decision, 'reason', 'no authorization')}")
        else:
            evidence.append("no omit authorization available: required")
        if ref in required_refs:
            if not required:
                evidence.append(
                    "required semantic unit/relation keeps vector geometry: "
                    "an omit authorization cannot waive it")
            required = True
        if not required:
            unit_kind = "optional_decor"
        box = getattr(obj, "local_box", None)
        topology = VisualTopology(
            member_refs=[ref], member_count=1, kind="single",
            source_box_pt=(
                {"x": emu_to_pt(box.x), "y": emu_to_pt(box.y),
                 "w": emu_to_pt(box.w), "h": emu_to_pt(box.h)}
                if box is not None else {}),
            aspect_ratio=(round(emu_to_pt(box.w) / emu_to_pt(box.h), 6)
                          if box is not None and emu_to_pt(box.h) > 0 else 0.0),
        )
        units.append(VisualUnit(
            unit_id=f"vu:{ref}", source_refs=[ref],
            semantic_unit_ids=list(sem_index.get(ref, [])),
            relation_ids=list(relation_targets.get(ref, [])),
            kind=unit_kind, required=required, internal_topology=topology,
            source_aspect_constraints=(
                [{"aspect": topology.aspect_ratio,
                  "origin": "source_bbox"}]
                if topology.aspect_ratio else []),
            evidence=evidence))
    return units


def _palette(rules: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if rules is None:
        return out
    for name, value in dict(getattr(rules, "colors", {}) or {}).items():
        out[str(name)] = str(value).strip().lstrip("#").upper()
    conditional = getattr(rules, "conditional_colors", {}) or {}
    if isinstance(conditional, dict):
        for name, condition in conditional.items():
            rgb = str(getattr(condition, "rgb", "") or "").strip().lstrip("#").upper()
            if rgb:
                out[str(name)] = rgb
    return out


def plan_visual_normalization(
    unit: VisualUnit,
    semantic_slide: Any = None,
    rules: Any = None,
    surface_context: dict[str, str] | None = None,
    *,
    stroke_token: str = "",
    fill_token: str = "",
    asset_sha256: str = "",
) -> VisualNormalizationPlan:
    """Decide ONE action for one visual unit, before emission (§6.2).

    The normative ban on source colours is preserved: the correct action is
    a permitted recolour/redraw from the loaded profile, never restoring the
    source RGB. "stroke not in palette -> noLine" is not an algorithm for a
    required diagram, and "fill cleared -> object kept" is not semantic
    preservation.
    """
    surface = surface_context or {}
    palette = _palette(rules)
    evidence = list(unit.evidence)
    plan = VisualNormalizationPlan(
        unit_id=unit.unit_id, surface_token=str(surface.get("surface", "")),
        stroke_token=stroke_token, fill_token=fill_token)
    if not unit.required:
        plan.action = "authorize_omit"
        plan.reason = ("optional decor confirmed by the omit authorization; "
                       "the whole cluster is removed and its space "
                       "recomposed, not left as an empty frame")
        plan.evidence = evidence
        return plan
    if unit.kind == "brand" and asset_sha256:
        plan.action = "use_approved_asset"
        plan.brand_asset_sha256 = asset_sha256
        plan.reason = ("brand asset keeps its own identity; colorway/surface "
                       "is resolved by resolve_brand_asset, never by the "
                       "renderer")
        plan.evidence = evidence
        return plan
    if stroke_token and stroke_token in palette:
        plan.action = "keep_native_recolor"
        plan.reason = (f"normative stroke token {stroke_token!r} is in the "
                       "loaded palette; source geometry is kept and "
                       "recoloured from the profile")
        plan.evidence = evidence
        return plan
    if fill_token and fill_token in palette:
        plan.action = "keep_native_recolor"
        plan.reason = (f"normative fill token {fill_token!r} is in the "
                       "loaded palette; the unit stays visible")
        plan.evidence = evidence
        return plan
    plan.action = "unresolved"
    plan.reason = (
        f"required {unit.kind} unit {unit.unit_id} has no applicable "
        "normative style token: emitting it would either keep a forbidden "
        "source colour or clear its visible strokes and leave an empty "
        "frame. A permitted recolour/redraw/approved asset is required "
        "first; silent noFill is not preservation.")
    plan.evidence = evidence
    return plan


def analyze_image_alpha(data: bytes) -> dict[str, Any]:
    """Actual alpha/colour statistics of a raster asset (§6.4).

    Returns counts of visible pixels and, for each distinct RGB, how many
    of them are visible. A white-on-white logo is detected here from its
    own bytes — not attributed to the renderer.
    """
    try:
        import io

        from PIL import Image
    except Exception:  # noqa: BLE001 - recorded, never guessed
        return {"available": False,
                "reason": "Pillow unavailable; alpha not analysed"}
    try:
        with Image.open(io.BytesIO(data)) as img:
            rgba = img.convert("RGBA")
            colors: dict[str, int] = {}
            visible = 0
            total = rgba.width * rgba.height
            for pixel in rgba.getdata():
                rgb, alpha = pixel[0:3], pixel[3]
                if alpha == 0:
                    continue
                visible += 1
                key = f"{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"
                colors[key] = colors.get(key, 0) + 1
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"decode failed: {exc}"}
    dominant = sorted(colors.items(), key=lambda kv: (-kv[1], kv[0]))
    return {
        "available": True,
        "pixels": total,
        "visible_pixels": visible,
        "colors": dict(dominant[:8]),
        "dominant_rgb": dominant[0][0] if dominant else "",
        "dominant_share": (round(dominant[0][1] / visible, 6)
                           if visible and dominant else 0.0),
    }


def resolve_brand_asset(
    asset_ref: str,
    data: bytes,
    target_surface: str,
    rules: Any,
    approved_assets: list[dict[str, Any]] | None = None,
) -> BrandAssetPlan:
    """Pick a permitted colorway / surface for a brand asset (§6.4).

    A white PNG on a white surface is a colorway problem, proven from the
    asset bytes. The resolution is an EXISTING approved colorway with
    verified lineage, or a brand surface from the loaded JSON. No new logo
    is drawn, no arbitrary pixel recolour is applied and the brand is never
    changed to make the layout fit.
    """
    palette = _palette(rules)
    analysis = analyze_image_alpha(data)
    plan = BrandAssetPlan(
        asset_ref=asset_ref, source_sha256=_sha256_bytes(data),
        alpha_analysis=analysis)
    if not analysis.get("available"):
        plan.action = "unresolved"
        plan.reason = analysis.get("reason", "alpha analysis unavailable")
        return plan
    dominant = str(analysis.get("dominant_rgb", "")).upper()
    surface_rgb = palette.get(str(target_surface or ""), "")
    plan.action = "keep_as_is"
    plan.reason = (f"dominant visible colour #{dominant} on surface "
                   f"{target_surface or '(none)'}"
                   f"#{surface_rgb or '??'}")
    if not surface_rgb:
        plan.action = "unresolved"
        plan.reason += "; target surface token is not in the loaded palette"
        return plan
    if dominant == "FFFFFF" and surface_rgb == "FFFFFF":
        for candidate in approved_assets or []:
            path = str(candidate.get("path", "") or "")
            sha = str(candidate.get("sha256", "") or "")
            if not path or not sha:
                continue
            try:
                blob = Path(path).read_bytes()
            except OSError:
                continue
            if _sha256_bytes(blob) != sha:
                continue
            stats = analyze_image_alpha(blob)
            other = str(stats.get("dominant_rgb", "")).upper()
            if other and other != "FFFFFF":
                plan.action = "use_approved_colorway"
                plan.chosen_sha256 = sha
                plan.chosen_path = path
                plan.reason = (
                    f"asset is #{dominant} on a #{surface_rgb} surface "
                    f"(invisible); approved colorway {candidate.get('id', '')!r} "
                    f"dominant #{other} has verified sha256 {sha[:12]}…")
                return plan
        plan.action = "unresolved"
        plan.reason = (
            "asset is white on a white surface and no approved colorway "
            "with verified lineage is available; an exact asset/policy "
            "finding is raised instead of inventing a logo")
        return plan
    return plan


def check_visual_representation(
    expected_units: list[VisualUnit],
    plans: dict[str, VisualNormalizationPlan],
    saved_drawings: dict[str, dict[str, Any]],
) -> RepresentationReport:
    """Every REQUIRED unit must have a bound output that actually shows.

    ``saved_drawings`` maps a source object id to what the saved package
    really contains for it: ``{bound, has_geometry, has_paths, has_stroke,
    has_fill, has_labels, data_ok, representation}``. A missing, unbound or
    fully transparent output is a failure — an empty bounding box does not
    discharge a representation contract.
    """
    report = RepresentationReport()
    for unit in expected_units:
        plan = plans.get(unit.unit_id)
        saved = saved_drawings.get(unit.unit_id, {})
        entry = {
            "unit_id": unit.unit_id,
            "source_refs": unit.source_refs,
            "kind": unit.kind,
            "required": unit.required,
            "action": getattr(plan, "action", "missing_plan"),
            "bound": bool(saved.get("bound", False)),
            "has_geometry": bool(saved.get("has_geometry", False)),
            "has_stroke": bool(saved.get("has_stroke", False)),
            "has_fill": bool(saved.get("has_fill", False)),
            "has_labels": bool(saved.get("has_labels", False)),
            "data_ok": bool(saved.get("data_ok", False)),
        }
        report.units.append(entry)
        if not unit.required:
            continue
        if not entry["bound"] or not entry["has_geometry"]:
            report.missing_representation.append(unit.unit_id)
            continue
        if not (entry["has_stroke"] or entry["has_fill"] or entry["has_labels"]):
            report.invisible_representation.append(unit.unit_id)
    report.ok = (not report.missing_representation
                 and not report.invisible_representation)
    return report

def check_scene_visual_coverage(
    objects: list[Any],
    semantic_slide: Any,
    transform_ledger: Any,
    source_snapshot: str,
    lowered_plan: dict[str, Any],
    rules: Any,
) -> Any:
    """Reject authored scenes whose REQUIRED drawing units have no paint.

    This is a pre-build gate, not a post-emission excuse: every required
    visual unit must belong to a region that declares an applicable fill,
    stroke, foreground, or approved brand asset. An empty ``surface_token``
    on a required decor region is a named repair, never a silent "source
    fill dropped -> invisible frame".
    """
    from .models import VisualCoverageEntry, VisualCoverageReport
    from .semantics import can_omit_source_object

    palette = _palette(rules)
    units = classify_visual_units(
        objects,
        semantic_slide,
        transform_ledger,
        source_snapshot,
        can_omit=can_omit_source_object
        if semantic_slide is not None and transform_ledger is not None
        else None,
    )
    by_id = {str(getattr(o, "id", "") or ""): o for o in objects}
    owners: dict[str, dict[str, Any]] = {}
    for zone in lowered_plan.get("zones", []) or []:
        if not isinstance(zone, dict):
            continue
        for ref in list(zone.get("content_refs", []) or []):
            owners[str(ref)] = zone
    report = VisualCoverageReport()
    for unit in units:
        entry = VisualCoverageEntry(
            unit_id=unit.unit_id,
            source_refs=list(unit.source_refs),
            kind=unit.kind,
            required=unit.required,
        )
        if not unit.required:
            entry.resolution = "not_required"
            report.entries.append(entry)
            continue
        report.expected_required_units.append(unit.unit_id)
        refs = [str(r) for r in unit.source_refs if str(r)]
        regions = [owners.get(ref) for ref in refs]
        missing = [ref for ref, zone in zip(refs, regions, strict=True) if zone is None]
        if missing:
            entry.resolution = "missing_region"
            entry.issues.append("SCENE_VISUAL_COVERAGE_MISSING_REGION")
            report.missing_region.extend(missing)
            report.absent_units.append(unit.unit_id)
            report.issues.append(
                f"SCENE_VISUAL_COVERAGE_MISSING_REGION:{unit.unit_id}:{','.join(missing)}"
            )
            report.entries.append(entry)
            continue
        resolved = True
        for ref, zone in zip(refs, regions, strict=True):
            surface = str(zone.get("surface_token", "") or "") or str(
                zone.get("fill_token", "") or ""
            )
            stroke = str(zone.get("stroke_token", "") or "")
            foreground = str(zone.get("foreground_token", "") or "")
            entry.region_ref = entry.region_ref or str(zone.get("zone_id", "") or "")
            entry.surface_token = entry.surface_token or surface
            entry.stroke_token = entry.stroke_token or stroke
            entry.foreground_token = entry.foreground_token or foreground
            obj = by_id.get(ref)
            kind = str(getattr(obj, "kind", "") or "")
            payload = getattr(obj, "payload", None)
            has_asset = bool(
                getattr(payload, "asset", None) if payload is not None else None
            )
            if kind == "image" or (unit.kind == "brand" and has_asset):
                entry.resolution = "brand_asset"
                continue
            if unit.kind in ("chart", "table"):
                entry.resolution = "painted"
                continue
            if surface and surface in palette:
                entry.resolution = "painted"
                continue
            if stroke and stroke in palette:
                entry.resolution = "painted"
                continue
            if foreground and foreground in palette:
                entry.resolution = "text_foreground"
                continue
            # Source line/fill is never a normative token. Only a declared
            # palette token can discharge coverage.
            resolved = False
            entry.resolution = "unresolved"
            entry.issues.append(f"SCENE_VISUAL_COVERAGE_UNRESOLVED:{ref}")
            report.unresolved.append(ref)
            report.issues.append(
                f"SCENE_VISUAL_COVERAGE_UNRESOLVED:{unit.unit_id}:{ref}:"
                f"surface={surface!r}:stroke={stroke!r}:foreground={foreground!r}"
            )
        if resolved:
            report.present_units.append(unit.unit_id)
        else:
            report.absent_units.append(unit.unit_id)
        report.entries.append(entry)
    report.ok = not report.absent_units
    return report
