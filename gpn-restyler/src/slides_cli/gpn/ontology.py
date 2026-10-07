"""Mandatory corpus resolution: ``project_root/ontology`` and
``project_root/slide_examples`` (spec §1.0, §3.3, §3.4).

The two corpora are always ``project_root / "ontology"`` and
``project_root / "slide_examples"`` — never ``Path("/ontology")`` and never a
directory outside the user's project. Originals are read without modification;
derived manifests are written to run/cache directories.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable
from pathlib import Path

from .errors import AssetMissingError, InputError, SchemaError
from .models import CorpusFileRecord, ExampleDiscoveryReport, Issue, OntologySources, Severity

log = logging.getLogger(__name__)

SUPPORTED_CORPUS_FORMATS = {".json": "json", ".md": "markdown"}
TEXT_READ_LIMIT = 512 * 1024  # only metadata-bearing JSON is parsed in full


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_corpus_hash(records: Iterable[CorpusFileRecord]) -> str:
    """SHA-256 of canonical JSON ``[(relative_path, sha256, normative), ...]``."""
    payload = [
        (r.relative_path, r.sha256, r.normative)
        for r in sorted(records, key=lambda r: r.relative_path)
    ]
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(blob)


def _iter_corpus_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.name.startswith("."):
            files.append(path)
    return files


def _confined(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _record_for(path: Path, project_root: Path, normative: bool) -> CorpusFileRecord:
    suffix = path.suffix.lower()
    kind = SUPPORTED_CORPUS_FORMATS.get(suffix, "attachment")
    data = path.read_bytes()
    return CorpusFileRecord(
        relative_path=path.resolve().relative_to(project_root.resolve()).as_posix(),
        kind=kind,  # type: ignore[arg-type]
        sha256=sha256_bytes(data),
        byte_count=len(data),
        normative=normative,
    )


def _select_primary_json(
    json_files: list[Path], selected_file: Path | None, ontology_root: Path
) -> tuple[Path | None, list[Issue], list[str]]:
    issues: list[Issue] = []
    if selected_file is not None:
        if not _confined(selected_file, ontology_root):
            raise InputError(
                "ONTOLOGY_FILE_OUTSIDE_CORPUS",
                f"selected ontology file must live inside {ontology_root}: {selected_file}",
                artifact_paths=[selected_file],
            )
        if not selected_file.is_file():
            raise InputError(
                "ONTOLOGY_FILE_NOT_FOUND",
                f"selected ontology file not found: {selected_file}",
                artifact_paths=[selected_file],
            )
        return selected_file, issues, [str(selected_file)]

    candidates: list[tuple[Path, str, str]] = []
    for path in json_files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            issues.append(
                Issue(
                    rule_id="ONTOLOGY_JSON_PARSE",
                    code="ONTOLOGY_JSON_PARSE_FAILED",
                    severity=Severity.ERROR,
                    details=f"{path.name}: {exc}",
                    evidence=[str(path)],
                    repairable=False,
                )
            )
            continue
        metadata = data.get("metadata") if isinstance(data, dict) else None
        if not isinstance(metadata, dict):
            continue
        if metadata.get("name") != "Онтология оформления деловых презентаций по библиотеке ГПН":
            continue
        candidates.append((path, str(metadata.get("version", "")), str(metadata.get("date", ""))))

    if not candidates:
        issues.append(
            Issue(
                rule_id="ONTOLOGY_PRIMARY_JSON",
                code="ONTOLOGY_PRIMARY_JSON_MISSING",
                severity=Severity.ERROR,
                details="no ontology JSON with the expected metadata.name found in "
                f"{ontology_root}",
                evidence=[str(ontology_root)],
                repairable=False,
            )
        )
        return None, issues, []

    versions = {(v, d) for _, v, d in candidates}
    if len(candidates) > 1 and len(versions) > 1:
        # Ambiguous: never guess by filename or mtime.
        issues.append(
            Issue(
                rule_id="ONTOLOGY_PRIMARY_JSON",
                code="ONTOLOGY_SELECTION_AMBIGUOUS",
                severity=Severity.ERROR,
                details=(
                    "multiple incompatible ontology JSON candidates found; "
                    "select one explicitly via paths.ontology_file"
                ),
                evidence=[f"{p.name} version={v} date={d}" for p, v, d in candidates],
                repairable=True,
            )
        )
        return None, issues, [str(p) for p, _, _ in candidates]

    # Same version: deterministic pick by relative path order.
    chosen = sorted(candidates, key=lambda item: item[0].as_posix())[0][0]
    if len(candidates) > 1:
        issues.append(
            Issue(
                rule_id="ONTOLOGY_PRIMARY_JSON",
                code="ONTOLOGY_DUPLICATE_SAME_VERSION",
                severity=Severity.WARNING,
                details=f"{len(candidates)} ontology JSON files share one version; "
                f"using {chosen.name}",
                evidence=[str(p) for p, _, _ in candidates],
                repairable=False,
            )
        )
    return chosen, issues, [str(p) for p, _, _ in candidates]


def resolve_ontology_sources(
    project_root: Path, selected_file: Path | None = None
) -> OntologySources:
    """Resolve the mandatory ``project_root/ontology`` corpus (spec §3.4)."""
    project_root = Path(project_root).expanduser().resolve()
    root = project_root / "ontology"
    if not root.is_dir():
        raise AssetMissingError(
            "ONTOLOGY_CORPUS_MISSING",
            f"mandatory ontology corpus not found: {root}",
            artifact_paths=[root],
        )

    files = _iter_corpus_files(root)
    issues: list[Issue] = []
    records: list[CorpusFileRecord] = []
    json_files: list[Path] = []
    markdown_files: list[Path] = []

    for path in files:
        if path.is_symlink() and not _confined(path, root):
            issues.append(
                Issue(
                    rule_id="ONTOLOGY_SYMLINK_CONFINEMENT",
                    code="CORPUS_SYMLINK_ESCAPES_ROOT",
                    severity=Severity.ERROR,
                    details=f"symlink escapes corpus root: {path}",
                    evidence=[str(path)],
                    repairable=False,
                )
            )
            continue
        suffix = path.suffix.lower()
        if suffix == ".json":
            json_files.append(path)
        elif suffix == ".md":
            markdown_files.append(path)
        else:
            issues.append(
                Issue(
                    rule_id="ONTOLOGY_ATTACHMENT_FORMAT",
                    code="CORPUS_UNSUPPORTED_ATTACHMENT_FORMAT",
                    severity=Severity.WARNING,
                    details=f"unknown normative attachment format in ontology/: {path.name}",
                    evidence=[str(path)],
                    repairable=False,
                )
            )
        try:
            records.append(_record_for(path, project_root, normative=True))
        except OSError as exc:
            issues.append(
                Issue(
                    rule_id="ONTOLOGY_READ",
                    code="CORPUS_FILE_UNREADABLE",
                    severity=Severity.ERROR,
                    details=f"{path.name}: {exc}",
                    evidence=[str(path)],
                    repairable=False,
                )
            )

    primary, selection_issues, candidates = _select_primary_json(json_files, selected_file, root)
    issues.extend(selection_issues)

    # Detect unreadable/broken markdown normative files.
    for path in markdown_files:
        try:
            path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            issues.append(
                Issue(
                    rule_id="ONTOLOGY_MARKDOWN_READ",
                    code="CORPUS_NORMATIVE_FILE_BROKEN",
                    severity=Severity.ERROR,
                    details=f"{path.name}: {exc}",
                    evidence=[str(path)],
                    repairable=False,
                )
            )

    selected_version: str | None = None
    ontology_hash: str | None = None
    if primary is not None:
        ontology_hash = sha256_file(primary)
        try:
            meta = json.loads(primary.read_text(encoding="utf-8")).get("metadata", {})
            selected_version = str(meta.get("version") or "") or None
        except (json.JSONDecodeError, UnicodeDecodeError):
            issues.append(
                Issue(
                    rule_id="ONTOLOGY_JSON_PARSE",
                    code="ONTOLOGY_JSON_PARSE_FAILED",
                    severity=Severity.ERROR,
                    details=f"cannot parse selected ontology JSON: {primary.name}",
                    evidence=[str(primary)],
                    repairable=False,
                )
            )

    corpus_hash = canonical_corpus_hash(records)
    has_blocking = any(i.severity == Severity.ERROR for i in issues)
    ready = bool(records) and primary is not None and not has_blocking

    return OntologySources(
        root=root,
        primary_json=primary,
        markdown_files=sorted(markdown_files, key=lambda p: p.as_posix()),
        normative_files=sorted(records, key=lambda r: r.relative_path),
        ontology_hash=ontology_hash,
        ontology_corpus_hash=corpus_hash,
        issues=issues,
        ready=ready,
        selected_version=selected_version,
        candidates=candidates,
    )


def discover_slide_examples(project_root: Path) -> ExampleDiscoveryReport:
    """Resolve ``project_root/slide_examples`` (spec §3.4, step 7)."""
    project_root = Path(project_root).expanduser().resolve()
    root = project_root / "slide_examples"
    issues: list[Issue] = []
    if not root.is_dir():
        return ExampleDiscoveryReport(
            root=root,
            files=[],
            examples_corpus_hash=canonical_corpus_hash([]),
            missing=[str(root)],
            issues=[
                Issue(
                    rule_id="SLIDE_EXAMPLES_MISSING",
                    code="SLIDE_EXAMPLES_CORPUS_MISSING",
                    severity=Severity.ERROR,
                    details=f"slide_examples corpus not found: {root}",
                    evidence=[str(root)],
                    repairable=False,
                )
            ],
            ready=False,
        )

    records: list[CorpusFileRecord] = []
    supported = {".pptx", ".pdf", ".png", ".jpeg", ".jpg", ".svg"}
    for path in _iter_corpus_files(root):
        if path.is_symlink() and not _confined(path, root):
            issues.append(
                Issue(
                    rule_id="SLIDE_EXAMPLES_SYMLINK",
                    code="CORPUS_SYMLINK_ESCAPES_ROOT",
                    severity=Severity.ERROR,
                    details=f"symlink escapes corpus root: {path}",
                    evidence=[str(path)],
                    repairable=False,
                )
            )
            continue
        if path.suffix.lower() not in supported:
            issues.append(
                Issue(
                    rule_id="SLIDE_EXAMPLES_FORMAT",
                    code="EXAMPLE_UNSUPPORTED_FORMAT",
                    severity=Severity.WARNING,
                    details=f"unsupported example format (visual evidence only for "
                    f"pdf/png/jpeg/svg): {path.name}",
                    evidence=[str(path)],
                    repairable=False,
                )
            )
        records.append(_record_for(path, project_root, normative=False))

    missing: list[str] = []
    if not records:
        missing.append(str(root))
        issues.append(
            Issue(
                rule_id="SLIDE_EXAMPLES_EMPTY",
                code="SLIDE_EXAMPLES_CORPUS_EMPTY",
                severity=Severity.ERROR,
                details="slide_examples/ contains no usable files",
                evidence=[str(root)],
                repairable=False,
            )
        )

    corpus_hash = canonical_corpus_hash(records)
    ready = bool(records) and not any(i.severity == Severity.ERROR for i in issues)
    return ExampleDiscoveryReport(
        root=root,
        files=sorted(records, key=lambda r: r.relative_path),
        examples_corpus_hash=corpus_hash,
        missing=missing,
        issues=issues,
        ready=ready,
    )


def write_corpus_manifests(
    sources: OntologySources,
    discovery: ExampleDiscoveryReport,
    destination: Path,
) -> dict[str, Path]:
    """Persist corpus manifests outside the corpora themselves (spec §3.4, step 8)."""
    destination.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    def _dump(name: str, payload: dict[str, object]) -> Path:
        target = destination / name
        blob = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(blob + "\n", encoding="utf-8")
        tmp.replace(target)
        written[name] = target
        return target

    _dump(
        "ontology_manifest.json",
        {
            "root": str(sources.root),
            "primary_json": str(sources.primary_json) if sources.primary_json else None,
            "selected_version": sources.selected_version,
            "ontology_hash": sources.ontology_hash,
            "ontology_corpus_hash": sources.ontology_corpus_hash,
            "markdown_files": [str(p) for p in sources.markdown_files],
            "files": [r.model_dump(mode="json") for r in sources.normative_files],
            "ready": sources.ready,
            "issues": [i.model_dump(mode="json") for i in sources.issues],
        },
    )
    _dump(
        "examples_manifest.json",
        {
            "root": str(discovery.root),
            "examples_corpus_hash": discovery.examples_corpus_hash,
            "files": [r.model_dump(mode="json") for r in discovery.files],
            "missing": discovery.missing,
            "ready": discovery.ready,
            "issues": [i.model_dump(mode="json") for i in discovery.issues],
        },
    )
    conflicts = [i.model_dump(mode="json") for i in sources.issues if i.severity == Severity.ERROR]
    _dump("ontology_conflicts.json", {"conflicts": conflicts})
    return written


def require_ready_for_strict_export(sources: OntologySources) -> None:
    """Block strict production output until the corpus is unambiguous (§1.0)."""
    if sources.ready:
        return
    raise AssetMissingError(
        "ONTOLOGY_NOT_READY",
        "ontology corpus is not ready for strict export: "
        + "; ".join(i.code for i in sources.issues if i.severity == Severity.ERROR),
        artifact_paths=[sources.root],
    )


def describe_sources(sources: OntologySources) -> str:
    return (
        f"ontology root={sources.root} version={sources.selected_version} "
        f"hash={sources.ontology_hash} corpus={sources.ontology_corpus_hash} "
        f"ready={sources.ready}"
    )


def assert_selected_file_allowed(selected_file: Path | None, ontology_root: Path) -> None:
    """Validate an explicit ``--ontology-file`` selection early (§3.4)."""
    if selected_file is None:
        return
    if not _confined(selected_file, ontology_root):
        raise SchemaError(
            "ONTOLOGY_FILE_OUTSIDE_CORPUS",
            f"selected ontology file must live inside {ontology_root}",
            artifact_paths=[selected_file],
        )
