"""Focused contracts for isolated durable Tool role model calls."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

from gateway.tool_run_api import ToolRunAPIMixin


def test_catalog_basename_normalization_is_unambiguous_and_consistent(monkeypatch):
    from gateway import exact_clone_process as process
    a = SimpleNamespace(file_name="interior/living.webp", mime_type="image/webp")
    b = SimpleNamespace(file_name="other/living.webp", mime_type="image/webp")
    catalog = SimpleNamespace(assets={a.file_name: a})
    monkeypatch.setattr(process, "_runtime_catalog", lambda: catalog)
    candidate = {"assets": [{"assetKey": "photo", "fileName": "living.webp", "mimeType": "image/webp"}],
                 "template": {"assets": {"photo": {"fileName": "living.webp", "mimeType": "image/webp"}}}}
    result = process._canonical_catalog_paths(candidate)
    assert result["assets"][0]["fileName"] == "interior/living.webp"
    assert result["template"]["assets"]["photo"]["fileName"] == "interior/living.webp"
    assert candidate["assets"][0]["fileName"] == "living.webp"
    catalog.assets[b.file_name] = b
    assert process._canonical_catalog_paths(candidate) == candidate
    catalog.assets.clear()
    assert process._canonical_catalog_paths(candidate) == candidate
    catalog.assets[a.file_name] = a
    candidate["template"]["assets"]["photo"]["fileName"] = "different.webp"
    assert process._canonical_catalog_paths(candidate) == candidate


def _usage(input_tokens, output_tokens, cached_tokens=0):
    return SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        input_tokens_details=SimpleNamespace(cached_tokens=cached_tokens),
        output_tokens_details=SimpleNamespace(reasoning_tokens=0))


def test_roles_use_distinct_strict_response_schemas():
    comparator = ToolRunAPIMixin._tool_role_json_schema("comparator-1")
    reviewer = ToolRunAPIMixin._tool_role_json_schema("final-reviewer-a-run-1")
    builder = ToolRunAPIMixin._tool_role_json_schema("builder-initial")
    assert "patch" in comparator["required"]
    assert "patch" not in reviewer["properties"]
    assert builder["required"] == ["template", "assets"]
    assert all(item["additionalProperties"] is False for item in (comparator, reviewer, builder))


def test_builder_schema_rejects_missing_or_wrong_placement_layout():
    import jsonschema
    import pytest
    schema = ToolRunAPIMixin._tool_role_json_schema("builder-initial")
    layout = schema["properties"]["template"]["properties"]["feedLayout"]
    valid = {"placement": "feed", "layers": [], "safeZones": []}
    jsonschema.validate(valid, layout)
    for invalid in (
        {"placement": "4:5", "layers": [], "safeZones": []},
        {"placement": "feed", "layers": []},
        {"placement": "feed", "layers": [], "safeZones": {}},
    ):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(invalid, layout)
    story = schema["properties"]["template"]["properties"]["storyLayout"]
    jsonschema.validate({**valid, "placement": "story"}, story)
    declaration = schema["properties"]["assets"]["items"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"fileName": "photo.png"}, declaration)


def test_multimodal_role_input_is_native_responses_shape():
    result = ToolRunAPIMixin._tool_responses_input([
        {"type": "text", "text": "return JSON"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
    ])
    assert [part["type"] for part in result[0]["content"]] == ["input_text", "input_image"]
    assert "ROLE CONTRACT" in result[0]["content"][0]["text"]


def test_meta_contributor_usage_has_audited_cost():
    result = ToolRunAPIMixin._tool_response_usage(
        SimpleNamespace(usage=_usage(1_000, 100, 200)),
        provider="meta-direct", model="muse-spark-1.3-contributor",
        base_url="https://api.meta.ai/v1", api_key=None)
    assert result["cost_status"] == "estimated"
    assert result["estimated_cost_usd"] == 0.0001004
    assert result["total_tokens"] == 1_100


def test_concentrate_gemini_38_uses_catalog_price():
    result = ToolRunAPIMixin._tool_response_usage(
        SimpleNamespace(usage=_usage(1_000, 100, 200)), provider="concentrate",
        model="gemini-3.8-flash", base_url="https://api.concentrate.ai/v1", api_key=None)
    assert result["cost_status"] == "estimated"
    assert result["estimated_cost_usd"] == 0.001125


def test_concentrate_gemini_uses_shared_google_pricing():
    result = ToolRunAPIMixin._tool_response_usage(
        SimpleNamespace(usage=_usage(1_000, 100)), provider="concentrate",
        model="gemini-2.5-pro", base_url="https://api.concentrate.ai/v1", api_key=None)
    assert result["cost_status"] == "estimated"
    assert result["estimated_cost_usd"] == 0.00225


def test_execute_path_does_not_construct_conversational_agent():
    source = inspect.getsource(ToolRunAPIMixin._execute_tool_run)
    assert "client.responses.create(" in source
    assert "self._create_agent(" not in source
    assert '"strict": True' in source
