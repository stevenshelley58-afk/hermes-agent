"""Read-only monitor projection of durable model accounting."""
from types import SimpleNamespace

from gateway.tool_run_api import ToolRunAPIMixin


def test_failed_run_exposes_usage_without_mutating_saved_output():
    usage = {"api_call_count": 2, "estimated_cost_usd": 0.12,
             "input_tokens": 120, "output_tokens": 30, "total_tokens": 150}
    api = ToolRunAPIMixin()
    api._tool_run_store = SimpleNamespace(provider_usage_totals=lambda run_id: usage)
    original = {"run_id": "run-test", "status": "failed", "output": {"cost": {"reported_usd": 0.1}}}
    view = api._tool_run_monitor_view(original)
    assert view["output"]["usage"]["total_tokens"] == 150
    assert view["output"]["cost"] == {"reported_usd": 0.1, "estimated_usd": 0.12}
    assert original["output"] == {"cost": {"reported_usd": 0.1}}
    assert view["status"] == "failed"


def test_run_without_calls_does_not_invent_zero_cost():
    api = ToolRunAPIMixin()
    api._tool_run_store = SimpleNamespace(provider_usage_totals=lambda run_id: {"api_call_count": 0})
    original = {"run_id": "run-test", "status": "queued", "output": None}
    assert api._tool_run_monitor_view(original) == original
