"""Bounded issue-group refinement for the exact-clone controller."""

from __future__ import annotations

import copy
import json
import math
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



# Review output may use friendly field names, while patch paths use the
# renderer's canonical nested names. Keep this allow-list deliberately small:
# a target is an authorization to mutate one property, not a general JSON
# pointer into the candidate.
_TARGET_PROPERTY_ALIASES = {
    **_FIELD_PATHS,
    "geometry/x": "geometry/x",
    "geometry/y": "geometry/y",
    "geometry/width": "geometry/width",
    "geometry/height": "geometry/height",
    "font/file": "font/file",
    "fontsize": "fontSize",
    "fontfamily": "fontFamily",
    "fontweight": "fontWeight",
    "lineheight": "lineHeight",
    "tracking": "tracking",
    "opacity": "opacity",
    "cornerradius": "cornerRadius",
    "rotationdegrees": "rotationDegrees",
    "maxlines": "maxLines",
    "maxcharacters": "maxCharacters",
    "alignment": "alignment",
    "defaultcrop/x": "defaultCrop/x",
    "defaultcrop/y": "defaultCrop/y",
    "defaultcrop/width": "defaultCrop/width",
    "defaultcrop/height": "defaultCrop/height",
    "defaultcrop/scale": "defaultCrop/scale",
    "effects/shadow/blur": "effects/shadow/blur",
    "effects/shadow/offsetx": "effects/shadow/offsetX",
    "effects/shadow/offsety": "effects/shadow/offsetY",
    "effects/stroke/width": "effects/stroke/width",
    "fill": "fill",
    "colourrole": "colourRole",
    "effects/stroke/colourrole": "effects/stroke/colourRole",
    "effects/shadow/colourrole": "effects/shadow/colourRole",
    "stroke": "effects/stroke",
    "shadow": "effects/shadow",
    "effects/stroke": "effects/stroke",
    "effects/shadow": "effects/shadow",
    "mask": "mask",
    "defaultcrop": "defaultCrop",
    "effects": "effects",
    "blendmode": "effects/blendMode",
    "colour": "fill/colour",
    "color": "fill/colour",
    "fill/colour": "fill/colour",
    "fill/color": "fill/colour",
    "effects/stroke/colour": "effects/stroke/colour",
    "effects/stroke/color": "effects/stroke/colour",
    "effects/shadow/colour": "effects/shadow/colour",
    "effects/shadow/color": "effects/shadow/colour",
}
_NUMERIC_TARGET_PROPERTIES = {
    "geometry/x", "geometry/y", "geometry/width", "geometry/height",
    "fontSize", "fontWeight", "lineHeight", "tracking", "opacity",
    "cornerRadius", "rotationDegrees", "maxLines", "maxCharacters",
    "defaultCrop/x", "defaultCrop/y", "defaultCrop/width",
    "defaultCrop/height", "defaultCrop/scale", "effects/shadow/blur",
    "effects/shadow/offsetX", "effects/shadow/offsetY", "effects/stroke/width",
}
_STRING_TARGET_PROPERTIES = {
    "colourRole", "effects/stroke/colourRole", "effects/shadow/colourRole",
    "font/file", "fontFamily", "alignment", "mask", "effects/blendMode",
    "fill/colour", "effects/stroke/colour", "effects/shadow/colour",
}
_OBJECT_TARGET_PROPERTIES = {
    "fill", "effects", "effects/stroke", "effects/shadow", "defaultCrop",
}
_SUPPORTED_TARGET_PROPERTIES = (
    _NUMERIC_TARGET_PROPERTIES | _STRING_TARGET_PROPERTIES | _OBJECT_TARGET_PROPERTIES
)


