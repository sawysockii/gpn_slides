"""Semantic rule binding with condition fingerprints (Stage 3.5 §5.1).

A rule ID alone never proves a binding: if the condition text behind an ID
changed to a different meaning, the previously reviewed checker no longer
demonstrates the new rule. Each reviewed binding therefore stores a
*fingerprint of the reviewed condition* (normalized text hash), not the
corporate text itself.

- matching fingerprint  → the reviewed semantic binding stays active;
- changed fingerprint    → ``needs_binding``: the checker is not dispatched,
  the rule evaluates to an honest ``unknown`` (blocking when hard);
- new rule without an
  reviewed library entry → ``deferred``/``needs_binding``; never a fake pass.

The library is engine data (reviewed against a specific ontology hash), not a
corporate norm: it pins *which* condition texts were reviewed and by which
checker, and carries an explicit ``reviewed_source`` with the ontology sha256.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .models import (
    RuleBinding,
    RuleBindingReport,
    RuleBindingReportEntry,
    RuleCoverage,
    RuleSpec,
)

DEFAULT_LIBRARY_PATH = Path(__file__).with_name("rule_binding_library.json")

# Which JSON pointers each semantic checker reads as its expected values.
# This is schema-adapter/engine data (where the checker looks), never a
# duplicated normative value.
CHECKER_PARAMETER_REFS: dict[str, list[str]] = {
    "check_c01": ["/style_profile/color_tokens"],
    "check_c02": ["/style_profile/font_style_policy/rules",
                  "/style_profile/typography"],
    "check_c03": [],
    "check_c04": [],
    "check_c05": [],
    "check_c10": [],
    "check_c11": [],
    "check_c13": [],
    "check_c14": [],
    "check_c16": ["/style_profile/canvas"],
    "check_c17": ["/style_profile/list_styles/0/marker/ooxml"],
    "check_c18": ["/style_profile/list_styles/0/text/nested_size_policy"],
    "check_c19": ["/style_profile/minimum_text_font_size/value_pt"],
    "check_c20": [],
    "check_c21": ["/style_profile/font_style_policy/rules",
                  "/style_profile/font_style_policy/forbidden_combinations"],
    "check_c22": ["/style_profile/font_style_policy/rules",
                  "/style_profile/font_style_policy/validation"],
    "check_r04": ["/style_profile/minimum_text_font_size/value_pt",
                  "/style_profile/typography"],
}


def normalize_condition(text: str) -> str:
    """Whitespace/case normalization only — never a semantic rewrite."""
    return re.sub(r"\s+", " ", (text or "").strip()).casefold()


def condition_fingerprint(text: str) -> str:
    """Stable fingerprint of a normalized condition (change detection only)."""
    return hashlib.sha256(normalize_condition(text).encode("utf-8")).hexdigest()


def load_binding_library(path: Path | None = None) -> dict[str, Any]:
    """Load the reviewed binding library (engine data, versioned with code)."""
    library_path = Path(path) if path else DEFAULT_LIBRARY_PATH
    if not library_path.is_file():
        return {"entries": {}, "reviewed_source": {}}
    try:
        data = json.loads(library_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"entries": {}, "reviewed_source": {}}
    if not isinstance(data, dict):
        return {"entries": {}, "reviewed_source": {}}
    entries = data.get("entries")
    return {
        "entries": entries if isinstance(entries, dict) else {},
        "reviewed_source": data.get("reviewed_source") or {},
        "library_path": str(library_path),
    }


def bind_rules(
    specs: dict[str, RuleSpec],
    bindings: dict[str, RuleBinding],
    *,
    library: dict[str, Any] | None = None,
    coverage: list[RuleCoverage] | None = None,
) -> RuleBindingReport:
    """Attach fingerprints and reviewed status to every registry binding.

    Mutates ``bindings`` in place (statuses downgraded to ``needs_binding``
    when the reviewed condition no longer matches) and returns the report
    artifact described in §5.1.
    """
    lib = library if library is not None else load_binding_library()
    lib_entries: dict[str, Any] = lib.get("entries") or {}
    reviewed_source = lib.get("reviewed_source") or {}

    checked_by_rule: dict[str, list[str]] = {}
    for cov in coverage or []:
        if cov.checked_scopes:
            checked_by_rule[cov.rule_id] = list(cov.checked_scopes)

    report_entries: list[RuleBindingReportEntry] = []
    for rule_id in sorted(specs.keys()):
        spec = specs[rule_id]
        binding = bindings.get(rule_id)
        if binding is None:
            binding = RuleBinding(
                rule_id=rule_id,
                checker_key="",
                check_kind="hybrid",
                implementation_status="needs_binding",
                unresolved_reason="no binding registered for this rule",
            )
            bindings[rule_id] = binding

        fingerprint = condition_fingerprint(spec.condition)
        binding.condition_fingerprint = fingerprint
        reviewed = lib_entries.get(rule_id)
        reviewed_fp = str((reviewed or {}).get("condition_sha256") or "")

        unresolved = ""
        if binding.implementation_status in ("implemented", "partial"):
            if not reviewed:
                binding.implementation_status = "needs_binding"
                unresolved = (
                    "implemented checker has no reviewed entry in the binding "
                    "library; strict status blocked until reviewed"
                )
            elif reviewed_fp != fingerprint:
                binding.implementation_status = "needs_binding"
                unresolved = (
                    "condition fingerprint changed since review "
                    f"(reviewed {reviewed_fp[:12]}…, current {fingerprint[:12]}…); "
                    "the previous checker does not demonstrate the new text"
                )
        elif binding.implementation_status == "deferred":
            stage = binding.deferred_stage
            unresolved = (
                f"no deterministic checker yet (deferred to stage {stage})"
                if stage is not None else "no deterministic checker yet"
            )
        binding.unresolved_reason = unresolved

        report_entries.append(RuleBindingReportEntry(
            rule_id=rule_id,
            source_refs=list(spec.source_refs),
            normalized_condition=normalize_condition(spec.condition),
            condition_fingerprint=fingerprint,
            severity=spec.severity,
            scope=spec.applicability,
            checker_type=binding.check_kind,
            checker_key=binding.checker_key,
            parameter_refs=list(CHECKER_PARAMETER_REFS.get(binding.checker_key, [])),
            implementation_status=binding.implementation_status,
            checked_scopes=checked_by_rule.get(rule_id, []),
            unresolved_reason=unresolved,
        ))

    return RuleBindingReport(
        library_source=str(lib.get("library_path") or DEFAULT_LIBRARY_PATH),
        library_reviewed_ontology_sha256=str(
            reviewed_source.get("ontology_sha256") or ""),
        entries=report_entries,
    )
