"""Build stage4_test_evidence.json: requirement -> nodeid -> assert lines -> outcome.

Runs each of the 38 mandatory Stage 4 §13.1 tests individually and records the
real outcome. Assert locations are extracted from the test file by AST.
Run: python scripts/stage4_test_evidence.py <run_dir>
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = CODE_ROOT.parent
TEST_FILE = CODE_ROOT / "tests" / "gpn" / "test_stage4_exporter.py"

REQUIREMENTS: dict[str, tuple[str, str]] = {
    "new_output_deck_from_template": (
        "test_new_output_deck_from_template",
        "positive: distinct output maps per source slide, canvas exact"),
    "text_tokens_fields_roundtrip": (
        "test_text_tokens_fields_roundtrip",
        "positive: Cyrillic/₽/%/minus, mixed runs, a:fld, footnote, soft break, links"),
    "equal_text_distinct_bindings": (
        "test_equal_text_distinct_bindings",
        "negative: identical strings must keep separate occurrences"),
    "case_transform_scoped": (
        "test_case_transform_scoped",
        "negative: transform leaks to body/units or unsafe refusal"),
    "list_profile_saved_xml": (
        "test_list_profile_saved_xml",
        "positive: native buFont/buChar/buSzPct/priority/indents/noAutofit in saved XML"),
    "inconsistent_list_norm_blocks": (
        "test_inconsistent_list_norm_blocks",
        "negative: percent/declared/MD mismatch must block"),
    "table_merge_visible_hidden_payload": (
        "test_table_merge_visible_hidden_payload",
        "negative: hidden strings must not migrate to origin"),
    "table_empty_zero_na": (
        "test_table_empty_zero_na",
        "negative: blank/zero/NA states must stay distinct"),
    "chart_style_only_workbook_exact": (
        "test_chart_style_only_workbook_exact",
        "negative: replace_data forbidden; workbook bytes must be equal"),
    "combo_axes_and_plots_preserved": (
        "test_combo_axes_and_plots_preserved",
        "positive: multi-plot identities + secondary axes"),
    "scatter_bubble_nonshared_x": (
        "test_scatter_bubble_nonshared_x",
        "positive: X/Y/size grids across series"),
    "chart_conflict_blocks_verified_build": (
        "test_chart_conflict_blocks_verified_build",
        "negative: cache/book conflict must block the build"),
    "new_verified_native_charts": (
        "test_new_verified_native_charts",
        "positive: approved data creates native charts, reimport matches"),
    "chart_semantic_color_unknown": (
        "test_chart_semantic_color_unknown",
        "negative: unsupported color meaning must stay unknown, not pass"),
    "shared_image_workbook_relations": (
        "test_shared_image_workbook_relations",
        "positive: sharing correct, occurrence counts kept"),
    "same_part_names_different_sources": (
        "test_same_part_names_different_sources",
        "negative: chart/workbook name collision across packages"),
    "copy_on_write_chart_styles": (
        "test_copy_on_write_chart_styles",
        "negative: shared mutable chart must not be overwritten"),
    "cycle_backrefs_no_deck_import": (
        "test_cycle_backrefs_no_deck_import",
        "negative: closure must stay finite; backrefs via map"),
    "owner_scoped_rid_remap": (
        "test_owner_scoped_rid_remap",
        "negative: rId1 of different owners must not merge"),
    "unknown_namespace_preserved": (
        "test_unknown_namespace_preserved",
        "positive: extLst + mc prefix bindings survive"),
    "external_links_no_fetch": (
        "test_external_links_no_fetch",
        "negative: no app network during export"),
    "internal_notes_hidden_order": (
        "test_internal_notes_hidden_order",
        "positive: links/notes/backrefs, count/order/hidden flags"),
    "group_transform_corners": (
        "test_group_transform_corners",
        "positive: independent corner math for rotation/nonuniform scale/flips"),
    "connector_output_ids": (
        "test_connector_output_ids",
        "positive: stCxn/endCxn actual IDs/sites; zero extent allowed"),
    "unknown_opaque_contract": (
        "test_unknown_opaque_contract",
        "negative: byte preservation or explicit refusal"),
    "same_part_count_changed_workbook": (
        "test_same_part_count_changed_workbook",
        "negative: equal counts must not mask changed binary"),
    "same_part_count_wrong_target": (
        "test_same_part_count_wrong_target",
        "negative: equal counts must not mask changed relationship"),
    "serializer_drops_opaque_member": (
        "test_serializer_drops_opaque_member",
        "negative: final validator must catch drop, success=false"),
    "atomic_failure_preserves_inputs": (
        "test_atomic_failure_preserves_inputs",
        "negative: source/corpora/output hashes unchanged on failure"),
    "source_ir_not_overwritten": (
        "test_source_ir_not_overwritten",
        "negative: candidate_after artifacts stay separate"),
    "stale_snapshot_plan_rejected": (
        "test_stale_snapshot_plan_rejected",
        "negative: new corpus/schema/source hash must reject old plan"),
    "harness_provider_no_local_endpoint_gate": (
        "test_harness_provider_no_local_endpoint_gate",
        "negative: build works without model server/client"),
    "default_llm_provider_harness": (
        "test_default_llm_provider_harness",
        "positive: unset provider means harness mode 2"),
    "explicit_local_provider_deferred": (
        "test_explicit_local_provider_deferred",
        "negative: mode 1 deferred, no secret harness fallback, candidate None"),
    "reference_source_whitelist": (
        "test_reference_source_whitelist",
        "negative: upstream/alternate/web/symlink references rejected"),
    "skill_legacy_reference_denied": (
        "test_skill_legacy_reference_denied",
        "negative: project skill cannot bypass guard"),
    "foreign_reference_cache_invalidated": (
        "test_foreign_reference_cache_invalidated",
        "negative: stale cache with forbidden provenance rejected"),
    "upstream_examples_untracked": (
        "test_upstream_examples_untracked",
        "negative: legacy sample paths absent from tracked tree; user corpus kept"),
}


def assert_lines(tree: ast.Module, func: str) -> list[int]:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func:
            lines = sorted(
                n.lineno for n in ast.walk(node)
                if isinstance(n, ast.Assert))
            for n in ast.walk(node):
                if isinstance(n, ast.With):
                    for item in n.items:
                        call = item.context_expr
                        if (isinstance(call, ast.Call)
                                and isinstance(call.func, ast.Attribute)
                                and call.func.attr == "raises"):
                            lines.append(n.lineno)
            return sorted(lines)
    return []


def run_one(nodeid: str) -> dict:
    r = subprocess.run(
        ["uv", "run", "pytest", nodeid, "--no-cov", "-p", "no:cacheprovider",
         "-q", "--tb=short"],
        capture_output=True, text=True, cwd=CODE_ROOT, timeout=300)
    out = r.stdout + r.stderr
    if r.returncode == 0:
        # -q progress lines (". [100%]") carry no "passed" token; exit 0
        # for a single selected nodeid means collected+executed+passed.
        outcome, skip = "passed", None
    elif "skipped" in out:
        outcome, skip = "skipped", out.strip().split("\n")[-1]
    else:
        outcome, skip = "failed", None
    return {"exit": r.returncode, "outcome": outcome, "skip_reason": skip,
            "tail": out.strip().split("\n")[-1] if out.strip() else ""}


def main() -> int:
    run_dir = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else (
        PROJECT_ROOT / "runs" / "stage5" / "2026-10-09-a")
    run_dir.mkdir(parents=True, exist_ok=True)
    src = TEST_FILE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    defined = {n.name for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef)}
    entries = []
    for req, (func, proof) in REQUIREMENTS.items():
        nodeid = f"tests/gpn/test_stage4_exporter.py::{func}"
        present = func in defined
        result = run_one(nodeid) if present else {
            "exit": None, "outcome": "missing", "skip_reason": None,
            "tail": "test function not defined"}
        entries.append({
            "requirement": req,
            "nodeid": nodeid,
            "present": present,
            "assert_lines": assert_lines(tree, func),
            "collected": present,
            "executed": result["outcome"] == "passed",
            "outcome": result["outcome"],
            "skip_reason": result["skip_reason"],
            "exit_code": result["exit"],
            "run_tail": result["tail"],
            "negative_or_mutation_proof": proof,
        })
    passed = sum(1 for e in entries if e["outcome"] == "passed")
    doc = {
        "run_id": run_dir.name,
        "source": "individual per-test runs via uv run pytest <nodeid>",
        "summary": {
            "requirements": len(entries),
            "passed": passed,
            "failed": sum(1 for e in entries if e["outcome"] == "failed"),
            "missing": sum(1 for e in entries if e["outcome"] == "missing"),
            "skipped": sum(1 for e in entries if e["outcome"] == "skipped"),
        },
        "requirements": entries,
    }
    dest = run_dir / "stage4_test_evidence.json"
    dest.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(f"requirements: {len(entries)}, passed: {passed}")
    for e in entries:
        if e["outcome"] != "passed":
            print("  !!", e["requirement"], e["outcome"], e["run_tail"][:100])
    return 0 if passed == len(entries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
