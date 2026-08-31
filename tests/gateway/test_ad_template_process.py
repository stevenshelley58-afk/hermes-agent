import json
import os
import shlex
import sys
from pathlib import Path
import pytest
import gateway.ad_template_process as process
from gateway.ad_template_process import (
    AdTemplateProcessError, deterministic_documents, import_template,
    validate_artifacts, validate_final_review, validate_iterations, SoleProcessOrchestrator, vision_message,
)
from gateway.tool_run_api import ToolRunAPIMixin

def evidence(score=9.4, reason="Improve spacing"):
    actionable_change = (
        "placement=feed; layers=feed-bg; "
        "current={x:0,y:0,width:1080,height:1350}; "
        "target={x:0,y:0,width:1080,height:1350}; "
        f"change={reason}"
    )
    return {
        "rubric": {field: score for field in process.RUBRIC_FIELDS},
        "reason": reason,
        "differences": [reason] if score < 9.5 else [],
        "required_changes": [actionable_change] if score < 9.5 else [],
        "hard_failures": [],
    }

def iteration(score=9.4, number=1):
    return {"iteration": number, "comparison": evidence(score), "decision": "accepted" if score >= 9.5 else "revise"}

def test_only_real_stages_and_durable_source_preview(tmp_path):
    assert process.STAGES == ("source", "build", "render", "compare", "final-check", "live")
    assert ToolRunAPIMixin._tool_stage_order() == list(process.STAGES)
    assert ToolRunAPIMixin._canonical_tool_stage("story-draft") == "build"
    assert ToolRunAPIMixin._canonical_tool_stage("final-review") == "final-check"
    assert ToolRunAPIMixin._tool_stage_from_process_event("final-review.retried", "final-check") == "final-check"
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
    feed.write_bytes(b"feed")
    story.write_bytes(b"story")
    return {
        "template": candidate["template"], "assets": candidate["assets"],
        "previews": [
            {"name": feed.name, "path": str(feed), "placement": "feed"},
            {"name": story.name, "path": str(story), "placement": "story"},
        ],
        "render": {"feed": str(feed), "story": str(story)},
        "template_path": str(workspace / "artifact.json"),
    }

def overlap_candidate(template_id="overlap-candidate"):
    candidate = valid_candidate(template_id)
    candidate["template"]["imageInputs"] = [
        {"key": "hero", "label": "Hero", "acceptedTypes": ["image/jpeg"]},
        {"key": "thumb", "label": "Thumbnail", "acceptedTypes": ["image/jpeg"]},
    ]
    candidate["template"]["feedLayout"]["layers"].extend([
        {
            "type": "image_slot", "layerId": "feed-hero", "inputKey": "hero",
            "geometry": {"x": 72, "y": 184, "width": 600, "height": 400}, "mask": "none",
            "minSourceWidth": 600, "minSourceHeight": 400,
            "defaultCrop": {"x": 0, "y": 0, "width": 1, "height": 1},
            "allowedPlacementOverrides": ["crop", "position"],
        },
        {
            "type": "vector", "layerId": "feed-price-panel",
            "geometry": {"x": 72, "y": 184, "width": 600, "height": 400},
            "shape": "rect", "colourRole": "primary", "opacity": 0.9,
        },
        {
            "type": "image_slot", "layerId": "feed-thumb-1", "inputKey": "thumb",
            "geometry": {"x": 72, "y": 584, "width": 180, "height": 220}, "mask": "none",
            "minSourceWidth": 180, "minSourceHeight": 220,
            "defaultCrop": {"x": 0, "y": 0, "width": 1, "height": 1},
            "allowedPlacementOverrides": ["crop", "position"],
        },
    ])
    return candidate

def test_one_comparator_per_iteration_and_final_review_only_after_pass():
    assert validate_iterations([iteration()])[0]["comparison"]["score"] == 9.4
    with pytest.raises(AdTemplateProcessError):
        validate_iterations([{"iteration": 1, "comparison": {"score": 9.4, "reason": "x"}, "reviewers": []}])
    review = validate_final_review({"reviewers": [{"id": "reviewer-a", "route": "a/m", **evidence(9.6, "good")}, {"id": "reviewer-b", "route": "b/m", **evidence(9.7, "good")}]}, accepted=True)
    assert review["decision"] == "accepted"

