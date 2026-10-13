"""Stage 5 measurement bridge tests (spec §§5, 11.2, 13.1).

Integration tests drive the real installed Chromium with exact local font
bytes. Unit tests cover protocol integrity, UTF-16 mapping and units without
a browser. Fixtures are synthetic; the user corpus is read-only.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import pytest

from slides_cli.gpn.measurement import (
    codepoint_to_utf16,
    measure_components,
    split_run_by_fragments,
    utf16_to_codepoint,
    utf16_units,
)
from slides_cli.gpn.models import MeasurementRequest

PROJECT_ROOT = Path(__file__).resolve().parents[3]


@lru_cache(maxsize=1)
def _bundle() -> dict:
    from slides_cli.gpn.typography import (
        build_font_inventory_from_discovery,
        discover_local_fonts,
        prepare_measurement_fonts,
    )

    report = discover_local_fonts(None)
    inv = build_font_inventory_from_discovery(
        report, {"GPN_DIN Condensed Bold", "GPN_DIN Regular",
                 "GPN_DIN Condensed Regular"})
    assert inv.missing_faces == [], f"pilot needs installed GPN fonts: {inv.missing_faces}"
    bundle = prepare_measurement_fonts(inv, "", profile_kind="production")
    assets = {b.browser_alias: {"path": b.path, "sha256": b.sha256}
              for b in bundle.bindings if not b.role_or_face.startswith("marker:")}
    return {"bold": _alias_for(bundle, "GPN_DIN Condensed Bold"),
            "regular": _alias_for(bundle, "GPN_DIN Regular"),
            "assets": assets}


def _alias_for(bundle, face: str) -> str:
    for b in bundle.bindings:
        if b.role_or_face == face:
            return b.browser_alias
    raise AssertionError(f"no bundle binding for {face}")


def _style(alias: str, size: float, **kw) -> dict:
    style = {"face_alias": alias, "size_pt": size, "bold": False,
             "italic": False, "underline": "", "color_rgb": "404040",
             "language": "ru", "baseline_shift": "none", "uppercase": False}
    style.update(kw)
    return style


def _para(pid: str, runs: list[dict], **kw) -> dict:
    para = {"id": pid, "runs": runs, "space_before_pt": 0.0,
            "space_after_pt": 0.0, "line_spacing_mode": "multiple",
            "line_spacing_value": 1.0, "alignment": "left", "level": 0,
            "list_kind": "none", "first_line_indent_pt": 0.0,
            "left_indent_pt": 0.0}
    para.update(kw)
    return para


def _run(rid: str, text: str, style: dict) -> dict:
    return {"id": rid, "text": text, "style": style}


def _request(cid: str, paragraphs: list[dict], width_pt: float,
             height_pt: float = 400.0, **kw) -> MeasurementRequest:
    b = _bundle()
    font_assets = [
        {"alias": alias, "path": info["path"], "sha256": info["sha256"],
         "face_index": None, "style": "normal", "weight": 400}
        for alias, info in b["assets"].items()
    ]
    comp = {"id": cid, "kind": "text", "payload_ref": f"test:{cid}",
            "paragraphs": paragraphs,
            "box_pt": {"x": 36.0, "y": 36.0, "w": width_pt, "h": height_pt},
            "margins_pt": {"l": 7.2, "r": 7.2, "t": 3.6, "b": 3.6},
            "vertical_anchor": "top", "wrap_policy": "wrap",
            "rotation_deg": 0.0, "list_preview": {}}
    comp.update(kw)
    return MeasurementRequest(
        schema_version="1.0", request_id=f"test-{cid}",
        canvas_pt={"w": 960.0, "h": 540.0}, css_px_per_in=96.0,
        profile_kind="production", components=[comp],
        font_assets=font_assets,
        engine_options={"timeout_ms": 60000, "device_scale_factor": 1,
                        "allowlist_roots": ["/Users/wysockii/Library/Fonts/",
                                            "/System/Library/Fonts/"]},
        input_hashes={}, debug_output_dir="")


def _measure(req: MeasurementRequest, tmp_path: Path) -> dict:
    out = measure_components(req, timeout_seconds=90.0,
                             project_root=PROJECT_ROOT, use_cache=False,
                             debug_output_dir=str(tmp_path / "debug"))
    assert out["cache_hit"] is False
    return out["response"]


# ---------------------------------------------------------------------------
# Units + UTF-16 mapping (no browser)
# ---------------------------------------------------------------------------

def test_units_pt_px_emu() -> None:
    """Independent physical-size calculation: 72pt = 1in = 96px = 914400 EMU,
    single rounding through the shared units module."""
    from slides_cli.gpn.units import (
        css_px_to_pt,
        emu_to_pt,
        pt_to_css_px,
        pt_to_emu,
    )

    assert pt_to_css_px(72.0, 96.0) == 96.0
    assert css_px_to_pt(96.0, 96.0) == 72.0
    assert pt_to_emu(72.0) == 914400
    assert emu_to_pt(914400) == 72.0
    assert emu_to_pt(pt_to_emu(10.5)) == pytest.approx(10.5)


def test_utf16_source_mapping() -> None:
    """Python codepoints and DOM UTF-16 offsets agree on non-BMP tokens
    (emoji, combining marks, NBSP, Cyrillic, ₽)."""
    text = "Добыча ₽ \U0001F600e\u0301"
    units = utf16_units(text)
    assert units[text.index("\U0001F600")] == 2
    assert sum(units) == len(text.encode("utf-16-le")) // 2
    for cp in range(len(text) + 1):
        u16 = codepoint_to_utf16(text, cp)
        assert utf16_to_codepoint(text, u16) == cp
    emoji_cp = text.index("\U0001F600")
    assert codepoint_to_utf16(text, emoji_cp + 1) - codepoint_to_utf16(text, emoji_cp) == 2
    assert utf16_to_codepoint(text, codepoint_to_utf16(text, emoji_cp) + 1) == emoji_cp


# ---------------------------------------------------------------------------
# Integration: real Chromium + exact GPN bytes
# ---------------------------------------------------------------------------

def test_real_multiline_measurement(tmp_path: Path) -> None:
    """Narrowing the box changes line count and occupied height (real engine,
    not a mock): the same text wraps differently at 400pt vs 150pt."""
    b = _bundle()
    text = "Динамика выручки по кварталам две тысячи двадцать шестого года"
    wide = _request("wide", [_para("p0", [_run("r0", text, _style(b["bold"], 18.0))])], 400.0)
    narrow = _request("narrow", [_para("p0", [_run("r0", text, _style(b["bold"], 18.0))])], 150.0)
    wide_item = _measure(wide, tmp_path)["items"][0]
    narrow_item = _measure(narrow, tmp_path)["items"][0]
    assert wide_item["confidence"] == "exact_browser"
    assert narrow_item["line_count"] > wide_item["line_count"]
    assert narrow_item["occupied_height_pt"] > wide_item["occupied_height_pt"]
    assert narrow_item["overflow_x"] is False


def test_mixed_runs_fields_unicode(tmp_path: Path) -> None:
    """Cyrillic/₽/§/NBSP/superscript/soft-break survive with run identity;
    fragments reconstruct the exact source strings (no trim, no collapse)."""
    b = _bundle()
    run_texts = ["Выручка 120,5 ₽", " § ", "13", "первая\u2009строка\nвторая"]
    styles = [_style(b["regular"], 14.0), _style(b["regular"], 14.0),
              _style(b["regular"], 9.0, baseline_shift="superscript"),
              _style(b["regular"], 14.0)]
    runs = [_run(f"r{i}", t, s) for i, (t, s) in enumerate(zip(run_texts, styles, strict=True))]
    req = _request("mixed", [_para("p0", runs)], 400.0)
    item = _measure(req, tmp_path)["items"][0]
    by_run: dict[str, list[dict]] = {}
    for f in item["fragments"]:
        by_run.setdefault(f["run_id"], []).append(f)
    for i, text in enumerate(run_texts):
        frags = sorted(by_run[f"r{i}"], key=lambda f: f["utf16_start"])
        assert "".join(split_run_by_fragments(text, frags)) == text


def test_empty_paragraph_end_style(tmp_path: Path) -> None:
    """Empty and trailing paragraphs occupy styled height, never vanish."""
    b = _bundle()
    req = _request("empty", [
        _para("p0", [_run("r0", "Заголовок", _style(b["bold"], 18.0))]),
        _para("p1", []),
        _para("p2", [_run("r2", "Хвост", _style(b["regular"], 12.0))]),
        _para("p3", []),
    ], 400.0)
    item = _measure(req, tmp_path)["items"][0]
    assert item["paragraph_count"] == 4
    pids = {f["paragraph_id"] for f in item["fragments"]}
    assert {"p0", "p1", "p2", "p3"} <= pids
    assert item["occupied_height_pt"] > 0


def test_paragraph_spacing_modes(tmp_path: Path) -> None:
    """Points vs multiple spacing are distinct representations with distinct
    measured heights (no universal 1.2 line-height)."""
    b = _bundle()

    def req_with(spacing: dict, cid: str) -> MeasurementRequest:
        paras = [_para("p0", [_run("r0", "Строка один", _style(b["regular"], 14.0))], **spacing),
                 _para("p1", [_run("r1", "Строка два", _style(b["regular"], 14.0))], **spacing)]
        return _request(cid, paras, 400.0)

    pts = _measure(req_with({"space_before_pt": 6.0, "space_after_pt": 6.0,
                             "line_spacing_mode": "points",
                             "line_spacing_value": 20.0}, "pts"), tmp_path)["items"][0]
    mult = _measure(req_with({"space_before_pt": 0.0, "space_after_pt": 0.0,
                              "line_spacing_mode": "multiple",
                              "line_spacing_value": 1.0}, "mult"), tmp_path)["items"][0]
    assert pts["occupied_height_pt"] > mult["occupied_height_pt"]
    assert pts["occupied_height_pt"] > 0 and mult["occupied_height_pt"] > 0


def test_horizontal_unbreakable_overflow(tmp_path: Path) -> None:
    """A long NBSP-joined token overflows x independently of y fit."""
    b = _bundle()
    token = "Неразрывный" + "\u00a0супердлинный\u00a0токен\u00a0без\u00a0разрывов" * 3
    req = _request("nbsp", [_para("p0", [_run("r0", token, _style(b["regular"], 14.0))])], 120.0)
    item = _measure(req, tmp_path)["items"][0]
    assert item["overflow_x"] is True
    assert item["excess_pt"]["x"] > 0
    assert any("overflow_x" in i for i in item["issues"])


def test_vertical_natural_height_overflow(tmp_path: Path) -> None:
    """A fixed-height container never masks lower lines (natural height)."""
    b = _bundle()
    text = " ".join(f"строка{i}" for i in range(12))
    req = _request("tall", [_para("p0", [_run("r0", text, _style(b["regular"], 14.0))])],
                   300.0, height_pt=30.0)
    item = _measure(req, tmp_path)["items"][0]
    assert item["overflow_y"] is True
    assert item["excess_pt"]["y"] > 0
    assert item["line_count"] > 1


def test_title_transform_measured_and_exported(tmp_path: Path) -> None:
    """Uppercase is measured as the actual output view; source bytes behind
    the fragments keep their original case (transform is a view manifest)."""
    b = _bundle()
    req = _request("title", [_para(
        "p0", [_run("r0", "Состав поставки", _style(b["bold"], 24.0, uppercase=True))])], 400.0)
    item = _measure(req, tmp_path)["items"][0]
    assert "uppercase" in item["applied_transforms"]
    frags = sorted(
        [f for f in item["fragments"] if f["run_id"] == "r0"],
        key=lambda f: f["utf16_start"])
    assert "".join(split_run_by_fragments(
        "Состав поставки", frags)) == "Состав поставки"


def test_list_indent_continuation_marker(tmp_path: Path) -> None:
    """Hanging indent: first-line text origin shifts by the indent while
    continuation lines align with it; the Wingdings square marker envelope
    is measurable on exact bytes (never a placeholder claim)."""
    b = _bundle()
    req = _request("list", [
        _para("p0", [_run("r0", "Первый пункт с достаточно длинным текстом "
                               "для переноса на вторую строку",
                          _style(b["regular"], 12.0))],
              list_kind="bullet", level=0, left_indent_pt=18.0,
              first_line_indent_pt=-9.0),
        _para("p1", [_run("r1", "Второй пункт", _style(b["regular"], 12.0))],
              list_kind="bullet", level=0, left_indent_pt=18.0,
              first_line_indent_pt=-9.0),
    ], 250.0)
    item = _measure(req, tmp_path)["items"][0]
    p0_lines: dict[int, list[dict]] = {}
    for f in item["fragments"]:
        if f["paragraph_id"] == "p0":
            p0_lines.setdefault(f["line_index"], []).append(f)
    assert len(p0_lines) >= 2
    first_x = min(f["logical_rect_pt"]["x"] for f in p0_lines[min(p0_lines)])
    cont_x = min(f["logical_rect_pt"]["x"] for f in p0_lines[max(p0_lines)])
    assert cont_x > first_x
    assert item["text_origin_x_pt"] > 0


def test_model_payload_cannot_inject_html_css(tmp_path: Path) -> None:
    """Script/URL/CSS in the payload stays data: fragments reconstruct the
    exact input string and no external request can leave the offline page."""
    b = _bundle()
    evil = ('<script>alert(1)</script> https://example.invalid/x '
            '<style>body{display:none}</style> & "quotes"')
    req = _request("evil", [_para("p0", [_run("r0", evil, _style(b["regular"], 12.0))])], 400.0)
    item = _measure(req, tmp_path)["items"][0]
    frags = sorted(
        [f for f in item["fragments"] if f["run_id"] == "r0"],
        key=lambda f: f["utf16_start"])
    assert "".join(split_run_by_fragments(evil, frags)) == evil
    assert item["overflow_x"] is False


def test_font_check_true_is_not_font_evidence(tmp_path: Path) -> None:
    """A pattern-valid alias with no bound asset is a protocol refusal, not
    a verified measurement (live DOM fonts.check would return true for the
    nonexistent family, which proves it is not evidence)."""
    req = _request("ghost", [_para(
        "p0", [_run("r0", "Текст", _style("MEASURE_FACE_deadbeefcafe", 12.0))])], 400.0)
    req = req.model_copy(update={"request_id": "test-ghost"})
    import subprocess
    import tempfile

    from slides_cli.gpn.measurement import measure_tool_path, node_binary

    with tempfile.TemporaryDirectory() as tmp:
        req_path = Path(tmp) / "request.json"
        resp_path = Path(tmp) / "response.json"
        req_path.write_text(req.model_dump_json(), encoding="utf-8")
        proc = subprocess.run(
            [node_binary(), str(measure_tool_path()), str(req_path), str(resp_path)],
            capture_output=True, text=True, timeout=60)
        assert proc.returncode == 2
        resp = json.loads(resp_path.read_text(encoding="utf-8"))
        assert resp["items"] == []
        assert any("unknown face alias" in i for i in resp["issues"])


def test_measurement_browser_missing_or_crash() -> None:
    """Missing node binary and missing tool are typed actionable errors;
    neither becomes a zero-size pass or a cache success."""
    import slides_cli.gpn.measurement as m
    from slides_cli.gpn.measurement import MeasurementError, measure_tool_path

    b = _bundle()
    req = _request("nocrash", [_para(
        "p0", [_run("r0", "Текст", _style(b["regular"], 12.0))])], 400.0)
    orig = m.node_binary
    m.node_binary = lambda: None
    try:
        with pytest.raises(MeasurementError) as exc:
            measure_components(req, timeout_seconds=30.0,
                               project_root=PROJECT_ROOT, use_cache=False)
        assert exc.value.code == "MEASUREMENT_NO_NODE"
    finally:
        m.node_binary = orig
    assert measure_tool_path().is_file()


def test_measurement_protocol_integrity() -> None:
    """NaN geometry is rejected at the bridge; response/id/hash mismatches
    are rejected on the Python side (no extra item replaces a missing one)."""
    import subprocess
    import tempfile

    import pydantic

    from slides_cli.gpn.measurement import (
        _validate_response,
        measure_tool_path,
        node_binary,
    )
    from slides_cli.gpn.models import MeasurementResponse

    b = _bundle()
    req = _request("nan", [_para("p0", [_run("r0", "Текст", _style(b["regular"], 12.0))])], 400.0)
    bad = req.model_dump(mode="json")
    bad["components"][0]["box_pt"]["w"] = float("nan")
    # Python boundary rejects non-finite geometry before any spawn.
    with pytest.raises(pydantic.ValidationError):
        MeasurementRequest.model_validate(bad)
    # Malformed JSON never becomes a measurement (fatal, no zero-size pass).
    with tempfile.TemporaryDirectory() as tmp:
        req_path = Path(tmp) / "request.json"
        resp_path = Path(tmp) / "response.json"
        req_path.write_text('{"schema_version": "1.0", "broken": NaN}',
                            encoding="utf-8")
        proc = subprocess.run(
            [node_binary(), str(measure_tool_path()), str(req_path), str(resp_path)],
            capture_output=True, text=True, timeout=60)
        assert proc.returncode == 3

    good = MeasurementResponse(schema_version="1.0", request_id="test-nan",
                               request_hash="h", engine={},
                               font_load={"faces": [], "all_loaded": True, "issues": []},
                               items=[], issues=[])
    assert _validate_response(req, good, "h") != []


def test_measurement_timeout_cleanup(tmp_path: Path) -> None:
    """An impossible timeout is a typed error with checkpoint-safe cleanup:
    the browser/process is closed and no temp-request debris is trusted."""
    import glob
    import tempfile

    from slides_cli.gpn.measurement import MeasurementError

    b = _bundle()
    req = _request("slow", [_para("p0", [_run("r0", "Текст", _style(b["regular"], 12.0))])], 400.0)
    before = set(glob.glob(str(Path(tempfile.gettempdir()) / "gpn-measure-*")))
    with pytest.raises(MeasurementError) as exc:
        measure_components(req, timeout_seconds=0.001,
                           project_root=PROJECT_ROOT, use_cache=False)
    assert exc.value.code == "MEASUREMENT_TIMEOUT"
    after = set(glob.glob(str(Path(tempfile.gettempdir()) / "gpn-measure-*")))
    assert after <= before


def test_font_hash_invalidates_measurement(tmp_path: Path) -> None:
    """Different exact bytes never reuse previous metrics: the cache key
    changes and the second run is a verified miss with its own evidence."""
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    from slides_cli.gpn.measurement import cache_key_hash, cache_key_parts

    def _variant(path: Path, advance: int) -> None:
        fb = FontBuilder(1000, isTTF=True)
        order = [".notdef", "A"]
        fb.setupGlyphOrder(order)
        fb.setupCharacterMap({ord("A"): "A"})
        glyphs = {}
        for name in order:
            pen = TTGlyphPen(None)
            pen.moveTo((0, 0))
            pen.lineTo((500, 0))
            pen.lineTo((500, 700))
            pen.closePath()
            glyphs[name] = pen.glyph()
        fb.setupGlyf(glyphs)
        fb.setupHorizontalMetrics({".notdef": (600, 0), "A": (advance, 0)})
        fb.setupHorizontalHeader(ascent=800, descent=200)
        fb.setupNameTable({"familyName": "CacheFam", "styleName": "Regular",
                           "fullName": "CacheFam Regular",
                           "uniqueFontIdentifier": "CacheFam-Regular",
                           "psName": "CacheFam-Regular"})
        fb.setupOS2()
        fb.setupPost()
        fb.save(str(path))

    import hashlib

    fa = tmp_path / "a.ttf"
    fb_path = tmp_path / "b.ttf"
    _variant(fa, 600)
    _variant(fb_path, 900)
    ha = hashlib.sha256(fa.read_bytes()).hexdigest()
    hb = hashlib.sha256(fb_path.read_bytes()).hexdigest()
    assert ha != hb

    def req_for(path: Path, digest: str, cid: str) -> MeasurementRequest:
        st = _style(f"MEASURE_FACE_{digest[:12]}", 20.0)
        comp = {"id": cid, "kind": "text", "payload_ref": f"test:{cid}",
                "paragraphs": [_para("p0", [_run("r0", "AAAA", st)])],
                "box_pt": {"x": 36.0, "y": 36.0, "w": 400.0, "h": 400.0},
                "margins_pt": {}, "vertical_anchor": "top",
                "wrap_policy": "wrap", "rotation_deg": 0.0, "list_preview": {}}
        return MeasurementRequest(
            schema_version="1.0", request_id=f"test-{cid}",
            canvas_pt={"w": 960.0, "h": 540.0}, css_px_per_in=96.0,
            profile_kind="test_only", components=[comp],
            font_assets=[{"alias": f"MEASURE_FACE_{digest[:12]}",
                          "path": str(path), "sha256": digest,
                          "face_index": None, "style": "normal",
                          "weight": 400}],
            engine_options={"timeout_ms": 60000, "device_scale_factor": 1,
                            "allowlist_roots": [str(tmp_path)]},
            input_hashes={}, debug_output_dir="")

    ra = req_for(fa, ha, "cka")
    rb = req_for(fb_path, hb, "ckb")
    ka = cache_key_hash(cache_key_parts(ra))
    kb = cache_key_hash(cache_key_parts(rb))
    assert ka != kb
    outa = measure_components(ra, timeout_seconds=90.0,
                              project_root=PROJECT_ROOT, use_cache=True)
    outb = measure_components(rb, timeout_seconds=90.0,
                              project_root=PROJECT_ROOT, use_cache=True)
    assert outa["cache_hit"] is False
    assert outb["cache_hit"] is False
    wa = outa["response"]["items"][0]["intrinsic_preferred_width_pt"]
    wb = outb["response"]["items"][0]["intrinsic_preferred_width_pt"]
    assert wb > wa


def test_harness_default_no_local_gate(tmp_path: Path) -> None:
    """Measurement runs with no model server, API key or local endpoint:
    the bridge is LLM-independent by construction (default harness mode)."""
    import os

    for var in [v for v in os.environ if "API" in v.upper() or "MODEL" in v.upper()]:
        del os.environ[var]
    b = _bundle()
    req = _request("nogate", [_para(
        "p0", [_run("r0", "Без модели", _style(b["regular"], 12.0))])], 400.0)
    item = _measure(req, tmp_path)["items"][0]
    assert item["confidence"] == "exact_browser"
    assert item["issues"] == []

def test_zero_extent_connector_measures_clean(tmp_path: Path) -> None:
    """A zero-height line box is valid geometry (Stage 4), not a protocol
    error: the bridge returns a measured item with no overflow instead of
    rejecting the request or dropping the component."""
    req = _request("zero-line", [], 400.0, height_pt=0.0,
                   margins_pt={})
    item = _measure(req, tmp_path)["items"][0]
    assert item["component_id"] == "zero-line"
    assert item["overflow_x"] is False
    assert item["overflow_y"] is False
    assert item["confidence"] == "exact_browser"
