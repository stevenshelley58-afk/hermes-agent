"""Executable orchestration for the sole ad-template process."""
from __future__ import annotations
import base64, json, math, mimetypes, os, re, shlex, shutil, subprocess, sys, urllib.error, urllib.request, uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping

THRESHOLD = 9.5
MAX_SCHEMA_REPAIRS_PER_ITERATION = 3
STAGES = ("source", "build", "render", "compare", "final-check", "live")
MAX_CANDIDATE_CONTEXT_CHARS = 100_000

class AdTemplateProcessError(ValueError):
    pass

def _number(value: Any) -> float:
    try: result = float(value)
    except (TypeError, ValueError): raise AdTemplateProcessError("score must be numeric") from None
    if not 0 <= result <= 10: raise AdTemplateProcessError("score must be between 0 and 10")
    return result

RUBRIC_FIELDS = ("layout_geometry", "hierarchy_typography", "colour_tone", "editable_decomposition", "native_story")

def _assessment(value: Any, role: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise AdTemplateProcessError(f"{role} returned invalid evidence")
    rubric = value.get("rubric")
    if not isinstance(rubric, dict):
        raise AdTemplateProcessError(f"{role} must provide all rubric subscores")
    if set(rubric) != set(RUBRIC_FIELDS):
        raise AdTemplateProcessError(f"{role} rubric must contain exactly the five required fields")
    scores = {field: _number(rubric.get(field)) for field in RUBRIC_FIELDS}
    hard_failures = value.get("hard_failures") or value.get("hard_fail") or []
    if isinstance(hard_failures, str): hard_failures = [hard_failures]
    if not isinstance(hard_failures, list): raise AdTemplateProcessError(f"{role} hard_failures must be a list")
    reason = str(value.get("reason") or value.get("mismatches") or "").strip()
    if len(reason) < 3: raise AdTemplateProcessError(f"{role} must explain its decision")
    score = round(sum(scores.values()) / len(scores), 2)
    if hard_failures: score = 0.0
    return {"score": score, "reason": reason, "rubric": scores, "hard_failures": [str(item)[:240] for item in hard_failures]}

def validate_iterations(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > 30: raise AdTemplateProcessError("iterations must contain 1 to 30 records")
    result, accepted, retry_after_review = [], False, False
    for index, raw in enumerate(value, 1):
        if not isinstance(raw, dict) or raw.get("iteration", index) != index: raise AdTemplateProcessError("iterations must be consecutive and one-based")
        comparison = raw.get("comparison")
        if not isinstance(comparison, dict): raise AdTemplateProcessError("each iteration requires one comparator")
        if any(key in raw or key in comparison for key in ("reviewers", "reviewer", "primary", "strict")): raise AdTemplateProcessError("final reviewers are only allowed after the comparator passes")
        evidence = _assessment(comparison, "comparator")
        score = evidence["score"]
        decision = str(raw.get("decision") or ("accepted" if score >= THRESHOLD else "revise"))
        expected = "accepted" if score >= THRESHOLD else "revise"
        if decision != expected: raise AdTemplateProcessError("iteration decision does not match comparator score")
        reason = evidence["reason"]
        if accepted and not retry_after_review:
            raise AdTemplateProcessError("no iteration may follow an accepted candidate unless final reviewers requested revision")
        accepted = score >= THRESHOLD
        retry_after_review = bool(raw.get("final_review_failed"))
        item = dict(raw); item.update(iteration=index, comparison=evidence, decision=decision); result.append(item)
    return result

def validate_final_review(value: Any, *, accepted: bool) -> Dict[str, Any]:
    if not accepted:
        if value not in (None, {}, []): raise AdTemplateProcessError("final reviewers cannot run before a passing comparator")
        return {}
    if not isinstance(value, dict) or not isinstance(value.get("reviewers"), list) or len(value["reviewers"]) != 2: raise AdTemplateProcessError("exactly two final reviewers are required")
    normalized = []
    for item in value["reviewers"]:
        if not isinstance(item, dict): raise AdTemplateProcessError("reviewer record must be an object")
        identity = str(item.get("id") or item.get("name") or "").strip()
        if not identity: raise AdTemplateProcessError("reviewer identity is required")
        evidence = _assessment(item, "final reviewer")
        score, reason = evidence["score"], evidence["reason"]
        route = str(item.get("route") or "").strip()
        if not route: raise AdTemplateProcessError("reviewer route is required")
        normalized.append({"id": identity, "route": route, **evidence})
    if normalized[0]["id"] == normalized[1]["id"] or normalized[0]["route"] == normalized[1]["route"]: raise AdTemplateProcessError("final reviewers must use independent instances and routes")
    passed = all(item["score"] >= THRESHOLD for item in normalized); decision = "accepted" if passed else "revise"
    if str(value.get("decision") or decision) != decision: raise AdTemplateProcessError("final review decision does not match scores")
    return {"reviewers": normalized, "decision": decision, "threshold": THRESHOLD}

def deterministic_documents(template: Any) -> Dict[str, str]:
    if not isinstance(template, dict) or template.get("schema") != "blockwise.ad-template": raise AdTemplateProcessError("template must use the exact Blockwise schema")
    feed, story = template.get("feedLayout"), template.get("storyLayout")
    if not isinstance(feed, dict) or not isinstance(story, dict): raise AdTemplateProcessError("template must contain Feed and Story layouts")
    for name, doc, placement in (("feed", feed, "feed"), ("story", story, "story")):
        layers = doc.get("layers")
        if doc.get("placement") != placement or not isinstance(layers, list) or not layers or any(not isinstance(layer, dict) or not layer.get("layerId") or not layer.get("type") for layer in layers):
            raise AdTemplateProcessError(f"{name} layout has invalid layers")
    encode = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {"feed.json": encode(feed), "story.json": encode(story), "template.json": encode(template)}

def _under(path: Path, root: Path) -> bool:
    try: path.relative_to(root); return True
    except ValueError: return False

def validate_artifacts(output: Mapping[str, Any], workspace: Path) -> Dict[str, Any]:
    previews = output.get("previews")
    if not isinstance(previews, list) or not previews: raise AdTemplateProcessError("renderer produced no previews")
    safe = []
    for item in previews:
        if not isinstance(item, dict): raise AdTemplateProcessError("preview record must be an object")
        raw = Path(str(item.get("path") or "")).resolve()
        if not _under(raw, workspace) or not raw.is_file(): raise AdTemplateProcessError("preview artifact is missing or outside the run workspace")
        placement = str(item.get("placement") or "").lower()
        if placement not in {"feed", "story"}: raise AdTemplateProcessError("preview placement must be Feed or Story")
        safe.append({"name": raw.name, "placement": placement})
    render = output.get("render")
    if not isinstance(render, dict) or any(not isinstance(render.get(place), str) for place in ("feed", "story")):
        raise AdTemplateProcessError("renderer receipt must contain Feed and Story paths")
    for place in ("feed", "story"):
        render_path = Path(render[place]).resolve()
        if not _under(render_path, workspace) or not render_path.is_file(): raise AdTemplateProcessError("render artifact is missing or outside the run workspace")
    return {"previews": safe}

TEMPLATE_FIELDS = {
    "schema", "templateId", "createdAt", "feedLayout", "storyLayout",
    "imageInputs", "textInputs", "semanticColours", "assets", "fonts", "metadata",
}
METADATA_FIELDS = {
    "title", "description", "gallerySamples", "metaCopyDefaults", "aiWritingGuidance",
    "publishRequirements", "replacementAssets", "realAssetRefs",
}
COLOUR_ROLES = {"background", "primary", "secondary", "accent", "mainText", "inverseText"}
VECTOR_SHAPES = ("rect", "rounded", "circle", "line", "pill", "notched", "wave", "ring")
LINE_HEIGHT_MIN = 0.8
LINE_HEIGHT_MAX = 2.5
SUPPORTED_ICONS = ("arrow", "check", "phone", "mail", "globe", "pin")
VISIBLE_LOGO_ASSET_KEYS: frozenset[str] = frozenset()
LAYER_FIELDS = {
    "plate": ({"type", "layerId", "colourRole", "geometry", "protected"}, {"assetKey"}),
    "image_slot": ({"type", "layerId", "inputKey", "geometry", "mask", "minSourceWidth", "minSourceHeight", "defaultCrop", "allowedPlacementOverrides"}, set()),
    "overlay_patch": ({"type", "layerId", "geometry", "colourRole", "opacity"}, {"assetKey"}),
    "text": ({"type", "layerId", "inputKey", "font", "fontSize", "lineHeight", "tracking", "alignment", "maxCharacters", "maxLines", "colourRole", "overflowBehaviour", "geometry"}, set()),
    "logo": ({"type", "layerId", "inputKey", "geometry"}, set()),
    "vector": ({"type", "layerId", "geometry", "shape", "colourRole", "opacity"}, set()),
    "icon": ({"type", "layerId", "geometry", "icon", "colourRole"}, set()),
}
ALLOWED_FONT_FILES = {
    "manrope-400.woff2", "manrope-500.woff2", "manrope-600.woff2", "manrope-700.woff2", "manrope-800.woff2",
    "playfair-display-700.woff2", "playfair-display-800.woff2", "playfair-display-900.woff2",
    "lora-600.woff2", "lora-700.woff2", "poppins-500.woff2", "poppins-700.woff2", "poppins-900.woff2",
    "barlow-600.woff2",
}

def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())

def _number_value(value: Any, *, positive: bool = False) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and (not positive or value > 0)

def _positive_int(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0

def _strict_keys(value: Mapping[str, Any], required: set[str], optional: set[str] | None = None) -> bool:
    optional = optional or set()
    return required.issubset(value) and set(value).issubset(required | optional)

def _rect(value: Any, *, bounds: tuple[int, int] | None = None) -> bool:
    if not isinstance(value, dict) or set(value) != {"x", "y", "width", "height"}:
        return False
    if not all(_number_value(value[key]) for key in ("x", "y", "width", "height")):
        return False
    if value["width"] <= 0 or value["height"] <= 0:
        return False
    if bounds and (value["x"] < 0 or value["y"] < 0 or value["x"] + value["width"] > bounds[0] or value["y"] + value["height"] > bounds[1]):
        return False
    return True

def _validate_rect(value: Any, *, path: str, bounds: tuple[int, int]) -> None:
    expected = {"x", "y", "width", "height"}
    if not isinstance(value, dict):
        raise AdTemplateProcessError(f"{path} must be an object with exactly x, y, width, and height")
    missing = sorted(expected - set(value))
    extra = sorted(set(value) - expected)
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if extra:
            details.append(f"unexpected {', '.join(extra)}")
        raise AdTemplateProcessError(f"{path} must contain exactly x, y, width, and height ({'; '.join(details)})")
    for key in ("x", "y", "width", "height"):
        if not _number_value(value[key]):
            raise AdTemplateProcessError(f"{path}.{key} must be a finite number")
    if value["width"] <= 0:
        raise AdTemplateProcessError(f"{path}.width must be greater than zero")
    if value["height"] <= 0:
        raise AdTemplateProcessError(f"{path}.height must be greater than zero")
    if value["x"] < 0:
        raise AdTemplateProcessError(f"{path}.x must be zero or greater")
    if value["y"] < 0:
        raise AdTemplateProcessError(f"{path}.y must be zero or greater")
    width, height = bounds
    if value["x"] + value["width"] > width:
        raise AdTemplateProcessError(f"{path} exceeds the {width}x{height} canvas: x + width must be <= {width}")
    if value["y"] + value["height"] > height:
        raise AdTemplateProcessError(f"{path} exceeds the {width}x{height} canvas: y + height must be <= {height}")

def _font(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {"file"} and _nonempty(value.get("file"))

def _reject_builder_bytes(value: Any) -> None:
    if isinstance(value, dict):
        if "bytesBase64" in value:
            raise AdTemplateProcessError("builder must never return asset bytes")
        for child in value.values():
            _reject_builder_bytes(child)
    elif isinstance(value, list):
        for child in value:
            _reject_builder_bytes(child)

def _validate_layout(layout: Any, *, placement: str, width: int, height: int) -> None:
    layout_path = f"{placement}Layout"
    if not isinstance(layout, dict) or set(layout) != {"placement", "layers", "safeZones"} or layout.get("placement") != placement:
        raise AdTemplateProcessError(f"{layout_path} must contain exactly placement='{placement}', layers, and safeZones")
    layers = layout.get("layers")
    safe_zones = layout.get("safeZones")
    if not isinstance(layers, list) or not 1 <= len(layers) <= 256:
        raise AdTemplateProcessError(f"{layout_path}.layers must be a list with 1 to 256 layers")
    if not isinstance(safe_zones, list) or len(safe_zones) > 32:
        raise AdTemplateProcessError(f"{layout_path}.safeZones must be a list with at most 32 rectangles")
    for zone_index, zone in enumerate(safe_zones):
        _validate_rect(zone, path=f"{layout_path}.safeZones[{zone_index}]", bounds=(width, height))
    ids = set()
    for layer_index, layer in enumerate(layers):
        layer_path = f"{layout_path}.layers[{layer_index}]"
        if not isinstance(layer, dict) or layer.get("type") not in LAYER_FIELDS:
            raise AdTemplateProcessError(f"{layer_path} has an unsupported layer type")
        required, optional = LAYER_FIELDS[layer["type"]]
        if not _strict_keys(layer, required, optional):
            raise AdTemplateProcessError(f"{layer_path} does not match the exact {layer['type']} layer shape")
        if not _nonempty(layer.get("layerId")):
            raise AdTemplateProcessError(f"{layer_path}.layerId must be a non-empty string")
        if layer["layerId"] in ids:
            raise AdTemplateProcessError(f"{layer_path}.layerId duplicates an earlier layer")
        _validate_rect(layer.get("geometry"), path=f"{layer_path}.geometry", bounds=(width, height))
        ids.add(layer["layerId"])
        layer_type = layer["type"]
        for key in ("inputKey", "colourRole", "icon", "assetKey"):
            if key in layer and not _nonempty(layer[key]):
                raise AdTemplateProcessError(f"{layer_path}.{key} must be a non-empty string")
        if "colourRole" in layer and layer["colourRole"] not in COLOUR_ROLES:
            raise AdTemplateProcessError(f"{layer_path}.colourRole is invalid")
        if layer_type == "plate" and not isinstance(layer["protected"], bool):
            raise AdTemplateProcessError("plate protected must be boolean")
        if layer_type == "image_slot":
            if layer["mask"] not in {"rounded_rect", "circle", "none"}:
                raise AdTemplateProcessError(f"{layer_path}.mask must be one of rounded_rect, circle, or none")
            if not _positive_int(layer["minSourceWidth"]):
                raise AdTemplateProcessError(f"{layer_path}.minSourceWidth must be a positive integer")
            if not _positive_int(layer["minSourceHeight"]):
                raise AdTemplateProcessError(f"{layer_path}.minSourceHeight must be a positive integer")
            if not _rect(layer["defaultCrop"]):
                raise AdTemplateProcessError(f"{layer_path}.defaultCrop must contain exactly numeric x, y, width, and height with positive width and height")
            overrides = layer["allowedPlacementOverrides"]
            if not isinstance(overrides, list):
                raise AdTemplateProcessError(f"{layer_path}.allowedPlacementOverrides must be a list containing only crop and/or position")
            for override_index, item in enumerate(overrides):
                if item not in {"crop", "position"}:
                    raise AdTemplateProcessError(f"{layer_path}.allowedPlacementOverrides[{override_index}] must be crop or position")
        if layer_type in {"overlay_patch", "vector"} and (not _number_value(layer["opacity"]) or not 0 <= layer["opacity"] <= 1):
            raise AdTemplateProcessError(f"{layer_type} opacity is invalid")
        if layer_type == "text":
            if not _font(layer["font"]) or not _number_value(layer["fontSize"], positive=True) or not _number_value(layer["tracking"]) or not _positive_int(layer["maxCharacters"]) or not _positive_int(layer["maxLines"]):
                raise AdTemplateProcessError("text layer sizing is invalid")
            line_height = layer["lineHeight"]
            if not _number_value(line_height) or not LINE_HEIGHT_MIN <= line_height <= LINE_HEIGHT_MAX:
                offending = json.dumps(line_height, ensure_ascii=True)[:160]
                raise AdTemplateProcessError(f"{layer_path}.lineHeight={offending} must be a unitless multiplier between 0.8 and 2.5 (normally 1.1 to 1.6), never pixels")
            if layer["alignment"] not in {"left", "center", "right"}:
                raise AdTemplateProcessError(f"{layer_path}.alignment must be left, center, or right")
            if layer["overflowBehaviour"] not in {"refuse", "truncate", "scale_down"}:
                raise AdTemplateProcessError(f"{layer_path}.overflowBehaviour must be refuse, truncate, or scale_down")
        if layer_type == "vector" and layer["shape"] not in VECTOR_SHAPES:
            allowed = ", ".join(VECTOR_SHAPES)
            offending = json.dumps(layer["shape"], ensure_ascii=True)[:160]
            raise AdTemplateProcessError(f"{layer_path}.shape={offending} must be one of {allowed}")
        if layer_type == "icon" and layer["icon"] not in SUPPORTED_ICONS:
            allowed = ", ".join(SUPPORTED_ICONS)
            offending = json.dumps(layer["icon"], ensure_ascii=True)[:160]
            raise AdTemplateProcessError(f"{layer_path}.icon={offending} must be one of {allowed}")

def validate_template_artifact(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict) or set(value) != TEMPLATE_FIELDS or value.get("schema") != "blockwise.ad-template":
        raise AdTemplateProcessError("builder must return the exact unversioned Blockwise template schema")
    _reject_builder_bytes(value)
    if not _nonempty(value.get("templateId")) or len(value["templateId"]) > 160 or not _nonempty(value.get("createdAt")):
        raise AdTemplateProcessError("template identity is invalid")
    try:
        datetime.fromisoformat(value["createdAt"].replace("Z", "+00:00"))
    except ValueError:
        raise AdTemplateProcessError("createdAt must be an ISO datetime") from None
    _validate_layout(value["feedLayout"], placement="feed", width=1080, height=1350)
    _validate_layout(value["storyLayout"], placement="story", width=1080, height=1920)
    if not isinstance(value["imageInputs"], list) or not isinstance(value["textInputs"], list):
        raise AdTemplateProcessError("template inputs must be lists")
    image_keys: set[str] = set()
    for item in value["imageInputs"]:
        if not isinstance(item, dict) or not _strict_keys(item, {"key", "label", "acceptedTypes"}, {"required", "defaultAssetKey"}) or not _nonempty(item["key"]) or not _nonempty(item["label"]):
            raise AdTemplateProcessError("image input does not match the Blockwise contract")
        if "required" in item and not isinstance(item["required"], bool):
            raise AdTemplateProcessError("image input required must be boolean")
        if "defaultAssetKey" in item and not _nonempty(item["defaultAssetKey"]):
            raise AdTemplateProcessError("image input default asset is invalid")
        if not isinstance(item["acceptedTypes"], list) or not item["acceptedTypes"] or not all(_nonempty(entry) for entry in item["acceptedTypes"]):
            raise AdTemplateProcessError("image input acceptedTypes are invalid")
        if item["key"] in image_keys:
            raise AdTemplateProcessError("image input keys must be unique")
        image_keys.add(item["key"])
    text_keys: set[str] = set()
    for item in value["textInputs"]:
        if not isinstance(item, dict) or set(item) != {"key", "label", "placeholder", "maxLength"} or not _nonempty(item["key"]) or not _nonempty(item["label"]) or not isinstance(item["placeholder"], str) or not _positive_int(item["maxLength"]):
            raise AdTemplateProcessError("text input does not match the Blockwise contract")
        if item["key"] in text_keys:
            raise AdTemplateProcessError("text input keys must be unique")
        text_keys.add(item["key"])
    duplicate_input_keys = sorted(image_keys & text_keys)
    if duplicate_input_keys:
        raise AdTemplateProcessError(f"input key {json.dumps(duplicate_input_keys[0])} must be unique across imageInputs and textInputs")
    if not isinstance(value["semanticColours"], dict) or set(value["semanticColours"]) != COLOUR_ROLES or not all(_nonempty(colour) for colour in value["semanticColours"].values()):
        raise AdTemplateProcessError("semanticColours must contain exactly the six Blockwise colour roles")
    if not isinstance(value["assets"], dict):
        raise AdTemplateProcessError("assets must be a declaration record")
    for key, asset in value["assets"].items():
        if not _nonempty(key) or not isinstance(asset, dict) or set(asset) != {"fileName", "mimeType"} or not _nonempty(asset["fileName"]) or not _nonempty(asset["mimeType"]):
            raise AdTemplateProcessError("template asset declarations are invalid")
    if not isinstance(value["fonts"], list):
        raise AdTemplateProcessError('fonts must be a JSON list of objects shaped exactly {"file":"allowed-font.woff2"}, never an object or map')
    for font_index, item in enumerate(value["fonts"]):
        if not _font(item):
            raise AdTemplateProcessError(f'fonts[{font_index}] must be shaped exactly {{"file":"allowed-font.woff2"}}')
        if item["file"] not in ALLOWED_FONT_FILES:
            raise AdTemplateProcessError(f"fonts[{font_index}].file must name one of the allowed bundled font files")
    font_files = {item["file"] for item in value["fonts"]}
    asset_keys = set(value["assets"])
    image_inputs_by_key = {item["key"]: item for item in value["imageInputs"]}
    for item in value["imageInputs"]:
        if item.get("defaultAssetKey") and item["defaultAssetKey"] not in asset_keys:
            raise AdTemplateProcessError("image input default asset is undeclared")
    for layout_name, layout in (("feedLayout", value["feedLayout"]), ("storyLayout", value["storyLayout"])):
        for layer_index, layer in enumerate(layout["layers"]):
            layer_path = f"{layout_name}.layers[{layer_index}]"
            if layer["type"] in {"image_slot", "logo"} and layer["inputKey"] not in image_keys:
                offending = json.dumps(layer["inputKey"], ensure_ascii=True)[:160]
                allowed = ", ".join(sorted(image_keys)) or "(none declared)"
                raise AdTemplateProcessError(
                    f"{layer_path}.inputKey={offending} for {layer['type']} is undeclared; "
                    f"declare it exactly once in imageInputs (declared: {allowed})"
                )
            if layer["type"] == "logo":
                input_record = image_inputs_by_key.get(layer["inputKey"]) or {}
                default_key = input_record.get("defaultAssetKey")
                if not default_key or default_key not in VISIBLE_LOGO_ASSET_KEYS:
                    offending = json.dumps(layer["inputKey"], ensure_ascii=True)[:160]
                    raise AdTemplateProcessError(
                        f"{layer_path}.inputKey={offending} for logo must reference an imageInput.defaultAssetKey "
                        "from the visible source-free logo catalog; none is available, so use editable brand text plus a vector mark instead"
                    )
            if layer["type"] == "text" and layer["inputKey"] not in text_keys:
                offending = json.dumps(layer["inputKey"], ensure_ascii=True)[:160]
                allowed = ", ".join(sorted(text_keys)) or "(none declared)"
                raise AdTemplateProcessError(f"{layer_path}.inputKey={offending} for text is undeclared (declared: {allowed})")
            if layer["type"] == "text" and layer["font"]["file"] not in font_files:
                offending = json.dumps(layer["font"]["file"], ensure_ascii=True)[:160]
                allowed = ", ".join(sorted(font_files)) or "(none declared)"
                raise AdTemplateProcessError(f"{layer_path}.font.file={offending} is undeclared (declared: {allowed})")
            if layer.get("assetKey") and layer["assetKey"] not in asset_keys:
                offending = json.dumps(layer["assetKey"], ensure_ascii=True)[:160]
                allowed = ", ".join(sorted(asset_keys)) or "(none declared)"
                raise AdTemplateProcessError(f"{layer_path}.assetKey={offending} is undeclared (declared: {allowed})")
            if layer.get("colourRole") and layer["colourRole"] not in value["semanticColours"]:
                raise AdTemplateProcessError("layer colourRole is undeclared")
    metadata = value["metadata"]
    if not isinstance(metadata, dict) or set(metadata) != METADATA_FIELDS or not _nonempty(metadata["title"]) or not isinstance(metadata["description"], str):
        raise AdTemplateProcessError("template metadata does not match the Blockwise contract")
    samples = metadata["gallerySamples"]
    if not isinstance(samples, dict) or not set(samples).issubset({"feed", "story"}):
        raise AdTemplateProcessError("metadata gallerySamples is invalid")
    for sample in samples.values():
        if not isinstance(sample, dict) or not _strict_keys(sample, {"placement", "purpose"}, {"assetKey"}) or sample["placement"] not in {"feed", "story"} or not _nonempty(sample["purpose"]) or ("assetKey" in sample and sample["assetKey"] not in asset_keys):
            raise AdTemplateProcessError("metadata gallery sample is invalid")
    copy_defaults = metadata["metaCopyDefaults"]
    if not isinstance(copy_defaults, dict) or set(copy_defaults) != {"primaryText", "headlines", "descriptions", "cta"} or not all(isinstance(copy_defaults[key], list) and all(isinstance(item, str) for item in copy_defaults[key]) for key in ("primaryText", "headlines", "descriptions")) or not isinstance(copy_defaults["cta"], str):
        raise AdTemplateProcessError("metadata metaCopyDefaults is invalid")
    writing = metadata["aiWritingGuidance"]
    if not isinstance(writing, dict) or set(writing) != {"summary", "fields"} or not isinstance(writing["summary"], str) or not isinstance(writing["fields"], dict) or not all(_nonempty(key) and isinstance(item, str) for key, item in writing["fields"].items()):
        raise AdTemplateProcessError("metadata aiWritingGuidance is invalid")
    publish = metadata["publishRequirements"]
    if not isinstance(publish, dict) or not _strict_keys(publish, {"objective", "specialAdCategory", "instantForm", "destination"}, {"requiredCtaTypes"}) or not _nonempty(publish["objective"]) or not (publish["specialAdCategory"] is None or isinstance(publish["specialAdCategory"], str)):
        raise AdTemplateProcessError("metadata publishRequirements is invalid")
    instant_form = publish["instantForm"]
    if not isinstance(instant_form, dict) or not _strict_keys(instant_form, {"required", "dependency"}, {"defaults"}) or not isinstance(instant_form["required"], bool) or not (instant_form["dependency"] is None or isinstance(instant_form["dependency"], str)) or ("defaults" in instant_form and (not isinstance(instant_form["defaults"], dict) or not all(_nonempty(key) and isinstance(item, str) for key, item in instant_form["defaults"].items()))):
        raise AdTemplateProcessError("metadata instantForm is invalid")
    destination = publish["destination"]
    if not isinstance(destination, dict) or set(destination) != {"required", "kind", "dependency"} or not isinstance(destination["required"], bool) or destination["kind"] not in {"website", "instant_form", "none"} or not (destination["dependency"] is None or isinstance(destination["dependency"], str)):
        raise AdTemplateProcessError("metadata destination is invalid")
    if "requiredCtaTypes" in publish and (not isinstance(publish["requiredCtaTypes"], list) or not all(isinstance(item, str) for item in publish["requiredCtaTypes"])):
        raise AdTemplateProcessError("metadata requiredCtaTypes is invalid")
    replacements = metadata["replacementAssets"]
    if not isinstance(replacements, list):
        raise AdTemplateProcessError("metadata replacementAssets must be a list")
    for item in replacements:
        if not isinstance(item, dict) or not _strict_keys(item, {"inputKey", "assetKey"}, {"purpose"}) or item["inputKey"] not in image_keys or item["assetKey"] not in asset_keys or ("purpose" in item and not isinstance(item["purpose"], str)):
            raise AdTemplateProcessError("metadata replacement asset is invalid")
    real_assets = metadata["realAssetRefs"]
    if not isinstance(real_assets, list):
        raise AdTemplateProcessError("metadata realAssetRefs must be a list")
    declared_input_keys = image_keys | text_keys
    for ref_index, item in enumerate(real_assets):
        path = f"metadata.realAssetRefs[{ref_index}]"
        if not isinstance(item, dict) or set(item) != {"inputKey", "kind", "required"}:
            raise AdTemplateProcessError(f"{path} must contain exactly inputKey, kind, and required")
        if item["inputKey"] not in declared_input_keys:
            offending = json.dumps(item["inputKey"], ensure_ascii=True)[:160]
            allowed = ", ".join(sorted(declared_input_keys)) or "(none declared)"
            raise AdTemplateProcessError(f"{path}.inputKey={offending} is undeclared (declared inputs: {allowed})")
        if not _nonempty(item["kind"]):
            raise AdTemplateProcessError(f"{path}.kind must be a non-empty descriptive string")
        if not isinstance(item["required"], bool):
            raise AdTemplateProcessError(f"{path}.required must be boolean")
    return value

def validate_builder_candidate(value: Any) -> Dict[str, Any]:
    _reject_builder_bytes(value)
    if not isinstance(value, dict) or set(value) != {"template", "assets"}:
        raise AdTemplateProcessError("builder must return exactly template and assets")
    template = validate_template_artifact(value.get("template"))
    assets = value.get("assets")
    if not isinstance(assets, list):
        raise AdTemplateProcessError("builder assets must be a list")
    declarations = template["assets"]
    seen = set()
    for asset_index, asset in enumerate(assets):
        path = f"assets[{asset_index}]"
        if not isinstance(asset, dict) or set(asset) != {"assetKey", "fileName", "mimeType"}:
            raise AdTemplateProcessError(f"{path} must contain exactly assetKey, fileName, and mimeType")
        key = asset.get("assetKey")
        if not _nonempty(key):
            raise AdTemplateProcessError(f"{path}.assetKey must be a non-empty string")
        if key in seen:
            raise AdTemplateProcessError(f"{path}.assetKey duplicates an earlier asset")
        if key not in declarations or asset.get("fileName") != declarations[key].get("fileName") or asset.get("mimeType") != declarations[key].get("mimeType"):
            raise AdTemplateProcessError(f"{path} must exactly match its template.assets declaration")
        seen.add(key)
    if set(declarations) != seen:
        raise AdTemplateProcessError("builder assets must supply every template.assets declaration exactly once")
    return value

_PRIVATE_EVIDENCE_KEYS = frozenset({
    "authorization", "bytesbase64", "credential", "credentials", "hash",
    "private", "privatefields", "secret", "signature", "sourcepath", "token",
})

def _prompt_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 12:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if re.search(r"data:[^,]{0,200};base64,", value, re.IGNORECASE):
            return "[omitted inline data]"
        return value[:8000]
    if isinstance(value, list):
        return [_prompt_value(item, depth=depth + 1) for item in value[:512]]
    if isinstance(value, Mapping):
        safe: Dict[str, Any] = {}
        for raw_key, child in list(value.items())[:512]:
            key = str(raw_key)[:160]
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if normalized in _PRIVATE_EVIDENCE_KEYS or any(normalized.startswith(prefix) for prefix in _PRIVATE_EVIDENCE_KEYS):
                continue
            safe[key] = _prompt_value(child, depth=depth + 1)
        return safe
    return str(value)[:1000]

def _prompt_object(value: Any, keys: tuple[str, ...]) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {key: _prompt_value(value[key]) for key in keys if key in value}

def _prompt_rect(value: Any) -> Dict[str, Any]:
    return _prompt_object(value, ("x", "y", "width", "height"))

def _prompt_layer(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    layer_type = value.get("type")
    required, optional = LAYER_FIELDS.get(layer_type, ({"type", "layerId", "geometry"}, set()))
    projected = _prompt_object(value, tuple(sorted(required | optional)))
    if "geometry" in value:
        projected["geometry"] = _prompt_rect(value["geometry"])
    if "defaultCrop" in value:
        projected["defaultCrop"] = _prompt_rect(value["defaultCrop"])
    if "font" in value:
        projected["font"] = _prompt_object(value["font"], ("file",))
    return projected

def _prompt_layout(value: Any) -> Dict[str, Any]:
    projected = _prompt_object(value, ("placement",))
    if isinstance(value, Mapping) and isinstance(value.get("layers"), list):
        projected["layers"] = [_prompt_layer(item) for item in value["layers"][:256]]
    if isinstance(value, Mapping) and isinstance(value.get("safeZones"), list):
        projected["safeZones"] = [_prompt_rect(item) for item in value["safeZones"][:32]]
    return projected

def _prompt_metadata(value: Any) -> Dict[str, Any]:
    projected = _prompt_object(value, ("title", "description"))
    if not isinstance(value, Mapping):
        return projected
    samples = value.get("gallerySamples")
    if isinstance(samples, Mapping):
        projected["gallerySamples"] = {
            placement: _prompt_object(samples[placement], ("assetKey", "placement", "purpose"))
            for placement in ("feed", "story") if placement in samples
        }
    if "metaCopyDefaults" in value:
        projected["metaCopyDefaults"] = _prompt_object(value["metaCopyDefaults"], ("primaryText", "headlines", "descriptions", "cta"))
    if "aiWritingGuidance" in value:
        projected["aiWritingGuidance"] = _prompt_object(value["aiWritingGuidance"], ("summary", "fields"))
    publish = value.get("publishRequirements")
    if isinstance(publish, Mapping):
        publish_out = _prompt_object(publish, ("objective", "specialAdCategory", "requiredCtaTypes"))
        if "instantForm" in publish:
            publish_out["instantForm"] = _prompt_object(publish["instantForm"], ("required", "dependency", "defaults"))
        if "destination" in publish:
            publish_out["destination"] = _prompt_object(publish["destination"], ("required", "kind", "dependency"))
        projected["publishRequirements"] = publish_out
    if isinstance(value.get("replacementAssets"), list):
        projected["replacementAssets"] = [_prompt_object(item, ("inputKey", "assetKey", "purpose")) for item in value["replacementAssets"][:512]]
    if isinstance(value.get("realAssetRefs"), list):
        projected["realAssetRefs"] = [_prompt_object(item, ("inputKey", "kind", "required")) for item in value["realAssetRefs"][:512]]
    return projected

def _prompt_template(value: Any) -> Dict[str, Any]:
    projected = _prompt_object(value, ("schema", "templateId", "createdAt"))
    if not isinstance(value, Mapping):
        return projected
    if "feedLayout" in value:
        projected["feedLayout"] = _prompt_layout(value["feedLayout"])
    if "storyLayout" in value:
        projected["storyLayout"] = _prompt_layout(value["storyLayout"])
    if isinstance(value.get("imageInputs"), list):
        projected["imageInputs"] = [_prompt_object(item, ("key", "label", "required", "acceptedTypes", "defaultAssetKey")) for item in value["imageInputs"][:512]]
    if isinstance(value.get("textInputs"), list):
        projected["textInputs"] = [_prompt_object(item, ("key", "label", "placeholder", "maxLength")) for item in value["textInputs"][:512]]
    if "semanticColours" in value:
        projected["semanticColours"] = _prompt_object(value["semanticColours"], tuple(sorted(COLOUR_ROLES)))
    declarations = value.get("assets")
    if isinstance(declarations, Mapping):
        projected["assets"] = {str(key)[:160]: _prompt_object(item, ("fileName", "mimeType")) for key, item in list(declarations.items())[:512]}
    if isinstance(value.get("fonts"), list):
        projected["fonts"] = [_prompt_object(item, ("file",)) for item in value["fonts"][:128]]
    if "metadata" in value:
        projected["metadata"] = _prompt_metadata(value["metadata"])
    return projected

def _candidate_contract_projection(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"template": {}, "assets": []}
    raw_assets = value.get("assets")
    assets = [_prompt_object(item, ("assetKey", "fileName", "mimeType")) for item in raw_assets[:512]] if isinstance(raw_assets, list) else []
    return {"template": _prompt_template(value.get("template")), "assets": assets}

def _safe_candidate_prompt_json(value: Any) -> str:
    """Return compact, exact allowlisted builder-contract JSON for the next model call."""
    if not isinstance(value, Mapping):
        return ""
    encoded = json.dumps(_candidate_contract_projection(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded) > MAX_CANDIDATE_CONTEXT_CHARS:
        raise AdTemplateProcessError(f"safe candidate context exceeds {MAX_CANDIDATE_CONTEXT_CHARS} characters")
    return encoded

def _candidate_trace_projection(value: Any) -> Dict[str, Any]:
    projected = _candidate_contract_projection(value)
    previews = []
    if isinstance(value, Mapping) and isinstance(value.get("previews"), list):
        for item in value["previews"][:16]:
            if not isinstance(item, Mapping):
                continue
            name = Path(str(item.get("name") or "")).name
            placement = str(item.get("placement") or "").lower()
            if name and placement in {"feed", "story"}:
                previews.append({"name": name, "placement": placement})
    projected["previews"] = previews
    return projected


def persist_rejected_candidate(candidate: Any, iteration_workspace: Path, *, iteration: int, attempt: int, reason: str) -> Path:
    safe_candidate = _candidate_contract_projection(candidate)
    iteration_workspace.mkdir(parents=True, exist_ok=True)
    path = iteration_workspace / f"rejected-candidate-{attempt:02d}.json"
    evidence = {"iteration": iteration, "attempt": attempt, "reason": reason, "candidate": safe_candidate}
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(evidence, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)
    return path

def resolve_catalog_assets(template: Mapping[str, Any], assets: Any) -> List[Dict[str, Any]]:
    if not isinstance(assets, list): raise AdTemplateProcessError("builder assets must be a list")
    declarations = template.get("assets") if isinstance(template.get("assets"), dict) else {}
    catalog = os.environ.get("AD_TEMPLATE_ASSET_CATALOG_DIR", "/vps/ad-template-assets").strip()
    root = Path(catalog).expanduser().resolve() if catalog else None
    resolved = []
    seen = set()
    for item in assets:
        if not isinstance(item, dict): raise AdTemplateProcessError("asset declaration must be an object")
        key = str(item.get("assetKey") or "").strip()
        if not key or key in seen or key not in declarations: raise AdTemplateProcessError("asset key is undeclared or duplicated")
        seen.add(key)
        declared = declarations[key]
        if not isinstance(declared, dict):
            raise AdTemplateProcessError("template asset declaration must be an object")
        if "bytesBase64" in item:
            raise AdTemplateProcessError("builder must never return asset bytes")
        file_name = str(item.get("fileName") or "").strip()
        mime = str(item.get("mimeType") or "").strip()
        declared_file = str(declared.get("fileName") or "").strip()
        declared_mime = str(declared.get("mimeType") or "").strip()
        if file_name != declared_file or mime != declared_mime:
            raise AdTemplateProcessError("builder asset metadata must exactly match the template declaration")
        relative = Path(file_name)
        if not file_name or "\\" in file_name or relative.is_absolute() or relative.as_posix() != file_name or any(part in {"", ".", ".."} for part in relative.parts):
            raise AdTemplateProcessError("asset fileName must be a normalized relative catalog path")
        guessed_mime = mimetypes.guess_type(file_name)[0]
        if not mime or (not mime.startswith("image/") and not mime.startswith("font/")) or guessed_mime != mime:
            raise AdTemplateProcessError("asset mimeType does not match its catalog file")
        if root is None: raise AdTemplateProcessError("AD_TEMPLATE_ASSET_CATALOG_DIR is required for catalog assets")
        path = (root / relative).resolve()
        if not _under(path, root) or not path.is_file(): raise AdTemplateProcessError("asset is missing from the source-free catalog")
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        resolved.append({"assetKey": key, "fileName": file_name, "mimeType": mime, "bytesBase64": encoded})
    if set(declarations) != seen: raise AdTemplateProcessError("every declared template asset must be supplied")
    return resolved

def run_generator_cli(candidate: Mapping[str, Any], workspace: Path) -> Dict[str, Any]:
    command = os.environ.get("AD_TEMPLATE_GENERATOR_CMD", "").strip()
    if not command: raise AdTemplateProcessError("AD_TEMPLATE_GENERATOR_CMD must point to the shared Blockwise renderer CLI")
    workspace.mkdir(parents=True, exist_ok=True)
    artifact_path = workspace / "artifact.json"
    if not isinstance(candidate, Mapping) or set(candidate) != {"template", "assets"} or not isinstance(candidate.get("assets"), list):
        raise AdTemplateProcessError("builder must return template and assets")
    _reject_builder_bytes(candidate)
    template = validate_template_artifact(candidate.get("template"))
    assets = resolve_catalog_assets(template, candidate["assets"])
    artifact_path.write_text(json.dumps({"template": template, "assets": assets}, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    out_dir = workspace / "rendered"
    argv = shlex.split(command) + ["--input", str(artifact_path), "--assets-dir", str(workspace), "--out-dir", str(out_dir)]
    proc = subprocess.run(argv, text=True, capture_output=True, timeout=600, check=False)
    if proc.returncode: raise AdTemplateProcessError(f"shared Blockwise renderer failed ({proc.returncode})")
    receipt_path = out_dir / "receipt.json"
    try: receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError): raise AdTemplateProcessError("shared renderer returned no receipt") from None
    outputs = receipt.get("outputs") if isinstance(receipt, dict) else {}
    if not isinstance(outputs, dict) or not all(isinstance(outputs.get(place), dict) and isinstance(outputs[place].get("path"), str) for place in ("feed", "story")):
        raise AdTemplateProcessError("shared renderer receipt is incomplete")
    previews = [{"name": Path(outputs[place]["path"]).name, "path": outputs[place]["path"], "placement": place, "width": outputs[place].get("width"), "height": outputs[place].get("height")} for place in ("feed", "story")]
    result = {"template": template, "assets": assets, "previews": previews, "render": {place: outputs[place]["path"] for place in ("feed", "story")}, "receipt": receipt, "template_path": str(artifact_path)}
    validate_artifacts(result, workspace)
    return result

def blockwise_artifact_template(template: Any, *, run_id: str) -> Dict[str, Any]:
    return validate_template_artifact(template)

def import_template(output: Mapping[str, Any], *, run_id: str, project_id: str) -> Dict[str, Any]:
    url = os.environ.get("BLOCKWISE_TEMPLATE_IMPORT_URL", "").strip()
    token = (os.environ.get("BLOCKWISE_INTERNAL_AUTH_SECRET") or "").strip()
    if not url or not token: raise AdTemplateProcessError("Blockwise template import endpoint and BLOCKWISE_INTERNAL_AUTH_SECRET are required")
    template = blockwise_artifact_template(output.get("template"), run_id=run_id)
    assets = output.get("assets") if isinstance(output.get("assets"), list) else []
    if not assets: raise AdTemplateProcessError("Blockwise import requires declared assets")
    for asset in assets:
        if not isinstance(asset, dict) or not all(asset.get(key) for key in ("assetKey", "fileName", "mimeType", "bytesBase64")):
            raise AdTemplateProcessError("Blockwise asset payload is invalid")
    body = json.dumps({"template": template, "assets": assets}).encode()
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    import_host = os.environ.get("BLOCKWISE_TEMPLATE_IMPORT_HOST", "").strip()
    if import_host:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(?::[0-9]{1,5})?", import_host):
            raise AdTemplateProcessError("BLOCKWISE_TEMPLATE_IMPORT_HOST is invalid")
        headers["Host"] = import_host
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response: payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc: raise AdTemplateProcessError("Blockwise template import failed") from exc
    if not isinstance(payload, dict) or not payload.get("templateId"): raise AdTemplateProcessError("Blockwise import returned no templateId")
    replayed = bool(payload.get("replayed"))
    return {"template_id": str(payload["templateId"]), "status": "replayed" if replayed else "imported", "asset_count": int(payload.get("assetCount") or len(assets)), "replayed": replayed}

def generator_prompt(*, run_id: str, project_id: str, brief: str, placements: Any, source: str, feedback: str = "", validation_feedback: str = "", repair_attempt: int = 0, prior_candidate: Any = None, rejected_candidate: Any = None) -> str:
    repair_clause = (
        ' Return one JSON object with exactly two top-level keys: {"template": {...}, "assets": []}; '
        'never omit assets even when it is empty, and do not add prose or another wrapper. '
        'For every image_slot, mask must be exactly rounded_rect, circle, or none; defaultCrop must be exactly '
        '{"x":0,"y":0,"width":1,"height":1} for a full-source crop (not "cover"); allowedPlacementOverrides '
        'must be a JSON list containing only crop and/or position (not feed/story). For every text layer, alignment '
        'must be exactly left, center, or right and overflowBehaviour must be exactly refuse, truncate, or scale_down (never ellipsis). '
        'lineHeight is a unitless multiplier between 0.8 and 2.5, normally 1.1 to 1.6; never supply lineHeight in pixels. '
        ' For every vector layer, shape must be exactly one of rect, rounded, circle, line, pill, notched, wave, or ring; '
        'never use aliases such as rectangle or rounded_rect_stroke. Every icon layer must use exactly arrow, check, phone, mail, globe, or pin; '
        'other icon names render as empty circles and are invalid. Every image_slot inputKey must be declared exactly once '
        'in imageInputs. Do not emit a logo layer because the current source-free catalog has no visible logo default; recreate '
        'every source logo/brand role as editable brand text plus a simple supported vector mark; declare that text input exactly once '
        'in textInputs. Every text layer inputKey must be declared exactly once in textInputs. imageInputs and textInputs keys must be unique across both lists. Every text-layer font file must appear exactly once in fonts. '
        'Every layer assetKey, image defaultAssetKey, gallery sample assetKey, and replacementAssets assetKey must resolve '
        'to template.assets. Every replacementAssets inputKey must resolve to imageInputs. Every realAssetRefs inputKey may '
        'resolve to imageInputs or textInputs, and must name a declared key. '
        'Each realAssetRefs entry must contain exactly {"inputKey":"declaredKey","kind":"non_empty_descriptive_kind","required":true}; '
        'kind is a non-empty descriptive string such as property_photo, interior_photo, brand_logo, property_address, or contact_details.'
        ' fonts must always be a JSON list such as [{"file":"manrope-400.woff2"},{"file":"manrope-700.woff2"}]; '
        'never return fonts as an object, named map, or record.'
    )
    if validation_feedback:
        repair_clause += (
            f" STRICT SCHEMA REPAIR {repair_attempt} of {MAX_SCHEMA_REPAIRS_PER_ITERATION}: "
            f"the previous candidate was rejected before rendering with this exact validation error: {validation_feedback}. "
            "Return a complete corrected template-and-assets JSON object, not a patch."
        )
    rejected_json = _safe_candidate_prompt_json(rejected_candidate)
    prior_json = _safe_candidate_prompt_json(prior_candidate)
    if rejected_json:
        repair_clause += (
            " IMMEDIATELY PRIOR REJECTED CANDIDATE (safe builder-contract JSON): "
            f"{rejected_json}. Make the minimum correction required by the exact validation error, preserve every "
            "already-correct field, and return the complete corrected {template,assets} object."
        )
    elif prior_json:
        repair_clause += (
            " PRIOR VALID CANDIDATE TO REVISE IN PLACE (safe builder-contract JSON): "
            f"{prior_json}. Revise this candidate in place using the reviewer feedback; preserve every correct layer, "
            "input, asset, font, metadata field, and cross-reference, and return the complete revised {template,assets} object."
        )

    return f"""Run the sole ad-template process as the builder agent. Inspect the attached source pixels, remove advertiser identity (names, logos, phones, URLs, portraits), and return JSON with exactly {{template, assets}}. template must contain exactly schema='blockwise.ad-template', templateId, createdAt (ISO datetime), feedLayout, storyLayout, imageInputs, textInputs, semanticColours, assets, fonts, metadata. semanticColours must contain exactly background, primary, secondary, accent, mainText, inverseText; every layer colourRole is one of those six. Each layout contains exactly placement, layers, safeZones; Feed is placement feed within 1080x1350 and Story is placement story within 1080x1920, including safeZones. Geometry and every safe-zone rectangle contain exactly {{x,y,width,height}}: x,y are the top-left coordinates from canvas origin (0,0); width,height are positive sizes, not right/bottom coordinates; x + width must stay within canvas width and y + height within canvas height. Exact valid examples are Feed safeZones=[{{"x":48,"y":48,"width":984,"height":1254}}] within 1080x1350 and Story safeZones=[{{"x":60,"y":250,"width":960,"height":1420}}] within 1080x1920. Layer shapes are exact: plate={{type,layerId,colourRole,assetKey?,geometry,protected}}; image_slot={{type,layerId,inputKey,geometry,mask,minSourceWidth,minSourceHeight,defaultCrop,allowedPlacementOverrides}}; overlay_patch={{type,layerId,geometry,colourRole,opacity,assetKey?}}; text={{type,layerId,inputKey,font:{{file}},fontSize,lineHeight,tracking,alignment,maxCharacters,maxLines,colourRole,overflowBehaviour,geometry}}; logo={{type,layerId,inputKey,geometry}}; vector={{type,layerId,geometry,shape,colourRole,opacity}}; icon={{type,layerId,geometry,icon,colourRole}}. imageInputs are {{key,label,required?,acceptedTypes,defaultAssetKey?}}; textInputs are {{key,label,placeholder,maxLength}}. fonts are {{file}} and every text-layer font must be declared; allowed bundled font files are {', '.join(sorted(ALLOWED_FONT_FILES))}. metadata is exact: title:string; description:string; gallerySamples={{feed?:{{assetKey?,placement,purpose}},story?:{{assetKey?,placement,purpose}}}}; metaCopyDefaults={{primaryText:string[],headlines:string[],descriptions:string[],cta:string}}; aiWritingGuidance={{summary:string,fields:record<string,string>}}; publishRequirements={{objective:string,specialAdCategory:string|null,instantForm:{{required:boolean,dependency:string|null,defaults?:record<string,string>}},destination:{{required:boolean,kind:'website'|'instant_form'|'none',dependency:string|null}},requiredCtaTypes?:string[]}}; replacementAssets={{inputKey,assetKey,purpose?}}[]; realAssetRefs={{inputKey,kind,required}}[]. Every layer/input/font/colour/asset metadata reference must resolve inside the same template. Declare source-free replacement assets in template.assets as assetKey -> {{fileName,mimeType}} and in top-level assets only as {{assetKey,fileName,mimeType}} with an exact match. Hermes resolves bytes from the fixed safe catalog; never emit bytesBase64 anywhere and never guess filenames. Allowed normalized relative catalog paths are home/open-home-living.webp, home/home-dusk.webp, home/mt-lawley-federation.webp, home/home-pool.webp, home/interior-styled.webp, home/subiaco-townhouse.webp, and adstudio-samples/photos/int-bedroom.png. Never include the source composite, source_path, hashes, signatures, private fields, or a full-source plate. Keep geometry inside canvas and Story native. Hermes renders, compares once per iteration, then runs two fresh final reviewers only after comparator >= {THRESHOLD}; never self-score or invent review evidence. Run {run_id}; project {project_id}; fixed placements {json.dumps(placements)}; brief {brief[:4000]}; prior reviewer feedback {feedback[:3000]}.{repair_clause}"""

def review_prompt(*, final: bool) -> str:
    role = "fresh independent final reviewer" if final else "iteration comparator"
    role += (
        ". Treat the source only as the structural visual reference. Mandatory removal of source advertiser identities, "
        "logos, names, phones, URLs, portraits, contact details, source photography, and all source pixels must not be penalized. "
        "Instead, score whether the same visual roles, media-slot count and shapes, composition, spacing, hierarchy, typography, "
        "and colour relationships are recreated with source-free photography and neutral editable logo, image, contact, copy, icon, "
        "patch, and CTA layers. A neutral editable replacement is correct; copied source identity or pixels, or a missing editable role, is not"
    )
    return f"""Act as the {role}. Compare the attached source image with the attached rendered Feed and native Story previews. Return JSON only with exactly reason, hard_failures, and rubric. rubric must contain exactly these five numeric 0-10 fields: layout_geometry (source composition, proportions, spacing, alignment), hierarchy_typography (type hierarchy, wrapping, readability), colour_tone (palette, contrast, photographic tone), editable_decomposition (all copy, logo, CTA, patches, icons and media are real editable layers), native_story (a deliberate 1080x1920 composition, not a stretched, cropped, or letterboxed Feed). Explain concrete visible mismatches in reason. hard_failures must be a JSON list. Add a hard failure if any source advertiser name, logo, phone, URL, portrait, contact identity, or source composite pixel is reused; if Feed or Story is missing, clipped, unreadable, outside its canvas or safe zones; if critical text, logo, CTA, patch, icon or media is flattened/non-editable; if Story is a Feed crop, stretch, letterbox, or has essential content in platform UI zones; or if an asset is missing/unknown. Any hard failure makes the score zero. Do not reuse or infer another reviewer's score. Passing requires the mean of all five fields to be at least {THRESHOLD} with no hard failures."""

def vision_message(text: str, paths: List[str]) -> List[Dict[str, Any]]:
    parts: List[Dict[str, Any]] = [{"type": "text", "text": text}]
    for raw in paths:
        path = Path(str(raw)).expanduser().resolve()
        if not path.is_file():
            raise AdTemplateProcessError("vision input image is missing")
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"}})
    if len(parts) == 1:
        raise AdTemplateProcessError("vision role requires attached image pixels")
    return parts

class SoleProcessOrchestrator:
    """Runs builder, comparator, final reviewers, renderer, and importer as separate roles."""
    def __init__(self, *, call_agent: Callable[[str, Any, str], Dict[str, Any]], workspace: Path, run_id: str, project_id: str, emit: Callable[[str, str, Dict[str, Any]], None], should_stop: Callable[[], bool] | None = None):
        self.call_agent, self.workspace, self.run_id, self.project_id, self.emit = call_agent, workspace, run_id, project_id, emit
        self.should_stop = should_stop or (lambda: False)

    def _check_stop(self) -> None:
        if self.should_stop():
            raise AdTemplateProcessError("sole ad-template process was cancelled")

    def run(self, *, source: str, brief: str, placements: Any, routes: List[Dict[str, str]], review_round: int = 0, total_iterations: int = 0, feedback: str = "", history: List[Dict[str, Any]] | None = None, revision_candidate: Mapping[str, Any] | None = None) -> Dict[str, Any]:
        if len(routes) < 4: raise AdTemplateProcessError("builder, comparator, and two final reviewers require four configured roles")
        self._check_stop()
        history = list(history or [])
        iterations = []
        candidate: Dict[str, Any] = {}
        for offset in range(31 - total_iterations - 1):
            index = total_iterations + offset + 1
            iteration_prior = revision_candidate if offset == 0 else candidate
            iteration_workspace = self.workspace / "iterations" / f"{index:02d}"
            self._check_stop()
            self.emit("stage.started", "build", {"iteration": index, "role": "builder"})
            validation_feedback = ""
            rejected_candidate: Any = None
            for repair_attempt in range(MAX_SCHEMA_REPAIRS_PER_ITERATION + 1):
                self._check_stop()
                instance = "builder-%d" % index if repair_attempt == 0 else "builder-%d-repair-%d" % (index, repair_attempt)
                candidate = self.call_agent(
                    instance,
                    vision_message(generator_prompt(
                        run_id=self.run_id, project_id=self.project_id, brief=brief,
                        placements=placements, source=source, feedback=feedback,
                        validation_feedback=validation_feedback, repair_attempt=repair_attempt,
                        prior_candidate=iteration_prior if repair_attempt == 0 else None,
                        rejected_candidate=rejected_candidate if repair_attempt > 0 else None,
                    ), [source]),
                    f"{routes[0].get('provider')}/{routes[0].get('model')}",
                )
                self._check_stop()
                try:
                    validate_builder_candidate(candidate)
                except AdTemplateProcessError as exc:
                    validation_feedback = str(exc)
                    self._check_stop()
                    rejected_candidate = candidate
                    persist_rejected_candidate(
                        candidate, iteration_workspace, iteration=index,
                        attempt=repair_attempt + 1, reason=validation_feedback,
                    )
                    self._check_stop()
                    self.emit("candidate.rejected", "build", {
                        "iteration": index, "attempt": repair_attempt + 1,
                        "reason": validation_feedback, "decision": "repair",
                    })
                    self._check_stop()
                    if repair_attempt >= MAX_SCHEMA_REPAIRS_PER_ITERATION:
                        raise AdTemplateProcessError(
                            f"builder candidate remained schema-invalid after {MAX_SCHEMA_REPAIRS_PER_ITERATION} repairs: {validation_feedback}"
                        ) from None
                    continue
                break
            self.emit("iteration.started", "build", {"iteration": index, "role": "builder"})
            # Only a strictly valid candidate becomes a visual iteration.
            self._check_stop()
            rendered = run_generator_cli(candidate, iteration_workspace)
            self._check_stop()
            preview_receipt = validate_artifacts(rendered, iteration_workspace)
            public_preview_root = self.workspace / "previews"
            public_preview_root.mkdir(parents=True, exist_ok=True)
            public_previews = []
            for item in rendered.get("previews", []):
                placement = str(item.get("placement") or "preview").lower()
                destination = public_preview_root / f"iteration-{index:02d}-{placement}.png"
                shutil.copyfile(str(item["path"]), destination)
                public_previews.append({"name": destination.name, "path": str(destination), "placement": placement})
            public_render = {}
            for placement in ("feed", "story"):
                destination = public_preview_root / f"iteration-{index:02d}-{placement}.png"
                public_render[placement] = str(destination)
            rendered["previews"] = public_previews
            rendered["render"] = public_render
            validate_artifacts(rendered, self.workspace)
            self.emit("iteration.rendered", "render", {"iteration": index, "previews": [{"name": str(x.get("name") or ""), "placement": str(x.get("placement") or "")} for x in public_previews]})
            candidate = {**candidate, **rendered}
            self._check_stop()
            comparison = self.call_agent("comparator-%d" % index, vision_message(review_prompt(final=False), [source, str((rendered.get("render") or {}).get("feed") or ""), str((rendered.get("render") or {}).get("story") or "")]), f"{routes[1].get('provider')}/{routes[1].get('model')}")
            self._check_stop()
            evidence = _assessment(comparison, "comparator")
            score, reason = evidence["score"], evidence["reason"]
            decision = "accepted" if score >= THRESHOLD else "revise"
            record = {"iteration": index, "candidate": _candidate_trace_projection(candidate), "comparison": evidence, "decision": decision}
            iterations.append(record)
            self.emit("iteration.compared", "compare", {"iteration": index, "score": score, "reason": reason, "decision": decision, "preview_names": [str(x.get("name")) for x in candidate.get("previews", []) if isinstance(x, dict)]})
            if score >= THRESHOLD: break
            feedback = reason
        if not iterations or iterations[-1]["decision"] != "accepted": raise AdTemplateProcessError("comparator never reached threshold")
        reviewers = []
        for n, route in enumerate(routes[2:4], 1):
            identity = f"final-reviewer-{self.run_id}-{n}-{uuid.uuid4().hex[:8]}"
            provider_route = f"{route.get('provider')}/{route.get('model')}"
            route_identity = provider_route
            self.emit("final-review.started", "final-check", {"reviewer": identity, "route": route_identity})
            self._check_stop()
            review = self.call_agent(identity, vision_message(review_prompt(final=True), [source, str((candidate.get("render") or {}).get("feed") or ""), str((candidate.get("render") or {}).get("story") or "")]), provider_route)
            self._check_stop()
            if not isinstance(review, dict): raise AdTemplateProcessError("final reviewer returned invalid result")
            evidence = _assessment(review, "final reviewer")
            reviewers.append({"id": identity, "route": route_identity, **evidence})
        final_review = validate_final_review({"reviewers": reviewers}, accepted=True)
        if final_review["decision"] != "accepted":
            self.emit("final-review.completed", "final-check", {"decision": "revise", "reviewers": final_review["reviewers"]})
            if review_round >= 5 or total_iterations + len(iterations) >= 30:
                raise AdTemplateProcessError("final reviewers failed after the bounded automatic revision loop")
            iterations[-1]["final_review_failed"] = True
            reasons = "; ".join(f"{item['id']}: {item['reason']}" for item in final_review["reviewers"])
            return self.run(source=source, brief=brief, placements=placements, routes=routes, review_round=review_round + 1, total_iterations=total_iterations + len(iterations), feedback=reasons, history=history + iterations, revision_candidate=candidate)
        self.emit("final-review.completed", "final-check", {"decision": "accepted", "reviewers": final_review["reviewers"]})
        generated = candidate
        documents = deterministic_documents(generated.get("template"))
        validate_artifacts(generated, self.workspace)
        self._check_stop()
        imported = import_template({**generated, "documents": documents}, run_id=self.run_id, project_id=self.project_id)
        self._check_stop()
        self.emit("template.imported", "live", imported)
        return {"template": generated.get("template") or candidate.get("template"), "iterations": history + iterations, "final_review": final_review, "previews": generated.get("previews"), "documents": documents, "template_path": generated.get("template_path"), "render_path": generated.get("render_path") or generated.get("render", {}).get("feed"), "import": imported, "process": "only-ad-template-process"}
