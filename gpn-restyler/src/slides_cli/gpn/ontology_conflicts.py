"""Ontology conflict analysis (spec §5).

Extracts normative statements from JSON and Markdown sources, then compares
them to detect contradictions, compatible additions, and unresolved items.
"""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from .models import (
    Issue,
    NormalizedPredicate,
    NormativeExtraction,
    NormativeRef,
    NormativeStatement,
    OntologyConflict,
    OntologyConflictReport,
    OntologyDocument,
    PredicateComparison,
    ScopeComparison,
    Severity,
)

log = logging.getLogger(__name__)


def extract_normative_statements(
    document: OntologyDocument,
) -> NormativeExtraction:
    """Extract normative statements from an ontology document (spec §5.1)."""
    statements: list[NormativeStatement] = []
    sections_examined: list[str] = []
    evidence_sections: list[str] = []
    unparsed: list[str] = []
    issues: list[Issue] = []

    if document.source_kind == "json":
        try:
            data = json.loads(document.content)
        except json.JSONDecodeError as exc:
            issues.append(Issue(
                rule_id="ONTOLOGY_PARSE",
                code="JSON_PARSE_FAILED",
                severity=Severity.ERROR,
                details=str(exc),
            ))
            return NormativeExtraction(
                statements=statements,
                sections_examined=sections_examined,
                evidence_sections=evidence_sections,
                unparsed_normative_sections=unparsed,
                issues=issues,
            )

        sections_examined.append("metadata")
        sections_examined.append("formal_model")
        sections_examined.append("style_profile")
        sections_examined.append("catalogs")
        sections_examined.append("catalogue_rules")

        # Extract constraints from formal_model
        constraints = data.get("formal_model", {}).get("constraints", [])
        for i, constraint in enumerate(constraints):
            if not isinstance(constraint, dict):
                continue
            cid = constraint.get("id", f"constraint-{i}")
            condition = constraint.get("condition", "")
            lowered = condition.lower()
            modality = "must_not" if "нет" in lowered or "запрещ" in lowered else "must"
            statements.append(NormativeStatement(
                id=f"constraint:{cid}",
                subject="style",
                property=cid,
                operator="=",
                typed_value=condition,
                applicability="all",
                modality=modality,  # type: ignore[arg-type]
                rule_id=cid,
                refs=[NormativeRef(
                    source_kind="json",
                    relative_path="formal_model.constraints",
                    json_pointer=f"/formal_model/constraints/{i}",
                    source_hash=document.source_hash,
                )],
            ))

        # Extract typography roles
        typography = data.get("style_profile", {}).get("typography", [])
        for i, role in enumerate(typography):
            if not isinstance(role, dict):
                continue
            role_name = role.get("role", f"role-{i}")
            font = role.get("font", "")
            size = role.get("size_pt")
            color = role.get("color", "")
            uppercase = role.get("uppercase", False)

            if font:
                statements.append(NormativeStatement(
                    id=f"role:{role_name}:font",
                    subject=role_name,
                    property="font",
                    operator="=",
                    typed_value=font,
                    applicability="all",
                    modality="must",
                    rule_id="C02",
                    refs=[NormativeRef(
                        source_kind="json",
                        relative_path="style_profile.typography",
                        json_pointer=f"/style_profile/typography/{i}/font",
                        source_hash=document.source_hash,
                    )],
                ))
            if size is not None:
                statements.append(NormativeStatement(
                    id=f"role:{role_name}:size",
                    subject=role_name,
                    property="size_pt",
                    operator="=",
                    typed_value=str(size),
                    applicability="all",
                    modality="must",
                    rule_id="C19",
                    refs=[NormativeRef(
                        source_kind="json",
                        relative_path="style_profile.typography",
                        json_pointer=f"/style_profile/typography/{i}/size_pt",
                        source_hash=document.source_hash,
                    )],
                ))
            if color:
                statements.append(NormativeStatement(
                    id=f"role:{role_name}:color",
                    subject=role_name,
                    property="color",
                    operator="=",
                    typed_value=color,
                    applicability="all",
                    modality="must",
                    rule_id="C01",
                    refs=[NormativeRef(
                        source_kind="json",
                        relative_path="style_profile.typography",
                        json_pointer=f"/style_profile/typography/{i}/color",
                        source_hash=document.source_hash,
                    )],
                ))
            if uppercase:
                statements.append(NormativeStatement(
                    id=f"role:{role_name}:uppercase",
                    subject=role_name,
                    property="uppercase",
                    operator="=",
                    typed_value="true",
                    applicability="all",
                    modality="must",
                    rule_id="C01",
                    refs=[NormativeRef(
                        source_kind="json",
                        relative_path="style_profile.typography",
                        json_pointer=f"/style_profile/typography/{i}/uppercase",
                        source_hash=document.source_hash,
                    )],
                ))

        # Extract color tokens
        color_tokens = data.get("style_profile", {}).get("color_tokens", [])
        for i, token in enumerate(color_tokens):
            if not isinstance(token, dict):
                continue
            token_id = token.get("id", f"color-{i}")
            value = token.get("value", "")
            usage = token.get("usage", "default")
            modality = "conditional" if usage == "conditional" else "must"
            statements.append(NormativeStatement(
                id=f"color:{token_id}",
                subject="color",
                property=token_id,
                operator="=",
                typed_value=value,
                applicability=usage,
                modality=modality,  # type: ignore[arg-type]
                rule_id="C01",
                refs=[NormativeRef(
                    source_kind="json",
                    relative_path="style_profile.color_tokens",
                    json_pointer=f"/style_profile/color_tokens/{i}",
                    source_hash=document.source_hash,
                )],
            ))

        # Extract font style policy constraints
        fsp = data.get("style_profile", {}).get("font_style_policy", {})
        fsp_constraints = fsp.get("constraints", [])
        for i, constraint in enumerate(fsp_constraints):
            if not isinstance(constraint, dict):
                continue
            cid = constraint.get("id", f"fsp-{i}")
            condition = constraint.get("condition", "")
            statements.append(NormativeStatement(
                id=f"fsp:{cid}",
                subject="font_policy",
                property=cid,
                operator="=",
                typed_value=condition,
                applicability="all",
                modality="must_not" if "bold:true" in condition.lower() else "must",
                rule_id=cid,
                refs=[NormativeRef(
                    source_kind="json",
                    relative_path="style_profile.font_style_policy.constraints",
                    json_pointer=f"/style_profile/font_style_policy/constraints/{i}",
                    source_hash=document.source_hash,
                )],
            ))

        # Extract list styles
        list_styles = data.get("style_profile", {}).get("list_styles", [])
        for i, ls in enumerate(list_styles):
            if not isinstance(ls, dict):
                continue
            ls_id = ls.get("id", f"list-{i}")
            marker = ls.get("marker", {})
            bu_char = marker.get("ooxml", {}).get("buChar", "")
            bu_font = marker.get("ooxml", {}).get("buFont", "")
            bu_sz = marker.get("ooxml", {}).get("buSzPct", 0)
            if bu_char:
                statements.append(NormativeStatement(
                    id=f"list:{ls_id}:char",
                    subject=ls_id,
                    property="bullet_char",
                    operator="=",
                    typed_value=bu_char,
                    applicability="all",
                    modality="must",
                    rule_id="C17",
                    refs=[NormativeRef(
                        source_kind="json",
                        relative_path="style_profile.list_styles",
                        json_pointer=f"/style_profile/list_styles/{i}/marker/ooxml/buChar",
                        source_hash=document.source_hash,
                    )],
                ))
            if bu_font:
                statements.append(NormativeStatement(
                    id=f"list:{ls_id}:font",
                    subject=ls_id,
                    property="bullet_font",
                    operator="=",
                    typed_value=bu_font,
                    applicability="all",
                    modality="must",
                    rule_id="C17",
                    refs=[NormativeRef(
                        source_kind="json",
                        relative_path="style_profile.list_styles",
                        json_pointer=f"/style_profile/list_styles/{i}/marker/ooxml/buFont",
                        source_hash=document.source_hash,
                    )],
                ))
            if bu_sz:
                statements.append(NormativeStatement(
                    id=f"list:{ls_id}:size",
                    subject=ls_id,
                    property="bullet_size_pct",
                    operator="=",
                    typed_value=str(bu_sz / 1000),
                    applicability="all",
                    modality="must",
                    rule_id="C17",
                    refs=[NormativeRef(
                        source_kind="json",
                        relative_path="style_profile.list_styles",
                        json_pointer=f"/style_profile/list_styles/{i}/marker/ooxml/buSzPct",
                        source_hash=document.source_hash,
                    )],
                ))

    elif document.source_kind in ("embedded_markdown", "external_markdown"):
        lines = document.content.split("\n")
        sections_examined.append("markdown")
        in_code_block = False
        for i, line in enumerate(lines):
            if line.strip().startswith("```"):
                in_code_block = not in_code_block
                continue
            if in_code_block:
                continue

            # Extract headings as sections
            if line.startswith("#"):
                heading = line.lstrip("#").strip()
                sections_examined.append(heading)

            # Extract table rows with constraints
            if "|" in line and not line.strip().startswith("|---"):
                cells = [c.strip() for c in line.split("|")]
                if (
                    len(cells) >= 3
                    and re.fullmatch(r"[CR]\d{2}", cells[1])
                ):
                    rule_id = cells[1]
                    # Condition + remedy columns: the normative content may
                    # span both (e.g. C18 minimum 8pt lives in the remedy
                    # column of the MD table). Join non-empty trailing cells
                    # so the comparison sees the full row, not a truncation.
                    row_cells = [c for c in cells[2:] if c.strip()]
                    # Drop trailing empty strings from the split artifacts.
                    condition = " | ".join(row_cells)
                    lowered = condition.lower()
                    md_modality = (
                        "must_not"
                        if "нет" in lowered or "запрещ" in lowered
                        else "must"
                    )
                    statements.append(NormativeStatement(
                        id=f"md:{rule_id}",
                        subject="style",
                        property=rule_id,
                        operator="=",
                        typed_value=condition,
                        applicability="all",
                        modality=md_modality,  # type: ignore[arg-type]
                        rule_id=rule_id,
                        refs=[NormativeRef(
                            source_kind=document.source_kind,
                            relative_path="markdown",
                            line_start=i + 1,
                            line_end=i + 1,
                            source_hash=document.source_hash,
                        )],
                    ))

    return NormativeExtraction(
        statements=statements,
        sections_examined=sections_examined,
        evidence_sections=evidence_sections,
        unparsed_normative_sections=unparsed,
        issues=issues,
    )


