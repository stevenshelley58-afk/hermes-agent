from __future__ import annotations

import copy

from PIL import Image, ImageDraw
import pytest

from gateway.ad_template_output_qa import (
    AdTemplateOutputQaError,
    repair_known_output_qa,
    run_ad_output_qa,
    validate_output_qa,
)


CANVAS = {"feed": (1080, 1350), "story": (1080, 1920)}


def _candidate(*, badge: bool = False, cta: bool = False, label_opacity: float = 1) -> dict:
    def layout(placement: str) -> dict:
        width, height = CANVAS[placement]
        layers = [{
            "type": "plate", "layerId": f"{placement}-plate", "protected": True,
            "geometry": {"x": 0, "y": 0, "width": width, "height": height},
        }]
        if badge:
            layers.extend([
                {"type": "vector", "shape": "pill", "layerId": f"{placement}-badge", "colourRole": "background",
                 "geometry": {"x": 100, "y": 100, "width": 300, "height": 60}},
                {"type": "text", "layerId": f"{placement}-label", "inputKey": "eyebrow", "colourRole": "inverseText",
                 "alignment": "center", "opacity": label_opacity,
                 "geometry": {"x": 100, "y": 110, "width": 300, "height": 40}},
            ])
        if cta:
            layers.append({"type": "text", "layerId": f"{placement}-cta", "inputKey": "cta", "alignment": "center",
                           "geometry": {"x": 100, "y": 500, "width": 300, "height": 50}})
        return {"placement": placement, "layers": layers, "safeZones": []}

    return {"template": {
        "feedLayout": layout("feed"), "storyLayout": layout("story"),
        "semanticColours": {"background": "#000000", "inverseText": "#FFFFFF"},
    }}


def _render(path, placement: str, *, ink_y: int | None = None, ink_x: int = 140) -> None:
    """Render the native placement canvas with a protected black plate and badge."""
    image = Image.new("RGBA", CANVAS[placement], (0, 0, 0, 255))
    if ink_y is not None:
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((100, 100, 400, 160), radius=30, fill=(0, 0, 0, 255))
        draw.rectangle((ink_x, ink_y, ink_x + 220, ink_y + 22), fill=(255, 255, 255, 255))
    image.save(path)


def _native_renders(tmp_path, *, ink_y: int | None = 119, ink_x: int = 140) -> dict[str, object]:
    renders = {}
    for placement in CANVAS:
        path = tmp_path / f"{placement}.png"
        _render(path, placement, ink_y=ink_y, ink_x=ink_x)
        renders[placement] = path
    return renders


def _replace_label_input(candidate: dict, value: str) -> None:
    for placement in CANVAS:
        candidate["template"][f"{placement}Layout"]["layers"][2]["inputKey"] = value


def test_native_feed_and_story_centered_badge_ink_passes(tmp_path):
    result = run_ad_output_qa(_candidate(badge=True), _native_renders(tmp_path, ink_y=119))

    assert result["status"] == "pass"
    assert all(item["status"] == "pass" for placement in CANVAS for item in result["checks"][placement]["controlInk"])


def test_badge_ink_above_center_fails_and_known_repair_is_immutable(tmp_path):
    candidate = _candidate(badge=True)
    result = run_ad_output_qa(candidate, _native_renders(tmp_path, ink_y=110))

    assert result["status"] == "fail"
    assert all(item["status"] == "fail" for placement in CANVAS for item in result["checks"][placement]["controlInk"])
    repaired, changes = repair_known_output_qa(candidate, result)
    assert {change["placement"] for change in changes} == set(CANVAS)
    assert all(repaired["template"][f"{placement}Layout"]["layers"][2]["geometry"]["y"] > 110 for placement in CANVAS)
    assert all(repaired["template"][f"{placement}Layout"]["layers"][2]["geometry"]["y"] < 120 for placement in CANVAS)
    assert all(candidate["template"][f"{placement}Layout"]["layers"][2]["geometry"]["y"] == 110 for placement in CANVAS)


