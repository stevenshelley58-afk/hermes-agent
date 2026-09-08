import copy
import io

from PIL import Image
import pytest

from gateway.ad_template_production_repair import repair_production_candidate
from gateway.ad_template_runtime import AdTemplateProcessError


def fixture(width=940, height=90):
    image = io.BytesIO()
    Image.new("RGB", (1200, 800), "white").save(image, format="PNG")
    return {"template": {
        "imageInputs": [{"key": "room", "defaultAssetKey": "photo"}],
        "storyLayout": {"layers": [{
            "type": "image_slot", "layerId": "gallery", "inputKey": "room",
            "geometry": {"x": 70, "y": 1460, "width": width, "height": height},
            "defaultCrop": {"x": 0, "y": 0, "width": 1, "height": 1},
            "mask": "none", "cornerRadius": 24,
        }]},
    }}, {"photo": image.getvalue()}


@pytest.mark.parametrize("width,height", [(940, 90), (200, 500), (600, 400)])
def test_crop_preserves_image_proportions_without_moving_slot(width, height):
    candidate, assets = fixture(width, height)
    before = copy.deepcopy(candidate)
    repaired, changes = repair_production_candidate(candidate, assets)
    layer = repaired["template"]["storyLayout"]["layers"][0]
    crop = layer["defaultCrop"]
    assert crop["width"] * 1200 / (crop["height"] * 800) == pytest.approx(width / height)
    assert crop["x"] + crop["width"] / 2 == pytest.approx(0.5)
    assert crop["y"] + crop["height"] / 2 == pytest.approx(0.5)
    assert layer["geometry"] == before["template"]["storyLayout"]["layers"][0]["geometry"]
    assert layer["mask"] == "rounded_rect"
    assert candidate == before
    again, next_changes = repair_production_candidate(repaired, assets)
    assert again == repaired and not next_changes
    assert changes


def test_crop_region_is_respected_and_invalid_crop_fails_closed():
    candidate, assets = fixture()
    layer = candidate["template"]["storyLayout"]["layers"][0]
    layer["defaultCrop"] = {"x": 0.2, "y": 0.1, "width": 0.6, "height": 0.8}
    repaired, _ = repair_production_candidate(candidate, assets)
    crop = repaired["template"]["storyLayout"]["layers"][0]["defaultCrop"]
    assert crop["x"] >= 0.2 and crop["x"] + crop["width"] <= 0.8 + 1e-9
    assert crop["y"] >= 0.1 and crop["y"] + crop["height"] <= 0.9 + 1e-9
    layer["defaultCrop"]["width"] = 4
    with pytest.raises(AdTemplateProcessError, match="in-bounds"):
        repair_production_candidate(candidate, assets)


def test_zero_radius_removes_pill_but_does_not_invent_rounding():
    candidate = {"template": {"feedLayout": {"layers": [
        {"type": "vector", "layerId": "button", "shape": "pill", "cornerRadius": 0},
        {"type": "vector", "layerId": "intentional", "shape": "pill"},
    ]}}}
    repaired, changes = repair_production_candidate(candidate, {})
    assert repaired["template"]["feedLayout"]["layers"][0]["shape"] == "rect"
    assert repaired["template"]["feedLayout"]["layers"][1]["shape"] == "pill"
    assert len(changes) == 1
