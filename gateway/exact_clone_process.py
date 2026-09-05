"""Bounded exact-clone controller for Blockwise ad templates.

This is the only executable template-generation flow.  It builds one complete
candidate, then preserves that candidate and applies small RFC-6902-style JSON
patches.  Models never replace the complete document after the initial build.
"""

from __future__ import annotations

import base64
import copy
import csv
import hashlib
import hmac
import io
import json
import mimetypes
import os
import re
import shlex
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Sequence

from PIL import Image, ImageChops, ImageFilter, ImageOps, ImageStat, UnidentifiedImageError

from gateway.ad_template_catalog import (
    CatalogIntegrityError,
    load_safe_asset_catalog,
    resolve_declared_assets,
)
from gateway.ad_template_runtime import (
    AdTemplateProcessError,
    AdTemplateRendererRejection,
    AdTemplateStructuredOutputError,
    AdTemplateTransportError,
    vision_message,
)


PROCESS_ID = "exact-clone"
LIKENESS_THRESHOLD = 9.8
TYPOGRAPHY_SUBSTITUTION_THRESHOLD = 9.5
NORMAL_COMPARISONS = 4
ESCALATION_COMPARISONS = 2
MAX_COMPARISONS = NORMAL_COMPARISONS + ESCALATION_COMPARISONS
MAX_FINAL_REVIEW_ROUNDS = 2
MAX_OUTPUT_RETRIES = 1
MAX_PATCH_REPLANS = 2
MAX_PATCH_OPERATIONS = 64
MAX_PATCH_BYTES = 32_000
MAX_CONTRACT_REPAIRS = 6
MAX_RENDERER_REASON_CHARS = 16_000
REGRESSION_EPSILON = 0.05
AVAILABLE_FONT_FILES = frozenset({
    "/fonts/adstudio/poppins-500.woff2",
    "/fonts/adstudio/poppins-700.woff2",
    "/fonts/adstudio/manrope-400.woff2",
    "/fonts/adstudio/manrope-700.woff2",
    "/fonts/adstudio/playfair-display-700.woff2",
    "/fonts/adstudio/cormorant-garamond-700.woff2",
})
SOURCE_MAP_VERSION = 2
QA_PROJECTION_VERSION = 4
EVALUATION_POLICY_VERSION = 4
STAGES = (
    "source",
    "aspect-reference",
    "build",
    "render",
    "compare",
    "final-check",
    "import",
    "smoke-test",
    "ready-for-review",
    "publish",
    "live",
)
SCORE_FIELDS = (
    "overall",
    "geometry",
    "typography",
    "colourEffects",
    "imageCrop",
    "details",
)
EFFECT_FIELDS = (
    "shading",
    "gradients",
    "shadows",
    "transparency",
    "borders",
    "masks",
    "texture",
)
_EFFECT_STATES = frozenset({"match", "not_present", "mismatch"})
_MUTABLE_PATCH_ROOTS = (
    "/template/feedLayout/",
    "/template/storyLayout/",
    "/template/imageInputs/",
    "/template/textInputs/",
    "/template/semanticColours/",
    "/template/fonts/",
    "/template/metadata/",
)


def _runtime_catalog():
    configured = os.environ.get("AD_TEMPLATE_ASSET_CATALOG_DIR", "").strip()
    root = (
        Path(configured).expanduser()
        if configured
        else Path(__file__).resolve().parents[1] / "assets" / "ad-template-generator" / "catalog"
    )
    try:
        return load_safe_asset_catalog(root)
    except (CatalogIntegrityError, FileNotFoundError, OSError) as exc:
        raise AdTemplateProcessError(f"safe asset catalog is invalid: {exc}") from exc


def _safe_json(value: Any, *, max_bytes: int = 200_000) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > max_bytes:
        raise AdTemplateProcessError("structured model value exceeds the bounded process limit")
    return encoded


def _candidate_structure(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"template", "assets"}:
        raise AdTemplateProcessError("builder must return exactly template and assets")
    template = value.get("template")
    declarations = value.get("assets")
    if not isinstance(template, dict) or template.get("schema") != "blockwise.ad-template":
        raise AdTemplateProcessError("builder template must use blockwise.ad-template")
    if not isinstance(declarations, list):
        raise AdTemplateProcessError("builder assets must be a declaration list")
    required_template_fields = {
        "schema", "templateId", "createdAt", "feedLayout", "storyLayout",
        "imageInputs", "textInputs", "semanticColours", "assets", "fonts",
        "metadata",
    }
    missing = sorted(required_template_fields - set(template))
    unexpected = sorted(set(template) - required_template_fields)
    if missing or unexpected:
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected {', '.join(unexpected)}")
        raise AdTemplateProcessError(
            "builder template fields do not match the direct Blockwise contract: "
            + "; ".join(details)
        )
    for field in ("feedLayout", "storyLayout", "semanticColours", "assets", "metadata"):
        if not isinstance(template.get(field), dict):
            raise AdTemplateProcessError(f"builder template {field} must be an object")
    for field in ("imageInputs", "textInputs", "fonts"):
        if not isinstance(template.get(field), list):
            raise AdTemplateProcessError(f"builder template {field} must be a list")
    for placement, field in (("feed", "feedLayout"), ("story", "storyLayout")):
        layout = template[field]
        if layout.get("placement") != placement or not isinstance(layout.get("layers"), list) or not isinstance(layout.get("safeZones"), list):
            raise AdTemplateProcessError(f"builder template {field} must contain placement, layers and safeZones")
    declared_keys = set()
    for declaration in declarations:
        if not isinstance(declaration, dict) or set(declaration) != {"assetKey", "fileName", "mimeType"}:
            raise AdTemplateProcessError("builder asset declarations must contain exactly assetKey, fileName and mimeType")
        if any(not isinstance(declaration[key], str) or not declaration[key].strip() for key in declaration):
            raise AdTemplateProcessError("builder asset declaration values must be non-empty strings")
        declared_keys.add(declaration["assetKey"])
    if len(declared_keys) != len(declarations) or declared_keys != set(template["assets"]):
        raise AdTemplateProcessError("builder asset declarations must exactly match template.assets")
    if any("bytesBase64" in item for item in declarations if isinstance(item, dict)):
        raise AdTemplateProcessError("builder must not return asset bytes")
    _safe_json(value)
    return copy.deepcopy(value)


def _candidate_envelope(value: Any) -> Dict[str, Any]:
    candidate = _candidate_structure(value)
    violations: list[str] = []
    allowed_types = {"plate", "image_slot", "overlay_patch", "text", "logo", "vector", "icon"}
    for placement, field, canvas_height in (
        ("feed", "feedLayout", 1350),
        ("story", "storyLayout", 1920),
    ):
        layers = candidate["template"][field]["layers"]
        if not layers:
            violations.append(f"{field}.layers must not be empty")
            continue
        for index, layer in enumerate(layers):
            if not isinstance(layer, dict) or layer.get("type") not in allowed_types:
                violations.append(
                    f"/template/{field}/layers/{index}/type must be one of "
                    + ", ".join(sorted(allowed_types))
                )
        first = layers[0] if isinstance(layers[0], dict) else {}
        geometry = first.get("geometry") if isinstance(first.get("geometry"), dict) else {}
        try:
            values = tuple(float(geometry[key]) for key in ("x", "y", "width", "height"))
        except (KeyError, TypeError, ValueError):
            values = ()
        normalized = bool(values) and all(abs(value) <= 1.001 for value in values)
        expected = (0.0, 0.0, 1.0, 1.0) if normalized else (0.0, 0.0, 1080.0, float(canvas_height))
        if first.get("type") != "plate" or first.get("protected") is not True or values != expected:
            violations.append(
                f"{placement} first layer must be type plate, protected true, and full-canvas "
                f"geometry 0,0,1080,{canvas_height} (or normalized 0,0,1,1)"
            )
    template = candidate["template"]
    declared_fonts = {
        item.get("file") for item in template.get("fonts", [])
        if isinstance(item, dict)
    }
    for layout_key in ("feedLayout", "storyLayout"):
        for layer in template[layout_key]["layers"]:
            if isinstance(layer, dict) and layer.get("type") == "text":
                font = layer.get("font")
                file = font.get("file") if isinstance(font, dict) else None
                if not isinstance(file, str) or not file.strip() or file not in declared_fonts:
                    raise AdTemplateProcessError(f"text layer {layer.get('layerId')} requests an undeclared font")
    if violations:
        raise AdTemplateRendererRejection(violations)
    return candidate


def _source_placement(path: str) -> tuple[str, str, Dict[str, int]]:
    try:
        with Image.open(path) as image:
            width, height = image.size
    except (OSError, UnidentifiedImageError) as exc:
        raise AdTemplateProcessError("source must be a readable image") from exc
    if width <= 0 or height <= 0:
        raise AdTemplateProcessError("source dimensions are invalid")
    ratio = width / height
    feed_delta = abs(ratio - 0.8)
    story_delta = abs(ratio - (9 / 16))
    source = "feed" if feed_delta <= story_delta else "story"
    target = "story" if source == "feed" else "feed"
    canvas = {"width": 1080, "height": 1920 if target == "story" else 1350}
    return source, target, canvas

def source_canvas_reference(source: str, workspace: Path, placement: str) -> str:
    """Put all source measurements and vision images in renderer pixel units."""
    root = workspace / "references"
    root.mkdir(parents=True, exist_ok=True)
    target = root / "source-canvas.png"
    dimensions = (1080, 1350 if placement == "feed" else 1920)
    with Image.open(source) as image:
        image = ImageOps.exif_transpose(image).convert("RGBA")
        white = Image.new("RGBA", image.size, "white")
        white.alpha_composite(image)
        # Preserve the entire input, not an ImageOps.fit/center crop. The
        # original upload remains untouched in previews/source.*.
        normalized = white.convert("RGB").resize(dimensions, Image.Resampling.LANCZOS)
    temporary = target.with_suffix(".png.tmp")
    normalized.save(temporary, format="PNG")
    os.replace(temporary, target)
    return str(target)


def build_source_map(path: str) -> Dict[str, Any]:
    """Measure source pixels before a model is allowed to infer coordinates."""
    with Image.open(path) as opened:
        rgb = ImageOps.exif_transpose(opened).convert("RGB")
    width, height = rgb.size
    sample = rgb.copy()
    sample.thumbnail((128, 128), Image.Resampling.LANCZOS)
    quantized = sample.quantize(colors=8, method=Image.Quantize.MEDIANCUT).convert("RGB")
    colours = quantized.getcolors(maxcolors=sample.width * sample.height) or []
    palette = [
        {"hex": "#%02x%02x%02x" % colour, "pixels": int(count)}
        for count, colour in sorted(colours, reverse=True)[:8]
    ]

    edge_small = ImageOps.grayscale(rgb)
    edge_small.thumbnail((360, 640), Image.Resampling.LANCZOS)
    edges = edge_small.filter(ImageFilter.FIND_EDGES)
    edge_regions: list[dict[str, Any]] = []
    columns, rows = 6, 8
    for row in range(rows):
        for column in range(columns):
            left = round(column * edges.width / columns)
            top = round(row * edges.height / rows)
            right = round((column + 1) * edges.width / columns)
            bottom = round((row + 1) * edges.height / rows)
            cell = edges.crop((left, top, right, bottom))
            density = sum(1 for pixel in cell.getdata() if pixel >= 40) / max(1, cell.width * cell.height)
            if density >= 0.08:
                edge_regions.append({
                    "x": round(left * width / edges.width),
                    "y": round(top * height / edges.height),
                    "width": max(1, round((right - left) * width / edges.width)),
                    "height": max(1, round((bottom - top) * height / edges.height)),
                    "density": round(density, 4),
                })
    edge_regions.sort(key=lambda item: (-item["density"], item["y"], item["x"]))

    # Projection bands supply deterministic large rectangular regions without
    # requiring another runtime service or native CV dependency.
    row_density: list[float] = []
    for y in range(edges.height):
        strip = edges.crop((0, y, edges.width, y + 1))
        row_density.append(sum(1 for pixel in strip.getdata() if pixel >= 40) / max(1, edges.width))
    active = [density >= 0.075 for density in row_density]
    bands: list[tuple[int, int]] = []
    start: int | None = None
    for index, enabled in enumerate([*active, False]):
        if enabled and start is None:
            start = index
        elif not enabled and start is not None:
            if index - start >= 2:
                bands.append((start, index))
            start = None
    rectangles = [{
        "x": 0,
        "y": round(top * height / edges.height),
        "width": width,
        "height": max(1, round((bottom - top) * height / edges.height)),
        "kind": "edge-band",
    } for top, bottom in bands[:16]]

    ocr: list[dict[str, Any]] = []
    tesseract = shutil.which("tesseract")
    if not tesseract and Path("/usr/bin/tesseract").is_file():
        tesseract = "/usr/bin/tesseract"
    ocr_status = "unavailable"
    if tesseract:
        try:
            for pass_name, extra_args, minimum_confidence in (
                ("layout", [], 20.0),
                ("sparse", ["--psm", "11"], 50.0),
            ):
                result = subprocess.run(
                    [tesseract, str(Path(path).resolve()), "stdout", *extra_args, "tsv"],
                    capture_output=True, text=True, timeout=30, check=False,
                )
                if result.returncode != 0:
                    if pass_name == "layout":
                        ocr_status = "failed"
                    continue
                reader = csv.DictReader(io.StringIO(result.stdout), delimiter="\t")
                for row in reader:
                    text = str(row.get("text") or "").strip()
                    try:
                        confidence = float(row.get("conf") or -1)
                        left = int(row.get("left") or 0)
                        top = int(row.get("top") or 0)
                        item_width = int(row.get("width") or 0)
                        item_height = int(row.get("height") or 0)
                    except ValueError:
                        continue
                    if not text or confidence < minimum_confidence or item_width <= 0 or item_height <= 0:
                        continue
                    candidate = {
                        "text": text[:200],
                        "x": left, "y": top, "width": item_width, "height": item_height,
                        "confidence": round(confidence, 1),
                        "ocrPass": pass_name,
                        "pageId": int(row.get("page_num") or 0),
                        "blockId": int(row.get("block_num") or 0),
                        "paragraphId": int(row.get("par_num") or 0),
                        "lineId": int(row.get("line_num") or 0),
                        "wordId": int(row.get("word_num") or 0),
                    }
                    if pass_name == "sparse":
                        candidate_area = item_width * item_height
                        duplicate = False
                        for existing in ocr:
                            overlap_width = max(0, min(left + item_width, existing["x"] + existing["width"]) - max(left, existing["x"]))
                            overlap_height = max(0, min(top + item_height, existing["y"] + existing["height"]) - max(top, existing["y"]))
                            overlap = overlap_width * overlap_height
                            existing_area = max(1, existing["width"] * existing["height"])
                            if overlap / max(1, min(candidate_area, existing_area)) >= 0.6:
                                duplicate = True
                                break
                        if duplicate:
                            continue
                    ocr.append(candidate)
                    if len(ocr) >= 300:
                        break
                ocr_status = "completed"
                if len(ocr) >= 300:
                    break
        except (OSError, subprocess.SubprocessError, ValueError):
            ocr_status = "failed"
    return {
        "sourceMapVersion": SOURCE_MAP_VERSION,
        "canvas": {"width": width, "height": height},
        "ocrStatus": ocr_status,
        "ocr": ocr,
        "dominantPalette": palette,
        "edgeRegions": edge_regions[:16],
        "rectangleRegions": rectangles,
    }




