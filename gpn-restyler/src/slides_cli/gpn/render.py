"""Stage 5 §12.3-12.4: native PPTX render probe + browser/native calibration.

``render_native_candidate`` renders the FINAL checked candidate bytes through
the local LibreOffice renderer into a PDF, extracts page images (poppler),
text and font inventory, and reports what was actually verified — never
turning a successful browser preview into a claimed native render.

``calibrate_measurement`` builds a small technical fixture from the live
ontology + exact faces, measures it in the browser and in the native renderer,
and records a conservative per-case safety envelope. The fixture is
``test_only`` by construction; it is never a design reference.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .models import (
    CalibrationProfile,
    CalibrationSample,
    NativeRenderProbe,
    RenderFontEvidence,
)

RENDERER = "libreoffice"


def _which(*names: str) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def find_soffice() -> str | None:
    return _which("soffice") or (
        "/Applications/LibreOffice.app/Contents/MacOS/soffice"
        if Path("/Applications/LibreOffice.app/Contents/MacOS/soffice").is_file()
        else None)


def soffice_version(binary: str) -> str:
    try:
        proc = subprocess.run([binary, "--version"], capture_output=True,
                              text=True, timeout=60)
        return (proc.stdout or proc.stderr).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _norm_ps(name: str) -> str:
    """Normalize a PostScript-like font name for comparison."""
    cleaned = re.sub(r"^[A-Z]{6}\+", "", name or "")
    return re.sub(r"[^a-z0-9]", "", cleaned.lower())


def candidate_typefaces(candidate_path: str | Path) -> list[str]:
    """Declared latin typefaces in the FINAL saved slide XML.

    The honest expected-face set for the render probe: what the exporter
    actually wrote (run-level rPr + theme defaults), not what a bundle
    could bind. Raises on unreadable packages.
    """
    import zipfile

    from lxml import etree

    faces: set[str] = set()
    with zipfile.ZipFile(candidate_path) as zf:
        names = [n for n in zf.namelist()
                 if n.startswith("ppt/slides/slide")
                 and n.endswith(".xml")]
        for name in names:
            root = etree.fromstring(zf.read(name))
            for latin in root.findall(
                    ".//{http://schemas.openxmlformats.org/drawingml/2006/main}latin"):
                face = latin.get("typeface")
                if face:
                    faces.add(face)
    return sorted(faces)


def render_native_candidate(
    candidate_path: str | Path,
    options: dict[str, Any] | None = None,
    fonts: Any = None,
) -> NativeRenderProbe:
    """Render the final candidate natively and report scoped evidence."""
    options = dict(options or {})
    candidate = Path(candidate_path)
    probe = NativeRenderProbe()
    if not candidate.is_file():
        probe.result = "unknown"
        probe.issues.append(f"candidate missing: {candidate}")
        return probe
    probe.candidate_path = str(candidate)
    probe.candidate_hash = _sha256(candidate)

    soffice = find_soffice()
    if soffice is None:
        probe.result = "unknown"
        probe.support_limits.append("libreoffice_not_available")
        probe.issues.append("soffice not found; native render skipped")
        return probe
    probe.renderer = RENDERER
    probe.renderer_version = soffice_version(soffice)

    pdftoppm = _which("pdftoppm")
    pdftotext = _which("pdftotext")
    pdffonts = _which("pdffonts")
    pdfinfo = _which("pdfinfo")

    expected_pages = int(options.get("expected_page_count", 0) or 0)
    timeout = float(options.get("timeout_seconds", 240.0))
    dpi = int(options.get("dpi", 96))
    out_dir = Path(options.get("output_dir", candidate.parent / "render"))
    out_dir.mkdir(parents=True, exist_ok=True)

    expected_faces = list(options.get("expected_faces", []) or [])
    if not expected_faces and fonts is not None:
        expected_faces = [b.internal_name for b in
                          (getattr(fonts, "bindings", []) or [])
                          if getattr(b, "internal_name", "")]
    probe.expected_faces = expected_faces

    with tempfile.TemporaryDirectory(prefix="gpn-render-") as tmp:
        tmp_path = Path(tmp)
        profile = tmp_path / "lo-profile"
        profile.mkdir()
        cmd = [
            soffice, "--headless", "--norestore",
            f"-env:UserInstallation=file://{profile}",
            "--convert-to", "pdf", "--outdir", str(tmp_path), str(candidate),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            probe.result = "fail"
            probe.issues.append(f"renderer timeout after {timeout}s")
            probe.support_limits.append("renderer_timeout")
            return probe
        except OSError as exc:
            probe.result = "unknown"
            probe.issues.append(f"renderer spawn failed: {exc}")
            return probe
        pdf_src = tmp_path / (candidate.stem + ".pdf")
        if proc.returncode != 0 or not pdf_src.is_file():
            probe.result = "fail"
            probe.issues.append(
                f"conversion failed exit={proc.returncode}: "
                f"{(proc.stderr or '')[-400:]}")
            return probe
        pdf_path = out_dir / "candidate.pdf"
        shutil.copy2(pdf_src, pdf_path)
        probe.rendered = True
        probe.pdf_path = str(pdf_path)
        probe.pdf_sha256 = _sha256(pdf_path)

        if pdfinfo is not None:
            info = ""
            with contextlib.suppress(OSError, ValueError,
                                     subprocess.SubprocessError):
                info = subprocess.run([pdfinfo, str(pdf_path)],
                                      capture_output=True, text=True,
                                      timeout=60).stdout
            for line in info.splitlines():
                if line.lower().startswith("pages:"):
                    with contextlib.suppress(ValueError):
                        probe.page_count = int(
                            line.split(":", 1)[1].strip() or 0)
        probe.expected_page_count = expected_pages

        if pdftoppm is not None:
            prefix = out_dir / "page"
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                subprocess.run(
                    [pdftoppm, "-png", "-r", str(dpi), str(pdf_path),
                     str(prefix)],
                    capture_output=True, text=True, timeout=timeout)
            probe.page_images = sorted(
                str(p) for p in out_dir.glob("page-*.png"))
        else:
            probe.support_limits.append("pdftoppm_not_available")

        if pdftotext is not None:
            try:
                text = subprocess.run(
                    [pdftotext, str(pdf_path), "-"],
                    capture_output=True, text=True, timeout=timeout).stdout
                probe.text_spans = sum(
                    1 for t in text.split() if t.strip())
                probe.checked_scopes.append("text_extracted")
            except (OSError, subprocess.SubprocessError):
                text = ""
        else:
            text = ""
            probe.support_limits.append("pdftotext_not_available")

        if pdffonts is not None:
            try:
                raw = subprocess.run(
                    [pdffonts, str(pdf_path)],
                    capture_output=True, text=True, timeout=60).stdout
                probe.fonts = _parse_pdffonts(raw, expected_faces)
                probe.checked_scopes.append("pdf_font_inventory")
            except (OSError, subprocess.SubprocessError):
                probe.support_limits.append("pdffonts_failed")
        else:
            probe.support_limits.append("pdffonts_not_available")

        matched = {f.matched_internal_name for f in probe.fonts if f.is_expected}
        probe.missing_expected_faces = [
            f for f in expected_faces if f not in matched]
        # A wrong-font substitution is only proven when an EXPECTED face is
        # absent while some embedded face stands in for it. Extra faces
        # (symbol/list marker fonts) are legitimate and never auto-fail.
        probe.font_substitution_detected = bool(
            expected_faces) and (
            bool(probe.missing_expected_faces)
            and any(f.embedded and not f.is_expected for f in probe.fonts))

        if text:
            missing_text = _missing_visible_text(text, options)
            if missing_text:
                probe.clip_or_overlap_findings.append(
                    f"expected text not found in render: {missing_text[:3]}")

    # Scoped verdict: we verified page count (when expected), font inventory,
    # and text presence. Geometry/clip remains a partial scope.
    probe.unsupported_scopes = [
        "word_level_geometry_precision",
        "glyph_clip_below_extractor_resolution",
        "chart_automatic_label_placement",
    ]
    probe.support_limits.append("renderer_is_not_target_powerpoint")
    ok = bool(probe.rendered)
    if expected_pages and probe.page_count != expected_pages:
        ok = False
        probe.issues.append(
            f"page count {probe.page_count} != expected {expected_pages}")
    if probe.missing_expected_faces or probe.font_substitution_detected:
        ok = False
        if probe.missing_expected_faces:
            probe.issues.append(
                f"expected faces absent from render: "
                f"{probe.missing_expected_faces}")
        if probe.font_substitution_detected:
            probe.issues.append("native renderer substituted a font face")
    if probe.clip_or_overlap_findings:
        ok = False
    probe.result = "pass" if ok else ("fail" if probe.rendered else "unknown")
    return probe


def _parse_pdffonts(raw: str,
                    expected_faces: list[str]) -> list[RenderFontEvidence]:
    out: list[RenderFontEvidence] = []
    expected_norm = {_norm_ps(name): name for name in expected_faces}
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    for line in lines[2:]:  # skip two header lines
        name = line[:36].strip()
        if not name:
            continue
        embedded = "yes" in line[60:75].lower()
        subset = name.startswith(("ABCDEF+",)) or bool(
            re.match(r"^[A-Z]{6}\+", name))
        norm = _norm_ps(name)
        matched = ""
        for key, internal in expected_norm.items():
            if key and (key in norm or norm in key):
                matched = internal
                break
        out.append(RenderFontEvidence(
            pdf_name=name, subtype=line[37:47].strip(), embedded=embedded,
            subset=subset, matched_internal_name=matched,
            is_expected=bool(matched),
            suspected_substitution=False))
    return out


def _missing_visible_text(text: str, options: dict[str, Any]) -> list[str]:
    probes = [str(s) for s in (options.get("expected_text_probes", []) or [])]
    compact = re.sub(r"\s+", " ", text)
    return [p for p in probes if p and p not in compact]


# ---------------------------------------------------------------------------
# §12.4 calibration
# ---------------------------------------------------------------------------

def calibrate_measurement(
    browser_samples: list[dict[str, Any]],
    native_samples: list[dict[str, Any]],
    bindings: Any = None,
    options: dict[str, Any] | None = None,
) -> CalibrationProfile:
    """Compare browser-predicted vs native-rendered bounds (physical PT).

    Never forces agreement by shrinking text or substituting a font: the
    output is a conservative safety margin plus honest unsupported gaps.
    """
    options = dict(options or {})
    native_renderer = str(options.get("native_renderer", RENDERER))
    by_id_native = {str(s.get("sample_id", "")): s
                    for s in native_samples or []}
    profile = CalibrationProfile(native_renderer=native_renderer)
    faces: set[str] = set()
    consumed: set[str] = set()
    for b in browser_samples or []:
        sid = str(b.get("sample_id", ""))
        n = by_id_native.get(sid)
        if n is None:
            profile.unsupported_combinations.append(
                f"{sid}: no native counterpart")
            continue
        consumed.add(sid)
        face = str(b.get("face", ""))
        if face:
            faces.add(face)
        bw = float(b.get("width_pt", 0.0) or 0.0)
        bh = float(b.get("height_pt", 0.0) or 0.0)
        nw = float(n.get("width_pt", 0.0) or 0.0)
        nh = float(n.get("height_pt", 0.0) or 0.0)
        profile.samples.append(CalibrationSample(
            sample_id=sid, case=str(b.get("case", "")),
            browser_width_pt=bw, browser_height_pt=bh,
            native_width_pt=nw, native_height_pt=nh))
        profile.max_width_delta_pt = max(
            profile.max_width_delta_pt, abs(nw - bw))
        profile.max_height_delta_pt = max(
            profile.max_height_delta_pt, abs(nh - bh))
    for sid in by_id_native:
        if sid not in consumed:
            profile.unsupported_combinations.append(
                f"{sid}: native sample without browser counterpart")
    profile.faces = sorted(faces)
    pad = float(options.get("safety_pad_pt", 1.0))
    profile.safety_margin_pt = round(
        max(profile.max_width_delta_pt, profile.max_height_delta_pt) + pad, 3)
    if profile.samples and not profile.unsupported_combinations:
        profile.coverage = "full"
    elif profile.samples:
        profile.coverage = "partial"
    else:
        profile.coverage = "none"
    profile.calibration_id = "cal-" + hashlib.sha256(json.dumps(
        {"deltas": [profile.max_width_delta_pt, profile.max_height_delta_pt],
         "faces": profile.faces, "renderer": native_renderer},
        sort_keys=True).encode()).hexdigest()[:16]
    profile.sample_hashes = {
        str(s.get("sample_id", "")): hashlib.sha256(
            json.dumps(s, sort_keys=True, default=str).encode()).hexdigest()[:16]
        for s in (browser_samples or [])}
    return profile


def build_calibration_fixture(
    out_path: str | Path,
    rules: Any,
    bundle: Any,
    *,
    cases: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build a small technical calibration deck (test_only) and return the
    per-case browser measurement inputs. Never a design reference."""
    from pptx import Presentation
    from pptx.util import Emu, Pt

    cases = cases or _default_calibration_cases()
    prs = Presentation()
    prs.slide_width = Emu(12192000)
    prs.slide_height = Emu(6858000)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    roles = getattr(rules, "roles", {}) or {}
    x = Emu(int(0.5 * 914400))
    y = Emu(int(0.4 * 914400))
    for case in cases:
        box = slide.shapes.add_textbox(x, y, Emu(int(4.0 * 914400)),
                                       Emu(int(0.9 * 914400)))
        tf = box.text_frame
        tf.word_wrap = True
        tf.text = ""
        first = True
        case["box_pt"] = {"x": 0.5 * 72, "y": 0.4 * 72, "w": 4.0 * 72,
                          "h": 0.9 * 72}
        case["runs"] = []
        for run_spec in case["paragraphs"]:
            p = tf.paragraphs[0] if first else tf.add_paragraph()
            first = False
            r = p.add_run()
            r.text = run_spec["text"]
            role = run_spec.get("role", "body")
            size = roles.get(role)
            size_pt = float(getattr(size, "default_size_pt", 12.0) or 12.0)
            r.font.size = Pt(size_pt)
            face = getattr(size, "name", "") if size is not None else ""
            case["runs"].append({
                "text": run_spec["text"], "role": role,
                "face": face, "size_pt": size_pt})
        y = Emu(int(y) + int(1.0 * 914400))
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out_path))
    return cases


