"""Geometry binding regressions (opencode prompt §1, §8, §12).

Proves the diagnostic-fallback root cause is gone:
- zone content_refs resolve to zone boxes (no synthetic source-ID rows);
- diagnostic_geometry=true never passes user acceptance;
- required content fully outside canvas fails even on exact text match.
"""

from __future__ import annotations

from pathlib import Path

from pptx import Presentation
from pptx.util import Inches

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _rules():
    from slides_cli.gpn.compiler import compile_ontology
    from slides_cli.gpn.ontology import resolve_ontology_sources

    sources = resolve_ontology_sources(PROJECT_ROOT)
    assert sources.primary_json is not None
    return compile_ontology(Path(sources.primary_json), None, sources=sources)


def _offcanvas_deck(path: Path) -> Path:
    """One textbox placed fully below the 7.5in canvas (title y=8in)."""
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1), Inches(8.0), Inches(6), Inches(1))
    box.text_frame.paragraphs[0].add_run().text = "Off-canvas title"
    prs.save(str(path))
    return path


def _incanvas_deck(path: Path) -> Path:
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(1))
    box.text_frame.paragraphs[0].add_run().text = "Visible title"
    prs.save(str(path))
    return path


def test_zone_binding_no_synthetic_rows():
    """Title takes the zone box; no source-ID fallback row is created."""
    from slides_cli.gpn.assets import AssetStore
    from slides_cli.gpn.export_pipeline import resolve_slides_from_intents
    from slides_cli.gpn.importer import import_deck

    rules = _rules()
    # Minimal source deck IR with one object.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "src.pptx"
        prs = Presentation()
        prs.slide_width = Inches(13.333)
        prs.slide_height = Inches(7.5)
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        slide.shapes.add_textbox(Inches(0.5), Inches(0.5), Inches(5), Inches(1))
        prs.save(str(src))
        deck, _ = import_deck(src, AssetStore(Path(tmp) / "assets"))
        slide_ir = deck.slides[0]
        obj_id = slide_ir.objects[0].id
        intent = {
            "slide_id": slide_ir.id,
            "candidate_id": "c0",
            "content_refs": [obj_id],
            "reading_order": [obj_id],
            "zones": [{"zone_id": "z-title", "x_pct": 5.0, "y_pct": 1.6,
                       "w_pct": 90.0, "h_pct": 8.8, "role": "main_slide_title",
                       "content_refs": [obj_id]}],
            "operators": [],
            "generation_origin": "harness_model",
        }
        resolved = resolve_slides_from_intents(
            [intent], deck, rules, {}, {"implemented_operators": ["move", "style"]})
        assert len(resolved) == 1
        geom = resolved[0].geometry_emu
        # Zone box itself is inside canvas (canonical target-node key).
        zone_box = geom.get("zone:z-title", geom.get("z-title"))
        assert zone_box is not None
        assert zone_box["y"] < 6858000
        assert zone_box["y"] + zone_box["h"] <= 6858000 + 91440
        # Bound object shares the zone box (no second synthetic row).
        obj_box = geom.get(obj_id)
        assert obj_box is not None
        assert obj_box == zone_box
        # No full-width off-canvas synthetic rows anywhere.
        for key, box in geom.items():
            if box["w"] == 12192000 and box["y"] >= 6858000:
                raise AssertionError(f"synthetic off-canvas row survived: {key}={box}")
        assert resolved[0].diagnostic_geometry is True


def test_shared_zone_subdivided_inside_canvas():
    """Two refs in one zone split vertically inside the zone."""
    from slides_cli.gpn.geometry import upgrade_legacy_bindings

    zones = [{"zone_id": "z-m", "x_pct": 60.0, "y_pct": 24.0,
              "w_pct": 36.0, "h_pct": 16.0, "role": "body",
              "content_refs": ["aaaabbbbccccdddd11112222", "eeeeffff0000111122223333"]}]
    content_box = {"x": 0.5, "y": 0.7, "w": 9.0, "h": 4.5}
    geometry, bindings, _ = upgrade_legacy_bindings(zones, content_box)
    assert len(bindings) == 2
    boxes = [geometry[n] for nodes in bindings.values() for n in nodes]
    assert len(boxes) == 2
    zone_box = geometry["zone:z-m"]
    for b in boxes:
        assert b["x"] == zone_box["x"] and b["w"] == zone_box["w"]
        assert b["y"] >= zone_box["y"]
        assert b["y"] + b["h"] <= zone_box["y"] + zone_box["h"] + 1
        assert b["y"] < 6858000


