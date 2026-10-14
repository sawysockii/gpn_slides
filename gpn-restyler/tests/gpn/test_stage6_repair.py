"""Stage 6 repair regressions (§10): scene actually drives geometry.

Stub measurers (no Chromium) prove solver/contract logic; real-Chromium
fit evidence lives in the run traces (layout_contract_trace.json).
Real ontology snapshot + real local fonts are used wherever styles
resolve, so role/size mutations are model-level, never Python literals.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import conftest as fx
import pytest

from slides_cli.gpn.composition import compile_composition
from slides_cli.gpn.layout import resolve_layout
from slides_cli.gpn.planner import LayoutIntent

PROJECT_ROOT = Path(__file__).resolve().parents[3]

TEST_PROFILE = {"content_box": {"x": 0.0, "y": 0.0, "w": 10.0, "h": 6.0},
                "unit": "in", "verified": True, "source": "test_template"}


@lru_cache(maxsize=1)
def _rules():
    from slides_cli.gpn.compiler import compile_ontology
    from slides_cli.gpn.ontology import resolve_ontology_sources

    sources = resolve_ontology_sources(PROJECT_ROOT)
    assert sources.primary_json is not None
    return compile_ontology(PROJECT_ROOT / sources.primary_json, None,
                            sources=sources)


@lru_cache(maxsize=1)
def _bundle():
    from slides_cli.gpn.typography import (
        build_font_inventory_from_discovery,
        discover_local_fonts,
        prepare_measurement_fonts,
    )

    report = discover_local_fonts(None)
    faces = {f for role in _rules().roles.values() for f in role.allowed_faces}
    inv = build_font_inventory_from_discovery(report, faces)
    assert inv.missing_faces == []
    return prepare_measurement_fonts(inv, "", profile_kind="production")


def _deck(slide_texts: list[str], tmp_path: Path):
    from slides_cli.gpn.assets import AssetStore
    from slides_cli.gpn.importer import import_deck
    from slides_cli.gpn.provenance import build_ledger

    prs = fx.new_deck()
    slide = fx.blank_slide(prs)
    for i, text in enumerate(slide_texts):
        fx.add_textbox(slide, text, left=1.0, top=0.5 + i * 1.0,
                       width=3.0, height=0.8, size=18)
    src = tmp_path / "src.pptx"
    prs.save(str(src))
    store = AssetStore(tmp_path / "assets")
    deck, _ = import_deck(src, store)
    return deck, build_ledger(deck, decoration=[])


def _intent(slide_id: str, refs: list[str], zones: list[dict],
            candidate_id: str = "c0") -> LayoutIntent:
    return LayoutIntent.model_validate({
        "slide_id": slide_id, "candidate_id": candidate_id,
        "content_refs": list(refs), "communication_job": "test",
        "reading_order": list(refs), "dominant_ref": refs[0] if refs else "",
        "zones": zones, "relations": [], "operators": [],
        "reference_ids": [], "novel_recipe": "", "uncertainties": [],
        "rationale": "test"})


def _stub_measurer(height_pt: float = 20.0, overflow: bool = False,
                   seen: list | None = None, drop: set | None = None):
    def run(request):
        if seen is not None:
            seen.append(request.model_dump(mode="json"))
        items = []
        for c in request.components:
            cid = c.id
            base = cid.rsplit("-a", 1)[0]
            if drop and (cid in drop or base in drop):
                continue
            box = c.box_pt
            items.append({
                "component_id": cid,
                "occupied_height_pt": (float(box.h) + 50.0
                                         if overflow
                                         else min(float(box.h), height_pt)),
                "occupied_width_pt": float(box.w),
                "intrinsic_min_width_pt": 24.0,
                "intrinsic_preferred_width_pt": float(box.w),
                "overflow_x": False, "overflow_y": overflow,
                "excess_pt": {"x": 0.0, "y": 50.0 if overflow else 0.0}})
        return {"items": items}
    return run


def _options(**kw) -> dict:
    opts = {"project_root": PROJECT_ROOT, "use_cache": False,
            "profile_kind": "production", "max_local_layout_attempts": 2,
            "layout_mode": "recompose", "generation_origin": "test"}
    opts.update(kw)
    return opts


def _emu_box(resolved: dict, payload_ref: str) -> dict:
    return resolved["geometry_emu"][payload_ref]


# ---------------------------------------------------------------------------
# §10#2: recompose drives target boxes (the 249/327 source-box regression)
# ---------------------------------------------------------------------------

def test_recompose_drives_target_geometry_not_source(tmp_path: Path) -> None:
    from slides_cli.gpn.units import emu_to_pt

    deck, ledger = _deck(["value"], tmp_path)
    slide = deck.slides[0]
    ref = slide.objects[0].id
    src_box = slide.objects[0].local_box
    # Intent hint deliberately far from the source box.
    intent = _intent(slide.id, [ref], [{
        "zone_id": "z-v", "x_pct": 50.0, "y_pct": 50.0, "w_pct": 20.0,
        "h_pct": 15.0, "role": "body", "content_refs": [ref]}])
    tree = compile_composition(intent, slide, ledger, _rules(), TEST_PROFILE)
    res = resolve_layout(tree, slide, ledger, _rules(), TEST_PROFILE,
                         _bundle(), _stub_measurer(), _options())
    assert res.status in ("resolved", "needs_review"), res.unresolved
    got = _emu_box(res.resolved_slide, ref)
    src = {"x": src_box.x, "y": src_box.y, "w": src_box.w, "h": src_box.h}
    assert got != src, "solver resurrected the source box"
    # Hint 50%,50%,20%,15% of the 10x6in test box.
    assert abs(emu_to_pt(got["x"]) - (5.0 * 72.0)) < 1.0
    assert abs(emu_to_pt(got["w"]) - (2.0 * 72.0)) < 1.0


def test_source_sized_overflow_box_is_infeasible(tmp_path: Path) -> None:
    """A deliberately source-sized invalid box must fail fit, not pass."""
    deck, ledger = _deck(["01"], tmp_path)
    slide = deck.slides[0]
    ref = slide.objects[0].id
    intent = _intent(slide.id, [ref], [{
        "zone_id": "z-v", "x_pct": 5.0, "y_pct": 5.0, "w_pct": 10.0,
        "h_pct": 8.0, "role": "large_metric", "content_refs": [ref]}])
    tree = compile_composition(intent, slide, ledger, _rules(), TEST_PROFILE)
    res = resolve_layout(tree, slide, ledger, _rules(), TEST_PROFILE,
                         _bundle(), _stub_measurer(overflow=True),
                         _options())
    assert res.status in ("needs_recompose", "search_exhausted",
                           "constraint_conflict"), res.status
    assert any("overflow_y" in u or "needs_recompose" in u
               or "constraint_conflict" in u or "search_exhausted" in u
               for u in res.unresolved)


# ---------------------------------------------------------------------------
# §10#3/#4: value/label roles are independent per zone
# ---------------------------------------------------------------------------

def test_metric_value_and_label_keep_independent_roles(tmp_path: Path) -> None:
    deck, ledger = _deck(["30+", "people signed up already"], tmp_path)
    slide = deck.slides[0]
    value_ref = slide.objects[0].id
    label_ref = slide.objects[1].id
    intent = _intent(slide.id, [value_ref, label_ref], [
        {"zone_id": "z-v", "x_pct": 5.0, "y_pct": 30.0, "w_pct": 20.0,
         "h_pct": 12.0, "role": "large_metric", "content_refs": [value_ref]},
        {"zone_id": "z-l", "x_pct": 5.0, "y_pct": 43.0, "w_pct": 40.0,
         "h_pct": 8.0, "role": "body", "content_refs": [label_ref]}])
    tree = compile_composition(intent, slide, ledger, _rules(), TEST_PROFILE)
    roles = {node.payload_ref: node.style_role
             for node in tree.components.values()}
    assert roles[value_ref] == "large_metric"
    assert roles[label_ref] == "body"
    seen: list = []
    res = resolve_layout(tree, slide, ledger, _rules(), TEST_PROFILE,
                         _bundle(), _stub_measurer(seen=seen), _options())
    assert res.status in ("resolved", "needs_review"), res.unresolved
    styles = {}
    for req in seen:
        for comp in req["components"]:
            for para in comp["paragraphs"]:
                for run in para["runs"]:
                    styles.setdefault(comp["payload_ref"],
                                      run["style"]["size_pt"])
    assert styles[value_ref] == pytest.approx(
        _rules().roles["large_metric"].default_size_pt)
    assert styles[label_ref] == pytest.approx(
        _rules().roles["body"].default_size_pt)
    assert styles[value_ref] != styles[label_ref]


# ---------------------------------------------------------------------------
# §10#5: measure/native identity (margins/face/wrap change the request)
# ---------------------------------------------------------------------------

def test_measurement_request_hash_reflects_layout_state() -> None:
    from slides_cli.gpn.measurement import canonical_request_hash
    from slides_cli.gpn.models import MeasurementRequest

    def req(**kw) -> MeasurementRequest:
        base = {"schema_version": "1.0", "request_id": "t",
                "canvas_pt": {"w": 960.0, "h": 540.0}, "css_px_per_in": 96.0,
                "profile_kind": "production", "components": [{
                    "id": "c", "kind": "text", "payload_ref": "r",
                    "paragraphs": [{"id": "p", "runs": [{
                        "id": "r", "text": "x",
                        "style": {"face_alias": "F", "size_pt": 12.0}}]}],
                    "box_pt": {"x": 0.0, "y": 0.0, "w": 100.0, "h": 50.0},
                    "margins_pt": {"l": 7.2, "r": 7.2, "t": 3.6, "b": 3.6},
                    "vertical_anchor": "top", "wrap_policy": "wrap",
                    "rotation_deg": 0.0, "list_preview": {}}],
                "font_assets": [], "engine_options": {}, "input_hashes": {},
                "debug_output_dir": ""}
        base["components"][0].update(kw.pop("box", {}))
        base.update(kw)
        return MeasurementRequest.model_validate(base)

    a = req()
    b = req()
    b.components[0].margins_pt = {"l": 0.0, "r": 0.0, "t": 0.0, "b": 0.0}
    c = req()
    c.components[0].wrap_policy = "nowrap"
    assert canonical_request_hash(a) == canonical_request_hash(req())
    assert canonical_request_hash(a) != canonical_request_hash(b)
    assert canonical_request_hash(a) != canonical_request_hash(c)


# ---------------------------------------------------------------------------
# §10#6: missing measurement components fail, never pass on defaults
# ---------------------------------------------------------------------------

def test_missing_measurement_component_is_coverage_gap(tmp_path: Path) -> None:
    deck, ledger = _deck(["alpha", "beta"], tmp_path)
    slide = deck.slides[0]
    refs = [o.id for o in slide.objects]
    intent = _intent(slide.id, refs, [{
        "zone_id": "z-a", "x_pct": 5.0, "y_pct": 5.0, "w_pct": 90.0,
        "h_pct": 40.0, "role": "body", "content_refs": [refs[0]]},
        {"zone_id": "z-b", "x_pct": 5.0, "y_pct": 50.0, "w_pct": 90.0,
         "h_pct": 40.0, "role": "body", "content_refs": [refs[1]]}])
    tree = compile_composition(intent, slide, ledger, _rules(), TEST_PROFILE)
    comp_ids = set(tree.components)
    dropped = {next(iter(comp_ids))}
    res = resolve_layout(tree, slide, ledger, _rules(), TEST_PROFILE,
                         _bundle(), _stub_measurer(drop=dropped), _options())
    assert res.status == "needs_review"
    assert any("MEASUREMENT_COVERAGE_GAP" in u for u in res.unresolved)


# ---------------------------------------------------------------------------
# §10#12: containers use measured heights, not equal shares
# ---------------------------------------------------------------------------

def test_same_zone_stacks_by_measured_heights(tmp_path: Path) -> None:
    deck, ledger = _deck(["short", "tall"], tmp_path)
    slide = deck.slides[0]
    refs = [o.id for o in slide.objects]

    def heights(request):
        items = []
        for c in request.components:
            paras = getattr(c, "paragraphs", None) or []
            text = "".join(
                getattr(r, "text", "") or ""
                for p in paras for r in (getattr(p, "runs", None) or []))
            tall = "tall" in text
            items.append({
                "component_id": c.id,
                "occupied_height_pt": 60.0 if tall else 20.0,
                "occupied_width_pt": float(c.box_pt.w),
                "intrinsic_min_width_pt": 24.0,
                "intrinsic_preferred_width_pt": float(c.box_pt.w),
                "overflow_x": False, "overflow_y": False,
                "excess_pt": {"x": 0.0, "y": 0.0}})
        return {"items": items}

    intent = _intent(slide.id, refs, [{
        "zone_id": "z-short", "x_pct": 5.0, "y_pct": 5.0, "w_pct": 90.0,
        "h_pct": 60.0, "role": "body", "content_refs": refs[:1]},
        {"zone_id": "z-tall", "x_pct": 5.0, "y_pct": 70.0, "w_pct": 90.0,
         "h_pct": 25.0, "role": "body", "content_refs": refs[1:]}])
    tree = compile_composition(intent, slide, ledger, _rules(), TEST_PROFILE)
    res = resolve_layout(tree, slide, ledger, _rules(), TEST_PROFILE,
                         _bundle(), heights, _options())
    assert res.status == "resolved", res.unresolved
    boxes = [res.resolved_slide["geometry_emu"][node.payload_ref
            or node.id]
            for node in tree.components.values()]
    from slides_cli.gpn.units import emu_to_pt

    heights_pt = sorted(emu_to_pt(b["h"]) for b in boxes)
    # The tall group needs more room than the short one even though its
    # declared zone is smaller: measured need wins, never an equal split.
    assert heights_pt[1] > heights_pt[0] + 10.0


# ---------------------------------------------------------------------------
# §10#11: authorized asset replacement (compare-level)
# ---------------------------------------------------------------------------

def test_authorized_asset_replacement_passes_unauthorized_fails() -> None:
    from slides_cli.gpn.export_pipeline import compare_export_preservation

    class _Atom:
        def __init__(self, aid, value):
            self.id = aid
            self.kind = "asset"
            self.canonical_value = value
            self.transform = "identity"

    class _Binding:
        def __init__(self, aid):
            self.source_atom_id = aid
            self.transform = "identity"
            self.target_subaddress = "picture"
            self.target_address = type("A", (), {
                "slide_part": "ppt/slides/slide1.xml", "shape_id": 3})()

    class _Ledger:
        atoms = [_Atom("obj#asset", "OLD")]

    class _Obj:
        id = "after-obj"

        class _Payload:
            class _Asset:
                sha256 = "NEW"
            asset = _Asset()
        payload = _Payload()

        class _Ref:
            shape_id = 3
        source_ref = _Ref()

    class _Slide:
        slide_part = "ppt/slides/slide1.xml"
        objects = [_Obj()]

    class _IR:
        slides = [_Slide()]

    ok_policy = {"authorized_asset_replacements": {
        "obj#asset": {"old_sha256": "OLD", "new_sha256": "NEW"}}}
    diff = compare_export_preservation(
        _Ledger(), _IR(), [_Binding("obj#asset")], ok_policy)
    assert diff.ok, diff.changed

    diff2 = compare_export_preservation(
        _Ledger(), _IR(), [_Binding("obj#asset")], {})
    assert not diff2.ok and diff2.changed


# ---------------------------------------------------------------------------
# §10#16/#17: central aggregation blocks and accepts
# ---------------------------------------------------------------------------

def test_aggregate_product_acceptance_blocks_and_accepts() -> None:
    # Stage 7: dict-only AND logic replaced by the typed expected-first
    # derivation (single public path). Same intent, typed contracts.
    from slides_cli.gpn.acceptance import aggregate_product_acceptance
    from slides_cli.gpn.models import (
        ArtifactStore,
        CandidateKey,
        GateEvidence,
        GateExpectation,
        GateStatus,
        InputKind,
        InputSpec,
        ProductExpectation,
        ProductScope,
        RectEMU,
        ReviewedPage,
        SavedPackageManifest,
        SavedSlide,
        SelectionEntry,
        SlideExpectation,
        SnapshotSet,
        VisualReview,
    )

    def gate(gid: str, status: GateStatus) -> GateEvidence:
        return GateEvidence(gate_id=gid, status=status,
                            expected_units=[f"{gid}:u"],
                            checked_units=[f"{gid}:u"],
                            subject_candidate_sha256="abc",
                            snapshots=SnapshotSet())

    def build(statuses: list[GateStatus], verdict: str):
        exp = ProductExpectation(
            input_spec=InputSpec(
                kind=InputKind.PPTX, path=Path("/tmp/x.pptx"),
                project_root=Path("/tmp"), output_path=Path("/tmp/o.pptx"),
                requested_scope=ProductScope.RESTYLE),
            slides=[SlideExpectation(source_slide_id="s1"),
                    SlideExpectation(source_slide_id="s2")],
            required_gates=[
                GateExpectation(gate_id="g1", expected_units=["g1:u"],
                                applies_to=["deck"]),
                GateExpectation(gate_id="g2", expected_units=["g2:u"],
                                applies_to=["deck"])])
        saved = SavedPackageManifest(
            candidate_path="/tmp/c.pptx", candidate_sha256="abc",
            canvas=RectEMU(x=0, y=0, w=12192000, h=6858000),
            slides=[SavedSlide(sld_id="1", r_id="r1",
                               part="ppt/slides/slide1.xml"),
                    SavedSlide(sld_id="2", r_id="r2",
                               part="ppt/slides/slide2.xml")])
        gates = [gate("g1", statuses[0]), gate("g2", statuses[1])]
        reviews = [VisualReview(
            task_id="t", candidate_sha256="abc",
            pages=[ReviewedPage(page=1, image="p1.png",
                                sha256="a" * 64, byte_count=5),
                   ReviewedPage(page=2, image="p2.png",
                                sha256="b" * 64, byte_count=5)],
            verdict=verdict, actual_model="test")]
        selections = [SelectionEntry(
            source_slide_id="s1",
            candidate_key=CandidateKey(run_id="r", source_slide_id="s1",
                                       candidate_id="c1"),
            candidate_sha256="abc"),
            SelectionEntry(
            source_slide_id="s2",
            candidate_key=CandidateKey(run_id="r", source_slide_id="s2",
                                       candidate_id="c1"),
            candidate_sha256="abc")]
        return aggregate_product_acceptance(
            exp, saved, gates, reviews, selections,
            artifact_store=ArtifactStore(Path("/tmp")))

    blocked = build([GateStatus.PASS, GateStatus.FAIL], "accept")
    assert not blocked.accepted and blocked.findings

    accepted = build([GateStatus.PASS, GateStatus.PASS], "accept")
    assert accepted.accepted, [f.message for f in accepted.findings]

    needs_repair = build([GateStatus.PASS, GateStatus.PASS], "repair")
    assert not needs_repair.accepted
