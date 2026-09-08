import asyncio
import json
from pathlib import Path

import pytest

import gateway.tool_run_api as tool_run_api
from gateway.review_revisions import (
    ReviewRevisionError,
    ReviewRevisionStore,
    freeze_snapshot,
    load_snapshot_candidate,
    restore_before_candidate,
    structured_review_instructions,
    validate_structured_review,
)
from gateway.tool_run_api import ToolRunAPIMixin
from gateway.tool_runs import TOOL_RUN_COMMAND_SCHEMA, ToolRunStore


PNG = b"\x89PNG\r\n\x1a\nfixture"


def candidate(template_id="candidate"):
    return {"template": {"schema": "blockwise.ad-template", "templateId": template_id,
                          "feedLayout": {"layers": []}, "storyLayout": {"layers": []}}, "assets": []}


def workspace(tmp_path, run_id="trun_" + "a" * 32):
    root = tmp_path / "hermes" / "tool_runs" / "ad-template-generator" / run_id
    (root / "final" / "rendered").mkdir(parents=True)
    value = candidate()
    (root / "final" / "artifact.json").write_text(json.dumps(value), encoding="utf-8")
    for placement in ("feed", "story"):
        (root / "final" / "rendered" / f"{placement}.png").write_bytes(PNG)
    return root, value


def command(key="review"):
    return {"schema": TOOL_RUN_COMMAND_SCHEMA, "request_id": key, "tool_id": "ad-template-generator",
            "action": "build-template", "scope": {"project_id": "blockwise"},
            "payload": {"sources": [{"path": "/run/source.png"}]}, "idempotency_key": key,
            "model_policy_revision": 1}


def ready(store, key, output=None):
    run, _ = store.create_run(command(key))
    return store.transition_run(run["run_id"], expected_statuses={"queued"}, status="ready_for_review",
                                stage="ready-for-review", attention=True, event_kind="template.ready-for-review",
                                event_status="ok", output=output or {"template": {"templateId": "candidate"}})


def test_structured_review_validation_and_instruction_bounds():
    value = validate_structured_review({"message": "", "annotations": [{"placement": "feed", "x": .1, "y": .2, "width": .3, "height": .4, "message": "Fix logo"}]})
    assert value["annotations"][0]["x"] == .1
    assert "Focus on the feed region" in structured_review_instructions(value)
    with pytest.raises(ReviewRevisionError):
        validate_structured_review({"message": "x", "annotations": [{"placement": "feed", "x": float("nan"), "y": 0, "width": .1, "height": .1, "message": "x"}]})
    with pytest.raises(ReviewRevisionError):
        validate_structured_review({"message": "x", "annotations": [{"placement": "feed", "x": .9, "y": 0, "width": .2, "height": .1, "message": "x"}]})


def test_snapshot_freezes_final_bytes_and_rejects_tampering(tmp_path):
    root, value = workspace(tmp_path)
    rid = "rrev_" + "b" * 32
    frozen = freeze_snapshot(root, rid, "before", value)
    (root / "final" / "rendered" / "feed.png").write_bytes(PNG + b"changed")
    assert (root / "previews" / frozen["previews"]["feed"]).read_bytes() == PNG
    assert load_snapshot_candidate(root, frozen) == value
    candidate_file = root / "review-revisions" / rid / frozen["candidate"]
    candidate_file.write_bytes(candidate_file.read_bytes() + b"x")
    with pytest.raises(ReviewRevisionError): load_snapshot_candidate(root, frozen)


def test_snapshot_rejects_symlink_and_ledger_names_match(tmp_path):
    root, value = workspace(tmp_path)
    (root / "final" / "rendered" / "story.png").unlink()
    (root / "final" / "rendered" / "story.png").symlink_to(root / "outside.png")
    with pytest.raises(ReviewRevisionError): freeze_snapshot(root, "rrev_" + "c" * 32, "before", value)
    store = ToolRunStore(str(tmp_path / "state.db")); ledger = ReviewRevisionStore(store)
    rid = "rrev_" + "d" * 32; root, value = workspace(tmp_path, "trun_" + "e" * 32)
    frozen = freeze_snapshot(root, rid, "before", value)
    record = ledger.create_pending(run_id="trun_" + "e" * 32, project_id="blockwise", idempotency_key="k", message="Fix", annotations=[], expected_revision=0, candidate_hash=frozen["candidate_hash"], before=frozen, revision_id=rid, request_hash="hash")
    assert record["revision_id"] == rid
    assert record["before_json"]["candidate"] == f"{rid}-before-candidate.json"


