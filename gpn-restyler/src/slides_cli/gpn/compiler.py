"""Ontology compiler (spec §6).

Compiles the normative ontology JSON into a CompiledOntology model with
roles, colors, font policy, list styles, catalog records, and rule registry.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .bindings import bind_rules, load_binding_library
from .errors import OntologySchemaError, OntologySourceError
from .models import (
    CatalogRecord,
    ColorCondition,
    CompiledOntology,
    ExtensionContract,
    FontForbiddenCombination,
    FontPolicyRule,
    Issue,
    ListLevelProfile,
    ListStyleProfile,
    MinimumFontSize,
    NestedSizePolicy,
    ParagraphSpacing,
    RectEMU,
    RoleStyle,
    RuleBinding,
    RuleRegistry,
    RuleSpec,
)
from .ontology import OntologySources, sha256_file

if TYPE_CHECKING:
    from ..model import NativeShapeStyle
    from .patching import StyleApplicationContext

log = logging.getLogger(__name__)

REQUIRED_ROOT_KEYS = {"metadata", "formal_model", "style_profile", "catalogs", "catalogue_rules"}
SCHEMA_ADAPTER_ID = "gpn-ontology-json/1.0"
COMPILER_VERSION = "stage3.5/1.0"


def _ontology_issue(
    code: str,
    severity: str,
    details: str,
    *,
    rule_id: str = "ONTOLOGY",
) -> Issue:
    """Compilation issue with the mandatory machine-readable rule id."""
    return Issue(
        rule_id=rule_id,
        code=code,
        severity=severity,  # type: ignore[arg-type]
        details=details,
    )


def _require_positive_int(value: Any, pointer: str) -> int:
    """Read a positive integer EMU/pt value, or raise a typed schema error.

    ``bool`` is rejected explicitly (it is an ``int`` subclass in Python) and
    there is no default: a missing normative value is an error, never a guess
    (§4.2, §12.1 test_missing_source_has_no_builtin_fallback).
    """
    if value is None:
        raise OntologySchemaError(
            "ONTOLOGY/MISSING",
            f"required normative value missing at {pointer}",
        )
    if isinstance(value, bool) or not isinstance(value, int):
        raise OntologySchemaError(
            "ONTOLOGY/UNSUPPORTED_SCHEMA",
            f"{pointer} must be an integer EMU value, got {type(value).__name__}",
        )
    if value <= 0:
        raise OntologySchemaError(
            "ONTOLOGY/UNSUPPORTED_SCHEMA",
            f"{pointer} must be > 0 EMU, got {value}",
        )
    return value


def _compile_canvas(canvas_data: Any) -> RectEMU:
    if not isinstance(canvas_data, dict):
        raise OntologySchemaError(
            "ONTOLOGY/UNSUPPORTED_SCHEMA",
            "/style_profile/canvas must be an object with width_emu/height_emu",
        )
    return RectEMU(
        x=0,
        y=0,
        w=_require_positive_int(
            canvas_data.get("width_emu"), "/style_profile/canvas/width_emu"),
        h=_require_positive_int(
            canvas_data.get("height_emu"), "/style_profile/canvas/height_emu"),
    )


def _compile_nested_size_policy(
    policy_data: Any, list_id: str
) -> tuple[NestedSizePolicy | None, list[Issue]]:
    """Load the structured nested-size policy with no built-in numbers.

    Every threshold/subtraction must be present in the JSON: 12/2/1/8 are not
    hardcoded anywhere in the engine (§4.4.6).
    """
    issues: list[Issue] = []
    if not isinstance(policy_data, dict):
        issues.append(_ontology_issue(
            code="ONTOLOGY/UNSUPPORTED_SCHEMA",
            severity="error",
            details=(
                f"list {list_id!r}: text.nested_size_policy must be an object "
                "with if_parent_gte/then_subtract/otherwise_subtract/minimum"
            ),
        ))
        return None, issues

    required = ("if_parent_gte", "then_subtract", "otherwise_subtract", "minimum")
    values: dict[str, float] = {}
    for key in required:
        raw = policy_data.get(key)
        if raw is None or isinstance(raw, bool) or not isinstance(raw, (int, float)):
            issues.append(_ontology_issue(
                code="ONTOLOGY/MISSING",
                severity="error",
                details=(
                    f"list {list_id!r}: nested_size_policy.{key} is missing or "
                    "not numeric; no default value may be substituted"
                ),
            ))
            return None, issues
        values[key] = float(raw)

    if values["then_subtract"] <= 0 or values["otherwise_subtract"] <= 0:
        issues.append(_ontology_issue(
            code="ONTOLOGY/UNSUPPORTED_SCHEMA",
            severity="error",
            details=f"list {list_id!r}: nested size subtractions must be positive",
        ))
        return None, issues
    if values["minimum"] <= 0:
        issues.append(_ontology_issue(
            code="ONTOLOGY/UNSUPPORTED_SCHEMA",
            severity="error",
            details=f"list {list_id!r}: nested_size_policy.minimum must be > 0",
        ))
        return None, issues

    return NestedSizePolicy(
        if_parent_gte=values["if_parent_gte"],
        then_subtract=values["then_subtract"],
        otherwise_subtract=values["otherwise_subtract"],
        minimum=values["minimum"],
        parent_at_minimum_action=str(policy_data.get("parent_at_minimum_action", "")),
    ), issues


def _first_matching_color_rule(
    level: int, color_rules: list[Any]
) -> tuple[str, str | None]:
    """Evaluate structured marker color rules by priority for one level.

    Only structured conditions are executable. An unstructured (text) rule is
    not guessed: it leaves the level on the inherited paragraph-foreground
    branch and is reported by the binding report as unresolved.
    """
    mode = "follow_paragraph_text"
    token: str | None = None
    for rule in sorted(
        (r for r in color_rules if isinstance(r, dict)),
        key=lambda r: r.get("priority", 0),
    ):
        when = rule.get("when", {})
        if not isinstance(when, dict):
            continue
        if "level" in when and when.get("level") != level:
            continue
        level_min = when.get("level_min")
        if level_min is not None and level < int(level_min):
            continue
        rule_mode = str(rule.get("mode", "follow_paragraph_text"))
        mode = rule_mode
        token = str(rule.get("color_token")) if rule_mode == "fixed" else None
        break
    return mode, token


def compile_ontology(
    path: Path,
    template: Any = None,
    *,
    sources: OntologySources,
) -> CompiledOntology:
    """Compile the ontology at ``path`` into a CompiledOntology (spec §6.2)."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"ontology file not found: {path}")

    if sources.primary_json and path.resolve() != Path(sources.primary_json).resolve():
        raise ValueError(
            f"path {path} does not match sources.primary_json {sources.primary_json}"
        )

    content = path.read_text(encoding="utf-8")
    source_hash = sha256_file(path)

    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc

    issues: list[Issue] = []

    # Validate root keys
    missing_keys = REQUIRED_ROOT_KEYS - set(data.keys())
    if missing_keys:
        raise ValueError(f"missing required root keys: {missing_keys}")

    # Canvas — read from the loaded JSON only. There is no builtin corporate
    # size: a missing/invalid canvas is a typed schema error, never a guess
    # (§4.2, §12.1 test_missing_source_has_no_builtin_fallback).
    canvas_data = data.get("style_profile", {}).get("canvas", {})
    canvas = _compile_canvas(canvas_data)

    # Roles
    roles: dict[str, RoleStyle] = {}
    typography = data.get("style_profile", {}).get("typography", [])
    for index, role_data in enumerate(typography):
        if not isinstance(role_data, dict):
            continue
        role_name = role_data.get("role", "")
        if not role_name:
            continue
        if role_name in roles:
            issues.append(_ontology_issue(
                code="ONTOLOGY/DUPLICATE_ROLE",
                severity="error",
                details=(
                    f"typography[{index}] repeats role {role_name!r}; the "
                    "loaded collections must have unique ids"
                ),
            ))
            continue
        uppercase = role_data.get("uppercase")
        if not isinstance(uppercase, bool):
            if "uppercase" in role_data:
                issues.append(_ontology_issue(
                    code="ONTOLOGY/UNSUPPORTED_SCHEMA",
                    severity="error",
                    details=(
                        f"typography[{index}] ({role_name}) declares uppercase="
                        f"{uppercase!r}, which is not a boolean"
                    ),
                ))
            uppercase = None
            issues.append(_ontology_issue(
                code="ONTOLOGY/UPPERCASE_UNRESOLVED",
                severity="warning",
                details=(
                    f"role {role_name!r} does not declare uppercase; an absent "
                    "property is unresolved, not false"
                ),
            ))
        roles[role_name] = RoleStyle(
            role=role_name,
            allowed_faces=[role_data.get("font", "")] if role_data.get("font") else [],
            default_size_pt=role_data.get("size_pt"),
            allowed_sizes_pt=[role_data.get("size_pt")] if role_data.get("size_pt") else [],
            color_tokens=[role_data.get("color", "")] if role_data.get("color") else [],
            uppercase=uppercase,
            font_face_policy_id="default",
            applicability=str(role_data.get("context_ru", "")),
            status=role_data.get("status", "description_only"),  # type: ignore[arg-type]
            evidence_refs=[
                f"style_profile.typography[{index}]",
                *[f"style_profile.typography[{index}].evidence[{i}]"
                  for i in range(len(role_data.get("evidence") or []))],
            ],
        )

    # Colors
    colors: dict[str, str] = {}
    conditional_colors: dict[str, ColorCondition] = {}
    color_tokens = data.get("style_profile", {}).get("color_tokens", [])
    for token_data in color_tokens:
        if not isinstance(token_data, dict):
            continue
        token_id = token_data.get("id", "")
        value = token_data.get("value", "").lstrip("#")
        usage = token_data.get("usage", "default")
        if usage == "conditional":
            conditional_colors[token_id] = ColorCondition(
                token=token_id,
                rgb=value,
                allowed_context_ids=[str(s) for s in token_data.get("source_slides", [])],
                required_evidence_refs=[f"color_tokens.{token_id}"],
            )
        else:
            colors[token_id] = value

    # Font policy
    fsp = data.get("style_profile", {}).get("font_style_policy", {})
    font_policy: dict[str, Any] = {
        "rules": fsp.get("rules", []),
        "forbidden_combinations": fsp.get("forbidden_combinations", []),
        "theme_alias_policy": fsp.get("theme_alias_policy", ""),
    }

    font_policy_rules: list[FontPolicyRule] = []
    for rule_data in fsp.get("rules", []):
        if not isinstance(rule_data, dict):
            continue
        font_policy_rules.append(FontPolicyRule(
            role=str(rule_data.get("role", "")),
            typeface=str(rule_data.get("typeface", "")),
            bold=bool(rule_data.get("bold", False)),
            exception=bool(rule_data.get("exception", False)),
        ))

    font_forbidden: list[FontForbiddenCombination] = []
    for fc_data in fsp.get("forbidden_combinations", []):
        if not isinstance(fc_data, dict):
            continue
        replace_with = fc_data.get("replace_with", {})
        font_forbidden.append(FontForbiddenCombination(
            typeface=str(fc_data.get("typeface", "")),
            bold=bool(fc_data.get("bold", False)),
            replace_with_typeface=str(replace_with.get("typeface", "")),
            replace_with_bold=bool(replace_with.get("bold", False)),
        ))

    # Minimum text font size
    min_size_data = data.get("style_profile", {}).get("minimum_text_font_size", {})
    minimum_text_font_size: MinimumFontSize | None = None
    if isinstance(min_size_data, dict) and min_size_data.get("value_pt") is not None:
        try:
            minimum_text_font_size = MinimumFontSize(
                value_pt=float(min_size_data["value_pt"]),
                severity=str(min_size_data.get("severity", "hard")),
                applies_to=str(min_size_data.get("applies_to", "")),
                scope_note=str(min_size_data.get("scope_note", "")),
                source=str(min_size_data.get("source", "")),
                remedy=str(min_size_data.get("remedy", "")),
            )
        except (TypeError, ValueError):
            minimum_text_font_size = None

    # Composition operators
    composition_operators: list[str] = [
        str(op) for op in data.get("formal_model", {})
        .get("enums", {}).get("composition_operators", [])
        if isinstance(op, str)
    ]

    # List styles
    lists: dict[str, ListStyleProfile] = {}
    list_styles = data.get("style_profile", {}).get("list_styles", [])
    for ls_index, ls_data in enumerate(list_styles):
        if not isinstance(ls_data, dict):
            continue
        ls_id = ls_data.get("id", "")
        if not ls_id:
            continue
        if ls_id in lists:
            issues.append(_ontology_issue(
                code="ONTOLOGY/DUPLICATE_LIST_STYLE",
                severity="error",
                details=f"list_styles[{ls_index}] repeats id {ls_id!r}",
            ))
            continue
        marker = ls_data.get("marker", {})
        ooxml = marker.get("ooxml", {})
        color_rules = marker.get("color_rules", [])

        level_profiles: list[ListLevelProfile] = []
        text = ls_data.get("text", {})

        nested_policy_data = text.get("nested_size_policy")
        nested_policy = None
        if nested_policy_data is not None:
            nested_policy, policy_issues = _compile_nested_size_policy(
                nested_policy_data, ls_id
            )
            issues.extend(policy_issues)

        policy_desc = (
            f"nested_size_policy loaded from JSON: parent-{nested_policy.then_subtract} "
            f"if >={nested_policy.if_parent_gte} else parent-"
            f"{nested_policy.otherwise_subtract}, min {nested_policy.minimum}"
            if nested_policy else
            "nested_size_policy: unresolved in the loaded JSON; nested sizes are "
            "not derivable without it"
        )

        for level in range(9):
            # Level sizes are NOT precomputed here. The size algorithm needs
            # the real parent size of the paragraph being resolved, which
            # belongs to the source paragraph/role — not to the ontology
            # record. Only resolve_list_level() applies the loaded policy,
            # and an unresolved parent stays unresolved (§4.4.6, §4.4.7).
            marker_color_mode, marker_color_token = _first_matching_color_rule(
                level, color_rules
            )

            level_profiles.append(ListLevelProfile(
                level=level,
                size_pt=None,
                typeface=text.get("font_family"),
                text_token=text.get("primary_color"),
                marker_color_mode=marker_color_mode,
                marker_color_token=marker_color_token,
                mar_left_emu=None,
                hanging_indent_emu=None,
                spacing=ParagraphSpacing(),
                derived=True,
                derivation_rule=policy_desc,
            ))

        lists[ls_id] = ListStyleProfile(
            id=ls_id,
            representation=ls_data.get("representation", ""),
            marker_settings={
                "buChar": ooxml.get("buChar", ""),
                "buFont": ooxml.get("buFont", ""),
                "buSzPct": str(ooxml.get("buSzPct", "")),
                "buChar_unicode": ooxml.get("buChar_unicode", ""),
                "buFont_pitchFamily": ooxml.get("buFont_pitchFamily", ""),
                "buFont_charset": ooxml.get("buFont_charset", ""),
            },
            color_rules=[dict(cr) for cr in color_rules],
            level_profiles=level_profiles,
            exceptions=ls_data.get("scope_exceptions", []),
            evidence_refs=[f"style_profile.list_styles[{ls_index}]"],
            nested_size_policy=nested_policy,
            nested_text_color=text.get("nested_white_background_rgb"),
            nested_text_color_token=text.get("nested_white_background_color_token"),
            list_text_typeface=text.get("font_family"),
            list_text_primary_color=text.get("primary_color"),
        )

        if ooxml.get("buSzPct") in (None, ""):
            issues.append(_ontology_issue(
                code="ONTOLOGY/UNSUPPORTED_SCHEMA",
                severity="error",
                details=(
                    f"list_styles[{ls_index}] ({ls_id}) declares no "
                    "marker.ooxml.buSzPct; the marker relative size must not be "
                    "re-derived from an OOXML conversion"
                ),
            ))

    # Catalog records
    def _str_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return [str(v) for v in value]

    catalog_records: list[CatalogRecord] = []
    catalogs = data.get("catalogs", {})
    for kind, records in catalogs.items():
        if not isinstance(records, list):
            continue
        for rec in records:
            if not isinstance(rec, dict):
                continue
            catalog_records.append(CatalogRecord(
                id=str(rec.get("id", "")),
                kind=str(kind),
                name=str(rec.get("name", "")),
                semantic_features=_str_list(rec.get("semantic_features")),
                input_data=str(rec.get("input", "")),
                encoding=str(rec.get("encoding", "")),
                recipe=str(rec.get("recipe", "")),
                pitfalls=_str_list(rec.get("pitfalls")),
                provenance_status=rec.get("provenance_status", "unknown"),  # type: ignore[arg-type]
                source_refs=_str_list(rec.get("source_refs")),
                renderer_id=rec.get("renderer_id"),
                implementation_status="description_only",
            ))

    # Rule registry. Which checker exists for which rule is engine data kept
    # in the reviewed binding library (§5.1) — the compiler never declares a
    # closed set of corporate rule ids. A rule with no reviewed binding stays
    # ``needs_binding`` and therefore evaluates to an honest unknown.
    specs: dict[str, RuleSpec] = {}
    bindings: dict[str, RuleBinding] = {}

    reviewed_entries = load_binding_library().get("entries") or {}

    def _make_binding(cid: str, severity: str) -> RuleBinding:
        reviewed = reviewed_entries.get(cid)
        if not isinstance(reviewed, dict):
            return RuleBinding(
                rule_id=cid,
                checker_key="",
                check_kind="deterministic" if severity == "hard" else "hybrid",
                implementation_status="needs_binding",
                unresolved_reason=(
                    "no reviewed entry in the binding library; the rule stays "
                    "unknown until a checker is reviewed against its condition"
                ),
            )
        return RuleBinding(
            rule_id=cid,
            checker_key=str(reviewed.get("checker_key", "")),
            check_kind="deterministic" if severity == "hard" else "hybrid",
            supported_scopes=["source", "reference"],
            implementation_status=str(
                reviewed.get("implementation_status_at_review", "deferred")
            ),  # type: ignore[arg-type]
            required_evidence=list(reviewed.get("source_refs") or [cid]),
            deferred_stage=None,
        )

    constraints = data.get("formal_model", {}).get("constraints", [])
    for constraint in constraints:
        if not isinstance(constraint, dict):
            continue
        cid = constraint.get("id", "")
        if not cid:
            continue
        severity = constraint.get("severity", "hard")
        specs[cid] = RuleSpec(
            id=cid,
            severity=severity,  # type: ignore[arg-type]
            condition=constraint.get("condition", ""),
            remedy=constraint.get("remedy", ""),
            source_refs=[f"formal_model.constraints.{cid}"],
        )
        bindings[cid] = _make_binding(cid, str(severity))

    # Font style policy constraints (C21, C22 in the current corpus)
    fsp_constraints = fsp.get("constraints", [])
    for constraint in fsp_constraints:
        if not isinstance(constraint, dict):
            continue
        cid = constraint.get("id", "")
        if not cid:
            continue
        if cid not in specs:
            specs[cid] = RuleSpec(
                id=cid,
                severity="hard",
                condition=constraint.get("condition", ""),
                remedy=constraint.get("remedy", ""),
                source_refs=[f"font_style_policy.constraints.{cid}"],
            )
            bindings[cid] = _make_binding(cid, "hard")

    registry = RuleRegistry(
        specs=specs,
        bindings=bindings,
        registry_version="1.0",
    )

    # Attach condition fingerprints and reviewed-binding status (§5.1):
    # a changed condition behind the same ID never keeps a proven binding.
    bind_rules(registry.specs, registry.bindings)

    # Extension contract
    ext_data = data.get("formal_model", {}).get("extension_contract", {})
    extension_contract = ExtensionContract(
        required_fields=ext_data.get("required", []),
        style_new_tokens_allowed=ext_data.get("style_new_tokens_allowed", False),
        semantic_new_geometry_allowed=ext_data.get("semantic_new_geometry_allowed", False),
        requires_render_review=ext_data.get("requires_render_review", False),
        requires_data_invariants=ext_data.get("requires_data_invariants", False),
    )

    # Metadata
    metadata_raw = data.get("metadata", {})
    metadata = {k: str(v) for k, v in metadata_raw.items()}

    return CompiledOntology(
        schema_version=SCHEMA_ADAPTER_ID,
        source_hash=source_hash,
        ontology_corpus_hash=sources.ontology_corpus_hash,
        metadata=metadata,
        canvas=canvas,
        roles=roles,
        colors=colors,
        conditional_colors=conditional_colors,
        font_policy=font_policy,
        font_policy_rules=font_policy_rules,
        font_forbidden_combinations=font_forbidden,
        minimum_text_font_size=minimum_text_font_size,
        composition_operators=composition_operators,
        lists=lists,
        catalog_records=catalog_records,
        rule_registry=registry,
        extension_contract=extension_contract,
        compilation_issues=issues,
    )


