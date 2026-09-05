"""Provider accounting helpers for durable Tool-run budget enforcement."""

from __future__ import annotations

import math
from typing import Any, Dict


def agent_provider_usage(agent: Any) -> Dict[str, Any]:
    input_tokens = int(getattr(agent, "session_input_tokens", 0) or 0)
    output_tokens = int(getattr(agent, "session_output_tokens", 0) or 0)
    total_tokens = int(getattr(agent, "session_total_tokens", 0) or 0)
    api_calls = int(getattr(agent, "session_api_calls", 0) or 0)
    cost = float(getattr(agent, "session_estimated_cost_usd", 0.0) or 0.0)
    cost_status = str(getattr(agent, "session_cost_status", "unknown") or "unknown")
    if cost_status not in {"actual", "estimated", "included", "unknown"}:
        cost_status = "unknown"
    if min(input_tokens, output_tokens, total_tokens, api_calls) < 0 or not math.isfinite(cost) or cost < 0:
        raise RuntimeError("provider returned invalid usage accounting")
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "api_calls": api_calls,
        "estimated_cost_usd": cost,
        "cost_status": cost_status,
    }


def assert_run_usage_accounted(totals: Dict[str, Any], cost_limit: float, *, before_call: bool) -> None:
    if int(totals.get("unpriced_call_count") or 0) > 0:
        raise RuntimeError(
            "sole ad-template process has provider usage without cost accounting; refusing further model calls"
        )
    cost = float(totals.get("estimated_cost_usd") or 0.0)
    exhausted = cost >= cost_limit if before_call else cost > cost_limit
    if cost_limit > 0 and exhausted:
        raise RuntimeError(
            f"sole ad-template process exceeded whole-run cost limit {cost_limit:.2f} "
            f"(durable total {cost:.4f})"
        )
