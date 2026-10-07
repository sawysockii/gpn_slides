"""OPC package graph reader for PPTX files (spec §10.1).

Reads the ZIP/OPC structure without ``extractall``: content types, relationship
graphs, slide order from ``presentation.xml`` (never from sorted filenames),
and stores every part as an immutable asset.
"""

from __future__ import annotations

import posixpath
import zipfile
from pathlib import Path
from typing import Literal

from lxml import etree
from pydantic import BaseModel, ConfigDict, Field

from .assets import AssetStore, sha256_bytes
from .errors import InputError
from .models import AssetRef, Issue, PackageManifest, PartIR, RelationshipIR, Severity

CONTENT_TYPES = "[Content_Types].xml"
ROOT_RELS = "_rels/.rels"
NS_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
NS_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
PML = "http://schemas.openxmlformats.org/presentationml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_OFFICE_DOC = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
)
REL_WORKSHEET = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
)

_XML_PARSER = etree.XMLParser(
    resolve_entities=False,
    no_network=True,
    recover=False,
    huge_tree=False,
    load_dtd=False,
)


class ResourceLimits(BaseModel):
    """Engineer-grade input limits (spec §7.8)."""

    model_config = ConfigDict(extra="forbid")

    max_zip_parts: int = 10000
    max_total_uncompressed_bytes: int = 1 << 30  # 1 GiB
    max_xml_depth: int = 200


class PackageGraph(BaseModel):
    """Full relationship graph of one OPC package."""

    model_config = ConfigDict(extra="forbid")

    parts_by_name: dict[str, PartIR] = Field(default_factory=dict)
    outgoing: dict[str, list[RelationshipIR]] = Field(default_factory=dict)
    incoming: dict[str, list[RelationshipIR]] = Field(default_factory=dict)
    root_relationships: list[RelationshipIR] = Field(default_factory=list)
    slide_order: list[str] = Field(default_factory=list)
    content_types: dict[str, str] = Field(default_factory=dict)
    content_types_asset: AssetRef | None = None
    issues: list[Issue] = Field(default_factory=list)
    source_path: Path | None = None
    source_sha256: str = ""

    def rels_of(self, part_name: str) -> list[RelationshipIR]:
        return list(self.outgoing.get(part_name, []))

    def find_relationship(self, part_name: str, rel_id: str) -> RelationshipIR | None:
        for rel in self.outgoing.get(part_name, []):
            if rel.rel_id == rel_id:
                return rel
        return None

    def resolve_target(self, rel: RelationshipIR) -> str | None:
        return rel.resolved_part


def normalize_part_name(name: str) -> str:
    """Normalize a ZIP entry / OPC part name to POSIX form without leading slash."""
    name = name.replace("\\", "/")
    if name.startswith("/"):
        name = name[1:]
    return posixpath.normpath(name)


def _resolve_target(owner_part: str, target: str) -> tuple[Literal["internal", "external"], str]:
    if "://" in target or target.startswith("mailto:"):
        return "external", target
    owner_dir = posixpath.dirname(owner_part)
    resolved = posixpath.normpath(posixpath.join(owner_dir, target))
    return "internal", resolved.lstrip("/")


def _rels_name_for(part_name: str) -> str:
    directory, base = posixpath.split(part_name)
    return posixpath.join(directory, "_rels", base + ".rels")


