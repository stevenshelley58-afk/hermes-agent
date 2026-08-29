import json
from types import SimpleNamespace

import pytest

import gateway.tool_run_api as tool_run_api
from gateway.platforms.api_server import APIServerAdapter
from gateway.tool_run_api import ToolRunAPIMixin
from gateway.tool_runs import TOOL_RUN_COMMAND_SCHEMA, ToolRunStore


def _command(source: str, *, idempotency_key: str) -> dict:
    return {
        "schema": TOOL_RUN_COMMAND_SCHEMA,
        "request_id": f"req-{idempotency_key}",
        "tool_id": "ad-template-generator",
        "action": "build-template",
        "scope": {"project_id": "blockwise"},
        "payload": {
            "brief": "Build one ad template",
            "placements": ["feed", "story"],
            "sources": [{"name": "source.png", "path": source}],
        },
        "idempotency_key": idempotency_key,
        "model_policy_revision": 1,
    }


class _PreviewAgent:
    def __init__(self, callback, store, run_id, observed):
        self._callback = callback
        self._store = store
        self._run_id = run_id
        self._observed = observed
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_total_tokens = 0
        self.session_estimated_cost_usd = 0.0

    def run_conversation(self, **_kwargs):
        self._callback(
            "tool.started",
            "terminal",
            "grep -r 'release import compare' /srv /opt/ad-template-builder",
            {},
        )
        self._observed["stage_after_preview"] = self._store.get_run(self._run_id)["stage"]
        self._observed["stage_events_after_preview"] = [
            event["node_id"]
            for event in self._store.events(self._run_id)
            if event["kind"] == "stage.started"
        ]
        return {"final_response": "{}"}


class _ToolRunHarness(ToolRunAPIMixin):
    def __init__(self, store):
        self._tool_run_store = store
        self._tool_run_tasks = {}
        self._tool_run_agents = {}
        self._tool_run_shutdown = False
        self._model_name = "test-model"
        self.observed = {}
        self.system_prompts = []

    def _resolve_route(self, _model):
        return {}

    def _create_agent(self, **kwargs):
        self.system_prompts.append(kwargs["ephemeral_system_prompt"])
        run_id = kwargs["session_id"].split(":", 1)[0]
        return _PreviewAgent(
            kwargs["tool_progress_callback"], self._tool_run_store, run_id, self.observed
        )

    def _check_auth(self, _request):
        return None


class _ExplicitEventOrchestrator:
    def __init__(self, *, call_agent, emit, **_kwargs):
        self.call_agent = call_agent
        self.emit = emit

    def run(self, **_kwargs):
        # A generic role activity preview happens before any orchestrator
        # lifecycle evidence. It must remain ordinary source-stage activity.
        self.call_agent(
            "builder-1",
            [{"type": "text", "text": "complete contract"}],
            "deepseek/deepseek-v4-flash",
        )
        for kind, node in (
            ("stage.started", "build"),
            ("iteration.started", "build"),
            ("iteration.rendered", "render"),
            ("iteration.compared", "compare"),
            ("final-review.started", "final-check"),
            ("final-review.completed", "final-check"),
            ("template.imported", "live"),
        ):
            self.emit(kind, node, {})
        return {
            "final_response": json.dumps(
                {
                    "iterations": [],
                    "import": {"template_id": "tpl-test", "status": "imported"},
                }
            )
        }


@pytest.mark.asyncio
async def test_generic_terminal_preview_never_advances_or_emits_stage(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    source = tmp_path / "source.png"
    source.write_bytes(b"source-pixels")
    store = ToolRunStore(str(tmp_path / "tool-runs.db"))
    run, _ = store.create_run(_command(str(source), idempotency_key="stage-truth"))
    api = _ToolRunHarness(store)
    monkeypatch.setattr(tool_run_api, "SoleProcessOrchestrator", _ExplicitEventOrchestrator)
    monkeypatch.setattr(api, "_prepare_candidate_output", lambda _run_id, output: output)

    await api._execute_tool_run(run["run_id"])

    assert api.observed["stage_after_preview"] == "source"
    assert api.observed["stage_events_after_preview"] == ["source"]
    events = store.events(run["run_id"])
    assert [
        event["node_id"] for event in events if event["kind"] == "stage.started"
    ] == ["source", "build", "render", "compare", "final-check", "live"]
    terminal_event = next(event for event in events if event["kind"] == "tool.started")
    assert terminal_event["node_id"] == "source"
    completed = store.get_run(run["run_id"])
    assert (completed["status"], completed["stage"], completed["progress"]) == (
        "completed",
        "live",
        1.0,
    )
    assert api.system_prompts == [ToolRunAPIMixin._isolated_tool_role_prompt()]


def test_isolated_role_prompt_forbids_discovery_and_orchestrator_work():
    prompt = ToolRunAPIMixin._isolated_tool_role_prompt().lower()
    assert "complete role contract" in prompt
    assert "attached images" in prompt
    assert "do not inspect" in prompt and "repositories or the filesystem" in prompt
    assert "terminal" in prompt and "broad shell searches" in prompt
    assert "/srv" in prompt and "/opt/ad-template-builder" in prompt
    assert "orchestrator alone" in prompt and "renderer" in prompt and "import" in prompt
    assert "exactly one requested json object" in prompt


class _InterruptibleRole:
    def __init__(self):
        self.messages = []

    def hard_interrupt(self, message=None):
        self.messages.append(message)


@pytest.mark.asyncio
async def test_cancel_without_live_task_transitions_and_stops_active_role(tmp_path):
    store = ToolRunStore(str(tmp_path / "cancel.db"))
    run, _ = store.create_run(_command("source.png", idempotency_key="cancel"))
    store.update_run(run["run_id"], status="running")
    api = _ToolRunHarness(store)
    role = _InterruptibleRole()
    api._tool_run_agents[run["run_id"]] = {"builder-1": role}
    cancelling_statuses = []
    request_cancel = store.request_cancel

    def tracked_request_cancel(run_id):
        result = request_cancel(run_id)
        cancelling_statuses.append(result["status"])
        return result

    store.request_cancel = tracked_request_cancel
    request = SimpleNamespace(match_info={"run_id": run["run_id"]})

    response = await api._handle_cancel_tool_run(request)

    assert response.status == 202
    assert json.loads(response.body)["status"] == "cancelled"
    assert cancelling_statuses == ["cancelling"]
    assert store.get_run(run["run_id"])["status"] == "cancelled"
    assert role.messages == ["api_server_tool_run_cancel"]
    assert [event["kind"] for event in store.events(run["run_id"])[-2:]] == [
        "command.cancel-requested",
        "run.cancelled",
    ]


@pytest.mark.asyncio
async def test_disconnect_stops_active_tool_role_with_current_interrupt_contract():
    role = _InterruptibleRole()
    adapter = SimpleNamespace(
        name="api_server",
        _mark_disconnected=lambda: None,
        _tool_run_shutdown=False,
        _tool_run_agents={"run": {"builder-1": role}},
        _tool_run_tasks={},
        _tool_run_store=None,
        _response_store=None,
        _site=None,
        _runner=None,
        _close_cached_session_dbs=lambda: None,
        _app=object(),
    )

    await APIServerAdapter.disconnect(adapter)

    assert role.messages == ["api_server_tool_run_shutdown"]
    assert adapter._tool_run_shutdown is True
    assert adapter._tool_run_agents == {}
    assert adapter._app is None
