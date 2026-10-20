"""Locate a source slide image by slide identity, never lexicographic order."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def source_slide_images(run: Path, slide_id: str) -> list[str]:
    source = run / "source_ir.json"
    if not source.is_file():
        return []
    deck = json.loads(source.read_text(encoding="utf-8"))
    matches = [i for i, s in enumerate(deck.get("slides", [])) if s.get("id") == slide_id]
    if len(matches) != 1:
        raise ValueError(f"SOURCE_IMAGE_SLIDE_IDENTITY:{slide_id}")
    index = matches[0]
    root = run / "source_render"
    probe_path = root / "source_render_probe.json"
    if probe_path.is_file():
        probe = json.loads(probe_path.read_text(encoding="utf-8"))
        actual = probe.get("candidate_hash") or probe.get("candidate_sha256")
        expected = deck.get("source_sha256")
        if actual and expected and actual != expected:
            raise ValueError("SOURCE_RENDER_STALE: source bytes do not match the image probe")
        pages = probe.get("page_images", [])
        if len(pages) == len(deck.get("slides", [])):
            path = Path(pages[index])
            if not path.is_absolute():
                path = root / path
            if path.is_file():
                return [str(path.resolve())]
    # pdftoppm uses zero padding only when the page count needs it.
    paths = [p for p in root.glob("slide-*.png") if p.stem[6:].isdigit()]
    candidates = [p for p in paths if int(p.stem[6:]) == index + 1]
    if len({hashlib.sha256(p.read_bytes()).hexdigest() for p in candidates}) > 1:
        raise ValueError(f"SOURCE_IMAGE_AMBIGUOUS:{slide_id}")
    return [str(sorted(candidates)[0].resolve())] if candidates else []
