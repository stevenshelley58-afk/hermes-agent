from types import SimpleNamespace

import pytest

from gateway.tool_run_usage import agent_provider_usage, assert_run_usage_accounted
from gateway.tool_runs import TOOL_RUN_COMMAND_SCHEMA, ToolRunStore


def _run(store: ToolRunStore, key: str = "usage-budget") -> str:
    run, _ = store.create_run({
        "schema": TOOL_RUN_COMMAND_SCHEMA,
        "request_id": f"request-{key}",
        "tool_id": "ad-template-generator",
        "action": "build-template",
        "scope": {"project_id": "blockwise"},
        "payload": {"sources": [{"path": "/run/source.png"}]},
        "idempotency_key": key,
        "model_policy_revision": 1,
    })
    return run["run_id"]


def test_provider_usage_is_durable_and_cumulative_across_store_reopen(tmp_path):
    path = tmp_path / "usage.db"
    store = ToolRunStore(str(path))
    run_id = _run(store)
    first = store.record_provider_usage(
        run_id,
        call_id=f"{run_id}:compare-1:a",
        provider="concentrate",
        model="cheap-vision",
        role="compare-1",
        duration_ms=1250,
        input_tokens=100,
        output_tokens=20,
        total_tokens=120,
        api_calls=1,
        estimated_cost_usd=0.015,
        cost_status="actual",
        outcome="ok",
    )
    assert first["call_count"] == 1
    store.close()

    reopened = ToolRunStore(str(path))
    totals = reopened.record_provider_usage(
        run_id,
        call_id=f"{run_id}:compare-2:b",
        provider="concentrate",
        model="cheap-vision",
        role="compare-2",
        duration_ms=750,
        input_tokens=80,
        output_tokens=10,
        total_tokens=90,
        api_calls=1,
        estimated_cost_usd=0.01,
        cost_status="estimated",
        outcome="error",
    )

    assert totals == {
        "input_tokens": 180,
        "output_tokens": 30,
        "total_tokens": 210,
        "estimated_cost_usd": 0.025,
        "duration_ms": 2000,
        "call_count": 2,
        "api_call_count": 2,
        "unpriced_call_count": 0,
    }
    events = [event for event in reopened.events(run_id) if event["kind"] == "provider.usage"]
    assert [event["data"]["role"] for event in events] == ["compare-1", "compare-2"]
    assert events[1]["status"] == "error"


def test_agent_usage_reads_canonical_session_counters():
    usage = agent_provider_usage(SimpleNamespace(
        session_input_tokens=37,
        session_output_tokens=11,
        session_total_tokens=52,
        session_api_calls=2,
        session_estimated_cost_usd=0.0042,
        session_cost_status="estimated",
        # Legacy prompt/completion values deliberately differ.
        session_prompt_tokens=999,
        session_completion_tokens=999,
    ))
    assert usage == {
        "input_tokens": 37,
        "output_tokens": 11,
        "total_tokens": 52,
        "api_calls": 2,
        "estimated_cost_usd": 0.0042,
        "cost_status": "estimated",
    }


def test_budget_blocks_resume_from_durable_total_and_unknown_pricing():
    with pytest.raises(RuntimeError, match="durable total 1.0000"):
        assert_run_usage_accounted(
            {"estimated_cost_usd": 1.0, "unpriced_call_count": 0},
            1.0,
            before_call=True,
        )
    with pytest.raises(RuntimeError, match="without cost accounting"):
        assert_run_usage_accounted(
            {"estimated_cost_usd": 0.0, "unpriced_call_count": 1},
            10.0,
            before_call=True,
        )
    # The call that reaches the exact limit is allowed to finish, but no next
    # call may start; a call that exceeds it fails immediately.
    assert_run_usage_accounted(
        {"estimated_cost_usd": 1.0, "unpriced_call_count": 0},
        1.0,
        before_call=False,
    )
    with pytest.raises(RuntimeError, match="durable total 1.0100"):
        assert_run_usage_accounted(
            {"estimated_cost_usd": 1.01, "unpriced_call_count": 0},
            1.0,
            before_call=False,
        )