def test_snapshot_normalizes_reordered_final_assets_with_embedded_bytes(tmp_path):
    root, value = workspace(tmp_path)
    declarations = [{"assetKey": "asset-a", "fileName": "a.png", "mimeType": "image/png"}, {"assetKey": "asset-b", "fileName": "b.png", "mimeType": "image/png"}]
    value["assets"] = declarations
    final = candidate()
    final["assets"] = [{**declarations[1], "bytesBase64": "YmluYXJ5"}, {**declarations[0], "bytesBase64": "b3RoZXI="}]
    (root / "final" / "artifact.json").write_text(json.dumps(final), encoding="utf-8")
    frozen = freeze_snapshot(root, "rrev_" + "2" * 32, "before", value)
    assert load_snapshot_candidate(root, frozen)["assets"] == declarations


def test_restore_clears_review_state_but_keeps_iterations(tmp_path, monkeypatch):
    import hermes_constants
    home = tmp_path / "hermes"; monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: home)
    root, value = workspace(tmp_path, "trun_" + "f" * 32)
    from gateway.ad_template_generator_process import persist_checkpoint
    persist_checkpoint(root, {"candidate": value, "iterations": [{"iteration": 1}], "accepted": True,
                              "finalReview": {"decision": "accept"}, "manualRevision": 2,
                              "process": "exact-clone"})
    rid = "rrev_" + "1" * 32; frozen = freeze_snapshot(root, rid, "before", value)
    restored = restore_before_candidate(root, {"before_json": frozen})
    assert restored["iterations"] == [{"iteration": 1}]
    assert "accepted" not in restored and "finalReview" not in restored
    assert restored["candidate"]["template"]["metadata"].get("generationReview") is None if isinstance(restored["candidate"]["template"].get("metadata"), dict) else True


class API(ToolRunAPIMixin):
    def __init__(self, store): self._tool_run_store = store; self.started = []
    @staticmethod
    def _check_auth(_request): return None
    def _start_tool_task(self, run_id, *, finalize=False): self.started.append((run_id, finalize))


class Request:
    def __init__(self, run_id, body, query=None): self.match_info={"run_id": run_id}; self.body=body; self.query=query or {}
    async def json(self): return self.body


@pytest.mark.asyncio
async def test_handler_scope_approved_lock_and_duplicate_conflict(tmp_path, monkeypatch):
    import hermes_constants
    home=tmp_path / "hermes"; monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: home)
    store=ToolRunStore(str(tmp_path / "state.db")); run=ready(store, "handler")
    root, value=workspace(tmp_path, run["run_id"]); from gateway.ad_template_generator_process import persist_checkpoint
    checkpoint = {"process":"exact-clone", "candidate":value, "manualRevision":0, "iterations":[]}
    persist_checkpoint(root, checkpoint)
    (root / "exact-clone-checkpoint.json").write_text(json.dumps(checkpoint), encoding="utf-8")
    monkeypatch.setattr(tool_run_api, "review_template_action", lambda **_: {"status":"discarded"})
    monkeypatch.setattr(tool_run_api, "request_checkpoint_revision", lambda *_: None)
    api=API(store); body={"review":{"message":"Fix", "annotations":[]}, "project_id":"blockwise", "expected_revision":0, "idempotency_key":"same"}
    first=await api._handle_request_changes_tool_run(Request(run["run_id"],body)); assert first.status==202; assert api.started
    duplicate=await api._handle_request_changes_tool_run(Request(run["run_id"],body)); assert duplicate.status==200
    conflict=dict(body, review={"message":"Different", "annotations":[]}); conflict_response=await api._handle_request_changes_tool_run(Request(run["run_id"],conflict)); assert conflict_response.status==409


