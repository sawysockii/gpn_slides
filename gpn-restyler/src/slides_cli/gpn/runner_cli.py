"""Stage 7 §11: public restyle/create/resume/status/bundle routes.

Structured stdout (command/run id/status/pending tasks/candidates/output);
progress on stderr. Exit 0 only for the accepted user result; awaiting
harness is a distinct machine state (exit 4), never a terminal failure.
Preserved exits: invalid_input=2, assets/review/validation=3, pending=4.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_BLOCKED = 3
EXIT_PENDING = 4


def _progress(msg: str) -> None:
    print(msg, file=sys.stderr)


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _load_responses(paths: list[Path] | None) -> list:
    from .models import ModelTaskResponse

    out = []
    for path in paths or []:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        items = data if isinstance(data, list) else [data]
        out.extend(ModelTaskResponse.model_validate(item) for item in items)
    return out


def _summarize(result, run_dir: Path) -> dict[str, Any]:
    return {
        "run_dir": str(run_dir),
        "status": result.status,
        "pending_tasks": [t.model_dump(mode="json")
                          for t in result.pending_tasks],
        "actions_executed": [a.model_dump(mode="json")
                             for a in result.actions_executed],
        "current_candidates": [c.model_dump(mode="json")
                               for c in result.current_candidates],
        "accepted_product": result.accepted_product.model_dump(mode="json")
        if result.accepted_product else None,
        "findings": [f.model_dump(mode="json") for f in result.findings],
    }


def _exit_for(status: str) -> int:
    return {"accepted": EXIT_OK, "awaiting_harness": EXIT_PENDING,
            "running": EXIT_PENDING}.get(status, EXIT_BLOCKED)


def _provider(args: argparse.Namespace) -> str | None:
    provider = getattr(args, "llm_provider", "harness") or "harness"
    if provider == "local_server":
        _emit({"deferred": True,
               "reason": "local_server adapter not implemented; "
                         "use --llm-provider harness"})
        return None
    return provider


def _run_dir_for_start(project_root: Path,
                       explicit: Path | None) -> Path | None:
    if explicit is None:
        return None
    p = explicit if explicit.is_absolute() else project_root / explicit
    return p


def cmd_restyle(args: argparse.Namespace, config: Any,
                project_root: Path) -> int:
    from .document_ingest import detect_kind  # noqa: PLC0415
    from .models import InputSpec, ProductScope  # noqa: PLC0415
    from .restyle_runner import advance_run, start_run  # noqa: PLC0415

    if _provider(args) is None:
        return EXIT_BLOCKED
    source = args.source if Path(args.source).is_absolute() \
        else project_root / args.source
    if not source.is_file():
        _emit({"error": f"input not found: {source}"})
        return EXIT_INVALID
    try:
        kind = detect_kind(source)
    except ValueError as exc:
        _emit({"error": str(exc)})
        return EXIT_INVALID
    if kind.value not in ("pptx", "ppt"):
        _emit({"error": f"restyle needs PPTX/PPT, got {kind.value}; "
                        f"use create for documents"})
        return EXIT_INVALID
    output = args.output if Path(args.output).is_absolute() \
        else project_root / args.output
    spec = InputSpec(kind=kind, path=source, project_root=project_root,
                     output_path=output,
                     requested_scope=ProductScope.RESTYLE,
                     overwrite=bool(args.overwrite))
    try:
        state = start_run(spec, config,
                          run_dir=_run_dir_for_start(
                              project_root, args.run_dir))
    except ValueError as exc:
        _emit({"error": str(exc)})
        return EXIT_BLOCKED
    _progress(f"run {state.run_id} started at {state.run_dir}")
    result = advance_run(state, _load_responses(args.responses))
    summary = _summarize(result, Path(state.run_dir))
    _emit(summary)
    return _exit_for(result.status)


def cmd_create(args: argparse.Namespace, config: Any,
               project_root: Path) -> int:
    from .document_ingest import detect_kind  # noqa: PLC0415
    from .models import InputKind, InputSpec, ProductScope  # noqa: PLC0415
    from .restyle_runner import advance_run, start_run  # noqa: PLC0415

    if _provider(args) is None:
        return EXIT_BLOCKED
    prompt_text: str | None = args.prompt
    if args.prompt_file is not None:
        prompt_text = Path(args.prompt_file).read_text(encoding="utf-8")
    path = None
    kind = InputKind.PROMPT
    if args.source is not None:
        source = args.source if Path(args.source).is_absolute() \
            else project_root / args.source
        if not source.is_file():
            _emit({"error": f"input not found: {source}"})
            return EXIT_INVALID
        try:
            kind = detect_kind(source)
        except ValueError as exc:
            _emit({"error": str(exc)})
            return EXIT_INVALID
        if kind in (InputKind.PPTX, InputKind.PPT):
            _emit({"error": "create needs a document/prompt; "
                            "use restyle for presentations"})
            return EXIT_INVALID
        path = source
    elif not (prompt_text or "").strip():
        _emit({"error": "create needs a source file, --prompt or "
                        "--prompt-file"})
        return EXIT_INVALID
    output = args.output if Path(args.output).is_absolute() \
        else project_root / args.output
    try:
        spec = InputSpec(kind=kind, path=path, prompt_text=prompt_text,
                         project_root=project_root, output_path=output,
                         requested_slide_count=args.slides,
                         requested_scope=ProductScope.CREATE,
                         overwrite=bool(args.overwrite))
    except ValueError as exc:
        _emit({"error": str(exc)})
        return EXIT_INVALID
    try:
        state = start_run(spec, config,
                          run_dir=_run_dir_for_start(
                              project_root, args.run_dir))
    except ValueError as exc:
        _emit({"error": str(exc)})
        return EXIT_BLOCKED
    _progress(f"run {state.run_id} started at {state.run_dir}")
    result = advance_run(state, _load_responses(args.responses))
    _emit(_summarize(result, Path(state.run_dir)))
    return _exit_for(result.status)


def cmd_resume(args: argparse.Namespace, config: Any,
               project_root: Path) -> int:
    from .restyle_runner import resume_run  # noqa: PLC0415

    run_dir = args.run_dir if Path(args.run_dir).is_absolute() \
        else project_root / args.run_dir
    if not (run_dir / "run_state.json").is_file():
        _emit({"error": f"no durable state at {run_dir}"})
        return EXIT_INVALID
    result = resume_run(run_dir, config,
                        responses=_load_responses(args.responses))
    _emit(_summarize(result, run_dir))
    return _exit_for(result.status)


def cmd_status(args: argparse.Namespace, config: Any,
               project_root: Path) -> int:
    from .restyle_runner import run_status  # noqa: PLC0415

    run_dir = args.run_dir if Path(args.run_dir).is_absolute() \
        else project_root / args.run_dir
    if not (run_dir / "run_state.json").is_file():
        _emit({"error": f"no durable state at {run_dir}"})
        return EXIT_INVALID
    _emit(run_status(run_dir))
    return EXIT_OK


def cmd_bundle(args: argparse.Namespace, config: Any,
               project_root: Path) -> int:
    from .review_bundle import (  # noqa: PLC0415
        build_review_bundle,
        collect_code_evidence,
    )

    run_dir = args.run_dir if Path(args.run_dir).is_absolute() \
        else project_root / args.run_dir
    output = args.output if Path(args.output).is_absolute() \
        else project_root / args.output
    try:
        collect_code_evidence(run_dir, project_root)
        report = build_review_bundle(run_dir, output,
                                     project_root=project_root)
    except ValueError as exc:
        _emit({"error": str(exc)})
        return EXIT_BLOCKED
    _emit(report.model_dump(mode="json"))
    return EXIT_OK if report.bundle_complete else EXIT_BLOCKED