_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
_NUM_UNIT_RE = re.compile(
    r"(?P<num>\d+(?:[.,]\d+)?)\s*(?P<unit>%|pt|px|emu|мм|mm)?",
    re.IGNORECASE,
)

_QUAL_GE = ("не менее", "минимум", "minimum", "at least", "atleast", "≥", ">=")
_QUAL_LE = ("не более", "максимум", "maximum", "at most", "atmost", "≤", "<=", "up to")
_QUAL_EQ = ("ровно", "exactly", "equal", "==")
_CONDITIONAL_TOKENS = ("conditional", "услов", "при подтвержд", "контекст", "context")
_SKY_TOKENS = ("sky", "cyan", "голуб", "бирюз")
_KNOWN_SCOPES = {
    "all", "global", "body", "footnote", "title", "subtitle", "caption",
    "metric", "section", "unknown",
}


def _norm_numbers(text: str) -> frozenset[str]:
    """Normalized numeric tokens for conflict detection (80% vs 90%)."""
    return frozenset(
        t.replace(",", ".").lstrip("0") or "0"
        for t in _NUMBER_RE.findall(text.lower().replace(" ", ""))
    )


def _norm_scope(raw: str) -> str:
    s = (raw or "").strip().casefold()
    if not s:
        return "unknown"
    # Keep conditional qualifiers; collapse whitespace.
    s = re.sub(r"\s+", " ", s)
    if s in _KNOWN_SCOPES:
        return s
    # Applicability strings like "conditional:sky" stay as-is (lowercased).
    return s


