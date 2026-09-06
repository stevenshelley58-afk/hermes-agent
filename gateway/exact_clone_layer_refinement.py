"""Bounded issue-group refinement for the exact-clone controller."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image

from gateway.ad_template_runtime import AdTemplateProcessError

MAX_GROUP_LAYERS = 8
MAX_GROUPS_PER_CALL = 4
MAX_OPERATIONS = 32
MAX_PATCH_BYTES = 16_000
_CANVAS = {"feed": (1080, 1350), "story": (1080, 1920)}
_FIELD_PATHS = {
    "x": "geometry/x",
    "y": "geometry/y",
    "width": "geometry/width",
    "height": "geometry/height",
    "font": "font/file",
    "fontsize": "fontSize",
    "fontfamily": "fontFamily",
    "fontweight": "fontWeight",
    "lineheight": "lineHeight",
    "tracking": "tracking",
    "opacity": "opacity",
    "cornerradius": "cornerRadius",
    "rotationdegrees": "rotationDegrees",
    "crop": "defaultCrop",
    "maxlines": "maxLines",
    "maxcharacters": "maxCharacters",
}

def _candidate_layers(candidate: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    template = candidate.get("template")
    if not isinstance(template, Mapping):
        raise AdTemplateProcessError("layer refinement candidate has no template")
    result: dict[str, dict[str, Any]] = {}
    for placement, layout_key in (("feed", "feedLayout"), ("story", "storyLayout")):
        layout = template.get(layout_key)
        layers = layout.get("layers") if isinstance(layout, Mapping) else None
        if not isinstance(layers, list):
            continue
        for index, layer in enumerate(layers):
            layer_id = layer.get("layerId") if isinstance(layer, Mapping) else None
            if not isinstance(layer_id, str) or not layer_id:
                continue
            result[layer_id] = {
                "placement": placement,
                "pointer": f"/template/{layout_key}/layers/{index}",
                "layer": copy.deepcopy(layer),
            }
    return result


def _explicit_properties(
    issue: Mapping[str, Any],
    numeric_targets: Mapping[str, float],
) -> set[str]:
    instruction = str(issue.get("instruction") or "")
    properties = set(numeric_targets)
    if re.search(r"/fonts/[^\s,]+\.woff2", instruction, re.IGNORECASE):
        properties.add("font/file")
    alignment = re.search(
        r"\balign(?:ment)?\b\s*(?:to|=|at|:)\s*(left|center|right)\b",
        instruction,
        re.IGNORECASE,
    )
    if alignment:
        properties.add("alignment")
    explicit_objects = {
        "defaultCrop": r"\bcrop\b",
        "stroke": r"\bstroke\b",
        "shadow": r"\bshadow\b",
        "mask": r"\bmask\b",
        "effects": r"\beffects?\b",
        "fill": r"\bfill\b",
        "blendMode": r"\bblendMode\b",
    }
    for property_path, pattern in explicit_objects.items():
        if re.search(pattern, instruction, re.IGNORECASE):
            properties.add(property_path)
    return properties

def _numeric_targets(
    issue: Mapping[str, Any],
    layer: Mapping[str, Any],
    layer_id: str,
) -> dict[str, float]:
    instruction = str(issue.get("instruction") or "")
    mentions = sorted(
        (match.start(), match.end(), candidate_id)
        for candidate_id in issue.get("layerIds", [])
        for match in [re.search(
            rf"(?<![A-Za-z0-9_-]){re.escape(candidate_id)}(?![A-Za-z0-9_-])",
            instruction,
        )]
        if match is not None
    )
    own = next(
        (index for index, item in enumerate(mentions) if item[2] == layer_id),
        None,
    )
    if own is not None:
        start = mentions[own][1]
        end = mentions[own + 1][0] if own + 1 < len(mentions) else len(instruction)
        instruction = instruction[start:end]
    targets: dict[str, float] = {}
    fields = "x|y|width|height|fontSize|lineHeight|tracking|opacity|cornerRadius|rotationDegrees|maxLines|maxCharacters"
    direct = re.compile(
        rf"\b({fields})\b\s*(?:to|=|at|:)\s*(-?\d+(?:\.\d+)?)",
        flags=re.IGNORECASE,
    )
    for match in direct.finditer(instruction):
        token = match.group(1).lower()
        property_path = _FIELD_PATHS.get(token)
        if property_path:
            targets[property_path] = float(match.group(2))
    delta = re.compile(
        rf"\b({fields})\b[^.;]{{0,24}}?([+-]\d+(?:\.\d+)?)\s*(?:px)?\s*delta",
        flags=re.IGNORECASE,
    )
    for match in delta.finditer(instruction):
        token = match.group(1).lower()
        property_path = _FIELD_PATHS.get(token)
        if not property_path or property_path in targets:
            continue
        current: Any = layer
        for part in property_path.split("/"):
            current = current.get(part) if isinstance(current, Mapping) else None
        if isinstance(current, (int, float)) and not isinstance(current, bool):
            targets[property_path] = float(current) + float(match.group(2))
    return targets


def build_refinement_contract(
    candidate: Mapping[str, Any],
    issues: Sequence[Mapping[str, Any]],
    *,
    source_placement: str,
    available_fonts: Sequence[str],
    suggested_patch: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not issues:
        raise AdTemplateProcessError("layer refinement requires review issues")
    layers = _candidate_layers(candidate)
    # Comparator validation already rejects unknown layer IDs with a bounded
    # retry; this net drops any residual hallucinated references so one bad
    # issue cannot discard the whole comparison.
    actionable_issues = []
    for issue in issues:
        known_ids = [
            layer_id for layer_id in issue.get("layerIds", [])
            if layer_id in layers
        ]
        if known_ids:
            if len(known_ids) != len(issue.get("layerIds", [])):
                issue = {**copy.deepcopy(dict(issue)), "layerIds": known_ids}
            actionable_issues.append(issue)
    if not actionable_issues:
        raise AdTemplateProcessError(
            "review issues reference unknown layer IDs: "
            + ", ".join(sorted({
                layer_id for issue in issues for layer_id in issue.get("layerIds", [])
                if layer_id not in layers
            }))
        )
    issues = actionable_issues

    preferred = [
        issue for issue in issues
        if issue.get("placement") in {source_placement, "both"}
    ]
    seed = preferred[0] if preferred else issues[0]
    group_placement = (
        source_placement if seed.get("placement") == "both"
        else str(seed.get("placement"))
    )
    selected_issues: list[Mapping[str, Any]] = [seed]
    selected_ids = list(dict.fromkeys(seed["layerIds"]))
    if len(selected_ids) > MAX_GROUP_LAYERS:
        raise AdTemplateProcessError("review issue exceeds the bounded layer group")
    changed = True
    while changed:
        changed = False
        selected_input_keys = {
            layers[layer_id]["layer"].get("inputKey")
            for layer_id in selected_ids
            if isinstance(layers[layer_id]["layer"].get("inputKey"), str)
        }
        for issue in issues:
            if issue in selected_issues:
                continue
            issue_ids = list(issue["layerIds"])
            issue_input_keys = {
                layers[layer_id]["layer"].get("inputKey")
                for layer_id in issue_ids
                if isinstance(layers[layer_id]["layer"].get("inputKey"), str)
            }
            if not (set(issue_ids) & set(selected_ids) or issue_input_keys & selected_input_keys):
                continue
            proposed = list(dict.fromkeys([*selected_ids, *issue_ids]))
            if len(proposed) > MAX_GROUP_LAYERS:
                continue
            selected_ids = proposed
            selected_issues.append(issue)
            changed = True

    locks: dict[str, list[str]] = {}
    targets: dict[str, dict[str, float]] = {}
    for layer_id in selected_ids:
        related = [
            issue for issue in selected_issues if layer_id in issue["layerIds"]
        ]
        target_values: dict[str, float] = {}
        properties: set[str] = set()
        for issue in related:
            issue_targets = _numeric_targets(
                issue, layers[layer_id]["layer"], layer_id
            )
            target_values.update(issue_targets)
            properties.update(_explicit_properties(issue, issue_targets))
        locks[layer_id] = sorted(properties)
        targets[layer_id] = target_values

    template = candidate["template"]
    text_inputs = template.get("textInputs") or []
    dependencies: list[dict[str, Any]] = []
    for layer_id in selected_ids:
        input_key = layers[layer_id]["layer"].get("inputKey")
        if not isinstance(input_key, str):
            continue
        related_instructions = " ".join(
            str(issue.get("instruction") or "")
            for issue in selected_issues
            if layer_id in issue["layerIds"]
        )
        if not re.search(
            r"\b(?:text|copy|lines?|bullets?|placeholder|maxLength|editable)\b",
            related_instructions,
            re.IGNORECASE,
        ):
            continue
        input_index = next(
            (
                index for index, item in enumerate(text_inputs)
                if isinstance(item, Mapping) and item.get("key") == input_key
            ),
            None,
        )
        if input_index is None:
            continue
        shared_layers = sorted(
            other_id
            for other_id, item in layers.items()
            if item["layer"].get("inputKey") == input_key
        )
        dependency = {
            "inputKey": input_key,
            "pointer": f"/template/textInputs/{input_index}",
            "allowedProperties": ["placeholder", "maxLength"],
            "sharedLayerIds": shared_layers,
        }
        if dependency not in dependencies:
            dependencies.append(dependency)

    extra_allowed_paths: list[str] = []
    suggested_operations: list[dict[str, Any]] = []
    raw_operations = (
        suggested_patch.get("operations", [])
        if isinstance(suggested_patch, Mapping)
        else []
    )
    selected_pointers = {
        layer_id: layers[layer_id]["pointer"] for layer_id in selected_ids
    }
    dependency_pointers = {
        item["pointer"]: item for item in dependencies
    }
    for operation in raw_operations:
        if not isinstance(operation, Mapping) or not isinstance(operation.get("path"), str):
            continue
        operation_path = operation["path"]
        selected_layer_id = next((
            layer_id for layer_id, pointer in selected_pointers.items()
            if operation_path.startswith(pointer + "/")
        ), None)
        if selected_layer_id is not None:
            property_path = operation_path[len(selected_pointers[selected_layer_id]) + 1:]
            if (
                not property_path
                or property_path.split("/", 1)[0]
                in {"layerId", "type", "inputKey", "protected"}
            ):
                raise AdTemplateProcessError(
                    "comparator proposed an unsafe layer identity change"
                )
            locks[selected_layer_id] = sorted(
                set(locks[selected_layer_id]) | {property_path}
            )
            suggested_operations.append(copy.deepcopy(dict(operation)))
            continue
        dependency = next((
            item for pointer, item in dependency_pointers.items()
            if operation_path.startswith(pointer + "/")
        ), None)
        if dependency is not None:
            property_name = operation_path.rsplit("/", 1)[-1]
            if property_name not in dependency["allowedProperties"]:
                raise AdTemplateProcessError(
                    "comparator proposed an unsafe text input dependency change"
                )
            suggested_operations.append(copy.deepcopy(dict(operation)))
            continue
        if operation_path.startswith("/template/semanticColours/"):
            role = operation_path.rsplit("/", 1)[-1]
            consumers = {
                layer_id for layer_id, item in layers.items()
                if item["layer"].get("colourRole") == role
            }
            if consumers and consumers.issubset(set(selected_ids)):
                extra_allowed_paths.append(operation_path)
                suggested_operations.append(copy.deepcopy(dict(operation)))
            continue
        if operation_path == "/template/fonts/-" and any(
            "font/file" in properties for properties in locks.values()
        ):
            suggested_operations.append(copy.deepcopy(dict(operation)))
    for issue in selected_issues:
        instruction = str(issue.get("instruction") or "")
        if not re.search(r"#[0-9a-f]{3,8}\b", instruction, re.IGNORECASE):
            continue
        for layer_id in issue["layerIds"]:
            role = layers[layer_id]["layer"].get("colourRole")
            if not isinstance(role, str) or not role:
                continue
            consumers = {
                other_id for other_id, item in layers.items()
                if item["layer"].get("colourRole") == role
            }
            if consumers and consumers.issubset(set(selected_ids)):
                extra_allowed_paths.append(f"/template/semanticColours/{role}")
    extra_roles = {
        path.rsplit("/", 1)[-1]
        for path in extra_allowed_paths
        if path.startswith("/template/semanticColours/")
    }
    # A selected layer without any structured property target cannot be
    # patched by the bounded refinement contract.  Drop it (and any issue
    # that exclusively referenced such layers) while keeping the valid
    # scored evidence, instead of discarding the whole comparison.
    dropped_layer_ids: set[str] = set()
    while True:
        empty = sorted(
            layer_id for layer_id, properties in locks.items()
            if layer_id in selected_ids
            and not properties
            and layers[layer_id]["layer"].get("colourRole") not in extra_roles
        )
        if not empty:
            break
        dropped_layer_ids.update(empty)
        selected_ids = [
            layer_id for layer_id in selected_ids
            if layer_id not in dropped_layer_ids
        ]
        surviving = [
            issue for issue in selected_issues
            if any(layer_id in selected_ids for layer_id in issue["layerIds"])
        ]
        if not surviving or not selected_ids:
            break
        selected_issues = surviving
    if not selected_issues or not selected_ids:
        raise AdTemplateProcessError(
            "review issue has no structured property target for: "
            + ", ".join(sorted(dropped_layer_ids) or sorted(
                layer_id for issue in selected_issues for layer_id in issue["layerIds"]
            ))
        )

    declared_fonts = sorted({
        item.get("file")
        for item in template.get("fonts", [])
        if isinstance(item, Mapping) and isinstance(item.get("file"), str)
    })
    return {
        "primaryPlacement": group_placement,
        "issues": copy.deepcopy(selected_issues),
        "layerIds": selected_ids,
        "layers": {
            layer_id: {
                "placement": layers[layer_id]["placement"],
                "pointer": layers[layer_id]["pointer"],
                "allowedProperties": locks[layer_id],
                "numericTargets": targets[layer_id],
                "current": layers[layer_id]["layer"],
            }
            for layer_id in selected_ids
        },
        "textInputDependencies": dependencies,
        "declaredFonts": declared_fonts,
        "availableFonts": sorted(set(available_fonts) | set(declared_fonts)),
        "extraAllowedPaths": sorted(set(extra_allowed_paths)),
        "suggestedOperations": suggested_operations,
        "unpatchableLayerIds": sorted(dropped_layer_ids),
    }


def build_refinement_batch_contract(
    candidate: Mapping[str, Any],
    issues: Sequence[Mapping[str, Any]],
    *,
    source_placement: str,
    available_fonts: Sequence[str],
    suggested_patch: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    remaining = list(issues)
    groups: list[dict[str, Any]] = []
    while remaining and len(groups) < MAX_GROUPS_PER_CALL:
        group = build_refinement_contract(
            candidate,
            remaining,
            source_placement=source_placement,
            available_fonts=available_fonts,
            suggested_patch=suggested_patch,
        )
        groups.append(group)
        selected = group["issues"]
        remaining = [issue for issue in remaining if issue not in selected]
    merged_layers: dict[str, Any] = {}
    dependencies: list[dict[str, Any]] = []
    extra_paths: set[str] = set()
    suggested: list[dict[str, Any]] = []
    group_paths: list[list[str]] = []
    for group in groups:
        merged_layers.update(group["layers"])
        for dependency in group["textInputDependencies"]:
            if dependency not in dependencies:
                dependencies.append(dependency)
        extra_paths.update(group["extraAllowedPaths"])
        for operation in group["suggestedOperations"]:
            if operation not in suggested:
                suggested.append(operation)
        group_paths.append(sorted(_allowed_paths(group)))
    if not groups:
        raise AdTemplateProcessError("layer refinement produced no bounded groups")
    return {
        "primaryPlacement": groups[0]["primaryPlacement"],
        "groups": groups,
        "remainingIssueCount": len(remaining),
        "issues": [issue for group in groups for issue in group["issues"]],
        "layerIds": list(dict.fromkeys(
            layer_id for group in groups for layer_id in group["layerIds"]
        )),
        "layers": merged_layers,
        "textInputDependencies": dependencies,
        "declaredFonts": groups[0]["declaredFonts"],
        "availableFonts": groups[0]["availableFonts"],
        "extraAllowedPaths": sorted(extra_paths),
        "suggestedOperations": suggested,
        "groupAllowedPaths": group_paths,
    }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def refinement_prompt(contract: Mapping[str, Any]) -> str:
    return f"""You are the layer-refinement role in an exact-clone compiler. Correct only the review-selected bounded groups. The attached images begin with repeated source/current crop pairs in the same order as contract.groups; every pair uses one exact canvas rectangle. Whole-frame evidence follows those pairs. Preserve all unlisted layers and fields byte-for-byte.

