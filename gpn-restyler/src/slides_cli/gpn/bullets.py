"""LS01 list profile and minimal OOXML bullet writer (spec §10).

``build_list_profile`` binds the compiled LS01 norm to verified source
indents/spacing; ``apply_ls01_paragraph`` mutates only an explicitly passed
derivative paragraph copy (never the immutable SourceDeckIR or the source
library). Idempotent: re-applying the same resolved profile changes nothing.

All normative values (marker font/char/size, nested size policy, colors) are
read from the compiled ontology snapshot — never hardcoded here.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Any

from lxml import etree

from .models import (
    CompiledOntology,
    ListLevelProfile,
    ListStyleProfile,
    NestedSizePolicy,
    ParagraphSpacing,
    SlideIR,
    TemplateProfile,
)

log = logging.getLogger(__name__)

A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS = {"a": A}

# OOXML a:font descriptor defaults for a symbol marker face (format
# constants, not corporate norms).
OOXML_DEFAULT_PITCH_FAMILY = "2"
OOXML_DEFAULT_CHARSET = "2"


def _qn(tag: str) -> str:
    prefix, local = tag.split(":", 1)
    return f"{{{NS[prefix]}}}{local}"


@dataclass
class ListLevelResolution:
    """Resolved LS01 formatting for one paragraph level (spec §10)."""

    level: int
    text_size_pt: float | None = None
    text_typeface: str | None = None
    text_color: str | None = None
    marker_font: str = ""
    marker_char: str = ""
    marker_pitch_family: str = ""
    marker_charset: str = ""
    marker_size_pct: int = 0
    marker_color_mode: str = "follow_paragraph_text"
    marker_color: str | None = None
    mar_left_emu: int | None = None
    indent_emu: int | None = None
    spacing: ParagraphSpacing = field(default_factory=ParagraphSpacing)
    derived: bool = False
    evidence: list[str] = field(default_factory=list)
    status: str = "ok"
    issues: list[str] = field(default_factory=list)


def build_list_profile(
    rules: CompiledOntology,
    template: TemplateProfile,
    references: list[SlideIR],
    *,
    list_id: str | None = None,
) -> ListStyleProfile:
    """Bind the loaded list norm to verified source indents/spacing (§10.1).

    ``list_id`` is looked up in the loaded collections. When it is omitted,
    the id is resolved from the loaded data itself: a corpus that declares
    exactly one list style binds that one; several candidates are an
    unresolved error instead of a hardcoded corporate id (§4.4.1).
    """
    if list_id is None:
        loaded = sorted(rules.lists)
        if len(loaded) == 1:
            list_id = loaded[0]
        elif not loaded:
            return ListStyleProfile(id="")
        else:
            raise ValueError(
                "list_id is required: the loaded ontology declares "
                f"{len(loaded)} list styles ({', '.join(loaded)}); refusing "
                "to guess a corporate list id"
            )
    compiled = rules.lists.get(list_id)
    if compiled is None:
        raise ValueError(
            f"list style {list_id!r} is not present in the loaded snapshot "
            f"(available: {', '.join(sorted(rules.lists)) or 'none'})"
        )

    # Confirm geometry from real source paragraphs (marL/indent rhythm).
    observed_indents: dict[int, tuple[int | None, int | None]] = {}
    for slide in references:
        for obj in slide.objects:
            payload = obj.payload
            text = getattr(payload, "text", None)
            if text is None and hasattr(payload, "paragraphs"):
                text = payload
            if text is None:
                continue
            for para in getattr(text, "paragraphs", []):
                if para.list_kind == "none":
                    continue
                if para.source_indent_emu is not None:
                    observed_indents[para.level] = (
                        para.source_indent_emu,
                        para.source_hanging_emu,
                    )

    levels: list[ListLevelProfile] = []
    for base in compiled.level_profiles:
        mar_l, hanging = observed_indents.get(base.level, (None, None))
        levels.append(
            ListLevelProfile(
                level=base.level,
                size_pt=base.size_pt,
                typeface=base.typeface,
                text_token=base.text_token,
                marker_color_mode=base.marker_color_mode,
                marker_color_token=base.marker_color_token,
                mar_left_emu=mar_l if mar_l is not None else base.mar_left_emu,
                hanging_indent_emu=hanging if hanging is not None else base.hanging_indent_emu,
                spacing=base.spacing,
                source_ref=base.source_ref,
                derived=base.derived or base.level not in observed_indents,
                derivation_rule=base.derivation_rule,
            )
        )

    return ListStyleProfile(
        id=compiled.id,
        representation=compiled.representation,
        marker_settings=dict(compiled.marker_settings),
        color_rules=list(compiled.color_rules),
        level_profiles=levels,
        exceptions=list(compiled.exceptions),
        evidence_refs=list(compiled.evidence_refs)
        + [f"observed_indents:{sorted(observed_indents)}"],
        nested_size_policy=compiled.nested_size_policy,
        nested_text_color=compiled.nested_text_color,
        nested_text_color_token=compiled.nested_text_color_token,
        list_text_typeface=compiled.list_text_typeface,
        list_text_primary_color=compiled.list_text_primary_color,
    )


def nested_size_for_level(
    parent_size_pt: float, *, level: int, policy: NestedSizePolicy
) -> float:
    """Compute the nested text size from the loaded policy (spec §4.4.6).

    ``parent >= if_parent_gte → parent - then_subtract`` else
    ``parent - otherwise_subtract``. Pure arithmetic over the loaded numbers:
    the historical 12/2/1/8 constants exist only inside the current corpus
    document, never in this algorithm. The ``minimum`` floor is *not* applied
    here — callers decide whether a below-minimum child is infeasible
    (``resolve_list_level``) instead of silently clamping.
    """
    if not 0 <= level <= 8:
        raise ValueError(f"list level {level} outside 0..8")
    if parent_size_pt >= policy.if_parent_gte:
        return parent_size_pt - policy.then_subtract
    return parent_size_pt - policy.otherwise_subtract


def resolve_list_level(
    *,
    level: int,
    parent_size_pt: float | None,
    background_token: str,
    paragraph_text_token: str,
    profile: ListStyleProfile,
) -> ListLevelResolution:
    """Resolve one LS01 level (spec §10.1–10.2).

    Size rule is read from the compiled nested_size_policy (loaded from JSON):
    parent>=if_parent_gte → parent-then_subtract else parent-otherwise_subtract;
    minimum from policy; a child that would need <minimum (or parent==minimum)
    is ``infeasible`` — never silently clamped.
    Marker color truth table (§10.2):
      1. level>=1 → buClrTx (paragraph foreground);
      2. level0 + white bg + paragraph token black/dark → fixed color from
         compiled color_rules (resolved via palette);
      3. otherwise → buClrTx.
    """
    if not 0 <= level <= 8:
        return ListLevelResolution(
            level=level, status="infeasible",
            issues=[f"level {level} outside 0..8"],
        )

    policy = profile.nested_size_policy

    if parent_size_pt is None:
        base = next((p for p in profile.level_profiles if p.level == level), None)
        text_size = base.size_pt if base else None
        derived = True
    elif policy is not None:
        text_size = nested_size_for_level(parent_size_pt, level=level, policy=policy)
        derived = level > 0
    else:
        text_size = None
        derived = True

    if text_size is not None and policy is not None and text_size < policy.minimum:
        return ListLevelResolution(
            level=level, status="infeasible",
            issues=[
                f"parent {parent_size_pt}pt would force child {text_size}pt < "
                f"{policy.minimum}; rebuild hierarchy or raise the base size"
            ],
        )
    if policy is not None and parent_size_pt == policy.minimum and level > 0:
        return ListLevelResolution(
            level=level, status="infeasible",
            issues=[
                f"parent at minimum {policy.minimum} cannot have a nested child; "
                "rebuild hierarchy"
            ],
        )

    base = next((p for p in profile.level_profiles if p.level == level), None)

    if level >= 1:
        color_mode = "follow_paragraph_text"
        marker_color = None
    elif (
        level == 0
        and background_token == "white"
        and paragraph_text_token in ("black", "dark")
    ):
        color_mode = "fixed"
        marker_color = _resolve_fixed_marker_color(profile)
    else:
        color_mode = "follow_paragraph_text"
        marker_color = None

    if background_token not in ("white", "light"):
        text_color = None
        issues = [f"background {background_token!r} needs a confirmed profile condition"]
    elif level >= 1 and background_token == "white":
        text_color = profile.nested_text_color
        issues = []
    else:
        text_color = None
        issues = []

    marker_font = profile.marker_settings.get("buFont", "")
    marker_char = profile.marker_settings.get("buChar", "")
    marker_pitch_family = profile.marker_settings.get("buFont_pitchFamily", "")
    marker_charset = profile.marker_settings.get("buFont_charset", "")
    marker_size_pct = int(profile.marker_settings.get("buSzPct", "0") or "0")

    return ListLevelResolution(
        level=level,
        text_size_pt=text_size,
        text_typeface=base.typeface if base else profile.list_text_typeface,
        text_color=text_color,
        marker_color_mode=color_mode,
        marker_color=marker_color,
        marker_font=marker_font,
        marker_char=marker_char,
        marker_pitch_family=marker_pitch_family,
        marker_charset=marker_charset,
        marker_size_pct=marker_size_pct,
        mar_left_emu=base.mar_left_emu if base else None,
        indent_emu=base.hanging_indent_emu if base else None,
        spacing=base.spacing if base else ParagraphSpacing(),
        derived=derived,
        evidence=[base.source_ref] if base and base.source_ref else [],
        status="ok",
        issues=issues,
    )


def _resolve_fixed_marker_color(profile: ListStyleProfile) -> str | None:
    """Resolve the fixed marker color from compiled color_rules priority."""
    for cr in profile.color_rules:
        when = cr.get("when", "")
        if isinstance(when, dict):
            level = when.get("level")
            bg = when.get("background_token")
            text_in = when.get("paragraph_text_token_in", [])
            if level == 0 and bg == "white" and isinstance(text_in, list):
                token = cr.get("color_token", "")
                rgb = cr.get("rgb", "")
                if rgb:
                    return str(rgb).lstrip("#").upper()
                if token:
                    return f"token:{token}"
        elif when == "otherwise":
            continue
    return None


def _upsert(parent: etree._Element, tag: str) -> etree._Element:
    node = parent.find(tag, namespaces=NS)
    if node is None:
        node = etree.SubElement(parent, _qn(tag))
    return node


def _remove_all(parent: etree._Element, *tags: str) -> None:
    for tag in tags:
        for node in parent.findall(tag, namespaces=NS):
            parent.remove(node)


def apply_ls01_paragraph(
    paragraph_element: etree._Element,
    resolved: ListLevelResolution,
) -> None:
    """Apply an LS01 resolution to a derivative ``a:p`` copy (spec §10.3).

    Mutates only the passed element. Removes conflicting bullet branches
    (buNone/buAutoNum/buBlip vs buChar), keeps exactly one color branch
    (fixed buClr vs buClrTx), sets Wingdings § 80%, and writes native
    marL/indent/spacing. Idempotent for the same resolution.
    """
    if resolved.status != "ok":
        raise ValueError(f"cannot apply infeasible LS01 resolution: {resolved.issues}")

    ppr = paragraph_element.find("a:pPr", namespaces=NS)
    if ppr is None:
        ppr = etree.Element(_qn("a:pPr"))
        paragraph_element.insert(0, ppr)

    if not 0 <= resolved.level <= 8:
        raise ValueError(f"level {resolved.level} outside 0..8")
    if resolved.level == 0:
        ppr.attrib.pop("lvl", None)
    else:
        ppr.set("lvl", str(resolved.level))

    # Exactly one bullet branch: remove the alternatives first.
    _remove_all(ppr, "a:buNone", "a:buAutoNum", "a:buBlip")
    for old in ppr.findall("a:buChar", namespaces=NS):
        ppr.remove(old)
    bu_char = etree.SubElement(ppr, _qn("a:buChar"))
    bu_char.set("char", resolved.marker_char)

    for old in ppr.findall("a:buFont", namespaces=NS):
        ppr.remove(old)
    bu_font = etree.SubElement(ppr, _qn("a:buFont"))
    bu_font.set("typeface", resolved.marker_font)
    # pitchFamily/charset are not declared by the loaded ontology for the
    # marker font: they come from the OOXML font descriptor defaults and are
    # recorded as format constants, not as a JSON-sourced normative value
    # (§4.4.2).
    bu_font.set("pitchFamily", resolved.marker_pitch_family or OOXML_DEFAULT_PITCH_FAMILY)
    bu_font.set("charset", resolved.marker_charset or OOXML_DEFAULT_CHARSET)

    # Exactly one size mode.
    for old in ppr.findall("a:buSz", namespaces=NS):
        ppr.remove(old)
    for old in ppr.findall("a:buSzPct", namespaces=NS):
        ppr.remove(old)
    bu_sz = etree.SubElement(ppr, _qn("a:buSzPct"))
    bu_sz.set("val", str(resolved.marker_size_pct))

    # Exactly one color branch.
    for old in ppr.findall("a:buClr", namespaces=NS):
        ppr.remove(old)
    for old in ppr.findall("a:buClrTx", namespaces=NS):
        ppr.remove(old)
    if resolved.marker_color_mode == "fixed" and resolved.marker_color:
        bu_clr = etree.SubElement(ppr, _qn("a:buClr"))
        srgb = etree.SubElement(bu_clr, _qn("a:srgbClr"))
        srgb.set("val", resolved.marker_color.upper())
    else:
        etree.SubElement(ppr, _qn("a:buClrTx"))

    if resolved.mar_left_emu is not None:
        ppr.set("marL", str(resolved.mar_left_emu))
    if resolved.indent_emu is not None:
        ppr.set("indent", str(resolved.indent_emu))
    if resolved.spacing.before_pt is not None:
        spc_bef = _upsert(ppr, "a:spcBef")
        _remove_all(spc_bef, "a:spcPts", "a:spcPct")
        pts = etree.SubElement(spc_bef, _qn("a:spcPts"))
        pts.set("val", str(int(resolved.spacing.before_pt * 100)))
    if resolved.spacing.after_pt is not None:
        spc_aft = _upsert(ppr, "a:spcAft")
        _remove_all(spc_aft, "a:spcPts", "a:spcPct")
        pts = etree.SubElement(spc_aft, _qn("a:spcPts"))
        pts.set("val", str(int(resolved.spacing.after_pt * 100)))

    _ = copy  # keep import used for callers cloning elements first
    _ = Any