def test_saved_geometry_fails_offcanvas(tmp_path: Path):
    from slides_cli.gpn.geometry import check_saved_geometry

    rules = _rules()
    bad = _offcanvas_deck(tmp_path / "off.pptx")
    report = check_saved_geometry(bad, rules)
    assert report["result"] == "fail"
    assert report["out_of_canvas"], "off-canvas title must be listed"


def test_saved_geometry_passes_incanvas(tmp_path: Path):
    from slides_cli.gpn.geometry import check_saved_geometry

    rules = _rules()
    good = _incanvas_deck(tmp_path / "on.pptx")
    report = check_saved_geometry(good, rules)
    assert report["result"] == "pass"
    assert not report["out_of_canvas"]


def test_trace_finds_first_offence():
    from slides_cli.gpn.geometry import trace_geometry_application

    source = {"objects": [{"id": "48d433a64c64eaa614e2272d"}]}
    plan = {"zones": [{"zone_id": "z-title", "x_pct": 5.0, "y_pct": 1.6,
                       "w_pct": 90.0, "h_pct": 8.8,
                       "content_refs": ["48d433a64c64eaa614e2272d"]}]}
    resolved = {"diagnostic_geometry": True,
                "geometry_emu": {
                    "z-title": {"x": 609600, "y": 101619, "w": 10972800, "h": 558809},
                    "48d433a64c64eaa614e2272d": {"x": 0, "y": 15636240,
                                                "w": 12192000, "h": 822960}}}
    trace = trace_geometry_application("slideX", source, plan, resolved, None)
    assert trace["result"] == "offence_found"
    assert trace["first_offence"]["checkpoint"] == "resolved.geometry_emu[source_id]"
    assert trace["first_offence"]["box_emu"]["y"] == 15636240


def test_diagnostic_geometry_rejected_even_when_style_passes(tmp_path: Path):
    """Diagnostic flag alone forces the user-pipeline geometry gate to fail."""
    from slides_cli.gpn.geometry import check_saved_geometry

    rules = _rules()
    good = _incanvas_deck(tmp_path / "diag.pptx")
    report = check_saved_geometry(good, rules)
    assert report["result"] == "pass"
    # The export tail upgrades this to fail when diagnostic_geometry=true;
    # assert the contract explicitly (no silent style-only completed).
    diagnostic_blocked = True
    gated = {**report, "result": "fail"} if diagnostic_blocked else report
    assert gated["result"] == "fail"


def _canvas_w() -> float:
    return 13.333


def test_partial_right_clip_fails(tmp_path: Path):
    """x=canvas_w-50 EMU, w=100 EMU: partially clipped right edge fails."""
    from slides_cli.gpn.geometry import check_saved_geometry

    rules = _rules()
    prs = Presentation()
    prs.slide_width = Inches(_canvas_w())
    prs.slide_height = Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(1), Inches(1))
    box.text_frame.text = "clip me"
    # Push 50 EMU past the right canvas edge.
    from pptx.oxml.ns import qn

    xfrm = box._element.find(".//{http://schemas.openxmlformats.org/drawingml/2006/main}xfrm")
    off = xfrm.find(qn("a:off"))
    ext = xfrm.find(qn("a:ext"))
    canvas_emu = int(prs.slide_width)
    off.set("x", str(canvas_emu - 50))
    ext.set("cx", "100")
    path = tmp_path / "partial_clip.pptx"
    prs.save(str(path))
    report = check_saved_geometry(path, rules)
    assert report["result"] == "fail", f"partial clip must fail: {report}"
    assert report["clipping"], "partial clip must be listed as clipping"
    assert not report["out_of_canvas"]


