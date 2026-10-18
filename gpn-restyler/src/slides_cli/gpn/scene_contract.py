"""Semantic/zone/source namespaces at the actual authoring boundary.

No corporate tokens are invented here. A zone controls a logical target
block; its source occurrences and per-reference styles are lowered once.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def _get(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def scene_token_errors(scene: Any, rules: Any) -> list[str]:
    """Check nested roles/colors before costly measurement and native build."""
    roles = set(_get(rules, "roles", {}) or {})
    colors = set(_get(rules, "colors", {}) or {})
    conditional = _get(rules, "conditional_colors", {}) or {}
    colors.update(
        conditional
        if isinstance(conditional, dict)
        else (_get(c, "token", "") for c in conditional)
    )
    errors: list[str] = []
    if not roles:
        return ["ONTOLOGY_ROLE_SET_EMPTY: no loaded authoring roles"]
    for zone in _get(scene, "zones", []) or []:
        zid = str(_get(zone, "zone_id", ""))
        role = str(_get(zone, "role", "") or "")
        if role and role not in roles:
            errors.append(f"STYLE_ROLE_UNBOUND: zone {zid}: {role!r}; allowed={sorted(roles)}")
        refs = set(_get(zone, "content_refs", []) or [])
        for ref, role in (_get(zone, "ref_roles", {}) or {}).items():
            if ref not in refs:
                errors.append(f"REF_ROLE_UNBOUND: zone {zid}: {ref!r} is not a zone content ref")
            if role not in roles:
                errors.append(
                    f"STYLE_ROLE_UNBOUND: zone {zid}/{ref}: {role!r}; allowed={sorted(roles)}"
                )
        for field in ("surface_token", "foreground_token", "stroke_token"):
            token = str(_get(zone, field, "") or "")
            if token and token not in colors:
                errors.append(f"COLOR_TOKEN_UNBOUND: zone {zid}: {field}={token!r}")
    return errors


def lower_scene_plan(
    plan: dict[str, Any],
    semantic_slide: Any,
    source_object_ids: set[str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Translate semantic refs and zone order into the legacy native contract.

    Unknown refs, duplicate ownership, and conflicting styles fail visibly.
    A semantic relation NEVER grants a geometric overlap permission.
    """
    out = deepcopy(plan)
    units: dict[str, list[str]] = {}
    for unit in _get(semantic_slide, "units", []) or []:
        refs = [str(_get(r, "object_id", "") or "") for r in _get(unit, "source_refs", []) or []]
        units[str(_get(unit, "id", ""))] = list(dict.fromkeys(r for r in refs if r))
    known = (
        set(source_object_ids)
        if source_object_ids is not None
        else {ref for refs in units.values() for ref in refs}
    )
    errors: set[str] = set()

    def expand(ref: str) -> list[str]:
        if ref in units:
            values = units[ref]
            if not values:
                errors.add(f"SEMANTIC_REF_HAS_NO_SOURCE:{ref}")
            errors.update(v for v in values if v not in known)
            return [v for v in values if v in known]
        if ref in known:
            return [ref]
        errors.add(ref or "EMPTY_CONTENT_REF")
        return []

    zones = [dict(z) for z in out.get("zones", []) if isinstance(z, dict)]
    by_zone: dict[str, dict[str, Any]] = {}
    for z in zones:
        zid = str(z.get("zone_id", ""))
        if not zid or zid in by_zone:
            errors.add(f"ZONE_ID_DUPLICATE_OR_EMPTY:{zid}")
        by_zone[zid] = z
    authored_order = [str(v) for v in out.get("reading_order", []) or []]
    if authored_order and all(v in by_zone for v in authored_order):
        if len(set(authored_order)) != len(authored_order):
            errors.add("READING_ORDER_DUPLICATE_ZONE")
        ordered = list(dict.fromkeys(authored_order))
        ordered.extend(zid for zid in by_zone if zid not in ordered)
        zones = [by_zone[zid] for zid in ordered]
    elif authored_order:
        # Explicit legacy source-order input remains readable; no substring inference.
        for ref in authored_order:
            if ref not in known and ref not in units:
                errors.add(f"READING_ORDER_UNKNOWN_NODE:{ref}")

    owners: dict[str, str] = {}
    expanded_zones: list[dict[str, Any]] = []
    for z in zones:
        zid = str(z.get("zone_id", ""))
        raw_refs = [str(r) for r in z.get("content_refs", []) or []]
        refs = list(dict.fromkeys(v for r in raw_refs for v in expand(r)))
        roles: dict[str, str] = {}
        for ref, role in dict(z.get("ref_roles", {}) or {}).items():
            if str(ref) not in raw_refs:
                errors.add(f"REF_ROLE_OUTSIDE_ZONE:{zid}:{ref}")
            for source_ref in expand(str(ref)):
                if source_ref in roles and roles[source_ref] != str(role):
                    errors.add(f"ROLE_BINDING_CONFLICT:{zid}:{source_ref}")
                roles[source_ref] = str(role)
        for ref in refs:
            if ref in owners and owners[ref] != zid:
                errors.add(f"SOURCE_OWNERSHIP_DUPLICATE:{ref}:{owners[ref]}:{zid}")
            owners[ref] = zid
        groups = z.get("paragraph_groups", []) or []
        if groups:
            lowered_groups = [
                list(dict.fromkeys(v for ref in group for v in expand(str(ref))))
                for group in groups
            ]
            flat = [ref for group in lowered_groups for ref in group]
            if set(flat) != set(refs) or len(flat) != len(set(flat)):
                errors.add(f"TEXT_FLOW_COVERAGE_MISMATCH:{zid}")
            z["paragraph_groups"] = lowered_groups
        z["content_refs"], z["ref_roles"] = refs, roles
        if refs:
            expanded_zones.append(z)
    out["zones"] = expanded_zones
    zone_sources = {str(z["zone_id"]): z["content_refs"] for z in expanded_zones}
    ordered_sources = [ref for z in expanded_zones for ref in z["content_refs"]]
    # Internal compile_composition expects source-object order, not zone IDs.
    if authored_order and all(v in known or v in units for v in authored_order):
        supplied = list(dict.fromkeys(v for r in authored_order for v in expand(r)))
        ordered_sources = supplied + [r for r in ordered_sources if r not in supplied]
    out["content_refs"] = ordered_sources
    out["reading_order"] = list(ordered_sources)
    dominant = str(out.get("dominant_ref", "") or "")
    dominant_refs = (
        zone_sources.get(dominant)
        if dominant in by_zone
        else (expand(dominant) if dominant else [])
    )
    if dominant and not dominant_refs:
        errors.add(f"DOMINANT_REF_EMPTY:{dominant}")
    out["dominant_ref"] = dominant_refs[0] if dominant_refs else ""

    for relation in out.get("relations", []) or []:
        for key in ("from_ref", "to_ref"):
            ref = str(relation.get(key, ""))
            values = expand(ref)
            if values:
                relation[key] = values[0]

    permissions: list[dict[str, Any]] = []
    for raw in out.get("overlap_permissions", []) or []:
        permission = dict(raw)
        underlay = expand(str(permission.get("underlay_id", "")))
        content = expand(str(permission.get("content_id", "")))
        if len(underlay) != 1:
            errors.add("OVERLAP_UNDERLAY_AMBIGUOUS")
            continue
        for child in content:
            permissions.append({**permission, "underlay_id": underlay[0], "content_id": child})
    out["overlap_permissions"] = permissions
    permitted = {frozenset((p["underlay_id"], p["content_id"])) for p in permissions}
    explicit_pairs: list[list[str]] = []
    for pair in out.get("allowed_overlaps", []) or []:
        if len(pair) != 2:
            errors.add("ALLOWED_OVERLAP_NOT_A_PAIR")
            continue
        for first in expand(str(pair[0])):
            for second in expand(str(pair[1])):
                if frozenset((first, second)) not in permitted:
                    errors.add(f"OVERLAP_PERMISSION_MISSING:{first}:{second}")
                explicit_pairs.append([first, second])
    out["allowed_overlaps"] = explicit_pairs
    return out, sorted(errors)
