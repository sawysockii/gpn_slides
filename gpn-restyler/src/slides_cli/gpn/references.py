"""Minimal grounded example retrieval for planning (Stage 3.5 §6.3).

Builds lightweight descriptors from the real ``slide_examples`` corpus and
retrieves a few relevant ones for a slide view. This is NOT the Stage 6
index: no vector/semantic search, no full validation of examples.

Example style is nonnormative evidence: a descriptor records that the
example's own ontology conformance is unverified, so its styling is never
transferred as permission. Only the semantic composition idea may be used.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DESCRIPTOR_FILE = "examples_descriptors.json"
MAX_DESCRIPTORS = 500


def build_example_descriptors(
    project_root: Path,
    run_dir: Path,
    *,
    force: bool = False,
) -> list[dict[str, Any]]:
    """Import slide_examples once and cache per-slide descriptors in run_dir."""
    run_dir = Path(run_dir)
    cache_path = run_dir / DESCRIPTOR_FILE
    if cache_path.is_file() and not force:
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("examples descriptor cache unreadable; rebuilding")

    from .assets import AssetStore
    from .importer import import_deck
    from .ontology import discover_slide_examples

    discovery = discover_slide_examples(Path(project_root))
    descriptors: list[dict[str, Any]] = []
    if not discovery.files:
        return _store(cache_path, descriptors)

    store = AssetStore(run_dir / "examples_assets")
    for record in discovery.files[:5]:
        file_path = Path(project_root) / record.relative_path
        if not file_path.is_file():
            continue
        try:
            deck, _ = import_deck(file_path, store)
        except Exception as exc:  # noqa: BLE001 — a broken example must not abort planning
            log.warning("example import failed for %s: %s", file_path, exc)
            continue
        for slide in deck.slides:
            kinds: dict[str, int] = {}
            texts: list[str] = []
            for obj in slide.objects:
                kinds[obj.kind] = kinds.get(obj.kind, 0) + 1
                paras = getattr(obj.payload, "paragraphs", None)
                for para in list(paras or [])[:2]:
                    line = "".join(run.text for run in para.runs).strip()
                    if line:
                        texts.append(line[:120])
            descriptors.append({
                "example_id": f"{file_path.name}:{slide.id}",
                "source_path": str(file_path),
                "slide_id": slide.id,
                "object_kinds": kinds,
                "title_snippet": texts[0] if texts else "",
                "text_snippets": texts[:5],
                "layout_descriptor": {
                    "object_count": len(slide.objects),
                    "kinds": sorted(kinds),
                },
                "communication_job": "reference_example",
                "style_eligibility": "nonnormative_evidence",
                "checked_scopes": [],
                "violations": [],
                "unverified_scopes": ["style", "semantic", "render"],
                "note": (
                    "example conformance unverified; style must not be "
                    "transferred as permission"
                ),
            })
            if len(descriptors) >= MAX_DESCRIPTORS:
                break
        if len(descriptors) >= MAX_DESCRIPTORS:
            break
    return _store(cache_path, descriptors)


def retrieve_planning_examples(
    view: Any,
    descriptors: list[dict[str, Any]],
    *,
    k: int = 2,
) -> list[dict[str, Any]]:
    """Pick up to k examples by object-kind overlap and title keywords."""
    view_kinds: set[str] = set()
    if view is not None:
        for obj in getattr(view, "objects", []) or []:
            kind = obj.get("kind", "")
            if kind:
                view_kinds.add(str(kind))
    title_words = _keywords(view)

    scored: list[tuple[float, dict[str, Any]]] = []
    for desc in descriptors:
        kinds = set((desc.get("object_kinds") or {}).keys())
        kind_overlap = len(view_kinds & kinds) / max(len(view_kinds | kinds), 1)
        desc_words = set()
        for snippet in desc.get("text_snippets") or []:
            desc_words.update(_words(snippet))
        word_overlap = len(title_words & desc_words) / max(len(title_words | desc_words), 1)
        score = 0.7 * kind_overlap + 0.3 * word_overlap
        scored.append((score, desc))
    scored.sort(key=lambda pair: pair[0], reverse=True)

    picked = [desc for score, desc in scored[:k] if score > 0]
    return picked or [desc for _, desc in scored[:k]]


def _keywords(view: Any) -> set[str]:
    words: set[str] = set()
    if view is None:
        return words
    for obj in getattr(view, "objects", []) or []:
        for line in obj.get("text", []) or []:
            words.update(_words(line))
    return words


def _words(text: str) -> set[str]:
    return {
        token.lower()
        for token in "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in text).split()
        if len(token) >= 4
    }


def _store(path: Path, descriptors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(descriptors, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return descriptors
