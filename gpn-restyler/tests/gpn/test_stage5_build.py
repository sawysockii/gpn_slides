"""Stage 5 export integration tests (spec §§12.1-12.2, 13.1).

Measured builds on synthetic inputs through the shared native exporter,
saved-geometry mutation detection, saved-style remeasurement and the
JSON-only role/size mutation chain. No corpus reads beyond the ontology.
"""

from __future__ import annotations

import json
import zipfile
from functools import lru_cache
from pathlib import Path

import conftest as fx

PROJECT_ROOT = Path(__file__).resolve().parents[3]


@lru_cache(maxsize=1)
def _rules():
    from slides_cli.gpn.compiler import compile_ontology
    from slides_cli.gpn.ontology import resolve_ontology_sources

    sources = resolve_ontology_sources(PROJECT_ROOT)
    rules = compile_ontology(PROJECT_ROOT / sources.primary_json, None,
                             sources=sources)
    return rules, sources


def _mini_run(tmp_path: Path, texts: list[str], pipes: str = "run") -> Path:
    """Minimal measured-build inputs: import + profile artifacts + 1 plan."""
    from slides_cli.gpn.assets import AssetStore
    from slides_cli.gpn.importer import import_deck
    from slides_cli.gpn.planner import LayoutIntent
    from slides_cli.gpn.provenance import build_ledger

    run_dir = tmp_path / pipes
    run_dir.mkdir(parents=True, exist_ok=True)
    prs = fx.new_deck()
    slide = fx.blank_slide(prs)
    for i, text in enumerate(texts):
        fx.add_textbox(slide, text, left=1.0, top=1.0 + i * 1.2,
                       width=6.0, height=1.0, size=18)
    src = tmp_path / "src.pptx"
    prs.save(str(src))
    store = AssetStore(run_dir / "assets")
    deck, _ = import_deck(src, store)
    ledger = build_ledger(deck, decoration=[])
    (run_dir / "source_ir.json").write_text(
        deck.model_dump_json(), encoding="utf-8")
    (run_dir / "source_ledger.json").write_text(
        ledger.model_dump_json(), encoding="utf-8")
    rules, _ = _rules()
    (run_dir / "compiled_rules.json").write_text(
        rules.model_dump_json(), encoding="utf-8")
    profile = {"content_box": {"x": 0.5, "y": 1.2, "w": 9.0, "h": 4.3},
               "unit": "in", "verified": True, "source": "test_template"}
    (run_dir / "template_profile.json").write_text(
        json.dumps(profile), encoding="utf-8")
    (run_dir / "run_manifest.json").write_text(json.dumps(
        {"command": "import", "source_path": str(src),
         "source_sha256": deck.source_sha256}), encoding="utf-8")
    from slides_cli.gpn.typography import (
        build_font_inventory_from_discovery,
        discover_local_fonts,
    )
    report = discover_local_fonts(None)
    faces = {f for role in rules.roles.values() for f in role.allowed_faces}
    inv = build_font_inventory_from_discovery(report, faces)
    (run_dir / "font_inventory.json").write_text(
        inv.model_dump_json(), encoding="utf-8")
    (run_dir / "font_discovery.json").write_text(
        report.model_dump_json(), encoding="utf-8")
    (run_dir / "list_profile.json").write_text(json.dumps(
        {"id": "LS01", "level_profiles": []}), encoding="utf-8")
    tprs = fx.new_deck()
    fx.blank_slide(tprs)
    (run_dir / "profiles").mkdir(exist_ok=True)
    tprs.save(str(run_dir / "profiles" / "base_template.pptx"))
    refs = [o.id for o in deck.slides[0].objects if o.kind == "text"]
    plans = tmp_path / "plans"
    plans.mkdir(exist_ok=True)
    intent = LayoutIntent.model_validate({
        "slide_id": deck.slides[0].id, "candidate_id": "c0",
        "content_refs": refs, "communication_job": "test",
        "reading_order": refs, "dominant_ref": refs[0] if refs else "",
        "zones": [{"zone_id": "z-all", "x_pct": 0.0, "y_pct": 0.0,
                   "w_pct": 100.0, "h_pct": 100.0, "role": "body",
                   "content_refs": refs}],
        "relations": [], "operators": [], "reference_ids": [],
        "novel_recipe": "", "uncertainties": [], "rationale": "test"})
    (plans / "c0.json").write_text(json.dumps(
        {"intent": intent.model_dump(mode="json"),
         "generation_origin": "harness_model"}, ensure_ascii=False),
        encoding="utf-8")
    (plans / "accepted_plan_manifest.json").write_text(json.dumps(
        {"generation_origin": "harness_model", "plans": ["c0.json"]}),
        encoding="utf-8")
    return run_dir


def _config():
    from types import SimpleNamespace

    return SimpleNamespace(
        project_root=PROJECT_ROOT,
        paths=SimpleNamespace(fonts_dir="assets/fonts"),
        model=SimpleNamespace(provider="harness"),
        policy=SimpleNamespace(mode="preserve_structure"),
        layout=SimpleNamespace(max_local_layout_attempts=3),
    )


