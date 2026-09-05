from __future__ import annotations

import copy
from pathlib import Path

from PIL import Image

import gateway.exact_clone_process as process
from gateway.tool_runs import TOOL_RUN_COMMAND_SCHEMA, ToolRunError, ToolRunStore
import pytest


def _template() -> dict:
    def layout(placement: str, height: int) -> dict:
        return {
            "placement": placement,
            "layers": [{
                "type": "plate", "layerId": f"{placement}-plate",
                "colourRole": "background", "protected": True,
                "geometry": {"x": 0, "y": 0, "width": 1080, "height": height},
            }, {
                "type": "image_slot", "layerId": f"{placement}-hero",
                "inputKey": "hero", "geometry": {"x": 0, "y": 0, "width": 1080, "height": height},
            }],
            "safeZones": [],
        }
    return {
        "schema": "blockwise.ad-template",
        "templateId": "exact-clone-test",
        "createdAt": "2026-09-05T00:00:00Z",
        "feedLayout": layout("feed", 1350),
        "storyLayout": layout("story", 1920),
        "imageInputs": [{"key": "hero", "label": "Hero", "acceptedTypes": ["image/png"]}],
        "textInputs": [],
        "semanticColours": {},
        "assets": {},
        "fonts": [],
        "metadata": {"title": "Exact clone", "description": "initial"},
    }


def _review(*, accept: bool) -> dict:
    score = 9.8 if accept else 9.2
    issue = [] if accept else [{
        "placement": "feed",
        "layerIds": ["feed-title"],
        "category": "geometry",
        "instruction": "Set x to 102px (a +2px delta)",
        "severity": "material",
    }]
    return {
        "decision": "accept" if accept else "revise",
        "scores": {
            "overall": score,
            "geometry": score,
            "typography": score,
            "colourEffects": score,
            "imageCrop": score,
            "details": score,
        },
        "issues": issue,
        "warnings": [],
        "effects": {
            "shading": "not_present",
            "gradients": "not_present",
            "shadows": "not_present",
            "transparency": "not_present",
            "borders": "not_present",
            "masks": "not_present",
            "texture": "not_present",
        },
        "fontSubstitution": None,
    }