def _default_calibration_cases() -> list[dict[str, Any]]:
    return [
        {"sample_id": "short", "case": "short_russian",
         "paragraphs": [{"role": "body", "text": "Короткая строка"}]},
        {"sample_id": "multiline", "case": "multiline_russian",
         "paragraphs": [{"role": "body", "text":
                         "Длинная многострочная строка для проверки "
                         "переноса и фактической высоты блока текста"}]},
        {"sample_id": "mixed", "case": "mixed_runs",
         "paragraphs": [{"role": "body", "text": "Смешанные "},
                        {"role": "title", "text": "ЗАГОЛОВОК"}]},
        {"sample_id": "empty", "case": "empty_paragraph",
         "paragraphs": [{"role": "body", "text": "Первый"},
                        {"role": "body", "text": ""}]},
        {"sample_id": "index", "case": "footnote_index",
         "paragraphs": [{"role": "footnote", "text": "Сноска 1 000 ₽"}]},
        {"sample_id": "list", "case": "list_item",
         "paragraphs": [{"role": "body", "text": "Пункт списка"}]},
    ]


def measure_fixture_in_browser(
    fixture_cases: list[dict[str, Any]],
    bundle: Any,
    measurer: Any,
    *,
    canvas_pt: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Measure the calibration cases in the real browser bridge."""
    from .models import MeasureComponent, MeasurementRequest

    components = []
    for case in fixture_cases:
        alias = ""
        for b in getattr(bundle, "bindings", []) or []:
            if b.internal_name and (
                    b.internal_name.lower() in str(
                        case["runs"][0].get("face", "")).lower()
                    or b.role_or_face.endswith(case["runs"][0].get("role", ""))):
                alias = b.browser_alias
                break
        if not alias and getattr(bundle, "bindings", []):
            alias = bundle.bindings[0].browser_alias
        paragraphs = []
        for i, run in enumerate(case["runs"]):
            paragraphs.append({
                "id": f"{case['sample_id']}-p{i}",
                "runs": [{"id": f"{case['sample_id']}-r{i}",
                          "text": run["text"],
                          "style": {"face_alias": alias,
                                    "size_pt": run["size_pt"],
                                    "bold": False, "italic": False,
                                    "underline": "", "color_rgb": "000000",
                                    "language": "ru",
                                    "baseline_shift": "none",
                                    "uppercase": False}}],
                "space_before_pt": 0.0, "space_after_pt": 0.0,
                "line_spacing_mode": "multiple", "line_spacing_value": 1.0,
                "alignment": "left", "level": 0, "list_kind": "none",
                "first_line_indent_pt": 0.0, "left_indent_pt": 0.0})
        components.append(MeasureComponent(
            id=case["sample_id"], kind="text", payload_ref=case["sample_id"],
            paragraphs=paragraphs,
            box_pt={"x": case["box_pt"]["x"], "y": case["box_pt"]["y"],
                    "w": case["box_pt"]["w"], "h": 400.0},
            margins_pt={}, vertical_anchor="top", wrap_policy="wrap",
            rotation_deg=0.0, list_preview={}))
    request = MeasurementRequest(
        schema_version="1.0", request_id="calibration",
        canvas_pt=canvas_pt or {"w": 960.0, "h": 540.0},
        css_px_per_in=96.0, profile_kind="test_only", components=components,
        font_assets=[{"alias": b.browser_alias, "path": b.path,
                      "sha256": b.sha256, "face_index": b.face_index,
                      "style": "normal", "weight": 400}
                     for b in (getattr(bundle, "bindings", []) or [])],
        engine_options={"timeout_ms": 120000, "device_scale_factor": 1,
                        "allowlist_roots": []},
        input_hashes={}, debug_output_dir="")
    response = measurer(request)
    out = []
    for item in response.get("items", []):
        out.append({
            "sample_id": item["component_id"],
            "case": item["component_id"],
            "face": (fixture_cases[0]["runs"][0].get("face", "")
                     if fixture_cases else ""),
            "width_pt": float(item.get("intrinsic_preferred_width_pt", 0.0)),
            "height_pt": float(item.get("occupied_height_pt", 0.0)),
        })
    return out


def native_text_bboxes(pdf_path: str | Path,
                       timeout: float = 120.0) -> list[dict[str, Any]]:
    """Word-level bounding boxes from pdftotext -bbox (PDF points)."""
    pdftotext = _which("pdftotext")
    if pdftotext is None:
        return []
    try:
        raw = subprocess.run(
            [pdftotext, "-bbox", "-q", str(pdf_path), "-"],
            capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    out: list[dict[str, Any]] = []
    for m in re.finditer(
            r'<word xMin="([\d.]+)" yMin="([\d.]+)" '
            r'xMax="([\d.]+)" yMax="([\d.]+)">(.*?)</word>', raw):
        out.append({
            "x0": float(m.group(1)), "y0": float(m.group(2)),
            "x1": float(m.group(3)), "y1": float(m.group(4)),
            "text": m.group(5)})
    return out
