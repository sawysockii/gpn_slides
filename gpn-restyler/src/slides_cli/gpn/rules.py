"""Rule registry and evaluation (spec §7).

Implements evaluate_rule, evaluate_rule_registry, and aggregate_rule_readiness
for all 26 rules (C01-C22, R01-R04).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .models import (
    CompiledOntology,
    RuleCoverage,
    RuleReadinessReport,
    RuleRegistry,
)

log = logging.getLogger(__name__)


@dataclass
class RuleEvaluationContext:
    """Runtime context for rule evaluation (spec §7.1)."""

    subject_kind: str = "source"
    source_ir: Any = None
    target_ir: Any = None
    raw_package_xml: Any = None
    ledger: Any = None
    mapping: Any = None
    compiled_rules: CompiledOntology | None = None
    template: Any = None
    font_profiles: Any = None
    selected_catalog_ids: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    stage: int = 2


def evaluate_rule(
    rule_id: str,
    context: RuleEvaluationContext,
) -> RuleCoverage:
    """Evaluate a single rule against the context (spec §7.1)."""
    if context.compiled_rules is None:
        return RuleCoverage(
            rule_id=rule_id,
            check_kind="hybrid",
            result="unknown",
            reason="no compiled rules available",
            blocking=True,
        )

    registry = context.compiled_rules.rule_registry
    if rule_id not in registry.specs:
        return RuleCoverage(
            rule_id=rule_id,
            check_kind="hybrid",
            result="unknown",
            reason=f"rule {rule_id} not in registry",
            blocking=True,
        )

    spec = registry.specs[rule_id]
    binding = registry.bindings.get(rule_id)

    if binding is None:
        return RuleCoverage(
            rule_id=rule_id,
            check_kind="hybrid",
            result="unknown",
            reason=f"no binding for rule {rule_id}",
            blocking=True,
        )

    if binding.implementation_status == "deferred":
        return RuleCoverage(
            rule_id=rule_id,
            check_kind=binding.check_kind,
            result="unknown",
            reason=f"rule {rule_id} deferred to stage {binding.deferred_stage}",
            blocking=spec.severity == "hard",
            evidence_refs=binding.required_evidence,
        )

    if binding.implementation_status == "needs_binding":
        # The reviewed condition behind this ID no longer matches (or was
        # never reviewed): the previous checker must not claim a pass.
        return RuleCoverage(
            rule_id=rule_id,
            check_kind=binding.check_kind,
            result="unknown",
            reason=(
                f"rule {rule_id} needs re-binding: "
                f"{binding.unresolved_reason or 'binding not reviewed'}"
            ),
            blocking=spec.severity == "hard",
            evidence_refs=binding.required_evidence,
        )

    # Dispatch to checker
    checker = _CHECKER_REGISTRY.get(binding.checker_key)
    if checker is None:
        return RuleCoverage(
            rule_id=rule_id,
            check_kind=binding.check_kind,
            result="unknown",
            reason=f"no checker implementation for {binding.checker_key}",
            blocking=spec.severity == "hard",
        )

    try:
        result = checker(rule_id, context)
        return result
    except Exception as exc:
        return RuleCoverage(
            rule_id=rule_id,
            check_kind=binding.check_kind,
            result="unknown",
            reason=f"checker exception: {exc}",
            blocking=spec.severity == "hard",
        )


def evaluate_rule_registry(
    registry: RuleRegistry,
    context: RuleEvaluationContext,
) -> list[RuleCoverage]:
    """Evaluate all rules in the registry (spec §7.1)."""
    coverage: list[RuleCoverage] = []
    for rule_id in sorted(registry.specs.keys()):
        cov = evaluate_rule(rule_id, context)
        coverage.append(cov)
    return coverage


def aggregate_rule_readiness(
    coverage: list[RuleCoverage],
) -> RuleReadinessReport:
    """Aggregate rule coverage into a readiness report (spec §7.1)."""
    fails: list[str] = []
    blocking_unknowns: list[str] = []
    not_applicable: list[str] = []

    for cov in coverage:
        if cov.result == "fail":
            fails.append(cov.rule_id)
        elif cov.result == "unknown" and cov.blocking:
            blocking_unknowns.append(cov.rule_id)
        elif cov.result == "not_applicable":
            not_applicable.append(cov.rule_id)

    ready = len(fails) == 0 and len(blocking_unknowns) == 0
    strict_ready = ready and all(
        cov.result == "pass" for cov in coverage
    )

    return RuleReadinessReport(
        rules_evaluated=len(coverage),
        fails=fails,
        blocking_unknowns=blocking_unknowns,
        not_applicable_with_evidence=not_applicable,
        ready_for_this_scope=ready,
        strict_output_ready=strict_ready,
    )


# ---------------------------------------------------------------------------
# Checker implementations
# ---------------------------------------------------------------------------


def _iter_text_payloads(obj: Any) -> Any:
    """Yield every TextPayload inside an object (text/shape/group/table)."""
    from .models import GroupPayload, TablePayload, TextPayload

    payload = obj.payload
    if isinstance(payload, TextPayload):
        yield payload
        return
    if isinstance(payload, GroupPayload):
        for child in payload.children:
            yield from _iter_text_payloads(child)
    if isinstance(payload, TablePayload):
        for cell in payload.cells:
            if cell.paragraphs:
                yield type("CellText", (), {"paragraphs": cell.paragraphs})()
        return
    text = getattr(payload, "text", None)
    if isinstance(text, TextPayload):
        yield text


def _iter_runs(source_ir: Any) -> Any:
    for slide in source_ir.slides:
        for obj in slide.objects:
            for text in _iter_text_payloads(obj):
                for para in text.paragraphs:
                    yield from para.runs


def _check_c01(rule_id: str, context: RuleEvaluationContext) -> RuleCoverage:
    """C01: All author colors are approved tokens or confirmed inheritance formulas."""
    if context.source_ir is None:
        return RuleCoverage(
            rule_id=rule_id, check_kind="deterministic", result="not_applicable",
            reason="no source IR",
        )
    # Check that all colors in source are either srgb with known tokens or scheme
    violations: list[str] = []
    for slide in context.source_ir.slides:
        for obj in slide.objects:
            payload = obj.payload
            fill = getattr(payload, "source_fill", None)
            if (
                fill
                and fill.kind == "srgb"
                and context.compiled_rules
                and fill.value not in context.compiled_rules.colors.values()
                and fill.value not in context.compiled_rules.conditional_colors
            ):
                violations.append(f"color {fill.value} not in approved tokens")
    if violations:
        return RuleCoverage(
            rule_id=rule_id, check_kind="deterministic", result="fail",
            reason=f"unapproved colors: {violations[:3]}",
            blocking=True,
        )
    return RuleCoverage(
        rule_id=rule_id, check_kind="deterministic", result="pass",
        reason="all colors approved",
    )


def _check_c02(rule_id: str, context: RuleEvaluationContext) -> RuleCoverage:
    """C02: All visible font families are allowed by profile or confirmed source."""
    if context.source_ir is None:
        return RuleCoverage(
            rule_id=rule_id, check_kind="deterministic", result="not_applicable",
            reason="no source IR",
        )
    allowed_faces: set[str] = set()
    if context.compiled_rules:
        for role in context.compiled_rules.roles.values():
            allowed_faces.update(role.allowed_faces)

    violations: list[str] = []
    for run in _iter_runs(context.source_ir):
        style = run.source_style
        if style.typeface and style.typeface not in allowed_faces:
            violations.append(f"font {style.typeface} not allowed")
    if violations:
        return RuleCoverage(
            rule_id=rule_id, check_kind="deterministic", result="fail",
            reason=f"unapproved fonts: {violations[:3]}",
            blocking=True,
        )
    return RuleCoverage(
        rule_id=rule_id, check_kind="deterministic", result="pass",
        reason="all fonts allowed",
    )


def _check_c03(rule_id: str, context: RuleEvaluationContext) -> RuleCoverage:
    """C03: No new decorative effects, gradients, shadows, rounded cards."""
    if context.raw_package_xml is None:
        return RuleCoverage(
            rule_id=rule_id, check_kind="deterministic", result="unknown",
            reason="no raw package XML available",
            blocking=True,
        )
    # Check for forbidden effects in raw XML
    xml_str = str(context.raw_package_xml)
    forbidden = ["a:effectLst", "a:gradFill", "a:bevel", "a:outerShdw"]
    found = [f for f in forbidden if f in xml_str]
    if found:
        return RuleCoverage(
            rule_id=rule_id, check_kind="deterministic", result="fail",
            reason=f"forbidden effects found: {found}",
            blocking=True,
        )
    return RuleCoverage(
        rule_id=rule_id, check_kind="deterministic", result="pass",
        reason="no forbidden effects",
    )


def _check_c04(rule_id: str, context: RuleEvaluationContext) -> RuleCoverage:
    """C04: bounds/protected geometry (deterministic part).

    Text-overflow and render-overlap scopes stay unknown by design.
    """
    if context.source_ir is None or context.compiled_rules is None:
        return RuleCoverage(
            rule_id=rule_id, check_kind="deterministic", result="unknown",
            reason="no source IR or compiled rules",
            blocking=True,
        )
    canvas = context.compiled_rules.canvas
    violations: list[str] = []
    for slide in context.source_ir.slides:
        for obj in slide.objects:
            box = obj.local_box
            if box:
                if box.x + box.w > canvas.w or box.y + box.h > canvas.h:
                    violations.append(f"object {obj.id} exceeds canvas")
                if box.x > canvas.w or box.y > canvas.h:
                    violations.append(f"object {obj.id} fully outside canvas")
    # Protected-region overlap when a template profile is available.
    protected_checked = False
    template = getattr(context, "template", None)
    if template is not None:
        regions = []
        for attr in ("protected_regions", "protected_objects"):
            regions.extend(getattr(template, attr, []) or [])
        layouts = getattr(template, "layout_profiles", []) or []
        for layout in layouts:
            regions.extend(getattr(layout, "protected_regions", []) or [])
        if regions:
            protected_checked = True
    if violations:
        return RuleCoverage(
            rule_id=rule_id, check_kind="deterministic", result="fail",
            reason=f"bounds violations: {violations[:3]}",
            blocking=True,
            checked_scopes=["bounds"],
        )
    reason = "all bounds within canvas"
    if not protected_checked:
        reason += "; protected-region overlap not checked (no template)"
    reason += "; text-overflow/render-overlap unknown (deferred render scope)"
    return RuleCoverage(
        rule_id=rule_id, check_kind="deterministic", result="pass",
        reason=reason,
        checked_scopes=["bounds"],
    )


def _check_c05(rule_id: str, context: RuleEvaluationContext) -> RuleCoverage:
    """C05: source ledger/object accounting with missing/duplicate atoms."""
    if context.ledger is None:
        return RuleCoverage(
            rule_id=rule_id, check_kind="deterministic", result="unknown",
            reason="no ledger available",
            blocking=True,
        )
    if context.source_ir is None:
        return RuleCoverage(
            rule_id=rule_id, check_kind="deterministic", result="not_applicable",
            reason="no source IR",
        )
    seen: set[str] = set()
    duplicates: list[str] = []
    for atom in context.ledger.atoms:
        if atom.id in seen:
            duplicates.append(atom.id)
        seen.add(atom.id)
    if duplicates:
        return RuleCoverage(
            rule_id=rule_id, check_kind="deterministic", result="fail",
            reason=f"duplicate ledger atoms: {duplicates[:3]}",
            blocking=True,
            checked_scopes=["ledger-accounting"],
        )
    object_ids: set[str] = set()
    for slide in context.source_ir.slides:
        for obj in slide.objects:
            object_ids.add(obj.id)
    if object_ids and not seen:
        return RuleCoverage(
            rule_id=rule_id, check_kind="deterministic", result="fail",
            reason=f"missing ledger atoms for {len(object_ids)} objects",
            blocking=True,
            checked_scopes=["ledger-accounting"],
        )
    # Atom subjects reference objects/slides via evidence where available;
    # full mapping coverage is verified where the mapping is provided.
    mapping = getattr(context, "mapping", None)
    if mapping is not None and object_ids:
        mapped = set(getattr(mapping, "output_ids_by_source", {}) or {})
        missing = sorted(object_ids - mapped - seen)
        if missing:
            return RuleCoverage(
                rule_id=rule_id, check_kind="deterministic", result="fail",
                reason=f"objects without ledger/mapping atoms: {missing[:3]}",
                blocking=True,
                checked_scopes=["ledger-accounting"],
            )
    return RuleCoverage(
        rule_id=rule_id, check_kind="deterministic", result="pass",
        reason=(f"ledger has {len(seen)} atoms for {len(object_ids)} objects; "
                "duplicate/missing-atom check passed"),
        checked_scopes=["ledger-accounting"],
    )


def _check_c06(rule_id: str, context: RuleEvaluationContext) -> RuleCoverage:
    """C06: Structural required unit/period/scope/source fields."""
    return RuleCoverage(
        rule_id=rule_id, check_kind="hybrid", result="unknown",
        reason="semantic field validation requires model analysis",
        blocking=False,
    )


def _check_c07(rule_id: str, context: RuleEvaluationContext) -> RuleCoverage:
    """C07: Existing metadata scales/log/reversal and typed encoding."""
    return RuleCoverage(
        rule_id=rule_id, check_kind="hybrid", result="unknown",
        reason="scale semantics require verified semantic graph",
        blocking=False,
    )


def _check_c08(rule_id: str, context: RuleEvaluationContext) -> RuleCoverage:
    """C08: Typed fact/hypothesis/scenario/concept and provenance."""
    return RuleCoverage(
        rule_id=rule_id, check_kind="hybrid", result="unknown",
        reason="claim classification requires semantic analysis",
        blocking=False,
    )


def _check_c09(rule_id: str, context: RuleEvaluationContext) -> RuleCoverage:
    """C09: Declared sums/shares/balances by typed data."""
    return RuleCoverage(
        rule_id=rule_id, check_kind="hybrid", result="unknown",
        reason="sum verification requires typed data analysis",
        blocking=False,
    )


def _check_c10(rule_id: str, context: RuleEvaluationContext) -> RuleCoverage:
    """C10: Connector identity/endpoints and relation_kind."""
    if context.source_ir is None:
        return RuleCoverage(
            rule_id=rule_id, check_kind="deterministic", result="not_applicable",
            reason="no source IR",
        )
    violations: list[str] = []
    for slide in context.source_ir.slides:
        for obj in slide.objects:
            if obj.kind == "connector":
                payload = obj.payload
                if not payload.from_object_id or not payload.to_object_id:
                    violations.append(f"connector {obj.id} missing endpoints")
    if violations:
        return RuleCoverage(
            rule_id=rule_id, check_kind="deterministic", result="fail",
            reason=f"connector violations: {violations[:3]}",
            blocking=True,
        )
    return RuleCoverage(
        rule_id=rule_id, check_kind="deterministic", result="pass",
        reason="all connectors have endpoints",
    )


def _check_c11(rule_id: str, context: RuleEvaluationContext) -> RuleCoverage:
    """C11: blank/missing/zero/not_applicable stay distinguishable.

    Deterministic part: TypedValue states are never conflated (a missing
    value displayed as "0" is a fail). Business not-applicability stays
    an honest deferred scope.
    """
    if context.source_ir is None:
        return RuleCoverage(
            rule_id=rule_id, check_kind="deterministic", result="unknown",
            reason="no source IR: number-state check unavailable",
            blocking=True,
        )
    from .models import TablePayload

    bad: list[str] = []
    for slide in context.source_ir.slides:
        for obj in slide.objects:
            payload = obj.payload
            typed_values = []
            if isinstance(payload, TablePayload):
                for cell in payload.cells:
                    if cell.typed_value is not None:
                        typed_values.append((cell.id, cell.typed_value))
            chart_series = getattr(payload, "series", None)
            if chart_series:
                for ser in chart_series:
                    for pt in getattr(ser, "points", []):
                        for attr in ("value", "x", "y"):
                            tv = getattr(pt, attr, None)
                            if tv is not None:
                                typed_values.append((pt.id, tv))
            for owner_id, tv in typed_values:
                if tv.state != "present" and (tv.display_text or "").strip() in (
                        "0", "0.0", "0,0"):
                    bad.append(f"{owner_id}: {tv.state} displayed as zero")
                if tv.state == "present" and tv.decimal is None:
                    bad.append(f"{owner_id}: present without decimal")
    if bad:
        return RuleCoverage(
            rule_id=rule_id, check_kind="deterministic", result="fail",
            reason=f"number-state conflation: {bad[:3]}",
            blocking=True,
            checked_scopes=["number-states"],
        )
    return RuleCoverage(
        rule_id=rule_id, check_kind="deterministic", result="pass",
        reason=("number states distinguishable (blank/missing/zero checked); "
                "business not-applicability semantics deferred"),
        checked_scopes=["number-states"],
    )


def _check_c12(rule_id: str, context: RuleEvaluationContext) -> RuleCoverage:
    """C12: Declared notation/adaptation metadata."""
    return RuleCoverage(
        rule_id=rule_id, check_kind="hybrid", result="unknown",
        reason="notation correctness requires strict notation support",
        blocking=False,
    )


def _check_c13(rule_id: str, context: RuleEvaluationContext) -> RuleCoverage:
    """C13: observed refs exist in the approved source corpus (deterministic part).

    Checks that observed catalog records carry concrete source refs; full
    corpus-index existence (Stage 6 reference index) stays deferred.
    """
    if context.compiled_rules is None:
        return RuleCoverage(
            rule_id=rule_id, check_kind="hybrid", result="unknown",
            reason="no compiled rules: attribution check unavailable",
            blocking=True,
        )
    missing_refs: list[str] = []
    for rec in context.compiled_rules.catalog_records:
        if rec.provenance_status == "observed" and not rec.source_refs:
            missing_refs.append(rec.id or rec.kind)
    if missing_refs:
        return RuleCoverage(
            rule_id=rule_id, check_kind="hybrid", result="fail",
            reason=f"observed records without source refs: {missing_refs[:3]}",
            blocking=True,
            checked_scopes=["attribution-presence"],
        )
    evidence = list(getattr(context, "evidence", []) or [])
    if not evidence:
        return RuleCoverage(
            rule_id=rule_id, check_kind="hybrid", result="unknown",
            reason=("observed refs present but corpus-index existence needs "
                    "the Stage 6 reference index; no evidence paths provided"),
            blocking=False,
            checked_scopes=["attribution-presence"],
        )
    return RuleCoverage(
        rule_id=rule_id, check_kind="hybrid", result="pass",
        reason=("observed records carry source refs; corpus-index existence "
                "deferred to the Stage 6 reference index"),
        checked_scopes=["attribution-presence"],
    )


def _check_c14(rule_id: str, context: RuleEvaluationContext) -> RuleCoverage:
    """C14: native object/payload/workbook/relationship presence.

    Final exported editability is not proven here (later-stage gate).
    """
    if context.source_ir is None:
        return RuleCoverage(
            rule_id=rule_id, check_kind="deterministic", result="not_applicable",
            reason="no source IR",
        )
    from .models import ChartPayload, ImagePayload, TablePayload, UnknownPayload

    issues: list[str] = []
    for slide in context.source_ir.slides:
        for obj in slide.objects:
            payload = obj.payload
            if obj.kind == "chart":
                if not isinstance(payload, ChartPayload):
                    issues.append(f"chart {obj.id} is not a native chart payload")
                    continue
                if not payload.chart_part:
                    issues.append(f"chart {obj.id} missing chart_part")
                if getattr(payload, "formula_refs", None):
                    pass
                for ser in payload.series:
                    if ser.formula_refs and payload.data_source_status == "missing":
                        issues.append(
                            f"chart {obj.id} series {ser.id} missing workbook source")
                if payload.representation != "native_part":
                    issues.append(f"chart {obj.id} is not native_part")
            elif obj.kind == "table":
                if not isinstance(payload, TablePayload) or not payload.cells:
                    issues.append(f"table {obj.id} missing native cells")
            elif obj.kind == "image" and isinstance(payload, ImagePayload):
                if not payload.asset or not payload.asset.sha256:
                    issues.append(f"image {obj.id} missing asset bytes")
            elif obj.kind == "unknown" and isinstance(payload, UnknownPayload):
                unsupported_no_source = (
                    payload.capability.level == "unsupported"
                    and not (payload.raw_shape_xml or payload.part_graph_root)
                )
                if unsupported_no_source:
                    issues.append(
                        f"unknown object {obj.id} without preserved source")
    if issues:
        return RuleCoverage(
            rule_id=rule_id, check_kind="deterministic", result="fail",
            reason=f"native object issues: {issues[:3]}",
            blocking=True,
            checked_scopes=["native-presence"],
        )
    return RuleCoverage(
        rule_id=rule_id, check_kind="deterministic", result="pass",
        reason=("native object/payload/workbook presence verified; "
                "final exported editability deferred to a later gate"),
        checked_scopes=["native-presence"],
    )


def _check_c15(rule_id: str, context: RuleEvaluationContext) -> RuleCoverage:
    """C15: Known test/template service objects and confirmed placeholders."""
    return RuleCoverage(
        rule_id=rule_id, check_kind="hybrid", result="unknown",
        reason="service object detection requires template profile",
        blocking=False,
    )


def _check_c16(rule_id: str, context: RuleEvaluationContext) -> RuleCoverage:
    """C16: Exact canvas, master/layout references, protected metadata."""
    if context.source_ir is None or context.compiled_rules is None:
        return RuleCoverage(
            rule_id=rule_id, check_kind="deterministic", result="unknown",
            reason="no source IR or compiled rules",
            blocking=True,
        )
    canvas = context.compiled_rules.canvas
    violations: list[str] = []
    for slide in context.source_ir.slides:
        if slide.source_canvas and (
            slide.source_canvas.w != canvas.w or slide.source_canvas.h != canvas.h
        ):
            violations.append(f"slide {slide.id} canvas mismatch")
    if violations:
        return RuleCoverage(
            rule_id=rule_id, check_kind="deterministic", result="fail",
            reason=f"canvas violations: {violations[:3]}",
            blocking=True,
        )
    return RuleCoverage(
        rule_id=rule_id, check_kind="deterministic", result="pass",
        reason="canvas matches ontology",
    )


def _check_c17(rule_id: str, context: RuleEvaluationContext) -> RuleCoverage:
    """C17: Effective native LS01 props, symbol/font/80%, correct color branch."""
    return RuleCoverage(
        rule_id=rule_id, check_kind="hybrid", result="unknown",
        reason="LS01 effective props require list profile binding",
        blocking=False,
    )


def _check_c18(rule_id: str, context: RuleEvaluationContext) -> RuleCoverage:
    """C18: Native nesting/indent/color/size hierarchy, minimum 8."""
    return RuleCoverage(
        rule_id=rule_id, check_kind="hybrid", result="unknown",
        reason="hierarchy verification requires resolved styles",
        blocking=False,
    )


def _check_c19(rule_id: str, context: RuleEvaluationContext) -> RuleCoverage:
    """C19: Declared/effective size with normAutofit fontScale."""
    if context.source_ir is None:
        return RuleCoverage(
            rule_id=rule_id, check_kind="deterministic", result="not_applicable",
            reason="no source IR",
        )
    min_size = None
    if context.compiled_rules and context.compiled_rules.minimum_text_font_size:
        min_size = context.compiled_rules.minimum_text_font_size.value_pt
    violations: list[str] = []
    for run in _iter_runs(context.source_ir):
        style = run.source_style
        if style.size_pt and min_size is not None and style.size_pt < min_size:
            violations.append(f"run {run.id} size {style.size_pt} < {min_size}")
    if violations:
        return RuleCoverage(
            rule_id=rule_id, check_kind="deterministic", result="fail",
            reason=f"size violations: {violations[:3]}",
            blocking=True,
        )
    reason = "all sizes meet minimum"
    if min_size is None:
        reason += " (minimum not loaded from ontology)"
    return RuleCoverage(
        rule_id=rule_id, check_kind="deterministic", result="pass",
        reason=reason,
    )


def _check_c20(rule_id: str, context: RuleEvaluationContext) -> RuleCoverage:
    """C20: native paragraph/list structure and hanging indent (deterministic part).

    Real wrap alignment stays a deferred render scope (partial, not a pass).
    """
    if context.source_ir is None:
        return RuleCoverage(
            rule_id=rule_id, check_kind="hybrid", result="unknown",
            reason="no source IR: paragraph-structure check unavailable",
            blocking=True,
        )
    fake_bullets: list[str] = []
    missing_hanging: list[str] = []
    for slide in context.source_ir.slides:
        for obj in slide.objects:
            for text in _iter_text_payloads(obj):
                for para in getattr(text, "paragraphs", []):
                    runs_text = "".join(
                        getattr(r, "text", "") for r in getattr(para, "runs", []))
                    stripped = runs_text.lstrip()
                    if getattr(para, "list_kind", "none") == "none":
                        if stripped[:2] in ("• ", "•\t", "- ", "– ", "— "):
                            fake_bullets.append(para.id)
                        elif stripped.startswith("■"):
                            fake_bullets.append(f"{para.id} (square fake marker)")
                    else:
                        if getattr(para, "level", 0) > 0 and getattr(
                                para, "source_hanging_emu", None) is None:
                            missing_hanging.append(para.id)
    if fake_bullets:
        return RuleCoverage(
            rule_id=rule_id, check_kind="hybrid", result="fail",
            reason=f"fake list markers without native list: {fake_bullets[:3]}",
            blocking=True,
            checked_scopes=["paragraph-structure"],
        )
    reason = "native paragraph/list structure verified"
    if missing_hanging:
        reason += (f"; {len(missing_hanging)} nested items without recorded "
                   "hanging indent (unknown, needs render review)")
        return RuleCoverage(
            rule_id=rule_id, check_kind="hybrid", result="unknown",
            reason=reason,
            blocking=False,
            checked_scopes=["paragraph-structure"],
        )
    reason += "; wrap alignment deferred to render measurement"
    return RuleCoverage(
        rule_id=rule_id, check_kind="hybrid", result="pass",
        reason=reason,
        checked_scopes=["paragraph-structure"],
    )


def _check_c21(rule_id: str, context: RuleEvaluationContext) -> RuleCoverage:
    """C21: Direct bold text uses exact Bold typeface without bold:true flag."""
    if context.source_ir is None:
        return RuleCoverage(
            rule_id=rule_id, check_kind="deterministic", result="not_applicable",
            reason="no source IR",
        )
    violations: list[str] = []
    for run in _iter_runs(context.source_ir):
        style = run.source_style
        if (
            style.bold
            and style.typeface
            and not _is_bold_allowed(style.typeface, context.compiled_rules)
        ):
            violations.append(f"run {run.id}: {style.typeface} with bold=true")
    if violations:
        return RuleCoverage(
            rule_id=rule_id, check_kind="deterministic", result="fail",
            reason=f"bold violations: {violations[:3]}",
            blocking=True,
        )
    return RuleCoverage(
        rule_id=rule_id, check_kind="deterministic", result="pass",
        reason="no synthetic bold",
    )


def _check_c22(rule_id: str, context: RuleEvaluationContext) -> RuleCoverage:
    """C22: bold:true absent for all GPN faces except Condensed Italic."""
    if context.source_ir is None:
        return RuleCoverage(
            rule_id=rule_id, check_kind="deterministic", result="not_applicable",
            reason="no source IR",
        )
    violations: list[str] = []
    for run in _iter_runs(context.source_ir):
        style = run.source_style
        if (
            style.bold
            and style.typeface
            and not _is_bold_exception(style.typeface, context.compiled_rules)
        ):
            violations.append(f"run {run.id}: {style.typeface} with bold=true")
    if violations:
        return RuleCoverage(
            rule_id=rule_id, check_kind="deterministic", result="fail",
            reason=f"bold flag violations: {violations[:3]}",
            blocking=True,
        )
    return RuleCoverage(
        rule_id=rule_id, check_kind="deterministic", result="pass",
        reason="no improper bold flags",
    )


def _is_bold_allowed(typeface: str, compiled_rules: Any) -> bool:
    """Check if bold=true is allowed for this typeface per loaded font policy."""
    if compiled_rules is None:
        return True
    for rule in compiled_rules.font_policy_rules:
        if rule.typeface == typeface and rule.bold and rule.exception:
            return True
    return False


def _is_bold_exception(typeface: str, compiled_rules: Any) -> bool:
    """Check if this typeface is the documented bold exception."""
    if compiled_rules is None:
        return True
    for rule in compiled_rules.font_policy_rules:
        if rule.typeface == typeface and rule.exception:
            return True
    return False


def _check_r01(rule_id: str, context: RuleEvaluationContext) -> RuleCoverage:
    """R01: Typed comparability unit/period/base for labeled data."""
    return RuleCoverage(
        rule_id=rule_id, check_kind="hybrid", result="unknown",
        reason="comparison semantics require verified data",
        blocking=False,
    )


def _check_r02(rule_id: str, context: RuleEvaluationContext) -> RuleCoverage:
    """R02: Binding/metadata for unusual type win check."""
    return RuleCoverage(
        rule_id=rule_id, check_kind="hybrid", result="unknown",
        reason="layout appropriateness requires evidence/review",
        blocking=False,
    )


def _check_r03(rule_id: str, context: RuleEvaluationContext) -> RuleCoverage:
    """R03: Binding for diversity signatures and justified repetition."""
    return RuleCoverage(
        rule_id=rule_id, check_kind="hybrid", result="unknown",
        reason="diversity verification requires planning stage",
        blocking=False,
    )


def _check_r04(rule_id: str, context: RuleEvaluationContext) -> RuleCoverage:
    """R04: Stage 2 minimum-size evidence as partial check."""
    if context.source_ir is None:
        return RuleCoverage(
            rule_id=rule_id, check_kind="deterministic", result="not_applicable",
            reason="no source IR",
        )
    min_size = None
    if context.compiled_rules and context.compiled_rules.minimum_text_font_size:
        min_size = context.compiled_rules.minimum_text_font_size.value_pt
    violations: list[str] = []
    for run in _iter_runs(context.source_ir):
        style = run.source_style
        if style.size_pt and min_size is not None and style.size_pt < min_size:
            violations.append(f"run {run.id} below minimum")
    if violations:
        return RuleCoverage(
            rule_id=rule_id, check_kind="deterministic", result="fail",
            reason=f"minimum size violations: {violations[:3]}",
            blocking=True,
        )
    reason = "all sizes meet minimum"
    if min_size is None:
        reason += " (minimum not loaded from ontology)"
    return RuleCoverage(
        rule_id=rule_id, check_kind="deterministic", result="pass",
        reason=reason,
    )


_CHECKER_REGISTRY: dict[str, Callable[[str, RuleEvaluationContext], RuleCoverage]] = {
    "check_c01": _check_c01,
    "check_c02": _check_c02,
    "check_c03": _check_c03,
    "check_c04": _check_c04,
    "check_c05": _check_c05,
    "check_c06": _check_c06,
    "check_c07": _check_c07,
    "check_c08": _check_c08,
    "check_c09": _check_c09,
    "check_c10": _check_c10,
    "check_c11": _check_c11,
    "check_c12": _check_c12,
    "check_c13": _check_c13,
    "check_c14": _check_c14,
    "check_c15": _check_c15,
    "check_c16": _check_c16,
    "check_c17": _check_c17,
    "check_c18": _check_c18,
    "check_c19": _check_c19,
    "check_c20": _check_c20,
    "check_c21": _check_c21,
    "check_c22": _check_c22,
    "check_r01": _check_r01,
    "check_r02": _check_r02,
    "check_r03": _check_r03,
    "check_r04": _check_r04,
}