def deterministic_pixel_metrics(reference_path: str, candidate_path: str) -> Dict[str, float]:
    """Return stable diagnostics for the source-filled QA comparison."""
    with Image.open(reference_path) as reference_open, Image.open(candidate_path) as candidate_open:
        reference = ImageOps.fit(ImageOps.exif_transpose(reference_open).convert("RGB"), (256, 256), method=Image.Resampling.LANCZOS)
        candidate = ImageOps.fit(ImageOps.exif_transpose(candidate_open).convert("RGB"), (256, 256), method=Image.Resampling.LANCZOS)
    difference = ImageChops.difference(reference, candidate)
    mae = sum(ImageStat.Stat(difference).mean) / 3
    pixel_similarity = max(0.0, 1.0 - mae / 255.0)
    ref_edges = ImageOps.grayscale(reference).filter(ImageFilter.FIND_EDGES).point(lambda value: 255 if value >= 40 else 0)
    candidate_edges = ImageOps.grayscale(candidate).filter(ImageFilter.FIND_EDGES).point(lambda value: 255 if value >= 40 else 0)
    intersection = sum(1 for left, right in zip(ref_edges.getdata(), candidate_edges.getdata()) if left and right)
    union = sum(1 for left, right in zip(ref_edges.getdata(), candidate_edges.getdata()) if left or right)
    edge_similarity = intersection / max(1, union)
    ref_mean = ImageStat.Stat(reference).mean
    candidate_mean = ImageStat.Stat(candidate).mean
    colour_similarity = max(0.0, 1.0 - sum(abs(left - right) for left, right in zip(ref_mean, candidate_mean)) / (3 * 255))
    return {
        "pixelSimilarity": round(pixel_similarity, 5),
        "edgeSimilarity": round(edge_similarity, 5),
        "colourSimilarity": round(colour_similarity, 5),
    }


def _reconstruct_ocr_text(words: Sequence[Mapping[str, Any]]) -> str:
    """Preserve visual lines instead of interleaving OCR words by raw y."""
    normalized = []
    for word in words:
        try:
            x = float(word.get("x", 0))
            y = float(word.get("y", 0))
            width = max(1.0, float(word.get("width", 0)))
            height = max(1.0, float(word.get("height", 0)))
        except (TypeError, ValueError):
            continue
        text = str(word.get("text") or "").strip()
        if text:
            line_key = None
            if all(word.get(key) is not None for key in ("ocrPass", "pageId", "blockId", "paragraphId", "lineId")):
                line_key = (
                    str(word.get("ocrPass")), int(word.get("pageId") or 0),
                    int(word.get("blockId") or 0), int(word.get("paragraphId") or 0),
                    int(word.get("lineId") or 0),
                )
            normalized.append({
                "x": x, "y": y, "width": width, "height": height,
                "center": y + height / 2, "text": text, "lineKey": line_key,
                "confidence": float(word.get("confidence") or 0),
            })
    lines: list[dict[str, Any]] = []
    for word in sorted(normalized, key=lambda item: (item["center"], item["x"])):
        if word["lineKey"] is not None:
            keyed = next((line for line in lines if line.get("lineKey") == word["lineKey"]), None)
            if keyed is not None:
                keyed["words"].append(word)
                keyed["center"] = sum(item["center"] for item in keyed["words"]) / len(keyed["words"])
                continue
            lines.append({"center": word["center"], "height": word["height"], "lineKey": word["lineKey"], "words": [word]})
            continue
        matches = [
            line for line in lines if line.get("lineKey") is None
            if abs(word["center"] - line["center"]) <= 0.6 * max(word["height"], line["height"])
        ]
        if matches:
            line = min(matches, key=lambda item: abs(word["center"] - item["center"]))
            line["words"].append(word)
            count = len(line["words"])
            line["center"] = ((line["center"] * (count - 1)) + word["center"]) / count
            line["height"] = max(line["height"], word["height"])
        else:
            lines.append({"center": word["center"], "height": word["height"], "lineKey": None, "words": [word]})
    output = []
    for line in sorted(lines, key=lambda item: item["center"]):
        ordered = sorted(line["words"], key=lambda item: item["x"])
        tokens = [word["text"] for word in ordered]
        if tokens and (
            tokens[0] in {"*", "©", "®", "•", "·"}
            or (len(tokens[0]) == 1 and ordered[0]["confidence"] < 55 and ordered[0]["width"] <= ordered[0]["height"])
        ):
            tokens[0] = "•"
            if len(tokens) > 1:
                tokens[1] = re.sub(r"^(\d+)[.,](?=[A-Z])", r"\1 ", tokens[1])
                tokens[1] = re.sub(r"^(\d+)(?=[A-Z])", r"\1 ", tokens[1])
        text = " ".join(tokens)
        output.append(re.sub(r"\s+([,.;:!?])", r"\1", text))
    return "\n".join(output)


def _ocr_words_by_text_layer(
    layers: Sequence[Mapping[str, Any]], placement_map: Mapping[str, Any],
    *, canvas_width: int, canvas_height: int,
) -> Dict[int, list[Mapping[str, Any]]]:
    """Assign each OCR token to at most one overlapping editable text layer."""
    source_canvas = placement_map.get("canvas") if isinstance(placement_map.get("canvas"), Mapping) else {}
    source_width = max(1.0, float(source_canvas.get("width") or canvas_width))
    source_height = max(1.0, float(source_canvas.get("height") or canvas_height))
    text_boxes: list[tuple[int, tuple[float, float, float, float]]] = []
    for index, layer in enumerate(layers):
        if not isinstance(layer, Mapping) or layer.get("type") != "text":
            continue
        geometry = layer.get("geometry") if isinstance(layer.get("geometry"), Mapping) else {}
        try:
            x, y, width, height = (float(geometry[key]) for key in ("x", "y", "width", "height"))
        except (KeyError, TypeError, ValueError):
            continue
        if max(abs(x), abs(y), abs(width), abs(height)) <= 1.001:
            x, width = x * canvas_width, width * canvas_width
            y, height = y * canvas_height, height * canvas_height
        if width > 0 and height > 0:
            text_boxes.append((index, (x, y, x + width, y + height)))

    assignments: Dict[int, list[Mapping[str, Any]]] = {}
    for word in placement_map.get("ocr", []):
        if not isinstance(word, Mapping):
            continue
        try:
            left = float(word.get("x", 0)) * canvas_width / source_width
            top = float(word.get("y", 0)) * canvas_height / source_height
            width = float(word.get("width", 0)) * canvas_width / source_width
            height = float(word.get("height", 0)) * canvas_height / source_height
        except (TypeError, ValueError):
            continue
        if width <= 0 or height <= 0:
            continue
        right, bottom = left + width, top + height
        word_area = width * height
        center_x, center_y = left + width / 2, top + height / 2
        candidates = []
        for index, (box_left, box_top, box_right, box_bottom) in text_boxes:
            intersection = max(0.0, min(right, box_right) - max(left, box_left)) * max(0.0, min(bottom, box_bottom) - max(top, box_top))
            ratio = intersection / max(1.0, word_area)
            if ratio < 0.25:
                continue
            box_center_x, box_center_y = (box_left + box_right) / 2, (box_top + box_bottom) / 2
            distance = (center_x - box_center_x) ** 2 + (center_y - box_center_y) ** 2
            area = (box_right - box_left) * (box_bottom - box_top)
            candidates.append((-ratio, distance, area, index))
        if candidates:
            index = min(candidates)[3]
            assignments.setdefault(index, []).append(word)
    return assignments


def _validate_aspect_reference(
    value: Any, *, source_placement: str, target_placement: str, canvas: Mapping[str, int],
) -> Dict[str, Any]:
    required = {"sourcePlacement", "targetPlacement", "canvas", "regions", "preserve"}
    if not isinstance(value, dict) or set(value) != required:
        raise AdTemplateProcessError("aspect reference has an invalid shape")
    if value["sourcePlacement"] != source_placement or value["targetPlacement"] != target_placement:
        raise AdTemplateProcessError("aspect reference placements do not match the source")
    if value["canvas"] != dict(canvas):
        raise AdTemplateProcessError("aspect reference canvas is invalid")
    regions = value["regions"]
    if not isinstance(regions, list) or not regions or len(regions) > 64:
        raise AdTemplateProcessError("aspect reference requires bounded layout regions")
    seen: set[str] = set()
    for item in regions:
        if not isinstance(item, dict) or set(item) != {"regionId", "sourceRole", "target", "zIndex"}:
            raise AdTemplateProcessError("aspect reference region is invalid")
        region_id = item.get("regionId")
        target = item.get("target")
        if not isinstance(region_id, str) or not region_id.strip() or region_id in seen:
            raise AdTemplateProcessError("aspect reference region IDs must be unique")
        seen.add(region_id)
        if not isinstance(item.get("sourceRole"), str) or not item["sourceRole"].strip():
            raise AdTemplateProcessError("aspect reference sourceRole is required")
        if not isinstance(item.get("zIndex"), int):
            raise AdTemplateProcessError("aspect reference zIndex must be an integer")
        if not isinstance(target, dict) or set(target) != {"x", "y", "width", "height"}:
            raise AdTemplateProcessError("aspect reference target rectangle is invalid")
        if any(isinstance(target[key], bool) or not isinstance(target[key], (int, float)) for key in target):
            raise AdTemplateProcessError("aspect reference target values must be numeric")
        if target["width"] <= 0 or target["height"] <= 0:
            raise AdTemplateProcessError("aspect reference target must have positive size")
        if target["x"] < 0 or target["y"] < 0 or target["x"] + target["width"] > canvas["width"] or target["y"] + target["height"] > canvas["height"]:
            raise AdTemplateProcessError("aspect reference target must remain on canvas")
    preserve = value["preserve"]
    if not isinstance(preserve, list) or not preserve or len(preserve) > 64 or any(not isinstance(item, str) or not item.strip() for item in preserve):
        raise AdTemplateProcessError("aspect reference preserve list is invalid")
    return copy.deepcopy(value)


def aspect_reference_prompt(*, source_placement: str, target_placement: str, canvas: Mapping[str, int], brief: str) -> str:
    return f"""You are the aspect-reference role for an exact-clone ad-template compiler. Inspect the attached source pixels. Do not redesign, simplify, improve, or invent. Describe how the same source composition must be reflowed into the reciprocal {target_placement} canvas while preserving every visible region, hierarchy, spacing relationship, shading, gradient, shadow, transparency, border, mask, texture, image crop role, and editable text/logo role.

Return one JSON object with exactly sourcePlacement, targetPlacement, canvas, regions, preserve. sourcePlacement must be {source_placement}; targetPlacement must be {target_placement}; canvas must be {json.dumps(dict(canvas), separators=(',', ':'))}. regions is an ordered list of objects with exactly regionId, sourceRole, target and zIndex. target has exactly numeric x,y,width,height inside the canvas. preserve is a non-empty list of concrete source-visible properties that must not change. This is measurement and reflow only, never creative direction. Return JSON only. Brief: {brief[:2000]}"""


def build_prompt(*, run_id: str, project_id: str, brief: str, placements: Any, reference: Mapping[str, Any], source_map: Mapping[str, Any]) -> str:
    catalog = "\n".join(_runtime_catalog().prompt_lines())
    return f"""Build one editable Blockwise template as a near-pixel clone of the attached source. This is reconstruction, not creative work. Preserve exact region geometry, whitespace, hierarchy, typography character, image count/crops, colours, shading, gradients, shadows, transparency, borders, masks, texture, decoration and visual density. Reproduce source effects rather than flattening or omitting them. Feed must clone a Feed source; Story must follow the immutable reciprocal aspect reference below. Neutralize only source advertiser identity, contact details and photographs, while preserving their exact visual footprint as editable inputs and source-free catalog defaults.

Return JSON only with exactly {{"template":{{...}},"assets":[]}}. The template object must have exactly these keys: schema, templateId, createdAt, feedLayout, storyLayout, imageInputs, textInputs, semanticColours, assets, fonts, metadata. Do not return schemaVersion, fields, placements, name or any legacy pack envelope.

DIRECT BLOCKWISE CONTRACT:
- schema is "blockwise.ad-template"; templateId is a stable safe ID; createdAt is an ISO-8601 UTC datetime.
- feedLayout is {{placement:"feed",layers:[],safeZones:[]}} and storyLayout is {{placement:"story",layers:[],safeZones:[]}}. Populate layers; keep safeZones an array of {{x,y,width,height}} rectangles or []. Do not put canvas dimensions or aspect ratios in placement. Feed canvas is 1080x1350 and Story is 1080x1920. Use absolute pixel geometry {{x,y,width,height}}. The first layer is a protected full-canvas plate. Every layer requires a type field set to its literal type below. Layer IDs are unique across both placements.
- Allowed ordered layer types are: plate {{layerId,colourRole,assetKey?,geometry,protected,effects?,fill?,cornerRadius?}}; image_slot {{layerId,inputKey,geometry,mask,minSourceWidth,minSourceHeight,defaultCrop,allowedPlacementOverrides,effects?,cornerRadius?,opacity?}}; overlay_patch {{layerId,geometry,colourRole,opacity,assetKey?,effects?,fill?,cornerRadius?}}; text {{layerId,inputKey,font,fontSize,sizeRatio?,fontFamily?,fontWeight?,italic?,case?,opacity?,effects?,lineHeight,tracking,alignment,maxCharacters,maxLines,colourRole,overflowBehaviour,geometry}}; logo {{layerId,geometry,inputKey,effects?,cornerRadius?,opacity?}}; vector {{layerId,geometry,shape,colourRole,opacity,effects?,fill?,cornerRadius?}}; icon {{layerId,geometry,icon,colourRole,opacity?,effects?}}.
- colourRole is one of background, primary, secondary, accent, mainText, inverseText. vector shape is rect, rounded, circle, line, pill, notched, wave or ring. icon is arrow, check, tick, phone, mail, globe or location.
- effects may contain rotationDegrees, blendMode, shadow and stroke. fill may be {{type:"linear_gradient",angleDegrees,stops:[{{offset,colourRole,opacity}},...]}}. Preserve every visible source effect with these fields.
- image_slot mask is rounded_rect, circle or none; defaultCrop is {{x:0,y:0,width:1,height:1}}; allowedPlacementOverrides contains only crop and/or position. Text overflowBehaviour is refuse, truncate or scale_down; alignment is left, center or right; font is {{file:"/fonts/adstudio/...woff2"}}; tracking is absolute pixels from -4 to 4.
- Renderer text constraints: Feed effective font size must be at least 24px; Story at least 32px. Multiline lineHeight must be at least 1. Preserve source geometry and hierarchy within these constraints; do not let text exceed its box or erase contacts.
- imageInputs is a list of {{key,label,required?,acceptedTypes,defaultAssetKey?}}. textInputs is a list of {{key,label,placeholder,maxLength}}. Every image/logo/text layer inputKey is declared. Keep neutral reusable placeholders here with lengths close to the source; QA retains these authored strings and only substitutes source photo crops.
- semanticColours contains exactly background, primary, secondary, accent, mainText, inverseText. assets is an object mapping each assetKey to {{fileName,mimeType}}. fonts is a list of unique {{file}} objects; text layer font.file must be declared. Use matching available font paths such as /fonts/adstudio/poppins-500.woff2, /fonts/adstudio/poppins-700.woff2, /fonts/adstudio/manrope-400.woff2, /fonts/adstudio/manrope-700.woff2, /fonts/adstudio/playfair-display-700.woff2 or /fonts/adstudio/cormorant-garamond-700.woff2.
- metadata contains exactly title, description, gallerySamples, metaCopyDefaults, aiWritingGuidance, publishRequirements, replacementAssets, realAssetRefs. gallerySamples is {{feed?:{{assetKey?,placement:"feed",purpose}},story?:{{assetKey?,placement:"story",purpose}}}}. metaCopyDefaults is {{primaryText:[],headlines:[],descriptions:[],cta}}. aiWritingGuidance is {{summary,fields}}. publishRequirements is {{objective,specialAdCategory,instantForm:{{required,dependency,defaults?}},destination:{{required,kind,dependency}},fulfilment?,offer?,claims?,requiredCtaTypes}}. replacementAssets is a list of {{inputKey,assetKey,purpose?}}. realAssetRefs is a list of {{inputKey,kind,required}}. Do not create generationReview; the controller adds it after final review.
- The outer assets list contains exactly one {{assetKey,fileName,mimeType}} declaration for every template.assets entry, with matching values. Never return bytes, hashes, signatures, source paths or a flattened source image.
- Use colourRole (British spelling), never colorRole; vector/icon layers still require their own colourRole even when stroke/effects are present. Do not invent maxSourceWidth/maxSourceHeight; image slots require minSourceWidth, minSourceHeight and allowedPlacementOverrides.
- Omit optional offer/fulfilment/claims when not applicable, never use empty objects as placeholders. If supplied, fulfilment requires required:boolean and dependency:string|null. offer is null or {{name,promise:string|null,terms:string[],eligibility:string|null,expiresAt:ISO-datetime|null}}. Real advertiser fulfilment/evidence must be supplied before publishing; do not invent it.

Use only these catalog files:\n{catalog}

RECIPROCAL ASPECT REFERENCE: {_safe_json(reference)}
DETERMINISTIC SOURCE MAP (measurements override coordinate guesses): {_safe_json(source_map)}
Run {run_id}; project {project_id}; placements {json.dumps(placements)}; brief {brief[:3000]}."""


