#!/usr/bin/env python3
"""Read-only vision-route benchmark for the sole ad-template process.

The harness replays one already-rendered comparison without invoking the
controller, renderer, import route, or Tool-run store.  It deliberately keeps
all provider error dumps in a temporary directory so a failed benchmark cannot
pollute Hermes session history.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


DEFAULT_MODELS = (
    "qwen3-vl-flash",
    "qwen3-vl-30b-a3b",
    "qwen3-vl-235b-a22b",
)
_TARGET_FIELD = re.compile(
    r"(?:\bx\b|\by\b|width|height|font(?:size|family|weight)?|lineheight|"
    r"tracking|colour|color|crop)",
    re.IGNORECASE,
)
_TARGET_VALUE = re.compile(r"(?:#[0-9a-f]{3,8}|-?\d+(?:\.\d+)?|\.woff2\b)", re.IGNORECASE)
_REVIEW_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "scores", "issues", "warnings", "effects", "fontSubstitution", "patch"],
    "properties": {
        "decision": {"enum": ["accept", "revise"]},
        "scores": {
            "type": "object",
            "additionalProperties": False,
            "required": ["overall", "geometry", "typography", "colourEffects", "imageCrop", "details"],
            "properties": {
                key: {"type": "number", "minimum": 0, "maximum": 10}
                for key in ("overall", "geometry", "typography", "colourEffects", "imageCrop", "details")
            },
        },
        "issues": {
            "type": "array",
            "maxItems": 64,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["placement", "layerIds", "category", "instruction", "severity"],
                "properties": {
                    "placement": {"enum": ["feed", "story", "both"]},
                    "layerIds": {"type": "array", "items": {"type": "string"}},
                    "category": {"enum": ["geometry", "typography", "colourEffects", "imageCrop", "details"]},
                    "instruction": {"type": "string"},
                    "severity": {"enum": ["blocker", "material", "minor"]},
                },
            },
        },
        "warnings": {"type": "array", "maxItems": 32, "items": {"type": "string"}},
        "effects": {
            "type": "object",
            "additionalProperties": False,
            "required": ["shading", "gradients", "shadows", "transparency", "borders", "masks", "texture"],
            "properties": {
                key: {"enum": ["match", "not_present", "mismatch"]}
                for key in ("shading", "gradients", "shadows", "transparency", "borders", "masks", "texture")
            },
        },
        "fontSubstitution": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["source", "used", "reason"],
                    "properties": {
                        "source": {"type": "string"},
                        "used": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                },
            ]
        },
        "patch": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["operations"],
                    "properties": {
                        "operations": {
                            "type": "array",
                            "maxItems": 64,
                            "items": {
                                "type": "object",
                                "required": ["op", "path"],
                                "properties": {
                                    "op": {"enum": ["replace", "add", "remove"]},
                                    "path": {"type": "string"},
                                    "value": {},
                                },
                            },
                        }
                    },
                },
            ]
        },
    },
}


def _response_text(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("final_response") or value.get("output") or "")
    return "" if value is None else str(value)


def _candidate_layer_ids(candidate: Mapping[str, Any]) -> set[str]:
    template = candidate.get("template")
    if not isinstance(template, Mapping):
        return set()
    result: set[str] = set()
    for layout_key in ("feedLayout", "storyLayout"):
        layout = template.get(layout_key)
        layers = layout.get("layers") if isinstance(layout, Mapping) else None
        if not isinstance(layers, list):
            continue
        for layer in layers:
            if isinstance(layer, Mapping) and isinstance(layer.get("layerId"), str):
                result.add(layer["layerId"])
    return result


def assess_review(
    review: Mapping[str, Any],
    *,
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    validate: Callable[[Any], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return auditable quality signals for one validated model response."""
    if validate is None:
        from gateway.exact_clone_process import validate_review

        validate = validate_review

    validated = validate(dict(review))
    baseline_validated = validate(dict(baseline))
    actual_layers = _candidate_layer_ids(candidate)
    reported_layers = {
        layer_id
        for issue in validated["issues"]
        for layer_id in issue.get("layerIds", [])
        if isinstance(layer_id, str)
    }
    expected_layers = {
        layer_id
        for issue in baseline_validated["issues"]
        for layer_id in issue.get("layerIds", [])
        if isinstance(layer_id, str)
    }
    known_issue_recall = (
        len(reported_layers & expected_layers) / len(expected_layers)
        if expected_layers
        else 1.0
    )
    instructions = [str(issue.get("instruction") or "") for issue in validated["issues"]]
    patchable = [
        bool(_TARGET_FIELD.search(instruction) and _TARGET_VALUE.search(instruction))
        for instruction in instructions
    ]
    patchable_ratio = sum(patchable) / len(patchable) if patchable else 1.0
    score_error = abs(
        float(validated["scores"]["overall"])
        - float(baseline_validated["scores"]["overall"])
    )
    invalid_layers = sorted(reported_layers - actual_layers)
    revision_accurate = validated["decision"] == baseline_validated["decision"]
    qualified = bool(
        revision_accurate
        and not invalid_layers
        and patchable_ratio == 1.0
        and known_issue_recall == 1.0
        and score_error <= 1.5
    )
    return {
        "qualified": qualified,
        "revision_accurate": revision_accurate,
        "score_absolute_error": round(score_error, 2),
        "known_issue_recall": round(known_issue_recall, 3),
        "patchable_issue_ratio": round(patchable_ratio, 3),
        "invalid_layer_ids": invalid_layers,
        "review": validated,
    }