def test_quality_gate_rejects_one_weak_dimension_even_when_mean_passes():
    weak = evidence(9.6, "Delivered-size copy remains too small")
    weak["rubric"]["layout_geometry"] = 9.1
    record = validate_iterations([{"iteration": 1, "comparison": weak, "decision": "revise"}])[0]
    assert record["comparison"]["score"] >= process.THRESHOLD
    assert record["comparison"]["minimum_score"] == 9.1
    assert record["decision"] == "revise"

    reviewers = [
        {"id": "reviewer-a", "route": "a/m", **weak},
        {"id": "reviewer-b", "route": "b/m", **evidence(9.8, "Strong")},
    ]
    assert validate_final_review({"reviewers": reviewers}, accepted=True)["decision"] == "revise"


def test_autonomous_loop_can_converge_after_thirty_iterations(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    scores = iter([9.0] * 30 + [9.7])
    renders = []

    def call_agent(instance, prompt, route):
        if instance.startswith("builder-"):
            return valid_candidate(f"candidate-{instance}")
        if instance.startswith("comparator-"):
            return evidence(next(scores), f"comparison {instance}")
        return evidence(9.7, "Independent final pass")

    monkeypatch.setattr(process, "run_generator_cli", lambda candidate, workspace: fake_render(candidate, workspace, renders))
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: {"template_id": "tpl-long-run", "status": "imported"})
    result = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_long",
        project_id="blockwise", emit=lambda *_args: None,
    ).run(source=str(source), brief="", placements=["feed", "story"], routes=_quality_routes(), require_quality_route=True)

    assert len(result["iterations"]) == 31
    assert result["iterations"][-1]["decision"] == "accepted"
    assert result["import"]["status"] == "imported"


def test_source_match_and_concrete_change_list_are_hard_gates():
    weak_match = evidence(9.8, "Header and image grid still differ")
    weak_match["rubric"]["feed_source_likeness"] = 9.4
    weak_match["required_changes"] = []
    record = validate_iterations([
        {"iteration": 1, "comparison": weak_match, "decision": "revise"}
    ])[0]
    assert record["comparison"]["score"] >= process.THRESHOLD
    assert record["decision"] == "revise"

    unfinished = evidence(9.8, "Footer remains too tall")
    unfinished["required_changes"] = [
        "placement=feed; layers=feed-footer; current={x:72,y:1120,width:936,height:140}; "
        "target={x:72,y:1140,width:936,height:120}; change=Reduce footer height to match the source"
    ]
    record = validate_iterations([
        {"iteration": 1, "comparison": unfinished, "decision": "revise"}
    ])[0]
    assert record["decision"] == "revise"

    vague = evidence(9.4, "Footer remains too tall")
    vague["required_changes"] = ["Reduce footer height to match the source"]
    with pytest.raises(AdTemplateProcessError, match="placement, layers, current geometry, target geometry"):
        validate_iterations([{"iteration": 1, "comparison": vague, "decision": "revise"}])


def test_comparator_target_overlap_rejects_exact_hero_thumbnail_collision():
    candidate = overlap_candidate()
    assessment = evidence(8.6, "Extend the hero and price panel")
    assessment["required_changes"] = [
        "placement=feed; layers=feed-hero,feed-price-panel; "
        "current={x:72,y:184,width:600,height:400}; "
        "target={x:72,y:184,width:600,height:620}; "
        "change=Extend the hero and price panel to y=804"
    ]
    parsed = process._assessment(assessment, "comparator", require_change_list=True)
    with pytest.raises(
        process.ComparatorSelfConsistencyError,
        match=r"newly overlaps feed layers (feed-hero and feed-thumb-1|feed-price-panel and feed-thumb-1)",
    ):
        process._validate_required_change_targets(parsed, candidate)

    preexisting = overlap_candidate("preexisting-overlap")
    for layer in preexisting["template"]["feedLayout"]["layers"]:
        if layer.get("layerId") in {"feed-hero", "feed-price-panel"}:
            layer["geometry"]["height"] = 620
    assessment["required_changes"] = [
        "placement=feed; layers=feed-hero,feed-price-panel; "
        "current={x:72,y:184,width:600,height:620}; "
        "target={x:72,y:184,width:600,height:600}; "
        "change=Reduce an overlap that already exists"
    ]
    process._validate_required_change_targets(
        process._assessment(assessment, "comparator", require_change_list=True),
        preexisting,
    )


