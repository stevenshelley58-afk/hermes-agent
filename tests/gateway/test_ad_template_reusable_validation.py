from pathlib import Path
import pytest
from gateway.ad_template_reusable_validation import (
    ReusableTemplateValidationError,
    validate_reusable_template,
)


def candidate():
    template = {
        "textInputs": [{"key": "headline", "maxLength": 24, "placeholder": "Hello"}],
        "imageInputs": [
            {"key": "hero", "required": True, "defaultAssetKey": "hero-asset"}
        ],
        "assets": {"hero-asset": {"fileName": "hero.png", "mimeType": "image/png"}},
        "feedLayout": {
            "layers": [
                {"type": "plate", "layerId": "feed-bg"},
                {
                    "type": "image_slot",
                    "layerId": "feed-hero",
                    "inputKey": "hero",
                    "defaultCrop": {"x": 0, "y": 0, "width": 1, "height": 1},
                },
            ]
        },
        "storyLayout": {
            "layers": [
                {"type": "plate", "layerId": "story-bg"},
                {
                    "type": "image_slot",
                    "layerId": "story-hero",
                    "inputKey": "hero",
                    "defaultCrop": {"x": 0, "y": 0, "width": 1, "height": 1},
                },
            ]
        },
    }
    return {
        "template": template,
        "assets": [
            {"assetKey": "hero-asset", "fileName": "hero.png", "mimeType": "image/png"}
        ],
    }


def renderer(candidate, workspace, **kwargs):
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "feed.png").write_bytes(b"feed")
    (workspace / "story.png").write_bytes(b"story")
    return {
        "render": {
            "feed": str(workspace / "feed.png"),
            "story": str(workspace / "story.png"),
        }
    }


def test_valid_variants_and_crop_are_rendered(tmp_path):
    calls = []

    def render(c, w, **kwargs):
        calls.append(c)
        return renderer(c, w, **kwargs)

    evidence = validate_reusable_template(
        candidate(), workspace=tmp_path, render=render, renderer_identity="renderer@1"
    )
    assert evidence["status"] == "passed"
    assert [x["name"] for x in evidence["scenarios"]] == [
        "short",
        "max",
        "unicode",
        "optional-empty",
    ]
    assert len(calls) == 4
    assert calls[2]["template"]["feedLayout"]["layers"][1]["defaultCrop"]["x"] == 0.05


def test_cached_passes_are_reused(tmp_path):
    calls = []

    def render(c, w, **kwargs):
        calls.append(c)
        return renderer(c, w, **kwargs)

    first = validate_reusable_template(
        candidate(), workspace=tmp_path, render=render, renderer_identity="renderer@1"
    )
    second = validate_reusable_template(
        candidate(),
        workspace=tmp_path,
        render=render,
        cached=first,
        renderer_identity="renderer@1",
    )
    assert len(calls) == 4 and second["scenarios"] == first["scenarios"]


def test_undeclared_required_binding_fails_closed(tmp_path):
    value = candidate()
    value["template"]["imageInputs"][0].pop("defaultAssetKey")
    with pytest.raises(
        ReusableTemplateValidationError, match="required image input hero"
    ):
        validate_reusable_template(value, workspace=tmp_path, render=renderer)


def test_renderer_failure_has_concrete_scenario_error(tmp_path):
    def fail(c, w, **kwargs):
        raise RuntimeError("text overflow in Story headline")

    with pytest.raises(ReusableTemplateValidationError, match="text overflow"):
        validate_reusable_template(
            candidate(), workspace=tmp_path, render=fail, renderer_identity="renderer@1"
        )


def test_stop_interrupts_before_next_renderer_call(tmp_path):
    calls = []
    def render(c, w, **kwargs):
        calls.append(c)
        return renderer(c, w, **kwargs)
    def stop():
        if calls:
            raise RuntimeError("cancelled by operator")
    with pytest.raises(RuntimeError, match="cancelled by operator"):
        validate_reusable_template(candidate(), workspace=tmp_path, render=render, check_stop=stop)
    assert len(calls) == 1


def test_failed_render_retains_counted_evidence(tmp_path):
    def fail(c, w, **kwargs):
        raise RuntimeError("bad crop")
    with pytest.raises(ReusableTemplateValidationError) as failure:
        validate_reusable_template(candidate(), workspace=tmp_path, render=fail)
    evidence = failure.value.evidence
    assert evidence["status"] == "failed"
    assert evidence["counts"] == {"total": 1, "passed": 0, "failed": 1}
    assert evidence["results"][0]["error"] == "bad crop"


def test_asset_change_invalidates_cached_renders(tmp_path):
    calls = []
    def render(c, w, **kwargs):
        calls.append(c)
        return renderer(c, w, **kwargs)
    first = validate_reusable_template(candidate(), workspace=tmp_path, render=render, asset_overrides={"hero": b"old"})
    validate_reusable_template(candidate(), workspace=tmp_path, render=render, asset_overrides={"hero": b"new"}, cached=first)
    assert len(calls) == 8