def _validate_issue(value: Any) -> Dict[str, Any]:
    required = {"placement", "layerIds", "category", "instruction", "severity"}
    if not isinstance(value, dict) or set(value) != required:
        raise AdTemplateProcessError("review issue has an invalid shape")
    placement = value.get("placement")
    if placement not in {"feed", "story", "both"}:
        raise AdTemplateProcessError("review issue placement is invalid")
    layer_ids = value.get("layerIds")
    if not isinstance(layer_ids, list) or not layer_ids or len(layer_ids) > 20 or any(not isinstance(item, str) or not item.strip() for item in layer_ids):
        raise AdTemplateProcessError("review issue layerIds are invalid")
    category = value.get("category")
    if category not in {"geometry", "typography", "colourEffects", "imageCrop", "details"}:
        raise AdTemplateProcessError("review issue category is invalid")
    instruction = value.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 1200:
        raise AdTemplateProcessError("review issue instruction is invalid")
    target_field = re.search(
        r"\b(?:x|y|width|height|font|fontSize|fontFamily|fontWeight|lineHeight|tracking|colour|color|crop)\b",
        instruction,
        flags=re.IGNORECASE,
    )
    target_value = re.search(r"(?:#[0-9a-f]{3,8}\b|[-+]?\d+(?:\.\d+)?(?:px|%)?|/fonts/\S+)", instruction, flags=re.IGNORECASE)
    if not target_field or not target_value:
        raise AdTemplateProcessError(
            "review issue instruction requires a concrete field and numeric, colour, crop or font target"
        )
    if value.get("severity") not in {"blocker", "material", "minor"}:
        raise AdTemplateProcessError("review issue severity is invalid")
    return copy.deepcopy(value)


def validate_review(value: Any) -> Dict[str, Any]:
    required = {"decision", "scores", "issues", "warnings", "effects", "fontSubstitution"}
    if not isinstance(value, dict) or set(value) != required:
        raise AdTemplateProcessError("visual review has an invalid shape")
    scores = value.get("scores")
    if not isinstance(scores, dict) or set(scores) != set(SCORE_FIELDS):
        raise AdTemplateProcessError("visual review scores must use the exact ordered rubric")
    raw_score_values = [scores[field] for field in SCORE_FIELDS]
    unit_scale = all(
        not isinstance(raw, bool)
        and isinstance(raw, (int, float))
        and 0 <= float(raw) <= 1
        for raw in raw_score_values
    )
    normalized_scores: Dict[str, float] = {}
    for field in SCORE_FIELDS:
        raw = scores[field]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 0 <= float(raw) <= 10:
            raise AdTemplateProcessError("visual review score must be between 0 and 10")
        normalized_scores[field] = round(float(raw) * (10 if unit_scale else 1), 2)
    effects = value.get("effects")
    if not isinstance(effects, dict) or set(effects) != set(EFFECT_FIELDS) or any(state not in _EFFECT_STATES for state in effects.values()):
        raise AdTemplateProcessError("visual effects review must explicitly cover every effect")
    issues = value.get("issues")
    if not isinstance(issues, list) or len(issues) > 64:
        raise AdTemplateProcessError("visual review issues are invalid")
    normalized_issues = [_validate_issue(item) for item in issues]
    warnings = value.get("warnings")
    if not isinstance(warnings, list) or len(warnings) > 32 or any(not isinstance(item, str) or len(item) > 1000 for item in warnings):
        raise AdTemplateProcessError("visual review warnings are invalid")
    font_substitution = value.get("fontSubstitution")
    if font_substitution is not None:
        if not isinstance(font_substitution, dict) or set(font_substitution) != {"source", "used", "reason"} or any(not isinstance(font_substitution[key], str) or not font_substitution[key].strip() for key in font_substitution):
            raise AdTemplateProcessError("font substitution evidence is invalid")
    typography_floor = TYPOGRAPHY_SUBSTITUTION_THRESHOLD if font_substitution else LIKENESS_THRESHOLD
    passed = (
        normalized_scores["overall"] >= LIKENESS_THRESHOLD
        and normalized_scores["geometry"] >= LIKENESS_THRESHOLD
        and normalized_scores["typography"] >= typography_floor
        and normalized_scores["colourEffects"] >= LIKENESS_THRESHOLD
        and normalized_scores["imageCrop"] >= LIKENESS_THRESHOLD
        and normalized_scores["details"] >= LIKENESS_THRESHOLD
        and not normalized_issues
        and "mismatch" not in effects.values()
    )
    # The model reports evidence; the controller owns the gate decision.
    # A contradictory label must never fail or accidentally pass a run.
    expected = "accept" if passed else "revise"
    return {
        "decision": expected,
        "scores": normalized_scores,
        "issues": normalized_issues,
        "warnings": list(warnings),
        "effects": dict(effects),
        "fontSubstitution": copy.deepcopy(font_substitution),
    }


def review_prompt(*, final: bool, candidate: Mapping[str, Any], reference: Mapping[str, Any], metrics: Mapping[str, Any]) -> str:
    role = "independent final reviewer" if final else "iteration comparator"
    output_fields = (
        "decision, scores, issues, warnings, effects, fontSubstitution"
        if final else
        "decision, scores, issues, warnings, effects, fontSubstitution, patch"
    )
    patch_contract = "" if final else f"""
When revision is required, return the exact correction as patch in this same response. patch must be {{"operations":[{{"op":"replace|add|remove","path":"/template/...","value":...}}]}} with no more than {MAX_PATCH_OPERATIONS} operations and no more than {MAX_PATCH_BYTES} encoded bytes. A remove operation omits value; add/replace requires value. Every operation must directly implement a listed issue against the current candidate using an existing JSON Pointer path (add may create only an allowed missing field). Do not change schema, templateId, createdAt, asset declarations or source-free asset assignments. When the evidence passes the 9.8 gate, issues must be [] and patch must be null. Do not return a full replacement template."""
    return f"""You are one {role} for an exact-clone template. Attached images are ordered: original source, source-photo-filled Feed QA render, source-photo-filled Story QA render, then (for final review) neutral production Feed and Story renders, followed by the original-placement overlay and difference views. The original source is the ONLY design authority. Source placement is {reference["sourcePlacement"]}; it must match the source as close to pixel-for-pixel as editable reconstruction permits. The other placement is a native aspect adaptation using the measured layout plan below: preserve the source design, hierarchy, effects and image roles without stretching or cropping the whole ad. There is no separate generated-ad target and no pixel-similarity score for that different aspect ratio. Score its composition, source-design preservation and production correctness visually. Both placements must pass the same 9.8 quality gate. QA substitutes source photo crops but retains authored neutral text. Separate source likeness from production correctness. Before scoring, check the whole frame for overlapping elements, clipped or missing text, stray glyphs, illegible text and missing media. Any such defect blocks acceptance regardless of average score. Do not reward creative redesign. Missing shading, gradients, shadows, transparency, borders, masks, texture or decorative details are material defects.

Return JSON only with exactly {output_fields}. scores must contain exactly, in this order: overall, geometry, typography, colourEffects, imageCrop, details. effects must contain exactly, in this order: shading, gradients, shadows, transparency, borders, masks, texture; each is match, not_present, or mismatch. issues is a list of objects with exactly placement (feed|story|both), layerIds (real candidate layer IDs), category (geometry|typography|colourEffects|imageCrop|details), instruction, severity (blocker|material|minor). Every issue instruction must be directly patchable: name at least one exact target field (x, y, width, height, font/fontSize/fontFamily/fontWeight, lineHeight, tracking, colour, or crop) and give its measured numeric/hex/font-file target or delta from the attached overlay. Vague phrases such as "match the source", "align", or "fix spacing" without target values are invalid. Every visible discrepancy is an issue; acceptance requires issues=[] and every effect matched or genuinely absent. decision is evidence only; the controller derives accept/revise from scores, issues, effects and the font rule. An obvious defect blocks acceptance regardless of average. fontSubstitution is null or exactly {{source,used,reason}}.{patch_contract} Return no prose.

PRODUCTION SAFETY: Do not reproduce accidental source clipping, duplicate glyphs, or missing contact text as a requested correction. Match the source structure while keeping customer replacements readable; list unavoidable source defects as warnings, never a reason to damage the production template. Repeated feature wording intentionally present in the source is not itself a stray-glyph defect.
COORDINATE AND FIT RULES: Feed is exactly 1080x1350; Story is exactly 1080x1920. The original-source comparison image is normalized to its matching canvas without cropping. All geometry targets use those canvas pixels, NEVER thumbnail/display pixels. Preserve the renderer's minimum font sizes: Feed 24px and Story 32px, multiline lineHeight >= 1. Do not request a smaller font; reflow the native adaptation or resize the editable box instead. The comparison source map and render share the same coordinate scale.

AVAILABLE BUNDLED FONT FILES: {_safe_json(sorted(AVAILABLE_FONT_FILES))}. Existing declared asset-backed fonts may also be used. Never request a font file that is neither available here nor supplied by the candidate. Record unavoidable differences in fontSubstitution and choose the closest available face, weight and tracking; do not repeatedly demand unavailable proprietary fonts.
IMAGE ORDER NOTE: Neutral production images follow the three original-source/QA images and precede the original-placement overlay/difference views. Never interpret a difference heatmap as a customer preview.

RECIPROCAL ASPECT REFERENCE: {_safe_json(reference)}
DETERMINISTIC PIXEL/EDGE/COLOUR DIAGNOSTICS: {_safe_json(metrics)}. These are measured from the source-filled QA renders; vision remains the fidelity judge.
CANDIDATE CONTRACT: {_safe_json(candidate)}"""


def patch_prompt(*, candidate: Mapping[str, Any], issues: Sequence[Mapping[str, Any]], manual_instructions: str = "") -> str:
    return f"""Apply only the listed exact-clone corrections to the current valid Blockwise candidate. Do not redesign, regenerate or replace the document. Preserve every field and layer not named by the corrections. Return a bounded JSON patch only: {{"operations":[{{"op":"replace|add|remove","path":"/template/...","value":...}}]}}. Use JSON Pointer paths. Do not change schema, templateId, createdAt, asset declarations or source-free asset assignments. Maximum {MAX_PATCH_OPERATIONS} operations. A remove operation omits value; add/replace requires value. Return JSON only.

CURRENT CANDIDATE: {_safe_json(candidate)}
CORRECTIONS: {_safe_json(list(issues), max_bytes=80_000)}
MANUAL REVIEW INSTRUCTIONS: {manual_instructions[:4000]}"""


def contract_repair_prompt(*, candidate: Mapping[str, Any], reasons: Sequence[str]) -> str:
    return f"""Repair only the listed Blockwise contract/renderer validation failures in this otherwise complete exact-clone candidate. Preserve its visual design, geometry, inputs, neutral assets, copy and metadata except where a listed failure requires a direct correction. Return a bounded JSON patch only: {{"operations":[{{"op":"replace|add|remove","path":"/template/...","value":...}}]}}. Use JSON Pointer paths. Address every listed failure in this one patch. Do not return a full template and do not make creative changes. Maximum {MAX_PATCH_OPERATIONS} operations. Return JSON only.

CURRENT CANDIDATE: {_safe_json(candidate)}
BLOCKWISE CONTRACT/RENDERER FAILURES: {_safe_json(list(reasons), max_bytes=40_000)}"""


def _decode_pointer_token(token: str) -> str:
    if re.search(r"~(?![01])", token):
        raise AdTemplateProcessError("patch path contains an invalid JSON Pointer escape")
    return token.replace("~1", "/").replace("~0", "~")


def validate_patch(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"operations"}:
        raise AdTemplateProcessError("revision must return exactly operations")
    operations = value.get("operations")
    if not isinstance(operations, list) or not operations or len(operations) > MAX_PATCH_OPERATIONS:
        raise AdTemplateProcessError("revision operations must be a bounded non-empty list")
    if len(_safe_json(value).encode("utf-8")) > MAX_PATCH_BYTES:
        raise AdTemplateProcessError("revision patch exceeds the byte limit")
    normalized = []
    for operation in operations:
        if not isinstance(operation, dict):
            raise AdTemplateProcessError("revision operation must be an object")
        op = operation.get("op")
        expected = {"op", "path"} if op == "remove" else {"op", "path", "value"}
        if set(operation) != expected or op not in {"add", "replace", "remove"}:
            raise AdTemplateProcessError("revision operation has an invalid shape")
        path = operation.get("path")
        if not isinstance(path, str) or not any(path.startswith(root) for root in _MUTABLE_PATCH_ROOTS):
            raise AdTemplateProcessError("revision path is outside editable template fields")
        tokens = path.split("/")[1:]
        if len(tokens) < 3 or any(token == "" for token in tokens):
            raise AdTemplateProcessError("revision path is not a bounded field path")
        [_decode_pointer_token(token) for token in tokens]
        normalized.append(copy.deepcopy(operation))
    return {"operations": normalized}


def apply_patch(candidate: Mapping[str, Any], value: Any, *, strict: bool = True) -> Dict[str, Any]:
    patch = validate_patch(value)
    result = copy.deepcopy(dict(candidate))
    before = _safe_json(result)
    immutable = {
        "schema": result.get("template", {}).get("schema"),
        "templateId": result.get("template", {}).get("templateId"),
        "createdAt": result.get("template", {}).get("createdAt"),
        "assets": copy.deepcopy(result.get("template", {}).get("assets")),
        "declarations": copy.deepcopy(result.get("assets")),
    }
    for operation in patch["operations"]:
        tokens = [_decode_pointer_token(token) for token in operation["path"].split("/")[1:]]
        parent: Any = result
        for token in tokens[:-1]:
            if isinstance(parent, list):
                if not token.isdigit() or int(token) >= len(parent):
                    raise AdTemplateProcessError("revision path list index does not exist")
                parent = parent[int(token)]
            elif isinstance(parent, dict) and token in parent:
                parent = parent[token]
            else:
                raise AdTemplateProcessError("revision path does not exist")
        leaf = tokens[-1]
        if isinstance(parent, list):
            if operation["op"] == "add" and leaf == "-":
                parent.append(copy.deepcopy(operation["value"]))
            elif leaf.isdigit() and int(leaf) < len(parent):
                index = int(leaf)
                if operation["op"] == "remove":
                    parent.pop(index)
                elif operation["op"] == "replace":
                    parent[index] = copy.deepcopy(operation["value"])
                else:
                    parent.insert(index, copy.deepcopy(operation["value"]))
            else:
                raise AdTemplateProcessError("revision list operation is out of bounds")
        elif isinstance(parent, dict):
            if operation["op"] == "remove":
                if leaf not in parent:
                    raise AdTemplateProcessError("revision remove path does not exist")
                del parent[leaf]
            elif operation["op"] == "replace":
                if leaf not in parent:
                    raise AdTemplateProcessError("revision replace path does not exist")
                parent[leaf] = copy.deepcopy(operation["value"])
            else:
                parent[leaf] = copy.deepcopy(operation["value"])
        else:
            raise AdTemplateProcessError("revision path parent is not editable")
    if _safe_json(result) == before:
        raise AdTemplateProcessError("revision patch made no material candidate change")
    template = result.get("template") if isinstance(result.get("template"), dict) else {}
    if {
        "schema": template.get("schema"),
        "templateId": template.get("templateId"),
        "createdAt": template.get("createdAt"),
        "assets": template.get("assets"),
        "declarations": result.get("assets"),
    } != immutable:
        raise AdTemplateProcessError("revision patch changed immutable template identity or assets")
    return _candidate_envelope(result) if strict else _candidate_structure(result)


