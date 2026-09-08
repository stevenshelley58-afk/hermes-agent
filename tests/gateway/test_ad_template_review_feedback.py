import copy

import pytest

import gateway.ad_template_generator_process as process
from tests.gateway.test_ad_template_generator_process import _review, _template
from gateway.ad_template_generator_layer_refinement import (
    build_refinement_contract, validate_refinement_patch,
)


def test_semantic_colour_target_survives_review_and_locked_patch():
    template = _template()
    template["feedLayout"]["layers"].append(_candidate()["template"]["feedLayout"]["layers"][0])
    index = len(template["feedLayout"]["layers"]) - 1
    candidate = {"template": template, "assets": [
        {"assetKey": key, **declaration} for key, declaration in template["assets"].items()
    ]}
    review = _invalid_review()
    review["issues"] = review["issues"][:1]
    review["issues"][0]["targets"] = [
        {"layerId": "checkbox", "property": "colourRole", "value": "background"},
    ]
    validated = process.validate_review(review, candidate=candidate)
    assert validated["decision"] == "revise"
    contract = build_refinement_contract(
        candidate, validated["issues"], source_placement="feed", available_fonts=[],
    )
    patch = {"operations": [{
        "op": "replace", "path": f"/template/feedLayout/layers/{index}/colourRole",
        "value": "background",
    }]}
    assert validate_refinement_patch(patch, contract=contract) == patch
    updated = process.apply_patch(candidate, patch)
    assert updated["template"]["feedLayout"]["layers"][index]["colourRole"] == "background"
    assert candidate["template"]["feedLayout"]["layers"][index]["colourRole"] == "mainText"
    patch["operations"][0]["value"] = "accent"
    with pytest.raises(process.AdTemplateProcessError):
        validate_refinement_patch(patch, contract=contract)


@pytest.mark.parametrize("value", ["#ffffff", "white", [], None])
def test_semantic_colour_target_rejects_non_roles(value):
    review = _invalid_review()
    review["issues"] = review["issues"][:1]
    review["issues"][0]["targets"] = [
        {"layerId": "checkbox", "property": "colourRole", "value": value},
    ]
    with pytest.raises(process.AdTemplateProcessError, match="semantic role"):
        process.validate_review(review, candidate=_candidate())


def test_review_prompt_names_real_renderer_colour_fields():
    prompt = process.review_prompt(
        final=False, candidate=_candidate(), reference={"sourcePlacement": "feed"}, metrics={},
    )
    assert "Solid colours use colourRole, NOT fill/colour" in prompt
    assert "opacity:1" in prompt
    assert "Story fontSize >=32" in prompt



def _candidate():
    return {"template": {
        "feedLayout": {"layers": [{
            "layerId": "checkbox", "type": "vector", "colourRole": "mainText",
            "shape": "rect", "opacity": 1,
            "geometry": {"x": 20, "y": 20, "width": 30, "height": 30},
        }]},
        "storyLayout": {"layers": [{
            "layerId": "date", "type": "text", "fontSize": 36,
            "inputKey": "date", "font": {"file": "/fonts/adstudio/manrope-400.woff2"},
            "lineHeight": 1.2, "tracking": 0, "alignment": "left",
            "maxCharacters": 30, "maxLines": 2, "colourRole": "mainText",
            "overflowBehaviour": "refuse",
            "geometry": {"x": 100, "y": 100, "width": 400, "height": 80},
        }]},
    }}


def _invalid_review():
    result = _review(accept=False)
    result["issues"] = [{
        "placement": placement, "layerIds": [layer], "category": category,
        "instruction": instruction, "severity": "material",
        "targets": [{"layerId": layer, "property": prop, "value": value}],
    } for placement, layer, category, instruction, prop, value in [
        ("feed", "checkbox", "colourEffects", "Restore the outlined checkbox", "fill/colour", "#ffffff"),
        ("story", "date", "typography", "Date text is clipped; preserve the full date", "fontSize", 20),
    ]]
    return result


def test_review_reports_independent_errors_together_without_mutation():
    candidate, review = _candidate(), _invalid_review()
    before = copy.deepcopy((candidate, review))
    with pytest.raises(process.AdTemplateProcessError) as error:
        process.validate_review(review, candidate=candidate)
    message = str(error.value)
    assert "issues[0]" in message and "fill/colour" in message
    assert "issues[1]" in message and "date (story)" in message
    assert "minimum 32px" in message
    assert (candidate, review) == before


def test_single_format_retry_can_correct_all_reported_issues(monkeypatch):
    candidate, invalid = _candidate(), _invalid_review()
    corrected = copy.deepcopy(invalid)
    corrected["issues"][0]["targets"] = []
    corrected["issues"][1]["targets"] = [
        {"layerId": "date", "property": "geometry/width", "value": 500},
    ]
    monkeypatch.setattr(process, "vision_message", lambda prompt, paths, **kwargs: prompt)
    calls, events = [], []

    def call_agent(instance, prompt, route):
        calls.append(instance)
        if len(calls) == 1:
            return invalid
        assert "fill/colour" in prompt and "minimum 32px" in prompt
        assert "Do not erase a defect or inflate a score" in prompt
        return corrected

    result = process._call_json(
        call_agent, instance="comparator-1", prompt="Review both placements", paths=[],
        route={"provider": "concentrate", "model": "test"},
        validate=lambda value: process.validate_review(value, candidate=candidate),
        emit=lambda *args: events.append(args),
    )
    assert calls == ["comparator-1", "comparator-1-format-retry"]
    assert len(events) == 1
    assert result["decision"] == "revise"
    assert len(result["issues"]) == 2
    assert result["scores"] == invalid["scores"]
    assert candidate["template"]["storyLayout"]["layers"][0]["fontSize"] == 36


def test_invalid_retry_still_fails_closed(monkeypatch):
    monkeypatch.setattr(process, "vision_message", lambda prompt, paths, **kwargs: prompt)
    with pytest.raises(process.AdTemplateProcessError, match="minimum 32px"):
        process._call_json(
            lambda *args: _invalid_review(), instance="review", prompt="review", paths=[],
            route={"provider": "concentrate", "model": "test"},
            validate=lambda value: process.validate_review(value, candidate=_candidate()),
            emit=lambda *args: None,
        )