def _parse_relationships(data: bytes, owner_part: str, issues: list[Issue]) -> list[RelationshipIR]:
    try:
        root = etree.fromstring(data, parser=_XML_PARSER)
    except etree.XMLSyntaxError as exc:
        issues.append(
            Issue(
                rule_id="OPC_RELS_PARSE",
                code="RELS_XML_INVALID",
                severity=Severity.ERROR,
                details=f"{owner_part}: {exc}",
                evidence=[owner_part],
                repairable=False,
            )
        )
        return []
    rels: list[RelationshipIR] = []
    seen: set[str] = set()
    for node in root.findall(f"{{{NS_REL}}}Relationship"):
        rel_id = node.get("Id") or ""
        rel_type = node.get("Type") or ""
        target = node.get("Target") or ""
        mode_raw = node.get("TargetMode") or "Internal"
        if not rel_id or not rel_type or not target:
            issues.append(
                Issue(
                    rule_id="OPC_RELS_INCOMPLETE",
                    code="REL_INCOMPLETE",
                    severity=Severity.ERROR,
                    details=f"{owner_part}: relationship missing Id/Type/Target",
                    evidence=[owner_part],
                    repairable=False,
                )
            )
            continue
        if rel_id in seen:
            issues.append(
                Issue(
                    rule_id="OPC_RELS_DUPLICATE",
                    code="REL_DUPLICATE_ID",
                    severity=Severity.ERROR,
                    details=f"{owner_part}: duplicate relationship id {rel_id}",
                    evidence=[owner_part, rel_id],
                    repairable=False,
                )
            )
        seen.add(rel_id)
        if mode_raw.lower() == "external" or "://" in target:
            mode: Literal["internal", "external"] = "external"
            resolved = None
        else:
            mode, resolved = _resolve_target(owner_part, target)
        rels.append(
            RelationshipIR(
                owner_part=owner_part,
                rel_id=rel_id,
                rel_type=rel_type,
                target_mode=mode,
                target=target,
                resolved_part=resolved,
            )
        )
    return rels


def _parse_content_types(data: bytes, issues: list[Issue]) -> dict[str, str]:
    try:
        root = etree.fromstring(data, parser=_XML_PARSER)
    except etree.XMLSyntaxError as exc:
        issues.append(
            Issue(
                rule_id="OPC_CONTENT_TYPES",
                code="CONTENT_TYPES_INVALID",
                severity=Severity.ERROR,
                details=str(exc),
                evidence=[CONTENT_TYPES],
                repairable=False,
            )
        )
        return {}
    types: dict[str, str] = {}
    for node in root.findall(f"{{{NS_CT}}}Override"):
        part = normalize_part_name(node.get("PartName") or "")
        ctype = node.get("ContentType") or ""
        if part and ctype:
            types[part] = ctype
    for node in root.findall(f"{{{NS_CT}}}Default"):
        ext = (node.get("Extension") or "").lower()
        ctype = node.get("ContentType") or ""
        if ext and ctype:
            types[f"*.{ext}"] = ctype
    return types


def _content_type_for(part_name: str, types: dict[str, str]) -> str:
    if part_name in types:
        return types[part_name]
    ext = part_name.rsplit(".", 1)[-1].lower() if "." in part_name else ""
    return types.get(f"*.{ext}", "application/octet-stream")