def validate_comparator_result(value: Any, *, candidate: Mapping[str, Any]) -> Dict[str, Any]:
    """Keep valid visual evidence even when a cheap model proposes a bad patch.

    A malformed review still receives the existing bounded format retry.  A
    semantically invalid patch does not: the controller can retain the scored
    evidence and route only the correction step to the strong fallback model.
    """
    review_fields = {
        "decision", "scores", "issues", "warnings", "effects",
        "fontSubstitution",
    }
    if not isinstance(value, dict) or not review_fields.issubset(value) or not set(value).issubset(review_fields | {"patch"}):
        raise AdTemplateProcessError("comparator result has an invalid shape")
    review = validate_review({field: value[field] for field in review_fields})
    raw_patch = value.get("patch")
    if review["decision"] == "accept":
        return {
            "review": review,
            "patch": None,
            "candidate": copy.deepcopy(dict(candidate)),
            "patchError": None if raw_patch is None else "accepted comparison must return patch null",
        }

    if raw_patch is None:
        return {
            "review": review,
            "patch": None,
            "candidate": copy.deepcopy(dict(candidate)),
            "patchError": "revising comparison omitted its bounded patch",
        }
    try:
        patch = validate_patch(raw_patch)
        updated = apply_patch(candidate, patch)
    except (AdTemplateProcessError, AdTemplateStructuredOutputError) as exc:
        return {
            "review": review,
            "patch": None,
            "candidate": copy.deepcopy(dict(candidate)),
            "patchError": str(exc),
        }
    return {
        "review": review,
        "patch": patch,
        "candidate": updated,
        "patchError": None,
    }


def _renderer_reasons(stderr: str, stdout: str) -> list[str]:
    text = "\n".join(part for part in (stderr, stdout) if part).strip()
    if not text:
        return []
    # The Blockwise text preflight reports every violation in one JSON line.
    # Truncating that line made the repair role see only the first field and
    # waste one bounded repair per successive field.
    return [
        line.strip()[:MAX_RENDERER_REASON_CHARS]
        for line in text.splitlines() if line.strip()
    ][-20:]


def _renderer_text_violations(reasons: Sequence[str]) -> list[Dict[str, Any]]:
    """Recover the renderer's complete machine-readable text failure set."""
    violations: list[Dict[str, Any]] = []
    decoder = json.JSONDecoder()
    marker = "AD_TEMPLATE_TEXT_PREFLIGHT_FAILED "
    for reason in reasons:
        if marker in reason:
            try:
                payload, _ = decoder.raw_decode(reason.split(marker, 1)[1].lstrip())
            except (json.JSONDecodeError, TypeError):
                continue
            items = payload.get("violations") if isinstance(payload, dict) else None
            if isinstance(items, list):
                violations.extend(copy.deepcopy(item) for item in items if isinstance(item, dict))
        else:
            match = re.fullmatch(r"Unsupported text overflow behaviour on ([A-Za-z0-9._:-]+)", reason.strip())
            if match:
                violations.append({"kind": "unsupported_overflow", "layerId": match.group(1)})
    return violations


def _expand_text_box(layer: Dict[str, Any], *, canvas_width: int, canvas_height: int, floor: float) -> None:
    geometry = layer.get("geometry")
    if not isinstance(geometry, dict):
        return
    try:
        raw = {key: float(geometry[key]) for key in ("x", "y", "width", "height")}
    except (KeyError, TypeError, ValueError):
        return
    normalized = all(abs(value) <= 1.001 for value in raw.values())
    scale_x = canvas_width if normalized else 1.0
    scale_y = canvas_height if normalized else 1.0
    x, y = raw["x"] * scale_x, raw["y"] * scale_y
    width, height = raw["width"] * scale_x, raw["height"] * scale_y
    max_lines = max(1, int(layer.get("maxLines") or 1))
    line_height = max(1.0, float(layer.get("lineHeight") or 1.0))
    target_width = min(float(canvas_width), max(width + 2 * floor, width * 1.35))
    target_height = min(
        float(canvas_height),
        max(height * 1.2, floor * (1 + (max_lines - 1) * line_height) + floor * 0.35),
    )
    alignment = layer.get("alignment")
    if alignment == "right":
        x = max(0.0, x + width - target_width)
    elif alignment == "center":
        x = max(0.0, x - (target_width - width) / 2)
    target_width = min(target_width, canvas_width - x)
    if y + target_height > canvas_height:
        y = max(0.0, canvas_height - target_height)
    values = {"x": x, "y": y, "width": target_width, "height": target_height}
    for key, value in values.items():
        converted = value / (canvas_width if key in {"x", "width"} else canvas_height) if normalized else value
        geometry[key] = round(converted, 6) if normalized else int(round(converted))


def _apply_deterministic_contract_repairs(
    candidate: Mapping[str, Any], reasons: Sequence[str], *, qa_candidate: Mapping[str, Any] | None = None,
) -> tuple[Dict[str, Any], int]:
    """Repair every mechanical text violation together without spending an LLM turn."""
    violations = _renderer_text_violations(reasons)
    if not violations:
        return copy.deepcopy(dict(candidate)), 0
    repaired = copy.deepcopy(dict(candidate))
    template = repaired.get("template") if isinstance(repaired.get("template"), dict) else {}
    qa_template = qa_candidate.get("template") if isinstance(qa_candidate, Mapping) and isinstance(qa_candidate.get("template"), dict) else {}
    qa_text = {
        item.get("key"): item.get("placeholder")
        for item in qa_template.get("textInputs", [])
        if isinstance(item, dict) and isinstance(item.get("key"), str) and isinstance(item.get("placeholder"), str)
    }
    changed_layers: set[str] = set()
    for violation in violations:
        layer_id = violation.get("layerId")
        placement = violation.get("placement")
        placements = (placement,) if placement in {"feed", "story"} else ("feed", "story")
        for current_placement in placements:
            field = "feedLayout" if current_placement == "feed" else "storyLayout"
            layout = template.get(field) if isinstance(template.get(field), dict) else {}
            layers = layout.get("layers") if isinstance(layout.get("layers"), list) else []
            layer = next((item for item in layers if isinstance(item, dict) and item.get("layerId") == layer_id), None)
            if not isinstance(layer, dict) or layer.get("type") != "text":
                continue
            before = _safe_json(layer)
            kind = violation.get("kind")
            floor = float(violation.get("readabilityFloorPx") or (24 if current_placement == "feed" else 28))
            if kind == "unsupported_overflow":
                layer["overflowBehaviour"] = "shrink"
            if _safe_json(layer) != before:
                changed_layers.add(f"{current_placement}:{layer_id}")
    return _candidate_structure(repaired), len(changed_layers)


def build_ephemeral_qa_candidate(
    candidate: Mapping[str, Any], *, source: str, reciprocal_reference: str,
    source_placement: str, source_map: Mapping[str, Any], target_map: Mapping[str, Any],
    workspace: Path,
) -> tuple[Dict[str, Any], Dict[str, bytes]]:
    """Fill QA-only layers from references without changing the shippable candidate."""
    qa = copy.deepcopy(_candidate_envelope(candidate))
    template = qa["template"]
    image_inputs = template.get("imageInputs")
    template_assets = template.get("assets")
    if not isinstance(image_inputs, list) or not isinstance(template_assets, dict):
        raise AdTemplateProcessError("template inputs and assets are required for QA projection")
    original_image_inputs = {item.get("key"): item for item in image_inputs if isinstance(item, dict)}
    override_bytes: Dict[str, bytes] = {}
    qa_root = workspace / "qa-source-overrides"
    qa_root.mkdir(parents=True, exist_ok=True)
    target_placement = "story" if source_placement == "feed" else "feed"
    placement_data = {
        source_placement: (source, source_map),
        target_placement: (reciprocal_reference, target_map),
    }
    source_only = Path(source).resolve() == Path(reciprocal_reference).resolve()
    source_layout = template["feedLayout" if source_placement == "feed" else "storyLayout"]
    source_geometries = {layer.get("inputKey"): copy.deepcopy(layer.get("geometry"))
                         for layer in source_layout["layers"]
                         if layer.get("type") in {"image_slot", "logo"}}

    for placement, layout_key in (("feed", "feedLayout"), ("story", "storyLayout")):
        layout = template.get(layout_key)
        if not isinstance(layout, dict) or not isinstance(layout.get("layers"), list):
            raise AdTemplateProcessError("template layout is invalid for QA projection")
        reference_path, placement_map = placement_data[placement]
        with Image.open(reference_path) as opened:
            reference_image = ImageOps.exif_transpose(opened).convert("RGB")
        canvas_width, canvas_height = (1080, 1350 if placement == "feed" else 1920)
        for index, layer in enumerate(layout["layers"]):
            if not isinstance(layer, dict):
                continue
            layer_type = layer.get("type")
            input_key = layer.get("inputKey")
            geometry = layer.get("geometry") if isinstance(layer.get("geometry"), dict) else {}
            if layer_type in {"image_slot", "logo"} and input_key in original_image_inputs:
                crop_width, crop_height = canvas_width, canvas_height
                if source_only and placement != source_placement:
                    geometry = source_geometries.get(input_key)
                    if not isinstance(geometry, dict):
                        continue
                    crop_height = 1350 if source_placement == "feed" else 1920
                geometry = dict(geometry)
                try:
                    if all(abs(float(geometry.get(key, 2))) <= 1.001 for key in ("x", "y", "width", "height")):
                        for key in ("x", "width"): geometry[key] *= crop_width
                        for key in ("y", "height"): geometry[key] *= crop_height
                    left = max(0, round(float(geometry["x"]) * reference_image.width / crop_width))
                    top = max(0, round(float(geometry["y"]) * reference_image.height / crop_height))
                    right = min(reference_image.width, round((float(geometry["x"]) + float(geometry["width"])) * reference_image.width / crop_width))
                    bottom = min(reference_image.height, round((float(geometry["y"]) + float(geometry["height"])) * reference_image.height / crop_height))
                except (KeyError, TypeError, ValueError):
                    continue
                if right <= left or bottom <= top:
                    continue
                crop = reference_image.crop((left, top, right, bottom))
                buffer = io.BytesIO()
                crop.save(buffer, format="PNG", optimize=True)
                asset_key = f"qa-{placement}-{index}"
                file_name = f"qa/{placement}-{index}.png"
                input_clone = copy.deepcopy(original_image_inputs[input_key])
                qa_input_key = f"qa_{placement}_{index}_{input_key}"[:120]
                input_clone["key"] = qa_input_key
                input_clone["defaultAssetKey"] = asset_key
                image_inputs.append(input_clone)
                layer["inputKey"] = qa_input_key
                template_assets[asset_key] = {"fileName": file_name, "mimeType": "image/png"}
                qa["assets"].append({"assetKey": asset_key, "fileName": file_name, "mimeType": "image/png"})
                override_bytes[asset_key] = buffer.getvalue()
            # OCR remains source-map evidence only. Keep candidate-authored
            # neutral text in QA so words cannot merge or mutate layout.
    return _candidate_envelope(qa), override_bytes


def prepare_demo_assets(candidate, *, source, source_placement, workspace, route, call_image_model, emit):
    """Generate photo defaults once; preserve a per-run plan across retries."""
    document = copy.deepcopy(candidate)
    template = document["template"]
    root = (workspace / "demo-assets").resolve()
    root.mkdir(parents=True, exist_ok=True)
    plan_path = root / "plan.json"
    if plan_path.exists():
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if not isinstance(plan, dict) or not isinstance(plan.get("assets"), list):
            raise AdTemplateProcessError("demo asset plan is invalid")
    else:
        inputs = {item["key"]: item for item in template["imageInputs"]}
        declarations = {item["assetKey"]: item for item in document["assets"]}
        catalog = _runtime_catalog()
        layout = template["feedLayout" if source_placement == "feed" else "storyLayout"]
        planned = {}
        for layer in layout["layers"]:
            if layer.get("type") != "image_slot":
                continue
            item = inputs.get(layer.get("inputKey"), {})
            key = item.get("defaultAssetKey")
            declared = declarations.get(key, {})
            asset = catalog.assets.get(declared.get("fileName"))
            if key in planned or asset is None or asset.usage != "photo-default":
                continue
            planned[key] = {
                "assetKey": key, "file": f"photo-{len(planned)}.png",
                "label": str(item.get("label") or "Property photograph")[:200],
                "geometry": copy.deepcopy(layer["geometry"]),
            }
        if len(planned) > 8:
            raise AdTemplateProcessError("demo photo plan exceeds eight assets")
        plan = {"route": dict(route), "placement": source_placement, "assets": list(planned.values())}
        temporary = plan_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(plan), encoding="utf-8")
        os.replace(temporary, plan_path)
    if plan.get("route") != dict(route):
        raise AdTemplateProcessError("demo photo route changed; resume with the original model")
    declarations = {item["assetKey"]: item for item in document["assets"]}
    overrides = {}
    with Image.open(source) as original:
        reference = ImageOps.exif_transpose(original).convert("RGB")
    canvas_width, canvas_height = (1080, 1350 if source_placement == "feed" else 1920)
    for item in plan["assets"]:
        key = item["assetKey"]
        if key not in declarations or key not in template["assets"]:
            raise AdTemplateProcessError("demo photo asset key changed during revision")
        target = (root / item["file"]).resolve()
        if target.parent != root or target.suffix != ".png":
            raise AdTemplateProcessError("demo photo path escaped its run")
        pending = target.with_suffix(".pending")
        if not target.exists():
            if pending.exists():
                raise AdTemplateProcessError("demo photo call outcome is unknown; inspect its provider receipt before retry")
            geometry = item["geometry"]
            x, y, width, height = (float(geometry[field]) for field in ("x", "y", "width", "height"))
            if max(abs(x), abs(y), abs(width), abs(height)) <= 1.001:
                x, width, y, height = x * canvas_width, width * canvas_width, y * canvas_height, height * canvas_height
            box = (max(0, round(x * reference.width / canvas_width)), max(0, round(y * reference.height / canvas_height)),
                   min(reference.width, round((x + width) * reference.width / canvas_width)),
                   min(reference.height, round((y + height) * reference.height / canvas_height)))
            if box[2] <= box[0] or box[3] <= box[1]:
                raise AdTemplateProcessError("demo photo reference crop is empty")
            crop_path = target.with_name(target.stem + "-reference.png")
            reference.crop(box).save(crop_path, format="PNG")
            prompt = (
                "Create one photorealistic fictional property photograph for an editable ad template. "
                "Use the attached crop only as a composition, lighting, camera angle and subject-type reference. "
                "Create a different property, not an exact reproduction. No text, logo, watermark, contact details, "
                "people, collage, border, or ad interface. This is only the photo asset. "
                f"Slot: {item['label']}. Match the reference crop aspect ratio."
            )
            pending.write_text("Provider call started; do not blindly repeat.", encoding="utf-8")
            emit("asset.generation-started", "build", {"assetKey": key, "provider": route.get("provider"), "model": route.get("model")})
            raw = Path(call_image_model(prompt, str(crop_path), source_placement, route))
            with Image.open(raw) as generated:
                if generated.width < 64 or generated.height < 64 or generated.width * generated.height > 40_000_000:
                    raise AdTemplateProcessError("generated demo photo has invalid dimensions")
                temporary = target.with_suffix(".tmp")
                ImageOps.exif_transpose(generated).convert("RGB").save(temporary, format="PNG", optimize=True)
                os.replace(temporary, target)
            pending.unlink()
            emit("asset.generated", "build", {"assetKey": key, "fileName": item["file"]})
        with Image.open(target) as verified:
            verified.verify()
        overrides[key] = target.read_bytes()
        declaration = {"fileName": f"demo/{item['file']}", "mimeType": "image/png"}
        template["assets"][key] = declaration
        declarations[key].update(declaration)
    return document, overrides

