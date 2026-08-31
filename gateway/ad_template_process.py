"""Executable orchestration for the sole ad-template process."""
from __future__ import annotations
import base64, json, math, mimetypes, os, re, shlex, shutil, subprocess, sys, urllib.error, urllib.request, uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping

THRESHOLD = 9.5
MIN_RUBRIC_SCORE = 9.2
MAX_ITERATIONS = 60
MAX_FINAL_REVIEW_ROUNDS = 10
MAX_SCHEMA_REPAIRS_PER_ITERATION = 3
MAX_CHEAP_STRUCTURED_OUTPUT_RETRIES = 1
STAGES = ("source", "build", "render", "compare", "final-check", "live")
MAX_CANDIDATE_CONTEXT_CHARS = 100_000
MIN_MATERIAL_SCORE_GAIN = 0.5
STORY_CONTENT_SAFE_ZONE = {"x": 72, "y": 240, "width": 936, "height": 1380}
MAX_COMPARATOR_SELF_CONSISTENCY_RETRIES = 3
MAX_FINAL_REVIEW_OUTPUT_RETRIES = 3
MATERIAL_OVERLAP_RATIO = 0.08

class AdTemplateProcessError(ValueError):
    pass

class ComparatorSelfConsistencyError(AdTemplateProcessError):
    """Comparator feedback contradicts the current layered document."""

    pass

class AdTemplateStructuredOutputError(RuntimeError):
    """A model role returned no complete JSON object for its declared contract."""

    pass

def _number(value: Any) -> float:
    try: result = float(value)
    except (TypeError, ValueError): raise AdTemplateProcessError("score must be numeric") from None
    if not 0 <= result <= 10: raise AdTemplateProcessError("score must be between 0 and 10")
    return result

RUBRIC_FIELDS = (
    "feed_source_likeness",
    "layout_geometry",
    "spacing_proportions",
    "typography_likeness",
    "colour_likeness",
    "image_slot_composition",
    "editable_decomposition",
    "native_story_translation",
)

def _parse_required_change(value: str) -> Dict[str, Any] | None:
    placement = re.search(r"\bplacement\s*=\s*(feed|story)\b", value, re.IGNORECASE)
    layers = re.search(r"\blayers?\s*=\s*([^;]+)", value, re.IGNORECASE)
    current = re.search(r"\bcurrent(?:\s+geometry)?\s*=\s*(\{[^{}]+\})", value, re.IGNORECASE)
    target = re.search(r"\btarget(?:\s+geometry)?\s*=\s*(\{[^{}]+\})", value, re.IGNORECASE)
    change = re.search(r"\bchange\s*=\s*(\S.+)$", value, re.IGNORECASE)
    if not all((placement, layers, current, target, change)):
        return None
    layer_ids = [item.strip() for item in layers.group(1).split(",") if item.strip()]
    if not layer_ids:
        return None
    geometries = []
    for geometry in (current.group(1), target.group(1)):
        parsed: Dict[str, float] = {}
        for key in ("x", "y", "width", "height"):
            match = re.search(
                rf"[\"']?{key}[\"']?\s*[:=]\s*(-?(?:\d+(?:\.\d*)?|\.\d+))",
                geometry,
                re.IGNORECASE,
            )
            if not match:
                return None
            parsed[key] = float(match.group(1))
        geometries.append(parsed)
    return {
        "placement": placement.group(1).lower(),
        "layer_ids": layer_ids,
        "current": geometries[0],
        "target": geometries[1],
        "change": change.group(1).strip(),
    }

def _required_change_is_actionable(value: str) -> bool:
    """Require placement, layer IDs, and current/target geometry in each change."""
    return _parse_required_change(value) is not None

def _rect_matches(left: Mapping[str, Any], right: Mapping[str, Any], tolerance: float = 0.5) -> bool:
    return all(abs(float(left[key]) - float(right[key])) <= tolerance for key in ("x", "y", "width", "height"))

