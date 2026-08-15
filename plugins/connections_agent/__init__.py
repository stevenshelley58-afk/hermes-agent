"""Private Connections workflow plugin for the single Hermes default profile."""
from __future__ import annotations

from .runtime import (
    CONNECTIONS_STATUS_SCHEMA,
    CONNECTIONS_REQUEST_SCHEMA,
    CONNECTIONS_RESEND_SCHEMA,
    CONNECTIONS_INSPECT_SCHEMA,
    ConnectionsRuntime,
    ConnectionsTokenProvider,
    load_settings,
)


def register(ctx) -> None:
    """Register the Connections tools only when explicitly enabled."""
    settings = load_settings(ctx)
    if not settings.enabled:
        return

    runtime = ConnectionsRuntime(settings)
    ctx.register_tool(
        name="connections_agent_status",
        toolset="connections",
        schema=CONNECTIONS_STATUS_SCHEMA,
        handler=lambda args, **_: runtime.status_tool(args),
        description="Show safe Connections Agent and adapter readiness metadata.",
        emoji="[link]",
    )
    ctx.register_tool(
        name="connections_agent_request",
        toolset="connections",
        schema=CONNECTIONS_REQUEST_SCHEMA,
        handler=lambda args, **_: runtime.request_tool(args),
        description="Send a non-secret Connections action through Frank's receipt contract.",
        emoji="[compass]",
    )
    ctx.register_tool(
        name="connections_agent_resend_mcp",
        toolset="connections",
        schema=CONNECTIONS_RESEND_SCHEMA,
        handler=lambda args, **_: runtime.resend_mcp_tool(args),
        description="Activate the restricted Resend MCP adapter after a recorded rotation.",
        emoji="[mail]",
    )
    ctx.register_tool(
        name="connections_agent_inspect",
        toolset="connections",
        schema=CONNECTIONS_INSPECT_SCHEMA,
        handler=lambda args, **_: runtime.inspect_tool(args),
        description="Inspect Frank's bounded private Connections projection before planning.",
        emoji="[search]",
    )

    broker_key = settings.broker_key
    if broker_key:
        ctx.register_dashboard_auth_provider(
            ConnectionsTokenProvider(secret=broker_key)
        )
