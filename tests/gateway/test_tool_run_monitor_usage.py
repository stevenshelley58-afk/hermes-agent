"""Read-only monitor projection of durable model accounting."""
from types import SimpleNamespace

from gateway.tool_run_api import ToolRunAPIMixin


def test_failed_run_exposes_usage_without_mutating_saved_output():
    usage = {"api_call_count": 2, "estimated_cost_usd": 0.12,
             "input_tokens": 120, "output_tokens": 30, "total_tokens": 150}
    api = ToolRunAPIMixin()
    api._tool_run_store = SimpleNamespace(provider_usage_totals=lambda run_id: usage, events=lambda *args, **kwargs: [])
    original = {"run_id": "run-test", "status": "failed", "output": {"cost": {"reported_usd": 0.1}}}
    view = api._tool_run_monitor_view(original)
    assert view["output"]["usage"]["total_tokens"] == 150
    assert view["output"]["cost"] == {"reported_usd": 0.1, "estimated_usd": 0.12}
    assert original["output"] == {"cost": {"reported_usd": 0.1}}
    assert view["status"] == "failed"


def test_run_without_calls_does_not_invent_zero_cost():
    api = ToolRunAPIMixin()
    api._tool_run_store = SimpleNamespace(provider_usage_totals=lambda run_id: {"api_call_count": 0}, events=lambda *args, **kwargs: [])
    original = {"run_id": "run-test", "status": "queued", "output": None}
    assert api._tool_run_monitor_view(original) == original


def test_failed_run_retains_ordered_previews_and_comparator_reasons():
    events = [
        {"kind": "iteration.compared", "data": {"iteration": 2, "score": 9.2, "reason": "Move title y to 74", "decision": "revise"}},
        {"kind": "iteration.rendered", "data": {"iteration": 2, "previews": ["iteration-02-feed.png"]}},
        {"kind": "iteration.compared", "data": {"iteration": 1, "score": 8.7, "reason": "Wrong font", "decision": "revise"}},
        {"kind": "iteration.rendered", "data": {"iteration": 1, "previews": ["iteration-01-feed.png"]}},
    ]
    api = ToolRunAPIMixin()
    api._tool_run_store = SimpleNamespace(
        provider_usage_totals=lambda run_id: {"api_call_count": 0},
        events=lambda run_id, **kwargs: events,
    )
    original = {"run_id": "run-test", "status": "failed", "output": None}
    view = api._tool_run_monitor_view(original)
    records = view["output"]["iterations"]
    assert [item["iteration"] for item in records] == [1, 2]
    assert records[0]["comparison"]["reason"] == "Wrong font"
    assert records[1]["previews"] == ["iteration-02-feed.png"]
    assert original["output"] is None
