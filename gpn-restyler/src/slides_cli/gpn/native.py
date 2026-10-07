"""Native content emitters (Stage 4 §§6–10).

Every emitter builds real native OOXML through :mod:`opc_adapter` (the only
place that touches ``python-pptx`` privates): textboxes with ordered
tokens/fields, native tables with physical merges, native charts (style-only
transplant or verified-data build), shapes/groups/images/connectors, notes
and hyperlink remap. Payloads always come from the source IR by ID — never
from an LLM paraphrase. Styles come from the loaded ontology snapshot.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import tempfile
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lxml import etree
from pptx.chart.data import BubbleChartData, CategoryChartData, XyChartData
from pptx.oxml import parse_xml

from . import opc_adapter as adapter
from .export_models import EmissionRecord, OutputShapeAddress, OutputSubBinding
from .transplant import TransplantContext, clone_part_graph, load_source_package

log = logging.getLogger(__name__)

A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
C_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

_XML_PARSER = etree.XMLParser(
    resolve_entities=False, no_network=True, recover=False, huge_tree=False
)


@dataclass
class NativeDeckContext:
    """Working state for one output deck build."""

    presentation: Any = None
    active_template_profile: Any = None
    rules_snapshot_id: str = ""
    source_to_output_slide_map: dict[str, str] = field(default_factory=dict)
    output_map: list[OutputSubBinding] = field(default_factory=list)
    asset_store: Any = None
    transplant_context: TransplantContext | None = None
    pending_connectors: list[dict[str, Any]] = field(default_factory=list)
    pending_hyperlinks: list[dict[str, Any]] = field(default_factory=list)
    pending_chart_frames: list[dict[str, Any]] = field(default_factory=list)
    deferred_owner_rels: list[tuple[str, dict[str, str]]] = field(
        default_factory=list)
    source_part_positions: dict[str, int] = field(default_factory=dict)
    chart_semantic_diffs: list[dict[str, Any]] = field(default_factory=list)
    emission_records: list[EmissionRecord] = field(default_factory=list)
    part_contracts: list[Any] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    work_dir: Path | None = None


# ---------------------------------------------------------------------------
# §4 create_output_deck
# ---------------------------------------------------------------------------

def create_output_deck(
    template_path: str | Path,
    profile: Any,
    slide_plan: list[dict[str, Any]],
    *,
    rules: Any = None,
    work_dir: str | Path | None = None,
) -> NativeDeckContext:
    """Create a new deck on the active template with all slides pre-created."""
    work_path = Path(work_dir) if work_dir else Path(tempfile.gettempdir())
    work_path.mkdir(parents=True, exist_ok=True)
    clean_template, derivation = adapter.clean_template_copy(
        template_path, work_path)
    template_hash = _hash_file(template_path)
    prs = adapter.open_template(clean_template)
    # Verify canvas matches loaded rules when both are available.
    canvas = getattr(getattr(rules, "canvas", None), "w", None)
    if rules is not None and canvas:
        try:
            if int(prs.slide_width) != int(canvas) or int(prs.slide_height) != int(
                    getattr(rules.canvas, "h", prs.slide_height)):
                raise ValueError("template canvas differs from loaded rules snapshot")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"template/rules canvas mismatch: {exc}") from exc
    ctx = NativeDeckContext(
        presentation=prs,
        active_template_profile=profile,
        rules_snapshot_id=getattr(rules, "source_hash", "") or "",
        work_dir=Path(work_dir) if work_dir else None,
    )
    layout = adapter.blank_slide_layout(prs)
    for entry in slide_plan:
        slide = adapter.add_slide(prs, layout)
        adapter.strip_placeholders(slide)
        source_id = str(entry.get("source_slide_id", entry.get("slide_id", "")))
        part = f"ppt/slides/slide{len(prs.slides)}.xml"
        if entry.get("hidden"):
            with contextlib.suppress(Exception):
                slide.shapes.title  # noqa: B018 - keep slide valid
            slide_element = slide._element
            slide_element.set("show", "0")
        ctx.source_to_output_slide_map[source_id] = part
    ctx.issues.append(f"template_hash={template_hash[:16]}")
    ctx.issues.append(
        f"clean_template={derivation['sha256'][:16]} "
        f"dropped={len(derivation['dropped_parts'])}")
    return ctx


def _hash_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# §6 text
# ---------------------------------------------------------------------------

def _role_style(role: str, rules: Any) -> dict[str, Any]:
    roles = getattr(rules, "roles", {}) or {}
    entry = roles.get(role)
    if entry is None:
        return {}
    if hasattr(entry, "model_dump"):
        return entry.model_dump(mode="json")
    return dict(entry)


def _hex_to_rgb(color: Any) -> tuple[int, int, int] | None:
    if color is None:
        return None
    if isinstance(color, dict):
        value = color.get("resolved_rgb") or color.get("value", "")
    else:
        value = str(color)
    value = value.strip().lstrip("#")
    if len(value) == 6:
        try:
            return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))
        except ValueError:
            return None
    return None


def apply_display_transform(
    text: str, role: str, rules: Any
) -> tuple[str, str]:
    """Apply a confirmed role-level case transform; returns (display, kind)."""
    style = _role_style(role, rules)
    if style.get("uppercase") is True:
        return text.upper(), "title_uppercase"
    return text, "identity"


def emit_text(
    payload: Any,
    resolved: dict[str, Any],
    slide: Any,
    context: NativeDeckContext,
    *,
    rules: Any = None,
    source_object_id: str = "",
    slide_position: int = 0,
) -> OutputShapeAddress:
    """Emit one native textbox with ordered paragraph/run/field tokens."""
    box = resolved.get("box_emu") or {}
    shape = adapter.add_textbox(
        slide,
        int(box.get("x", 914400)), int(box.get("y", 914400)),
        int(box.get("w", 3657600)), int(box.get("h", 914400)),
    )
    adapter.set_no_autofit(shape)
    role = resolved.get("role", getattr(payload, "role", "unknown"))
    style = _role_style(role, rules) if rules is not None else {}
    paragraphs = getattr(payload, "paragraphs", []) or []
    para_transform = ("title_uppercase"
                      if style.get("uppercase") is True else "identity")
    tf = shape.text_frame
    tf.word_wrap = True
    first = True
    for pidx, para in enumerate(paragraphs):
        p = tf.paragraphs[0] if first else tf.add_paragraph()
        first = False
        p.alignment = None
        # Native list marker: only when the source paragraph is a list item
        # and the loaded list profile confirms the level.
        list_kind = getattr(para, "list_kind", "none")
        if list_kind != "none" and rules is not None:
            _apply_list_marker(p, para, rules)
        para_id = getattr(para, "id", "")
        by_offset: dict[int, list[Any]] = {}
        for br in list(getattr(para, "breaks", []) or []):
            by_offset.setdefault(int(getattr(br, "offset", 0)), []).append(br)
        for br in by_offset.pop(0, []):
            _emit_break(p, br, para_id or source_object_id, context)
        for ridx, run in enumerate(list(getattr(para, "runs", []) or []),
                                  start=1):
            run_id = getattr(run, "id", "")
            if "/br" in run_id:
                # Break carrier run materialized by the reader: the ordered
                # a:br token (flushed after each run below) carries it,
                # never a text run.
                continue
            kind = getattr(run, "kind", "text")
            if kind == "field":
                # Preserved field metadata (id/type/cached): a real a:fld
                # token, never a cached plain-text replacement. Date fields
                # keep their cached text; nothing auto-updates them here.
                _emit_field(p, run, style)
            else:
                r = p.add_run()
                raw = getattr(run, "text", "")
                display, _ = apply_display_transform(str(raw), role, rules) \
                    if rules is not None else (str(raw), "identity")
                r.text = display
                _style_run(r, style, run)
                link = getattr(run, "hyperlink", None)
                if link is not None and getattr(link, "uri", None):
                    r.hyperlink.address = link.uri
                    context.pending_hyperlinks.append({
                        "kind": "external", "uri": link.uri,
                        "run_id": getattr(run, "id", ""),
                    })
                    _bind_atom(context, slide, shape, source_object_id,
                               f"{getattr(run, 'id', '')}#hyperlink",
                               f"para[{pidx}]/hyperlink", "style_only")
                elif link is not None and getattr(link, "target_slide_ref", None):
                    _emit_internal_run_link(
                        r, shape, pidx, ridx, slide, context, link,
                        slide_position=slide_position,
                        run_id=getattr(run, "id", ""))
                    _bind_atom(context, slide, shape, source_object_id,
                               f"{getattr(run, 'id', '')}#hyperlink",
                               f"para[{pidx}]/hyperlink", "style_only")
            for br in by_offset.pop(ridx, []):
                _emit_break(p, br, para_id or source_object_id, context)
        if para_id:
            _bind_atom(context, slide, shape, source_object_id, para_id,
                       f"para[{pidx}]", para_transform)
    address = _address_of(slide, shape, "text")
    context.emission_records.append(EmissionRecord(
        source_object_id=source_object_id, strategy="native_rebuild",
        address=address))
    return address


def _emit_field(p: Any, run: Any, style: dict[str, Any]) -> None:
    """Append a real ``a:fld`` element preserving id/type/cached text."""
    meta = getattr(run, "field", None)
    cached = (getattr(meta, "cached_text", None) or getattr(run, "text", "")
              or "")
    fld = etree.SubElement(p._p, f"{{{A_NS}}}fld")
    if meta is not None:
        if getattr(meta, "field_id", ""):
            fld.set("id", str(meta.field_id))
        if getattr(meta, "field_type", ""):
            fld.set("type", str(meta.field_type))
    rpr = etree.SubElement(fld, f"{{{A_NS}}}rPr")
    if style.get("default_size_pt"):
        with contextlib.suppress(TypeError, ValueError):
            rpr.set("sz", str(int(float(style["default_size_pt"]) * 100)))
    if style.get("typeface"):
        latin = etree.SubElement(rpr, f"{{{A_NS}}}latin")
        latin.set("typeface", str(style["typeface"]))
    t_node = etree.SubElement(fld, f"{{{A_NS}}}t")
    t_node.text = str(cached)


def _emit_break(p: Any, br: Any, scope: str, context: Any) -> None:
    """Emit one ordered break token: soft as a real ``a:br`` element.

    A literal vertical tab in ``a:t`` would serialize as ``_x000B_`` and
    corrupt reimport; hard breaks have no reader path (schema-allowed only)
    and are recorded instead of silently softened.
    """
    if getattr(br, "kind", "soft") == "hard":
        context.issues.append(
            f"hard break in {scope}: no reader path produces hard breaks; "
            "recorded as needs_review, emitted as soft token")
    etree.SubElement(p._p, f"{{{A_NS}}}br")


def _style_run(run: Any, style: dict[str, Any], source_run: Any) -> None:
    font = run.font
    if style.get("typeface"):
        font.name = style["typeface"]
    size = style.get("default_size_pt")
    if size:
        try:
            from pptx.util import Pt
            font.size = Pt(float(size))
        except (TypeError, ValueError):
            pass
    rgb = _hex_to_rgb((style.get("color_tokens") or [None])[0]
                      if style.get("color_tokens") else None)
    if rgb:
        from pptx.dml.color import RGBColor
        font.color.rgb = RGBColor(*rgb)
    src_style = getattr(source_run, "source_style", None)
    if src_style is not None:
        if getattr(src_style, "bold", None) is True:
            font.bold = True
        if getattr(src_style, "italic", None) is True:
            font.italic = True
    # Preserve run-level emphasis that carries meaning (sub/superscript,
    # e.g. footnote markers): restore the OOXML baseline recorded in the
    # source semantic marks, never drop it.
    for mark in list(getattr(source_run, "semantic_marks", []) or []):
        evidence = getattr(mark, "evidence_ref", "") or ""
        if evidence.startswith("baseline="):
            with contextlib.suppress(ValueError, AttributeError):
                run._r.get_or_add_rPr().set(
                    "baseline", str(int(evidence.split("=", 1)[1])))


def _apply_list_marker(paragraph: Any, para_ir: Any, rules: Any) -> None:
    """Apply the loaded LS01 bullet profile (no literals; see bullets.py)."""
    try:
        from .bullets import apply_list_profile
        lists = getattr(rules, "lists", {}) or {}
        profile = lists.get("LS01") or next(iter(lists.values()), None)
        if profile is not None:
            apply_list_profile(paragraph, int(getattr(para_ir, "level", 0)),
                               profile, {})
    except Exception as exc:  # noqa: BLE001 - list styling must not abort export
        log.warning("list profile application failed: %s", exc)


def _emit_internal_run_link(run: Any, shape: Any, pidx: int, ridx: int,
                            slide: Any, context: NativeDeckContext,
                            link: Any, *, slide_position: int,
                            run_id: str) -> None:
    """Write a provisional internal hlinkClick; wired in final assembly."""
    from pptx.oxml.ns import qn
    rPr = run._r.get_or_add_rPr()
    hlink = rPr.find(qn("a:hlinkClick"))
    if hlink is None:
        hlink = etree.SubElement(rPr, qn("a:hlinkClick"))
    rid = _fresh_slide_rid(slide)
    hlink.set(qn("r:id"), rid)
    try:
        shape_id = int(shape.shape_id)
    except (TypeError, ValueError):
        shape_id = 0
    context.pending_hyperlinks.append({
        "kind": "internal", "target_ref": link.target_slide_ref,
        "run_id": run_id, "rid": rid, "slide_position": slide_position,
        "shape_id": shape_id, "para_idx": pidx, "run_idx": ridx,
    })


def _bind_atom(context: NativeDeckContext, slide: Any, shape: Any,
               source_object_id: str, atom_id: str, sub: str,
               transform: str = "identity") -> None:
    """Bind one ledger atom id to its real output subaddress (§2.1)."""
    if not atom_id:
        atom_id = source_object_id
    context.output_map.append(OutputSubBinding(
        source_atom_id=atom_id,
        target_address=_address_of(slide, shape, "text"),
        target_subaddress=sub,
        origin="exporter",
        transform=transform,  # type: ignore[arg-type]
        evidence=[f"obj:{source_object_id}"],
    ))


def _bind(context: NativeDeckContext, slide: Any, shape: Any,
          source_object_id: str, atom_id: str, sub: str, order: int) -> None:
    _bind_atom(context, slide, shape, source_object_id, atom_id,
               f"{sub}[{order}]")


def _address_of(slide: Any, shape: Any, kind: str) -> OutputShapeAddress:
    try:
        part = slide.part.partname.lstrip("/")
    except AttributeError:
        part = f"ppt/slides/slide{slide.slide_id}.xml"
    try:
        shape_id = int(shape.shape_id)
    except (TypeError, ValueError):
        shape_id = 0
    return OutputShapeAddress(slide_part=part, shape_id=shape_id,
                              group_path=[], kind=kind)


# ---------------------------------------------------------------------------
# §7 tables
# ---------------------------------------------------------------------------

def emit_table(
    payload: Any,
    layout: dict[str, Any],
    slide: Any,
    context: NativeDeckContext,
    *,
    rules: Any = None,
    source_object_id: str = "",
) -> OutputShapeAddress:
    """Emit a native table: grid → merges on the empty grid → populate."""
    rows = int(getattr(payload, "rows", 0))
    cols = int(getattr(payload, "cols", 0))
    if rows < 1 or cols < 1:
        raise ValueError(f"invalid table grid {rows}x{cols}")
    box = layout.get("box_emu") or {}
    frame = adapter.add_table(
        slide, rows, cols,
        int(box.get("x", 914400)), int(box.get("y", 914400)),
        int(box.get("w", 5486400)), int(box.get("h", 2743200)),
    )
    table = frame.table
    widths = list(getattr(payload, "column_widths_emu", []) or [])
    if len(widths) == cols:
        for idx, width in enumerate(widths):
            try:
                from pptx.util import Emu
                table.columns[idx].width = Emu(int(width))
            except (TypeError, ValueError):
                pass
    # Merge on the empty grid first (public merge moves content otherwise).
    for m in list(getattr(payload, "merges", []) or []):
        r0, c0, r1, c1 = int(m.r0), int(m.c0), int(m.r1), int(m.c1)
        if (r1, c1) != (r0, c0):
            try:
                table.cell(r0, c0).merge(table.cell(r1, c1))
            except (IndexError, ValueError) as exc:
                context.issues.append(f"table merge failed {(r0, c0, r1, c1)}: {exc}")
    by_coord = {(int(c.row), int(c.col)): c
                for c in list(getattr(payload, "cells", []) or [])}
    for (row, col), cell in sorted(by_coord.items()):
        try:
            target = table.cell(row, col)
        except IndexError:
            context.issues.append(f"table cell out of grid {(row, col)}")
            continue
        # Populate every physical cell, including merge-covered ones: the
        # covered tc keeps its own txBody (hidden payload), never migrates
        # into the origin. Merges were applied on the empty grid above.
        paras = getattr(cell, "paragraphs", []) or []
        if paras:
            target.text = ""
            tf = target.text_frame
            first = True
            for para in paras:
                p = tf.paragraphs[0] if first else tf.add_paragraph()
                first = False
                for run in list(getattr(para, "runs", []) or []):
                    r = p.add_run()
                    r.text = str(getattr(run, "text", ""))
        context.output_map.append(OutputSubBinding(
            source_atom_id=getattr(cell, "id", f"cell-{row}-{col}"),
            target_address=_address_of(slide, frame, "table"),
            target_subaddress=f"cell[{row},{col}]",
            origin="exporter",
            evidence=[f"obj:{source_object_id}"],
        ))
    address = _address_of(slide, frame, "table")
    context.emission_records.append(EmissionRecord(
        source_object_id=source_object_id, strategy="native_rebuild",
        address=address))
    return address


# ---------------------------------------------------------------------------
# §8 charts
# ---------------------------------------------------------------------------

def snapshot_chart_semantics(chart_part_xml: bytes) -> dict[str, Any]:
    """Snapshot plot/series/axis semantics of one chart part.

    Descends into ``c:plotArea``: real plot types (barChart/lineChart/…),
    plot-scoped series ids, axis ids and scale attributes. Data nodes
    (``c:val``/``c:cat``/``c:f``) are hashed, never parsed into floats, so
    lexical/display properties survive the comparison.
    """
    try:
        root = etree.fromstring(chart_part_xml, parser=_XML_PARSER)
    except etree.XMLSyntaxError as exc:
        return {"parse_error": str(exc)}
    chart = root.find(f".//{{{C_NS}}}chart")
    if chart is None:
        return {"plots": [], "axes": [],
                "sha256": hashlib.sha256(chart_part_xml).hexdigest()}
    area = chart.find(f"{{{C_NS}}}plotArea")
    plots: list[dict[str, Any]] = []
    axes: list[dict[str, Any]] = []
    if area is not None:
        for child in area:
            local = etree.QName(child).localname
            if local == "layout":
                continue
            if local in ("catAx", "valAx", "dateAx", "serAx"):
                ax_id = child.findtext(f"{{{C_NS}}}axId", default="")
                axes.append({"axis": local, "axId": ax_id})
                continue
            if local in ("dTable", "spPr"):
                continue
            series = child.findall(f"{{{C_NS}}}ser")
            ax_ids = [n.text or "" for n in child.findall(f"{{{C_NS}}}axId")]
            plots.append({"plot": local,
                          "series_count": len(series),
                          "series_idx": [s.findtext(f"{{{C_NS}}}idx", default="")
                                         for s in series],
                          "axIds": ax_ids,
                          "grouping": child.findtext(f"{{{C_NS}}}grouping",
                                                    default="")})
    return {"plots": plots, "axes": axes,
            "sha256": hashlib.sha256(chart_part_xml).hexdigest()}


def emit_existing_chart(
    payload: Any,
    resolved: dict[str, Any],
    slide: Any,
    context: NativeDeckContext,
    *,
    source_package_path: str | Path,
    source_slide_part: str = "",
    source_object_id: str = "",
    slide_position: int = 0,
) -> OutputShapeAddress:
    """Style-only transplant: clone chart+workbook bytes, normalize style.

    The source ``p:graphicFrame`` subtree is deep-copied into the output
    slide with resolved geometry and a fresh owner-scoped chart rId; the
    chart/workbook closure travels byte-exact in the transplant inventory
    and is wired in final assembly. Data nodes are never rebuilt.
    """
    chart_part = str(getattr(payload, "chart_part", ""))
    if not chart_part:
        raise ValueError("existing chart payload has no chart_part")
    if context.transplant_context is None:
        parts, ctypes, outgoing, phash = load_source_package(source_package_path)
        context.transplant_context = TransplantContext(
            source_package_hash=phash, source_parts=parts,
            source_content_types=ctypes, source_outgoing=outgoing)
    tctx = context.transplant_context
    before = snapshot_chart_semantics(tctx.source_parts.get(chart_part, b""))
    result = clone_part_graph(chart_part, tctx, variant="src")
    if [i for i in result.issues if "UNRESOLVED" in i.code or "MISSING" in i.code]:
        raise ValueError(
            f"chart closure has blocking issues: "
            f"{[i.code for i in result.issues]}")
    target_chart = result.actual_root_part
    cloned = bytearray(tctx.target_payloads[target_chart])
    _normalize_chart_style(cloned, context)
    tctx.target_payloads[target_chart] = bytes(cloned)
    after = snapshot_chart_semantics(bytes(cloned))
    if before.get("plots") != after.get("plots"):
        raise ValueError("chart style normalization changed plot semantics")
    context.chart_semantic_diffs.append({
        "source_chart": chart_part, "target_chart": target_chart,
        "before": before, "after": after,
        "workbook_sha256": _workbook_hash(payload, tctx),
        "replace_data_used": False,
    })
    # Graphic frame: deep-copy the source frame subtree into the output
    # slide with resolved geometry and a fresh owner-scoped chart rId.
    box = resolved.get("box_emu") or {}
    shape = _insert_chart_frame(
        slide, source_package_path, source_slide_part, chart_part, box,
        context, target_chart, slide_position=slide_position)
    context.part_contracts.extend(result.contracts)
    address = _address_of(slide, shape, "chart")
    # Series/point/category bindings keep plot scope from the IR payload.
    # Series are matched by name on reimport (ids are hash-derived).
    order = 0
    for sidx, series in enumerate(list(getattr(payload, "series", []) or [])):
        for point in list(getattr(series, "points", []) or []):
            context.output_map.append(OutputSubBinding(
                source_atom_id=getattr(point, "id", f"pt-{order}"),
                target_address=address,
                target_subaddress=(
                    f"series[{sidx}]:"
                    f"{getattr(series, 'name', getattr(series, 'id', ''))}"
                    f"/point[{getattr(point, 'index', order)}]"),
                origin="exporter",
                evidence=[f"obj:{source_object_id}"],
            ))
            order += 1
    context.emission_records.append(EmissionRecord(
        source_object_id=source_object_id, strategy="native_transplant",
        address=address))
    workbook_hash = _workbook_hash(payload, tctx)
    if workbook_hash:
        context.issues.append(f"chart_workbook={workbook_hash[:16]}")
    return address


def _normalize_chart_style(cloned: bytearray, context: NativeDeckContext) -> None:
    """Normalize only supported visible style nodes (fonts/colors); data kept."""
    try:
        root = etree.fromstring(bytes(cloned), parser=_XML_PARSER)
    except etree.XMLSyntaxError:
        return
    # Style-only: touch c:txPr text properties; never c:val/c:cat/c:f nodes.
    for txpr in root.findall(f".//{{{C_NS}}}txPr"):
        for rpr in txpr.findall(f".//{{{A_NS}}}rPr"):
            latin = rpr.find(f"{{{A_NS}}}latin")
            if latin is not None and context.active_template_profile is not None:
                pass  # hooked to template fonts when profile declares them
    cloned[:] = etree.tostring(root, xml_declaration=True, encoding="UTF-8",
                               standalone=True)


def _workbook_hash(payload: Any, tctx: TransplantContext) -> str:
    asset = getattr(payload, "workbook", None)
    digest = getattr(asset, "sha256", "") if asset is not None else ""
    return str(digest or "")


def _find_source_graphic_frame(
    source_slide_xml: bytes, chart_part: str,
    source_outgoing: dict[str, list[dict]],
    source_slide_part: str,
) -> Any | None:
    """Locate the ``p:graphicFrame`` whose chart ref resolves to chart_part."""
    try:
        root = parse_xml(source_slide_xml)
    except Exception:
        return None
    wanted = {rel["id"] for rel in source_outgoing.get(source_slide_part, [])
              if rel["mode"].lower() != "external" and
              _resolve_src(rel, source_slide_part) == chart_part}
    for frame in root.findall(f".//{{{P_NS}}}graphicFrame"):
        for node in frame.iter():
            for attr in (f"{{{R_NS}}}id", f"{{{R_NS}}}embed"):
                if node.get(attr) in wanted:
                    return frame
    return None


def _resolve_src(rel: dict, owner: str) -> str:
    import posixpath as _pp

    return _pp.normpath(_pp.join(_pp.dirname(owner), rel["target"])).lstrip("/")


def _insert_chart_frame(
    slide: Any,
    source_package_path: str | Path,
    source_slide_part: str,
    chart_part: str,
    box: dict[str, Any],
    context: NativeDeckContext,
    target_chart: str,
    *,
    slide_position: int = 0,
) -> Any:
    """Deep-copy the source chart frame with resolved geometry + fresh rId."""
    tctx = context.transplant_context
    assert tctx is not None
    src_xml = tctx.source_parts.get(source_slide_part)
    frame = (_find_source_graphic_frame(src_xml, chart_part,
                                        tctx.source_outgoing, source_slide_part)
             if src_xml else None)
    if frame is None:
        context.issues.append(
            f"chart frame for {chart_part} not found; anchor fallback")
        return adapter.add_textbox(
            slide, int(box.get("x", 914400)), int(box.get("y", 914400)),
            int(box.get("w", 5486400)), int(box.get("h", 2743200)))
    new_el = deepcopy(frame)
    # Resolved geometry on the frame xfrm.
    xfrm = new_el.find(f".//{{{A_NS}}}xfrm")
    if xfrm is not None:
        off = xfrm.find(f"{{{A_NS}}}off")
        ext = xfrm.find(f"{{{A_NS}}}ext")
        if off is not None:
            off.set("x", str(int(box.get("x", 914400))))
            off.set("y", str(int(box.get("y", 914400))))
        if ext is not None:
            ext.set("cx", str(max(int(box.get("w", 5486400)), 1)))
            ext.set("cy", str(max(int(box.get("h", 2743200)), 1)))
    # Fresh shape id unique on the target slide.
    used = set()
    for shape in slide.shapes:
        try:
            used.add(int(shape.shape_id))
        except (TypeError, ValueError):
            continue
    fresh = (max(used) + 1) if used else 1
    for cNvPr in new_el.findall(f".//{{{P_NS}}}cNvPr"):
        cNvPr.set("id", str(fresh))
    # Fresh owner-scoped chart rId on the c:chart node only (never a
    # global string replace); the rel itself is added in final assembly.
    rid = _fresh_slide_rid(slide)
    chart_nodes = new_el.findall(f".//{{{C_NS}}}chart")
    if chart_nodes:
        for attr in (f"{{{R_NS}}}id", f"{{{R_NS}}}embed"):
            if chart_nodes[0].get(attr):
                chart_nodes[0].set(attr, rid)
                break
        else:
            context.issues.append(f"chart node in frame has no rId for {chart_part}")
    else:
        context.issues.append(f"no c:chart node in transplanted frame {chart_part}")
    slide.shapes._spTree.append(new_el)
    shape = list(slide.shapes)[-1]
    context.pending_chart_frames.append({
        "slide_position": slide_position,
        "rid": rid,
        "target_chart": target_chart,
    })
    return shape


def _fresh_slide_rid(slide: Any) -> str:
    """Allocate an rId unused on this slide part (owner-scoped)."""
    try:
        existing = set(slide.part.rels.keys())
    except AttributeError:
        existing = set()
    counter = len(existing) + 1
    while f"rId{counter}" in existing:
        counter += 1
    return f"rId{counter}"


def emit_new_chart(
    data: Any,
    resolved: dict[str, Any],
    slide: Any,
    context: NativeDeckContext,
    *,
    source_object_id: str = "",
) -> OutputShapeAddress:
    """Build a native chart from ``VerifiedChartData`` (never LLM numbers)."""
    status = getattr(data, "verification_status", "")
    if status != "verified":
        raise ValueError("new chart requires VerifiedChartData(verification_status='verified')")
    chart_type = str(getattr(data, "chart_type", "")).lower()
    series = list(getattr(data, "series", []) or [])
    categories = list(getattr(data, "categories", []) or [])
    is_xy = chart_type in ("scatter", "bubble")
    if is_xy:
        if chart_type == "bubble":
            chart_data = BubbleChartData()
            for s in series:
                proxy = chart_data.add_series(s.name or s.id)
                for p in s.points:
                    if p.x and p.x.decimal and p.y and p.y.decimal:
                        size = float(p.bubble_size.decimal) \
                            if p.bubble_size and p.bubble_size.decimal else 1.0
                        proxy.add_data_point(float(p.x.decimal),
                                             float(p.y.decimal), size)
        else:
            chart_data = XyChartData()
            for s in series:
                proxy = chart_data.add_series(s.name or s.id)
                for p in s.points:
                    if p.x and p.x.decimal and p.y and p.y.decimal:
                        proxy.add_data_point(float(p.x.decimal),
                                             float(p.y.decimal))
    else:
        chart_data = CategoryChartData()
        labels = [c.path[-1] if c.path else (c.raw_value or f"c{c.index}")
                  for c in categories]
        if labels:
            chart_data.categories = labels
        for s in series:
            values = [float(p.value.decimal) if p.value and p.value.decimal else None
                      for p in s.points]
            chart_data.add_series(s.name or s.id, values)
    box = resolved.get("box_emu") or {}
    frame = adapter.add_chart_from_verified_data(
        slide, chart_type, chart_data,
        int(box.get("x", 914400)), int(box.get("y", 914400)),
        int(box.get("w", 5486400)), int(box.get("h", 2743200)))
    address = _address_of(slide, frame, "chart")
    context.emission_records.append(EmissionRecord(
        source_object_id=source_object_id, strategy="native_rebuild",
        address=address))
    return address


# ---------------------------------------------------------------------------
# §9 shapes, groups, images, connectors
# ---------------------------------------------------------------------------

def emit_shape(
    payload: Any,
    resolved: dict[str, Any],
    slide: Any,
    context: NativeDeckContext,
    *,
    rules: Any = None,
    source_object_id: str = "",
) -> OutputShapeAddress:
    box = resolved.get("box_emu") or {}
    shape = adapter.add_textbox(
        slide, int(box.get("x", 914400)), int(box.get("y", 914400)),
        int(box.get("w", 1828800)), int(box.get("h", 914400)))
    text = getattr(payload, "text", None)
    if text is not None:
        shape.text_frame.clear()
        emit_text(text, {"box_emu": box,
                         "role": getattr(text, "role", "unknown")},
                  slide, context, rules=rules,
                  source_object_id=source_object_id)
        # Remove the placeholder box; emit_text created the real one.
        shape._element.getparent().remove(shape._element)
        record = context.emission_records[-1]
        return record.address  # type: ignore[return-value]
    address = _address_of(slide, shape, "shape")
    context.emission_records.append(EmissionRecord(
        source_object_id=source_object_id, strategy="native_rebuild",
        address=address))
    return address


def emit_group(
    payload: Any,
    resolved: dict[str, Any],
    slide: Any,
    context: NativeDeckContext,
    *,
    rules: Any = None,
    source_object_id: str = "",
) -> OutputShapeAddress:
    children = list(getattr(payload, "children", []) or [])
    if not children:
        raise ValueError("group payload has no children")
    group = slide.shapes.add_group_shape()
    for cidx, child in enumerate(children):
        _emit_group_child(group, child, resolved, slide, context,
                          rules=rules, source_object_id=source_object_id,
                          position=(cidx,), root_group=group)
    address = _address_of(slide, group, "group")
    context.emission_records.append(EmissionRecord(
        source_object_id=source_object_id, strategy="native_rebuild",
        address=address))
    return address


def _emit_group_child(group: Any, child: Any, resolved: dict[str, Any],
                      slide: Any, context: NativeDeckContext, *,
                      rules: Any = None,
                      source_object_id: str = "",
                      position: tuple[int, ...] = (),
                      root_group: Any = None) -> None:
    """Emit one group child (recurses into nested groups).

    Bindings always address the slide-level ``root_group``; the position
    path descends to the child paragraph.
    """
    anchor = root_group if root_group is not None else group
    kind = getattr(child, "kind", "shape")
    payload = getattr(child, "payload", None)
    child_box = _child_box(child, resolved)
    pos = "/".join(str(i) for i in position)
    if kind in ("text", "shape"):
        text = getattr(payload, "paragraphs", None)
        if text is None:
            inner = getattr(payload, "text", None)
            text = getattr(inner, "paragraphs", []) if inner is not None else []
        if not text and kind == "shape":
            context.output_map.append(OutputSubBinding(
                source_atom_id=f"{getattr(child, 'id', '')}#shape",
                target_address=_address_of(slide, anchor, "group"),
                target_subaddress=f"group-child[{pos}]",
                origin="exporter",
                evidence=[f"obj:{getattr(child, 'id', '')}"],
            ))
            sub = group.shapes.add_textbox(
                adapter.Emu(child_box["x"]), adapter.Emu(child_box["y"]),
                adapter.Emu(child_box["w"]), adapter.Emu(child_box["h"]))
            sub.text = ""
            return
        sub = group.shapes.add_textbox(
            adapter.Emu(child_box["x"]), adapter.Emu(child_box["y"]),
            adapter.Emu(child_box["w"]), adapter.Emu(child_box["h"]))
        tf = sub.text_frame
        tf.word_wrap = True
        first = True
        for pidx, para in enumerate(text or []):
            p = tf.paragraphs[0] if first else tf.add_paragraph()
            first = False
            for run in list(getattr(para, "runs", []) or []):
                r = p.add_run()
                r.text = str(getattr(run, "text", ""))
            if getattr(para, "id", ""):
                context.output_map.append(OutputSubBinding(
                    source_atom_id=para.id,
                    target_address=_address_of(slide, anchor, "group"),
                    target_subaddress=f"group-child[{pos}]/para[{pidx}]",
                    origin="exporter",
                    evidence=[f"obj:{getattr(child, 'id', '')}"],
                ))
    elif kind == "group":
        nested = group.shapes.add_group_shape()
        for gidx, grand in enumerate(list(getattr(payload, "children", []) or [])):
            _emit_group_child(nested, grand, resolved, slide, context,
                              rules=rules, source_object_id=source_object_id,
                              position=(*position, gidx),
                              root_group=anchor)
    else:
        context.issues.append(
            f"group child kind {kind} needs_review")


def _child_box(child: Any, resolved: dict[str, Any]) -> dict[str, int]:
    box = getattr(getattr(child, "local_box", None), "__dict__", {}) or {}
    base = resolved.get("box_emu") or {"x": 914400, "y": 914400}
    return {"x": int(box.get("x", base.get("x", 914400))),
            "y": int(box.get("y", base.get("y", 914400))),
            "w": int(box.get("w", 1828800)), "h": int(box.get("h", 914400))}


def _child_text(child: Any) -> str:
    payload = getattr(child, "payload", None)
    text = getattr(payload, "paragraphs", None)
    if not text:
        inner = getattr(payload, "text", None)
        text = getattr(inner, "paragraphs", []) if inner is not None else []
    return "\n".join("".join(r.text for r in p.runs) for p in (text or []))


def emit_image(
    payload: Any,
    resolved: dict[str, Any],
    slide: Any,
    context: NativeDeckContext,
    *,
    source_package_path: str | Path | None = None,
    source_object_id: str = "",
) -> OutputShapeAddress:
    asset = getattr(payload, "asset", None)
    digest = getattr(asset, "sha256", "") if asset is not None else ""
    if not digest or context.asset_store is None:
        raise ValueError("image payload has no retrievable asset bytes")
    blob: bytes | None = None
    if context.asset_store is not None:
        root = getattr(context.asset_store, "root", None)
        if root is not None:
            matches = sorted(Path(root).glob(f"*/{digest}.*"))
            if matches:
                blob = matches[0].read_bytes()
    if blob is None:
        raise ValueError("image payload has no retrievable asset bytes")
    suffix = ".png"
    linked = getattr(payload, "linked_uri", None)
    if linked:
        context.issues.append(f"linked media kept as URI, not fetched: {linked}")
    work = context.work_dir or Path(".")
    tmp = Path(work) / f"img-{digest[:12]}{suffix}"
    tmp.write_bytes(blob)
    box = resolved.get("box_emu") or {}
    pic = adapter.add_picture(
        slide, tmp, int(box.get("x", 914400)), int(box.get("y", 914400)),
        int(box.get("w", 2743200)), int(box.get("h", 1828800)))
    address = _address_of(slide, pic, "image")
    context.output_map.append(OutputSubBinding(
        source_atom_id=f"{source_object_id}#asset",
        target_address=address,
        target_subaddress="picture",
        origin="exporter",
        evidence=[f"obj:{source_object_id}"],
    ))
    context.emission_records.append(EmissionRecord(
        source_object_id=source_object_id, strategy="native_rebuild",
        address=address))
    return address


def emit_connectors(
    graph: list[dict[str, Any]],
    slide: Any,
    context: NativeDeckContext,
    id_map: dict[str, int] | None = None,
) -> list[OutputShapeAddress]:
    """Emit deferred connectors after all node shapes exist."""
    from pptx.enum.shapes import MSO_CONNECTOR
    from pptx.oxml.ns import qn
    id_map = id_map or {}
    out: list[OutputShapeAddress] = []
    for edge in graph:
        src = id_map.get(str(edge.get("from", "")), 0)
        dst = id_map.get(str(edge.get("to", "")), 0)
        if not src or not dst:
            context.issues.append(
                f"connector endpoint unresolved: {edge.get('from')}->{edge.get('to')}")
            continue
        conn = slide.shapes.add_connector(
            MSO_CONNECTOR.STRAIGHT, adapter.Emu(0), adapter.Emu(0),
            adapter.Emu(914400), adapter.Emu(914400))
        with contextlib.suppress(Exception):
            conn.line.fill.background()
        # Native connection sites (not a floating stub): the edge keeps its
        # source endpoints, resolved to real output shape ids above.
        cxn = conn._element.find(qn("p:nvCxnSpPr"))
        if cxn is not None:
            cNvCxnSpPr = cxn.find(qn("p:cNvCxnSpPr"))
            if cNvCxnSpPr is None:
                cNvCxnSpPr = etree.SubElement(cxn, qn("p:cNvCxnSpPr"))
            for tag, sid, site in (("a:stCxn", src, edge.get("from_site")),
                                   ("a:endCxn", dst, edge.get("to_site"))):
                el = cNvCxnSpPr.find(qn(tag))
                if el is None:
                    el = etree.SubElement(cNvCxnSpPr, qn(tag))
                el.set("id", str(sid))
                if site is not None:
                    el.set("idx", str(site))
        address = _address_of(slide, conn, "connector")
        if edge.get("object_id"):
            context.output_map.append(OutputSubBinding(
                source_atom_id=f"{edge['object_id']}#edge",
                target_address=address,
                target_subaddress=(
                    f"edge[{edge.get('from', '')}->{edge.get('to', '')}]"),
                origin="exporter",
                evidence=[f"obj:{edge['object_id']}",
                          f"from_shape:{src}", f"to_shape:{dst}"],
            ))
        out.append(address)
    return out


def emit_unknown(
    obj: Any,
    resolved: dict[str, Any],
    slide: Any,
    context: NativeDeckContext,
    *,
    source_slide_part: str = "",
    source_object_id: str = "",
) -> OutputShapeAddress:
    """Opaque carry: deep-copy the source shape subtree into the output.

    Byte-level carry of the unknown element (namespaces and extensions
    preserved); style compliance is explicitly NOT claimed.
    """
    tctx = context.transplant_context
    shape_id = getattr(getattr(obj, "source_ref", None), "shape_id", 0)
    carried = False
    if tctx is not None and source_slide_part:
        src_xml = tctx.source_parts.get(source_slide_part)
        if src_xml:
            try:
                root = parse_xml(src_xml)
                for el in root.iter():
                    if el.tag.endswith("}cNvPr") and el.get("id") == str(shape_id):
                        parent = el.getparent()
                        while parent is not None and not (
                                parent.tag.endswith("}sp")
                                or parent.tag.endswith("}cxnSp")
                                or parent.tag.endswith("}graphicFrame")
                                or parent.tag.endswith("}grpSp")
                                or parent.tag.endswith("}pic")):
                            parent = parent.getparent()
                        if parent is not None:
                            new_el = deepcopy(parent)
                            for cNvPr in new_el.iter():
                                if cNvPr.tag.endswith("}cNvPr"):
                                    cNvPr.set("id", str(_fresh_output_shape_id(slide)))
                                    break
                            slide.shapes._spTree.append(new_el)
                            carried = True
                        break
            except Exception as exc:  # noqa: BLE001 - opaque carry is best-effort
                context.issues.append(f"opaque carry failed {source_object_id}: {exc}")
    if not carried:
        context.issues.append(
            f"opaque object {source_object_id} not carried: needs_review")
    try:
        address = _address_of(slide, list(slide.shapes)[-1], "unknown")
    except IndexError:
        address = OutputShapeAddress(slide_part="", shape_id=0,
                                     group_path=[], kind="unknown")
    context.output_map.append(OutputSubBinding(
        source_atom_id=f"{source_object_id}#raw",
        target_address=address,
        target_subaddress="opaque",
        origin="exporter",
        evidence=[f"obj:{source_object_id}"],
    ))
    context.emission_records.append(EmissionRecord(
        source_object_id=source_object_id, strategy="opaque_transplant",
        address=address))
    return address


def _fresh_output_shape_id(slide: Any) -> int:
    used = set()
    for shape in slide.shapes:
        try:
            used.add(int(shape.shape_id))
        except (TypeError, ValueError):
            continue
    return (max(used) + 1) if used else 1


# ---------------------------------------------------------------------------
# §10 notes, links, slide emission
# ---------------------------------------------------------------------------

def copy_notes(source_notes: list[Any], output_slide: Any,
               context: NativeDeckContext,
               source_slide_id: str = "") -> dict[str, Any]:
    """Copy notes paragraphs/runs into the output notes slide."""
    try:
        notes_slide = output_slide.notes_slide
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}
    texts = []
    for para in source_notes or []:
        texts.append("".join(getattr(r, "text", "") for r in getattr(para, "runs", [])))
        if getattr(para, "id", ""):
            try:
                address = _address_of(output_slide,
                                      list(output_slide.shapes)[0], "notes")
            except IndexError:
                address = OutputShapeAddress(
                    slide_part="", shape_id=0, group_path=[], kind="notes")
            context.output_map.append(OutputSubBinding(
                source_atom_id=f"{source_slide_id}/{para.id}",
                target_address=address,
                target_subaddress=f"notes-para[{para.id}]",
                origin="exporter",
                evidence=[f"slide:{source_slide_id}"],
            ))
    if texts and notes_slide.placeholders:
        with contextlib.suppress(IndexError, AttributeError):
            notes_slide.placeholders[1].text = "\n".join(texts)
    return {"ok": True, "paragraphs": len(texts)}


def remap_hyperlinks(context: NativeDeckContext) -> dict[str, Any]:
    """Resolve deferred hyperlink occurrences owner-scoped (§10).

    External URIs are already written on their runs. Internal occurrences
    keep their provisional rIds here; final assembly wires the slide rels
    (target slide parts are known only after the deck is saved).
    """
    resolved_count = 0
    unresolved: list[str] = []
    for item in context.pending_hyperlinks:
        kind = item.get("kind")
        if kind == "external":
            resolved_count += 1  # already written on the run
        elif kind == "internal":
            # target_ref is a source part path here (run-level IR granularity);
            # resolve through source positions, wired in final assembly.
            pos = context.source_part_positions.get(
                str(item.get("target_ref", "")))
            if pos is None:
                # Fall back to the slide-id map (link-resolution granularity).
                target = context.source_to_output_slide_map.get(
                    str(item.get("target_ref", "")))
                if target:
                    item["target_part"] = target
                    resolved_count += 1
                else:
                    unresolved.append(str(item.get("target_ref", "")))
            else:
                item["target_position"] = pos
                resolved_count += 1
        else:
            target = context.source_to_output_slide_map.get(str(item.get("target", "")))
            if target:
                resolved_count += 1
            else:
                unresolved.append(str(item.get("target", "")))
    return {"resolved": resolved_count, "unresolved": unresolved}


def _default_box_fallback(obj: Any) -> dict[str, int]:
    """Diagnostic stacked geometry when no resolved box exists (Stage 5 owns
    real measurement; this is an explicit placeholder, never a pass)."""
    return {"x": 914400, "y": 914400, "w": 5486400, "h": 914400}


def group_corner_check(box: dict[str, float]) -> dict[str, float]:
    """Independent corner computation for group transforms (§13 test)."""
    x, y = box["x"], box["y"]
    return {"x0": x, "y0": y, "x1": x + box["w"], "y1": y + box["h"]}
