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
    # Check for forbidden effects in raw XML.
    # Recovery §5 (resolve_effective_shape_effects): a shape without a
    # direct a:effectLst is NOT effect-free. python-pptx inserts p:style
    # with a:effectRef idx=1/2 pointing at theme effectStyleLst entries
    # that contain a:outerShdw. Any surviving effectRef therefore inherits
    # a prohibited shadow and must fail — the emitter is required to strip
    # it (opc_adapter.strip_shape_effect_inheritance) and write an explicit
    # empty a:effectLst.
    import re as _re

    xml_str = str(context.raw_package_xml)
    # An explicit EMPTY effectLst is the normative stripped state written
    # by strip_shape_effect_inheritance — it proves no effect. Only a
    # non-empty effectLst (with effect children) is forbidden.
    scrubbed = _re.sub(r"<a:effectLst\s*/>", "", xml_str)
    scrubbed = _re.sub(r"<a:effectLst\s*>(\s*)</a:effectLst>", "", scrubbed)
    forbidden = ["a:effectLst", "a:gradFill", "a:bevel", "a:outerShdw"]
    found = [f for f in forbidden if f in scrubbed]
    if found:
        return RuleCoverage(
            rule_id=rule_id, check_kind="deterministic", result="fail",
            reason=f"forbidden effects found: {found}",
            blocking=True,
        )
    if "a:effectRef" in xml_str or "effectRef" in xml_str:
        return RuleCoverage(
            rule_id=rule_id, check_kind="deterministic", result="fail",
            reason="inherited effectRef survives: theme effectStyleLst "
                   "entries contain outerShdw; strip_shape_effect_inheritance "
                   "required (no direct effectLst is not proof of no effect)",
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


def check_saved_authoring_rules(
    package_path: Any,
    compiled_rules: Any,
) -> dict[str, Any]:
    """Check C01/C03/C21/C22 on reopened actual bytes (recovery §5).

    Reads the saved candidate PPTX via zip/XML — never the expected IR,
    never a stale cache. Every normative value comes from the loaded
    ontology snapshot (palette, font policy); no literal allowlist lives
    here.

    Checks (authoring scope: shape fills/lines, text runs; photo pixels
    are out of palette scope by checker design):
    - C01: every srgbClr in authoring fills/runs is in the snapshot
      palette (direct values + conditional rgb).
    - C03: no roundRect preset geometry; no surviving a:effectRef
      (theme inheritance); no non-empty direct a:effectLst.
    - C21/C22: no b="1" except the documented exception face.
    - surface_foreground: every text run carries an explicit solidFill
      (inherited tx1 on an unknown surface is unreadable — the bad
      candidate failure mode).
    """
    import zipfile

    from lxml import etree

    report: dict[str, Any] = {
        "candidate": str(package_path),
        "rules": {},
        "hard_fail": False,
    }
    palette: set[str] = set()
    if compiled_rules is not None:
        colors = dict(getattr(compiled_rules, "colors", {}) or {})
        palette = {str(v).strip().lstrip("#").upper()
                   for v in colors.values()}
        conditional = getattr(compiled_rules, "conditional_colors", {}) or {}
        if isinstance(conditional, dict):
            for cond in conditional.values():
                rgb = getattr(cond, "rgb", "")
                if rgb:
                    palette.add(str(rgb).strip().lstrip("#").upper())
    exception_faces: set[str] = set()
    if compiled_rules is not None:
        for rule in list(getattr(compiled_rules, "font_policy_rules",
                                 []) or []):
            if getattr(rule, "exception", False) and getattr(
                    rule, "typeface", ""):
                exception_faces.add(str(rule.typeface))

    c01_bad: list[str] = []
    c03_bad: list[str] = []
    bold_bad: list[str] = []
    fg_bad: list[str] = []
    slides_checked = 0
    try:
        with zipfile.ZipFile(package_path) as archive:
            names = [n for n in archive.namelist()
                     if n.startswith("ppt/slides/slide") and n.endswith(".xml")]
            for name in sorted(names):
                data = archive.read(name)
                try:
                    root = etree.fromstring(data)
                except Exception:  # noqa: BLE001 - corrupt part reported
                    c03_bad.append(f"{name}: unparseable slide XML")
                    continue
                slides_checked += 1
                ns = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main",
                      "p": "http://schemas.openxmlformats.org/presentationml/2006/main"}
                for sp in root.findall(".//p:sp", ns) + root.findall(
                        ".//p:cxnSp", ns):
                    geom = sp.find(".//a:prstGeom", ns)
                    if geom is not None and geom.get("prst") == "roundRect":
                        c03_bad.append(
                            f"{name}: roundRect decorative geometry")
                    for ref in sp.findall(".//a:effectRef", ns):
                        c03_bad.append(
                            f"{name}: surviving effectRef idx="
                            f"{ref.get('idx')} (theme shadow inheritance)")
                    for lst in sp.findall(".//a:effectLst", ns):
                        if len(list(lst)) > 0:
                            c03_bad.append(
                                f"{name}: non-empty direct effectLst")
                    for clr in sp.findall(
                            ".//a:solidFill/a:srgbClr", ns):
                        val = str(clr.get("val", "")).upper()
                        # Shape fills live inside p:spPr (authoring scope);
                        # fills inside a:rPr are run foregrounds below.
                        parent = clr.getparent()
                        grand = parent.getparent() if parent is not None else None
                        if grand is not None and grand.tag.endswith("}rPr"):
                            continue
                        if val and val not in palette:
                            c01_bad.append(
                                f"{name}: authoring fill {val} not in palette")
                for rpr in root.findall(".//a:rPr", ns):
                    latin = rpr.find("a:latin", ns)
                    face = latin.get("typeface", "") if latin is not None else ""
                    if rpr.get("b") == "1" and face not in exception_faces:
                        bold_bad.append(
                            f"{name}: b=1 on {face or 'unknown face'}")
                    fill = rpr.find("a:solidFill", ns)
                    if fill is None:
                        fg_bad.append(
                            f"{name}: run without explicit foreground "
                            f"(face={face or '?'})")
                    else:
                        clr = fill.find("a:srgbClr", ns)
                        if clr is not None:
                            val = str(clr.get("val", "")).upper()
                            if val and val not in palette:
                                c01_bad.append(
                                    f"{name}: run color {val} not in palette")
    except FileNotFoundError:
        report["error"] = f"candidate not found: {package_path}"
        report["hard_fail"] = True
        return report
    except Exception as exc:  # noqa: BLE001 - honest failure, not pass
        report["error"] = f"saved-bytes read failed: {exc}"
        report["hard_fail"] = True
        return report

    def _entry(passed: bool, violations: list[str],
               detail: str) -> dict[str, Any]:
        return {"result": "pass" if passed else "fail",
                "violations": violations[:10],
                "violation_count": len(violations), "detail": detail}

    report["slides_checked"] = slides_checked
    report["rules"]["C01"] = _entry(
        not c01_bad, c01_bad,
        "all saved authoring srgbClr in snapshot palette")
    report["rules"]["C03"] = _entry(
        not c03_bad, c03_bad,
        "no roundRect/effectRef/non-empty effectLst in saved bytes")
    report["rules"]["C21_C22"] = _entry(
        not bold_bad, bold_bad,
        "no b=1 except documented exception face")
    report["rules"]["surface_foreground"] = _entry(
        not fg_bad, fg_bad,
        "every run carries explicit foreground")
    report["hard_fail"] = bool(c01_bad or c03_bad or bold_bad or fg_bad)
    return report


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


