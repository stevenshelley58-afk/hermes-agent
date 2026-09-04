import base64
import io
import json
import sys
from pathlib import Path
import pytest
from PIL import Image
import gateway.ad_template_process as process
from gateway.ad_template_process import (
    AdTemplateProcessError, deterministic_documents, import_template,
    validate_artifacts, validate_final_review, validate_iterations, SoleProcessOrchestrator, vision_message,
)
from gateway.tool_run_api import ToolRunAPIMixin

def evidence(score=9.4, reason="Improve spacing", *, best_available=False, axes=None, changes=None):
    axis_states = axes or {
        "macro_topology": {"decision": "pass", "reason": "The macro composition is coherent"},
        "micro_typography_legibility": {"decision": "pass", "reason": "The typography is legible"},
    }
    return {
        "rubric": {field: score for field in process.RUBRIC_FIELDS},
        "reason": reason,
        "hard_failures": [],
        "visual_evidence": {
            "regions": {
                placement: [{
                    "bbox": {"x": 0.05, "y": 0.05, "width": 0.9, "height": 0.7},
                    "kind": "photo",
                    "semantic_role": "property hero",
                    "ocr_text": "",
                    "content_state": "meaningful",
                }]
                for placement in ("source", "feed", "story")
            },
            "axes": axis_states,
            "best_so_far": {
                "available": best_available,
                "macro_topology": "equivalent" if best_available else "not_applicable",
                "micro_typography_legibility": "equivalent" if best_available else "not_applicable",
                "preferred": "current",
            },
            "changes": changes or [],
            "rendered_checks": {
                "judged_rendered_pixels": True,
                "identity_treatment": "meaningful_editable_identity",
                "story_space": "functional_composition",
            },
        },
        "rendered_pixel_checks": {
            "judged_rendered_pixels": True,
            "identity_treatment": "meaningful_editable_identity",
            "story_space": "functional_composition",
        },
    }

def iteration(score=9.4, number=1):
    return {"iteration": number, "comparison": evidence(score), "decision": "accepted" if score >= 9.5 else "revise"}

def test_only_real_stages_and_durable_source_preview(tmp_path):
    assert process.STAGES == ("source", "build", "render", "compare", "final-check", "live")
    assert ToolRunAPIMixin._tool_stage_order() == list(process.STAGES)
    assert ToolRunAPIMixin._canonical_tool_stage("story-draft") == "build"
    assert ToolRunAPIMixin._canonical_tool_stage("final-review") == "final-check"
    assert ToolRunAPIMixin._canonical_tool_stage("import") == "live"
    source = tmp_path / "upload.JPEG"
    source.write_bytes(b"source-pixels")
    target = ToolRunAPIMixin._copy_source_preview(tmp_path / "run", source)
    assert target == tmp_path / "run" / "previews" / "source.jpeg"
    assert target.read_bytes() == b"source-pixels"

def metadata(title="Smoke"):
    return {
        "title": title, "description": "", "gallerySamples": {},
        "metaCopyDefaults": {"primaryText": [], "headlines": [], "descriptions": [], "cta": "LEARN_MORE"},
        "aiWritingGuidance": {"summary": "", "fields": {}},
        "publishRequirements": {
            "objective": "OUTCOME_TRAFFIC", "specialAdCategory": None,
            "instantForm": {"required": False, "dependency": None},
            "destination": {"required": True, "kind": "website", "dependency": "landing_page_url"},
            "requiredCtaTypes": ["LEARN_MORE"],
        },
        "replacementAssets": [], "realAssetRefs": [],
    }

def semantic_colours():
    return {"background": "#FFFFFF", "primary": "#1A56DB", "secondary": "#6B7280", "accent": "#F59E0B", "mainText": "#111827", "inverseText": "#FFFFFF"}

def valid_candidate(template_id="candidate"):
    def layout(placement, height):
        return {"placement": placement, "layers": [{"type": "plate", "layerId": f"{placement}-bg", "colourRole": "background", "geometry": {"x": 0, "y": 0, "width": 1080, "height": height}, "protected": False}], "safeZones": [{"x": 0, "y": 0, "width": 1080, "height": height}]}
    return {"template": {"schema": "blockwise.ad-template", "templateId": template_id, "createdAt": "2026-08-30T00:00:00.000Z", "feedLayout": layout("feed", 1350), "storyLayout": layout("story", 1920), "imageInputs": [], "textInputs": [], "semanticColours": semantic_colours(), "assets": {}, "fonts": [], "metadata": metadata()}, "assets": []}

def fake_render(candidate, workspace, calls):
    calls.append(candidate)
    workspace.mkdir(parents=True, exist_ok=True)
    feed = workspace / "feed.png"
    story = workspace / "story.png"
    Image.new("RGB", (1080, 1350), "white").save(feed, format="PNG")
    Image.new("RGB", (1080, 1920), "white").save(story, format="PNG")
    return {
        "template": candidate["template"], "assets": candidate["assets"],
        "previews": [
            {"name": feed.name, "path": str(feed), "placement": "feed"},
            {"name": story.name, "path": str(story), "placement": "story"},
        ],
        "render": {"feed": str(feed), "story": str(story)},
        "template_path": str(workspace / "artifact.json"),
    }

def test_one_comparator_per_iteration_and_final_review_only_after_pass():
    assert validate_iterations([iteration()])[0]["comparison"]["score"] == 9.4
    with pytest.raises(AdTemplateProcessError):
        validate_iterations([{"iteration": 1, "comparison": {"score": 9.4, "reason": "x"}, "reviewers": []}])
    review = validate_final_review({"reviewers": [{"id": "reviewer-a", "route": "a/m", **evidence(9.6, "good")}, {"id": "reviewer-b", "route": "b/m", **evidence(9.7, "good")}]}, accepted=True)
    assert review["decision"] == "accepted"

def test_quality_gate_rejects_one_weak_dimension_even_when_mean_passes():
    weak = evidence(9.6, "Delivered-size copy remains too small")
    weak["rubric"]["real_shell_legibility"] = 9.1
    record = validate_iterations([{"iteration": 1, "comparison": weak, "decision": "revise"}])[0]
    assert record["comparison"]["score"] >= process.THRESHOLD
    assert record["comparison"]["minimum_score"] == 9.1
    assert record["decision"] == "revise"

    reviewers = [
        {"id": "reviewer-a", "route": "a/m", **weak},
        {"id": "reviewer-b", "route": "b/m", **evidence(9.8, "Strong")},
    ]
    assert validate_final_review({"reviewers": reviewers}, accepted=True)["decision"] == "revise"


def test_final_review_pixel_checks_fail_empty_logo_and_dead_story_band():
    empty_logo = evidence(9.9, "The rendered pixels contain an empty logo frame")
    empty_logo["rendered_pixel_checks"]["identity_treatment"] = "empty_visible_placeholder"
    clean = evidence(9.9, "Rendered logo and Story composition are complete")
    result = validate_final_review(
        {"reviewers": [
            {"id": "reviewer-a", "route": "a/m", **empty_logo},
            {"id": "reviewer-b", "route": "b/m", **clean},
        ]},
        accepted=True,
        require_rendered_pixel_checks=True,
    )
    assert result["decision"] == "revise"
    assert result["reviewers"][0]["score"] == 0.0
    assert "empty visible identity, logo, or mask placeholder" in result["reviewers"][0]["hard_failures"][0].lower()

    dead_story = evidence(10.0, "The Story ends in a large decorative blank band")
    dead_story["rendered_pixel_checks"]["story_space"] = "large_nonfunctional_dead_band"
    result = validate_final_review(
        {"reviewers": [
            {"id": "reviewer-a", "route": "a/m", **dead_story},
            {"id": "reviewer-b", "route": "b/m", **clean},
        ]},
        accepted=True,
        require_rendered_pixel_checks=True,
    )
    assert result["decision"] == "revise"
    assert result["reviewers"][0]["rubric"]["native_story"] == 9.1
    assert result["reviewers"][0]["minimum_score"] == 9.1


def test_final_review_pixel_checks_are_required_for_current_policy():
    missing = evidence(9.9, "Looks complete")
    missing.pop("rendered_pixel_checks")
    with pytest.raises(AdTemplateProcessError, match="rendered_pixel_checks"):
        validate_final_review(
            {"reviewers": [
                {"id": "reviewer-a", "route": "a/m", **missing},
                {"id": "reviewer-b", "route": "b/m", **evidence(9.9, "Complete")},
            ]},
            accepted=True,
            require_rendered_pixel_checks=True,
        )