def _canonical_target_property(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AdTemplateProcessError("review target property is invalid")
    raw = value.strip()
    key = raw.replace("_", "").replace("-", "").lower()
    property_path = _TARGET_PROPERTY_ALIASES.get(key)
    if property_path is None:
        raise AdTemplateProcessError(
            f"review target property is unsupported: {raw}"
        )
    return property_path


def _validate_target_value(property_path: str, value: Any) -> Any:
    if property_path in {"colourRole", "effects/stroke/colourRole", "effects/shadow/colourRole"}:
        if not isinstance(value, str) or value not in {
            "background", "primary", "secondary", "accent", "mainText", "inverseText",
        }:
            raise AdTemplateProcessError("review target colourRole must name a declared semantic role")
        return value
    if property_path in _NUMERIC_TARGET_PROPERTIES:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise AdTemplateProcessError(
                f"review target {property_path} must be numeric"
            )
        if not math.isfinite(float(value)):
            raise AdTemplateProcessError(
                f"review target {property_path} must be finite"
            )
        return float(value)
    if property_path == "alignment":
        if not isinstance(value, str) or value not in {"left", "center", "right"}:
            raise AdTemplateProcessError("review target alignment is invalid")
        return value
    if property_path in _OBJECT_TARGET_PROPERTIES:
        if property_path in {"effects", "effects/stroke", "effects/shadow"}:
            if not isinstance(value, dict):
                raise AdTemplateProcessError(f"review target {property_path} must be a JSON object, not encoded JSON text")
            effect_objects = value if property_path == "effects" else {property_path.split("/")[1]: value}
            for effect_name in ("stroke", "shadow"):
                if effect_name not in effect_objects:
                    continue
                effect = effect_objects[effect_name]
                fields = {"colourRole", "opacity", "width"} if effect_name == "stroke" else {"colourRole", "opacity", "blur", "offsetX", "offsetY"}
                if not isinstance(effect, dict) or set(effect) != fields:
                    raise AdTemplateProcessError(
                        f"review target effects/{effect_name} must be an actual JSON object with exactly "
                        f"{', '.join(sorted(fields))}; do not encode nested objects as strings"
                    )
                _validate_target_value("colourRole", effect["colourRole"])
                bounds = {"opacity": (0, 1), "width": (0, 100), "blur": (0, 100), "offsetX": (-200, 200), "offsetY": (-200, 200)}
                for field in fields - {"colourRole"}:
                    number = effect[field]
                    low, high = bounds[field]
                    if (isinstance(number, bool) or not isinstance(number, (int, float))
                            or not math.isfinite(number) or not low <= number <= high
                            or (field == "width" and number <= 0)):
                        raise AdTemplateProcessError(f"review target effects/{effect_name}/{field} is outside renderer bounds")
        if not isinstance(value, (dict, list)):
            raise AdTemplateProcessError(
                f"review target {property_path} must be a JSON object or array"
            )
        forbidden = {"layerId", "inputKey", "assetKey", "assets", "protected"}
        def check_json(item: Any) -> None:
            if isinstance(item, bool) or item is None or isinstance(item, str):
                return
            if isinstance(item, (int, float)):
                if not math.isfinite(float(item)):
                    raise AdTemplateProcessError(
                        f"review target {property_path} contains a non-finite number"
                    )
                return
            if isinstance(item, list):
                for child in item:
                    check_json(child)
                return
            if isinstance(item, dict):
                if any(key in forbidden for key in item):
                    raise AdTemplateProcessError(
                        f"review target {property_path} contains a protected field"
                    )
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise AdTemplateProcessError(
                            f"review target {property_path} has a non-string key"
                        )
                    check_json(child)
                return
            raise AdTemplateProcessError(
                f"review target {property_path} contains an unsupported value"
            )
        check_json(value)
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 2_000:
            raise AdTemplateProcessError(
                f"review target {property_path} is too large"
            )
        return copy.deepcopy(value)
    if property_path in {"font/file", "fontFamily"}:
        if not isinstance(value, str) or not value.strip():
            raise AdTemplateProcessError(
                f"review target {property_path} must be a non-empty string"
            )
        return value.strip()
    if property_path == "mask":
        if value not in {"rounded_rect", "circle", "none"}:
            raise AdTemplateProcessError("review target mask is invalid")
        return value
    if property_path == "effects/blendMode":
        if value not in {"normal", "multiply", "screen", "overlay"}:
            raise AdTemplateProcessError("review target blendMode is invalid")
        return value
    if property_path in {
        "fill/colour", "effects/stroke/colour", "effects/shadow/colour",
    }:
        if (
            not isinstance(value, str)
            or not re.fullmatch(r"#[0-9a-fA-F]{3,8}", value.strip())
        ):
            raise AdTemplateProcessError(
                f"review target {property_path} must be a hex colour"
            )
        return value.strip()
    raise AdTemplateProcessError(f"review target property is unsupported: {property_path}")


def _structured_targets(
    issue: Mapping[str, Any], layers: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]] | None:
    # None means a historical issue with no targets field. A present field is
    # strict so malformed live review data never falls back to ambiguous prose.
    if "targets" not in issue:
        return None
    raw_targets = issue.get("targets")
    if not isinstance(raw_targets, list) or len(raw_targets) > MAX_OPERATIONS:
        raise AdTemplateProcessError("review issue targets are invalid")
    issue_layer_ids = set(issue.get("layerIds") or [])
    result: dict[str, dict[str, Any]] = {}
    for raw_target in raw_targets:
        if not isinstance(raw_target, Mapping) or set(raw_target) != {"layerId", "property", "value"}:
            raise AdTemplateProcessError("review issue target has an invalid shape")
        layer_id = raw_target.get("layerId")
        if not isinstance(layer_id, str) or layer_id not in layers:
            raise AdTemplateProcessError(
                f"review target references unknown layer ID: {layer_id}"
            )
        if layer_id not in issue_layer_ids:
            raise AdTemplateProcessError(
                f"review target layer ID is not listed by its issue: {layer_id}"
            )
        property_path = _canonical_target_property(raw_target.get("property"))
        layer = layers[layer_id]["layer"]
        raw_value = raw_target.get("value")
        if property_path in {"effects/stroke", "effects/shadow"}:
            # Whole effect objects can be added. When the optional parent is
            # absent, lock the equivalent effects object so JSON patch can
            # create it atomically without discarding any existing effect.
            raw_value = _validate_target_value(property_path, raw_value)
            if "effects" not in layer:
                raw_value = {property_path.split("/")[1]: raw_value}
                property_path = "effects"
        layer_type = layer.get("type")
        if property_path == "colourRole" and "colourRole" not in layer:
            raise AdTemplateProcessError(
                f"review target colourRole is unavailable on {layer_id}"
            )
        if (
            property_path in {
                "font/file", "fontSize", "fontFamily", "fontWeight",
                "lineHeight", "tracking", "alignment", "maxLines", "maxCharacters",
            }
            and layer_type != "text"
        ):
            raise AdTemplateProcessError(
                f"review target {property_path} is not mutable on {layer_id}"
            )
        if property_path.startswith("defaultCrop") and layer_type != "image_slot":
            raise AdTemplateProcessError(
                f"review target {property_path} is not mutable on {layer_id}"
            )
        if (
            property_path.startswith(("fill/", "effects/", "defaultCrop/"))
            and property_path not in {"effects/stroke", "effects/shadow"}
            and not _legacy_property_supported(layer, property_path)
        ):
            raise AdTemplateProcessError(
                f"review target {property_path} is unavailable on {layer_id}; "
                "use an existing candidate property, or targets=[] for a concrete structural/semantic-colour correction"
            )
        target_value = _validate_target_value(property_path, raw_value)
        if property_path == "tracking" and not -4 <= target_value <= 4:
            raise AdTemplateProcessError("review target tracking is outside renderer bounds")
        if property_path == "lineHeight" and target_value < 1:
            raise AdTemplateProcessError("review target lineHeight is below renderer minimum")
        placement = layers[layer_id]["placement"]
        minimum_font_size = 24 if placement == "feed" else 32
        if property_path == "fontSize" and target_value < minimum_font_size:
            raise AdTemplateProcessError(
                f"review target fontSize is below placement minimum: {layer_id} "
                f"({placement}) requested {target_value:g}px; minimum {minimum_font_size}px. Resize/reflow its box instead"
            )
        if property_path in {"geometry/x", "geometry/y"} and target_value < 0:
            raise AdTemplateProcessError("review target geometry position is negative")
        if property_path in {"geometry/width", "geometry/height"} and target_value <= 0:
            raise AdTemplateProcessError("review target geometry size is not positive")
        if property_path == "opacity" and not 0 <= target_value <= 1:
            raise AdTemplateProcessError("review target opacity is outside renderer bounds")
        prior = result.setdefault(layer_id, {}).get(property_path)
        if prior is not None and prior != target_value:
            raise AdTemplateProcessError(
                f"review issue has conflicting target values for {layer_id}/{property_path}"
            )
        result[layer_id][property_path] = target_value
    missing_layers = sorted(issue_layer_ids - set(result))
    if raw_targets and missing_layers:
        raise AdTemplateProcessError(
            "review issue targets omit listed layer IDs: " + ", ".join(missing_layers)
        )
    for layer_id, layer_targets in result.items():
        geometry = layers[layer_id]["layer"].get("geometry")
        if not isinstance(geometry, Mapping):
            continue
        proposed = dict(geometry)
        for property_path, target_value in layer_targets.items():
            if property_path.startswith("geometry/"):
                proposed[property_path.split("/", 1)[1]] = target_value
        if any(field in layer_targets for field in {
            "geometry/x", "geometry/y", "geometry/width", "geometry/height"
        }):
            placement = layers[layer_id]["placement"]
            canvas_width, canvas_height = _CANVAS[placement]
            values = [proposed.get(field) for field in ("x", "y", "width", "height")]
            if (
                any(
                    isinstance(item, bool)
                    or not isinstance(item, (int, float))
                    or not math.isfinite(float(item))
                    for item in values
                )
                or values[0] < 0
                or values[1] < 0
                or values[2] <= 0
                or values[3] <= 0
                or values[0] + values[2] > canvas_width
                or values[1] + values[3] > canvas_height
            ):
                raise AdTemplateProcessError(
                    f"review target geometry is outside the {placement} canvas for {layer_id}"
                )
    return result

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
    if re.search(
        r"\bcolou?r\b\s*(?:to|=|at|:)\s*#[0-9a-f]{3,8}\b",
        instruction,
        re.IGNORECASE,
    ):
        properties.add("fill/colour")
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