def test_exact_clone_is_measured_image_referenced_patch_bounded_and_quarantined(monkeypatch, tmp_path):
    source = tmp_path / "source.png"
    Image.new("RGB", (800, 1000), "white").save(source)
    order: list[str] = []
    image_calls: list[tuple] = []
    agent_calls: list[tuple[str, str]] = []
    rendered_candidates: list[dict] = []
    imported: dict = {}
    comparison_count = 0
    patch_count = 0
    contract_repair_count = 0
    renderer_rejected = False
    emitted: list[tuple[str, str, dict]] = []

    real_source_map = process.build_source_map

    def source_map(path: str):
        order.append("source-map")
        return real_source_map(path)

    monkeypatch.setattr(process, "build_source_map", source_map)

    def image_model(prompt, source_path, placement, route):
        order.append("image-model")
        image_calls.append((prompt, source_path, placement, dict(route)))
        target = tmp_path / "generated-reference.png"
        Image.new("RGB", (1080, 1920), "white").save(target)
        return str(target)

    def call_agent(instance, prompt, route):
        nonlocal comparison_count, patch_count, contract_repair_count
        agent_calls.append((instance, route))
        if instance.startswith("aspect-reference"):
            return {
                "sourcePlacement": "feed",
                "targetPlacement": "story",
                "canvas": {"width": 1080, "height": 1920},
                "regions": [{
                    "regionId": "main", "sourceRole": "main",
                    "target": {"x": 0, "y": 0, "width": 1080, "height": 1920},
                    "zIndex": 0,
                }],
                "preserve": ["all visible geometry and effects"],
            }
        if instance == "builder-initial":
            return {"template": _template(), "assets": []}
        if instance.startswith("contract-repair-"):
            contract_repair_count += 1
            return {"operations": [{
                "op": "replace", "path": "/template/metadata/description",
                "value": "contract-repaired",
            }]}
        if instance.startswith("patch-"):
            patch_count += 1
            return {"operations": [{
                "op": "replace", "path": "/template/metadata/description",
                "value": f"patch-{patch_count}",
            }]}
        if instance.startswith("comparator-"):
            comparison_count += 1
            result = _review(accept=comparison_count == 6)
            if comparison_count == 3:
                result["scores"] = {key: 7.1 for key in result["scores"]}
            return result
        if instance.startswith("final-reviewer-"):
            return _review(accept=True)
        raise AssertionError(instance)

    def render(candidate, workspace, *, asset_overrides=None):
        nonlocal renderer_rejected
        if not renderer_rejected:
            renderer_rejected = True
            raise process.AdTemplateRendererRejection(["synthetic strict schema failure"])
        rendered_candidates.append(copy.deepcopy(candidate))
        workspace.mkdir(parents=True, exist_ok=True)
        artifact = workspace / "artifact.json"
        artifact.write_text("{}", encoding="utf-8")
        output = workspace / "rendered"
        output.mkdir(exist_ok=True)
        feed = output / "feed.png"
        story = output / "story.png"
        Image.new("RGB", (1080, 1350), "white").save(feed)
        Image.new("RGB", (1080, 1920), "white").save(story)
        return {
            "render": {"feed": str(feed), "story": str(story)},
            "previews": [], "review_previews": [], "template_path": str(artifact),
        }

    monkeypatch.setattr(process, "run_renderer", render)

    def import_template(output, **_kwargs):
        imported.update(copy.deepcopy(output))
        return {
            "template_id": output["template"]["templateId"],
            "status": "imported", "asset_count": 0, "replayed": False,
            "library_status": "quarantined", "run_id": "trun_test",
        }

    monkeypatch.setattr(process, "import_template", import_template)
    monkeypatch.setattr(process, "review_template_action", lambda **kwargs: {
        "templateId": kwargs["template_id"], "status": "passed",
    })

    routes = [
        {"provider": "openai-codex", "model": "gpt-image-2-high"},
        {"provider": "openai-codex", "model": "builder"},
        {"provider": "openai-codex", "model": "comparator"},
        {"provider": "openai-codex", "model": "final-a"},
        {"provider": "deepseek", "model": "final-b"},
        {"provider": "openai-codex", "model": "escalation"},
    ]
    result = process.ExactCloneOrchestrator(
        call_agent=call_agent,
        call_image_model=image_model,
        workspace=tmp_path / "run",
        run_id="trun_test",
        project_id="blockwise",
        emit=lambda kind, node, data: emitted.append((kind, node, data)),
    ).run(source=str(source), brief="clone", placements=["feed", "story"], routes=routes)

    assert order[:2] == ["source-map", "image-model"]
    assert len(image_calls) == 1
    assert comparison_count == process.MAX_COMPARISONS == 6
    assert patch_count == 5
    assert contract_repair_count == 1
    regression = next(data for kind, _, data in emitted if kind == "regression.reverted")
    assert regression == {
        "from_iteration": 3, "from_score": 7.1,
        "to_iteration": 1, "to_score": 9.2,
    }
    patch_routes = [route for instance, route in agent_calls if instance.startswith("patch-")]
    assert patch_routes[:3] == ["openai-codex/builder"] * 3
    assert patch_routes[3:] == ["openai-codex/escalation"] * 2
    assert len([name for name, _ in agent_calls if name.startswith("final-reviewer-")]) == 2
    assert result["import"]["library_status"] == "quarantined"
    assert result["smoke_test"]["status"] == "passed"
    assert result["template"]["metadata"]["generationReview"]["likenessThreshold"] == 9.8
    assert result["template"]["metadata"]["generationReview"]["comparator"]["decision"] == "ready"
    assert all(item["decision"] == "pass" for item in result["template"]["metadata"]["generationReview"]["finalReviewers"])
    assert imported["template"]["assets"] == {}
    assert imported["assets"] == []
    assert all(not key.startswith("qa-") for key in imported["template"]["assets"])
    assert any(
        any(str(key).startswith("qa-") for key in item["template"]["assets"])
        for item in rendered_candidates[:-1]
    )
    assert all(not str(key).startswith("qa-") for key in rendered_candidates[-1]["template"]["assets"])
    assert result["previews"] and all(item["kind"] == "final-neutral-shippable" for item in result["previews"])
    reciprocal = next(
        item for item in result["references"]
        if item.get("kind") == "reciprocal-image-reference"
    )
    assert reciprocal["name"] == "reference-story.png"
    assert (tmp_path / "run" / "previews" / reciprocal["name"]).is_file()