def test_comparator_visual_evidence_is_normalized_and_placement_scoped():
    candidate = valid_candidate("placement-scoped")
    comparison = evidence(9.9, "Feed headline needs stronger hierarchy")
    comparison["visual_evidence"]["axes"]["macro_topology"] = {
        "decision": "revise",
        "reason": "The Feed headline has no dominant relationship to the hero",
    }
    comparison["visual_evidence"]["changes"] = [{
        "placement": "feed",
        "level": "macro",
        "target_layer_ids": ["feed-bg"],
        "instruction": "Rebalance the Feed hero and headline region",
    }]
    assessed = process._assessment(
        comparison,
        "comparator",
        require_visual_evidence=True,
        candidate=candidate,
        best_available=False,
    )
    assert assessed["visual_evidence"]["regions"]["feed"][0]["bbox"] == {
        "x": 0.05, "y": 0.05, "width": 0.9, "height": 0.7,
    }
    assert assessed["minimum_score"] == 9.1

    wrong_placement = json.loads(json.dumps(comparison))
    wrong_placement["visual_evidence"]["changes"][0]["target_layer_ids"] = ["story-bg"]
    with pytest.raises(AdTemplateProcessError, match="must name only feed layers"):
        process._assessment(
            wrong_placement,
            "comparator",
            require_visual_evidence=True,
            candidate=candidate,
            best_available=False,
        )

    invalid_bbox = json.loads(json.dumps(comparison))
    invalid_bbox["visual_evidence"]["regions"]["feed"][0]["bbox"]["width"] = 1.1
    with pytest.raises(AdTemplateProcessError, match="normalized image bounds"):
        process._assessment(
            invalid_bbox,
            "comparator",
            require_visual_evidence=True,
            candidate=candidate,
            best_available=False,
        )


def test_comparator_preserves_best_on_either_macro_or_micro_regression():
    candidate = valid_candidate("regression")
    comparison = evidence(9.9, "Current typography regressed", best_available=True)
    comparison["visual_evidence"]["best_so_far"].update(
        macro_topology="improved",
        micro_typography_legibility="regressed",
        preferred="best",
    )
    assessed = process._assessment(
        comparison,
        "comparator",
        require_visual_evidence=True,
        candidate=candidate,
        best_available=True,
    )
    assert assessed["minimum_score"] == 9.1
    assert process._passes_quality_gate(assessed) is False

    false_preference = json.loads(json.dumps(comparison))
    false_preference["visual_evidence"]["best_so_far"]["preferred"] = "current"
    with pytest.raises(AdTemplateProcessError, match="preserve the immutable best"):
        process._assessment(
            false_preference,
            "comparator",
            require_visual_evidence=True,
            candidate=candidate,
            best_available=True,
        )


def test_comparator_hard_fails_empty_identity_copied_contact_and_duplicate_facts():
    candidate = valid_candidate("identity")
    comparison = evidence(9.9, "Rendered identity and facts are not acceptable")
    comparison["visual_evidence"]["regions"]["source"].append({
        "bbox": {"x": 0.1, "y": 0.8, "width": 0.3, "height": 0.05},
        "kind": "contact",
        "semantic_role": "source agency email",
        "ocr_text": "hello@reallygreatsite.com",
        "content_state": "meaningful",
    })
    comparison["visual_evidence"]["regions"]["feed"].extend([
        {
            "bbox": {"x": 0.1, "y": 0.8, "width": 0.3, "height": 0.05},
            "kind": "contact",
            "semantic_role": "contact",
            "ocr_text": "hello@reallygreatsite.com",
            "content_state": "meaningful",
        },
        {
            "bbox": {"x": 0.1, "y": 0.7, "width": 0.15, "height": 0.04},
            "kind": "fact",
            "semantic_role": "bedrooms",
            "ocr_text": "2 bedrooms",
            "content_state": "meaningful",
        },
        {
            "bbox": {"x": 0.3, "y": 0.7, "width": 0.15, "height": 0.04},
            "kind": "fact",
            "semantic_role": "repeated bedrooms",
            "ocr_text": "2 bedrooms",
            "content_state": "meaningful",
        },
        {
            "bbox": {"x": 0.75, "y": 0.05, "width": 0.2, "height": 0.1},
            "kind": "mask",
            "semantic_role": "brand placeholder",
            "ocr_text": "",
            "content_state": "empty_placeholder",
        },
    ])
    assessed = process._assessment(
        comparison,
        "comparator",
        require_visual_evidence=True,
        candidate=candidate,
        best_available=False,
    )
    assert assessed["score"] == 0.0
    failures = " ".join(assessed["hard_failures"]).lower()
    assert "copies source advertiser identity or contact text" in failures
    assert "repeats a factual bullet" in failures
    assert "empty visible identity, logo, or mask rectangle" in failures


def test_story_safe_zone_applies_only_to_essential_layers():
    candidate = valid_candidate("story-safe")
    story = candidate["template"]["storyLayout"]
    story["safeZones"] = [{"x": 72, "y": 240, "width": 936, "height": 1380}]
    story["layers"].extend([
        {
            "type": "image_slot", "layerId": "story-full-bleed-photo", "inputKey": "hero",
            "geometry": {"x": 0, "y": 0, "width": 1080, "height": 1920}, "mask": "none",
            "minSourceWidth": 1080, "minSourceHeight": 1920,
            "defaultCrop": {"x": 0, "y": 0, "width": 1, "height": 1},
            "allowedPlacementOverrides": ["crop", "position"],
        },
        {
            "type": "text", "layerId": "story-headline", "inputKey": "headline",
            "font": {"file": "manrope-700.woff2"}, "fontSize": 72, "lineHeight": 1.1,
            "tracking": 20, "alignment": "left", "maxCharacters": 80, "maxLines": 2,
            "colourRole": "inverseText", "overflowBehaviour": "refuse",
            "geometry": {"x": 72, "y": 260, "width": 700, "height": 180},
        },
    ])
    candidate["template"]["imageInputs"] = [{
        "key": "hero", "label": "Hero", "acceptedTypes": ["image/jpeg"],
    }]
    candidate["template"]["textInputs"] = [{
        "key": "headline", "label": "Headline", "placeholder": "A better way home", "maxLength": 80,
    }]
    candidate["template"]["fonts"] = [{"file": "manrope-700.woff2"}]
    assert process.validate_template_artifact(candidate["template"]) is candidate["template"]

    candidate["template"]["storyLayout"]["layers"][-1]["geometry"]["y"] = 80
    with pytest.raises(AdTemplateProcessError, match="essential text content"):
        process.validate_template_artifact(candidate["template"])


def test_final_review_is_bound_to_comparator_completion_iteration():
    reviewers = [
        {"id": "reviewer-a", "route": "a/m", **evidence(9.8, "Complete")},
        {"id": "reviewer-b", "route": "b/m", **evidence(9.8, "Complete")},
    ]
    result = validate_final_review(
        {"reviewers": reviewers, "completion_iteration": 3},
        accepted=True,
        require_rendered_pixel_checks=True,
        completion_iteration=3,
    )
    assert result["completion_iteration"] == 3
    with pytest.raises(AdTemplateProcessError, match="completion candidate"):
        validate_final_review(
            {"reviewers": reviewers, "completion_iteration": 2},
            accepted=True,
            require_rendered_pixel_checks=True,
            completion_iteration=3,
        )

def test_bare_model_scores_are_rejected():
    with pytest.raises(AdTemplateProcessError):
        validate_iterations([iteration(9.8)]) if False else validate_iterations([{"iteration": 1, "comparison": {"score": 10, "reason": "looks good"}}])
    with pytest.raises(AdTemplateProcessError):
        validate_final_review({"reviewers": [{"id": "a", "route": "a/m", "score": 10, "reason": "ok"}, {"id": "b", "route": "b/m", "score": 10, "reason": "ok"}]}, accepted=True)
    with pytest.raises(AdTemplateProcessError):
        process._assessment({"rubric": {**evidence(9.8)["rubric"], "overall": 10}, "reason": "extra score"}, "comparator")

def test_adversarial_reviewer_identity_route_and_self_score_fail():
    with pytest.raises(AdTemplateProcessError):
        validate_final_review({"reviewers": [{"id": "same", "route": "a/m", **evidence(10, "ok")}, {"id": "same", "route": "b/m", **evidence(10, "ok")}]}, accepted=True)
    with pytest.raises(AdTemplateProcessError):
        validate_final_review({"reviewers": [{"id": "a", "route": "a/m", **evidence(10, "ok")}, {"id": "b", "route": "a/m", **evidence(10, "ok")}]}, accepted=True)

def test_nonexistent_artifact_and_import_fail(tmp_path, monkeypatch):
    with pytest.raises(AdTemplateProcessError):
        validate_artifacts({"previews": [{"path": str(tmp_path / "missing.png"), "placement": "feed"}]}, tmp_path)
    monkeypatch.delenv("BLOCKWISE_TEMPLATE_IMPORT_URL", raising=False)
    with pytest.raises(AdTemplateProcessError):
        import_template({"template": {}}, run_id="trun-test", project_id="blockwise")

def test_layered_documents_are_stable():
    layout = lambda placement, height: {"placement": placement, "layers": [{"type": "plate", "layerId": placement, "colourRole": "background", "geometry": {"x": 0, "y": 0, "width": 1080, "height": height}, "protected": False}], "safeZones": [{"x": 0, "y": 0, "width": 1080, "height": height}]}
    template = {"schema": "blockwise.ad-template", "templateId": "stable", "createdAt": "2026-08-30T00:00:00.000Z", "feedLayout": layout("feed", 1350), "storyLayout": layout("story", 1920), "imageInputs": [], "textInputs": [], "semanticColours": {"background": "#FFFFFF"}, "assets": {}, "fonts": [], "metadata": {}}
    first = deterministic_documents(template)
    assert first == deterministic_documents(template)
    assert list(first) == ["feed.json", "story.json", "template.json"]
    with pytest.raises(AdTemplateProcessError): deterministic_documents({"feed": {}, "story": {}})