def test_horizontally_shifted_badge_ink_fails(tmp_path):
    result = run_ad_output_qa(_candidate(badge=True), _native_renders(tmp_path, ink_x=150))

    assert result["status"] == "fail"
    assert all(item["moveLabelRightByPx"] < -6 for placement in CANVAS for item in result["checks"][placement]["controlInk"])


def test_opaque_native_canvases_without_controls_pass(tmp_path):
    assert run_ad_output_qa(_candidate(), _native_renders(tmp_path, ink_y=None))["status"] == "pass"


@pytest.mark.parametrize("renders", [None, {}])
def test_missing_renders_are_unknown_never_pass(renders):
    result = run_ad_output_qa(_candidate(), renders)

    assert result["status"] == "needs_review"
    assert any(item.get("status") == "unknown" for item in result["findings"])


def test_unreadable_render_is_unknown_never_pass(tmp_path):
    corrupt = tmp_path / "not-an-image.png"
    corrupt.write_text("not a png", encoding="utf-8")
    result = run_ad_output_qa(_candidate(), {"feed": corrupt, "story": corrupt})

    assert result["status"] != "pass"
    assert any(item.get("status") == "unknown" for item in result["findings"])


@pytest.mark.parametrize("input_key", ["button_label", "rectangular_label"])
def test_informational_labels_are_not_embedded_ctas(tmp_path, input_key):
    candidate = _candidate(badge=True)
    _replace_label_input(candidate, input_key)

    result = run_ad_output_qa(candidate, _native_renders(tmp_path))
    assert result["status"] == "pass"
    assert not any(item["category"] == "meta-embedded-cta" for item in result["findings"])


def test_explicit_cta_input_is_rejected(tmp_path):
    result = run_ad_output_qa(_candidate(cta=True), _native_renders(tmp_path, ink_y=None))

    assert result["status"] == "fail"
    assert {item["placement"] for item in result["findings"] if item["category"] == "meta-embedded-cta"} == set(CANVAS)


def test_translucent_badge_label_is_unknown_not_pass(tmp_path):
    result = run_ad_output_qa(_candidate(badge=True, label_opacity=0.5), _native_renders(tmp_path))

    assert result["status"] == "needs_review"
    assert all(item["status"] == "unknown" for placement in CANVAS for item in result["checks"][placement]["controlInk"])


def test_badge_without_measurable_background_colour_is_unknown_not_pass(tmp_path):
    candidate = _candidate(badge=True)
    del candidate["template"]["semanticColours"]["background"]
    result = run_ad_output_qa(candidate, _native_renders(tmp_path))

    assert result["status"] == "needs_review"
    assert all(item["status"] == "unknown" for placement in CANVAS for item in result["checks"][placement]["controlInk"])


def test_validate_rejects_contradictory_pass_with_failing_findings():
    result = {
        "version": "ad-output-qa-v1", "status": "pass",
        "findings": [{"category": "control-ink", "status": "fail", "reason": "measured off-centre"}],
    }

    with pytest.raises(AdTemplateOutputQaError):
        validate_output_qa(result)


@pytest.mark.parametrize("style", [
    {"opacity": 0.5}, {"fill": {"type": "linear_gradient"}},
    {"effects": {"rotationDegrees": 5}},
])
def test_unsupported_background_is_unknown_not_a_measured_pass(tmp_path, style):
    candidate = _candidate(badge=True)
    for placement in CANVAS:
        candidate["template"][f"{placement}Layout"]["layers"][1].update(style)
    result = run_ad_output_qa(candidate, _native_renders(tmp_path))
    assert result["status"] == "needs_review"
    assert all(item["status"] == "unknown" for placement in CANVAS for item in result["checks"][placement]["controlInk"])


def test_stale_repair_evidence_does_not_move_a_changed_badge(tmp_path):
    result = run_ad_output_qa(_candidate(badge=True), _native_renders(tmp_path, ink_y=110))
    changed = _candidate(badge=True)
    for placement in CANVAS:
        changed["template"][f"{placement}Layout"]["layers"][1]["geometry"]["x"] = 500

    repaired, changes = repair_known_output_qa(changed, result)

    assert changes == []
