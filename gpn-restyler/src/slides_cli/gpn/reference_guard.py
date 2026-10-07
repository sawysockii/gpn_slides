"""Reference-source whitelist guard (Stage 4 §0.1, §1.4).

The only allowed style-reference root is ``<project_root>/slide_examples``.
Every reference loader, index builder, packet builder and project skill
reference tool must call :func:`assert_allowed_reference` before reading.

- Canonical resolved path must stay inside the single allowed root.
- The file must have a manifest entry with a matching sha256 (no stale
  cache/packet/plan with foreign provenance is usable).
- Symlinks escaping the root, path traversal and remote URLs are denied
  with ``REFERENCE/FORBIDDEN_SOURCE`` (never a silent fallback).
- Derived thumbnails/descriptors are allowed only with proven lineage to
  an allowed file (``lineage`` record pointing at the manifest entry).

Legacy upstream sample paths removed by the user (``gpn-restyler/examples/``,
agent-slides demo/showcase folders) are denied explicitly even if a stale
cache still names them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

FORBIDDEN_CODE = "REFERENCE/FORBIDDEN_SOURCE"

# Exact legacy paths that must never resolve as references again. These are
# path suffixes (POSIX) matched against the canonical path relative to either
# the project root or the code root.
LEGACY_FORBIDDEN_SUFFIXES = (
    "gpn-restyler/examples/example1.pptx",
    "gpn-restyler/examples/example2.pptx",
    "gpn-restyler/examples/example3.pptx",
    "gpn-restyler/examples",
    "examples/example1.pptx",
    "examples/example2.pptx",
    "examples/example3.pptx",
    "agent-slides/examples",
    "agent-slides/demo",
    "agent-slides/showcase",
    "agent-slides/templates",
)

REMOTE_SCHEMES = ("http", "https", "ftp", "s3", "gs")


@dataclass(frozen=True, slots=True)
class AllowedReference:
    """Provenance proof for one allowed reference file."""

    canonical_path: str
    relative_path: str
    sha256: str
    purpose: str
    derived: bool = False


class ForbiddenReferenceError(ValueError):
    """Raised when a reference source is outside the user whitelist."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code


def _is_remote(value: str) -> bool:
    lowered = value.strip().lower()
    if "://" not in lowered and not lowered.startswith("mailto:"):
        return False
    scheme = urlparse(lowered).scheme
    return scheme in REMOTE_SCHEMES


def _manifest_index(manifest: Any) -> dict[str, str]:
    """Build {relative_path: sha256} from a manifest payload."""
    index: dict[str, str] = {}
    if manifest is None:
        return index
    files: Any = None
    if isinstance(manifest, dict):
        files = manifest.get("files")
        if files is None and "relative_path" in manifest:
            files = [manifest]
    else:
        files = getattr(manifest, "files", None)
        if files is None and hasattr(manifest, "relative_path"):
            files = [manifest]
    for entry in files or []:
        if isinstance(entry, dict):
            rel = str(entry.get("relative_path", ""))
            digest = str(entry.get("sha256", ""))
        else:
            rel = str(getattr(entry, "relative_path", ""))
            digest = str(getattr(entry, "sha256", ""))
        if rel and digest:
            index[rel] = digest
    return index


def _check_legacy_suffix(posix_rel: str, candidate: str) -> bool:
    return posix_rel == candidate or posix_rel.endswith("/" + candidate)


