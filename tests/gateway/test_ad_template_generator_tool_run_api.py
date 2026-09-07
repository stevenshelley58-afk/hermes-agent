import asyncio
import json
import threading

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
async def test_model_catalog_exposes_canonical_capabilities_with_legacy_alias(
    tmp_path, monkeypatch,
):
    from hermes_cli import inventory

    monkeypatch.setattr(inventory, "load_picker_context", lambda: object())
    monkeypatch.setattr(
        inventory,
        "build_model_options_payload",
        lambda *_args, **_kwargs: {"providers": []},
    )
    store = ToolRunStore(str(tmp_path / "models.db"))

    response = await _API(store)._handle_tool_run_models(_Request(""))

    result = _payload(response)
    assert response.status == 200
    assert result["ad_template_generator_capabilities"]
    assert (
        result["ad_studio_capabilities"]
        == result["ad_template_generator_capabilities"]
    )
    capabilities = result["ad_template_generator_capabilities"]
    by_route = {(item["provider"], item["model"]): item for item in capabilities}
    assert by_route[("meta-direct", "muse-image-1.0")][
        "capability_verified"
    ] is True
    assert by_route[("meta-direct", "muse-image-1.0")]["supports_vision"] is True
    assert by_route[("meta-direct", "muse-image-1.0")]["supports_tools"] is False
    assert by_route[("concentrate", "gemini-3.8-flash")][
        "capability_verified"
    ] is True
    assert by_route[("openai-codex", "gpt-image-2-high")][
        "capability_verified"
    ] is True
    for route in (
        ("gemini", "gemini-3.1-flash-image"),
        ("gemini", "gemini-3-pro-image"),
        ("openai-api", "gpt-image-2"),
    ):
        assert by_route[route]["capability_verified"] is False


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



def _failed_with_checkpoint(store: ToolRunStore, key: str, home, *, status="failed"):
    run, _ = store.create_run(_command(key))
    run = store.transition_run(
        run["run_id"], expected_statuses={"queued"}, status="running",
        stage="compare", attention=True, event_kind="stage.started",
    )
    run = store.update_run(
        run["run_id"], status=status, stage="compare", attention=True,
        error=f"original {status} error",
    )
    workspace = home / "tool_runs" / "ad-template-generator" / run["run_id"]
    workspace.mkdir(parents=True)
    (workspace / "exact-clone-checkpoint.json").write_text(
        json.dumps({
            "process": "exact-clone",
            "candidate": {"template": {"templateId": f"candidate-{key}"}, "assets": []},
            "iterations": [],
        }),
        encoding="utf-8",
    )
    return run, workspace


@pytest.mark.asyncio
async def test_request_changes_requeues_failed_checkpoint_without_import_or_policy_loss(
    tmp_path, monkeypatch,
):
    import hermes_constants

    home = tmp_path / "hermes"
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: home)
    store = ToolRunStore(str(tmp_path / "failed.db"))
    run, _ = _failed_with_checkpoint(store, "failed-revision", home)
    policy = run["model_policy"]
    before_usage = store.provider_usage_totals(run["run_id"])
    store.record_provider_usage(
        run["run_id"], call_id="call-1", provider="test", model="model",
        role="compare", duration_ms=10, input_tokens=3, output_tokens=2,
        total_tokens=5, api_calls=1, estimated_cost_usd=0.25,
        cost_status="estimated", outcome="ok",
    )
    before_usage = store.provider_usage_totals(run["run_id"])
    revisions = []
    discarded = []
    monkeypatch.setattr(
        tool_run_api, "request_checkpoint_revision",
        lambda workspace, instructions: revisions.append((workspace, instructions)) or {},
    )
    monkeypatch.setattr(
        tool_run_api, "review_template_action",
        lambda **kwargs: discarded.append(kwargs),
    )

    api = _API(store)
    response = await api._handle_request_changes_tool_run(
        _Request(run["run_id"], {"instructions": "Use the measured headline geometry."}),
    )

    result = _payload(response)
    refreshed = store.get_run(run["run_id"])
    assert response.status == 202
    assert result["status"] == "queued"
    assert refreshed["status"] == "queued"
    assert refreshed["model_policy"] == policy
    assert store.provider_usage_totals(run["run_id"]) == before_usage
    assert revisions[0][1] == "Use the measured headline geometry."
    assert discarded == []
    assert api.started == [(run["run_id"], False)]
    assert [event["kind"] for event in store.events(run["run_id"])[-2:]] == [
        "command.changes-requested", "command.queued",
    ]


@pytest.mark.asyncio
async def test_request_changes_failed_import_discards_quarantine_before_requeue(
    tmp_path, monkeypatch,
):
    import hermes_constants

    home = tmp_path / "hermes"
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: home)
    store = ToolRunStore(str(tmp_path / "failed-import.db"))
    run, _ = _failed_with_checkpoint(store, "failed-import", home)
    store.append_event(
        run["run_id"], "template.imported", status="ok", node_id="import",
        data={"template_id": "imported-template"},
    )
    revisions = []
    discarded = []
    monkeypatch.setattr(
        tool_run_api, "request_checkpoint_revision",
        lambda workspace, instructions: revisions.append(instructions) or {},
    )
    monkeypatch.setattr(
        tool_run_api, "review_template_action",
        lambda **kwargs: discarded.append(kwargs) or {"status": "discarded"},
    )

    api = _API(store)
    response = await api._handle_request_changes_tool_run(
        _Request(run["run_id"], {"instructions": "Repair the failed imported draft."}),
    )

    assert response.status == 202
    assert revisions == ["Repair the failed imported draft."]
    assert discarded == [{
        "template_id": "imported-template", "run_id": run["run_id"],
        "action": "discard", "reason": "operator requested changes",
    }]
    assert api.started == [(run["run_id"], False)]


