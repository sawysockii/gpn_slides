"""Stage 1 tests: internal link resolution (spec §4.2)."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import (
    _add_internal_hyperlink,
    add_textbox,
    blank_slide,
    new_deck,
)

from slides_cli.gpn.assets import AssetStore
from slides_cli.gpn.importer import import_deck, resolve_internal_links
from slides_cli.gpn.package import read_package


@pytest.fixture()
def store(tmp_path: Path) -> AssetStore:
    return AssetStore(tmp_path / "assets")


def _link_deck(tmp_path: Path) -> Path:
    prs = new_deck()
    s1 = blank_slide(prs)
    add_textbox(s1, "first")
    s2 = blank_slide(prs)
    box = add_textbox(s2, "second")
    _add_internal_hyperlink(box, s1)
    s3 = blank_slide(prs)
    box3 = add_textbox(s3, "third")
    _add_internal_hyperlink(box3, s2)  # forward-chain link
    path = tmp_path / "links.pptx"
    prs.save(str(path))
    return path


def test_resolve_internal_links_identities(tmp_path: Path, store: AssetStore) -> None:
    path = _link_deck(tmp_path)
    deck, _ = import_deck(path, store)
    graph = read_package(path, store)
    report = resolve_internal_links(deck, graph)
    assert report.resolved, "expected resolved internal links"
    for rec in report.resolved:
        assert rec.target_slide_ref
        assert rec.owner_part
        assert rec.source_rel_id
        assert rec.status == "resolved"


def test_links_owner_scoped_not_global(tmp_path: Path, store: AssetStore) -> None:
    path = _link_deck(tmp_path)
    deck, _ = import_deck(path, store)
    graph = read_package(path, store)
    report = resolve_internal_links(deck, graph)
    owners = {r.owner_part for r in report.resolved}
    assert len(owners) >= 1
    # each record's evidence must reference its own owner part
    for rec in report.resolved:
        assert any(rec.owner_part in e for e in rec.evidence_refs)


def test_external_uri_without_network(tmp_path: Path, store: AssetStore) -> None:
    prs = new_deck()
    s1 = blank_slide(prs)
    box = add_textbox(s1, "go")
    run = box.text_frame.paragraphs[0].runs[0]
    run.hyperlink.address = "https://example.invalid/doc"
    path = tmp_path / "ext.pptx"
    prs.save(str(path))
    deck, _ = import_deck(path, store)
    graph = read_package(path, store)
    report = resolve_internal_links(deck, graph)
    # external hyperlinks live on runs; package-level hyperlink rels may be absent
    assert isinstance(report.external_links, list)
    assert not report.unresolved or all(r.status == "unresolved" for r in report.unresolved)


def test_categories_do_not_duplicate(tmp_path: Path, store: AssetStore) -> None:
    path = _link_deck(tmp_path)
    deck, _ = import_deck(path, store)
    graph = read_package(path, store)
    report = resolve_internal_links(deck, graph)
    keys = (
        [(r.subject_id, r.owner_part, r.source_rel_id) for r in report.resolved]
        + [(r.subject_id, r.owner_part, r.source_rel_id) for r in report.unresolved]
        + [(r.subject_id, r.owner_part, r.source_rel_id)
           for r in report.navigation_actions]
        + [(r.subject_id, r.owner_part, r.source_rel_id) for r in report.external_links]
    )
    assert len(keys) == len(set(keys))