def assert_allowed_reference(
    path: str | Path,
    purpose: str,
    project_root: str | Path,
    manifest: Any,
    *,
    expected_sha256: str | None = None,
    lineage: dict[str, Any] | None = None,
) -> AllowedReference:
    """Validate one reference file against the user-corpus whitelist.

    Args:
        path: candidate reference path (absolute, relative or remote URL).
        purpose: why the reference is read (packet/index/skill/...).
        project_root: the project root owning ``slide_examples/``.
        manifest: example discovery manifest (dict or model with ``files``).
        expected_sha256: optional pinned digest the file must match.
        lineage: optional derivative proof
            (``{"source_relative_path": ..., "source_sha256": ...}``).

    Returns:
        Provenance proof for the allowed file.

    Raises:
        ForbiddenReferenceError: with code ``REFERENCE/FORBIDDEN_SOURCE``.
    """
    raw = str(path)
    if _is_remote(raw):
        raise ForbiddenReferenceError(
            FORBIDDEN_CODE,
            f"remote reference denied for purpose {purpose!r}: {raw!r}",
        )
    root = Path(project_root).expanduser().resolve()
    allowed_root = (root / "slide_examples").resolve()
    candidate = (Path(raw).expanduser() if Path(raw).is_absolute() else (root / raw))
    try:
        canonical = candidate.resolve()
    except OSError as exc:
        raise ForbiddenReferenceError(
            FORBIDDEN_CODE, f"unresolvable reference {raw!r}: {exc}"
        ) from exc
    if canonical.is_symlink() or any(p.is_symlink() for p in [canonical]):
        # A symlink that itself resolves outside the root is denied below;
        # a symlink inside is still denied unless its target is confined.
        pass
    try:
        rel = canonical.relative_to(allowed_root).as_posix()
    except ValueError:
        # Also report the project-relative form for legacy-suffix matching.
        try:
            proj_rel = canonical.relative_to(root).as_posix()
        except ValueError:
            proj_rel = canonical.as_posix()
        for legacy in LEGACY_FORBIDDEN_SUFFIXES:
            if _check_legacy_suffix(proj_rel, legacy) or _check_legacy_suffix(
                raw.replace("\\", "/"), legacy
            ):
                raise ForbiddenReferenceError(
                    FORBIDDEN_CODE,
                    f"legacy upstream sample denied for purpose {purpose!r}: {raw!r}",
                ) from None
        raise ForbiddenReferenceError(
            FORBIDDEN_CODE,
            f"reference outside slide_examples/ denied for purpose {purpose!r}: {raw!r}",
        ) from None
    project_rel = f"slide_examples/{rel}"
    for legacy in LEGACY_FORBIDDEN_SUFFIXES:
        if _check_legacy_suffix(project_rel, legacy):
            raise ForbiddenReferenceError(
                FORBIDDEN_CODE,
                f"legacy upstream sample denied for purpose {purpose!r}: {raw!r}",
            )
    if lineage is not None:
        src_rel = str(lineage.get("source_relative_path", ""))
        src_hash = str(lineage.get("source_sha256", ""))
        if src_rel != project_rel:
            raise ForbiddenReferenceError(
                FORBIDDEN_CODE,
                f"derivative lineage mismatch for {raw!r}: "
                f"lineage points at {src_rel!r}",
            )
        index = _manifest_index(manifest)
        if index.get(src_rel) != src_hash or not src_hash:
            raise ForbiddenReferenceError(
                FORBIDDEN_CODE,
                f"derivative lineage has no manifest proof for {src_rel!r}",
            )
        return AllowedReference(
            canonical_path=str(canonical),
            relative_path=project_rel,
            sha256=src_hash,
            purpose=purpose,
            derived=True,
        )
    if not canonical.is_file():
        raise ForbiddenReferenceError(
            FORBIDDEN_CODE, f"reference is not a file for purpose {purpose!r}: {raw!r}"
        )
    index = _manifest_index(manifest)
    recorded = index.get(project_rel, "")
    if not recorded:
        raise ForbiddenReferenceError(
            FORBIDDEN_CODE,
            f"reference has no manifest entry for purpose {purpose!r}: {project_rel!r}",
        )
    if expected_sha256 and expected_sha256 != recorded:
        raise ForbiddenReferenceError(
            FORBIDDEN_CODE,
            f"reference manifest mismatch for {project_rel!r}: "
            "pinned sha256 differs from the discovery manifest",
        )
    # Verify actual bytes match the manifest (stale cache with the same path
    # but different content is denied).
    import hashlib

    h = hashlib.sha256()
    with open(canonical, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    actual = h.hexdigest()
    if actual != recorded:
        raise ForbiddenReferenceError(
            FORBIDDEN_CODE,
            f"reference content changed since manifest for {project_rel!r}",
        )
    return AllowedReference(
        canonical_path=str(canonical),
        relative_path=project_rel,
        sha256=actual,
        purpose=purpose,
        derived=False,
    )


def check_plan_example_provenance(
    plan: dict[str, Any],
    project_root: str | Path,
    manifest: Any,
) -> list[str]:
    """Validate every ``reference_ids`` entry of a plan packet/intent.

    Returns the list of denial messages (empty = all allowed).
    """
    denials: list[str] = []
    refs = plan.get("reference_ids") or plan.get("referenceIds") or []
    for ref in refs if isinstance(refs, list) else []:
        try:
            assert_allowed_reference(ref, "plan_reference", project_root, manifest)
        except ForbiddenReferenceError as exc:
            denials.append(str(exc))
    return denials