def _scope_overlap(left_scope: str, right_scope: str) -> ScopeComparison:
    """Decide scope overlap without assuming unknown == global/disjoint."""
    ls = _norm_scope(left_scope)
    rs = _norm_scope(right_scope)
    if ls == "unknown" or rs == "unknown":
        return ScopeComparison(
            left_scope=ls, right_scope=rs, overlap=None,
            reason="unclear scope is neither global nor disjoint",
        )
    if ls == rs:
        return ScopeComparison(
            left_scope=ls, right_scope=rs, overlap=True, reason="same scope")
    # Conditional vs unconditional with the same base is an applicability
    # difference, never an automatic merge.
    if "conditional" in ls or "conditional" in rs or "услов" in ls or "услов" in rs:
        return ScopeComparison(
            left_scope=ls, right_scope=rs, overlap=None,
            reason="applicability_difference: conditional vs unconditional",
        )
    if ls in ("all", "global") or rs in ("all", "global"):
        return ScopeComparison(
            left_scope=ls, right_scope=rs, overlap=True,
            reason="specific scope inside global scope",
        )
    # Distinct specific scopes (body vs footnote) are disjoint.
    return ScopeComparison(
        left_scope=ls, right_scope=rs, overlap=False,
        reason=f"disjoint scopes: {ls!r} vs {rs!r}",
    )


