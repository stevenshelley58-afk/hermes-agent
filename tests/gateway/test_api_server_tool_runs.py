import hashlib
import json
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.tool_runs import TOOL_RUN_COMMAND_SCHEMA, ToolRunStore, default_ad_template_policy


def make_adapter(tmp_path):
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
    adapter._tool_run_store.close()
    adapter._tool_run_store = ToolRunStore(str(tmp_path / "tool-runs.db"))
    return adapter


def make_app(adapter):
    app = web.Application()
    app.router.add_post("/v1/tool-runs", adapter._handle_create_tool_run)
    app.router.add_get("/v1/tool-runs", adapter._handle_list_tool_runs)
    app.router.add_get("/v1/tool-runs/policies/{tool_id}", adapter._handle_list_tool_policies)
    app.router.add_post("/v1/tool-runs/policies/{tool_id}", adapter._handle_create_tool_policy)
    app.router.add_get("/v1/tool-runs/{run_id}", adapter._handle_get_tool_run)
    app.router.add_get("/v1/tool-runs/{run_id}/events", adapter._handle_tool_run_events)
    app.router.add_post("/v1/tool-runs/{run_id}/models", adapter._handle_tool_run_model_change)
    app.router.add_post("/v1/tool-runs/{run_id}/approval", adapter._handle_tool_run_approval)
    app.router.add_post("/v1/tool-runs/{run_id}/retry", adapter._handle_retry_tool_run)
    app.router.add_post("/v1/tool-runs/{run_id}/cancel", adapter._handle_cancel_tool_run)
    return app


def command(key="job-1"):
    return {
        "schema": TOOL_RUN_COMMAND_SCHEMA,
        "request_id": f"req-{key}",
        "tool_id": "ad-template-generator",
        "action": "build-template",
        "scope": {"project_id": "blockwise"},
        "payload": {"job_name": "August ads", "sources": [{"ref": "source:one"}]},
        "idempotency_key": key,
        "model_policy_revision": 1,
    }


