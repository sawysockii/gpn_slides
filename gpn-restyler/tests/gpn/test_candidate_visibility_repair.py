"""Real source/native bytes for scope and merged-text binding regressions."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import conftest as fx
import pytest

from slides_cli.gpn.assets import AssetStore
from slides_cli.gpn.export_pipeline import _authored_block_paragraphs
from slides_cli.gpn.importer import import_deck
from slides_cli.gpn.measured_build import check_required_visibility_for_run
from slides_cli.gpn.models import CompiledOntology, RoleStyle
from slides_cli.gpn.native import NativeDeckContext, emit_rich_text_block


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False))


def native_case(root, count=3, selected_index=1):
    source = fx.new_deck()
    for index in range(count):
        slide = fx.blank_slide(source)
        fx.add_textbox(slide, f"Exact source claim {index}", left=1, top=1)
        fx.add_textbox(slide, f"Exact source qualifier {index}", left=1, top=2)
    source_path = root / "source.pptx"
    source.save(source_path)
    store = AssetStore(root / "assets")
    deck, ledger = import_deck(source_path, store)
    selected = deck.slides[selected_index]
    paragraphs = _authored_block_paragraphs(
        [o.id for o in selected.objects], {o.id: o for o in selected.objects}
    )
    target = fx.new_deck()
    output_slide = fx.blank_slide(target)
    context = NativeDeckContext(presentation=target)
    rules = CompiledOntology(
        roles={
            "body": RoleStyle(
                role="body",
                default_size_pt=12,
                allowed_faces=["DejaVu Sans"],
                color_tokens=["text"],
            )
        },
        colors={"text": "112233"},
    )
    emit_rich_text_block(
        paragraphs,
        {"x": 914400, "y": 914400, "w": 7000000, "h": 2000000},
        output_slide,
        context,
        role="body",
        rules=rules,
        node_id="rt-zone:logical-block",
    )
    candidate = root / "candidate.pptx"
    target.save(candidate)
    after, _ = import_deck(candidate, AssetStore(root / "after_assets"))
    write(root / "source_ir.json", deck.model_dump(mode="json"))
    write(root / "source_ledger.json", ledger.model_dump(mode="json"))
    write(
        root / "emission_report.json",
        {
            "records": [r.model_dump(mode="json") for r in context.emission_records],
            "issues": [],
        },
    )
    write(
        root / "output_object_map.json",
        {
            "bindings": [b.model_dump(mode="json") for b in context.output_map],
        },
    )
    write(root / "saved_geometry_report.json", {"out_of_canvas": [], "result": "pass"})
    return deck, selected, after, context


def test_selected_candidate_does_not_require_other_source_slides(tmp_path):
    deck, selected, after, _ = native_case(tmp_path)
    assert selected.slide_part != after.slides[0].slide_part  # real source->target renumbering
    report = check_required_visibility_for_run(tmp_path, after, selected_slide_ids=[selected.id])
    assert report["result"] == "pass", report
    assert report["selected_source_slide_ids"] == [selected.id]
    assert report["expected_source_atom_count"] == 2
    assert len(report["out_of_scope_required_atoms"]) == 4
    assert len(deck.slides) == 3 and len(after.slides) == 1


def test_merged_block_resolves_all_original_atoms_to_one_saved_shape(tmp_path):
    _, _, after, context = native_case(tmp_path, 1, 0)
    assert context.emission_records[0].source_object_id == "rt-zone:logical-block"
    report = check_required_visibility_for_run(tmp_path, after)
    assert report["result"] == "pass", report
    assert report["expected_source_atom_count"] == 2
    assert report["required_after_ids"] == 1
    assert len(report["mapping_evidence"]) == 2
    assert (
        report["candidate_sha256"]
        == hashlib.sha256((tmp_path / "candidate.pptx").read_bytes()).hexdigest()
    )


def test_full_source_scope_still_rejects_one_slide_candidate(tmp_path):
    _, _, after, _ = native_case(tmp_path)
    report = check_required_visibility_for_run(tmp_path, after)
    assert report["result"] == "fail"
    assert report["source_scope"] == "full_source"
    assert len(report["missing"]) == 4
    assert len(report["selected_source_slide_ids"]) == 3


@pytest.mark.parametrize("damage", ["binding", "target", "empty_map"])
def test_real_missing_content_binding_is_not_excused_by_existing_rich_block(tmp_path, damage):
    _, selected, after, _ = native_case(tmp_path)
    path = tmp_path / "output_object_map.json"
    mapping = json.loads(path.read_text())
    if damage == "binding":
        mapping["bindings"].pop()
    elif damage == "target":
        mapping["bindings"][0]["target_address"]["shape_id"] = 99999
    else:
        mapping["bindings"] = []
    write(path, mapping)
    report = check_required_visibility_for_run(tmp_path, after, selected_slide_ids=[selected.id])
    assert report["result"] == "fail", report
    assert report["missing"]


@pytest.mark.parametrize("selection", [[], ["unknown-slide"], ["duplicate", "duplicate"]])
def test_invalid_source_scope_blocks_instead_of_empty_pass(tmp_path, selection):
    _, _, after, _ = native_case(tmp_path)
    report = check_required_visibility_for_run(tmp_path, after, selected_slide_ids=selection)
    assert report["result"] == "unknown"
    assert "VISIBILITY_SCOPE" in report["reason"]


def test_no_required_atoms_is_unknown_and_inputs_stay_immutable(tmp_path):
    _, selected, after, _ = native_case(tmp_path)
    write(tmp_path / "source_ledger.json", {"atoms": []})
    paths = [
        tmp_path / n for n in ("source_ir.json", "source_ledger.json", "output_object_map.json")
    ]
    before = [p.read_bytes() for p in paths]
    report = check_required_visibility_for_run(tmp_path, after, selected_slide_ids=[selected.id])
    assert report["result"] == "unknown"
    assert before == [p.read_bytes() for p in paths]


def test_one_source_atom_with_multiple_saved_targets_requires_every_target(tmp_path):
    _, selected, after, _ = native_case(tmp_path)
    path = tmp_path / "output_object_map.json"
    mapping = json.loads(path.read_text())
    import copy

    invalid_repeat = copy.deepcopy(mapping["bindings"][0])
    invalid_repeat["target_address"]["shape_id"] = 99999
    mapping["bindings"].append(invalid_repeat)
    write(path, mapping)
    report = check_required_visibility_for_run(tmp_path, after, selected_slide_ids=[selected.id])
    assert report["result"] == "fail"
    assert any("saved_target_absent" in s for s in report["missing"])


def test_saved_check_wiring_preserves_prior_failure_and_uses_requested_scope(tmp_path, monkeypatch):
    from slides_cli.gpn import measured_build
    from slides_cli.gpn.export_models import NativeBuildResult

    _, selected, _, _ = native_case(tmp_path)
    monkeypatch.setattr(measured_build, "compare_saved_geometry", lambda *args: {"ok": False})
    monkeypatch.setattr(
        measured_build, "validate_resolved_geometry", lambda *args: {"result": "pass"}
    )
    result = NativeBuildResult(
        candidate_path=str(tmp_path / "candidate.pptx"),
        saved_geometry_verified=False,
        operation_status="needs_review",
        artifacts={},
    )
    measured_build._write_saved_checks(
        tmp_path,
        [],
        result,
        {"visibility_selected_slide_ids": [selected.id]},
        None,
        {"x": 0, "y": 0, "w": 960, "h": 540},
    )
    report = json.loads((tmp_path / "required_visibility_report.json").read_text())
    assert report["result"] == "pass" and report["selected_source_slide_ids"] == [selected.id]
    assert result.saved_geometry_verified is False  # another gate cannot be cleared


def test_stale_pilot_manifest_cannot_shrink_default_full_source_check(tmp_path):
    _, selected, after, _ = native_case(tmp_path)
    write(
        tmp_path / "ledger_scope_manifest.json",
        {
            "slides": [{"id": selected.id}],
            "selected_atoms": 2,
        },
    )
    report = check_required_visibility_for_run(tmp_path, after)
    assert report["result"] == "fail"
    assert report["source_scope"] == "full_source" and report["expected_source_atom_count"] == 6


@pytest.mark.parametrize(
    "relative", ["profiles/base_template.pptx", "base_template.pptx", "asset_overrides.json"]
)
def test_restored_template_or_asset_override_invalidates_cached_failure(tmp_path, relative):
    from slides_cli.gpn.candidate_jobs import build_fingerprint

    plan = tmp_path / "plan.json"
    write(plan, {"candidate_id": "c1"})
    before = build_fingerprint(tmp_path, plan)
    asset = tmp_path / relative
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(b"dependency now exists; fingerprint fixture only")
    restored = build_fingerprint(tmp_path, plan)
    asset.write_bytes(b"dependency revision")
    revised = build_fingerprint(tmp_path, plan)
    assert len({before, restored, revised}) == 3
    assert json.loads(plan.read_text()) == {"candidate_id": "c1"}


@pytest.mark.parametrize("blocking", [False, True])
def test_verified_opc_recovery_info_does_not_clear_visibility_failure(
    tmp_path, monkeypatch, blocking
):
    # Runner finding classification boundary. These are not native/render
    # gate assertions; native bytes are checked by the tests above.
    from slides_cli.gpn import measured_build
    from slides_cli.gpn import restyle_runner as runner
    from slides_cli.gpn.models import InputSpec

    sid = "fixture-slide"
    state = runner.RunState(
        run_id="r",
        run_dir=str(tmp_path),
        input_spec=InputSpec(
            kind="prompt",
            prompt_text="test",
            project_root=tmp_path,
            output_path=tmp_path / "out.pptx",
        ),
    )
    plan = tmp_path / "scenes/fixture-slide/c1/plan.json"
    plan.parent.mkdir(parents=True)
    write(plan, {})

    def measured_boundary(root, *args):
        root = Path(root)
        candidate = root / "candidate.pptx"
        candidate.write_bytes(b"classification boundary fixture; not a render proof")
        write(root / "layout_geometry_report.json", {"result": "pass"})
        issues = ["ASSET_RECOVERED_FROM_SOURCE_OPC: verified digest"]
        if blocking:
            issues.append("REQUIRED_VISIBILITY_FAIL: missing required object")
        return SimpleNamespace(
            candidate_path=str(candidate),
            operation_status="needs_review" if blocking else "completed",
            saved_geometry_verified=not blocking,
            issues=issues,
        )

    monkeypatch.setattr(measured_build, "run_measured_build", measured_boundary)
    result = runner._build_deck_candidate(
        state, "job", {sid: "c1"}, None, focus_slide=sid, focus_candidate="c1"
    )
    recovery = next(f for f in result.findings if f.code == "ASSET_RECOVERED_FROM_SOURCE_OPC")
    assert recovery.severity == "info"
    assert result.eligible is (not blocking)
    if blocking:
        assert any(
            f.code == "REQUIRED_VISIBILITY_FAIL" and f.severity == "error" for f in result.findings
        )


@pytest.mark.parametrize("corrupt_source", [False, True])
def test_recovered_picture_has_exact_bytes_in_native_package_or_blocks(tmp_path, corrupt_source):
    from PIL import Image

    from slides_cli.gpn.models import AssetRef
    from slides_cli.gpn.native import emit_image

    # Real decodable PNG, real resolver/emitter, real saved PPTX. This is
    # an asset-preservation fixture, not a corporate design/render proof.
    stream = io.BytesIO()
    Image.new("RGB", (30, 20), (10, 50, 90)).save(stream, format="PNG")
    blob = stream.getvalue()
    digest = hashlib.sha256(blob).hexdigest()
    asset = AssetRef(
        sha256=digest, relative_path="absent.png", media_type="image/png", byte_count=len(blob)
    )
    presentation = fx.new_deck()
    slide = fx.blank_slide(presentation)
    context = NativeDeckContext(
        presentation=presentation,
        work_dir=tmp_path,
        asset_store=AssetStore(tmp_path / "empty_store"),
        transplant_context=SimpleNamespace(
            source_parts={"ppt/media/image1.png": blob + b"corrupt" if corrupt_source else blob},
            source_content_types={"ppt/media/image1.png": "image/png"},
        ),
    )
    args = (
        SimpleNamespace(asset=asset),
        {"box_emu": {"x": 0, "y": 0, "w": 914400, "h": 609600}},
        slide,
        context,
    )
    if corrupt_source:
        with pytest.raises(ValueError, match="REQUIRED_ASSET_UNRESOLVED"):
            emit_image(*args, source_object_id="test-image")
        assert not any(s.startswith("ASSET_RECOVERED_FROM_SOURCE_OPC") for s in context.issues)
        assert not list(slide.shapes)
        return
    emit_image(*args, source_object_id="test-image")
    output = tmp_path / "native_image.pptx"
    presentation.save(output)
    with zipfile.ZipFile(output) as archive:
        media = [archive.read(n) for n in archive.namelist() if n.startswith("ppt/media/")]
        assert media == [blob]
        assert hashlib.sha256(media[0]).hexdigest() == digest
        assert b"<p:pic>" in archive.read("ppt/slides/slide1.xml")
    assert any(s.startswith("ASSET_RECOVERED_FROM_SOURCE_OPC") for s in context.issues)


@pytest.mark.parametrize(
    ("damage", "expected"),
    [
        (None, "pass"),
        ("unknown", "unknown"),
        ("fail_without_missing", "fail"),
        ("unknown_atoms", "unknown"),
        ("stale_sha", "fail"),
        ("missing_sha", "unknown"),
        ("partial_scope", "unknown"),
        ("missing_scope", "unknown"),
        ("duplicate_scope", "unknown"),
        ("measure_unknown", "unknown"),
        ("measure_fail", "fail"),
    ],
)
def test_final_fit_gate_requires_current_positive_report_for_every_source_slide(
    tmp_path, damage, expected
):
    from slides_cli.gpn.restyle_runner import _gate_source_reports

    write(tmp_path / "source_ir.json", {"slides": [{"id": "s1"}, {"id": "s2"}]})
    visibility = {
        "result": "pass",
        "missing": [],
        "unknown_required": [],
        "candidate_sha256": "a" * 64,
        "selected_source_slide_ids": ["s1", "s2"],
    }
    measure = {"ok": True}
    if damage == "unknown":
        visibility["result"] = "unknown"
    elif damage == "fail_without_missing":
        visibility["result"] = "fail"
    elif damage == "unknown_atoms":
        visibility["unknown_required"] = ["source-atom"]
    elif damage == "stale_sha":
        visibility["candidate_sha256"] = "b" * 64
    elif damage == "missing_sha":
        visibility.pop("candidate_sha256")
    elif damage == "partial_scope":
        visibility["selected_source_slide_ids"] = ["s1"]
    elif damage == "missing_scope":
        visibility.pop("selected_source_slide_ids")
    elif damage == "duplicate_scope":
        visibility["selected_source_slide_ids"] = ["s1", "s1", "s2"]
    elif damage == "measure_unknown":
        measure = {}
    elif damage == "measure_fail":
        measure = {"ok": False}
    write(tmp_path / "required_visibility_report.json", visibility)
    write(tmp_path / "saved_measurement_report.json", measure)
    gate = _gate_source_reports(tmp_path, "a" * 64)["text_fit_visibility"]
    assert gate[0] == expected, gate
    assert bool(gate[1]) is (expected == "pass")
    assert bool(gate[2]) is (expected != "pass")
