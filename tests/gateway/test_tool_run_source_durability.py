"""A failed model preflight must not consume the only staged source."""

from __future__ import annotations
import asyncio

from pathlib import Path

import pytest

from gateway.tool_run_api import ToolRunAPIMixin
from gateway.tool_runs import TOOL_RUN_COMMAND_SCHEMA, ToolRunStore


def test_preflight_failure_preserves_durable_source_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / ".hermes"
    staging = home / "tool_runs" / "staging" / "source.png"
    staging.parent.mkdir(parents=True)
    staging.write_bytes(b"original-source-pixels")
    monkeypatch.setenv("HERMES_HOME", str(home))

    store = ToolRunStore(str(tmp_path / "runs.db"))
    run, _ = store.create_run({
        "schema": TOOL_RUN_COMMAND_SCHEMA,
        "request_id": "source-durability-request",
        "tool_id": "ad-template-generator",
        "action": "build-template",
        "scope": {"project_id": "blockwise"},
        "payload": {
            "brief": "Reconstruct exactly",
            "placements": ["feed", "story"],
            "sources": [{
                "name": "source.png",
                "path": str(staging),
                "size": staging.stat().st_size,
            }],
        },
        "idempotency_key": "source-durability-key",
        "model_policy_revision": 1,
    })

    api = ToolRunAPIMixin.__new__(ToolRunAPIMixin)
    api._tool_run_store = store
    api._tool_run_stop_events = {}
    api._tool_run_agents = {}
    api._tool_run_tasks = {}

    attempts = 0

    def unavailable(_run):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("provider plugin unavailable")

    api._frozen_tool_route_plan = unavailable
    asyncio.run(api._execute_tool_run(run["run_id"]))

    workspace = home / "tool_runs" / "ad-template-generator" / run["run_id"]
    durable = workspace / "previews" / "source.png"
    assert not staging.exists()
    assert durable.read_bytes() == b"original-source-pixels"
    assert api._durable_tool_source(workspace, str(staging)) == durable.resolve()

    # A second execution resolves the persisted preview before reaching the
    # same deliberately failing preflight; it does not fail on missing intake.
    asyncio.run(api._execute_tool_run(run["run_id"]))
    assert attempts == 2
    assert durable.read_bytes() == b"original-source-pixels"
    assert store.get_run(run["run_id"])["status"] == "failed"
