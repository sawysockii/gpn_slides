"""TOML configuration and project-root resolution (spec §3.4, §6, §9.1).

All resource paths resolve against ``project_root`` (never the process cwd or
the git root of a nested code clone). The single documented exception is
``paths.ontology_file``, which resolves inside ``paths.ontology_dir``.
"""

from __future__ import annotations

import ipaddress
import tomllib
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .errors import InputError, SchemaError
from .models import Scalar

CANONICAL_ONTOLOGY_DIR = "ontology"
CANONICAL_REFERENCES_DIR = "slide_examples"


class PathsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ontology_dir: str = CANONICAL_ONTOLOGY_DIR
    ontology_file: str = ""  # "" -> None; otherwise relative to ontology_dir
    template: str = "assets/templates/gpn_base.pptx"
    fonts_dir: str = "assets/fonts"
    references_dir: str = CANONICAL_REFERENCES_DIR
    runs_dir: str = "runs"

    @property
    def ontology_file_resolved(self) -> str | None:
        return self.ontology_file or None


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Stage 3.5 amendment 1.0: "harness" performs every LLM call through the
    # agent harness' own online model via the model_requests/model_responses
    # JSON contract; "local" is the (not yet connected) HTTP endpoint.
    provider: Literal["local", "harness"] = "local"
    base_url: str = "http://127.0.0.1:1234/v1"
    model_id: str = ""
    temperature: float = 0.25
    max_output_tokens: int = 4096
    context_budget_tokens: int = 12000
    timeout_seconds: float = 600
    max_retries: int = 1
    concurrency: int = 1
    use_json_schema: Literal["auto", "always", "never"] = "auto"
    # Bounded wait for the harness answer file per generation request
    # (provider=harness only; local HTTP uses timeout_seconds).
    harness_wait_seconds: float = 900.0


class LayoutConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    css_px_per_in: float = 96.0
    geometry_tolerance_pt: float = 0.75
    text_safety_padding_pt: float = 1.0
    max_local_layout_attempts: int = 6
    candidate_count: int = 3


class PolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["preserve_structure", "recompose"] = "preserve_structure"
    allow_split: bool = False
    allow_cross_slide_move: bool = False
    allow_text_rewrite: bool = False
    allow_unsupported_raster: bool = False
    require_native_data_objects: bool = True
    max_model_repairs_per_slide: int = 2


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_render_processes: int = 1
    max_model_requests: int = 1
    render_timeout_seconds: float = 180
    offline: bool = True


class DocumentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models_dir: str = "assets/models/docling"
    parser_device: Literal["cpu", "mps"] = "cpu"
    parser_workers: int = 1
    ocr_mode: Literal["auto", "off", "force"] = "auto"
    ocr_languages: list[str] = Field(default_factory=lambda: ["rus", "eng"])
    content_policy: Literal["preserve", "faithful_summary", "concept"] = "faithful_summary"
    target_slide_count: int = 0  # 0 = auto
    allow_external_fetch: bool = False
    allow_illustrative_data: bool = False