def test_visual_gate_requires_every_score_and_effect_at_98():
    passing = _review(accept=True)
    assert process.validate_review(passing)["decision"] == "accept"
    failing = copy.deepcopy(passing)
    failing["scores"]["details"] = 9.79
    failing["decision"] = "revise"
    assert process.validate_review(failing)["decision"] == "revise"
    effect_failure = copy.deepcopy(passing)
    effect_failure["effects"]["shadows"] = "mismatch"
    effect_failure["decision"] = "revise"
    assert process.validate_review(effect_failure)["decision"] == "revise"


def test_visual_gate_derives_decision_from_evidence_not_model_label():
    passing = _review(accept=True)
    passing["decision"] = "revise"
    assert process.validate_review(passing)["decision"] == "accept"

    failing = _review(accept=False)
    failing["decision"] = "accept"
    assert process.validate_review(failing)["decision"] == "revise"

    best = process.validate_review(_review(accept=False))
    regressed = copy.deepcopy(best)
    regressed["scores"] = {key: 7.1 for key in regressed["scores"]}
    assert process._review_regressed(regressed, best)
    assert not process._review_regressed(best, best)

    vague = _review(accept=False)
    vague["issues"][0]["instruction"] = "Match the source alignment"
    with pytest.raises(process.AdTemplateProcessError, match="concrete field"):
        process.validate_review(vague)


def test_comparator_unit_interval_scores_are_normalized_to_ten_point_scale():
    review = _review(accept=False)
    review["scores"] = {
        "overall": 0.98,
        "geometry": 0.99,
        "typography": 0.98,
        "colourEffects": 1.0,
        "imageCrop": 0.99,
        "details": 0.98,
    }
    review["issues"] = []

    normalized = process.validate_review(review)

    assert normalized["scores"] == {
        "overall": 9.8,
        "geometry": 9.9,
        "typography": 9.8,
        "colourEffects": 10.0,
        "imageCrop": 9.9,
        "details": 9.8,
    }
    assert normalized["decision"] == "accept"


def test_ocr_reconstruction_preserves_lines_and_horizontal_word_order():
    words = [
        {"text": "move", "x": 60, "y": 31, "height": 10},
        {"text": "Ready", "x": 10, "y": 10, "height": 12},
        {"text": "home", "x": 50, "y": 9, "height": 12},
        {"text": "to", "x": 10, "y": 30, "height": 11},
        {"text": "in.", "x": 100, "y": 30, "height": 11},
    ]
    assert process._reconstruct_ocr_text(words) == "Ready home\nto move in."


def test_ocr_reconstruction_uses_tesseract_lines_and_repairs_bullet_tokens():
    words = [
        {"text": "2.Bedrooms", "x": 20, "y": 10, "width": 80, "height": 10, "confidence": 46,
         "ocrPass": "layout", "pageId": 1, "blockId": 2, "paragraphId": 1, "lineId": 1},
        {"text": "*", "x": 5, "y": 12, "width": 4, "height": 4, "confidence": 32,
         "ocrPass": "layout", "pageId": 1, "blockId": 2, "paragraphId": 1, "lineId": 1},
        {"text": "2Bathrooms", "x": 20, "y": 30, "width": 80, "height": 10, "confidence": 43,
         "ocrPass": "layout", "pageId": 1, "blockId": 2, "paragraphId": 1, "lineId": 2},
        {"text": "©", "x": 5, "y": 32, "width": 4, "height": 4, "confidence": 47,
         "ocrPass": "layout", "pageId": 1, "blockId": 2, "paragraphId": 1, "lineId": 2},
    ]
    assert process._reconstruct_ocr_text(words) == "• 2 Bedrooms\n• 2 Bathrooms"