def _legacy_property_supported(
    layer: Mapping[str, Any], property_path: str
) -> bool:
    if property_path.startswith("geometry/"):
        return isinstance(layer.get("geometry"), Mapping)
    if property_path == "fill/colour":
        return isinstance(layer.get("fill"), Mapping)
    if property_path.startswith("effects/"):
        current: Any = layer
        for part in property_path.split("/"):
            current = current.get(part) if isinstance(current, Mapping) else None
        return current is not None
    if property_path.startswith("defaultCrop/"):
        return isinstance(layer.get("defaultCrop"), Mapping)
    if property_path in {
        "font/file", "fontSize", "fontFamily", "fontWeight",
        "lineHeight", "tracking", "alignment", "maxLines", "maxCharacters",
    }:
        return layer.get("type") == "text"
    if property_path in _SUPPORTED_TARGET_PROPERTIES:
        return True
    return property_path in layer

def _legacy_numeric_targets(
    issue: Mapping[str, Any],
    layer: Mapping[str, Any],
    layer_id: str,
) -> dict[str, float]:
    """Extract targets from historical prose without relying on word order."""
    instruction = str(issue.get("instruction") or "")
    issue_ids = [
        item for item in issue.get("layerIds", [])
        if isinstance(item, str)
    ]
    mentions = [
        (match.start(), match.end(), candidate_id)
        for candidate_id in issue_ids
        for match in re.finditer(
            rf"(?<![A-Za-z0-9_-]){re.escape(candidate_id)}(?![A-Za-z0-9_-])",
            instruction,
            re.IGNORECASE,
        )
    ]
    fields = (
        "x|y|width|height|fontSize|lineHeight|tracking|opacity|cornerRadius|"
        "rotationDegrees|maxLines|maxCharacters"
    )
    parsed: dict[str, list[tuple[int, float, int, int]]] = {}

    def add(match: re.Match[str], priority: int, value_group: int = 2) -> None:
        property_path = _FIELD_PATHS.get(match.group(1).lower())
        if not property_path:
            return
        value = float(match.group(value_group))
        parsed.setdefault(property_path, []).append(
            (priority, value, match.start(), match.end())
        )

    direct = re.compile(
        rf"\b({fields})\b\s*(?:to|=|at|:)\s*(-?\d+(?:\.\d+)?)",
        flags=re.IGNORECASE,
    )
    for match in direct.finditer(instruction):
        add(match, 0)
    from_to = re.compile(
        rf"\b({fields})\b\s+from\s+[-+]?\d+(?:\.\d+)?\s*to\s+(-?\d+(?:\.\d+)?)",
        flags=re.IGNORECASE,
    )
    for match in from_to.finditer(instruction):
        add(match, 1)
    bare = re.compile(
        rf"\b({fields})\b\s+(-?\d+(?:\.\d+)?)",
        flags=re.IGNORECASE,
    )
    for match in bare.finditer(instruction):
        prefix = instruction[max(0, match.start() - 18):match.start()]
        if re.search(r"\b(?:current(?:ly)?|existing|before|was|is)\s*$", prefix, re.IGNORECASE):
            continue
        add(match, 2)
    delta = re.compile(
        rf"\b({fields})\b[^.;]{{0,24}}?([+-]\d+(?:\.\d+)?)\s*(?:px)?\s*delta",
        flags=re.IGNORECASE,
    )
    for match in delta.finditer(instruction):
        property_path = _FIELD_PATHS.get(match.group(1).lower())
        if not property_path:
            continue
        current: Any = layer
        for part in property_path.split("/"):
            current = current.get(part) if isinstance(current, Mapping) else None
        if isinstance(current, (int, float)) and not isinstance(current, bool):
            parsed.setdefault(property_path, []).append(
                (3, float(current) + float(match.group(2)), match.start(), match.end())
            )

    targets: dict[str, float] = {}
    owned_values: dict[str, set[float]] = {}
    for property_path, candidates in parsed.items():
        best_priority = min(item[0] for item in candidates)
        chosen = [item for item in candidates if item[0] == best_priority]
        for _, value, begin, finish in chosen:
            # A single-layer issue has no ambiguity, including the observed form
            # where all coordinates precede the layer name. For grouped issues,
            # attach each field to its nearest explicit layer mention.
            if len(issue_ids) == 1:
                owner = issue_ids[0]
            else:
                nearby = []
                for mention_start, mention_end, candidate_id in mentions:
                    distance = (
                        0 if mention_start <= finish and begin <= mention_end
                        else min(abs(begin - mention_end), abs(mention_start - finish))
                    )
                    if distance <= 120:
                        nearby.append((distance, candidate_id))
                nearby.sort()
                owner = (
                    nearby[0][1]
                    if nearby and (len(nearby) == 1 or nearby[0][0] < nearby[1][0])
                    else None
                )
            if owner is None:
                continue
            owned_values.setdefault(owner + "|" + property_path, set()).add(value)
            if owner == layer_id:
                targets[property_path] = value
    conflicts = [
        key.rsplit("|", 1)[1] for key, values in owned_values.items()
        if len(values) > 1 and key.startswith(layer_id + "|")
    ]
    if conflicts:
        raise AdTemplateProcessError(
            f"review issue has conflicting numeric targets for {layer_id}/"
            + ", ".join(sorted(conflicts))
        )

    # Historical comparator values are clamped to the renderer's safe range.
    if "tracking" in targets:
        targets["tracking"] = max(-4.0, min(4.0, targets["tracking"]))
    if "lineHeight" in targets:
        targets["lineHeight"] = max(1.0, targets["lineHeight"])
    for field in ("geometry/width", "geometry/height"):
        if field in targets:
            targets[field] = max(1.0, targets[field])
    for field in ("geometry/x", "geometry/y"):
        if field in targets:
            targets[field] = max(0.0, targets[field])
    return targets


