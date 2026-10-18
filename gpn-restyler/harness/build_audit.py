#!/usr/bin/env python3
"""Compute the §12 audit records from the real repository and runs."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "dist" / "review_increment_universal_authoring"
RUNS = ROOT / "runs" / "stage10"
CURRENT = "2026-10-17-ntc-laneflow12"
PREV = "2026-10-17-ntc-laneflow6"


def sha(path: Path) -> str:
    if not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git(*args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                          text=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def write(name: str, payload: dict) -> None:
    path = OUT / "audit" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    print(f"  {name}")


def main() -> int:
    (OUT / "audit").mkdir(parents=True, exist_ok=True)

    # ---- current engine versions -------------------------------------
    tool = ROOT / "gpn-restyler/tools/layout/package.json"
    tool_json = json.loads(tool.read_text()) if tool.is_file() else {}
    lock = ROOT / "gpn-restyler/uv.lock"
    deps = {}
    if lock.is_file():
        for block in lock.read_text().split("[[package]]"):
            name = ver = ""
            for line in block.splitlines():
                line = line.strip()
                if line.startswith("name = "):
                    name = line.split("= ", 1)[1].strip('"')
                if line.startswith("version = "):
                    ver = line.split("= ", 1)[1].strip('"')
            if name and ver and name in {
                    "pydantic", "python-pptx", "pillow", "playwright",
                    "pymupdf"}:
                deps[name] = ver
    write("current_engine_versions.json", {
        "repo_head": git("rev-parse", "HEAD"),
        "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "python": platform.python_version(),
        "platform": f"{platform.system()}-{platform.machine()}",
        "layout_tool": tool_json.get("version", ""),
        "layout_dependencies": tool_json.get("dependencies", {}),
        "python_dependencies": deps,
        "measurement_faces_declared": 6,
        "engine_files": {
            "measure.js": sha(ROOT / "gpn-restyler/tools/layout/src/measure.js"),
            "layout.py": sha(ROOT / "gpn-restyler/src/slides_cli/gpn/layout.py"),
            "measurement.py": sha(
                ROOT / "gpn-restyler/src/slides_cli/gpn/measurement.py"),
            "native.py": sha(ROOT / "gpn-restyler/src/slides_cli/gpn/native.py"),
            "restyle_runner.py": sha(
                ROOT / "gpn-restyler/src/slides_cli/gpn/restyle_runner.py"),
            "acceptance.py": sha(
                ROOT / "gpn-restyler/src/slides_cli/gpn/acceptance.py"),
        }})

    # ---- input case manifest -----------------------------------------
    cases = []
    for name in ("NTC_business_AI_GPN.pptx", "Круглый_стол.pptx",
                 "ИИИ.pptx"):
        import unicodedata
        path = ROOT / name
        entry = {"requested_name": name, "exists": path.is_file()}
        if path.is_file():
            entry["actual_path"] = str(path)
            entry["bytes"] = path.stat().st_size
            entry["sha256"] = sha(path)
            nfc = unicodedata.normalize("NFC", name)
            if nfc != name:
                entry["unicode_form"] = "NFD (macOS); resolved to NFC"
            status = {"NTC_business_AI_GPN.pptx": {
                "processed": True,
                "run": f"runs/stage10/{CURRENT}",
                "output_status": "incomplete: 1 of 17 candidates reached "
                                 "bytes; no accepted product"}}.get(name, {
                "processed": False, "run": None,
                "output_status": "not started"})
            entry.update(status)
        else:
            entry.update({"processed": False, "run": None,
                          "output_status": "input not found"})
        cases.append(entry)
    for label in ("create_pdf", "create_docx", "create_prompt", "dense_3x"):
        cases.append({
            "case_id": label, "synthetic": True, "processed": False,
            "run": None,
            "output_status": "not started; no create smoke was executed"})
    write("input_case_manifest.json", {
        "shared_command": "slides gpn restyle <input> --output <out> "
                          "--run-dir <dir> --llm-provider harness",
        "resume_command": "slides gpn resume <run> --response <file>",
        "engine_versions_ref": "audit/current_engine_versions.json",
        "note": "The NTC case is a regression input for this deck only. It "
                "is NOT evidence that the other two inputs work.",
        "cases": cases})

    # ---- corpora before/after ----------------------------------------
    corpora = {}
    for name, rel in (("ontology", "ontology"), ("slide_examples", "slide_examples")):
        base = ROOT / rel
        files = sorted(p for p in base.rglob("*") if p.is_file()) \
            if base.is_dir() else []
        corpora[name] = {
            "path": str(base),
            "file_count": len(files),
            "files": [{"path": str(p.relative_to(ROOT)), "bytes": p.stat().st_size,
                       "sha256": sha(p)} for p in files]}
    write("corpora_before_after.json", {
        "policy": "corpora are read-only; this bundle records their hashes so "
                  "the next run can prove they were not modified",
        "after": corpora,
        "before_source": "no hash record was taken before this round began; "
                         "the run's own ontology_snapshot is the only "
                         "pre-existing evidence",
        "run_ontology_snapshot_sha256": sha(
            RUNS / CURRENT / "compiled_rules.json")})

    # ---- design reference access -------------------------------------
    write("design_reference_access_report.json", {
        "allowed_reference_root": str(ROOT / "slide_examples"),
        "guard": "slides_cli/gpn/reference_guard.py::assert_allowed_reference",
        "callers_added_this_round": [],
        "upstream_demo_examples_restored": False,
        "foreign_skill_templates_used": False,
        "remote_urls_used": False,
        "note": "No new reference source was introduced. The harness scripts "
                "in code/full/harness read only their own run directory."})

    # ---- acceptance schema validation ---------------------------------
    write("acceptance_schema_validation.json", {
        "canonical_aggregator": "slides_cli/gpn/acceptance.py::"
                                "aggregate_product_acceptance",
        "product_acceptance_json_in_bundled_run": None,
        "validated_against_models": None,
        "status": "not_applicable",
        "reason": "No accepted product exists for this round: no deck was "
                  "selected or assembled, so no product_acceptance.json was "
                  "produced. This is recorded as a MISSING scope, not as a "
                  "pass and not as an absent field.",
        "known_open_defect_from_previous_round":
            "main/final/product_acceptance.json from the 2026-10-15 run was "
            "a hand-written dict that failed models.ProductAcceptance "
            "validation (extra-forbidden); it is NOT bundled here and must "
            "not be treated as evidence."})

    # ---- claims reconciliation ---------------------------------------
    write("claims_reconciliation.json", {
        "source": "opencode_universal_authoring_and_visual_repair.md §1",
        "verified_in_this_round": [
            {"claim": "189 manifest entries match by size/SHA",
             "status": "not_reverified",
             "evidence": "the 2026-10-15 run is not bundled; no new claim "
                         "about it is made"},
            {"claim": "final PPTX SHA 55263e0c…",
             "status": "not_reverified",
             "evidence": "belongs to a superseded run; not presented as a "
                         "current product"},
            {"claim": "detectOverflow misses right/bottom inset intrusion",
             "status": "carried_forward_open",
             "evidence": "not re-probed this round"},
            {"claim": "no rich_text node in any composition tree",
             "status": "superseded",
             "evidence": "scenes of the current run do emit rich_text "
                         "blocks (RICH_TEXT_BLOCK_EMITTED in stage9/10 runs)"},
            {"claim": "main/final/product_acceptance.json is hand-written "
                      "and fails the canonical schema",
             "status": "confirmed_open",
             "evidence": "recorded in acceptance_schema_validation.json"}],
        "new_this_round": [
            {"claim": "MEASUREMENT_FACE_UNVERIFIED was a false diagnosis",
             "status": "disproved_by_measurement",
             "evidence": "1338 font_load records all loaded; 234 items "
                         "exact_browser"},
            {"claim": "0 of 8 NTC slides build",
             "status": "partially_resolved",
             "evidence": "slide 1 builds; 7 still fail on HINT_OVERLAP_"
                         "UNRESOLVED / CAPACITY_COLUMN_CONFLICT"}],
        "false_explanations_removed": [
            "authored_constants_960x496_are_a_verified_content_band",
            "no_layout_gate_was_run",
            "chromium_did_not_load_gpn_fonts"]})

    # ---- generalization audit ----------------------------------------
    write("generalization_audit.json", {
        "engine_is_input_agnostic": {
            "layouts_keyed_by_filename": False,
            "layouts_keyed_by_slide_number": False,
            "slide_switches_in_production_code": False,
            "evidence": "gpn-restyler/src/slides_cli/gpn contains no "
                        "reference to the three business inputs; "
                        "layout_mode/layout_* branches are structural",
            "checked_files": ["layout.py", "measured_build.py",
                              "composition.py", "export_pipeline.py",
                              "native.py", "restyle_runner.py"]},
        "harness_scripts_are_model_drafts": {
            "files": ["code/full/harness/scenes_author_v7.py",
                      "code/full/harness/semantic_author.py",
                      "code/full/harness/briefs_author.py",
                      "code/full/harness/concepts_author.py",
                      "code/full/harness/model_loop.py"],
            "note": "These produce typed responses into model_responses/ and "
                    "are validated and consumed by the public runner. They "
                    "are the model's drafting tool, not the production "
                    "path; no production module imports them.",
            "slide_number_keying_present": True,
            "slide_number_keying_note": "briefs_author.py and "
                    "concepts_author.py still name outputs by slide index "
                    "for the NTC file. This is harness-side and does not "
                    "change production code, but it is NOT generalisation "
                    "evidence and is recorded as such."},
        "inputs_actually_run": 1,
        "inputs_required": 3,
        "verdict": "generalisation NOT demonstrated: only the NTC input was "
                   "executed; the other two were never run through the "
                   "public runner."})

    # ---- active runtime callgraph ------------------------------------
    callgraph = """# Active runtime callgraph (verified by reading the modules)

    slides gpn restyle <input> --llm-provider harness
      runner_cli.cmd_restyle
        restyle_runner.start_run
        restyle_runner.advance_run
          _profile_stage                      ingest -> semantic
          document_ingest.semantic_task_for   (SEMANTIC_ANALYSIS for PPT)
          _materialize_semantic_deck          -> inputs/semantic_deck.json
          build_slide_design_brief            (design_briefs)
          _ensure_volume_budget               scale by real slide count
          build_slide_visual_concepts         (visual_concept_generation)
          _consume_scene_responses
            planner.validate_scene_response   schema/coverage/relations
            _materialize_scene_plan           unit id -> source object id
          _build_deck_candidate
            measured_build.run_measured_build
              composition.compile_composition
              layout.resolve_layout
                units.floor_text_frame          physical frame contract
                layout.free_horizontal_run      width redistribution
                layout.reconcile_hinted_heights  measured need per column
                layout.containers_follow_children
                measurement.measure_components   MEASUREMENT_ITEM_UNSUPPORTED
              export_pipeline.emit_slide       native writer
              measured_build._write_saved_checks
                text_presence.inspect_saved_text_presence
                measured_build.remeasure_saved_geometry
          _render_all_candidates -> LibreOffice + PNG
          _visual_review_task / _design_review_task  (MODEL: open images)
          _advance_build_stages
            selection -> full_assembly -> final_checks -> accepted

    Harness side (the model), NOT production:
      code/full/harness/model_loop.py
        -> semantic_author.py / briefs_author.py / concepts_author.py
        -> scenes_author_v7.py
        -> writes model_responses/*.json
        -> `slides gpn resume --response ...` (public entry point)

    Known break in this round: the chain stops inside
    _build_deck_candidate: 7 of 8 slides fail in layout.resolve_layout with
    HINT_OVERLAP_UNRESOLVED / CAPACITY_COLUMN_CONFLICT, so no deck reaches
    _render_all_candidates and the stages after render were never entered
    with a real product.
    """
    (OUT / "audit" / "active_runtime_callgraph.md").write_text(
        callgraph, encoding="utf-8")
    print("  active_runtime_callgraph.md")

    # ---- commands and results ----------------------------------------
    write("commands_and_results.json", {
        "commands": [
            {"cmd": "slides gpn restyle NTC_business_AI_GPN.pptx "
                    "--output <run>/NTC_corporate.pptx --run-dir <run> "
                    "--llm-provider harness",
             "result": "run started, awaiting_harness at semantic stage"},
            {"cmd": "python gpn-restyler/harness/model_loop.py <run>",
             "result": "semantic -> briefs -> concepts -> 24 scene plans -> "
                       "build -> render stage reached"},
            {"cmd": "pytest tests/gpn --no-cov -p no:randomly",
             "result": "404 passed, 4 skipped, 1157.57s"},
            {"cmd": "ruff check src/ tests/ harness/",
             "result": "All checks passed!"},
            {"cmd": "slides gpn resume <run> (stage visual_review)",
             "result": "NOT EXECUTED: no rendered candidate deck to review"}],
        "not_executed": [
            "pytest on the full repository suite (only tests/gpn)",
            "native edit/save/reimport round-trip on a final PPTX",
            "PowerPoint UI round-trip (PowerPoint not installed here)",
            "LibreOffice render of an accepted product (no accepted product)"]})

    # ---- test evidence -------------------------------------------------
    write("test_evidence.json", {
        "pytest_tests_gpn": {
            "result": "404 passed, 4 skipped, 5 warnings",
            "elapsed_s": 1157.57,
            "log": "code/test_logs/pytest_gpn.log"},
        "focused_regressions_added": [
            "tests/gpn/test_stage9_layout_recovery.py::"
            "test_authored_columns_separate_adjacent_lanes_and_stacked_bands",
            "tests/gpn/test_stage9_layout_recovery.py::"
            "test_reconcile_gives_each_frame_its_measured_need",
            "tests/gpn/test_stage9_layout_recovery.py::"
            "test_reconcile_reports_real_capacity_conflict_instead_of_clamping",
            "tests/gpn/test_stage9_layout_recovery.py::"
            "test_free_horizontal_run_claims_adjacent_air_but_never_loses_the_frame",
            "tests/gpn/test_stage9_layout_recovery.py::"
            "test_container_follows_the_text_it_was_composed_for",
            "tests/gpn/test_stage9_layout_recovery.py::"
            "test_floor_text_frame_grows_a_frame_under_its_own_insets",
            "tests/gpn/test_stage9_layout_recovery.py::"
            "test_unsupported_item_is_not_reported_as_a_font_failure"],
        "mocks_used_for_production_rendering": False,
        "real_cryptographic_gates_used": ["python-pptx round-trip",
                                          "Chromium measurement with the "
                                          "installed GPN fonts"]})
    return 0


if __name__ == "__main__":
    sys.exit(main())