Return exactly {{"operations":[{{"op":"replace|add","path":"/template/...","value":...}}]}}. Every layer operation must use one exact pointer and allowed property from the contract. Never replace a whole layer or change layerId, type, inputKey, assets, metadata, semantic colours, or another placement. A dependent text input may change only placeholder or maxLength; its sharedLayerIds explicitly show every placement affected by that editable value. Preserve six distinct source bullet lines as editable placeholder lines when requested. For typography, choose the closest permitted font first, then tune size, tracking and geometry without worsening rendered ink width or alignment. A font declaration may only append one available font as {{"file":"..."}} at /template/fonts/-. tracking is -4..4; lineHeight is at least 1; fontSize is at least 24 for Feed and 32 for Story; geometry width/height must remain positive and all geometry must stay on its canvas. Follow numericTargets exactly when present. Maximum {MAX_OPERATIONS} operations and {MAX_PATCH_BYTES} encoded bytes. Return JSON only.

LAYER REFINEMENT CONTRACT: {_json(contract)}"""


def _allowed_paths(contract: Mapping[str, Any]) -> set[str]:
    allowed: set[str] = set()
    for layer in contract["layers"].values():
        allowed.update(
            f"{layer['pointer']}/{property_path}"
            for property_path in layer["allowedProperties"]
        )
    for dependency in contract["textInputDependencies"]:
        allowed.update(
            f"{dependency['pointer']}/{property_name}"
            for property_name in dependency["allowedProperties"]
        )
    allowed.update(contract.get("extraAllowedPaths", []))
    return allowed


def validate_refinement_patch(
    value: Any,
    *,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"operations"}:
        raise AdTemplateProcessError("layer refinement must return exactly operations")
    operations = value.get("operations")
    if (
        not isinstance(operations, list)
        or not operations
        or len(operations) > MAX_OPERATIONS
        or len(_json(value).encode("utf-8")) > MAX_PATCH_BYTES
    ):
        raise AdTemplateProcessError("layer refinement patch exceeds its bounded contract")
    allowed_paths = _allowed_paths(contract)
    fonts = set(contract["availableFonts"])
    font_changes_allowed = any(
        "font/file" in item["allowedProperties"]
        for item in contract["layers"].values()
    )
    pointers = {
        layer_id: layer["pointer"]
        for layer_id, layer in contract["layers"].items()
    }
    for operation in operations:
        if not isinstance(operation, dict):
            raise AdTemplateProcessError("layer refinement operation is invalid")
        op = operation.get("op")
        expected = {"op", "path"} if op == "remove" else {"op", "path", "value"}
        if set(operation) != expected or op not in {"add", "replace", "remove"}:
            raise AdTemplateProcessError("layer refinement operation has an invalid shape")
        path = operation.get("path")
        value_item = operation.get("value")
        if path == "/template/fonts/-":
            if not font_changes_allowed:
                raise AdTemplateProcessError(
                    "layer refinement font declaration is outside its property locks"
                )
            if (
                op != "add"
                or not isinstance(value_item, dict)
                or set(value_item) != {"file"}
                or not isinstance(value_item.get("file"), str)
            ):
                raise AdTemplateProcessError("layer refinement font declaration is invalid")
            if value_item["file"] not in fonts:
                raise AdTemplateProcessError("layer refinement requested an unavailable font")
            if value_item["file"] in set(contract["declaredFonts"]):
                raise AdTemplateProcessError("layer refinement duplicated a declared font")
            continue
        suggested_modes = {
            (item.get("path"), item.get("op"))
            for item in contract.get("suggestedOperations", [])
            if isinstance(item, Mapping)
        }
        if path not in allowed_paths:
            raise AdTemplateProcessError("layer refinement path is outside its property locks")
        if op == "remove" and (path, op) not in suggested_modes:
            raise AdTemplateProcessError("layer refinement removal was not comparator-authorized")
        layer_id = next(
            (key for key, pointer in pointers.items() if path.startswith(pointer + "/")),
            None,
        )
        if layer_id is None:
            continue
        property_path = path[len(pointers[layer_id]) + 1:]
        target = contract["layers"][layer_id]["numericTargets"].get(property_path)
        if target is not None and (
            op == "remove"
            or isinstance(value_item, bool)
            or not isinstance(value_item, (int, float))
            or abs(float(value_item) - float(target)) > 0.01
        ):
            raise AdTemplateProcessError("layer refinement violated a measured numeric target")
        placement = contract["layers"][layer_id]["placement"]
        if property_path == "tracking" and (
            isinstance(value_item, bool)
            or not isinstance(value_item, (int, float))
            or not -4 <= float(value_item) <= 4
        ):
            raise AdTemplateProcessError("layer refinement tracking must be between -4 and 4")
        if property_path == "lineHeight" and (
            isinstance(value_item, bool)
            or not isinstance(value_item, (int, float))
            or float(value_item) < 1
        ):
            raise AdTemplateProcessError("layer refinement lineHeight must be at least 1")
        if property_path == "fontSize" and (
            isinstance(value_item, bool)
            or not isinstance(value_item, (int, float))
            or float(value_item) < (24 if placement == "feed" else 32)
        ):
            raise AdTemplateProcessError("layer refinement fontSize is below the placement minimum")
        if property_path in {"geometry/width", "geometry/height"} and (
            isinstance(value_item, bool)
            or not isinstance(value_item, (int, float))
            or float(value_item) <= 0
        ):
            raise AdTemplateProcessError("layer refinement geometry size must be positive")
        if property_path in {"geometry/x", "geometry/y"} and (
            isinstance(value_item, bool)
            or not isinstance(value_item, (int, float))
            or float(value_item) < 0
        ):
            raise AdTemplateProcessError("layer refinement geometry position must be non-negative")
        if property_path == "font/file" and value_item not in fonts:
            raise AdTemplateProcessError("layer refinement requested an unavailable font")
    operation_paths = {operation["path"] for operation in operations}
    for group_paths in contract.get("groupAllowedPaths", []):
        if not operation_paths.intersection(group_paths):
            raise AdTemplateProcessError(
                "layer refinement omitted one selected issue group"
            )
    for layer_id, item in contract["layers"].items():
        geometry = copy.deepcopy(item["current"].get("geometry"))
        if not isinstance(geometry, dict):
            continue
        prefix = item["pointer"] + "/geometry/"
        for operation in operations:
            if operation["path"].startswith(prefix):
                field = operation["path"][len(prefix):]
                if operation["op"] == "remove":
                    geometry.pop(field, None)
                else:
                    geometry[field] = operation["value"]
        canvas_width, canvas_height = _CANVAS[item["placement"]]
        if (
            any(
                isinstance(geometry.get(field), bool)
                or not isinstance(geometry.get(field), (int, float))
                for field in ("x", "y", "width", "height")
            )
            or geometry["x"] < 0
            or geometry["y"] < 0
            or geometry["width"] <= 0
            or geometry["height"] <= 0
            or geometry["x"] + geometry["width"] > canvas_width
            or geometry["y"] + geometry["height"] > canvas_height
        ):
            raise AdTemplateProcessError(
                f"layer refinement moved {layer_id} outside its canvas"
            )
    return copy.deepcopy(value)



def write_fixed_crop(
    source: str,
    *,
    placement: str,
    box: Mapping[str, int],
    target: Path,
) -> str:
    width, height = _CANVAS[placement]
    bounds = (
        int(box["x"]),
        int(box["y"]),
        int(box["x"] + box["width"]),
        int(box["y"] + box["height"]),
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        image.convert("RGB").resize((width, height)).crop(bounds).save(target)
    return str(target)


def _ink_metrics(path: str, threshold: int = 120) -> dict[str, Any] | None:
    with Image.open(path) as image:
        gray = image.convert("L")
        pixels = gray.load()
        border = (
            [pixels[x, 0] for x in range(gray.width)]
            + [pixels[x, gray.height - 1] for x in range(gray.width)]
            + [pixels[0, y] for y in range(gray.height)]
            + [pixels[gray.width - 1, y] for y in range(gray.height)]
        )
        border_mean = sum(border) / max(1, len(border))
        border_variance = sum(
            (value - border_mean) ** 2 for value in border
        ) / max(1, len(border))
        if border_mean < 200 or border_variance ** 0.5 > 25:
            return None
        mask = gray.point(lambda value: 255 if value < threshold else 0)
        bounds = mask.getbbox()
        ink_pixels = sum(1 for value in mask.getdata() if value)
    if bounds is None:
        return None
    left, top, right, bottom = bounds
    return {
        "bounds": {
            "x": left,
            "y": top,
            "width": right - left,
            "height": bottom - top,
        },
        "pixels": ink_pixels,
        "threshold": threshold,
        "borderMean": round(border_mean, 2),
        "borderStdDev": round(border_variance ** 0.5, 2),
    }


def validate_text_ink_regression(
    contract: Mapping[str, Any],
    *,
    source_crop: str,
    before_crop: str,
    after_crop: str,
    tolerance: int = 2,
) -> dict[str, Any]:
    primary = contract["primaryPlacement"]
    primary_layers = [
        item for item in contract["layers"].values()
        if item["placement"] == primary
    ]
    if not primary_layers or any(
        item["current"].get("type") != "text" for item in primary_layers
    ):
        return {"mode": "not-text-only"}
    metrics = {
        "source": _ink_metrics(source_crop),
        "before": _ink_metrics(before_crop),
        "after": _ink_metrics(after_crop),
    }
    if any(item is None for item in metrics.values()):
        return {
            "mode": "visual-only",
            "reason": "fixed crop is not uniform-light-background dark text",
        }
    source_bounds = metrics["source"]["bounds"]
    regressions: dict[str, dict[str, int]] = {}
    for field in ("x", "y", "width", "height"):
        before_error = abs(metrics["before"]["bounds"][field] - source_bounds[field])
        after_error = abs(metrics["after"]["bounds"][field] - source_bounds[field])
        if after_error > before_error + tolerance:
            regressions[field] = {
                "beforeError": before_error,
                "afterError": after_error,
            }
    if regressions:
        raise AdTemplateProcessError(
            "layer refinement worsened fixed-crop text ink: " + _json({
                "regressions": regressions,
                "metrics": metrics,
                "tolerance": tolerance,
            })
        )
    return {"mode": "text-ink", "tolerance": tolerance, **metrics}


def find_candidate_render(paths: Sequence[str], placement: str) -> str:
    suffixes = {f"{placement}.png", f"-{placement}.png"}
    for raw in paths:
        name = Path(raw).name
        if name in suffixes or any(name.endswith(suffix) for suffix in suffixes):
            if not name.endswith(("-overlay.png", "-difference.png")):
                return str(raw)
    raise AdTemplateProcessError(
        f"layer refinement {placement} candidate render is unavailable"
    )

def write_matching_crops(
    contract: Mapping[str, Any],
    *,
    reference_path: str,
    candidate_path: str,
    workspace: Path,
    padding: int = 24,
) -> tuple[list[str], dict[str, int]]:
    placement = str(contract["primaryPlacement"])
    width, height = _CANVAS[placement]
    geometries: list[dict[str, Any]] = []
    for layer in contract["layers"].values():
        geometry = layer["current"].get("geometry")
        if layer["placement"] != placement or not isinstance(geometry, Mapping):
            continue
        current = dict(geometry)
        proposed = dict(current)
        for property_path, target_value in layer["numericTargets"].items():
            if property_path.startswith("geometry/"):
                proposed[property_path.split("/", 1)[1]] = target_value
        geometries.extend((current, proposed))
    if not geometries:
        raise AdTemplateProcessError("layer refinement group has no crop geometry")
    left = max(0, int(min(float(item["x"]) for item in geometries)) - padding)
    top = max(0, int(min(float(item["y"]) for item in geometries)) - padding)
    right = min(
        width,
        int(max(float(item["x"]) + float(item["width"]) for item in geometries)) + padding,
    )
    bottom = min(
        height,
        int(max(float(item["y"]) + float(item["height"]) for item in geometries)) + padding,
    )
    if right <= left or bottom <= top:
        raise AdTemplateProcessError("layer refinement crop is empty")
    workspace.mkdir(parents=True, exist_ok=True)
    box = (left, top, right, bottom)
    paths: list[str] = []
    for name, source in (("source", reference_path), ("candidate", candidate_path)):
        target = workspace / f"{name}-crop.png"
        with Image.open(source) as image:
            image.convert("RGB").resize((width, height)).crop(box).save(target)
        paths.append(str(target))
    return paths, {"x": left, "y": top, "width": right - left, "height": bottom - top}