@pytest.mark.asyncio
async def test_revision_finalizes_after_snapshot_and_list_reads_checkpoint_revision(tmp_path, monkeypatch):
    import hermes_constants
    home=tmp_path / "hermes"; monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: home)
    store=ToolRunStore(str(tmp_path / "state.db")); run=ready(store, "finalize")
    root, value=workspace(tmp_path, run["run_id"])
    checkpoint={"process":"exact-clone", "candidate":value, "manualRevision":19, "iterations":[]}
    (root / "exact-clone-checkpoint.json").write_text(json.dumps(checkpoint), encoding="utf-8")
    ledger=ReviewRevisionStore(store); rid="rrev_"+"3"*32; before=freeze_snapshot(root,rid,"before",value)
    record=ledger.create_pending(run_id=run["run_id"], project_id="blockwise", idempotency_key="finalize", message="Fix", annotations=[], expected_revision=18, candidate_hash=before["candidate_hash"], before=before, revision_id=rid, request_hash="hash")
    finalized=ledger.finalize_after(root,run["run_id"],value)
    assert finalized["status"] == "ready_for_review" and finalized["after_json"]["previews"]["feed"].startswith(rid+"-after-")
    response=await API(store)._handle_list_review_revisions(Request(run["run_id"], None, {"project_id":"blockwise"}))
    payload=json.loads(response.body.decode())
    assert response.status == 200 and payload["current_revision"] == 19


@pytest.mark.asyncio
async def test_handler_rejects_stale_revision_and_project_mismatch(tmp_path, monkeypatch):
    import hermes_constants
    home=tmp_path / "hermes"; monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: home)
    store=ToolRunStore(str(tmp_path / "state.db")); run=ready(store, "stale")
    root, value=workspace(tmp_path, run["run_id"])
    checkpoint={"process":"exact-clone", "candidate":value, "manualRevision":2, "iterations":[]}
    (root / "exact-clone-checkpoint.json").write_text(json.dumps(checkpoint), encoding="utf-8")
    api=API(store)
    body={"review":{"message":"Fix", "annotations":[]}, "project_id":"blockwise", "expected_revision":1, "idempotency_key":"stale"}
    response=await api._handle_request_changes_tool_run(Request(run["run_id"],body)); assert response.status==409
    mismatch=dict(body, project_id="merrypaws", expected_revision=2, idempotency_key="scope")
    response=await api._handle_request_changes_tool_run(Request(run["run_id"],mismatch)); assert response.status==409


@pytest.mark.asyncio
async def test_approved_run_and_invalid_undo_do_not_discard(tmp_path, monkeypatch):
    import hermes_constants
    home=tmp_path / "hermes"; monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: home)
    store=ToolRunStore(str(tmp_path / "state.db")); run=ready(store, "approved")
    store.transition_run(run["run_id"], expected_statuses={"ready_for_review"}, status="completed", stage="live", attention=False, event_kind="template.published", event_status="ok")
    calls=[]; monkeypatch.setattr(tool_run_api, "review_template_action", lambda **kw: calls.append(kw))
    api=API(store)
    body={"review":{"message":"Fix", "annotations":[]}, "project_id":"blockwise", "expected_revision":0, "idempotency_key":"approved"}
    response=await api._handle_request_changes_tool_run(Request(run["run_id"],body)); assert response.status==409; assert calls==[]
    run=ready(store, "undo-invalid"); root,value=workspace(tmp_path, run["run_id"])
    checkpoint={"process":"exact-clone", "candidate":value, "manualRevision":0, "iterations":[]}
    (root / "exact-clone-checkpoint.json").write_text(json.dumps(checkpoint), encoding="utf-8")
    body={"review":{"message":"Restore", "annotations":[]}, "project_id":"blockwise", "expected_revision":0, "idempotency_key":"undo-invalid", "undo_of":"rrev_"+"9"*32}
    response=await api._handle_request_changes_tool_run(Request(run["run_id"],body)); assert response.status==409; assert calls==[]
