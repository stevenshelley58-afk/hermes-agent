"""Painted-label measurements used by the enforceable output QA gate.

The editable text rectangle is not the painted glyph rectangle. This module
keeps the old advisory return shape and exposes a detailed variant for the
output gate. Only high-confidence badge matches are considered.
"""
from __future__ import annotations

import math
from typing import Any, Mapping

from PIL import Image, ImageColor

MAX_ALIGNMENT_OFFSET_PX = 6.0
MIN_INK_PIXELS = 20
BUTTON_SHAPES = frozenset({"rect", "rounded", "pill"})


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _geometry(value: Any) -> bool:
    return isinstance(value, Mapping) and all(_number(value.get(k)) for k in ("x", "y", "width", "height"))


def _colour(value: Any) -> tuple[int, int, int] | None:
    try:
        return ImageColor.getrgb(value) if isinstance(value, str) else None
    except (ValueError, TypeError):
        return None


def _ink_bounds(image: Image.Image, box: tuple[int, int, int, int], fg: tuple[int, int, int]):
    left, top, right, bottom = box
    if right <= left or bottom <= top:
        return None
    crop = image.crop(box)
    points = [(x, y) for y in range(crop.height) for x in range(crop.width)
              if sum(abs(a - z) for a, z in zip(crop.getpixel((x, y)), fg)) < 110]
    if len(points) < MIN_INK_PIXELS:
        return None
    xs, ys = zip(*points)
    if min(xs) == 0 or min(ys) == 0 or max(xs) == crop.width - 1 or max(ys) == crop.height - 1:
        return None
    return {"x": left + min(xs), "y": top + min(ys),
            "width": max(xs) - min(xs) + 1, "height": max(ys) - min(ys) + 1,
            "pixelCount": len(points)}


def measure_control_ink_alignment(candidate: Mapping[str, Any], placement: str, path: str) -> list[dict[str, Any]]:
    """Return measured badge alignment records, including unknowns.

    Ordinary centred headings are intentionally ignored. Missing colours,
    painted pixels, clipping, and ambiguous panel matches are ``unknown`` and
    never represented as a zero-offset pass.
    """
    template = candidate.get("template", {}) if isinstance(candidate, Mapping) else {}
    layout = template.get(placement + "Layout", {}) if isinstance(template, Mapping) else {}
    layers = layout.get("layers", []) if isinstance(layout, Mapping) else []
    colours = template.get("semanticColours", {}) if isinstance(template, Mapping) else {}
    if not isinstance(layers, list) or not isinstance(colours, Mapping):
        return []
    try:
        opened = Image.open(path)
        image = opened.convert("RGB")
    except (OSError, ValueError):
        return []
    try:
        if image.size != (1080, 1350 if placement == "feed" else 1920):
            return []
        result: list[dict[str, Any]] = []
        for index, label in enumerate(layers):
            if not isinstance(label, Mapping) or label.get("type") != "text" or label.get("alignment") != "center":
                continue
            g = label.get("geometry")
            if not _geometry(g):
                continue
            matches = []
            for background in reversed(layers[:index]):
                if not isinstance(background, Mapping) or background.get("type") != "vector" or background.get("shape") not in BUTTON_SHAPES:
                    continue
                b = background.get("geometry")
                if not _geometry(b):
                    continue
                if (abs(float(b["x"]) - float(g["x"])) > min(32, float(b["width"]) * 0.1)
                        or abs(float(b["width"]) - float(g["width"])) > 8
                        or not float(b["y"]) <= float(g["y"]) < float(b["y"]) + float(b["height"])
                        or not 20 <= float(b["height"]) <= 240
                        or float(b["width"]) > 800):
                    continue
                matches.append(background)
            if len(matches) == 0:
                continue
            base = {"layerId": label.get("layerId"), "backgroundId": matches[0].get("layerId"), "buttonBounds": dict(matches[0].get("geometry"))}
            if len(matches) != 1:
                result.append({**base, "status": "unknown", "reason": "ambiguous badge background match"})
                continue
            background = matches[0]
            if label.get("rotationDegrees", 0) not in (0, 0.0) or background.get("rotationDegrees", 0) not in (0, 0.0):
                result.append({**base, "status": "unknown", "reason": "transformed badge or label is outside axis-aligned ink calibration"})
                continue
            label_opacity = label.get("opacity", 1)
            background_opacity = background.get("opacity", 1)
            background_effects = background.get("effects") or {}
            if (label.get("effects") or any(key in label for key in ("transform", "blendMode"))
                    or not _number(label_opacity) or float(label_opacity) != 1):
                result.append({**base, "status": "unknown", "reason": "translucent or transformed label is outside ink calibration"})
                continue
            if (not _number(background_opacity) or float(background_opacity) != 1
                    or background.get("fill") or background.get("transform") or background.get("blendMode")
                    or not isinstance(background_effects, Mapping)
                    or any(key != "stroke" for key in background_effects)):
                result.append({**base, "status": "unknown", "reason": "translucent, gradient or transformed background is outside ink calibration"})
                continue
            fg = _colour(colours.get(label.get("colourRole")))
            bg = _colour(colours.get(background.get("colourRole")))
            if fg is None or bg is None:
                result.append({**base, "status": "unknown", "reason": "missing semantic colours for painted-ink measurement"})
                continue
            if sum(abs(a - z) for a, z in zip(fg, bg)) < 260:
                result.append({**base, "status": "unknown", "reason": "foreground/background contrast is too low for reliable ink measurement"})
                continue
            b = matches[0]["geometry"]
            left, top = max(0, round(float(b["x"]) + 5)), max(0, round(float(b["y"]) + 5))
            right = min(image.width, round(float(b["x"]) + float(b["width"]) - 5))
            bottom = min(image.height, round(float(b["y"]) + float(b["height"]) - 5))
            ink = _ink_bounds(image, (left, top, right, bottom), fg)
            if ink is None:
                result.append({**base, "status": "unknown", "reason": "painted glyph bounds unavailable or touch crop boundary"})
                continue
            dy = round(float(b["y"]) + float(b["height"]) / 2 - ink["y"] - ink["height"] / 2, 2)
            dx = round(float(b["x"]) + float(b["width"]) / 2 - ink["x"] - ink["width"] / 2, 2)
            result.append({**base, "status": "pass" if abs(dy) <= MAX_ALIGNMENT_OFFSET_PX and abs(dx) <= MAX_ALIGNMENT_OFFSET_PX else "fail",
                           "paintedInkBounds": ink, "moveLabelDownByPx": dy,
                           "moveLabelRightByPx": dx,
                           "labelYForCurrentButton": round(float(g["y"]) + dy, 2),
                           "tolerancePx": MAX_ALIGNMENT_OFFSET_PX,
                           "scope": "High-confidence painted glyph centering for badge labels only."})
        return result
    finally:
        opened.close()


def control_ink_alignment(candidate, placement, path):
    """Compatibility facade returning only measured records (not unknowns)."""
    return [item for item in measure_control_ink_alignment(candidate, placement, path) if item.get("status") != "unknown"]
