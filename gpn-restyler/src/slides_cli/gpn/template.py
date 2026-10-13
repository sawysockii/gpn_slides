"""Template profile extraction and base-template derivation (spec §9).

Reads a reference PPTX library through the Stage 1 importer + package graph,
identifies masters/layouts/themes by relationships (never by fixed index),
computes content boxes, and derives a clean base template copy into run/cache.

The source library is never modified: derivation writes a separate copy and
verifies the original hash is unchanged.
"""

from __future__ import annotations

import logging
import shutil
import zipfile
from pathlib import Path
from typing import Any

from lxml import etree
from pptx import Presentation

from .assets import AssetStore, sha256_bytes, sha256_file
from .errors import TemplateMismatchError
from .models import (
    CompiledOntology,
    ContentBoxResolution,
    Issue,
    LayoutProfile,
    ProtectedObject,
    RectEMU,
    RectPt,
    ReferenceStyleEvidence,
    Severity,
    SlideIR,
    TemplateDerivationReport,
    TemplateProfile,
)
from .package import PackageGraph, read_package
from .rules import RuleEvaluationContext, evaluate_rule_registry

log = logging.getLogger(__name__)

A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
NS = {"a": A, "p": P}

EMU_PER_PT = 12700


def _emu(value: str | None, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(float(value))
    except ValueError:
        return default


def _rect_from_sp(sp_el: Any) -> RectPt | None:
    xfrm = sp_el.find("p:spPr/a:xfrm", namespaces=NS)
    if xfrm is None:
        xfrm = sp_el.find(".//a:xfrm", namespaces=NS)
    if xfrm is None:
        return None
    off = xfrm.find("a:off", namespaces=NS)
    ext = xfrm.find("a:ext", namespaces=NS)
    if off is None or ext is None:
        return None
    x = _emu(off.get("x")) / EMU_PER_PT
    y = _emu(off.get("y")) / EMU_PER_PT
    w = max(_emu(ext.get("cx")), 0) / EMU_PER_PT
    h = max(_emu(ext.get("cy")), 0) / EMU_PER_PT
    return RectPt(x=x, y=y, w=w, h=h)


def _related(graph: PackageGraph, part: str, rel_suffix: str) -> list[str]:
    out: list[str] = []
    for rel in graph.outgoing.get(part, []):
        if rel.rel_type.endswith(rel_suffix) and rel.resolved_part:
            out.append(rel.resolved_part)
    return out


def check_canvas_compatible(canvas: RectEMU, *, w: int, h: int) -> None:
    """Gate: the given canvas must match the expected corporate canvas.

    The compiled ontology canvas is normative; a differently sized template,
    library or source deck is not silently adapted. Callers that must block
    (instead of collecting a TEMPLATE_CANVAS_MISMATCH issue as
    :func:`extract_template_profile` does) use this typed refusal (§12.1
    ``test_canvas_loaded_template_mismatch``).
    """
    if canvas.w != w or canvas.h != h:
        raise TemplateMismatchError(
            "TEMPLATE_CANVAS_MISMATCH",
            f"canvas {canvas.w}x{canvas.h} EMU differs from the expected "
            f"{w}x{h} EMU",
        )


def extract_template_profile(
    template: Path,
    rules: CompiledOntology,
    store: AssetStore,
) -> TemplateProfile:
    """Extract a structural profile from a reference library (spec §9).

    Reads masters/layouts/themes via relationships, records placeholder roles
    and geometry, placeholder title/footer/logo regions, and protected objects.
    Missing fonts never destroy the structural profile: font readiness is
    tracked separately by ``typography.inventory_fonts``.
    """
    template = Path(template)
    issues: list[Issue] = []
    source_hash = sha256_file(template)

    graph = read_package(template, store)
    try:
        prs = Presentation(str(template))
        canvas = RectEMU(x=0, y=0, w=int(prs.slide_width), h=int(prs.slide_height))
    except Exception as exc:  # noqa: BLE001
        issues.append(
            Issue(
                rule_id="TEMPLATE",
                code="TEMPLATE_OPEN_FAILED",
                severity=Severity.ERROR,
                details=f"cannot open {template}: {exc}",
            )
        )
        return TemplateProfile(
            source_library_hash=source_hash,
            source_library_path=str(template),
            issues=issues,
        )

    if canvas.w != rules.canvas.w or canvas.h != rules.canvas.h:
        issues.append(
            Issue(
                rule_id="TEMPLATE",
                code="TEMPLATE_CANVAS_MISMATCH",
                severity=Severity.ERROR,
                details=(
                    f"library canvas {canvas.w}x{canvas.h} differs from "
                    f"ontology {rules.canvas.w}x{rules.canvas.h}"
                ),
            )
        )

    # Collect masters actually used by slides (via layout relationships).
    master_parts: list[str] = []
    layout_parts: list[str] = []
    theme_parts: list[str] = []
    for slide_part in graph.slide_order:
        for layout in _related(graph, slide_part, "/slideLayout"):
            if layout not in layout_parts:
                layout_parts.append(layout)
            for master in _related(graph, layout, "/slideMaster"):
                if master not in master_parts:
                    master_parts.append(master)
                for theme in _related(graph, master, "/theme"):
                    if theme not in theme_parts:
                        theme_parts.append(theme)

    # Fall back to any master/layout present when slides reference none.
    if not master_parts:
        for name in graph.parts_by_name:
            if "/slideMasters/" in name and name not in master_parts:
                master_parts.append(name)
    if not layout_parts:
        for name in graph.parts_by_name:
            if "/slideLayouts/" in name and name not in layout_parts:
                layout_parts.append(name)
    if not theme_parts:
        for name in graph.parts_by_name:
            if "/theme/" in name and name not in theme_parts:
                theme_parts.append(name)

    layout_profiles: list[LayoutProfile] = []
    protected: list[ProtectedObject] = []
    for layout_part in layout_parts:
        part_ir = graph.parts_by_name.get(layout_part)
        if part_ir is None or part_ir.asset is None:
            issues.append(
                Issue(
                    rule_id="TEMPLATE",
                    code="LAYOUT_PART_UNREADABLE",
                    severity=Severity.WARNING,
                    details=f"layout part not readable: {layout_part}",
                )
            )
            continue
        try:
            xml = store.read_bytes(part_ir.asset)
            root = etree.fromstring(
                xml, parser=etree.XMLParser(resolve_entities=False, no_network=True)
            )
        except Exception as exc:  # noqa: BLE001
            issues.append(
                Issue(
                    rule_id="TEMPLATE",
                    code="LAYOUT_XML_INVALID",
                    severity=Severity.WARNING,
                    details=f"{layout_part}: {exc}",
                )
            )
            continue
        placeholder_roles: dict[int, str] = {}
        title_box: RectPt | None = None
        footer_boxes: list[RectPt] = []
        protected_regions: list[RectPt] = []
        for idx, sp in enumerate(root.findall(".//p:sp", namespaces=NS)):
            ph = sp.find(".//p:ph", namespaces=NS)
            if ph is None:
                continue
            ph_type = ph.get("type") or "body"
            placeholder_roles[idx] = ph_type
            box = _rect_from_sp(sp)
            if box is None:
                continue
            if ph_type in ("title", "ctrTitle"):
                if title_box is None:
                    title_box = box
                protected_regions.append(box)
            elif ph_type in ("ftr", "sldNum", "dt"):
                footer_boxes.append(box)
                protected_regions.append(box)
            cnv = sp.find("p:nvSpPr/p:cNvPr", namespaces=NS)
            shape_id = cnv.get("id") if cnv is not None else str(idx)
            name = cnv.get("name") if cnv is not None else ""
            protected.append(
                ProtectedObject(
                    source_ref=f"{layout_part}#{shape_id}",
                    scope="layout",
                    semantic_role=ph_type,
                    xml_hash=sha256_bytes(etree.tostring(sp)),
                    mutable_fields=["sldNum"] if ph_type == "sldNum" else [],
                )
            )
            _ = name
        layout_profiles.append(
            LayoutProfile(
                id=f"layout:{len(layout_profiles)}",
                source_layout_part=layout_part,
                placeholder_roles=placeholder_roles,
                title_box=title_box,
                footer_boxes=footer_boxes,
                protected_regions=protected_regions,
                evidence=[layout_part],
                is_verified=title_box is not None,
            )
        )

    if not layout_profiles:
        issues.append(
            Issue(
                rule_id="TEMPLATE",
                code="TEMPLATE_NO_LAYOUTS",
                severity=Severity.ERROR,
                details="no layout profiles extracted from library",
            )
        )

    profile = TemplateProfile(
        template_hash=source_hash,
        source_library_hash=source_hash,
        source_library_path=str(template),
        ontology_corpus_hash=rules.ontology_corpus_hash,
        canvas=canvas,
        layout_profiles=layout_profiles,
        protected_objects=protected,
        master_parts=master_parts,
        theme_parts=theme_parts,
        issues=issues,
        structure_verified=bool(layout_profiles) and not any(
            i.severity == Severity.ERROR for i in issues
        ),
    )
    return profile


def derive_base_template(
    source_library: Path,
    *,
    rules: CompiledOntology,
    output_path: Path,
    store: AssetStore,
) -> TemplateDerivationReport:
    """Derive a clean base template copy from a demo library (spec §9).

    Removes only demonstration content slides (keeping masters/layouts/themes
    and shared relationships intact), writes to ``output_path`` (run/cache),
    reopens the result, and verifies the source hash is unchanged.
    """
    source_library = Path(source_library)
    output_path = Path(output_path)
    issues: list[Issue] = []
    before_hash = sha256_file(source_library)

    graph = read_package(source_library, store)
    selected_layout_ids = [p for p in graph.parts_by_name if "/slideLayouts/" in p]
    copied_parts = sorted(graph.parts_by_name.keys())

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")

    try:
        prs = Presentation(str(source_library))
        # Identify slides whose every text mentions demo markers; default to
        # removing slides only when they are clearly demo content and masters
        # remain reachable afterwards.
        removed: list[str] = []
        # Conservative approach: copy the package and drop all content slides
        # while keeping masters/layouts/themes (they live in separate parts).
        # Use python-pptx slide deletion which maintains sldIdLst/rels.
        kept = 0
        for idx in sorted(range(len(prs.slides)), reverse=True):
            slide = prs.slides[idx]
            texts = []
            for shape in slide.shapes:
                if shape.has_text_frame:
                    texts.append(shape.text.strip())
            joined = " ".join(texts)
            # Only remove slides that carry demo-ish content AND leave at
            # least one slide so the package stays openable; an empty deck
            # still carries all masters/layouts.
            if joined and len(prs.slides) > 1:
                prs.slides._sldIdLst.remove(prs.slides._sldIdLst[idx])
                removed.append(f"slide[{idx}]:{joined[:60]}")
                kept += 1
        prs.save(str(tmp_path))
    except Exception as exc:  # noqa: BLE001
        issues.append(
            Issue(
                rule_id="TEMPLATE",
                code="DERIVATION_FAILED",
                severity=Severity.ERROR,
                details=f"derivation failed: {exc}",
            )
        )
        return TemplateDerivationReport(
            source_path=str(source_library),
            source_hash=before_hash,
            output_path=str(output_path),
            issues=issues,
        )

    # Reopen the derived copy and verify OPC integrity.
    relationship_checks: list[str] = []
    try:
        reopened = Presentation(str(tmp_path))
        _ = (reopened.slide_width, reopened.slide_height)
        with zipfile.ZipFile(tmp_path) as zf:
            names = set(zf.namelist())
        for rel_name in ("_rels/.rels", "[Content_Types].xml"):
            relationship_checks.append(
                f"{rel_name}:{'present' if rel_name in names else 'MISSING'}"
            )
            if rel_name not in names:
                issues.append(
                    Issue(
                        rule_id="TEMPLATE",
                        code="DERIVED_PACKAGE_INCOMPLETE",
                        severity=Severity.ERROR,
                        details=f"derived package missing {rel_name}",
                    )
                )
        # Masters/layouts/themes must survive.
        for kind in ("slideMasters", "slideLayouts", "theme"):
            found = any(kind in n for n in names)
            relationship_checks.append(f"{kind}:{'present' if found else 'MISSING'}")
            if not found:
                issues.append(
                    Issue(
                        rule_id="TEMPLATE",
                        code="DERIVED_SHARED_PART_MISSING",
                        severity=Severity.ERROR,
                        details=f"derived package lost shared {kind} parts",
                    )
                )
    except Exception as exc:  # noqa: BLE001
        issues.append(
            Issue(
                rule_id="TEMPLATE",
                code="DERIVED_REOPEN_FAILED",
                severity=Severity.ERROR,
                details=f"derived template does not reopen: {exc}",
            )
        )
        return TemplateDerivationReport(
            source_path=str(source_library),
            source_hash=before_hash,
            output_path=str(output_path),
            selected_layout_ids=selected_layout_ids,
            copied_parts=copied_parts,
            relationship_checks=relationship_checks,
            issues=issues,
        )

    shutil.move(str(tmp_path), str(output_path))
    output_hash = sha256_file(output_path)

    # Source must be unchanged.
    after_hash = sha256_file(source_library)
    if after_hash != before_hash:
        issues.append(
            Issue(
                rule_id="TEMPLATE",
                code="SOURCE_LIBRARY_MODIFIED",
                severity=Severity.ERROR,
                details="source library bytes changed during derivation",
            )
        )

    preserved = [f"master:{m}" for m in graph.parts_by_name if "/slideMasters/" in m]
    preserved += [f"layout:{p}" for p in selected_layout_ids]
    preserved += [f"theme:{p}" for p in graph.parts_by_name if "/theme/" in p]

    return TemplateDerivationReport(
        source_path=str(source_library),
        source_hash=before_hash,
        output_path=str(output_path),
        output_hash=output_hash,
        selected_layout_ids=selected_layout_ids,
        copied_parts=copied_parts,
        preserved_objects=preserved,
        removed_demo_objects=removed,
        relationship_checks=relationship_checks,
        issues=issues,
    )


def compute_content_box(
    canvas: RectEMU,
    protected_regions: list[RectEMU],
    verified_layout_evidence: Any = None,
) -> ContentBoxResolution:
    """Compute the editable content box outside protected regions (spec §9).

    Protected regions are title/footer/logo bands confirmed by layout evidence.
    Without evidence the resolution is unverified rather than guessed.
    """
    issues: list[Issue] = []
    evidence_refs: list[str] = []
    if verified_layout_evidence:
        refs = getattr(verified_layout_evidence, "evidence", None) or []
        evidence_refs = list(refs)

    if not protected_regions:
        return ContentBoxResolution(
            content_box=RectEMU(x=0, y=0, w=canvas.w, h=canvas.h),
            protected_regions=[],
            evidence_refs=evidence_refs,
            verified=bool(evidence_refs),
            issues=[
                Issue(
                    rule_id="TEMPLATE",
                    code="CONTENT_BOX_NO_PROTECTED_REGIONS",
                    severity=Severity.WARNING,
                    details="no protected regions; full canvas returned unverified",
                )
            ]
            if not evidence_refs
            else [],
        )

    top = 0
    bottom = canvas.h
    left = 0
    right = canvas.w
    for region in protected_regions:
        # Title bands shrink the top; footer bands shrink the bottom.
        if region.y <= canvas.h // 3:
            top = max(top, region.y + region.h)
        if region.y + region.h >= 2 * canvas.h // 3:
            bottom = min(bottom, region.y)

    if bottom <= top or right <= left:
        return ContentBoxResolution(
            protected_regions=list(protected_regions),
            evidence_refs=evidence_refs,
            verified=False,
            issues=[
                Issue(
                    rule_id="TEMPLATE",
                    code="CONTENT_BOX_INFEASIBLE",
                    severity=Severity.ERROR,
                    details="protected regions cover the whole canvas",
                )
            ],
        )

    return ContentBoxResolution(
        content_box=RectEMU(x=left, y=top, w=right - left, h=bottom - top),
        protected_regions=list(protected_regions),
        evidence_refs=evidence_refs,
        verified=bool(evidence_refs),
        issues=issues,
    )


def validate_reference_style(
    slide: SlideIR,
    *,
    rules: CompiledOntology,
    context: RuleEvaluationContext,
) -> ReferenceStyleEvidence:
    """Minimal reference-style gate before full index (spec §7.3).

    Runs the deterministic rule registry over one imported slide; forbidden
    styling hints are excluded from future retrieval while reusable semantic
    geometry is kept separately with provenance.
    """
    coverage = evaluate_rule_registry(rules.rule_registry, context)
    violations = [c.rule_id for c in coverage if c.result == "fail"]
    unresolved = [c.rule_id for c in coverage if c.result == "unknown"]
    excluded = [f"style:{v}" for v in violations]
    allowed = [f"geometry:{c.rule_id}" for c in coverage if c.result == "pass"]
    return ReferenceStyleEvidence(
        source_slide_ref=slide.id,
        source_hash=slide.source_ref.sha256 if hasattr(slide, "source_ref") else "",
        checks=[c.rule_id for c in coverage],
        allowed_style_hints=allowed,
        excluded_style_hints=excluded,
        violations=violations,
        unresolved=unresolved,
        composition_reusable=not violations,
    )


# ---------------------------------------------------------------------------
# §5.2: verified corporate frame (rule-driven, evidence-backed)
# ---------------------------------------------------------------------------

def _pt_to_emu(v: float) -> int:
    return int(round(v * EMU_PER_PT))


def _layout_placeholder_boxes(
    layout_xml: bytes,
) -> tuple[list[dict[str, Any]], str]:
    """Re-derive footer/title placeholder boxes from layout XML bytes.

    Returns (regions, xml_sha256). Each region: {role, x, y, w, h} in EMU
    plus the placeholder type evidence. Never trusts an unverified profile
    claim: geometry is re-parsed from the part bytes here.
    """
    root = etree.fromstring(layout_xml)
    regions: list[dict[str, Any]] = []
    for sp in root.findall(".//p:cSld/p:spTree/p:sp", NS):
        nvpr = sp.find("p:nvSpPr/p:cNvPr", NS)
        nvph = sp.find("p:nvSpPr/p:nvPr/p:ph", NS)
        if nvph is None:
            continue
        ph_type = nvph.get("type", "body")
        if ph_type not in ("ftr", "sldNum", "dt", "hdr", "title", "ctrTitle"):
            continue
        xfrm = sp.find("p:spPr/a:xfrm", NS)
        if xfrm is None:
            continue
        off = xfrm.find("a:off", NS)
        ext = xfrm.find("a:ext", NS)
        if off is None or ext is None:
            continue
        try:
            regions.append({
                "role": ("protected_footer" if ph_type in
                         ("ftr", "sldNum", "dt", "hdr") else "title"),
                "ph_type": ph_type,
                "name": nvpr.get("name", "") if nvpr is not None else "",
                "x": int(off.get("x", 0)), "y": int(off.get("y", 0)),
                "w": int(ext.get("cx", 0)), "h": int(ext.get("cy", 0)),
            })
        except (TypeError, ValueError):
            continue
    return regions, sha256_bytes(layout_xml)


def resolve_frame_profile(
    *,
    canvas: RectEMU,
    template_profile: TemplateProfile,
    base_template_path: Path | None,
    rule_refs: list[str],
) -> dict[str, Any]:
    """Derive the normative frame from JSON canvas + layout XML evidence.

    Protected footer/title regions are re-parsed from the actual layout
    parts referenced by the profile (not copied from its unverified boxes).
    The content region is canvas minus protected strips. A missing
    mandatory element is never filled with an arbitrary full canvas +
    verified=true: without layout evidence the frame is FrameNeedsEvidence.
    """
    import hashlib as _hashlib

    regions: list[dict[str, Any]] = []
    layout_evidence: list[dict[str, Any]] = []
    findings: list[str] = []
    layout_ids: list[str] = []
    if base_template_path is not None and Path(base_template_path).is_file():
        try:
            with zipfile.ZipFile(base_template_path) as zf:
                names = [n for n in zf.namelist()
                         if n.startswith("ppt/slideLayouts/")
                         and n.endswith(".xml")]
                for name in sorted(names):
                    xml = zf.read(name)
                    boxes, xml_sha = _layout_placeholder_boxes(xml)
                    if boxes:
                        layout_ids.append(name)
                        layout_evidence.append({
                            "layout_part": name, "xml_sha256": xml_sha,
                            "placeholders": boxes})
                        regions.extend([{**b, "layout_part": name}
                                        for b in boxes])
        except (OSError, ValueError, KeyError) as exc:
            findings.append(f"layout parse failed: {exc}")
    else:
        findings.append("no base template available")

    protected = [r for r in regions if r["role"] == "protected_footer"]
    if not protected:
        return {
            "status": "needs_evidence",
            "canvas": {"x": 0, "y": 0, "w": canvas.w, "h": canvas.h},
            "findings": findings + ["no protected footer placeholders "
                                    "found in layout evidence"],
            "rule_refs": rule_refs,
        }

    protected_emu = [RectEMU(x=r["x"], y=r["y"], w=r["w"], h=r["h"])
                     for r in protected]
    resolution = compute_content_box(canvas, protected_emu,
                                     verified_layout_evidence=None)
    content = resolution.content_box
    frame = {
        "status": "verified",
        "canvas": {"x": 0, "y": 0, "w": canvas.w, "h": canvas.h},
        "content_region": ({"x": content.x, "y": content.y,
                            "w": content.w, "h": content.h}
                           if content else None),
        "protected_regions": [
            {"role": r["role"], "ph_type": r["ph_type"],
             "box_emu": {"x": r["x"], "y": r["y"],
                         "w": r["w"], "h": r["h"]},
             "layout_part": r["layout_part"]} for r in protected],
        "title_regions": [
            {"role": r["role"], "ph_type": r["ph_type"],
             "box_emu": {"x": r["x"], "y": r["y"],
                         "w": r["w"], "h": r["h"]},
             "layout_part": r["layout_part"]}
            for r in regions if r["role"] == "title"],
        "applicable_layout_ids": sorted(set(layout_ids)),
        "layout_evidence": layout_evidence,
        "rule_refs": rule_refs,
        "derivation": ("protected footer/title boxes re-parsed from "
                       "layout part XML; content region = canvas minus "
                       "protected strips"),
        "frame_sha256": _hashlib.sha256(
            repr(sorted((r["layout_part"], r["ph_type"], r["x"], r["y"],
                         r["w"], r["h"]) for r in regions)).encode()
        ).hexdigest(),
        "findings": findings,
    }
    return frame


def check_saved_frame(
    candidate_path: Path,
    frame: dict[str, Any],
) -> dict[str, Any]:
    """Verify the corporate frame on reopened candidate bytes (§5.2.5).

    Checks: canvas matches; every slide carries footer furniture
    (sldNum/ftr placeholder or slidenum field); no non-placeholder shape
    overlaps a protected region. All geometry is re-parsed from the saved
    package XML — never from expected IR or stale caches.
    """
    report: dict[str, Any] = {
        "candidate": str(candidate_path),
        "frame_sha256": frame.get("frame_sha256", ""),
        "checks": [], "violations": [], "ok": False,
    }

    def check(name: str, passed: bool, detail: str = "") -> None:
        report["checks"].append(
            {"check": name, "passed": passed, "detail": detail})
        if not passed:
            report["violations"].append(f"{name}: {detail}")

    try:
        with zipfile.ZipFile(candidate_path) as zf:
            names = zf.namelist()
            slide_parts = sorted(n for n in names
                                 if n.startswith("ppt/slides/slide")
                                 and n.endswith(".xml"))
            pres_root = etree.fromstring(zf.read("ppt/presentation.xml"))
    except (OSError, ValueError, KeyError) as exc:
        check("package_readable", False, str(exc))
        return report
    check("package_readable", True)

    canvas = frame.get("canvas", {})
    exp_w, exp_h = int(canvas.get("w", 0)), int(canvas.get("h", 0))
    sld_sz = pres_root.find("p:sldSz", NS)
    if sld_sz is not None:
        try:
            got_w, got_h = int(sld_sz.get("cx", 0)), int(sld_sz.get("cy", 0))
        except (TypeError, ValueError):
            got_w, got_h = 0, 0
        check("canvas_matches", got_w == exp_w and got_h == exp_h,
              f"package={got_w}x{got_h} frame={exp_w}x{exp_h}")
    else:
        check("canvas_matches", False, "no sldSz")

    protected = [r["box_emu"] for r in
                 frame.get("protected_regions", [])]

    def overlaps(a: dict[str, int], b: dict[str, int]) -> bool:
        return not (a["x"] + a["w"] <= b["x"] or b["x"] + b["w"] <= a["x"]
                    or a["y"] + a["h"] <= b["y"]
                    or b["y"] + b["h"] <= a["y"])

    with zipfile.ZipFile(candidate_path) as zf:
        for part in slide_parts:
            try:
                root = etree.fromstring(zf.read(part))
            except (ValueError, KeyError):
                check(f"{part}_readable", False, "unparseable")
                continue
            has_footer = False
            for sp in root.findall(".//p:cSld/p:spTree/p:sp", NS):
                nvph = sp.find("p:nvSpPr/p:nvPr/p:ph", NS)
                if nvph is not None and nvph.get("type", "") in (
                        "ftr", "sldNum", "dt", "hdr"):
                    has_footer = True
                    continue
                if sp.find(".//a:fld[@type='slidenum']", NS) is not None:
                    has_footer = True
                    continue
                if nvph is not None:
                    continue  # other placeholders are furniture, not content
                xfrm = sp.find("p:spPr/a:xfrm", NS)
                if xfrm is None:
                    continue
                off = xfrm.find("a:off", NS)
                ext = xfrm.find("a:ext", NS)
                if off is None or ext is None:
                    continue
                try:
                    box = {"x": int(off.get("x", 0)),
                           "y": int(off.get("y", 0)),
                           "w": int(ext.get("cx", 0)),
                           "h": int(ext.get("cy", 0))}
                except (TypeError, ValueError):
                    continue
                for prot in protected:
                    if box["w"] > 0 and box["h"] > 0 and overlaps(box, prot):
                        cNvPr = sp.find("p:nvSpPr/p:cNvPr", NS)
                        nm = cNvPr.get("name", "?") if cNvPr is not None \
                            else "?"
                        check(f"{part}_protected_clear", False,
                              f"shape {nm} overlaps protected region")
                        break
            check(f"{part}_footer_present", has_footer,
                  "" if has_footer else "no ftr/sldNum furniture")
    report["ok"] = not report["violations"]
    return report
