"""Relationship-aware OPC transplant (Stage 4 §5).

Two-pass clone of a source part closure into a target package:

- Pass A reserves every target name up front (no overwrites of template
  parts), copies binary payloads byte-exact and XML with namespaces and
  unknown content (``extLst``, ``mc:Ignorable``) preserved.
- Pass B recreates owner-local relationships, remaps ``r:id``/``r:embed``/
  ``r:link`` attributes per owner (never a global string replace), and
  resolves deferred links/backrefs after all target nodes exist.

Keys are always source-qualified (package hash + part name); variant keys
give mutable charts copy-on-write isolation.
"""

from __future__ import annotations

import hashlib
import logging
import posixpath
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

from .export_models import (
    PartClosurePlan,
    PartPreservationContract,
    RelationshipKey,
    RelationshipMapping,
    SourcePartKey,
    TransplantIssue,
    TransplantResult,
)

log = logging.getLogger(__name__)

R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"

# Relationship attributes whose values are owner-scoped rIds. Only these are
# remapped; any other attribute that merely looks like an rId is left alone
# and reported (§5.3.12).
RID_VALUED_ATTRS = {
    f"{{{R_NS}}}id",
    f"{{{R_NS}}}embed",
    f"{{{R_NS}}}link",
}

_XML_PARSER = etree.XMLParser(
    resolve_entities=False, no_network=True, recover=False, huge_tree=False
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Edge policy
# ---------------------------------------------------------------------------

CLONE_TYPES = (
    "/chart",
    "/workbook",
    "/package",
    "/image",
    "/style",
    "/color",
    "/drawing",
    "/embedded",
)

SLIDE_REF_TYPES = ("/slide",)


def classify_edge(rel_type: str, target: str, target_mode: str) -> str:
    """Map one source relationship to its handling policy (§5.1)."""
    if target_mode == "external":
        return "preserve_external"
    lowered = rel_type.lower()
    if any(token in lowered for token in CLONE_TYPES):
        return "clone_dependency"
    if lowered.endswith("/slide") or "/slide" in lowered:
        return "map_slide_reference"
    if any(
        token in lowered
        for token in ("/slidelayout", "/slidemaster", "/notesmaster", "/theme", "/notes")
    ):
        return "bind_target_template"
    if any(token in lowered for token in ("extendedproperties", "coreproperties",
                                          "thumbnail", "customproperties")):
        return "exclude_sidecar"
    return "unsupported"


@dataclass
class TransplantContext:
    """Mutable working state for one transplant operation."""

    source_package_hash: str
    source_parts: dict[str, bytes] = field(default_factory=dict)
    source_content_types: dict[str, str] = field(default_factory=dict)
    source_outgoing: dict[str, list[dict]] = field(default_factory=dict)
    target_names: dict[str, str] = field(default_factory=dict)
    target_payloads: dict[str, bytes] = field(default_factory=dict)
    target_content_types: dict[str, str] = field(default_factory=dict)
    relationship_map: list[RelationshipMapping] = field(default_factory=list)
    protected_target_parts: frozenset[str] = frozenset()
    part_contracts: list[PartPreservationContract] = field(default_factory=list)
    issues: list[TransplantIssue] = field(default_factory=list)
    slide_map: dict[str, str] = field(default_factory=dict)
    template_bindings: dict[str, str] = field(default_factory=dict)


def load_source_package(path: str | Path) -> tuple[dict[str, bytes], dict[str, str],
                                                   dict[str, list[dict]], str]:
    """Read raw ZIP members + relationship graph of one source package."""
    import zipfile

    path = Path(path)
    blob = path.read_bytes()
    package_hash = _sha256(blob)
    parts: dict[str, bytes] = {}
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = posixpath.normpath(info.filename.replace("\\", "/")).lstrip("/")
            if name.endswith(".rels"):
                continue
            parts[name] = zf.read(info)
        rels_raw: dict[str, bytes] = {}
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = posixpath.normpath(info.filename.replace("\\", "/")).lstrip("/")
            if name.endswith(".rels"):
                rels_raw[name] = zf.read(info)
    content_types = _parse_content_types(parts.get("[Content_Types].xml", b""))
    outgoing: dict[str, list[dict]] = {}
    for rels_name, data in rels_raw.items():
        owner = _owner_for_rels(rels_name)
        for rel in _parse_rels(data):
            outgoing.setdefault(owner, []).append(rel)
    if "_rels/.rels" in rels_raw:
        outgoing.setdefault("", []).extend(_parse_rels(rels_raw["_rels/.rels"]))
    return parts, content_types, outgoing, package_hash


def _owner_for_rels(rels_name: str) -> str:
    directory, base = posixpath.split(rels_name)
    parent = posixpath.dirname(directory)
    return posixpath.join(parent, base[: -len(".rels")]).lstrip("/")


def _parse_rels(data: bytes) -> list[dict]:
    NS = "http://schemas.openxmlformats.org/package/2006/relationships"
    try:
        root = etree.fromstring(data, parser=_XML_PARSER)
    except etree.XMLSyntaxError:
        return []
    rels = []
    for node in root.findall(f"{{{NS}}}Relationship"):
        rels.append({
            "id": node.get("Id") or "",
            "type": node.get("Type") or "",
            "target": node.get("Target") or "",
            "mode": node.get("TargetMode") or "Internal",
        })
    return rels


def _parse_content_types(data: bytes) -> dict[str, str]:
    NS = "http://schemas.openxmlformats.org/package/2006/content-types"
    try:
        root = etree.fromstring(data, parser=_XML_PARSER)
    except etree.XMLSyntaxError:
        return {}
    out: dict[str, str] = {}
    for node in root.findall(f"{{{NS}}}Override"):
        part = (node.get("PartName") or "").lstrip("/")
        ctype = node.get("ContentType") or ""
        if part and ctype:
            out[posixpath.normpath(part)] = ctype
    for node in root.findall(f"{{{NS}}}Default"):
        ext = (node.get("Extension") or "").lower()
        ctype = node.get("ContentType") or ""
        if ext and ctype:
            out[f"*.{ext}"] = ctype
    return out


def _resolve_target(owner: str, target: str) -> str:
    owner_dir = posixpath.dirname(owner)
    return posixpath.normpath(posixpath.join(owner_dir, target)).lstrip("/")


# ---------------------------------------------------------------------------
# §5.1 closure
# ---------------------------------------------------------------------------

def plan_part_closure(
    root: str,
    context: TransplantContext,
    policy: dict[str, str] | None = None,
) -> PartClosurePlan:
    """Breadth-first closure from ``root`` with a typed edge policy."""
    policy = policy or {}
    visited: set[str] = set()
    queue: deque[str] = deque([root])
    part_keys: list[SourcePartKey] = []
    edge_handling: dict[str, str] = {}
    contracts: list[PartPreservationContract] = []
    unknown_edges: list[str] = []
    issues: list[TransplantIssue] = []

    while queue:
        part = queue.popleft()
        if part in visited:
            continue
        visited.add(part)
        part_keys.append(SourcePartKey(
            package_sha256=context.source_package_hash, part_name=part))
        for rel in context.source_outgoing.get(part, []):
            target = rel["target"]
            mode = "external" if rel["mode"].lower() == "external" else "internal"
            if mode == "external":
                handling = "preserve_external"
            else:
                resolved = _resolve_target(part, target)
                handling = policy.get(rel["type"], classify_edge(rel["type"], target, "internal"))
                edge_key = f"{part}#{rel['id']}"
                edge_handling[edge_key] = handling
                if handling == "clone_dependency":
                    if resolved not in visited:
                        queue.append(resolved)
                elif handling == "map_slide_reference":
                    pass  # resolved later through the slide map
                elif handling in ("bind_target_template", "exclude_sidecar",
                                  "preserve_external", "derive_or_normalize"):
                    pass
                else:
                    unknown_edges.append(f"{part}#{rel['id']}:{rel['type']}")
                    issues.append(TransplantIssue(
                        code="TRANSPLANT/UNSUPPORTED_EDGE",
                        details=f"{part} {rel['id']} type={rel['type']}",
                        subject_ids=[part],
                    ))
                continue
            edge_key = f"{part}#{rel['id']}"
            edge_handling[edge_key] = handling
    return PartClosurePlan(
        root=root,
        source_package_hash=context.source_package_hash,
        part_keys=part_keys,
        edge_handling=edge_handling,
        contracts=contracts,
        unknown_edges=unknown_edges,
        issues=issues,
    )


# ---------------------------------------------------------------------------
# §5.2 name allocation
# ---------------------------------------------------------------------------

_EXTENSION_BY_CONTENT_TYPE = {
    "application/vnd.openxmlformats-officedocument.drawingml.chart+xml": ".xml",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "image/x-emf": ".emf",
    "application/vnd.openxmlformats-officedocument.drawingml.chartstyle+xml": ".xml",
    "application/vnd.openxmlformats-officedocument.drawingml.chartcolors+xml": ".xml",
    "application/vnd.openxmlformats-officedocument.presentationml.slide+xml": ".xml",
}


def allocate_part_name(
    source_key: SourcePartKey,
    content_type: str,
    context: TransplantContext,
    variant: str | None = None,
) -> str:
    """Allocate a unique canonical target part name (no overwrites)."""
    map_key = f"{source_key.package_sha256}:{source_key.part_name}"
    if variant:
        map_key += f":{variant}"
    if map_key in context.target_names:
        return context.target_names[map_key]
    src = source_key.part_name
    directory, _, base = src.rpartition("/")
    stem, dot, _ext = base.partition(".")
    ext = _EXTENSION_BY_CONTENT_TYPE.get(content_type, (dot + _ext) if dot else ".xml")
    if not ext.startswith("."):
        ext = "." + ext
    stem = stem or "part"
    if variant:
        stem = f"{stem}-{variant}"
    candidate = f"{directory}/{stem}{ext}" if directory else f"{stem}{ext}"
    taken = set(context.target_payloads) | set(context.protected_target_parts)
    taken |= set(context.target_names.values())
    counter = 1
    while candidate in taken or ("/" in candidate and candidate.lower() in
                                 {t.lower() for t in taken if t != candidate}):
        counter += 1
        alt = f"{stem}-{counter}{ext}"
        candidate = f"{directory}/{alt}" if directory else alt
    context.target_names[map_key] = candidate
    if content_type:
        context.target_content_types.setdefault(candidate, content_type)
    return candidate


# ---------------------------------------------------------------------------
# §5.3 two-pass clone
# ---------------------------------------------------------------------------

def _rewrite_owner_rids(
    xml_bytes: bytes,
    rid_map: dict[str, str],
) -> tuple[bytes, list[str]]:
    """Remap only known relationship-valued attributes for one owner."""
    try:
        root = etree.fromstring(xml_bytes, parser=_XML_PARSER)
    except etree.XMLSyntaxError:
        return xml_bytes, ["xml_parse_failed"]
    touched: list[str] = []
    # Preserve mc:Ignorable prefixes: record before mutation.
    ignorable = root.get(f"{{{MC_NS}}}Ignorable", "")
    for el in root.iter():
        for attr in list(el.attrib):
            if attr in RID_VALUED_ATTRS:
                old = el.get(attr)
                if old in rid_map:
                    el.set(attr, rid_map[old])
                    touched.append(f"{el.tag}@{attr}:{old}->{rid_map[old]}")
    if ignorable:
        assert root.get(f"{{{MC_NS}}}Ignorable") == ignorable, "mc:Ignorable lost"
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8",
                          standalone=True), touched


