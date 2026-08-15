"""Hermes-side Connections runtime and narrow Infisical broker.

The module deliberately has no generic secret proxy. It exposes only fixed
metadata/create/rotate/delete operations against one configured Infisical CE
project and one secret path. Values are accepted only for the fixed mutation
operations, used in Hermes memory for the provider handoff, and never returned
or written to logs/state.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from hermes_cli.dashboard_auth import (
    DashboardAuthProvider,
    LoginStart,
    Session,
    TokenPrincipal,
)
from tools.registry import tool_error, tool_result
from utils import atomic_json_write

try:
    from hermes_constants import get_hermes_home
except ImportError:  # pragma: no cover - standalone module tests
    def get_hermes_home() -> Path:
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


SCHEMA_VERSION = "hermes.connections.vault-broker.v1"
BROKER_BASE_PATH = "/api/plugins/connections-agent/vault-broker"
PROFILE_NAME = "default"
SESSION_NAME = "Connections Agent"
RESEND_SERVER_NAME = "resend"
RESEND_CAPABILITIES = ("email.send", "email.status")
RESEND_MCP_PACKAGE = "resend-mcp@2.13.0"
RESEND_MCP_TOOL_NAMES = ("send-email", "get-email")
SUCCESS_OUTCOMES = frozenset({"created", "updated", "verified", "synced", "revoked", "deleted"})
PLAN_ACTIONS = frozenset({"discover", "create", "update", "verify", "sync", "revoke", "delete"})
ERROR_CATEGORIES = frozenset({"auth", "configuration", "network", "not_found", "permission_denied", "rate_limited", "timeout", "unavailable", "validation", "unknown"})
ERROR_CODES = frozenset({
    "auth_failed", "confirmation_required", "infisical_auth_failed", "infisical_not_found",
    "infisical_permission_denied", "infisical_unavailable", "infisical_rate_limited", "invalid_completion",
    "idempotency_conflict", "idempotency_store_unavailable", "idempotency_uncertain", "invalid_request",
    "mcp_unavailable", "provider_evidence_required", "provider_error", "provider_rejected",
    "rate_limited", "receipt_missing", "setup_needed", "timeout", "unknown_error",
})
_SECRET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_MAX_REQUEST_BYTES = 64 * 1024
_MAX_SECRET_VALUE_BYTES = 16 * 1024
_SAFE_METADATA_FIELDS = {
    "id", "_id", "environment", "version", "type", "secretKey",
    "secretPath", "createdAt", "updatedAt", "secretValueHidden",
    "isRotatedSecret", "rotationId", "folderId",
}
_OPAQUE_RECEIPT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{7,255}$")
_FRANK_CONFIRMATION_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
_FRANK_PROVIDER_RECEIPT_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_PROVIDER_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")
_SAFE_TARGET_FIELDS = frozenset({"provider", "connection_id", "consumer", "project", "environment"})
_SAFE_BODY_FIELDS = frozenset({
    "provider", "name", "scope_kind", "scope_id", "status", "connection_ref",
    "credential_ref", "admin_url", "capabilities", "notes", "last_verified_at",
})
_SAFE_PROVIDERS = frozenset({"resend", "stripe", "activepieces", "mcp", "api"})
_SAFE_SCOPES = frozenset({"global", "project", "tool", "agent", "service"})
_SAFE_STATUSES = frozenset({"setup_needed", "connected", "verified", "error"})
_SAFE_REFERENCE_FIELDS = frozenset({"connection_ref", "credential_ref"})
_INSPECT_TOP_FIELDS = frozenset({"schema", "connections", "attention", "activity"})
_INSPECT_CONNECTION_FIELDS = frozenset({
    "id", "name", "provider", "status", "scope_kind", "scope_id", "connection_ref",
    "credential_ref", "last_verified_at", "capabilities", "revision",
})
_INSPECT_ACTION_FIELDS = frozenset({
    "sequence", "receipt_id", "correlation_id", "source", "actor", "action", "state",
    "progress", "started_at", "updated_at", "completed_at", "target", "result",
})
_INSPECT_RESULT_FIELDS = frozenset({
    "connection_id", "provider", "status", "revision", "removed", "verified_at", "outcome",
    "pending", "provider_receipt", "error_code", "error_category",
})
_SENSITIVE_FIELD_RE = re.compile(
    r"(?:secret|token|auth|password|credential|api[_-]?key|payment|card|cvv|private[_-]?key|bearer)",
    re.IGNORECASE,
)
_SECRET_LIKE_VALUE_RE = re.compile(
    r"(?:\b(?:re|sk|rk|pk)_[A-Za-z0-9_-]{8,}\b|\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b|\b(?:api[_-]?key|secret|token|password)\s*[:=])",
    re.IGNORECASE,
)
_PAYMENT_LIKE_VALUE_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,239}$")
_VAULT_REFERENCE_RE = re.compile(
    r"^(?:vault|openbao|bitwarden|1password|pass|keyring|secret)://[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._/-]*$",
    re.IGNORECASE,
)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def _open_no_redirect(request: urllib.request.Request):
    try:
        response = _NO_REDIRECT_OPENER.open(request, timeout=15)
        status = response.getcode()
        if status is not None and 300 <= status < 400:
            response.close()
            raise ConnectionsError("redirects are not accepted for authenticated requests")
        return response
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise ConnectionsError("redirects are not accepted for authenticated requests") from exc
        raise

CONNECTIONS_STATUS_SCHEMA = {
    "name": "connections_agent_status",
    "description": "Show safe Connections Agent status metadata.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}
CONNECTIONS_REQUEST_SCHEMA = {
    "name": "connections_agent_request",
    "description": "Send a non-secret plan/apply request through Frank's receipt contract.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["plan", "apply"]},
            "request": {
                "oneOf": [
                    {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": sorted(PLAN_ACTIONS)},
                            "target": {"type": "object"},
                            "body": {"type": "object"},
                            "connection_id": {"type": "string", "minLength": 8, "maxLength": 256},
                            "expected_revision": {"type": "integer", "minimum": 0},
                        },
                        "required": ["action"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {
                            "plan_id": {"type": "string", "minLength": 8, "maxLength": 256},
                            "confirmation_token": {"type": "string", "minLength": 8, "maxLength": 256},
                        },
                        "required": ["plan_id"],
                        "additionalProperties": False,
                    },
                ],
            },
            "plan_id": {"type": "string", "minLength": 8, "maxLength": 256},
            "idempotency_key": {"type": "string", "minLength": 8, "maxLength": 128},
        },
        "required": ["action", "request", "idempotency_key"],
        "additionalProperties": False,
    },
}
CONNECTIONS_RESEND_SCHEMA = {
    "name": "connections_agent_resend_mcp",
    "description": "Activate the restricted Resend MCP adapter after a recorded rotation.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}
CONNECTIONS_INSPECT_SCHEMA = {
    "name": "connections_agent_inspect",
    "description": "Inspect Frank's bounded private Connections projection before planning.",
    "parameters": {
        "type": "object",
        "properties": {"activity_limit": {"type": "integer", "minimum": 1, "maximum": 50}},
        "additionalProperties": False,
    },
}


class ConnectionsError(RuntimeError):
    """Safe, user-facing error whose text contains no remote response body."""

    def __init__(self, message: str, *, error_code: str | None = None, error_category: str | None = None):
        super().__init__(message)
        self.error_code = error_code
        self.error_category = error_category


class SetupNeeded(ConnectionsError):
    """The provider key has not completed the Frank rotation flow."""


@dataclass(frozen=True)
class ConnectionsSettings:
    enabled: bool
    frank_url: str
    infisical_url: str
    project_id: str
    environment: str
    secret_path: str
    resend_secret_name: str
    agent_key: str
    broker_key: str
    infisical_token: str
    infisical_client_id: str = ""
    infisical_client_secret: str = ""
    infisical_organization_slug: str = ""


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _setting(ctx: Any, key: str, default: Any) -> Any:
    try:
        return ctx.get_config(key, default)
    except Exception:
        return default


def _config_setting_without_context(key: str, default: Any) -> Any:
    """Read the canonical default-profile plugin settings for dashboard startup."""
    try:
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly() or {}
        plugins = config.get("plugins") if isinstance(config, Mapping) else None
        entries = plugins.get("entries") if isinstance(plugins, Mapping) else None
        entry = entries.get("connections-agent") if isinstance(entries, Mapping) else None
        settings = entry.get("settings") if isinstance(entry, Mapping) else None
        if isinstance(settings, Mapping) and key in settings:
            return settings[key]
    except Exception:
        pass
    return default


def _canonical_setting(ctx: Any, key: str, default: Any) -> Any:
    return _setting(ctx, key, default) if ctx is not None else _config_setting_without_context(key, default)


def load_settings(ctx: Any = None) -> ConnectionsSettings:
    """Read namespaced config.yaml settings; credentials remain env-only."""
    enabled = _canonical_setting(ctx, "enabled", False)
    frank_url = _canonical_setting(ctx, "frank_url", "")
    infisical_url = _canonical_setting(ctx, "infisical_url", "")
    project_id = _canonical_setting(ctx, "infisical_project_id", "")
    environment = _canonical_setting(ctx, "infisical_environment", "dev")
    path_value = _canonical_setting(ctx, "secret_path", "/connections")
    secret_name = str(_canonical_setting(ctx, "resend_secret_name", "RESEND_API_KEY") or "RESEND_API_KEY").strip()
    if not _SECRET_NAME_RE.fullmatch(secret_name):
        secret_name = "RESEND_API_KEY"
    path = str(path_value or "/connections").strip()
    if not path.startswith("/") or ".." in path:
        path = "/connections"
    return ConnectionsSettings(
        enabled=_truthy(enabled),
        frank_url=str(frank_url or "").strip().rstrip("/"),
        infisical_url=str(infisical_url or "").strip().rstrip("/"),
        project_id=str(project_id or "").strip(),
        environment=str(environment or "dev").strip(),
        secret_path=path,
        resend_secret_name=secret_name,
        agent_key=os.environ.get("HERMES_CONNECTIONS_AGENT_KEY", "").strip(),
        broker_key=os.environ.get("HERMES_VAULT_BROKER_KEY", "").strip(),
        infisical_token=os.environ.get("HERMES_CONNECTIONS_INFISICAL_TOKEN", "").strip(),
        infisical_client_id=os.environ.get("HERMES_CONNECTIONS_INFISICAL_CLIENT_ID", "").strip(),
        infisical_client_secret=os.environ.get("HERMES_CONNECTIONS_INFISICAL_CLIENT_SECRET", "").strip(),
        infisical_organization_slug=os.environ.get("HERMES_CONNECTIONS_INFISICAL_ORGANIZATION_SLUG", "").strip(),
    )


def _safe_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {key: value[key] for key in _SAFE_METADATA_FIELDS if key in value}


def _secret_name(value: Any) -> str:
    name = str(value or "").strip()
    if not _SECRET_NAME_RE.fullmatch(name):
        raise ConnectionsError("secret_name is invalid")
    return name


def _opaque_receipt(value: Any) -> dict[str, str] | None:
    """Keep only a token-shaped receipt id; never forward provider payloads."""
    if isinstance(value, str) and _OPAQUE_RECEIPT_RE.fullmatch(value.strip()):
        return {"receipt_id": value.strip()}
    if isinstance(value, Mapping):
        if set(value) != {"receipt_id"}:
            return None
        receipt_id = value.get("receipt_id") or value.get("id")
        if isinstance(receipt_id, str) and _OPAQUE_RECEIPT_RE.fullmatch(receipt_id.strip()):
            return {"receipt_id": receipt_id.strip()}
    return None


def _reject_sensitive_payload(value: Any, *, path: tuple[str, ...] = ()) -> None:
    """Reject nested credential-shaped keys and values before transport."""
    if isinstance(value, Mapping):
        for raw_key, nested in value.items():
            key = str(raw_key)
            lowered = key.lower()
            if _SENSITIVE_FIELD_RE.search(key):
                if not (not path and lowered == "confirmation_token") and lowered not in _SAFE_REFERENCE_FIELDS:
                    raise ConnectionsError("sensitive fields are not accepted by the agent action tool")
            _reject_sensitive_payload(nested, path=path + (lowered,))
        return
    if isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _reject_sensitive_payload(nested, path=path + (str(index),))
        return
    if isinstance(value, str) and _SECRET_LIKE_VALUE_RE.search(value):
        raise ConnectionsError("secret-like values are not accepted by the agent action tool")
    if isinstance(value, str) and _PAYMENT_LIKE_VALUE_RE.search(value):
        raise ConnectionsError("payment-like values are not accepted by the agent action tool")


def build_resend_mcp_config(secret_value: str) -> dict[str, Any]:
    """Build the pinned, capability-filtered stdio config in memory only."""
    if not isinstance(secret_value, str) or not secret_value:
        raise SetupNeeded("Resend secret is unavailable")
    return {
        "command": "npx",
        "args": ["-y", RESEND_MCP_PACKAGE],
        "env": {"RESEND_API_KEY": secret_value},
        "tools": {"include": list(RESEND_MCP_TOOL_NAMES)},
        "supports_parallel_tool_calls": False,
    }


def _resend_registered_tool_names(mcp_module: Any) -> list[str]:
    """Read only the attributable Resend server registration.

    ``register_mcp_servers`` returns the process-wide MCP registry, so its
    return value cannot establish Resend readiness. The private server record
    is the authoritative attribution seam; missing or changed seams fail
    closed instead of accepting unrelated tools.
    """
    servers = getattr(mcp_module, "_servers", None)
    server = servers.get(RESEND_SERVER_NAME) if isinstance(servers, Mapping) else None
    if server is None:
        return []
    ready = getattr(server, "_ready", None)
    task = getattr(server, "_task", None)
    if (
        getattr(server, "_error", None) is not None
        or getattr(server, "session", None) is None
        or ready is None
        or not callable(getattr(ready, "is_set", None))
        or not ready.is_set()
        or task is None
        or not callable(getattr(task, "done", None))
        or task.done()
    ):
        return []
    raw_names = {str(getattr(tool, "name", "")) for tool in getattr(server, "_tools", ())}
    registered_names = {str(name) for name in getattr(server, "_registered_tool_names", ())}
    expected_raw = set(RESEND_MCP_TOOL_NAMES)
    expected_registered = {
        f"mcp__{RESEND_SERVER_NAME}__send_email",
        f"mcp__{RESEND_SERVER_NAME}__get_email",
    }
    if not expected_raw.issubset(raw_names) or not expected_registered.issubset(registered_names):
        return []
    return list(RESEND_MCP_TOOL_NAMES)


def _safe_error_code(value: Any, fallback: str = "unknown_error") -> str:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in ERROR_CODES else fallback


def _safe_error_category(value: Any, fallback: str = "unknown") -> str:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in ERROR_CATEGORIES else fallback


def _infisical_http_failure(status: int) -> ConnectionsError:
    mapping = {
        401: ("infisical_auth_failed", "auth"),
        403: ("infisical_permission_denied", "permission_denied"),
        404: ("infisical_not_found", "not_found"),
        429: ("infisical_rate_limited", "rate_limited"),
    }
    code, category = mapping.get(status, ("infisical_unavailable", "unavailable"))
    return ConnectionsError("Infisical CE request failed", error_code=code, error_category=category)


def classify_failure(exc: BaseException) -> tuple[str, str]:
    """Map internal errors to the allowlisted, non-text failure contract."""
    explicit_code = getattr(exc, "error_code", None)
    explicit_category = getattr(exc, "error_category", None)
    if explicit_code or explicit_category:
        return _safe_error_code(explicit_code), _safe_error_category(explicit_category)
    text = str(exc).lower()
    if isinstance(exc, SetupNeeded) or "configured" in text or "setup_needed" in text:
        return "setup_needed", "configuration"
    if "idempotency" in text or "request rate" in text:
        return "rate_limited", "rate_limited"
    if "confirmation" in text:
        return "confirmation_required", "permission_denied"
    if "receipt" in text:
        return "receipt_missing", "validation"
    if "timeout" in text:
        return "timeout", "timeout"
    if "infisical" in text:
        return "infisical_unavailable", "unavailable"
    if "frank action" in text:
        return "provider_error", "unavailable"
    return "unknown_error", "unknown"


def failure_payload(exc: BaseException, *, provider: str = "infisical-ce", provider_receipt: Any = None) -> dict[str, Any]:
    """Build the only failure shape allowed across the integration boundary."""
    error_code, error_category = classify_failure(exc)
    receipt = _opaque_receipt(provider_receipt)
    if receipt is None:
        # This is a broker failure receipt, not a claim that the provider
        # completed the requested operation. It gives Frank a stable opaque
        # reference without copying an upstream response or error string.
        receipt = {"receipt_id": str(uuid.uuid4())}
    return {
        "schema": SCHEMA_VERSION,
        "outcome": "failed",
        "provider_receipt": receipt,
        "error_code": error_code,
        "error_category": error_category,
    }


def _safe_text(value: Any, label: str, *, max_length: int = 240, opaque: bool = False) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ConnectionsError(f"{label} must be text")
    text = " ".join(value.split()).strip()
    if not text or len(text) > max_length:
        raise ConnectionsError(f"{label} is invalid")
    if _SECRET_LIKE_VALUE_RE.search(text) or _PAYMENT_LIKE_VALUE_RE.search(text):
        raise ConnectionsError(f"{label} contains unsafe data")
    if opaque and not _OPAQUE_RECEIPT_RE.fullmatch(text):
        raise ConnectionsError(f"{label} is not opaque")
    return text


def _safe_ref(value: Any, label: str, *, vault: bool = False) -> str:
    text = _safe_text(value, label, max_length=300)
    if vault:
        if not _VAULT_REFERENCE_RE.fullmatch(text):
            raise ConnectionsError(f"{label} is not an opaque vault reference")
    elif not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,299}", text):
        raise ConnectionsError(f"{label} is not an opaque reference")
    return text


def _safe_target(value: Any) -> dict[str, str]:
    if value in (None, {}):
        return {}
    if not isinstance(value, Mapping) or set(value) - _SAFE_TARGET_FIELDS:
        raise ConnectionsError("target contains unsupported fields")
    result: dict[str, str] = {}
    for field in sorted(_SAFE_TARGET_FIELDS):
        if field not in value or value[field] in (None, ""):
            continue
        text = _safe_text(value[field], f"target.{field}", max_length=240)
        if field == "provider":
            text = text.lower()
            if text not in _SAFE_PROVIDERS:
                raise ConnectionsError("target.provider is invalid")
        elif field == "connection_id":
            if not _OPAQUE_RECEIPT_RE.fullmatch(text):
                raise ConnectionsError("target.connection_id is not opaque")
        elif not _SAFE_ID_RE.fullmatch(text.lower()):
            raise ConnectionsError(f"target.{field} is invalid")
        result[field] = text
    return result


def _safe_capabilities(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 24:
        raise ConnectionsError("body.capabilities must be a short list")
    result = []
    for capability in value:
        text = _safe_text(capability, "body.capability", max_length=80).lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._:-]{0,79}", text):
            raise ConnectionsError("body.capabilities contains an invalid value")
        result.append(text)
    return sorted(set(result))


def _safe_body(value: Any) -> dict[str, Any]:
    if value in (None, {}):
        return {}
    if not isinstance(value, Mapping) or set(value) - _SAFE_BODY_FIELDS:
        raise ConnectionsError("body contains unsupported fields")
    result: dict[str, Any] = {}
    for field, raw in value.items():
        if field == "capabilities":
            result[field] = _safe_capabilities(raw)
        elif raw is None:
            raise ConnectionsError(f"body.{field} must be text")
        elif raw == "":
            result[field] = ""
        elif field == "provider":
            provider = _safe_text(raw, "body.provider").lower()
            if provider not in _SAFE_PROVIDERS:
                raise ConnectionsError("body.provider is invalid")
            result[field] = provider
        elif field == "scope_kind":
            scope = _safe_text(raw, "body.scope_kind").lower()
            if scope not in _SAFE_SCOPES:
                raise ConnectionsError("body.scope_kind is invalid")
            result[field] = scope
        elif field == "status":
            status = _safe_text(raw, "body.status").lower()
            if status not in _SAFE_STATUSES:
                raise ConnectionsError("body.status is invalid")
            result[field] = status
        elif field == "connection_ref":
            result[field] = _safe_ref(raw, field)
        elif field == "credential_ref":
            result[field] = _safe_ref(raw, field, vault=True)
        elif field == "admin_url":
            text = _safe_text(raw, field, max_length=300)
            parsed = urllib.parse.urlparse(text)
            if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
                raise ConnectionsError("admin_url must be an https URL without credentials")
            result[field] = text
        elif field in {"scope_id"}:
            if raw in (None, ""):
                result[field] = ""
                continue
            text = _safe_text(raw, field).lower()
            if not _SAFE_ID_RE.fullmatch(text):
                raise ConnectionsError(f"body.{field} is invalid")
            result[field] = text
        else:
            result[field] = _safe_text(raw, f"body.{field}", max_length=600 if field == "notes" else 240)
    return result


def _safe_provider_error_code(value: Any) -> str:
    text = _safe_text(value, "provider_error_code", max_length=64).lower()
    if not _PROVIDER_ERROR_CODE_RE.fullmatch(text):
        raise ConnectionsError("provider_error_code is invalid")
    return text


def sanitize_action_request(body: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the explicit Frank plan/apply request shapes."""
    if not isinstance(body, Mapping):
        raise ConnectionsError("action request must be an object")
    _reject_sensitive_payload(body)
    keys = {str(key) for key in body}
    plan_keys = {"action", "target", "body", "expected_revision", "connection_id"}
    # Provider evidence is deliberately absent from the model-facing schema.
    # Only an executed Hermes adapter may create evidence for Frank apply.
    apply_keys = {"plan_id", "confirmation_token"}
    if "action" in keys:
        if keys - plan_keys:
            raise ConnectionsError("plan request fields are not allowlisted")
        action = str(body.get("action") or "").strip().lower()
        if action not in PLAN_ACTIONS:
            raise ConnectionsError("plan action is not allowlisted")
        target = _safe_target(body.get("target"))
        if body.get("connection_id") not in (None, ""):
            connection_id = _safe_text(body["connection_id"], "connection_id", opaque=True)
            target.setdefault("connection_id", connection_id)
        expected_revision = body.get("expected_revision")
        if expected_revision is not None and (isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0):
            raise ConnectionsError("expected_revision must be a non-negative integer")
        metadata = _safe_body(body.get("body"))
        if action in {"create", "update"} and not metadata:
            raise ConnectionsError(f"{action} plan requires safe metadata body")
        if action == "create" and not metadata.get("name"):
            raise ConnectionsError("create plan requires body.name")
        if action not in {"create", "update"} and metadata:
            raise ConnectionsError(f"{action} plan does not accept a metadata body")
        if action in {"update", "verify", "sync", "revoke", "delete"} and not target.get("connection_id"):
            raise ConnectionsError(f"{action} plan requires connection_id")
        result: dict[str, Any] = {"action": action}
        if target:
            result["target"] = target
        if metadata:
            result["body"] = metadata
        if expected_revision is not None:
            result["expected_revision"] = expected_revision
        return result
    if keys - apply_keys or "plan_id" not in keys:
        raise ConnectionsError("apply request fields are not allowlisted")
    result = {"plan_id": _safe_text(body["plan_id"], "plan_id", opaque=True)}
    for field in ("confirmation_token",):
        if field in body:
            result[field] = _safe_text(body[field], field, opaque=True)
    return result


