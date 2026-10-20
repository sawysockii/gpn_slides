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
    deferred_owner_rels: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    source_part_positions: dict[str, int] = field(default_factory=dict)
    chart_semantic_diffs: list[dict[str, Any]] = field(default_factory=list)
    emission_records: list[EmissionRecord] = field(default_factory=list)
    part_contracts: list[Any] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    work_dir: Path | None = None
    list_profile: Any = None
    # Stage6 brand repair: explicit run-level asset overrides
    # ({source_object_id: {old/new_sha256, replacement_relpath, reason}}),
    # hash-verified at emit and ledgered in issues. Empty by default.
    asset_overrides: dict = field(default_factory=dict)


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
    list_profile: Any = None,
) -> NativeDeckContext:
    """Create a new deck on the active template with all slides pre-created."""
    work_path = Path(work_dir) if work_dir else Path(tempfile.gettempdir())
    work_path.mkdir(parents=True, exist_ok=True)
    clean_template, derivation = adapter.clean_template_copy(template_path, work_path)
    template_hash = _hash_file(template_path)
    prs = adapter.open_template(clean_template)
    # Verify canvas matches loaded rules when both are available.
    canvas = getattr(getattr(rules, "canvas", None), "w", None)
    if rules is not None and canvas:
        try:
            if int(prs.slide_width) != int(canvas) or int(prs.slide_height) != int(
                getattr(rules.canvas, "h", prs.slide_height)
            ):
                raise ValueError("template canvas differs from loaded rules snapshot")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"template/rules canvas mismatch: {exc}") from exc
    ctx = NativeDeckContext(
        presentation=prs,
        active_template_profile=profile,
        rules_snapshot_id=getattr(rules, "source_hash", "") or "",
        work_dir=Path(work_dir) if work_dir else None,
        list_profile=list_profile,
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
        f"clean_template={derivation['sha256'][:16]} dropped={len(derivation['dropped_parts'])}"
    )
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


def apply_display_transform(text: str, role: str, rules: Any) -> tuple[str, str]:
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
        int(box.get("x", 914400)),
        int(box.get("y", 914400)),
        int(box.get("w", 3657600)),
        int(box.get("h", 914400)),
    )
    adapter.set_no_autofit(shape)
    adapter.strip_shape_effect_inheritance(shape)
    # Recovery §5: clear any source-derived shape fill/line on textboxes.
    # Text containers are authoring surfaces: normative white/no-fill with
    # no line, never the source card color.
    with contextlib.suppress(Exception):
        shape.fill.background()
    with contextlib.suppress(Exception):
        shape.line.fill.background()
    role = resolved.get("role", getattr(payload, "role", "unknown"))
    style = _role_style(role, rules) if rules is not None else {}
    # §5.3: a declared surface may set the foreground that stands on it
    # (e.g. white on an allowed dark surface). The token is resolved from
    # the loaded palette; an unknown token leaves the role color intact and
    # is recorded, never guessed.
    if rules is not None and style.get("color_tokens"):
        token = style["color_tokens"][0]
        rgb = _resolve_token_rgb(token, rules)
        if rgb:
            style = {**style, "resolved_rgb": rgb}
    fg_token = str(resolved.get("foreground_token", "") or "")
    if rules is not None and fg_token:
        rgb = _resolve_token_rgb(fg_token, rules)
        if rgb:
            style = {**style, "resolved_rgb": rgb}
        else:
            context.issues.append(
                f"SURFACE_TOKEN_UNKNOWN: {source_object_id} foreground "
                f"token {fg_token!r} not in the loaded palette; role color "
                "kept"
            )
    paragraphs = getattr(payload, "paragraphs", []) or []
    para_transform = "title_uppercase" if style.get("uppercase") is True else "identity"
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
            _apply_list_marker(p, para, rules, context)
        para_id = getattr(para, "id", "")
        by_offset: dict[int, list[Any]] = {}
        for br in list(getattr(para, "breaks", []) or []):
            by_offset.setdefault(int(getattr(br, "offset", 0)), []).append(br)
        for br in by_offset.pop(0, []):
            _emit_break(p, br, para_id or source_object_id, context)
        for ridx, run in enumerate(list(getattr(para, "runs", []) or []), start=1):
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
                display, _ = (
                    apply_display_transform(str(raw), role, rules)
                    if rules is not None
                    else (str(raw), "identity")
                )
                r.text = display
                _style_run(r, style, run)
                link = getattr(run, "hyperlink", None)
                if link is not None and getattr(link, "uri", None):
                    r.hyperlink.address = link.uri
                    context.pending_hyperlinks.append(
                        {
                            "kind": "external",
                            "uri": link.uri,
                            "run_id": getattr(run, "id", ""),
                        }
                    )
                    _bind_atom(
                        context,
                        slide,
                        shape,
                        source_object_id,
                        f"{getattr(run, 'id', '')}#hyperlink",
                        f"para[{pidx}]/hyperlink",
                        "style_only",
                    )
                elif link is not None and getattr(link, "target_slide_ref", None):
                    _emit_internal_run_link(
                        r,
                        shape,
                        pidx,
                        ridx,
                        slide,
                        context,
                        link,
                        slide_position=slide_position,
                        run_id=getattr(run, "id", ""),
                    )
                    _bind_atom(
                        context,
                        slide,
                        shape,
                        source_object_id,
                        f"{getattr(run, 'id', '')}#hyperlink",
                        f"para[{pidx}]/hyperlink",
                        "style_only",
                    )
            for br in by_offset.pop(ridx, []):
                _emit_break(p, br, para_id or source_object_id, context)
        if para_id:
            _bind_atom(
                context, slide, shape, source_object_id, para_id, f"para[{pidx}]", para_transform
            )
    address = _address_of(slide, shape, "text")
    context.emission_records.append(
        EmissionRecord(
            source_object_id=source_object_id, strategy="native_rebuild", address=address
        )
    )
    return address