def test_catalog_asset_resolution_rejects_unknown_and_traversal(tmp_path, monkeypatch):
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "property-photo.png").write_bytes(b"catalog-photo")
    template = {"schema": "blockwise.ad-template", "assets": {"property-photo": {"fileName": "home/property-photo.png", "mimeType": "image/png"}}}
    monkeypatch.setenv("AD_TEMPLATE_ASSET_CATALOG_DIR", str(tmp_path))
    resolved = process.resolve_catalog_assets(template, [{"assetKey": "property-photo", "fileName": "home/property-photo.png", "mimeType": "image/png"}])
    assert resolved[0]["bytesBase64"] == "Y2F0YWxvZy1waG90bw=="
    with pytest.raises(AdTemplateProcessError): process.resolve_catalog_assets(template, [{"assetKey": "missing", "fileName": "home/property-photo.png", "mimeType": "image/png"}])
    with pytest.raises(AdTemplateProcessError): process.resolve_catalog_assets({"schema": "blockwise.ad-template", "assets": {"x": {"fileName": "../property-photo.png", "mimeType": "image/png"}}}, [{"assetKey": "x", "fileName": "../property-photo.png", "mimeType": "image/png"}])
    with pytest.raises(AdTemplateProcessError): process.resolve_catalog_assets(template, [{"assetKey": "property-photo", "fileName": "home/property-photo.png", "mimeType": "image/jpeg"}])
    with pytest.raises(AdTemplateProcessError): process.resolve_catalog_assets(template, [{"assetKey": "property-photo", "fileName": "home/property-photo.png", "mimeType": "image/png", "bytesBase64": ""}])


def test_builder_contract_is_strict_and_prompts_require_quality_scores():
    layout = lambda placement, height: {"placement": placement, "layers": [{"type": "plate", "layerId": placement, "colourRole": "background", "geometry": {"x": 0, "y": 0, "width": 1080, "height": height}, "protected": False}], "safeZones": [{"x": 0, "y": 0, "width": 1080, "height": height}]}
    template = {"schema": "blockwise.ad-template", "templateId": "strict", "createdAt": "2026-08-30T00:00:00.000Z", "feedLayout": layout("feed", 1350), "storyLayout": layout("story", 1920), "imageInputs": [], "textInputs": [], "semanticColours": semantic_colours(), "assets": {}, "fonts": [], "metadata": metadata("Strict")}
    assert process.validate_template_artifact(template) is template
    wrong_story = json.loads(json.dumps(template)); wrong_story["storyLayout"]["placement"] = "feed"
    with pytest.raises(AdTemplateProcessError): process.validate_template_artifact(wrong_story)
    unsafe_zone = json.loads(json.dumps(template)); unsafe_zone["storyLayout"]["safeZones"][0]["height"] = 1921
    with pytest.raises(AdTemplateProcessError, match=r"storyLayout\.safeZones\[0\].*y \+ height must be <= 1920"):
        process.validate_template_artifact(unsafe_zone)
    extra_metadata = json.loads(json.dumps(template)); extra_metadata["metadata"]["version"] = 2
    with pytest.raises(AdTemplateProcessError): process.validate_template_artifact(extra_metadata)
    inline = json.loads(json.dumps(template)); inline["metadata"]["gallerySamples"]["bytesBase64"] = "forbidden"
    with pytest.raises(AdTemplateProcessError): process.validate_template_artifact(inline)
    for final in (False, True):
        prompt = process.review_prompt(final=final, candidate={
            "template": {**template, "privateNote": "never expose"},
            "assets": [{"assetKey": "hero", "fileName": "home/open-home-living.webp", "mimeType": "image/webp", "bytesBase64": "forbidden"}],
        })
        for field in process.RUBRIC_FIELDS:
            assert field in prompt
        assert "hard failure" in prompt.lower()
        assert "source composite pixel" in prompt
        assert "must not be penalized" in prompt
        assert "source-free photography" in prompt
        assert "neutral editable replacement is correct" in prompt
        assert '"templateId":"strict"' in prompt
        assert '"assetKey":"hero"' in prompt
        assert "privateNote" not in prompt
        assert "bytesBase64" not in prompt
        if final:
            assert "rendered_pixel_checks" in prompt
            assert "identity_treatment" in prompt
            assert "empty_visible_placeholder" in prompt
            assert "large_nonfunctional_dead_band" in prompt
            assert "Never infer" in prompt
        else:
            assert "rendered_pixel_checks" not in prompt
            assert "visual_evidence" in prompt
            assert "macro_topology" in prompt
            assert "micro_typography_legibility" in prompt
            assert "target_layer_ids" in prompt
    builder = process.generator_prompt(run_id="run", project_id="blockwise", brief="", placements=["feed", "story"], source="source.png")
    for key in process.METADATA_FIELDS:
        assert key in builder
    assert "never emit bytesBase64 anywhere" in builder
    assert "home/open-home-living.webp" in builder
    assert 'Feed safeZones=[{"x":72,"y":96,"width":936,"height":1158}]' in builder
    assert 'Story safeZones=[{"x":72,"y":240,"width":936,"height":1380}]' in builder
    assert "width,height are positive sizes, not right/bottom coordinates" in builder
    assert '"template": {...}, "assets": []' in builder
    assert "mask must be exactly rounded_rect, circle, or none" in builder
    assert 'defaultCrop must be exactly {"x":0,"y":0,"width":1,"height":1}' in builder
    assert "overflowBehaviour must be exactly refuse, truncate, or scale_down" in builder
    assert 'fonts must always be a JSON list such as [{"file":"manrope-400.woff2"}' in builder
    assert "shape must be exactly one of rect, rounded, circle, line, pill, notched, wave, or ring" in builder
    assert "lineHeight is a unitless multiplier between 0.8 and 2.5" in builder
    assert "tracking is additional letter spacing measured in native canvas pixels between -128 and 256" in builder
    assert "preserve wider source-inspired display tracking" in builder
    assert "never an em multiplier" in builder
    assert "Never render an empty logo box" in builder
    assert "Every icon layer must use exactly arrow, check, phone, mail, globe, or pin" in builder
    assert "Every image_slot inputKey must be declared exactly once in imageInputs" in builder
    assert "real logo layer" in builder
    assert "one dominant idea" in builder
    assert "brochure density" in builder
    assert "Never reintroduce a source identifier" in builder
    assert "full-bleed photography" in builder
    assert "specialAdCategory must be HOUSING" in builder
    assert "Every text layer inputKey must be declared exactly once in textInputs" in builder
    assert 'Each realAssetRefs entry must contain exactly {"inputKey":"declaredKey"' in builder
    assert "Every layer assetKey, image defaultAssetKey, gallery sample assetKey" in builder
    repair_builder = process.generator_prompt(
        run_id="run", project_id="blockwise", brief="", placements=["feed", "story"], source="source.png",
        validation_feedback="template is invalid", repair_attempt=1,
        rejected_candidate={"template": {"templateId": "broken", "bytesBase64": "forbidden", "privateNote": "secret"}, "assets": []},
    )
    assert "IMMEDIATELY PRIOR REJECTED CANDIDATE" in repair_builder
    assert '"templateId":"broken"' in repair_builder
    assert '"bytesBase64":' not in repair_builder
    assert "privateNote" not in repair_builder
    assert "forbidden" not in repair_builder and "secret" not in repair_builder
    oversized_candidate = {
        "template": {
            "metadata": {
                "metaCopyDefaults": {
                    "primaryText": ["x" * 8000 for _ in range(20)],
                },
            },
        },
        "assets": [],
    }
    with pytest.raises(AdTemplateProcessError, match=r"safe candidate context exceeds 100000 characters"):
        process.generator_prompt(
            run_id="run", project_id="blockwise", brief="", placements=["feed", "story"], source="source.png",
            prior_candidate=oversized_candidate,
        )



    invalid_fonts = json.loads(json.dumps(template))
    invalid_fonts["fonts"] = {"body": {"file": "manrope-400.woff2"}}
    with pytest.raises(AdTemplateProcessError, match=r"fonts must be a JSON list.*never an object or map"):
        process.validate_template_artifact(invalid_fonts)

    invalid_slot = json.loads(json.dumps(template))
    invalid_slot["feedLayout"]["layers"].append({
        "type": "image_slot", "layerId": "feed-hero", "inputKey": "heroImage",
        "geometry": {"x": 10, "y": 10, "width": 100, "height": 100},
        "mask": "rounded", "minSourceWidth": 100, "minSourceHeight": 100,
        "defaultCrop": "cover", "allowedPlacementOverrides": ["story"],
    })
    with pytest.raises(AdTemplateProcessError, match=r"feedLayout\.layers\[1\]\.mask must be one of rounded_rect"):
        process.validate_template_artifact(invalid_slot)

    invalid_crop = json.loads(json.dumps(invalid_slot))
    invalid_crop["feedLayout"]["layers"][1]["mask"] = "rounded_rect"
    with pytest.raises(AdTemplateProcessError, match=r"feedLayout\.layers\[1\]\.defaultCrop must contain exactly"):
        process.validate_template_artifact(invalid_crop)

    invalid_override = json.loads(json.dumps(invalid_crop))
    invalid_override["feedLayout"]["layers"][1]["defaultCrop"] = {"x": 0, "y": 0, "width": 1, "height": 1}
    with pytest.raises(AdTemplateProcessError, match=r"feedLayout\.layers\[1\]\.allowedPlacementOverrides\[0\] must be crop or position"):
        process.validate_template_artifact(invalid_override)

    invalid_text = json.loads(json.dumps(template))
    invalid_text["feedLayout"]["layers"].append({
        "type": "text", "layerId": "feed-headline", "inputKey": "headline",
        "font": {"file": "manrope-700.woff2"}, "fontSize": 32, "lineHeight": 1.1,
        "tracking": 0, "alignment": "center", "maxCharacters": 80, "maxLines": 2,
        "colourRole": "mainText", "overflowBehaviour": "ellipsis",
        "geometry": {"x": 10, "y": 10, "width": 400, "height": 100},
    })
    with pytest.raises(AdTemplateProcessError, match=r"feedLayout\.layers\[1\]\.overflowBehaviour must be refuse, truncate, or scale_down"):
        process.validate_template_artifact(invalid_text)

    invalid_line_height = json.loads(json.dumps(invalid_text))
    invalid_line_height["feedLayout"]["layers"][1]["overflowBehaviour"] = "truncate"
    invalid_line_height["feedLayout"]["layers"][1]["lineHeight"] = 29
    with pytest.raises(AdTemplateProcessError, match=r"feedLayout\.layers\[1\]\.lineHeight=29 must be a unitless multiplier between 0\.8 and 2\.5"):
        process.validate_template_artifact(invalid_line_height)

    invalid_tracking = json.loads(json.dumps(invalid_text))
    invalid_tracking["feedLayout"]["layers"][1]["overflowBehaviour"] = "truncate"
    invalid_tracking["feedLayout"]["layers"][1]["tracking"] = 400
    with pytest.raises(AdTemplateProcessError, match=r"tracking=400 must be sane native canvas pixels between -128 and 256, never an em multiplier"):
        process.validate_template_artifact(invalid_tracking)
    wide_tracking = json.loads(json.dumps(invalid_text))
    wide_tracking["feedLayout"]["layers"][1]["overflowBehaviour"] = "truncate"
    wide_tracking["feedLayout"]["layers"][1]["tracking"] = 36
    wide_tracking["textInputs"] = [{"key": "headline", "label": "Headline", "placeholder": "NEW", "maxLength": 80}]
    wide_tracking["fonts"] = [{"file": "manrope-700.woff2"}]
    assert process.validate_template_artifact(wide_tracking) is wide_tracking
    invalid_vector = json.loads(json.dumps(template))
    invalid_vector["feedLayout"]["layers"].append({
        "type": "vector", "layerId": "feed-rule",
        "geometry": {"x": 10, "y": 10, "width": 100, "height": 4},
        "shape": "rectangle", "colourRole": "accent", "opacity": 1,
    })
    with pytest.raises(AdTemplateProcessError, match=r'feedLayout\.layers\[1\]\.shape="rectangle" must be one of rect, rounded'):
        process.validate_template_artifact(invalid_vector)

    invalid_icon = json.loads(json.dumps(template))
    invalid_icon["feedLayout"]["layers"].append({
        "type": "icon", "layerId": "feed-phone",
        "geometry": {"x": 10, "y": 10, "width": 40, "height": 40},
        "icon": "fax", "colourRole": "mainText",
    })
    with pytest.raises(AdTemplateProcessError, match=r'feedLayout\.layers\[1\]\.icon="fax" must be one of arrow, check, phone, mail, globe, pin'):
        process.validate_template_artifact(invalid_icon)

    invalid_logo = json.loads(json.dumps(template))
    invalid_logo["feedLayout"]["layers"].append({
        "type": "logo", "layerId": "feed-logo", "inputKey": "brandLogo",
        "geometry": {"x": 10, "y": 10, "width": 100, "height": 100},
    })
    with pytest.raises(AdTemplateProcessError, match=r'feedLayout\.layers\[1\]\.inputKey="brandLogo" for logo is undeclared; declare it exactly once in imageInputs'):
        process.validate_template_artifact(invalid_logo)

    blank_logo = json.loads(json.dumps(invalid_logo))
    blank_logo["imageInputs"] = [{"key": "brandLogo", "label": "Brand logo", "acceptedTypes": ["image/png"]}]
    assert process.validate_template_artifact(blank_logo) is blank_logo

    real_text_ref = json.loads(json.dumps(template))
    real_text_ref["textInputs"] = [{"key": "address", "label": "Address", "placeholder": "1 Example St", "maxLength": 120}]
    real_text_ref["metadata"]["realAssetRefs"] = [{"inputKey": "address", "kind": "property_address", "required": True}]
    assert process.validate_template_artifact(real_text_ref) is real_text_ref

    invalid_real_ref = json.loads(json.dumps(template))
    invalid_real_ref["metadata"]["realAssetRefs"] = [{"inputKey": "missing", "kind": "property_photo", "required": True}]
    with pytest.raises(AdTemplateProcessError, match=r'metadata\.realAssetRefs\[0\]\.inputKey="missing" is undeclared'):
        process.validate_template_artifact(invalid_real_ref)

    duplicate_input = json.loads(json.dumps(real_text_ref))
    duplicate_input["imageInputs"] = [{"key": "address", "label": "Address image", "acceptedTypes": ["image/png"]}]
    with pytest.raises(AdTemplateProcessError, match=r'input key "address" must be unique across imageInputs and textInputs'):
        process.validate_template_artifact(duplicate_input)