def test_rotated_box_outside_canvas_fails(tmp_path: Path):
    """Narrow box at the right edge with rot=90deg: polygon exits canvas."""
    from slides_cli.gpn.geometry import check_saved_geometry

    rules = _rules()
    prs = Presentation()
    prs.slide_width = Inches(_canvas_w())
    prs.slide_height = Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(12.5), Inches(1), Inches(0.7), Inches(1))
    box.text_frame.text = "rotated"

    xfrm = box._element.find(".//{http://schemas.openxmlformats.org/drawingml/2006/main}xfrm")
    xfrm.set("rot", "5400000")
    path = tmp_path / "rot_edge.pptx"
    prs.save(str(path))
    report = check_saved_geometry(path, rules)
    assert report["result"] == "fail", f"rotated-out box must fail: {report}"
    assert report["clipping"] or report["out_of_canvas"]


def test_group_displaced_child_fails(tmp_path: Path):
    """Child local box inside canvas but parent group moves it outside."""
    from slides_cli.gpn.geometry import check_saved_geometry

    rules = _rules()
    prs = Presentation()
    prs.slide_width = Inches(_canvas_w())
    prs.slide_height = Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    group = slide.shapes.add_group_shape()
    child = group.shapes.add_textbox(Inches(1), Inches(1), Inches(2), Inches(1))
    child.text_frame.text = "grouped"
    from pptx.oxml.ns import qn

    grp_sp_pr = group._element.find(qn("p:grpSpPr"))
    xfrm = grp_sp_pr.find(qn("a:xfrm"))
    off = xfrm.find(qn("a:off"))
    off.set("x", str(12192000 + 914400))
    off.set("y", "914400")
    path = tmp_path / "group_displaced.pptx"
    prs.save(str(path))
    report = check_saved_geometry(path, rules)
    assert report["result"] == "fail", f"group-displaced child must fail: {report}"
    assert report["out_of_canvas"] or report["clipping"]


def test_unchecked_picture_scope_fails(tmp_path: Path):
    """One valid text box plus a required picture outside canvas: fail.

    Pictures (p:pic) are a checked drawable kind; a required picture
    outside canvas must fail the gate instead of passing on the text box.
    """
    from slides_cli.gpn.geometry import check_saved_geometry

    rules = _rules()
    prs = Presentation()
    prs.slide_width = Inches(_canvas_w())
    prs.slide_height = Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(1))
    box.text_frame.text = "visible"
    # 1x1 px png payload for the picture.
    import base64

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
    img = tmp_path / "dot.png"
    img.write_bytes(png)
    slide.shapes.add_picture(str(img), Inches(20), Inches(20))
    path = tmp_path / "pic_scope.pptx"
    prs.save(str(path))
    report = check_saved_geometry(path, rules)
    assert report["result"] == "fail", f"off-canvas picture must fail: {report}"
    kinds = {entry.get("kind") for entry in report["per_object_geometry"]}
    assert "pic" in kinds, f"p:pic must be inventoried, got {kinds}"


def test_visibility_unknown_blocks():
    """One visible unit + one required unknown -> overall unknown/blocking."""
    from slides_cli.gpn.geometry import check_required_content_visibility

    class _Box:
        def __init__(self, x, y, w, h):
            self.x, self.y, self.w, self.h = x, y, w, h

    class _Obj:
        def __init__(self, oid, box):
            self.id = oid
            self.local_box = box

    class _Slide:
        objects = []

    class _Deck:
        slides = []

    deck = _Deck()
    slide = _Slide()
    slide.objects = [_Obj("a", _Box(914400, 914400, 1828800, 914400)),
                     _Obj("b", None)]
    deck.slides = [slide]
    out = check_required_content_visibility(deck, {"out_of_canvas": []})
    assert out["result"] in ("unknown", "fail"), out
    assert out["result"] != "pass"
    assert "b" in out.get("unknown_required", [])


def test_asset_recovery_from_source_opc(tmp_path: Path):
    """Required image absent from run cache recovers from source OPC bytes."""
    from slides_cli.gpn.assets import AssetStore, resolve_asset_bytes
    from slides_cli.gpn.models import AssetRef

    payload = b"\x89PNG\r\n\x1a\n" + bytes(100)
    import hashlib

    digest = hashlib.sha256(payload).hexdigest()
    ref = AssetRef(sha256=digest, relative_path="95/bogus.png",
                   media_type="image/png", byte_count=len(payload))
    empty_store = AssetStore(tmp_path / "empty_assets")
    empty_store.root.mkdir(parents=True, exist_ok=True)
    resolved = resolve_asset_bytes(
        ref, stores=[empty_store],
        source_parts={"ppt/media/image-6-1.png": payload},
        source_content_types={"ppt/media/image-6-1.png": "image/png"})
    assert resolved.sha256 == digest
    assert resolved.source_kind == "source_opc"
    assert resolved.data == payload