def test_comparator_allows_intentional_vector_frame_over_image():
    candidate = overlap_candidate("vector-frame-overlap")
    assessment = evidence(8.9, "Lower the gallery frame over the hero edge")
    assessment["required_changes"] = [
        "placement=feed; layers=feed-price-panel; "
        "current={x:72,y:184,width:600,height:400}; "
        "target={x:72,y:500,width:600,height:180}; "
        "change=Overlap the source-visible frame across the lower hero edge"
    ]
    process._validate_required_change_targets(
        process._assessment(assessment, "comparator", require_change_list=True),
        candidate,
    )


def test_comparator_allows_source_justified_overlapping_image_collage():
    candidate = overlap_candidate("source-overlapping-collage")
    assessment = evidence(8.9, "Match the overlapping source collage")
    assessment["required_changes"] = [
        "placement=feed; layers=feed-hero; "
        "current={x:72,y:184,width:600,height:400}; "
        "target={x:72,y:184,width:600,height:620}; "
        "change=Create the source-visible overlapping photo collage by intentionally overlapping the hero with the thumbnail row"
    ]
    process._validate_required_change_targets(
        process._assessment(assessment, "comparator", require_change_list=True),
        candidate,
    )


def test_comparator_approximate_current_geometry_uses_actual_document_baseline():
    candidate = overlap_candidate("approximate-current")
    assessment = evidence(8.9, "Tighten the hero crop")
    assessment["required_changes"] = [
        "placement=feed; layers=feed-hero; "
        "current={x:70,y:180,width:605,height:405}; "
        "target={x:72,y:184,width:600,height:380}; "
        "change=Tighten the hero without touching the thumbnail row"
    ]
    process._validate_required_change_targets(
        process._assessment(assessment, "comparator", require_change_list=True),
        candidate,
    )


def test_comparator_retries_self_inconsistent_overlap_and_persists_event(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    events, calls, renders = [], [], []

    bad = evidence(8.6, "Extend the hero and price panel")
    bad["required_changes"] = [
        "placement=feed; layers=feed-hero,feed-price-panel; "
        "current={x:72,y:184,width:600,height:400}; "
        "target={x:72,y:184,width:600,height:620}; "
        "change=Extend the hero and price panel to y=804"
    ]

    def call_agent(instance, prompt, route):
        calls.append((instance, prompt[0]["text"]))
        if instance.startswith("builder-"):
            return overlap_candidate(f"candidate-{instance}")
        if instance == "comparator-1":
            return bad
        return evidence(9.7, "Self-consistent pass")

    monkeypatch.setattr(process, "run_generator_cli", lambda candidate, workspace: fake_render(candidate, workspace, renders))
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: {"template_id": "tpl-overlap", "status": "imported"})
    result = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_overlap",
        project_id="blockwise", emit=lambda kind, node, data: events.append((kind, data)),
    ).run(source=str(source), brief="", placements=["feed", "story"], routes=_quality_routes(), require_quality_route=True)

    comparator_calls = [name for name, _ in calls if name.startswith("comparator-")]
    assert comparator_calls == ["comparator-1", "comparator-1-retry-1"]
    retry_events = [data for kind, data in events if kind == "comparator.retried"]
    assert retry_events == [{
        "iteration": 1,
        "attempt": 1,
        "reason": "required change newly overlaps feed layers feed-hero and feed-thumb-1",
    }]
    retry_prompt = next(prompt for name, prompt in calls if name == "comparator-1-retry-1")
    assert "previous response was rejected" in retry_prompt
    assert "feed-hero and feed-thumb-1" in retry_prompt
    assert result["iterations"][0]["decision"] == "accepted"