def test_source_ingestion_is_private_hashed_and_jail_bounded(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    source = shared / "batch" / "source.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"\x89PNG\r\n\x1a\nprivate-pixels")
    monkeypatch.setenv("FRANK_SHARED_UPLOAD_ROOT", str(shared))
    with patch("hermes_constants.get_hermes_home", return_value=tmp_path / "hermes"):
        value = command()
        value["payload"]["sources"] = [{"path": str(source), "name": "source.png"}]
        ingested = APIServerAdapter._ingest_tool_sources(value)
        private = ingested["payload"]["sources"][0]
        assert private["ingested"] is True
        assert private["ref"].startswith("source:")
        assert str(tmp_path / "hermes" / "tool_assets") in private["path"]
        escaped = command("escaped")
        outside = tmp_path / "outside.png"
        outside.write_bytes(b"\x89PNG\r\n\x1a\nprivate-pixels")
        escaped["payload"]["sources"] = [{"path": str(outside)}]
        with pytest.raises(ValueError, match="outside"):
            APIServerAdapter._ingest_tool_sources(escaped)


def test_release_artifact_is_bound_to_private_bytes_and_signature(tmp_path):
    release = tmp_path / "hermes" / "tool_releases" / "ad-template-generator" / "pack.json"
    release.parent.mkdir(parents=True)
    signature = {"algorithm": "Ed25519", "key_id": "release-1", "value": "signed"}
    release.write_text(json.dumps({"integrity": {"signature": signature}}), encoding="utf-8")
    output = {
        "template_pack_path": str(release),
        "sha256": hashlib.sha256(release.read_bytes()).hexdigest(),
        "signature": signature,
    }
    with patch("hermes_constants.get_hermes_home", return_value=tmp_path / "hermes"):
        APIServerAdapter._validate_release_artifact(output)
        output["sha256"] = "0" * 64
        with pytest.raises(RuntimeError, match="checksum"):
            APIServerAdapter._validate_release_artifact(output)
        outside = tmp_path / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        output["template_pack_path"] = str(outside)
        with pytest.raises(RuntimeError, match="outside"):
            APIServerAdapter._validate_release_artifact(output)


@pytest.mark.asyncio
async def test_create_list_and_get_are_durable_not_chat(tmp_path):
    adapter = make_adapter(tmp_path)
    app = make_app(adapter)
    with patch.object(adapter, "_start_tool_task") as start:
        async with TestClient(TestServer(app)) as client:
            created = await client.post("/v1/tool-runs", json={"command": command()})
            assert created.status == 202
            run = await created.json()
            start.assert_called_once_with(run["run_id"])
            listed = await client.get("/v1/tool-runs?tool_id=ad-template-generator&project_id=blockwise")
            assert [item["run_id"] for item in (await listed.json())["data"]] == [run["run_id"]]
            fetched = await client.get(f"/v1/tool-runs/{run['run_id']}")
            assert (await fetched.json())["model_policy_revision"] == 1
    adapter._tool_run_store.close()


@pytest.mark.asyncio
async def test_same_idempotency_key_does_not_start_twice(tmp_path):
    adapter = make_adapter(tmp_path)
    app = make_app(adapter)
    with patch.object(adapter, "_start_tool_task") as start:
        async with TestClient(TestServer(app)) as client:
            first = await client.post("/v1/tool-runs", json=command())
            second = await client.post("/v1/tool-runs", json=command())
            assert first.status == 202
            assert second.status == 200
            assert (await first.json())["run_id"] == (await second.json())["run_id"]
            assert start.call_count == 1
    adapter._tool_run_store.close()


@pytest.mark.asyncio
async def test_policy_save_creates_revision_and_run_change_is_evented(tmp_path):
    adapter = make_adapter(tmp_path)
    app = make_app(adapter)
    policy = default_ad_template_policy()
    policy["name"] = "Current cheaper choice"
    policy["stages"]["analyse"]["primary"] = {"provider": "google", "model": "gemini-3.6-flash"}
    with patch.object(adapter, "_start_tool_task"):
        async with TestClient(TestServer(app)) as client:
            saved = await client.post("/v1/tool-runs/policies/ad-template-generator", json={"policy": policy})
            assert saved.status == 201
            assert (await saved.json())["revision"] == 2
            created = await client.post("/v1/tool-runs", json=command())
            run = await created.json()
            changed = await client.post(f"/v1/tool-runs/{run['run_id']}/models", json={"policy": policy})
            assert changed.status == 200
            assert (await changed.json())["model_policy_revision"] == 3
            events = adapter._tool_run_store.events(run["run_id"])
            assert events[-1]["kind"] == "model-policy.changed"
    adapter._tool_run_store.close()


@pytest.mark.asyncio
async def test_sse_replays_from_last_event_id(tmp_path):
    adapter = make_adapter(tmp_path)
    app = make_app(adapter)
    run, _ = adapter._tool_run_store.create_run(command())
    adapter._tool_run_store.append_event(run["run_id"], "stage.started", status="running", node_id="analyse", data={})
    adapter._tool_run_store.update_run(run["run_id"], status="completed", stage="release", progress=1, output={"release_id": "release-1"})
    adapter._tool_run_store.append_event(run["run_id"], "release.published", node_id="release", data={"release_id": "release-1"})
    async with TestClient(TestServer(app)) as client:
        response = await client.get(f"/v1/tool-runs/{run['run_id']}/events", headers={"Last-Event-ID": "0"})
        text = await response.text()
        assert "id: 1" in text
        assert "id: 2" in text
        assert "id: 0" not in text
        assert "release.published" in text
    adapter._tool_run_store.close()


@pytest.mark.asyncio
async def test_approval_requires_native_zoom_confirmation(tmp_path):
    adapter = make_adapter(tmp_path)
    app = make_app(adapter)
    run, _ = adapter._tool_run_store.create_run(command())
    adapter._tool_run_store.update_run(run["run_id"], status="waiting_for_approval", stage="studio-qa", progress=0.9)
    with patch.object(adapter, "_start_tool_task") as start:
        async with TestClient(TestServer(app)) as client:
            denied = await client.post(f"/v1/tool-runs/{run['run_id']}/approval", json={"decision": "approve"})
            assert denied.status == 400
            approved = await client.post(
                f"/v1/tool-runs/{run['run_id']}/approval",
                json={"decision": "approve", "confirm_100_percent": True},
            )
            assert approved.status == 202
            assert (await approved.json())["stage"] == "release"
            start.assert_called_once_with(run["run_id"], finalize=True)
    adapter._tool_run_store.close()