def resolve_role_style(
    role_id: str,
    compiled: CompiledOntology,
    sources: OntologySources | None = None,
    *,
    context: StyleApplicationContext | None = None,
) -> NativeShapeStyle:
    """Resolve ``role_id`` into explicit native style primitives (spec §4.3).

    One resolution path for checkers, the patching guard and emitters: every
    value comes from the compiled snapshot — this module keeps no second copy
    of the corporate standard. An unknown role is an error, never a fallback
    body style.

    ``sources`` is the provenance record the snapshot was built from; when
    the compiled corpus hash disagrees with it the call is refused instead
    of resolving against mixed corpus versions. ``sources=None`` means the
    caller owns provenance (e.g. an already verified run snapshot).
    """
    if sources is not None:
        compiled_hash = (compiled.ontology_corpus_hash or "").strip()
        sources_hash = (sources.ontology_corpus_hash or "").strip()
        if compiled_hash and sources_hash and compiled_hash != sources_hash:
            raise OntologySourceError(
                "ONTOLOGY/SNAPSHOT_SOURCE_MISMATCH",
                "compiled snapshot and provenance sources are different corpora",
            )
    from .patching import StyleApplicationContext as _DefaultContext
    from .patching import resolve_native_style

    return resolve_native_style(
        style_role=role_id,
        rules=compiled,
        context=context if context is not None else _DefaultContext(),
    )