def test_comparator_schema_recovery_survives_three_invalid_outputs(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    events, calls, renders = [], [], []
    invalid = evidence(8.6, "Move the hero closer to the source")
    invalid["required_changes"] = ["Move the hero lower"]
    comparator_calls = 0

    def call_agent(instance, prompt, route):
        nonlocal comparator_calls
        calls.append((instance, prompt[0]["text"]))
        if instance.startswith("builder-"):
            return overlap_candidate(f"candidate-{instance}")
        if instance.startswith("comparator-"):
            comparator_calls += 1
            if comparator_calls <= 3:
                return invalid
        return evidence(9.7, "Schema-correct pass")

    monkeypatch.setattr(process, "run_generator_cli", lambda candidate, workspace: fake_render(candidate, workspace, renders))
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: {"template_id": "tpl-comparator-retry", "status": "imported"})
    result = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_comparator_three_retries",
        project_id="blockwise", emit=lambda kind, node, data: events.append((kind, data)),
    ).run(source=str(source), brief="", placements=["feed", "story"], routes=_quality_routes(), require_quality_route=True)

    comparator_entries = [item for item in calls if item[0].startswith("comparator-")]
    assert len(comparator_entries) == 4
    assert comparator_entries[-1][0] == "comparator-1-retry-3"
    assert "placement=Feed or Story" in comparator_entries[-1][1]
    assert "current={x,y,width,height}" in comparator_entries[-1][1]
    retry_events = [data for kind, data in events if kind == "comparator.retried"]
    assert len(retry_events) == 3
    assert [item["attempt"] for item in retry_events] == [1, 2, 3]
    assert all(
        item["reason"] == "comparator required_changes must name placement, layers, current geometry, target geometry, and change"
        for item in retry_events
    )
    assert len(renders) == 1
    assert result["iterations"][0]["decision"] == "accepted"