def _extract_bool_flag(text: str) -> str | None:
    """Extract b=false/b=true style flags; None when absent."""
    m = re.search(r"\bb\s*=\s*(true|false|0|1)\b", text.casefold())
    if m:
        v = m.group(1)
        if v in ("true", "1"):
            return "true"
        return "false"
    m = re.search(r"\bbold\s*:\s*(true|false)\b", text.casefold())
    if m:
        return m.group(1)
    return None


def _parse_numeric_bound(text: str) -> tuple[str, Decimal | None, str]:
    """Parse a single scalar bound into (operator, value, unit).

    Returns operator in eq/ge/le/unknown. Decimal is used for exact bounds.
    """
    lowered = text.casefold()
    ge = any(q in lowered for q in _QUAL_GE)
    le = any(q in lowered for q in _QUAL_LE)
    matches = list(_NUM_UNIT_RE.finditer(text))
    # Keep only matches that actually contain digits (regex guarantees it).
    nums: list[tuple[Decimal, str]] = []
    for m in matches:
        raw_num = m.group("num").replace(",", ".")
        try:
            val = Decimal(raw_num)
        except InvalidOperation:
            continue
        unit = (m.group("unit") or "").casefold()
        if unit == "мм":
            unit = "mm"
        nums.append((val, unit))
    if not nums:
        return ("unknown", None, "")
    if ge and not le and len(nums) >= 1:
        # "не менее 8": lower bound is the first number.
        return ("ge", nums[0][0], nums[0][1])
    if le and not ge and len(nums) >= 1:
        return ("le", nums[0][0], nums[0][1])
    if len(nums) == 1:
        return ("eq", nums[0][0], nums[0][1])
    # Multiple numbers without a range qualifier cannot be compared safely.
    return ("unknown", None, "")


def normalize_predicate(statement: NormativeStatement) -> NormalizedPredicate:
    """Parse a normative statement into a typed predicate (Stage 3 §2.1)."""
    text = statement.typed_value or ""
    lowered = text.casefold()
    modality = statement.modality
    if modality == "must_not" or any(
        tok in lowered for tok in ("запрещ", "запрещено", "недопустим")
    ):
        operator: str = "forbid"
        values: list[str] = []
        unit = ""
        domain = "forbid"
    else:
        op, val, unit = _parse_numeric_bound(text)
        operator = op
        values = [str(val)] if val is not None else []
        domain = "scalar" if val is not None else "unknown"
    scope_raw = statement.applicability or ""
    # Subject may carry the scope (body/footnote) when applicability is "all".
    subject_cf = (statement.subject or "").casefold()
    if (not scope_raw or scope_raw.casefold() == "all") and subject_cf in _KNOWN_SCOPES \
            and subject_cf not in ("all", "global", "unknown"):
        scope = subject_cf
    else:
        scope = _norm_scope(scope_raw) if scope_raw else "unknown"
        if scope == "unknown" and subject_cf in _KNOWN_SCOPES \
                and subject_cf not in ("all", "global"):
            scope = subject_cf
    refs = []
    for r in statement.refs:
        refs.append(f"{r.source_kind}:{r.relative_path}:{r.source_hash[:12]}")
    return NormalizedPredicate(
        subject=statement.subject or "",
        property=statement.property or "",
        value_domain=domain,
        operator=operator,  # type: ignore[arg-type]
        values=values,
        unit=unit,
        scope=scope,
        source_refs=refs,
        authority=statement.rule_id or "",
    )


def _interval_of(
    operator: str, values: list[str],
) -> tuple[Decimal | None, bool, Decimal | None, bool] | None:
    """Return (lower, lower_inclusive, upper, upper_inclusive) or None."""
    if operator == "eq" and len(values) == 1:
        v = Decimal(values[0])
        return (v, True, v, True)
    if operator == "ge" and len(values) == 1:
        v = Decimal(values[0])
        return (v, True, None, False)
    if operator == "le" and len(values) == 1:
        v = Decimal(values[0])
        return (None, False, v, True)
    return None


def _intervals_equal(
    a: tuple[Decimal | None, bool, Decimal | None, bool],
    b: tuple[Decimal | None, bool, Decimal | None, bool],
) -> bool:
    return a == b


