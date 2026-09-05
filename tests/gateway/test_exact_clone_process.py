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
        "instruction": "Move the title two pixels right",
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
        nonlocal comparison_count, patch_count
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
        if instance.startswith("patch-"):
            patch_count += 1
            return {"operations": [{
                "op": "replace", "path": "/template/metadata/description",
                "value": f"patch-{patch_count}",
            }]}
        if instance.startswith("comparator-"):
            comparison_count += 1
            return _review(accept=comparison_count == 6)
        if instance.startswith("final-reviewer-"):
            return _review(accept=True)
        raise AssertionError(instance)

    def render(candidate, workspace, *, asset_overrides=None):
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
        emit=lambda *_args: None,
    ).run(source=str(source), brief="clone", placements=["feed", "story"], routes=routes)

    assert order[:2] == ["source-map", "image-model"]
    assert len(image_calls) == 1
    assert comparison_count == process.MAX_COMPARISONS == 6
    assert patch_count == 5
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
