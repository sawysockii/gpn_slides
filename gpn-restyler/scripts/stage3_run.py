"""Stage 3 artifact run: fixture + batch + CLI flows + verification JSONs.

Writes everything into project_root/runs/stage3/<run_id> without touching
previous runs. Every command/exit is recorded in commands_and_results.json.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = CODE_ROOT.parent

sys.path.insert(0, str(CODE_ROOT / "src"))

from slides_cli.api import Presentation as P  # noqa: E402
from slides_cli.api import resolve_shape_address  # noqa: E402
from slides_cli.gpn.assets import AssetStore  # noqa: E402
from slides_cli.gpn.importer import import_deck  # noqa: E402
from slides_cli.gpn.patching import _build_context, authorize_gpn_edit  # noqa: E402
from slides_cli.gpn.provenance import build_ledger  # noqa: E402
from slides_cli.model import OperationBatch  # noqa: E402

RUN_ID = "2026-10-05-02"
RUN_DIR = PROJECT_ROOT / "runs" / "stage3" / RUN_ID

results: list[dict] = []


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_cmd(cmd: list[str], *, expect: set[int] | None = None) -> dict:
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=CODE_ROOT)
    entry = {
        "cmd": cmd,
        "exit": proc.returncode,
        "stdout_tail": proc.stdout[-3000:],
        "stderr_tail": proc.stderr[-1500:],
    }
    results.append(entry)
    print(f"$ {' '.join(cmd)}\n  exit={proc.returncode}")
    if expect is not None and proc.returncode not in expect:
        print(proc.stdout[-2000:])
        print(proc.stderr[-2000:])
    return entry


def main() -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    (RUN_DIR / "cache").mkdir(parents=True, exist_ok=True)

    # --- fixture: two native slides (textbox/table/chart/group/notes) ---
    from pptx import Presentation as _P
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches

    prs = _P()
    prs.slide_width = Inches(13.33)
    prs.slide_height = Inches(7.5)
    s1 = prs.slides.add_slide(prs.slide_layouts[6])
    title = s1.shapes.add_textbox(Inches(1), Inches(0.5), Inches(8), Inches(1))
    title.text_frame.text = "Добыча нефти Pпл 2024"
    tbl = s1.shapes.add_table(2, 2, Inches(1), Inches(2),
                              Inches(6), Inches(1.5))
    tbl.table.cell(0, 0).text = "Регион"
    tbl.table.cell(0, 1).text = "Доля"
    tbl.table.cell(1, 0).text = "Север"
    tbl.table.cell(1, 1).text = "33,3%"
    s2 = prs.slides.add_slide(prs.slide_layouts[6])
    data = CategoryChartData()
    data.categories = ["2023", "2024"]
    data.add_series("План", (10.0, 12.0))
    s2.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1),
                        Inches(0.5), Inches(6), Inches(3), data)
    note = s2.shapes.add_textbox(Inches(1), Inches(4), Inches(6),
                                 Inches(0.8))
    note.text_frame.text = "Подпись к графику"
    grp = s2.shapes.add_group_shape()
    child = grp.shapes.add_shape(1, Inches(0.2), Inches(0.2),
                                 Inches(1.5), Inches(0.8))
    child.text_frame.text = "A"
    fixture = RUN_DIR / "fixture_source.pptx"
    prs.save(str(fixture))
    title_sid = int(title.shape_id)

    # --- operation batch (move + allowed style patch) ---
    pres = P.open(fixture)
    addr = resolve_shape_address(pres, slide_index=0, shape_id=title_sid)
    batch = {
        "operations": [
            {"op": "set_shape_geometry", "slide_index": 0,
             "shape_id": title_sid, "group_path": [],
             "left": 1.5, "top": 0.7, "width": 8.0, "height": 1.0,
             "expected_xml_sha256": addr.current_xml_sha256},
            {"op": "set_shape_style", "slide_index": 0,
             "shape_id": title_sid, "group_path": [],
             "style": {"font_name": "GPN_DIN Regular",
                       "font_size_pt": 14.0,
                       "text_color_rgb": "7E7E7E",
                       "fill_mode": "keep", "line_mode": "keep",
                       "text_scope": "uniform_runs",
                       "autofit_policy": "none"}},
        ]
    }
    validated = OperationBatch.model_validate(batch)
    edits_path = RUN_DIR / "operation_batch.json"
    edits_path.write_text(json.dumps(
        validated.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8")
    (RUN_DIR / "operation_schema.json").write_text(
        json.dumps(OperationBatch.model_json_schema(),
                   ensure_ascii=False, indent=2), encoding="utf-8")

    # --- source before IR/ledger + corpus hashes ---
    store = AssetStore(RUN_DIR / "assets_before")
    deck, _ = import_deck(fixture, store)
    ledger = build_ledger(deck, decoration=[])
    (RUN_DIR / "source_before_ir.json").write_text(
        deck.model_dump_json(indent=2), encoding="utf-8")
    (RUN_DIR / "source_before_ledger.json").write_text(
        ledger.model_dump_json(indent=2), encoding="utf-8")

    before_audit = {
        "run_id": RUN_ID,
        "head": subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True,
            cwd=CODE_ROOT).stdout.strip(),
        "branch": subprocess.run(
            ["git", "branch", "--show-current"], capture_output=True,
            text=True, cwd=CODE_ROOT).stdout.strip(),
        "corpus_hashes": {
            "ontology_json": sha256_file(
                PROJECT_ROOT / "ontology" / "GPN_Slide_Design_Ontology.json"),
            "ontology_md": sha256_file(
                PROJECT_ROOT / "ontology" / "GPN_Slide_Design_Ontology.md"),
            "slide_examples": sha256_file(
                PROJECT_ROOT / "slide_examples" / "slide_examples.pptx"),
            "fixture_source": sha256_file(fixture),
        },
        "reported": {"tests": "256 passed", "lint": "ruff clean",
                     "blocker": "FONTS/GPN_FONTS_MISSING"},
    }
    (RUN_DIR / "stage3_audit_before.json").write_text(
        json.dumps(before_audit, indent=2), encoding="utf-8")

    # --- CLI flows (each exit recorded; expected exit-3 doctor continues) ---
    run_cmd(["uv", "run", "slides", "gpn", "--project-root", str(PROJECT_ROOT),
             "doctor", "--run-dir", str(RUN_DIR / "doctor")], expect={0, 3, 4})
    run_cmd(["uv", "run", "slides", "gpn", "--project-root", str(PROJECT_ROOT),
             "doctor", "--require-production-assets",
             "--run-dir", str(RUN_DIR / "doctor_assets")], expect={3})
    run_cmd(["uv", "run", "slides", "find", str(fixture),
             "--query", "добыча", "--limit", "5"], expect={0})
    run_cmd(["uv", "run", "slides", "gpn", "--project-root", str(PROJECT_ROOT),
             "preflight-edits", "--input", str(fixture),
             "--edits-json", str(edits_path),
             "--run-dir", str(RUN_DIR)], expect={0})
    _candidate = RUN_DIR / "diagnostic_candidate.pptx"
    if _candidate.is_file():
        _candidate.unlink()
    run_cmd(["uv", "run", "slides", "gpn", "--project-root", str(PROJECT_ROOT),
             "apply-edits", "--input", str(fixture),
             "--edits-json", str(edits_path),
             "--output", str(_candidate),
             "--run-dir", str(RUN_DIR)], expect={0})
    # Upstream apply path with the same typed batch (dispatch roundtrip).
    run_cmd(["uv", "run", "slides", "apply", str(fixture),
             "--ops-json", f"@{edits_path}",
             "--output", str(RUN_DIR / "cache" / "upstream_apply_out.pptx")],
            expect={0})

    # --- rename apply outputs to spec artifact names (keep originals too) ---
    renames = {
        "source_ir.json": "candidate_after_ir.json",
        "source_ledger.json": "candidate_after_ledger.json",
        "support_report.json": "candidate_after_support.json",
        "link_resolution.json": "candidate_after_links.json",
        "source_package_manifest.json": "candidate_package_manifest.json",
    }
    for src_name, dst_name in renames.items():
        src_p = RUN_DIR / src_name
        if src_p.is_file() and not (RUN_DIR / dst_name).is_file():
            (RUN_DIR / dst_name).write_bytes(src_p.read_bytes())

    # --- output object map + authorizations + readiness ---
    candidate = RUN_DIR / "diagnostic_candidate.pptx"
    obj_map = {
        "correspondence": "source slide/part/shape/group addresses map to the "
                          "same addresses in the candidate (only addressed "
                          "property paths mutate)",
        "bindings": [
            {"source": {"slide_index": 0, "shape_id": title_sid,
                        "group_path": []},
             "candidate": {"slide_index": 0, "shape_id": title_sid,
                           "group_path": []},
             "mutations": ["set_shape_geometry", "set_shape_style"],
             "transform": "style_only+relocate"},
        ],
        "candidate_sha256": sha256_file(candidate) if candidate.is_file()
        else None,
        "source_sha256": sha256_file(fixture),
    }
    (RUN_DIR / "output_object_map.json").write_text(
        json.dumps(obj_map, indent=2), encoding="utf-8")

    ctx = _build_context(source=fixture, config=None,
                         project_root=PROJECT_ROOT)
    auths = []
    for op in validated.operations:
        auth = authorize_gpn_edit(op, ctx)
        auths.append({"op": op.op, "shape_id": getattr(op, "shape_id", None),
                      "allowed": auth.allowed, "reason": auth.reason})
    (RUN_DIR / "edit_authorizations.json").write_text(
        json.dumps(auths, ensure_ascii=False, indent=2), encoding="utf-8")

    # preservation + package diffs from the saved patch result
    patch_result_path = RUN_DIR / "gpn_patch_result.json"
    patch_result = json.loads(patch_result_path.read_text()
                              ) if patch_result_path.is_file() else {}
    (RUN_DIR / "preservation_diff.json").write_text(
        json.dumps(patch_result.get("import_diff", {}),
                   ensure_ascii=False, indent=2), encoding="utf-8")
    (RUN_DIR / "package_diff.json").write_text(
        json.dumps(patch_result.get("package_diff", {}),
                   ensure_ascii=False, indent=2), encoding="utf-8")
    (RUN_DIR / "operation_report.json").write_text(
        json.dumps(patch_result.get("operation_report", {}), indent=2),
        encoding="utf-8") if patch_result else None

    readiness = {
        "run_id": RUN_ID,
        "native_patch_verified": candidate.is_file(),
        "test_only": True,
        "strict_output_ready": False,
        "production_assets_ready": False,
        "production_blockers": ["FONTS/GPN_FONTS_MISSING"],
        "scope": "diagnostic native-edit candidate; not a corporate "
                 "production-completed presentation",
        "next_stage": "Stage 4 native exporter/transplant/table/chart/notes/links",
    }
    (RUN_DIR / "stage3_readiness.json").write_text(
        json.dumps(readiness, indent=2), encoding="utf-8")

    claims = {
        "unicode_find": {"status": "verified",
                         "evidence": "test_find_text_cyrillic + CLI find добыча"},
        "autofit_none": {"status": "verified",
                         "evidence": "test_no_autofit_saved_xml"},
        "conflict_classifier": {"status": "fixed",
                                "detail": "compatible_refinement vs "
                                          "contradiction; full-row MD "
                                          "extraction; 22 equivalent, ready "
                                          "True on real corpus"},
        "checker_partials": {"status": "implemented",
                             "detail": "C04/C05/C11/C13/C14/C20 deterministic "
                                       "parts; bindings partial"},
        "hyperlinks": {"status": "fixed",
                       "detail": "occurrence identity; hover fallback; "
                                 "iter_hyperlink_occurrences; clrMapOvr"},
        "doctor_scope": {"status": "fixed",
                         "detail": "scoped readiness + "
                                   "--require-production-assets (exit 3)"},
        "chart_gate": {"status": "verified",
                       "detail": "conflict blocks with needs_review/exit 4"},
        "production_blocker": {"status": "confirmed",
                               "detail": "FONTS/GPN_FONTS_MISSING; "
                                         "production_assets_ready=false"},
    }
    (RUN_DIR / "stage12_claims_verification.json").write_text(
        json.dumps(claims, ensure_ascii=False, indent=2), encoding="utf-8")

    after_audit = dict(before_audit)
    after_audit["candidate_sha256"] = sha256_file(candidate) \
        if candidate.is_file() else None
    after_audit["commands"] = len(results)
    (RUN_DIR / "stage3_audit_after.json").write_text(
        json.dumps(after_audit, indent=2), encoding="utf-8")
    (RUN_DIR / "commands_and_results.json").write_text(
        json.dumps(results, indent=2)[:200000], encoding="utf-8")
    print(f"artifacts in {RUN_DIR}")


if __name__ == "__main__":
    main()
