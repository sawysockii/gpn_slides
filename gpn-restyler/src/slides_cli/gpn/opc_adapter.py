"""Isolated OPC/python-pptx adapter (Stage 4 §5, §11.1).

All raw ``python-pptx`` private operations live here — never scattered across
business emitters. The adapter:

- opens the verified template and creates output slides from verified
  layout identities;
- exposes typed helpers (add textbox/table/picture/chart frame, set
  geometry without touching nested bodies, set noAutofit);
- performs final package assembly: python-pptx save followed by byte-level
  surgery that adds transplanted parts, content types and owner-local
  relationships, then validates the final ZIP.

Units: EMU = round(inches * 914400), converted exactly once at the edge.
"""

from __future__ import annotations

import hashlib
import logging
import zipfile
from pathlib import Path
from typing import Any

from lxml import etree
from pptx import Presentation
from pptx.enum.chart import XL_CHART_TYPE
from pptx.util import Emu

log = logging.getLogger(__name__)

EMU_PER_INCH = 914400

CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NAME = "[Content_Types].xml"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

_XML_PARSER = etree.XMLParser(
    resolve_entities=False, no_network=True, recover=False, huge_tree=False
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def emu(inches: float) -> int:
    return int(round(float(inches) * EMU_PER_INCH))


# ---------------------------------------------------------------------------
# Presentation handling
# ---------------------------------------------------------------------------

def open_template(template_path: str | Path) -> Any:
    """Open the verified active template (read-only check first)."""
    path = Path(template_path)
    if not path.is_file():
        raise FileNotFoundError(f"template not found: {path}")
    return Presentation(str(path))


def blank_slide_layout(prs: Any, layout_index: int | None = None) -> Any:
    """Return a truly blank layout (verified, never hardcoded Contacts).

    Recovery fix (2026-10-11): the previous hardcoded ``layout_index=6``
    selected ``slideLayout7`` ("Контакты") in the derived template, putting
    contact-specific furniture on every proposal slide. Now the layout is
    chosen by evidence: fewest placeholders, preferring names containing
    blank/empty/pustoj. An explicit index is honored only when it points at
    such a blank layout; otherwise the evidence-based choice wins.
    """
    layouts = list(prs.slide_layouts)
    if not layouts:
        raise ValueError("template has no slide layouts")
    if layout_index is not None and 0 <= layout_index < len(layouts):
        cand = layouts[layout_index]
        try:
            ph = list(cand.placeholders)
            nm = (cand.name or "").lower()
            if len(ph) == 0 or "blank" in nm or "пуст" in nm or "empty" in nm:
                return cand
        except Exception:  # noqa: BLE001 - fall through to evidence search
            pass
    def _score(lay: Any) -> tuple[int, int, str]:
        try:
            n_ph = len(list(lay.placeholders))
        except Exception:  # noqa: BLE001
            n_ph = 99
        nm = str(getattr(lay, "name", "") or "").lower()
        blank_bonus = 0 if ("blank" in nm or "пуст" in nm or "empty" in nm) else 1
        contact_penalty = 5 if ("контакт" in nm or "contact" in nm) else 0
        return (blank_bonus, n_ph + contact_penalty, nm)
    return sorted(layouts, key=_score)[0]


def strip_shape_effect_inheritance(shape: Any) -> None:
    """Normalize a fresh shape to normative no-effect (recovery §5).

    python-pptx inserts ``<p:style>`` with ``a:effectRef idx=1/2`` pointing
    at theme ``effectStyleLst`` entries that contain ``a:outerShdw``. A shape
    without a direct ``a:effectLst`` is therefore NOT effect-free. This
    helper removes the inherited style refs and writes an explicit empty
    ``a:effectLst`` on the shape properties so ``EffectiveEffects`` resolves
    to none. Safe to call on textboxes/autoshapes/connectors/pictures.
    """
    el = getattr(shape, "_element", None)
    if el is None:
        return
    # p:sp / p:cxnSp / p:pic each carry p:spPr or p:cxnSpPr/p:nvPr structure
    # holding a:effectLst or p:style/effectRef. Strip both levels.
    for sp_pr in list(el.iter()):
        tag = etree.QName(sp_pr).localname if isinstance(sp_pr.tag, str) else ""
        if tag == "style":
            # Remove effectRef/lnRef/fillRef/fmtRef children; drop the whole
            # p:style node so no theme effect applies.
            parent = sp_pr.getparent()
            if parent is not None:
                parent.remove(sp_pr)
        elif tag == "effectLst":
            parent = sp_pr.getparent()
            if parent is not None:
                parent.remove(sp_pr)
    # Write explicit empty effectLst on every spPr/cxnSpPr so inheritance
    # cannot reintroduce a shadow downstream.
    a_ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    p_ns = "http://schemas.openxmlformats.org/presentationml/2006/main"
    for pr_tag in (f"{{{a_ns}}}spPr", f"{{{p_ns}}}cxnSpPr"):
        for pr in el.findall(f".//{pr_tag}"):
            if pr.find(f"{{{a_ns}}}effectLst") is None:
                etree.SubElement(pr, f"{{{a_ns}}}effectLst")


def add_slide(prs: Any, layout: Any = None) -> Any:
    """Append one slide and return it."""
    layout = layout if layout is not None else blank_slide_layout(prs)
    return prs.slides.add_slide(layout)


def strip_placeholders(slide: Any) -> int:
    """Remove filler placeholder shapes from a new slide (§4).

    Layout placeholders (``p:ph``) are never output content: filling a
    matched placeholder or creating a native shape in the allowed area is
    the exporter's job, leaving duplicate titles/filler boxes behind is
    not. Returns the removed count.
    """
    removed = 0
    tree = slide.shapes._spTree
    for el in list(tree):
        tag = etree.QName(el).localname if isinstance(el.tag, str) else ""
        if tag not in ("sp", "cxnSp", "graphicFrame", "pic"):
            continue
        if el.find(".//{http://schemas.openxmlformats.org/presentationml/2006/main}ph") \
                is not None:
            tree.remove(el)
            removed += 1
    return removed


def next_shape_id(slide: Any) -> int:
    """Max used shape id + 1 on one slide (keeps ids unique)."""
    best = 0
    for shape in slide.shapes:
        try:
            best = max(best, int(shape.shape_id))
        except (TypeError, ValueError):
            continue
    return best + 1


def add_textbox(slide: Any, left_emu: int, top_emu: int,
                width_emu: int, height_emu: int) -> Any:
    """Add a native textbox at exact EMU geometry."""
    return slide.shapes.add_textbox(Emu(left_emu), Emu(top_emu),
                                    Emu(width_emu), Emu(height_emu))


def set_no_autofit(shape: Any) -> None:
    """Set exactly one ``a:noAutofit`` on the shape's own bodyPr."""
    body_pr = shape.text_frame._txBody.find(
        "{http://schemas.openxmlformats.org/drawingml/2006/main}bodyPr")
    if body_pr is None:
        return
    ns_a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    for tag in ("a:noAutofit", "a:normAutofit", "a:spAutoFit"):
        for node in body_pr.findall(f"{{{ns_a}}}{tag[2:]}"):
            body_pr.remove(node)
    node = etree.SubElement(body_pr, f"{{{ns_a}}}noAutofit")


def add_table(slide: Any, rows: int, cols: int, left_emu: int, top_emu: int,
              width_emu: int, height_emu: int) -> Any:
    """Add a native table graphic frame."""
    return slide.shapes.add_table(rows, cols, Emu(left_emu), Emu(top_emu),
                                  Emu(width_emu), Emu(height_emu))


DECOR_PRESET_MAP = {
    "rect": "RECTANGLE",
    "roundRect": "ROUNDED_RECTANGLE",
    "ellipse": "OVAL",
}


def add_autoshape(slide: Any, preset_name: str, left_emu: int, top_emu: int,
                  width_emu: int, height_emu: int) -> Any:
    """Add a native autoshape with an explicit preset geometry token."""
    from pptx.enum.shapes import MSO_AUTO_SHAPE_TYPE

    preset = getattr(MSO_AUTO_SHAPE_TYPE, preset_name, None)
    if preset is None:
        raise ValueError(f"unknown autoshape preset: {preset_name!r}")
    return slide.shapes.add_shape(
        preset, Emu(left_emu), Emu(top_emu),
        Emu(width_emu), Emu(height_emu))


def add_straight_connector(slide: Any, left_emu: int, top_emu: int,
                           width_emu: int, height_emu: int) -> Any:
    """Add a native straight connector (source ``line`` geometry).

    python-pptx takes begin/end coordinates (not a box): the end point
    is begin + extent, so horizontal/vertical zero extents stay valid.
    """
    from pptx.enum.shapes import MSO_CONNECTOR_TYPE

    return slide.shapes.add_connector(
        MSO_CONNECTOR_TYPE.STRAIGHT, Emu(left_emu), Emu(top_emu),
        Emu(left_emu + width_emu), Emu(top_emu + height_emu))


def add_picture(slide: Any, image_path: str | Path, left_emu: int, top_emu: int,
                width_emu: int | None = None,
                height_emu: int | None = None) -> Any:
    """Add a native picture (bytes come from the source payload)."""
    kwargs: dict[str, Any] = {}
    if width_emu is not None:
        kwargs["width"] = Emu(width_emu)
    if height_emu is not None:
        kwargs["height"] = Emu(height_emu)
    return slide.shapes.add_picture(str(image_path), Emu(left_emu),
                                    Emu(top_emu), **kwargs)


CHART_TYPE_MAP = {
    "column": XL_CHART_TYPE.COLUMN_CLUSTERED,
    "bar": XL_CHART_TYPE.BAR_CLUSTERED,
    "line": XL_CHART_TYPE.LINE,
    "pie": XL_CHART_TYPE.PIE,
    "doughnut": XL_CHART_TYPE.DOUGHNUT,
    "area": XL_CHART_TYPE.AREA,
    "scatter": XL_CHART_TYPE.XY_SCATTER,
    "bubble": XL_CHART_TYPE.BUBBLE,
}


def add_chart_from_verified_data(slide: Any, chart_type: str, chart_data: Any,
                                 left_emu: int, top_emu: int,
                                 width_emu: int, height_emu: int) -> Any:
    """Create a native chart from ``VerifiedChartData``-built chart data."""
    key = str(chart_type).lower()
    if key not in CHART_TYPE_MAP:
        raise ValueError(f"unsupported chart family for native build: {chart_type!r}")
    return slide.shapes.add_chart(
        CHART_TYPE_MAP[key], Emu(left_emu), Emu(top_emu),
        Emu(width_emu), Emu(height_emu), chart_data,
    )


def save_presentation(prs: Any, path: str | Path) -> None:
    prs.save(str(path))


# ---------------------------------------------------------------------------
# Final package assembly (§11.1 step 3): add transplanted parts after save.
# ---------------------------------------------------------------------------

def assemble_final_package(
    saved_pptx: str | Path,
    extra_parts: dict[str, bytes],
    extra_content_types: dict[str, str],
    extra_rels: dict[str, list[dict[str, str]]],
    output_path: str | Path,
) -> dict[str, Any]:
    """Merge transplanted parts into a saved deck and re-zip atomically.

    ``extra_rels`` maps owner part ("" = package root) to relationship dicts
    with keys ``id/type/target/mode``. Returns a report with the final hash.
    """
    saved_pptx = Path(saved_pptx)
    output_path = Path(output_path)
    members: dict[str, bytes] = {}
    with zipfile.ZipFile(saved_pptx) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            members[info.filename] = zf.read(info)

    collisions = [name for name in extra_parts if name in members]
    if collisions:
        raise ValueError(f"part name collision during assembly: {collisions[:5]}")
    members.update(extra_parts)

    # Content types: add Override entries for new parts.
    ct_root = etree.fromstring(members[CT_NAME], parser=_XML_PARSER)
    existing = {n.get("PartName")
                for n in ct_root.findall(f"{{{CT_NS}}}Override")}
    for part, ctype in sorted(extra_content_types.items()):
        if "/" + part in existing or part in existing:
            continue
        node = etree.SubElement(ct_root, f"{{{CT_NS}}}Override")
        node.set("PartName", "/" + part)
        node.set("ContentType", ctype or "application/octet-stream")
    members[CT_NAME] = etree.tostring(ct_root, xml_declaration=True,
                                      encoding="UTF-8", standalone=True)

    # Relationships: append per-owner rIds (owner-scoped, unique).
    for owner, rels in extra_rels.items():
        rels_name = _rels_name(owner)
        if rels_name in members:
            rels_root = etree.fromstring(members[rels_name], parser=_XML_PARSER)
            seen = {n.get("Id") for n in rels_root.findall(f"{{{REL_NS}}}Relationship")}
        else:
            rel_ns = REL_NS.split("/package")[0] + "/package/2006/relationships"
            rels_root = etree.Element(f"{{{REL_NS}}}Relationships",
                                      nsmap={None: rel_ns})
            seen = set()
        for rel in rels:
            rid = rel["id"]
            counter = 1
            while rid in seen:
                counter += 1
                rid = f"{rel['id']}-{counter}"
            seen.add(rid)
            node = etree.SubElement(rels_root, f"{{{REL_NS}}}Relationship")
            node.set("Id", rid)
            node.set("Type", rel["type"])
            node.set("Target", rel["target"])
            if rel.get("mode", "").lower() == "external":
                node.set("TargetMode", "External")
        members[rels_name] = etree.tostring(rels_root, xml_declaration=True,
                                            encoding="UTF-8", standalone=True)

    tmp = output_path.with_suffix(output_path.suffix + ".tmp-assemble")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(members):
            # Reproducible packaging: fixed entry timestamps, so identical
            # slide content yields identical bytes across rebuilds (reviews
            # transfer by hash). NOTE: docProps/core.xml dcterms still
            # carry python-pptx save time inside the bytes; full byte
            # reproducibility is not claimed until those are normalized.
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, members[name])
    tmp.replace(output_path)
    digest = sha256_bytes(output_path.read_bytes())
    return {"path": str(output_path), "sha256": digest,
            "added_parts": sorted(extra_parts),
            "collisions": collisions}