class AppConfig(BaseModel):
    """Validated immutable application configuration (spec §6)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1.0"
    default_workflow: Literal["presentation", "document"] = "presentation"
    paths: PathsConfig = Field(default_factory=PathsConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)
    layout: LayoutConfig = Field(default_factory=LayoutConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    document: DocumentConfig = Field(default_factory=DocumentConfig)
    project_root: Path
    config_path: Path
    allowed_remote_endpoints: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistency(self) -> AppConfig:
        if self.model.concurrency != self.runtime.max_model_requests:
            raise ValueError(
                "model.concurrency must equal runtime.max_model_requests (both must be 1)"
            )
        if self.model.concurrency != 1:
            raise ValueError("model.concurrency must be 1: sequential model calls only")
        if self.policy.mode == "preserve_structure" and self.policy.allow_split:
            raise ValueError(
                "policy.allow_split=true is invalid with policy.mode=preserve_structure"
            )
        _validate_base_url(self.model.base_url, self.allowed_remote_endpoints)
        for name in (
            "timeout_seconds",
            "harness_wait_seconds",
        ):
            if getattr(self.model, name) <= 0:
                raise ValueError(f"model.{name} must be positive")
        if self.layout.css_px_per_in <= 0:
            raise ValueError("layout.css_px_per_in must be positive")
        if self.document.target_slide_count < 0:
            raise ValueError("document.target_slide_count must be >= 0")
        return self

    # -- derived helpers -------------------------------------------------
    def resolve_path(self, relative: str | Path) -> Path:
        p = Path(relative)
        return p if p.is_absolute() else (self.project_root / p)

    @property
    def ontology_dir(self) -> Path:
        return self.resolve_path(self.paths.ontology_dir)

    @property
    def references_dir(self) -> Path:
        return self.resolve_path(self.paths.references_dir)

    @property
    def runs_dir(self) -> Path:
        return self.resolve_path(self.paths.runs_dir)

    @property
    def selected_ontology_file(self) -> Path | None:
        rel = self.paths.ontology_file_resolved
        return None if rel is None else self.ontology_dir / rel

    @property
    def target_slide_count(self) -> int | None:
        return None if self.document.target_slide_count == 0 else self.document.target_slide_count


def _validate_base_url(base_url: str, allowed_remote: list[str]) -> None:
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"model.base_url must be an http(s) URL, got {base_url!r}")
    host = parsed.hostname
    if host in allowed_remote:
        return
    if host in ("localhost",):
        return
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError(
            f"model.base_url host {host!r} is not loopback; add it to "
            "allowed_remote_endpoints to permit it explicitly"
        ) from None
    if not addr.is_loopback:
        raise ValueError(
            f"model.base_url host {host!r} is not loopback; add it to "
            "allowed_remote_endpoints to permit it explicitly"
        )


def resolve_project_root(config_path: Path, explicit_root: Path | None = None) -> Path:
    """Resolve the user's project root exactly once (spec §3.4, step 1).

    Standard layout ``<root>/config/gpn.toml`` gives ``root`` as the parent of
    ``config/``. A non-standard config location requires ``explicit_root``.
    """
    config_path = Path(config_path).expanduser().resolve()
    if explicit_root is not None:
        root = Path(explicit_root).expanduser().resolve()
        if not root.is_dir():
            raise InputError(
                "PROJECT_ROOT_NOT_A_DIRECTORY",
                f"project root does not exist or is not a directory: {root}",
                artifact_paths=[root],
            )
        return root
    parent = config_path.parent
    if parent.name == "config":
        root = parent.parent
        if root.is_dir():
            return root.resolve()
    raise InputError(
        "PROJECT_ROOT_AMBIGUOUS",
        "cannot infer project root from a non-standard config location; "
        "pass --project-root /path/to/project",
        artifact_paths=[config_path],
    )


_KNOWN_SECTIONS: dict[str, set[str]] = {
    "paths": {
        "ontology_dir", "ontology_file", "template", "fonts_dir",
        "references_dir", "runs_dir",
    },
    "model": {
        "base_url", "model_id", "temperature", "max_output_tokens",
        "context_budget_tokens", "timeout_seconds", "max_retries",
        "concurrency", "use_json_schema", "harness_wait_seconds",
    },
    "layout": {
        "css_px_per_in", "geometry_tolerance_pt", "text_safety_padding_pt",
        "max_local_layout_attempts", "candidate_count",
    },
    "policy": {
        "mode", "allow_split", "allow_cross_slide_move", "allow_text_rewrite",
        "allow_unsupported_raster", "require_native_data_objects",
        "max_model_repairs_per_slide",
    },
    "runtime": {"max_render_processes", "max_model_requests", "render_timeout_seconds", "offline"},
    "document": {
        "models_dir", "parser_device", "parser_workers", "ocr_mode", "ocr_languages",
        "content_policy", "target_slide_count", "allow_external_fetch",
        "allow_illustrative_data",
    },
}
_KNOWN_ROOT_KEYS = {"schema_version", "default_workflow", "allowed_remote_endpoints"}


def _flatten_overrides(overrides: dict[str, Scalar]) -> dict[str, dict[str, Any]]:
    flat: dict[str, dict[str, Any]] = {}
    for key, value in overrides.items():
        if "." in key:
            section, field_name = key.split(".", 1)
        else:
            section, field_name = ("_", key)
        known = section in _KNOWN_SECTIONS and field_name in _KNOWN_SECTIONS[section]
        if section != "_" and not known:
            raise SchemaError("UNKNOWN_CONFIG_OVERRIDE", f"unknown config override key: {key}")
        flat.setdefault(section, {})[field_name] = value
    return flat


def _validate_canonical_dirs(config: AppConfig, project_root: Path) -> None:
    if config.paths.ontology_dir.replace("\\", "/").strip("/") != CANONICAL_ONTOLOGY_DIR:
        raise SchemaError(
            "NON_CANONICAL_ONTOLOGY_DIR",
            f"paths.ontology_dir must be the canonical project directory "
            f"'{CANONICAL_ONTOLOGY_DIR}', got {config.paths.ontology_dir!r}",
        )
    if config.paths.references_dir.replace("\\", "/").strip("/") != CANONICAL_REFERENCES_DIR:
        raise SchemaError(
            "NON_CANONICAL_REFERENCES_DIR",
            f"paths.references_dir must be the canonical project directory "
            f"'{CANONICAL_REFERENCES_DIR}', got {config.paths.references_dir!r}",
        )
    # Canonical corpora must live directly under project_root.
    if config.ontology_dir.parent != project_root:
        raise SchemaError(
            "ONTOLOGY_DIR_OUTSIDE_PROJECT_ROOT",
            f"ontology dir resolves outside project root: {config.ontology_dir}",
        )
    if config.references_dir.parent != project_root:
        raise SchemaError(
            "REFERENCES_DIR_OUTSIDE_PROJECT_ROOT",
            f"references dir resolves outside project root: {config.references_dir}",
        )
    sel = config.selected_ontology_file
    if sel is not None:
        try:
            sel.relative_to(config.ontology_dir)
        except ValueError as exc:
            raise SchemaError(
                "ONTOLOGY_FILE_OUTSIDE_ONTOLOGY_DIR",
                f"paths.ontology_file must stay inside ontology_dir: {sel}",
            ) from exc


def load_config(
    path: Path,
    overrides: dict[str, Scalar] | None = None,
    *,
    project_root: Path | None = None,
) -> AppConfig:
    """Load and validate ``gpn.toml`` (spec §9.1). Read-only; no side effects.

    Args:
        path: path to the TOML config file.
        overrides: flat CLI overrides keyed ``section.field`` (no secrets).
        project_root: explicit project root; inferred for ``<root>/config/*.toml``.

    Returns:
        Immutable :class:`AppConfig` with absolute paths.
    """
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise InputError("CONFIG_NOT_FOUND", f"config file not found: {config_path}",
                         artifact_paths=[config_path])
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise SchemaError("CONFIG_TOML_INVALID", f"invalid TOML in {config_path}: {exc}",
                          artifact_paths=[config_path]) from exc

    root = resolve_project_root(config_path, project_root)

    version = str(raw.get("schema_version", ""))
    if version != "1.0":
        raise SchemaError(
            "CONFIG_SCHEMA_VERSION",
            f"unsupported schema_version {version!r}; expected '1.0'",
            artifact_paths=[config_path],
        )

    unknown_root = set(raw) - _KNOWN_ROOT_KEYS - set(_KNOWN_SECTIONS)
    if unknown_root:
        raise SchemaError(
            "UNKNOWN_CONFIG_KEY",
            f"unknown config keys: {', '.join(sorted(unknown_root))}",
            artifact_paths=[config_path],
        )
    for section, keys in raw.items():
        if section in _KNOWN_SECTIONS and isinstance(keys, dict):
            unknown = set(keys) - _KNOWN_SECTIONS[section]
            if unknown:
                raise SchemaError(
                    "UNKNOWN_CONFIG_KEY",
                    f"unknown keys in [{section}]: {', '.join(sorted(unknown))}",
                    artifact_paths=[config_path],
                )

    merged: dict[str, Any] = {k: v for k, v in raw.items() if k in _KNOWN_SECTIONS}
    for section, values in _flatten_overrides(overrides or {}).items():
        if section == "_":
            if "allowed_remote_endpoints" in values:
                merged["allowed_remote_endpoints"] = values["allowed_remote_endpoints"]
            for k, v in values.items():
                if k in _KNOWN_ROOT_KEYS:
                    merged[k] = v
        else:
            merged.setdefault(section, {}).update(values)

    try:
        config = AppConfig(**merged, project_root=root, config_path=config_path)
    except (ValueError, TypeError) as exc:
        raise SchemaError(
            "CONFIG_VALIDATION",
            f"config validation failed: {exc}",
            artifact_paths=[config_path],
        ) from exc

    _validate_canonical_dirs(config, root)
    return config
