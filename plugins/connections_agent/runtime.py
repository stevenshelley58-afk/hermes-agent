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
ERROR_CATEGORIES = frozenset({"auth", "configuration", "network", "not_found", "permission_denied", "rate_limited", "timeout", "unavailable", "validation", "unknown"})
ERROR_CODES = frozenset({
    "auth_failed", "confirmation_required", "infisical_unavailable", "invalid_completion",
    "invalid_request", "mcp_unavailable", "provider_error", "provider_rejected",
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
_OPAQUE_RECEIPT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,255}$")
_SAFE_ACTION_IDENTIFIERS = frozenset({
    "action_id", "connection_id", "provider", "provider_id", "capability",
    "operation", "profile", "plan_id", "confirmation_token",
})
_COMPLETION_FIELDS = frozenset({
    "outcome", "provider_receipt", "provider_error_code", "provider_error_category",
})
_SENSITIVE_FIELD_RE = re.compile(
    r"(?:secret|token|auth|password|credential|api[_-]?key|payment|card|cvv|private[_-]?key|bearer)",
    re.IGNORECASE,
)
_SECRET_LIKE_VALUE_RE = re.compile(
    r"(?:\b(?:re|sk|rk|pk)_[A-Za-z0-9_-]{8,}\b|\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b|\b(?:api[_-]?key|secret|token|password)\s*[:=])",
    re.IGNORECASE,
)

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
            "request": {"type": "object"},
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


class ConnectionsError(RuntimeError):
    """Safe, user-facing error whose text contains no remote response body."""


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


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _setting(ctx: Any, key: str, default: Any) -> Any:
    try:
        return ctx.get_config(key, default)
    except Exception:
        return default


def _env_or_setting(ctx: Any, key: str, default: Any, env_name: str, *aliases: str) -> Any:
    """Use the same environment-backed settings source in tools and API."""
    for name in (env_name, *aliases):
        if name in os.environ:
            return os.environ[name]
    return _setting(ctx, key, default) if ctx is not None else default


def load_settings(ctx: Any = None) -> ConnectionsSettings:
    """Read one authoritative settings surface: env, then plugin config, then defaults.

    The environment names are intentionally usable by both plugin registration
    and dashboard API import, which has no PluginContext. Credentials are
    environment-only; the legacy broker-key alias remains accepted for rollout.
    """
    enabled = _env_or_setting(ctx, "enabled", False, "HERMES_CONNECTIONS_ENABLED")
    frank_url = _env_or_setting(ctx, "frank_url", "", "HERMES_CONNECTIONS_FRANK_URL")
    infisical_url = _env_or_setting(ctx, "infisical_url", "", "HERMES_CONNECTIONS_INFISICAL_URL")
    project_id = _env_or_setting(ctx, "infisical_project_id", "", "HERMES_CONNECTIONS_INFISICAL_PROJECT_ID")
    environment = _env_or_setting(ctx, "infisical_environment", "dev", "HERMES_CONNECTIONS_INFISICAL_ENVIRONMENT")
    path_value = _env_or_setting(ctx, "secret_path", "/connections", "HERMES_CONNECTIONS_INFISICAL_SECRET_PATH")
    secret_name = str(_env_or_setting(ctx, "resend_secret_name", "RESEND_API_KEY", "HERMES_CONNECTIONS_RESEND_SECRET_NAME") or "RESEND_API_KEY").strip()
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
        broker_key=os.environ.get("HERMES_CONNECTIONS_BROKER_KEY", os.environ.get("HERMES_VAULT_BROKER_KEY", "")).strip(),
        infisical_token=os.environ.get("HERMES_CONNECTIONS_INFISICAL_TOKEN", "").strip(),
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
                if not (not path and lowered == "confirmation_token"):
                    raise ConnectionsError("sensitive fields are not accepted by the agent action tool")
            _reject_sensitive_payload(nested, path=path + (lowered,))
        return
    if isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _reject_sensitive_payload(nested, path=path + (str(index),))
        return
    if isinstance(value, str) and _SECRET_LIKE_VALUE_RE.search(value):
        raise ConnectionsError("secret-like values are not accepted by the agent action tool")


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


def _safe_error_code(value: Any, fallback: str = "unknown_error") -> str:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in ERROR_CODES else fallback


def _safe_error_category(value: Any, fallback: str = "unknown") -> str:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in ERROR_CATEGORIES else fallback


def classify_failure(exc: BaseException) -> tuple[str, str]:
    """Map internal errors to the allowlisted, non-text failure contract."""
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
        return "infisical_unavailable", "network"
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


def sanitize_action_request(body: Mapping[str, Any]) -> dict[str, Any]:
    """Strip provider prose and enforce the completion outcome vocabulary."""
    if not isinstance(body, Mapping):
        raise ConnectionsError("action request must be an object")
    _reject_sensitive_payload(body)
    allowed = _SAFE_ACTION_IDENTIFIERS | _COMPLETION_FIELDS
    unknown = {str(key) for key in body if str(key) not in allowed}
    if unknown:
        raise ConnectionsError("action request fields are not allowlisted")
    cleaned: dict[str, Any] = {}
    for key, value in body.items():
        name = str(key)
        if name == "provider_receipt":
            receipt = _opaque_receipt(value)
            if receipt is None:
                raise ConnectionsError("provider receipt is not opaque")
            cleaned[name] = receipt
        elif name == "profile":
            if value != PROFILE_NAME:
                raise ConnectionsError("profile must be default")
            cleaned[name] = PROFILE_NAME
        elif name == "confirmation_token":
            if not isinstance(value, str) or not _OPAQUE_RECEIPT_RE.fullmatch(value.strip()):
                raise ConnectionsError("confirmation_token is not opaque")
            cleaned[name] = value.strip()
        else:
            if not isinstance(value, str) or not 1 <= len(value) <= 256:
                raise ConnectionsError(f"{name} must be a bounded identifier")
            cleaned[name] = value

    outcome = cleaned.get("outcome")
    if outcome is not None:
        outcome = str(outcome).strip().lower()
        if outcome == "failed":
            cleaned["outcome"] = "failed"
            if set(cleaned) - (_SAFE_ACTION_IDENTIFIERS | {"outcome", "provider_receipt", "provider_error_code", "provider_error_category"}):
                raise ConnectionsError("failed completion contains unsupported fields")
            cleaned["provider_error_code"] = _safe_error_code(cleaned.get("provider_error_code"), "unknown_error")
            cleaned["provider_error_category"] = _safe_error_category(cleaned.get("provider_error_category"), "unknown")
            cleaned.pop("error_code", None)
            cleaned.pop("error_category", None)
            if "provider_receipt" not in cleaned:
                raise ConnectionsError("failed completion requires provider_receipt")
        elif outcome in SUCCESS_OUTCOMES:
            cleaned["outcome"] = outcome
            if "error_code" in cleaned or "error_category" in cleaned or "provider_error_code" in cleaned or "provider_error_category" in cleaned:
                raise ConnectionsError("error fields are allowed only for failed completion")
        else:
            raise ConnectionsError("completion outcome is not allowlisted")
    return cleaned


def sanitize_action_response(payload: Mapping[str, Any], *, action: str) -> dict[str, Any]:
    """Return safe Frank response metadata and redact provider error prose."""
    outcome = str(payload.get("outcome") or "").strip().lower()
    if outcome == "failed":
        return failure_payload(
            ConnectionsError("Frank returned a failed completion"),
            provider=str(payload.get("provider") or "frank") if isinstance(payload.get("provider"), str) else "frank",
            provider_receipt=payload.get("provider_receipt"),
        ) | {
            "error_code": _safe_error_code(payload.get("provider_error_code")),
            "error_category": _safe_error_category(payload.get("provider_error_category")),
        }
    if outcome and outcome not in SUCCESS_OUTCOMES:
        return failure_payload(ConnectionsError("Frank returned an invalid completion"), provider="frank")
    allowed = {"schema", "outcome", "action_id", "connection_id", "provider", "capability", "plan_id", "receipt_id", "provider_receipt", "created_at", "updated_at", "state", "status"}
    result = {key: value for key, value in payload.items() if key in allowed}
    if "provider_receipt" in result:
        result["provider_receipt"] = _opaque_receipt(result["provider_receipt"])
        if result["provider_receipt"] is None:
            result.pop("provider_receipt")
    if action == "apply" and not outcome:
        return failure_payload(ConnectionsError("Frank returned no completion outcome"), provider="frank")
    return result


class InfisicalClient:
    """Fixed-scope Infisical CE v4 client; never exposes its response body."""

    def __init__(self, settings: ConnectionsSettings):
        if not settings.infisical_url or not settings.project_id or not settings.infisical_token:
            raise SetupNeeded("Infisical CE is not configured on Hermes")
        self.settings = settings

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
        raw = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=raw,
            method=method,
            headers={
                "Authorization": f"Bearer {self.settings.infisical_token}",
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if raw is not None else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read(_MAX_REQUEST_BYTES).decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            raise ConnectionsError(f"Infisical CE request failed ({method})") from exc
        return payload if isinstance(payload, dict) else {}

    def list_metadata(self) -> list[dict[str, Any]]:
        payload = self._request("GET", self._url())
        entries = payload.get("secrets")
        return [_safe_metadata(item) for item in entries if isinstance(item, Mapping)] if isinstance(entries, list) else []

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
        return _safe_metadata(payload.get("secret"))

    def delete(self, secret_name: str) -> dict[str, Any]:
        payload = self._request("DELETE", self._url(secret_name), {
            "projectId": self.settings.project_id,
            "environment": self.settings.environment,
            "secretPath": self.settings.secret_path,
            "type": "shared",
        })
        return _safe_metadata(payload.get("secret"))


class _MutationLedger:
    """Bounded in-memory idempotency and rate-limit ledger."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, str, dict[str, Any]]] = {}
        self._hits: dict[str, list[float]] = {}

    def begin(self, principal: str, route: str, key: str, body: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        if not _IDEMPOTENCY_RE.fullmatch(key):
            raise ConnectionsError("Idempotency-Key is required and invalid")
        now = time.monotonic()
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        entry_key = f"{principal}:{route}:{key}"
        with self._lock:
            self._entries = {k: v for k, v in self._entries.items() if v[0] > now}
            hits = [item for item in self._hits.get(principal, []) if item > now - 60]
            if len(hits) >= 30:
                raise ConnectionsError("request rate limit exceeded")
            self._hits[principal] = hits + [now]
            existing = self._entries.get(entry_key)
            if existing:
                if existing[1] != fingerprint:
                    raise ConnectionsError("Idempotency-Key was reused with a different request")
                if not existing[2]:
                    raise ConnectionsError("request with this Idempotency-Key is still in progress")
                return dict(existing[2])
            self._entries[entry_key] = (now + 300, fingerprint, {})
        return None

    def abort(self, principal: str, route: str, key: str, body: Mapping[str, Any]) -> None:
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with self._lock:
            entry_key = f"{principal}:{route}:{key}"
            existing = self._entries.get(entry_key)
            if existing and existing[1] == fingerprint and not existing[2]:
                self._entries.pop(entry_key, None)

    def finish(self, principal: str, route: str, key: str, body: Mapping[str, Any], result: dict[str, Any]) -> None:
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with self._lock:
            entry_key = f"{principal}:{route}:{key}"
            self._entries[entry_key] = (time.monotonic() + 300, fingerprint, dict(result))


_LEDGER = _MutationLedger()


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

    def status(self) -> dict[str, Any]:
        provider_state = _resend_provider_state(self.settings)
        configured = bool(self.settings.infisical_url and self.settings.project_id and self.settings.infisical_token)
        return {
            "schema": SCHEMA_VERSION,
            "profile": PROFILE_NAME,
            "session_name": SESSION_NAME,
            "plugin": "connections-agent",
            "enabled": self.settings.enabled,
            "infisical": {"configured": configured, "reachable": None, "verified": None, "mode": "ce-v4-fixed-scope"},
            "providers": [{"id": "resend-mcp", "state": provider_state, "capabilities": list(RESEND_CAPABILITIES)}],
            "mcp": {"server": RESEND_SERVER_NAME, "command": "npx", "args": ["-y", RESEND_MCP_PACKAGE], "secret_source": "hermes-infisical", "secret_name": self.settings.resend_secret_name, "state": provider_state} if provider_state != "setup_needed" else {"server": RESEND_SERVER_NAME, "state": "setup_needed"},
        }

    def status_tool(self, _args: Mapping[str, Any]) -> str:
        return tool_result(self.status())

    def resend_mcp_tool(self, _args: Mapping[str, Any]) -> str:
        try:
            if not _resend_was_rotated(self.settings):
                raise SetupNeeded("Resend remains setup_needed until Frank records a new rotation")
            client = InfisicalClient(self.settings)
            # The value is used only as an in-memory child-process environment
            # value. It is intentionally absent from the returned result,
            # plugin state, config files, and logs.
            secret_value = client.read_value(self.settings.resend_secret_name)
            from tools.mcp_tool import register_mcp_servers

            names = register_mcp_servers({RESEND_SERVER_NAME: build_resend_mcp_config(secret_value)})
            if not names:
                return tool_result(failure_payload(ConnectionsError("Resend MCP discovery returned no allowlisted tools"), provider="resend-mcp"))
            _record_resend_connection(self.settings, names)
            return tool_result({
                "schema": SCHEMA_VERSION,
                "server": RESEND_SERVER_NAME,
                "state": "connected-awaiting-verification",
                "registered_tools": list(names),
                "capabilities": list(RESEND_CAPABILITIES),
            })
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
            returned_plan_id = str(result.get("plan_id") or plan_id).strip()
            if action == "plan" and _OPAQUE_RECEIPT_RE.fullmatch(returned_plan_id):
                self._plan_idempotency_keys[returned_plan_id] = key
            if action == "apply" and result.get("outcome") == "verified" and result.get("provider") in {"resend", "resend-mcp"}:
                _record_resend_verification(self.settings, result)
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
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read(_MAX_REQUEST_BYTES).decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            raise ConnectionsError("Frank action request failed") from exc
        if not isinstance(payload, dict):
            raise ConnectionsError("Frank returned an invalid response")
        return sanitize_action_response(payload, action=endpoint.rsplit("/", 1)[-1])

    def broker_health(self) -> dict[str, Any]:
        configured = bool(self.settings.infisical_url and self.settings.project_id and self.settings.infisical_token)
        base = {"schema": SCHEMA_VERSION, "profile": PROFILE_NAME, "broker": "connections-agent", "secret_values": False, "infisical": {"configured": configured, "reachable": False, "verified": False}}
        if not configured:
            return base | {"ok": False, "state": "setup_needed"}
        try:
            InfisicalClient(self.settings).list_metadata()
        except ConnectionsError:
            return base | {"ok": False, "state": "error", "outcome": "failed", "error_code": "infisical_unavailable", "error_category": "unavailable"}
        return base | {"ok": True, "state": "verified", "outcome": "verified", "infisical": {"configured": True, "reachable": True, "verified": True}}

    def broker_list_metadata(self, *, principal: str) -> dict[str, Any]:
        client = InfisicalClient(self.settings)
        return {"schema": SCHEMA_VERSION, "operation": "list-metadata", "outcome": "verified", "receipt": {"id": str(uuid.uuid4()), "provider": "infisical-ce", "status": "completed"}, "secrets": client.list_metadata()}

    def broker_mutate(self, operation: str, body: Mapping[str, Any], *, principal: str, idempotency_key: str) -> dict[str, Any]:
        secret_name = _secret_name(body.get("secret_name") or self.settings.resend_secret_name)
        if secret_name != self.settings.resend_secret_name:
            raise ConnectionsError("only the configured Resend secret is in scope")
        cached = _LEDGER.begin(principal, operation, idempotency_key, body)
        if cached:
            return cached
        try:
            if operation == "delete":
                confirmation = str(body.get("confirmation_token") or "").strip()
                receipt = body.get("provider_receipt")
                if len(confirmation) < 16 or not isinstance(receipt, Mapping) or not receipt.get("receipt_id"):
                    raise ConnectionsError("delete requires a Frank confirmation token and provider receipt")
            client = InfisicalClient(self.settings)
            if operation in {"create", "rotate"}:
                value = body.get("secret_value")
                if not isinstance(value, str) or not value:
                    raise ConnectionsError("secret_value is required for this fixed mutation")
                metadata = client.mutate(operation, secret_name, value)
                if operation == "rotate":
                    _record_resend_rotation(self.settings, metadata)
                else:
                    _record_resend_create(self.settings, metadata)
            else:
                metadata = client.delete(secret_name)
                _record_resend_deleted(self.settings, metadata)
            result = {"schema": SCHEMA_VERSION, "operation": operation, "outcome": {"create": "created", "rotate": "updated", "delete": "deleted"}[operation], "receipt": {"id": str(uuid.uuid4()), "provider": "infisical-ce", "status": "completed", "secret_name": secret_name}, "metadata": metadata}
            _LEDGER.finish(principal, operation, idempotency_key, body, result)
            return result
        except Exception:
            _LEDGER.abort(principal, operation, idempotency_key, body)
            raise