def clean_template_copy(
    template_path: str | Path, work_dir: str | Path
) -> tuple[Path, dict[str, Any]]:
    """Copy a template minus its content slides (spec §4.3).

    Masters/layouts/themes/notes-masters and protected assets are kept;
    ``ppt/slides/slide*.xml`` parts, their rels, presentation slide rels,
    ``sldId`` entries and content-type overrides are dropped. Returns the
    clean copy path and a derivation manifest.
    """
    template_path = Path(template_path)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    members = read_zip_members(template_path)
    dropped = sorted(
        n for n in members
        if (n.startswith("ppt/slides/slide") and "/_rels/" not in n
            and "slideLayout" not in n and "slideMaster" not in n)
        or (n.startswith("ppt/slides/_rels/slide") and n.endswith(".rels")))
    for name in dropped:
        members.pop(name, None)

    # presentation.xml: clear sldIdLst, keep masters/layouts refs.
    pres_xml = members.get("ppt/presentation.xml")
    if pres_xml is not None:
        root = etree.fromstring(pres_xml, parser=_XML_PARSER)
        lst = root.find(f"{{{P_NS}}}sldIdLst")
        if lst is not None:
            for child in list(lst):
                lst.remove(child)
        members["ppt/presentation.xml"] = etree.tostring(
            root, xml_declaration=True, encoding="UTF-8", standalone=True)

    # presentation rels: drop slide relationships (keep master/layout/theme).
    pres_rels = "ppt/_rels/presentation.xml.rels"
    if pres_rels in members:
        root = etree.fromstring(members[pres_rels], parser=_XML_PARSER)
        for node in list(root.findall(f"{{{REL_NS}}}Relationship")):
            if (node.get("Type") or "").endswith("/slide"):
                root.remove(node)
        members[pres_rels] = etree.tostring(
            root, xml_declaration=True, encoding="UTF-8", standalone=True)

    # Content types: drop overrides for removed parts.
    ct_root = etree.fromstring(members[CT_NAME], parser=_XML_PARSER)
    dropped_set = set(dropped)
    for node in list(ct_root.findall(f"{{{CT_NS}}}Override")):
        if (node.get("PartName") or "").lstrip("/") in dropped_set:
            ct_root.remove(node)
    members[CT_NAME] = etree.tostring(
        ct_root, xml_declaration=True, encoding="UTF-8", standalone=True)

    out = work_dir / "clean_template.pptx"
    tmp = out.with_suffix(".pptx.tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, members[name])
    tmp.replace(out)
    # The clean copy must reopen with zero content slides.
    check = Presentation(str(out))
    assert len(check.slides) == 0, "clean template still has slides"
    manifest = {
        "source_template": str(template_path),
        "clean_copy": str(out),
        "dropped_parts": dropped,
        "kept_slide_count": 0,
        "sha256": sha256_bytes(out.read_bytes()),
    }
    return out, manifest


def _rels_name(owner: str) -> str:
    if owner == "":
        return "_rels/.rels"
    directory, _, base = owner.rpartition("/")
    return f"{directory}/_rels/{base}.rels" if directory else f"_rels/{base}.rels"


def read_zip_members(path: str | Path) -> dict[str, bytes]:
    """Read all ZIP members of a final package (for validators)."""
    members: dict[str, bytes] = {}
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if not info.is_dir():
                members[info.filename] = zf.read(info)
    return members


def validate_package_graph(path: str | Path) -> dict[str, Any]:
    """Validate the final ZIP/package graph (spec §5.6)."""
    from .export_models import PackageGraphReport

    problems: dict[str, Any] = {
        "readable": False, "duplicate_members": [], "dangling_targets": [],
        "content_type_problems": [], "duplicate_rids": [],
    }
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            names = [i.filename for i in infos if not i.is_dir()]
            bad = zf.testzip()
            if bad is not None:
                problems["content_type_problems"].append(f"crc_failed:{bad}")
                return {**problems, "ok": False}
    except zipfile.BadZipFile:
        return {**problems, "ok": False}
    seen: set[str] = set()
    for name in names:
        if name in seen:
            problems["duplicate_members"].append(name)
        seen.add(name)
    members = read_zip_members(path)
    ct_data = members.get("[Content_Types].xml", b"")
    try:
        ct_root = etree.fromstring(ct_data, parser=_XML_PARSER)
        overrides = {n.get("PartName", "").lstrip("/")
                      for n in ct_root.findall(f"{{{CT_NS}}}Override")}
    except etree.XMLSyntaxError:
        problems["content_type_problems"].append("content_types_unparseable")
        return {**problems, "readable": True, "ok": False}
    import posixpath as _pp
    for name in names:
        if name in ("[Content_Types].xml",) or name.endswith(".rels"):
            continue
        if name not in overrides and "." not in _pp.basename(name):
            problems["content_type_problems"].append(f"no_content_type:{name}")
    # Relationship targets.
    for name, data in members.items():
        if not name.endswith(".rels"):
            continue
        directory, _, base = name.rpartition("/")
        if directory.endswith("_rels"):
            owner_dir = directory[: -len("_rels")].rstrip("/")
            owner = f"{owner_dir}/{base[:-len('.rels')]}" if owner_dir else base[:-len(".rels")]
        elif directory == "_rels":
            owner = ""
        else:
            continue
        try:
            root = etree.fromstring(data, parser=_XML_PARSER)
        except etree.XMLSyntaxError:
            problems["content_type_problems"].append(f"rels_unparseable:{name}")
            continue
        ids: set[str] = set()
        for node in root.findall(f"{{{REL_NS}}}Relationship"):
            rid = node.get("Id") or ""
            if rid in ids:
                problems["duplicate_rids"].append(f"{owner}#{rid}")
            ids.add(rid)
            if (node.get("TargetMode") or "Internal").lower() != "external":
                target = node.get("Target") or ""
                resolved = _pp.normpath(_pp.join(_pp.dirname(owner), target)).lstrip("/")
                if resolved not in members and resolved not in seen:
                    problems["dangling_targets"].append(f"{owner}#{rid}->{target}")
    problems["readable"] = True
    problems["ok"] = not (problems["duplicate_members"] or problems["dangling_targets"]
                          or problems["content_type_problems"]
                          or problems["duplicate_rids"])
    report = PackageGraphReport(**problems)
    return report.model_dump(mode="json")
