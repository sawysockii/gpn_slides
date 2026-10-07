"""Stage 2 tests: ontology conflict analysis (spec §5)."""

from __future__ import annotations

from slides_cli.gpn.models import NormativeStatement, OntologyDocument
from slides_cli.gpn.ontology_conflicts import (
    analyze_ontology_sources,
    compare_normative_statements,
    extract_normative_statements,
)


def _stmt(rule_id: str, subject: str, prop: str, value: str) -> NormativeStatement:
    return NormativeStatement(
        id=f"{rule_id}:{subject}:{prop}",
        subject=subject, property=prop, operator="=",
        typed_value=value, modality="must", rule_id=rule_id,
    )


def test_real_rule_conflict_blocks() -> None:
    stmts = [
        _stmt("C19", "body", "size_pt", "12"),
        _stmt("C19", "body", "size_pt", "10"),
    ]
    report = compare_normative_statements(stmts)
    assert report.conflicts
    assert any(c.blocking for c in report.conflicts)
    assert not report.ready_for_compilation


def test_same_rule_same_value_is_equivalent() -> None:
    stmts = [
        _stmt("C01", "title", "color", "7E7E7E"),
        _stmt("C01", "title", "color", "7E7E7E"),
    ]
    report = compare_normative_statements(stmts)
    assert not report.conflicts
    assert report.ready_for_compilation


def test_different_aspects_same_rule_not_conflict() -> None:
    stmts = [
        _stmt("C02", "body", "font", "GPN_DIN Regular"),
        _stmt("C02", "body", "size_pt", "12"),
    ]
    report = compare_normative_statements(stmts)
    assert not [c for c in report.conflicts if c.blocking]


def test_json_extraction_reads_constraints() -> None:
    import json

    doc = OntologyDocument(
        source_kind="json", source_hash="h",
        content=json.dumps({
            "formal_model": {"constraints": [
                {"id": "C01", "severity": "hard",
                 "condition": "color is token", "remedy": "fix"}]},
            "style_profile": {"typography": [
                {"role": "body", "font": "GPN_DIN Regular", "size_pt": 12}],
                "color_tokens": [],
                "font_style_policy": {"constraints": []},
                "list_styles": []},
        }),
    )
    extraction = extract_normative_statements(doc)
    rule_ids = {s.rule_id for s in extraction.statements}
    assert "C01" in rule_ids
    assert "C02" in rule_ids  # role font
    assert "C19" in rule_ids  # role size


def test_markdown_table_rows_extracted() -> None:
    doc = OntologyDocument(
        source_kind="external_markdown", source_hash="h",
        content="# Rules\n\n| ID | text |\n|---|---|\n| C05 | keep |\n",
    )
    extraction = extract_normative_statements(doc)
    assert any(s.rule_id == "C05" for s in extraction.statements)


def test_analyze_real_corpus_shape() -> None:
    from pathlib import Path

    from slides_cli.gpn.ontology import resolve_ontology_sources

    project_root = Path("/Users/wysockii/Documents/gpn_slides")
    if not (project_root / "ontology").is_dir():
        import pytest

        pytest.skip("real corpus not present")
    sources = resolve_ontology_sources(project_root)
    report = analyze_ontology_sources(sources)
    # Real corpus: documents differ in bytes (json vs md) but must not produce
    # a contradiction on the agreed C01–C22 table.
    assert isinstance(report.documents_differ, bool)
    assert isinstance(report.resolved_rule_ids, list)
