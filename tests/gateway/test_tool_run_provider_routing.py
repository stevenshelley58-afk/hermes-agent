"""Frozen generator routes must retain their configured provider identity."""

from __future__ import annotations

import gateway.platforms.api_server as api_server

from gateway.tool_run_api import ToolRunAPIMixin


def test_preflight_accepts_named_custom_provider_selected_by_runtime(monkeypatch) -> None:
    """A configured provider may use Hermes' shared custom transport."""
    monkeypatch.setattr(
        api_server,
        "_resolve_request_runtime_agent_kwargs",
        lambda provider, target_model: {
            "provider": "custom",
            "requested_provider": provider,
            "api_mode": "codex_responses",
            "base_url": "https://provider.example/v1",
        },
    )

    ToolRunAPIMixin._preflight_tool_candidate(
        {"provider": "meta-direct", "model": "muse-spark-1.3-contributor"}
    )


def test_preflight_rejects_custom_transport_without_configured_identity(monkeypatch) -> None:
    """A generic custom fallback is never a substitute for a frozen route."""
    monkeypatch.setattr(
        api_server,
        "_resolve_request_runtime_agent_kwargs",
        lambda _provider, target_model: {
            "provider": "custom",
            "requested_provider": "different-provider",
            "api_mode": "codex_responses",
            "base_url": "https://provider.example/v1",
        },
    )

    try:
        ToolRunAPIMixin._preflight_tool_candidate(
            {"provider": "meta-direct", "model": "muse-spark-1.3-contributor"}
        )
    except RuntimeError as exc:
        assert "identity did not resolve" in str(exc)
    else:
        raise AssertionError("a mismatched configured provider identity must fail closed")


def test_preflight_retains_named_provider_identity_from_real_config(tmp_path, monkeypatch) -> None:
    """The API runtime wrapper must not drop the resolver's named identity."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        "providers:\n"
        "  meta-direct:\n"
        "    base_url: https://provider.example/v1\n"
        "    api_mode: codex_responses\n"
        "    key_env: TEST_META_MODEL_API_KEY\n"
        "    model: muse-spark-1.3-contributor\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("TEST_META_MODEL_API_KEY", "test-only-provider-key")

    runtime = api_server._resolve_request_runtime_agent_kwargs(
        "meta-direct", target_model="muse-spark-1.3-contributor"
    )

    assert runtime["provider"] == "custom"