def _canonical_catalog_paths(candidate: Mapping[str, Any]) -> Dict[str, Any]:
    """Resolve unambiguous catalog basenames; never guess or change asset identity."""
    result = copy.deepcopy(candidate)
    catalog = _runtime_catalog()
    for declaration in result["assets"]:
        name = declaration["fileName"]
        if name in catalog.assets or "/" in name or "\\" in name:
            continue
        matches = [asset for asset in catalog.assets.values()
                   if Path(asset.file_name).name == name and asset.mime_type == declaration["mimeType"]]
        if len(matches) != 1:
            continue  # Unknown/ambiguous references still fail catalog validation.
        linked = result["template"]["assets"].get(declaration["assetKey"])
        if not isinstance(linked, dict) or linked.get("fileName") != name or linked.get("mimeType") != declaration["mimeType"]:
            continue  # Do not conceal disagreement between the two declarations.
        declaration["fileName"] = matches[0].file_name
        linked["fileName"] = matches[0].file_name
    return result


def _resolve_runtime_assets(document: Mapping[str, Any], overrides: Mapping[str, bytes] | None = None) -> list[dict[str, Any]]:
    overrides = dict(overrides or {})
    declarations = list(document["assets"])
    try:
        resolved = list(resolve_declared_assets(
            _runtime_catalog(),
            [item for item in declarations if item.get("assetKey") not in overrides],
        ))
    except CatalogIntegrityError as exc:
        raise AdTemplateProcessError(f"safe asset resolution failed: {exc}") from exc
    by_key = {item.get("assetKey"): item for item in declarations if isinstance(item, dict)}
    for key, payload in overrides.items():
        declaration = by_key.get(key)
        if not isinstance(declaration, dict) or declaration.get("mimeType") != "image/png":
            raise AdTemplateProcessError("run-local asset override must be declared as PNG")
        resolved.append({"assetKey": key, "fileName": declaration["fileName"], "mimeType": "image/png", "bytesBase64": base64.b64encode(payload).decode("ascii")})
    return resolved


def run_renderer(candidate: Mapping[str, Any], workspace: Path, *, asset_overrides: Mapping[str, bytes] | None = None) -> Dict[str, Any]:
    command = os.environ.get("AD_TEMPLATE_GENERATOR_CMD", "").strip()
    if not command:
        raise AdTemplateProcessError("AD_TEMPLATE_GENERATOR_CMD must point to the shared Blockwise renderer CLI")
    document = _candidate_envelope(candidate)
    overrides = dict(asset_overrides or {})
    resolved = _resolve_runtime_assets(document, overrides)
    workspace.mkdir(parents=True, exist_ok=True)
    artifact_path = workspace / "artifact.json"
    temporary = artifact_path.with_suffix(".json.tmp")
    temporary.write_text(_safe_json({"template": document["template"], "assets": resolved}, max_bytes=20_000_000), encoding="utf-8")
    os.replace(temporary, artifact_path)
    output_root = workspace / "rendered"
    proc = subprocess.run(
        shlex.split(command) + ["--input", str(artifact_path), "--assets-dir", str(workspace), "--out-dir", str(output_root)],
        text=True,
        capture_output=True,
        timeout=600,
        check=False,
    )
    if proc.returncode:
        reasons = _renderer_reasons(proc.stderr, proc.stdout)
        if reasons:
            raise AdTemplateRendererRejection(reasons)
        raise AdTemplateProcessError(f"shared Blockwise renderer failed ({proc.returncode})")
    receipt_path = output_root / "receipt.json"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdTemplateProcessError("shared renderer returned no valid receipt") from exc
    outputs = receipt.get("outputs") if isinstance(receipt, dict) else None
    if not isinstance(outputs, dict):
        raise AdTemplateProcessError("shared renderer receipt is incomplete")
    render: Dict[str, str] = {}
    previews: list[dict[str, Any]] = []
    for placement in ("feed", "story"):
        item = outputs.get(placement)
        path = Path(str(item.get("path") if isinstance(item, dict) else "")).resolve()
        if not path.is_file():
            raise AdTemplateProcessError("shared renderer output is missing")
        try:
            path.relative_to(workspace.resolve())
        except ValueError as exc:
            raise AdTemplateProcessError("shared renderer output escaped the run workspace") from exc
        render[placement] = str(path)
        previews.append({"name": path.name, "path": str(path), "placement": placement})
    review_previews: list[dict[str, str]] = []
    for index, item in enumerate(receipt.get("reviewPreviews") or []):
        path = Path(str(item.get("path") if isinstance(item, dict) else "")).resolve()
        if not path.is_file():
            raise AdTemplateProcessError("shared renderer review preview is missing")
        try:
            path.relative_to(workspace.resolve())
        except ValueError as exc:
            raise AdTemplateProcessError("shared renderer review preview escaped the run workspace") from exc
        review_previews.append({
            "name": str(item.get("name") or path.name),
            "path": str(path),
            "placement": str(item.get("placement") or f"meta-shell-{index + 1}"),
        })
    return {
        "candidate": document,
        "resolved_assets": resolved,
        "render": render,
        "previews": previews,
        "review_previews": review_previews,
        "receipt": receipt,
        "template_path": str(artifact_path),
    }


def _copy_public_previews(rendered: Mapping[str, Any], workspace: Path, iteration: int, *, kind: str = "qa-source-filled") -> Dict[str, Any]:
    root = workspace / "previews"
    root.mkdir(parents=True, exist_ok=True)
    public_render: Dict[str, str] = {}
    public_previews: list[dict[str, str]] = []
    for placement in ("feed", "story"):
        source = Path(str(rendered["render"][placement]))
        target = root / f"iteration-{iteration:02d}-{placement}.png"
        temporary = target.with_suffix(".png.tmp")
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)
        public_render[placement] = str(target)
        public_previews.append({"name": target.name, "path": str(target), "placement": placement, "kind": kind})
    for index, item in enumerate(rendered.get("review_previews") or [], 1):
        source = Path(str(item["path"]))
        suffix = source.suffix.lower() if source.suffix else ".png"
        target = root / f"iteration-{iteration:02d}-meta-{index}{suffix}"
        temporary = target.with_suffix(target.suffix + ".tmp")
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)
        public_previews.append({"name": target.name, "path": str(target), "placement": str(item.get("placement") or "meta-shell"), "kind": kind})
    result = dict(rendered)
    result["render"] = public_render
    result["previews"] = public_previews
    return result


def _comparison_views(
    source: str, reciprocal_reference: str, rendered: Mapping[str, Any],
    workspace: Path, iteration: int, source_placement: str, target_placement: str,
) -> list[dict[str, str]]:
    root = workspace / "previews"
    try:
        records = []
        for placement, reference_path in (
            (source_placement, source), (target_placement, reciprocal_reference),
        ):
            if placement == target_placement and Path(source).resolve() == Path(reciprocal_reference).resolve():
                continue  # No fabricated pixel target for a different aspect ratio.
            current_path = Path(str(rendered["render"][placement]))
            with Image.open(reference_path) as source_image, Image.open(current_path) as current_image:
                reference = source_image.convert("RGB").resize(current_image.size, Image.Resampling.LANCZOS)
                current = current_image.convert("RGB")
                products = {
                    "overlay": Image.blend(reference, current, 0.5),
                    "difference": ImageChops.difference(reference, current),
                    "reference-edges": reference.filter(ImageFilter.FIND_EDGES),
                    "render-edges": current.filter(ImageFilter.FIND_EDGES),
                }
            for name, image in products.items():
                target = root / f"iteration-{iteration:02d}-{placement}-{name}.png"
                image.save(target, format="PNG")
                records.append({"name": target.name, "path": str(target), "placement": placement, "kind": name})
        return records
    except (OSError, UnidentifiedImageError) as exc:
        raise AdTemplateProcessError("comparison views could not be generated") from exc


def _vision_paths(source: str, reciprocal_reference: str, rendered: Mapping[str, Any], comparisons: Sequence[Mapping[str, str]], production_rendered: Mapping[str, Any] | None = None) -> list[str]:
    paths = [source, reciprocal_reference, rendered["render"]["feed"], rendered["render"]["story"]]
    if production_rendered is not None:
        paths.extend((production_rendered["render"]["feed"], production_rendered["render"]["story"]))
    # Pixel overlays and absolute differences expose the material visual delta;
    # edge similarity is already supplied as deterministic numeric evidence.
    # Bounding the image set keeps every role request inside one stable vision
    # payload instead of silently losing later images at provider limits.
    paths.extend(
        item["path"] for item in comparisons
        if item.get("kind") in {"overlay", "difference"}
    )
    return list(dict.fromkeys(str(path) for path in paths if path))


def _comparison_metrics(
    *, source: str, reciprocal_reference: str, source_placement: str,
    target_placement: str, rendered: Mapping[str, Any],
) -> Dict[str, Any]:
    render = rendered.get("render") if isinstance(rendered.get("render"), Mapping) else {}
    source_render = render.get(source_placement)
    target_render = render.get(target_placement)
    if not isinstance(source_render, str) or not isinstance(target_render, str):
        raise AdTemplateProcessError("native placement renders are unavailable for comparison")
    return {
        source_placement: deterministic_pixel_metrics(source, source_render),
        target_placement: ({"mode": "native-reflow", "pixelComparison": False}
                           if Path(source).resolve() == Path(reciprocal_reference).resolve()
                           else deterministic_pixel_metrics(reciprocal_reference, target_render)),
    }


