from __future__ import annotations

from pathlib import Path

from PIL import Image
import pytest

from gateway.ad_template_runtime import AdTemplateProcessError
from gateway.ad_template_generator_layer_refinement import (
    build_refinement_batch_contract,
    build_refinement_contract,
    refinement_prompt,
    validate_refinement_patch,
    validate_text_ink_regression,
    write_matching_crops,
)


def _candidate():
    def text(layer_id, input_key, y, placement):
        return {
            "type": "text",
            "layerId": layer_id,
            "inputKey": input_key,
            "font": {"file": "/fonts/adstudio/manrope-400.woff2"},
            "fontSize": 24 if placement == "feed" else 32,
            "lineHeight": 1.2,
            "tracking": 0,
            "alignment": "left",
            "maxCharacters": 200,
            "maxLines": 6,
            "colourRole": "mainText",
            "overflowBehaviour": "truncate",
            "geometry": {"x": 80, "y": y, "width": 420, "height": 220},
        }

    return {
        "template": {
            "feedLayout": {
                "layers": [text("feed-features", "features", 800, "feed")]
            },
            "storyLayout": {
                "layers": [text("story-features", "features", 1200, "story")]
            },
            "textInputs": [
                {
                    "key": "features",
                    "label": "Features",
                    "placeholder": "One\nTwo\nThree\nFour\nFive\nSix",
                    "maxLength": 200,
                }
            ],
            "fonts": [{"file": "/fonts/adstudio/manrope-400.woff2"}],
        },
        "assets": [],
    }


def _issues():
    return [
        {
            "placement": "feed",
            "layerIds": ["feed-features"],
            "category": "geometry",
            "instruction": "Set x to 90 and width to 440; keep six bullet lines editable.",
            "severity": "material",
        }
    ]


def test_contract_locks_real_layer_properties_and_shared_text_dependency():
    contract = build_refinement_contract(
        _candidate(),
        _issues(),
        source_placement="feed",
        available_fonts=["/fonts/adstudio/manrope-700.woff2"],
    )

    layer = contract["layers"]["feed-features"]
    assert layer["allowedProperties"] == ["geometry/width", "geometry/x"]
    assert layer["numericTargets"] == {
        "geometry/x": 90.0,
        "geometry/width": 440.0,
    }
    assert contract["textInputDependencies"] == [
        {
            "inputKey": "features",
            "pointer": "/template/textInputs/0",
            "allowedProperties": ["placeholder", "maxLength"],
            "sharedLayerIds": ["feed-features", "story-features"],
        }
    ]
    assert "six distinct source bullet lines" in refinement_prompt(contract)


def test_contract_rejects_review_invented_layer_id():
    with pytest.raises(AdTemplateProcessError, match="unknown layer IDs"):
        build_refinement_contract(
            _candidate(),
            [{**_issues()[0], "layerIds": ["invented"]}],
            source_placement="feed",
            available_fonts=[],
        )


def test_contract_trims_invented_layer_ids_from_mixed_issue():
    issue = {**_issues()[0], "layerIds": ["feed-features", "invented"]}
    contract = build_refinement_contract(
        _candidate(),
        [issue],
        source_placement="feed",
        available_fonts=[],
    )
    assert contract["issues"] == [{**issue, "layerIds": ["feed-features"]}]


def test_contract_locks_from_to_and_bare_numeric_targets():
    issue = {
        "placement": "story",
        "layerIds": ["story-features"],
        "category": "geometry",
        "instruction": "Reduce story-features width from 360 to 280 and set fontSize 36.",
        "severity": "material",
    }
    contract = build_refinement_contract(
        _candidate(),
        [issue],
        source_placement="feed",
        available_fonts=[],
    )
    targets = contract["layers"]["story-features"]["numericTargets"]
    assert targets["geometry/width"] == 280.0
    assert targets["fontSize"] == 36.0


def test_comparator_validation_rejects_invented_layer_id_for_bounded_retry():
    from gateway.ad_template_generator_process import validate_comparator_result
    from tests.gateway.test_ad_template_generator_process import _review
    candidate = _candidate()
    review = _review(accept=False)
    review["comparisonToBest"] = "not_applicable"
    review["issues"] = [{
        "placement": "feed", "layerIds": ["story_frame"], "category": "geometry",
        "instruction": "Set y to 695 for the features block.", "severity": "material",
    }]
    with pytest.raises(AdTemplateProcessError, match="unknown layer IDs"):
        validate_comparator_result(review, candidate=candidate)


