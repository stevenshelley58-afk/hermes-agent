import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import gateway.ad_template_process as process
import gateway.tool_run_api as tool_run_api
from gateway.platforms.api_server import APIServerAdapter
from gateway.tool_run_api import ToolRunAPIMixin
from gateway.tool_runs import (
    TOOL_RUN_COMMAND_SCHEMA,
    ToolRunStore,
    default_ad_template_policy,
)


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


def _valid_template() -> dict:
    def layout(placement: str, height: int) -> dict:
        return {
            "placement": placement,
            "layers": [{
                "type": "plate",
                "layerId": f"{placement}-background",
                "colourRole": "background",
                "geometry": {"x": 0, "y": 0, "width": 1080, "height": height},
                "protected": False,
            }],
            "safeZones": [{"x": 0, "y": 0, "width": 1080, "height": height}],
        }

    return {
        "schema": "blockwise.ad-template",
        "templateId": "completion-test",
        "createdAt": "2026-08-30T00:00:00+00:00",
        "feedLayout": layout("feed", 1350),
        "storyLayout": layout("story", 1920),
        "imageInputs": [],
        "textInputs": [],
        "semanticColours": {
            "background": "#FFFFFF",
            "primary": "#1A56DB",
            "secondary": "#6B7280",
            "accent": "#F59E0B",
            "mainText": "#111827",
            "inverseText": "#FFFFFF",
        },
        "assets": {},
        "fonts": [],
        "metadata": {
            "title": "Completion test",
            "description": "",
            "gallerySamples": {},
            "metaCopyDefaults": {
                "primaryText": [], "headlines": [], "descriptions": [],
                "cta": "LEARN_MORE",
            },
            "aiWritingGuidance": {"summary": "", "fields": {}},
            "publishRequirements": {
                "objective": "OUTCOME_TRAFFIC",
                "specialAdCategory": None,
                "instantForm": {"required": False, "dependency": None},
                "destination": {
                    "required": True, "kind": "website",
                    "dependency": "landing_page_url",
                },
                "requiredCtaTypes": ["LEARN_MORE"],
            },
            "replacementAssets": [],
            "realAssetRefs": [],
        },
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
        self._observed["user_message"] = _kwargs.get("user_message")
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
        self._tool_run_stop_events = {}
        self._tool_run_drain_tasks = set()
        self._tool_run_shutdown = False
        self._model_name = "test-model"
        self.observed = {}
        self.system_prompts = []
        self.agent_kwargs = []

    def _resolve_route(self, _model):
        return {}

    def _create_agent(self, **kwargs):
        self.agent_kwargs.append(kwargs)
        self.system_prompts.append(kwargs["ephemeral_system_prompt"])
        run_id = kwargs["session_id"].split(":", 1)[0]
        return _PreviewAgent(
            kwargs["tool_progress_callback"], self._tool_run_store, run_id, self.observed
        )

    def _check_auth(self, _request):
        return None


class _StructuredRoleAgent:
    def __init__(self, instance_id: str, template: dict, messages: list):
        self._instance_id = instance_id
        self._template = template
        self._messages = messages
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_total_tokens = 0
        self.session_estimated_cost_usd = 0.0

    def run_conversation(self, **_kwargs):
        self._messages.append(_kwargs.get("user_message"))
        if self._instance_id.startswith("builder-"):
            payload = {"template": self._template, "assets": []}
        else:
            payload = {
                "reason": "All visible requirements pass",
                "hard_failures": [],
                "rubric": {field: 9.7 for field in process.RUBRIC_FIELDS},
            }
        return {"final_response": json.dumps(payload)}


class _RealOrchestratorHarness(_ToolRunHarness):
    def __init__(self, store, template):
        super().__init__(store)
        self._template = template
        self.role_calls = []
        self.role_messages = []

    def _create_agent(self, **kwargs):
        self.agent_kwargs.append(kwargs)
        instance_id = kwargs["session_id"].split(":", 2)[1]
        self.role_calls.append(instance_id)
        return _StructuredRoleAgent(instance_id, self._template, self.role_messages)


class _ExplicitEventOrchestrator:
    def __init__(self, *, call_agent, emit, **_kwargs):
        self.call_agent = call_agent
        self.emit = emit

    def run(self, *, routes, **_kwargs):
        assert [
            (route["provider"], route["model"])
            for route in routes
        ] == [
            ("openai-codex", "gpt-5.6-luna"),
            ("openai-codex", "gpt-5.6-luna"),
            ("deepseek", "deepseek-v4-flash-vision-exp"),
            ("openai-codex", "gpt-5.6-luna"),
            ("openai-codex", "gpt-5.6-sol"),
        ]
        # A generic role activity preview happens before any orchestrator
        # lifecycle evidence. It must remain ordinary source-stage activity.
        self.call_agent(
            "builder-1",
            [{"type": "text", "text": "complete contract"}],
            "openai-codex/gpt-5.6-luna",
        )
        for kind, node, data in (
            ("stage.started", "build", {}),
            ("iteration.started", "build", {}),
            ("iteration.rendered", "render", {}),
            ("iteration.compared", "compare", {}),
            ("builder.escalated", "build", {
                "iteration": 2,
                "from_provider": "openai-codex",
                "from_model": "gpt-5.6-luna",
                "to_provider": "openai-codex",
                "to_model": "gpt-5.6-sol",
                "reason": "insufficient_improvement",
                "previous_score": 8.2,
                "score": 8.3,
            }),
            ("final-review.started", "final-check", {}),
            ("final-review.completed", "final-check", {}),
            ("template.imported", "live", {}),
        ):
            self.emit(kind, node, data)
        return {
            "template": {},
            "iterations": [],
            "final_review": {},
            "previews": [],
            "documents": {},
            "import": {"template_id": "tpl-test", "status": "imported"},
            "process": "only-ad-template-process",
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
    ] == ["source", "build", "render", "compare", "build", "final-check", "live"]
    escalation = next(event for event in events if event["kind"] == "builder.escalated")
    assert escalation["node_id"] == "build"
    assert escalation["status"] == "running"
    assert escalation["data"] == {
        "iteration": 2,
        "from_provider": "openai-codex",
        "from_model": "gpt-5.6-luna",
        "to_provider": "openai-codex",
        "to_model": "gpt-5.6-sol",
        "reason": "insufficient_improvement",
        "previous_score": 8.2,
        "score": 8.3,
    }
    terminal_event = next(event for event in events if event["kind"] == "tool.started")
    assert terminal_event["node_id"] == "source"
    completed = store.get_run(run["run_id"])
    assert (completed["status"], completed["stage"], completed["progress"]) == (
        "completed",
        "live",
        1.0,
    )
    assert api.system_prompts == [ToolRunAPIMixin._isolated_tool_role_prompt()]
    assert api.agent_kwargs[0]["enabled_toolsets_override"] == []
    assert api.agent_kwargs[0]["persistence_disabled"] is True
    assert callable(api.agent_kwargs[0]["stream_delta_callback"])
    assert callable(api.agent_kwargs[0]["reasoning_callback"])
    assert "thinking_callback" not in api.agent_kwargs[0]


@pytest.mark.asyncio
async def test_execution_rejects_stale_pinned_policy_before_agent(tmp_path):
    store = ToolRunStore(str(tmp_path / "stale-execution.db"))
    run, _ = store.create_run(
        _command("source.png", idempotency_key="stale-execution")
    )
    stale = default_ad_template_policy()
    stale["seed_revision"] = 3
    stale["stages"]["analyse"]["primary"]["model"] = "deepseek-v4-flash"
    store._conn.execute(
        "UPDATE tool_runs SET policy_json=? WHERE run_id=?",
        (json.dumps(stale, separators=(",", ":"), sort_keys=True), run["run_id"]),
    )
    store._conn.commit()
    api = _ToolRunHarness(store)

    await api._execute_tool_run(run["run_id"])

    failed = store.get_run(run["run_id"])
    assert failed["status"] == "failed"
    assert "audited" in failed["error"]
    assert api.agent_kwargs == []


def test_direct_orchestrator_result_does_not_weaken_generic_agent_parser():
    result = {
        "template": {},
        "iterations": [],
        "final_review": {},
        "previews": [],
        "documents": {},
        "import": {},
        "process": "only-ad-template-process",
    }
    with pytest.raises(RuntimeError, match="structured JSON"):
        ToolRunAPIMixin._tool_json_output(result)
    assert ToolRunAPIMixin._tool_json_output(result, process_result=True) == result


@pytest.mark.asyncio
async def test_retry_reuses_persisted_source_after_staging_cleanup(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    staging = home / "tool_runs" / "staging"
    staging.mkdir(parents=True)
    source = staging / "source.png"
    source.write_bytes(b"source-pixels")
    monkeypatch.setenv("HERMES_HOME", str(home))
    store = ToolRunStore(str(tmp_path / "retry-source.db"))
    run, _ = store.create_run(
        _command(str(source), idempotency_key="retry-durable-source")
    )
    api = _ToolRunHarness(store)
    seen_sources = []

    class _FailOnce:
        def __init__(self, **_kwargs):
            pass

        def run(self, *, source, **_kwargs):
            seen_sources.append(source)
            assert Path(source).read_bytes() == b"source-pixels"
            raise process.AdTemplateProcessError("intentional first-run failure")

    monkeypatch.setattr(tool_run_api, "SoleProcessOrchestrator", _FailOnce)
    await api._execute_tool_run(run["run_id"])

    assert store.get_run(run["run_id"])["status"] == "failed"
    assert not source.exists()
    durable = (
        home
        / "tool_runs"
        / "ad-template-generator"
        / run["run_id"]
        / "previews"
        / "source.png"
    )
    assert durable.read_bytes() == b"source-pixels"

    class _SucceedOnRetry:
        def __init__(self, **_kwargs):
            pass

        def run(self, *, source, **_kwargs):
            seen_sources.append(source)
            assert Path(source).read_bytes() == b"source-pixels"
            return {
                "template": {},
                "iterations": [],
                "final_review": {},
                "previews": [],
                "documents": {},
                "import": {"template_id": "tpl-retried", "status": "imported"},
                "process": "only-ad-template-process",
            }

    monkeypatch.setattr(tool_run_api, "SoleProcessOrchestrator", _SucceedOnRetry)
    monkeypatch.setattr(api, "_prepare_candidate_output", lambda _run_id, output: output)
    started = []
    monkeypatch.setattr(
        api,
        "_start_tool_task",
        lambda run_id, finalize=False: started.append((run_id, finalize)),
    )
    response = await api._handle_retry_tool_run(
        SimpleNamespace(match_info={"run_id": run["run_id"]})
    )
    assert response.status == 202
    assert started == [(run["run_id"], False)]

    await api._execute_tool_run(run["run_id"])

    completed = store.get_run(run["run_id"])
    assert completed["status"] == "completed"
    assert completed["output"]["import"]["template_id"] == "tpl-retried"
    assert seen_sources == [str(durable), str(durable)]
    assert "command.queued" in {
        event["kind"] for event in store.events(run["run_id"])
    }


@pytest.mark.asyncio
async def test_real_orchestrator_successful_import_reaches_completed(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    source = tmp_path / "source.png"
    source.write_bytes(b"source-pixels")
    template = _valid_template()
    store = ToolRunStore(str(tmp_path / "completion.db"))
    run, _ = store.create_run(_command(str(source), idempotency_key="completion"))
    api = _RealOrchestratorHarness(store, template)

    def render(candidate, workspace: Path):
        rendered = workspace / "rendered"
        rendered.mkdir(parents=True, exist_ok=True)
        feed = rendered / "feed.png"
        story = rendered / "story.png"
        feed.write_bytes(b"feed-pixels")
        story.write_bytes(b"story-pixels")
        artifact = workspace / "artifact.json"
        artifact.write_text(json.dumps(candidate), encoding="utf-8")
        return {
            **candidate,
            "previews": [
                {"name": feed.name, "path": str(feed), "placement": "feed"},
                {"name": story.name, "path": str(story), "placement": "story"},
            ],
            "render": {"feed": str(feed), "story": str(story)},
            "receipt": {"outputs": {"feed": {}, "story": {}}},
            "template_path": str(artifact),
        }

    monkeypatch.setattr(process, "run_generator_cli", render)
    monkeypatch.setattr(
        process,
        "import_template",
        lambda _output, *, run_id, project_id: {
            "template_id": f"tpl-{run_id[-8:]}",
            "status": "imported",
            "project_id": project_id,
        },
    )
    assert tool_run_api.SoleProcessOrchestrator is process.SoleProcessOrchestrator

    await api._execute_tool_run(run["run_id"])

    completed = store.get_run(run["run_id"])
    assert completed["status"] == "completed"
    assert completed["stage"] == "live"
    assert completed["error"] is None
    assert completed["output"]["process"] == "only-ad-template-process"
    assert completed["output"]["import"]["status"] == "imported"
    assert len(api.role_calls) == 4
    assert api.role_calls[0].startswith("builder-")
    assert api.role_calls[1].startswith("comparator-")
    assert all(name.startswith("final-reviewer-") for name in api.role_calls[2:])
    assert not any(event["kind"] == "run.failed" for event in store.events(run["run_id"]))
    assert all(kwargs["enabled_toolsets_override"] == [] for kwargs in api.agent_kwargs)
    builder_message = api.role_messages[0]
    assert builder_message[0]["type"] == "text"
    assert builder_message[1]["type"] == "image_url"
    assert builder_message[1]["image_url"]["url"].startswith("data:image/png;base64,")
    comparator_message = api.role_messages[1]
    assert comparator_message[0]["type"] == "text"
    assert [part["type"] for part in comparator_message[1:]] == [
        "image_url", "image_url", "image_url",
    ]


@pytest.mark.asyncio
async def test_initial_builder_format_recovery_persists_events_and_all_role_costs(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    source = tmp_path / "source.png"
    source.write_bytes(b"source-pixels")
    template = _valid_template()
    store = ToolRunStore(str(tmp_path / "format-recovery.db"))
    run, _ = store.create_run(
        _command(str(source), idempotency_key="format-recovery")
    )

    class _Agent:
        def __init__(self, owner, instance_id):
            self.owner = owner
            self.instance_id = instance_id
            self.session_prompt_tokens = 10
            self.session_completion_tokens = 5
            self.session_total_tokens = 15
            self.session_estimated_cost_usd = 0.05

        def run_conversation(self, **_kwargs):
            self.owner.calls.append(self.instance_id)
            if self.instance_id.startswith("builder-"):
                self.owner.builder_calls += 1
                if self.owner.builder_calls <= 2:
                    return {"final_response": "not-json"}
                payload = {"template": template, "assets": []}
            else:
                payload = {
                    "reason": "All visible requirements pass",
                    "hard_failures": [],
                    "rubric": {
                        field: 9.7 for field in process.RUBRIC_FIELDS
                    },
                }
            return {"final_response": json.dumps(payload)}

    class _Harness(_ToolRunHarness):
        def __init__(self, run_store):
            super().__init__(run_store)
            self.calls = []
            self.builder_calls = 0

        def _create_agent(self, **kwargs):
            self.agent_kwargs.append(kwargs)
            instance_id = kwargs["session_id"].split(":", 2)[1]
            return _Agent(self, instance_id)

    api = _Harness(store)

    def render(candidate, workspace: Path):
        rendered = workspace / "rendered"
        rendered.mkdir(parents=True, exist_ok=True)
        feed = rendered / "feed.png"
        story = rendered / "story.png"
        feed.write_bytes(b"feed-pixels")
        story.write_bytes(b"story-pixels")
        artifact = workspace / "artifact.json"
        artifact.write_text(json.dumps(candidate), encoding="utf-8")
        return {
            **candidate,
            "previews": [
                {"name": feed.name, "path": str(feed), "placement": "feed"},
                {"name": story.name, "path": str(story), "placement": "story"},
            ],
            "render": {"feed": str(feed), "story": str(story)},
            "receipt": {"outputs": {"feed": {}, "story": {}}},
            "template_path": str(artifact),
        }

    monkeypatch.setattr(process, "run_generator_cli", render)
    monkeypatch.setattr(
        process,
        "import_template",
        lambda _output, *, run_id, project_id: {
            "template_id": "tpl-format-recovery",
            "status": "imported",
            "project_id": project_id,
        },
    )

    await api._execute_tool_run(run["run_id"])

    completed = store.get_run(run["run_id"])
    assert completed["status"] == "completed"
    assert api.calls[:3] == [
        "builder-1",
        "builder-1-output-retry-1",
        "builder-1-output-retry-2",
    ]
    assert len([name for name in api.calls if name.startswith("comparator-")]) == 1
    assert len([name for name in api.calls if name.startswith("final-reviewer-")]) == 2
    assert completed["output"]["usage"] == {
        "input_tokens": 60,
        "output_tokens": 30,
        "total_tokens": 90,
        "estimated_cost_usd": pytest.approx(0.30),
    }
    assert completed["output"]["cost"]["reported_usd"] == pytest.approx(0.30)
    events = store.events(run["run_id"])
    assert len([event for event in events if event["kind"] == "builder.output-retry"]) == 1
    escalation = next(
        event for event in events if event["kind"] == "builder.escalated"
    )
    assert escalation["data"]["reason"] == "structured_output_invalid"
    assert len([event for event in events if event["kind"] == "iteration.compared"]) == 1
    assert len([event for event in events if event["kind"] == "final-review.started"]) == 2
    assert len([event for event in events if event["kind"] == "final-review.completed"]) == 1


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

def _one_second_candidates(_run, stage):
    routes = {
        "analyse": {
            "provider": "openai-codex",
            "model": "gpt-5.6-luna",
        },
        "compare": {
            "provider": "openai-codex",
            "model": "gpt-5.6-luna",
        },
        "final-review-a": {
            "provider": "deepseek",
            "model": "deepseek-v4-flash-vision-exp",
        },
        "final-review-b": {
            "provider": "openai-codex",
            "model": "gpt-5.6-luna",
        },
        "quality-escalation": {
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
        },
    }
    return [routes[stage]], {
        "timeout_seconds": 1,
        "max_attempts": 1,
        "max_cost_usd": 1,
    }


class _OneRoleOrchestrator:
    def __init__(self, *, call_agent, emit, should_stop, **_kwargs):
        self.call_agent = call_agent
        self.emit = emit
        self.should_stop = should_stop

    def run(self, *, source, **_kwargs):
        payload = self.call_agent(
            "builder-1",
            process.vision_message("inspect", [source]),
            "openai-codex/gpt-5.6-luna",
        )
        if self.should_stop():
            raise process.AdTemplateProcessError(
                "sole ad-template process was cancelled"
            )
        return {
            "template": payload,
            "iterations": [],
            "final_review": {},
            "previews": [],
            "documents": {},
            "import": {"template_id": "tpl-heartbeat", "status": "imported"},
            "process": "only-ad-template-process",
        }


class _TimedRoleAgent:
    def __init__(self, behavior, kwargs):
        self._behavior = behavior
        self.kwargs = kwargs
        self.interrupts = []
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_total_tokens = 0
        self.session_estimated_cost_usd = 0.0

    def run_conversation(self, **kwargs):
        return self._behavior(self, kwargs)

    def hard_interrupt(self, message=None):
        self.interrupts.append(message)


class _TimedRoleHarness(_ToolRunHarness):
    def __init__(self, store, behavior):
        super().__init__(store)
        self._behavior = behavior
        self.created_agents = []

    def _create_agent(self, **kwargs):
        self.agent_kwargs.append(kwargs)
        agent = _TimedRoleAgent(self._behavior, kwargs)
        self.created_agents.append(agent)
        return agent


@pytest.mark.asyncio
async def test_stream_heartbeat_prevents_false_inactivity_timeout(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    source = tmp_path / "source.png"
    source.write_bytes(b"source-pixels")
    store = ToolRunStore(str(tmp_path / "heartbeat.db"))
    run, _ = store.create_run(_command(str(source), idempotency_key="heartbeat"))

    def streamed(agent, _kwargs):
        for _ in range(6):
            time.sleep(0.22)
            agent.kwargs["stream_delta_callback"]("chunk")
        return {"final_response": "{}"}

    api = _TimedRoleHarness(store, streamed)
    monkeypatch.setattr(api, "_tool_candidates", _one_second_candidates)
    monkeypatch.setattr(tool_run_api, "SoleProcessOrchestrator", _OneRoleOrchestrator)
    monkeypatch.setattr(api, "_prepare_candidate_output", lambda _run_id, output: output)

    await api._execute_tool_run(run["run_id"])

    completed = store.get_run(run["run_id"])
    assert completed["status"] == "completed"
    assert api.created_agents[0].interrupts == []


@pytest.mark.asyncio
async def test_silent_timeout_interrupts_drains_and_consumes_late_exception(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    source = tmp_path / "source.png"
    source.write_bytes(b"source-pixels")
    store = ToolRunStore(str(tmp_path / "silent-timeout.db"))
    run, _ = store.create_run(_command(str(source), idempotency_key="silent-timeout"))
    finished = {"value": False}

    def late_failure(_agent, _kwargs):
        time.sleep(1.2)
        finished["value"] = True
        raise RuntimeError("late role exception")

    api = _TimedRoleHarness(store, late_failure)
    monkeypatch.setattr(api, "_tool_candidates", _one_second_candidates)
    monkeypatch.setattr(tool_run_api, "SoleProcessOrchestrator", _OneRoleOrchestrator)

    await api._execute_tool_run(run["run_id"])
    assert store.get_run(run["run_id"])["status"] == "failed"
    assert api.created_agents[0].interrupts == [
        "api_server_tool_run_stage_timeout"
    ]
    await asyncio.sleep(0.35)
    assert finished["value"] is True
    assert api._tool_run_drain_tasks == set()


@pytest.mark.asyncio
async def test_late_timed_out_builder_cannot_render_or_import(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    source = tmp_path / "source.png"
    source.write_bytes(b"source-pixels")
    store = ToolRunStore(str(tmp_path / "late-builder.db"))
    run, _ = store.create_run(_command(str(source), idempotency_key="late-builder"))
    rendered = []
    imported = []

    def late_builder(_agent, _kwargs):
        time.sleep(1.2)
        return {"final_response": json.dumps({"template": {}, "assets": []})}

    api = _TimedRoleHarness(store, late_builder)
    monkeypatch.setattr(api, "_tool_candidates", _one_second_candidates)
    monkeypatch.setattr(process, "run_generator_cli", lambda *args: rendered.append(args))
    monkeypatch.setattr(process, "import_template", lambda *args, **kwargs: imported.append((args, kwargs)))

    await api._execute_tool_run(run["run_id"])
    assert store.get_run(run["run_id"])["status"] == "failed"
    await asyncio.sleep(0.35)
    assert api.created_agents[0].interrupts == [
        "api_server_tool_run_stage_timeout"
    ]
    assert api._tool_run_drain_tasks == set()
    assert rendered == []
    assert imported == []
