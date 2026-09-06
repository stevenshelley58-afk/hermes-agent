from __future__ import annotations

import io
import json

from PIL import Image

from gateway.exact_clone_photo_qa import (
    materialize_source_photo_plan,
    source_photo_overrides,
)


def _save_source(path, *, photo=(220, 20, 20), side=(10, 10, 10)):
    image = Image.new("RGB", (100, 50), side)
    image.paste(photo, (0, 0, 60, 50))
    image.save(path)


def _map(ocr=None):
    return {"canvas": {"width": 100, "height": 50}, "ocr": ocr or []}


def _region(**changes):
    value = {
        "sourceRole": "image-hero",
        "bounds": {"x": 0, "y": 0, "width": 60, "height": 50},
        "confidence": 0.99,
        "textFree": True,
    }
    value.update(changes)
    return value


def _layer(**changes):
    value = {
        "inputKey": "hero",
        "geometry": {"x": 0, "y": 0, "width": 60, "height": 50},
    }
    value.update(changes)
    return value


def _pixel(payload):
    with Image.open(io.BytesIO(payload)) as image:
        return image.convert("RGB").getpixel((0, 0))


def test_photo_plan_freezes_source_bytes_before_candidate_geometry_changes(tmp_path):
    source = tmp_path / "source.png"
    _save_source(source)
    plan = materialize_source_photo_plan(
        source=source, source_map=_map(), source_regions=[_region()],
        source_layers=[_layer()], workspace=tmp_path / "run",
    )
    original = source_photo_overrides(plan)["hero"]
    _save_source(source, photo=(1, 2, 3))
    changed_candidate_geometry = {"x": 60, "y": 0, "width": 40, "height": 50}
    assert changed_candidate_geometry != _layer()["geometry"]
    assert source_photo_overrides(plan)["hero"] == original
    assert _pixel(original) == (220, 20, 20)


def test_missing_or_low_confidence_regions_are_skipped_not_cropped(tmp_path):
    source = tmp_path / "source.png"
    _save_source(source)
    plan = materialize_source_photo_plan(
        source=source, source_map=_map(),
        source_regions=[_region(confidence=0.94)],
        source_layers=[_layer()], workspace=tmp_path / "run",
    )
    assert source_photo_overrides(plan) == {}
    assert json.loads(plan.read_text())["bindings"]["hero"] == {"kind": "skipped"}


def test_non_finite_region_bounds_are_rejected_not_crashing(tmp_path):
    source = tmp_path / "source.png"
    _save_source(source)
    plan = materialize_source_photo_plan(
        source=source, source_map=_map(),
        source_regions=[
            _region(bounds={"x": float("nan"), "y": 0, "width": 60, "height": 50}),
            _region(sourceRole="image-other", bounds={"x": 0, "y": 0, "width": float("inf"), "height": 50}),
        ],
        source_layers=[_layer()], workspace=tmp_path / "run",
    )
    assert source_photo_overrides(plan) == {}
    assert json.loads(plan.read_text())["bindings"]["hero"] == {"kind": "skipped"}


def test_ocr_overlap_rejects_even_high_confidence_text_free_region(tmp_path):
    source = tmp_path / "source.png"
    _save_source(source)
    plan = materialize_source_photo_plan(
        source=source,
        source_map=_map([{"x": 50, "y": 10, "width": 8, "height": 12}]),
        source_regions=[_region()],
        source_layers=[_layer()], workspace=tmp_path / "run",
    )
    assert source_photo_overrides(plan) == {}


def test_one_to_one_mapping_uses_original_source_layer_overlap_not_target_coordinates(tmp_path):
    source = tmp_path / "source.png"
    image = Image.new("RGB", (100, 50), "black")
    image.paste("red", (0, 0, 40, 50))
    image.paste("blue", (60, 0, 100, 50))
    image.save(source)
    plan = materialize_source_photo_plan(
        source=source, source_map=_map(),
        source_regions=[
            _region(bounds={"x": 0, "y": 0, "width": 40, "height": 50}),
            _region(bounds={"x": 60, "y": 0, "width": 40, "height": 50}),
        ],
        source_layers=[
            _layer(inputKey="left", geometry={"x": 0, "y": 0, "width": 40, "height": 50}),
            _layer(inputKey="right", geometry={"x": 60, "y": 0, "width": 40, "height": 50}),
        ],
        workspace=tmp_path / "run",
    )
    overrides = source_photo_overrides(plan)
    assert _pixel(overrides["left"]) == (255, 0, 0)
    assert _pixel(overrides["right"]) == (0, 0, 255)


def test_verified_photo_crop_excludes_adjacent_baked_sidebar_glyphs(tmp_path):
    source = tmp_path / "source.png"
    _save_source(source, photo=(220, 20, 20), side=(0, 0, 0))
    plan = materialize_source_photo_plan(
        source=source, source_map=_map(), source_regions=[_region()],
        source_layers=[_layer()], workspace=tmp_path / "run",
    )
    with Image.open(io.BytesIO(source_photo_overrides(plan)["hero"])) as crop:
        assert crop.size == (60, 50)
        assert set(crop.convert("RGB").getdata()) == {(220, 20, 20)}