# ---------------------------------------------------------------------------
# §5.1: rule application coverage on saved bytes
# ---------------------------------------------------------------------------

# Rules whose scope is native charts only: without such objects in the
# saved inventory they are not_applicable (with inventory proof), never a
# silent pass and never an artificial blocker. Measurement/status/list
# rules (C06/C08/C09/C12/C15/C17/C18) apply to metric text and layout too
# and go through deterministic checkers or hybrid harness review.
_CHART_ONLY_RULES: tuple[str, ...] = ("C07",)
_TABLE_ONLY_RULES: tuple[str, ...] = ()


def _saved_inventory(drawing_inventory: dict[str, int]) -> dict[str, bool]:
    return {
        "has_chart": bool(drawing_inventory.get("graphicFrame", 0)),
        "has_table": bool(drawing_inventory.get("graphicFrame", 0)),
        "has_image": bool(drawing_inventory.get("image", 0)),
        "has_connector": bool(drawing_inventory.get("connector", 0)),
    }


def build_rule_applications(
    compiled: Any,
    semantic: Any,
    saved_inventory: dict[str, int],
) -> list[dict[str, Any]]:
    """One application per registry rule with applicability evidence (§5.1).

    Applicability is decided from the actual saved inventory + semantic
    projection, never from "reports we found". Unknown new norms get
    needs_binding, never a silent pass.
    """
    inv = _saved_inventory(saved_inventory or {})
    registry = getattr(compiled, "rule_registry", None)
    specs = getattr(registry, "specs", {}) or {}
    bindings = getattr(registry, "bindings", {}) or {}
    applications: list[dict[str, Any]] = []
    for rule_id in sorted(specs):
        binding = bindings.get(rule_id)
        status = getattr(binding, "implementation_status", "deferred")
        applicable = True
        na_reason = ""
        if rule_id in _CHART_ONLY_RULES and not inv["has_chart"]:
            applicable = False
            na_reason = ("chart-only rule; saved inventory has no "
                         "native chart graphicFrames")
        elif rule_id in _TABLE_ONLY_RULES and not inv["has_table"]:
            applicable = False
            na_reason = ("table-only rule; saved inventory has no "
                         "native table graphicFrames")
        applications.append({
            "rule_id": rule_id,
            "severity": getattr(specs[rule_id], "severity", "hard"),
            "condition": getattr(specs[rule_id], "condition", ""),
            "implementation_status": status,
            "applicable": applicable,
            "not_applicable_reason": na_reason,
            "inventory_evidence": [
                f"drawing_inventory={dict(saved_inventory or {})}"],
        })
    return applications


