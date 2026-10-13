"""Stage 5 §1.1: independent re-verification of the Stage 4 candidate bytes.

Re-opens the saved candidate PPTX from disk (never trusting the run's own
reports), rebuilds the package graph + ledger from the candidate bytes, and
compares against the immutable source ledger restricted to the declared
pilot scope. Also re-checks workbook hashes, internal relationship targets,
canvas/template identity and the forbidden-reference state.

Usage: python scripts/stage5_verify_stage4.py <run_dir>
"""

from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from collections import Counter
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = CODE_ROOT.parent
sys.path.insert(0, str(CODE_ROOT / "src"))

from slides_cli.gpn.assets import AssetStore  # noqa: E402
from slides_cli.gpn.importer import import_deck  # noqa: E402
from slides_cli.gpn.opc_adapter import validate_package_graph  # noqa: E402


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    run_dir = Path(sys.argv[1]).resolve()
    candidate = run_dir / "candidate.pptx"
    out: dict = {"run_dir": str(run_dir), "checks": {}}

    # 1. Candidate hash vs readiness report
    readiness = json.loads((run_dir / "stage4_readiness.json").read_text(
        encoding="utf-8"))
    actual_hash = sha256_file(candidate)
    out["checks"]["candidate_hash"] = {
        "actual": actual_hash,
        "reported": readiness.get("candidate_sha256"),
        "match": actual_hash == readiness.get("candidate_sha256"),
    }

    # 2. Rebuild package graph from candidate bytes
    graph_report = validate_package_graph(candidate)
    out["checks"]["package_graph"] = graph_report

    # 3. Re-import candidate and rebuild ledger from candidate bytes
    store = AssetStore(run_dir / "verify_assets")
    cand_ir, cand_ledger = import_deck(candidate, store)
    out["checks"]["candidate_import"] = {
        "slides": len(cand_ir.slides),
        "objects": sum(len(s.objects) for s in cand_ir.slides),
        "import_issues": [i.code for i in cand_ir.import_issues],
        "source_sha256": cand_ir.source_sha256,
    }

    # 4. Compare candidate ledger vs source ledger restricted to pilot scope
    source_ledger = json.loads((run_dir / "source_ledger.json").read_text(
        encoding="utf-8"))
    scope = json.loads((run_dir / "ledger_scope_manifest.json").read_text(
        encoding="utf-8"))
    selected_parts = {s["id"].split(":", 1)[1]
                      for s in scope["slides"] if ":" in s["id"]}

    def atoms_of(ledger: dict, parts: set[str] | None) -> Counter:
        c: Counter = Counter()
        for atom in ledger["atoms"]:
            ref = atom.get("source_ref", {}) or {}
            if parts is not None and ref.get("slide_part") not in parts:
                continue
            c[(atom["kind"], atom["canonical_value"])] += 1
        return c

    src_atoms = atoms_of(source_ledger, selected_parts)
    cand_atoms = atoms_of(
        {"atoms": [{"kind": a.kind, "canonical_value": a.canonical_value,
                    "source_ref": {"slide_part": a.source_ref.slide_part}}
                   for a in cand_ledger.atoms]}, None)
    missing = {f"{k[0]}:{k[1][:60]}": n for k, n in src_atoms.items()
               if cand_atoms.get(k, 0) < n}
    extra = {f"{k[0]}:{k[1][:60]}": n for k, n in cand_atoms.items()
             if src_atoms.get(k, 0) < n}
    out["checks"]["ledger_preservation"] = {
        "scope_atoms": sum(src_atoms.values()),
        "candidate_atoms": sum(cand_atoms.values()),
        "missing": missing,
        "unexpected_extra": extra,
        "preserved": not missing,
    }

    # 5. Workbook binary hashes inside candidate bytes
    with zipfile.ZipFile(candidate) as zf:
        members = {i.filename: zf.read(i) for i in zf.infolist()
                   if not i.is_dir()}
    wb_report = json.loads((run_dir / "workbook_hashes.json").read_text(
        encoding="utf-8"))
    wb_actual = {name: hashlib.sha256(data).hexdigest()
                 for name, data in members.items()
                 if name in wb_report}
    out["checks"]["workbook_hashes"] = {
        "expected": wb_report,
        "actual": wb_actual,
        "match": wb_actual == wb_report,
    }

    # 6. Canvas identity vs active template
    tmpl = json.loads((run_dir / "active_template_manifest.json").read_text(
        encoding="utf-8"))
    canvas = cand_ir.slides[0].source_canvas if cand_ir.slides else None
    cand_canvas = [canvas.x, canvas.y, canvas.w, canvas.h] if canvas else None
    tmpl_canvas = tmpl.get("canvas_emu")
    out["checks"]["canvas"] = {
        "candidate": cand_canvas,
        "template": tmpl_canvas,
        "match": (cand_canvas == [0, 0, *tmpl_canvas]
                  if cand_canvas and tmpl_canvas and len(tmpl_canvas) == 2
                  else False),
    }

    # 7. Forbidden references: upstream examples absent from tracked tree
    import subprocess
    tracked = subprocess.run(
        ["git", "ls-files", "gpn-restyler/examples/"], capture_output=True,
        text=True, cwd=str(PROJECT_ROOT)).stdout
    out["checks"]["forbidden_references"] = {
        "upstream_examples_tracked": "example1.pptx" in tracked,
        "user_corpus_present": (PROJECT_ROOT / "slide_examples").is_dir(),
    }

    # 8. List profile present and loaded
    list_profile = run_dir / "list_profile.json"
    lp = json.loads(list_profile.read_text(encoding="utf-8")) \
        if list_profile.is_file() else {}
    out["checks"]["list_profile"] = {
        "present": bool(lp),
        "level_profiles": len(lp.get("level_profiles", []))
        if isinstance(lp, dict) else 0,
        "id": lp.get("id") if isinstance(lp, dict) else None,
    }

    ok = all([
        out["checks"]["candidate_hash"]["match"],
        graph_report.get("ok") is True,
        out["checks"]["ledger_preservation"]["preserved"],
        out["checks"]["workbook_hashes"]["match"],
        out["checks"]["canvas"]["match"],
        not out["checks"]["forbidden_references"]["upstream_examples_tracked"],
        out["checks"]["forbidden_references"]["user_corpus_present"],
        out["checks"]["list_profile"]["present"],
        out["checks"]["list_profile"]["level_profiles"] > 0,
    ])
    out["all_ok"] = ok
    dest = run_dir / "stage4_claims_verification.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