def test_contract_drops_unpatchable_issue_and_keeps_scored_evidence():
    patchable, unpatchable = _issues()[0], {
        "placement": "story",
        "layerIds": ["story-features"],
        "category": "details",
        "instruction": "The story features block should sit better within the card.",
        "severity": "minor",
    }
    contract = build_refinement_contract(
        _candidate(),
        [unpatchable, patchable],
        source_placement="feed",
        available_fonts=[],
    )
    assert contract["issues"] == [patchable]
    assert contract["layerIds"] == ["feed-features"]
    assert contract["unpatchableLayerIds"] == ["story-features"]


def test_contract_clamps_out_of_range_measured_targets_to_renderer_bounds():
    issue = {
        "placement": "feed",
        "layerIds": ["feed-features"],
        "category": "typography",
        "instruction": "Set tracking to 6 and fontSize to 20 for the features block.",
        "severity": "material",
    }
    contract = build_refinement_contract(
        _candidate(),
        [issue],
        source_placement="feed",
        available_fonts=[],
    )
    targets = contract["layers"]["feed-features"]["numericTargets"]
    assert targets["tracking"] == 4.0
    assert targets["fontSize"] == 24.0


def test_contract_clamps_geometry_targets_to_the_placement_canvas():
    issue = {
        "placement": "feed",
        "layerIds": ["feed-features"],
        "category": "geometry",
        "instruction": (
            "Move and resize the features block to the story frame footprint: "
            "set y to 20 and height to 1880."
        ),
        "severity": "material",
    }
    contract = build_refinement_contract(
        _candidate(),
        [issue],
        source_placement="feed",
        available_fonts=[],
    )
    targets = contract["layers"]["feed-features"]["numericTargets"]
    assert targets["geometry/y"] == 20.0
    assert targets["geometry/height"] == 1350.0 - 20.0


def test_contract_still_raises_when_no_issue_has_a_structured_target():
    vague = {
        "placement": "feed",
        "layerIds": ["feed-features"],
        "category": "details",
        "instruction": "The features block should sit better within the card.",
        "severity": "minor",
    }
    with pytest.raises(AdTemplateProcessError, match="no structured property target"):
        build_refinement_contract(
            _candidate(),
            [vague],
            source_placement="feed",
            available_fonts=[],
        )


def test_patch_enforces_property_targets_and_freezes_other_layers():
    contract = build_refinement_contract(
        _candidate(),
        _issues(),
        source_placement="feed",
        available_fonts=[],
    )
    valid = {
        "operations": [
            {
                "op": "replace",
                "path": "/template/feedLayout/layers/0/geometry/x",
                "value": 90,
            },
            {
                "op": "replace",
                "path": "/template/textInputs/0/placeholder",
                "value": "A\nB\nC\nD\nE\nF",
            },
        ]
    }
    assert validate_refinement_patch(valid, contract=contract) == valid

    wrong_target = {
        "operations": [
            {
                "op": "replace",
                "path": "/template/feedLayout/layers/0/geometry/x",
                "value": 91,
            }
        ]
    }
    with pytest.raises(AdTemplateProcessError, match="numeric target"):
        validate_refinement_patch(wrong_target, contract=contract)

    other_layer = {
        "operations": [
            {
                "op": "replace",
                "path": "/template/storyLayout/layers/0/geometry/x",
                "value": 90,
            }
        ]
    }
    with pytest.raises(AdTemplateProcessError, match="property locks"):
        validate_refinement_patch(other_layer, contract=contract)


def test_patch_enforces_renderer_property_and_font_bounds():
    candidate = _candidate()
    issues = [
        {
            "placement": "feed",
            "layerIds": ["feed-features"],
            "category": "typography",
            "instruction": "Set tracking to 4 and font to /fonts/adstudio/manrope-700.woff2.",
            "severity": "material",
        }
    ]
    contract = build_refinement_contract(
        candidate,
        issues,
        source_placement="feed",
        available_fonts=["/fonts/adstudio/manrope-700.woff2"],
    )
    tracking = {
        "operations": [
            {
                "op": "replace",
                "path": "/template/feedLayout/layers/0/tracking",
                "value": 7,
            }
        ]
    }
    with pytest.raises(AdTemplateProcessError, match="numeric target|tracking"):
        validate_refinement_patch(tracking, contract=contract)

    font = {
        "operations": [
            {
                "op": "replace",
                "path": "/template/feedLayout/layers/0/font/file",
                "value": "/fonts/adstudio/unavailable.woff2",
            }
        ]
    }
    with pytest.raises(AdTemplateProcessError, match="unavailable font"):
        validate_refinement_patch(font, contract=contract)


