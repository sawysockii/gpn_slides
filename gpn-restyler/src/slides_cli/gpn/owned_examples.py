"""Supply only user-owned examples, with cached native input renders.

Reference appearance is evidence, never permission to override the JSON.
Every read is guarded against the current allowlist. A content-addressed
cache is shared by cases, outside both immutable input corpora.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any


def select_owned_examples(project: Path, slide: Any, *, count: int = 2) -> list[dict]:
    from .ontology import discover_slide_examples
    from .reference_guard import assert_allowed_reference
    from .references import build_example_descriptors, retrieve_planning_examples

    project = Path(project)
    discovery = discover_slide_examples(project)
    if not discovery.ready:
        return []
    cache = project / "runs/reference_cache" / discovery.examples_corpus_hash
    cache.mkdir(parents=True, exist_ok=True)
    # The cache key changes when ANY allowed corpus byte changes.
    with (cache / ".descriptor.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        descriptors = build_example_descriptors(project, cache)
    units = getattr(slide, "units", [])
    view = SimpleNamespace(
        objects=[
            {
                "kind": "text" if u.kind not in ("image", "table", "chart") else u.kind,
                "text": [u.content.text],
            }
            for u in units
        ]
    )
    selected = retrieve_planning_examples(view, descriptors, k=count)
    result = []
    for descriptor in selected:
        proof = assert_allowed_reference(
            descriptor["source_path"], "design_model_reference", project, discovery
        )
        result.append(
            {
                **descriptor,
                "source_sha256": proof.sha256,
                "source_relative_path": proof.relative_path,
            }
        )
    return result


def render_owned_examples(
    project: Path, examples: list[dict], *, font_inventory: Path | None = None
) -> dict:
    """Resolve selected pages to real PNGs; never search outside slide_examples."""
    from .ontology import discover_slide_examples
    from .reference_guard import assert_allowed_reference
    from .render import find_soffice, render_source_deck, soffice_version

    project = Path(project)
    discovery = discover_slide_examples(project)
    binary = find_soffice()
    environment = hashlib.sha256(
        (Path(__file__).parent / "render.py").read_bytes()
        + (soffice_version(binary) if binary else "unavailable").encode()
        + (font_inventory.read_bytes() if font_inventory and font_inventory.is_file() else b"")
    ).hexdigest()
    images, records, issues = [], [], []
    for example in examples:
        proof = assert_allowed_reference(
            example["source_path"],
            "design_reference_render",
            project,
            discovery,
            expected_sha256=example["source_sha256"],
        )
        source = Path(proof.canonical_path)
        match = re.match(r"^slide(\d+):", example["slide_id"])
        if source.suffix.lower() != ".pptx" or not match:
            issues.append(f"OWNED_REFERENCE_PAGE_UNRESOLVED:{example['example_id']}")
            continue
        index = int(match[1])
        folder = project / "runs/reference_cache" / proof.sha256 / environment
        folder.mkdir(parents=True, exist_ok=True)
        manifest_path = folder / "reference_render.json"
        with (folder / ".render.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
            valid = (
                manifest.get("source_relative_path") == proof.relative_path
                and manifest.get("source_sha256") == proof.sha256
                and bool(manifest.get("pages"))
            ) and all(
                Path(p["path"]).resolve().is_relative_to(folder.resolve())
                and Path(p["path"]).is_file()
                and hashlib.sha256(Path(p["path"]).read_bytes()).hexdigest() == p["sha256"]
                for p in manifest.get("pages", [])
            )
            if not valid:
                # Read the actual OPC slide list; hidden slides count too.
                from lxml import etree

                with zipfile.ZipFile(source) as package:
                    xml = etree.fromstring(package.read("ppt/presentation.xml"))
                total = len(
                    xml.findall(
                        "{http://schemas.openxmlformats.org/presentationml/2006/main}sldIdLst/*"
                    )
                )
                probe = render_source_deck(source, folder, expected_page_count=total)
                if not probe.rendered or len(probe.page_images) != total:
                    issues.append(f"OWNED_REFERENCE_RENDER_FAILED:{example['example_id']}")
                    continue
                manifest = {
                    "source_relative_path": proof.relative_path,
                    "source_sha256": proof.sha256,
                    "pages": [
                        {
                            "path": str(Path(p).resolve()),
                            "sha256": hashlib.sha256(Path(p).read_bytes()).hexdigest(),
                        }
                        for p in probe.page_images
                    ],
                }
                temporary = manifest_path.with_suffix(".tmp")
                temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
                temporary.replace(manifest_path)
        if index >= len(manifest.get("pages", [])):
            issues.append(f"OWNED_REFERENCE_PAGE_UNRESOLVED:{example['example_id']}")
            continue
        page = manifest["pages"][index]
        images.append(page["path"])
        records.append(
            {
                "example_id": example["example_id"],
                "slide_id": example["slide_id"],
                "source_relative_path": proof.relative_path,
                "source_sha256": proof.sha256,
                "image": page,
                "style_authority": "ontology JSON only; example is nonnormative",
            }
        )
    return {"references": records, "required_images": list(dict.fromkeys(images)), "issues": issues}