def emit_rich_text_block(
    paragraphs: list[dict[str, Any]],
    box_emu: dict[str, int],
    slide: Any,
    context: NativeDeckContext,
    *,
    role: str = "",
    rules: Any = None,
    node_id: str = "",
    surface_tokens: dict[str, str] | None = None,
    slide_position: int = 0,
) -> OutputShapeAddress:
    """One native frame; exact paragraph-atom character ranges, no fake run atoms."""
    from types import SimpleNamespace

    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    from pptx.util import Pt

    if set(box_emu) < {"x", "y", "w", "h"} or box_emu.get("w", 0) <= 0 or box_emu.get("h", 0) <= 0:
        raise ValueError("RICH_TEXT_GEOMETRY_REQUIRED")
    shape = adapter.add_textbox(slide, box_emu["x"], box_emu["y"], box_emu["w"], box_emu["h"])
    adapter.set_no_autofit(shape)
    adapter.strip_shape_effect_inheritance(shape)
    shape.fill.background()
    shape.line.fill.background()
    tokens = surface_tokens or {}
    role_cache = {}

    def resolve_role(name):
        if name in role_cache:
            return role_cache[name]
        style = _role_style(name, rules) if rules is not None else {}
        if rules is not None:
            if not style:
                raise ValueError(f"STYLE_ROLE_UNBOUND:{name}")
            colors = style.get("color_tokens", []) or []
            token = str(tokens.get("foreground_token", "") or (colors[0] if colors else ""))
            # A role may declare no color token at all (the loaded ontology
            # keeps color_tokens empty for most roles): then the block keeps
            # the inherited authoring foreground and no color is invented.
            # A token that IS declared must exist in the loaded palette; an
            # unknown one is recorded and never guessed.
            if token:
                rgb = _resolve_token_rgb(token, rules)
                if rgb:
                    style = {**style, "resolved_rgb": rgb}
                else:
                    context.issues.append(
                        f"SURFACE_TOKEN_UNKNOWN: {node_id or 'rich_text_block'} role "
                        f"{name!r} color token {token!r} not in the loaded palette; "
                        "role color kept"
                    )
        role_cache[name] = style
        return style

    resolve_role(role)
    tf = shape.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.TOP
    for attr, pt in (
        ("margin_left", 7.2),
        ("margin_right", 7.2),
        ("margin_top", 3.6),
        ("margin_bottom", 3.6),
    ):
        setattr(tf, attr, Pt(pt))
    for pidx, para in enumerate(paragraphs):
        p = tf.paragraphs[0] if pidx == 0 else tf.add_paragraph()
        p.alignment = {
            "left": PP_ALIGN.LEFT,
            "center": PP_ALIGN.CENTER,
            "right": PP_ALIGN.RIGHT,
            "justify": PP_ALIGN.JUSTIFY,
        }.get(para.get("alignment", "left"), PP_ALIGN.LEFT)
        p.space_before = Pt(float(para.get("space_before_pt", 0) or 0))
        p.space_after = Pt(float(para.get("space_after_pt", 0) or 0))
        spacing = para.get("line_spacing_value")
        if spacing is not None:
            p.line_spacing = (
                Pt(float(spacing)) if para.get("line_spacing_mode") == "points" else float(spacing)
            )
        if para.get("list_kind", "none") != "none" and rules is not None:
            data = dict(para)
            if isinstance(data.get("numbering"), dict):
                data["numbering"] = SimpleNamespace(**data["numbering"])
            _apply_list_marker(p, SimpleNamespace(**data), rules, context)
        para_role = str(para.get("role", "") or role)
        cursor, spans = 0, {}
        for run_data in para.get("runs", []) or []:
            source_ref = str(run_data.get("source_ref", para.get("source_ref", node_id)) or "")
            source_para = str(run_data.get("source_para_id", para.get("id", "")) or "")
            run_role = str(run_data.get("role", "") or para_role)
            style = {**resolve_role(run_role), **dict(run_data.get("style", {}) or {})}
            kind = run_data.get("kind", "text")
            text = str(run_data.get("text", "") or "")
            display, transform = (
                apply_display_transform(text, run_role, rules)
                if rules is not None
                else (text, "identity")
            )
            semantic_marks = [SimpleNamespace(**m) for m in run_data.get("semantic_marks", [])]
            start = cursor
            if kind == "break":
                _emit_break(
                    p,
                    SimpleNamespace(kind=run_data.get("break_kind", "soft")),
                    source_para,
                    context,
                )
                emitted_text = "\n"
            elif kind == "field":
                meta = run_data.get("field")
                source_run = SimpleNamespace(
                    text=text,
                    field=SimpleNamespace(**meta) if meta else None,
                    semantic_marks=semantic_marks,
                )
                _emit_field(p, source_run, style)
                emitted_text = str((meta or {}).get("cached_text") or text)
            else:
                native_run = p.add_run()
                native_run.text = display
                _style_run(native_run, style, SimpleNamespace(semantic_marks=semantic_marks))
                emitted_text = display
                link_data = run_data.get("hyperlink")
                if link_data:
                    link = SimpleNamespace(**link_data)
                    if getattr(link, "uri", None):
                        native_run.hyperlink.address = link.uri
                    elif getattr(link, "target_slide_ref", None):
                        _emit_internal_run_link(
                            native_run,
                            shape,
                            pidx,
                            len(p.runs),
                            slide,
                            context,
                            link,
                            slide_position=slide_position,
                            run_id=str(run_data.get("id", "")),
                        )
                    _bind_atom(
                        context,
                        slide,
                        shape,
                        source_ref,
                        f"{run_data.get('id', '')}#hyperlink",
                        f"para[{pidx}]/hyperlink_at[{start}]",
                        "style_only",
                    )
            cursor += len(emitted_text)
            if source_para:
                span = spans.setdefault(
                    source_para,
                    {"ref": source_ref, "start": start, "end": cursor, "transform": transform},
                )
                if span["transform"] != transform:
                    raise ValueError(f"MIXED_SOURCE_PARAGRAPH_TRANSFORM:{source_para}")
                span["end"] = cursor
        if not spans and para.get("id"):
            spans[str(para["id"])] = {
                "ref": str(para.get("source_ref", node_id)),
                "start": 0,
                "end": 0,
                "transform": "identity",
            }
        for atom, span in spans.items():
            _bind_atom(
                context,
                slide,
                shape,
                span["ref"],
                atom,
                f"para[{pidx}]/chars[{span['start']}:{span['end']}]",
                "title_uppercase" if span["transform"] == "title_uppercase" else "native_rebuild",
            )
    address = _address_of(slide, shape, "text")
    context.emission_records.append(
        EmissionRecord(source_object_id=node_id, strategy="native_rich_block", address=address)
    )
    return address


def _emit_field(p: Any, run: Any, style: dict[str, Any]) -> None:
    """Append a real ``a:fld`` element preserving id/type/cached text."""
    meta = getattr(run, "field", None)
    cached = getattr(meta, "cached_text", None) or getattr(run, "text", "") or ""
    fld = etree.SubElement(p._p, f"{{{A_NS}}}fld")
    if meta is not None:
        if getattr(meta, "field_id", ""):
            fld.set("id", str(meta.field_id))
        if getattr(meta, "field_type", ""):
            fld.set("type", str(meta.field_type))
    carrier = p.add_run()
    _style_run(carrier, style, run)
    source_rpr = carrier._r.get_or_add_rPr()
    import copy

    fld.append(copy.deepcopy(source_rpr))
    p._p.remove(carrier._r)
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
            "recorded as needs_review, emitted as soft token"
        )
    etree.SubElement(p._p, f"{{{A_NS}}}br")


def set_slide_background(slide: Any, rgb_hex: str, source_ref: str, context: Any) -> None:
    """Fill the slide backdrop with a normative solid color (§6.4).

    A dark brand surface is content: a white logo and light text need it
    the way body text needs its frame. The RGB always comes from a loaded
    palette token resolved by the caller, never from a source literal.
    """
    from pptx.dml.color import RGBColor

    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = RGBColor.from_string(rgb_hex)
    context.issues.append(f"BACKGROUND_APPLIED: {source_ref} slide backdrop -> #{rgb_hex}")


def _resolve_token_rgb(token: Any, rules: Any) -> str | None:
    """Resolve a color token to 6-hex RGB via the compiled palette."""
    if not isinstance(token, str):
        return None
    bare = token.strip().lstrip("#").upper()
    if len(bare) == 6 and all(ch in "0123456789ABCDEF" for ch in bare):
        return bare
    colors = dict(getattr(rules, "colors", {}) or {})
    value = colors.get(token)
    if isinstance(value, str):
        bare = value.strip().lstrip("#").upper()
        if len(bare) == 6 and all(ch in "0123456789ABCDEF" for ch in bare):
            return bare
    # Conditional palette entries (JSON-declared usage + evidence refs).
    conditional = getattr(rules, "conditional_colors", {}) or {}
    cond = conditional.get(token) if isinstance(conditional, dict) else None
    rgb = getattr(cond, "rgb", "") if cond is not None else ""
    bare = str(rgb or "").strip().lstrip("#").upper()
    if len(bare) == 6 and all(ch in "0123456789ABCDEF" for ch in bare):
        return bare
    return None


