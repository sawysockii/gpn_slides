"""Stage 0 tests: project root, corpus resolution, manifests (spec §3.4, §21.3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from slides_cli.gpn.errors import AssetMissingError, InputError
from slides_cli.gpn.ontology import (
    canonical_corpus_hash,
    discover_slide_examples,
    resolve_ontology_sources,
    sha256_file,
    write_corpus_manifests,
)

META_NAME = "Онтология оформления деловых презентаций по библиотеке ГПН"


def _make_project(tmp: Path, *, with_examples: bool = True) -> Path:
    (tmp / "ontology").mkdir(parents=True)
    payload = {
        "metadata": {"name": META_NAME, "version": "1.2.0", "date": "2026-09-21"},
        "formal_model": {},
        "style_profile": {},
        "catalogs": {},
        "catalogue_rules": {},
    }
    (tmp / "ontology" / "GPN_Slide_Design_Ontology.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    (tmp / "ontology" / "rules.md").write_text("# Правила\n", encoding="utf-8")
    if with_examples:
        (tmp / "slide_examples").mkdir()
        (tmp / "slide_examples" / "lib.pptx").write_bytes(b"PK\x03\x04fake")
    return tmp


def test_project_corpus_paths_from_foreign_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _make_project(tmp_path / "proj")
    foreign = tmp_path / "elsewhere"
    foreign.mkdir()
    monkeypatch.chdir(foreign)

    sources = resolve_ontology_sources(project)
    discovery = discover_slide_examples(project)

    assert sources.root == project / "ontology"
    assert not sources.root.is_absolute() or str(sources.root).startswith(str(project))
    assert discovery.root == project / "slide_examples"
    assert all(r.relative_path.startswith("ontology/") for r in sources.normative_files)
    assert all(r.relative_path.startswith("slide_examples/") for r in discovery.files)


def test_missing_ontology_corpus_raises_needs_assets(tmp_path: Path) -> None:
    with pytest.raises(AssetMissingError) as exc:
        resolve_ontology_sources(tmp_path / "empty")
    assert exc.value.status == "needs_assets"


def test_missing_slide_examples_marks_not_ready(tmp_path: Path) -> None:
    project = _make_project(tmp_path / "proj", with_examples=False)
    report = discover_slide_examples(project)
    assert report.ready is False
    assert any(i.code == "SLIDE_EXAMPLES_CORPUS_MISSING" for i in report.issues)
    assert report.examples_corpus_hash == canonical_corpus_hash([])


def test_primary_json_selected_and_hash_matches(tmp_path: Path) -> None:
    project = _make_project(tmp_path / "proj")
    sources = resolve_ontology_sources(project)
    assert sources.ready is True
    assert sources.primary_json == project / "ontology" / "GPN_Slide_Design_Ontology.json"
    assert sources.ontology_hash == sha256_file(sources.primary_json)  # type: ignore[arg-type]
    assert sources.selected_version == "1.2.0"
    assert len(sources.ontology_corpus_hash) == 64


def test_real_project_ontology_matches_checked_version() -> None:
    root = Path(__file__).resolve().parents[3]
    if not (root / "ontology" / "GPN_Slide_Design_Ontology.json").exists():
        pytest.skip("real project corpus not present")
    sources = resolve_ontology_sources(root)
    assert sources.ready, [i.model_dump() for i in sources.issues]
    assert sources.ontology_hash == (
        "d24be59d5b52f67f1edf28b15722615ac01488c6553a0f17ffe0278fda6b7a90"
    )
    assert sources.selected_version == "1.2.0"
    discovery = discover_slide_examples(root)
    assert discovery.ready
    assert discovery.examples_corpus_hash == canonical_corpus_hash(discovery.files)


def test_markdown_change_invalidates_corpus_hash(tmp_path: Path) -> None:
    project = _make_project(tmp_path / "proj")
    before = resolve_ontology_sources(project).ontology_corpus_hash
    md = project / "ontology" / "rules.md"
    md.write_text("# Правила (изменено)\n", encoding="utf-8")
    after = resolve_ontology_sources(project).ontology_corpus_hash
    assert before != after


def test_ambiguous_selection_returns_needs_review(tmp_path: Path) -> None:
    project = _make_project(tmp_path / "proj")
    other = {
        "metadata": {"name": META_NAME, "version": "2.0.0", "date": "2026-10-01"},
    }
    (project / "ontology" / "other.json").write_text(
        json.dumps(other, ensure_ascii=False), encoding="utf-8"
    )
    sources = resolve_ontology_sources(project)
    assert sources.ready is False
    assert sources.primary_json is None
    assert any(i.code == "ONTOLOGY_SELECTION_AMBIGUOUS" for i in sources.issues)


def test_selected_file_outside_corpus_rejected(tmp_path: Path) -> None:
    project = _make_project(tmp_path / "proj")
    outside = tmp_path / "evil.json"
    outside.write_text("{}", encoding="utf-8")
    with pytest.raises(InputError):
        resolve_ontology_sources(project, selected_file=outside)


def test_broken_json_marks_not_ready(tmp_path: Path) -> None:
    project = _make_project(tmp_path / "proj")
    (project / "ontology" / "broken.md").write_bytes(b"\xff\xfe\x00bad")
    sources = resolve_ontology_sources(project)
    assert sources.ready is False
    assert any(i.code == "CORPUS_NORMATIVE_FILE_BROKEN" for i in sources.issues)


def test_manifests_written_outside_corpus(tmp_path: Path) -> None:
    project = _make_project(tmp_path / "proj")
    sources = resolve_ontology_sources(project)
    discovery = discover_slide_examples(project)
    cache = tmp_path / "run-cache"
    written = write_corpus_manifests(sources, discovery, cache)
    expected = {"ontology_manifest.json", "examples_manifest.json", "ontology_conflicts.json"}
    assert set(written) == expected
    manifest = json.loads((cache / "ontology_manifest.json").read_text(encoding="utf-8"))
    assert manifest["ontology_corpus_hash"] == sources.ontology_corpus_hash
    # originals untouched
    rules = project / "ontology" / "rules.md"
    assert sha256_file(rules) == sha256_file(rules)
    assert not list((project / "ontology").glob("*.tmp"))


def test_manifest_deterministic(tmp_path: Path) -> None:
    project = _make_project(tmp_path / "proj")
    sources = resolve_ontology_sources(project)
    assert sources.ontology_corpus_hash == resolve_ontology_sources(project).ontology_corpus_hash