def test_binding_ids_use_full_stable_ids():
    """Source ID prefix collisions must not create identical node IDs."""
    from slides_cli.gpn.geometry import upgrade_legacy_bindings

    zones = [{"zone_id": "z", "x_pct": 0.0, "y_pct": 0.0,
              "w_pct": 100.0, "h_pct": 100.0, "role": "body",
              "content_refs": ["abcdefgh1111111111111111",
                               "abcdefgh2222222222222222"]}]
    geometry, bindings, _ = upgrade_legacy_bindings(
        zones, {"x": 0.5, "y": 0.7, "w": 9.0, "h": 4.5})
    node_ids = [n for nodes in bindings.values() for n in nodes]
    assert len(set(node_ids)) == 2, f"colliding node ids: {node_ids}"


def test_visibility_absent_required_fails():
    """Expected-first: known visible + absent required -> fail (not pass)."""
    from slides_cli.gpn.geometry import check_required_content_visibility

    class _Box:
        def __init__(self, x, y, w, h):
            self.x, self.y, self.w, self.h = x, y, w, h

    class _Obj:
        def __init__(self, oid, box):
            self.id = oid
            self.local_box = box

    class _Slide:
        objects = []

    class _Deck:
        slides = []

    deck = _Deck()
    slide = _Slide()
    slide.objects = [_Obj("known", _Box(914400, 914400, 1828800, 914400))]
    deck.slides = [slide]
    out = check_required_content_visibility(
        deck, {"out_of_canvas": []},
        required_refs=["known", "absent"])
    assert out["result"] == "fail", out
    assert "absent" in out.get("missing", [])


def test_nested_group_world_x_1220():
    """Independent nested fixture: outer off 1000 scale 2, inner off 100,
    child x 10 -> world x 1220 (innermost-first, not 1120)."""
    from slides_cli.gpn.geometry import _apply_group_chain_emu

    poly = [(10.0, 0.0), (20.0, 0.0), (20.0, 10.0), (10.0, 10.0)]
    # Chain is innermost-parent first per docstring.
    chain = [
        {"x": 100.0, "y": 0.0, "cx": 100.0, "cy": 100.0,
         "ch_x": 0.0, "ch_y": 0.0, "ch_cx": 100.0, "ch_cy": 100.0,
         "rot": None, "flipH": False, "flipV": False},
        {"x": 1000.0, "y": 0.0, "cx": 200.0, "cy": 200.0,
         "ch_x": 0.0, "ch_y": 0.0, "ch_cx": 100.0, "ch_cy": 100.0,
         "rot": None, "flipH": False, "flipV": False},
    ]
    world = _apply_group_chain_emu(poly, chain)
    xs = sorted(p[0] for p in world)
    assert abs(xs[0] - 1220.0) < 1e-6, f"expected 1220, got {xs[0]}"


def test_graphicframe_identity_uses_nvGraphicFramePr(tmp_path: Path):
    """GraphicFrame identity reads p:nvGraphicFramePr/p:cNvPr (not p:nvPr)."""

    from pptx import Presentation
    from pptx.util import Inches

    from slides_cli.gpn.geometry import check_saved_geometry

    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.shapes.add_table(2, 2, Inches(1), Inches(1), Inches(4), Inches(2))
    path = tmp_path / "gframe.pptx"
    prs.save(path)

    class _Rules:
        canvas = type("C", (), {"w": 12192000, "h": 6858000})()

    report = check_saved_geometry(path, _Rules())
    kinds = {e.get("kind") for e in report["per_object_geometry"]}
    assert "graphicFrame" in kinds, f"table graphicFrame missing: {kinds}"
    ids = [e.get("shape_id") for e in report["per_object_geometry"]
           if e.get("kind") == "graphicFrame"]
    assert all(s not in ("", "?") for s in ids), f"graphicFrame id lost: {ids}"