def _role_face(style: dict[str, Any]) -> str:
    """Exact corporate face for a role style (normative, never themed)."""
    face = style.get("typeface") or ((style.get("allowed_faces") or [None])[0])
    return str(face) if face else ""


def _style_run(run: Any, style: dict[str, Any], source_run: Any) -> None:
    font = run.font
    # Corporate face: the exact resolved role face is the normative
    # typeface. Without it every run silently falls back to the theme
    # default and no render check can confirm corporate typography.
    face = _role_face(style)
    if face:
        font.name = face
    size = style.get("default_size_pt")
    if size:
        try:
            from pptx.util import Pt

            font.size = Pt(float(size))
        except (TypeError, ValueError):
            pass
    rgb = _hex_to_rgb(
        style.get("resolved_rgb")
        or style.get("color_rgb")
        or ((style.get("color_tokens") or [None])[0] if style.get("color_tokens") else None)
    )
    # Recovery §5 (resolve_authoring_style): every authoring run carries an
    # explicit approved foreground. Roles without color_tokens fall back to
    # the normative black token (000000 from the loaded palette), never to
    # inherited tx1 which is unreadable on colored surfaces.
    if rgb is None:
        rgb = (0, 0, 0)
    from pptx.dml.color import RGBColor

    font.color.rgb = RGBColor(*rgb)
    # Recovery §5: source bold/italic NEVER pass through as b/i flags.
    # Source emphasis is a semantic hint resolved upstream into an exact
    # allowed face via font policy (resolve_authoring_style); writing
    # b=true on a Bold face is redundant (C21) and on a Regular face is
    # synthetic (C22). The only allowed b=true is the documented
    # Condensed-Italic exception, which the role face already encodes.
    # Explicitly clear the flags so python-pptx/theme defaults cannot leak.
    with contextlib.suppress(Exception):
        font.bold = False
    with contextlib.suppress(Exception):
        font.italic = False
    # Preserve run-level emphasis that carries meaning (sub/superscript,
    # e.g. footnote markers): restore the OOXML baseline recorded in the
    # source semantic marks, never drop it.
    for mark in list(getattr(source_run, "semantic_marks", []) or []):
        evidence = getattr(mark, "evidence_ref", "") or ""
        if evidence.startswith("baseline="):
            with contextlib.suppress(ValueError, AttributeError):
                run._r.get_or_add_rPr().set("baseline", str(int(evidence.split("=", 1)[1])))


def _apply_list_marker(
    paragraph: Any, para_ir: Any, rules: Any, context: NativeDeckContext | None = None
) -> None:
    """Apply the loaded LS01 bullet profile (no literals; see bullets.py)."""
    try:
        from .bullets import apply_ls01_paragraph, resolve_list_level

        profile = getattr(context, "list_profile", None) if context else None
        if profile is None:
            lists = getattr(rules, "lists", {}) or {}
            compiled = lists.get("LS01") or next(iter(lists.values()), None)
            if compiled is None:
                return
            profile = compiled
        resolved = resolve_list_level(
            level=int(getattr(para_ir, "level", 0)),
            parent_size_pt=None,
            background_token=getattr(para_ir, "background_token", "white"),
            paragraph_text_token=getattr(para_ir, "paragraph_text_token", "black"),
            profile=profile,
        )
        if resolved.status != "ok":
            log.warning("list level infeasible: %s", resolved.issues)
            return
        ppr = paragraph._p.get_or_add_pPr()
        apply_ls01_paragraph(ppr.getparent(), resolved)
    except Exception as exc:  # noqa: BLE001 - list styling must not abort export
        log.warning("list profile application failed: %s", exc)


def _emit_internal_run_link(
    run: Any,
    shape: Any,
    pidx: int,
    ridx: int,
    slide: Any,
    context: NativeDeckContext,
    link: Any,
    *,
    slide_position: int,
    run_id: str,
) -> None:
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
    context.pending_hyperlinks.append(
        {
            "kind": "internal",
            "target_ref": link.target_slide_ref,
            "run_id": run_id,
            "rid": rid,
            "slide_position": slide_position,
            "shape_id": shape_id,
            "para_idx": pidx,
            "run_idx": ridx,
        }
    )


def _bind_atom(
    context: NativeDeckContext,
    slide: Any,
    shape: Any,
    source_object_id: str,
    atom_id: str,
    sub: str,
    transform: str = "identity",
) -> None:
    """Bind one ledger atom id to its real output subaddress (§2.1)."""
    if not atom_id:
        atom_id = source_object_id
    context.output_map.append(
        OutputSubBinding(
            source_atom_id=atom_id,
            target_address=_address_of(slide, shape, "text"),
            target_subaddress=sub,
            origin="exporter",
            transform=transform,  # type: ignore[arg-type]
            evidence=[f"obj:{source_object_id}"],
        )
    )


def _bind(
    context: NativeDeckContext,
    slide: Any,
    shape: Any,
    source_object_id: str,
    atom_id: str,
    sub: str,
    order: int,
) -> None:
    _bind_atom(context, slide, shape, source_object_id, atom_id, f"{sub}[{order}]")


