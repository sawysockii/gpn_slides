"""Stage 2 tests: template profile and derivation (spec §9)."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import add_textbox, blank_slide, new_deck

from slides_cli.gpn.assets import AssetStore, sha256_file
from slides_cli.gpn.compiler import compile_ontology
from slides_cli.gpn.models import RectEMU
from slides_cli.gpn.ontology import resolve_ontology_sources
from slides_cli.gpn.template import (
    compute_content_box,
    derive_base_template,
    extract_template_profile,
)

PROJECT_ROOT = Path("/Users/wysockii/Documents/gpn_slides")


@pytest.fixture()
def compiled():
    sources = resolve_ontology_sources(PROJECT_ROOT)
    assert sources.primary_json is not None
    return compile_ontology(Path(sources.primary_json), None, sources=sources)


@pytest.fixture()
def library(tmp_path: Path) -> Path:
    prs = new_deck()
    s1 = blank_slide(prs)
    add_textbox(s1, "Demo title one")
    s2 = blank_slide(prs)
    add_textbox(s2, "Demo title two")
    path = tmp_path / "library.pptx"
    prs.save(str(path))
    return path


def test_template_derivation_reopens(tmp_path: Path, library: Path, compiled) -> None:
    before = sha256_file(library)
    store = AssetStore(tmp_path / "assets")
    out = tmp_path / "profiles" / "base_template.pptx"
    report = derive_base_template(library, rules=compiled,
                                  output_path=out, store=store)
    assert out.is_file()
    assert sha256_file(library) == before, "source library must be unchanged"
    assert report.output_hash == sha256_file(out)
    assert report.source_hash == before
    # shared parts survive derivation
    checks = " ".join(report.relationship_checks)
    assert "slideMasters:present" in checks
    assert "slideLayouts:present" in checks
    # reopen explicitly
    from pptx import Presentation

    reopened = Presentation(str(out))
    assert (reopened.slide_width, reopened.slide_height) == (12192000, 6858000)


def test_extract_template_profile_structure(tmp_path: Path, library: Path,
                                            compiled) -> None:
    store = AssetStore(tmp_path / "assets")
    profile = extract_template_profile(library, compiled, store)
    assert profile.source_library_hash == sha256_file(library)
    assert (profile.canvas.w, profile.canvas.h) == (12192000, 6858000)
    assert profile.layout_profiles
    assert profile.master_parts or profile.theme_parts
    assert profile.structure_verified


def test_compute_content_box_unverified_without_evidence() -> None:
    canvas = RectEMU(x=0, y=0, w=12192000, h=6858000)
    title = RectEMU(x=0, y=0, w=12192000, h=1500000)
    footer = RectEMU(x=0, y=6000000, w=12192000, h=858000)
    res = compute_content_box(canvas, [title, footer], None)
    assert res.content_box is not None
    assert res.content_box.y >= 1500000
    assert res.content_box.y + res.content_box.h <= 6000000
    assert res.verified is False  # no layout evidence supplied


def test_corpus_hash_invalidation(tmp_path: Path) -> None:
    from slides_cli.gpn.models import CorpusFileRecord
    from slides_cli.gpn.ontology import canonical_corpus_hash

    rec = CorpusFileRecord(relative_path="ontology/a.md", kind="markdown",
                           sha256="0" * 64, byte_count=10, normative=True)
    h1 = canonical_corpus_hash([rec])
    rec2 = CorpusFileRecord(relative_path="ontology/a.md", kind="markdown",
                            sha256="1" * 64, byte_count=11, normative=True)
    assert canonical_corpus_hash([rec2]) != h1


def test_ambiguous_ontology_selection(tmp_path: Path) -> None:
    import json

    root = tmp_path / "proj" / "ontology"
    root.mkdir(parents=True)
    meta = {"name": "Онтология оформления деловых презентаций по библиотеке ГПН",
            "version": "9.9.9", "date": "2026-01-01"}
    (root / "a.json").write_text(json.dumps({"metadata": meta}), encoding="utf-8")
    meta2 = dict(meta, version="9.9.8")
    (root / "b.json").write_text(json.dumps({"metadata": meta2}), encoding="utf-8")
    from slides_cli.gpn.ontology import resolve_ontology_sources

    sources = resolve_ontology_sources(tmp_path / "proj")
    assert sources.ready is False
    assert any("AMBIGUOUS" in i.code for i in sources.issues)
    assert len(sources.candidates) == 2