def _safe_result_projection(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    allowed = {"connection_id", "provider", "status", "revision", "count", "matched", "changed", "removed", "verified_at", "action", "mode", "outcome", "pending", "reason", "provider_receipt", "local_metadata", "error_code", "error_category"}
    result: dict[str, Any] = {}
    for field in allowed:
        if field not in value:
            continue
        raw = value[field]
        if field == "provider_receipt":
            if raw:
                receipt = _opaque_receipt(raw)
                if receipt is None:
                    raise ConnectionsError("response provider receipt is invalid")
                result[field] = receipt
        elif field in {"error_code"}:
            result[field] = _safe_provider_error_code(raw)
        elif field in {"error_category"}:
            result[field] = _safe_error_category(raw)
        elif field == "connection_id" and raw is not None:
            result[field] = _safe_text(raw, field, opaque=True)
        elif field == "provider" and raw is not None:
            provider = _safe_text(raw, field).lower()
            if provider not in _SAFE_PROVIDERS:
                raise ConnectionsError("response provider is invalid")
            result[field] = provider
        elif field in {"status", "action", "mode", "outcome", "reason", "verified_at"} and raw is not None:
            result[field] = _safe_text(raw, field, max_length=240)
        elif field in {"revision", "count"} and isinstance(raw, int) and not isinstance(raw, bool):
            result[field] = raw
        elif field in {"matched", "changed", "removed", "pending", "local_metadata"} and isinstance(raw, bool):
            result[field] = raw
    return result


def _safe_action_projection(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for field in ("schema", "sequence", "receipt_id", "correlation_id", "source", "actor", "action", "state", "started_at", "completed_at"):
        if field not in value:
            continue
        raw = value[field]
        if field in {"sequence", "started_at", "completed_at"} and isinstance(raw, int):
            result[field] = raw
        elif raw is not None:
            result[field] = _safe_text(raw, field, opaque=field in {"receipt_id", "correlation_id"})
    if "target" in value:
        result["target"] = _safe_target(value.get("target"))
    if "progress" in value and isinstance(value["progress"], Mapping):
        progress = value["progress"]
        safe_progress: dict[str, Any] = {}
        if isinstance(progress.get("percent"), int):
            safe_progress["percent"] = progress["percent"]
        if progress.get("step") is not None:
            safe_progress["step"] = _safe_text(progress["step"], "progress.step")
        result["progress"] = safe_progress
    if "result" in value:
        result["result"] = _safe_result_projection(value.get("result"))
    return result


def _safe_connection_projection(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    allowed = _SAFE_BODY_FIELDS | {"id", "created_at", "updated_at", "revision"}
    candidate = {field: value[field] for field in allowed if field in value}
    try:
        result = _safe_body({field: value[field] for field in candidate if field in _SAFE_BODY_FIELDS})
    except ConnectionsError:
        return None
    for field in ("id", "created_at", "updated_at", "revision"):
        if field == "id" and candidate.get(field) is not None:
            result[field] = _safe_text(candidate[field], "connection.id", opaque=True)
        elif field in candidate and isinstance(candidate[field], int) and not isinstance(candidate[field], bool):
            result[field] = candidate[field]
    return result


def sanitize_action_response(payload: Mapping[str, Any], *, action: str) -> dict[str, Any]:
    """Project Frank's nested plan/action/connection envelope to safe metadata."""
    if not isinstance(payload, Mapping):
        return failure_payload(ConnectionsError("Frank returned an invalid response"), provider="frank")
    result: dict[str, Any] = {}
    if isinstance(payload.get("plan"), Mapping):
        plan = payload["plan"]
        safe_plan: dict[str, Any] = {}
        for field in ("plan_id", "action", "source", "actor", "state", "expected_revision", "confirmation_required", "confirmation_token", "confirmation_consumed", "created_at", "expires_at"):
            if field not in plan:
                continue
            raw = plan[field]
            if field == "expected_revision" and isinstance(raw, int) and not isinstance(raw, bool):
                safe_plan[field] = raw
            elif field in {"confirmation_required", "confirmation_consumed"} and isinstance(raw, bool):
                safe_plan[field] = raw
            elif raw is not None:
                safe_plan[field] = _safe_text(raw, f"plan.{field}", opaque=field in {"plan_id", "confirmation_token"})
        if isinstance(plan.get("target"), Mapping):
            safe_plan["target"] = _safe_target(plan["target"])
        result["plan"] = safe_plan
    if isinstance(payload.get("action"), Mapping):
        result["action"] = _safe_action_projection(payload["action"])
    connection = _safe_connection_projection(payload.get("connection"))
    if connection is not None:
        result["connection"] = connection
    for field in ("replayed", "pending", "provider_failed"):
        if isinstance(payload.get(field), bool):
            result[field] = payload[field]
    if action == "plan" and not result.get("plan"):
        return failure_payload(ConnectionsError("Frank returned no plan"), provider="frank")
    if action == "apply" and not result.get("action"):
        return failure_payload(ConnectionsError("Frank returned no action"), provider="frank")
    action_result = result.get("action", {}).get("result") if isinstance(result.get("action"), Mapping) else None
    if action == "apply" and isinstance(action_result, Mapping) and action_result.get("outcome") in SUCCESS_OUTCOMES:
        # The model-facing transport cannot submit provider evidence. Until a
        # Hermes adapter executes and binds evidence server-side, a provider
        # success echoed by Frank is not trusted or projected as verified.
        return failure_payload(ConnectionsError(
            "provider evidence is unavailable",
            error_code="provider_evidence_required",
            error_category="unavailable",
        ), provider="frank")
    return result


def _safe_inspect_connection(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping) or set(value) - _INSPECT_CONNECTION_FIELDS:
        return None
    result: dict[str, Any] = {}
    for field in sorted(value):
        raw = value[field]
        if field in {"id", "connection_id", "receipt_id"} and raw is not None:
            result[field] = _safe_text(raw, field, opaque=True)
        elif field == "provider" and raw is not None:
            provider = _safe_text(raw, field).lower()
            if provider not in _SAFE_PROVIDERS:
                return None
            result[field] = provider
        elif field in {"name", "scope_id", "scope_kind", "status", "last_verified_at"} and raw is not None:
            result[field] = _safe_text(raw, field)
        elif field in {"state", "action", "outcome", "error_code", "error_category", "severity", "kind", "message_code", "created_at", "updated_at", "started_at", "completed_at"} and raw is not None:
            if field == "error_code":
                result[field] = _safe_provider_error_code(raw)
            elif field == "error_category":
                result[field] = _safe_error_category(raw)
            else:
                result[field] = _safe_text(raw, field)
        elif field in {"sequence", "revision"} and isinstance(raw, int) and not isinstance(raw, bool):
            result[field] = raw
        elif field in {"pending", "resolved", "attention"} and isinstance(raw, bool):
            result[field] = raw
        elif field == "capabilities":
            result[field] = _safe_capabilities(raw)
        elif field == "connection_ref" and raw is not None:
            result[field] = _safe_ref(raw, field)
        elif field == "credential_ref" and raw is not None:
            result[field] = _safe_ref(raw, field, vault=True)
    return result


def _safe_inspect_action(value: Any) -> dict[str, Any] | None:
    if (
        not isinstance(value, Mapping)
        or set(value) - _INSPECT_ACTION_FIELDS
        or not {"action", "state"}.issubset(value)
    ):
        return None
    result: dict[str, Any] = {}
    for field in _INSPECT_ACTION_FIELDS:
        if field not in value:
            continue
        if field == "target":
            target = _safe_target(value[field])
            if not target:
                return None
            result[field] = target
        elif field == "result":
            raw = value[field]
            if not isinstance(raw, Mapping) or set(raw) - _INSPECT_RESULT_FIELDS:
                return None
            result[field] = _safe_result_projection(raw)
        elif field == "progress":
            raw = value[field]
            if not isinstance(raw, Mapping) or set(raw) - {"percent", "step"}:
                return None
            progress: dict[str, Any] = {}
            if "percent" in raw:
                if not isinstance(raw["percent"], int) or isinstance(raw["percent"], bool) or not 0 <= raw["percent"] <= 100:
                    return None
                progress["percent"] = raw["percent"]
            if "step" in raw:
                progress["step"] = _safe_text(raw["step"], "progress.step")
            result[field] = progress
        elif field == "sequence":
            if not isinstance(value[field], int) or isinstance(value[field], bool):
                return None
            result[field] = value[field]
        elif value[field] is not None:
            result[field] = _safe_text(value[field], field, opaque=field in {"receipt_id", "correlation_id"})
    return result


def sanitize_inspect_response(payload: Mapping[str, Any], *, activity_limit: int) -> dict[str, Any]:
    """Project only the dedicated Frank private inspect envelope."""
    if not isinstance(payload, Mapping) or set(payload) != _INSPECT_TOP_FIELDS:
        return failure_payload(ConnectionsError("Frank inspect returned an invalid response"), provider="frank")
    result: dict[str, Any] = {}
    if payload.get("schema") != "schema://frank.connections-agent-inspect/v1":
        return failure_payload(ConnectionsError("Frank inspect returned an invalid schema"), provider="frank")
    result["schema"] = payload["schema"]
    connections = payload.get("connections")
    if not isinstance(connections, list):
        return failure_payload(ConnectionsError("Frank inspect returned invalid connections"), provider="frank")
    safe_connections = [_safe_inspect_connection(raw) for raw in connections]
    if any(item is None for item in safe_connections):
        return failure_payload(ConnectionsError("Frank inspect returned unsafe connection metadata"), provider="frank")
    result["connections"] = safe_connections
    attention = payload.get("attention")
    if not isinstance(attention, list):
        return failure_payload(ConnectionsError("Frank inspect returned invalid attention"), provider="frank")
    safe_attention = [_safe_inspect_action(raw) for raw in attention]
    if any(item is None for item in safe_attention):
        return failure_payload(ConnectionsError("Frank inspect returned unsafe attention metadata"), provider="frank")
    result["attention"] = safe_attention
    activity = payload.get("activity")
    if not isinstance(activity, list) or len(activity) > activity_limit:
        return failure_payload(ConnectionsError("Frank inspect returned invalid activity"), provider="frank")
    safe_activity = [_safe_inspect_action(raw) for raw in activity]
    if any(item is None for item in safe_activity):
        return failure_payload(ConnectionsError("Frank inspect returned unsafe activity metadata"), provider="frank")
    result["activity"] = safe_activity
    return result


class InfisicalClient:
    """Fixed-scope Infisical CE v4 client; never exposes its response body."""

    def __init__(self, settings: ConnectionsSettings):
        universal_auth = bool(settings.infisical_client_id and settings.infisical_client_secret)
        static_auth = bool(settings.infisical_token)
        if not settings.infisical_url or not settings.project_id or not (universal_auth or static_auth):
            raise SetupNeeded("Infisical CE is not configured on Hermes")
        self.settings = settings
        self._universal_auth = universal_auth
        self._access_token = settings.infisical_token if not universal_auth else ""
        self._token_expires_at = 0.0
        self._token_lock = threading.Lock()

    def _url(self, secret_name: str | None = None, *, include_value: bool = False) -> str:
        base = self.settings.infisical_url + "/api/v4/secrets"
        if secret_name is not None:
            base += "/" + urllib.parse.quote(secret_name, safe="")
        query = {
            "projectId": self.settings.project_id,
            "environment": self.settings.environment,
            "secretPath": self.settings.secret_path,
            "type": "shared",
        }
        if secret_name is None:
            query.update({"viewSecretValue": "false", "expandSecretReferences": "false", "recursive": "false"})
        else:
            query.update({"viewSecretValue": "true" if include_value else "false", "expandSecretReferences": "false"})
        return base + "?" + urllib.parse.urlencode(query)

    def _request(self, method: str, url: str, body: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return self._request_with_refresh(method, url, body, allow_refresh=True)

    def _authenticate(self) -> str:
        payload = {
            "clientId": self.settings.infisical_client_id,
            "clientSecret": self.settings.infisical_client_secret,
        }
        if self.settings.infisical_organization_slug:
            payload["organizationSlug"] = self.settings.infisical_organization_slug
        request = urllib.request.Request(
            self.settings.infisical_url + "/api/v1/auth/universal-auth/login",
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            method="POST",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        try:
            with _open_no_redirect(request) as response:
                result = json.loads(response.read(_MAX_REQUEST_BYTES).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise _infisical_http_failure(exc.code) from exc
        except TimeoutError as exc:
            raise ConnectionsError("Infisical universal auth failed", error_code="timeout", error_category="timeout") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ConnectionsError("Infisical universal auth failed", error_code="infisical_unavailable", error_category="unavailable") from exc
        except (ValueError, ConnectionsError) as exc:
            if isinstance(exc, ConnectionsError):
                raise
            raise ConnectionsError("Infisical universal auth returned invalid metadata", error_code="invalid_completion", error_category="validation") from exc
        token = result.get("accessToken") if isinstance(result, Mapping) else None
        expires_in = result.get("expiresIn") if isinstance(result, Mapping) else None
        if not isinstance(token, str) or not token or not isinstance(expires_in, (int, float)):
            raise ConnectionsError("Infisical universal auth returned invalid metadata")
        with self._token_lock:
            self._access_token = token
            self._token_expires_at = time.monotonic() + max(1.0, float(expires_in) - 30.0)
        return token

    def _token(self) -> str:
        if not self._universal_auth:
            return self._access_token
        with self._token_lock:
            if self._access_token and time.monotonic() < self._token_expires_at:
                return self._access_token
        return self._authenticate()

    def _invalidate_token(self) -> None:
        with self._token_lock:
            self._access_token = ""
            self._token_expires_at = 0.0

    def _request_with_refresh(self, method: str, url: str, body: Optional[dict[str, Any]], *, allow_refresh: bool) -> dict[str, Any]:
        raw = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=raw,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if raw is not None else {}),
            },
        )
        try:
            with _open_no_redirect(request) as response:
                payload = json.loads(response.read(_MAX_REQUEST_BYTES).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and self._universal_auth and allow_refresh:
                self._invalidate_token()
                return self._request_with_refresh(method, url, body, allow_refresh=False)
            raise _infisical_http_failure(exc.code) from exc
        except TimeoutError as exc:
            raise ConnectionsError("Infisical CE request timed out", error_code="timeout", error_category="timeout") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ConnectionsError("Infisical CE request unavailable", error_code="infisical_unavailable", error_category="unavailable") from exc
        except ValueError as exc:
            raise ConnectionsError("Infisical CE returned invalid metadata", error_code="invalid_completion", error_category="validation") from exc
        return payload if isinstance(payload, dict) else {}

    def list_metadata(self) -> list[dict[str, Any]]:
        payload = self._request("GET", self._url())
        entries = payload.get("secrets")
        if not isinstance(entries, list):
            raise ConnectionsError("Infisical CE returned invalid metadata")
        return [_safe_metadata(item) for item in entries if isinstance(item, Mapping)]

    def read_value(self, secret_name: str) -> str:
        payload = self._request("GET", self._url(secret_name, include_value=True))
        secret = payload.get("secret")
        value = secret.get("secretValue") if isinstance(secret, Mapping) else None
        if not isinstance(value, str) or not value or len(value.encode("utf-8")) > _MAX_SECRET_VALUE_BYTES:
            raise SetupNeeded("Resend secret is unavailable")
        return value

    def mutate(self, operation: str, secret_name: str, secret_value: str) -> dict[str, Any]:
        if len(secret_value.encode("utf-8")) > _MAX_SECRET_VALUE_BYTES:
            raise ConnectionsError("secret value exceeds the broker limit")
        body = {
            "projectId": self.settings.project_id,
            "environment": self.settings.environment,
            "secretPath": self.settings.secret_path,
            "type": "shared",
            "secretValue": secret_value,
        }
        method = "POST" if operation == "create" else "PATCH"
        payload = self._request(method, self._url(secret_name), body)
        return self._safe_secret_metadata(payload)

    def delete(self, secret_name: str) -> dict[str, Any]:
        payload = self._request("DELETE", self._url(secret_name), {
            "projectId": self.settings.project_id,
            "environment": self.settings.environment,
            "secretPath": self.settings.secret_path,
            "type": "shared",
        })
        return self._safe_secret_metadata(payload)

    @staticmethod
    def _safe_secret_metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
        secret = payload.get("secret") if isinstance(payload, Mapping) else None
        if not isinstance(secret, Mapping):
            raise ConnectionsError("Infisical CE returned invalid secret metadata")
        safe = _safe_metadata(secret)
        if not safe:
            raise ConnectionsError("Infisical CE returned empty secret metadata")
        return safe


class _MutationLedger:
    """Durable, profile-scoped idempotency ledger for vault mutations.

    Only cryptographic digests, state, timestamps, and already-sanitized
    completion responses cross the persistence boundary. Client keys and
    mutation request bodies (including secret values) are never stored.
    """

    _SCHEMA = "hermes.connections.mutation-idempotency.v1"
    _DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._path_override = path

    def _path(self) -> Path:
        return self._path_override or (
            get_hermes_home()
            / "plugin-data"
            / "connections-agent"
            / "mutation-idempotency.json"
        )

    @staticmethod
    def _key_digest(principal: str, route: str, key: str) -> str:
        material = json.dumps(
            [principal, route, key],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    @staticmethod
    def _request_fingerprint(body: Mapping[str, Any]) -> str:
        material = json.dumps(
            body,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    def _empty(self) -> dict[str, Any]:
        return {"schema": self._SCHEMA, "entries": {}}

    def _load(self) -> dict[str, Any]:
        path = self._path()
        try:
            raw_payload = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return self._empty()
        except OSError as exc:
            raise ConnectionsError(
                "Connections idempotency store is unavailable",
                error_code="idempotency_store_unavailable",
                error_category="unavailable",
            ) from exc
        try:
            payload = json.loads(raw_payload)
        except (ValueError, TypeError) as exc:
            raise ConnectionsError(
                "Connections idempotency store is unavailable",
                error_code="idempotency_store_unavailable",
                error_category="unavailable",
            ) from exc
        if (
            not isinstance(payload, Mapping)
            or set(payload) != {"schema", "entries"}
            or payload.get("schema") != self._SCHEMA
            or not isinstance(payload.get("entries"), Mapping)
        ):
            raise ConnectionsError(
                "Connections idempotency store is unavailable",
                error_code="idempotency_store_unavailable",
                error_category="unavailable",
            )
        entries: dict[str, Any] = {}
        for digest, raw in payload["entries"].items():
            if not isinstance(digest, str) or not self._DIGEST_RE.fullmatch(digest):
                raise ConnectionsError(
                    "Connections idempotency store is unavailable",
                    error_code="idempotency_store_unavailable",
                    error_category="unavailable",
                )
            if not isinstance(raw, Mapping):
                raise ConnectionsError(
                    "Connections idempotency store is unavailable",
                    error_code="idempotency_store_unavailable",
                    error_category="unavailable",
                )
            state = raw.get("state")
            fingerprint = raw.get("request_fingerprint")
            expected_fields = {
                "request_fingerprint",
                "state",
                "created_at",
                "updated_at",
            } | ({"result"} if state == "completed" else set())
            if (
                set(raw) != expected_fields
                or state not in {"in_progress", "uncertain", "completed"}
                or not isinstance(fingerprint, str)
                or not self._DIGEST_RE.fullmatch(fingerprint)
                or isinstance(raw.get("created_at"), bool)
                or not isinstance(raw.get("created_at"), int)
                or isinstance(raw.get("updated_at"), bool)
                or not isinstance(raw.get("updated_at"), int)
            ):
                raise ConnectionsError(
                    "Connections idempotency store is unavailable",
                    error_code="idempotency_store_unavailable",
                    error_category="unavailable",
                )
            result = raw.get("result")
            if state == "completed":
                if not isinstance(result, Mapping):
                    raise ConnectionsError(
                        "Connections idempotency store is unavailable",
                        error_code="idempotency_store_unavailable",
                        error_category="unavailable",
                    )
            elif "result" in raw:
                raise ConnectionsError(
                    "Connections idempotency store is unavailable",
                    error_code="idempotency_store_unavailable",
                    error_category="unavailable",
                )
            entries[digest] = dict(raw)
        return {"schema": self._SCHEMA, "entries": entries}

    def _save(self, payload: Mapping[str, Any]) -> None:
        try:
            atomic_json_write(
                self._path(),
                dict(payload),
                indent=None,
                mode=0o600,
                sort_keys=True,
                separators=(",", ":"),
            )
        except Exception as exc:
            raise ConnectionsError(
                "Connections idempotency store is unavailable",
                error_code="idempotency_store_unavailable",
                error_category="unavailable",
            ) from exc

    def begin(self, principal: str, route: str, key: str, body: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        if not _IDEMPOTENCY_RE.fullmatch(key):
            raise ConnectionsError(
                "Idempotency-Key is required and invalid",
                error_code="invalid_request",
                error_category="validation",
            )
        fingerprint = self._request_fingerprint(body)
        entry_key = self._key_digest(principal, route, key)
        with self._lock:
            payload = self._load()
            entries = payload["entries"]
            existing = entries.get(entry_key)
            if existing:
                if existing["request_fingerprint"] != fingerprint:
                    raise ConnectionsError(
                        "Idempotency-Key was reused with a different request",
                        error_code="idempotency_conflict",
                        error_category="validation",
                    )
                if existing["state"] != "completed":
                    raise ConnectionsError(
                        "Idempotency-Key outcome is uncertain; manual reconciliation is required",
                        error_code="idempotency_uncertain",
                        error_category="unavailable",
                    )
                return dict(existing["result"])
            now = int(time.time())
            entries[entry_key] = {
                "request_fingerprint": fingerprint,
                "state": "in_progress",
                "created_at": now,
                "updated_at": now,
            }
            self._save(payload)
        return None

    def mark_uncertain(self, principal: str, route: str, key: str, body: Mapping[str, Any]) -> None:
        fingerprint = self._request_fingerprint(body)
        entry_key = self._key_digest(principal, route, key)
        with self._lock:
            payload = self._load()
            existing = payload["entries"].get(entry_key)
            if not existing or existing["request_fingerprint"] != fingerprint:
                raise ConnectionsError(
                    "Connections idempotency store is unavailable",
                    error_code="idempotency_store_unavailable",
                    error_category="unavailable",
                )
            if existing["state"] == "completed":
                return
            existing["state"] = "uncertain"
            existing["updated_at"] = int(time.time())
            existing.pop("result", None)
            self._save(payload)

    def finish(self, principal: str, route: str, key: str, body: Mapping[str, Any], result: dict[str, Any]) -> None:
        fingerprint = self._request_fingerprint(body)
        entry_key = self._key_digest(principal, route, key)
        with self._lock:
            payload = self._load()
            existing = payload["entries"].get(entry_key)
            if not existing or existing["request_fingerprint"] != fingerprint:
                raise ConnectionsError(
                    "Connections idempotency store is unavailable",
                    error_code="idempotency_store_unavailable",
                    error_category="unavailable",
                )
            existing["state"] = "completed"
            existing["updated_at"] = int(time.time())
            existing["result"] = dict(result)
            self._save(payload)


class _MutationRateLimiter:
    """Small process-local request-rate guard, independent of replay state."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}

    def check(self, principal: str) -> None:
        now = time.monotonic()
        with self._lock:
            hits = [item for item in self._hits.get(principal, []) if item > now - 60]
            if len(hits) >= 30:
                raise ConnectionsError(
                    "request rate limit exceeded",
                    error_code="rate_limited",
                    error_category="rate_limited",
                )
            self._hits[principal] = hits + [now]


_LEDGER = _MutationLedger()
_RATE_LIMITER = _MutationRateLimiter()


def _state_path() -> Path:
    return get_hermes_home() / "plugin-data" / "connections-agent" / "resend-state.json"


def _resend_provider_state(settings: ConnectionsSettings) -> str:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        if data.get("secret_name") != settings.resend_secret_name:
            return "setup_needed"
        state = data.get("state")
        if state in {"configured", "connected-awaiting-verification", "verified"}:
            return state
        if data.get("operation") == "rotate":
            return "configured"
    except (OSError, ValueError, TypeError):
        pass
    return "setup_needed"


def _resend_was_rotated(settings: ConnectionsSettings) -> bool:
    return _resend_provider_state(settings) != "setup_needed"


def _write_resend_state(settings: ConnectionsSettings, *, state: str, operation: str, metadata: Mapping[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    safe = {
        "schema": SCHEMA_VERSION,
        "secret_name": settings.resend_secret_name,
        "operation": operation,
        "state": state,
        "updated_at": int(time.time()),
        "provider": "resend-mcp",
        "metadata": dict(metadata),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(safe, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _record_resend_rotation(settings: ConnectionsSettings, metadata: Mapping[str, Any]) -> None:
    _write_resend_state(settings, state="configured", operation="rotate", metadata=_safe_metadata(metadata))


def _record_resend_create(settings: ConnectionsSettings, metadata: Mapping[str, Any]) -> None:
    _write_resend_state(settings, state="configured", operation="create", metadata=_safe_metadata(metadata))


def _record_resend_deleted(settings: ConnectionsSettings, metadata: Mapping[str, Any]) -> None:
    _write_resend_state(settings, state="setup_needed", operation="delete", metadata=_safe_metadata(metadata))


def _record_resend_connection(settings: ConnectionsSettings, tool_names: list[str]) -> None:
    _write_resend_state(settings, state="connected-awaiting-verification", operation="mcp-connect", metadata={"tool_count": len(tool_names)})


def _record_resend_verification(settings: ConnectionsSettings, result: Mapping[str, Any]) -> None:
    receipt = _opaque_receipt(result.get("provider_receipt"))
    if receipt is None:
        raise ConnectionsError("provider verification requires an opaque receipt")
    _write_resend_state(settings, state="verified", operation="provider-verify", metadata=receipt)


class ConnectionsTokenProvider(DashboardAuthProvider):
    name = "connections-vault-broker"
    display_name = "Connections Vault Broker"
    supports_token = True
    supports_session = False

    def __init__(self, *, secret: str):
        if len(secret) < 32:
            raise ValueError("HERMES_VAULT_BROKER_KEY must be at least 32 characters")
        self._secret = secret

    def verify_token(self, *, token: str) -> Optional[TokenPrincipal]:
        if token and hmac.compare_digest(token.encode(), self._secret.encode()):
            return TokenPrincipal("frank-vault-broker", self.name, ("vault.metadata", "vault.mutate"))
        return None

    def start_login(self, *, redirect_uri: str) -> LoginStart:
        raise NotImplementedError

    def complete_login(self, *, code: str, state: str, code_verifier: str, redirect_uri: str) -> Session:
        raise NotImplementedError

    def verify_session(self, *, access_token: str) -> Optional[Session]:
        return None

    def refresh_session(self, *, refresh_token: str) -> Session:
        raise NotImplementedError

    def revoke_session(self, *, refresh_token: str) -> None:
        return None


class ConnectionsRuntime:
    def __init__(self, settings: ConnectionsSettings):
        self.settings = settings
        self._plan_idempotency_keys: dict[str, str] = {}
        self._resend_process_tool_names: tuple[str, ...] = ()
        self._resend_restore_error = False

    def _activate_resend_mcp(self) -> list[str]:
        """Restore or activate Resend and prove its tools in this process."""
        if not _resend_was_rotated(self.settings):
            raise SetupNeeded("Resend remains setup_needed until Frank records a new rotation")
        from tools import mcp_tool

        names = _resend_registered_tool_names(mcp_tool)
        if set(names) != set(RESEND_MCP_TOOL_NAMES):
            client = InfisicalClient(self.settings)
            # The value is used only as an in-memory child-process environment
            # value. It is absent from state, responses, and logs.
            secret_value = client.read_value(self.settings.resend_secret_name)
            mcp_tool.register_mcp_servers({RESEND_SERVER_NAME: build_resend_mcp_config(secret_value)})
            names = _resend_registered_tool_names(mcp_tool)
        if set(names) != set(RESEND_MCP_TOOL_NAMES):
            raise SetupNeeded("Resend MCP discovery did not prove the pinned Resend server")
        self._resend_process_tool_names = tuple(RESEND_MCP_TOOL_NAMES)
        self._resend_restore_error = False
        _record_resend_connection(self.settings, list(names))
        return list(names)

    def status(self) -> dict[str, Any]:
        persisted_state = _resend_provider_state(self.settings)
        try:
            from tools import mcp_tool
            process_tools = _resend_registered_tool_names(mcp_tool)
        except Exception:
            process_tools = []
        if set(process_tools) == set(RESEND_MCP_TOOL_NAMES):
            self._resend_process_tool_names = tuple(RESEND_MCP_TOOL_NAMES)
        elif persisted_state in {"connected-awaiting-verification", "verified"}:
            try:
                self._activate_resend_mcp()
            except ConnectionsError:
                self._resend_restore_error = True
        provider_state = persisted_state
        if persisted_state in {"connected-awaiting-verification", "verified"}:
            provider_state = "connected-awaiting-verification" if self._resend_process_tool_names else "error"
        configured = bool(self.settings.infisical_url and self.settings.project_id and (self.settings.infisical_token or (self.settings.infisical_client_id and self.settings.infisical_client_secret)))
        return {
            "schema": SCHEMA_VERSION,
            "profile": PROFILE_NAME,
            "session_name": SESSION_NAME,
            "plugin": "connections-agent",
            "enabled": self.settings.enabled,
            "infisical": {"configured": configured, "reachable": None, "verified": None, "mode": "ce-v4-fixed-scope"},
            "providers": [{"id": "resend-mcp", "state": provider_state, "capabilities": list(RESEND_CAPABILITIES)}],
            "mcp": {"server": RESEND_SERVER_NAME, "command": "npx", "args": ["-y", RESEND_MCP_PACKAGE], "secret_source": "hermes-infisical", "secret_name": self.settings.resend_secret_name, "state": provider_state} if provider_state not in {"setup_needed", "error"} else {"server": RESEND_SERVER_NAME, "state": provider_state},
        }

    def status_tool(self, _args: Mapping[str, Any]) -> str:
        return tool_result(self.status())

    def resend_mcp_tool(self, _args: Mapping[str, Any]) -> str:
        try:
            names = self._activate_resend_mcp()
            return tool_result({
                "schema": SCHEMA_VERSION,
                "server": RESEND_SERVER_NAME,
                "state": "connected-awaiting-verification",
                "registered_tools": list(names),
                "capabilities": list(RESEND_CAPABILITIES),
            })
        except ConnectionsError as exc:
            return tool_error(str(exc))

    def inspect_tool(self, args: Mapping[str, Any]) -> str:
        try:
            if not isinstance(args, Mapping) or set(args) - {"activity_limit"}:
                raise ConnectionsError("inspect accepts only activity_limit")
            raw_limit = args.get("activity_limit", 20)
            if isinstance(raw_limit, bool) or not isinstance(raw_limit, int) or not 1 <= raw_limit <= 50:
                raise ConnectionsError("activity_limit must be between 1 and 50")
            if not self.settings.frank_url or not self.settings.agent_key:
                raise SetupNeeded("Frank inspect transport is not configured on Hermes")
            return tool_result(self._frank_inspect_request(raw_limit))
        except ConnectionsError as exc:
            return tool_error(str(exc))

    def request_tool(self, args: Mapping[str, Any]) -> str:
        try:
            action = str(args.get("action") or "").strip().lower()
            if action not in {"plan", "apply"} or not isinstance(args.get("request"), Mapping):
                raise ConnectionsError("action and request are required")
            if not self.settings.frank_url or not self.settings.agent_key:
                raise SetupNeeded("Frank action transport is not configured on Hermes")
            key = str(args.get("idempotency_key") or "").strip()
            body = sanitize_action_request(dict(args["request"]))
            plan_id = str(args.get("plan_id") or body.get("plan_id") or "").strip()
            if action == "apply":
                if not _OPAQUE_RECEIPT_RE.fullmatch(plan_id):
                    raise ConnectionsError("apply requires an opaque plan_id")
                if self._plan_idempotency_keys.get(plan_id) == key:
                    raise ConnectionsError("apply requires a new idempotency_key")
                body["plan_id"] = plan_id
            if any(
                "secret" in str(k).lower()
                or ("token" in str(k).lower() and str(k).lower() != "confirmation_token")
                for k in body
            ):
                raise ConnectionsError("secret or token fields are not accepted by the agent action tool")
            endpoint = f"{self.settings.frank_url}/api/connections/agent/{action}"
            result = self._frank_request(endpoint, body, key)
            returned_plan_id = str((result.get("plan") or {}).get("plan_id") or plan_id).strip() if isinstance(result.get("plan"), Mapping) else plan_id
            if action == "plan" and _OPAQUE_RECEIPT_RE.fullmatch(returned_plan_id):
                self._plan_idempotency_keys[returned_plan_id] = key
            action_result = ((result.get("action") or {}).get("result") if isinstance(result.get("action"), Mapping) else None)
            if action == "apply" and isinstance(action_result, Mapping) and action_result.get("outcome") in SUCCESS_OUTCOMES:
                raise ConnectionsError("provider evidence is unavailable", error_code="provider_evidence_required", error_category="unavailable")
            return tool_result(result)
        except ConnectionsError as exc:
            return tool_error(str(exc))

    def _frank_request(self, endpoint: str, body: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        if not _IDEMPOTENCY_RE.fullmatch(idempotency_key):
            raise ConnectionsError("idempotency_key is invalid")
        encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _MAX_REQUEST_BYTES:
            raise ConnectionsError("agent request is too large")
        request = urllib.request.Request(endpoint, data=encoded, method="POST", headers={
            "Authorization": f"Bearer {self.settings.agent_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Hermes-Profile": PROFILE_NAME,
            "Idempotency-Key": idempotency_key,
        })
        try:
            with _open_no_redirect(request) as response:
                payload = json.loads(response.read(_MAX_REQUEST_BYTES).decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            raise ConnectionsError("Frank action request failed") from exc
        if not isinstance(payload, dict):
            raise ConnectionsError("Frank returned an invalid response")
        return sanitize_action_response(payload, action=endpoint.rsplit("/", 1)[-1])

    def _frank_inspect_request(self, activity_limit: int) -> dict[str, Any]:
        if not 1 <= activity_limit <= 50:
            raise ConnectionsError("activity_limit is invalid")
        endpoint = f"{self.settings.frank_url}/api/connections/agent/inspect?activity_limit={activity_limit}"
        request = urllib.request.Request(endpoint, method="GET", headers={
            "Authorization": f"Bearer {self.settings.agent_key}",
            "Accept": "application/json",
            "X-Hermes-Profile": PROFILE_NAME,
        })
        try:
            with _open_no_redirect(request) as response:
                payload = json.loads(response.read(_MAX_REQUEST_BYTES).decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            raise ConnectionsError("Frank inspect request failed") from exc
        if not isinstance(payload, dict):
            raise ConnectionsError("Frank inspect returned an invalid response")
        return sanitize_inspect_response(payload, activity_limit=activity_limit)

    def broker_health(self) -> dict[str, Any]:
        configured = bool(self.settings.infisical_url and self.settings.project_id and (self.settings.infisical_token or (self.settings.infisical_client_id and self.settings.infisical_client_secret)))
        base = {"schema": SCHEMA_VERSION, "profile": PROFILE_NAME, "broker": "connections-agent", "secret_values": False, "infisical": {"configured": configured, "reachable": False, "verified": False}}
        if not configured:
            return base | {"ok": False, "status": "setup_needed", "state": "setup_needed"}
        try:
            InfisicalClient(self.settings).list_metadata()
        except ConnectionsError as exc:
            safe = failure_payload(exc)
            category = safe["error_category"]
            status = "permission_denied" if category == "permission_denied" else "error" if category in {"auth", "validation", "configuration"} else "unavailable"
            return base | {"ok": False, "status": status, "state": "error", "outcome": "failed", "error_code": safe["error_code"], "error_category": category}
        return base | {"ok": True, "status": "verified", "state": "verified", "outcome": "verified", "infisical": {"configured": True, "reachable": True, "verified": True}}

    def broker_list_metadata(self, *, principal: str) -> dict[str, Any]:
        client = InfisicalClient(self.settings)
        return {"schema": SCHEMA_VERSION, "operation": "list-metadata", "outcome": "verified", "receipt": {"id": str(uuid.uuid4()), "provider": "infisical-ce", "status": "completed"}, "secrets": client.list_metadata()}

    def broker_mutate(self, operation: str, body: Mapping[str, Any], *, principal: str, idempotency_key: str) -> dict[str, Any]:
        if operation not in {"create", "rotate", "delete"}:
            raise ConnectionsError("broker mutation operation is invalid")
        secret_name = _secret_name(body.get("secret_name") or self.settings.resend_secret_name)
        if secret_name != self.settings.resend_secret_name:
            raise ConnectionsError("only the configured Resend secret is in scope")
        if operation == "delete":
            confirmation = str(body.get("confirmation_token") or "").strip()
            receipt = body.get("provider_receipt")
            if (
                not _FRANK_CONFIRMATION_TOKEN_RE.fullmatch(confirmation)
                or not isinstance(receipt, Mapping)
                or set(receipt) != {"receipt_id"}
                or not isinstance(receipt.get("receipt_id"), str)
                or not _FRANK_PROVIDER_RECEIPT_ID_RE.fullmatch(receipt["receipt_id"])
            ):
                raise ConnectionsError("delete requires a Frank confirmation token and provider receipt")
        else:
            value = body.get("secret_value")
            if not isinstance(value, str) or not value:
                raise ConnectionsError("secret_value is required for this fixed mutation")
        client = InfisicalClient(self.settings)
        _RATE_LIMITER.check(principal)
        cached = _LEDGER.begin(principal, operation, idempotency_key, body)
        if cached is not None:
            return cached
        try:
            if operation in {"create", "rotate"}:
                metadata = _safe_metadata(client.mutate(operation, secret_name, value))
                if operation == "rotate":
                    _record_resend_rotation(self.settings, metadata)
                else:
                    _record_resend_create(self.settings, metadata)
            else:
                metadata = _safe_metadata(client.delete(secret_name))
                _record_resend_deleted(self.settings, metadata)
            result = {"schema": SCHEMA_VERSION, "operation": operation, "outcome": {"create": "created", "rotate": "updated", "delete": "deleted"}[operation], "receipt": {"id": str(uuid.uuid4()), "provider": "infisical-ce", "status": "completed", "secret_name": secret_name}, "secret": metadata}
            _LEDGER.finish(principal, operation, idempotency_key, body, result)
            return result
        except Exception as exc:
            # Once the durable in-progress record exists, any exception may
            # have occurred after the provider committed. Retrying would risk
            # a second create/rotate/delete, so retain a terminal uncertain
            # state for operator reconciliation.
            try:
                _LEDGER.mark_uncertain(principal, operation, idempotency_key, body)
            except ConnectionsError:
                # The original in-progress record remains fail-closed when an
                # attempted uncertain-state update cannot be written.
                pass
            raise ConnectionsError(
                "Vault mutation outcome is uncertain; manual reconciliation is required",
                error_code="idempotency_uncertain",
                error_category="unavailable",
            ) from exc