def _material_overlap(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    overlap_width = max(0.0, min(float(left["x"]) + float(left["width"]), float(right["x"]) + float(right["width"])) - max(float(left["x"]), float(right["x"])))
    overlap_height = max(0.0, min(float(left["y"]) + float(left["height"]), float(right["y"]) + float(right["height"])) - max(float(left["y"]), float(right["y"])))
    overlap_area = overlap_width * overlap_height
    smaller_area = min(float(left["width"]) * float(left["height"]), float(right["width"]) * float(right["height"]))
    return smaller_area > 0 and overlap_area / smaller_area >= MATERIAL_OVERLAP_RATIO

def _validate_required_change_targets(evidence: Mapping[str, Any], candidate: Mapping[str, Any]) -> None:
    """Reject comparator targets that contradict or newly collide with the document."""
    template = candidate.get("template") if isinstance(candidate.get("template"), dict) else {}
    proposals_by_placement: Dict[str, Dict[str, Dict[str, float]]] = {}
    changed_by_placement: Dict[str, set[str]] = {}
    justified_overlap_layers: Dict[str, set[str]] = {}
    for raw in evidence.get("required_changes") or []:
        parsed = _parse_required_change(str(raw))
        if parsed is None:
            raise ComparatorSelfConsistencyError("required change geometry is not machine-readable")
        placement = parsed["placement"]
        layout = template.get(f"{placement}Layout")
        layers = layout.get("layers") if isinstance(layout, dict) else None
        if not isinstance(layers, list):
            raise ComparatorSelfConsistencyError(f"required change names missing {placement} document")
        layer_map = {
            str(layer.get("layerId")): layer
            for layer in layers
            if isinstance(layer, dict) and layer.get("layerId")
        }
        bounds = (1080, 1350 if placement == "feed" else 1920)
        target = parsed["target"]
        if (
            target["x"] < 0
            or target["y"] < 0
            or target["width"] <= 0
            or target["height"] <= 0
            or target["x"] + target["width"] > bounds[0]
            or target["y"] + target["height"] > bounds[1]
        ):
            raise ComparatorSelfConsistencyError(f"required change target exceeds the {placement} canvas")
        proposals = proposals_by_placement.setdefault(placement, {})
        changed = changed_by_placement.setdefault(placement, set())
        overlap_layers = justified_overlap_layers.setdefault(placement, set())
        change_text = parsed["change"].lower()
        if "overlap" in change_text and "source" in change_text:
            overlap_layers.update(parsed["layer_ids"])
        for layer_id in parsed["layer_ids"]:
            layer = layer_map.get(layer_id)
            if layer is None:
                raise ComparatorSelfConsistencyError(f"required change names unknown {placement} layer {layer_id}")
            geometry = layer.get("geometry")
            if not isinstance(geometry, dict):
                raise ComparatorSelfConsistencyError(f"required change names non-geometric {placement} layer {layer_id}")
            existing = proposals.get(layer_id)
            if existing is not None and not _rect_matches(existing, target):
                raise ComparatorSelfConsistencyError(f"required changes propose conflicting targets for {placement} layer {layer_id}")
            proposals[layer_id] = dict(target)
            if not _rect_matches(geometry, target):
                changed.add(layer_id)

    for placement, proposals in proposals_by_placement.items():
        layout = template[f"{placement}Layout"]
        layers = [layer for layer in layout["layers"] if isinstance(layer, dict) and layer.get("layerId")]
        geometry_by_id = {str(layer["layerId"]): dict(layer["geometry"]) for layer in layers}
        proposed_geometry = {**geometry_by_id, **proposals}
        changed = changed_by_placement.get(placement, set())
        justified = justified_overlap_layers.get(placement, set())
        for index, left_layer in enumerate(layers):
            left_id = str(left_layer["layerId"])
            for right_layer in layers[index + 1:]:
                right_id = str(right_layer["layerId"])
                if not ({left_id, right_id} & changed):
                    continue
                # Frames, patches and cards intentionally overlay images in the
                # source designs. Only two independently replaceable media
                # slots can create a destructive collision.
                if {left_layer.get("type"), right_layer.get("type")} != {"image_slot"}:
                    continue
                if (
                    _material_overlap(proposed_geometry[left_id], proposed_geometry[right_id])
                    and not _material_overlap(geometry_by_id[left_id], geometry_by_id[right_id])
                    and not ({left_id, right_id} & justified)
                ):
                    raise ComparatorSelfConsistencyError(
                        f"required change newly overlaps {placement} layers {left_id} and {right_id}"
                    )

def _assessment(value: Any, role: str, *, require_change_list: bool = False) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise AdTemplateProcessError(f"{role} returned invalid evidence")
    rubric = value.get("rubric")
    if not isinstance(rubric, dict):
        raise AdTemplateProcessError(f"{role} must provide all rubric subscores")
    if set(rubric) != set(RUBRIC_FIELDS):
        raise AdTemplateProcessError(f"{role} rubric must contain exactly the eight required fields")
    scores = {field: _number(rubric.get(field)) for field in RUBRIC_FIELDS}
    hard_failures = value.get("hard_failures") or value.get("hard_fail") or []
    if isinstance(hard_failures, str): hard_failures = [hard_failures]
    if not isinstance(hard_failures, list): raise AdTemplateProcessError(f"{role} hard_failures must be a list")
    reason = str(value.get("reason") or value.get("mismatches") or "").strip()
    if len(reason) < 3: raise AdTemplateProcessError(f"{role} must explain its decision")
    differences = value.get("differences")
    required_changes = value.get("required_changes")
    if require_change_list:
        if not isinstance(differences, list) or not all(isinstance(item, str) and item.strip() for item in differences):
            raise AdTemplateProcessError(f"{role} must provide a concrete differences list")
        if not isinstance(required_changes, list) or not all(isinstance(item, str) and item.strip() for item in required_changes):
            raise AdTemplateProcessError(f"{role} must provide a concrete required_changes list")
        differences = [item.strip() for item in differences]
        required_changes = [item.strip() for item in required_changes]
        preliminary_score = sum(scores.values()) / len(scores)
        if (
            role == "final reviewer"
            and not required_changes
            and (
                hard_failures
                or scores["feed_source_likeness"] < THRESHOLD
                or preliminary_score < THRESHOLD
                or min(scores.values()) < MIN_RUBRIC_SCORE
            )
        ):
            raise AdTemplateProcessError(f"{role} must provide a concrete required_changes list")
        if not all(_required_change_is_actionable(item) for item in required_changes):
            raise AdTemplateProcessError(
                f"{role} required_changes must name placement, layers, current geometry, target geometry, and change"
            )
    score = round(sum(scores.values()) / len(scores), 2)
    if hard_failures: score = 0.0
    return {
        "score": score,
        "minimum_score": min(scores.values()),
        "reason": reason,
        "rubric": scores,
        "hard_failures": [str(item) for item in hard_failures],
        **({"differences": differences, "required_changes": required_changes} if require_change_list else {}),
    }

def _passes_quality_gate(evidence: Mapping[str, Any]) -> bool:
    return (
        not evidence.get("hard_failures")
        and not evidence.get("required_changes")
        and _number((evidence.get("rubric") or {}).get("feed_source_likeness")) >= THRESHOLD
        and _number(evidence.get("score")) >= THRESHOLD
        and _number(evidence.get("minimum_score")) >= MIN_RUBRIC_SCORE
    )

def validate_iterations(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > MAX_ITERATIONS: raise AdTemplateProcessError(f"iterations must contain 1 to {MAX_ITERATIONS} records")
    result, accepted, retry_after_review = [], False, False
    for index, raw in enumerate(value, 1):
        if not isinstance(raw, dict) or raw.get("iteration", index) != index: raise AdTemplateProcessError("iterations must be consecutive and one-based")
        comparison = raw.get("comparison")
        if not isinstance(comparison, dict): raise AdTemplateProcessError("each iteration requires one comparator")
        if any(key in raw or key in comparison for key in ("reviewers", "reviewer", "primary", "strict")): raise AdTemplateProcessError("final reviewers are only allowed after the comparator passes")
        evidence = _assessment(comparison, "comparator", require_change_list=True)
        score = evidence["score"]
        passes = _passes_quality_gate(evidence)
        decision = str(raw.get("decision") or ("accepted" if passes else "revise"))
        expected = "accepted" if passes else "revise"
        if decision != expected: raise AdTemplateProcessError("iteration decision does not match comparator score")
        reason = evidence["reason"]
        if accepted and not retry_after_review:
            raise AdTemplateProcessError("no iteration may follow an accepted candidate unless final reviewers requested revision")
        accepted = passes
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
        evidence = _assessment(item, "final reviewer", require_change_list=True)
        score, reason = evidence["score"], evidence["reason"]
        route = str(item.get("route") or "").strip()
        if not route: raise AdTemplateProcessError("reviewer route is required")
        normalized.append({"id": identity, "route": route, **evidence})
    if normalized[0]["id"] == normalized[1]["id"] or normalized[0]["route"] == normalized[1]["route"]: raise AdTemplateProcessError("final reviewers must use independent instances and routes")
    passed = all(_passes_quality_gate(item) for item in normalized); decision = "accepted" if passed else "revise"
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
        if placement == "story" and layer_type in {"text", "logo", "icon"}:
            geometry = layer["geometry"]
            safe = STORY_CONTENT_SAFE_ZONE
            if (
                geometry["x"] < safe["x"]
                or geometry["y"] < safe["y"]
                or geometry["x"] + geometry["width"] > safe["x"] + safe["width"]
                or geometry["y"] + geometry["height"] > safe["y"] + safe["height"]
            ):
                raise AdTemplateProcessError(
                    f"{layer_path} essential {layer_type} geometry must stay inside the Story content-safe zone "
                    "x=72..1008 and y=240..1620"
                )
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

def normalize_asset_declarations(value: Any) -> Any:
    """Repair the one mechanical asset-envelope omission models commonly make."""
    if not isinstance(value, dict) or set(value) != {"template", "assets"}:
        return value
    template = value.get("template")
    if not isinstance(template, dict):
        return value
    top_assets = value.get("assets")
    declarations = template.get("assets")
    if isinstance(top_assets, list) and top_assets and declarations is None:
        normalized = {}
        for item in top_assets:
            if not isinstance(item, dict) or set(item) != {"assetKey", "fileName", "mimeType"}:
                return value
            key = item.get("assetKey")
            if not _nonempty(key) or key in normalized:
                return value
            normalized[key] = {"fileName": item.get("fileName"), "mimeType": item.get("mimeType")}
        value = {**value, "template": {**template, "assets": normalized}}
    elif isinstance(declarations, dict) and declarations and top_assets == []:
        mirrored = []
        for key, item in declarations.items():
            if not _nonempty(key) or not isinstance(item, dict) or set(item) != {"fileName", "mimeType"}:
                return value
            mirrored.append({"assetKey": key, "fileName": item.get("fileName"), "mimeType": item.get("mimeType")})
        value = {**value, "assets": mirrored}
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
        'in imageInputs. When the source has a visible brand/logo role, preserve it with a real logo layer bound to an optional '
        'imageInput whose defaultAssetKey resolves to brand/neutral-real-estate.png, so the rendered comparison contains a visible '
        'neutral mark and the editor can replace it with a Brand Pack or customer logo. Never leave a source-visible logo region blank '
        'and never fake a logo with a baked plate. Every text layer inputKey must be declared exactly once in textInputs. '
        'imageInputs and textInputs keys must be unique across both lists. Every text-layer font file must appear exactly once in fonts. '
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

    # Source fidelity is the sole visual objective. The source bitmap is never
    # shipped, but its observable design must be reconstructed as editable
    # layers instead of being simplified into a different archetype.
    return f"""Build one layered Blockwise ad template by recreating the attached source image as closely as possible. The rendered Feed must be a near-match to the source at thumbnail and full size: preserve its layout regions, relative geometry, spacing, image-slot count and shapes, crop intent, border and divider treatment, typography scale and style, palette, information hierarchy, and visible content structure. Do not redesign, simplify, modernise, improve, or reinterpret the source. The only acceptable visual substitutions are neutral editable replacements for advertiser identity and source photography. Remove source names, logos, phone numbers, URLs, portraits, contact identity, and source pixels, but keep equivalent editable logo, text, icon, patch, and image-slot roles in the same visual positions. Never flatten the source image into a plate.

The attached source is the authority. For a revision, apply every item in prior reviewer feedback to the prior valid candidate and leave already-matching regions unchanged. Story must be a native 1080x1920 translation of the same design system and hierarchy, with essential content outside the top 240px and bottom 300px platform zones; it is not evidence that Feed may diverge from the source.

Return JSON only with exactly {{"template":{{...}},"assets":[]}}. template must use schema "blockwise.ad-template" and contain exactly templateId, createdAt, feedLayout, storyLayout, imageInputs, textInputs, semanticColours, assets, fonts, metadata plus schema. Feed is 1080x1350 with safeZones=[{{"x":72,"y":96,"width":936,"height":1158}}]. Story is 1080x1920 with safeZones=[{{"x":72,"y":240,"width":936,"height":1380}}]. Geometry is always {{x,y,width,height}} from the top-left and must remain inside the canvas. Each layout contains exactly placement, layers, and safeZones. Every layer must match one exact shape: plate={{type,layerId,colourRole,assetKey?,geometry,protected}}; image_slot={{type,layerId,inputKey,geometry,mask,minSourceWidth,minSourceHeight,defaultCrop,allowedPlacementOverrides}}; overlay_patch={{type,layerId,geometry,colourRole,opacity,assetKey?}}; text={{type,layerId,inputKey,font:{{file}},fontSize,lineHeight,tracking,alignment,maxCharacters,maxLines,colourRole,overflowBehaviour,geometry}}; logo={{type,layerId,inputKey,geometry}}; vector={{type,layerId,geometry,shape,colourRole,opacity}}; icon={{type,layerId,geometry,icon,colourRole}}. Do not omit layerId, opacity, protected, or any other required field. Text uses a declared bundled font, native-canvas fontSize, unitless lineHeight 0.8-2.5, tracking 0-4 canvas pixels, alignment left|center|right, maxCharacters, maxLines, colourRole, overflowBehaviour refuse|truncate|scale_down, and geometry. image_slot mask is rounded_rect|circle|none and defaultCrop is normalized {{"x":0,"y":0,"width":1,"height":1}}. imageInputs entries are exactly {{key,label,required?,acceptedTypes,defaultAssetKey?}}; textInputs entries are exactly {{key,label,placeholder,maxLength}}; fonts entries are exactly {{file}}. Every reference must resolve.

semanticColours contains exactly background, primary, secondary, accent, mainText, inverseText. Allowed font files are {', '.join(sorted(ALLOWED_FONT_FILES))}. metadata contains exactly title:string, description:string, gallerySamples:{{feed?:{{assetKey?,placement,purpose}},story?:{{assetKey?,placement,purpose}}}}, metaCopyDefaults:{{primaryText:string[],headlines:string[],descriptions:string[],cta:string}}, aiWritingGuidance:{{summary:string,fields:record<string,string>}}, publishRequirements:{{objective:string,specialAdCategory:string|null,instantForm:{{required:boolean,dependency:string|null,defaults?:record<string,string>}},destination:{{required:boolean,kind:'website'|'instant_form'|'none',dependency:string|null}},requiredCtaTypes?:string[]}}, replacementAssets:{{inputKey,assetKey,purpose?}}[], realAssetRefs:{{inputKey,kind,required}}[]. For property ads set specialAdCategory to HOUSING. Every layer, input, font, colour, asset, replacement, gallery, and realAsset reference must resolve inside the same template. template.assets is required and is a declaration record shaped exactly {{"asset-key":{{"fileName":"normalized/catalog/path.webp","mimeType":"image/webp"}}}}; it is never a list and may be {{}} only when no asset is used. Top-level assets is always a list of {{assetKey,fileName,mimeType}} and must exactly mirror template.assets; never emit bytesBase64 anywhere. Allowed catalog files are brand/neutral-real-estate.png, home/open-home-living.webp, home/home-dusk.webp, home/mt-lawley-federation.webp, home/home-pool.webp, home/interior-styled.webp, home/subiaco-townhouse.webp, and adstudio-samples/photos/int-bedroom.png. Use one coherent property across slots. When the source contains a logo, declare the neutral brand asset and bind it as the logo imageInput defaultAssetKey so the logo is visible in every rendered iteration.

Hermes will render the candidate, attach the source and render to a vision comparator, and feed its complete comparator evidence into the next revision. Prior reviewer feedback contains the rubric, minimum_score, hard_failures, differences, required_changes, and reason; apply every required change exactly and preserve already-correct regions. Do not self-score. Run {run_id}; project {project_id}; placements {json.dumps(placements)}; brief {brief[:4000]}; prior reviewer feedback {feedback}.{repair_clause}"""

    return f"""Run the sole ad-template process as the builder agent. Inspect the attached source pixels, remove advertiser identity (names, logos, phones, URLs, portraits), and return JSON with exactly {{template, assets}}. Treat the source as structural inspiration, not a quality ceiling: explicitly avoid inheriting brochure density, tiny copy, weak hierarchy, duplicated contact details, incoherent photography, or a Feed layout stretched into Story. Build a conversion-focused Meta ad around one dominant idea: a strong hook, one coherent hero treatment, only essential proof or facts, and one clear CTA. Dense descriptions and contact lists belong in Meta primary text or the destination, not inside the image. At native pixels use type large enough to remain legible inside a 500px, 390px and 320px-wide Meta shell; do not rely on scale-down or truncation to rescue excess copy. Feed must use a deliberate 72px horizontal and 96px vertical protected content margin. Story must be independently composed with its top 240px and bottom 300px protected from platform UI. Use true editable text, image, logo, CTA, patch and icon roles. Use one coherent property/photo subject across default slots; never mix unrelated properties. For a property listing, publishRequirements.specialAdCategory must be HOUSING. template must contain exactly schema='blockwise.ad-template', templateId, createdAt (ISO datetime), feedLayout, storyLayout, imageInputs, textInputs, semanticColours, assets, fonts, metadata. semanticColours must contain exactly background, primary, secondary, accent, mainText, inverseText; every layer colourRole is one of those six. Each layout contains exactly placement, layers, safeZones; Feed is placement feed within 1080x1350 and Story is placement story within 1080x1920, including safeZones. Geometry and every safe-zone rectangle contain exactly {{x,y,width,height}}: x,y are the top-left coordinates from canvas origin (0,0); width,height are positive sizes, not right/bottom coordinates; x + width must stay within canvas width and y + height within canvas height. Exact safe areas are Feed safeZones=[{{"x":72,"y":96,"width":936,"height":1158}}] within 1080x1350 and Story safeZones=[{{"x":72,"y":240,"width":936,"height":1380}}] within 1080x1920. Layer shapes are exact: plate={{type,layerId,colourRole,assetKey?,geometry,protected}}; image_slot={{type,layerId,inputKey,geometry,mask,minSourceWidth,minSourceHeight,defaultCrop,allowedPlacementOverrides}}; overlay_patch={{type,layerId,geometry,colourRole,opacity,assetKey?}}; text={{type,layerId,inputKey,font:{{file}},fontSize,lineHeight,tracking,alignment,maxCharacters,maxLines,colourRole,overflowBehaviour,geometry}}; logo={{type,layerId,inputKey,geometry}}; vector={{type,layerId,geometry,shape,colourRole,opacity}}; icon={{type,layerId,geometry,icon,colourRole}}. imageInputs are {{key,label,required?,acceptedTypes,defaultAssetKey?}}; textInputs are {{key,label,placeholder,maxLength}}. fonts are {{file}} and every text-layer font must be declared; allowed bundled font files are {', '.join(sorted(ALLOWED_FONT_FILES))}. metadata is exact: title:string; description:string; gallerySamples={{feed?:{{assetKey?,placement,purpose}},story?:{{assetKey?,placement,purpose}}}}; metaCopyDefaults={{primaryText:string[],headlines:string[],descriptions:string[],cta:string}}; aiWritingGuidance={{summary:string,fields:record<string,string>}}; publishRequirements={{objective:string,specialAdCategory:string|null,instantForm:{{required:boolean,dependency:string|null,defaults?:record<string,string>}},destination:{{required:boolean,kind:'website'|'instant_form'|'none',dependency:string|null}},requiredCtaTypes?:string[]}}; replacementAssets={{inputKey,assetKey,purpose?}}[]; realAssetRefs={{inputKey,kind,required}}[]. Every layer/input/font/colour/asset metadata reference must resolve inside the same template. Declare source-free replacement assets in template.assets as assetKey -> {{fileName,mimeType}} and in top-level assets only as {{assetKey,fileName,mimeType}} with an exact match. Hermes resolves bytes from the fixed safe catalog; never emit bytesBase64 anywhere and never guess filenames. Allowed normalized relative catalog paths are home/open-home-living.webp, home/home-dusk.webp, home/mt-lawley-federation.webp, home/home-pool.webp, home/interior-styled.webp, home/subiaco-townhouse.webp, and adstudio-samples/photos/int-bedroom.png. Never include the source composite, source_path, hashes, signatures, private fields, or a full-source plate. Keep geometry inside canvas and Story native. Hermes renders, compares once per iteration, then runs two fresh final reviewers only after the comparator clears both the {THRESHOLD} mean and {MIN_RUBRIC_SCORE} subscore floor; never self-score or invent review evidence. Run {run_id}; project {project_id}; fixed placements {json.dumps(placements)}; brief {brief[:4000]}; prior reviewer feedback {feedback[:3000]}.{repair_clause}"""

def review_prompt(*, final: bool, candidate: Any = None) -> str:
    role = "fresh independent final reviewer" if final else "iteration comparator"
    candidate_context = _safe_candidate_prompt_json(candidate)
    return f"""You are the {role} in a source-matching loop. The attached images are ordered: (1) the source reference, (2) the rendered Feed candidate, and (3) the rendered Story translation. Inspect the actual pixels together. The primary objective is not generic ad quality; it is faithful reconstruction of the source design as editable layers.

Compare source versus Feed region by region: outer frame and margins; header/title block; image count, grid, aspect ratios and crop intent; logo/price panel; divider lines; body columns; feature list; footer/contact row; typography family/style/weight/scale/line wrapping; palette; alignment; whitespace; borders, corner radii, icons and decorative details. Neutral replacement photography and neutral advertiser identity must not reduce the score when their visual role, size, crop, and position match. Treat source and replacement photographs as content-agnostic slot fill: never mention, score, or request changes for the neutral photo subject, scene, uniqueness, repetition, or pixels. Compare only slot count, geometry, mask, crop intent, and layering because source pixels are prohibited from shipping. Any simplification, omitted section, changed information density, different skeleton, moved panel, different image-slot structure, or generic redesign must reduce the score heavily. Story is scored only as a deliberate native 1080x1920 translation of the same visual system; it cannot compensate for a Feed mismatch. Assess Story dead space only inside the content-safe band y=240..1620. Empty space in y=0..239 and y=1620..1919 is mandatory platform-UI protection and must never be reported or scored as dead space, a spacing defect, a difference, or a required change.

Use this scale strictly: 10.0 means the Feed is visually indistinguishable in structure at thumbnail size except permitted neutral content substitutions; 9.5 means only tiny finishing differences remain; 9.0 still has clearly visible spacing, scale, typography, or geometry differences; 8.0 is recognisably based on the source but materially different; 5.0 is merely the same category; 0 is missing or invalid. Do not award 9.5 to a design that a person can immediately distinguish from the source.

Return JSON only with exactly reason, differences, required_changes, hard_failures, and rubric. differences is a list of concrete visible source-versus-render discrepancies, naming region and measurements or relative movement where possible. required_changes is the ordered list the builder must apply next; include only material work still needed to reach 9.5 and return [] only when the candidate genuinely clears the gate. Every required_changes string must use this exact actionable structure: placement=feed|story; layers=comma-separated layerIds; current={{x:...,y:...,width:...,height:...}}; target={{x:...,y:...,width:...,height:...}}; change=specific instruction. Name all affected layer IDs and use their current and intended target geometry. Copy current geometry exactly from Candidate contract JSON, then check every proposed target rectangle against every existing layer before returning it. Never propose a target that newly overlaps an image slot or opaque vector panel with another image slot unless that overlap is visibly present in the source and the change text explicitly identifies and justifies the source-visible overlap. Use a separate required_changes item when affected layers do not share identical current and target geometry. hard_failures is a list. rubric contains exactly these eight 0-10 numbers: feed_source_likeness, layout_geometry, spacing_proportions, typography_likeness, colour_likeness, image_slot_composition, editable_decomposition, native_story_translation. A passing candidate requires feed_source_likeness >= {THRESHOLD}, mean >= {THRESHOLD}, every field >= {MIN_RUBRIC_SCORE}, no hard failures, and no required changes.

Hard-fail source pixels flattened into the output, copied advertiser identity, missing/unreadable renders, clipping, canvas/safe-zone violations, non-editable critical roles, unknown assets, or a stretched/cropped/letterboxed Story. Do not infer another reviewer's score. Candidate contract JSON: {candidate_context}"""

    role = "fresh independent final reviewer" if final else "iteration comparator"
    role += (
        ". Treat the source only as a structural reference whose defects must not be inherited. Mandatory removal of source advertiser identities, "
        "logos, names, phones, URLs, portraits, contact details, source photography, and all source pixels must not be penalized. "
        "Instead, score whether its useful archetype has been elevated into a persuasive, legible Meta ad with source-free photography and "
        "neutral editable logo, image, copy, icon, patch, and CTA layers. Do not reward literal reproduction of brochure density, tiny type, "
        "duplicated contact details, weak hierarchy, or unrelated default photography. A neutral editable replacement is correct; copied "
        "source identity or pixels, or a missing editable role, is not"
    )
    candidate_context = _safe_candidate_prompt_json(candidate)
    return f"""Act as the {role}. Compare the attached source image with the attached rendered Feed and native Story previews. Evaluate the renders as they will appear inside real Meta shells at 500px, 390px and 320px wide, not only at full-resolution zoom. Return JSON only with exactly reason, hard_failures, and rubric. rubric must contain exactly these eight numeric 0-10 fields: source_inspired_direction (retains useful structural intent while improving source defects), ad_effectiveness (one clear hook, persuasive hierarchy, essential proof and CTA without brochure clutter), hierarchy_typography (strong type hierarchy, intentional wrapping and delivered-screen readability), real_shell_legibility (all essential content remains clear in Meta Feed and Story shells at the stated sizes), colour_art_direction (cohesive palette, contrast, photographic tone and finish), editable_decomposition (the supplied candidate JSON has genuine editable text, logo, CTA, patches, icons and media roles), replacement_robustness (slot geometry, masks, logo treatment and copy bounds plausibly survive realistic replacement content), native_story (a deliberate 1080x1920 composition, not a stretched, cropped, padded or letterboxed Feed). Explain concrete visible mismatches in reason. hard_failures must be a JSON list. Add a hard failure if any source advertiser name, logo, phone, URL, portrait, contact identity, or source composite pixel is reused; if Feed or Story is missing, clipped, distorted, unreadable at delivered size, outside its canvas or protected zones; if the design is a dense brochure rather than a Meta ad; if default photographs visibly represent unrelated subjects; if critical text, logo, CTA, patch, icon or media is flattened/non-editable; if Story is a Feed crop, stretch, letterbox, or has essential content in the top 240px or bottom 300px platform UI zones; or if an asset is missing/unknown. Any hard failure makes the score zero. Do not reuse or infer another reviewer's score. Passing requires a mean of at least {THRESHOLD}, every subscore at least {MIN_RUBRIC_SCORE}, and no hard failures. Candidate contract JSON: {candidate_context}"""

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

    def run(self, *, source: str, brief: str, placements: Any, routes: List[Dict[str, str]], review_round: int = 0, total_iterations: int = 0, feedback: str = "", history: List[Dict[str, Any]] | None = None, revision_candidate: Mapping[str, Any] | None = None, selected_builder_route: Mapping[str, str] | None = None, builder_escalated: bool = False, previous_score: float | None = None, low_gain_streak: int = 0, require_quality_route: bool = False, resume_final_check: bool = False) -> Dict[str, Any]:
        if len(routes) < 4: raise AdTemplateProcessError("builder, comparator, and two final reviewers require four configured roles")
        quality_route = routes[4] if len(routes) > 4 else None
        if require_quality_route and quality_route is None:
            raise AdTemplateProcessError("automatic builder quality escalation requires a configured quality route")
        builder_route = dict(selected_builder_route or (quality_route if builder_escalated and quality_route else routes[0]))
        for label, route in (("builder", builder_route), ("quality", quality_route)):
            if route is None:
                continue
            if not str(route.get("provider") or "").strip() or not str(route.get("model") or "").strip():
                raise AdTemplateProcessError(f"{label} builder route is invalid")
        self._check_stop()
        history = list(history or [])
        iterations = []
        candidate: Dict[str, Any] = dict(revision_candidate or {}) if resume_final_check else {}
        working_candidate: Mapping[str, Any] | None = revision_candidate
        if resume_final_check:
            validated_history = validate_iterations(history)
            if not candidate or validated_history[-1]["decision"] != "accepted":
                raise AdTemplateProcessError("final-check resume requires one accepted candidate checkpoint")
            history = validated_history
        # This is an iterative editing loop, not a best-of sampler. Every
        # revision must start from the immediately preceding rendered document
        # and consume that document's own comparison. A high-scoring older
        # document may be retained in the trace, but must never replace the
        # working document or feedback for the next turn.

        def builder_route_identity(route: Mapping[str, str]) -> str:
            return f"{route.get('provider')}/{route.get('model')}"
        iteration_offsets = () if resume_final_check else range(MAX_ITERATIONS - total_iterations)
        for offset in iteration_offsets:
            index = total_iterations + offset + 1
            iteration_prior = working_candidate
            iteration_workspace = self.workspace / "iterations" / f"{index:02d}"
            self._check_stop()
            self.emit("stage.started", "build", {"iteration": index, "role": "builder"})
            validation_feedback = ""
            rejected_candidate: Any = None
            structured_output_failures = 0
            output_retry_pending = False
            for repair_attempt in range(MAX_SCHEMA_REPAIRS_PER_ITERATION + 1):
                self._check_stop()
                while True:
                    if output_retry_pending:
                        instance = "builder-%d-output-retry-%d" % (index, structured_output_failures)
                    else:
                        instance = "builder-%d" % index if repair_attempt == 0 else "builder-%d-repair-%d" % (index, repair_attempt)
                    try:
                        candidate = self.call_agent(
                            instance,
                            vision_message(generator_prompt(
                                run_id=self.run_id, project_id=self.project_id, brief=brief,
                                placements=placements, source=source, feedback=feedback,
                                validation_feedback=validation_feedback,
                                repair_attempt=max(repair_attempt, structured_output_failures),
                                prior_candidate=iteration_prior if repair_attempt == 0 else None,
                                rejected_candidate=rejected_candidate if repair_attempt > 0 else None,
                            ), [source]),
                            builder_route_identity(builder_route),
                        )
                        output_retry_pending = False
                        break
                    except AdTemplateStructuredOutputError:
                        self._check_stop()
                        structured_output_failures += 1
                        output_retry_pending = True
                        validation_feedback = (
                            "builder returned no complete JSON object with exactly template and assets"
                        )
                        if structured_output_failures <= MAX_CHEAP_STRUCTURED_OUTPUT_RETRIES:
                            self.emit("builder.output-retry", "build", {
                                "iteration": index,
                                "attempt": structured_output_failures + 1,
                                "provider": builder_route["provider"],
                                "model": builder_route["model"],
                                "reason": "structured_output_invalid",
                            })
                            continue
                        if quality_route is not None and builder_route_identity(builder_route) != builder_route_identity(quality_route):
                            prior_route = builder_route
                            builder_route = dict(quality_route)
                            builder_escalated = True
                            self.emit("builder.escalated", "build", {
                                "iteration": index,
                                "from_provider": prior_route["provider"],
                                "from_model": prior_route["model"],
                                "to_provider": builder_route["provider"],
                                "to_model": builder_route["model"],
                                "reason": "structured_output_invalid",
                            })
                            continue
                        raise AdTemplateProcessError(
                            "builder returned invalid structured output after bounded retry and quality escalation"
                        ) from None
                self._check_stop()
                try:
                    candidate = normalize_asset_declarations(candidate)
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
                    if (
                        repair_attempt >= MAX_CHEAP_STRUCTURED_OUTPUT_RETRIES
                        and quality_route is not None
                        and builder_route_identity(builder_route) != builder_route_identity(quality_route)
                    ):
                        prior_route = builder_route
                        builder_route = dict(quality_route)
                        builder_escalated = True
                        self.emit("builder.escalated", "build", {
                            "iteration": index,
                            "from_provider": prior_route["provider"],
                            "from_model": prior_route["model"],
                            "to_provider": builder_route["provider"],
                            "to_model": builder_route["model"],
                            "reason": "candidate_contract_invalid",
                        })
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
            comparison_prompt = review_prompt(final=False, candidate=candidate)
            comparison_rejection = ""
            for comparison_attempt in range(MAX_COMPARATOR_SELF_CONSISTENCY_RETRIES + 1):
                retry_suffix = ""
                if comparison_rejection:
                    retry_suffix = (
                        "\n\nYour previous response was rejected by the strict comparison schema: "
                        f"{comparison_rejection}. Reinspect the candidate JSON and pixels, then return the complete "
                        "corrected JSON object. When the quality gate is not passed, required_changes MUST be a "
                        "non-empty array. Every required_changes item MUST be one string containing placement=Feed "
                        "or Story; layers=<real layer ids>; current={x,y,width,height}; target={x,y,width,height}; "
                        "change=<concrete instruction>. Current geometry must match the current layered document. "
                        "Target geometry must remain on-canvas and must not introduce a new opaque overlap unless "
                        "that overlap is visible in the source and the change explicitly says source overlap. "
                        "Return only the corrected JSON object."
                    )
                try:
                    comparison = self.call_agent(
                        "comparator-%d" % index if comparison_attempt == 0 else f"comparator-{index}-retry-{comparison_attempt}",
                        vision_message(
                            comparison_prompt + retry_suffix,
                            [
                                source,
                                str((rendered.get("render") or {}).get("feed") or ""),
                                str((rendered.get("render") or {}).get("story") or ""),
                            ],
                        ),
                        f"{routes[1].get('provider')}/{routes[1].get('model')}",
                    )
                    self._check_stop()
                    evidence = _assessment(comparison, "comparator", require_change_list=True)
                    _validate_required_change_targets(evidence, candidate)
                except (AdTemplateProcessError, AdTemplateStructuredOutputError, ComparatorSelfConsistencyError) as exc:
                    comparison_rejection = str(exc)
                    if comparison_attempt >= MAX_COMPARATOR_SELF_CONSISTENCY_RETRIES:
                        raise
                    self.emit(
                        "comparator.retried",
                        "compare",
                        {
                            "iteration": index,
                            "attempt": comparison_attempt + 1,
                            "reason": comparison_rejection,
                        },
                    )
                    continue
                break
            score, reason = evidence["score"], evidence["reason"]
            decision = "accepted" if _passes_quality_gate(evidence) else "revise"
            record = {
                "iteration": index,
                "candidate": _candidate_trace_projection(candidate),
                "comparison": evidence,
                "decision": decision,
                "builder_route": {"provider": builder_route["provider"], "model": builder_route["model"]},
                "builder_escalated": builder_escalated,
            }
            iterations.append(record)
            self.emit("iteration.compared", "compare", {
                "iteration": index,
                "score": score,
                "minimum_score": evidence["minimum_score"],
                "reason": reason,
                "rubric": evidence["rubric"],
                "hard_failures": evidence["hard_failures"],
                "differences": evidence["differences"],
                "required_changes": evidence["required_changes"],
                "decision": decision,
                "preview_names": [str(x.get("name")) for x in candidate.get("previews", []) if isinstance(x, dict)],
            })
            current_feedback = json.dumps({
                "rubric": evidence["rubric"],
                "minimum_score": evidence["minimum_score"],
                "hard_failures": evidence["hard_failures"],
                "differences": evidence["differences"],
                "required_changes": evidence["required_changes"],
                "reason": reason,
            }, ensure_ascii=False)
            working_candidate = candidate
            if _passes_quality_gate(evidence):
                previous_score = score
                break

            escalation_reason = ""
            if not builder_escalated and quality_route is not None and previous_score is not None:
                gain = score - previous_score
                if score < previous_score:
                    escalation_reason = "regression"
                elif gain < MIN_MATERIAL_SCORE_GAIN:
                    low_gain_streak += 1
                    if low_gain_streak >= 2:
                        escalation_reason = "insufficient_improvement"
                else:
                    low_gain_streak = 0
            if escalation_reason:
                prior_route = builder_route
                builder_route = dict(quality_route or {})
                builder_escalated = True
                self.emit("builder.escalated", "build", {
                    "iteration": index,
                    "from_provider": prior_route["provider"], "from_model": prior_route["model"],
                    "to_provider": builder_route["provider"], "to_model": builder_route["model"],
                    "reason": escalation_reason, "previous_score": previous_score, "score": score,
                })
            previous_score = score
            feedback = current_feedback
        accepted_records = history if resume_final_check else iterations
        if not accepted_records or accepted_records[-1]["decision"] != "accepted": raise AdTemplateProcessError(f"quality loop exhausted {MAX_ITERATIONS} iterations without a final-review-ready candidate")
        reviewers = []
        for n, route in enumerate(routes[2:4], 1):
            identity = f"final-reviewer-{self.run_id}-{n}-{uuid.uuid4().hex[:8]}"
            provider_route = f"{route.get('provider')}/{route.get('model')}"
            route_identity = provider_route
            self.emit("final-review.started", "final-check", {"reviewer": identity, "route": route_identity})
            final_prompt = review_prompt(final=True, candidate=candidate)
            rejection = ""
            for output_attempt in range(MAX_FINAL_REVIEW_OUTPUT_RETRIES + 1):
                retry_suffix = ""
                if rejection:
                    retry_suffix = (
                        "\n\nYour previous final-review response was rejected by the strict evidence schema: "
                        f"{rejection}. Return the complete corrected JSON object. required_changes MUST be a "
                        "non-empty array when hard_failures is non-empty, feed_source_likeness < 9.5, the rubric "
                        "mean < 9.5, or any rubric score < 9.3. Every required_changes item MUST contain the exact "
                        "keys placement, layers, current, target, and change; placement must be Feed or Story; "
                        "layers must name at least one real layer id; and current, target, and change must each be "
                        "a concrete non-empty string. Do not lower the score to avoid this requirement. Return only "
                        "the corrected JSON object."
                    )
                attempt_identity = identity if output_attempt == 0 else f"{identity}-retry-{output_attempt}"
                try:
                    self._check_stop()
                    review = self.call_agent(
                        attempt_identity,
                        vision_message(
                            final_prompt + retry_suffix,
                            [source, str((candidate.get("render") or {}).get("feed") or ""), str((candidate.get("render") or {}).get("story") or "")],
                        ),
                        provider_route,
                    )
                    self._check_stop()
                    evidence = _assessment(review, "final reviewer", require_change_list=True)
                except (AdTemplateProcessError, AdTemplateStructuredOutputError) as exc:
                    rejection = str(exc)
                    if output_attempt >= MAX_FINAL_REVIEW_OUTPUT_RETRIES:
                        raise
                    self.emit("final-review.retried", "final-check", {
                        "reviewer": identity,
                        "route": route_identity,
                        "attempt": output_attempt + 1,
                        "reason": rejection,
                    })
                    continue
                reviewers.append({"id": attempt_identity, "route": route_identity, **evidence})
                break
        final_review = validate_final_review({"reviewers": reviewers}, accepted=True)
        if final_review["decision"] != "accepted":
            self.emit("final-review.completed", "final-check", {"decision": "revise", "reviewers": final_review["reviewers"]})
            if review_round >= MAX_FINAL_REVIEW_ROUNDS or total_iterations + len(iterations) >= MAX_ITERATIONS:
                raise AdTemplateProcessError("final reviewers failed after the bounded automatic revision loop")
            accepted_records[-1]["final_review_failed"] = True
            reasons = json.dumps([
                {
                    "reviewer": item["id"],
                    "source_match_score": item["rubric"]["feed_source_likeness"],
                    "differences": item["differences"],
                    "required_changes": item["required_changes"],
                    "reason": item["reason"],
                }
                for item in final_review["reviewers"]
            ], ensure_ascii=False)
            return self.run(
                source=source, brief=brief, placements=placements, routes=routes,
                review_round=review_round + 1, total_iterations=len(history + iterations),
                feedback=reasons, history=history + iterations, revision_candidate=candidate,
                selected_builder_route=builder_route, builder_escalated=builder_escalated,
                previous_score=score, low_gain_streak=low_gain_streak,
                require_quality_route=require_quality_route,
            )
        self.emit("final-review.completed", "final-check", {"decision": "accepted", "reviewers": final_review["reviewers"]})
        generated = candidate
        documents = deterministic_documents(generated.get("template"))
        validate_artifacts(generated, self.workspace)
        self._check_stop()
        imported = import_template({**generated, "documents": documents}, run_id=self.run_id, project_id=self.project_id)
        self._check_stop()
        self.emit("template.imported", "live", imported)
        return {"template": generated.get("template") or candidate.get("template"), "iterations": history + iterations, "final_review": final_review, "previews": generated.get("previews"), "documents": documents, "template_path": generated.get("template_path"), "render_path": generated.get("render_path") or generated.get("render", {}).get("feed"), "import": imported, "process": "only-ad-template-process", "builder_escalated": builder_escalated, "builder_route": {"provider": builder_route["provider"], "model": builder_route["model"]}}