def _intervals_intersect(
    a: tuple[Decimal | None, bool, Decimal | None, bool],
    b: tuple[Decimal | None, bool, Decimal | None, bool],
) -> tuple[Decimal | None, bool, Decimal | None, bool] | None:
    """Intersect two intervals; None when empty."""
    lo: Decimal | None
    lo_inc = False
    hi: Decimal | None
    hi_inc = False
    a_lo, a_lo_inc, a_hi, a_hi_inc = a
    b_lo, b_lo_inc, b_hi, b_hi_inc = b
    if a_lo is None:
        lo, lo_inc = b_lo, b_lo_inc
    elif b_lo is None or a_lo > b_lo:
        lo, lo_inc = a_lo, a_lo_inc
    elif b_lo > a_lo:
        lo, lo_inc = b_lo, b_lo_inc
    else:
        lo, lo_inc = a_lo, (a_lo_inc and b_lo_inc)
    if a_hi is None:
        hi, hi_inc = b_hi, b_hi_inc
    elif b_hi is None or a_hi < b_hi:
        hi, hi_inc = a_hi, a_hi_inc
    elif b_hi < a_hi:
        hi, hi_inc = b_hi, b_hi_inc
    else:
        hi, hi_inc = a_hi, (a_hi_inc and b_hi_inc)
    if lo is not None and hi is not None:
        if lo > hi:
            return None
        if lo == hi and not (lo_inc and hi_inc):
            return None
    return (lo, lo_inc, hi, hi_inc)


def compare_predicates(
    left: NormalizedPredicate,
    right: NormalizedPredicate,
    *,
    scope: ScopeComparison,
) -> PredicateComparison:
    """Compare two normalized predicates per the Stage 3 §2.1 table."""
    refs = list(left.source_refs) + list(right.source_refs)
    # Different predicates (subject/property) are not comparable.
    if (left.subject.casefold() != right.subject.casefold()
            or left.property.casefold() != right.property.casefold()):
        return PredicateComparison(
            relation="disjoint_scope",
            reason="different subject/property predicates",
            refs=refs,
        )
    if scope.overlap is False:
        return PredicateComparison(
            relation="disjoint_scope",
            reason=scope.reason or "disjoint scopes",
            refs=refs,
        )
    if scope.overlap is None:
        return PredicateComparison(
            relation="unresolved",
            reason=scope.reason or "unclear scope overlap",
            refs=refs,
        )
    # Same predicate, overlapping scope: forbid vs allow is a contradiction.
    if (left.operator == "forbid") != (right.operator == "forbid"):
        return PredicateComparison(
            relation="contradiction",
            reason="forbid vs allow on the same predicate",
            refs=refs,
        )
    if left.operator == "forbid" and right.operator == "forbid":
        return PredicateComparison(
            relation="equivalent", intersection="forbid",
            reason="both forbid the same predicate", refs=refs)
    # Units must match exactly before numeric comparison.
    if left.unit != right.unit and left.unit and right.unit:
        return PredicateComparison(
            relation="unresolved",
            reason=f"incompatible units: {left.unit!r} vs {right.unit!r}",
            refs=refs,
        )
    left_known = left.operator in ("eq", "ge", "le")
    right_known = right.operator in ("eq", "ge", "le")
    if left_known and right_known:
        li = _interval_of(left.operator, left.values)
        ri = _interval_of(right.operator, right.values)
        if li is None or ri is None:
            return PredicateComparison(
                relation="unresolved",
                reason="uncomputable scalar domain", refs=refs)
        if _intervals_equal(li, ri):
            return PredicateComparison(
                relation="equivalent",
                intersection=str(li),
                reason="equal allowed sets", refs=refs)
        inter = _intervals_intersect(li, ri)
        if inter is None:
            return PredicateComparison(
                relation="contradiction",
                intersection="empty",
                reason=f"empty intersection: {li} vs {ri}",
                refs=refs,
            )
        return PredicateComparison(
            relation="compatible_refinement",
            intersection=str(inter),
            reason=f"nonempty proper intersection: {li} vs {ri}",
            refs=refs,
        )
    # At least one side is unparsed: no ID-only fallback to equivalent.
    return PredicateComparison(
        relation="unresolved",
        reason="unsupported predicate or uncomputable applicability",
        refs=refs,
    )


def _qualifier_clash(a: NormativeStatement, b: NormativeStatement) -> bool:
    """Legacy helper kept for callers: True only for a real contradiction."""
    la = normalize_predicate(a)
    lb = normalize_predicate(b)
    sc = _scope_overlap(la.scope, lb.scope)
    return compare_predicates(la, lb, scope=sc).relation == "contradiction"


