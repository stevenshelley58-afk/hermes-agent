"""Deterministic production repairs, applied to the editable document before QA."""
import copy
import io
import math

from PIL import Image

from gateway.ad_template_runtime import AdTemplateProcessError


def repair_production_candidate(candidate, image_bytes):
    result = copy.deepcopy(candidate)
    template = result["template"]
    inputs = {item["key"]: item.get("defaultAssetKey") for item in template.get("imageInputs", [])}
    sizes = {}
    changes = []
    for placement, height in (("feed", 1350), ("story", 1920)):
        for layer in template.get(placement + "Layout", {}).get("layers", []):
            layer_id = layer.get("layerId")
            if layer.get("type") == "image_slot":
                # cornerRadius has no painted effect with mask=none.
                if layer.get("cornerRadius", 0) > 0 and layer.get("mask") == "none":
                    layer["mask"] = "rounded_rect"
                    changes.append({"layerId": layer_id, "property": "mask", "value": "rounded_rect"})
                key = inputs.get(layer.get("inputKey"))
                crop = layer.get("defaultCrop")
                if key not in image_bytes or not isinstance(crop, dict):
                    continue  # Missing declarations remain the renderer's strict error.
                if key not in sizes:
                    with Image.open(io.BytesIO(image_bytes[key])) as image:
                        sizes[key] = image.size
                image_width, image_height = sizes[key]
                geometry = layer["geometry"]
                width, slot_height = float(geometry["width"]), float(geometry["height"])
                if max(abs(float(geometry[name])) for name in ("x", "y", "width", "height")) <= 1.001:
                    width, slot_height = width * 1080, slot_height * height
                x, y, cw, ch = (float(crop[name]) for name in ("x", "y", "width", "height"))
                if (not all(math.isfinite(value) for value in (width, slot_height, x, y, cw, ch))
                        or min(width, slot_height, cw, ch) <= 0
                        or min(x, y) < 0 or x + cw > 1.000001 or y + ch > 1.000001):
                    raise AdTemplateProcessError("production image-fit repair requires valid in-bounds crop and geometry")
                ratio = width / slot_height
                source_ratio = cw * image_width / (ch * image_height)
                if math.isclose(source_ratio, ratio, rel_tol=1e-6):
                    continue
                # Cover within the selected source region, preserving its centre.
                # Never stretch pixels, expand geometry, change assets or invent scores.
                if source_ratio > ratio:
                    new_width = ch * image_height * ratio / image_width
                    x, cw = x + (cw - new_width) / 2, new_width
                else:
                    new_height = cw * image_width / ratio / image_height
                    y, ch = y + (ch - new_height) / 2, new_height
                repaired = {"x": x, "y": y, "width": cw, "height": ch}
                layer["defaultCrop"] = repaired
                changes.append({"layerId": layer_id, "property": "defaultCrop", "value": repaired})
            elif layer.get("type") == "vector" and layer.get("shape") == "pill" and layer.get("cornerRadius") == 0:
                layer["shape"] = "rect"
                changes.append({"layerId": layer_id, "property": "shape", "value": "rect"})
    return result, changes