def test_matching_crops_use_one_fixed_canvas_rectangle(tmp_path):
    contract = build_refinement_contract(
        _candidate(),
        _issues(),
        source_placement="feed",
        available_fonts=[],
    )
    source = tmp_path / "source.png"
    current = tmp_path / "current.png"
    Image.new("RGB", (1080, 1350), "white").save(source)
    Image.new("RGB", (1080, 1350), "black").save(current)

    paths, box = write_matching_crops(
        contract,
        reference_path=str(source),
        candidate_path=str(current),
        workspace=tmp_path / "evidence",
    )

    assert box == {"x": 56, "y": 776, "width": 498, "height": 268}
    with Image.open(paths[0]) as left, Image.open(paths[1]) as right:
        assert left.size == right.size == (box["width"], box["height"])
        assert left.getpixel((0, 0)) == (255, 255, 255)
        assert right.getpixel((0, 0)) == (0, 0, 0)



def test_group_does_not_collect_unrelated_same_placement_issue():
    candidate = _candidate()
    unrelated = dict(candidate["template"]["feedLayout"]["layers"][0])
    unrelated.update(layerId="feed-footer", inputKey="footer")
    candidate["template"]["feedLayout"]["layers"].append(unrelated)
    candidate["template"]["textInputs"].append(
        {"key": "footer", "label": "Footer", "placeholder": "Footer", "maxLength": 20}
    )
    issues = [
        *_issues(),
        {
            "placement": "feed",
            "layerIds": ["feed-footer"],
            "category": "geometry",
            "instruction": "Set y to 1200.",
            "severity": "minor",
        },
    ]

    contract = build_refinement_contract(
        candidate, issues, source_placement="feed", available_fonts=[]
    )

    assert contract["layerIds"] == ["feed-features"]
    assert len(contract["issues"]) == 1


def test_property_name_without_explicit_target_stays_locked():
    issue = {
        **_issues()[0],
        "instruction": "Keep width unchanged; set x to 90.",
    }
    contract = build_refinement_contract(
        _candidate(), [issue], source_placement="feed", available_fonts=[]
    )
    assert contract["layers"]["feed-features"]["allowedProperties"] == [
        "geometry/x"
    ]


def test_fixed_crop_ink_guard_reproduces_heading_width_regression(tmp_path):
    contract = build_refinement_contract(
        _candidate(), _issues(), source_placement="feed", available_fonts=[]
    )
    # Restrict the synthetic contract to text-only evidence, as in the heading pilot.
    source = tmp_path / "source-crop.png"
    before = tmp_path / "before-crop.png"
    after = tmp_path / "after-crop.png"
    for path, box in (
        (source, (51, 44, 646, 118)),
        (before, (68, 22, 663, 111)),
        (after, (72, 45, 616, 123)),
    ):
        image = Image.new("RGB", (700, 150), "white")
        from PIL import ImageDraw
        ImageDraw.Draw(image).rectangle(box, fill="black")
        image.save(path)

    with pytest.raises(AdTemplateProcessError, match="worsened fixed-crop text ink"):
        validate_text_ink_regression(
            contract,
            source_crop=str(source),
            before_crop=str(before),
            after_crop=str(after),
        )


def test_fixed_crop_ink_guard_accepts_non_regressing_text(tmp_path):
    contract = build_refinement_contract(
        _candidate(), _issues(), source_placement="feed", available_fonts=[]
    )
    paths = []
    for name, box in (
        ("source", (51, 44, 646, 118)),
        ("before", (68, 22, 663, 111)),
        ("after", (52, 43, 645, 119)),
    ):
        path = tmp_path / f"{name}.png"
        image = Image.new("RGB", (700, 150), "white")
        from PIL import ImageDraw
        ImageDraw.Draw(image).rectangle(box, fill="black")
        image.save(path)
        paths.append(str(path))

    result = validate_text_ink_regression(
        contract, source_crop=paths[0], before_crop=paths[1], after_crop=paths[2]
    )
    assert result["mode"] == "text-ink"