def test_orchestrator_calls_real_roles_and_persists_receipts(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    renders = []
    monkeypatch.setattr(process, "run_generator_cli", lambda candidate, workspace: fake_render(candidate, workspace, renders))
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: {"template_id": "tpl_real", "status": "imported"})
    calls = []
    events = []
    def layout(placement, height):
        return {"placement": placement, "layers": [{"type": "plate", "layerId": f"{placement}-bg", "colourRole": "background", "geometry": {"x": 0, "y": 0, "width": 1080, "height": height}, "protected": False}], "safeZones": [{"x": 0, "y": 0, "width": 1080, "height": height}]}
    template = {"schema": "blockwise.ad-template", "templateId": "candidate", "createdAt": "2026-08-30T00:00:00.000Z", "feedLayout": layout("feed", 1350), "storyLayout": layout("story", 1920), "imageInputs": [], "textInputs": [], "semanticColours": semantic_colours(), "assets": {}, "fonts": [], "metadata": metadata()}

    def call_agent(instance, prompt, route):
        calls.append((instance, prompt, route))
        if instance.startswith("builder-"):
            return {"template": template, "assets": []}
        if instance.startswith("comparator-"):
            return evidence(9.6, "Composition is ready")
        return evidence(9.7, "Final review is ready")

    result = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_test",
        project_id="blockwise", emit=lambda kind, node, data: events.append((kind, data)),
    ).run(source=str(source), brief="test", placements=["square"], routes=[
        {"provider": "builder", "model": "cheap-a"},
        {"provider": "compare", "model": "cheap-b"},
        {"provider": "review-a", "model": "cheap-c"},
        {"provider": "review-b", "model": "cheap-d"},
    ])
    assert [item[0].split("-")[0] for item in calls] == ["builder", "comparator", "final", "final"]
    assert all(isinstance(item[1], list) for item in calls)
    assert len(calls[0][1]) == 2 and len(calls[1][1]) == 4 and len(calls[2][1]) == 4 and len(calls[3][1]) == 4
    assert all(part["type"] == "image_url" for part in calls[1][1][1:])
    assert len([item for item in events if item[0] == "iteration.compared"]) == 1
    completed = [item for item in events if item[0] == "final-review.completed"]
    assert len(completed) == 1 and len(completed[0][1]["reviewers"]) == 2
    assert [
        item["route"] for item in result["final_review"]["reviewers"]
    ] == ["review-a/cheap-c", "review-b/cheap-d"]
    assert result["import"] == {"template_id": "tpl_real", "status": "imported"}
    assert all(Path(item["path"]).is_file() for item in result["previews"])
    dimensions = {}
    for item in result["previews"]:
        raw = Path(item["path"]).read_bytes()
        assert raw[:8] == b"\x89PNG\r\n\x1a\n"
        dimensions[item["placement"]] = (int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big"))
    assert dimensions == {"feed": (1080, 1350), "story": (1080, 1920)}

def test_vision_roles_reject_filename_only_inputs(tmp_path):
    with pytest.raises(AdTemplateProcessError):
        vision_message("inspect this", [str(tmp_path / "filename-only.png")])


def test_bounded_vision_payload_preserves_useful_resolution_and_caps_bytes(tmp_path):
    source = tmp_path / "large.png"
    Image.effect_noise((2400, 2400), 100).convert("RGB").save(source, format="PNG")
    message = vision_message("compare", [str(source)], bounded=True)
    header, encoded = message[1]["image_url"]["url"].split(",", 1)
    payload = base64.b64decode(encoded)
    assert header == "data:image/jpeg;base64"
    assert len(payload) <= process.VISION_MAX_SERIALIZED_IMAGE_BYTES
    with Image.open(io.BytesIO(payload)) as rendered:
        assert max(rendered.size) <= process.VISION_MAX_LONG_EDGE
        assert min(rendered.size) >= 960


def test_blockwise_import_contract_uses_bearer_and_camel_case_receipt(monkeypatch, tmp_path):
    seen = {}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'{"templateId":"tpl-1","assetCount":2,"replayed":false}'
    def fake_urlopen(request, timeout):
        seen["authorization"] = request.headers.get("Authorization")
        seen["host"] = request.headers.get("Host")
        seen["body"] = json.loads(request.data.decode())
        return Response()
    monkeypatch.setenv("BLOCKWISE_TEMPLATE_IMPORT_URL", "http://blockwise.test/import")
    monkeypatch.setenv("BLOCKWISE_TEMPLATE_IMPORT_HOST", "blockwise.sale")
    monkeypatch.setenv("BLOCKWISE_INTERNAL_AUTH_SECRET", "secret")
    monkeypatch.setattr(process.urllib.request, "urlopen", fake_urlopen)
    feed = tmp_path / "feed.png"; story = tmp_path / "story.png"
    feed.write_bytes(b"feed-png"); story.write_bytes(b"story-png")
    layout = lambda placement, height: {"placement": placement, "layers": [{"type": "plate", "layerId": placement, "colourRole": "background", "geometry": {"x": 0, "y": 0, "width": 1080, "height": height}, "protected": False}], "safeZones": [{"x": 0, "y": 0, "width": 1080, "height": height}]}
    template = {"schema": "blockwise.ad-template", "templateId": "trun-test", "createdAt": "2026-08-30T00:00:00.000Z", "feedLayout": layout("feed", 1350), "storyLayout": layout("story", 1920), "imageInputs": [], "textInputs": [], "semanticColours": semantic_colours(), "assets": {"feed": {"fileName": "feed.png", "mimeType": "image/png"}, "story": {"fileName": "story.png", "mimeType": "image/png"}}, "fonts": [], "metadata": metadata()}
    output = {"template": template, "assets": [{"assetKey": "feed", "fileName": "feed.png", "mimeType": "image/png", "bytesBase64": "ZmVlZC1wbmc="}, {"assetKey": "story", "fileName": "story.png", "mimeType": "image/png", "bytesBase64": "c3RvcnktcG5n"}], "previews": [{"path": str(feed), "placement": "feed"}, {"path": str(story), "placement": "story"}]}
    receipt = process.import_template(output, run_id="trun-test", project_id="blockwise")
    assert seen["authorization"] == "Bearer secret"
    assert seen["host"] == "blockwise.sale"
    assert seen["body"]["template"]["schema"] == "blockwise.ad-template"
    assert "feedLayout" in seen["body"]["template"] and "storyLayout" in seen["body"]["template"]
    assert "version" not in seen["body"]["template"] and "inputs" not in seen["body"]["template"] and "Meta" not in seen["body"]["template"]
    assert set(seen["body"]) == {"template", "assets"}
    assert {asset["assetKey"] for asset in seen["body"]["assets"]} == {"feed", "story"}
    assert all(asset["bytesBase64"] for asset in seen["body"]["assets"])
    assert receipt == {"template_id": "tpl-1", "status": "imported", "asset_count": 2, "replayed": False}


def test_blockwise_replayed_import_is_a_valid_ready_receipt(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'{"templateId":"tpl-replay","assetCount":1,"replayed":true}'
    monkeypatch.setenv("BLOCKWISE_TEMPLATE_IMPORT_URL", "http://127.0.0.1:8080/import")
    monkeypatch.setenv("BLOCKWISE_INTERNAL_AUTH_SECRET", "secret")
    monkeypatch.setattr(process.urllib.request, "urlopen", lambda request, timeout: Response())
    layout = lambda placement, height: {"placement": placement, "layers": [{"type": "plate", "layerId": placement, "colourRole": "background", "geometry": {"x": 0, "y": 0, "width": 1080, "height": height}, "protected": False}], "safeZones": [{"x": 0, "y": 0, "width": 1080, "height": height}]}
    template = {"schema": "blockwise.ad-template", "templateId": "tpl-replay", "createdAt": "2026-08-30T00:00:00.000Z", "feedLayout": layout("feed", 1350), "storyLayout": layout("story", 1920), "imageInputs": [], "textInputs": [], "semanticColours": semantic_colours(), "assets": {"feed": {"fileName": "feed.png", "mimeType": "image/png"}}, "fonts": [], "metadata": metadata()}
    receipt = process.import_template(
        {"template": template, "assets": [{"assetKey": "feed", "fileName": "feed.png", "mimeType": "image/png", "bytesBase64": "ZmVlZA=="}], "previews": []},
        run_id="run-replay", project_id="blockwise",
    )
    assert receipt == {"template_id": "tpl-replay", "status": "replayed", "asset_count": 1, "replayed": True}


def test_blockwise_import_host_rejects_header_injection(monkeypatch):
    monkeypatch.setenv("BLOCKWISE_TEMPLATE_IMPORT_URL", "http://127.0.0.1:8080/import")
    monkeypatch.setenv("BLOCKWISE_TEMPLATE_IMPORT_HOST", "blockwise.sale\r\nX-Bad: yes")
    monkeypatch.setenv("BLOCKWISE_INTERNAL_AUTH_SECRET", "secret")
    with pytest.raises(AdTemplateProcessError):
        process.import_template({"template": {}, "assets": []}, run_id="run", project_id="blockwise")


def test_late_builder_return_cannot_render_or_import(tmp_path, monkeypatch):
    """A cooperative stop observed after a role returns gates all side effects."""
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    stopped = {"value": False}
    rendered = []
    imported = []

    def call_agent(instance, prompt, route):
        assert instance == "builder-1"
        assert prompt[1]["image_url"]["url"].startswith("data:image/png;base64,")
        stopped["value"] = True
        return {"template": {}, "assets": []}

    monkeypatch.setattr(
        process,
        "run_generator_cli",
        lambda candidate, workspace: rendered.append((candidate, workspace)),
    )
    monkeypatch.setattr(
        process,
        "import_template",
        lambda output, run_id, project_id: imported.append(output),
    )

    orchestrator = SoleProcessOrchestrator(
        call_agent=call_agent,
        workspace=tmp_path / "run",
        run_id="trun_cancelled",
        project_id="blockwise",
        emit=lambda *_args: None,
        should_stop=lambda: stopped["value"],
    )
    with pytest.raises(AdTemplateProcessError, match="cancelled"):
        orchestrator.run(
            source=str(source),
            brief="",
            placements=["feed", "story"],
            routes=[
                {"provider": "openai-codex", "model": "gpt-5.6-sol"},
                {"provider": "openai-codex", "model": "gpt-5.6-luna"},
                {"provider": "deepseek", "model": "deepseek-v4-flash-vision-exp"},
                {"provider": "openai-codex", "model": "gpt-5.6-luna"},
            ],
        )

    assert rendered == []
    assert imported == []


def test_schema_invalid_candidate_repairs_before_one_render_and_comparator(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    bad = json.loads(json.dumps(valid_candidate("bad")))
    bad["template"]["feedLayout"]["safeZones"][0] = {"x": 48, "y": 48, "right": 1032, "bottom": 1302}
    bad["template"]["metadata"]["private"] = "must-not-persist"
    bad["template"]["metadata"]["hash"] = "must-not-persist"
    bad["template"]["metadata"]["path"] = "/tmp/private-source.png"
    bad["template"]["metadata"]["renderPath"] = "/tmp/private-render.png"
    bad["template"]["metadata"]["dataUri"] = "data:image/png;base64,c2VjcmV0"
    bad["template"]["metadata"]["base64"] = "c2VjcmV0"
    bad["template"]["metadata"]["accessToken"] = "secret-access"
    bad["template"]["metadata"]["apiToken"] = "secret-api"
    good = valid_candidate("good")
    calls, renders, imports, events = [], [], [], []

    def call_agent(instance, prompt, route):
        calls.append((instance, prompt, route))
        if instance == "builder-1":
            return bad
        if instance == "builder-1-repair-1":
            return good
        if instance == "comparator-1":
            return evidence(9.6, "Valid Feed and Story match")
        return evidence(9.7, "Independent final pass")

    monkeypatch.setattr(process, "run_generator_cli", lambda candidate, workspace: fake_render(candidate, workspace, renders))
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: imports.append(output) or {"template_id": "tpl-good", "status": "imported"})
    result = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_repair",
        project_id="blockwise", emit=lambda kind, node, data: events.append((kind, node, data)),
    ).run(source=str(source), brief="", placements=["feed", "story"], routes=[
        {"provider": "builder", "model": "vision"},
        {"provider": "compare", "model": "vision"},
        {"provider": "review-a", "model": "vision"},
        {"provider": "review-b", "model": "vision"},
    ])

    assert [item[0] for item in calls[:3]] == ["builder-1", "builder-1-repair-1", "comparator-1"]
    repair_prompt = calls[1][1][0]["text"]
    exact_reason = "feedLayout.safeZones[0] must contain exactly x, y, width, and height (missing height, width; unexpected bottom, right)"
    assert exact_reason in repair_prompt
    assert "IMMEDIATELY PRIOR REJECTED CANDIDATE" in repair_prompt
    assert '"templateId":"bad"' in repair_prompt
    assert '"safeZones":[{"x":48,"y":48}]' in repair_prompt
    assert '"right":' not in repair_prompt and '"bottom":' not in repair_prompt
    assert "must-not-persist" not in repair_prompt
    assert '"private":' not in repair_prompt and '"hash":' not in repair_prompt
    for forbidden in ("path", "renderPath", "dataUri", "base64", "accessToken", "apiToken"):
        assert f'"{forbidden}":' not in repair_prompt
    assert "data:image/png;base64" not in repair_prompt
    assert len(renders) == 1 and renders[0]["template"]["templateId"] == "good"
    assert len([item for item in calls if item[0].startswith("comparator-")]) == 1
    assert len(imports) == 1 and result["import"]["template_id"] == "tpl-good"
    kinds = [item[0] for item in events]
    assert kinds.index("candidate.rejected") < kinds.index("iteration.started") < kinds.index("iteration.rendered") < kinds.index("iteration.compared")
    rejected = next(item[2] for item in events if item[0] == "candidate.rejected")
    assert rejected == {"iteration": 1, "attempt": 1, "reason": exact_reason, "decision": "repair"}
    evidence_path = tmp_path / "run" / "iterations" / "01" / "rejected-candidate-01.json"
    persisted = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert persisted["candidate"]["template"]["feedLayout"]["safeZones"][0] == {"x": 48, "y": 48}
    assert "private" not in persisted["candidate"]["template"]["metadata"]
    assert "hash" not in persisted["candidate"]["template"]["metadata"]
    persisted_blob = json.dumps(persisted, sort_keys=True)
    for forbidden in ("path", "renderPath", "dataUri", "base64", "accessToken", "apiToken"):
        assert f'"{forbidden}":' not in persisted_blob
    assert "data:image/png;base64" not in persisted_blob


def test_initial_non_json_builder_output_retries_cheap_then_escalates_without_extra_reviews(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    good = valid_candidate("structured-recovery")
    calls, renders, imports, events = [], [], [], []

    def call_agent(instance, prompt, route):
        calls.append((instance, route))
        if instance.startswith("builder-"):
            if len([item for item in calls if item[0].startswith("builder-")]) <= 2:
                raise process.AdTemplateStructuredOutputError(
                    "Builder did not return one structured JSON result"
                )
            return good
        if instance.startswith("comparator-"):
            return evidence(9.6, "Recovered candidate passes")
        return evidence(9.7, "Independent final pass")

    monkeypatch.setattr(
        process,
        "run_generator_cli",
        lambda candidate, workspace: fake_render(candidate, workspace, renders),
    )
    monkeypatch.setattr(
        process,
        "import_template",
        lambda output, run_id, project_id: imports.append(output)
        or {"template_id": "tpl-structured-recovery", "status": "imported"},
    )
    result = SoleProcessOrchestrator(
        call_agent=call_agent,
        workspace=tmp_path / "run",
        run_id="trun_structured_recovery",
        project_id="blockwise",
        emit=lambda kind, node, data: events.append((kind, node, data)),
    ).run(
        source=str(source),
        brief="",
        placements=["feed", "story"],
        routes=[
            {"provider": "openai-codex", "model": "gpt-5.6-luna"},
            {"provider": "openai-codex", "model": "gpt-5.6-luna"},
            {"provider": "deepseek", "model": "deepseek-v4-flash-vision-exp"},
            {"provider": "openai-codex", "model": "gpt-5.6-luna"},
            {"provider": "openai-codex", "model": "gpt-5.6-sol"},
        ],
        require_quality_route=True,
    )

    assert calls[:3] == [
        ("builder-1", "openai-codex/gpt-5.6-luna"),
        ("builder-1-output-retry-1", "openai-codex/gpt-5.6-luna"),
        ("builder-1-output-retry-2", "openai-codex/gpt-5.6-sol"),
    ]
    assert len([item for item in calls if item[0].startswith("comparator-")]) == 1
    assert len([item for item in calls if item[0].startswith("final-reviewer-")]) == 2
    assert len(renders) == 1 and len(imports) == 1
    assert result["builder_escalated"] is True
    assert result["builder_route"] == {
        "provider": "openai-codex",
        "model": "gpt-5.6-sol",
    }
    retry = next(item for item in events if item[0] == "builder.output-retry")
    assert retry[2] == {
        "iteration": 1,
        "attempt": 2,
        "provider": "openai-codex",
        "model": "gpt-5.6-luna",
        "reason": "structured_output_invalid",
    }
    escalation = next(item for item in events if item[0] == "builder.escalated")
    assert escalation[2]["reason"] == "structured_output_invalid"


def test_invalid_builder_contract_retries_cheap_then_uses_quality_builder(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    bad = valid_candidate("invalid-contract")
    bad["template"]["storyLayout"]["safeZones"] = [
        {"x": 0, "y": 0, "width": 1080, "height": 1921}
    ]
    good = valid_candidate("quality-contract")
    calls, renders, events = [], [], []

    def call_agent(instance, prompt, route):
        calls.append((instance, route))
        if instance in {"builder-1", "builder-1-repair-1"}:
            return json.loads(json.dumps(bad))
        if instance == "builder-1-repair-2":
            return good
        if instance.startswith("comparator-"):
            return evidence(9.6, "Quality builder candidate passes")
        return evidence(9.7, "Independent final pass")

    monkeypatch.setattr(
        process,
        "run_generator_cli",
        lambda candidate, workspace: fake_render(candidate, workspace, renders),
    )
    monkeypatch.setattr(
        process,
        "import_template",
        lambda output, run_id, project_id: {
            "template_id": "tpl-quality-contract",
            "status": "imported",
        },
    )
    SoleProcessOrchestrator(
        call_agent=call_agent,
        workspace=tmp_path / "run",
        run_id="trun_contract_recovery",
        project_id="blockwise",
        emit=lambda kind, node, data: events.append((kind, node, data)),
    ).run(
        source=str(source),
        brief="",
        placements=["feed", "story"],
        routes=[
            {"provider": "openai-codex", "model": "gpt-5.6-luna"},
            {"provider": "openai-codex", "model": "gpt-5.6-luna"},
            {"provider": "deepseek", "model": "deepseek-v4-flash-vision-exp"},
            {"provider": "openai-codex", "model": "gpt-5.6-luna"},
            {"provider": "openai-codex", "model": "gpt-5.6-sol"},
        ],
        require_quality_route=True,
    )

    assert calls[:3] == [
        ("builder-1", "openai-codex/gpt-5.6-luna"),
        ("builder-1-repair-1", "openai-codex/gpt-5.6-luna"),
        ("builder-1-repair-2", "openai-codex/gpt-5.6-sol"),
    ]
    assert len([item for item in calls if item[0].startswith("comparator-")]) == 1
    assert len([item for item in calls if item[0].startswith("final-reviewer-")]) == 2
    assert len(renders) == 1
    escalation = next(item for item in events if item[0] == "builder.escalated")
    assert escalation[2]["reason"] == "candidate_contract_invalid"


def test_visual_revision_prompt_carries_prior_valid_candidate(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    first = valid_candidate("visual-v1")
    second = valid_candidate("visual-v2")
    calls, renders, imports = [], [], []

    def call_agent(instance, prompt, route):
        calls.append((instance, prompt[0]["text"]))
        if instance == "builder-1":
            return first
        if instance == "comparator-1":
            return evidence(9.0, "Layout needs revision")
        if instance == "builder-2":
            return second
        if instance == "comparator-2":
            return evidence(9.6, "Revision matches", best_available=True)
        return evidence(9.7, "Independent final pass")

    def render_with_private_runtime_fields(candidate, workspace):
        output = fake_render(candidate, workspace, renders)
        output["assets"] = [{
            "assetKey": "hero", "fileName": "home/open-home-living.webp", "mimeType": "image/webp",
            "bytesBase64": "c2VjcmV0", "dataUri": "data:image/webp;base64,c2VjcmV0",
            "base64": "c2VjcmV0", "accessToken": "secret-access", "apiToken": "secret-api",
            "path": str(workspace / "asset.webp"), "renderPath": str(workspace / "render.webp"),
        }]
        output["receipt"] = {"path": str(workspace / "receipt.json"), "accessToken": "secret-access"}
        output["dataUri"] = "data:image/png;base64,c2VjcmV0"
        return output

    monkeypatch.setattr(process, "run_generator_cli", render_with_private_runtime_fields)
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: imports.append(output) or {"template_id": "tpl-visual-v2", "status": "imported"})
    result = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_visual_revision",
        project_id="blockwise", emit=lambda *_args: None,
    ).run(source=str(source), brief="", placements=["feed", "story"], routes=[
        {"provider": "builder", "model": "vision"},
        {"provider": "compare", "model": "vision"},
        {"provider": "review-a", "model": "vision"},
        {"provider": "review-b", "model": "vision"},
    ])

    builder_one = next(text for instance, text in calls if instance == "builder-1")
    builder_two = next(text for instance, text in calls if instance == "builder-2")
    assert "PRIOR VALID CANDIDATE TO REVISE IN PLACE" not in builder_one
    assert "PRIOR VALID CANDIDATE TO REVISE IN PLACE" in builder_two
    assert '"templateId":"visual-v1"' in builder_two
    assert "Layout needs revision" in builder_two
    assert str(tmp_path) not in builder_two
    for forbidden in ("bytesBase64", "dataUri", "base64", "accessToken", "apiToken", "path", "renderPath"):
        assert f'"{forbidden}":' not in builder_two
    assert len(result["iterations"]) == 2
    trace_blob = json.dumps(result["iterations"], sort_keys=True)
    for forbidden in ("bytesBase64", "dataUri", "base64", "accessToken", "apiToken", "path", "renderPath"):
        assert f'"{forbidden}":' not in trace_blob
    assert str(tmp_path) not in trace_blob
    for index, item in enumerate(result["iterations"], start=1):
        assert item["candidate"]["previews"] == [
            {"name": f"iteration-{index:02d}-feed.png", "placement": "feed"},
            {"name": f"iteration-{index:02d}-story.png", "placement": "story"},
        ]
    assert result["import"]["template_id"] == "tpl-visual-v2" and len(imports) == 1
    assert imports[0]["assets"][0]["bytesBase64"] == "c2VjcmV0"
    assert imports[0]["assets"][0]["path"].startswith(str(tmp_path))
    assert imports[0]["receipt"]["accessToken"] == "secret-access"


def test_final_review_revision_prompt_carries_accepted_candidate(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    first = valid_candidate("final-v1")
    second = valid_candidate("final-v2")
    calls, renders, imports = [], [], []
    final_calls = {"count": 0}

    def call_agent(instance, prompt, route):
        calls.append((instance, prompt[0]["text"]))
        if instance == "builder-1":
            return first
        if instance == "builder-2":
            return second
        if instance.startswith("comparator-"):
            return evidence(9.6, "Comparator pass", best_available=instance != "comparator-1")
        final_calls["count"] += 1
        if final_calls["count"] == 1:
            return evidence(9.0, "Final spacing needs revision")
        return evidence(9.7, "Independent final pass")

    monkeypatch.setattr(process, "run_generator_cli", lambda candidate, workspace: fake_render(candidate, workspace, renders))
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: imports.append(output) or {"template_id": "tpl-final-v2", "status": "imported"})
    result = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_final_revision",
        project_id="blockwise", emit=lambda *_args: None,
    ).run(source=str(source), brief="", placements=["feed", "story"], routes=[
        {"provider": "builder", "model": "vision"},
        {"provider": "compare", "model": "vision"},
        {"provider": "review-a", "model": "vision-a"},
        {"provider": "review-b", "model": "vision-b"},
    ])

    builder_two = next(text for instance, text in calls if instance == "builder-2")
    assert "PRIOR VALID CANDIDATE TO REVISE IN PLACE" in builder_two
    assert '"templateId":"final-v1"' in builder_two
    assert "Final spacing needs revision" in builder_two
    assert str(tmp_path) not in builder_two
    assert result["iterations"][0]["final_review_failed"] is True
    assert result["iterations"][1]["decision"] == "accepted"
    assert final_calls["count"] == 4
    assert result["import"]["template_id"] == "tpl-final-v2" and len(imports) == 1


def test_schema_repair_is_bounded_and_invalid_candidates_have_no_visual_side_effects(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    bad = valid_candidate("always-bad")
    bad["template"]["storyLayout"]["safeZones"] = [{"x": 0, "y": 0, "width": 1080, "height": 1921}]
    calls, renders, imports, events = [], [], [], []

    def call_agent(instance, prompt, route):
        calls.append(instance)
        return json.loads(json.dumps(bad))

    monkeypatch.setattr(process, "run_generator_cli", lambda candidate, workspace: renders.append(candidate))
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: imports.append(output))
    orchestrator = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_bounded",
        project_id="blockwise", emit=lambda kind, node, data: events.append((kind, data)),
    )
    with pytest.raises(AdTemplateProcessError, match="schema-invalid after 3 repairs"):
        orchestrator.run(source=str(source), brief="", placements=["feed", "story"], routes=[
            {"provider": "builder", "model": "vision"},
            {"provider": "compare", "model": "vision"},
            {"provider": "review-a", "model": "vision"},
            {"provider": "review-b", "model": "vision"},
        ])

    assert calls == ["builder-1", "builder-1-repair-1", "builder-1-repair-2", "builder-1-repair-3"]
    assert renders == [] and imports == []
    assert [item[0] for item in events].count("candidate.rejected") == 4
    assert not any(item[0] in {"iteration.started", "iteration.rendered", "iteration.compared", "final-review.started"} for item in events)
    assert len(list((tmp_path / "run" / "iterations" / "01").glob("rejected-candidate-*.json"))) == 4


