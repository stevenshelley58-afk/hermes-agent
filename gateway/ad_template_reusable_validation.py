"""Bounded deterministic acceptance checks for reusable ad templates."""

from __future__ import annotations
import copy, hashlib, json, os, shlex
from pathlib import Path
from typing import Any, Callable, Mapping

MAX_SCENARIOS = 4
_SCENARIOS = ("short", "max", "unicode", "optional-empty")


class ReusableTemplateValidationError(ValueError):
    def __init__(self, message, evidence=None):
        super().__init__(message)
        self.evidence = evidence


def _json(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _identity(c, s, r, asset_identity=""):
    return hashlib.sha256(
        _json({
            "version": 2,
            "candidate": c,
            "scenario": s,
            "renderer": r,
            "assets": asset_identity,
        }).encode()
    ).hexdigest()


def _renderer_identity(value):
    command = value or os.environ.get("AD_TEMPLATE_GENERATOR_CMD", "")
    parts = shlex.split(command)
    if parts:
        path = Path(parts[-1])
        if path.is_file():
            return command + ":" + hashlib.sha256(path.read_bytes()).hexdigest()
    return command


def _bounded(v, limit):
    return v if not limit or limit <= 0 else v[:limit]


def _maps(c):
    t = c.get("template")
    if not isinstance(t, Mapping):
        raise ReusableTemplateValidationError("template is missing")
    tr, ir = t.get("textInputs"), t.get("imageInputs")
    if not isinstance(tr, list) or not isinstance(ir, list):
        raise ReusableTemplateValidationError(
            "textInputs and imageInputs must be lists"
        )

    def build(items, label):
        out = {}
        for x in items:
            if (
                not isinstance(x, dict)
                or not isinstance(x.get("key"), str)
                or not x["key"]
            ):
                raise ReusableTemplateValidationError(
                    f"{label} input declaration has no valid key"
                )
            if x["key"] in out:
                raise ReusableTemplateValidationError(
                    f"duplicate {label} input {x['key']}"
                )
            out[x["key"]] = x
        return out

    return build(tr, "text"), build(ir, "image")


def _bindings(c):
    t = c["template"]
    texts, images = _maps(c)
    ta, decl = t.get("assets"), c.get("assets")
    if not isinstance(ta, dict) or not isinstance(decl, list):
        raise ReusableTemplateValidationError(
            "template and outer asset declarations are required"
        )
    declared = {x.get("assetKey"): x for x in decl if isinstance(x, dict)}
    for placement, key in (("feed", "feedLayout"), ("story", "storyLayout")):
        layout = t.get(key)
        layers = layout.get("layers") if isinstance(layout, Mapping) else None
        if not isinstance(layers, list):
            raise ReusableTemplateValidationError(f"{placement} layers are missing")
        for layer in layers:
            if not isinstance(layer, Mapping):
                raise ReusableTemplateValidationError(
                    f"{placement} contains a non-object layer"
                )
            inp, typ = layer.get("inputKey"), layer.get("type")
            if typ == "text" and inp not in texts:
                raise ReusableTemplateValidationError(
                    f"{placement} text layer {layer.get('layerId')} references undeclared input {inp!r}"
                )
            if typ in {"image_slot", "logo"}:
                if inp not in images:
                    raise ReusableTemplateValidationError(
                        f"{placement} {typ} layer {layer.get('layerId')} references undeclared input {inp!r}"
                    )
                item = images[inp]
                asset = item.get("defaultAssetKey")
                if item.get("required") is True and not isinstance(asset, str):
                    raise ReusableTemplateValidationError(
                        f"required image input {inp} has no defaultAssetKey"
                    )
                if not isinstance(asset, str):
                    if item.get("required") is True:
                        raise ReusableTemplateValidationError(
                            f"required image input {inp} has no defaultAssetKey"
                        )
                    continue
                if asset not in ta or asset not in declared:
                    raise ReusableTemplateValidationError(
                        f"image input {inp} default {asset!r} is not declared in both asset maps"
                    )
                left, right = ta[asset], declared[asset]
                if (
                    not isinstance(left, Mapping)
                    or not isinstance(right, Mapping)
                    or (left.get("fileName"), left.get("mimeType"))
                    != (right.get("fileName"), right.get("mimeType"))
                ):
                    raise ReusableTemplateValidationError(
                        f"image input {inp} default {asset} has conflicting asset metadata"
                    )


def _scenario(c, s):
    r = copy.deepcopy(dict(c))
    t = r["template"]
    texts, _ = _maps(r)
    if s == "short":
        for x in texts.values():
            x["placeholder"] = _bounded("A short editable value", x.get("maxLength"))
    elif s == "max":
        for x in texts.values():
            n = x.get("maxLength")
            if not isinstance(n, int) or isinstance(n, bool) or n <= 0:
                raise ReusableTemplateValidationError(
                    f"text input {x['key']} must declare a positive maxLength"
                )
            x["placeholder"] = _bounded("Maximum editable content " * n, n)
    elif s == "unicode":
        for x in texts.values():
            x["placeholder"] = _bounded("Ångström 東京 — café 🏡", x.get("maxLength"))
        for key in ("feedLayout", "storyLayout"):
            for layer in t[key]["layers"]:
                if layer.get("type") == "image_slot":
                    layer["defaultCrop"] = {
                        "x": 0.05,
                        "y": 0.05,
                        "width": 0.9,
                        "height": 0.9,
                    }
                    break
    elif s == "optional-empty":
        for x in texts.values():
            if x.get("required") is not True:
                x["placeholder"] = ""
    else:
        raise ReusableTemplateValidationError(f"unknown reusable scenario {s}")
    return r


def reusable_repair_context(candidate):
    """Expose the actual deterministic replacement payloads, not guessed copy."""
    try:
        return {
            scenario: {
                item["key"]: item["placeholder"]
                for item in _scenario(candidate, scenario)["template"]["textInputs"]
            }
            for scenario in _SCENARIOS
        }
    except ReusableTemplateValidationError as exc:
        return {"validationError": str(exc)}


def validate_reusable_template(
    candidate: Mapping[str, Any],
    *,
    workspace: Path,
    render: Callable[..., Mapping[str, Any]],
    asset_overrides: Mapping[str, bytes] | None = None,
    cached: Mapping[str, Any] | None = None,
    renderer_identity: str | None = None,
    check_stop: Callable[[], None] | None = None,
) -> dict[str, Any]:
    asset_identity = hashlib.sha256(
        b"".join(
            k.encode() + hashlib.sha256(v).digest()
            for k, v in sorted((asset_overrides or {}).items())
        )
    ).hexdigest()
    renderer = _renderer_identity(renderer_identity)
    out = {
        "schema": "blockwise.reusable-template-validation.v1",
        "schema_version": 1,
        "status": "passed",
        "renderer": renderer,
        "scenarioLimit": MAX_SCENARIOS,
        "counts": {"total": 0, "passed": 0, "failed": 0},
        "scenarios": [],
    }
    try:
        _bindings(candidate)
    except ReusableTemplateValidationError as exc:
        out["status"] = "failed"
        out["error"] = str(exc)[:1200]
        out["counts"] = {"total": 0, "passed": 0, "failed": 0}
        out["results"] = []
        raise ReusableTemplateValidationError(out["error"], evidence=out) from exc
    previous = (
        cached
        if isinstance(cached, Mapping) and cached.get("renderer") == renderer
        else {}
    )
    old = {
        x.get("identity"): x
        for x in previous.get("scenarios", [])
        if isinstance(x, Mapping) and isinstance(x.get("identity"), str)
    }
    for i, s in enumerate(_SCENARIOS[:MAX_SCENARIOS]):
        if check_stop is not None:
            check_stop()
        identity = _identity(candidate, s, renderer, asset_identity)
        prior = old.get(identity)
        if (
            isinstance(prior, Mapping)
            and prior.get("status") == "passed"
            and isinstance(prior.get("outputs"), Mapping)
            and all(
                isinstance(prior["outputs"].get(p), str)
                and Path(prior["outputs"][p]).is_file()
                for p in ("feed", "story")
            )
        ):
            out["scenarios"].append(dict(prior))
            continue
        try:
            result = render(
                _scenario(candidate, s),
                workspace / "reusable-validation" / f"{i:02d}-{s}-{identity[:16]}",
                asset_overrides=asset_overrides,
            )
            outputs = result.get("render") if isinstance(result, Mapping) else None
            if not isinstance(outputs, Mapping) or not all(
                isinstance(outputs.get(p), str) and Path(outputs[p]).is_file()
                for p in ("feed", "story")
            ):
                raise ReusableTemplateValidationError(
                    "shared renderer returned incomplete or missing Feed/Story outputs"
                )
            out["scenarios"].append({
                "name": s,
                "identity": identity,
                "status": "passed",
                "outputs": dict(outputs),
            })
        except Exception as exc:
            error = str(exc)[:16000]
            out["scenarios"].append({
                "name": s,
                "identity": identity,
                "status": "failed",
                "error": error,
            })
            out["status"] = "failed"
            out["error"] = f"reusable scenario {s} failed: {error}"
            out["counts"] = {
                "total": len(out["scenarios"]),
                "passed": sum(
                    1 for x in out["scenarios"] if x.get("status") == "passed"
                ),
                "failed": sum(
                    1 for x in out["scenarios"] if x.get("status") == "failed"
                ),
            }
            out["results"] = out["scenarios"]
            raise ReusableTemplateValidationError(out["error"], evidence=out) from exc
    out["counts"] = {
        "total": len(out["scenarios"]),
        "passed": sum(1 for x in out["scenarios"] if x.get("status") == "passed"),
        "failed": sum(1 for x in out["scenarios"] if x.get("status") == "failed"),
    }
    out["results"] = out["scenarios"]
    return out
