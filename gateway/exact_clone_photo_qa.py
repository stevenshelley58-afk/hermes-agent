"""Immutable, fail-closed source-photo bindings for exact-clone QA."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image


MIN_SOURCE_REGION_CONFIDENCE = 0.95
_PLAN_DIRECTORY = "qa-source-plan"
_PLAN_FILE = "plan.json"
_BOUND_KEYS = ("x", "y", "width", "height")


def materialize_source_photo_plan(
    *,
    source: str | Path,
    source_map: Mapping[str, Any],
    source_regions: Sequence[Mapping[str, Any]],
    source_layers: Sequence[Mapping[str, Any]],
    workspace: Path,
) -> Path:
    """Materialize verified source crops once, before candidate refinement."""
    root = workspace / _PLAN_DIRECTORY
    root.mkdir(parents=True, exist_ok=True)
    path = root / _PLAN_FILE
    if path.exists():
        return path
    with Image.open(source) as opened:
        image = opened.copy()
    _validate_canvas(image, source_map)
    ocr = _ocr_bounds(source_map)
    regions = [
        (index, region, bounds)
        for index, region in enumerate(source_regions)
        if isinstance(region, Mapping)
        if (bounds := _verified_bounds(region, image.size, ocr)) is not None
    ]
    bindings: dict[str, dict[str, Any]] = {}
    used_regions: set[int] = set()
    for layer in _source_layers(source_layers):
        selected = _select_region(layer, regions, used_regions)
        if selected is None:
            bindings[layer["inputKey"]] = {"kind": "skipped"}
            continue
        index, region, bounds = selected
        used_regions.add(index)
        payload = _png_bytes(image.crop((bounds["x"], bounds["y"], bounds["right"], bounds["bottom"])))
        name = f"{len(bindings):03d}-{_safe_name(layer['inputKey'])}.png"
        (root / name).write_bytes(payload)
        bindings[layer["inputKey"]] = {
            "kind": "source-photo",
            "cropFile": name,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "sourceBounds": {key: bounds[key] for key in _BOUND_KEYS},
            "sourceRole": region.get("sourceRole"),
        }
    plan = json.dumps({"version": 1, "bindings": bindings}, sort_keys=True, separators=(",", ":")) + "\n"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(plan, encoding="utf-8")
    os.replace(temporary, path)
    return path


def source_photo_overrides(plan_path: str | Path) -> dict[str, bytes]:
    """Load only verified, previously materialized photo bytes."""
    path = Path(plan_path)
    plan = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(plan, Mapping) or plan.get("version") != 1 or not isinstance(plan.get("bindings"), Mapping):
        raise ValueError("invalid source-photo QA plan")
    root = path.parent.resolve()
    overrides: dict[str, bytes] = {}
    for input_key, binding in plan["bindings"].items():
        if not isinstance(input_key, str) or not isinstance(binding, Mapping):
            raise ValueError("invalid source-photo QA plan binding")
        if binding.get("kind") == "skipped":
            continue
        if binding.get("kind") != "source-photo" or not isinstance(binding.get("cropFile"), str):
            raise ValueError("invalid source-photo QA plan binding")
        crop = (root / binding["cropFile"]).resolve()
        try:
            crop.relative_to(root)
        except ValueError as exc:
            raise ValueError("source-photo QA crop escapes its plan directory") from exc
        payload = crop.read_bytes()
        if hashlib.sha256(payload).hexdigest() != binding.get("sha256"):
            raise ValueError("source-photo QA crop hash mismatch")
        overrides[input_key] = payload
    return overrides


def _validate_canvas(image: Image.Image, source_map: Mapping[str, Any]) -> None:
    canvas = source_map.get("canvas")
    if canvas is not None and (
        not isinstance(canvas, Mapping)
        or canvas.get("width") != image.width
        or canvas.get("height") != image.height
    ):
        raise ValueError("source-map canvas does not match source image")


def _source_layers(layers: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for layer in layers:
        if not isinstance(layer, Mapping):
            continue
        key = layer.get("inputKey")
        bounds = _bounds(layer.get("geometry"))
        if not isinstance(key, str) or not key or key in seen or bounds is None:
            continue
        seen.add(key)
        result.append({"inputKey": key, "geometry": bounds, "sourceRole": layer.get("sourceRole")})
    return result


def _select_region(
    layer: Mapping[str, Any],
    regions: Sequence[tuple[int, Mapping[str, Any], Mapping[str, int]]],
    used_regions: set[int],
) -> tuple[int, Mapping[str, Any], Mapping[str, int]] | None:
    role = layer.get("sourceRole")
    candidates = [
        item for item in regions
        if item[0] not in used_regions
        and (not isinstance(role, str) or item[1].get("sourceRole") == role)
        and _intersection(layer["geometry"], item[2]) > 0
    ]
    return max(candidates, key=lambda item: _intersection(layer["geometry"], item[2]), default=None)


def _ocr_bounds(source_map: Mapping[str, Any]) -> list[dict[str, int]]:
    records = source_map.get("ocr", [])
    return [
        bounds for record in records if isinstance(record, Mapping)
        if (bounds := _bounds(record)) is not None
    ] if isinstance(records, list) else []


def _verified_bounds(
    region: Mapping[str, Any],
    size: tuple[int, int],
    ocr: Sequence[Mapping[str, int]],
) -> dict[str, int] | None:
    confidence = region.get("confidence")
    if region.get("textFree") is not True or isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return None
    bounds = _bounds(region.get("bounds"))
    if confidence < MIN_SOURCE_REGION_CONFIDENCE or bounds is None or bounds["right"] > size[0] or bounds["bottom"] > size[1]:
        return None
    return None if any(_intersection(bounds, text) > 0 for text in ocr) else bounds


def _bounds(value: Any) -> dict[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, int] = {}
    for key in _BOUND_KEYS:
        item = value.get(key)
        if (
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(item)
            or int(item) != item
        ):
            return None
        result[key] = int(item)
    if result["x"] < 0 or result["y"] < 0 or result["width"] <= 0 or result["height"] <= 0:
        return None
    result["right"] = result["x"] + result["width"]
    result["bottom"] = result["y"] + result["height"]
    return result


def _intersection(left: Mapping[str, int], right: Mapping[str, int]) -> int:
    return max(0, min(left["right"], right["right"]) - max(left["x"], right["x"])) * max(
        0, min(left["bottom"], right["bottom"]) - max(left["y"], right["y"])
    )


def _png_bytes(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _safe_name(value: str) -> str:
    return "".join(character if character.isalnum() or character in "-_" else "_" for character in value)
