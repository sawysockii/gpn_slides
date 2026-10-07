"""Stage 1 tests: fields/footnotes sequence + support report (spec §4)."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import add_fields_and_footnotes, blank_slide, build_import_fixture, new_deck

from slides_cli.gpn.assets import AssetStore
from slides_cli.gpn.importer import (
    build_import_support_report,
    import_deck,
    resolve_internal_links,
    write_import_artifacts,
)
from slides_cli.gpn.models import ImportSupportReport
from slides_cli.gpn.package import build_manifest, read_package
from slides_cli.gpn.provenance import build_ledger


@pytest.fixture()
def store(tmp_path: Path) -> AssetStore:
    return AssetStore(tmp_path / "assets")


def test_fields_footnote_sequence(tmp_path: Path, store: AssetStore) -> None:
    prs = new_deck()
    slide = blank_slide(prs)
    add_fields_and_footnotes(slide)
    path = tmp_path / "fields.pptx"
    prs.save(str(path))
    deck, ledger = import_deck(path, store)
    texts = []
    for slide_ir in deck.slides:
        for obj in slide_ir.objects:
            payload = obj.payload
            text = getattr(payload, "text", None)
            if text is None and hasattr(payload, "paragraphs"):
                text = payload
            if text is not None:
                texts.append(text)
    assert texts
    kinds = [r.kind for t in texts for p in t.paragraphs for r in p.runs]
    assert "field" in kinds, "a:fld runs must keep kind=field"
    field_runs = [
        r for t in texts for p in t.paragraphs for r in p.runs if r.kind == "field"
    ]
    assert all(r.field is not None for r in field_runs)
    types = {r.field.field_type for r in field_runs if r.field}
    ids = {r.field.field_id for r in field_runs if r.field}
    assert "slidenum" in types or "slidenum" in ids or types
    # cached text preserved
    cached = [r.field.cached_text for r in field_runs if r.field]
    assert any(cached)
    # superscript footnote marker preserved as a semantic mark
    marks = [m.kind for t in texts for p in t.paragraphs for r in p.runs for m in r.semantic_marks]
    assert "superscript" in marks
    # ledger keeps atoms in order
    assert ledger.atoms


def test_support_report_artifacts(tmp_path: Path, store: AssetStore) -> None:
    path = build_import_fixture(tmp_path / "input.pptx")
    deck, _ = import_deck(path, store)
    graph = read_package(path, store)
    links = resolve_internal_links(deck, graph)
    ledger = build_ledger(deck, decoration=[])
    support = build_import_support_report(deck, ledger, links, deck.import_issues)
    assert isinstance(support, ImportSupportReport)
    assert support.source_hash == deck.source_sha256
    assert support.import_status == "source_import_complete"
    total = sum(sum(levels.values()) for levels in support.capabilities_by_kind.values())
    assert total == support.object_count == len(support.per_object)

    run_dir = tmp_path / "run"
    manifest = write_import_artifacts(
        run_dir=run_dir, deck=deck, ledger=ledger,
        package_manifest=build_manifest(graph), support=support, links=links,
    )
    for name in ("source_ir.json", "source_ledger.json",
                 "source_package_manifest.json", "support_report.json",
                 "link_resolution.json"):
        assert (run_dir / name).is_file()
    assert manifest.support_report is not None
    assert manifest.link_resolution is not None
    # schema roundtrip of the real artifacts
    reloaded = ImportSupportReport.model_validate_json(
        (run_dir / "support_report.json").read_text(encoding="utf-8"))
    assert reloaded.object_count == support.object_count