def _numeric_targets(
    issue: Mapping[str, Any],
    layer: Mapping[str, Any],
    layer_id: str,
) -> dict[str, float]:
    # Kept as a compatibility seam for callers/tests that used the old helper.
    return _legacy_numeric_targets(issue, layer, layer_id)


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
    # Present machine-readable targets are strict. Do this before the
    # historical unknown-ID trimming so a malformed live review receives the
    # existing bounded retry rather than being silently rewritten.
    for issue in issues:
        if "targets" in issue:
            _structured_targets(issue, layers)
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
    targets: dict[str, dict[str, Any]] = {}
    property_targets: dict[str, dict[str, Any]] = {}
    unpatchable_issue_records: list[dict[str, Any]] = []
    for layer_id in selected_ids:
        related = [
            issue for issue in selected_issues if layer_id in issue["layerIds"]
        ]
        target_values: dict[str, Any] = {}
        authoritative_properties: set[str] = set()
        properties: set[str] = set()
        placement = layers[layer_id]["placement"]
        for issue in related:
            structured = _structured_targets(issue, layers)
            issue_targets = (
                structured.get(layer_id, {})
                if structured is not None
                else _numeric_targets(issue, layers[layer_id]["layer"], layer_id)
            )
            if (
                structured is None
                and "fontSize" in issue_targets
                and "fontSize" in _NUMERIC_TARGET_PROPERTIES
            ):
                issue_targets["fontSize"] = max(
                    32.0 if placement in {"story", "both"} else 24.0,
                    issue_targets["fontSize"],
                )
            for property_path, value in issue_targets.items():
                prior = target_values.get(property_path)
                if prior is not None and prior != value:
                    raise AdTemplateProcessError(
                        f"review issue has conflicting target values for {layer_id}/{property_path}"
                    )
                target_values[property_path] = value
            if structured is not None:
                authoritative_properties.update(issue_targets)
                properties.update(issue_targets)
            else:
                properties.update(
                    property_path for property_path in _explicit_properties(
                        issue, issue_targets
                    )
                    if (
                        property_path in _SUPPORTED_TARGET_PROPERTIES
                        and _legacy_property_supported(
                            layers[layer_id]["layer"], property_path
                        )
                    )
                )
        # Keep combined geometry inside the placement canvas so an
        # out-of-canvas reviewer phrasing converges on the nearest
        # renderable rectangle instead of creating an impossible contract
        # that every bounded retry would fail.
        canvas_width, canvas_height = _CANVAS[placement]
        if any(field in target_values for field in ("geometry/x", "geometry/y", "geometry/width", "geometry/height")):
            current_geometry = layers[layer_id]["layer"].get("geometry") or {}
            x = float(target_values.get("geometry/x", current_geometry.get("x") or 0))
            y = float(target_values.get("geometry/y", current_geometry.get("y") or 0))
            width = float(target_values.get("geometry/width", current_geometry.get("width") or 1))
            height = float(target_values.get("geometry/height", current_geometry.get("height") or 1))
            x = max(0.0, min(x, canvas_width - 1.0))
            y = max(0.0, min(y, canvas_height - 1.0))
            width = max(1.0, min(width, canvas_width - x))
            height = max(1.0, min(height, canvas_height - y))
            if "geometry/x" in target_values and "geometry/x" not in authoritative_properties:
                target_values["geometry/x"] = x
            if "geometry/y" in target_values and "geometry/y" not in authoritative_properties:
                target_values["geometry/y"] = y
            if "geometry/width" in target_values and "geometry/width" not in authoritative_properties:
                target_values["geometry/width"] = width
            if "geometry/height" in target_values and "geometry/height" not in authoritative_properties:
                target_values["geometry/height"] = height
        locks[layer_id] = sorted(properties)
        targets[layer_id] = {
            property_path: value for property_path, value in target_values.items()
            if property_path in _NUMERIC_TARGET_PROPERTIES
        }
        property_targets[layer_id] = {
            property_path: value
            for property_path, value in target_values.items()
            if property_path in authoritative_properties
        }

    structured_without_targets = [
        issue for issue in selected_issues
        if "targets" in issue and not issue.get("targets")
    ]
    if structured_without_targets:
        unpatchable_issue_records.extend({
            "issue": copy.deepcopy(dict(issue)),
            "reason": "review issue supplied no patchable targets",
        } for issue in structured_without_targets)
        selected_issues = [
            issue for issue in selected_issues
            if issue not in structured_without_targets
        ]

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
            if (
                layer_id in issue["layerIds"]
                and not ("targets" in issue and issue.get("targets"))
            )
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
                in {
                    "layerId", "type", "inputKey", "protected", "assetKey",
                    "assets", "colourRole", "shape", "icon",
                }
                or property_path not in _SUPPORTED_TARGET_PROPERTIES
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
    normalized_targets = [
        {
            "layerId": layer_id,
            "property": property_path,
            "value": copy.deepcopy(value),
        }
        for layer_id in selected_ids
        for property_path, value in property_targets[layer_id].items()
    ]
    return {
        "primaryPlacement": group_placement,
        "issues": copy.deepcopy(selected_issues),
        "targets": normalized_targets,
        "layerIds": selected_ids,
        "layers": {
            layer_id: {
                "placement": layers[layer_id]["placement"],
                "pointer": layers[layer_id]["pointer"],
                "allowedProperties": locks[layer_id],
                "numericTargets": targets[layer_id],
                "propertyTargets": property_targets[layer_id],
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
        "unpatchableIssues": unpatchable_issue_records,
    }


def build_refinement_batch_contract(
    candidate: Mapping[str, Any],
    issues: Sequence[Mapping[str, Any]],
    *,
    source_placement: str,
    available_fonts: Sequence[str],
    suggested_patch: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    # Build valid groups independently. One malformed/vague issue must not
    # prevent unrelated review work from reaching the bounded refinement role.
    remaining = list(issues)
    groups: list[dict[str, Any]] = []
    unpatchable: list[dict[str, Any]] = []
    while remaining and len(groups) < MAX_GROUPS_PER_CALL:
        try:
            group = build_refinement_contract(
                candidate,
                remaining,
                source_placement=source_placement,
                available_fonts=available_fonts,
                suggested_patch=suggested_patch,
            )
        except AdTemplateProcessError as exc:
            # Isolate an issue that cannot be contracted on its own. If all
            # individual issues are valid, the failure is an interaction
            # (typically conflicting values); quarantine the first bounded
            # issue so the remaining groups can still be recovered.
            failing_index = None
            failing_reason = str(exc)
            for index, issue in enumerate(remaining):
                try:
                    build_refinement_contract(
                        candidate,
                        [issue],
                        source_placement=source_placement,
                        available_fonts=available_fonts,
                        suggested_patch=suggested_patch,
                    )
                except AdTemplateProcessError as individual_exc:
                    failing_index = index
                    failing_reason = str(individual_exc)
                    break
            if failing_index is None:
                failing_index = 0
            failed_issue = remaining.pop(failing_index)
            unpatchable.append({
                "issue": copy.deepcopy(dict(failed_issue)),
                "reason": failing_reason,
            })
            continue
        groups.append(group)
        selected = group["issues"]
        # Remove occurrences, not all equal values, so duplicate historical
        # issues cannot accidentally disappear from the batch.
        for selected_issue in [
            *selected,
            *[
                item.get("issue")
                for item in group.get("unpatchableIssues", [])
                if isinstance(item, Mapping)
            ],
        ]:
            for index, issue in enumerate(remaining):
                if issue == selected_issue:
                    remaining.pop(index)
                    break

    merged_layers: dict[str, Any] = {}
    dependencies: list[dict[str, Any]] = []
    extra_paths: set[str] = set()
    suggested: list[dict[str, Any]] = []
    group_paths: list[list[str]] = []
    for group in groups:
        unpatchable.extend(group.get("unpatchableIssues", []))
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
        if unpatchable:
            summary = "; ".join(
                item["reason"] for item in unpatchable[:3]
            )
            raise AdTemplateProcessError(
                "layer refinement produced no patchable groups; "
                f"unpatchable issues: {summary}"
            )
        raise AdTemplateProcessError("layer refinement produced no bounded groups")
    unpatchable_layer_ids = sorted({
        layer_id
        for item in unpatchable
        for layer_id in item["issue"].get("layerIds", [])
        if isinstance(layer_id, str)
    })
    normalized_targets = [
        target
        for group in groups
        for target in group.get("targets", [])
    ]
    return {
        "primaryPlacement": groups[0]["primaryPlacement"],
        "groups": groups,
        "targets": normalized_targets,
        "remainingIssueCount": len(remaining),
        "unpatchableIssueCount": len(unpatchable),
        "unpatchableIssues": unpatchable,
        "unpatchableLayerIds": unpatchable_layer_ids,
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

Return exactly {{"operations":[{{"op":"replace|add","path":"/template/...","value":...}}]}}. Every layer operation must use one exact pointer and allowed property from the contract. Never replace a whole layer or change layerId, type, inputKey, assets, metadata, semantic colours, or another placement. A dependent text input may change only placeholder or maxLength; its sharedLayerIds explicitly show every placement affected by that editable value. Preserve the actual source item count in every placement, never a hardcoded number of bullet lines. Keep the current font family fixed for positional repairs; exact font-family identity is excluded. Tune size, tracking and geometry only for measured defects, without worsening rendered ink width or alignment. A font declaration may only append one available font as {{"file":"..."}} at /template/fonts/-. tracking is -4..4; lineHeight is at least 1; fontSize is at least 24 for Feed and 32 for Story; geometry width/height must remain positive and all geometry must stay on its canvas. Follow numericTargets and propertyTargets exactly when present. Machine-readable targets are authoritative and must not be inferred from English wording. Maximum {MAX_OPERATIONS} operations and {MAX_PATCH_BYTES} encoded bytes. Return JSON only.

Correct visible label alignment relative to its unchanged container, not by moving the whole group. Keep every checklist row and enclosure.
JSON POINTER PREFLIGHT: replace only an existing leaf property; use add for an allowed missing optional property. Use the contract's layer pointers and current values, never indices remembered from a prior candidate. Do not repeat already-satisfied targets or include unrelated edits.

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


def _read_contract_path(value: Any, property_path: str) -> tuple[bool, Any]:
    current = value
    for part in property_path.split("/"):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return False, None
    return True, current


def _contract_target(item: Mapping[str, Any], property_path: str) -> tuple[bool, Any]:
    for key in ("propertyTargets", "numericTargets"):
        targets = item.get(key)
        if isinstance(targets, Mapping) and property_path in targets:
            return True, copy.deepcopy(targets[property_path])
    return False, None


def _target_matches(property_path: str, current: Any, target: Any) -> bool:
    if property_path in _NUMERIC_TARGET_PROPERTIES:
        return (
            isinstance(current, (int, float))
            and not isinstance(current, bool)
            and math.isfinite(float(current))
            and isinstance(target, (int, float))
            and not isinstance(target, bool)
            and math.isfinite(float(target))
            and abs(float(current) - float(target)) <= 0.01
        )
    return current == target


def _group_paths_satisfied(
    contract: Mapping[str, Any], group_paths: Sequence[str],
) -> bool:
    if not group_paths:
        return False
    layers = contract.get("layers")
    if not isinstance(layers, Mapping):
        return False
    for path in group_paths:
        matched = False
        for item in layers.values():
            if not isinstance(item, Mapping):
                continue
            pointer = item.get("pointer")
            if not isinstance(pointer, str) or not path.startswith(pointer + "/"):
                continue
            property_path = path[len(pointer) + 1:]
            allowed = item.get("allowedProperties")
            if not isinstance(allowed, list) or property_path not in allowed:
                continue
            has_target, target = _contract_target(item, property_path)
            current = item.get("current")
            exists, current_value = _read_contract_path(current, property_path)
            if not has_target or not exists or not _target_matches(
                property_path, current_value, target,
            ):
                return False
            matched = True
            break
        if not matched:
            # Dependency and semantic-colour paths are intentionally not
            # considered deterministically satisfied.
            return False
    return True


def _safe_suggested_operations(contract: Mapping[str, Any]) -> bool:
    """Check whether suggestions are redundant exact layer-target hints."""
    layers = contract.get("layers")
    suggestions = contract.get("suggestedOperations")
    if suggestions in (None, []):
        return True
    if not isinstance(layers, Mapping) or not isinstance(suggestions, list):
        return False
    for operation in suggestions:
        if not isinstance(operation, Mapping):
            return False
        if operation.get("op") not in {"add", "replace"}:
            return False
        path = operation.get("path")
        if (
            not isinstance(path, str)
            or path.startswith("/template/semanticColours/")
            or path.startswith("/template/textInputs/")
            or path == "/template/fonts/-"
            or "value" not in operation
        ):
            return False
        matched = False
        for item in layers.values():
            if not isinstance(item, Mapping):
                continue
            pointer = item.get("pointer")
            if not isinstance(pointer, str) or not path.startswith(pointer + "/"):
                continue
            property_path = path[len(pointer) + 1:]
            allowed = item.get("allowedProperties")
            if not isinstance(allowed, list) or property_path not in allowed:
                return False
            has_target, target = _contract_target(item, property_path)
            if not has_target or not _target_matches(
                property_path, operation["value"], target,
            ):
                return False
            matched = True
            break
        if not matched:
            return False
    return True


def is_refinement_satisfied(contract: Mapping[str, Any]) -> bool:
    """Return whether every compiled group already equals its exact targets."""
    if not isinstance(contract, Mapping):
        return False
    if contract.get("unpatchableIssues") or contract.get("unpatchableIssueCount"):
        return False
    if contract.get("remainingIssueCount"):
        return False
    groups = contract.get("groups")
    if not isinstance(groups, list):
        groups = [contract]
    if not groups:
        return False
    for group in groups:
        if not isinstance(group, Mapping):
            return False
        if group.get("unpatchableIssues") or group.get("unpatchableIssueCount"):
            return False
        layers = group.get("layers")
        if not isinstance(layers, Mapping) or not layers:
            return False
        if group.get("textInputDependencies") or group.get("extraAllowedPaths"):
            return False
        if not _safe_suggested_operations(group):
            return False
        for item in layers.values():
            if not isinstance(item, Mapping):
                return False
            allowed = item.get("allowedProperties")
            if not isinstance(allowed, list) or not allowed:
                return False
            for property_path in allowed:
                has_target, target = _contract_target(item, property_path)
                exists, current = _read_contract_path(
                    item.get("current"), property_path,
                )
                if not has_target or not exists or not _target_matches(
                    property_path, current, target,
                ):
                    return False
    return True


def validate_structured_repair_candidate(before, updated, issues):
    """Keep exact targets locked even when other issues need generic repair."""
    original_layers = _candidate_layers(before)
    repaired_layers = _candidate_layers(updated)
    failures = []
    for issue in issues:
        if not issue.get("targets"):
            continue
        targets = _structured_targets(issue, original_layers) or {}
        for layer_id, properties in targets.items():
            layer = repaired_layers.get(layer_id, {}).get("layer", {})
            for property_path, target in properties.items():
                exists, current = _read_contract_path(layer, property_path)
                if not exists or not _target_matches(property_path, current, target):
                    failures.append(f"{layer_id}/{property_path} must equal {json.dumps(target)}")
    if failures:
        raise AdTemplateProcessError(
            "repair skipped measured corrections: " + "; ".join(failures)[:8000]
        )
    return copy.deepcopy(updated)


def compile_refinement_patch(
    contract: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Compile only exact, fully targeted layer repairs.

    An empty operation list means all exact targets are already present.
    Qualitative, semantic, dependency, font-declaration, and incomplete
    contracts return None so callers can use bounded model repair instead.
    """
    if not isinstance(contract, Mapping):
        return None
    if (
        contract.get("unpatchableIssues")
        or contract.get("unpatchableIssueCount")
        or contract.get("remainingIssueCount")
        or contract.get("textInputDependencies")
        or contract.get("extraAllowedPaths")
        or not _safe_suggested_operations(contract)
    ):
        return None
    groups = contract.get("groups")
    if not isinstance(groups, list):
        groups = [contract]
    if not groups:
        return None
    operations: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, Mapping):
            return None
        layers = group.get("layers")
        if not isinstance(layers, Mapping) or not layers:
            return None
        for layer_id in sorted(layers):
            item = layers[layer_id]
            if not isinstance(item, Mapping):
                return None
            pointer = item.get("pointer")
            allowed = item.get("allowedProperties")
            current_layer = item.get("current")
            if (
                not isinstance(pointer, str)
                or not pointer.startswith("/template/")
                or not isinstance(allowed, list)
                or not allowed
                or not isinstance(current_layer, Mapping)
            ):
                return None
            for property_path in sorted(allowed):
                if not isinstance(property_path, str) or not property_path:
                    return None
                has_target, target = _contract_target(item, property_path)
                if not has_target:
                    return None
                exists, current = _read_contract_path(
                    current_layer, property_path,
                )
                if exists and _target_matches(property_path, current, target):
                    continue
                if property_path == "font/file":
                    declared_fonts = group.get("declaredFonts") or contract.get("declaredFonts") or []
                    if target not in declared_fonts:
                        return None
                parts = property_path.split("/")
                if any(part in {"", ".", ".."} for part in parts):
                    return None
                if exists:
                    operation = "replace"
                else:
                    parent_path = "/".join(parts[:-1])
                    if not parent_path:
                        parent_exists, parent = True, current_layer
                    else:
                        parent_exists, parent = _read_contract_path(
                            current_layer, parent_path,
                        )
                    if (
                        not parent_exists
                        or not isinstance(parent, (Mapping, list))
                        or isinstance(parent, list)
                    ):
                        return None
                    operation = "add"
                operations.append({
                    "op": operation,
                    "path": pointer + "/" + property_path,
                    "value": copy.deepcopy(target),
                })
                if len(operations) > MAX_OPERATIONS:
                    return None
    patch = {"operations": operations}
    if len(_json(patch).encode("utf-8")) > MAX_PATCH_BYTES:
        return None
    try:
        validate_refinement_patch(patch, contract=contract)
    except (AdTemplateProcessError, AdTemplateStructuredOutputError, KeyError, TypeError, ValueError):
        return None
    return patch


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
        or len(operations) > MAX_OPERATIONS
        or len(_json(value).encode("utf-8")) > MAX_PATCH_BYTES
    ):
        raise AdTemplateProcessError("layer refinement patch exceeds its bounded contract")
    if not operations and not is_refinement_satisfied(contract):
        raise AdTemplateProcessError(
            "layer refinement empty patch does not satisfy every exact target"
        )
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
        numeric_target = contract["layers"][layer_id]["numericTargets"].get(property_path)
        property_target = contract["layers"][layer_id].get(
            "propertyTargets", {}
        ).get(property_path)
        if property_target is not None:
            if (
                op == "remove"
                or (
                    property_path in _NUMERIC_TARGET_PROPERTIES
                    and (
                        isinstance(value_item, bool)
                        or not isinstance(value_item, (int, float))
                        or not math.isfinite(float(value_item))
                        or abs(float(value_item) - float(property_target)) > 0.01
                    )
                )
                or (
                    property_path not in _NUMERIC_TARGET_PROPERTIES
                    and value_item != property_target
                )
            ):
                raise AdTemplateProcessError(
                    "layer refinement violated an authoritative property target "
                    "(numeric target mismatch)"
                )
        elif numeric_target is not None and (
            op == "remove"
            or isinstance(value_item, bool)
            or not isinstance(value_item, (int, float))
            or not math.isfinite(float(value_item))
            or abs(float(value_item) - float(numeric_target)) > 0.01
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
    for layer_id, item in contract["layers"].items():
        for property_path, target_value in item.get("propertyTargets", {}).items():
            path = item["pointer"] + "/" + property_path
            if path in operation_paths:
                continue
            exists, current = _read_contract_path(
                item.get("current"), property_path,
            )
            if not exists or not _target_matches(
                property_path, current, target_value,
            ):
                raise AdTemplateProcessError(
                    "layer refinement omitted authoritative target "
                    f"{layer_id}/{property_path}"
                )
    for group_paths in contract.get("groupAllowedPaths", []):
        if not operation_paths.intersection(group_paths) and not _group_paths_satisfied(
            contract, group_paths,
        ):
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
