"""Executable orchestration for the sole ad-template process."""
from __future__ import annotations
import base64, json, math, mimetypes, os, re, shlex, shutil, subprocess, sys, urllib.error, urllib.request, uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping

THRESHOLD = 9.5
STAGES = ("source", "build", "render", "compare", "final-check", "live")

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
    if not isinstance(layout, dict) or set(layout) != {"placement", "layers", "safeZones"} or layout.get("placement") != placement:
        raise AdTemplateProcessError(f"{placement} layout does not match the Blockwise contract")
    layers = layout.get("layers")
    safe_zones = layout.get("safeZones")
    if not isinstance(layers, list) or not 1 <= len(layers) <= 256 or not isinstance(safe_zones, list) or len(safe_zones) > 32 or not all(_rect(item, bounds=(width, height)) for item in safe_zones):
        raise AdTemplateProcessError(f"{placement} layout layers or safe zones are invalid")
    ids = set()
    for layer in layers:
        if not isinstance(layer, dict) or layer.get("type") not in LAYER_FIELDS:
            raise AdTemplateProcessError(f"{placement} layout contains an unsupported layer")
        required, optional = LAYER_FIELDS[layer["type"]]
        if not _strict_keys(layer, required, optional) or not _nonempty(layer.get("layerId")) or layer["layerId"] in ids or not _rect(layer.get("geometry"), bounds=(width, height)):
            raise AdTemplateProcessError(f"{placement} layout contains an invalid or duplicate layer")
        ids.add(layer["layerId"])
        layer_type = layer["type"]
        for key in ("inputKey", "colourRole", "icon", "assetKey"):
            if key in layer and not _nonempty(layer[key]):
                raise AdTemplateProcessError(f"{placement} {layer_type} layer has an empty {key}")
        if "colourRole" in layer and layer["colourRole"] not in COLOUR_ROLES:
            raise AdTemplateProcessError(f"{placement} {layer_type} colourRole is invalid")
        if layer_type == "plate" and not isinstance(layer["protected"], bool):
            raise AdTemplateProcessError("plate protected must be boolean")
        if layer_type == "image_slot":
            if layer["mask"] not in {"rounded_rect", "circle", "none"} or not _positive_int(layer["minSourceWidth"]) or not _positive_int(layer["minSourceHeight"]) or not _rect(layer["defaultCrop"]):
                raise AdTemplateProcessError("image slot constraints are invalid")
            overrides = layer["allowedPlacementOverrides"]
            if not isinstance(overrides, list) or any(item not in {"crop", "position"} for item in overrides):
                raise AdTemplateProcessError("image slot placement overrides are invalid")
        if layer_type in {"overlay_patch", "vector"} and (not _number_value(layer["opacity"]) or not 0 <= layer["opacity"] <= 1):
            raise AdTemplateProcessError(f"{layer_type} opacity is invalid")
        if layer_type == "text":
            if not _font(layer["font"]) or not _number_value(layer["fontSize"], positive=True) or not _number_value(layer["lineHeight"], positive=True) or not _number_value(layer["tracking"]) or not _positive_int(layer["maxCharacters"]) or not _positive_int(layer["maxLines"]):
                raise AdTemplateProcessError("text layer sizing is invalid")
            if layer["alignment"] not in {"left", "center", "right"} or layer["overflowBehaviour"] not in {"refuse", "truncate", "scale_down"}:
                raise AdTemplateProcessError("text layer behaviour is invalid")
        if layer_type == "vector" and layer["shape"] not in {"rect", "rounded", "circle", "line", "pill", "notched", "wave", "ring"}:
            raise AdTemplateProcessError("vector shape is invalid")

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
    if not isinstance(value["semanticColours"], dict) or set(value["semanticColours"]) != COLOUR_ROLES or not all(_nonempty(colour) for colour in value["semanticColours"].values()):
        raise AdTemplateProcessError("semanticColours must contain exactly the six Blockwise colour roles")
    if not isinstance(value["assets"], dict):
        raise AdTemplateProcessError("assets must be a declaration record")
    for key, asset in value["assets"].items():
        if not _nonempty(key) or not isinstance(asset, dict) or set(asset) != {"fileName", "mimeType"} or not _nonempty(asset["fileName"]) or not _nonempty(asset["mimeType"]):
            raise AdTemplateProcessError("template asset declarations are invalid")
    if not isinstance(value["fonts"], list) or not all(_font(item) and item["file"] in ALLOWED_FONT_FILES for item in value["fonts"]):
        raise AdTemplateProcessError("template fonts do not match the Blockwise contract")
    font_files = {item["file"] for item in value["fonts"]}
    asset_keys = set(value["assets"])
    for item in value["imageInputs"]:
        if item.get("defaultAssetKey") and item["defaultAssetKey"] not in asset_keys:
            raise AdTemplateProcessError("image input default asset is undeclared")
    for layout in (value["feedLayout"], value["storyLayout"]):
        for layer in layout["layers"]:
            if layer["type"] in {"image_slot", "logo"} and layer["inputKey"] not in image_keys:
                raise AdTemplateProcessError("image layer inputKey is undeclared")
            if layer["type"] == "text" and (layer["inputKey"] not in text_keys or layer["font"]["file"] not in font_files):
                raise AdTemplateProcessError("text layer inputKey or font is undeclared")
            if layer.get("assetKey") and layer["assetKey"] not in asset_keys:
                raise AdTemplateProcessError("layer assetKey is undeclared")
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
    for item in real_assets:
        if not isinstance(item, dict) or set(item) != {"inputKey", "kind", "required"} or item["inputKey"] not in image_keys or not _nonempty(item["kind"]) or not isinstance(item["required"], bool):
            raise AdTemplateProcessError("metadata real asset reference is invalid")
    return value

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

