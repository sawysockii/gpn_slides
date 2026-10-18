"""Stage 9 §5.2: what a saved shape ACTUALLY displays.

The previous saved text-fit gate created a synthetic measurement request for
every ``text``/``shape`` object in the reopened package, including figures
that contain an empty ``a:p``. Ten of the reported "overflows" in the
2026-10-15 recovery run came from exactly that: eight small ellipses and two
empty frames that have no ``a:t`` and no field at all. Measuring them with a
default font manufactured an overflow that does not exist in the file.

Presence here is decided from the reopened OOXML bytes only:

* real ``a:t`` content (any length),
* ``a:fld`` fields with their cached value / runtime type,
* ``a:br`` soft breaks,
* bullet / numbering state (``a:buChar``, ``a:buAutoNum``, ``a:buNone``),
* effective run/paragraph visibility state.

It is deliberately NOT decided from text length, bounding box area, colour or
source object id: a one-character label is text, and a large empty frame is
not a panel. An ``empty_geometry`` verdict removes the shape from the TEXT FIT
scope only — it stays in geometry, style, path and semantic coverage.
"""

from __future__ import annotations

import contextlib
import zipfile
from pathlib import Path
from typing import Any

from lxml import etree

from .models import TextPresence, TextPresenceKind

A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
NS = {"a": A_NS, "p": P_NS}

# Shape containers that may carry a text body.
_TEXT_HOSTS = ("p:sp", "p:cxnSp", "p:pic")

_EMPTY = TextPresence(
    kind=TextPresenceKind.EMPTY_GEOMETRY,
    token_refs=[],
    visible_content_evidence=[],
    reason="txBody carries no a:t text, no field, no soft break and no "
           "bullet/numbering that renders a glyph",
)


def _alpha_of(fill: Any) -> float | None:
    """Effective alpha of a solidFill element, or None when not solid."""
    if fill is None:
        return None
    if fill.find("a:noFill", NS) is not None:
        return 0.0
    clr = fill.find("a:srgbClr", NS)
    if clr is None:
        clr = fill.find("a:schemeClr", NS)
    if clr is None:
        return None
    alpha = clr.find("a:alpha", NS)
    if alpha is not None:
        try:
            return int(alpha.get("val", "100000")) / 100000.0
        except (TypeError, ValueError):
            return None
    return 1.0


def _run_is_painted(rpr: Any, def_rpr: Any) -> bool:
    """Whether the run's fill is actually painted (not noFill/alpha 0)."""
    for holder in (rpr, def_rpr):
        if holder is None:
            continue
        fill = holder.find("a:solidFill", NS)
        if fill is None:
            continue
        alpha = _alpha_of(fill)
        return alpha is None or alpha > 0.0
    return True


def _bullet_state(ppr: Any) -> str:
    if ppr is None:
        return "inherited"
    if ppr.find("a:buNone", NS) is not None:
        return "none"
    if ppr.find("a:buChar", NS) is not None:
        return "bullet"
    auto = ppr.find("a:buAutoNum", NS)
    if auto is not None:
        if str(auto.get("type", "")).lower() == "none":
            return "none"
        return "numbered"
    if ppr.find("a:buBlip", NS) is not None:
        return "bullet"
    return "inherited"


def _inherited_bullet(tx_body: Any, level: int) -> str:
    """lstStyle / defPPr bullet for a level, or 'none' when undeclared."""
    lst = tx_body.find("a:lstStyle", NS)
    if lst is None:
        return "none"
    ppr = lst.find(f"a:lvl{level + 1}pPr", NS)
    return _bullet_state(ppr)


def _field_renders(field: Any) -> tuple[bool, str]:
    """(renders, reason) for one ``a:fld``."""
    ftype = str(field.get("type", "") or "").lower()
    cached = "".join(t.text or "" for t in field.findall("a:t", NS))
    if cached.strip():
        return True, f"field type={ftype or 'slidenum'} cached value present"
    if ftype in ("slidenum", "datetime1", "datetime2", "datetime3", "datetime4"):
        # Runtime-resolved field with an empty cache: the renderer still
        # prints something, so this is NOT an empty geometry.
        return True, f"runtime field type={ftype} resolves at render time"
    if ftype:
        return True, f"field type={ftype} with runtime semantics"
    return False, "field without type or cached value"