def build_field_origins(compiled: CompiledOntology) -> dict[str, Any]:
    """Field-by-field provenance: pointer / authority / derivation (§3.1)."""
    entries: list[dict[str, Any]] = []

    def _add(field: str, authority: str, pointer: str | None = None,
             derivation: str | None = None,
             dependency_refs: list[str] | None = None) -> None:
        entry: dict[str, Any] = {"field": field, "authority": authority}
        if pointer:
            entry["source_pointer"] = pointer
        if derivation:
            entry["derivation"] = derivation
        if dependency_refs:
            entry["dependency_refs"] = dependency_refs
        entries.append(entry)

    _add("canvas", "structured_norm", "/style_profile/canvas")

    for role_name, role in sorted(compiled.roles.items()):
        base = "/style_profile/typography"
        pointer = next(
            (ref for ref in role.evidence_refs
             if ref.startswith("style_profile.typography[")),
            f"{base}/role={role_name}",
        )
        _add(f"roles.{role_name}", "structured_norm", pointer,
             dependency_refs=list(role.evidence_refs))
        if role.default_size_pt is not None:
            _add(f"roles.{role_name}.default_size_pt", "structured_norm",
                 f"{pointer}/size_pt")
        if role.color_tokens:
            _add(f"roles.{role_name}.color_tokens", "structured_norm",
                 f"{pointer}/color")
        if role.uppercase is not None:
            _add(f"roles.{role_name}.uppercase", "structured_norm",
                 f"{pointer}/uppercase")
        else:
            _add(f"roles.{role_name}.uppercase", "derived", None,
                 derivation=(
                     "not declared in the loaded JSON for this role; an absent "
                     "property is unresolved, never false"
                 ),
                 dependency_refs=[pointer])

    for token in sorted(compiled.colors):
        _add(f"colors.{token}", "structured_norm",
             f"/style_profile/color_tokens/id={token}/value")
    for token, cond in sorted(compiled.conditional_colors.items()):
        _add(f"conditional_colors.{token}", "structured_norm",
             f"/style_profile/color_tokens/id={token}/value",
             dependency_refs=list(cond.allowed_context_ids))

    if compiled.minimum_text_font_size is not None:
        _add("minimum_text_font_size", "structured_norm",
             "/style_profile/minimum_text_font_size")

    for idx, _rule in enumerate(compiled.font_policy_rules):
        _add(f"font_policy_rules[{idx}]", "structured_norm",
             f"/style_profile/font_style_policy/rules[{idx}]")
    for idx, _fc in enumerate(compiled.font_forbidden_combinations):
        _add(f"font_forbidden_combinations[{idx}]", "structured_norm",
             f"/style_profile/font_style_policy/forbidden_combinations[{idx}]")

    if compiled.composition_operators:
        _add("composition_operators", "structured_norm",
             "/formal_model/enums/composition_operators")

    for ls_id, profile in sorted(compiled.lists.items()):
        base = f"/style_profile/list_styles/id={ls_id}"
        _add(f"lists.{ls_id}.marker_settings", "structured_norm",
             f"{base}/marker/ooxml")
        _add(f"lists.{ls_id}.color_rules", "structured_norm",
             f"{base}/marker/color_rules")
        if profile.nested_size_policy is not None:
            _add(f"lists.{ls_id}.nested_size_policy", "structured_norm",
                 f"{base}/text/nested_size_policy")
        if profile.nested_text_color is not None:
            _add(f"lists.{ls_id}.nested_text_color", "structured_norm",
                 f"{base}/text/nested_white_background_rgb")
        if profile.list_text_typeface is not None:
            _add(f"lists.{ls_id}.list_text_typeface", "structured_norm",
                 f"{base}/text/font_family")
        for level in profile.level_profiles:
            _add(
                f"lists.{ls_id}.level_profiles[{level.level}].size_pt",
                "derived",
                f"{base}/text/nested_size_policy" if profile.nested_size_policy else None,
                derivation=(
                    "not precomputed: resolve_list_level() applies the loaded "
                    "nested_size_policy to the real parent size of the paragraph "
                    "being resolved; an unknown parent stays unresolved"
                    if level.size_pt is None else
                    "level size resolved at emit time from the loaded policy"
                ),
                dependency_refs=(
                    [f"{base}/text/nested_size_policy"]
                    if profile.nested_size_policy else
                    [f"{base}/text/nested_size_policy"],
                ),
            )
            _add(
                f"lists.{ls_id}.level_profiles[{level.level}].mar_left_emu",
                "derived",
                f"{base}/paragraphs",
                derivation=(
                    "not precomputed: the JSON declares "
                    "paragraphs.inherit_indents_from_verified_source=true and "
                    "carries no marL values, so marL/indent come from the "
                    "verified source library at bind time (build_list_profile) "
                    "or stay None"
                ),
                dependency_refs=[f"{base}/paragraphs"],
            )

    for rule_id, spec in sorted(compiled.rule_registry.specs.items()):
        binding = compiled.rule_registry.bindings.get(rule_id)
        _add(
            f"rule_registry.specs.{rule_id}",
            "text_norm",
            spec.source_refs[0] if spec.source_refs else None,
            dependency_refs=[binding.checker_key] if binding else None,
        )

    for idx, record in enumerate(compiled.catalog_records):
        _add(f"catalog_records[{idx}] ({record.id})", "structured_norm",
             f"/catalogs/{record.kind}/id={record.id}")

    _add("extension_contract", "structured_norm",
         "/formal_model/extension_contract")

    return {
        "generated_from": {
            "source_hash": compiled.source_hash,
            "ontology_corpus_hash": compiled.ontology_corpus_hash,
        },
        "authority_values": {
            "structured_norm": "machine-readable value loaded from the ontology JSON",
            "text_norm": "normative text (condition/remedy) loaded from JSON/Markdown",
            "derived": "computed by the engine from loaded inputs; derivation recorded",
            "engine_policy": "engine implementation choice, not a corporate norm",
        },
        "entries": entries,
    }