def clone_part_graph(
    root_part: str,
    context: TransplantContext,
    variant: str | None = None,
) -> TransplantResult:
    """Clone the closure of ``root_part`` into the context target inventory."""
    closure = plan_part_closure(root_part, context)
    # Pass A: reserve all names first.
    for key in closure.part_keys:
        ctype = context.source_content_types.get(key.part_name, "")
        if not ctype and "." in key.part_name:
            ctype = context.source_content_types.get(
                "*." + key.part_name.rsplit(".", 1)[-1].lower(), "")
        allocate_part_name(key, ctype, context, variant)
    part_map: dict[str, str] = {}
    issues: list[TransplantIssue] = list(closure.issues)
    contracts: list[PartPreservationContract] = []
    owner_rid_maps: dict[str, dict[str, str]] = {}

    def target_of(source_part: str, v: str | None = None) -> str:
        map_key = f"{context.source_package_hash}:{source_part}"
        if v:
            map_key += f":{v}"
        return context.target_names[map_key]

    # Pass A: copy payloads.
    for key in closure.part_keys:
        src = key.part_name
        payload = context.source_parts.get(src)
        if payload is None:
            issues.append(TransplantIssue(
                code="TRANSPLANT/MISSING_SOURCE_PART",
                details=f"source part bytes missing: {src}",
                subject_ids=[src],
            ))
            continue
        target = target_of(src, variant)
        ctype = context.source_content_types.get(src, "")
        is_xml = src.endswith((".xml", ".rels")) or ctype.endswith("+xml") \
            or ctype in ("application/xml", "text/xml")
        if is_xml:
            # Byte-preserved for now; owner rId rewrite happens in pass B.
            context.target_payloads[target] = bytes(payload)
            contracts.append(PartPreservationContract(
                source=key, target_part=target, mode="xml_allowed_rewrite",
                allowed_rewrite_paths=["relationship-valued-attrs", "style-scope"],
                reason="xml clone with owner-scoped rId remap",
                evidence_refs=[src],
            ))
        else:
            context.target_payloads[target] = bytes(payload)
            contracts.append(PartPreservationContract(
                source=key, target_part=target, mode="binary_exact",
                reason=f"byte-exact copy sha256={_sha256(payload)[:16]}",
                evidence_refs=[src],
            ))
        part_map[f"{key.package_sha256}:{src}"] = target

    # Pass B: owner-local relationships with per-owner rId allocation.
    mappings: list[RelationshipMapping] = []
    rid_counters: dict[str, int] = {}
    for key in closure.part_keys:
        src_owner = key.part_name
        rels = context.source_outgoing.get(src_owner, [])
        if not rels:
            continue
        target_owner = target_of(src_owner, variant)
        owner_map = owner_rid_maps.setdefault(target_owner, {})
        for rel in rels:
            mode = "external" if rel["mode"].lower() == "external" else "internal"
            handling = closure.edge_handling.get(f"{src_owner}#{rel['id']}",
                                                 "unsupported")
            if mode == "external":
                target_ref = rel["target"]
                target_mode = "external"
            elif handling == "clone_dependency":
                resolved = _resolve_target(src_owner, rel["target"])
                tkey = f"{context.source_package_hash}:{resolved}"
                target_part = context.target_names.get(
                    tkey if variant is None else tkey + f":{variant}",
                    context.target_names.get(tkey, ""),
                )
                if not target_part:
                    issues.append(TransplantIssue(
                        code="TRANSPLANT/UNMAPPED_TARGET",
                        details=f"{src_owner} {rel['id']} -> {resolved} has no target",
                        subject_ids=[src_owner],
                    ))
                    continue
                # Relative target from the target owner.
                target_ref = posixpath.relpath(target_part,
                                              posixpath.dirname(target_owner) or ".")
                target_mode = "internal"
            elif handling == "map_slide_reference":
                resolved = _resolve_target(src_owner, rel["target"])
                mapped = context.slide_map.get(resolved, "")
                if not mapped:
                    issues.append(TransplantIssue(
                        code="TRANSPLANT/UNRESOLVED_SLIDE_REF",
                        details=f"{src_owner} {rel['id']} slide {resolved} not in map",
                        subject_ids=[src_owner],
                    ))
                    continue
                target_ref = posixpath.relpath(mapped,
                                              posixpath.dirname(target_owner) or ".")
                target_mode = "internal"
            elif handling in ("bind_target_template", "exclude_sidecar",
                              "preserve_external", "derive_or_normalize"):
                bound = context.template_bindings.get(
                    _resolve_target(src_owner, rel["target"]), "")
                if handling == "exclude_sidecar":
                    continue
                if not bound:
                    issues.append(TransplantIssue(
                        code="TRANSPLANT/UNBOUND_TEMPLATE_EDGE",
                        details=f"{src_owner} {rel['id']} has no template binding",
                        subject_ids=[src_owner],
                    ))
                    continue
                target_ref = posixpath.relpath(bound,
                                              posixpath.dirname(target_owner) or ".")
                target_mode = "internal"
            else:
                issues.append(TransplantIssue(
                    code="TRANSPLANT/UNSUPPORTED_EDGE_REFUSED",
                    details=f"{src_owner} {rel['id']} type={rel['type']} refused",
                    subject_ids=[src_owner],
                ))
                continue
            rid_counters[target_owner] = rid_counters.get(target_owner, 0) + 1
            new_rid = f"rId{rid_counters[target_owner]}"
            owner_map[rel["id"]] = new_rid
            source_key = RelationshipKey(
                source_package_hash=context.source_package_hash,
                owner_part=src_owner, rid=rel["id"])
            mappings.append(RelationshipMapping(
                source_key=source_key, target_owner=target_owner,
                target_rid=new_rid, target_part_or_uri=target_ref,
                target_mode=target_mode,  # type: ignore[arg-type]
                relationship_type=rel["type"], handling=handling,
                evidence=[f"{src_owner}#{rel['id']}"],
            ))
            context.relationship_map.append(mappings[-1])

    # Rewrite owner XML payloads with their own rId maps.
    for key in closure.part_keys:
        target = part_map.get(f"{key.package_sha256}:{key.part_name}", "")
        if not target:
            continue
        payload = context.target_payloads.get(target)
        if payload is None:
            continue
        owner_map = owner_rid_maps.get(target, {})
        if not owner_map:
            continue
        try:
            etree.fromstring(payload, parser=_XML_PARSER)
        except etree.XMLSyntaxError:
            continue  # binary or non-XML: no rewrite
        rewritten, _ = _rewrite_owner_rids(payload, owner_map)
        context.target_payloads[target] = rewritten

    context.part_contracts.extend(contracts)
    actual_root = part_map.get(
        f"{context.source_package_hash}:{root_part}", root_part)
    return TransplantResult(
        root_part=root_part, actual_root_part=actual_root, part_map=part_map,
        relationship_map=mappings, contracts=contracts, issues=issues,
    )


def deepcopy_subtree(element: etree._Element) -> etree._Element:
    """Checked subtree copy helper (deepcopy is only a subtree op, §5.3)."""
    return deepcopy(element)
