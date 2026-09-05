import asyncio
import json
import threading
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.mini_execution_policy import (
    CAPABILITY,
    MiniExecutionPolicyError,
    agent_overrides,
    execution_scope,
    policy_ack,
    policy_from_create_body,
    policy_from_session,
    validate_policy,
)
from gateway.platforms.api_server import APIServerAdapter
from hermes_state import SessionDB
from tools import file_tools, terminal_tool


@pytest.fixture
def mini_workspace(monkeypatch, tmp_path):
    root = tmp_path / "workspaces"
    root.mkdir()
    raw_id = "customer_1234"
    workspace = root / raw_id
    workspace.mkdir()
    monkeypatch.setenv("MINI_EXECUTION_WORKSPACE_ROOT", str(root))
    return raw_id, workspace


def _policy(mode, workspace):
    return {"version": "mini.v1", "mode": mode, "workspace": str(workspace)}


def test_policy_binds_kind_to_exact_server_allowlisted_workspace(mini_workspace):
    raw_id, workspace = mini_workspace
    policy = validate_policy(
        _policy("build", workspace),
        session_id=f"mini-job-{raw_id}",
        source="mini_app",
    )
    assert policy == _policy("build", workspace.resolve())
    assert policy_ack(policy) == {
        "capability": CAPABILITY,
        **policy,
    }


@pytest.mark.parametrize(
    "session_id,mode",
    [
        ("mini-intake-customer_1234", "build"),
        ("mini-job-customer_1234", "guide"),
        ("ordinary-session", "guide"),
    ],
)
def test_policy_rejects_mode_or_id_mismatch(mini_workspace, session_id, mode):
    _, workspace = mini_workspace
    with pytest.raises(MiniExecutionPolicyError):
        validate_policy(
            _policy(mode, workspace), session_id=session_id, source="mini_app"
        )


def test_policy_rejects_unknown_version_extra_fields_and_outside_root(mini_workspace, tmp_path):
    raw_id, workspace = mini_workspace
    session_id = f"mini-job-{raw_id}"
    bad_version = _policy("build", workspace) | {"version": "mini.v2"}
    extra = _policy("build", workspace) | {"toolsets": ["terminal"]}
    outside = tmp_path / "outside"
    outside.mkdir()
    for candidate in (bad_version, extra, _policy("build", outside)):
        with pytest.raises(MiniExecutionPolicyError):
            validate_policy(candidate, session_id=session_id, source="mini_app")


def test_create_contract_rejects_missing_policy_legacy_fields_and_non_mini_spoof(mini_workspace):
    raw_id, workspace = mini_workspace
    session_id = f"mini-intake-{raw_id}"
    with pytest.raises(MiniExecutionPolicyError):
        policy_from_create_body({}, session_id=session_id, source="mini_app")
    with pytest.raises(MiniExecutionPolicyError):
        policy_from_create_body(
            {
                "execution_policy": _policy("guide", workspace),
                "tool_policy": "none",
            },
            session_id=session_id,
            source="mini_app",
        )
    with pytest.raises(MiniExecutionPolicyError):
        policy_from_create_body(
            {"execution_policy": _policy("guide", workspace)},
            session_id="ordinary-session",
            source="api_server",
        )



def test_mini_session_id_namespace_is_reserved(mini_workspace):
    raw_id, _ = mini_workspace
    with pytest.raises(MiniExecutionPolicyError):
        policy_from_create_body(
            {},
            session_id=f"mini-job-{raw_id}",
            source="api_server",
        )
    with pytest.raises(MiniExecutionPolicyError):
        policy_from_session(
            {
                "id": f"mini-job-{raw_id}",
                "source": "api_server",
                "model_config": {},
            }
        )

def test_persisted_policy_fails_closed_when_missing_or_attached_to_other_source(mini_workspace):
    raw_id, workspace = mini_workspace
    with pytest.raises(MiniExecutionPolicyError):
        policy_from_session(
            {"id": f"mini-intake-{raw_id}", "source": "mini_app", "model_config": "{}"}
        )
    with pytest.raises(MiniExecutionPolicyError):
        policy_from_session(
            {
                "id": "ordinary-session",
                "source": "api_server",
                "model_config": json.dumps(
                    {"mini_execution_policy": _policy("guide", workspace)}
                ),
            }
        )


def test_guide_agent_is_explicitly_tool_free_and_private_context_free():
    kwargs = agent_overrides(
        {"version": "mini.v1", "mode": "guide", "workspace": "/unused"}
    )
    assert kwargs == {
        "skip_memory": True,
        "skip_context_files": True,
        "load_soul_identity": False,
        "skip_background_review": True,
        "enabled_toolsets": [],
        "max_iterations": 1,
    }
    import model_tools

    assert model_tools.get_tool_definitions(enabled_toolsets=[], quiet_mode=True) == []


