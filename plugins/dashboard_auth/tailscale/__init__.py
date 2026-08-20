"""Tailscale Serve identity dashboard authentication.

This provider is deliberately useful only behind Tailscale Serve.  Hermes
must listen on localhost so a remote client cannot forge the identity headers
that Serve injects.  The provider mints the ordinary Hermes cookie session;
Frank never receives or forwards a Hermes credential.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import time
from typing import Any, Optional

from hermes_cli.dashboard_auth import DashboardAuthProvider, LoginStart, RefreshExpiredError, Session

logger = logging.getLogger(__name__)

_DEFAULT_TTL_SECONDS = 12 * 60 * 60
_REFRESH_TTL_SECONDS = 30 * 24 * 60 * 60
_SIG_LEN = hashlib.sha256().digest_size
_MAX_LOGIN_LEN = 320
_PUBLIC_HOST_RE = re.compile(r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")

LAST_SKIP_REASON: str = ""


def _sign(payload: dict[str, Any], secret: bytes) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    sig = hmac.new(secret, raw, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw + sig).decode("ascii")


def _unsign(token: str, secret: bytes) -> Optional[dict[str, Any]]:
    try:
        blob = base64.urlsafe_b64decode(token.encode("ascii"))
        if len(blob) <= _SIG_LEN:
            return None
        raw, sig = blob[:-_SIG_LEN], blob[-_SIG_LEN:]
        expected = hmac.new(secret, raw, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else None
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        return None


def _valid_login(value: str) -> bool:
    if not value or len(value) > _MAX_LOGIN_LEN:
        return False
    if any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in value):
        return False
    return "," not in value and ";" not in value


def _valid_public_host(value: str) -> bool:
    if not value or len(value) > 253 or value != value.strip():
        return False
    return bool(_PUBLIC_HOST_RE.fullmatch(value))


def _host_only(value: str) -> str:
    value = value.strip().casefold()
    if value.startswith("[") and "]" in value:
        return value[1:value.find("]")]
    return value.rsplit(":", 1)[0] if ":" in value else value


def _loopback_peer(request: Any) -> bool:
    peer = getattr(getattr(request, "client", None), "host", "") or ""
    try:
        return ipaddress.ip_address(peer).is_loopback
    except ValueError:
        return False


class TailscaleAuthProvider(DashboardAuthProvider):
    """Allowlisted identity headers from a local Tailscale Serve proxy."""

    name = "tailscale"
    display_name = "Tailscale"
    supports_trusted_request = True

    def __init__(self, *, allowed_users: set[str], public_host: str, ttl_seconds: int) -> None:
        if not allowed_users:
            raise ValueError("allowed_users must not be empty")
        if not _valid_public_host(public_host):
            raise ValueError("public_host must be a hostname without a scheme or port")
        self._allowed_users = frozenset(item.casefold() for item in allowed_users)
        self.public_host = public_host.casefold()
        self._ttl = max(60, int(ttl_seconds))
        # This is intentionally process-local. It is not shared with Frank or
        # Tailscale; restart invalidates dashboard sessions and fails closed.
        self._secret = secrets.token_bytes(32)

    def start_login(self, *, redirect_uri: str) -> LoginStart:
        # The auth route handles trusted-request providers directly. Keep the
        # abstract OAuth method harmless if a caller invokes it explicitly.
        _ = redirect_uri
        return LoginStart(redirect_url="/login", cookie_payload={})

    def complete_login(
        self, *, code: str, state: str, code_verifier: str, redirect_uri: str
    ) -> Session:
        raise NotImplementedError("Tailscale uses trusted-request login")

    def complete_trusted_request(self, *, request: Any) -> Optional[Session]:
        # A direct bind on the Tailscale IP is never trusted. Serve must proxy
        # to a localhost-only Hermes listener before these headers are accepted.
        if not _loopback_peer(request):
            return None
        if _host_only(request.headers.get("host", "")) != self.public_host:
            return None
        login = str(request.headers.get("Tailscale-User-Login", "")).strip()
        if not _valid_login(login) or login.casefold() not in self._allowed_users:
            return None
        return self._mint_session(login)

    def verify_session(self, *, access_token: str) -> Optional[Session]:
        payload = _unsign(access_token, self._secret)
        if not self._payload_allowed(payload, kind="access"):
            return None
        return self._session_from_payload(access_token, "", payload)

    def refresh_session(self, *, refresh_token: str) -> Session:
        payload = _unsign(refresh_token, self._secret)
        if not self._payload_allowed(payload, kind="refresh"):
            raise RefreshExpiredError("refresh token expired or invalid")
        return self._mint_session(str(payload["sub"]))

    def revoke_session(self, *, refresh_token: str) -> None:
        _ = refresh_token

    def _payload_allowed(self, payload: Optional[dict[str, Any]], *, kind: str) -> bool:
        if payload is None or payload.get("kind") != kind:
            return False
        subject = str(payload.get("sub", ""))
        return bool(subject) and subject.casefold() in self._allowed_users and int(payload.get("exp", 0)) > int(time.time())

    def _mint_session(self, login: str) -> Session:
        now = int(time.time())
        exp = now + self._ttl
        access = _sign({"sub": login, "kind": "access", "exp": exp}, self._secret)
        refresh = _sign({"sub": login, "kind": "refresh", "exp": now + _REFRESH_TTL_SECONDS}, self._secret)
        return Session(
            user_id=login,
            email=login,
            display_name=login,
            org_id="",
            provider=self.name,
            expires_at=exp,
            access_token=access,
            refresh_token=refresh,
        )

    def _session_from_payload(self, access: str, refresh: str, payload: dict[str, Any]) -> Session:
        login = str(payload["sub"])
        return Session(
            user_id=login,
            email=login,
            display_name=login,
            org_id="",
            provider=self.name,
            expires_at=int(payload["exp"]),
            access_token=access,
            refresh_token=refresh,
        )


def _load_section() -> dict[str, Any]:
    try:
        from hermes_cli.config import cfg_get, load_config
        value = cfg_get(load_config(), "dashboard", "tailscale_auth", default=None)
    except Exception as exc:  # noqa: BLE001 — plugin must fail closed
        logger.debug("dashboard-auth-tailscale: config unavailable: %s", exc)
        return {}
    return value if isinstance(value, dict) else {}


def register(ctx) -> None:
    global LAST_SKIP_REASON
    LAST_SKIP_REASON = ""
    section = _load_section()
    raw_users = section.get("allowed_users", [])
    public_host = str(section.get("public_host", "") or "").strip()
    if not isinstance(raw_users, (list, tuple, set)):
        LAST_SKIP_REASON = "dashboard.tailscale_auth.allowed_users must be a list"
        logger.warning("dashboard-auth-tailscale: %s", LAST_SKIP_REASON)
        return
    users = {str(item).strip() for item in raw_users if isinstance(item, str) and _valid_login(item.strip())}
    if not users:
        LAST_SKIP_REASON = "dashboard.tailscale_auth.allowed_users is empty"
        logger.debug("dashboard-auth-tailscale: %s", LAST_SKIP_REASON)
        return
    try:
        ttl = int(section.get("session_ttl_seconds") or _DEFAULT_TTL_SECONDS)
        provider = TailscaleAuthProvider(allowed_users=users, public_host=public_host, ttl_seconds=ttl)
    except (TypeError, ValueError) as exc:
        LAST_SKIP_REASON = f"TailscaleAuthProvider construction failed: {exc}"
        logger.warning("dashboard-auth-tailscale: %s", LAST_SKIP_REASON)
        return
    ctx.register_dashboard_auth_provider(provider)
    logger.info("dashboard-auth-tailscale: registered allowlisted Serve identity provider (%d users)", len(users))
