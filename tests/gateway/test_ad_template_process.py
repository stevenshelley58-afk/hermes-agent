import json
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
    return {"rubric": {field: score for field in process.RUBRIC_FIELDS}, "reason": reason}

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

def test_one_comparator_per_iteration_and_final_review_only_after_pass():
    assert validate_iterations([iteration()])[0]["comparison"]["score"] == 9.4
    with pytest.raises(AdTemplateProcessError):
        validate_iterations([{"iteration": 1, "comparison": {"score": 9.4, "reason": "x"}, "reviewers": []}])
    review = validate_final_review({"reviewers": [{"id": "reviewer-a", "route": "a/m", **evidence(9.6, "good")}, {"id": "reviewer-b", "route": "b/m", **evidence(9.7, "good")}]}, accepted=True)
    assert review["decision"] == "accepted"

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


def test_builder_contract_is_strict_and_prompts_require_five_visible_scores():
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
        prompt = process.review_prompt(final=final)
        for field in process.RUBRIC_FIELDS:
            assert field in prompt
        assert "hard failure" in prompt.lower()
        assert "source composite pixel" in prompt
        assert "must not be penalized" in prompt
        assert "source-free photography" in prompt
        assert "neutral editable replacement is correct" in prompt
    builder = process.generator_prompt(run_id="run", project_id="blockwise", brief="", placements=["feed", "story"], source="source.png")
    for key in process.METADATA_FIELDS:
        assert key in builder
    assert "never emit bytesBase64 anywhere" in builder
    assert "home/open-home-living.webp" in builder
    assert 'Feed safeZones=[{"x":48,"y":48,"width":984,"height":1254}]' in builder
    assert 'Story safeZones=[{"x":60,"y":250,"width":960,"height":1420}]' in builder
    assert "width,height are positive sizes, not right/bottom coordinates" in builder
    assert '"template": {...}, "assets": []' in builder
    assert "mask must be exactly rounded_rect, circle, or none" in builder
    assert 'defaultCrop must be exactly {"x":0,"y":0,"width":1,"height":1}' in builder
    assert "overflowBehaviour must be exactly refuse, truncate, or scale_down" in builder
    assert 'fonts must always be a JSON list such as [{"file":"manrope-400.woff2"}' in builder

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


def test_orchestrator_calls_real_roles_and_persists_receipts(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    generator = Path("/projects/only-process-blockwise/packages/ad-template-renderer/dist/cli.js")
    monkeypatch.setenv("AD_TEMPLATE_GENERATOR_CMD", f"/usr/bin/node {shlex.quote(str(generator))}")
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
                {"provider": "deepseek", "model": "deepseek-v4-flash-vision-exp"},
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
    assert len(renders) == 1 and renders[0]["template"]["templateId"] == "good"
    assert len([item for item in calls if item[0].startswith("comparator-")]) == 1
    assert len(imports) == 1 and result["import"]["template_id"] == "tpl-good"
    kinds = [item[0] for item in events]
    assert kinds.index("candidate.rejected") < kinds.index("iteration.started") < kinds.index("iteration.rendered") < kinds.index("iteration.compared")
    rejected = next(item[2] for item in events if item[0] == "candidate.rejected")
    assert rejected == {"iteration": 1, "attempt": 1, "reason": exact_reason, "decision": "repair"}
    evidence_path = tmp_path / "run" / "iterations" / "01" / "rejected-candidate-01.json"
    persisted = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert persisted["candidate"]["template"]["feedLayout"]["safeZones"][0]["right"] == 1032
    assert "private" not in persisted["candidate"]["template"]["metadata"]
    assert "hash" not in persisted["candidate"]["template"]["metadata"]


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