def test_build_agent_has_only_terminal_and_file_tools():
    kwargs = agent_overrides(
        {"version": "mini.v1", "mode": "build", "workspace": "/workspace"}
    )
    assert kwargs["enabled_toolsets"] == ["terminal", "file"]
    import model_tools

    definitions = model_tools.get_tool_definitions(
        enabled_toolsets=kwargs["enabled_toolsets"], quiet_mode=True
    )
    assert {item["function"]["name"] for item in definitions} <= {
        "terminal", "process", "read_file", "write_file", "patch", "search_files"
    }


def test_restricted_task_config_forces_docker_without_global_mutation(mini_workspace):
    raw_id, workspace = mini_workspace
    task_id = f"mini-job-{raw_id}"
    terminal_tool.register_task_env_overrides(
        task_id,
        {
            "env_type": "docker",
            "cwd": "/workspace",
            "host_cwd": str(workspace),
            "mini_restricted_docker": True,
        },
    )
    try:
        config = {
            "env_type": "local",
            "container_persistent": True,
            "docker_volumes": ["/private:/private"],
            "docker_forward_env": ["SECRET"],
            "docker_env": {"SECRET": "value"},
            "docker_extra_args": ["--privileged"],
            "docker_network": True,
        }
        container = terminal_tool._container_config_from_config(config, task_id)
        assert file_tools._terminal_env_type_for_task(task_id) == "docker"
        assert terminal_tool._resolve_task_host_cwd(config, task_id) == str(workspace.resolve())
        assert container["container_persistent"] is False
        assert container["docker_volumes"] == []
        assert container["docker_forward_env"] == []
        assert container["docker_env"] == {}
        assert container["docker_extra_args"] == []
        assert container["docker_network"] is False
        assert container["docker_allow_shared_mounts"] is False
        assert container["docker_allow_env_passthrough"] is False
        assert container["docker_read_only_root"] is True
        assert (
            container["docker_supplemental_group_gid"]
            == workspace.stat().st_gid
        )
        with (
            patch.object(terminal_tool, "_maybe_reap_docker_orphans"),
            patch.object(terminal_tool, "_DockerEnvironment") as docker_ctor,
        ):
            terminal_tool._create_environment(
                "docker",
                "test/image",
                "/workspace",
                60,
                container_config=container,
                task_id=task_id,
                host_cwd=str(workspace),
            )
            assert (
                docker_ctor.call_args.kwargs["supplemental_group_gid"]
                == workspace.stat().st_gid
            )
    finally:
        terminal_tool.clear_task_env_overrides(task_id)


def test_execution_scope_cleanup_is_reference_counted_for_concurrent_turns(mini_workspace, monkeypatch):
    raw_id, workspace = mini_workspace
    session_id = f"mini-job-{raw_id}"
    policy = _policy("build", workspace)
    entered = threading.Barrier(3)
    release = threading.Event()
    calls = []
    monkeypatch.setattr(terminal_tool, "register_task_env_overrides", lambda *a: calls.append(("register", a)))
    monkeypatch.setattr(terminal_tool, "cleanup_vm", lambda *a, **kw: calls.append(("cleanup", a, kw)))
    monkeypatch.setattr(terminal_tool, "clear_task_env_overrides", lambda *a: calls.append(("clear", a)))

    def worker():
        with execution_scope(policy, session_id):
            entered.wait()
            release.wait(2)

    threads = [threading.Thread(target=worker), threading.Thread(target=worker)]
    for thread in threads:
        thread.start()
    entered.wait()
    assert [call[0] for call in calls] == ["register"]
    release.set()
    for thread in threads:
        thread.join(2)
    assert [call[0] for call in calls] == ["register", "cleanup", "clear"]


def test_execution_scope_cleans_up_after_exception(mini_workspace, monkeypatch):
    raw_id, workspace = mini_workspace
    session_id = f"mini-job-{raw_id}"
    calls = []
    monkeypatch.setattr(terminal_tool, "register_task_env_overrides", lambda *a: calls.append("register"))
    monkeypatch.setattr(terminal_tool, "cleanup_vm", lambda *a, **kw: calls.append("cleanup"))
    monkeypatch.setattr(terminal_tool, "clear_task_env_overrides", lambda *a: calls.append("clear"))
    with pytest.raises(RuntimeError):
        with execution_scope(_policy("build", workspace), session_id):
            raise RuntimeError("cancelled")
    assert calls == ["register", "cleanup", "clear"]