def _statements_conflict(a: NormativeStatement, b: NormativeStatement) -> bool:
    """True only for a real normative opposition, not a paraphrase."""
    # Boolean flags (b=false vs b=true) are contradictions, not paraphrases.
    fa, fb = _extract_bool_flag(a.typed_value), _extract_bool_flag(b.typed_value)
    if fa is not None and fb is not None and fa != fb:
        return True
    # Lost conditional color (sky/cyan) must never silently pass.
    la_low, lb_low = a.typed_value.casefold(), b.typed_value.casefold()
    a_sky = any(t in la_low for t in _SKY_TOKENS)
    b_sky = any(t in lb_low for t in _SKY_TOKENS)
    if a_sky != b_sky:
        return True
    la = normalize_predicate(a)
    lb = normalize_predicate(b)
    # Modality opposition on the same predicate is a contradiction.
    if {a.modality, b.modality} == {"must", "must_not"}:
        sc = _scope_overlap(la.scope, lb.scope)
        return bool(sc.overlap)
    sc = _scope_overlap(la.scope, lb.scope)
    if sc.overlap is False:
        return False
    rel = compare_predicates(la, lb, scope=sc).relation
    return rel == "contradiction"


def _pairwise_relation(
    anchor: NormativeStatement, other: NormativeStatement,
) -> tuple[str, PredicateComparison]:
    """Normalize both statements and compare with scope handling."""
    la = normalize_predicate(anchor)
    lb = normalize_predicate(other)
    sc = _scope_overlap(la.scope, lb.scope)
    same_predicate = (
        la.subject.casefold() == lb.subject.casefold()
        and la.property.casefold() == lb.property.casefold()
    )
    # Boolean flag flip on the same predicate is a contradiction.
    fa, fb = _extract_bool_flag(anchor.typed_value), _extract_bool_flag(other.typed_value)
    if (fa is not None and fb is not None and fa != fb and sc.overlap
            and same_predicate):
        return ("contradiction", PredicateComparison(
            relation="contradiction",
            reason=f"boolean flag changed: b={fa} vs b={fb}",
            refs=list(la.source_refs) + list(lb.source_refs),
        ))
    # Lost sky/cyan condition is never an automatic equivalent.
    la_low, lb_low = anchor.typed_value.casefold(), other.typed_value.casefold()
    sky_differs = (any(t in la_low for t in _SKY_TOKENS)
                   != any(t in lb_low for t in _SKY_TOKENS))
    if sky_differs and sc.overlap is not False and same_predicate:
        return ("unresolved", PredicateComparison(
            relation="unresolved",
            reason="lost conditional color applicability (sky/cyan)",
            refs=list(la.source_refs) + list(lb.source_refs),
        ))
    # Modality opposition on overlapping scope is a contradiction.
    modalities_oppose = (
        {anchor.modality, other.modality} == {"must", "must_not"} and sc.overlap
    )
    if modalities_oppose and same_predicate:
        return ("contradiction", PredicateComparison(
            relation="contradiction",
            reason="must vs must_not on the same predicate",
            refs=list(la.source_refs) + list(lb.source_refs),
        ))
    comp = compare_predicates(la, lb, scope=sc)
    # Unknown predicates with identical typed values are paraphrase-equivalent;
    # differing unknown texts stay unresolved (no ID-only fallback).
    if comp.relation == "unresolved" and sc.overlap:
        if anchor.typed_value == other.typed_value:
            comp = PredicateComparison(
                relation="equivalent", reason="identical unknown predicate",
                refs=comp.refs)
        elif la.operator == "unknown" and lb.operator == "unknown":
            nums_a = _norm_numbers(anchor.typed_value)
            nums_b = _norm_numbers(other.typed_value)
            if (anchor.modality == other.modality and nums_a == nums_b
                    and fa == fb
                    and (any(t in la_low for t in _SKY_TOKENS)
                         == any(t in lb_low for t in _SKY_TOKENS))):
                # Reviewed paraphrase of the same catalog row: same rule,
                # same predicate, same modality/numbers/conditions.
                comp = PredicateComparison(
                    relation="equivalent",
                    reason="reviewed paraphrase: same predicate/modality/numbers",
                    refs=comp.refs,
                )
    return (comp.relation, comp)


