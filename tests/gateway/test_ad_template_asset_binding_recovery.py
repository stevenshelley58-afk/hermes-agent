"""Regression coverage for pure asset-binding validation inside patch retries."""

import copy

from PIL import Image
import pytest

import gateway.ad_template_generator_process as process
from tests.gateway.test_ad_template_generator_process import _generator_asset_candidate


def _invalid_binding():
    candidate = _generator_asset_candidate()
    candidate["template"]["metadata"]["replacementAssets"][1]["assetKey"] = "missing-brand"
    return candidate


def _source(tmp_path):
    path = tmp_path / "source.png"
    Image.new("RGB", (20, 20), "white").save(path)
    return str(path)


def test_binding_error_is_retried_before_candidate_or_checkpoint_is_kept(tmp_path):
    candidate = _invalid_binding()
    original = copy.deepcopy(candidate)
    process.persist_checkpoint(tmp_path, {"candidate": original})
    before_checkpoint = (tmp_path / "exact-clone-checkpoint.json").read_bytes()
    calls, prompts, events = [], [], []

    def call_agent(instance, prompt, _route):
        calls.append(instance)
        prompts.append(prompt[0]["text"])
        if len(calls) == 1:
            return {"operations": [{"op": "replace", "path": "/template/metadata/description", "value": "rejected change"}]}
        return {"operations": [{"op": "replace", "path": "/template/metadata/replacementAssets/1/assetKey", "value": "brand-default"}]}

    _, repaired = process._call_applied_patch(
        call_agent, instance="asset-binding-repair", prompt="Repair the invalid binding.",
        paths=[_source(tmp_path)], route={"provider": "test", "model": "builder"},
        candidate=candidate, strict=False,
        validate_candidate=process._normalize_generator_asset_bindings,
        emit=lambda kind, _node, data: events.append((kind, data)),
    )
    assert calls == ["asset-binding-repair", "asset-binding-repair-format-retry"]
    assert "replacementAssets binding is undeclared" in prompts[1]
    assert repaired["template"]["metadata"]["description"] == original["template"]["metadata"]["description"]
    assert repaired["template"]["metadata"]["replacementAssets"][1]["assetKey"] == "brand-default"
    assert next(item for item in repaired["template"]["imageInputs"] if item["key"] == "brand")["defaultAssetKey"] == "brand-default"
    assert repaired["assets"] == original["assets"]
    assert repaired["template"]["assets"] == original["template"]["assets"]
    assert candidate == original
    assert (tmp_path / "exact-clone-checkpoint.json").read_bytes() == before_checkpoint
    assert not (tmp_path / "demo-assets").exists()
    assert events[0][0] == "role.output-retried"


def test_unresolved_bindings_exhaust_existing_bounds_without_mutation(tmp_path):
    candidate = _invalid_binding()
    original = copy.deepcopy(candidate)
    calls = []

    def call_agent(instance, _prompt, _route):
        calls.append(instance)
        return {"operations": [{"op": "replace", "path": "/template/metadata/description", "value": "still not repaired"}]}

    with pytest.raises(process.AdTemplateProcessError, match="exhausted bounded patch replans.*replacementAssets"):
        process._call_applied_patch(
            call_agent, instance="asset-binding-repair", prompt="Repair the binding.",
            paths=[_source(tmp_path)], route={"provider": "test", "model": "builder"},
            candidate=candidate, strict=False,
            validate_candidate=process._normalize_generator_asset_bindings,
            emit=lambda *_args: None,
        )
    assert len(calls) == (process.MAX_PATCH_REPLANS + 1) * (process.MAX_OUTPUT_RETRIES + 1)
    assert candidate == original
    assert not (tmp_path / "demo-assets").exists()


def test_asset_validator_cannot_bypass_immutable_asset_definitions(tmp_path):
    candidate = _generator_asset_candidate()
    original = copy.deepcopy(candidate)
    validated = []

    def validate(value):
        validated.append(value)
        return value

    def call_agent(_instance, _prompt, _route):
        return {"operations": [{"op": "replace", "path": "/template/assets/brand-default/fileName", "value": "different.png"}]}

    with pytest.raises(process.AdTemplateProcessError, match="asset|immutable"):
        process._call_applied_patch(
            call_agent, instance="asset-binding-repair", prompt="Repair the binding.",
            paths=[_source(tmp_path)], route={"provider": "test", "model": "builder"},
            candidate=candidate, strict=False, validate_candidate=validate,
            emit=lambda *_args: None,
        )
    assert validated == []
    assert candidate == original


def test_initial_builder_repairs_all_missing_declarations_before_freezing(tmp_path):
    valid = _generator_asset_candidate()
    invalid = copy.deepcopy(valid)
    invalid["assets"] = []
    invalid["template"]["assets"] = {}
    calls, prompts = [], []

    def build(instance, prompt, route):
        calls.append(instance)
        prompts.append(str(prompt))
        return invalid if len(calls) == 1 else valid

    result = process._call_json(
        build, instance="builder-initial", prompt="Build the complete template.",
        paths=[_source(tmp_path)], route={"provider": "test", "model": "builder"},
        validate=process._candidate_envelope, emit=lambda *_args: None,
    )
    assert calls == ["builder-initial", "builder-initial-format-retry"]
    assert "hero-default" in prompts[1] and "brand-default" in prompts[1]
    assert "complete initial document" in prompts[1]
    assert result == valid
    assert invalid["assets"] == []
