"""Stage 2 tests: rule registry and evaluation (spec §7)."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import add_textbox, blank_slide, new_deck

from slides_cli.gpn.assets import AssetStore
from slides_cli.gpn.compiler import compile_ontology
from slides_cli.gpn.importer import import_deck
from slides_cli.gpn.ontology import resolve_ontology_sources
from slides_cli.gpn.provenance import build_ledger
from slides_cli.gpn.rules import (
    RuleEvaluationContext,
    aggregate_rule_readiness,
    evaluate_rule,
    evaluate_rule_registry,
)

PROJECT_ROOT = Path("/Users/wysockii/Documents/gpn_slides")


@pytest.fixture()
def compiled():
    sources = resolve_ontology_sources(PROJECT_ROOT)
    assert sources.primary_json is not None
    return compile_ontology(Path(sources.primary_json), None, sources=sources)


def test_all_rule_ids_registered(compiled) -> None:
    ids = set(compiled.rule_registry.specs)
    assert {f"C{i:02d}" for i in range(1, 23)} <= ids
    assert {"R01", "R02", "R03", "R04"} <= ids
    assert "C04" in ids and "C20" in ids  # must not be skipped


def test_unknown_rule_evidence_blocks_strict(compiled) -> None:
    ctx = RuleEvaluationContext(subject_kind="source", compiled_rules=compiled)
    coverage = evaluate_rule_registry(compiled.rule_registry, ctx)
    assert len(coverage) == 26
    report = aggregate_rule_readiness(coverage)
    assert report.strict_output_ready is False
    assert report.blocking_unknowns  # deferred hard rules block strict


def test_unknown_rule_id_is_blocking_unknown(compiled) -> None:
    ctx = RuleEvaluationContext(subject_kind="source", compiled_rules=compiled)
    cov = evaluate_rule("C99", ctx)
    assert cov.result == "unknown"
    assert cov.blocking is True


def test_exact_face_inherited_bold_fails(tmp_path: Path, compiled) -> None:
    prs = new_deck()
    slide = blank_slide(prs)
    box = add_textbox(slide, "Bold body text")
    run = box.text_frame.paragraphs[0].runs[0]
    run.font.name = "GPN_DIN Regular"
    run.font.bold = True
    path = tmp_path / "bold.pptx"
    prs.save(str(path))
    store = AssetStore(tmp_path / "assets")
    deck, _ = import_deck(path, store)
    ledger = build_ledger(deck, decoration=[])
    ctx = RuleEvaluationContext(subject_kind="source", source_ir=deck,
                                ledger=ledger, compiled_rules=compiled)
    c21 = evaluate_rule("C21", ctx)
    assert c21.result == "fail"
    c22 = evaluate_rule("C22", ctx)
    assert c22.result == "fail"


def test_conditional_colors_need_evidence(compiled) -> None:
    assert "sky" in compiled.conditional_colors
    cond = compiled.conditional_colors["sky"]
    assert cond.required_evidence_refs  # no blanket allowed=true


def test_canvas_check_passes_on_fixture(tmp_path: Path, compiled) -> None:
    prs = new_deck()
    blank_slide(prs)
    path = tmp_path / "canvas.pptx"
    prs.save(str(path))
    store = AssetStore(tmp_path / "assets")
    deck, _ = import_deck(path, store)
    ledger = build_ledger(deck, decoration=[])
    ctx = RuleEvaluationContext(subject_kind="source", source_ir=deck,
                                ledger=ledger, compiled_rules=compiled)
    assert evaluate_rule("C16", ctx).result == "pass"
    assert evaluate_rule("C04", ctx).result == "pass"