def test_stop_during_schema_repair_prevents_render_import_and_rejection_write(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    bad = valid_candidate("bad")
    bad["template"]["feedLayout"]["safeZones"] = [{"x": -1, "y": 0, "width": 1080, "height": 1350}]
    stopped = {"value": False}
    calls, renders, imports, events = [], [], [], []

    def call_agent(instance, prompt, route):
        calls.append(instance)
        if instance == "builder-1":
            return bad
        stopped["value"] = True
        return valid_candidate("late-good")

    monkeypatch.setattr(process, "run_generator_cli", lambda candidate, workspace: renders.append(candidate))
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: imports.append(output))
    orchestrator = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_stop_repair",
        project_id="blockwise", emit=lambda kind, node, data: events.append((kind, data)),
        should_stop=lambda: stopped["value"],
    )
    with pytest.raises(AdTemplateProcessError, match="cancelled"):
        orchestrator.run(source=str(source), brief="", placements=["feed", "story"], routes=[
            {"provider": "builder", "model": "vision"},
            {"provider": "compare", "model": "vision"},
            {"provider": "review-a", "model": "vision"},
            {"provider": "review-b", "model": "vision"},
        ])

    assert calls == ["builder-1", "builder-1-repair-1"]
    assert renders == [] and imports == []
    assert [item[0] for item in events].count("candidate.rejected") == 1
    assert not (tmp_path / "run" / "iterations" / "01" / "rejected-candidate-02.json").exists()