def compare_normative_statements(
    statements: list[NormativeStatement],
) -> OntologyConflictReport:
    """Compare normative statements for conflicts (spec §5.2, Stage 3 §2.1)."""
    conflicts: list[OntologyConflict] = []
    equivalent: list[str] = []
    compatible_additions: list[str] = []
    compatible_refinements: list[str] = []
    resolved_rule_ids: list[str] = []
    unparsed: list[str] = []

    for stmt in statements:
        if stmt.extraction_status != "parsed":
            unparsed.append(stmt.id or stmt.rule_id or stmt.property)

    # Group by rule_id, then by (subject, property) predicate.
    by_rule: dict[str, list[NormativeStatement]] = {}
    for stmt in statements:
        rid = stmt.rule_id
        if rid:
            by_rule.setdefault(rid, []).append(stmt)

    for rule_id, stmts in by_rule.items():
        if len(stmts) == 1:
            sole = stmts[0]
            # MD-only new mandatory rules are preserved, never silently lost.
            md_only = any(
                r.source_kind in ("embedded_markdown", "external_markdown")
                for r in sole.refs)
            if md_only and sole.modality in ("must", "must_not"):
                compatible_additions.append(rule_id)
            resolved_rule_ids.append(rule_id)
            continue
        values = {s.typed_value for s in stmts}
        if len(values) == 1:
            equivalent.append(rule_id)
            resolved_rule_ids.append(rule_id)
            continue
        # Subgroup by predicate so different aspects are never merged.
        by_pred: dict[tuple[str, str], list[NormativeStatement]] = {}
        for s in stmts:
            by_pred.setdefault(
                (s.subject.casefold(), s.property.casefold()), []).append(s)
        if len(by_pred) > 1:
            # Different aspects of the same rule: check each predicate
            # separately; the rule is equivalent only when every predicate
            # is internally equivalent.
            rule_relations: set[str] = set()
            for _key, group in by_pred.items():
                if len(group) == 1:
                    continue
                anchor = group[0]
                for other in group[1:]:
                    rel, _comp = _pairwise_relation(anchor, other)
                    rule_relations.add(rel)
            if not rule_relations:
                # Distinct predicates/scopes: neither merged nor conflicted.
                resolved_rule_ids.append(rule_id)
                continue
            if "contradiction" in rule_relations or "unresolved" in rule_relations:
                anchor = stmts[0]
                bad = next(
                    o for o in stmts[1:]
                    if _pairwise_relation(anchor, o)[0] in (
                        "contradiction", "unresolved"))
                rel, comp = _pairwise_relation(anchor, bad)
                kind = "contradiction" if rel == "contradiction" else "unresolved"
                conflicts.append(OntologyConflict(
                    id=f"conflict:{rule_id}",
                    subject=anchor.subject,
                    property=anchor.property,
                    left=anchor,
                    right=bad,
                    kind=kind,  # type: ignore[arg-type]
                    blocking=True,
                    explanation=(f"Rule {rule_id} {rel}: {comp.reason}: "
                                 f"{anchor.typed_value!r} vs {bad.typed_value!r}"),
                ))
            elif "compatible_refinement" in rule_relations:
                compatible_refinements.append(rule_id)
                resolved_rule_ids.append(rule_id)
            else:
                equivalent.append(rule_id)
                resolved_rule_ids.append(rule_id)
            continue
        # Single predicate, multiple texts: typed comparison.
        anchor = stmts[0]
        relations: list[tuple[str, NormativeStatement, PredicateComparison]] = []
        for other in stmts[1:]:
            rel, comp = _pairwise_relation(anchor, other)
            relations.append((rel, other, comp))
        kinds = {r for r, _, _ in relations}
        if "contradiction" in kinds:
            _rel, other, comp = next(x for x in relations if x[0] == "contradiction")
            conflicts.append(OntologyConflict(
                id=f"conflict:{rule_id}",
                subject=anchor.subject,
                property=anchor.property,
                left=anchor,
                right=other,
                kind="contradiction",
                blocking=True,
                explanation=(f"Rule {rule_id} contradiction: {comp.reason}: "
                             f"{anchor.typed_value!r} vs {other.typed_value!r}"),
            ))
        elif "unresolved" in kinds:
            _rel, other, comp = next(x for x in relations if x[0] == "unresolved")
            conflicts.append(OntologyConflict(
                id=f"conflict:{rule_id}",
                subject=anchor.subject,
                property=anchor.property,
                left=anchor,
                right=other,
                kind="unresolved",
                blocking=True,
                explanation=(f"Rule {rule_id} unresolved: {comp.reason}: "
                             f"{anchor.typed_value!r} vs {other.typed_value!r}"),
            ))
        elif "compatible_refinement" in kinds:
            # Both norms are preserved with authority/version/applicability;
            # the narrower set is recorded, never an automatic tightening.
            compatible_refinements.append(rule_id)
            resolved_rule_ids.append(rule_id)
        elif "disjoint_scope" in kinds and len(kinds) == 1:
            resolved_rule_ids.append(rule_id)
        else:
            equivalent.append(rule_id)
            resolved_rule_ids.append(rule_id)

    # Cross-rule check on the same (subject, property) with overlapping scope.
    by_subject_property: dict[tuple[str, str], list[NormativeStatement]] = {}
    for stmt in statements:
        key = ((stmt.subject or "").casefold(), (stmt.property or "").casefold())
        by_subject_property.setdefault(key, []).append(stmt)

    for (subject, prop), stmts in by_subject_property.items():
        if len(stmts) < 2:
            continue
        if len({s.typed_value for s in stmts}) <= 1:
            continue
        if len({s.rule_id for s in stmts}) <= 1:
            continue
        anchor = stmts[0]
        for other in stmts[1:]:
            if anchor.rule_id == other.rule_id:
                continue
            rel, comp = _pairwise_relation(anchor, other)
            if rel == "contradiction":
                conflicts.append(OntologyConflict(
                    id=f"cross:{subject}:{prop}",
                    subject=anchor.subject,
                    property=anchor.property,
                    left=anchor,
                    right=other,
                    kind="contradiction",
                    blocking=True,
                    explanation=(f"Cross-rule contradiction on "
                                 f"{subject}.{prop}: {comp.reason}"),
                ))
                break
            if rel == "unresolved":
                conflicts.append(OntologyConflict(
                    id=f"cross:{subject}:{prop}",
                    subject=anchor.subject,
                    property=anchor.property,
                    left=anchor,
                    right=other,
                    kind="unresolved",
                    blocking=True,
                    explanation=(f"Cross-rule unresolved on "
                                 f"{subject}.{prop}: {comp.reason}"),
                ))
                break
            # compatible_refinement and disjoint_scope across rules are not
            # blocking: compatible additions of the full catalog never block.

    blocking = [c for c in conflicts if c.blocking]
    ready = len(blocking) == 0 and len(unparsed) == 0

    return OntologyConflictReport(
        source_hashes={},
        documents_differ=False,
        equivalent=equivalent,
        compatible_additions=compatible_additions,
        compatible_refinements=compatible_refinements,
        conflicts=conflicts,
        unparsed_normative_sections=unparsed,
        resolved_rule_ids=resolved_rule_ids,
        ready_for_compilation=ready,
    )