def read_package(
    path: Path, store: AssetStore, limits: ResourceLimits | None = None
) -> PackageGraph:
    """Read a PPTX ZIP into a full OPC graph (spec §10.1).

    Args:
        path: source ``.pptx`` file (never modified).
        store: content-addressed asset store receiving every part.
        limits: resource limits checked before payload reads.

    Returns:
        :class:`PackageGraph` with parts, relationships, slide order and issues.
    """
    limits = limits or ResourceLimits()
    path = Path(path)
    if not path.is_file():
        raise InputError("PACKAGE_NOT_FOUND", f"file not found: {path}", artifact_paths=[path])

    issues: list[Issue] = []
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise InputError(
            "PACKAGE_NOT_A_ZIP",
            f"not a valid PPTX/ZIP file: {path} ({exc})",
            artifact_paths=[path],
        ) from exc

    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > limits.max_zip_parts:
            raise InputError(
                "PACKAGE_TOO_MANY_PARTS",
                f"ZIP has {len(infos)} entries, limit {limits.max_zip_parts}",
                artifact_paths=[path],
            )
        total = sum(i.file_size for i in infos)
        if total > limits.max_total_uncompressed_bytes:
            raise InputError(
                "PACKAGE_TOO_LARGE",
                f"uncompressed size {total} exceeds limit {limits.max_total_uncompressed_bytes}",
                artifact_paths=[path],
            )
        names = {normalize_part_name(i.filename) for i in infos}
        if CONTENT_TYPES not in names:
            raise InputError(
                "PACKAGE_CONTENT_TYPES_MISSING",
                f"missing {CONTENT_TYPES}",
                artifact_paths=[path],
            )

        raw: dict[str, bytes] = {}
        for info in infos:
            name = normalize_part_name(info.filename)
            if name.startswith("_rels/") and name.endswith(".rels"):
                continue  # handled as relationship parts below
            try:
                raw[name] = zf.read(info)
            except Exception as exc:  # noqa: BLE001 - archive read failure is reportable
                issues.append(
                    Issue(
                        rule_id="OPC_PART_READ",
                        code="PART_READ_FAILED",
                        severity=Severity.ERROR,
                        details=f"{name}: {exc}",
                        evidence=[name],
                        repairable=False,
                    )
                )

        content_types = _parse_content_types(raw.pop(CONTENT_TYPES), issues)
        content_asset = store.put(
            zf.read(CONTENT_TYPES), "application/xml", source_ref=str(path)
        )

        # Relationship parts (zip paths like "ppt/slides/_rels/slide1.xml.rels").
        rels_raw: dict[str, bytes] = {}
        for info in infos:
            name = normalize_part_name(info.filename)
            if name.endswith(".rels"):
                rels_raw[name] = zf.read(info)

    parts: dict[str, PartIR] = {}
    outgoing: dict[str, list[RelationshipIR]] = {}
    incoming: dict[str, list[RelationshipIR]] = {}

    # Map rels file -> owner part.
    owner_for_rels: dict[str, str] = {}
    for rels_name in rels_raw:
        directory, base = posixpath.split(rels_name)
        # directory ends with "_rels"; owner lives one level up
        parent_dir = posixpath.dirname(directory)
        owner = posixpath.join(parent_dir, base[: -len(".rels")])
        owner_for_rels[rels_name] = owner

    all_rels: list[RelationshipIR] = []
    for rels_name, data in sorted(rels_raw.items()):
        owner = owner_for_rels[rels_name]
        all_rels.extend(_parse_relationships(data, owner, issues))

    # Root relationships.
    root_rels: list[RelationshipIR] = []
    if ROOT_RELS in rels_raw:
        root_rels = _parse_relationships(rels_raw[ROOT_RELS], "", issues)
    elif ROOT_RELS not in rels_raw:
        issues.append(
            Issue(
                rule_id="OPC_ROOT_RELS",
                code="ROOT_RELS_MISSING",
                severity=Severity.WARNING,
                details="_rels/.rels not found",
                evidence=[ROOT_RELS],
                repairable=False,
            )
        )

    for name, data in sorted(raw.items()):
        media_type = (
            "application/xml" if name.endswith((".xml", ".rels")) else "application/octet-stream"
        )
        if not name.endswith((".xml", ".rels")):
            ext = name.rsplit(".", 1)[-1].lower()
            media_type = {
                "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "gif": "image/gif", "svg": "image/svg+xml", "emf": "image/x-emf",
                "wmf": "image/x-wmf", "xlsx": (
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                ),
                "bin": "application/octet-stream",
            }.get(ext, "application/octet-stream")
        asset = store.put(data, media_type, source_ref=f"{path.name}!{name}")
        parts[name] = PartIR(
            part_name=name,
            content_type=_content_type_for(name, content_types),
            asset=asset,
            relationships=[],
        )

    # Attach relationships to their owners (owners may be parts or the package).
    dangling: list[tuple[str, RelationshipIR]] = []
    for rel in all_rels:
        if rel.owner_part == "":
            continue
        if (
            rel.target_mode == "internal"
            and rel.resolved_part
            and rel.resolved_part not in parts
        ):
            dangling.append((rel.owner_part, rel))
        (outgoing.setdefault(rel.owner_part, [])).append(rel)
        if rel.resolved_part and rel.target_mode == "internal":
            incoming.setdefault(rel.resolved_part, []).append(rel)
        if rel.owner_part in parts:
            parts[rel.owner_part].relationships.append(rel)

    for owner, rel in dangling:
        issues.append(
            Issue(
                rule_id="OPC_DANGLING_REL",
                code="REL_DANGLING_TARGET",
                severity=Severity.ERROR,
                details=f"{owner}: {rel.rel_id} -> {rel.target} not found in package",
                evidence=[owner, rel.rel_id, rel.target],
                repairable=False,
            )
        )

    # Content types coverage.
    for name in parts:
        covered = name in content_types or any(
            k.startswith("*.") and name.endswith(k[1:]) for k in content_types
        )
        if not covered:
            issues.append(
                Issue(
                    rule_id="OPC_CONTENT_TYPE_MISSING",
                    code="PART_CONTENT_TYPE_MISSING",
                    severity=Severity.WARNING,
                    details=f"no content type declared for {name}",
                    evidence=[name],
                    repairable=False,
                )
            )

    graph = PackageGraph(
        parts_by_name=parts,
        outgoing=outgoing,
        incoming=incoming,
        root_relationships=root_rels,
        slide_order=[],
        content_types=content_types,
        content_types_asset=content_asset,
        issues=issues,
        source_path=path,
        source_sha256=sha256_bytes(path.read_bytes()),
    )
    graph.slide_order = _slide_order(graph, issues)
    graph.issues = issues
    return graph