def _quality_routes():
    return [
        {"provider": "openai-codex", "model": "gpt-5.6-luna"},
        {"provider": "openai-codex", "model": "gpt-5.6-luna"},
        {"provider": "deepseek", "model": "deepseek-v4-flash-vision-exp"},
        {"provider": "openai-codex", "model": "gpt-5.6-luna"},
        {"provider": "openai-codex", "model": "gpt-5.6-sol"},
    ]


def test_builder_quality_escalation_ignores_steady_material_improvement(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    scores = iter([7.0, 7.5, 8.0, 9.6])
    builder_calls, events, renders = [], [], []

    def call_agent(instance, prompt, route):
        if instance.startswith("builder-"):
            builder_calls.append((instance, route, prompt[0]["text"]))
            return valid_candidate(f"steady-{len(builder_calls)}")
        if instance.startswith("comparator-"):
            return evidence(next(scores), "Material improvement", best_available=instance != "comparator-1")
        return evidence(9.7, "Independent final pass")

    monkeypatch.setattr(process, "run_generator_cli", lambda candidate, workspace: fake_render(candidate, workspace, renders))
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: {"template_id": "tpl-steady", "status": "imported"})
    result = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_steady",
        project_id="blockwise", emit=lambda kind, node, data: events.append((kind, data)),
    ).run(source=str(source), brief="", placements=["feed", "story"], routes=_quality_routes(), require_quality_route=True)

    assert [route for _, route, _ in builder_calls] == ["openai-codex/gpt-5.6-luna"] * 4
    assert not any(kind == "builder.escalated" for kind, _ in events)
    assert result["builder_escalated"] is False
    assert len(result["iterations"]) == 4
    assert all(item["builder_route"]["model"] == "gpt-5.6-luna" for item in result["iterations"])