def test_multi_layer_issue_keeps_each_named_numeric_target():
    candidate = _candidate()
    second = dict(candidate["template"]["feedLayout"]["layers"][0])
    second["geometry"] = dict(second["geometry"])
    second.update(layerId="feed-feature-heading", inputKey="feature-heading")
    candidate["template"]["feedLayout"]["layers"].append(second)
    candidate["template"]["textInputs"].append(
        {
            "key": "feature-heading",
            "label": "Feature heading",
            "placeholder": "Features",
            "maxLength": 20,
        }
    )
    issue = {
        "placement": "feed",
        "layerIds": ["feed-features", "feed-feature-heading"],
        "category": "geometry",
        "instruction": (
            "Set feed-features y to 900 and feed-feature-heading y to 850."
        ),
        "severity": "material",
    }

    contract = build_refinement_contract(
        candidate, [issue], source_placement="feed", available_fonts=[]
    )

    assert contract["layers"]["feed-features"]["numericTargets"] == {
        "geometry/y": 900.0
    }
    assert contract["layers"]["feed-feature-heading"]["numericTargets"] == {
        "geometry/y": 850.0
    }


def test_batch_contract_covers_disconnected_groups_in_one_bounded_call():
    candidate = _candidate()
    footer = dict(candidate["template"]["feedLayout"]["layers"][0])
    footer["geometry"] = dict(footer["geometry"])
    footer.update(layerId="feed-footer", inputKey="footer")
    candidate["template"]["feedLayout"]["layers"].append(footer)
    candidate["template"]["textInputs"].append(
        {"key": "footer", "label": "Footer", "placeholder": "Footer", "maxLength": 20}
    )
    issues = [
        *_issues(),
        {
            "placement": "feed",
            "layerIds": ["feed-footer"],
            "category": "colourEffects",
            "instruction": "Set opacity to 0.8.",
            "severity": "material",
        },
    ]
    contract = build_refinement_batch_contract(
        candidate,
        issues,
        source_placement="feed",
        available_fonts=[],
    )

    assert len(contract["groups"]) == 2
    assert contract["remainingIssueCount"] == 0
    valid = {
        "operations": [
            {
                "op": "replace",
                "path": "/template/feedLayout/layers/0/geometry/x",
                "value": 90,
            },
            {
                "op": "add",
                "path": "/template/feedLayout/layers/1/opacity",
                "value": 0.8,
            },
        ]
    }
    assert validate_refinement_patch(valid, contract=contract) == valid
    with pytest.raises(AdTemplateProcessError, match="omitted one selected issue group"):
        validate_refinement_patch(
            {"operations": [valid["operations"][0]]}, contract=contract
        )


def test_structured_patch_allows_exact_nested_crop_and_effect_properties():
    candidate = _candidate()
    layer = candidate["template"]["feedLayout"]["layers"][0]
    layer["defaultCrop"] = {"x": 0.5, "y": 0.5, "scale": 1}
    layer["effects"] = {"shadow": {"blur": 4}}
    issue = {
        "placement": "feed",
        "layerIds": ["feed-features"],
        "category": "imageCrop",
        "instruction": "Set crop x to 0.4 and shadow blur to 8.",
        "severity": "material",
    }
    suggested = {
        "operations": [
            {
                "op": "replace",
                "path": "/template/feedLayout/layers/0/defaultCrop/x",
                "value": 0.4,
            },
            {
                "op": "replace",
                "path": "/template/feedLayout/layers/0/effects/shadow/blur",
                "value": 8,
            },
        ]
    }
    contract = build_refinement_contract(
        candidate,
        [issue],
        source_placement="feed",
        available_fonts=[],
        suggested_patch=suggested,
    )

    assert set(contract["layers"]["feed-features"]["allowedProperties"]) >= {
        "defaultCrop/x",
        "effects/shadow/blur",
    }
    assert validate_refinement_patch(suggested, contract=contract) == suggested


