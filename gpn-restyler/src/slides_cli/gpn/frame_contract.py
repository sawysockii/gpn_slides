"""Shared frame resolution, independent of legacy native patch API imports."""

from __future__ import annotations

from typing import Any

from .models import CompiledOntology

EMU_PER_INCH = 914400
PT_PER_INCH = 72


def model_frame_context(run_dir: Any) -> dict[str, Any]:
    """Expose the exact engine coordinate contract to the authoring model."""
    import hashlib
    import json
    from pathlib import Path

    root = Path(run_dir)
    rules_path = root / "compiled_rules.json"
    profile_path = root / "template_profile.json"
    if not rules_path.is_file() or not profile_path.is_file():
        return {"verified": False, "issues": ["FRAME_CONTEXT_INPUT_MISSING"]}
    rules = CompiledOntology.model_validate_json(rules_path.read_text(encoding="utf-8"))
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    box, verified, source, issues = _resolve_content_box(profile, rules)
    return {
        "verified": verified,
        "source": source,
        "issues": issues,
        "canvas_emu": rules.canvas.model_dump(mode="json"),
        "content_box_pt": {k: v * PT_PER_INCH for k, v in box.items()},
        "zone_coordinates": "percent of content_box, NOT percent of canvas",
        "conversion": {
            "x_pt": "content_box_pt.x + content_box_pt.w * x_pct / 100",
            "y_pt": "content_box_pt.y + content_box_pt.h * y_pct / 100",
            "w_pt": "content_box_pt.w * w_pct / 100",
            "h_pt": "content_box_pt.h * h_pct / 100",
        },
        "rules_sha256": hashlib.sha256(rules_path.read_bytes()).hexdigest(),
        "profile_sha256": hashlib.sha256(profile_path.read_bytes()).hexdigest(),
    }


def _resolve_content_box(
    profile: Any, rules: CompiledOntology
) -> tuple[dict[str, float], bool, str, list[str]]:
    """Resolve the content box in inches from the template profile.

    Sources, in order:
    1. explicit ``{"content_box": ..., "verified": true, "source": ...}``
       dictionary (diagnostic fixtures may declare a verified test template);
    2. ``layout_profiles[].content_box`` of a TemplateProfile (points → in);
    3. derived box from canvas + protected regions via the existing
       template algorithm (unverified unless layout evidence is present).
    """
    issues: list[str] = []

    if profile is None:
        return {}, False, "", issues

    if isinstance(profile, dict) and profile.get("content_box"):
        raw = profile["content_box"]
        unit = str(profile.get("unit", "in")).lower()
        try:
            if unit == "emu":
                box = {
                    "x": float(raw["x"]) / EMU_PER_INCH,
                    "y": float(raw["y"]) / EMU_PER_INCH,
                    "w": float(raw["w"]) / EMU_PER_INCH,
                    "h": float(raw["h"]) / EMU_PER_INCH,
                }
            elif unit == "pt":
                box = {
                    "x": float(raw["x"]) / PT_PER_INCH,
                    "y": float(raw["y"]) / PT_PER_INCH,
                    "w": float(raw["w"]) / PT_PER_INCH,
                    "h": float(raw["h"]) / PT_PER_INCH,
                }
            else:
                box = {k: float(raw[k]) for k in ("x", "y", "w", "h")}
        except (KeyError, TypeError, ValueError):
            issues.append("explicit content_box is malformed; ignored")
            return {}, False, "", issues
        verified = bool(profile.get("verified", False))
        source = str(profile.get("source", "explicit_test_template"))
        if not verified:
            issues.append("explicit content_box not marked verified")
        return box, verified, source, issues

    layouts = _layout_profiles(profile)
    for layout in layouts:
        box_raw = _get(layout, "content_box")
        if not box_raw:
            continue
        box = {
            "x": _pt_to_in(_get(box_raw, "x")),
            "y": _pt_to_in(_get(box_raw, "y")),
            "w": _pt_to_in(_get(box_raw, "w")),
            "h": _pt_to_in(_get(box_raw, "h")),
        }
        verified = bool(_get(layout, "is_verified", False))
        source = f"layout_content_box:{_get(layout, 'id', '')}"
        return box, verified, source, issues

    derived = _derive_content_box(profile, rules, issues)
    if derived is not None:
        return derived
    return {}, False, "", issues


def _derive_content_box(
    profile: Any, rules: CompiledOntology, issues: list[str]
) -> tuple[dict[str, float], bool, str, list[str]] | None:
    from .models import RectEMU
    from .template import compute_content_box

    protected: list[Any] = []
    evidence: list[str] = []
    for layout in _layout_profiles(profile):
        for region in _get(layout, "protected_regions") or []:
            protected.append(
                RectEMU(
                    x=int(_pt_to_emu(_get(region, "x"))),
                    y=int(_pt_to_emu(_get(region, "y"))),
                    w=int(_pt_to_emu(_get(region, "w"))),
                    h=int(_pt_to_emu(_get(region, "h"))),
                )
            )
        evidence.extend(str(e) for e in (_get(layout, "evidence") or []))

    # Pass layout evidence so compute_content_box can verify the derivation.
    layout_evidence = None
    for layout in _layout_profiles(profile):
        if _get(layout, "evidence"):
            layout_evidence = layout
            break
    resolution = compute_content_box(rules.canvas, protected, layout_evidence)
    if resolution.content_box is None:
        return None
    box = {
        "x": resolution.content_box.x / EMU_PER_INCH,
        "y": resolution.content_box.y / EMU_PER_INCH,
        "w": resolution.content_box.w / EMU_PER_INCH,
        "h": resolution.content_box.h / EMU_PER_INCH,
    }
    # verified when: compute_content_box succeeded (canvas + protected
    # regions are real layout evidence from the compiled rules), regardless
    # of whether the template profiler attached an evidence string.
    verified = bool(resolution.verified)
    source = "derived:canvas_minus_protected_regions"
    if not verified:
        issues.append("content box derived without layout evidence; unverified")
    return box, verified, source, issues


def _layout_profiles(profile: Any) -> list[Any]:
    layouts = getattr(profile, "layout_profiles", None)
    if layouts is None and isinstance(profile, dict):
        layouts = profile.get("layout_profiles")
    return list(layouts or [])


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _pt_to_in(value: Any) -> float:
    return float(value or 0) / PT_PER_INCH


def _pt_to_emu(value: Any) -> float:
    return float(value or 0) * 12700
