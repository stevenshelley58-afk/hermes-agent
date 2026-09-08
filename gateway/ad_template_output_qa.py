"""Deterministic, fail-closed QA for rendered Meta ad output.

This is a pixel/contract safety net, not a likeness score.  It catches a
small set of high-confidence production defects that a vision model can miss:
painted badge-label displacement, embedded image CTAs, and rounded or
transparent outer canvases.  Unknown measurements are retained as evidence
and reported for visual review; they are never converted to a pass.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Mapping

from PIL import Image

from gateway.ad_template_control_ink import (
    MAX_ALIGNMENT_OFFSET_PX,
    measure_control_ink_alignment,
)

OUTPUT_QA_VERSION = "ad-output-qa-v1"
CONTROL_INK_TOLERANCE_PX = MAX_ALIGNMENT_OFFSET_PX
EXPECTED_CANVAS = {"feed": (1080, 1350), "story": (1080, 1920)}
CTA_TOKENS = frozenset({"cta", "button", "call_to_action", "call-to-action"})

def candidate_identity(candidate: Mapping[str, Any]) -> str:
    value = copy.deepcopy(dict(candidate))
    # Review metadata cannot move pixels; it is added after final review.
    metadata = value.get("template", {}).get("metadata", {})
    if isinstance(metadata, dict):
        metadata.pop("generationReview", None)
        if not metadata:
            value.get("template", {}).pop("metadata", None)
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode()).hexdigest()


def reviewed_unknowns_match(current: Mapping[str, Any], reviewed: Mapping[str, Any]) -> bool:
    """Only the same inspected pixels and unknowns can inherit visual clearance."""
    current_unknown = [item for item in current.get("findings", []) if item.get("status") == "unknown"]
    return bool(
        current.get("status") == reviewed.get("status") == "needs_review"
        and current.get("candidateSha256") == reviewed.get("candidateSha256")
        and current.get("renderSha256") == reviewed.get("renderSha256")
        and set(current.get("renderSha256", {})) == {"feed", "story"}
        and current_unknown
        and all(item.get("category") == "control-ink" for item in current_unknown)
        and current_unknown == [item for item in reviewed.get("findings", []) if item.get("status") == "unknown"]
    )


class AdTemplateOutputQaError(ValueError):
    """Raised when mandatory output QA cannot establish a safe output."""

    def __init__(self, result: Mapping[str, Any]):
        self.result = copy.deepcopy(dict(result))
        findings = self.result.get("findings") or []
        super().__init__("ad output QA " + str(self.result.get("status")) + ": " + "; ".join(
            str(item.get("reason", "unknown finding")) for item in findings[:4] if isinstance(item, Mapping)
        ))


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _bounds(value: Any) -> bool:
    return isinstance(value, Mapping) and all(_number(value.get(key)) for key in ("x", "y", "width", "height"))


def _layers(candidate: Mapping[str, Any], placement: str) -> list[Mapping[str, Any]]:
    template = candidate.get("template", {})
    layout = template.get(placement + "Layout", {}) if isinstance(template, Mapping) else {}
    value = layout.get("layers", []) if isinstance(layout, Mapping) else []
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _cta_findings(candidate: Mapping[str, Any], placement: str) -> list[dict[str, Any]]:
    """Detect only explicit image-CTA semantics, preserving informational pills."""
    findings = []
    token_pattern = re.compile(r"(?:^|[_-])(cta|call_to_action|call-to-action)(?:$|[_-])", re.IGNORECASE)
    for layer in _layers(candidate, placement):
        # IDs and human-facing labels are not semantics: ``contact_button``
        # is often an informational phone row. Require an explicit CTA input,
        # role, or renderer shape, while retaining a hard block for a literal
        # button shape regardless of its colour or copy.
        input_key = str(layer.get("inputKey", ""))
        role = str(layer.get("role", "")).lower()
        tokens = bool(token_pattern.search(input_key)) or role in {"cta", "call_to_action", "call-to-action"}
        explicit_shape = str(layer.get("shape", "")).lower() in {"button", "cta", "call_to_action", "call-to-action"}
        if not tokens and not explicit_shape:
            continue
        if tokens or explicit_shape:
            findings.append({"placement": placement, "layerId": layer.get("layerId"),
                             "category": "meta-embedded-cta",
                             "reason": "explicit embedded CTA/button layer is not allowed in Meta artwork"})
    return findings


def _full_bleed_findings(candidate: Mapping[str, Any], placement: str, path: str | Path | None) -> list[dict[str, Any]]:
    width, height = EXPECTED_CANVAS[placement]
    layers = _layers(candidate, placement)
    findings = []
    if not layers:
        return [{"placement": placement, "category": "full-bleed", "status": "unknown", "reason": "layout layers unavailable"}]
    first = layers[0]
    geometry = first.get("geometry")
    first_values = tuple(float(geometry[key]) for key in ("x", "y", "width", "height")) if _bounds(geometry) else ()
    if (first.get("type") != "plate" or first.get("protected") is not True
            or first_values not in {(0.0, 0.0, float(width), float(height)), (0.0, 0.0, 1.0, 1.0)}):
        findings.append({"placement": placement, "category": "full-bleed", "reason": "first layer is not the required opaque full-canvas plate"})
    # Only a mask spanning the whole canvas is an outer-canvas signal. Interior
    # rounded cards and badges remain valid design elements.
    for layer in layers:
        bounds = layer.get("geometry")
        mask = str(layer.get("mask", "")).lower()
        values = tuple(float(bounds[key]) for key in ("x", "y", "width", "height")) if _bounds(bounds) else ()
        if values in {(0.0, 0.0, float(width), float(height)), (0.0, 0.0, 1.0, 1.0)} and mask in {"rounded_rect", "rounded", "circle"}:
            findings.append({"placement": placement, "category": "full-bleed", "layerId": layer.get("layerId"),
                             "reason": "full-canvas layer uses an outer rounded/circular mask"})
    if path is None:
        findings.append({"placement": placement, "category": "full-bleed", "status": "unknown", "reason": "rendered canvas was not supplied"})
        return findings
    try:
        with Image.open(path) as opened:
            if opened.size != (width, height):
                findings.append({"placement": placement, "category": "full-bleed", "reason": f"rendered canvas is {opened.size}, expected {(width, height)}"})
                return findings
            rgba = opened.convert("RGBA")
            corner_alpha = [rgba.getpixel(point)[3] for point in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1))]
            if any(alpha != 255 for alpha in corner_alpha):
                findings.append({"placement": placement, "category": "full-bleed", "reason": "rendered outer corners are not fully opaque", "cornerAlpha": corner_alpha})
    except (OSError, ValueError):
        findings.append({"placement": placement, "category": "full-bleed", "status": "unknown", "reason": "rendered canvas could not be inspected"})
    return findings


def run_ad_output_qa(candidate: Mapping[str, Any], renders: Mapping[str, str | Path] | None, *, platform: str = "meta") -> dict[str, Any]:
    """Return deterministic output QA evidence with status ``pass``, ``fail`` or ``needs_review``.

    ``renders`` maps ``feed`` and ``story`` to their actual rendered PNGs. A
    missing render is unknown, not a successful empty check. ``pass`` means all
    applicable checks were measured and no finding remains.
    """
    renders = renders if isinstance(renders, Mapping) else {}
    checks: dict[str, Any] = {}
    findings: list[dict[str, Any]] = []
    unknown = []
    for placement in ("feed", "story"):
        placement_findings = _cta_findings(candidate, placement) if platform.lower() == "meta" else []
        placement_findings.extend(_full_bleed_findings(candidate, placement, renders.get(placement)))
        path = renders.get(placement)
        ink = measure_control_ink_alignment(candidate, placement, str(path)) if path is not None else []
        if path is None and any(layer.get("type") == "text" and layer.get("alignment") == "center" for layer in _layers(candidate, placement)):
            ink.append({"status": "unknown", "placement": placement, "reason": "rendered canvas was not supplied for painted-ink measurement"})
        checks[placement] = {"fullBleed": [item for item in placement_findings if item.get("category") == "full-bleed"], "controlInk": ink}
        findings.extend(placement_findings)
        findings.extend({"placement": placement, "category": "control-ink", **item} for item in ink if item.get("status") == "fail")
        unknown.extend({"placement": placement, "category": "control-ink", **item} for item in ink if item.get("status") == "unknown")
    findings.extend(unknown)
    hard_fail = [item for item in findings if item.get("status") != "unknown" and item.get("category") in {"meta-embedded-cta", "full-bleed", "control-ink"}]
    all_unknown = [item for item in findings if item.get("status") == "unknown"]
    status = "fail" if hard_fail else "needs_review" if all_unknown else "pass"
    render_hashes = {}
    for placement, path in renders.items():
        if placement not in EXPECTED_CANVAS:
            continue
        try:
            render_hashes[placement] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except (OSError, TypeError):
            pass  # Missing/unreadable renders already produce an unknown above.
    return {"version": OUTPUT_QA_VERSION, "status": status, "platform": platform,
            "candidateSha256": candidate_identity(candidate), "renderSha256": render_hashes,
            "tolerances": {"controlInkOffsetPx": CONTROL_INK_TOLERANCE_PX},
            "checks": checks, "findings": findings,
            "measured": not bool(all_unknown), "applicablePlacements": ["feed", "story"]}


def validate_output_qa(result: Mapping[str, Any], *, allow_needs_review: bool = False) -> dict[str, Any]:
    """Validate a QA result before a boundary; unresolved unknowns do not pass."""
    if not isinstance(result, Mapping) or result.get("version") != OUTPUT_QA_VERSION:
        raise AdTemplateOutputQaError({"status": "needs_review", "findings": [{"reason": "missing or unsupported output QA evidence"}]})
    status = result.get("status")
    findings = result.get("findings")
    if not isinstance(findings, list):
        raise AdTemplateOutputQaError(result)
    if status == "pass":
        if findings or set(result.get("renderSha256", {})) != {"feed", "story"}:
            raise AdTemplateOutputQaError(result)
        return copy.deepcopy(dict(result))
    if status == "needs_review" and allow_needs_review:
        if not findings or any(item.get("status") != "unknown" for item in findings):
            raise AdTemplateOutputQaError(result)
        return copy.deepcopy(dict(result))
    raise AdTemplateOutputQaError(result)


def repair_known_output_qa(candidate: Mapping[str, Any], result: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Apply only measured, bounded badge-label y corrections.

    The caller must rerender and run QA again. Unknowns and structural defects
    are deliberately not auto-repaired, preventing unsupported-effect loops.
    """
    updated = copy.deepcopy(dict(candidate))
    changes = []
    if result.get("candidateSha256") != candidate_identity(candidate):
        return updated, changes
    for placement in ("feed", "story"):
        for item in ((result.get("checks", {}).get(placement, {}) or {}).get("controlInk", []) if isinstance(result.get("checks"), Mapping) else []):
            if item.get("status") != "fail" or not _number(item.get("moveLabelDownByPx")):
                continue
            delta = float(item["moveLabelDownByPx"])
            if abs(delta) > 24:
                continue
            for layer in _layers(updated, placement):
                if layer.get("layerId") != item.get("layerId") or not _bounds(layer.get("geometry")):
                    continue
                delta_x = float(item.get("moveLabelRightByPx", 0)) if _number(item.get("moveLabelRightByPx", 0)) else 0.0
                if abs(delta_x) > 24:
                    continue
                old_x, old_y = float(layer["geometry"]["x"]), float(layer["geometry"]["y"])
                new_x, new_y = old_x + delta_x, old_y + delta
                if new_x < 0 or new_x + float(layer["geometry"]["width"]) > EXPECTED_CANVAS[placement][0] or new_y < 0 or new_y + float(layer["geometry"]["height"]) > EXPECTED_CANVAS[placement][1]:
                    continue
                layer["geometry"]["x"] = round(new_x, 2)
                layer["geometry"]["y"] = round(new_y, 2)
                changes.append({"placement": placement, "layerId": layer.get("layerId"), "property": "geometry/x,y", "from": {"x": old_x, "y": old_y}, "to": {"x": round(new_x, 2), "y": round(new_y, 2)}, "delta": {"x": round(delta_x, 2), "y": round(delta, 2)}})
                break
    return updated, changes