def test_ocr_token_is_assigned_once_to_best_overlapping_normalized_text_layer():
    layers = [
        {"type": "text", "geometry": {"x": 0, "y": 0, "width": 1, "height": 1}},
        {"type": "text", "geometry": {"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.1}},
    ]
    source_map = {
        "canvas": {"width": 100, "height": 100},
        "ocr": [{"text": "Price", "x": 12, "y": 12, "width": 10, "height": 5}],
    }
    assigned = process._ocr_words_by_text_layer(
        layers, source_map, canvas_width=1080, canvas_height=1350,
    )
    assert list(assigned) == [1]
    assert assigned[1][0]["text"] == "Price"


def test_checkpoint_always_advances_to_current_qa_projection(tmp_path):
    process.persist_checkpoint(tmp_path, {"qaProjectionVersion": 1, "iterations": []})
    checkpoint = process.load_checkpoint(tmp_path)
    assert checkpoint["qaProjectionVersion"] == process.QA_PROJECTION_VERSION == 3
    assert checkpoint["evaluationPolicyVersion"] == process.EVALUATION_POLICY_VERSION == 1


def test_patch_application_error_is_fed_back_for_one_bounded_retry(tmp_path):
    source = tmp_path / "source.png"
    Image.new("RGB", (20, 20), "white").save(source)
    calls: list[str] = []
    events: list[tuple[str, dict]] = []

    def call_agent(instance, _prompt, _route):
        calls.append(instance)
        if len(calls) == 1:
            return {"operations": [{
                "op": "replace", "path": "/template/metadata/missing",
                "value": "invalid",
            }]}
        return {"operations": [{
            "op": "replace", "path": "/template/metadata/description",
            "value": "corrected",
        }]}

    patch, candidate = process._call_applied_patch(
        call_agent,
        instance="patch-semantic",
        prompt="repair",
        paths=[str(source)],
        route={"provider": "openai-codex", "model": "gpt-5.6-sol"},
        candidate={"template": _template(), "assets": []},
        emit=lambda kind, _node, data: events.append((kind, data)),
    )

    assert calls == ["patch-semantic", "patch-semantic-format-retry"]
    assert patch["operations"][0]["path"] == "/template/metadata/description"
    assert candidate["template"]["metadata"]["description"] == "corrected"
    assert events[0][0] == "role.output-retried"
    assert "does not exist" in events[0][1]["reason"]


def test_exhausted_invalid_patch_paths_trigger_bounded_replan(tmp_path):
    source = tmp_path / "source.png"
    Image.new("RGB", (20, 20), "white").save(source)
    calls: list[str] = []
    events: list[tuple[str, dict]] = []

    def call_agent(instance, _prompt, _route):
        calls.append(instance)
        if len(calls) <= process.MAX_OUTPUT_RETRIES + 1:
            return {"operations": [{
                "op": "replace", "path": "/template/metadata/missing", "value": "wrong",
            }]}
        return {"operations": [{
            "op": "replace", "path": "/template/metadata/description", "value": "replanned",
        }]}

    candidate = {"template": _template(), "assets": []}
    patch, updated = process._call_applied_patch(
        call_agent,
        instance="patch-semantic",
        prompt="patch",
        paths=[str(source)],
        route={"provider": "openai-codex", "model": "builder"},
        candidate=candidate,
        emit=lambda kind, _node, data: events.append((kind, data)),
    )

    assert calls == [
        "patch-semantic", "patch-semantic-format-retry", "patch-semantic-replan-1",
    ]
    assert patch["operations"][0]["path"] == "/template/metadata/description"
    assert updated["template"]["metadata"]["description"] == "replanned"
    assert any(kind == "revision.replanned" for kind, _ in events)


def test_invalid_legacy_candidate_is_removed_without_losing_reference_checkpoint():
    checkpoint = {
        "sourceMap": {"canvas": {"width": 1080, "height": 1350}},
        "reciprocalReference": "/run/story-reference.png",
        "reference": {"regions": [{"regionId": "hero"}]},
        "targetReferenceMap": {"canvas": {"width": 1080, "height": 1920}},
        "candidate": {
            "template": {
                "schema": "blockwise.ad-template",
                "schemaVersion": "legacy",
                "fields": [],
                "placements": {},
            },
            "assets": [],
        },
        "iterations": [{"iteration": 1}],
        "cycleComparisons": 1,
    }

    assert process._checkpoint_candidate(checkpoint) is None
    assert "candidate" not in checkpoint
    assert checkpoint["iterations"] == []
    assert checkpoint["cycleComparisons"] == 0
    assert checkpoint["reference"] == {"regions": [{"regionId": "hero"}]}
    assert checkpoint["sourceMap"]["canvas"]["height"] == 1350
    assert checkpoint["reciprocalReference"] == "/run/story-reference.png"


def test_near_complete_candidate_with_missing_layer_types_is_preserved_for_bounded_repair():
    candidate = {"template": _template(), "assets": []}
    del candidate["template"]["feedLayout"]["layers"][0]["type"]
    del candidate["template"]["storyLayout"]["layers"][0]["type"]
    checkpoint = {"candidate": candidate, "iterations": [], "cycleComparisons": 0}

    assert process._checkpoint_candidate(checkpoint) == candidate
    with pytest.raises(process.AdTemplateRendererRejection) as rejected:
        process._candidate_envelope(candidate)
    message = str(rejected.value)
    assert "/template/feedLayout/layers/0/type" in message
    assert "/template/storyLayout/layers/0/type" in message
    assert process.MAX_CONTRACT_REPAIRS == 6


def test_renderer_reason_keeps_complete_batched_preflight_violations():
    violations = ",".join(
        f'{{"placement":"feed","layerId":"f-layer-{index}","kind":"cannot_fit_readability_floor"}}'
        for index in range(20)
    )
    line = f'AD_TEMPLATE_TEXT_PREFLIGHT_FAILED {{"violations":[{violations}]}}'

    reasons = process._renderer_reasons(line, "")

    assert len(line) > 500
    assert len(reasons) == 1
    assert "f-layer-0" in reasons[0]
    assert "f-layer-19" in reasons[0]


def test_all_text_preflight_violations_are_repaired_in_one_deterministic_batch():
    candidate = {"template": _template(), "assets": []}
    for placement, field in (("feed", "feedLayout"), ("story", "storyLayout")):
        candidate["template"][field]["layers"].append({
            "type": "text", "layerId": f"{placement}-copy", "inputKey": "copy",
            "fontSize": 32, "lineHeight": 1.0, "maxLines": 2,
            "alignment": "left", "overflowBehaviour": "shrink",
            "geometry": {"x": 100, "y": 100, "width": 100, "height": 40},
        })
    payload = {
        "code": "AD_TEMPLATE_TEXT_PREFLIGHT_FAILED",
        "violations": [
            {"placement": "feed", "layerId": "feed-copy", "kind": "cannot_fit_readability_floor", "readabilityFloorPx": 24},
            {"placement": "story", "layerId": "story-copy", "kind": "cannot_fit_readability_floor", "readabilityFloorPx": 28},
        ],
    }

    repaired, count = process._apply_deterministic_contract_repairs(
        candidate, [f"AD_TEMPLATE_TEXT_PREFLIGHT_FAILED {process._safe_json(payload)}"],
    )

    assert count == 2
    feed = repaired["template"]["feedLayout"]["layers"][-1]
    story = repaired["template"]["storyLayout"]["layers"][-1]
    assert feed["geometry"]["width"] > 100 and feed["geometry"]["height"] > 40
    assert story["geometry"]["width"] > 100 and story["geometry"]["height"] > 40
    assert feed["fontSize"] >= 24 and story["fontSize"] >= 28


def test_best_candidate_can_be_recovered_without_ephemeral_qa_inputs(tmp_path):
    template = _template()
    template["imageInputs"].append({
        "key": "qa_feed_1_hero", "label": "Hero", "acceptedTypes": ["image/png"],
        "defaultAssetKey": "qa-feed-1",
    })
    template["feedLayout"]["layers"][1]["inputKey"] = "qa_feed_1_hero"
    template["assets"]["qa-feed-1"] = {"fileName": "qa/feed-1.png", "mimeType": "image/png"}
    artifact = tmp_path / "iterations" / "02" / "artifact.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(
        process._safe_json({
            "template": template,
            "assets": [{
                "assetKey": "qa-feed-1", "fileName": "qa/feed-1.png",
                "mimeType": "image/png", "bytesBase64": "source-derived",
            }],
        }),
        encoding="utf-8",
    )

    recovered = process._neutral_candidate_from_iteration(tmp_path, 2)
    assert recovered is not None
    assert recovered["template"]["feedLayout"]["layers"][1]["inputKey"] == "hero"
    assert all(not item["key"].startswith("qa_") for item in recovered["template"]["imageInputs"])
    assert recovered["template"]["assets"] == {}
    assert recovered["assets"] == []


def test_review_url_is_derived_from_exact_import_route(monkeypatch):
    captured = {}
    monkeypatch.setenv(
        "BLOCKWISE_TEMPLATE_IMPORT_URL",
        "http://127.0.0.1:8080/api/internal/adstudio/template-artifacts",
    )
    monkeypatch.setattr(process, "_post_blockwise", lambda url, body, scope: captured.update(
        url=url, body=body, scope=scope,
    ) or {"templateId": "template-1", "status": "passed"})
    process.review_template_action(
        template_id="template-1", run_id="trun_1", action="smoke_test",
    )
    assert captured == {
        "url": "http://127.0.0.1:8080/api/internal/adstudio/template-artifacts/template-1/review",
        "body": {"action": "smoke_test", "runId": "trun_1"},
        "scope": "adstudio.templates.review",
    }


@pytest.mark.parametrize("library_status", [None, "active"])
def test_import_fails_closed_until_blockwise_confirms_quarantine(
    monkeypatch, library_status,
):
    monkeypatch.setenv(
        "BLOCKWISE_TEMPLATE_IMPORT_URL",
        "http://127.0.0.1:8080/api/internal/adstudio/template-artifacts",
    )
    monkeypatch.setattr(process, "_runtime_catalog", lambda: object())
    monkeypatch.setattr(process, "resolve_declared_assets", lambda *_args: [])
    response = {
        "templateId": "exact-clone-test",
        "assetCount": 0,
        "replayed": False,
    }
    if library_status is not None:
        response["libraryStatus"] = library_status
    monkeypatch.setattr(process, "_post_blockwise", lambda *_args, **_kwargs: response)

    with pytest.raises(process.AdTemplateProcessError, match="quarantined"):
        process.import_template(
            {"template": _template(), "assets": []},
            run_id="trun_test", project_id="blockwise",
        )


@pytest.mark.parametrize("asset_count", [False, "0", 1])
def test_import_requires_exact_integer_asset_count(monkeypatch, asset_count):
    monkeypatch.setenv(
        "BLOCKWISE_TEMPLATE_IMPORT_URL",
        "http://127.0.0.1:8080/api/internal/adstudio/template-artifacts",
    )
    monkeypatch.setattr(process, "_runtime_catalog", lambda: object())
    monkeypatch.setattr(process, "resolve_declared_assets", lambda *_args: [])
    monkeypatch.setattr(process, "_post_blockwise", lambda *_args, **_kwargs: {
        "templateId": "exact-clone-test",
        "assetCount": asset_count,
        "replayed": False,
        "libraryStatus": "quarantined",
    })

    with pytest.raises(process.AdTemplateProcessError, match="asset count"):
        process.import_template(
            {"template": _template(), "assets": []},
            run_id="trun_test", project_id="blockwise",
        )


def test_ready_review_publish_and_discard_states_are_atomic(tmp_path):
    store = ToolRunStore(str(tmp_path / "runs.db"))
    command = {
        "schema": TOOL_RUN_COMMAND_SCHEMA,
        "request_id": "req-review",
        "tool_id": "ad-template-generator",
        "action": "build-template",
        "scope": {"project_id": "blockwise"},
        "payload": {"sources": [{"path": "source.png"}]},
        "idempotency_key": "review-state",
    }
    run, _ = store.create_run(command)
    store.update_run(run["run_id"], status="running")
    ready = store.transition_run(
        run["run_id"], expected_statuses={"running"}, status="ready_for_review",
        stage="ready-for-review", attention=True,
        event_kind="template.ready-for-review", event_status="ok",
    )
    assert ready["status"] == "ready_for_review"
    assert ready["completed_at"] is None
    with pytest.raises(ToolRunError, match="no longer eligible"):
        store.transition_run(
            run["run_id"], expected_statuses={"running"}, status="completed",
            stage="live", attention=False, event_kind="template.published",
        )
    publishing = store.transition_run(
        run["run_id"], expected_statuses={"ready_for_review"}, status="publishing",
        stage="publish", attention=False, event_kind="command.approve",
    )
    assert publishing["status"] == "publishing"
    completed = store.transition_run(
        run["run_id"], expected_statuses={"publishing"}, status="completed",
        stage="live", attention=False, event_kind="template.published", event_status="ok",
    )
    assert completed["completed_at"] is not None