def test_authenticated_create_persists_policy_and_returns_exact_ack(mini_workspace, tmp_path):
    async def scenario():
        raw_id, workspace = mini_workspace
        adapter = APIServerAdapter(
            PlatformConfig(enabled=True, extra={"key": "sk-mini-test"})
        )
        db = SessionDB(tmp_path / "state.db")
        adapter._session_db = db
        app = web.Application()
        app.router.add_post("/api/sessions", adapter._handle_create_session)
        try:
            async with TestClient(TestServer(app)) as client:
                policy = _policy("guide", workspace)
                response = await client.post(
                    "/api/sessions",
                    json={
                        "id": f"mini-intake-{raw_id}",
                        "source": "mini_app",
                        "execution_policy": policy,
                    },
                    headers={"Authorization": "Bearer sk-mini-test"},
                )
                assert response.status == 201, await response.text()
                payload = await response.json()
                assert payload["execution_policy_ack"] == {
                    "capability": CAPABILITY,
                    **_policy("guide", workspace.resolve()),
                }
                stored = db.get_session(f"mini-intake-{raw_id}")
                assert policy_from_session(stored) == _policy("guide", workspace.resolve())
        finally:
            db.close()

    asyncio.run(scenario())


def test_existing_mini_session_without_policy_refuses_turn(mini_workspace, tmp_path):
    async def scenario():
        raw_id, _ = mini_workspace
        adapter = APIServerAdapter(
            PlatformConfig(enabled=True, extra={"key": "sk-mini-test"})
        )
        db = SessionDB(tmp_path / "state.db")
        session_id = f"mini-intake-{raw_id}"
        db.create_session(session_id, "mini_app")
        adapter._session_db = db
        app = web.Application()
        app.router.add_post("/api/sessions/{session_id}/chat", adapter._handle_session_chat)
        try:
            async with TestClient(TestServer(app)) as client:
                response = await client.post(
                    f"/api/sessions/{session_id}/chat",
                    json={"message": "hello"},
                    headers={"Authorization": "Bearer sk-mini-test"},
                )
                assert response.status == 409
                payload = await response.json()
                assert payload["error"]["code"] == "execution_policy_unavailable"
        finally:
            db.close()

    asyncio.run(scenario())

@pytest.mark.parametrize(
    "mode,expected_toolsets,expected_iterations",
    [("guide", [], 1), ("build", ["terminal", "file"], 90)],
)
def test_create_agent_applies_mini_private_context_and_tool_scope(
    monkeypatch, mode, expected_toolsets, expected_iterations
):
    captured = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.provider = kwargs.get("provider")
            self.model = kwargs.get("model")

    monkeypatch.setattr("run_agent.AIAgent", FakeAgent)
    monkeypatch.setattr(
        "gateway.run._resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_key": "sk-global",
            "base_url": "https://example.invalid",
            "api_mode": "chat_completions",
        },
    )
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda: "test/model")
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {})
    monkeypatch.setattr(
        "gateway.run.GatewayRunner._load_reasoning_config", staticmethod(lambda model="": {})
    )
    monkeypatch.setattr(
        "gateway.run.GatewayRunner._load_fallback_model", staticmethod(lambda: None)
    )
    monkeypatch.setattr("gateway.run._current_max_iterations", lambda: 90)
    monkeypatch.setattr("hermes_cli.tools_config._get_platform_tools", lambda *_: {"hermes-api-server"})

    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    monkeypatch.setattr(adapter, "_ensure_session_db", lambda: None)
    adapter._create_agent(
        session_id="mini-test",
        mini_execution_policy={
            "version": "mini.v1",
            "mode": mode,
            "workspace": "/workspace",
        },
    )
    assert captured["enabled_toolsets"] == expected_toolsets
    assert captured["max_iterations"] == expected_iterations
    assert captured["skip_memory"] is True
    assert captured["skip_context_files"] is True
    assert captured["load_soul_identity"] is False
    assert captured["skip_background_review"] is True


def test_valid_mini_turn_uses_only_persisted_policy(mini_workspace, tmp_path):
    async def scenario():
        raw_id, workspace = mini_workspace
        session_id = f"mini-intake-{raw_id}"
        policy = _policy("guide", workspace.resolve())
        adapter = APIServerAdapter(
            PlatformConfig(enabled=True, extra={"key": "sk-mini-test"})
        )
        db = SessionDB(tmp_path / "state.db")
        db.create_session(
            session_id,
            "mini_app",
            model_config={"mini_execution_policy": policy},
        )
        adapter._session_db = db
        app = web.Application()
        app.router.add_post("/api/sessions/{session_id}/chat", adapter._handle_session_chat)
        try:
            async with TestClient(TestServer(app)) as client:
                with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as run:
                    run.return_value = (
                        {"final_response": "hello", "messages": []},
                        {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                    )
                    response = await client.post(
                        f"/api/sessions/{session_id}/chat",
                        json={"message": "hello"},
                        headers={"Authorization": "Bearer sk-mini-test"},
                    )
                    assert response.status == 200, await response.text()
                    assert run.call_args.kwargs["mini_execution_policy"] == policy

                    rejected = await client.post(
                        f"/api/sessions/{session_id}/chat",
                        json={"message": "hello", "execution_policy": policy},
                        headers={"Authorization": "Bearer sk-mini-test"},
                    )
                    assert rejected.status == 400
                    assert run.call_count == 1
        finally:
            db.close()

    asyncio.run(scenario())
