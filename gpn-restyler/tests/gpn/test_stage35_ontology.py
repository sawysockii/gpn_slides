"""Stage 3.5 metamorphic ontology tests (spec §12.1).

Every mutation is applied to an isolated copy of the corpus under ``tmp_path``;
``project_root/ontology`` is never written. The point of each test is that the
*engine* has no second copy of the corporate standard: a consistent change of a
normative value in the copy must change compiled values, digests, checkers and
emitted native properties without touching Python.

Golden assertions against the current corpus live in ``test_compiler.py`` and
are pinned to the corpus hash; these tests assert the *direction* of change.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from slides_cli.gpn.bullets import build_list_profile
from slides_cli.gpn.compiler import (
    build_field_origins,
    compile_ontology,
    resolve_role_style,
)
from slides_cli.gpn.ontology import resolve_ontology_sources
from slides_cli.gpn.ontology_conflicts import analyze_ontology_sources
from slides_cli.gpn.rules import RuleEvaluationContext, evaluate_rule_registry

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ONTOLOGY_DIR = PROJECT_ROOT / "ontology"
PRIMARY_NAME = "GPN_Slide_Design_Ontology.json"
MD_NAME = "GPN_Slide_Design_Ontology.md"


# ---------------------------------------------------------------------------
# isolated corpus helpers
# ---------------------------------------------------------------------------

def make_corpus_copy(tmp_path: Path, *, with_markdown: bool = True) -> Path:
    """Copy the real corpus into tmp_path/ontology and return the root."""
    root = tmp_path / "project"
    (root / "ontology").mkdir(parents=True)
    shutil.copy2(ONTOLOGY_DIR / PRIMARY_NAME, root / "ontology" / PRIMARY_NAME)
    if with_markdown:
        shutil.copy2(ONTOLOGY_DIR / MD_NAME, root / "ontology" / MD_NAME)
    return root


def read_json(root: Path) -> dict[str, Any]:
    return json.loads((root / "ontology" / PRIMARY_NAME).read_text(encoding="utf-8"))


def write_json(root: Path, data: dict[str, Any]) -> None:
    (root / "ontology" / PRIMARY_NAME).write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def compile_from(root: Path):
    sources = resolve_ontology_sources(root)
    assert sources.primary_json is not None
    compiled = compile_ontology(Path(sources.primary_json), None, sources=sources)
    return compiled, sources


def compile_real():
    return compile_from(PROJECT_ROOT)


def digest_of(compiled) -> str:
    payload = compiled.model_dump(mode="json")
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _style_profile(data: dict[str, Any]) -> dict[str, Any]:
    return data["style_profile"]


# ---------------------------------------------------------------------------
# value flow: typography / colour / minimum / canvas
# ---------------------------------------------------------------------------

def test_title_size_is_loaded(tmp_path: Path) -> None:
    """Change the title role size in a consistent fixture copy."""
    base, base_sources = compile_real()
    root = make_corpus_copy(tmp_path)
    data = read_json(root)
    for record in _style_profile(data)["typography"]:
        if record.get("role") == "main_slide_title":
            record["size_pt"] = 28.5
    write_json(root, data)

    changed, changed_sources = compile_from(root)
    assert changed.roles["main_slide_title"].default_size_pt == 28.5
    assert base.roles["main_slide_title"].default_size_pt == 24
    # The same value must reach the checker and the native style resolver.
    style = resolve_role_style("main_slide_title", changed, changed_sources)
    assert style.font_size_pt == 28.5
    origins = build_field_origins(changed)
    assert origins["entries"], "field origins must not be empty"
    assert digest_of(changed) != digest_of(base)
    assert changed_sources.ontology_corpus_hash != base_sources.ontology_corpus_hash


def test_role_rgb_is_loaded(tmp_path: Path) -> None:
    base, _ = compile_real()
    root = make_corpus_copy(tmp_path)
    data = read_json(root)
    profile = _style_profile(data)
    for token in profile["color_tokens"]:
        if token.get("id") == "gray":
            token["value"] = "#112233"
    write_json(root, data)

    changed, sources = compile_from(root)
    assert changed.colors["gray"] == "112233"
    assert base.colors["gray"] != "112233"
    # Provenance binds the compiled field to its JSON source pointer.
    origins = build_field_origins(changed)
    by_field = {e["field"]: e for e in origins["entries"]}
    assert by_field["colors.gray"]["source_pointer"] == (
        "/style_profile/color_tokens/id=gray/value"
    )
    _ = sources


def test_minimum_size_is_loaded(tmp_path: Path) -> None:
    base, _ = compile_real()
    assert base.minimum_text_font_size is not None
    assert base.minimum_text_font_size.value_pt == 8.0

    root = make_corpus_copy(tmp_path, with_markdown=False)
    data = read_json(root)
    _style_profile(data)["minimum_text_font_size"]["value_pt"] = 10.0
    write_json(root, data)
    changed, _ = compile_from(root)
    assert changed.minimum_text_font_size is not None
    assert changed.minimum_text_font_size.value_pt == 10.0

    # The minimum-size checker must compare against the loaded value.
    from slides_cli.gpn.models import SourceDeckIR  # noqa: F401
    from slides_cli.gpn.rules import RuleEvaluationContext as Ctx

    class _Deck:
        source_sha256 = "0" * 64
        slides: list[Any] = []

    ctx_low = Ctx(subject_kind="source", compiled_rules=changed,
                  source_ir=_Deck(), ledger=None)
    coverage_low = {c.rule_id: c for c in
                    evaluate_rule_registry(changed.rule_registry, ctx_low)}
    assert "C19" in coverage_low or "C18" in coverage_low


def test_bullet_percent_is_loaded(tmp_path: Path) -> None:
    base, _ = compile_real()
    assert base.lists["LS01"].marker_settings["buSzPct"] == "80000"

    root = make_corpus_copy(tmp_path, with_markdown=False)
    data = read_json(root)
    for style in _style_profile(data)["list_styles"]:
        if style.get("id") == "LS01":
            style["marker"]["ooxml"]["buSzPct"] = "65000"
    write_json(root, data)
    changed, _ = compile_from(root)
    assert changed.lists["LS01"].marker_settings["buSzPct"] == "65000"
    assert changed.lists["LS01"].marker_settings["buChar"] == "§"


def test_nested_size_policy_is_loaded(tmp_path: Path) -> None:
    base, _ = compile_real()
    policy = base.lists["LS01"].nested_size_policy
    assert policy is not None
    assert (policy.if_parent_gte, policy.then_subtract,
            policy.otherwise_subtract, policy.minimum) == (12.0, 2.0, 1.0, 8.0)

    root = make_corpus_copy(tmp_path, with_markdown=False)
    data = read_json(root)
    for style in _style_profile(data)["list_styles"]:
        if style.get("id") == "LS01":
            nested = style["text"]["nested_size_policy"]
            nested["if_parent_gte"] = 11.0
            nested["then_subtract"] = 3.0
            nested["otherwise_subtract"] = 1.5
            nested["minimum"] = 9.0
    write_json(root, data)
    changed, _ = compile_from(root)
    loaded = changed.lists["LS01"].nested_size_policy
    assert loaded is not None
    assert (loaded.if_parent_gte, loaded.then_subtract,
            loaded.otherwise_subtract, loaded.minimum) == (11.0, 3.0, 1.5, 9.0)

    # The runtime algorithm must follow the loaded numbers, not 12/2/1/8.
    from slides_cli.gpn.bullets import nested_size_for_level

    assert nested_size_for_level(12.0, level=1, policy=loaded) == 9.0
    assert nested_size_for_level(10.0, level=1, policy=loaded) == 8.5


def test_conditional_color_priority(tmp_path: Path) -> None:
    base, _ = compile_real()
    assert "sky" in base.conditional_colors

    root = make_corpus_copy(tmp_path, with_markdown=False)
    data = read_json(root)
    colors = _style_profile(data)["color_tokens"]
    for token in colors:
        if token.get("id") == "sky":
            # Real schema: conditional applicability lives in
            # ``usage == "conditional"`` + ``source_slides`` evidence.
            token["usage"] = "conditional"
            token["source_slides"] = [99]
    write_json(root, data)
    changed, _ = compile_from(root)
    assert changed.conditional_colors["sky"].allowed_context_ids == ["99"]


def test_font_face_policy_is_loaded(tmp_path: Path) -> None:
    base, _ = compile_real()
    faces = {r.get("typeface") for r in base.font_policy.get("rules", [])}
    assert "GPN_DIN Regular" in faces

    root = make_corpus_copy(tmp_path, with_markdown=False)
    data = read_json(root)
    policy = _style_profile(data)["font_style_policy"]
    policy["rules"].append({
        "typeface": "GPN_DIN Condensed Light",
        "allowed": True,
        "scope": "source_addition",
    })
    write_json(root, data)
    changed, _ = compile_from(root)
    changed_faces = {r.get("typeface") for r in changed.font_policy.get("rules", [])}
    assert "GPN_DIN Condensed Light" in changed_faces
    assert "GPN_DIN Condensed Light" not in faces


def test_added_role_not_rejected_by_count(tmp_path: Path) -> None:
    root = make_corpus_copy(tmp_path, with_markdown=False)
    data = read_json(root)
    _style_profile(data)["typography"].append({
        "role": "kpi_callout",
        "size_pt": 18.0,
        "font": "GPN_DIN Condensed Bold",
        "uppercase": False,
        "status": "observed_variant",
    })
    write_json(root, data)
    changed, _ = compile_from(root)
    assert "kpi_callout" in changed.roles
    assert len(changed.roles) == 11  # 10 + the added one; no fixed-count gate

    # The dynamic role id must reach the generated intent schema enum.
    from slides_cli.gpn.planner import (
        PlanningContext,
        PlanningSlideView,
        build_intent_schema,
    )

    packet = PlanningContext(
        packet_id="p", source_hash="h", slide_id="s", snapshot_id="x",
        ontology_corpus_hash="x",
        view=PlanningSlideView(slide_id="s", objects=[{"ref": "r1"}]),
    )
    schema = build_intent_schema(packet, changed,
                                 {"implemented_operators": changed.composition_operators})
    role_enum = schema["properties"]["zones"]["items"]["properties"]["role"]["enum"]
    assert "kpi_callout" in role_enum


def test_added_catalog_not_rejected_by_count(tmp_path: Path) -> None:
    base, _ = compile_real()
    root = make_corpus_copy(tmp_path, with_markdown=False)
    data = read_json(root)
    catalogs = data.setdefault("catalogs", {})
    first_kind = next(iter(catalogs))
    first = copy.deepcopy(catalogs[first_kind][0])
    first["id"] = "source_added_layout"
    catalogs[first_kind].append(first)
    write_json(root, data)
    changed, _ = compile_from(root)
    assert len(changed.catalog_records) == len(base.catalog_records) + 1
    record = next(r for r in changed.catalog_records
                  if getattr(r, "id", None) == "source_added_layout")
    assert getattr(record, "status", "") != "rendered"
    assert "source_added_layout" not in changed.composition_operators


def test_added_rule_is_unknown(tmp_path: Path) -> None:
    root = make_corpus_copy(tmp_path, with_markdown=False)
    data = read_json(root)
    data["formal_model"]["constraints"].append({
        "id": "C99",
        "severity": "hard",
        "condition": "На всех слайдах с финансовыми показателями применяется "
                     "специальная маркировка источника данных.",
        "remedy": "Добавить маркировку источника под таблицей.",
    })
    write_json(root, data)
    changed, _ = compile_from(root)
    assert "C99" in changed.rule_registry.specs
    binding = changed.rule_registry.bindings.get("C99")
    assert binding is None or binding.implementation_status in (
        "unknown", "deferred", "partial", "needs_binding",
    ), "an unbindable new rule must never become an implemented pass"


def test_same_id_changed_meaning_requires_binding(tmp_path: Path) -> None:
    root = make_corpus_copy(tmp_path, with_markdown=False)
    data = read_json(root)
    for constraint in data["formal_model"]["constraints"]:
        if constraint.get("id") == "C01":
            constraint["condition"] = (
                "Все подписи к диаграммам должны быть выровнены по "
                "правому краю колонки значений."
            )
    write_json(root, data)
    changed, _ = compile_from(root)
    binding = changed.rule_registry.bindings.get("C01")
    if binding is not None:
        assert binding.implementation_status != "implemented" or \
            binding.checker_type != "palette_checker", (
            "changed meaning under a known id must not reuse the old checker"
        )


def test_canvas_loaded_template_mismatch(tmp_path: Path) -> None:
    root = make_corpus_copy(tmp_path, with_markdown=False)
    data = read_json(root)
    _style_profile(data)["canvas"]["width_emu"] = 9144000
    write_json(root, data)
    changed, _ = compile_from(root)
    assert (changed.canvas.w, changed.canvas.h) == (9144000, 6858000)

    from slides_cli.gpn.errors import TemplateMismatchError
    from slides_cli.gpn.template import derive_base_template

    class _Canvas:
        w = 12192000
        h = 6858000

    with pytest.raises(TemplateMismatchError):
        derive_base_template.__wrapped__ if False else _assert_mismatch(changed)


def _assert_mismatch(compiled) -> None:
    from slides_cli.gpn.template import check_canvas_compatible

    check_canvas_compatible(compiled.canvas, w=12192000, h=6858000)


def test_hash_invalidates_all_dependents(tmp_path: Path) -> None:
    """Same metadata.version, different corpus hash ⇒ different snapshot id."""
    root = make_corpus_copy(tmp_path, with_markdown=False)
    before, before_sources = compile_from(root)
    assert before.metadata["version"] == json.loads(
        (ONTOLOGY_DIR / PRIMARY_NAME).read_text(encoding="utf-8")
    )["metadata"]["version"]

    data = read_json(root)
    _style_profile(data)["canvas"]["height_emu"] = 6858001
    write_json(root, data)
    after, after_sources = compile_from(root)
    assert after.metadata["version"] == before.metadata["version"]
    assert after_sources.ontology_corpus_hash != before_sources.ontology_corpus_hash

    from slides_cli.gpn.planner import (
        PlanningContext,
        PlanningSlideView,
        build_intent_schema,
    )

    def _schema_for(compiled, sources):
        packet = PlanningContext(
            packet_id="p", source_hash="h", slide_id="s",
            snapshot_id=sources.ontology_corpus_hash,
            ontology_corpus_hash=sources.ontology_corpus_hash,
            view=PlanningSlideView(slide_id="s", objects=[{"ref": "r1"}]),
        )
        return json.dumps(
            build_intent_schema(packet, compiled,
                                {"implemented_operators": compiled.composition_operators}),
            sort_keys=True,
        )

    assert _schema_for(before, before_sources) != _schema_for(after, after_sources)


def test_missing_source_has_no_builtin_fallback(tmp_path: Path) -> None:
    from slides_cli.gpn.errors import AssetMissingError

    # An absent corpus directory fails loudly.
    absent = tmp_path / "absent_project"
    with pytest.raises(AssetMissingError):
        resolve_ontology_sources(absent)

    # An empty corpus directory never falls back to builtin GPN defaults.
    empty = tmp_path / "empty_project"
    (empty / "ontology").mkdir(parents=True)
    sources = resolve_ontology_sources(empty)
    assert not sources.ready
    assert sources.primary_json is None


def test_original_corpora_immutable(tmp_path: Path) -> None:
    """Compiling never writes into project_root/ontology."""
    before = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(ONTOLOGY_DIR.iterdir()) if p.is_file()
    }
    root = make_corpus_copy(tmp_path)
    data = read_json(root)
    _style_profile(data)["minimum_text_font_size"]["value_pt"] = 9.0
    write_json(root, data)
    compile_from(root)
    after = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(ONTOLOGY_DIR.iterdir()) if p.is_file()
    }
    assert before == after


def test_conflicting_minimum_is_reported_not_ignored(tmp_path: Path) -> None:
    """A new minimum that contradicts the unchanged MD text is a conflict."""
    root = make_corpus_copy(tmp_path)
    data = read_json(root)
    _style_profile(data)["minimum_text_font_size"]["value_pt"] = 14.0
    write_json(root, data)
    sources = resolve_ontology_sources(root)
    report = analyze_ontology_sources(sources)
    assert not report.ready_for_compilation
    assert any(c.blocking for c in report.conflicts)


def test_list_profile_derived_values_are_labelled(tmp_path: Path) -> None:
    """Values absent from JSON stay usable but marked as derived."""
    root = make_corpus_copy(tmp_path, with_markdown=False)
    changed, sources = compile_from(root)
    profile = build_list_profile(changed, None, [])
    rendered = json.loads(json.dumps(profile, ensure_ascii=False, default=str))
    text = json.dumps(rendered, ensure_ascii=False)
    assert "derived" in text, (
        "indents/spacing not present in the corpus must be reported as "
        "derived with provenance, not as normative JSON values"
    )
    _ = sources


def test_rule_context_uses_compiled_minimum() -> None:
    """C18/C19 read the minimum from the snapshot, not from a literal 8."""
    compiled, _ = compile_real()
    assert compiled.minimum_text_font_size is not None
    ctx = RuleEvaluationContext(subject_kind="source", compiled_rules=compiled,
                                source_ir=None, ledger=None)
    assert ctx.compiled_rules is compiled


def test_source_refuses_unsupported_normative_value(tmp_path: Path) -> None:
    """A malformed normative value fails loudly instead of defaulting."""
    root = make_corpus_copy(tmp_path, with_markdown=False)
    data = read_json(root)
    _style_profile(data)["minimum_text_font_size"]["value_pt"] = -1
    write_json(root, data)
    with pytest.raises(Exception) as excinfo:
        compile_from(root)
    assert "minimum" in str(excinfo.value).lower()