def _address_of(slide: Any, shape: Any, kind: str) -> OutputShapeAddress:
    try:
        part = slide.part.partname.lstrip("/")
    except AttributeError:
        part = f"ppt/slides/slide{slide.slide_id}.xml"
    try:
        shape_id = int(shape.shape_id)
    except (TypeError, ValueError):
        shape_id = 0
    return OutputShapeAddress(slide_part=part, shape_id=shape_id, group_path=[], kind=kind)


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
        slide,
        rows,
        cols,
        int(box.get("x", 914400)),
        int(box.get("y", 914400)),
        int(box.get("w", 5486400)),
        int(box.get("h", 2743200)),
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
    by_coord = {(int(c.row), int(c.col)): c for c in list(getattr(payload, "cells", []) or [])}
    # Measured row heights (Stage 5 typed binding); absent without layout.
    heights = list(getattr(payload, "row_heights_emu", []) or [])
    if len(heights) == rows:
        for idx, height in enumerate(heights):
            try:
                from pptx.util import Emu

                table.rows[idx].height = Emu(int(height))
            except (TypeError, ValueError):
                context.issues.append(f"table row height failed {idx}")
    # Corporate role for cell runs (Stage 5 binding); unknown keeps the
    # previous unstyled behavior byte for byte.
    role = layout.get("role", "unknown")
    style = _role_style(role, rules) if rules is not None else {}
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
                    _style_run(r, style, run)
        context.output_map.append(
            OutputSubBinding(
                source_atom_id=getattr(cell, "id", f"cell-{row}-{col}"),
                target_address=_address_of(slide, frame, "table"),
                target_subaddress=f"cell[{row},{col}]",
                origin="exporter",
                evidence=[f"obj:{source_object_id}"],
            )
        )
    address = _address_of(slide, frame, "table")
    context.emission_records.append(
        EmissionRecord(
            source_object_id=source_object_id, strategy="native_rebuild", address=address
        )
    )
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
        return {"plots": [], "axes": [], "sha256": hashlib.sha256(chart_part_xml).hexdigest()}
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
            plots.append(
                {
                    "plot": local,
                    "series_count": len(series),
                    "series_idx": [s.findtext(f"{{{C_NS}}}idx", default="") for s in series],
                    "axIds": ax_ids,
                    "grouping": child.findtext(f"{{{C_NS}}}grouping", default=""),
                }
            )
    return {"plots": plots, "axes": axes, "sha256": hashlib.sha256(chart_part_xml).hexdigest()}


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
            source_package_hash=phash,
            source_parts=parts,
            source_content_types=ctypes,
            source_outgoing=outgoing,
        )
    tctx = context.transplant_context
    before = snapshot_chart_semantics(tctx.source_parts.get(chart_part, b""))
    result = clone_part_graph(chart_part, tctx, variant="src")
    if [i for i in result.issues if "UNRESOLVED" in i.code or "MISSING" in i.code]:
        raise ValueError(f"chart closure has blocking issues: {[i.code for i in result.issues]}")
    target_chart = result.actual_root_part
    cloned = bytearray(tctx.target_payloads[target_chart])
    _normalize_chart_style(cloned, context)
    tctx.target_payloads[target_chart] = bytes(cloned)
    after = snapshot_chart_semantics(bytes(cloned))
    if before.get("plots") != after.get("plots"):
        raise ValueError("chart style normalization changed plot semantics")
    context.chart_semantic_diffs.append(
        {
            "source_chart": chart_part,
            "target_chart": target_chart,
            "before": before,
            "after": after,
            "workbook_sha256": _workbook_hash(payload, tctx),
            "replace_data_used": False,
        }
    )
    # Graphic frame: deep-copy the source frame subtree into the output
    # slide with resolved geometry and a fresh owner-scoped chart rId.
    box = resolved.get("box_emu") or {}
    shape = _insert_chart_frame(
        slide,
        source_package_path,
        source_slide_part,
        chart_part,
        box,
        context,
        target_chart,
        slide_position=slide_position,
    )
    context.part_contracts.extend(result.contracts)
    address = _address_of(slide, shape, "chart")
    # Series/point/category bindings keep plot scope from the IR payload.
    # Series are matched by name on reimport (ids are hash-derived).
    order = 0
    for sidx, series in enumerate(list(getattr(payload, "series", []) or [])):
        for point in list(getattr(series, "points", []) or []):
            context.output_map.append(
                OutputSubBinding(
                    source_atom_id=getattr(point, "id", f"pt-{order}"),
                    target_address=address,
                    target_subaddress=(
                        f"series[{sidx}]:"
                        f"{getattr(series, 'name', getattr(series, 'id', ''))}"
                        f"/point[{getattr(point, 'index', order)}]"
                    ),
                    origin="exporter",
                    evidence=[f"obj:{source_object_id}"],
                )
            )
            order += 1
    context.emission_records.append(
        EmissionRecord(
            source_object_id=source_object_id, strategy="native_transplant", address=address
        )
    )
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
    cloned[:] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _workbook_hash(payload: Any, tctx: TransplantContext) -> str:
    asset = getattr(payload, "workbook", None)
    digest = getattr(asset, "sha256", "") if asset is not None else ""
    return str(digest or "")


def _find_source_graphic_frame(
    source_slide_xml: bytes,
    chart_part: str,
    source_outgoing: dict[str, list[dict]],
    source_slide_part: str,
) -> Any | None:
    """Locate the ``p:graphicFrame`` whose chart ref resolves to chart_part."""
    try:
        root = parse_xml(source_slide_xml)
    except Exception:
        return None
    wanted = {
        rel["id"]
        for rel in source_outgoing.get(source_slide_part, [])
        if rel["mode"].lower() != "external" and _resolve_src(rel, source_slide_part) == chart_part
    }
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
    frame = (
        _find_source_graphic_frame(src_xml, chart_part, tctx.source_outgoing, source_slide_part)
        if src_xml
        else None
    )
    if frame is None:
        context.issues.append(f"chart frame for {chart_part} not found; anchor fallback")
        return adapter.add_textbox(
            slide,
            int(box.get("x", 914400)),
            int(box.get("y", 914400)),
            int(box.get("w", 5486400)),
            int(box.get("h", 2743200)),
        )
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
    context.pending_chart_frames.append(
        {
            "slide_position": slide_position,
            "rid": rid,
            "target_chart": target_chart,
        }
    )
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
                        size = (
                            float(p.bubble_size.decimal)
                            if p.bubble_size and p.bubble_size.decimal
                            else 1.0
                        )
                        proxy.add_data_point(float(p.x.decimal), float(p.y.decimal), size)
        else:
            chart_data = XyChartData()
            for s in series:
                proxy = chart_data.add_series(s.name or s.id)
                for p in s.points:
                    if p.x and p.x.decimal and p.y and p.y.decimal:
                        proxy.add_data_point(float(p.x.decimal), float(p.y.decimal))
    else:
        chart_data = CategoryChartData()
        labels = [c.path[-1] if c.path else (c.raw_value or f"c{c.index}") for c in categories]
        if labels:
            chart_data.categories = labels
        for s in series:
            values = [
                float(p.value.decimal) if p.value and p.value.decimal else None for p in s.points
            ]
            chart_data.add_series(s.name or s.id, values)
    box = resolved.get("box_emu") or {}
    frame = adapter.add_chart_from_verified_data(
        slide,
        chart_type,
        chart_data,
        int(box.get("x", 914400)),
        int(box.get("y", 914400)),
        int(box.get("w", 5486400)),
        int(box.get("h", 2743200)),
    )
    address = _address_of(slide, frame, "chart")
    context.emission_records.append(
        EmissionRecord(
            source_object_id=source_object_id, strategy="native_rebuild", address=address
        )
    )
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
    visual_plan: Any = None,
) -> OutputShapeAddress:
    box = resolved.get("box_emu") or {}
    text = getattr(payload, "text", None)
    paras = list(getattr(text, "paragraphs", []) or []) if text is not None else []
    has_text = any(
        "".join(getattr(r, "text", "") or "" for r in list(getattr(p, "runs", []) or []))
        or getattr(getattr(r, "field", None), "cached_text", "")
        for p in paras
        for r in list(getattr(p, "runs", []) or [])
    )
    if text is not None and has_text:
        shape = adapter.add_textbox(
            slide,
            int(box.get("x", 914400)),
            int(box.get("y", 914400)),
            int(box.get("w", 1828800)),
            int(box.get("h", 914400)),
        )
        shape.text_frame.clear()
        emit_text(
            text,
            {
                "box_emu": box,
                "role": resolved.get("role", getattr(text, "role", "unknown")),
                # A declared surface may set the text foreground (§5.3).
                # Dropping these here left a black label on a blue panel:
                # the surface painted, but its text never received the
                # foreground token.
                "foreground_token": resolved.get("foreground_token", ""),
                "surface_token": resolved.get("surface_token", ""),
                "stroke_token": resolved.get("stroke_token", ""),
            },
            slide,
            context,
            rules=rules,
            source_object_id=source_object_id,
        )
        # Remove the placeholder box; emit_text created the real one.
        shape._element.getparent().remove(shape._element)
        record = context.emission_records[-1]
        return record.address  # type: ignore[return-value]
    # Pure decoration (no text): recovery §3/§5 — source decorative fills
    # and rounded-card geometry are NOT preserved as authoring style.
    # The blue 0C539F roundRect cards are decoration (decoration_decisions),
    # not required business atoms. Emit permitted RECTANGLE geometry with
    # normative transparent fill so C01/C03 pass on saved bytes; the
    # semantic content (if any) travels via bound text objects.
    token = str(getattr(payload, "geometry_token", "") or "")
    # Recovery: roundRect is prohibited decorative geometry -> RECTANGLE.
    norm_token = "rect" if token == "roundRect" else token
    preset = adapter.DECOR_PRESET_MAP.get(norm_token)
    if token == "roundRect":
        context.issues.append(
            f"GEOMETRY_NORMALIZED: {source_object_id} roundRect->rect "
            "(C03 decorative rounded cards prohibited)"
        )
    placeholder = adapter.add_textbox(
        slide,
        int(box.get("x", 914400)),
        int(box.get("y", 914400)),
        int(box.get("w", 1828800)),
        int(box.get("h", 914400)),
    )
    address = _address_of(slide, placeholder, "shape")
    try:
        if norm_token == "line":
            shape = adapter.add_straight_connector(
                slide,
                int(box.get("x", 914400)),
                int(box.get("y", 914400)),
                int(box.get("w", 1828800)),
                int(box.get("h", 914400)),
            )
        elif preset is not None:
            shape = adapter.add_autoshape(
                slide,
                preset,
                int(box.get("x", 914400)),
                int(box.get("y", 914400)),
                int(box.get("w", 1828800)),
                int(box.get("h", 914400)),
            )
        else:
            context.issues.append(
                f"GEOMETRY_UNKNOWN: {source_object_id} token {token!r}; "
                "rect fallback recorded, not a silent pass"
            )
            shape = adapter.add_autoshape(
                slide,
                "RECTANGLE",
                int(box.get("x", 914400)),
                int(box.get("y", 914400)),
                int(box.get("w", 1828800)),
                int(box.get("h", 914400)),
            )
        _apply_decor_fill_line(
            shape,
            payload,
            norm_token,
            source_object_id,
            context,
            rules=rules,
            surface=resolved.get("surface_token", ""),
            stroke_token=resolved.get("stroke_token", ""),
            visual_plan=visual_plan,
        )
        adapter.strip_shape_effect_inheritance(shape)
        placeholder._element.getparent().remove(placeholder._element)
    except Exception as exc:  # noqa: BLE001 - decor must not kill export
        context.issues.append(f"decor emit failed {source_object_id}: {exc}")
        return address
    address = _address_of(slide, shape, "connector" if token == "line" else "shape")
    context.emission_records.append(
        EmissionRecord(
            source_object_id=source_object_id, strategy="native_rebuild", address=address
        )
    )
    _bind_atom(
        context, slide, shape, source_object_id, f"{source_object_id}#shape", "geometry", "relocate"
    )
    # Empty text payloads still own ledger atoms (canonical ""); bind them
    # so the preservation gate sees them, never as silent drops.
    for pidx, para in enumerate(paras):
        para_id = getattr(para, "id", "")
        if para_id:
            _bind_atom(
                context, slide, shape, source_object_id, para_id, f"para[{pidx}]", "identity"
            )
    return address


