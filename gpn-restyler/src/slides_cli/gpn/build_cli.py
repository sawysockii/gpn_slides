"""CLI handler for ``slides gpn build`` (Stage 4 §12).

Service operation: builds a new native deck from accepted plans. Stdout is
one JSON contract; diagnostics go to stderr.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _progress(msg: str) -> None:
    print(msg, file=sys.stderr)


def cmd_build(args: Any, config: Any, project_root: Path) -> int:
    from .errors import ExitCode, GpnError
    from .export_models import ExportOptions
    from .export_pipeline import resolve_llm_provider, run_native_build

    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = (project_root / run_dir).resolve()
    plans_dir = Path(args.plans_dir)
    if not plans_dir.is_absolute():
        plans_dir = (project_root / plans_dir).resolve()

    override = getattr(args, "llm_provider", None)
    if override is not None and hasattr(config, "model"):
        try:
            object.__setattr__  # noqa: B018 - immutable config check below
            config = config.model_copy(
                update={"model": config.model.model_copy(
                    update={"provider": override})})
        except AttributeError:
            pass

    options = ExportOptions(
        workflow=getattr(args, "workflow", "presentation"),
        diagnostic=True,
        overwrite=bool(getattr(args, "overwrite", False)),
    )
    _progress(f"[gpn build] run_dir={run_dir} plans_dir={plans_dir}")
    try:
        provider, _ = resolve_llm_provider(config)
    except GpnError as exc:
        _emit({"ok": False, "error": exc.to_json()})
        return int(exc.exit_code)
    try:
        result = run_native_build(run_dir, plans_dir, config, options)
    except GpnError as exc:
        _emit({"ok": False, "error": exc.to_json()})
        return int(exc.exit_code)
    _emit({"ok": result.operation_status in ("completed",),
           "command": "build", **result.model_dump(mode="json")})
    mapping = {"completed": ExitCode.COMPLETED,
               "needs_assets": ExitCode.NEEDS_ASSETS,
               "needs_review": ExitCode.NEEDS_REVIEW,
               "invalid_input": ExitCode.INVALID_INPUT,
               "failed": ExitCode.VALIDATION_FAILED}
    return int(mapping.get(result.operation_status, ExitCode.VALIDATION_FAILED))