@pytest.mark.parametrize(
    ("scores", "event_iteration"),
    [([8.0, 7.9, 9.6], 2), ([8.0, 8.2, 8.4, 9.6], 3)],
)
def test_builder_quality_escalates_on_regression_or_two_low_gains(tmp_path, monkeypatch, scores, event_iteration):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    score_iter = iter(scores)
    builder_calls, events, renders = [], [], []

    def call_agent(instance, prompt, route):
        if instance.startswith("builder-"):
            builder_calls.append((instance, route, prompt[0]["text"]))
            return valid_candidate(f"candidate-{instance}")
        if instance.startswith("comparator-"):
            return evidence(next(score_iter), f"comparison {instance}", best_available=instance != "comparator-1")
        return evidence(9.7, "Independent final pass")

    monkeypatch.setattr(process, "run_generator_cli", lambda candidate, workspace: fake_render(candidate, workspace, renders))
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: {"template_id": "tpl-escalated", "status": "imported"})
    result = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_escalate",
        project_id="blockwise", emit=lambda kind, node, data: events.append((kind, data)),
    ).run(source=str(source), brief="", placements=["feed", "story"], routes=_quality_routes(), require_quality_route=True)

    escalations = [data for kind, data in events if kind == "builder.escalated"]
    assert len(escalations) == 1
    assert escalations[0] == {
        "iteration": event_iteration,
        "from_provider": "openai-codex", "from_model": "gpt-5.6-luna",
        "to_provider": "openai-codex", "to_model": "gpt-5.6-sol",
        "reason": "regression" if event_iteration == 2 else "insufficient_improvement",
        "previous_score": scores[event_iteration - 2], "score": scores[event_iteration - 1],
    }
    assert [route for _, route, _ in builder_calls[:event_iteration]] == ["openai-codex/gpt-5.6-luna"] * event_iteration
    assert [route for _, route, _ in builder_calls[event_iteration:]] == ["openai-codex/gpt-5.6-sol"] * (len(builder_calls) - event_iteration)
    assert result["builder_escalated"] is True
    assert result["builder_route"]["model"] == "gpt-5.6-sol"
    assert len(result["iterations"]) == len(scores)