def test_asset_envelope_is_mechanically_mirrored_without_changing_content():
    candidate = valid_candidate("asset-normalization")
    declaration = {
        "assetKey": "hero",
        "fileName": "home/mt-lawley-federation.webp",
        "mimeType": "image/webp",
    }
    candidate["assets"] = [declaration]
    candidate["template"].pop("assets")
    normalized = process.normalize_asset_declarations(candidate)
    assert normalized["template"]["assets"] == {
        "hero": {"fileName": declaration["fileName"], "mimeType": declaration["mimeType"]}
    }
    assert normalized["assets"] == [declaration]

    reverse = valid_candidate("asset-normalization-reverse")
    reverse["template"]["assets"] = normalized["template"]["assets"]
    assert process.normalize_asset_declarations(reverse)["assets"] == [declaration]

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
    (tmp_path / "home" / "property-photo.webp").write_bytes(b"catalog-photo")
    template = {"schema": "blockwise.ad-template", "assets": {"property-photo": {"fileName": "home/property-photo.webp", "mimeType": "image/webp"}}}
    monkeypatch.setenv("AD_TEMPLATE_ASSET_CATALOG_DIR", str(tmp_path))
    resolved = process.resolve_catalog_assets(template, [{"assetKey": "property-photo", "fileName": "home/property-photo.webp", "mimeType": "image/webp"}])
    assert resolved[0]["bytesBase64"] == "Y2F0YWxvZy1waG90bw=="
    with pytest.raises(AdTemplateProcessError): process.resolve_catalog_assets(template, [{"assetKey": "missing", "fileName": "home/property-photo.webp", "mimeType": "image/webp"}])
    with pytest.raises(AdTemplateProcessError): process.resolve_catalog_assets({"schema": "blockwise.ad-template", "assets": {"x": {"fileName": "../property-photo.webp", "mimeType": "image/webp"}}}, [{"assetKey": "x", "fileName": "../property-photo.webp", "mimeType": "image/webp"}])
    with pytest.raises(AdTemplateProcessError): process.resolve_catalog_assets(template, [{"assetKey": "property-photo", "fileName": "home/property-photo.webp", "mimeType": "image/png"}])
    with pytest.raises(AdTemplateProcessError): process.resolve_catalog_assets(template, [{"assetKey": "property-photo", "fileName": "home/property-photo.webp", "mimeType": "image/webp", "bytesBase64": ""}])


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
        assert "source pixels flattened into the output" in prompt
        assert "The primary objective is not generic ad quality" in prompt
        assert "Neutral replacement photography" in prompt
        assert "required_changes" in prompt
        assert "Assess Story dead space only inside the content-safe band y=240..1620" in prompt
        assert "y=0..239 and y=1620..1919 is mandatory platform-UI protection" in prompt
        assert "placement=feed|story; layers=comma-separated layerIds" in prompt
        assert "check every proposed target rectangle against every existing layer" in prompt
        assert "Never propose a target that newly overlaps an image slot or opaque vector panel" in prompt
        assert "never mention, score, or request changes for the neutral photo subject" in prompt
        assert '"templateId":"strict"' in prompt
        assert '"assetKey":"hero"' in prompt
        assert "privateNote" not in prompt
        assert "bytesBase64" not in prompt
    builder = process.generator_prompt(run_id="run", project_id="blockwise", brief="", placements=["feed", "story"], source="source.png")
    for key in process.METADATA_FIELDS:
        assert key in builder
    assert "never emit bytesBase64 anywhere" in builder
    assert "home/open-home-living.webp" in builder
    assert "brand/neutral-real-estate.png" in builder
    assert "Never leave a source-visible logo region blank" in builder
    assert 'Feed is 1080x1350 with safeZones=[{"x":72,"y":96,"width":936,"height":1158}]' in builder
    assert 'Story is 1080x1920 with safeZones=[{"x":72,"y":240,"width":936,"height":1380}]' in builder
    assert "Geometry is always {x,y,width,height} from the top-left" in builder
    assert "Do not redesign, simplify, modernise, improve, or reinterpret the source" in builder
    assert "structural inspiration, not a quality ceiling" not in builder
    assert '"template": {...}, "assets": []' in builder
    assert "mask must be exactly rounded_rect, circle, or none" in builder
    assert 'defaultCrop must be exactly {"x":0,"y":0,"width":1,"height":1}' in builder
    assert "overflowBehaviour must be exactly refuse, truncate, or scale_down" in builder
    assert 'fonts must always be a JSON list such as [{"file":"manrope-400.woff2"}' in builder
    assert "shape must be exactly one of rect, rounded, circle, line, pill, notched, wave, or ring" in builder
    assert "lineHeight is a unitless multiplier between 0.8 and 2.5" in builder
    assert "Every icon layer must use exactly arrow, check, phone, mail, globe, or pin" in builder
    assert "Every image_slot inputKey must be declared exactly once in imageInputs" in builder
    assert "real logo layer" in builder
    assert "preserve its layout regions" in builder
    assert "image-slot count and shapes" in builder
    assert "set specialAdCategory to HOUSING" in builder
    assert "Every text layer inputKey must be declared exactly once in textInputs" in builder
    assert 'Each realAssetRefs entry must contain exactly {"inputKey":"declaredKey"' in builder
    assert "Every layer assetKey, image defaultAssetKey, gallery sample assetKey" in builder
    feedback = json.dumps({
        "rubric": {field: 8.7 for field in process.RUBRIC_FIELDS},
        "minimum_score": 8.7,
        "hard_failures": ["failure detail"],
        "differences": ["difference detail"],
        "required_changes": [
            "placement=story; layers=story-headline; current={x:72,y:260,width:700,height:140}; "
            "target={x:72,y:280,width:720,height:120}; change=Move the headline"
        ],
        "reason": "r" * 6000 + "UNTRUNCATED-END",
    })
    feedback_builder = process.generator_prompt(
        run_id="run", project_id="blockwise", brief="", placements=["feed", "story"],
        source="source.png", feedback=feedback,
    )
    for key in ("rubric", "minimum_score", "hard_failures", "differences", "required_changes", "reason"):
        assert f'"{key}"' in feedback_builder
    assert "UNTRUNCATED-END" in feedback_builder
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