def evaluate_saved_rules(
    applications: list[dict[str, Any]],
    *,
    candidate_path: Any,
    candidate_after_ir: Any,
    compiled: Any,
    candidate_sha256: str = "",
    model_evidence: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Evaluate rule applications on reopened saved bytes (§5.1).

    Deterministic checkers run on the reimported candidate IR; C01/C03/
    C21/C22 additionally run on raw saved bytes via
    check_saved_authoring_rules. Deferred/hybrid scopes are resolved by
    typed harness rule-review evidence (honest hybrid checker); without
    it they stay honest unknown (blocking for hard rules). Inapplicable
    rules carry inventory proof. Normative values always come from the
    compiled snapshot.
    """
    import hashlib as _hashlib

    model_evidence = model_evidence or {}
    context = RuleEvaluationContext(
        subject_kind="saved", source_ir=candidate_after_ir,
        compiled_rules=compiled)
    saved_bytes = check_saved_authoring_rules(candidate_path, compiled)
    saved_rules = saved_bytes.get("rules", {}) if isinstance(
        saved_bytes, dict) else {}

    evaluated: list[dict[str, Any]] = []
    for app in applications:
        rid = app["rule_id"]
        if not app["applicable"]:
            evaluated.append({**app, "result": "not_applicable",
                              "reason": app["not_applicable_reason"],
                              "blocking": False})
            continue
        if app["implementation_status"] == "needs_binding":
            evaluated.append({**app, "result": "unknown",
                              "reason": "unknown norm: needs_binding",
                              "blocking": app["severity"] == "hard"})
            continue
        mev = model_evidence.get(rid)
        if mev is not None and app["applicable"]:
            verdict = str(mev.get("verdict", ""))
            if verdict == "pass":
                evaluated.append({
                    **app, "result": "pass", "check_kind": "hybrid",
                    "reason": f"harness rule review: {mev.get('proof', '')}",
                    "blocking": False,
                    "checked_scopes": [mev.get("subscope", "rule")],
                    "evidence_refs": [mev.get("task_id", "rule_review")]})
                continue
            if verdict == "fail":
                evaluated.append({
                    **app, "result": "fail", "check_kind": "hybrid",
                    "reason": f"harness rule review: {mev.get('proof', '')}",
                    "blocking": True,
                    "checked_scopes": [mev.get("subscope", "rule")],
                    "evidence_refs": [mev.get("task_id", "rule_review")]})
                continue
        coverage = evaluate_rule(rid, context)
        if coverage.result == "unknown" and mev is not None and \
                str(mev.get("verdict", "")) in ("pass", "fail"):
            verdict = str(mev["verdict"])
            coverage = RuleCoverage(
                rule_id=rid, check_kind="hybrid",
                result=verdict,  # type: ignore[arg-type]
                reason=f"harness rule review: {mev.get('proof', '')}",
                blocking=verdict == "fail",
                checked_scopes=[str(mev.get("subscope", "rule"))],
                evidence_refs=[str(mev.get("task_id", "rule_review"))])
        entry: dict[str, Any] = {
            **app, "result": coverage.result, "reason": coverage.reason,
            "check_kind": coverage.check_kind,
            "blocking": coverage.blocking,
            "checked_scopes": list(coverage.checked_scopes),
            "evidence_refs": list(coverage.evidence_refs),
        }
        # Saved-bytes authoring checks override/confirm C01/C03/C21_C22.
        saved = saved_rules.get(rid) or saved_rules.get(
            rid.replace("C21_C22", "C21")) or {}
        if isinstance(saved, dict) and saved.get("status"):
            entry["saved_bytes_status"] = saved["status"]
            if saved["status"] == "fail":
                entry["result"] = "fail"
                entry["blocking"] = True
                entry["reason"] = (saved.get("reason", "") or
                                   entry["reason"])
        evaluated.append(entry)

    fails = [e["rule_id"] for e in evaluated if e["result"] == "fail"]
    blocking_unknowns = [e["rule_id"] for e in evaluated
                         if e["result"] == "unknown" and e["blocking"]]
    na = [e["rule_id"] for e in evaluated
          if e["result"] == "not_applicable"]
    checked = [e["rule_id"] for e in evaluated
               if e["result"] in ("pass", "not_applicable")]
    cond_fp = _hashlib.sha256(repr(sorted(
        (a["rule_id"], a["condition"]) for a in applications)
    ).encode()).hexdigest()
    return {
        "schema": "ontology-rule-coverage/v1",
        "candidate_sha256": candidate_sha256,
        "condition_fingerprint": cond_fp,
        "expected_applications": len(applications),
        "checked_applications": len(checked),
        "applications": evaluated,
        "fails": fails,
        "blocking_unknowns": blocking_unknowns,
        "not_applicable_with_evidence": na,
        "ok": not fails and not blocking_unknowns,
    }