def assess_comparator_result(
    value: Mapping[str, Any],
    *,
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate both the visual diagnosis and its one-call correction patch."""
    from gateway.exact_clone_process import validate_comparator_result

    comparator = validate_comparator_result(dict(value), candidate=candidate)
    result = assess_review(
        comparator["review"], baseline=baseline, candidate=candidate
    )
    requires_patch = comparator["review"]["decision"] == "revise"
    patch_valid = comparator["patchError"] is None and (
        comparator["patch"] is not None if requires_patch else comparator["patch"] is None
    )
    result["patch_valid"] = patch_valid
    result["patch_error"] = comparator["patchError"]
    result["patch_operations"] = (
        len(comparator["patch"].get("operations") or [])
        if isinstance(comparator["patch"], Mapping)
        else 0
    )
    result["qualified"] = bool(result["qualified"] and patch_valid)
    return result


def load_benchmark_case(run_root: Path, iteration: int) -> dict[str, Any]:
    """Load one immutable comparison case without writing beneath ``run_root``."""
    from gateway.exact_clone_process import review_prompt, vision_message

    root = run_root.expanduser().resolve(strict=True)
    checkpoint_path = root / "exact-clone-checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    if checkpoint.get("bestIteration") != iteration:
        raise ValueError(
            "benchmark iteration must be the checkpoint's retained best so the candidate and render match"
        )
    records = checkpoint.get("iterations")
    record = next(
        (
            item
            for item in records if isinstance(records, list)
            if isinstance(item, Mapping) and item.get("iteration") == iteration
        ),
        None,
    )
    if not isinstance(record, Mapping):
        raise ValueError(f"iteration {iteration} is not present in the checkpoint")
    candidate = checkpoint.get("bestCandidate")
    baseline = record.get("comparison")
    if not isinstance(candidate, Mapping) or not isinstance(baseline, Mapping):
        raise ValueError("benchmark checkpoint is missing candidate or comparison evidence")
    reciprocal = Path(str(checkpoint.get("reciprocalReference") or "")).resolve(strict=True)
    paths = [
        root / "previews" / "source.png",
        reciprocal,
        root / "previews" / f"iteration-{iteration:02d}-feed.png",
        root / "previews" / f"iteration-{iteration:02d}-story.png",
        root / "previews" / f"iteration-{iteration:02d}-feed-overlay.png",
        root / "previews" / f"iteration-{iteration:02d}-feed-difference.png",
        root / "previews" / f"iteration-{iteration:02d}-story-overlay.png",
        root / "previews" / f"iteration-{iteration:02d}-story-difference.png",
    ]
    for path in paths:
        if not path.is_file():
            raise ValueError(f"benchmark image is missing: {path.name}")
    prompt = review_prompt(
        final=False,
        candidate=candidate,
        reference=checkpoint.get("reference") or {},
        metrics=record.get("metrics") or {},
    )
    return {
        "candidate": candidate,
        "baseline": baseline,
        "message": vision_message(prompt, [str(path) for path in paths], bounded=True),
        "source_iteration": iteration,
        "run_id": root.name,
    }


def _catalog_metadata(model: str) -> dict[str, Any]:
    url = f"https://api.concentrate.ai/v1/models/{model}"
    request = urllib.request.Request(url, headers={"User-Agent": "Hermes-ad-template-benchmark/1"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except Exception as exc:
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}
    providers = payload.get("providers") if isinstance(payload, Mapping) else None
    compact_providers: dict[str, Any] = {}
    if isinstance(providers, Mapping):
        for provider, details in providers.items():
            if not isinstance(details, Mapping):
                continue
            pricing = details.get("pricing")
            supports = details.get("supports")
            compact_providers[str(provider)] = {
                "pricing": pricing,
                "context_window": details.get("context_window"),
                "max_output_tokens": details.get("max_output_tokens"),
                "supports": supports,
                "image_processing": details.get("image_processing"),
            }
    return {
        "available": True,
        "slug": payload.get("slug"),
        "providers": compact_providers,
    }


def _run_model(provider: str, model: str, case: Mapping[str, Any], max_tokens: int) -> dict[str, Any]:
    from gateway.tool_run_api import ToolRunAPIMixin
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from run_agent import AIAgent

    catalog = _catalog_metadata(model)
    started = time.monotonic()
    agent = None
    try:
        runtime = resolve_runtime_provider(requested=provider, target_model=model)
        kwargs = {
            key: runtime.get(key)
            for key in ("base_url", "api_key", "provider", "api_mode", "credential_pool")
        }
        kwargs.update(
            {
                "requested_provider": provider,
                "model": model,
                "max_iterations": 1,
                "max_tokens": max_tokens,
                "enabled_toolsets": [],
                "quiet_mode": True,
                "ephemeral_system_prompt": ToolRunAPIMixin._isolated_tool_role_prompt(),
                "session_id": f"ad-template-benchmark-{uuid.uuid4().hex}",
                "skip_context_files": True,
                "skip_memory": True,
                "skip_background_review": True,
                "session_db": None,
                "fallback_model": None,
                "request_overrides": {
                    "text": {
                        "format": {
                            "type": "json_schema",
                            "name": "ad_template_review",
                            "strict": True,
                            "schema": _REVIEW_JSON_SCHEMA,
                        }
                    }
                },
            }
        )
        agent = AIAgent(**kwargs)
        agent._persist_disabled = True
        agent._api_max_retries = 1
        agent._try_recover_primary_transport = lambda *_args, **_kwargs: False
        with tempfile.TemporaryDirectory(prefix="hermes-ad-template-benchmark-") as dump_root:
            agent.logs_dir = Path(dump_root)
            result = agent.run_conversation(
                user_message=case["message"],
                conversation_history=[],
                task_id="ad-template-read-only-benchmark",
            )
        text = _response_text(result).strip()
        if re.search(r"(?:HTTP\s+402|insufficient (?:funds|credits))", text, re.IGNORECASE):
            return {
                "model": model,
                "provider": provider,
                "ok": False,
                "strict_json": False,
                "latency_seconds": round(time.monotonic() - started, 3),
                "catalog": catalog,
                "error_type": "insufficient_credits",
                "error": "Concentrate rejected the read-only benchmark with HTTP 402.",
            }
        strict_json = False
        parsed: Any = None
        try:
            parsed = json.loads(text)
            strict_json = isinstance(parsed, dict)
        except (TypeError, json.JSONDecodeError):
            pass
        if not strict_json:
            parsed = ToolRunAPIMixin._tool_json_output(result)
        assessment = assess_comparator_result(
            parsed, baseline=case["baseline"], candidate=case["candidate"]
        )
        assessment["qualified"] = bool(strict_json and assessment["qualified"])
        return {
            "model": model,
            "provider": provider,
            "ok": True,
            "strict_json": strict_json,
            "latency_seconds": round(time.monotonic() - started, 3),
            "usage": {
                "input_tokens": getattr(agent, "session_prompt_tokens", None),
                "output_tokens": getattr(agent, "session_completion_tokens", None),
                "total_tokens": getattr(agent, "session_total_tokens", None),
                "estimated_cost_usd": getattr(agent, "session_estimated_cost_usd", None),
            },
            "catalog": catalog,
            "assessment": assessment,
        }
    except Exception as exc:
        return {
            "model": model,
            "provider": provider,
            "ok": False,
            "strict_json": False,
            "latency_seconds": round(time.monotonic() - started, 3),
            "catalog": catalog,
            "error_type": type(exc).__name__,
            "error": str(exc)[:2000],
        }
    finally:
        if agent is not None:
            try:
                agent.close()
            except Exception:
                pass


def _write_report(path: Path, report: Mapping[str, Any], run_root: Path) -> None:
    target = path.expanduser().resolve()
    root = run_root.expanduser().resolve()
    try:
        target.relative_to(root)
    except ValueError:
        pass
    else:
        raise ValueError("benchmark report must not be written inside the Tool-run workspace")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(target)


def _models(values: Iterable[str]) -> list[str]:
    result = list(dict.fromkeys(value.strip() for value in values if value.strip()))
    if not result:
        raise ValueError("at least one model is required")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--iteration", required=True, type=int)
    parser.add_argument("--provider", default="concentrate")
    parser.add_argument("--model", action="append", dest="models")
    parser.add_argument("--max-tokens", type=int, default=3500)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    # Custom-provider secrets remain in Hermes' existing credential file.  The
    # benchmark resolves them through the same runtime path as the gateway and
    # never includes them in its report.
    from dotenv import load_dotenv
    from hermes_constants import get_hermes_home

    load_dotenv(get_hermes_home() / ".env", override=False)
    case = load_benchmark_case(args.run_root, args.iteration)
    models = _models(args.models or DEFAULT_MODELS)
    results = [_run_model(args.provider, model, case, args.max_tokens) for model in models]
    qualified = [item["model"] for item in results if item.get("assessment", {}).get("qualified")]
    report = {
        "schema": "schema://hermes.ad-template-vision-benchmark/v1",
        "read_only": True,
        "run_id": case["run_id"],
        "source_iteration": case["source_iteration"],
        "provider": args.provider,
        "results": results,
        "qualified_models": qualified,
        "recommended_model": qualified[0] if qualified else None,
    }
    if args.output:
        _write_report(args.output, report, args.run_root)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if qualified else 2


if __name__ == "__main__":
    sys.exit(main())
