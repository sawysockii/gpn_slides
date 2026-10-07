"""Unit conversion and affine helpers (spec §9.2).

All functions are pure; EMU/Pt/CSS conversions use one documented rounding step.
"""

from __future__ import annotations

import math

from .errors import InputError
from .models import Affine2D, PointPt, RectPct, RectPt

EMU_PER_IN = 914400
PT_PER_IN = 72.0
EMU_PER_PT = 12700


def _check_finite(value: float, name: str = "value") -> float:
    if not math.isfinite(value):
        raise InputError("NON_FINITE_VALUE", f"{name} must be finite, got {value!r}")
    return value


def emu_to_pt(value: int) -> float:
    """Convert EMU to points (exact division, no rounding)."""
    return value / EMU_PER_PT


def pt_to_emu(value: float) -> int:
    """Convert points to EMU, rounding once to the nearest integer EMU."""
    return round(_check_finite(value) * EMU_PER_PT)


def pt_to_css_px(value: float, px_per_in: float) -> float:
    """Convert points to CSS pixels at the given CSS px/in scale."""
    _check_finite(value)
    _check_finite(px_per_in, "px_per_in")
    if px_per_in <= 0:
        raise InputError("INVALID_CSS_PX_PER_IN", "px_per_in must be positive")
    return value * px_per_in / PT_PER_IN


def css_px_to_pt(value: float, px_per_in: float) -> float:
    """Convert CSS pixels to points at the given CSS px/in scale."""
    _check_finite(value)
    _check_finite(px_per_in, "px_per_in")
    if px_per_in <= 0:
        raise InputError("INVALID_CSS_PX_PER_IN", "px_per_in must be positive")
    return value * PT_PER_IN / px_per_in


def pct_to_rect(box: RectPct, parent: RectPt) -> RectPt:
    """Map a percent box onto its parent rectangle (0–100 units)."""
    return RectPt(
        x=parent.x + box.x / 100.0 * parent.w,
        y=parent.y + box.y / 100.0 * parent.h,
        w=box.w / 100.0 * parent.w,
        h=box.h / 100.0 * parent.h,
    )


def compose_affine(parent: Affine2D, child: Affine2D) -> Affine2D:
    """Matrix product ``parent @ child`` (child applied first)."""
    return Affine2D(
        a=parent.a * child.a + parent.c * child.b,
        b=parent.b * child.a + parent.d * child.b,
        c=parent.a * child.c + parent.c * child.d,
        d=parent.b * child.c + parent.d * child.d,
        tx=parent.a * child.tx + parent.c * child.ty + parent.tx,
        ty=parent.b * child.tx + parent.d * child.ty + parent.ty,
    )


def invert_affine(transform: Affine2D) -> Affine2D:
    """Invert an affine transform; raises for singular matrices."""
    det = transform.determinant()
    if abs(det) < 1e-12:
        raise InputError(
            "SINGULAR_AFFINE_TRANSFORM",
            f"cannot invert singular affine transform (det={det!r})",
        )
    a, b, c, d = transform.a, transform.b, transform.c, transform.d
    return Affine2D(
        a=d / det,
        b=-b / det,
        c=-c / det,
        d=a / det,
        tx=(c * transform.ty - d * transform.tx) / det,
        ty=(b * transform.tx - a * transform.ty) / det,
    )


def transform_point(point: PointPt, transform: Affine2D) -> PointPt:
    """Apply ``x'=a*x+c*y+tx``, ``y'=b*x+d*y+ty``."""
    return PointPt(
        x=transform.a * point.x + transform.c * point.y + transform.tx,
        y=transform.b * point.x + transform.d * point.y + transform.ty,
    )


def transform_rect_corners(
    box: RectPt, transform: Affine2D
) -> tuple[PointPt, PointPt, PointPt, PointPt]:
    """Four transformed corners (tl, tr, br, bl); bounding box is derived only."""
    tl = transform_point(PointPt(x=box.x, y=box.y), transform)
    tr = transform_point(PointPt(x=box.x + box.w, y=box.y), transform)
    br = transform_point(PointPt(x=box.x + box.w, y=box.y + box.h), transform)
    bl = transform_point(PointPt(x=box.x, y=box.y + box.h), transform)
    return tl, tr, br, bl


def bounding_box_of_corners(corners: tuple[PointPt, ...]) -> RectPt:
    """Axis-aligned bounds of transformed corners (derived magnitude only)."""
    xs = [p.x for p in corners]
    ys = [p.y for p in corners]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    return RectPt(x=min_x, y=min_y, w=max_x - min_x, h=max_y - min_y)
