"""Bounded, advisory OCR alignment evidence for editable text layers.

The evidence is deliberately diagnostic. It never changes a candidate, score, gate,
or approval decision.
"""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Any, Mapping, Sequence

MAX_ENTRIES = 16
_LAYER_PADDING = 8.0
_MIN_CONFIDENCE = 80.0
_CANVAS = {"feed": (1080.0, 1350.0), "story": (1080.0, 1920.0)}
_WORD_RE = re.compile(r"\w+", flags=re.UNICODE)


def _num(value: Any) -> float | None:
    """Return finite numeric values only."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _token(value: Any) -> str:
    """Normalize one OCR/copy token without case or punctuation differences."""
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^\w]+", "", normalized, flags=re.UNICODE)


def _copy_tokens(value: Any) -> list[str]:
    """Split resolved text into meaningful punctuation-insensitive tokens."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    tokens = [_token(item) for item in _WORD_RE.findall(text)]
    return [item for item in tokens if item]
    if not isinstance(value, str):
        return ""


def _canvas(value: Any, fallback: tuple[float, float]) -> tuple[float, float]:
    raw = value.get("canvas") if isinstance(value, Mapping) else None
    if not isinstance(raw, Mapping):
        return fallback
    width, height = _num(raw.get("width")), _num(raw.get("height"))
    if width is None or height is None or width <= 0 or height <= 0:
        return fallback
    return width, height


def _words(
    value: Any,
    canvas: tuple[float, float],
    target: tuple[float, float],
) -> list[dict[str, Any]]:
    raw = value.get("ocr", []) if isinstance(value, Mapping) else []
    if not isinstance(raw, list):
        return []

    scale_x, scale_y = target[0] / canvas[0], target[1] / canvas[1]
    words: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        confidence = _num(item.get("confidence"))
        if confidence is None or confidence < _MIN_CONFIDENCE:
            continue
        token = _token(item.get("text"))
        values = [_num(item.get(key)) for key in ("x", "y", "width", "height")]
        if not token or len(token) < 2 or any(item is None for item in values):
            continue
        x, y, width, height = (float(item) for item in values)
        if width <= 0 or height <= 0:
            continue
        words.append({
            "token": token,
            "x": x * scale_x,
            "y": y * scale_y,
            "width": width * scale_x,
            "height": height * scale_y,
        })
    return words


def _layers(candidate: Mapping[str, Any], placement: str) -> list[Mapping[str, Any]]:
    template = candidate.get("template")
    layout = template.get(f"{placement}Layout") if isinstance(template, Mapping) else None
    raw = layout.get("layers") if isinstance(layout, Mapping) else []
    if not isinstance(raw, list):
        return []
    return [
        item for item in raw
        if (
            isinstance(item, Mapping)
            and item.get("type") == "text"
            and isinstance(item.get("layerId"), str)
            and bool(item.get("layerId"))
        )
    ]


def _resolved_copy(candidate: Mapping[str, Any], layer: Mapping[str, Any]) -> list[str]:
    """Resolve the layer's declared input text, never arbitrary box contents."""
    input_key = layer.get("inputKey")
    if not isinstance(input_key, str) or not input_key:
        return []
    template = candidate.get("template")
    inputs = template.get("textInputs") if isinstance(template, Mapping) else None
    if not isinstance(inputs, list):
        return []
    for item in inputs:
        if not isinstance(item, Mapping) or item.get("key") != input_key:
            continue
        return _copy_tokens(item.get("placeholder"))
    return []


def _resolved_match_tokens(
    candidate: Mapping[str, Any],
    layer: Mapping[str, Any],
    meaningful_tokens: Sequence[str],
) -> set[str]:
    """Add a joined form for OCR that treats punctuation as one word."""
    matches = set(meaningful_tokens)
    template = candidate.get("template")
    inputs = template.get("textInputs") if isinstance(template, Mapping) else None
    if not isinstance(inputs, list) or len(meaningful_tokens) < 2:
        return matches
    input_key = layer.get("inputKey")
    for item in inputs:
        if isinstance(item, Mapping) and item.get("key") == input_key:
            joined = _token(item.get("placeholder"))
            if joined:
                matches.add(joined)
            break

    return matches


def _box(
    layer: Mapping[str, Any], canvas: tuple[float, float],
) -> tuple[float, float, float, float] | None:
    raw = layer.get("geometry")
    if not isinstance(raw, Mapping):
        return None
    values = [_num(raw.get(key)) for key in ("x", "y", "width", "height")]
    if any(item is None for item in values):
        return None
    x, y, width, height = (float(item) for item in values)
    if max(abs(x), abs(y), abs(width), abs(height)) <= 1.001:
        x, width = x * canvas[0], width * canvas[0]
        y, height = y * canvas[1], height * canvas[1]
    if width <= 0 or height <= 0:
        return None
    return x, y, width, height


