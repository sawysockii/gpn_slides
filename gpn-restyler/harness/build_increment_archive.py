#!/usr/bin/env python3
"""Build the increment verification archive (spec §12).

Collects: the changed/created code, the audit records, the run evidence of
this round's increments, and a detailed blocker report. Writes a per-file
SHA-256/size manifest and zips the tree.

Rules it obeys:
* nothing is claimed complete that is not; missing mandatory inputs are
  listed explicitly in the manifest (``complete: false``);
* every bundled file is hashed, so a reference can be checked;
* bulky regenerable binaries (Chromium renders, PDFs, source media) are
  excluded by an explicit rule and listed in ``excluded`` with the reason.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "dist" / "review_increment_universal_authoring"
STAGE = OUT

# Regenerable or already-published binaries: excluded from the archive and
# named in manifest.excluded with the reason.
EXCLUDE_SUFFIX = {".png", ".jpg", ".jpeg", ".pdf", ".pptx", ".otf", ".ttf",
                  ".woff", ".woff2", ".so", ".dylib", ".dll", ".pyc"}
EXCLUDE_DIRS = {"node_modules", "__pycache__", ".git", ".venv", ".pytest_cache"}
EXCLUDE_PREFIX = ("assets/", "saved_assets/", "candidate_assets/",
                  "export_work/", "export_staging/", "profiles/")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_tree(src: Path, dst: Path, skip: set[str] | None = None) -> list[str]:
    copied: list[str] = []
    if not src.exists():
        return copied
    skip = skip or set()
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(src)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if rel.suffix in EXCLUDE_SUFFIX:
            continue
        if skip and rel.as_posix() in skip:
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied.append(rel.as_posix())
    return copied


def git(*args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                          text=True)
    return proc.stdout if proc.returncode == 0 else f"(git {' '.join(args)} failed)"


def main() -> int:
    if OUT.exists():
        # Only the subtrees this builder owns; the audit records and the
        # blocker report are produced by build_audit.py and the source
        # report, and must survive the stage they are hashed into.
        for owned in ("code", "runs", "audit", "shared", "verification"):
            shutil.rmtree(OUT / owned, ignore_errors=True)
    STAGE.mkdir(parents=True, exist_ok=True)
    (STAGE / "code" / "test_logs").mkdir(parents=True, exist_ok=True)

    # Blocker report and audit records first, so they are inside the
    # manifest this builder writes.
    subprocess.run([sys.executable,
                    str(ROOT / "gpn-restyler/harness/build_audit.py")],
                   cwd=str(ROOT), check=True)
    for src_name, dst_name in (("blockers_report.md", "BLOCKERS.md"),
                               ("README.md", "README.md")):
        src_doc = ROOT / "dist" / src_name
        if src_doc.is_file():
            shutil.copy2(src_doc, STAGE / dst_name)

    records: list[dict] = []

    def add(rel: str, source: Path) -> None:
        records.append({"path": rel, "bytes": source.stat().st_size,
                        "sha256": sha256(source)})

    def stage_dir(dst_rel: str, files: list[str]) -> None:
        dst = STAGE / dst_rel
        dst.mkdir(parents=True, exist_ok=True)
        for rel in files:
            src = ROOT / rel
            target = dst / Path(rel).name
            shutil.copy2(src, target)
            add(f"{dst_rel}/{Path(rel).name}", src)

    # ---------------------------------------------------------------- code
    code_files: list[str] = []
    for pattern, dest in (
        ("gpn-restyler/src/slides_cli/gpn/*.py",
         "code/full/src/slides_cli/gpn"),
        ("gpn-restyler/src/slides_cli/cli.py", "code/full/src/slides_cli"),
        ("gpn-restyler/src/slides_cli/__init__.py",
         "code/full/src/slides_cli"),
        ("gpn-restyler/tools/layout/src/*.js", "code/full/tools/layout/src"),
        ("gpn-restyler/tools/layout/package.json",
         "code/full/tools/layout"),
        ("gpn-restyler/tools/layout/package-lock.json",
         "code/full/tools/layout"),
        ("gpn-restyler/harness/*.py", "code/full/harness"),
        ("gpn-restyler/skills/gpn-restyle/*", "code/full/skills/gpn-restyle"),
    ):
        for path in sorted(ROOT.glob(pattern)):
            if not path.is_file():
                continue
            dst = STAGE / dest / path.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dst)
            code_files.append(str(path.relative_to(ROOT)))
            add(f"{dest}/{path.name}", path)

    # tests: whole gpn suite + conftest/fixtures
    for path in sorted((ROOT / "gpn-restyler/tests/gpn").rglob("*.py")):
        rel = path.relative_to(ROOT)
        dst = STAGE / "code/full/tests/gpn" / path.relative_to(
            ROOT / "gpn-restyler/tests/gpn")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dst)
        code_files.append(str(rel))
        add(f"code/full/tests/gpn/{path.relative_to(ROOT / 'gpn-restyler/tests/gpn')}",
            path)

    # test logs written by the verification run of this round
    for name in ("pytest_gpn.log", "pytest_focus.log", "ruff.log"):
        src_log = ROOT / "dist" / "review_increment_universal_authoring" \
            / "code" / "test_logs" / name
        src_alt = ROOT / "dist" / "test_logs" / name
        source = src_log if src_log.is_file() else src_alt
        if source.is_file():
            dst = STAGE / "code" / "test_logs" / name
            shutil.copy2(source, dst)
            code_files.append(str(source))
            add(f"code/test_logs/{name}", source)

    for rel in ("gpn-restyler/pyproject.toml", "gpn-restyler/uv.lock",
                "gpn-restyler/config/gpn.example.toml",
                "gpn-restyler/IMPLEMENTATION_STATE.md",
                "gpn-restyler/README.md", "development_history.md",
                "AGENTS.md"):
        src = ROOT / rel
        if not src.is_file():
            continue
        dst = STAGE / "code/full" / Path(rel).name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        code_files.append(rel)
        add(f"code/full/{Path(rel).name}", src)

    # git evidence
    diff = STAGE / "code" / "git_diff.patch"
    diff.parent.mkdir(parents=True, exist_ok=True)
    diff.write_text(git("diff", "HEAD"), encoding="utf-8")
    add("code/git_diff.patch", diff)
    head = STAGE / "code" / "repo_head_and_status.txt"
    head.write_text(
        "== HEAD ==\n" + git("rev-parse", "HEAD")
        + "\n== branch ==\n" + git("rev-parse", "--abbrev-ref", "HEAD")
        + "\n== status ==\n" + git("status", "--porcelain")
        + "\n== last 20 commits ==\n" + git("log", "--oneline", "-20"),
        encoding="utf-8")
    add("code/repo_head_and_status.txt", head)

    # ---------------------------------------------------------------- runs
    for run_name in ("2026-10-17-ntc-laneflow6",
                     "2026-10-17-ntc-laneflow12"):
        src = ROOT / "runs" / "stage10" / run_name
        if not src.is_dir():
            continue
        dest = STAGE / "runs" / run_name
        skip_dirs = {"assets", "saved_assets", "candidate_assets",
                     "export_work", "profiles", "ontology_snapshot",
                     "assets_in"}
        count = 0
        for path in sorted(src.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(src)
            if any(part in EXCLUDE_DIRS for part in rel.parts):
                continue
            if rel.parts and rel.parts[0] in skip_dirs:
                continue
            if rel.suffix in EXCLUDE_SUFFIX:
                continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            add(f"runs/{run_name}/{rel.as_posix()}", path)
            count += 1
        print(f"  {run_name}: {count} files")

    for path in sorted((STAGE / "audit").rglob("*")) + \
            [STAGE / "BLOCKERS.md"]:
        if path.is_file():
            add(path.relative_to(STAGE).as_posix(), path)

    manifest = {
        "schema": "review-increment-manifest/v1",
        "complete": False,
        "reason_incomplete": [
            "no full NTC deck was built (1 of 17 candidates reached "
            "bytes), so cases/ntc final renders, selection report and "
            "product acceptance are absent",
            "Круглый_стол.pptx and ИИИ.pptx were never run through the "
            "public runner",
            "create smokes (PDF/DOCX/prompt), dense-3x and the invariance "
            "checks were not executed",
            "renders, PDFs, PPTX bytes and font binaries are excluded by "
            "an explicit size rule; their hashes live in the run manifests",
        ],
        "excluded": {
            "suffixes": sorted(EXCLUDE_SUFFIX),
            "dirs": sorted(EXCLUDE_DIRS),
            "run_subtrees": ["assets/", "saved_assets/", "candidate_assets/",
                             "export_work/", "profiles/", "ontology_snapshot/"],
            "reason": "regenerable binaries; native render/font evidence is "
                      "produced on a machine that has the installed GPN "
                      "fonts, and the internal names/hashes are recorded in "
                      "shared/profiles/font_inventory.json",
        },
        "file_count": len(records),
        "total_bytes": sum(r["bytes"] for r in records),
        "files": sorted(records, key=lambda r: r["path"]),
    }
    manifest_path = STAGE / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")

    archive = ROOT / "dist" / "review_increment_universal_authoring.zip"
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(STAGE.rglob("*")):
            if path.is_file() and path.name != "manifest.json":
                zf.write(path, path.relative_to(STAGE.parent))
        zf.write(manifest_path, "review_increment_universal_authoring/manifest.json")
    print(f"archive: {archive} "
          f"({archive.stat().st_size / 1e6:.1f} MB, {len(records)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())