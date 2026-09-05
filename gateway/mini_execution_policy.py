"""Server-owned execution boundary for the Mini customer surface.

Mini is an authenticated API client, but its customer content is untrusted.
This module turns the create-session policy acknowledgement into a persisted
runtime contract. Callers cannot select arbitrary toolsets, host paths, or
terminal backends: the only supported modes are the two fixed Mini modes.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import threading
from typing import Any, Iterator, Mapping


POLICY_VERSION = "mini.v1"
CAPABILITY = "mini_execution_boundary.v1"
POLICY_MODEL_CONFIG_KEY = "mini_execution_policy"
MINI_SOURCE = "mini_app"

_SESSION_RE = re.compile(
    r"^mini-(?P<kind>intake|job)-(?P<raw_id>[A-Za-z0-9_-]{8,80})$"
)
_MODE_FOR_KIND = {"intake": "guide", "job": "build"}
_POLICY_FIELDS = frozenset({"version", "mode", "workspace"})
_LEGACY_EXECUTION_FIELDS = frozenset(
    {"tool_policy", "cwd", "workspace", "memory_scope"}
)
_DEFAULT_WORKSPACE_ROOT = "/srv/frank/data/window/mini-shared/workspaces"


class MiniExecutionPolicyError(ValueError):
    """A Mini request cannot be represented by the fixed policy contract."""


def _trusted_workspace_root() -> Path:
    """Return the one server-configured Mini workspace root."""

    configured = os.environ.get(
        "MINI_EXECUTION_WORKSPACE_ROOT", _DEFAULT_WORKSPACE_ROOT
    ).strip()
    if not configured:
        raise MiniExecutionPolicyError("Mini execution workspace root is unavailable")
    root = Path(configured)
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise MiniExecutionPolicyError(
            "Mini execution workspace root is unavailable"
        ) from exc
    if root.is_symlink() or not resolved.is_dir():
        raise MiniExecutionPolicyError("Mini execution workspace root is unsafe")
    return resolved


def _validated_workspace(session_id: str, workspace: Any) -> str:
    match = _SESSION_RE.fullmatch(session_id)
    if match is None:
        raise MiniExecutionPolicyError("Mini session ID is invalid")
    if not isinstance(workspace, str) or not workspace.strip():
        raise MiniExecutionPolicyError("Mini execution workspace is required")

    root = _trusted_workspace_root()
    expected = root / match.group("raw_id")
    supplied = Path(workspace.strip())
    try:
        supplied_resolved = supplied.resolve(strict=True)
        expected_resolved = expected.resolve(strict=True)
    except OSError as exc:
        raise MiniExecutionPolicyError(
            "Mini execution workspace is unavailable"
        ) from exc

    if (
        supplied.is_symlink()
        or not supplied_resolved.is_dir()
        or supplied_resolved != expected_resolved
        or expected_resolved.parent != root
    ):
        raise MiniExecutionPolicyError(
            "Mini execution workspace does not match the session"
        )
    return str(expected_resolved)


def validate_policy(
    value: Any,
    *,
    session_id: str,
    source: str,
) -> dict[str, str]:
    """Validate and normalize a persisted or newly requested Mini policy."""

    if source != MINI_SOURCE:
        raise MiniExecutionPolicyError(
            "Mini execution policy is only valid for source mini_app"
        )
    if not isinstance(value, Mapping) or set(value) != _POLICY_FIELDS:
        raise MiniExecutionPolicyError("Mini execution policy fields are invalid")
    if value.get("version") != POLICY_VERSION:
        raise MiniExecutionPolicyError("Mini execution policy version is unsupported")

    match = _SESSION_RE.fullmatch(session_id)
    if match is None:
        raise MiniExecutionPolicyError("Mini session ID is invalid")
    expected_mode = _MODE_FOR_KIND[match.group("kind")]
    if value.get("mode") != expected_mode:
        raise MiniExecutionPolicyError(
            "Mini execution mode does not match the session"
        )

    return {
        "version": POLICY_VERSION,
        "mode": expected_mode,
        "workspace": _validated_workspace(session_id, value.get("workspace")),
    }


def policy_from_create_body(
    body: Mapping[str, Any], *, session_id: str, source: str
) -> dict[str, str] | None:
    """Validate create-time policy presence and reject policy spoofing."""

    supplied = "execution_policy" in body
    mini_session_id = _SESSION_RE.fullmatch(session_id) is not None
    if source != MINI_SOURCE:
        if supplied:
            raise MiniExecutionPolicyError(
                "Mini execution policy is only valid for source mini_app"
            )
        if mini_session_id:
            raise MiniExecutionPolicyError(
                "Mini session IDs are reserved for source mini_app"
            )
        return None
    legacy = sorted(_LEGACY_EXECUTION_FIELDS.intersection(body))
    if legacy:
        raise MiniExecutionPolicyError(
            "Legacy Mini execution fields are not accepted: " + ", ".join(legacy)
        )
    if not supplied:
        raise MiniExecutionPolicyError("Mini execution policy is required")
    return validate_policy(
        body.get("execution_policy"), session_id=session_id, source=source
    )


def _model_config_mapping(session: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = session.get("model_config")
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, Mapping) else {}
    return {}


def policy_from_session(session: Mapping[str, Any]) -> dict[str, str] | None:
    """Return the validated persisted policy, failing on inconsistent state."""

    source = str(session.get("source") or "")
    session_id = str(session.get("id") or "")
    raw = _model_config_mapping(session).get(POLICY_MODEL_CONFIG_KEY)
    mini_session_id = _SESSION_RE.fullmatch(session_id) is not None
    if source != MINI_SOURCE and mini_session_id:
        raise MiniExecutionPolicyError(
            "Mini session ID has an inconsistent source"
        )
    if source == MINI_SOURCE:
        if raw is None:
            raise MiniExecutionPolicyError(
                "Mini session has no persisted execution policy"
            )
        return validate_policy(raw, session_id=session_id, source=source)
    if raw is not None:
        raise MiniExecutionPolicyError(
            "Non-Mini session contains a Mini execution policy"
        )
    return None


def reject_turn_policy_fields(body: Mapping[str, Any]) -> None:
    """Policy is immutable after session creation and never accepted per turn."""

    forbidden = {"execution_policy", *_LEGACY_EXECUTION_FIELDS}.intersection(body)
    if forbidden:
        raise MiniExecutionPolicyError(
            "Execution policy fields are only accepted when creating a Mini session"
        )


def policy_ack(policy: Mapping[str, str]) -> dict[str, str]:
    return {
        "capability": CAPABILITY,
        "version": policy["version"],
        "mode": policy["mode"],
        "workspace": policy["workspace"],
    }


def agent_overrides(policy: Mapping[str, str] | None) -> dict[str, Any]:
    """Return fixed per-agent controls without consulting global tool config."""

    if policy is None:
        return {}
    common = {
        "skip_memory": True,
        "skip_context_files": True,
        "load_soul_identity": False,
        "skip_background_review": True,
    }
    if policy["mode"] == "guide":
        return {**common, "enabled_toolsets": [], "max_iterations": 1}
    return {**common, "enabled_toolsets": ["terminal", "file"]}


_leases_lock = threading.Lock()
_leases: dict[str, int] = {}


@contextmanager
def execution_scope(
    policy: Mapping[str, str] | None, session_id: str
) -> Iterator[None]:
    """Install and reliably remove the build session's restricted task scope."""

    if policy is None or policy["mode"] != "build":
        yield
        return

    from tools.terminal_tool import register_task_env_overrides

    overrides = {
        "env_type": "docker",
        "cwd": "/workspace",
        "host_cwd": policy["workspace"],
        "mini_restricted_docker": True,
    }
    with _leases_lock:
        count = _leases.get(session_id, 0)
        if count == 0:
            register_task_env_overrides(session_id, overrides)
        _leases[session_id] = count + 1
    try:
        yield
    finally:
        should_cleanup = False
        with _leases_lock:
            remaining = _leases.get(session_id, 1) - 1
            if remaining <= 0:
                _leases.pop(session_id, None)
                should_cleanup = True
            else:
                _leases[session_id] = remaining
        if should_cleanup:
            from tools.terminal_tool import clear_task_env_overrides, cleanup_vm

            try:
                cleanup_vm(session_id, force_remove=True)
            finally:
                clear_task_env_overrides(session_id)
