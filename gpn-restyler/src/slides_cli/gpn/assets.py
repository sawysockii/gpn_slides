"""Content-addressed asset storage and atomic artifact writes (spec §9.3, §9.4)."""

from __future__ import annotations

import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .errors import InputError, PreservationError
from .models import ArtifactRef, AssetRef

_EXT_BY_MEDIA_TYPE = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "image/x-emf": ".emf",
    "image/x-wmf": ".wmf",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.presentationml.template": ".potx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.drawingml.chart": ".xml",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "application/pdf": ".pdf",
    "font/ttf": ".ttf",
    "font/otf": ".otf",
    "application/vnd.openxmlformats-package.relationships+xml": ".rels",
    "application/vnd.openxmlformats-officedocument.theme+xml": ".xml",
    "application/octet-stream": ".bin",
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extension_for(media_type: str, explicit: str | None = None) -> str:
    if explicit:
        return explicit if explicit.startswith(".") else f".{explicit}"
    return _EXT_BY_MEDIA_TYPE.get(media_type, ".bin")


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    raise TypeError(f"not JSON serializable: {type(value)!r}")


def atomic_write_bytes(path: Path, data: bytes) -> ArtifactRef:
    """Write bytes atomically (same-filesystem ``os.replace``)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return ArtifactRef(absolute_path=path, sha256=sha256_bytes(data), byte_count=len(data))


def atomic_write_json(path: Path, value: BaseModel | dict[str, Any] | list[Any]) -> ArtifactRef:
    """Deterministic UTF-8 JSON write; Decimal as string, sorted metadata keys."""
    if isinstance(value, BaseModel):
        payload: Any = value.model_dump(mode="json")
    else:
        payload = value
    blob = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        default=_json_default,
    ).encode("utf-8")
    return atomic_write_bytes(path, blob + b"\n")


class AssetStore:
    """Immutable SHA-256-addressed asset store rooted at ``root`` (spec §9.3)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def _path_for(self, digest: str, ext: str) -> Path:
        return self.root / digest[:2] / f"{digest}{ext}"

    def put(self, data: bytes, media_type: str, source_ref: str | None = None) -> AssetRef:
        """Store bytes; returns an existing asset when the hash already matches."""
        digest = sha256_bytes(data)
        ext = _extension_for(media_type)
        # Reuse any existing extension for this digest.
        existing = sorted(self.root.glob(f"*/{digest}.*")) if self.root.exists() else []
        if existing:
            path = existing[0]
            if path.stat().st_size != len(data) or sha256_file(path) != digest:
                raise PreservationError(
                    "ASSET_HASH_MISMATCH",
                    f"stored asset {path} does not match its content hash",
                    artifact_paths=[path],
                )
            return AssetRef(
                sha256=digest,
                relative_path=path.relative_to(self.root).as_posix(),
                media_type=media_type,
                byte_count=len(data),
            )
        target = self._path_for(digest, ext)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        with tmp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        return AssetRef(
            sha256=digest,
            relative_path=target.relative_to(self.root).as_posix(),
            media_type=media_type,
            byte_count=len(data),
        )

    def resolve(self, ref: AssetRef) -> Path:
        """Validate containment, existence, size and hash of a stored asset."""
        candidate = (self.root / ref.relative_path).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise InputError(
                "ASSET_OUTSIDE_STORE",
                f"asset path escapes store root: {ref.relative_path}",
            ) from exc
        if not candidate.is_file():
            raise InputError("ASSET_NOT_FOUND", f"asset missing: {ref.relative_path}",
                             artifact_paths=[candidate])
        size = candidate.stat().st_size
        if size != ref.byte_count:
            raise PreservationError(
                "ASSET_SIZE_MISMATCH",
                f"asset size {size} != declared {ref.byte_count}: {ref.relative_path}",
                artifact_paths=[candidate],
            )
        digest = sha256_file(candidate)
        if digest != ref.sha256:
            raise PreservationError(
                "ASSET_HASH_MISMATCH",
                f"asset hash {digest} != declared {ref.sha256}: {ref.relative_path}",
                artifact_paths=[candidate],
            )
        return candidate

    def read_bytes(self, ref: AssetRef) -> bytes:
        return self.resolve(ref).read_bytes()

    def path_of(self, ref: AssetRef) -> Path:
        return self.root / ref.relative_path