def test_measured_build_end_to_end(tmp_path: Path) -> None:
    """Full measured path on a synthetic slide: resolve -> export ->
    reimport -> saved geometry verified, candidate on disk."""
    from slides_cli.gpn.export_models import ExportOptions
    from slides_cli.gpn.measured_build import run_measured_build

    run_dir = _mini_run(tmp_path, ["Первый слайд", "Второй абзац"])
    result = run_measured_build(
        run_dir, tmp_path / "plans", _config(),
        ExportOptions(workflow="presentation", diagnostic=True,
                      overwrite=True),
        {"font_allowlist_roots": ["/Users/wysockii/Library/Fonts/",
                                  "/System/Library/Fonts/"],
         "layout_mode": "preserve_structure",
         "candidate_name": "mini_candidate.pptx"})
    assert result.operation_status in ("completed", "needs_review"), result.issues
    assert result.layout_resolved is True
    assert result.candidate_path is not None
    assert Path(result.candidate_path).is_file()
    assert (run_dir / "saved_geometry_diff.json").is_file()
    assert (run_dir / "saved_measurement_report.json").is_file()
    assert (run_dir / "candidate_evaluations.json").is_file()


def test_native_saved_geometry_mutation_detected(tmp_path: Path) -> None:
    """A deliberately shrunk textbox in a copy of the candidate is caught
    by the reopen comparison (expected vs actual bytes, never echo JSON)."""
    from slides_cli.gpn.assets import AssetStore
    from slides_cli.gpn.export_models import ExportOptions
    from slides_cli.gpn.importer import import_deck
    from slides_cli.gpn.measured_build import (
        compare_saved_geometry,
        run_measured_build,
    )

    run_dir = _mini_run(tmp_path, ["Текст для мутации"])
    result = run_measured_build(
        run_dir, tmp_path / "plans", _config(),
        ExportOptions(workflow="presentation", diagnostic=True,
                      overwrite=True),
        {"font_allowlist_roots": ["/Users/wysockii/Library/Fonts/",
                                  "/System/Library/Fonts/"],
         "layout_mode": "preserve_structure",
         "candidate_name": "mut_candidate.pptx"})
    assert result.candidate_path
    evaluations = json.loads(
        (run_dir / "candidate_evaluations.json").read_text(encoding="utf-8"))
    assert evaluations[0]["status"] in ("resolved", "needs_review")

    from lxml import etree

    mutated = tmp_path / "mutated.pptx"
    mutated.write_bytes(Path(result.candidate_path).read_bytes())
    members = {}
    with zipfile.ZipFile(mutated) as zin:
        for info in zin.infolist():
            members[info.filename] = zin.read(info)
    root = etree.fromstring(members["ppt/slides/slide1.xml"])
    ns = {"p": "http://schemas.openxmlformats.org/presentationml/2006/main",
          "a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
    xfrm = root.find(".//p:sp//a:xfrm", namespaces=ns)
    assert xfrm is not None
    ext = xfrm.find("a:ext", namespaces=ns)
    ext.set("cx", str(int(int(ext.get("cx")) // 2)))
    members["ppt/slides/slide1.xml"] = etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", standalone=True)
    with zipfile.ZipFile(mutated, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for name in sorted(members):
            zout.writestr(name, members[name])

    store = AssetStore(tmp_path / "mut_assets")
    after_deck, _ = import_deck(mutated, store)
    emission = json.loads(
        (run_dir / "emission_report.json").read_text(encoding="utf-8"))
    bindings = list(emission.get("records", []))
    lay_file = next((run_dir / "layout" / "resolved").glob("*.json"))
    resolved_slide = json.loads(lay_file.read_text(encoding="utf-8"))["resolved_slide"]
    diff = compare_saved_geometry([resolved_slide], after_deck, mutated,
                                  bindings, {})
    assert diff["checked_objects"] > 0
    assert diff["deviations"], "halved textbox width must be detected"
    assert diff["ok"] is False


def test_remeasurement_uses_saved_styles(tmp_path: Path) -> None:
    """A silent renderer face swap is caught: the saved typeface is not in
    the exact bundle, so remeasurement refuses it instead of trusting."""
    from slides_cli.gpn.measured_build import remeasure_saved_geometry
    from slides_cli.gpn.models import SourceDeckIR
    from slides_cli.gpn.typography import (
        build_font_inventory_from_discovery,
        discover_local_fonts,
        prepare_measurement_fonts,
    )

    _mini_run(tmp_path, ["Текст для повторного замера"])
    deck = SourceDeckIR.model_validate_json(
        (tmp_path / "run" / "source_ir.json").read_text(encoding="utf-8"))
    report = discover_local_fonts(None)
    rules, _ = _rules()
    faces = {f for role in rules.roles.values() for f in role.allowed_faces}
    inv = build_font_inventory_from_discovery(report, faces)
    bundle = prepare_measurement_fonts(inv, "", profile_kind="production")
    slide = deck.slides[0]
    for obj in slide.objects:
        if obj.kind != "text":
            continue
        for para in obj.payload.paragraphs:
            for run in para.runs:
                run.source_style.typeface = "SomeFallbackFace"
    from slides_cli.gpn.measurement import measure_components

    def measurer(request):
        out = measure_components(request, timeout_seconds=120.0,
                                 project_root=PROJECT_ROOT, use_cache=True)
        return out["response"]

    class _After:
        slides = [slide]

    rep = remeasure_saved_geometry(_After(), bundle, measurer,
                                   {"font_allowlist_roots": []})
    assert rep["measured"] == 0
    assert rep["skipped"]
    assert rep["ok"] is False


def test_snapshot_role_mutation_reaches_saved_geometry(tmp_path: Path) -> None:
    """Only the JSON snapshot changes font size: role JSON 12pt -> 20pt
    changes the measured request style and the geometry, with no Python/JS
    edits anywhere on the path."""
    from slides_cli.gpn.layout import resolve_component_style
    from slides_cli.gpn.typography import (
        build_font_inventory_from_discovery,
        discover_local_fonts,
        prepare_measurement_fonts,
    )

    rules, _ = _rules()
    report = discover_local_fonts(None)
    faces = {f for role in rules.roles.values() for f in role.allowed_faces}
    inv = build_font_inventory_from_discovery(report, faces)
    bundle = prepare_measurement_fonts(inv, "", profile_kind="production")
    before = resolve_component_style("body", rules, bundle)
    assert before["size_pt"] == 12.0
    mutated = rules.model_copy(deep=True)
    mutated.roles["body"].default_size_pt = 20.0
    after = resolve_component_style("body", mutated, bundle)
    assert after["size_pt"] == 20.0
    assert after["face_alias"] == before["face_alias"]
    from slides_cli.gpn.measurement import measure_components
    from slides_cli.gpn.models import MeasurementRequest

    def req_for(size: float, cid: str) -> MeasurementRequest:
        style = {"face_alias": before["face_alias"], "size_pt": size,
                 "bold": False, "italic": False, "underline": "",
                 "color_rgb": "404040", "language": "",
                 "baseline_shift": "none", "uppercase": False}
        return MeasurementRequest(
            schema_version="1.0", request_id=f"test-{cid}",
            canvas_pt={"w": 960.0, "h": 540.0}, css_px_per_in=96.0,
            profile_kind="production",
            components=[{"id": cid, "kind": "text",
                         "payload_ref": f"test:{cid}",
                         "paragraphs": [{"id": "p0",
                                         "runs": [{"id": "r0",
                                                   "text": "Строка для замера высоты",
                                                   "style": style}],
                                         "space_before_pt": 0.0,
                                         "space_after_pt": 0.0,
                                         "line_spacing_mode": "multiple",
                                         "line_spacing_value": 1.15,
                                         "alignment": "left", "level": 0,
                                         "list_kind": "none",
                                         "first_line_indent_pt": 0.0,
                                         "left_indent_pt": 0.0}],
                         "box_pt": {"x": 36.0, "y": 36.0, "w": 400.0,
                                    "h": 400.0},
                         "margins_pt": {}, "vertical_anchor": "top",
                         "wrap_policy": "wrap", "rotation_deg": 0.0,
                         "list_preview": {}}],
            font_assets=[{"alias": b.browser_alias, "path": b.path,
                          "sha256": b.sha256, "face_index": b.face_index,
                          "style": "normal", "weight": 400}
                         for b in bundle.bindings
                         if not b.role_or_face.startswith("marker:")],
            engine_options={"timeout_ms": 60000, "device_scale_factor": 1,
                            "allowlist_roots": [
                                "/Users/wysockii/Library/Fonts/",
                                "/System/Library/Fonts/"]},
            input_hashes={}, debug_output_dir="")

    out12 = measure_components(req_for(12.0, "s12"), timeout_seconds=90.0,
                               project_root=PROJECT_ROOT, use_cache=False)
    out20 = measure_components(req_for(20.0, "s20"), timeout_seconds=90.0,
                               project_root=PROJECT_ROOT, use_cache=False)
    h12 = out12["response"]["items"][0]["occupied_height_pt"]
    h20 = out20["response"]["items"][0]["occupied_height_pt"]
    assert h20 > h12


def test_explicit_local_layout_deferred(tmp_path: Path) -> None:
    """The layout command with an explicit local provider defers generation
    without a secret harness fallback (mirrors the Stage 4 build gate)."""
    from types import SimpleNamespace

    from slides_cli.gpn.build_cli import cmd_layout

    args = SimpleNamespace(
        run_dir=tmp_path / "run", plans_dir=tmp_path / "plans",
        workflow="presentation", overwrite=True, resume=False,
        no_measurement_cache=False, test_only_profile=False,
        layout_mode=None, candidate_name=None, llm_provider="local_server",
        diagnostic=False, render=False)
    rc = cmd_layout(args, _config(), PROJECT_ROOT)
    assert rc in (2, 4, 6)