def _apply_decor_fill_line(
    shape: Any,
    payload: Any,
    token: str,
    source_object_id: str,
    context: Any,
    rules: Any = None,
    surface: Any = "",
    stroke_token: Any = "",
    visual_plan: Any = None,
) -> None:
    """Execute the PRE-DECIDED visual normalization plan (§6.2, §10.1).

    The renderer no longer decides meaning from a random source colour. The
    recovery run cleared every stroke that was not in the palette, which
    deleted the ГГД schematic on slide 3 and the source→processing→table
    chain on slide 2 while the XML objects survived; the count was
    preserved and the graph was not. ``visual_norm.plan_visual_normalization``
    decides before emission and this function executes it:

    * ``keep_native_recolor`` / ``redraw_native`` — apply the plan's
      normative tokens; the source RGB is never restored.
    * ``authorize_omit`` — optional decor: no stroke, no fill, recorded.
    * ``unresolved`` on a REQUIRED unit — a blocking
      ``VISUAL_REPRESENTATION_UNRESOLVED`` finding. The shape is left with
      its geometry and NO styling is silently cleared, because a required
      representation must never be discharged by invisible strokes.
    """
    from pptx.util import Emu

    palette: set[str] = set()
    if rules is not None:
        colors = dict(getattr(rules, "colors", {}) or {})
        palette = {str(v).strip().lstrip("#").upper() for v in colors.values()}
        conditional = getattr(rules, "conditional_colors", {}) or {}
        if isinstance(conditional, dict):
            for cond in conditional.values():
                rgb = getattr(cond, "rgb", "")
                if rgb:
                    palette.add(str(rgb).strip().lstrip("#").upper())
    is_connector = token == "line"
    surface_rgb = _resolve_token_rgb(surface, rules) if rules is not None else None
    planned_stroke = (
        _resolve_token_rgb(getattr(visual_plan, "stroke_token", ""), rules)
        if (visual_plan is not None and rules is not None)
        else None
    )
    required_unit = True
    plan_action = ""
    plan_required = False
    if visual_plan is not None:
        plan_action = str(getattr(visual_plan, "action", "") or "")
        if plan_action == "authorize_omit":
            required_unit = False
        if str(getattr(visual_plan, "action", "")) == "unresolved":
            plan_required = True
    else:
        context.issues.append(
            f"VISUAL_PLAN_MISSING: {source_object_id} reached the renderer "
            "without a visual normalization plan; the renderer may not "
            "decide meaning from the source colour"
        )
    try:
        if is_connector:
            pass  # line handling below; connectors carry no fill
        elif surface_rgb:
            # §5.3: a declared surface container keeps a *palette* fill.
            # The RGB comes from the loaded JSON, never from the source
            # forensic fill and never from a literal in this module.
            from pptx.dml.color import RGBColor

            shape.fill.solid()
            shape.fill.fore_color.rgb = RGBColor.from_string(surface_rgb)
            context.issues.append(
                f"SURFACE_APPLIED: {source_object_id} fill token {surface!r} -> #{surface_rgb}"
            )
        else:
            if str(surface or "").strip():
                context.issues.append(
                    f"SURFACE_TOKEN_UNKNOWN: {source_object_id} surface "
                    f"token {surface!r} not in the loaded palette; "
                    "normative no-fill applied"
                )
            with contextlib.suppress(Exception):
                shape.fill.background()
            context.issues.append(
                f"DECOR_FILL_NORMALIZED: {source_object_id} source fill "
                "dropped; normative no-fill applied (C01)"
            )
    except Exception as exc:  # noqa: BLE001 - keep geometry, flag style
        context.issues.append(f"decor fill not normalized {source_object_id}: {exc}")
    if not required_unit:
        with contextlib.suppress(Exception):
            shape.fill.background()
        with contextlib.suppress(Exception):
            shape.line.fill.background()
        context.issues.append(
            f"DECOR_CLUSTER_OMITTED: {source_object_id} optional decor "
            f"removed as a whole unit (plan={plan_action}); its space is "
            "recomposed instead of leaving an empty frame"
        )
        return
    if plan_required:
        context.issues.append(
            f"VISUAL_REPRESENTATION_UNRESOLVED: {source_object_id} is a "
            "required visual unit with no applicable normative style token; "
            "its strokes were left unstyled for inspection instead of being "
            "cleared into an invisible frame (blocking)"
        )
        return
    line = getattr(payload, "source_line", None)
    color = getattr(line, "color", None) if line is not None else None
    stroke_rgb = planned_stroke
    try:
        val = stroke_rgb or (
            str(getattr(color, "value", "") or "").strip().lstrip("#").upper()
            if color is not None
            else ""
        )
        if stroke_rgb:
            from pptx.dml.color import RGBColor

            shape.line.color.rgb = RGBColor.from_string(stroke_rgb)
            context.issues.append(
                f"STROKE_APPLIED: {source_object_id} stroke token "
                f"{getattr(visual_plan, 'stroke_token', '')!r} -> "
                f"#{stroke_rgb}"
            )
            return
        if color is not None and getattr(color, "kind", "") == "srgb" and val:
            if val in palette:
                from pptx.dml.color import RGBColor

                shape.line.color.rgb = RGBColor.from_string(val)
                width = getattr(line, "width_emu", None)
                if width:
                    shape.line.width = Emu(int(width))
            else:
                with contextlib.suppress(Exception):
                    shape.line.fill.background()
                context.issues.append(
                    f"DECOR_LINE_NORMALIZED: {source_object_id} source line "
                    f"{val} not in palette; cleared (C01)"
                )
        elif color is None:
            with contextlib.suppress(Exception):
                shape.line.fill.background()
    except Exception as exc:  # noqa: BLE001
        context.issues.append(f"decor line not normalized {source_object_id}: {exc}")


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
        _emit_group_child(
            group,
            child,
            resolved,
            slide,
            context,
            rules=rules,
            source_object_id=source_object_id,
            position=(cidx,),
            root_group=group,
        )
    _write_group_xfrm(group, payload, resolved)
    address = _address_of(slide, group, "group")
    context.emission_records.append(
        EmissionRecord(
            source_object_id=source_object_id, strategy="native_rebuild", address=address
        )
    )
    return address