def _slide_order(graph: PackageGraph, issues: list[Issue]) -> list[str]:
    """Initial slide part list from presentation relationships.

    The authoritative order comes from ``sldIdLst`` and is refined by
    :func:`slide_part_order` inside ``import_deck``.
    """
    pres_part = None
    for rel in graph.root_relationships:
        if rel.rel_type == REL_OFFICE_DOC and rel.resolved_part:
            pres_part = rel.resolved_part
            break
    if pres_part is None and "ppt/presentation.xml" in graph.parts_by_name:
        pres_part = "ppt/presentation.xml"
    if pres_part is None:
        issues.append(
            Issue(
                rule_id="OPC_PRESENTATION",
                code="PRESENTATION_PART_MISSING",
                severity=Severity.ERROR,
                details="presentation part not found via root relationships",
                evidence=[],
                repairable=False,
            )
        )
        return []
    ordered = [
        rel.resolved_part
        for rel in graph.outgoing.get(pres_part, [])
        if rel.rel_type.endswith("/slide") and rel.resolved_part
    ]
    return ordered


def build_manifest(graph: PackageGraph) -> PackageManifest:
    """Build the serializable package manifest from a graph (spec §7.8)."""
    return PackageManifest(
        parts=list(graph.parts_by_name.values()),
        root_relationships=graph.root_relationships,
        slide_order=graph.slide_order,
        content_types_asset=graph.content_types_asset,
    )


def slide_part_order(graph: PackageGraph, presentation_xml: bytes) -> list[str]:
    """Resolve exact slide order from ``sldIdLst`` + relationships (§10.1)."""
    try:
        root = etree.fromstring(presentation_xml, parser=_XML_PARSER)
    except etree.XMLSyntaxError:
        return list(graph.slide_order)
    rel_by_id = {r.rel_id: r for r in graph.outgoing.get("ppt/presentation.xml", [])}
    ordered: list[str] = []
    sld_id_lst = root.find(f"{{{PML}}}sldIdLst")
    if sld_id_lst is None:
        return list(graph.slide_order)
    for node in sld_id_lst.findall(f"{{{PML}}}sldId"):
        rid = node.get(f"{{{R_NS}}}id")
        if not rid:
            continue
        rel = rel_by_id.get(rid)
        if rel and rel.resolved_part:
            ordered.append(rel.resolved_part)
    return ordered or list(graph.slide_order)