def _bounds(words: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    left = min(float(item["x"]) for item in words)
    top = min(float(item["y"]) for item in words)
    right = max(float(item["x"]) + float(item["width"]) for item in words)
    bottom = max(float(item["y"]) + float(item["height"]) for item in words)
    return {
        "x": round(left),
        "y": round(top),
        "width": max(1, round(right - left)),
        "height": max(1, round(bottom - top)),
    }


def _inside(
    words: Sequence[Mapping[str, Any]],
    box: tuple[float, float, float, float],
) -> list[Mapping[str, Any]]:
    x, y, width, height = box
    return [
        item for item in words
        if (
            item["x"] >= x - _LAYER_PADDING
            and item["y"] >= y - _LAYER_PADDING
            and item["x"] + item["width"] <= x + width + _LAYER_PADDING
            and item["y"] + item["height"] <= y + height + _LAYER_PADDING
        )
    ]


def _entry(
    placement: str,
    layer: Mapping[str, Any],
    matched: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
    coverage: float,
) -> dict[str, Any]:
    source_bounds = _bounds([source for source, _ in matched])
    candidate_bounds = _bounds([current for _, current in matched])
    return {
        "placement": placement,
        "layerId": str(layer["layerId"]),
        "inputKey": layer.get("inputKey"),
        "matchedWordCount": len(matched),
        "coverage": round(coverage, 4),
        "sourceInkBounds": source_bounds,
        "candidateInkBounds": candidate_bounds,
        "offset": {
            "dx": round(candidate_bounds["x"] - source_bounds["x"], 4),
            "dy": round(candidate_bounds["y"] - source_bounds["y"], 4),
        },
        "sizeRatios": {
            "width": round(candidate_bounds["width"] / max(1, source_bounds["width"]), 4),
            "height": round(candidate_bounds["height"] / max(1, source_bounds["height"]), 4),
        },
    }


def build_text_alignment_evidence(
    candidate: Mapping[str, Any],
    source_map: Mapping[str, Any],
    render_map: Mapping[str, Any],
    source_placement: str,
) -> dict[str, Any]:
    """Return bounded OCR alignment evidence, never a quality or approval score."""
    if source_placement not in _CANVAS:
        raise ValueError("source_placement must be feed or story")

    canvas = _CANVAS[source_placement]
    sources = _words(source_map, _canvas(source_map, canvas), canvas)
    current = _words(render_map, _canvas(render_map, canvas), canvas)

    source_counts: dict[str, int] = {}
    current_counts: dict[str, int] = {}
    for word in sources:
        source_counts[word["token"]] = source_counts.get(word["token"], 0) + 1
    for word in current:
        current_counts[word["token"]] = current_counts.get(word["token"], 0) + 1

    source_by_token = {
        word["token"]: word
        for word in sources
        if source_counts[word["token"]] == 1
    }
    unique_current = {
        token for token, count in current_counts.items() if count == 1
    }

    entries: list[dict[str, Any]] = []
    for layer in sorted(_layers(candidate, source_placement), key=lambda item: str(item["layerId"])):
        meaningful_tokens = _resolved_copy(candidate, layer)
        if not meaningful_tokens:
            continue
        resolved_tokens = _resolved_match_tokens(candidate, layer, meaningful_tokens)
        box = _box(layer, canvas)
        if box is None:
            continue
        inside = [
            word for word in _inside(current, box)
            if word["token"] in unique_current and word["token"] in resolved_tokens
        ]
        matched = [
            (source_by_token[word["token"]], word)
            for word in inside
            if word["token"] in source_by_token
        ]
        meaningful_count = len(meaningful_tokens)
        recognized_count = len({word["token"] for word in inside})
        coverage = min(
            len(matched) / max(1, meaningful_count),
            len(matched) / max(1, recognized_count),
        )
        single_long_word = (
            len(matched) == recognized_count == 1
            and len(resolved_tokens) == 1
            and len(next(iter(resolved_tokens))) >= 5
        )
        if (len(matched) < 2 and not single_long_word) or coverage < 0.7:
            continue
        entries.append(_entry(source_placement, layer, matched, coverage))
        if len(entries) >= MAX_ENTRIES:
            break

    return {"version": 1, "sourcePlacement": source_placement, "entries": entries}
