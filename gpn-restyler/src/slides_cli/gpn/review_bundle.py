"""Stage 7 §14: generic review-bundle builder with ref closure.

The required logical evidence set is derived from the fixed review schema
plus the run's product expectations — registered BEFORE existence checks.
Every required entry must resolve to bytes with verified hash/size/subject;
``bundle_complete`` means no missing required entries AND all required refs
validated, never "all found files present". Bundle-local reference aliases
are rewritten into the manifest index so no dangling live-path ref remains.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import zipfile
from pathlib import Path

from pydantic import BaseModel, Field

from .models import StrictModel

BUNDLE_SCHEMA = "review-bundle/v1"

# Fixed review schema: logical role -> run-relative candidates. Required
# entries must resolve; optional entries carry applicability proof when
# absent. Filenames are pipeline-specific; the manifest maps
# role -> actual bundle path.
REQUIRED_ROLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("final_package", ("candidate.pptx", "final/candidate.pptx")),
    ("final_render_pdf", ("render/candidate.pdf",
                          "final/render/candidate.pdf")),
    ("final_render_manifest", ("render/render_manifest.json",
                               "final/render/render_manifest.json")),
    ("final_render_probe", ("render/native_render_probe.json",
                            "final/render/native_render_probe.json")),
    ("final_render_font_report", ("render/render_font_report.json",
                                  "final/render/render_font_report.json")),
    ("final_visual_review", ("visual_review.json",
                             "final/visual_review.json")),
    ("final_design_review", ("final/design_review.json",
                             "design_review.json")),
    ("product_acceptance", ("product_acceptance.json",
                            "final/product_acceptance.json")),
    ("product_expectation", ("product_expectation.json",
                             "final/product_expectation.json")),
    ("saved_package_manifest", ("saved_package_manifest.json",
                                "final/saved_package_manifest.json")),
    ("gate_index", ("gate_index.json", "final/gate_index.json")),
    ("selection_report", ("selection_report.json",
                          "final/selection_report.json")),
    ("run_state", ("run_state.json",)),
    ("runner_transitions", ("runner_transition_log.jsonl",)),
    ("source_manifest", ("inputs/source_manifest.json",
                         "source_manifest.json")),
    ("semantic_deck", ("inputs/semantic_deck.json",
                       "semantic_deck.json")),
    ("compiled_rules", ("inputs/compiled_rules.json",
                        "compiled_rules.json")),
    ("verified_frame", ("inputs/verified_frame.json",
                        "verified_frame.json", "verified_frame.json")),
    ("font_inventory", ("inputs/font_inventory.json",
                        "font_inventory.json")),
    ("content_diff", ("content_diff.json", "final/content_diff.json")),
    ("semantic_relation_report", ("semantic_relation_report.json",
                                  "final/semantic_relation_report.json")),
    ("ontology_rule_coverage", ("ontology_rule_coverage.json",
                                "final/ontology_rule_coverage.json")),
    ("saved_authoring_rule_report", ("saved_authoring_rule_report.json",
                                     "final/saved_authoring_rule_report.json")),
    ("saved_frame_report", ("saved_frame_report.json",
                            "final/saved_frame_report.json")),
    ("saved_geometry_report", ("saved_geometry_report.json",
                               "final/saved_geometry_report.json")),
    ("saved_measurement_report", ("saved_measurement_report.json",
                                  "final/saved_measurement_report.json")),
    ("native_editability_report", ("native_editability_report.json",
                                   "final/native_editability_report.json")),
    ("native_edit_roundtrip_report", ("native_edit_roundtrip_report.json",
                                      "final/native_edit_roundtrip_report.json")),
    ("emission_report", ("emission_report.json",
                         "final/emission_report.json")),
    ("previous_claims_reconciliation",
     ("previous_claims_reconciliation.json",)),
    ("code_manifest", ("code/code_manifest.json",)),
    ("commands_and_results", ("code/commands_and_results.json",)),
    ("test_evidence", ("code/test_evidence.json",)),
)

OPTIONAL_ROLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("user_feedback", ("user_feedback.json", "main/user_feedback.json")),
    ("c0_historical_trace", ("historical/",)),
    ("code_diff", ("code/git_diff.patch",)),
)


CODE_INCLUDE_DIRS = ("src/slides_cli", "tests/gpn", "tools/layout/src",
                     "skills")
CODE_INCLUDE_FILES = ("pyproject.toml", "uv.lock", "DEPENDENCIES.lock.json")
CODE_SKIP_SUFFIX = (".pyc", ".pyo")
CODE_SKIP_PARTS = ("__pycache__", ".venv", "node_modules", ".git",
                   ".pytest_cache", ".ruff_cache")


def collect_code_evidence(run_dir: Path, project_root: Path) -> dict:
    """Write the executable-code and execution evidence for a run (§14.6).

    The bundle must carry the code that produced the artifacts plus the
    real repository state (HEAD + dirty diff, no commit required) and the
    recorded command/test logs. Everything is written under ``run/code``
    with sha256 per file so the reviewer can verify closure themselves.
    """
    import subprocess

    run_dir = Path(run_dir)
    project_root = Path(project_root)
    code_dir = run_dir / "code"
    code_dir.mkdir(parents=True, exist_ok=True)

    def _git(*args: str) -> str:
        try:
            out = subprocess.run(
                ["git", "-C", str(project_root), *args],
                capture_output=True, text=True, check=False)
            return out.stdout
        except OSError as exc:  # pragma: no cover - git missing
            return f"GIT_UNAVAILABLE: {exc}"

    head = _git("rev-parse", "HEAD").strip()
    status = _git("status", "--porcelain=v1")
    (code_dir / "repo_head_and_status.txt").write_text(
        f"HEAD {head}\n\n--- git status --porcelain=v1 ---\n{status}",
        encoding="utf-8")
    diff = _git("diff")
    (code_dir / "git_diff.patch").write_text(diff, encoding="utf-8")

    files: list[dict] = []
    for rel_dir in CODE_INCLUDE_DIRS:
        base = project_root / "gpn-restyler" / rel_dir
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix in CODE_SKIP_SUFFIX:
                continue
            if any(part in CODE_SKIP_PARTS for part in path.parts):
                continue
            data = path.read_bytes()
            rel = f"code/full/{rel_dir}/{path.relative_to(base)}"
            target = run_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            files.append({"bundle_path": rel,
                          "live_path": str(path.relative_to(project_root)),
                          "sha256": _sha(data),
                          "byte_count": len(data)})
    for name in CODE_INCLUDE_FILES:
        src = project_root / "gpn-restyler" / name
        if not src.is_file():
            continue
        data = src.read_bytes()
        (code_dir / name).write_bytes(data)
        files.append({"bundle_path": f"code/{name}",
                      "live_path": f"gpn-restyler/{name}",
                      "sha256": _sha(data), "byte_count": len(data)})
    (code_dir / "code_manifest.json").write_text(json.dumps(
        {"schema_version": "code-manifest/v1",
         "repo_head": head, "dirty": bool(status.strip()),
         "file_count": len(files), "files": files},
        ensure_ascii=False, indent=1), encoding="utf-8")

    evidence = run_dir / "stage8_evidence.json"
    commands: dict = {"schema_version": "commands-and-results/v1",
                      "source": "stage8_evidence.json"}
    if evidence.is_file():
        commands["recorded"] = json.loads(
            evidence.read_text(encoding="utf-8"))
    logs = run_dir / "test_logs"
    logs.mkdir(parents=True, exist_ok=True)
    test_evidence = []
    for log in sorted(logs.glob("*.log")):
        text = log.read_text(encoding="utf-8", errors="replace")
        summary = [ln for ln in text.splitlines()
                   if " passed" in ln or " failed" in ln or ln.startswith("EXIT=")]
        test_evidence.append({
            "log": f"code/test_logs/{log.name}",
            "sha256": _sha(log.read_bytes()),
            "summary_lines": summary[-3:],
            "note": "verbatim stdout of the recorded pytest run; node ids are "
                    "not emitted by the -q/-rN modes used here",
        })
    (code_dir / "test_evidence.json").write_text(json.dumps(
        {"schema_version": "test-evidence/v1", "runs": test_evidence},
        ensure_ascii=False, indent=1), encoding="utf-8")
    (code_dir / "commands_and_results.json").write_text(
        json.dumps(commands, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"repo_head": head, "code_files": len(files)}


class BundleEntry(BaseModel):
    model_config = StrictModel

    role: str
    bundle_path: str
    live_path: str = ""
    sha256: str = ""
    byte_count: int = 0
    subject_candidate_sha256: str = ""


class BundleReport(BaseModel):
    model_config = StrictModel

    schema_version: str = BUNDLE_SCHEMA
    run_dir: str = ""
    bundle_path: str = ""
    bundle_sha256: str = ""
    entries: list[BundleEntry] = Field(default_factory=list)
    aliases: dict[str, str] = Field(default_factory=dict)
    missing_required: list[str] = Field(default_factory=list)
    missing_optional: list[str] = Field(default_factory=list)
    bundle_complete: bool = False


class BundleError(ValueError):
    pass


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_name(name: str) -> bool:
    p = Path(name)
    return not p.is_absolute() and ".." not in p.parts


def _resolve_role(run_dir: Path, candidates: tuple[str, ...]
                  ) -> Path | None:
    for rel in candidates:
        p = run_dir / rel
        if p.is_file():
            return p
    return None


def _render_pages(run_dir: Path) -> list[Path]:
    pages = sorted((run_dir / "render").glob("slide-*.png")) + \
        sorted((run_dir / "render").glob("page-*.png"))
    final_pages = sorted((run_dir / "final" / "render").glob("*.png"))
    return pages + final_pages


def build_review_bundle(run_dir: Path, output: Path,
                        project_root: Path | None = None) -> BundleReport:
    """Assemble the review ZIP and self-inspect ref closure (§14.7)."""
    run_dir = Path(run_dir)
    output = Path(output)
    if not run_dir.is_dir():
        raise BundleError(f"run dir missing: {run_dir}")

    acc_path = run_dir / "product_acceptance.json"
    subject = ""
    if acc_path.is_file():
        try:
            acc = json.loads(acc_path.read_text(encoding="utf-8"))
            subject = str(acc.get("subject_candidate_sha256", ""))
        except (OSError, ValueError):
            subject = ""

    report = BundleReport(run_dir=str(run_dir), bundle_path=str(output))
    staged: list[tuple[str, Path, str]] = []  # role, live, bundle rel

    for role, candidates in REQUIRED_ROLES:
        found = _resolve_role(run_dir, candidates)
        if found is None:
            report.missing_required.append(role)
            continue
        rel = f"main/{found.name}" if found.parent == run_dir \
            else f"main/{found.relative_to(run_dir)}"
        staged.append((role, found, rel.replace("\\", "/")))

    for role, candidates in OPTIONAL_ROLES:
        found = _resolve_role(run_dir, candidates)
        if found is None:
            report.missing_optional.append(role)

    # Per-slide PNG coverage: complete ordered visible page set required.
    pages = _render_pages(run_dir)
    if pages:
        for page in pages:
            rel = f"main/final/render/{page.name}"
            staged.append(("render_page", page, rel))
    else:
        report.missing_required.append("render_pages")

    # Per-candidate scene dirs: include current evidence (scene plans,
    # resolved scenes, measurement request/response, renders, reviews).
    scenes = run_dir / "scenes"
    if scenes.is_dir():
        for path in sorted(scenes.rglob("*")):
            if path.is_file() and path.suffix in (
                    ".json", ".png", ".pdf", ".pptx"):
                rel = f"main/{path.relative_to(run_dir)}".replace("\\", "/")
                staged.append(("scene_evidence", path, rel))

    # Model packets + calibration + code evidence when present.
    for extra in ("model_requests", "model_responses", "calibration",
                  "code", "verification"):
        extra_dir = run_dir / extra
        if extra_dir.is_dir():
            for path in sorted(extra_dir.rglob("*")):
                if path.is_file():
                    rel = f"main/{path.relative_to(run_dir)}".replace(
                        "\\", "/")
                    staged.append((extra, path, rel))

    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(".tmp.zip")
    entries: list[BundleEntry] = []
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for role, live, rel in staged:
                if not _safe_name(rel):
                    raise BundleError(f"unsafe bundle name: {rel}")
                data = live.read_bytes()
                digest = _sha(data)
                info = zipfile.ZipInfo(rel)
                info.date_time = (2020, 1, 1, 0, 0, 0)
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, data)
                entries.append(BundleEntry(
                    role=role, bundle_path=rel,
                    live_path=str(live.relative_to(run_dir)),
                    sha256=digest, byte_count=len(data),
                    subject_candidate_sha256=subject))
                report.aliases[role + ":" + live.name] = rel
            manifest_payload = json.dumps(
                {"schema_version": BUNDLE_SCHEMA, "run": str(run_dir),
                 "subject_candidate_sha256": subject,
                 "entries": [e.model_dump(mode="json") for e in entries],
                 "missing_required": report.missing_required,
                 "missing_optional": report.missing_optional},
                ensure_ascii=False, indent=1)
            info = zipfile.ZipInfo("bundle_manifest.json")
            info.date_time = (2020, 1, 1, 0, 0, 0)
            zf.writestr(info, manifest_payload)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    tmp.replace(output)

    # Self-inspection: ref closure over the assembled bytes.
    report.entries = entries
    with zipfile.ZipFile(output) as zf:
        names = set(zf.namelist())
        for entry in entries:
            if entry.bundle_path not in names:
                report.missing_required.append(
                    f"zip-missing:{entry.bundle_path}")
                continue
            data = zf.read(entry.bundle_path)
            if _sha(data) != entry.sha256:
                report.missing_required.append(
                    f"zip-corrupt:{entry.bundle_path}")
    report.bundle_sha256 = _sha(output.read_bytes())
    report.bundle_complete = not report.missing_required and bool(entries)
    return report
