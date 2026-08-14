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
_SECRET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_MAX_REQUEST_BYTES = 64 * 1024
_MAX_SECRET_VALUE_BYTES = 16 * 1024
_SAFE_METADATA_FIELDS = {
    "id", "_id", "environment", "version", "type", "secretKey",
    "secretPath", "createdAt", "updatedAt", "secretValueHidden",
    "isRotatedSecret", "rotationId", "folderId",
}

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


def load_settings(ctx: Any = None) -> ConnectionsSettings:
    """Read non-secret settings from the plugin namespace and credentials env."""
    get = lambda key, default: _setting(ctx, key, default) if ctx is not None else default
    secret_name = str(get("resend_secret_name", "RESEND_API_KEY") or "RESEND_API_KEY").strip()
    if not _SECRET_NAME_RE.fullmatch(secret_name):
        secret_name = "RESEND_API_KEY"
    path = str(get("secret_path", "/connections") or "/connections").strip()
    if not path.startswith("/") or ".." in path:
        path = "/connections"
    return ConnectionsSettings(
        enabled=_truthy(get("enabled", False)),
        frank_url=str(get("frank_url", "") or "").strip().rstrip("/"),
        infisical_url=str(get("infisical_url", "") or "").strip().rstrip("/"),
        project_id=str(get("infisical_project_id", "") or "").strip(),
        environment=str(get("infisical_environment", "dev") or "dev").strip(),
        secret_path=path,
        resend_secret_name=secret_name,
        agent_key=os.environ.get("HERMES_CONNECTIONS_AGENT_KEY", "").strip(),
        broker_key=os.environ.get("HERMES_VAULT_BROKER_KEY", "").strip(),
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


def _resend_was_rotated(settings: ConnectionsSettings) -> bool:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data.get("secret_name") == settings.resend_secret_name and data.get("operation") == "rotate"
    except (OSError, ValueError, TypeError):
        return False


def _record_resend_rotation(settings: ConnectionsSettings, metadata: Mapping[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    safe = {
        "schema": SCHEMA_VERSION,
        "secret_name": settings.resend_secret_name,
        "operation": "rotate",
        "updated_at": int(time.time()),
        "provider": "resend-mcp",
        "metadata": _safe_metadata(metadata),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(safe, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


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

    def status(self) -> dict[str, Any]:
        ready = _resend_was_rotated(self.settings)
        return {
            "schema": SCHEMA_VERSION,
            "profile": PROFILE_NAME,
            "session_name": SESSION_NAME,
            "plugin": "connections-agent",
            "enabled": self.settings.enabled,
            "infisical": {"configured": bool(self.settings.infisical_url and self.settings.project_id and self.settings.infisical_token), "mode": "ce-v4-fixed-scope"},
            "providers": [{"id": "resend-mcp", "state": "ready" if ready else "setup_needed", "capabilities": list(RESEND_CAPABILITIES)}],
            "mcp": {"server": RESEND_SERVER_NAME, "command": "npx", "args": ["-y", "resend-mcp"], "secret_source": "hermes-infisical", "secret_name": self.settings.resend_secret_name} if ready else {"server": RESEND_SERVER_NAME, "state": "setup_needed"},
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

            names = register_mcp_servers({
                RESEND_SERVER_NAME: {
                    "command": "npx",
                    "args": ["-y", "resend-mcp"],
                    "env": {"RESEND_API_KEY": secret_value},
                    "tools": {"include": ["sendEmail", "getEmail"]},
                    "supports_parallel_tool_calls": False,
                }
            })
            return tool_result({
                "schema": SCHEMA_VERSION,
                "server": RESEND_SERVER_NAME,
                "state": "ready" if names else "setup_needed",
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
            body = dict(args["request"])
            if any(
                "secret" in str(k).lower()
                or ("token" in str(k).lower() and str(k).lower() != "confirmation_token")
                for k in body
            ):
                raise ConnectionsError("secret or token fields are not accepted by the agent action tool")
            endpoint = f"{self.settings.frank_url}/api/connections/agent/{action}"
            return tool_result(self._frank_request(endpoint, body, key))
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
        return payload if isinstance(payload, dict) else {"ok": True}

    def broker_health(self) -> dict[str, Any]:
        return {"schema": SCHEMA_VERSION, "ok": True, "profile": PROFILE_NAME, "broker": "connections-agent", "secret_values": False}

    def broker_list_metadata(self, *, principal: str) -> dict[str, Any]:
        client = InfisicalClient(self.settings)
        return {"schema": SCHEMA_VERSION, "operation": "list-metadata", "receipt": {"id": str(uuid.uuid4()), "provider": "infisical-ce", "status": "completed"}, "secrets": client.list_metadata()}

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
                metadata = client.delete(secret_name)
            result = {"schema": SCHEMA_VERSION, "operation": operation, "receipt": {"id": str(uuid.uuid4()), "provider": "infisical-ce", "status": "completed", "secret_name": secret_name}, "metadata": metadata}
            _LEDGER.finish(principal, operation, idempotency_key, body, result)
            return result
        except Exception:
            _LEDGER.abort(principal, operation, idempotency_key, body)
            raise
