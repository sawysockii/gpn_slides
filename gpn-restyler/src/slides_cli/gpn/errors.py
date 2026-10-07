"""Typed GPN errors and process exit codes (spec §8.1)."""

from __future__ import annotations

from enum import IntEnum
from pathlib import Path
from typing import Any


class ExitCode(IntEnum):
    """CLI exit codes. Value is the process return code."""

    COMPLETED = 0
    INVALID_INPUT = 2
    NEEDS_ASSETS = 3
    NEEDS_REVIEW = 4
    INFEASIBLE = 5
    VALIDATION_FAILED = 6
    MODEL_UNAVAILABLE = 7
    RENDER_FAILED = 8
    CANCELLED = 130


STATUS_BY_EXIT: dict[int, str] = {
    ExitCode.COMPLETED: "completed",
    ExitCode.INVALID_INPUT: "invalid_input",
    ExitCode.NEEDS_ASSETS: "needs_assets",
    ExitCode.NEEDS_REVIEW: "needs_review",
    ExitCode.INFEASIBLE: "infeasible",
    ExitCode.VALIDATION_FAILED: "validation_failed",
    ExitCode.MODEL_UNAVAILABLE: "model_unavailable",
    ExitCode.RENDER_FAILED: "render_failed",
    ExitCode.CANCELLED: "cancelled",
}


class GpnError(Exception):
    """Base structured error for the GPN pipeline.

    Attributes:
        code: stable machine-readable error code.
        message: human-readable description without corporate document content.
        subject_ids: slide/object/claim IDs the error addresses.
        artifact_paths: local run artifacts useful for debugging.
        recoverable: whether a later stage may retry instead of aborting.
        exit_code: process exit code mapped to a run status.
    """

    default_exit_code: ExitCode = ExitCode.INVALID_INPUT
    default_recoverable: bool = False

    def __init__(
        self,
        code: str,
        message: str,
        *,
        subject_ids: list[str] | None = None,
        artifact_paths: list[Path] | None = None,
        recoverable: bool | None = None,
        exit_code: ExitCode | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.subject_ids = list(subject_ids or [])
        self.artifact_paths = list(artifact_paths or [])
        self.recoverable = self.default_recoverable if recoverable is None else recoverable
        self.exit_code = self.default_exit_code if exit_code is None else exit_code
        super().__init__(message)

    def __str__(self) -> str:
        parts = [f"[{self.code}] {self.message}"]
        if self.subject_ids:
            parts.append(f"subjects={','.join(self.subject_ids)}")
        if self.artifact_paths:
            parts.append(f"artifacts={','.join(str(p) for p in self.artifact_paths)}")
        return " ".join(parts)

    @property
    def status(self) -> str:
        return STATUS_BY_EXIT[int(self.exit_code)]

    def to_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "subject_ids": list(self.subject_ids),
            "artifact_paths": [str(p) for p in self.artifact_paths],
            "recoverable": self.recoverable,
            "status": self.status,
        }


class InputError(GpnError):
    default_exit_code = ExitCode.INVALID_INPUT


class AssetMissingError(GpnError):
    default_exit_code = ExitCode.NEEDS_ASSETS
    default_recoverable = True


class SchemaError(GpnError):
    default_exit_code = ExitCode.INVALID_INPUT


class SemanticConflictError(GpnError):
    default_exit_code = ExitCode.NEEDS_REVIEW


class LayoutInfeasibleError(GpnError):
    default_exit_code = ExitCode.INFEASIBLE
    default_recoverable = True


class UnsupportedObjectError(GpnError):
    default_exit_code = ExitCode.NEEDS_REVIEW


class RenderError(GpnError):
    default_exit_code = ExitCode.RENDER_FAILED


class ModelTransportError(GpnError):
    default_exit_code = ExitCode.MODEL_UNAVAILABLE


class PreservationError(GpnError):
    default_exit_code = ExitCode.VALIDATION_FAILED


# Stage 3.5 typed errors (spec §3.3). Additive; existing classes above are
# unchanged and remain the historical contract.
class OntologySourceError(GpnError):
    default_exit_code = ExitCode.INVALID_INPUT


class OntologySchemaError(GpnError):
    default_exit_code = ExitCode.INVALID_INPUT


class OntologyConflictError(GpnError):
    default_exit_code = ExitCode.NEEDS_REVIEW


class RuleBindingError(GpnError):
    default_exit_code = ExitCode.NEEDS_REVIEW


class ModelConfigurationError(GpnError):
    default_exit_code = ExitCode.INVALID_INPUT


class ModelUnavailableError(GpnError):
    default_exit_code = ExitCode.MODEL_UNAVAILABLE
    default_recoverable = True


class ModelProtocolError(GpnError):
    default_exit_code = ExitCode.MODEL_UNAVAILABLE


class ContextBudgetError(GpnError):
    default_exit_code = ExitCode.NEEDS_REVIEW


class PlannerValidationError(GpnError):
    default_exit_code = ExitCode.VALIDATION_FAILED


class UnsupportedIntentError(GpnError):
    default_exit_code = ExitCode.NEEDS_REVIEW