def analyze_ontology_sources(
    sources: Any,
) -> OntologyConflictReport:
    """Analyze all ontology sources for conflicts (spec §5.2)."""
    from .models import OntologySources

    if not isinstance(sources, OntologySources):
        return OntologyConflictReport(
            conflicts=[],
            ready_for_compilation=False,
        )

    documents: list[OntologyDocument] = []

    # Primary JSON
    if sources.primary_json and sources.primary_json.is_file():
        content = sources.primary_json.read_text(encoding="utf-8")
        documents.append(OntologyDocument(
            source_kind="json",
            path=sources.primary_json,
            source_hash=sources.ontology_hash or "",
            content=content,
        ))

    # Embedded markdown from JSON
    if sources.primary_json and sources.primary_json.is_file():
        try:
            data = json.loads(sources.primary_json.read_text(encoding="utf-8"))
            embedded_md = data.get("normative_text_markdown", "")
            if embedded_md:
                documents.append(OntologyDocument(
                    source_kind="embedded_markdown",
                    path=sources.primary_json,
                    source_hash=sources.ontology_hash or "",
                    content=embedded_md,
                ))
        except (json.JSONDecodeError, OSError):
            pass

    # External markdown files
    for md_path in sources.markdown_files:
        if md_path.is_file():
            content = md_path.read_text(encoding="utf-8")
            from .ontology import sha256_file
            documents.append(OntologyDocument(
                source_kind="external_markdown",
                path=md_path,
                source_hash=sha256_file(md_path),
                content=content,
            ))

    all_statements: list[NormativeStatement] = []
    all_issues: list[Issue] = []
    unparsed: list[str] = []

    for doc in documents:
        extraction = extract_normative_statements(doc)
        all_statements.extend(extraction.statements)
        all_issues.extend(extraction.issues)
        unparsed.extend(extraction.unparsed_normative_sections)

    report = compare_normative_statements(all_statements)

    # Add source hashes
    source_hashes: dict[str, str] = {}
    for doc in documents:
        key = doc.path.as_posix() if doc.path else doc.source_kind
        source_hashes[key] = doc.source_hash
    report.source_hashes = source_hashes

    # Check if documents differ
    if len(documents) > 1:
        hashes = {d.source_hash for d in documents}
        report.documents_differ = len(hashes) > 1

    merged_unparsed = list(dict.fromkeys(
        list(report.unparsed_normative_sections) + list(unparsed)))
    report.unparsed_normative_sections = merged_unparsed

    # Block if there are issues or unparsed mandatory sections.
    if any(i.severity == Severity.ERROR for i in all_issues):
        report.ready_for_compilation = False
    if merged_unparsed:
        report.ready_for_compilation = False

    return report