def write_ontology_snapshot(sources: OntologySources, run_dir: Path) -> dict[str, Any]:
    """Copy every normative source into run_dir/ontology_snapshot (§4.1)."""
    snapshot_dir = Path(run_dir) / "ontology_snapshot"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    project_root = sources.root.parent
    files: list[dict[str, Any]] = []
    for record in sources.normative_files:
        src = project_root / record.relative_path
        if not src.is_file():
            continue
        rel = Path(record.relative_path)
        # Mirror the corpus layout without the leading ontology/ directory.
        if rel.parts and rel.parts[0] == sources.root.name:
            rel = Path(*rel.parts[1:]) if len(rel.parts) > 1 else Path(rel.name)
        dest = snapshot_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = src.read_bytes()
        dest.write_bytes(data)
        files.append({
            "relative_path": record.relative_path,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        })
    manifest = {
        "snapshot_of": str(sources.root),
        "ontology_hash": sources.ontology_hash,
        "ontology_corpus_hash": sources.ontology_corpus_hash,
        "selected_version": sources.selected_version,
        "files": files,
    }
    (snapshot_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def write_ontology_artifacts(
    *,
    compiled: CompiledOntology,
    conflicts: Any,
    coverage: list[Any],
    run_dir: Path,
    sources: OntologySources | None = None,
    binding_report: Any = None,
) -> dict[str, Path]:
    """Write compiled ontology artifacts to run_dir (spec §6.2, Stage 3.5 §13.1)."""
    from .assets import atomic_write_json

    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    ref = atomic_write_json(run_dir / "compiled_rules.json", compiled)
    written["compiled_rules"] = ref.absolute_path

    ref = atomic_write_json(run_dir / "rule_coverage.json", {"coverage": coverage})
    written["rule_coverage"] = ref.absolute_path

    ref = atomic_write_json(run_dir / "ontology_conflicts.json", conflicts)
    written["ontology_conflicts"] = ref.absolute_path

    # Field provenance: every executable normative value points at its source.
    field_origins = build_field_origins(compiled)
    ref = atomic_write_json(run_dir / "field_origins.json", field_origins)
    written["field_origins"] = ref.absolute_path

    # Rule binding report (§5.1): fingerprints, reviewed links, unresolved.
    report = binding_report
    if report is None:
        coverage_models = []
        for cov in coverage:
            if isinstance(cov, dict):
                try:
                    from .models import RuleCoverage as _RuleCoverage

                    coverage_models.append(_RuleCoverage.model_validate(cov))
                except Exception:  # noqa: BLE001 — report just loses scopes
                    continue
            else:
                coverage_models.append(cov)
        report = bind_rules(
            compiled.rule_registry.specs,
            compiled.rule_registry.bindings,
            coverage=coverage_models,
        )
    ref = atomic_write_json(run_dir / "rule_bindings.json", report)
    written["rule_bindings"] = ref.absolute_path

    # Immutable snapshot of the normative sources this run compiled against.
    if sources is not None:
        write_ontology_snapshot(sources, run_dir)
        written["ontology_snapshot"] = str(run_dir / "ontology_snapshot")

    ref = atomic_write_json(run_dir / "ontology_manifest.json", {
        "source_hash": compiled.source_hash,
        "ontology_corpus_hash": compiled.ontology_corpus_hash,
        "metadata": compiled.metadata,
        "canvas": compiled.canvas.model_dump(),
        "role_count": len(compiled.roles),
        "color_count": len(compiled.colors),
        "conditional_color_count": len(compiled.conditional_colors),
        "catalog_record_count": len(compiled.catalog_records),
        "rule_count": len(compiled.rule_registry.specs),
        "snapshot_dir": str(run_dir / "ontology_snapshot") if sources else None,
    })
    written["ontology_manifest"] = ref.absolute_path

    # model_rule_digest.md
    digest_lines = [
        "# Model Rule Digest",
        "",
        f"Source hash: `{compiled.source_hash}`",
        f"Corpus hash: `{compiled.ontology_corpus_hash}`",
        "",
        "## Canvas",
        "",
        f"{compiled.canvas.w} x {compiled.canvas.h} EMU",
        "",
        "## Roles",
        "",
    ]
    for role_name, role in sorted(compiled.roles.items()):
        digest_lines.append(f"- **{role_name}**: {role.default_size_pt}pt, {role.allowed_faces}")
    digest_lines.extend(["", "## Rules", ""])
    for rule_id, spec in sorted(compiled.rule_registry.specs.items()):
        digest_lines.append(f"- **{rule_id}** ({spec.severity}): {spec.condition[:80]}")
    digest_lines.extend(["", "## Catalog", "", f"{len(compiled.catalog_records)} records", ""])

    digest_path = run_dir / "model_rule_digest.md"
    digest_path.write_text("\n".join(digest_lines), encoding="utf-8")
    written["model_rule_digest"] = digest_path

    return written