def generator_prompt(*, run_id: str, project_id: str, brief: str, placements: Any, source: str, feedback: str = "") -> str:
    return f"""Run the sole ad-template process as the builder agent. Inspect the attached source pixels, remove advertiser identity (names, logos, phones, URLs, portraits), and return JSON with exactly {{template, assets}}. template must contain exactly schema='blockwise.ad-template', templateId, createdAt (ISO datetime), feedLayout, storyLayout, imageInputs, textInputs, semanticColours, assets, fonts, metadata. semanticColours must contain exactly background, primary, secondary, accent, mainText, inverseText; every layer colourRole is one of those six. Each layout contains exactly placement, layers, safeZones; Feed is placement feed within 1080x1350 and Story is placement story within 1080x1920, including safeZones. Layer shapes are exact: plate={{type,layerId,colourRole,assetKey?,geometry,protected}}; image_slot={{type,layerId,inputKey,geometry,mask,minSourceWidth,minSourceHeight,defaultCrop,allowedPlacementOverrides}}; overlay_patch={{type,layerId,geometry,colourRole,opacity,assetKey?}}; text={{type,layerId,inputKey,font:{{file}},fontSize,lineHeight,tracking,alignment,maxCharacters,maxLines,colourRole,overflowBehaviour,geometry}}; logo={{type,layerId,inputKey,geometry}}; vector={{type,layerId,geometry,shape,colourRole,opacity}}; icon={{type,layerId,geometry,icon,colourRole}}. Geometry is exactly {{x,y,width,height}}. imageInputs are {{key,label,required?,acceptedTypes,defaultAssetKey?}}; textInputs are {{key,label,placeholder,maxLength}}. fonts are {{file}} and every text-layer font must be declared; allowed bundled font files are {', '.join(sorted(ALLOWED_FONT_FILES))}. metadata is exact: title:string; description:string; gallerySamples={{feed?:{{assetKey?,placement,purpose}},story?:{{assetKey?,placement,purpose}}}}; metaCopyDefaults={{primaryText:string[],headlines:string[],descriptions:string[],cta:string}}; aiWritingGuidance={{summary:string,fields:record<string,string>}}; publishRequirements={{objective:string,specialAdCategory:string|null,instantForm:{{required:boolean,dependency:string|null,defaults?:record<string,string>}},destination:{{required:boolean,kind:'website'|'instant_form'|'none',dependency:string|null}},requiredCtaTypes?:string[]}}; replacementAssets={{inputKey,assetKey,purpose?}}[]; realAssetRefs={{inputKey,kind,required}}[]. Every layer/input/font/colour/asset metadata reference must resolve inside the same template. Declare source-free replacement assets in template.assets as assetKey -> {{fileName,mimeType}} and in top-level assets only as {{assetKey,fileName,mimeType}} with an exact match. Hermes resolves bytes from the fixed safe catalog; never emit bytesBase64 anywhere and never guess filenames. Allowed normalized relative catalog paths are home/open-home-living.webp, home/home-dusk.webp, home/mt-lawley-federation.webp, home/home-pool.webp, home/interior-styled.webp, home/subiaco-townhouse.webp, and adstudio-samples/photos/int-bedroom.png. Never include the source composite, source_path, hashes, signatures, private fields, or a full-source plate. Keep geometry inside canvas and Story native. Hermes renders, compares once per iteration, then runs two fresh final reviewers only after comparator >= {THRESHOLD}; never self-score or invent review evidence. Run {run_id}; project {project_id}; fixed placements {json.dumps(placements)}; brief {brief[:4000]}; prior reviewer feedback {feedback[:3000]}"""