def _emit_group_child(
    group: Any,
    child: Any,
    resolved: dict[str, Any],
    slide: Any,
    context: NativeDeckContext,
    *,
    rules: Any = None,
    source_object_id: str = "",
    position: tuple[int, ...] = (),
    root_group: Any = None,
) -> None:
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
            context.output_map.append(
                OutputSubBinding(
                    source_atom_id=f"{getattr(child, 'id', '')}#shape",
                    target_address=_address_of(slide, anchor, "group"),
                    target_subaddress=f"group-child[{pos}]",
                    origin="exporter",
                    evidence=[f"obj:{getattr(child, 'id', '')}"],
                )
            )
            sub = group.shapes.add_textbox(
                adapter.Emu(child_box["x"]),
                adapter.Emu(child_box["y"]),
                adapter.Emu(child_box["w"]),
                adapter.Emu(child_box["h"]),
            )
            sub.text = ""
            return
        sub = group.shapes.add_textbox(
            adapter.Emu(child_box["x"]),
            adapter.Emu(child_box["y"]),
            adapter.Emu(child_box["w"]),
            adapter.Emu(child_box["h"]),
        )
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
                context.output_map.append(
                    OutputSubBinding(
                        source_atom_id=para.id,
                        target_address=_address_of(slide, anchor, "group"),
                        target_subaddress=f"group-child[{pos}]/para[{pidx}]",
                        origin="exporter",
                        evidence=[f"obj:{getattr(child, 'id', '')}"],
                    )
                )
    elif kind == "group":
        nested = group.shapes.add_group_shape()
        for gidx, grand in enumerate(list(getattr(payload, "children", []) or [])):
            _emit_group_child(
                nested,
                grand,
                resolved,
                slide,
                context,
                rules=rules,
                source_object_id=source_object_id,
                position=(*position, gidx),
                root_group=anchor,
            )
        _write_group_xfrm(nested, payload, resolved)
    else:
        context.issues.append(f"group child kind {kind} needs_review")


def _write_group_xfrm(group_shape: Any, payload: Any, resolved: dict[str, Any]) -> None:
    """Serialize the group-space transform (off/ext/child/rot/flip).

    Children are emitted in group-local coordinates; without the source
    transform the group collapses to the origin and every saved-geometry
    and collision check fails downstream.
    """
    from pptx.oxml.ns import qn

    box = resolved.get("box_emu") or {}
    grp = group_shape._element.find(qn("p:grpSpPr"))
    if grp is None:
        return
    xfrm = grp.find(qn("a:xfrm"))
    if xfrm is None:
        xfrm = etree.SubElement(grp, qn("a:xfrm"))
    off = xfrm.find(qn("a:off"))
    if off is None:
        off = etree.SubElement(xfrm, qn("a:off"))
    off.set("x", str(int(box.get("x", 914400))))
    off.set("y", str(int(box.get("y", 914400))))
    ext = xfrm.find(qn("a:ext"))
    if ext is None:
        ext = etree.SubElement(xfrm, qn("a:ext"))
    ext.set("cx", str(max(int(box.get("w", 1828800)), 1)))
    ext.set("cy", str(max(int(box.get("h", 914400)), 1)))
    child_box = getattr(payload, "source_child_box", None)
    if child_box is not None:
        ch_off = xfrm.find(qn("a:chOff"))
        if ch_off is None:
            ch_off = etree.SubElement(xfrm, qn("a:chOff"))
        ch_off.set("x", str(int(getattr(child_box, "x", 0) or 0)))
        ch_off.set("y", str(int(getattr(child_box, "y", 0) or 0)))
        ch_ext = xfrm.find(qn("a:chExt"))
        if ch_ext is None:
            ch_ext = etree.SubElement(xfrm, qn("a:chExt"))
        ch_ext.set("cx", str(max(int(getattr(child_box, "w", 0) or 0), 1)))
        ch_ext.set("cy", str(max(int(getattr(child_box, "h", 0) or 0), 1)))
    try:
        rot = float(getattr(payload, "rot_deg", 0.0) or 0.0)
    except (TypeError, ValueError):
        rot = 0.0
    if rot:
        xfrm.set("rot", str(int(round(rot * 60000))))
    elif "rot" in xfrm.attrib:
        del xfrm.attrib["rot"]
    if getattr(payload, "flip_h", False):
        xfrm.set("flipH", "1")
    if getattr(payload, "flip_v", False):
        xfrm.set("flipV", "1")