def test_story_essential_layers_stay_inside_content_safe_zone_but_visual_layers_may_bleed():
    template = valid_candidate("story-safe-zone")["template"]
    template["storyLayout"]["safeZones"] = [dict(process.STORY_CONTENT_SAFE_ZONE)]
    template["imageInputs"] = [
        {"key": "story-photo", "label": "Story photo", "acceptedTypes": ["image/jpeg"]},
        {"key": "story-logo", "label": "Story logo", "acceptedTypes": ["image/png"]},
    ]
    template["textInputs"] = [
        {"key": "story-headline", "label": "Story headline", "placeholder": "Just listed", "maxLength": 80},
    ]
    template["fonts"] = [{"file": "manrope-700.woff2"}]
    decorative_layers = [
        {
            "type": "image_slot", "layerId": "story-photo", "inputKey": "story-photo",
            "geometry": {"x": 0, "y": 0, "width": 1080, "height": 1920}, "mask": "none",
            "minSourceWidth": 1080, "minSourceHeight": 1920,
            "defaultCrop": {"x": 0, "y": 0, "width": 1, "height": 1},
            "allowedPlacementOverrides": ["crop", "position"],
        },
        {
            "type": "vector", "layerId": "story-decor", "geometry": {"x": 0, "y": 0, "width": 1080, "height": 1920},
            "shape": "rect", "colourRole": "accent", "opacity": 0.2,
        },
    ]
    essential_layers = [
        {
            "type": "text", "layerId": "story-headline", "inputKey": "story-headline",
            "font": {"file": "manrope-700.woff2"}, "fontSize": 64, "lineHeight": 1.1,
            "tracking": 0, "alignment": "left", "maxCharacters": 80, "maxLines": 2,
            "colourRole": "mainText", "overflowBehaviour": "refuse",
            "geometry": {"x": 72, "y": 240, "width": 720, "height": 160},
        },
        {
            "type": "logo", "layerId": "story-logo", "inputKey": "story-logo",
            "geometry": {"x": 820, "y": 260, "width": 160, "height": 100},
        },
        {
            "type": "icon", "layerId": "story-pin", "icon": "pin", "colourRole": "accent",
            "geometry": {"x": 72, "y": 1540, "width": 48, "height": 48},
        },
    ]
    template["storyLayout"]["layers"].extend(decorative_layers + essential_layers)
    assert process.validate_template_artifact(template) is template

    for layer_id, geometry in (
        ("story-headline", {"x": 72, "y": 220, "width": 720, "height": 160}),
        ("story-logo", {"x": 820, "y": 1580, "width": 160, "height": 100}),
        ("story-pin", {"x": 40, "y": 1540, "width": 48, "height": 48}),
    ):
        invalid = json.loads(json.dumps(template))
        layer = next(item for item in invalid["storyLayout"]["layers"] if item["layerId"] == layer_id)
        layer["geometry"] = geometry
        with pytest.raises(AdTemplateProcessError, match="Story content-safe zone"):
            process.validate_template_artifact(invalid)



def test_orchestrator_calls_real_roles_and_persists_receipts(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    renderer_command = os.environ.get("AD_TEMPLATE_GENERATOR_CMD") or (
        f"/usr/bin/node {shlex.quote(str(Path('/projects/only-process-blockwise/packages/ad-template-renderer/dist/cli.js')))}"
    )
    monkeypatch.setenv("AD_TEMPLATE_GENERATOR_CMD", renderer_command)
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
    compared = [item for item in events if item[0] == "iteration.compared"]
    assert len(compared) == 1
    assert {
        "rubric", "minimum_score", "hard_failures", "differences", "required_changes", "reason",
    }.issubset(compared[0][1])
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
            return evidence(9.6, "Revision matches")
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
            return evidence(9.6, "Comparator pass")
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


def test_final_check_resume_reuses_checkpoint_score_when_reviewers_request_revision(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    calls, renders, imports = [], [], []
    reviewer_calls = 0
    checkpoint = valid_candidate("resume-checkpoint")
    checkpoint_feed = tmp_path / "checkpoint-feed.png"
    checkpoint_story = tmp_path / "checkpoint-story.png"
    checkpoint_feed.write_bytes(b"feed")
    checkpoint_story.write_bytes(b"story")
    checkpoint["render"] = {"feed": str(checkpoint_feed), "story": str(checkpoint_story)}

    def call_agent(instance, prompt, route):
        nonlocal reviewer_calls
        calls.append((instance, prompt[0]["text"], route))
        if instance.startswith("builder-"):
            return valid_candidate("resume-revised")
        if instance.startswith("comparator-"):
            return evidence(9.7, "Comparator pass")
        reviewer_calls += 1
        if reviewer_calls <= 2:
            return evidence(9.2, "Final spacing needs revision")
        return evidence(9.7, "Independent final pass")

    monkeypatch.setattr(process, "run_generator_cli", lambda candidate, workspace: fake_render(candidate, workspace, renders))
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: imports.append(output) or {"template_id": "tpl-resumed", "status": "imported"})
    result = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_resume_final",
        project_id="blockwise", emit=lambda *_args: None,
    ).run(
        source=str(source), brief="", placements=["feed", "story"], routes=[
            {"provider": "builder", "model": "vision"},
            {"provider": "compare", "model": "vision"},
            {"provider": "review-a", "model": "vision-a"},
            {"provider": "review-b", "model": "vision-b"},
        ],
        history=[iteration(9.7)], revision_candidate=checkpoint,
        total_iterations=1, previous_score=9.7, resume_final_check=True,
    )

    assert reviewer_calls == 4
    assert [item[0] for item in calls if item[0].startswith("builder-")] == ["builder-2"]
    assert len(renders) == 1 and len(imports) == 1
    assert result["iterations"][0]["final_review_failed"] is True
    assert result["iterations"][1]["decision"] == "accepted"
    assert result["import"]["template_id"] == "tpl-resumed"


