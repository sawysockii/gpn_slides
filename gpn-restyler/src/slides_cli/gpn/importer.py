"""PPTX → SourceDeckIR importer (spec §10.2).

Loss-free import: every shape gets an ObjectIR or an explicit import issue;
no content payload may disappear. The source file is never rewritten.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lxml import etree
from pptx import Presentation

from .assets import AssetStore, atomic_write_json
from .errors import InputError, RenderError
from .models import (
    Affine2D,
    ChartBindingComparison,
    ImportArtifactManifest,
    ImportSupportReport,
    Issue,
    LinkResolutionRecord,
    LinkResolutionReport,
    ObjectCapabilityReport,
    ObjectIR,
    RectEMU,
    Severity,
    SlideIR,
    SourceDeckIR,
    SourceLedger,
    Uncertainty,
    WorkbookSnapshot,
)
from .package import PackageGraph, ResourceLimits, build_manifest, read_package, slide_part_order
from .provenance import build_ledger
from .readers import (
    NS,
    ImportContext,
    extract_notes,
    is_hidden_slide,
    walk_shape_tree,
)


@dataclass
class HyperlinkOccurrence:
    """One hyperlink occurrence in document order (Stage 3 §2.3).

    Identity is owner_part + xml_path + event_kind, never the bare rId:
    two objects may share one relationship.
    """

    owner_part: str
    xml_path: str
    subject_id: str
    event_kind: str  # click | hover
    relationship_id: str
    action: str | None = None
    tooltip: str | None = None


def _localname(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.split("}", 1)[1] if "}" in tag else tag


def iter_hyperlink_occurrences(
    owner_part: str,
    xml_root: Any,
) -> Any:
    """Yield every run/field/shape hyperlink occurrence in XML order.

    Covers run/field nodes (a:hlinkClick/a:hlinkHover under rPr) and
    shape-level nodes (including nested groups), click and hover alike.
    """
    from pptx.oxml.ns import qn as _qn

    r_id_attr = _qn("r:id")
    # Build deterministic xml paths with sibling indices.
    root = xml_root
    # Map element id() -> path via iterative DFS in document order.
    stack: list[tuple[Any, str]] = [(root, "/root")]
    # Sibling counters per parent.
    while stack:
        node, path = stack.pop(0)
        try:
            children = list(node)
        except TypeError:
            continue
        counters: dict[str, int] = {}
        for child in children:
            lname = _localname(getattr(child, "tag", ""))
            counters[lname] = counters.get(lname, 0) + 1
            child_path = f"{path}/{lname}[{counters[lname]}]"
            if lname in ("hlinkClick", "hlinkHover"):
                event = "click" if lname == "hlinkClick" else "hover"
                rid = child.get(r_id_attr) or ""
                tooltip = child.get("tooltip")
                # Subject: nearest ancestor cNvPr @id, else run context.
                subject = ""
                parent = node
                depth = 0
                while parent is not None and depth < 12:
                    if _localname(getattr(parent, "tag", "")) == "cNvPr":
                        subject = f"cNvPr:{parent.get('id') or '?'}"
                        break
                    parent = parent.getparent() \
                        if hasattr(parent, "getparent") else None
                    depth += 1
                if not subject:
                    subject = f"run:{child_path}"
                yield HyperlinkOccurrence(
                    owner_part=owner_part,
                    xml_path=child_path,
                    subject_id=subject,
                    event_kind=event,
                    relationship_id=rid,
                    action=None,
                    tooltip=tooltip,
                )
            stack.append((child, child_path))

log = logging.getLogger(__name__)


@dataclass
class ImportOptions:
    """Options for :func:`import_deck` (spec §7.8)."""

    preserve_notes: bool = True
    extract_unknown_parts: bool = True
    resource_limits: ResourceLimits = field(default_factory=ResourceLimits)
    run_dir: Path | None = None


def _convert_legacy_ppt(source: Path, run_dir: Path) -> tuple[Path, dict[str, Any]]:
    """Read-only LibreOffice conversion of legacy ``.ppt`` (spec §10.2 step 1)."""
    soffice = shutil.which("soffice") or "/Applications/LibreOffice.app/Contents/MacOS/soffice"
    out_dir = run_dir / "legacy_conversion"
    out_dir.mkdir(parents=True, exist_ok=True)
    profile = out_dir / "lo_profile"
    profile.mkdir(parents=True, exist_ok=True)
    cmd = [
        soffice,
        "--headless",
        f"-env:UserInstallation=file://{profile}",
        "--convert-to",
        "pptx",
        "--outdir",
        str(out_dir),
        str(source),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RenderError(
            "LEGACY_CONVERSION_FAILED",
            f"LibreOffice could not convert {source.name}: {exc}",
            artifact_paths=[source],
        ) from exc
    converted = out_dir / (source.stem + ".pptx")
    report = {
        "command": cmd,
        "returncode": proc.returncode,
        "stderr": (proc.stderr or "")[-2000:],
        "original": str(source),
        "converted": str(converted),
    }
    if proc.returncode != 0 or not converted.is_file():
        raise RenderError(
            "LEGACY_CONVERSION_FAILED",
            f"LibreOffice conversion of {source.name} failed (rc={proc.returncode})",
            artifact_paths=[converted],
        )
    return converted, report


def _slide_order_and_canvas(path: Path) -> tuple[list[str], tuple[int, int], dict[str, str]]:
    prs = Presentation(str(path))
    canvas = (int(prs.slide_width), int(prs.slide_height))
    order: list[str] = []
    for slide in prs.slides:
        order.append(str(slide.part.partname).lstrip("/"))
    props: dict[str, str] = {}
    core = prs.core_properties
    for attr in ("title", "author", "subject", "comments", "category", "keywords", "language"):
        value = getattr(core, attr, None)
        if value:
            props[attr] = str(value)
    return order, canvas, props


def import_deck(
    source: Path,
    store: AssetStore,
    options: ImportOptions | None = None,
) -> tuple[SourceDeckIR, SourceLedger]:
    """Import a PPTX into SourceDeckIR + SourceLedger (spec §10.2)."""
    options = options or ImportOptions()
    source = Path(source)
    if not source.is_file():
        raise InputError("INPUT_NOT_FOUND", f"input file not found: {source}",
                         artifact_paths=[source])

    effective_source = source
    conversion_report: dict[str, Any] | None = None
    if source.suffix.lower() == ".ppt":
        if options.run_dir is None:
            raise InputError(
                "LEGACY_PPT_NEEDS_RUN_DIR",
                "legacy .ppt input requires a run directory for conversion",
                artifact_paths=[source],
            )
        effective_source, conversion_report = _convert_legacy_ppt(source, options.run_dir)
    elif source.suffix.lower() != ".pptx":
        raise InputError(
            "INPUT_NOT_PPTX",
            f"expected .pptx or .ppt input, got {source.suffix!r}",
            artifact_paths=[source],
        )

    graph = read_package(effective_source, store, options.resource_limits)
    source_hash = graph.source_sha256
    order, (canvas_w, canvas_h), props = _slide_order_and_canvas(effective_source)
    if not order:
        order = list(slide_part_order(graph, b""))

    issues: list[Issue] = list(graph.issues)
    slides: list[SlideIR] = []
    all_objects: list[ObjectIR] = []

    for index, slide_part in enumerate(order):
        part_ir = graph.parts_by_name.get(slide_part)
        if part_ir is None or part_ir.asset is None:
            issues.append(
                Issue(
                    rule_id="IMPORT_SLIDE_PART",
                    code="SLIDE_PART_MISSING",
                    severity=Severity.ERROR,
                    slide_id=slide_part,
                    details=f"slide part listed but not readable: {slide_part}",
                    repairable=False,
                )
            )
            continue
        xml = store.read_bytes(part_ir.asset)
        root = etree.fromstring(
            xml, parser=etree.XMLParser(resolve_entities=False, no_network=True)
        )
        ctx = ImportContext(
            graph=graph,
            asset_store=store,
            source_hash=source_hash,
            slide_part=slide_part,
        )
        sp_tree = root.find("p:cSld/p:spTree", namespaces=NS)
        if sp_tree is None:
            issues.append(
                Issue(
                    rule_id="IMPORT_SLIDE",
                    code="SLIDE_TREE_MISSING",
                    severity=Severity.ERROR,
                    slide_id=slide_part,
                    details="p:spTree not found",
                    repairable=False,
                )
            )
            continue

        objects = walk_shape_tree(sp_tree, slide_part, Affine2D(), (), ctx)

        # Resolve connector endpoints to full object ids after the walk.
        id_by_shape_id: dict[str, str] = {}
        for obj in objects:
            id_by_shape_id[str(obj.source_ref.shape_id)] = obj.id
        for obj in objects:
            payload = obj.payload
            if obj.kind == "connector" and hasattr(payload, "from_object_id"):
                if payload.from_object_id and payload.from_object_id.startswith("shapeid:"):
                    key = payload.from_object_id.split(":", 1)[1]
                    payload.from_object_id = id_by_shape_id.get(key)
                if payload.to_object_id and payload.to_object_id.startswith("shapeid:"):
                    key = payload.to_object_id.split(":", 1)[1]
                    payload.to_object_id = id_by_shape_id.get(key)

        notes: list[Any] = []
        if options.preserve_notes:
            notes = extract_notes(slide_part, ctx)

        layout_part = _related_part(graph, slide_part, "/slideLayout")
        master_part = None
        if layout_part:
            master_part = _related_part(graph, layout_part, "/slideMaster")

        issues.extend(ctx.issues)
        title_ref = _title_ref(objects)
        slide = SlideIR(
            id=f"slide{index}:{slide_part}",
            source_index=index,
            slide_part=slide_part,
            title_ref=title_ref,
            objects=objects,
            notes=notes,
            hidden=is_hidden_slide(root),
            source_master_part=master_part,
            source_layout_part=layout_part,
            source_canvas=RectEMU(x=0, y=0, w=canvas_w, h=canvas_h),
            relationships=list(graph.outgoing.get(slide_part, [])),
        )
        slides.append(slide)
        all_objects.extend(objects)

    deck = SourceDeckIR(
        input_kind="pptx",
        source_sha256=source_hash,
        source_filename=source.name,
        slides=slides,
        assets=_collect_assets(store, graph),
        package_manifest=build_manifest(graph),
        source_properties=props,
        import_issues=issues,
    )
    if conversion_report is not None:
        deck.source_properties["legacy_conversion"] = "see run report"
    ledger = build_ledger(deck, decoration=[])
    return deck, ledger


def import_deck_with_report(
    source: Path,
    store: AssetStore,
    options: ImportOptions | None = None,
) -> tuple[SourceDeckIR, SourceLedger, LinkResolutionReport, ImportSupportReport]:
    """Import a PPTX and produce full Stage 1 artifacts (spec §4.4)."""
    options = options or ImportOptions()
    deck, ledger = import_deck(source, store, options)
    graph = read_package(source, store, options.resource_limits)
    links = resolve_internal_links(deck, graph)
    support = build_import_support_report(deck, ledger, links, deck.import_issues)
    return deck, ledger, links, support


def _related_part(graph: PackageGraph, part: str, suffix: str) -> str | None:
    for rel in graph.outgoing.get(part, []):
        if rel.rel_type.endswith(suffix) and rel.resolved_part:
            return rel.resolved_part
    return None


def _title_ref(objects: list[ObjectIR]) -> str | None:
    for obj in objects:
        payload = obj.payload
        text = getattr(payload, "text", None) or (
            payload if obj.kind == "text" else None
        )
        if text is not None and getattr(text, "role", None) in ("title", "unknown"):
            paragraphs = getattr(text, "paragraphs", [])
            if paragraphs and paragraphs[0].runs:
                return paragraphs[0].id
    return None


def _collect_assets(store: AssetStore, graph: PackageGraph) -> list[Any]:
    seen: dict[str, Any] = {}
    for part in graph.parts_by_name.values():
        if part.asset is not None:
            seen[part.asset.sha256] = part.asset
    return list(seen.values())


# ---------------------------------------------------------------------------
# Stage 1: internal link resolution
# ---------------------------------------------------------------------------


def _iter_run_hyperlinks(
    deck: SourceDeckIR,
) -> Any:
    """Yield ``(slide, run_id, hyperlink)`` for every run-level hyperlink.

    Walks text/shape/group/table/note payloads so nested group links are
    covered with their true owner scope.
    """
    from .models import GroupPayload

    def _texts_of(obj: ObjectIR) -> Any:
        payload = obj.payload
        if isinstance(payload, GroupPayload):
            for child in payload.children:
                yield from _texts_of(child)
            if payload.text is not None:
                yield payload.text
            return
        text = getattr(payload, "text", None)
        if text is not None:
            yield text
            return
        if hasattr(payload, "paragraphs"):
            yield payload
            return
        cells = getattr(payload, "cells", None)
        if cells:
            for cell in cells:
                if cell.paragraphs:
                    yield from [type("P", (), {"paragraphs": cell.paragraphs})()]

    for slide in deck.slides:
        for obj in slide.objects:
            for text in _texts_of(obj):
                for para in text.paragraphs:
                    for run in para.runs:
                        if run.hyperlink is not None:
                            yield slide, run.id, run.hyperlink
        for para in slide.notes:
            for run in para.runs:
                if run.hyperlink is not None:
                    yield slide, run.id, run.hyperlink


def resolve_internal_links(
    deck: SourceDeckIR,
    graph: PackageGraph,
    slide_xml: dict[str, Any] | None = None,
) -> LinkResolutionReport:
    """Resolve all run/shape hyperlinks to source slide IDs (spec §4.2).

    First pass (importer) already built stable source slide IDs from the real
    presentation order. This second pass maps each hyperlink's owner-scoped
    ``r:id`` through OPC relationships — normalized against the owner part,
    never the process cwd — to canonical slide parts and source IDs.
    External URIs are recorded without any network request.
    """
    slide_part_to_id = {s.slide_part: s.id for s in deck.slides if s.slide_part}

    resolved: list[LinkResolutionRecord] = []
    unresolved: list[LinkResolutionRecord] = []
    navigation_actions: list[LinkResolutionRecord] = []
    external_links: list[LinkResolutionRecord] = []
    issues: list[Issue] = []
    # Occurrence identity is owner + subject + rel (shared rIds kept).
    seen_occurrences: set[tuple[str, str, str]] = set()

    def _resolve_one(
        slide: SlideIR,
        subject_id: str,
        owner_part: str,
        rel_id: str,
        fallback_uri: str | None,
    ) -> None:
        occurrence_key = (owner_part, subject_id, rel_id)
        if occurrence_key in seen_occurrences:
            return
        seen_occurrences.add(occurrence_key)
        rel = graph.find_relationship(owner_part, rel_id) if rel_id else None
        if rel is None:
            if fallback_uri:
                # External URI stored directly on the run (no package rel).
                external_links.append(LinkResolutionRecord(
                    subject_id=subject_id,
                    owner_part=owner_part,
                    source_rel_id=rel_id,
                    source_target=fallback_uri,
                    action=None,
                    status="external",
                    reason="external URI",
                    evidence_refs=[f"{owner_part}:{rel_id}"],
                ))
                return
            unresolved.append(LinkResolutionRecord(
                subject_id=subject_id,
                owner_part=owner_part,
                source_rel_id=rel_id,
                source_target="",
                status="unresolved",
                reason="dangling relationship id",
                evidence_refs=[f"{owner_part}:{rel_id}"],
            ))
            issues.append(Issue(
                rule_id="LINKS",
                code="LINK_DANGLING",
                severity=Severity.ERROR,
                slide_id=slide.id,
                details=f"hyperlink {rel_id} has no relationship in {owner_part}",
                evidence=[owner_part, rel_id],
            ))
            return
        if rel.target_mode == "external":
            external_links.append(LinkResolutionRecord(
                subject_id=subject_id,
                owner_part=owner_part,
                source_rel_id=rel.rel_id,
                source_target=rel.target,
                action=None,
                status="external",
                reason="external URI",
                evidence_refs=[f"{owner_part}:{rel.rel_id}"],
            ))
            return

        target_part = rel.resolved_part
        if target_part is None:
            unresolved.append(LinkResolutionRecord(
                subject_id=subject_id,
                owner_part=owner_part,
                source_rel_id=rel.rel_id,
                source_target=rel.target,
                status="unresolved",
                reason="dangling relationship",
                evidence_refs=[f"{owner_part}:{rel.rel_id}"],
            ))
            issues.append(Issue(
                rule_id="LINKS",
                code="LINK_DANGLING",
                severity=Severity.ERROR,
                slide_id=slide.id,
                details=f"hyperlink {rel.rel_id} has no resolved target",
                evidence=[owner_part, rel.rel_id],
            ))
            return

        if target_part in slide_part_to_id:
            target_slide_ref = slide_part_to_id[target_part]
            action = _classify_link_action(rel.target)
            if action in ("next", "previous", "first", "last"):
                navigation_actions.append(LinkResolutionRecord(
                    subject_id=subject_id,
                    owner_part=owner_part,
                    source_rel_id=rel.rel_id,
                    source_target=rel.target,
                    target_slide_ref=target_slide_ref,
                    action=action,
                    status="navigation",
                    reason=f"navigation action {action}",
                    evidence_refs=[f"{owner_part}:{rel.rel_id}"],
                ))
            else:
                resolved.append(LinkResolutionRecord(
                    subject_id=subject_id,
                    owner_part=owner_part,
                    source_rel_id=rel.rel_id,
                    source_target=rel.target,
                    target_slide_ref=target_slide_ref,
                    action=action,
                    status="resolved",
                    reason="internal slide link",
                    evidence_refs=[f"{owner_part}:{rel.rel_id}"],
                ))
        else:
            unresolved.append(LinkResolutionRecord(
                subject_id=subject_id,
                owner_part=owner_part,
                source_rel_id=rel.rel_id,
                source_target=rel.target,
                status="unresolved",
                reason=f"target part {target_part} not in slide map",
                evidence_refs=[f"{owner_part}:{rel.rel_id}"],
            ))
            issues.append(Issue(
                rule_id="LINKS",
                code="LINK_TARGET_NOT_SLIDE",
                severity=Severity.WARNING,
                slide_id=slide.id,
                details=f"hyperlink target {target_part} is not a slide",
                evidence=[owner_part, rel.rel_id, target_part],
            ))

    for slide, run_id, hyperlink in _iter_run_hyperlinks(deck):
        owner = slide.slide_part or ""
        _resolve_one(slide, run_id, owner, hyperlink.source_rel_id,
                     hyperlink.uri)

    # Shape-level + hover occurrences from raw slide XML (Stage 3 §2.3).
    # Run-click nodes are already covered via the IR pass; the raw pass only
    # adds occurrences not already recorded (shape nodes, hover events).
    if slide_xml:
        slide_by_part = {s.slide_part: s for s in deck.slides if s.slide_part}
        for owner_part, xml_root in slide_xml.items():
            slide = slide_by_part.get(owner_part)
            if slide is None:
                continue
            for occ in iter_hyperlink_occurrences(owner_part, xml_root):
                # Skip run-click duplicates already covered by the IR pass:
                # those live under rPr/fld parents and were resolved above.
                is_run_node = "/rPr[" in occ.xml_path or "/fld[" in occ.xml_path
                if is_run_node and occ.event_kind == "click":
                    continue
                rel = graph.find_relationship(
                    owner_part, occ.relationship_id) \
                    if occ.relationship_id else None
                fallback = None
                if rel is None and not occ.relationship_id:
                    continue
                subject = f"{occ.subject_id}:{occ.event_kind}:{occ.xml_path}"
                if rel is None:
                    # Dangling content link: blocking uncertainty surface.
                    _resolve_one(slide, subject, owner_part,
                                 occ.relationship_id, fallback)
                    continue
                if rel.target_mode == "external":
                    occurrence_key = (
                        owner_part, subject, occ.relationship_id)
                    if occurrence_key in seen_occurrences:
                        continue
                    seen_occurrences.add(occurrence_key)
                    external_links.append(LinkResolutionRecord(
                        subject_id=subject,
                        owner_part=owner_part,
                        source_rel_id=rel.rel_id,
                        source_target=rel.target,
                        action=None,
                        status="external",
                        reason=f"external URI ({occ.event_kind})",
                        evidence_refs=[f"{owner_part}:{rel.rel_id}"],
                    ))
                    continue
                _resolve_one(slide, subject, owner_part,
                             occ.relationship_id, None)

    return LinkResolutionReport(
        resolved=resolved,
        unresolved=unresolved,
        navigation_actions=navigation_actions,
        external_links=external_links,
        issues=issues,
    )


def _classify_link_action(target: str) -> str | None:
    """Classify a hyperlink target as a navigation action or None."""
    target_lower = target.lower().strip()
    if target_lower in ("next", "nextslide", "next slide"):
        return "next"
    if target_lower in ("previous", "prev", "previousslide", "previous slide"):
        return "previous"
    if target_lower in ("first", "firstslide", "first slide"):
        return "first"
    if target_lower in ("last", "lastslide", "last slide"):
        return "last"
    return None


# ---------------------------------------------------------------------------
# Stage 1: chart binding comparison
# ---------------------------------------------------------------------------


def compare_chart_bindings(
    chart: Any,
    *,
    workbook: WorkbookSnapshot | None,
) -> list[ChartBindingComparison]:
    """Compare cached chart values with workbook values (spec §4.3)."""
    comparisons: list[ChartBindingComparison] = []
    if not hasattr(chart, "series"):
        return comparisons

    for series in chart.series:
        for formula in series.formula_refs:
            if not formula:
                continue
            binding_id = f"{series.id}:{formula}"
            cached_values = [
                p.value for p in series.points
                if p.value and p.value.state == "present"
            ]

            if workbook is None:
                comparisons.append(ChartBindingComparison(
                    chart_id=getattr(chart, "chart_part", ""),
                    series_id=series.id,
                    binding_id=binding_id,
                    formula=formula,
                    cached_value=cached_values[0] if cached_values else None,
                    status="not_applicable",
                    reason="no workbook available; literal data only",
                ))
                continue

            workbook_values = _lookup_workbook_values(workbook, formula)
            if workbook_values is None:
                comparisons.append(ChartBindingComparison(
                    chart_id=getattr(chart, "chart_part", ""),
                    series_id=series.id,
                    binding_id=binding_id,
                    formula=formula,
                    cached_value=cached_values[0] if cached_values else None,
                    status="unverifiable",
                    reason="formula not found in workbook",
                ))
                continue

            for i, cached in enumerate(cached_values):
                if i >= len(workbook_values):
                    break
                wb_val = workbook_values[i]
                if cached is None or wb_val is None:
                    continue
                if cached.decimal is None or wb_val.decimal is None:
                    continue
                try:
                    from decimal import Decimal
                    if Decimal(cached.decimal) == Decimal(wb_val.decimal):
                        comparisons.append(ChartBindingComparison(
                            chart_id=getattr(chart, "chart_part", ""),
                            series_id=series.id,
                            binding_id=binding_id,
                            formula=formula,
                            cached_value=cached,
                            workbook_value=wb_val,
                            status="equal",
                            reason="values match",
                        ))
                    else:
                        comparisons.append(ChartBindingComparison(
                            chart_id=getattr(chart, "chart_part", ""),
                            series_id=series.id,
                            binding_id=binding_id,
                            formula=formula,
                            cached_value=cached,
                            workbook_value=wb_val,
                            status="conflict",
                            reason="cached value differs from workbook",
                        ))
                except Exception:
                    if cached.display_text != wb_val.display_text:
                        comparisons.append(ChartBindingComparison(
                            chart_id=getattr(chart, "chart_part", ""),
                            series_id=series.id,
                            binding_id=binding_id,
                            formula=formula,
                            cached_value=cached,
                            workbook_value=wb_val,
                            status="conflict",
                            reason="values differ",
                        ))

    return comparisons


def _lookup_workbook_values(
    workbook: WorkbookSnapshot,
    formula: str,
) -> list[Any] | None:
    """Look up values in a workbook snapshot by formula reference."""
    import re

    pattern = r"^'?(?P<sheet>[^'!]+)'?!(?P<rng>\$?[A-Z]+\$?\d+(?::\$?[A-Z]+\$?\d+)?)$"
    m = re.match(pattern, formula.strip())
    if not m:
        return None
    sheet_name = m.group("sheet")
    try:
        range_str = m.group("range").replace("$", "")
    except IndexError:
        range_str = m.group("rng").replace("$", "")

    for sheet in workbook.sheets:
        if sheet.sheet_name == sheet_name:
            values = []
            for cell in sheet.cells:
                if _cell_in_range(cell.cell_ref, range_str):
                    values.append(cell.value)
            return values if values else None
    return None


def _cell_in_range(cell_ref: str, range_str: str) -> bool:
    """Check if a cell reference is within a range."""
    m = range_str.split(":")
    if len(m) == 2:
        start, end = m
        return _cell_ge(cell_ref, start) and _cell_le(cell_ref, end)
    return cell_ref == range_str


def _cell_ge(cell_ref: str, start: str) -> bool:
    import re
    cell_col = re.match(r"[A-Z]+", cell_ref)
    start_col = re.match(r"[A-Z]+", start)
    if cell_col and start_col:
        return cell_col.group() >= start_col.group()
    return True


def _cell_le(cell_ref: str, end: str) -> bool:
    import re
    cell_col = re.match(r"[A-Z]+", cell_ref)
    end_col = re.match(r"[A-Z]+", end)
    if cell_col and end_col:
        return cell_col.group() <= end_col.group()
    return True


def collect_import_uncertainties(
    slide: SlideIR,
    comparisons: list[ChartBindingComparison],
) -> list[Uncertainty]:
    """Collect blocking uncertainties from chart comparisons (spec §4.3)."""
    uncertainties: list[Uncertainty] = []
    for comp in comparisons:
        if comp.status == "conflict":
            cached = comp.cached_value.display_text if comp.cached_value else "?"
            booked = comp.workbook_value.display_text if comp.workbook_value else "?"
            uncertainties.append(Uncertainty(
                id=f"chart-conflict:{comp.binding_id}",
                subject_id=slide.id,
                code="CHART_CACHE_WORKBOOK_CONFLICT",
                evidence_refs=comp.evidence_refs,
                question=f"cached value {cached} differs from workbook {booked}",
                blocking=True,
            ))
        elif comp.status == "unverifiable":
            uncertainties.append(Uncertainty(
                id=f"chart-unverifiable:{comp.binding_id}",
                subject_id=slide.id,
                code="CHART_FORMULA_UNVERIFIABLE",
                evidence_refs=comp.evidence_refs,
                question=f"formula {comp.formula} could not be verified against workbook",
                blocking=True,
            ))
    return uncertainties


# ---------------------------------------------------------------------------
# Stage 1: import support report
# ---------------------------------------------------------------------------


def build_import_support_report(
    deck: SourceDeckIR,
    ledger: SourceLedger,
    links: LinkResolutionReport,
    issues: list[Issue],
) -> ImportSupportReport:
    """Build the import support report (spec §4.4)."""
    capabilities_by_kind: dict[str, dict[str, int]] = {}
    per_object: list[ObjectCapabilityReport] = []
    uncertainties: list[Uncertainty] = []
    blocking_uncertainty_ids: list[str] = []

    for slide in deck.slides:
        for obj in slide.objects:
            kind = obj.kind
            level = obj.capability.level
            capabilities_by_kind.setdefault(kind, {})
            capabilities_by_kind[kind][level] = capabilities_by_kind[kind].get(level, 0) + 1

            per_object.append(ObjectCapabilityReport(
                object_id=obj.id,
                kind=kind,
                capability=obj.capability,
                source_ref=obj.source_ref,
                reason=obj.capability.reason,
                uncertainty_ids=[u.id for u in slide.uncertainties],
            ))

        uncertainties.extend(slide.uncertainties)

    for u in uncertainties:
        if u.blocking:
            blocking_uncertainty_ids.append(u.id)

    source_data_verified = len(blocking_uncertainty_ids) == 0

    return ImportSupportReport(
        source_hash=deck.source_sha256,
        object_count=len(per_object),
        capabilities_by_kind=capabilities_by_kind,
        per_object=per_object,
        uncertainties=uncertainties,
        unresolved_links=links.unresolved,
        issues=issues,
        import_status="source_import_complete",
        source_data_verified=source_data_verified,
        blocking_uncertainty_ids=blocking_uncertainty_ids,
    )


def write_import_artifacts(
    *,
    run_dir: Path,
    deck: SourceDeckIR,
    ledger: SourceLedger,
    package_manifest: Any,
    support: ImportSupportReport | None = None,
    links: LinkResolutionReport | None = None,
) -> ImportArtifactManifest:
    """Persist source_ir / source_ledger / source_package_manifest + support/link (§4.4)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    ref = atomic_write_json(run_dir / "source_ir.json", deck)
    source_ir_ref = ref
    ref = atomic_write_json(run_dir / "source_ledger.json", ledger)
    source_ledger_ref = ref
    ref = atomic_write_json(run_dir / "source_package_manifest.json", package_manifest)
    source_package_manifest_ref = ref

    support_ref = None
    if support is not None:
        ref = atomic_write_json(run_dir / "support_report.json", support)
        support_ref = ref

    links_ref = None
    if links is not None:
        ref = atomic_write_json(run_dir / "link_resolution.json", links)
        links_ref = ref

    return ImportArtifactManifest(
        source_ir=source_ir_ref,
        source_ledger=source_ledger_ref,
        source_package_manifest=source_package_manifest_ref,
        support_report=support_ref,
        link_resolution=links_ref,
    )