def _child_box(child: Any, resolved: dict[str, Any]) -> dict[str, int]:
    box = getattr(getattr(child, "local_box", None), "__dict__", {}) or {}
    base = resolved.get("box_emu") or {"x": 914400, "y": 914400}
    return {
        "x": int(box.get("x", base.get("x", 914400))),
        "y": int(box.get("y", base.get("y", 914400))),
        "w": int(box.get("w", 1828800)),
        "h": int(box.get("h", 914400)),
    }


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
    from .assets import _extension_for, resolve_asset_bytes

    # Stage6 brand repair: an explicit run-level asset override swaps one
    # source object's bytes for an approved variant (same mark, visible
    # colorway) with old/new hashes recorded. Never a silent substitution:
    # the override entry must match the payload digest, replacement bytes
    # are hash-verified, and the swap is ledgered in context issues.
    override = (getattr(context, "asset_overrides", {}) or {}).get(source_object_id)
    override_blob: bytes | None = None
    override_media = ""
    if override is not None:
        import hashlib as _hashlib

        if str(override.get("old_sha256", "")) != digest:
            raise ValueError(
                f"ASSET_OVERRIDE_MISMATCH: {source_object_id}: override "
                "old_sha256 does not match payload asset; refusing swap"
            )
        store = context.asset_store
        base = getattr(getattr(store, "root", None), "__str__", lambda: "")()
        cand = Path(base) / str(override.get("replacement_relpath", ""))
        if not cand.is_file():
            raise ValueError(
                f"REQUIRED_ASSET_UNRESOLVED: {source_object_id}: override "
                f"replacement bytes missing: {cand}"
            )
        override_blob = cand.read_bytes()
        actual = _hashlib.sha256(override_blob).hexdigest()
        if actual != str(override.get("new_sha256", "")):
            raise ValueError(
                f"ASSET_OVERRIDE_MISMATCH: {source_object_id}: replacement "
                "bytes fail new_sha256 verification; refusing swap"
            )
        override_media = str(override.get("media_type", "image/png"))
        context.issues.append(
            f"ASSET_OVERRIDE_APPLIED: {source_object_id} "
            f"{digest[:16]} -> {actual[:16]} "
            f"({override.get('reason', '')})"
        )
    tctx = getattr(context, "transplant_context", None)
    try:
        resolved_asset = resolve_asset_bytes(
            asset,
            stores=[context.asset_store] if context.asset_store is not None else [],
            source_parts=dict(getattr(tctx, "source_parts", {}) or {}) or None,
            source_content_types=dict(getattr(tctx, "source_content_types", {}) or {}) or None,
        )
    except Exception as exc:
        raise ValueError(f"REQUIRED_ASSET_UNRESOLVED: {source_object_id}: {exc}") from exc
    blob = override_blob if override_blob is not None else resolved_asset.data
    if override_blob is not None:
        suffix = _extension_for(override_media or "image/png")
    elif resolved_asset.source_kind == "source_opc":
        context.issues.append(
            f"ASSET_RECOVERED_FROM_SOURCE_OPC: {source_object_id} "
            f"{resolved_asset.provenance} sha={digest[:16]}"
        )
        suffix = _extension_for(resolved_asset.media_type or "image/png")
    else:
        suffix = _extension_for(resolved_asset.media_type or "image/png")
    # Pillow/python-pptx sniff by content; keep the true container suffix
    # (jpeg stays .jpg, svg/emf keep their suffix and fail loudly later
    # instead of masquerading as .png).
    linked = getattr(payload, "linked_uri", None)
    if linked:
        context.issues.append(f"linked media kept as URI, not fetched: {linked}")
    work = context.work_dir or Path(".")
    tmp = Path(work) / f"img-{digest[:12]}{suffix}"
    tmp.write_bytes(blob)
    box = resolved.get("box_emu") or {}
    pic = adapter.add_picture(
        slide,
        tmp,
        int(box.get("x", 914400)),
        int(box.get("y", 914400)),
        int(box.get("w", 2743200)),
        int(box.get("h", 1828800)),
    )
    address = _address_of(slide, pic, "image")
    context.output_map.append(
        OutputSubBinding(
            source_atom_id=f"{source_object_id}#asset",
            target_address=address,
            target_subaddress="picture",
            origin="exporter",
            evidence=[f"obj:{source_object_id}"],
        )
    )
    context.emission_records.append(
        EmissionRecord(
            source_object_id=source_object_id, strategy="native_rebuild", address=address
        )
    )
    return address


def emit_free_connector(
    payload: Any,
    resolved: dict[str, Any],
    slide: Any,
    context: NativeDeckContext,
    *,
    rules: Any = None,
    source_object_id: str = "",
    visual_plan: Any = None,
) -> OutputShapeAddress:
    """Emit an endpoint-less connector as a native straight line.

    No relation exists to preserve; the visible geometry is kept and the
    diagram_node atom bound (an edge atom could never be verified here).
    The normative stroke comes from the zone token first, then the
    pre-decided visual normalization plan (§6.2); without either the line
    keeps its default — recorded, not silently claimed and never cleared
    into invisibility for a required diagram.
    """
    box = resolved.get("box_emu") or {}
    conn = adapter.add_straight_connector(
        slide,
        int(box.get("x", 914400)),
        int(box.get("y", 914400)),
        int(box.get("w", 1828800)),
        int(box.get("h", 0)),
    )
    adapter.strip_shape_effect_inheritance(conn)
    # A bound arrow is a required relation (§6.2.4): its direction is
    # semantic, so the arrowhead is preserved. Colour comes only from a
    # declared palette token — never from a source RGB passthrough.
    line = getattr(payload, "source_line", None)
    begin = str(getattr(line, "begin_arrow", "") or "none") if line else "none"
    end = str(getattr(line, "end_arrow", "") or "none") if line else "none"
    if begin != "none" or end != "none":
        _set_connector_arrows(conn, begin, end, source_object_id, context)
    stroke = str(resolved.get("stroke_token", "") or "")
    if not stroke and visual_plan is not None:
        stroke = str(getattr(visual_plan, "stroke_token", "") or "")
        if stroke:
            context.issues.append(
                f"STROKE_APPLIED: {source_object_id} plan stroke token "
                f"{stroke!r} (zone declared none)"
            )
    if stroke:
        rgb = _resolve_token_rgb(stroke, rules) if rules is not None else None
        if rgb:
            from pptx.dml.color import RGBColor

            conn.line.color.rgb = RGBColor.from_string(rgb)
            width = getattr(line, "width_emu", None) if line else None
            if width:
                from pptx.util import Emu

                conn.line.width = Emu(int(width))
            context.issues.append(
                f"STROKE_APPLIED: {source_object_id} stroke token {stroke!r} -> #{rgb}"
            )
        else:
            context.issues.append(
                f"SURFACE_TOKEN_UNKNOWN: {source_object_id} stroke token "
                f"{stroke!r} not in the loaded palette"
            )
    address = _address_of(slide, conn, "connector")
    context.emission_records.append(
        EmissionRecord(
            source_object_id=source_object_id, strategy="native_rebuild", address=address
        )
    )
    _bind_atom(
        context, slide, conn, source_object_id, f"{source_object_id}#shape", "geometry", "relocate"
    )
    return address


