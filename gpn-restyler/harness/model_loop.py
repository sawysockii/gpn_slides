#!/usr/bin/env python3
"""Compatibility entry point for work orders served by the current model.

No archival authors, fake model identities or response re-signing.
"""

from __future__ import annotations

import argparse
import runpy
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument(
        "--max-steps",
        type=int,
        default=40,
        help="Compatibility only; the current model serves each work order.",
    )
    args = parser.parse_args()
    tool = Path(__file__).resolve().parents[1] / "tools/harness_batch.py"
    sys.argv = [str(tool), "--batch-root", str(args.run_dir.resolve()), "next"]
    runpy.run_path(str(tool), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