def _layer_summary(template: Mapping[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for placement, key in (("feed", "feedLayout"), ("story", "storyLayout")):
        layers = ((template.get(key) or {}).get("layers") or []) if isinstance(template.get(key), dict) else []
        counts: Dict[str, int] = {}
        for layer in layers:
            if isinstance(layer, dict):
                kind = str(layer.get("type") or "unknown")
                counts[kind] = counts.get(kind, 0) + 1
        result[placement] = {
            "count": len(layers),
            "types": counts,
            "ordered": [
                {
                    "layerId": str(layer.get("layerId") or ""),
                    "type": str(layer.get("type") or "unknown"),
                    "inputKey": str(layer.get("inputKey") or "") or None,
                    "geometry": copy.deepcopy(layer.get("geometry")),
                }
                for layer in layers if isinstance(layer, dict)
            ],
        }
    return result


def _checkpoint_path(workspace: Path) -> Path:
    return workspace / "exact-clone-checkpoint.json"


def load_checkpoint(workspace: Path) -> Dict[str, Any]:
    path = _checkpoint_path(workspace)
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdTemplateProcessError("exact-clone checkpoint is invalid") from exc
    if not isinstance(value, dict) or value.get("process") != PROCESS_ID:
        raise AdTemplateProcessError("exact-clone checkpoint has an invalid process")
    return value


def _checkpoint_candidate(checkpoint: Dict[str, Any]) -> Dict[str, Any] | None:
    candidate = checkpoint.get("candidate")
    if not candidate:
        return None
    try:
        # Preserve a direct-contract candidate that only needs a bounded
        # renderer/schema patch. Old envelopes are discarded and rebuilt.
        return _candidate_structure(candidate)
    except AdTemplateProcessError:
        # Old/incomplete model envelopes are not valid current candidates.
        # Preserve expensive deterministic/reference work, but force the
        # bounded builder role to return the direct Blockwise contract.
        checkpoint.pop("candidate", None)
        checkpoint["iterations"] = []
        checkpoint["cycleComparisons"] = 0
        return None


def _comparison_budget_used(checkpoint: Mapping[str, Any], iterations: Sequence[Any]) -> int:
    """Read the lifetime budget; legacy checkpoints derive it once from history."""
    if "comparisonBudgetUsed" in checkpoint:
        return max(0, int(checkpoint.get("comparisonBudgetUsed") or 0))
    return len(iterations)


def _review_quality(review: Mapping[str, Any]) -> tuple[float, float]:
    scores = review.get("scores") if isinstance(review.get("scores"), Mapping) else {}
    values = [float(scores.get(field, 0.0)) for field in SCORE_FIELDS]
    return (float(scores.get("overall", 0.0)), min(values, default=0.0))


def _review_regressed(current: Mapping[str, Any], best: Mapping[str, Any]) -> bool:
    current_quality = _review_quality(current)
    best_quality = _review_quality(best)
    return (
        current_quality[0] < best_quality[0] - REGRESSION_EPSILON
        or (
            abs(current_quality[0] - best_quality[0]) <= REGRESSION_EPSILON
            and current_quality[1] < best_quality[1] - REGRESSION_EPSILON
        )
    )


def _review_improved(current: Mapping[str, Any], best: Mapping[str, Any]) -> bool:
    current_quality = _review_quality(current)
    best_quality = _review_quality(best)
    return (
        current_quality[0] > best_quality[0] + REGRESSION_EPSILON
        or (
            abs(current_quality[0] - best_quality[0]) <= REGRESSION_EPSILON
            and current_quality[1] > best_quality[1] + REGRESSION_EPSILON
        )
    )


def _neutral_candidate_from_iteration(workspace: Path, iteration: int) -> Dict[str, Any] | None:
    """Recover the neutral candidate from a persisted QA renderer input."""
    artifact = workspace / "iterations" / f"{iteration:02d}" / "artifact.json"
    try:
        payload = json.loads(artifact.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    template = payload.get("template") if isinstance(payload, dict) else None
    if not isinstance(template, dict):
        return None
    template = copy.deepcopy(template)
    template["imageInputs"] = [
        item for item in template.get("imageInputs", [])
        if isinstance(item, dict) and not str(item.get("key") or "").startswith("qa_")
    ]
    template["textInputs"] = [
        item for item in template.get("textInputs", [])
        if isinstance(item, dict) and not str(item.get("key") or "").startswith("qa_")
    ]
    template["assets"] = {
        key: value for key, value in (template.get("assets") or {}).items()
        if not str(key).startswith("qa-")
    }
    for layout_key in ("feedLayout", "storyLayout"):
        for layer in (template.get(layout_key) or {}).get("layers", []):
            if not isinstance(layer, dict):
                continue
            input_key = str(layer.get("inputKey") or "")
            match = re.match(r"^qa_(?:feed|story)_\d+_(.+)$", input_key)
            if match:
                layer["inputKey"] = match.group(1)
    declarations = [
        {"assetKey": key, "fileName": value["fileName"], "mimeType": value["mimeType"]}
        for key, value in template["assets"].items()
        if isinstance(value, dict) and set(value) == {"fileName", "mimeType"}
    ]
    try:
        return _candidate_envelope({"template": template, "assets": declarations})
    except AdTemplateProcessError:
        return None


def persist_checkpoint(workspace: Path, value: Mapping[str, Any]) -> None:
    previous = load_checkpoint(workspace)
    budget = ({"comparisonBudgetUsed": previous["comparisonBudgetUsed"]}
              if "comparisonBudgetUsed" in previous else {})
    payload = {
        **budget,
        **copy.deepcopy(dict(value)),
        "process": PROCESS_ID,
        "qaProjectionVersion": QA_PROJECTION_VERSION,
        "evaluationPolicyVersion": EVALUATION_POLICY_VERSION,
    }
    target = _checkpoint_path(workspace)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(_safe_json(payload, max_bytes=2_000_000), encoding="utf-8")
    os.replace(temporary, target)


def request_checkpoint_revision(workspace: Path, instructions: str) -> Dict[str, Any]:
    if not isinstance(instructions, str) or not instructions.strip() or len(instructions) > 4000:
        raise AdTemplateProcessError("review instructions must be a bounded non-empty string")
    checkpoint = load_checkpoint(workspace)
    if not checkpoint.get("candidate"):
        raise AdTemplateProcessError("reviewed candidate checkpoint is unavailable")
    checkpoint.update(
        manualInstructions=instructions.strip(),
        accepted=False,
        finalReview=None,
        cycleComparisons=0,
        comparisonBudgetUsed=0,
        manualRevision=int(checkpoint.get("manualRevision") or 0) + 1,
    )
    persist_checkpoint(workspace, checkpoint)
    return checkpoint


def deterministic_documents(template: Any) -> Dict[str, str]:
    if not isinstance(template, dict) or template.get("schema") != "blockwise.ad-template":
        raise AdTemplateProcessError("template must use blockwise.ad-template")
    feed, story = template.get("feedLayout"), template.get("storyLayout")
    if not isinstance(feed, dict) or not isinstance(story, dict):
        raise AdTemplateProcessError("template must contain Feed and Story layouts")
    return {
        "feed.json": _safe_json(feed, max_bytes=2_000_000),
        "story.json": _safe_json(story, max_bytes=2_000_000),
        "template.json": _safe_json(template, max_bytes=4_000_000),
    }


def _signing_headers(url: str, body: bytes, *, scope: str) -> Dict[str, str]:
    secret = (os.environ.get("BLOCKWISE_INTERNAL_AUTH_SECRET") or "").strip()
    if not secret:
        raise AdTemplateProcessError("BLOCKWISE_INTERNAL_AUTH_SECRET is required")
    timestamp = str(int(time.time()))
    nonce = os.urandom(16).hex()
    parsed = urllib.parse.urlsplit(url)
    request_path = parsed.path or "/"
    if parsed.query:
        request_path = f"{request_path}?{parsed.query}"
    digest = hashlib.sha256(body).hexdigest()
    signed = "\n".join(("v1", timestamp, nonce, scope, "POST", request_path, digest))
    signature = hmac.new(secret.encode(), signed.encode(), hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Blockwise-Timestamp": timestamp,
        "X-Blockwise-Nonce": nonce,
        "X-Blockwise-Scope": scope,
        "X-Blockwise-Signature": signature,
    }
    host = os.environ.get("BLOCKWISE_TEMPLATE_IMPORT_HOST", "").strip()
    if host:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(?::[0-9]{1,5})?", host):
            raise AdTemplateProcessError("BLOCKWISE_TEMPLATE_IMPORT_HOST is invalid")
        headers["Host"] = host
    return headers


def _post_blockwise(url: str, payload: Mapping[str, Any], *, scope: str) -> Dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    request = urllib.request.Request(url, data=body, headers=_signing_headers(url, body, scope=scope), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read().decode("utf-8")).get("error")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            error = "http_error"
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(error))[:120]
        raise AdTemplateProcessError(f"Blockwise request failed ({exc.code}: {safe})") from exc
    except (urllib.error.URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdTemplateProcessError("Blockwise request failed") from exc
    if not isinstance(value, dict):
        raise AdTemplateProcessError("Blockwise returned an invalid response")
    return value


def import_template(output: Mapping[str, Any], *, run_id: str, project_id: str, asset_overrides: Mapping[str, bytes] | None = None) -> Dict[str, Any]:
    del project_id
    url = os.environ.get("BLOCKWISE_TEMPLATE_IMPORT_URL", "").strip()
    if not url:
        raise AdTemplateProcessError("BLOCKWISE_TEMPLATE_IMPORT_URL is required")
    template = output.get("template")
    declarations = output.get("assets")
    candidate = _candidate_envelope({"template": template, "assets": declarations})
    resolved = _resolve_runtime_assets(candidate, asset_overrides)
    result = _post_blockwise(
        url,
        {"template": candidate["template"], "assets": resolved},
        scope="adstudio.templates",
    )
    expected_id = str(candidate["template"].get("templateId") or "")
    if str(result.get("templateId") or "") != expected_id:
        raise AdTemplateProcessError("Blockwise import templateId mismatch")
    if result.get("libraryStatus") != "quarantined":
        raise AdTemplateProcessError("Blockwise import did not remain quarantined")
    asset_count = result.get("assetCount")
    if (
        isinstance(asset_count, bool)
        or not isinstance(asset_count, int)
        or asset_count != len(resolved)
    ):
        raise AdTemplateProcessError("Blockwise import asset count mismatch")
    return {
        "template_id": str(result["templateId"]),
        "status": "replayed" if result.get("replayed") else "imported",
        "asset_count": len(resolved),
        "replayed": bool(result.get("replayed")),
        "library_status": "quarantined",
        "run_id": run_id,
    }


def review_template_action(*, template_id: str, run_id: str, action: str, reason: str = "") -> Dict[str, Any]:
    if action not in {"smoke_test", "activate", "discard"}:
        raise AdTemplateProcessError("unsupported Blockwise template review action")
    base = os.environ.get("BLOCKWISE_TEMPLATE_IMPORT_URL", "").strip()
    if not base:
        raise AdTemplateProcessError("BLOCKWISE_TEMPLATE_IMPORT_URL is required")
    parsed = urllib.parse.urlsplit(base)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.query or parsed.fragment
        or parsed.path.rstrip("/") != "/api/internal/adstudio/template-artifacts"
    ):
        raise AdTemplateProcessError("BLOCKWISE_TEMPLATE_IMPORT_URL is not the exact internal template route")
    safe_id = urllib.parse.quote(template_id, safe="")
    url = f"{base.rstrip('/')}/{safe_id}/review"
    body: Dict[str, Any] = {"action": action, "runId": run_id}
    if reason:
        body["reason"] = reason[:1000]
    result = _post_blockwise(url, body, scope="adstudio.templates.review")
    if str(result.get("templateId") or "") != template_id:
        raise AdTemplateProcessError("Blockwise review response templateId mismatch")
    return result


def _call_json(
    call_agent: Callable[[str, Any, str], Dict[str, Any]],
    *, instance: str, prompt: str, paths: Sequence[str], route: Mapping[str, str],
    validate: Callable[[Any], Dict[str, Any]], emit: Callable[[str, str, Dict[str, Any]], None],
) -> Dict[str, Any]:
    rejection = ""
    for attempt in range(MAX_OUTPUT_RETRIES + 1):
        suffix = "" if not rejection else f"\n\nYour response was rejected: {rejection}. Return the complete corrected JSON object only."
        try:
            raw = call_agent(
                instance if attempt == 0 else f"{instance}-format-retry",
                vision_message(prompt + suffix, list(dict.fromkeys(paths)), bounded=True),
                f"{route.get('provider')}/{route.get('model')}",
            )
            return validate(raw)
        except (AdTemplateProcessError, AdTemplateStructuredOutputError) as exc:
            rejection = str(exc)
            if attempt >= MAX_OUTPUT_RETRIES:
                raise
            emit("role.output-retried", "build", {"role": instance, "reason": rejection, "attempt": attempt + 1})
    raise AssertionError("bounded output loop did not terminate")


def _call_applied_patch(
    call_agent: Callable[[str, Any, str], Dict[str, Any]],
    *, instance: str, prompt: str, paths: Sequence[str], route: Mapping[str, str],
    candidate: Mapping[str, Any], emit: Callable[[str, str, Dict[str, Any]], None],
    strict: bool = True,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    def validate_and_apply(value: Any) -> Dict[str, Any]:
        patch = validate_patch(value)
        updated = apply_patch(candidate, patch, strict=strict)
        return {"patch": patch, "candidate": updated}

    rejection = ""
    for replan in range(MAX_PATCH_REPLANS + 1):
        replan_prompt = prompt
        if rejection:
            replan_prompt += (
                "\n\nThe previous patch and its format retry were rejected by the current document: "
                f"{rejection}. Re-plan from the CURRENT CANDIDATE above. Every replace/remove path must "
                "already exist exactly in that candidate; use add only for an allowed missing field. "
                "Return a different complete patch, not the rejected operations."
            )
        try:
            result = _call_json(
                call_agent,
                instance=instance if replan == 0 else f"{instance}-replan-{replan}",
                prompt=replan_prompt,
                paths=paths,
                route=route,
                validate=validate_and_apply,
                emit=emit,
            )
            return result["patch"], result["candidate"]
        except (AdTemplateProcessError, AdTemplateStructuredOutputError) as exc:
            rejection = str(exc)
            if replan < MAX_PATCH_REPLANS:
                emit("revision.replanned", "build", {
                    "role": instance, "attempt": replan + 1, "reason": rejection,
                })
                continue
            emit("revision.skipped", "build", {
                "role": instance, "reason": rejection,
                "bounded_replans": MAX_PATCH_REPLANS,
            })
            return {"operations": []}, copy.deepcopy(dict(candidate))
    raise AssertionError("bounded patch re-plan loop did not terminate")


def _generation_review(
    *, source_placement: str, target_placement: str, comparator: Mapping[str, Any],
    final_review: Mapping[str, Any], warnings: Sequence[str],
) -> Dict[str, Any]:
    return {
        "process": PROCESS_ID,
        "sourcePlacement": source_placement,
        "targetPlacement": target_placement,
        "likenessThreshold": LIKENESS_THRESHOLD,
        "comparator": {
            "overall": comparator["scores"]["overall"],
            "geometry": comparator["scores"]["geometry"],
            "colourEffects": comparator["scores"]["colourEffects"],
            "compositionCrop": comparator["scores"]["imageCrop"],
            "typography": comparator["scores"]["typography"],
            "decision": "ready" if comparator["decision"] == "accept" else "revise",
        },
        "finalReviewers": [
            {
                "id": item["id"],
                "route": item["route"],
                "overall": item["scores"]["overall"],
                "minimum": min(item["scores"].values()),
                "decision": "pass" if item["decision"] == "accept" else "fail",
            }
            for item in final_review["reviewers"]
        ],
        "warnings": list(dict.fromkeys(str(item) for item in warnings if str(item).strip())),
        "fontSubstitution": comparator.get("fontSubstitution"),
    }


def validate_generation_review(value: Any) -> Dict[str, Any]:
    required = {
        "process", "sourcePlacement", "targetPlacement", "likenessThreshold",
        "comparator", "finalReviewers", "warnings", "fontSubstitution",
    }
    if not isinstance(value, dict) or set(value) != required or value.get("process") != PROCESS_ID:
        raise AdTemplateProcessError("generationReview has an invalid exact-clone shape")
    if {value.get("sourcePlacement"), value.get("targetPlacement")} != {"feed", "story"}:
        raise AdTemplateProcessError("generationReview placements must be reciprocal")
    if value.get("likenessThreshold") != LIKENESS_THRESHOLD:
        raise AdTemplateProcessError("generationReview likeness threshold is invalid")
    comparator = value.get("comparator")
    comparator_fields = {
        "overall", "geometry", "colourEffects", "compositionCrop", "typography", "decision",
    }
    if not isinstance(comparator, dict) or set(comparator) != comparator_fields or comparator.get("decision") != "ready":
        raise AdTemplateProcessError("generationReview comparator is invalid")
    if any(
        isinstance(comparator[field], bool) or not isinstance(comparator[field], (int, float))
        for field in comparator_fields - {"decision"}
    ):
        raise AdTemplateProcessError("generationReview comparator scores are invalid")
    font_substitution = value.get("fontSubstitution")
    if font_substitution is not None and (
        not isinstance(font_substitution, dict)
        or set(font_substitution) != {"source", "used", "reason"}
        or any(
            not isinstance(font_substitution[field], str)
            or not font_substitution[field].strip()
            for field in ("source", "used", "reason")
        )
    ):
        raise AdTemplateProcessError("generationReview font substitution is invalid")
    typography_floor = (
        TYPOGRAPHY_SUBSTITUTION_THRESHOLD
        if font_substitution else LIKENESS_THRESHOLD
    )
    score_values = {
        field: float(comparator[field])
        for field in comparator_fields - {"decision"}
    }
    if (
        any(score > 10 for score in score_values.values())
        or score_values["overall"] < LIKENESS_THRESHOLD
        or score_values["geometry"] < LIKENESS_THRESHOLD
        or score_values["colourEffects"] < LIKENESS_THRESHOLD
        or score_values["compositionCrop"] < LIKENESS_THRESHOLD
        or score_values["typography"] < typography_floor
    ):
        raise AdTemplateProcessError("generationReview comparator is below the likeness gate")
    reviewers = value.get("finalReviewers")
    if not isinstance(reviewers, list) or len(reviewers) != 2:
        raise AdTemplateProcessError("generationReview requires exactly two final reviewers")
    routes: set[str] = set()
    for reviewer in reviewers:
        if not isinstance(reviewer, dict) or set(reviewer) != {"id", "route", "overall", "minimum", "decision"}:
            raise AdTemplateProcessError("generationReview final reviewer is invalid")
        if (
            reviewer.get("decision") != "pass"
            or not isinstance(reviewer.get("id"), str)
            or not reviewer["id"].strip()
            or not isinstance(reviewer.get("route"), str)
            or not reviewer["route"].strip()
            or isinstance(reviewer.get("overall"), bool)
            or not isinstance(reviewer.get("overall"), (int, float))
            or not LIKENESS_THRESHOLD <= float(reviewer["overall"]) <= 10
            or isinstance(reviewer.get("minimum"), bool)
            or not isinstance(reviewer.get("minimum"), (int, float))
            or not typography_floor <= float(reviewer["minimum"]) <= 10
        ):
            raise AdTemplateProcessError("generationReview final reviewer did not pass")
        routes.add(reviewer["route"])
    if len(routes) != 2:
        raise AdTemplateProcessError("generationReview final reviewers are not independent")
    if not isinstance(value.get("warnings"), list):
        raise AdTemplateProcessError("generationReview warnings are invalid")
    return copy.deepcopy(value)


class ExactCloneOrchestrator:
    """One full build, bounded patches, two completion-only reviewers."""

    def __init__(
        self,
        *,
        call_agent: Callable[[str, Any, str], Dict[str, Any]],
        call_image_model: Callable[[str, str, str, Mapping[str, str]], str],
        workspace: Path,
        run_id: str,
        project_id: str,
        emit: Callable[[str, str, Dict[str, Any]], None],
        should_stop: Callable[[], bool] | None = None,
    ):
        self.call_agent = call_agent
        self.call_image_model = call_image_model
        self.workspace = workspace
        self.run_id = run_id
        self.project_id = project_id
        self.emit = emit
        self.should_stop = should_stop or (lambda: False)

    def _check_stop(self) -> None:
        if self.should_stop():
            raise AdTemplateProcessError("exact-clone run was cancelled")

    def run(
        self,
        *,
        source: str,
        brief: str,
        placements: Any,
        routes: List[Dict[str, str]],
        **_legacy: Any,
    ) -> Dict[str, Any]:
        if len(routes) < 5:
            raise AdTemplateProcessError("reference-image, builder, comparator and two final reviewers are required")
        image_route, builder_route, comparator_route, final_a_route, final_b_route = routes[:5]
        escalation_route = routes[5] if len(routes) > 5 else builder_route
        if (final_a_route.get("provider"), final_a_route.get("model")) == (final_b_route.get("provider"), final_b_route.get("model")):
            raise AdTemplateProcessError("final reviewers must use independent routes")
        started = time.time()
        checkpoint = load_checkpoint(self.workspace)
        checkpoint_policy_events: list[tuple[str, Dict[str, Any]]] = []
        if checkpoint and checkpoint.get("qaProjectionVersion") != QA_PROJECTION_VERSION:
            previous_version = checkpoint.get("qaProjectionVersion")
            checkpoint["accepted"] = False
            checkpoint.pop("finalReview", None)
            checkpoint_policy_events.append(("qa-projection.updated", {
                "from_version": previous_version,
                "to_version": QA_PROJECTION_VERSION,
                "preserved_iterations": len(checkpoint.get("iterations") or []),
            }))
        if checkpoint and checkpoint.get("evaluationPolicyVersion") != EVALUATION_POLICY_VERSION:
            previous_version = checkpoint.get("evaluationPolicyVersion")
            checkpoint["accepted"] = False
            checkpoint.pop("finalReview", None)
            checkpoint_policy_events.append(("evaluation-policy.updated", {
                "from_version": previous_version,
                "to_version": EVALUATION_POLICY_VERSION,
                "preserved_iterations": len(checkpoint.get("iterations") or []),
            }))
        if checkpoint_policy_events:
            persist_checkpoint(self.workspace, checkpoint)
            for event_kind, event_data in checkpoint_policy_events:
                self.emit(event_kind, "build", event_data)
        source_placement, target_placement, canvas = _source_placement(source)
        source = source_canvas_reference(source, self.workspace, source_placement)
        if checkpoint.get("sourceCoordinateMode") != "canvas-pixels":
            for key in ("sourceMap", "targetReferenceMap", "reference", "referenceMode", "bestReview", "bestCandidate", "bestIteration", "finalReview"):
                checkpoint.pop(key, None)
            checkpoint.update(sourceCoordinateMode="canvas-pixels", accepted=False)


        source_map = checkpoint.get("sourceMap")
        if not isinstance(source_map, dict) or source_map.get("sourceMapVersion") != SOURCE_MAP_VERSION:
            source_map = build_source_map(source)
            checkpoint.pop("targetReferenceMap", None)
            reference_root = self.workspace / "references"
            reference_root.mkdir(parents=True, exist_ok=True)
            (reference_root / "source-map.json").write_text(_safe_json(source_map), encoding="utf-8")
            self.emit("source-map.completed", "source", {
                "version": SOURCE_MAP_VERSION,
                "ocr_status": source_map["ocrStatus"],
                "ocr_boxes": len(source_map["ocr"]),
                "edge_regions": len(source_map["edgeRegions"]),
            })
            checkpoint.update(
                sourceMap=source_map, sourcePlacement=source_placement,
                targetPlacement=target_placement,
            )
            checkpoint.setdefault("iterations", [])
            checkpoint.setdefault("cycleComparisons", 0)
            persist_checkpoint(self.workspace, checkpoint)

        # The original source is the only design authority. Image models create
        # photo assets, not a second ad which could drift or be silently cropped.
        reciprocal_reference = source
        target_map = source_map
        if checkpoint.get("referenceMode") != "source-only":
            previous = checkpoint.get("reciprocalReference")
            if previous and previous != source:
                checkpoint.setdefault("supersededReferences", []).append(previous)
            checkpoint.update(referenceMode="source-only", reciprocalReference=source,
                              targetReferenceMap=source_map, reference=None, accepted=False)
            for key in ("finalReview", "bestReview", "bestCandidate", "bestIteration"):
                checkpoint.pop(key, None)
            persist_checkpoint(self.workspace, checkpoint)
            self.emit("reference.source-only", "aspect-reference", {
                "source_placement": source_placement, "target_placement": target_placement,
                "summary": "Original source retained; other placement uses a native layout plan, not a generated ad reference",
            })

        reference = checkpoint.get("reference")
        if not reference:
            self._check_stop()
            self.emit("aspect-reference.started", "aspect-reference", {"source_placement": source_placement, "target_placement": target_placement})
            reference = _call_json(
                self.call_agent,
                instance="aspect-reference",
                prompt=aspect_reference_prompt(source_placement=source_placement, target_placement=target_placement, canvas=canvas, brief=f"{brief[:1200]}\nSOURCE MAP: {_safe_json(source_map)}"),
                paths=[source, reciprocal_reference],
                route=builder_route,
                validate=lambda value: _validate_aspect_reference(value, source_placement=source_placement, target_placement=target_placement, canvas=canvas),
                emit=self.emit,
            )
            reference_root = self.workspace / "references"
            reference_root.mkdir(parents=True, exist_ok=True)
            (reference_root / "aspect-reference.json").write_text(_safe_json(reference), encoding="utf-8")
            self.emit("aspect-reference.completed", "aspect-reference", {"source_placement": source_placement, "target_placement": target_placement, "regions": len(reference["regions"])})
            persist_checkpoint(self.workspace, {"reference": reference, "sourceMap": source_map, "targetReferenceMap": target_map, "reciprocalReference": reciprocal_reference, "sourcePlacement": source_placement, "targetPlacement": target_placement, "iterations": [], "cycleComparisons": 0})

        had_checkpoint_candidate = bool(checkpoint.get("candidate"))
        candidate = _checkpoint_candidate(checkpoint)
        if had_checkpoint_candidate and candidate is None:
            persist_checkpoint(self.workspace, checkpoint)
            self.emit("candidate.checkpoint-rejected", "build", {
                "reason": "candidate did not match the direct Blockwise contract",
            })
        iterations = list(checkpoint.get("iterations") or [])
        cycle_comparisons = int(checkpoint.get("cycleComparisons") or 0)
        comparison_budget_used = _comparison_budget_used(checkpoint, iterations)
        manual_instructions = str(checkpoint.get("manualInstructions") or "")
        global_iteration = len(iterations)
        best_candidate = None
        best_review = None
        best_iteration = int(checkpoint.get("bestIteration") or 0)
        try:
            if checkpoint.get("bestCandidate") and checkpoint.get("bestReview"):
                best_candidate = _candidate_envelope(checkpoint["bestCandidate"])
                best_review = validate_review(checkpoint["bestReview"])
        except AdTemplateProcessError:
            best_candidate = None
            best_review = None
            best_iteration = 0
        if best_candidate is None:
            ranked = []
            for record in iterations:
                if not isinstance(record, dict) or not isinstance(record.get("iteration"), int):
                    continue
                try:
                    review = validate_review(record.get("comparison"))
                except AdTemplateProcessError:
                    continue
                recovered = _neutral_candidate_from_iteration(self.workspace, record["iteration"])
                if recovered is not None:
                    ranked.append((_review_quality(review), record["iteration"], recovered, review))
            if ranked:
                _, best_iteration, best_candidate, best_review = max(ranked, key=lambda item: item[:2])
                self.emit("candidate.best-recovered", "build", {
                    "iteration": best_iteration,
                    "score": best_review["scores"]["overall"],
                })
        if best_candidate is not None and best_review is not None and iterations:
            try:
                latest_review = validate_review(iterations[-1].get("comparison"))
            except (AdTemplateProcessError, AttributeError):
                latest_review = None
            if (
                latest_review is not None
                and _review_regressed(latest_review, best_review)
                and _safe_json(candidate) != _safe_json(best_candidate)
            ):
                self.emit("regression.reverted", "compare", {
                    "from_iteration": iterations[-1].get("iteration"),
                    "from_score": latest_review["scores"]["overall"],
                    "to_iteration": best_iteration,
                    "to_score": best_review["scores"]["overall"],
                })
                candidate = copy.deepcopy(best_candidate)
                checkpoint.update(
                    candidate=candidate,
                    bestCandidate=best_candidate,
                    bestReview=best_review,
                    bestIteration=best_iteration,
                )
                persist_checkpoint(self.workspace, checkpoint)
        if not candidate:
            self._check_stop()
            self.emit("candidate.build-started", "build", {"mode": "initial"})
            candidate = _call_json(
                self.call_agent,
                instance="builder-initial",
                prompt=build_prompt(run_id=self.run_id, project_id=self.project_id, brief=brief, placements=placements, reference=reference, source_map=source_map),
                paths=[source, reciprocal_reference],
                route=builder_route,
                validate=_candidate_envelope,
                emit=self.emit,
            )
            self.emit("candidate.built", "build", {"mode": "initial"})
            persist_checkpoint(self.workspace, {"reference": reference, "sourceMap": source_map, "targetReferenceMap": target_map, "reciprocalReference": reciprocal_reference, "sourcePlacement": source_placement, "targetPlacement": target_placement, "candidate": candidate, "iterations": iterations, "cycleComparisons": cycle_comparisons})
        elif manual_instructions:
            manual_issue = [{"placement": "both", "layerIds": ["operator-selected"], "category": "details", "instruction": manual_instructions, "severity": "material"}]
            patch, candidate = _call_applied_patch(
                self.call_agent,
                instance=f"manual-revision-{int(checkpoint.get('manualRevision') or 1)}",
                prompt=patch_prompt(candidate=candidate, issues=manual_issue, manual_instructions=manual_instructions),
                paths=[source, reciprocal_reference],
                route=escalation_route,
                candidate=candidate,
                emit=self.emit,
            )
            manual_instructions = ""
            self.emit("candidate.patch-applied", "build", {"source": "manual-review", "operations": len(patch["operations"])})

        accepted_review: Dict[str, Any] | None = None
        normalized_candidate = _canonical_catalog_paths(candidate)
        if normalized_candidate != candidate:
            candidate = normalized_candidate
            persist_checkpoint(self.workspace, {"candidate": candidate})

        final_rendered: Dict[str, Any] | None = None
        final_comparison_views: list[dict[str, str]] = []
        final_metrics: Dict[str, Any] = {}
        demo_overrides: Dict[str, bytes] = {}
        if (self.workspace / "demo-assets" / "plan.json").exists():
            candidate, demo_overrides = prepare_demo_assets(
                candidate, source=source, source_placement=source_placement, workspace=self.workspace,
                route=image_route, call_image_model=self.call_image_model, emit=self.emit,
            )
        while comparison_budget_used < MAX_COMPARISONS:
            self._check_stop()
            global_iteration += 1
            escalation_iteration = global_iteration > NORMAL_COMPARISONS
            iteration_root = self.workspace / "iterations" / f"{global_iteration:02d}"
            self.emit("iteration.started", "build", {"iteration": global_iteration, "cycle_iteration": cycle_comparisons + 1})
            contract_repairs = 0
            while True:
                try:
                    qa_candidate, qa_asset_overrides = build_ephemeral_qa_candidate(
                        candidate, source=source, reciprocal_reference=reciprocal_reference,
                        source_placement=source_placement, source_map=source_map,
                        target_map=target_map, workspace=iteration_root,
                    )
                    rendered = _copy_public_previews(
                        run_renderer(qa_candidate, iteration_root, asset_overrides={**demo_overrides, **qa_asset_overrides}),
                        self.workspace, global_iteration,
                    )
                    break
                except AdTemplateRendererRejection as rejection:
                    if contract_repairs >= MAX_CONTRACT_REPAIRS:
                        raise AdTemplateProcessError(
                            f"Blockwise contract remained invalid after {MAX_CONTRACT_REPAIRS} bounded repairs: {rejection}"
                        ) from rejection
                    contract_repairs += 1
                    reasons = list(rejection.reasons)
                    self.emit("candidate.contract-rejected", "build", {
                        "iteration": global_iteration,
                        "repair": contract_repairs,
                        "reasons": reasons,
                    })
                    deterministic_candidate, repaired_layers = _apply_deterministic_contract_repairs(
                        candidate, reasons, qa_candidate=qa_candidate,
                    )
                    if repaired_layers:
                        candidate = deterministic_candidate
                        self.emit("candidate.patch-applied", "build", {
                            "iteration": global_iteration,
                            "source": "deterministic-text-preflight",
                            "repair": contract_repairs,
                            "operations": repaired_layers,
                        })
                        persist_checkpoint(self.workspace, {
                            "reference": reference,
                            "sourceMap": source_map,
                            "targetReferenceMap": target_map,
                            "reciprocalReference": reciprocal_reference,
                            "sourcePlacement": source_placement,
                            "targetPlacement": target_placement,
                            "candidate": candidate,
                            "iterations": iterations,
                            "cycleComparisons": cycle_comparisons,
                            "accepted": False,
                            "bestCandidate": best_candidate,
                            "bestReview": best_review,
                            "bestIteration": best_iteration,
                        })
                        continue
                    repair_route = builder_route if contract_repairs == 1 else escalation_route
                    repair, candidate = _call_applied_patch(
                        self.call_agent,
                        instance=f"contract-repair-{global_iteration}-{contract_repairs}",
                        prompt=contract_repair_prompt(candidate=candidate, reasons=reasons),
                        paths=[source, reciprocal_reference],
                        route=repair_route,
                        candidate=candidate,
                        emit=self.emit,
                        strict=False,
                    )
                    self.emit("candidate.patch-applied", "build", {
                        "iteration": global_iteration,
                        "source": "blockwise-contract",
                        "repair": contract_repairs,
                        "operations": len(repair["operations"]),
                    })
                    persist_checkpoint(self.workspace, {
                        "reference": reference,
                        "sourceMap": source_map,
                        "targetReferenceMap": target_map,
                        "reciprocalReference": reciprocal_reference,
                        "sourcePlacement": source_placement,
                        "targetPlacement": target_placement,
                        "candidate": candidate,
                        "iterations": iterations,
                        "cycleComparisons": cycle_comparisons,
                        "accepted": False,
                        "bestCandidate": best_candidate,
                        "bestReview": best_review,
                        "bestIteration": best_iteration,
                    })
            cycle_comparisons += 1
            # These deterministic QA products are independent after rendering:
            # comparison views write disjoint named files while metrics only
            # reads images.  Keep result collection ordered so artifacts and
            # subsequent events remain byte-for-byte/order stable.
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="ad-template-qa") as executor:
                views_future = executor.submit(
                    _comparison_views,
                    source, reciprocal_reference, rendered, self.workspace,
                    global_iteration, source_placement, target_placement,
                )
                metrics_future = executor.submit(
                    _comparison_metrics,
                    source=source, reciprocal_reference=reciprocal_reference,
                    source_placement=source_placement, target_placement=target_placement,
                    rendered=rendered,
                )
                comparison_views = views_future.result()
                metrics = metrics_future.result()
            self.emit("iteration.rendered", "render", {
                "iteration": global_iteration,
                "previews": [item["name"] for item in rendered["previews"]],
                "diffs": [item["name"] for item in comparison_views],
                "metrics": metrics,
            })
            comparator_result = _call_json(
                self.call_agent,
                instance=f"comparator-{global_iteration}",
                prompt=review_prompt(final=False, candidate=candidate, reference=reference, metrics=metrics),
                paths=_vision_paths(source, reciprocal_reference, rendered, comparison_views),
                route=escalation_route if escalation_iteration else comparator_route,
                validate=lambda value: validate_comparator_result(value, candidate=candidate),
                emit=self.emit,
            )
            review = comparator_result["review"]
            comparison_budget_used += 1
            record = {
                "iteration": global_iteration,
                "cycle_iteration": cycle_comparisons,
                "mode": "escalation" if escalation_iteration else "normal",
                "decision": "accepted" if review["decision"] == "accept" else "revise",
                "comparison": review,
                "previews": [item["name"] for item in rendered["previews"]],
                "diffs": [item["name"] for item in comparison_views],
                "metrics": metrics,
            }
            iterations.append(record)
            revision_review = review
            fallback_reason: str | None = comparator_result["patchError"]
            if best_candidate is None or best_review is None:
                best_candidate = copy.deepcopy(candidate)
                best_review = copy.deepcopy(review)
                best_iteration = global_iteration
            else:
                if _review_regressed(review, best_review):
                    self.emit("regression.reverted", "compare", {
                        "from_iteration": global_iteration,
                        "from_score": review["scores"]["overall"],
                        "to_iteration": best_iteration,
                        "to_score": best_review["scores"]["overall"],
                    })
                    candidate = copy.deepcopy(best_candidate)
                    revision_review = copy.deepcopy(best_review)
                    record["regressed"] = True
                    fallback_reason = "comparison regressed; patch the restored best candidate"
                elif _review_improved(review, best_review):
                    best_candidate = copy.deepcopy(candidate)
                    best_review = copy.deepcopy(review)
                    best_iteration = global_iteration
                else:
                    record["plateaued"] = True
                    fallback_reason = "comparison plateaued; route correction to the strong model"
            self.emit("iteration.compared", "compare", {
                "iteration": global_iteration,
                "cycle_iteration": cycle_comparisons,
                "mode": record["mode"],
                "decision": review["decision"],
                "score": review["scores"]["overall"],
                "scores": review["scores"],
                "effects": review["effects"],
                "issues": review["issues"],
            })
            persist_checkpoint(self.workspace, {
                "reference": reference,
                "sourceMap": source_map,
                "targetReferenceMap": target_map,
                "reciprocalReference": reciprocal_reference,
                "sourcePlacement": source_placement,
                "targetPlacement": target_placement,
                "candidate": candidate,
                "iterations": iterations,
                "cycleComparisons": cycle_comparisons,
                "comparisonBudgetUsed": comparison_budget_used,
                "accepted": review["decision"] == "accept",
                "bestCandidate": best_candidate,
                "bestReview": best_review,
                "bestIteration": best_iteration,
            })
            if review["decision"] == "accept":
                accepted_review = review
                final_rendered = rendered
                final_comparison_views = comparison_views
                final_metrics = metrics
                break
            if comparison_budget_used >= MAX_COMPARISONS:
                break
            comparator_patch = comparator_result["patch"]
            if fallback_reason is None and comparator_patch is not None:
                self.emit("iteration.revision-requested", "build", {
                    "iteration": global_iteration,
                    "mode": "comparator-patch",
                    "issues": revision_review["issues"],
                })
                patch = comparator_patch
                candidate = comparator_result["candidate"]
                patch_source = "iteration-comparator"
            else:
                revision_route = escalation_route if global_iteration >= NORMAL_COMPARISONS else builder_route
                self.emit("iteration.revision-requested", "build", {
                    "iteration": global_iteration,
                    "mode": "strong-fallback",
                    "route": f"{revision_route.get('provider')}/{revision_route.get('model')}",
                    "reason": fallback_reason or "comparator patch was unavailable",
                    "issues": revision_review["issues"],
                })
                patch, candidate = _call_applied_patch(
                    self.call_agent,
                    instance=f"patch-fallback-{global_iteration}",
                    prompt=patch_prompt(candidate=candidate, issues=revision_review["issues"]),
                    paths=_vision_paths(source, reciprocal_reference, rendered, comparison_views),
                    route=revision_route,
                    candidate=candidate,
                    emit=self.emit,
                )
                patch_source = "strong-fallback"
            self.emit("candidate.patch-applied", "build", {
                "iteration": global_iteration,
                "source": patch_source,
                "operations": len(patch["operations"]),
            })
            persist_checkpoint(self.workspace, {
                "reference": reference,
                "sourceMap": source_map,
                "targetReferenceMap": target_map,
                "reciprocalReference": reciprocal_reference,
                "sourcePlacement": source_placement,
                "targetPlacement": target_placement,
                "candidate": candidate,
                "iterations": iterations,
                "cycleComparisons": cycle_comparisons,
                "accepted": False,
                "bestCandidate": best_candidate,
                "bestReview": best_review,
                "bestIteration": best_iteration,
            })

        if accepted_review is None or final_rendered is None:
            raise AdTemplateProcessError(f"exact-clone quality loop exhausted {MAX_COMPARISONS} comparisons below 9.8")

        final_review: Dict[str, Any] | None = None
        candidate, demo_overrides = prepare_demo_assets(
            candidate, source=source, source_placement=source_placement, workspace=self.workspace,
            route=image_route, call_image_model=self.call_image_model, emit=self.emit,
        )
        production_rendered = run_renderer(
            candidate, self.workspace / f"final-review-production-{global_iteration:02d}",
            asset_overrides=demo_overrides,
        )
        for final_round in range(1, MAX_FINAL_REVIEW_ROUNDS + 1):
            self._check_stop()
            reviewer_specs = (("a", final_a_route), ("b", final_b_route))
            for label, route in reviewer_specs:
                identity = f"final-reviewer-{label}-{self.run_id}-{final_round}"
                self.emit("final-review.started", "final-check", {"reviewer": identity, "route": f"{route.get('provider')}/{route.get('model')}", "round": final_round})

            def run_final_reviewer(label: str, route: Mapping[str, str]) -> tuple[Dict[str, Any], list[tuple[str, str, Dict[str, Any]]]]:
                identity = f"final-reviewer-{label}-{self.run_id}-{final_round}"
                buffered_events: list[tuple[str, str, Dict[str, Any]]] = []
                result = _call_json(
                    self.call_agent,
                    instance=identity,
                    prompt=review_prompt(final=True, candidate=candidate, reference=reference, metrics=final_metrics),
                    paths=_vision_paths(source, reciprocal_reference, final_rendered, final_comparison_views, production_rendered),
                    route=route,
                    validate=validate_review,
                    emit=lambda kind, node, data: buffered_events.append((kind, node, data)),
                )
                return ({"id": identity, "route": f"{route.get('provider')}/{route.get('model')}", **result}, buffered_events)

            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="ad-template-final-review") as executor:
                futures = [executor.submit(run_final_reviewer, label, route) for label, route in reviewer_specs]
                completed_reviewers = [future.result() for future in futures]
            self._check_stop()
            reviewers = []
            for reviewer, buffered_events in completed_reviewers:
                for kind, node, data in buffered_events:
                    self.emit(kind, node, data)
                reviewers.append(reviewer)
            accepted = all(
                item["decision"] == "accept"
                and item.get("fontSubstitution") == accepted_review.get("fontSubstitution")
                for item in reviewers
            )
            final_review = {"decision": "accepted" if accepted else "revise", "threshold": LIKENESS_THRESHOLD, "round": final_round, "reviewers": reviewers}
            self.emit("final-review.completed", "final-check", {"decision": final_review["decision"], "round": final_round, "reviewers": reviewers})
            if accepted:
                break
            if final_round >= MAX_FINAL_REVIEW_ROUNDS:
                raise AdTemplateProcessError("final reviewers did not accept the exact clone after one merged repair")
            merged_issues = [issue for reviewer in reviewers for issue in reviewer["issues"]]
            if not merged_issues:
                raise AdTemplateProcessError("final reviewers requested revision without actionable issues")
            patch, candidate = _call_applied_patch(
                self.call_agent,
                instance="final-merged-patch",
                prompt=patch_prompt(candidate=candidate, issues=merged_issues),
                paths=_vision_paths(source, reciprocal_reference, final_rendered, final_comparison_views, production_rendered),
                route=escalation_route,
                candidate=candidate,
                emit=self.emit,
            )
            global_iteration += 1
            iteration_root = self.workspace / "iterations" / f"{global_iteration:02d}"
            qa_candidate, qa_asset_overrides = build_ephemeral_qa_candidate(
                candidate, source=source, reciprocal_reference=reciprocal_reference,
                source_placement=source_placement, source_map=source_map,
                target_map=target_map, workspace=iteration_root,
            )
            final_rendered = _copy_public_previews(
                run_renderer(qa_candidate, iteration_root, asset_overrides={**demo_overrides, **qa_asset_overrides}),
                self.workspace, global_iteration,
            )
            production_rendered = run_renderer(
                candidate, self.workspace / f"final-review-production-{global_iteration:02d}",
                asset_overrides=demo_overrides,
            )
            final_comparison_views = _comparison_views(source, reciprocal_reference, final_rendered, self.workspace, global_iteration, source_placement, target_placement)
            final_metrics = _comparison_metrics(
                source=source, reciprocal_reference=reciprocal_reference,
                source_placement=source_placement, target_placement=target_placement,
                rendered=final_rendered,
            )
            final_comparator_result = _call_json(
                self.call_agent,
                instance="comparator-final-repair",
                prompt=review_prompt(final=False, candidate=candidate, reference=reference, metrics=final_metrics),
                paths=_vision_paths(source, reciprocal_reference, final_rendered, final_comparison_views, production_rendered),
                route=comparator_route,
                validate=lambda value: validate_comparator_result(value, candidate=candidate),
                emit=self.emit,
            )
            accepted_review = final_comparator_result["review"]
            if accepted_review["decision"] != "accept":
                raise AdTemplateProcessError("merged final repair regressed below the 9.8 comparator gate")
            iterations.append({
                "iteration": global_iteration,
                "cycle_iteration": cycle_comparisons,
                "mode": "final-repair",
                "decision": "accepted",
                "comparison": accepted_review,
                "previews": [item["name"] for item in final_rendered["previews"]],
                "diffs": [item["name"] for item in final_comparison_views],
                "metrics": final_metrics,
            })
            self.emit("iteration.compared", "compare", {
                "iteration": global_iteration,
                "mode": "final-repair",
                "decision": "accept",
                "score": accepted_review["scores"]["overall"],
                "scores": accepted_review["scores"],
                "effects": accepted_review["effects"],
                "issues": [],
            })

        assert final_review is not None
        template = copy.deepcopy(candidate["template"])
        metadata = template.get("metadata") if isinstance(template.get("metadata"), dict) else None
        if metadata is None:
            raise AdTemplateProcessError("template metadata is required")
        warnings = [warning for record in iterations for warning in record["comparison"].get("warnings", [])]
        metadata["generationReview"] = _generation_review(
            source_placement=source_placement,
            target_placement=target_placement,
            comparator=accepted_review,
            final_review=final_review,
            warnings=warnings,
        )
        candidate = {"template": template, "assets": candidate["assets"]}
        # generationReview changes only metadata, but the final contract is
        # still rendered once so Blockwise's shared schema is the last word.
        final_root = self.workspace / "final"
        final_rendered = _copy_public_previews(
            run_renderer(candidate, final_root, asset_overrides=demo_overrides), self.workspace, global_iteration + 1,
            kind="final-neutral-shippable",
        )
        documents = deterministic_documents(template)
        validated = {
            "template": template,
            "assets": candidate["assets"],
            "iterations": iterations,
            "final_review": final_review,
            "previews": final_rendered["previews"],
            "diffs": final_comparison_views,
            "references": [
                {"name": "source-map.json", "sourcePlacement": source_placement, "sourceMap": source_map},
                {"name": "aspect-reference.json", "sourcePlacement": source_placement, "targetPlacement": target_placement, "reference": reference},
            ],
            "documents": documents,
            "template_path": final_rendered["template_path"],
            "render_path": final_rendered["render"]["feed"],
            "layers": _layer_summary(template),
            "source": Path(source).name,
            "warnings": list(dict.fromkeys(warnings)),
            "font_substitution": accepted_review.get("fontSubstitution"),
            "metrics": final_metrics,
            "elapsed_seconds": round(time.time() - started, 3),
            "process": PROCESS_ID,
        }
        # Fully validate the output before the first external write.
        validate_exact_clone_output(validated, require_import=False)
        imported = import_template(validated, run_id=self.run_id, project_id=self.project_id, asset_overrides=demo_overrides)
        self.emit("template.imported", "import", imported)
        smoke = review_template_action(
            template_id=imported["template_id"], run_id=self.run_id, action="smoke_test",
        )
        if smoke.get("status") != "passed":
            raise AdTemplateProcessError("Blockwise template smoke test did not pass")
        self.emit("template.smoke-tested", "smoke-test", smoke)
        validated["import"] = imported
        validated["smoke_test"] = smoke
        persist_checkpoint(self.workspace, {
            "reference": reference,
            "sourceMap": source_map,
            "targetReferenceMap": target_map,
            "reciprocalReference": reciprocal_reference,
            "sourcePlacement": source_placement,
            "targetPlacement": target_placement,
            "candidate": candidate,
            "iterations": iterations,
            "cycleComparisons": cycle_comparisons,
            "accepted": True,
            "finalReview": final_review,
            "bestCandidate": best_candidate,
            "bestReview": best_review,
            "bestIteration": best_iteration,
            "import": imported,
            "smokeTest": smoke,
        })
        return validate_exact_clone_output(validated, require_import=True)


def validate_exact_clone_output(value: Any, *, require_import: bool) -> Dict[str, Any]:
    required = {
        "template", "assets", "iterations", "final_review", "previews", "diffs",
        "references", "documents", "template_path", "render_path", "layers",
        "warnings", "font_substitution", "elapsed_seconds", "process",
    }
    if require_import:
        required |= {"import", "smoke_test"}
    if not isinstance(value, dict) or value.get("process") != PROCESS_ID or not required.issubset(value):
        raise AdTemplateProcessError("exact-clone output is incomplete")
    _candidate_envelope({"template": value["template"], "assets": value["assets"]})
    metadata = value["template"].get("metadata") if isinstance(value["template"].get("metadata"), dict) else {}
    validate_generation_review(metadata.get("generationReview"))
    iterations = value.get("iterations")
    if not isinstance(iterations, list) or not iterations:
        raise AdTemplateProcessError("exact-clone output requires comparison history")
    accepted = validate_review(iterations[-1]["comparison"])
    if accepted["decision"] != "accept":
        raise AdTemplateProcessError("last comparator did not pass the 9.8 gate")
    final = value.get("final_review")
    if not isinstance(final, dict) or final.get("decision") != "accepted" or len(final.get("reviewers") or []) != 2:
        raise AdTemplateProcessError("two accepted final reviewers are required")
    routes = set()
    for reviewer in final["reviewers"]:
        evidence = validate_review({key: reviewer[key] for key in ("decision", "scores", "issues", "warnings", "effects", "fontSubstitution")})
        if evidence["decision"] != "accept":
            raise AdTemplateProcessError("final reviewer did not pass the 9.8 gate")
        route = reviewer.get("route")
        if not isinstance(route, str) or not route:
            raise AdTemplateProcessError("final reviewer route is missing")
        routes.add(route)
    if len(routes) != 2:
        raise AdTemplateProcessError("final reviewers must use independent routes")
    deterministic_documents(value["template"])
    if require_import:
        imported = value.get("import")
        smoke = value.get("smoke_test")
        if not isinstance(imported, dict) or not imported.get("template_id") or imported.get("library_status") != "quarantined":
            raise AdTemplateProcessError("Blockwise quarantined import receipt is invalid")
        if not isinstance(smoke, dict) or smoke.get("templateId") != imported["template_id"] or smoke.get("status") != "passed":
            raise AdTemplateProcessError("Blockwise smoke-test receipt is invalid")
    return dict(value)


def bounded_review_output(value: Mapping[str, Any], *, model_profile: Mapping[str, Any]) -> Dict[str, Any]:
    validated = validate_exact_clone_output(value, require_import=True)
    template = validated["template"]
    metadata = template.get("metadata") if isinstance(template.get("metadata"), dict) else {}
    return {
        "process": PROCESS_ID,
        "template": {
            "schema": template.get("schema"),
            "templateId": template.get("templateId"),
            "title": metadata.get("title"),
            "artifact": validated.get("template_path"),
        },
        "source": validated.get("source") or "source",
        "references": validated["references"],
        "previews": [{"name": item.get("name"), "placement": item.get("placement"), "kind": item.get("kind", "final-neutral-shippable")} for item in validated["previews"]],
        "diffs": [{"name": item.get("name"), "placement": item.get("placement"), "kind": item.get("kind")} for item in validated["diffs"]],
        "scores": {
            "comparator": validated["iterations"][-1]["comparison"]["scores"],
            "finalReviewers": [item["scores"] for item in validated["final_review"]["reviewers"]],
        },
        "warnings": validated["warnings"],
        "font_substitution": validated["font_substitution"],
        "model_profile": copy.deepcopy(dict(model_profile)),
        "iterations": [{
            "iteration": item["iteration"],
            "cycle_iteration": item["cycle_iteration"],
            "mode": item["mode"],
            "decision": item["decision"],
            "scores": item["comparison"]["scores"],
            "effects": item["comparison"]["effects"],
            "issues": item["comparison"]["issues"],
            "previews": item["previews"],
            "diffs": item["diffs"],
            "metrics": item["metrics"],
        } for item in validated["iterations"]],
        "elapsed_seconds": validated["elapsed_seconds"],
        "layers": validated["layers"],
        "metrics": validated["metrics"],
        "documents": {name: {"bytes": len(text.encode("utf-8")), "artifact": validated.get("template_path")} for name, text in validated["documents"].items()},
        "template_path": validated.get("template_path"),
        "render_path": validated.get("render_path"),
        "import": validated["import"],
        "smoke_test": validated["smoke_test"],
        "final_review": validated["final_review"],
    }