def test_invalid_final_review_output_retries_without_rebuilding_accepted_iteration(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    calls, events, renders, imports = [], [], [], []
    invalid = evidence(9.2, "Final spacing needs revision")
    invalid["required_changes"] = []

    def call_agent(instance, prompt, route):
        calls.append((instance, prompt[0]["text"], route))
        if instance.startswith("builder-"):
            return valid_candidate("final-output-retry")
        if instance.startswith("comparator-"):
            return evidence(9.7, "Comparator pass")
        if instance.startswith("final-reviewer-") and "-retry-" not in instance and len([
            item for item in calls if item[0].startswith("final-reviewer-")
        ]) == 1:
            return invalid
        return evidence(9.7, "Independent final pass")

    monkeypatch.setattr(process, "run_generator_cli", lambda candidate, workspace: fake_render(candidate, workspace, renders))
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: imports.append(output) or {"template_id": "tpl-final-retry", "status": "imported"})
    result = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_final_retry",
        project_id="blockwise", emit=lambda kind, node, data: events.append((kind, data)),
    ).run(source=str(source), brief="", placements=["feed", "story"], routes=[
        {"provider": "builder", "model": "vision"},
        {"provider": "compare", "model": "vision"},
        {"provider": "review-a", "model": "vision-a"},
        {"provider": "review-b", "model": "vision-b"},
    ])

    assert [name.split("-")[0] for name, _, _ in calls].count("builder") == 1
    assert len(renders) == 1 and len(imports) == 1
    final_calls = [item for item in calls if item[0].startswith("final-reviewer-")]
    assert len(final_calls) == 3
    assert "-retry-1" in final_calls[1][0]
    assert "previous final-review response was rejected" in final_calls[1][1]
    retried = [data for kind, data in events if kind == "final-review.retried"]
    assert len(retried) == 1
    assert retried[0]["attempt"] == 1
    assert retried[0]["reason"] == "final reviewer must provide a concrete required_changes list"
    assert result["iterations"][0]["decision"] == "accepted"
    assert result["final_review"]["decision"] == "accepted"