def review_prompt(*, final: bool) -> str:
    role = "fresh independent final reviewer" if final else "iteration comparator"
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

    def run(self, *, source: str, brief: str, placements: Any, routes: List[Dict[str, str]], review_round: int = 0, total_iterations: int = 0, feedback: str = "", history: List[Dict[str, Any]] | None = None) -> Dict[str, Any]:
        if len(routes) < 4: raise AdTemplateProcessError("builder, comparator, and two final reviewers require four configured roles")
        self._check_stop()
        history = list(history or [])
        iterations = []
        candidate: Dict[str, Any] = {}
        for offset in range(31 - total_iterations - 1):
            index = total_iterations + offset + 1
            self.emit("stage.started", "build", {"iteration": index, "role": "builder"})
            self._check_stop()
            candidate = self.call_agent("builder-%d" % index, vision_message(generator_prompt(run_id=self.run_id, project_id=self.project_id, brief=brief, placements=placements, source=source, feedback=feedback), [source]), f"{routes[0].get('provider')}/{routes[0].get('model')}")
            self._check_stop()
            if not isinstance(candidate, dict): raise AdTemplateProcessError("builder returned invalid candidate")
            self.emit("iteration.started", "build", {"iteration": index, "role": "builder"})
            # Render every candidate before comparison. This makes each
            # iteration preview a real generator artifact, not an agent claim.
            iteration_workspace = self.workspace / "iterations" / f"{index:02d}"
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
            record = {"iteration": index, "candidate": candidate, "comparison": evidence, "decision": decision}
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
            return self.run(source=source, brief=brief, placements=placements, routes=routes, review_round=review_round + 1, total_iterations=total_iterations + len(iterations), feedback=reasons, history=history + iterations)
        self.emit("final-review.completed", "final-check", {"decision": "accepted", "reviewers": final_review["reviewers"]})
        generated = candidate
        documents = deterministic_documents(generated.get("template"))
        validate_artifacts(generated, self.workspace)
        self._check_stop()
        imported = import_template({**generated, "documents": documents}, run_id=self.run_id, project_id=self.project_id)
        self._check_stop()
        self.emit("template.imported", "live", imported)
        return {"template": generated.get("template") or candidate.get("template"), "iterations": history + iterations, "final_review": final_review, "previews": generated.get("previews"), "documents": documents, "template_path": generated.get("template_path"), "render_path": generated.get("render_path") or generated.get("render", {}).get("feed"), "import": imported, "process": "only-ad-template-process"}