def test_semantic_colour_requires_all_role_consumers_in_selected_group():
    candidate = _candidate()
    candidate["template"]["semanticColours"] = {"mainText": "#111111"}
    outsider = dict(candidate["template"]["feedLayout"]["layers"][0])
    outsider["geometry"] = dict(outsider["geometry"])
    outsider.update(layerId="feed-footer", inputKey="footer")
    candidate["template"]["feedLayout"]["layers"].append(outsider)
    issue = {
        "placement": "feed",
        "layerIds": ["feed-features"],
        "category": "colourEffects",
        "instruction": "Set colour to #222222 and opacity to 0.8.",
        "severity": "material",
    }
    semantic_path = "/template/semanticColours/mainText"
    contract = build_refinement_contract(
        candidate, [issue], source_placement="feed", available_fonts=[]
    )
    assert semantic_path not in contract["extraAllowedPaths"]
    with pytest.raises(AdTemplateProcessError, match="property locks"):
        validate_refinement_patch(
            {"operations": [{"op": "replace", "path": semantic_path, "value": "#222222"}]},
            contract=contract,
        )

    outsider["colourRole"] = "secondary"
    candidate["template"]["storyLayout"]["layers"][0]["colourRole"] = "secondary"
    contract = build_refinement_contract(
        candidate, [issue], source_placement="feed", available_fonts=[]
    )
    assert semantic_path in contract["extraAllowedPaths"]
    patch = {
        "operations": [
            {"op": "replace", "path": semantic_path, "value": "#222222"},
            {
                "op": "add",
                "path": "/template/feedLayout/layers/0/opacity",
                "value": 0.8,
            },
        ]
    }
    assert validate_refinement_patch(patch, contract=contract) == patch


def test_reverse_text_uses_visual_review_instead_of_dark_ink_guard(tmp_path):
    contract = build_refinement_contract(
        _candidate(), _issues(), source_placement="feed", available_fonts=[]
    )
    paths = []
    for name in ("source", "before", "after"):
        path = tmp_path / f"{name}.png"
        image = Image.new("RGB", (300, 100), "black")
        from PIL import ImageDraw
        ImageDraw.Draw(image).rectangle((30, 30, 220, 60), fill="white")
        image.save(path)
        paths.append(str(path))

    result = validate_text_ink_regression(
        contract, source_crop=paths[0], before_crop=paths[1], after_crop=paths[2]
    )
    assert result["mode"] == "visual-only"


def test_structured_targets_are_authoritative_and_complete():
    issue = {
        **_issues()[0],
        "instruction": "Set width to 1 and x to 2 on feed-features.",
        "targets": [
            {"layerId": "feed-features", "property": "x", "value": 90},
            {"layerId": "feed-features", "property": "geometry/width", "value": 440},
        ],
    }
    contract = build_refinement_contract(
        _candidate(), [issue], source_placement="feed", available_fonts=[]
    )
    assert contract["targets"] == [
        {"layerId": "feed-features", "property": "geometry/x", "value": 90.0},
        {"layerId": "feed-features", "property": "geometry/width", "value": 440.0},
    ]
    with pytest.raises(AdTemplateProcessError, match="omitted authoritative target"):
        validate_refinement_patch(
            {"operations": [{
                "op": "replace",
                "path": "/template/feedLayout/layers/0/geometry/x",
                "value": 90,
            }]},
            contract=contract,
        )
    patch = {"operations": [
        {"op": "replace", "path": "/template/feedLayout/layers/0/geometry/x", "value": 90},
        {"op": "replace", "path": "/template/feedLayout/layers/0/geometry/width", "value": 440},
    ]}
    assert validate_refinement_patch(patch, contract=contract) == patch


@pytest.mark.parametrize(
    "target, message",
    [
        ({"layerId": "missing", "property": "x", "value": 1}, "unknown layer"),
        ({"layerId": "feed-features", "property": "unknown", "value": 1}, "unsupported"),
        ({"layerId": "feed-features", "property": "x", "value": "1"}, "numeric"),
        ({"layerId": "feed-features", "property": "x", "value": float("nan")}, "finite"),
    ],
)
def test_structured_targets_reject_unsafe_values(target, message):
    issue = {**_issues()[0], "targets": [target]}
    with pytest.raises(AdTemplateProcessError, match=message):
        build_refinement_contract(
            _candidate(), [issue], source_placement="feed", available_fonts=[]
        )