def test_final_review_schema_recovery_survives_three_invalid_outputs(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    calls, events, renders, imports = [], [], [], []
    invalid = evidence(9.2, "Final spacing needs revision")
    invalid["required_changes"] = []
    reviewer_a_calls = 0

    def call_agent(instance, prompt, route):
        nonlocal reviewer_a_calls
        calls.append((instance, prompt[0]["text"], route))
        if instance.startswith("builder-"):
            return valid_candidate("final-output-three-retries")
        if instance.startswith("comparator-"):
            return evidence(9.7, "Comparator pass")
        if route == "review-a/vision-a":
            reviewer_a_calls += 1
            if reviewer_a_calls <= 3:
                return invalid
        return evidence(9.7, "Independent final pass")

    monkeypatch.setattr(process, "run_generator_cli", lambda candidate, workspace: fake_render(candidate, workspace, renders))
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: imports.append(output) or {"template_id": "tpl-three-retries", "status": "imported"})
    result = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_final_three_retries",
        project_id="blockwise", emit=lambda kind, node, data: events.append((kind, data)),
    ).run(source=str(source), brief="", placements=["feed", "story"], routes=[
        {"provider": "builder", "model": "vision"},
        {"provider": "compare", "model": "vision"},
        {"provider": "review-a", "model": "vision-a"},
        {"provider": "review-b", "model": "vision-b"},
    ])

    assert [name.split("-")[0] for name, _, _ in calls].count("builder") == 1
    assert len(renders) == 1 and len(imports) == 1
    final_calls = [item for item in calls if item[0].startswith("final-reviewer-")]
    assert len(final_calls) == 5
    assert "-retry-3" in final_calls[3][0]
    assert "feed_source_likeness < 9.5" in final_calls[3][1]
    assert "exact keys placement, layers, current, target, and change" in final_calls[3][1]
    retried = [data for kind, data in events if kind == "final-review.retried"]
    assert len(retried) == 3
    assert [item["attempt"] for item in retried] == [1, 2, 3]
    assert result["iterations"][0]["decision"] == "accepted"
    assert result["final_review"]["decision"] == "accepted"


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
            return evidence(next(scores), "Material improvement")
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
            return evidence(next(score_iter), f"comparison {instance}")
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


def test_regressed_candidate_is_traced_and_next_revision_uses_current_candidate(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    scores = iter([8.6, 8.1, 9.6])
    calls, renders, events = [], [], []

    def call_agent(instance, prompt, route):
        calls.append((instance, route, prompt[0]["text"]))
        if instance.startswith("builder-"):
            return valid_candidate(f"candidate-{instance}")
        if instance.startswith("comparator-"):
            return evidence(next(scores), f"comparison {instance}")
        return evidence(9.7, "Independent final pass")

    monkeypatch.setattr(process, "run_generator_cli", lambda candidate, workspace: fake_render(candidate, workspace, renders))
    monkeypatch.setattr(process, "import_template", lambda output, run_id, project_id: {"template_id": "tpl-best", "status": "imported"})
    result = SoleProcessOrchestrator(
        call_agent=call_agent, workspace=tmp_path / "run", run_id="trun_best",
        project_id="blockwise", emit=lambda kind, node, data: events.append((kind, data)),
    ).run(source=str(source), brief="", placements=["feed", "story"], routes=_quality_routes(), require_quality_route=True)

    builder_prompts = {instance: prompt for instance, _, prompt in calls if instance.startswith("builder-")}
    assert '"templateId":"candidate-builder-1"' in builder_prompts["builder-2"]
    assert '"templateId":"candidate-builder-2"' in builder_prompts["builder-3"]
    assert "comparison comparator-2" in builder_prompts["builder-3"]
    for field in ("rubric", "minimum_score", "hard_failures", "differences", "required_changes", "reason"):
        assert f'"{field}"' in builder_prompts["builder-2"]
    compared_events = [data for kind, data in events if kind == "iteration.compared"]
    assert len(compared_events) == 3
    for event in compared_events:
        assert {
            "rubric", "minimum_score", "hard_failures", "differences", "required_changes", "reason",
        }.issubset(event)
    assert [route for instance, route, _ in calls if instance.startswith("builder-")] == [
        "openai-codex/gpt-5.6-luna",
        "openai-codex/gpt-5.6-luna",
        "openai-codex/gpt-5.6-sol",
    ]
    assert [item["candidate"]["template"]["templateId"] for item in result["iterations"]] == [
        "candidate-builder-1", "candidate-builder-2", "candidate-builder-3",
    ]
    assert [item["comparison"]["score"] for item in result["iterations"]] == [8.6, 8.1, 9.6]
    assert [instance.split("-")[0] for instance, _, _ in calls] == [
        "builder", "comparator", "builder", "comparator", "builder", "comparator", "final", "final",
    ]


def test_final_review_revision_continues_from_the_reviewed_then_current_candidate(tmp_path, monkeypatch):
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
            return evidence(next(scores), f"comparison {instance}")
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
    assert '"templateId":"candidate-builder-2"' in builder_prompts["builder-3"]
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
            return evidence(next(scores), f"comparison {instance}")
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