def _set_connector_arrows(
    conn: Any, begin: str, end: str, source_object_id: str, context: Any
) -> None:
    """Restore semantic arrowheads on a bound relation connector."""
    from pptx.oxml.ns import qn

    ln = conn.line._get_or_add_ln()
    for tag, value in (("a:headEnd", begin), ("a:tailEnd", end)):
        if value in ("", "none"):
            continue
        existing = ln.find(qn(tag))
        if existing is None:
            existing = ln.makeelement(qn(tag), {})
            ln.append(existing)
        existing.set("type", value if value != "none" else "none")
    context.issues.append(f"ARROWHEAD_PRESERVED: {source_object_id} begin={begin} end={end}")


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
                f"connector endpoint unresolved: {edge.get('from')}->{edge.get('to')}"
            )
            continue
        conn = slide.shapes.add_connector(
            MSO_CONNECTOR.STRAIGHT,
            adapter.Emu(0),
            adapter.Emu(0),
            adapter.Emu(914400),
            adapter.Emu(914400),
        )
        with contextlib.suppress(Exception):
            conn.line.fill.background()
        # Native connection sites (not a floating stub): the edge keeps its
        # source endpoints, resolved to real output shape ids above.
        cxn = conn._element.find(qn("p:nvCxnSpPr"))
        if cxn is not None:
            cNvCxnSpPr = cxn.find(qn("p:cNvCxnSpPr"))
            if cNvCxnSpPr is None:
                cNvCxnSpPr = etree.SubElement(cxn, qn("p:cNvCxnSpPr"))
            for tag, sid, site in (
                ("a:stCxn", src, edge.get("from_site")),
                ("a:endCxn", dst, edge.get("to_site")),
            ):
                el = cNvCxnSpPr.find(qn(tag))
                if el is None:
                    el = etree.SubElement(cNvCxnSpPr, qn(tag))
                el.set("id", str(sid))
                if site is not None:
                    el.set("idx", str(site))
        address = _address_of(slide, conn, "connector")
        if edge.get("object_id"):
            context.output_map.append(
                OutputSubBinding(
                    source_atom_id=f"{edge['object_id']}#edge",
                    target_address=address,
                    target_subaddress=(f"edge[{edge.get('from', '')}->{edge.get('to', '')}]"),
                    origin="exporter",
                    evidence=[f"obj:{edge['object_id']}", f"from_shape:{src}", f"to_shape:{dst}"],
                )
            )
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
                            or parent.tag.endswith("}pic")
                        ):
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
        context.issues.append(f"opaque object {source_object_id} not carried: needs_review")
    try:
        address = _address_of(slide, list(slide.shapes)[-1], "unknown")
    except IndexError:
        address = OutputShapeAddress(slide_part="", shape_id=0, group_path=[], kind="unknown")
    context.output_map.append(
        OutputSubBinding(
            source_atom_id=f"{source_object_id}#raw",
            target_address=address,
            target_subaddress="opaque",
            origin="exporter",
            evidence=[f"obj:{source_object_id}"],
        )
    )
    context.emission_records.append(
        EmissionRecord(
            source_object_id=source_object_id, strategy="opaque_transplant", address=address
        )
    )
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


def _notes_text_placeholder(notes_slide: Any) -> Any | None:
    """Locate the notes body placeholder (type body/idx 1) on a notes slide."""
    try:
        placeholders = list(notes_slide.placeholders)
    except Exception:  # noqa: BLE001 - notes slide without placeholders
        return None
    for ph in placeholders:
        try:
            ph_type = ph.placeholder_format.type
        except Exception:  # noqa: BLE001
            continue
        if str(ph_type).endswith("BODY") or "BODY" in str(ph_type):
            return ph
    for ph in placeholders:
        try:
            if ph.placeholder_format.idx == 1:
                return ph
        except Exception:  # noqa: BLE001
            continue
    return placeholders[1] if len(placeholders) > 1 else None


def _apply_notes_run_style(rpr: Any, style: Any) -> None:
    """Apply a ResolvedTextStyle onto an a:rPr/a:brPr element."""
    from pptx.oxml.ns import qn

    if style is None:
        return
    size_pt = getattr(style, "size_pt", None)
    if size_pt:
        rpr.set("sz", str(int(round(size_pt * 100))))
    bold = getattr(style, "bold", None)
    if bold is not None:
        rpr.set("b", "1" if bold else "0")
    italic = getattr(style, "italic", None)
    if italic is not None:
        rpr.set("i", "1" if italic else "0")
    underline = getattr(style, "underline", None)
    if underline:
        rpr.set("u", underline)
    typeface = getattr(style, "typeface", None)
    if typeface:
        latin = rpr.makeelement(qn("a:latin"), {})
        latin.set("typeface", typeface)
        rpr.append(latin)
    color = getattr(style, "color", None)
    resolved_rgb = getattr(color, "resolved_rgb", None) if color else None
    if resolved_rgb:
        fill = rpr.makeelement(qn("a:solidFill"), {})
        clr = rpr.makeelement(qn("a:srgbClr"), {})
        clr.set("val", resolved_rgb)
        fill.append(clr)
        rpr.append(fill)
    language = getattr(style, "language", None)
    if language:
        rpr.set("lang", language)


def copy_notes(
    source_notes: list[Any],
    output_slide: Any,
    context: NativeDeckContext,
    source_slide_id: str = "",
) -> dict[str, Any]:
    """Copy notes paragraphs 1:1 into the output notes slide.

    Each source paragraph maps to exactly one output paragraph; soft breaks
    (``a:br`` carriers) are emitted as ``a:br`` elements so paragraph count
    and break structure are preserved (a naive ``"\\n".join`` would turn a
    leading break-only paragraph into an extra empty paragraph and lose the
    ``notes:\\n`` atom).
    """
    from pptx.oxml.ns import qn

    try:
        notes_slide = output_slide.notes_slide
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}
    ph = _notes_text_placeholder(notes_slide)
    if ph is None:
        return {"ok": False, "reason": "notes_placeholder_missing"}
    tx_body = ph.text_frame._txBody
    break_after = set()
    for para in source_notes or []:
        for br in getattr(para, "breaks", []) or []:
            after = getattr(br, "after_run_id", "")
            if after:
                break_after.add(after)
    for old_p in tx_body.findall(qn("a:p")):
        tx_body.remove(old_p)
    for para in source_notes or []:
        p_elem = etree.SubElement(tx_body, qn("a:p"))
        for run in getattr(para, "runs", []) or []:
            run_id = getattr(run, "id", "")
            style = getattr(run, "source_style", None)
            if run_id in break_after:
                br = etree.SubElement(p_elem, qn("a:br"))
                rpr = etree.SubElement(br, qn("a:rPr"))
                _apply_notes_run_style(rpr, style)
                continue
            r = etree.SubElement(p_elem, qn("a:r"))
            rpr = etree.SubElement(r, qn("a:rPr"))
            _apply_notes_run_style(rpr, style)
            t = etree.SubElement(r, qn("a:t"))
            t.text = getattr(run, "text", "") or ""
        if getattr(para, "id", ""):
            try:
                address = _address_of(output_slide, list(output_slide.shapes)[0], "notes")
            except IndexError:
                address = OutputShapeAddress(slide_part="", shape_id=0, group_path=[], kind="notes")
            context.output_map.append(
                OutputSubBinding(
                    source_atom_id=f"{source_slide_id}/{para.id}",
                    target_address=address,
                    target_subaddress=f"notes-para[{para.id}]",
                    origin="exporter",
                    evidence=[f"slide:{source_slide_id}"],
                )
            )
    return {"ok": True, "paragraphs": len(source_notes or [])}


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
            pos = context.source_part_positions.get(str(item.get("target_ref", "")))
            if pos is None:
                # Fall back to the slide-id map (link-resolution granularity).
                target = context.source_to_output_slide_map.get(str(item.get("target_ref", "")))
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
