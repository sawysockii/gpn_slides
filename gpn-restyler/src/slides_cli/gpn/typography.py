"""Theme resolution, style inheritance, and font inventory (spec §3, §8, §11).

Stage 1: load_theme_profiles, build_style_inheritance_context,
resolve_effective_style, resolve_theme_typeface.
Stage 2: inventory_fonts, read_font_metadata, check_glyph_coverage.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lxml import etree

from .assets import AssetStore
from .models import (
    AssetRef,
    ColorExpression,
    ColorModifier,
    DiscoveredFontFile,
    FontAsset,
    FontDiscoveryReport,
    FontInventory,
    Issue,
    MeasurementFontBundle,
    ResolvedTextStyle,
    Severity,
    StyleSource,
    ThemeFontScheme,
    ThemeProfile,
    TypefaceResolution,
)
from .package import PackageGraph

log = logging.getLogger(__name__)

A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = {"a": A, "p": P, "r": R}

THEME_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme"
)
SLIDE_LAYOUT_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout"
)
SLIDE_MASTER_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster"
)


def qn(tag: str) -> str:
    prefix, local = tag.split(":", 1)
    nsmap = {"a": A, "p": P, "r": R}
    return f"{{{nsmap[prefix]}}}{local}"


# ---------------------------------------------------------------------------
# Stage 1: theme profiles
# ---------------------------------------------------------------------------


def _find(node: etree._Element, path: str) -> etree._Element | None:
    return node.find(path, namespaces=NS)


def _findall(node: etree._Element, path: str) -> list[etree._Element]:
    return list(node.findall(path, namespaces=NS))


def _font_scheme(scheme: etree._Element | None) -> tuple[ThemeFontScheme, dict[str, str]]:
    """Read a:fontScheme into typed major/minor fonts + script fonts."""
    if scheme is None:
        return ThemeFontScheme(), {}
    major = scheme.find("a:majorFont", namespaces=NS)
    minor = scheme.find("a:minorFont", namespaces=NS)

    def _font_part(part: etree._Element | None) -> dict[str, str | None]:
        if part is None:
            return {}
        latin = part.find("a:latin", namespaces=NS)
        ea = part.find("a:ea", namespaces=NS)
        cs = part.find("a:cs", namespaces=NS)
        return {
            "latin": latin.get("typeface") if latin is not None else None,
            "east_asian": ea.get("typeface") if ea is not None else None,
            "complex_script": cs.get("typeface") if cs is not None else None,
        }

    major_d = _font_part(major)
    minor_d = _font_part(minor)
    major_scheme = ThemeFontScheme(
        major_latin=major_d.get("latin"),
        major_east_asian=major_d.get("east_asian"),
        major_complex_script=major_d.get("complex_script"),
    )
    minor_scheme = ThemeFontScheme(
        minor_latin=minor_d.get("latin"),
        minor_east_asian=minor_d.get("east_asian"),
        minor_complex_script=minor_d.get("complex_script"),
    )

    script_fonts: dict[str, str] = {}
    if major is not None:
        for script_el in _findall(major, "a:font"):
            script = script_el.get("script") or ""
            typeface = script_el.get("typeface") or ""
            if script and typeface:
                script_fonts[f"major:{script}"] = typeface
    if minor is not None:
        for script_el in _findall(minor, "a:font"):
            script = script_el.get("script") or ""
            typeface = script_el.get("typeface") or ""
            if script and typeface:
                script_fonts[f"minor:{script}"] = typeface

    return major_scheme, minor_scheme, script_fonts  # type: ignore[return-value]


def _color_scheme(
    scheme: etree._Element | None,
) -> tuple[dict[str, str], dict[str, list[ColorModifier]]]:
    """Read a:clrScheme into color map + transform sequences."""
    if scheme is None:
        return {}, {}
    colors: dict[str, str] = {}
    transforms: dict[str, list[ColorModifier]] = {}
    for child in scheme:
        local = etree.QName(child).localname
        srgb = child.find("a:srgbClr", namespaces=NS)
        sys_clr = child.find("a:sysClr", namespaces=NS)
        if srgb is not None:
            colors[local] = (srgb.get("val") or "").upper()
        elif sys_clr is not None:
            colors[local] = sys_clr.get("lastClr") or sys_clr.get("val") or ""
        mods: list[ColorModifier] = []
        for mod in child:
            if etree.QName(mod).localname in (
                "tint", "shade", "lumMod", "lumOff", "alpha",
                "satMod", "hueMod", "redMod", "greenMod", "blueMod",
                "redOff", "greenOff", "blueOff", "gamma", "invGamma",
                "inv", "gray", "comp", "hsl",
            ):
                name = etree.QName(mod).localname
                val = mod.get("val")
                if val is not None:
                    with contextlib.suppress(ValueError):
                        mods.append(ColorModifier(name=name, value=int(val)))
        if mods:
            transforms[local] = mods
    return colors, transforms


def load_theme_profiles(
    graph: PackageGraph, store: AssetStore | None = None
) -> dict[str, ThemeProfile]:
    """Load all theme parts reachable from slides via layout→master→theme.

    ``store`` is the AssetStore that holds the package parts; without it
    theme XML bytes cannot be resolved and no profiles are returned.
    """
    profiles: dict[str, ThemeProfile] = {}
    if store is None:
        return profiles
    for slide_part in graph.slide_order:
        layout_part = _related_part(graph, slide_part, SLIDE_LAYOUT_REL_TYPE)
        if layout_part is None:
            continue
        master_part = _related_part(graph, layout_part, SLIDE_MASTER_REL_TYPE)
        if master_part is None:
            continue
        theme_part = _related_part(graph, master_part, THEME_REL_TYPE)
        if theme_part is None or theme_part in profiles:
            continue
        part_ir = graph.parts_by_name.get(theme_part)
        if part_ir is None or part_ir.asset is None:
            continue
        try:
            data = store.read_bytes(part_ir.asset)
            root = etree.fromstring(
                data, parser=etree.XMLParser(resolve_entities=False, no_network=True)
            )
        except Exception:
            continue
        font_scheme = root.find("a:themeElements/a:fontScheme", namespaces=NS)
        major, minor, script_fonts = _font_scheme(font_scheme)
        clr_scheme = root.find("a:themeElements/a:clrScheme", namespaces=NS)
        colors, transforms = _color_scheme(clr_scheme)
        profiles[theme_part] = ThemeProfile(
            theme_part=theme_part,
            source_hash=part_ir.asset.sha256,
            major_fonts=major,
            minor_fonts=minor,
            script_fonts=script_fonts,
            colors=colors,
            color_transforms=transforms,
        )
    return profiles


def _related_part(graph: PackageGraph, part: str, rel_type: str) -> str | None:
    for rel in graph.outgoing.get(part, []):
        if rel.rel_type == rel_type and rel.resolved_part:
            return rel.resolved_part
    return None


# ---------------------------------------------------------------------------
# Stage 1: style inheritance context
# ---------------------------------------------------------------------------


@dataclass
class StyleInheritanceContext:
    """Runtime context for resolving effective text style (spec §3.3)."""

    owner_part: str
    slide_part: str
    layout_part: str | None = None
    master_part: str | None = None
    theme_part: str | None = None
    placeholder_type: str | None = None
    placeholder_index: int | None = None
    paragraph_level: int = 0
    script: str = "latin"
    language: str | None = None
    resolved_theme: ThemeProfile | None = None
    color_map: dict[str, str] = field(default_factory=dict)
    property_layers: list[str] = field(default_factory=list)
    body_properties: dict[str, Any] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)


def build_style_inheritance_context(
    element: etree._Element,
    *,
    owner_part: str,
    import_context: Any,
) -> StyleInheritanceContext:
    """Build the inheritance context for a text element (spec §3.3)."""
    slide_part = owner_part
    layout_part = _related_part(import_context.graph, slide_part, SLIDE_LAYOUT_REL_TYPE)
    master_part = None
    theme_part = None
    if layout_part:
        master_part = _related_part(import_context.graph, layout_part, SLIDE_MASTER_REL_TYPE)
    if master_part:
        theme_part = _related_part(import_context.graph, master_part, THEME_REL_TYPE)

    placeholder_type = None
    placeholder_index = None
    ph = element.find(".//p:ph", namespaces=NS)
    if ph is not None:
        ph_type = ph.get("type")
        if ph_type:
            placeholder_type = ph_type
        ph_idx = ph.get("idx")
        if ph_idx:
            with contextlib.suppress(ValueError):
                placeholder_index = int(ph_idx)

    ppr = element.find("a:pPr", namespaces=NS)
    level = 0
    if ppr is not None:
        lvl = ppr.get("lvl")
        if lvl:
            with contextlib.suppress(ValueError):
                level = int(lvl)

    lang = None
    rpr = element.find("a:rPr", namespaces=NS)
    if rpr is not None:
        lang = rpr.get("lang")

    theme_profile = None
    if theme_part:
        profiles = getattr(import_context, "theme_profiles", {})
        theme_profile = profiles.get(theme_part)

    color_map: dict[str, str] = {}
    try:
        _root = element.getroottree().getroot() \
            if hasattr(element, "getroottree") else None
        color_map = parse_clr_map_override(_root)
    except Exception:
        color_map = {}

    body_properties: dict[str, Any] = {}
    tx_body = element.getparent() if element.tag == qn("a:r") else element
    if tx_body is not None:
        body_pr = tx_body.find("a:bodyPr", namespaces=NS)
        if body_pr is not None:
            body_properties["anchor"] = body_pr.get("anchor")
            body_properties["wrap"] = body_pr.get("wrap")
            norm_autofit = body_pr.find("a:normAutofit", namespaces=NS)
            if norm_autofit is not None:
                font_scale = norm_autofit.get("fontScale")
                if font_scale:
                    with contextlib.suppress(ValueError):
                        body_properties["font_scale"] = int(font_scale) / 100000.0

    layers = ["run", "paragraph", "list_style", "shape", "layout", "master", "theme"]

    return StyleInheritanceContext(
        owner_part=owner_part,
        slide_part=slide_part,
        layout_part=layout_part,
        master_part=master_part,
        theme_part=theme_part,
        placeholder_type=placeholder_type,
        placeholder_index=placeholder_index,
        paragraph_level=level,
        language=lang,
        resolved_theme=theme_profile,
        color_map=color_map,
        property_layers=layers,
        body_properties=body_properties,
    )


# ---------------------------------------------------------------------------
# Stage 1: effective style resolution
# ---------------------------------------------------------------------------


def _bool_attr(value: str | None) -> bool | None:
    """Parse OOXML boolean: '1'/'true' → True, '0'/'false' → False, None → None."""
    if value is None:
        return None
    v = value.strip().lower()
    if v in ("1", "true", "on"):
        return True
    if v in ("0", "false", "off"):
        return False
    return None


def _resolve_typeface_from_theme(
    raw_typeface: str,
    *,
    theme: ThemeProfile,
    script: str,
    language: str | None,
) -> TypefaceResolution:
    """Resolve a theme alias (+mj-lt, +mn-lt, etc.) to a real typeface."""
    if not raw_typeface:
        return TypefaceResolution(
            raw_typeface=raw_typeface or "",
            resolved_typeface=None,
            theme_part=theme.theme_part,
            issues=[Issue(
                rule_id="TYPOGRAPHY",
                code="THEME_ALIAS_EMPTY",
                severity=Severity.WARNING,
                details="empty typeface in theme alias",
            )],
        )

    if not raw_typeface.startswith("+"):
        return TypefaceResolution(
            raw_typeface=raw_typeface,
            resolved_typeface=raw_typeface,
            theme_part=theme.theme_part,
        )

    alias = raw_typeface[1:]
    is_major = alias.startswith("mj-")
    is_minor = alias.startswith("mn-")
    if not is_major and not is_minor:
        return TypefaceResolution(
            raw_typeface=raw_typeface,
            resolved_typeface=None,
            theme_part=theme.theme_part,
            issues=[Issue(
                rule_id="TYPOGRAPHY",
                code="THEME_ALIAS_UNKNOWN",
                severity=Severity.WARNING,
                details=f"unknown theme alias: {raw_typeface}",
            )],
        )

    scheme = theme.major_fonts if is_major else theme.minor_fonts
    script_key = script if script else "latin"

    if script_key == "latin" or script_key == "lt":
        resolved = scheme.major_latin if is_major else scheme.minor_latin
    elif script_key in ("eastAsian", "ea"):
        resolved = scheme.major_east_asian if is_major else scheme.minor_east_asian
    elif script_key in ("complexScript", "cs"):
        resolved = scheme.major_complex_script if is_major else scheme.minor_complex_script
    else:
        prefix = "major" if is_major else "minor"
        resolved = theme.script_fonts.get(f"{prefix}:{script_key}")

    if resolved is None or resolved == "":
        return TypefaceResolution(
            raw_typeface=raw_typeface,
            resolved_typeface=None,
            theme_part=theme.theme_part,
            issues=[Issue(
                rule_id="TYPOGRAPHY",
                code="THEME_ALIAS_UNRESOLVED",
                severity=Severity.WARNING,
                details=f"theme alias {raw_typeface} has no resolved typeface "
                        f"for script={script_key}",
            )],
        )

    return TypefaceResolution(
        raw_typeface=raw_typeface,
        resolved_typeface=resolved,
        theme_part=theme.theme_part,
    )


def resolve_theme_typeface(
    raw_typeface: str,
    *,
    theme: ThemeProfile,
    script: str,
    language: str | None,
) -> TypefaceResolution:
    """Public alias for _resolve_typeface_from_theme (spec §3.1)."""
    return _resolve_typeface_from_theme(
        raw_typeface, theme=theme, script=script, language=language
    )


def parse_clr_map_override(slide_element: Any) -> dict[str, str]:
    """Parse ``p:clrMapOvr`` into a scheme-name remapping (Stage 3 §2.3).

    Returns e.g. ``{"tx1": "dk2"}`` meaning the slide's tx1 resolves through
    the theme's dk2. Empty dict when the slide carries no override.
    """
    mapping: dict[str, str] = {}
    if slide_element is None:
        return mapping
    try:
        nodes = slide_element.xpath(".//*[local-name()='clrMapOvr']")
    except Exception:
        return mapping
    for ovr in nodes or []:
        for child in ovr:
            lname = etree.QName(child).localname
            if lname not in ("overrideClrMapping", "clrMap"):
                continue
            for attr, val in child.attrib.items():
                key = attr.split("}", 1)[-1] if "}" in attr else attr
                mapping[key] = val
    return mapping


def _color_from_scheme(
    scheme_name: str,
    *,
    theme: ThemeProfile,
    color_map: dict[str, str],
) -> ColorExpression | None:
    """Resolve a scheme color with modifiers preserved.

    ``color_map`` is the slide's clrMapOvr remapping: the requested scheme
    name is first mapped through the override, then resolved in the theme.
    """
    mapped = color_map.get(scheme_name, scheme_name) if color_map else scheme_name
    if mapped not in theme.colors:
        return ColorExpression(kind="scheme", value=scheme_name, resolved_rgb=None)
    rgb = theme.colors[mapped]
    mods = theme.color_transforms.get(mapped, [])
    return ColorExpression(
        kind="scheme",
        value=scheme_name,
        modifiers=mods,
        resolved_rgb=rgb,
    )


def resolve_effective_style(
    element: etree._Element,
    inheritance: StyleInheritanceContext,
) -> ResolvedTextStyle:
    """Resolve effective text style from element + inheritance (spec §3.3).

    Property layers (most specific first):
    1. run rPr (explicit)
    2. paragraph pPr/defRPr
    3. list style defRPr
    4. shape style/fontRef
    5. layout placeholder
    6. master txStyles
    7. theme font alias
    """
    provenance: dict[str, StyleSource] = {}
    unresolved: list[str] = []

    tag = etree.QName(element).localname
    if tag == "r":
        # Run element: rPr is a child, pPr lives on the parent paragraph.
        rpr = element.find("a:rPr", namespaces=NS)
        parent = element.getparent()
        if parent is not None and etree.QName(parent).localname == "p":
            ppr = parent.find("a:pPr", namespaces=NS)
        else:
            ppr = element.find("a:pPr", namespaces=NS)
    elif tag == "p":
        rpr = None
        ppr = element.find("a:pPr", namespaces=NS)
    else:
        rpr = element.find("a:rPr", namespaces=NS)
        ppr = element.find("a:pPr", namespaces=NS)

    typeface: str | None = None
    size_pt: float | None = None
    bold: bool | None = None
    italic: bool | None = None
    underline: str | None = None
    color: ColorExpression | None = None
    lang = inheritance.language

    # Layer 1: run rPr
    if rpr is not None:
        latin = rpr.find("a:latin", namespaces=NS)
        if latin is not None:
            raw = latin.get("typeface")
            if raw:
                if raw.startswith("+") and inheritance.resolved_theme:
                    resolved = _resolve_typeface_from_theme(
                        raw,
                        theme=inheritance.resolved_theme,
                        script=inheritance.script,
                        language=lang,
                    )
                    typeface = resolved.resolved_typeface
                    if resolved.resolved_typeface is None:
                        unresolved.append(f"typeface:{raw}")
                    provenance["typeface"] = StyleSource(
                        part_name=inheritance.owner_part,
                        xml_path="a:rPr/a:latin",
                        layer="run",
                        property_name="typeface",
                        raw_value=raw,
                    )
                else:
                    typeface = raw
                    provenance["typeface"] = StyleSource(
                        part_name=inheritance.owner_part,
                        xml_path="a:rPr/a:latin",
                        layer="run",
                        property_name="typeface",
                        raw_value=raw,
                    )
        sz = rpr.get("sz")
        if sz:
            try:
                size_pt = int(sz) / 100.0
                provenance["size_pt"] = StyleSource(
                    part_name=inheritance.owner_part,
                    xml_path="a:rPr/@sz",
                    layer="run",
                    property_name="size_pt",
                    raw_value=sz,
                )
            except ValueError:
                pass
        b_val = _bool_attr(rpr.get("b"))
        if b_val is not None:
            bold = b_val
            provenance["bold"] = StyleSource(
                part_name=inheritance.owner_part,
                xml_path="a:rPr/@b",
                layer="run",
                property_name="bold",
                raw_value=rpr.get("b") or "",
            )
        i_val = _bool_attr(rpr.get("i"))
        if i_val is not None:
            italic = i_val
            provenance["italic"] = StyleSource(
                part_name=inheritance.owner_part,
                xml_path="a:rPr/@i",
                layer="run",
                property_name="italic",
                raw_value=rpr.get("i") or "",
            )
        u_val = rpr.get("u")
        if u_val:
            underline = u_val
        solid = rpr.find("a:solidFill", namespaces=NS)
        if solid is not None:
            srgb = solid.find("a:srgbClr", namespaces=NS)
            scheme = solid.find("a:schemeClr", namespaces=NS)
            if srgb is not None:
                color = ColorExpression(
                    kind="srgb", value=(srgb.get("val") or "").upper()
                )
                provenance["color"] = StyleSource(
                    part_name=inheritance.owner_part,
                    xml_path="a:rPr/a:solidFill/a:srgbClr",
                    layer="run",
                    property_name="color",
                    raw_value=srgb.get("val") or "",
                )
            elif scheme is not None:
                scheme_val = scheme.get("val") or ""
                if inheritance.resolved_theme and scheme_val in inheritance.resolved_theme.colors:
                    color = _color_from_scheme(
                        scheme_val,
                        theme=inheritance.resolved_theme,
                        color_map=inheritance.color_map,
                    )
                else:
                    color = ColorExpression(kind="scheme", value=scheme_val)
                provenance["color"] = StyleSource(
                    part_name=inheritance.owner_part,
                    xml_path="a:rPr/a:solidFill/a:schemeClr",
                    layer="run",
                    property_name="color",
                    raw_value=scheme_val,
                )

    # Layer 2: paragraph pPr/defRPr
    if ppr is not None:
        def_rpr = ppr.find("a:defRPr", namespaces=NS)
        if def_rpr is not None:
            if typeface is None:
                latin = def_rpr.find("a:latin", namespaces=NS)
                if latin is not None:
                    raw = latin.get("typeface")
                    if raw:
                        if raw.startswith("+") and inheritance.resolved_theme:
                            resolved = _resolve_typeface_from_theme(
                                raw,
                                theme=inheritance.resolved_theme,
                                script=inheritance.script,
                                language=lang,
                            )
                            typeface = resolved.resolved_typeface
                            if resolved.resolved_typeface is None:
                                unresolved.append(f"typeface:{raw}")
                        else:
                            typeface = raw
                        provenance["typeface"] = StyleSource(
                            part_name=inheritance.owner_part,
                            xml_path="a:pPr/a:defRPr/a:latin",
                            layer="paragraph",
                            property_name="typeface",
                            raw_value=raw,
                        )
            if size_pt is None:
                sz = def_rpr.get("sz")
                if sz:
                    try:
                        size_pt = int(sz) / 100.0
                        provenance["size_pt"] = StyleSource(
                            part_name=inheritance.owner_part,
                            xml_path="a:pPr/a:defRPr/@sz",
                            layer="paragraph",
                            property_name="size_pt",
                            raw_value=sz,
                        )
                    except ValueError:
                        pass
            if bold is None:
                b_val = _bool_attr(def_rpr.get("b"))
                if b_val is not None:
                    bold = b_val
                    provenance["bold"] = StyleSource(
                        part_name=inheritance.owner_part,
                        xml_path="a:pPr/a:defRPr/@b",
                        layer="paragraph",
                        property_name="bold",
                        raw_value=def_rpr.get("b") or "",
                    )
            if italic is None:
                i_val = _bool_attr(def_rpr.get("i"))
                if i_val is not None:
                    italic = i_val
                    provenance["italic"] = StyleSource(
                        part_name=inheritance.owner_part,
                        xml_path="a:pPr/a:defRPr/@i",
                        layer="paragraph",
                        property_name="italic",
                        raw_value=def_rpr.get("i") or "",
                    )
            if color is None:
                solid = def_rpr.find("a:solidFill", namespaces=NS)
                if solid is not None:
                    srgb = solid.find("a:srgbClr", namespaces=NS)
                    scheme = solid.find("a:schemeClr", namespaces=NS)
                    if srgb is not None:
                        color = ColorExpression(
                            kind="srgb", value=(srgb.get("val") or "").upper()
                        )
                    elif scheme is not None:
                        scheme_val = scheme.get("val") or ""
                        theme = inheritance.resolved_theme
                        if theme and scheme_val in theme.colors:
                            color = _color_from_scheme(
                                scheme_val,
                                theme=theme,
                                color_map=inheritance.color_map,
                            )
                        else:
                            color = ColorExpression(kind="scheme", value=scheme_val)

    # Layer 7: theme fallback for typeface
    if typeface is None and inheritance.resolved_theme:
        scheme = inheritance.resolved_theme.minor_fonts
        if inheritance.script in ("latin", "lt"):
            typeface = scheme.minor_latin
        elif inheritance.script in ("eastAsian", "ea"):
            typeface = scheme.minor_east_asian
        elif inheritance.script in ("complexScript", "cs"):
            typeface = scheme.minor_complex_script
        if typeface:
            provenance["typeface"] = StyleSource(
                part_name=inheritance.theme_part or inheritance.owner_part,
                xml_path="theme/minorFont",
                layer="theme",
                property_name="typeface",
                raw_value=typeface,
            )
        else:
            unresolved.append("typeface:theme")

    font_scale = 1.0
    if "font_scale" in inheritance.body_properties:
        font_scale = inheritance.body_properties["font_scale"]

    inherited_from = list(provenance.keys())

    return ResolvedTextStyle(
        typeface=typeface,
        size_pt=size_pt,
        bold=bold,
        italic=italic,
        underline=underline,
        color=color,
        language=lang,
        inherited_from=inherited_from,
        effective_font_scale=font_scale,
        provenance=provenance,
        unresolved=unresolved,
    )


# ---------------------------------------------------------------------------
# Stage 2: font inventory
# ---------------------------------------------------------------------------


def read_font_metadata(path: Path, collection_index: int = 0) -> FontAsset:
    """Read font metadata via fonttools (spec §8)."""
    from fontTools.ttLib import TTCollection, TTFont

    asset_ref = None
    try:
        from .assets import sha256_file
        asset_ref = AssetRef(
            sha256=sha256_file(path),
            relative_path=path.name,
            media_type="font/ttf",
            byte_count=path.stat().st_size,
        )
    except OSError:
        pass

    typeface = ""
    postscript_name = ""
    family = ""
    subfamily = ""
    fs_type: int | None = None
    cmap_coverage: dict[str, bool] = {}
    symbol_mapping: dict[str, str] = {}

    try:
        if path.suffix.lower() in (".ttc", ".otc"):
            coll = TTCollection(str(path))
            if collection_index < len(coll.fonts):
                font = coll.fonts[collection_index]
            else:
                font = coll.fonts[0]
        else:
            font = TTFont(str(path), fontNumber=collection_index)

        name_table = font["name"]
        family = name_table.getDebugName(1) or ""
        subfamily = name_table.getDebugName(2) or ""
        typeface = name_table.getDebugName(4) or ""
        postscript_name = name_table.getDebugName(6) or ""

        if "OS/2" in font:
            fs_type = font["OS/2"].fsType

        if "cmap" in font:
            cmap_table = font["cmap"]
            for table in cmap_table.tables:
                cmap = table.cmap
                cmap_coverage["cyrillic"] = all(ord(c) in cmap for c in "АаБбВвГгДдЕеЁёЖж")
                cmap_coverage["latin"] = all(ord(c) in cmap for c in "AaBbCcDdEeFfGg")
                cmap_coverage["digits"] = all(ord(c) in cmap for c in "0123456789")
                cmap_coverage["currency"] = all(ord(c) in cmap for c in "€$£¥₽")
                cmap_coverage["symbols"] = all(ord(c) in cmap for c in "§—–−%")
                if table.platformID == 3 and table.platEncID == 0:
                    symbol_mapping["platform"] = "windows_symbol"
                    for char_code, glyph_name in cmap.items():
                        if 0xF000 <= char_code <= 0xF0FF:
                            symbol_mapping[f"0x{char_code:04X}"] = glyph_name

    except Exception:
        pass

    embedding_rights = "unknown"
    if fs_type is not None:
        if fs_type == 0:
            embedding_rights = "installable"
        elif fs_type & 0x02:
            embedding_rights = "restricted"
        elif fs_type & 0x04:
            embedding_rights = "preview_print"
        elif fs_type & 0x08:
            embedding_rights = "editable"
        else:
            embedding_rights = "restricted"

    return FontAsset(
        asset=asset_ref,
        typeface=typeface,
        postscript_name=postscript_name,
        family=family,
        subfamily=subfamily,
        collection_index=collection_index if path.suffix.lower() in (".ttc", ".otc") else None,
        cmap_coverage=cmap_coverage,
        symbol_mapping=symbol_mapping,
        fs_type=fs_type,
        embedding_rights=embedding_rights,
    )


def check_glyph_coverage(
    font_asset: FontAsset,
    required_characters: str,
    font_path: str | Path | None = None,
) -> dict[str, bool]:
    """Check whether a font covers the required characters (spec §8).

    With ``font_path`` the best Unicode cmap is read from the actual bytes
    and every character is checked individually. Without bytes nothing can
    be verified, so every character reports False (unverified is never a
    pass). Symbol-font private mappings (e.g. Wingdings charset=2) are not
    Unicode coverage and are handled separately via ``symbol_mapping``.
    """
    result: dict[str, bool] = {}
    cmap: dict[int, str] | None = None
    if font_path is not None:
        try:
            from fontTools.ttLib import TTFont
            cmap = TTFont(str(font_path), lazy=True).getBestCmap()
        except Exception:  # noqa: BLE001 - unreadable bytes stay unverified
            cmap = None
    for char in required_characters:
        result[char] = (ord(char) in cmap) if cmap is not None else False
    return result


def inventory_fonts(
    fonts_dir: Path,
    required_faces: set[str],
) -> FontInventory:
    """Inventory all fonts in a directory against required faces (spec §8)."""
    fonts_dir = Path(fonts_dir)
    faces: dict[str, FontAsset] = {}
    marker_faces: dict[str, FontAsset] = {}
    missing: list[str] = []
    unverified: list[str] = []
    issues: list[Issue] = []

    if not fonts_dir.is_dir():
        issues.append(Issue(
            rule_id="FONTS",
            code="FONTS_DIR_MISSING",
            severity=Severity.ERROR,
            details=f"fonts directory not found: {fonts_dir}",
        ))
        return FontInventory(
            faces=faces,
            marker_faces=marker_faces,
            missing_faces=sorted(required_faces),
            unverified_faces=sorted(required_faces),
            issues=issues,
        )

    font_files = sorted(
        p for p in fonts_dir.rglob("*")
        if p.suffix.lower() in (".ttf", ".otf", ".ttc", ".otc")
    )

    for path in font_files:
        try:
            if path.suffix.lower() in (".ttc", ".otc"):
                from fontTools.ttLib import TTCollection
                coll = TTCollection(str(path))
                for idx in range(len(coll.fonts)):
                    asset = read_font_metadata(path, collection_index=idx)
                    if asset.typeface:
                        faces[asset.typeface] = asset
            else:
                asset = read_font_metadata(path)
                if asset.typeface:
                    faces[asset.typeface] = asset
                    if "Wingdings" in asset.typeface or "wingding" in asset.typeface.lower():
                        marker_faces[asset.typeface] = asset
        except Exception as exc:
            issues.append(Issue(
                rule_id="FONTS",
                code="FONT_READ_FAILED",
                severity=Severity.WARNING,
                details=f"{path.name}: {exc}",
            ))

    for face in sorted(required_faces):
        if face not in faces:
            missing.append(face)
            unverified.append(face)
        elif not faces[face].cmap_coverage.get("cyrillic", False):
            unverified.append(face)
            issues.append(Issue(
                rule_id="FONTS",
                code="FONT_CYRILLIC_MISSING",
                severity=Severity.WARNING,
                details=f"{face}: Cyrillic coverage missing",
            ))

    if not any("Wingdings" in f for f in marker_faces):
        issues.append(Issue(
            rule_id="FONTS",
            code="MARKER_FONT_MISSING",
            severity=Severity.WARNING,
            details="Wingdings marker font not found",
        ))

    return FontInventory(
        faces=faces,
        marker_faces=marker_faces,
        missing_faces=missing,
        unverified_faces=unverified,
        issues=issues,
    )

# ---------------------------------------------------------------------------
# Stage 5 §4: system-wide local font discovery
# ---------------------------------------------------------------------------

_FONT_SUFFIXES = (".ttf", ".otf", ".ttc", ".otc")

_DARWIN_SEARCH_ROOTS: tuple[tuple[str, str], ...] = (
    ("user_fonts", "~/Library/Fonts"),
    ("local_fonts", "/Library/Fonts"),
    ("system_fonts", "/System/Library/Fonts"),
)


def _macos_enabled_font_names() -> tuple[set[str], list[str]]:
    """Best-effort enabled-font set from the read-only system font inventory.

    Returns (enabled_basenames, errors). Absence of system_profiler never
    means absence of fonts: every file found on disk is still reported,
    only its enabled flag stays None.
    """
    import json
    import subprocess

    try:
        proc = subprocess.run(
            ["system_profiler", "SPFontsDataType", "-json"],
            capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return set(), [f"system_profiler unavailable: {exc}"]
    if proc.returncode != 0:
        return set(), [f"system_profiler exit={proc.returncode}"]
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return set(), [f"system_profiler JSON unparseable: {exc}"]
    enabled: set[str] = set()
    for entry in data.get("SPFontsDataType", []):
        if entry.get("enabled") == "yes" and entry.get("_name"):
            enabled.add(str(entry["_name"]))
    return enabled, []


def discover_local_fonts(
    config: Any = None,
    platform: str | None = None,
    extra_roots: tuple[str | Path, ...] = (),
    project_root: Path | None = None,
) -> FontDiscoveryReport:
    """Discover installed fonts before checking the single configured dir.

    Scans the configured ``fonts_dir`` plus the platform font roots
    (macOS: ``~/Library/Fonts``, ``/Library/Fonts``,
    ``/System/Library/Fonts``) and any caller-provided extra roots.
    Every file is identified by internal name table + SHA-256; filename,
    CSS family string or lookalike fonts are never sufficient. Byte-identical
    copies are recorded as duplicates of the first path (preferred order:
    configured dir, user, local, system, extra); same internal name with
    different bytes is an ambiguity the caller must resolve explicitly.
    """
    import hashlib
    import sys

    platform = platform or sys.platform
    roots: list[tuple[str, str]] = []
    if config is not None and project_root is not None:
        raw = getattr(getattr(config, "paths", None), "fonts_dir",
                      "assets/fonts")
        roots.append(("configured_fonts_dir", str(project_root / str(raw))))
    if platform == "darwin":
        roots.extend(
            (origin, str(Path(p).expanduser()))
            for origin, p in _DARWIN_SEARCH_ROOTS)
    for extra in extra_roots:
        roots.append(("extra_root", str(extra)))

    enabled_names: set[str] = set()
    errors: list[str] = []
    if platform == "darwin":
        enabled_names, profiler_errors = _macos_enabled_font_names()
        errors.extend(profiler_errors)

    discovered: list[DiscoveredFontFile] = []
    search_roots: list[str] = []
    seen_hash_path: dict[tuple[str, int | None], DiscoveredFontFile] = {}
    for origin, root in roots:
        root_path = Path(root)
        if not root_path.is_dir():
            if origin == "configured_fonts_dir":
                errors.append(f"configured fonts_dir not found: {root}")
            continue
        search_roots.append(root)
        try:
            files = sorted(
                p for p in root_path.rglob("*")
                if p.is_file() and p.suffix.lower() in _FONT_SUFFIXES)
        except OSError as exc:
            errors.append(f"cannot scan {root}: {exc}")
            continue
        for path in files:
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as exc:
                errors.append(f"cannot read {path}: {exc}")
                continue
            indices: list[int | None] = [None]
            if path.suffix.lower() in (".ttc", ".otc"):
                try:
                    from fontTools.ttLib import TTCollection
                    indices = list(range(len(TTCollection(str(path)).fonts)))
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"cannot read collection {path}: {exc}")
                    continue
            for idx in indices:
                try:
                    asset = read_font_metadata(
                        path, collection_index=idx or 0)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"FONT_READ_FAILED {path.name}: {exc}")
                    continue
                key = (digest, idx)
                first = seen_hash_path.get(key)
                entry = DiscoveredFontFile(
                    path=str(path),
                    origin=origin,  # type: ignore[arg-type]
                    collection_index=idx,
                    family=asset.family,
                    full_name=asset.typeface,
                    postscript_name=asset.postscript_name,
                    subfamily=asset.subfamily,
                    sha256=digest,
                    byte_count=path.stat().st_size,
                    enabled=(path.name in enabled_names
                             if enabled_names else None),
                    duplicate_of=first.path if first else "",
                )
                discovered.append(entry)
                if first is None:
                    seen_hash_path[key] = entry

    faces: dict[str, list[str]] = {}
    for entry in discovered:
        if entry.full_name and not entry.duplicate_of:
            faces.setdefault(entry.full_name, []).append(entry.path)

    return FontDiscoveryReport(
        platform=platform,
        search_roots=search_roots,
        discovered=discovered,
        faces_by_internal_name=faces,
        errors=errors,
    )


def build_font_inventory_from_discovery(
    discovery: FontDiscoveryReport,
    required_faces: set[str],
) -> FontInventory:
    """Build a FontInventory from discovery results by exact internal name.

    Byte-identical duplicates are already folded (first path wins, preferred
    root order). A required face claimed by two different byte streams is
    reported as ambiguous and left missing rather than silently picking the
    first file.
    """
    by_name: dict[str, list[DiscoveredFontFile]] = {}
    for entry in discovery.discovered:
        if entry.full_name and not entry.duplicate_of:
            by_name.setdefault(entry.full_name, []).append(entry)

    faces: dict[str, FontAsset] = {}
    marker_faces: dict[str, FontAsset] = {}
    missing: list[str] = []
    unverified: list[str] = []
    issues: list[Issue] = []

    for face in sorted(required_faces):
        claimants = by_name.get(face, [])
        hashes = {c.sha256 for c in claimants}
        if not claimants:
            missing.append(face)
            unverified.append(face)
        elif len(hashes) > 1:
            missing.append(face)
            unverified.append(face)
            issues.append(Issue(
                rule_id="FONTS",
                code="FONT_AMBIGUOUS_FACE",
                severity=Severity.ERROR,
                details=f"{face}: {len(claimants)} different byte streams "
                        f"claim this internal name; refusing to guess",
            ))
        else:
            asset = read_font_metadata(
                Path(claimants[0].path),
                collection_index=claimants[0].collection_index or 0)
            faces[face] = asset
            if not asset.cmap_coverage.get("cyrillic", False):
                unverified.append(face)
                issues.append(Issue(
                    rule_id="FONTS",
                    code="FONT_CYRILLIC_MISSING",
                    severity=Severity.WARNING,
                    details=f"{face}: Cyrillic coverage missing",
                ))

    for name, claimants in by_name.items():
        if "Wingdings" in name or "wingding" in name.lower():
            picked = claimants[0]
            marker_faces[name] = read_font_metadata(
                Path(picked.path),
                collection_index=picked.collection_index or 0)
    if not any("Wingdings" in f for f in marker_faces):
        issues.append(Issue(
            rule_id="FONTS",
            code="MARKER_FONT_MISSING",
            severity=Severity.WARNING,
            details="Wingdings marker font not found",
        ))

    return FontInventory(
        faces=faces,
        marker_faces=marker_faces,
        missing_faces=missing,
        unverified_faces=unverified,
        issues=issues,
    )


# ---------------------------------------------------------------------------
# Stage 5 §4: measurement font bundle
# ---------------------------------------------------------------------------

_TEST_ONLY_FONT_CANDIDATES: tuple[str, ...] = (
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
)


def _resolve_test_only_font() -> Path | None:
    """Real available test font with Cyrillic (engine checks only)."""
    for candidate in _TEST_ONLY_FONT_CANDIDATES:
        path = Path(candidate)
        if not path.is_file():
            continue
        try:
            asset = read_font_metadata(path)
        except Exception:  # noqa: BLE001
            continue
        if asset.cmap_coverage.get("cyrillic", False):
            return path
    return None


def prepare_measurement_fonts(
    inventory: FontInventory,
    required_text: str,
    *,
    profile_kind: str = "production",
    rules: Any = None,
) -> MeasurementFontBundle:
    """Bind exact font bytes for measurement (Stage 5 §4).

    Production: every inventory face maps to its verified bytes by internal
    name + hash; cmap is checked against the actual output text (Cyrillic,
    signs, indices). Missing faces/glyphs yield ``needs_assets`` issues —
    never a substitute passed off as the corporate face. Synthesis is
    prohibited downstream by distinct exact-face aliases + font-synthesis:none.
    Test-only: roles map onto one real available system font with Cyrillic,
    recorded as an explicit test mapping that never alters production ontology.
    """
    import hashlib
    import json

    from .models import MeasurementFontBinding, MeasurementFontBundle

    if profile_kind not in ("production", "test_only"):
        raise ValueError(f"unknown profile_kind: {profile_kind!r}")

    bindings: list[MeasurementFontBinding] = []
    missing_faces: list[str] = []
    missing_glyphs: list[str] = []
    issues: list[str] = []

    if profile_kind == "test_only":
        test_font = _resolve_test_only_font()
        if test_font is None:
            issues.append("TEST_ONLY_FONT_MISSING: no system font with "
                          "Cyrillic coverage available for engine checks")
            return MeasurementFontBundle(
                profile_kind="test_only", issues=issues)
        digest = hashlib.sha256(test_font.read_bytes()).hexdigest()
        asset = read_font_metadata(test_font)
        bindings.append(MeasurementFontBinding(
            role_or_face="* (test_only role mapping)",
            internal_name=asset.typeface or test_font.name,
            path=str(test_font),
            sha256=digest,
            face_index=None,
            browser_alias=f"MEASURE_FACE_{digest[:12]}",
            missing_glyphs=sorted(
                {c for c in set(required_text)
                 if not check_glyph_coverage(
                     asset, c, font_path=test_font).get(c, False)}),
        ))
        bundle_hash = hashlib.sha256(json.dumps(
            [b.model_dump(mode="json") for b in bindings],
            sort_keys=True).encode()).hexdigest()
        return MeasurementFontBundle(
            profile_kind="test_only", bindings=bindings,
            bundle_hash=bundle_hash, issues=issues)

    faces: dict[str, FontAsset] = dict(inventory.faces)
    for face in sorted(inventory.missing_faces):
        if face not in faces:
            missing_faces.append(face)
    for face in sorted(faces):
        asset = faces[face]
        ref = asset.asset
        if ref is None or not ref.sha256:
            missing_faces.append(face)
            issues.append(f"FONT_BYTES_UNRESOLVED: {face} has no hashed bytes")
            continue
        path = _locate_face_bytes(asset)
        if path is None:
            missing_faces.append(face)
            issues.append(f"FONT_BYTES_NOT_FOUND: {face} "
                          f"sha256={ref.sha256[:16]} not on disk")
            continue
        cov = check_glyph_coverage(asset, required_text, font_path=path)
        missing_here = sorted(c for c, ok in cov.items() if not ok)
        missing_glyphs.extend(f"U+{ord(c):04X}" for c in missing_here)
        bindings.append(MeasurementFontBinding(
            role_or_face=face,
            internal_name=asset.typeface,
            path=str(path),
            sha256=ref.sha256,
            face_index=asset.collection_index,
            browser_alias=f"MEASURE_FACE_{ref.sha256[:12]}",
            missing_glyphs=sorted(set(missing_here)),
        ))

    for name, marker in inventory.marker_faces.items():
        ref = marker.asset
        if ref is None or not ref.sha256:
            continue
        path = _locate_face_bytes(marker)
        if path is None:
            issues.append(f"MARKER_BYTES_NOT_FOUND: {name}")
            continue
        bindings.append(MeasurementFontBinding(
            role_or_face=f"marker:{name}",
            internal_name=marker.typeface or name,
            path=str(path),
            sha256=ref.sha256,
            face_index=marker.collection_index,
            browser_alias=f"MEASURE_FACE_{ref.sha256[:12]}",
            missing_glyphs=[],
        ))

    if missing_faces:
        issues.append("NEEDS_ASSETS: production measurement not confirmed; "
                      f"missing faces: {sorted(set(missing_faces))}")
    if missing_glyphs:
        issues.append("MISSING_GLYPHS: required characters not covered: "
                      f"{sorted(set(missing_glyphs))[:12]}")

    bundle_hash = hashlib.sha256(json.dumps(
        [b.model_dump(mode="json") for b in bindings],
        sort_keys=True).encode()).hexdigest()
    return MeasurementFontBundle(
        profile_kind="production",
        bindings=bindings,
        bundle_hash=bundle_hash,
        missing_faces=sorted(set(missing_faces)),
        missing_glyphs=sorted(set(missing_glyphs)),
        issues=issues,
    )


def _locate_face_bytes(asset: FontAsset) -> Path | None:
    """Locate verified font bytes on disk by hash (Stage 5 §4).

    The asset record keeps only a basename; the file is re-found under the
    known font roots and accepted only when its SHA-256 matches the record.
    """
    import hashlib

    ref = asset.asset
    if ref is None or not ref.sha256:
        return None
    candidates: list[Path] = []
    for root in ("~/Library/Fonts", "/Library/Fonts",
                 "/System/Library/Fonts"):
        base = Path(root).expanduser()
        if base.is_dir():
            candidates.extend(base.rglob(ref.relative_path))
    for path in candidates:
        try:
            if hashlib.sha256(path.read_bytes()).hexdigest() == ref.sha256:
                return path
        except OSError:
            continue
    return None
