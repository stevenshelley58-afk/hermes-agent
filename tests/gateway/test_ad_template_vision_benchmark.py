from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest


_SCRIPT = Path(__file__).parents[2] / "scripts" / "ad_template_vision_benchmark.py"
_SPEC = importlib.util.spec_from_file_location("ad_template_vision_benchmark", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
benchmark = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(benchmark)


def _candidate():
    return {
        "template": {
            "feedLayout": {"layers": [{"layerId": "f-website"}]},
            "storyLayout": {"layers": [{"layerId": "s-website"}]},
        }
    }


def _review(*, score=8.6, layer_ids=None):
    return {
        "decision": "revise",
        "scores": {
            "overall": score,
            "geometry": 9.3,
            "typography": 8.2,
            "colourEffects": 9.6,
            "imageCrop": 9.8,
            "details": 7.2,
        },
        "issues": [
            {
                "placement": "both",
                "layerIds": layer_ids or ["f-website", "s-website"],
                "category": "geometry",
                "instruction": "Set x to 765 and width to 186.",
                "severity": "material",
            }
        ],
        "warnings": [],
        "effects": {
            "shading": "not_present",
            "gradients": "not_present",
            "shadows": "not_present",
            "transparency": "match",
            "borders": "match",
            "masks": "match",
            "texture": "not_present",
        },
        "fontSubstitution": None,
    }


def test_assessment_qualifies_a_precise_revision():
    result = benchmark.assess_review(
        _review(), baseline=_review(), candidate=_candidate(), validate=lambda value: value
    )
    assert result["qualified"] is True
    assert result["revision_accurate"] is True
    assert result["known_issue_recall"] == 1.0
    assert result["invalid_layer_ids"] == []


def test_assessment_rejects_unknown_layers_and_vague_changes():
    review = _review(layer_ids=["invented-layer"])
    review["issues"][0]["instruction"] = "Make this align better."
    result = benchmark.assess_review(
        review, baseline=_review(), candidate=_candidate(), validate=lambda value: value
    )
    assert result["qualified"] is False
    assert result["known_issue_recall"] == 0.0
    assert result["patchable_issue_ratio"] == 0.0
    assert result["invalid_layer_ids"] == ["invented-layer"]


def test_report_cannot_modify_the_tool_run_workspace(tmp_path):
    run_root = tmp_path / "run"
    run_root.mkdir()
    with pytest.raises(ValueError, match="must not be written"):
        benchmark._write_report(run_root / "benchmark.json", {}, run_root)


def test_response_text_exposes_provider_errors_without_credentials():
    text = benchmark._response_text({"final_response": "HTTP 402 insufficient credits"})
    assert text == "HTTP 402 insufficient credits"


def test_comparator_assessment_requires_an_applicable_patch(monkeypatch):
    valid = _review()
    valid["patch"] = {
        "operations": [
            {
                "op": "replace",
                "path": "/template/feedLayout/layers/0/x",
                "value": 765,
            }
        ]
    }

    def fake_validate(value, *, candidate):
        return {
            "review": {key: item for key, item in value.items() if key != "patch"},
            "patch": value["patch"],
            "candidate": candidate,
            "patchError": None,
        }

    process = types.ModuleType("gateway.exact_clone_process")
    process.validate_comparator_result = fake_validate
    process.validate_review = lambda value: value
    monkeypatch.setitem(sys.modules, "gateway.exact_clone_process", process)
    result = benchmark.assess_comparator_result(
        valid, baseline=_review(), candidate=_candidate()
    )
    assert result["qualified"] is True
    assert result["patch_valid"] is True
    assert result["patch_operations"] == 1


def test_catalog_json_schema_capability_reaches_benchmark_agent(monkeypatch):
    captured = {}

    class FakeAgent:
        def __init__(self, **_kwargs):
            self.supports_responses_text_format = False
            self.session_prompt_tokens = 1
            self.session_completion_tokens = 1
            self.session_total_tokens = 2
            self.session_estimated_cost_usd = 0.0

        def run_conversation(self, **_kwargs):
            captured["enabled"] = self.supports_responses_text_format
            return {"final_response": "{}"}

    runtime_provider = types.ModuleType("hermes_cli.runtime_provider")
    runtime_provider.resolve_runtime_provider = lambda **_kwargs: {
        "base_url": "https://example.invalid/v1",
        "api_key": "test",
        "provider": "custom",
        "api_mode": "codex_responses",
        "credential_pool": None,
    }
    run_agent = types.ModuleType("run_agent")
    run_agent.AIAgent = FakeAgent
    tool_api = types.ModuleType("gateway.tool_run_api")
    tool_api.ToolRunAPIMixin = types.SimpleNamespace(
        _isolated_tool_role_prompt=lambda: "benchmark",
        _tool_json_output=lambda value: value,
    )
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", runtime_provider)
    monkeypatch.setitem(sys.modules, "run_agent", run_agent)
    monkeypatch.setitem(sys.modules, "gateway.tool_run_api", tool_api)
    monkeypatch.setattr(
        benchmark,
        "_catalog_metadata",
        lambda _model: {
            "available": True,
            "providers": {
                "route": {
                    "supports": {"text": {"format": {"json_schema": True}}}
                }
            },
        },
    )
    monkeypatch.setattr(
        benchmark,
        "assess_comparator_result",
        lambda *_args, **_kwargs: {"qualified": False},
    )

    benchmark._run_model("concentrate", "qwen", {"message": [], "baseline": {}, "candidate": {}}, 100)
    assert captured["enabled"] is True
