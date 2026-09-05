import json

import pytest

import gateway.tool_run_api as tool_run_api
from gateway.tool_run_api import ToolRunAPIMixin
from gateway.tool_runs import TOOL_RUN_COMMAND_SCHEMA, ToolRunStore


def _command(key: str) -> dict:
    return {
        "schema": TOOL_RUN_COMMAND_SCHEMA,
        "request_id": f"request-{key}",
        "tool_id": "ad-template-generator",
        "action": "build-template",
        "scope": {"project_id": "blockwise"},
        "payload": {"sources": [{"path": "/run/source.png"}]},
        "idempotency_key": key,
        "model_policy_revision": 1,
    }


class _Request:
    def __init__(self, run_id: str, body=None):
        self.match_info = {"run_id": run_id}
        self.can_read_body = body is not None
        self._body = body

    async def json(self):
        return self._body


class _API(ToolRunAPIMixin):
    def __init__(self, store):
        self._tool_run_store = store
        self.started = []

    @staticmethod
    def _check_auth(_request):
        return None

    def _start_tool_task(self, run_id: str, *, finalize: bool = False):
        self.started.append((run_id, finalize))


def _ready(store: ToolRunStore, key: str):
    run, _ = store.create_run(_command(key))
    return store.transition_run(
        run["run_id"], expected_statuses={"queued"},
        status="ready_for_review", stage="ready-for-review", attention=True,
        event_kind="template.ready-for-review", event_status="ok",
        output={"template": {"templateId": f"template-{key}"}},
    )


def _payload(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


@pytest.mark.asyncio
async def test_approve_activates_quarantine_then_completes(tmp_path, monkeypatch):
    store = ToolRunStore(str(tmp_path / "approve.db"))
    run = _ready(store, "approve")
    calls = []
    monkeypatch.setattr(
        tool_run_api, "review_template_action",
        lambda **kwargs: calls.append(kwargs) or {
            "templateId": kwargs["template_id"], "status": "active",
        },
    )

    response = await _API(store)._handle_approve_tool_run(
        _Request(run["run_id"], {}),
    )

    result = _payload(response)
    assert response.status == 200
    assert result["status"] == "completed"
    assert result["stage"] == "live"
    assert calls == [{
        "template_id": "template-approve", "run_id": run["run_id"],
        "action": "activate",
    }]
    assert [event["kind"] for event in store.events(run["run_id"])[-2:]] == [
        "command.approve", "template.published",
    ]


@pytest.mark.asyncio
async def test_request_changes_discards_quarantine_and_requeues_checkpoint(
    tmp_path, monkeypatch,
):
    store = ToolRunStore(str(tmp_path / "changes.db"))
    run = _ready(store, "changes")
    calls = []
    checkpoints = []
    monkeypatch.setattr(
        tool_run_api, "request_checkpoint_revision",
        lambda workspace, instructions: checkpoints.append((workspace, instructions)) or {},
    )
    monkeypatch.setattr(
        tool_run_api, "review_template_action",
        lambda **kwargs: calls.append(kwargs) or {
            "templateId": kwargs["template_id"], "status": "discarded",
        },
    )
    api = _API(store)

    response = await api._handle_request_changes_tool_run(
        _Request(run["run_id"], {"instructions": "Restore the exact inset shadow."}),
    )

    result = _payload(response)
    assert response.status == 202
    assert result["status"] == "queued"
    assert result["stage"] == "build"
    assert checkpoints[0][1] == "Restore the exact inset shadow."
    assert calls[0]["action"] == "discard"
    assert api.started == [(run["run_id"], False)]
    assert [event["kind"] for event in store.events(run["run_id"])[-2:]] == [
        "command.changes-requested", "command.queued",
    ]


@pytest.mark.asyncio
async def test_discard_is_remote_first_and_terminal(tmp_path, monkeypatch):
    store = ToolRunStore(str(tmp_path / "discard.db"))
    run = _ready(store, "discard")
    calls = []
    monkeypatch.setattr(
        tool_run_api, "review_template_action",
        lambda **kwargs: calls.append(kwargs) or {
            "templateId": kwargs["template_id"], "status": "discarded",
        },
    )

    response = await _API(store)._handle_discard_tool_run(
        _Request(run["run_id"], {"reason": "Wrong source."}),
    )

    result = _payload(response)
    assert response.status == 200
    assert result["status"] == "discarded"
    assert result["completed_at"] is not None
    assert calls[0]["action"] == "discard"
    assert calls[0]["reason"] == "Wrong source."


@pytest.mark.asyncio
async def test_failed_activate_returns_run_to_ready_for_review(tmp_path, monkeypatch):
    store = ToolRunStore(str(tmp_path / "activate-failure.db"))
    run = _ready(store, "activate-failure")

    def fail(**_kwargs):
        raise tool_run_api.AdTemplateProcessError("Blockwise rejected activation")

    monkeypatch.setattr(tool_run_api, "review_template_action", fail)

    response = await _API(store)._handle_approve_tool_run(
        _Request(run["run_id"], {}),
    )

    result = _payload(response)
    assert response.status == 409
    assert result["error"]["code"] == "invalid_template_approval"
    recovered = store.get_run(run["run_id"])
    assert recovered["status"] == "ready_for_review"
    assert recovered["attention"] is True
    assert store.events(run["run_id"])[-1]["kind"] == "template.publish-failed"