@pytest.mark.asyncio
async def test_request_changes_failed_checkpoint_error_restores_terminal_state(
    tmp_path, monkeypatch,
):
    import hermes_constants

    home = tmp_path / "hermes"
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: home)
    store = ToolRunStore(str(tmp_path / "failed-checkpoint.db"))
    run, _ = _failed_with_checkpoint(store, "failed-checkpoint", home, status="cancelled")
    original_error = run["error"]
    monkeypatch.setattr(
        tool_run_api, "request_checkpoint_revision",
        lambda *_args: (_ for _ in ()).throw(
            tool_run_api.AdTemplateProcessError("checkpoint is unwritable")
        ),
    )

    api = _API(store)
    response = await api._handle_request_changes_tool_run(
        _Request(run["run_id"], {"instructions": "Retry the saved candidate."}),
    )

    result = _payload(response)
    restored = store.get_run(run["run_id"])
    assert response.status == 409
    assert result["error"]["code"] == "invalid_template_changes"
    assert restored["status"] == "cancelled"
    assert restored["error"] == original_error
    assert restored["attention"] is True
    assert api.started == []
    assert store.events(run["run_id"])[-1]["kind"] == "command.changes-failed"


@pytest.mark.asyncio
async def test_request_changes_running_rejected_before_checkpoint_read_or_mutation(
    tmp_path, monkeypatch,
):
    import hermes_constants

    home = tmp_path / "hermes"
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: home)
    store = ToolRunStore(str(tmp_path / "running.db"))
    run, _ = _failed_with_checkpoint(store, "running-rejected", home, status="blocked")
    run = store.update_run(run["run_id"], status="running", stage="compare", attention=False)
    checkpoint_path = home / "tool_runs" / "ad-template-generator" / run["run_id"] / "exact-clone-checkpoint.json"
    before_checkpoint = checkpoint_path.read_text(encoding="utf-8")
    monkeypatch.setattr(
        tool_run_api, "request_checkpoint_revision",
        lambda *_args: pytest.fail("running request must not mutate checkpoint"),
    )

    api = _API(store)
    response = await api._handle_request_changes_tool_run(
        _Request(run["run_id"], {"instructions": "Do not touch the active draft."}),
    )

    assert response.status == 409
    assert store.get_run(run["run_id"])["status"] == "running"
    assert checkpoint_path.read_text(encoding="utf-8") == before_checkpoint
    assert api.started == []


@pytest.mark.asyncio
async def test_request_changes_missing_failed_candidate_is_rejected_without_start(
    tmp_path, monkeypatch,
):
    import hermes_constants

    home = tmp_path / "hermes"
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: home)
    store = ToolRunStore(str(tmp_path / "missing-candidate.db"))
    run, workspace = _failed_with_checkpoint(store, "missing-candidate", home)
    checkpoint_path = workspace / "exact-clone-checkpoint.json"
    checkpoint_path.write_text(
        json.dumps({"process": "exact-clone", "iterations": []}), encoding="utf-8",
    )
    monkeypatch.setattr(
        tool_run_api, "request_checkpoint_revision",
        lambda *_args: pytest.fail("missing candidate must not enter revision"),
    )

    api = _API(store)
    response = await api._handle_request_changes_tool_run(
        _Request(run["run_id"], {"instructions": "Retry without a candidate."}),
    )

    assert response.status == 409
    assert store.get_run(run["run_id"])["status"] == "failed"
    assert api.started == []


@pytest.mark.asyncio
async def test_request_changes_claim_allows_only_one_concurrent_terminal_revision(
    tmp_path, monkeypatch,
):
    import hermes_constants

    home = tmp_path / "hermes"
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: home)
    store = ToolRunStore(str(tmp_path / "concurrent.db"))
    run, _ = _failed_with_checkpoint(store, "concurrent", home)
    entered = threading.Event()
    release = threading.Event()

    def hold_revision(_workspace, _instructions):
        entered.set()
        assert release.wait(2)
        return {}

    monkeypatch.setattr(tool_run_api, "request_checkpoint_revision", hold_revision)
    api = _API(store)
    first = asyncio.create_task(api._handle_request_changes_tool_run(
        _Request(run["run_id"], {"instructions": "First bounded revision."}),
    ))
    await asyncio.to_thread(entered.wait, 2)
    second = await api._handle_request_changes_tool_run(
        _Request(run["run_id"], {"instructions": "Second concurrent revision."}),
    )
    release.set()
    first_response = await first

    assert first_response.status == 202
    assert second.status == 409
    assert api.started == [(run["run_id"], False)]
    assert [event["kind"] for event in store.events(run["run_id"])].count(
        "command.changes-requested"
    ) == 1