def test_regressed_candidate_is_traced_but_next_revision_uses_best_candidate(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    scores = iter([8.6, 8.1, 9.6])
    calls, renders, prompt_lengths = [], [], {}

    def call_agent(instance, prompt, route):
        calls.append((instance, route, prompt[0]["text"]))
        prompt_lengths[instance] = len(prompt)
        if instance.startswith("builder-"):
            return valid_candidate(f"candidate-{instance}")
        if instance.startswith("comparator-"):
            score = next(scores)
            result = evidence(score, f"comparison {instance}", best_available=instance != "comparator-1")
            if score < 8.6:
                result["visual_evidence"]["best_so_far"].update(
                    macro_topology="regressed",
                    preferred="best",
                )
            return result
        return evidence(9.7, "Independent final pass")

    monkeypatch.setattr(process, "run_generator_cli", lambda candidate, workspace: fake_render(candidate, workspace, renders))
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: {"template_id": "tpl-best", "status": "imported"})
    result = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_best",
        project_id="blockwise", emit=lambda *_args: None,
    ).run(source=str(source), brief="", placements=["feed", "story"], routes=_quality_routes(), require_quality_route=True)

    builder_prompts = {instance: prompt for instance, _, prompt in calls if instance.startswith("builder-")}
    assert '"templateId":"candidate-builder-1"' in builder_prompts["builder-2"]
    assert '"templateId":"candidate-builder-1"' in builder_prompts["builder-3"]
    assert '"templateId":"candidate-builder-2"' not in builder_prompts["builder-3"]
    assert [route for instance, route, _ in calls if instance.startswith("builder-")] == [
        "openai-codex/gpt-5.6-luna",
        "openai-codex/gpt-5.6-luna",
        "openai-codex/gpt-5.6-sol",
    ]
    assert [item["candidate"]["template"]["templateId"] for item in result["iterations"]] == [
        "candidate-builder-1", "candidate-builder-2", "candidate-builder-3",
    ]
    assert [item["comparison"]["score"] for item in result["iterations"]] == [8.6, 8.1, 9.6]
    assert prompt_lengths["comparator-1"] == 4
    assert prompt_lengths["comparator-2"] == 6
    assert prompt_lengths["comparator-3"] == 6
    final_prompt_lengths = [length for instance, length in prompt_lengths.items() if instance.startswith("final-reviewer-")]
    assert final_prompt_lengths == [4, 4]
    assert [instance.split("-")[0] for instance, _, _ in calls] == [
        "builder", "comparator", "builder", "comparator", "builder", "comparator", "final", "final",
    ]


def test_final_review_revision_keeps_best_candidate_when_next_score_regresses(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    scores = iter([9.54, 9.28, 9.6])
    final_scores = iter([9.0, 9.7, 9.7, 9.7])
    calls, renders = [], []

    def call_agent(instance, prompt, route):
        calls.append((instance, route, prompt[0]["text"]))
        if instance.startswith("builder-"):
            return valid_candidate(f"candidate-{instance}")
        if instance.startswith("comparator-"):
            score = next(scores)
            result = evidence(score, f"comparison {instance}", best_available=instance != "comparator-1")
            if score < 9.5 and instance != "comparator-1":
                result["visual_evidence"]["best_so_far"].update(
                    micro_typography_legibility="regressed",
                    preferred="best",
                )
            return result
        score = next(final_scores)
        return evidence(score, "Revise spacing" if score < 9.5 else "Independent final pass")

    monkeypatch.setattr(process, "run_generator_cli", lambda candidate, workspace: fake_render(candidate, workspace, renders))
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: {"template_id": "tpl-final-best", "status": "imported"})
    result = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_final_best",
        project_id="blockwise", emit=lambda *_args: None,
    ).run(source=str(source), brief="", placements=["feed", "story"], routes=_quality_routes(), require_quality_route=True)

    builder_prompts = {instance: prompt for instance, _, prompt in calls if instance.startswith("builder-")}
    assert '"templateId":"candidate-builder-1"' in builder_prompts["builder-2"]
    assert '"templateId":"candidate-builder-1"' in builder_prompts["builder-3"]
    assert '"templateId":"candidate-builder-2"' not in builder_prompts["builder-3"]
    assert [item["candidate"]["template"]["templateId"] for item in result["iterations"]] == [
        "candidate-builder-1", "candidate-builder-2", "candidate-builder-3",
    ]
    assert result["iterations"][0]["final_review_failed"] is True
    assert [route for instance, route, _ in calls if instance.startswith("builder-")] == [
        "openai-codex/gpt-5.6-luna",
        "openai-codex/gpt-5.6-luna",
        "openai-codex/gpt-5.6-sol",
    ]


def test_builder_quality_escalation_is_sticky_through_repairs_and_final_review_revision(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    scores = iter([8.0, 7.9, 9.6, 9.6])
    builder_calls, events, renders = [], [], []
    final_calls = {"count": 0}

    invalid_quality = valid_candidate("quality-invalid")
    invalid_quality["template"]["storyLayout"]["safeZones"] = [{"x": 0, "y": 0, "width": 1080, "height": 1921}]

    def call_agent(instance, prompt, route):
        if instance.startswith("builder-"):
            builder_calls.append((instance, route, prompt[0]["text"]))
            if instance == "builder-3":
                return invalid_quality
            return valid_candidate(f"candidate-{instance}")
        if instance.startswith("comparator-"):
            score = next(scores)
            result = evidence(score, f"comparison {instance}", best_available=instance != "comparator-1")
            if score < 8.0 and instance != "comparator-1":
                result["visual_evidence"]["best_so_far"].update(
                    macro_topology="regressed",
                    preferred="best",
                )
            return result
        final_calls["count"] += 1
        if final_calls["count"] == 1:
            return evidence(9.0, "Final spacing needs revision")
        return evidence(9.7, "Independent final pass")

    monkeypatch.setattr(process, "run_generator_cli", lambda candidate, workspace: fake_render(candidate, workspace, renders))
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: {"template_id": "tpl-sticky", "status": "imported"})
    result = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_sticky",
        project_id="blockwise", emit=lambda kind, node, data: events.append((kind, data)),
    ).run(source=str(source), brief="", placements=["feed", "story"], routes=_quality_routes(), require_quality_route=True)

    assert [instance for instance, _, _ in builder_calls] == [
        "builder-1", "builder-2", "builder-3", "builder-3-repair-1", "builder-4",
    ]
    assert [route for _, route, _ in builder_calls] == [
        "openai-codex/gpt-5.6-luna", "openai-codex/gpt-5.6-luna",
        "openai-codex/gpt-5.6-sol", "openai-codex/gpt-5.6-sol", "openai-codex/gpt-5.6-sol",
    ]
    assert len([1 for kind, _ in events if kind == "builder.escalated"]) == 1
    assert len(result["iterations"]) == 4
    assert [item["iteration"] for item in result["iterations"]] == [1, 2, 3, 4]
    assert result["iterations"][2]["final_review_failed"] is True
    assert result["iterations"][3]["builder_escalated"] is True
    builder_four_prompt = next(prompt for instance, _, prompt in builder_calls if instance == "builder-4")
    assert '"templateId":"candidate-builder-3-repair-1"' in builder_four_prompt
    assert "Final spacing needs revision" in builder_four_prompt
    assert result["builder_route"]["model"] == "gpt-5.6-sol"


def test_required_quality_route_fails_before_any_builder_call(tmp_path):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    calls = []
    orchestrator = SoleProcessOrchestrator(
        call_agent=lambda *args: calls.append(args), workspace=tmp_path / "run",
        run_id="trun_missing_quality", project_id="blockwise", emit=lambda *_args: None,
    )
    with pytest.raises(AdTemplateProcessError, match="requires a configured quality route"):
        orchestrator.run(
            source=str(source), brief="", placements=["feed", "story"],
            routes=_quality_routes()[:4], require_quality_route=True,
        )
    assert calls == []
