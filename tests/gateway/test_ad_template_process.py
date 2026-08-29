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

def evidence(score=9.4, reason="Improve spacing"):
    return {"rubric": {field: score for field in process.RUBRIC_FIELDS}, "reason": reason}

def iteration(score=9.4, number=1):
    return {"iteration": number, "comparison": evidence(score), "decision": "accepted" if score >= 9.5 else "revise"}

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
    (tmp_path / "property-photo.webp").write_bytes(b"catalog-photo")
    template = {"schema": "blockwise.ad-template", "assets": {"property-photo": {"fileName": "property-photo.webp", "mimeType": "image/webp"}}}
    monkeypatch.setenv("AD_TEMPLATE_ASSET_CATALOG_DIR", str(tmp_path))
    resolved = process.resolve_catalog_assets(template, [{"assetKey": "property-photo", "fileName": "property-photo.webp", "mimeType": "image/webp"}])
    assert resolved[0]["bytesBase64"] == "Y2F0YWxvZy1waG90bw=="
    with pytest.raises(AdTemplateProcessError): process.resolve_catalog_assets(template, [{"assetKey": "missing", "fileName": "property-photo.webp", "mimeType": "image/webp"}])
    with pytest.raises(AdTemplateProcessError): process.resolve_catalog_assets({"schema": "blockwise.ad-template", "assets": {"x": {"fileName": "../property-photo.webp", "mimeType": "image/webp"}}}, [{"assetKey": "x", "fileName": "../property-photo.webp", "mimeType": "image/webp"}])


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
    template = {"schema": "blockwise.ad-template", "templateId": "candidate", "createdAt": "2026-08-30T00:00:00.000Z", "feedLayout": layout("feed", 1350), "storyLayout": layout("story", 1920), "imageInputs": [], "textInputs": [], "semanticColours": {"background": "#FFFFFF", "primary": "#1A56DB", "secondary": "#6B7280", "accent": "#F59E0B", "mainText": "#111827", "inverseText": "#FFFFFF"}, "assets": {}, "fonts": [], "metadata": {"title": "Smoke", "description": "Smoke", "gallerySamples": {}, "metaCopyDefaults": {}, "aiWritingGuidance": {}, "publishRequirements": {}, "replacementAssets": [], "realAssetRefs": []}}

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
        seen["body"] = json.loads(request.data.decode())
        return Response()
    monkeypatch.setenv("BLOCKWISE_TEMPLATE_IMPORT_URL", "http://blockwise.test/import")
    monkeypatch.setenv("BLOCKWISE_INTERNAL_AUTH_SECRET", "secret")
    monkeypatch.setattr(process.urllib.request, "urlopen", fake_urlopen)
    feed = tmp_path / "feed.png"; story = tmp_path / "story.png"
    feed.write_bytes(b"feed-png"); story.write_bytes(b"story-png")
    layout = lambda placement, height: {"placement": placement, "layers": [{"type": "plate", "layerId": placement, "colourRole": "background", "geometry": {"x": 0, "y": 0, "width": 1080, "height": height}, "protected": False}], "safeZones": [{"x": 0, "y": 0, "width": 1080, "height": height}]}
    template = {"schema": "blockwise.ad-template", "templateId": "trun-test", "createdAt": "2026-08-30T00:00:00.000Z", "feedLayout": layout("feed", 1350), "storyLayout": layout("story", 1920), "imageInputs": [], "textInputs": [], "semanticColours": {"background": "#FFFFFF"}, "assets": {}, "fonts": [], "metadata": {}}
    output = {"template": template, "assets": [{"assetKey": "feed", "fileName": "feed.png", "mimeType": "image/png", "bytesBase64": "ZmVlZC1wbmc="}, {"assetKey": "story", "fileName": "story.png", "mimeType": "image/png", "bytesBase64": "c3RvcnktcG5n"}], "previews": [{"path": str(feed), "placement": "feed"}, {"path": str(story), "placement": "story"}]}
    receipt = process.import_template(output, run_id="trun-test", project_id="blockwise")
    assert seen["authorization"] == "Bearer secret"
    assert seen["body"]["template"]["schema"] == "blockwise.ad-template"
    assert "feedLayout" in seen["body"]["template"] and "storyLayout" in seen["body"]["template"]
    assert "version" not in seen["body"]["template"] and "inputs" not in seen["body"]["template"] and "Meta" not in seen["body"]["template"]
    assert set(seen["body"]) == {"template", "assets"}
    assert {asset["assetKey"] for asset in seen["body"]["assets"]} == {"feed", "story"}
    assert all(asset["bytesBase64"] for asset in seen["body"]["assets"])
    assert receipt == {"template_id": "tpl-1", "status": "imported", "asset_count": 2, "replayed": False}