def test_structured_targets_reject_conflicts_and_support_bounded_effect_objects():
    candidate = _candidate()
    layer = candidate["template"]["feedLayout"]["layers"][0]
    layer["fill"] = {"type": "solid", "colour": "#111111"}
    conflict = {
        **_issues()[0],
        "targets": [
            {"layerId": "feed-features", "property": "x", "value": 90},
            {"layerId": "feed-features", "property": "x", "value": 91},
        ],
    }
    with pytest.raises(AdTemplateProcessError, match="conflicting"):
        build_refinement_contract(
            candidate, [conflict], source_placement="feed", available_fonts=[]
        )
    issue = {
        **_issues()[0],
        "targets": [{
            "layerId": "feed-features",
            "property": "fill",
            "value": {"type": "solid", "colour": "#222222"},
        }],
    }
    contract = build_refinement_contract(
        candidate, [issue], source_placement="feed", available_fonts=[]
    )
    patch = {"operations": [{
        "op": "replace",
        "path": "/template/feedLayout/layers/0/fill",
        "value": {"type": "solid", "colour": "#222222"},
    }]}
    assert validate_refinement_patch(patch, contract=contract) == patch


def test_legacy_geometry_before_layer_and_grouped_layer_syntax():
    candidate = _candidate()
    second = dict(candidate["template"]["feedLayout"]["layers"][0])
    second["geometry"] = dict(second["geometry"])
    second.update(layerId="feed-heading", inputKey="heading")
    candidate["template"]["feedLayout"]["layers"].append(second)
    issue = {
        "placement": "feed",
        "layerIds": ["feed-features"],
        "category": "geometry",
        "instruction": (
            "Set x to 0, y to 0, width to 1080, and height to 660 on "
            "layer feed-features to restore flush top bleed."
        ),
        "severity": "material",
    }
    contract = build_refinement_contract(
        candidate, [issue], source_placement="feed", available_fonts=[]
    )
    assert contract["layers"]["feed-features"]["numericTargets"] == {
        "geometry/x": 0.0, "geometry/y": 0.0, "geometry/width": 1080.0,
        "geometry/height": 660.0,
    }
    grouped = {
        "placement": "feed",
        "layerIds": ["feed-features", "feed-heading"],
        "category": "geometry",
        "instruction": (
            "Set feed-features y from 800 to 900 and feed-heading y from "
            "800 to 850."
        ),
        "severity": "material",
    }
    contract = build_refinement_contract(
        candidate, [grouped], source_placement="feed", available_fonts=[]
    )
    assert contract["layers"]["feed-features"]["numericTargets"] == {"geometry/y": 900.0}
    assert contract["layers"]["feed-heading"]["numericTargets"] == {"geometry/y": 850.0}


def test_batch_surfaces_unpatchable_issue_and_keeps_valid_group():
    vague = {
        "placement": "story",
        "layerIds": ["story-features"],
        "category": "details",
        "instruction": "Improve the card.",
        "severity": "minor",
    }
    contract = build_refinement_batch_contract(
        _candidate(), [vague, _issues()[0]], source_placement="feed", available_fonts=[]
    )
    assert contract["remainingIssueCount"] == 0
    assert contract["unpatchableIssueCount"] == 1
    assert contract["unpatchableLayerIds"] == ["story-features"]
    assert contract["groups"][0]["layerIds"] == ["feed-features"]


def test_legacy_current_values_are_not_targets():
    issue = {
        **_issues()[0],
        "instruction": (
            "Current x is 80, y is 800, width is 420, height is 220. "
            "Set x to 90."
        ),
    }
    contract = build_refinement_contract(
        _candidate(), [issue], source_placement="feed", available_fonts=[]
    )
    assert contract["layers"]["feed-features"]["numericTargets"] == {
        "geometry/x": 90.0
    }


def test_empty_structured_targets_are_surfaceable_unpatchable_issue():
    structural = {**_issues()[0], "targets": [], "instruction": "Add the missing CTA layer."}
    contract = build_refinement_batch_contract(
        _candidate(), [structural, _issues()[0]],
        source_placement="feed", available_fonts=[]
    )
    assert contract["unpatchableIssueCount"] == 1
    assert contract["unpatchableIssues"][0]["issue"]["targets"] == []