def inspect_saved_text_presence(sp_xml: Any) -> TextPresence:
    """Classify what one shape element actually displays.

    ``sp_xml`` is the ``lxml`` element of a ``p:sp`` / ``p:cxnSp`` /
    ``p:pic``. A missing text body is ``empty_geometry`` for text-fit
    purposes with an explicit reason.
    """
    if sp_xml is None:
        return TextPresence(
            kind=TextPresenceKind.EMPTY_GEOMETRY,
            reason="shape has no txBody element")
    tx_body = sp_xml.find("p:txBody", NS)
    if tx_body is None:
        return TextPresence(
            kind=TextPresenceKind.EMPTY_GEOMETRY,
            reason="shape has no txBody element")
    def_rpr = tx_body.find("a:lstStyle/a:defPPr/a:defRPr", NS)
    text_refs: list[str] = []
    field_refs: list[str] = []
    break_refs: list[str] = []
    bullet_refs: list[str] = []
    evidence: list[str] = []
    unpainted: list[str] = []
    has_field_runtime = False
    for p_index, p_el in enumerate(tx_body.findall("a:p", NS), start=1):
        ppr = p_el.find("a:pPr", NS)
        lvl = 0
        if ppr is not None and ppr.get("lvl"):
            try:
                lvl = int(ppr.get("lvl", "0"))
            except (TypeError, ValueError):
                lvl = 0
        bullet = _bullet_state(ppr)
        if bullet == "inherited":
            bullet = _inherited_bullet(tx_body, lvl)
        if bullet in ("bullet", "numbered"):
            bullet_refs.append(f"a:p[{p_index}].pPr.{bullet}")
        for r_index, r_el in enumerate(p_el.findall("a:r", NS), start=1):
            value = "".join(t.text or "" for t in r_el.findall("a:t", NS))
            if not value.strip():
                continue
            rpr = r_el.find("a:rPr", NS)
            painted = _run_is_painted(rpr, def_rpr)
            ref = f"a:p[{p_index}].a:r[{r_index}].a:t"
            if painted:
                text_refs.append(ref)
                evidence.append(f"{ref}: {len(value.strip())} visible chars")
            else:
                unpainted.append(ref)
                evidence.append(
                    f"{ref}: {len(value.strip())} chars, run fill is "
                    "noFill/alpha0 (token present, not painted)")
        for b_index, _br in enumerate(p_el.findall("a:br", NS), start=1):
            break_refs.append(f"a:p[{p_index}].a:br[{b_index}]")
        for f_index, fld in enumerate(p_el.findall("a:fld", NS), start=1):
            renders, why = _field_renders(fld)
            ref = f"a:p[{p_index}].a:fld[{f_index}]"
            if renders:
                field_refs.append(ref)
                has_field_runtime = True
                evidence.append(f"{ref}: {why}")
            else:
                evidence.append(f"{ref}: {why}")
    if field_refs:
        return TextPresence(
            kind=TextPresenceKind.FIELD, token_refs=field_refs,
            visible_content_evidence=evidence,
            reason="field content is resolved by the renderer, not by a "
                   "literal text run")
    if text_refs:
        return TextPresence(
            kind=TextPresenceKind.TEXT, token_refs=text_refs,
            visible_content_evidence=evidence,
            reason=("literal a:t tokens carry displayable content"
                    + (f"; {len(unpainted)} run(s) unpainted" if unpainted
                       else "")))
    if bullet_refs:
        return TextPresence(
            kind=TextPresenceKind.BULLET_OR_NUMBERING, token_refs=bullet_refs,
            visible_content_evidence=evidence,
            reason="bullet/numbering glyph is rendered by the paragraph "
                   "properties")
    if break_refs:
        return TextPresence(
            kind=TextPresenceKind.EMPTY_GEOMETRY, token_refs=break_refs,
            visible_content_evidence=evidence,
            reason="only soft/line breaks present; no text, field or bullet "
                   "renders a glyph")
    if unpainted:
        return TextPresence(
            kind=TextPresenceKind.TEXT, token_refs=unpainted,
            visible_content_evidence=evidence,
            reason="text tokens exist but every run fill is noFill/alpha0; "
                   "content is present and must still be measured and styled")
    if has_field_runtime:  # pragma: no cover - field_refs covers it
        return TextPresence(kind=TextPresenceKind.FIELD,
                            visible_content_evidence=evidence,
                            reason="runtime field")
    return TextPresence(
        kind=TextPresenceKind.EMPTY_GEOMETRY, token_refs=[],
        visible_content_evidence=evidence, reason=_EMPTY.reason)


def is_text_fit_applicable(presence: TextPresence) -> tuple[bool, str]:
    """Whether a saved shape belongs to the TEXT FIT scope (§5.2)."""
    if presence.kind is TextPresenceKind.EMPTY_GEOMETRY:
        return False, presence.reason
    if presence.kind is TextPresenceKind.UNKNOWN:
        return False, f"unknown text state: {presence.reason}"
    return True, presence.kind.value


def read_saved_presence(package_path: str | Path) -> dict[tuple[str, int], TextPresence]:
    """Presence per ``(slide_part, shape_id)`` from reopened package bytes."""
    out: dict[tuple[str, int], TextPresence] = {}
    try:
        archive = zipfile.ZipFile(package_path)
    except Exception:  # noqa: BLE001 - caller records unreadable package
        return out
    try:
        names = sorted(n for n in archive.namelist()
                       if n.startswith("ppt/slides/slide")
                       and n.endswith(".xml"))
    except Exception:  # noqa: BLE001
        return out
    try:
        for name in names:
            try:
                root = etree.fromstring(archive.read(name))
            except Exception:  # noqa: BLE001 - per-part unknown, continue
                continue
            for tag in _TEXT_HOSTS:
                for sp in root.findall(f".//{tag}", NS):
                    cnv = None
                    for nv in ("p:nvSpPr/p:cNvPr", "p:nvCxnSpPr/p:cNvPr",
                               "p:nvPicPr/p:cNvPr"):
                        cnv = sp.find(nv, NS)
                        if cnv is not None:
                            break
                    if cnv is None:
                        continue
                    try:
                        sid = int(cnv.get("id", ""))
                    except (TypeError, ValueError):
                        continue
                    out[(name, sid)] = inspect_saved_text_presence(sp)
    finally:
        with contextlib.suppress(Exception):
            archive.close()
    return out