"""Security tests for Tailscale Serve dashboard session entry."""
from __future__ import annotations

from http.cookies import SimpleCookie
import threading
import time

import httpx
import uvicorn
from fastapi.testclient import TestClient
from starlette.requests import Request

from hermes_cli import web_server
from hermes_cli.web_server import should_require_auth
from hermes_cli.dashboard_auth import clear_providers, register_provider
from hermes_cli.dashboard_auth.cookies import SESSION_AT_COOKIE, SESSION_RT_COOKIE
from plugins.dashboard_auth.tailscale import TailscaleAuthProvider


def _request(*, peer: str, login: str | None = None) -> Request:
    headers = [(b"host", b"srv1625369.tail3084c0.ts.net")]
    if login is not None:
        headers.append((b"tailscale-user-login", login.encode()))
    return Request({
        "type": "http", "method": "GET", "path": "/auth/login",
        "headers": headers, "client": (peer, 1234),
    })


def test_should_require_auth_explicit_trusted_localhost_mode():
    assert should_require_auth("127.0.0.1") is False
    assert should_require_auth("127.0.0.1", trusted_local_proxy=True) is True
    assert should_require_auth("100.78.126.112") is True


def test_provider_requires_loopback_peer_even_with_identity_header():
    provider = TailscaleAuthProvider(
        allowed_users={"operator@example.com"}, public_host="srv1625369.tail3084c0.ts.net", ttl_seconds=3600,
    )
    assert provider.complete_trusted_request(
        request=_request(peer="100.78.126.50", login="operator@example.com")
    ) is None


def test_provider_rejects_missing_malformed_and_unlisted_identity():
    provider = TailscaleAuthProvider(
        allowed_users={"operator@example.com"}, public_host="srv1625369.tail3084c0.ts.net", ttl_seconds=3600,
    )
    for login in (None, "other@example.com", "operator@example.com, forged", "operator@example.com\nX: forged"):
        assert provider.complete_trusted_request(
            request=_request(peer="127.0.0.1", login=login)
        ) is None


def test_provider_mints_and_verifies_normal_session_for_allowlisted_identity():
    provider = TailscaleAuthProvider(
        allowed_users={"operator@example.com"}, public_host="srv1625369.tail3084c0.ts.net", ttl_seconds=3600,
    )
    session = provider.complete_trusted_request(
        request=_request(peer="127.0.0.1", login="Operator@Example.com")
    )
    assert session is not None
    assert session.provider == "tailscale"
    assert session.email == "Operator@Example.com"
    verified = provider.verify_session(access_token=session.access_token)
    assert verified is not None
    assert verified.user_id == "Operator@Example.com"


def test_auth_login_mints_secure_session_and_safe_landing():
    provider = TailscaleAuthProvider(
        allowed_users={"operator@example.com"}, public_host="srv1625369.tail3084c0.ts.net", ttl_seconds=3600,
    )
    clear_providers()
    register_provider(provider)
    prev = (
        getattr(web_server.app.state, "bound_host", None),
        getattr(web_server.app.state, "bound_port", None),
        getattr(web_server.app.state, "auth_required", None),
    )
    web_server.app.state.bound_host = "srv1625369.tail3084c0.ts.net"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True
    web_server.app.state.trusted_proxy_mode = True
    web_server.app.state.trusted_public_host = "srv1625369.tail3084c0.ts.net"
    try:
        client = TestClient(web_server.app, base_url="https://srv1625369.tail3084c0.ts.net", client=("127.0.0.1", 4321))
        response = client.get(
            "/auth/login?provider=tailscale&next=/knowledge",
            headers={"Tailscale-User-Login": "operator@example.com"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert response.headers["location"] == "/knowledge"
        cookies = response.headers.get_list("set-cookie")
        at = next(item for item in cookies if SESSION_AT_COOKIE in item)
        rt = next(item for item in cookies if SESSION_RT_COOKIE in item)
        for item in (at, rt):
            assert "HttpOnly" in item
            assert "Secure" in item
            assert "samesite=lax" in item.lower()
    finally:
        clear_providers()
        web_server.app.state.bound_host, web_server.app.state.bound_port, web_server.app.state.auth_required = prev
        for name in ("trusted_proxy_mode", "trusted_public_host"):
            if hasattr(web_server.app.state, name):
                delattr(web_server.app.state, name)


def test_fresh_browser_direct_knowledge_path_auto_authenticates():
    """Frank's direct /knowledge link must not expose a second login form."""
    provider = TailscaleAuthProvider(
        allowed_users={"operator@example.com"}, public_host="srv1625369.tail3084c0.ts.net", ttl_seconds=3600,
    )
    clear_providers()
    register_provider(provider)
    prev = (
        getattr(web_server.app.state, "bound_host", None),
        getattr(web_server.app.state, "bound_port", None),
        getattr(web_server.app.state, "auth_required", None),
    )
    web_server.app.state.bound_host = "srv1625369.tail3084c0.ts.net"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True
    web_server.app.state.trusted_proxy_mode = True
    web_server.app.state.trusted_public_host = "srv1625369.tail3084c0.ts.net"
    try:
        client = TestClient(web_server.app, base_url="https://srv1625369.tail3084c0.ts.net", client=("127.0.0.1", 4321))
        response = client.get(
            "/knowledge",
            headers={"Tailscale-User-Login": "operator@example.com"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert response.headers["location"].startswith("/auth/login?provider=tailscale")
        login = client.get(
            response.headers["location"],
            headers={"Tailscale-User-Login": "operator@example.com"},
            follow_redirects=False,
        )
        assert login.status_code == 302
        assert login.headers["location"] == "/knowledge"
        me = client.get("/api/auth/me")
        assert me.status_code == 200
        assert me.json()["provider"] == "tailscale"
    finally:
        clear_providers()
        web_server.app.state.bound_host, web_server.app.state.bound_port, web_server.app.state.auth_required = prev
        for name in ("trusted_proxy_mode", "trusted_public_host"):
            if hasattr(web_server.app.state, name):
                delattr(web_server.app.state, name)


def test_auth_login_without_trusted_identity_falls_back_without_cookie():
    provider = TailscaleAuthProvider(
        allowed_users={"operator@example.com"}, public_host="srv1625369.tail3084c0.ts.net", ttl_seconds=3600,
    )
    clear_providers()
    register_provider(provider)
    prev = (
        getattr(web_server.app.state, "bound_host", None),
        getattr(web_server.app.state, "bound_port", None),
        getattr(web_server.app.state, "auth_required", None),
    )
    web_server.app.state.bound_host = "srv1625369.tail3084c0.ts.net"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True
    web_server.app.state.trusted_proxy_mode = True
    web_server.app.state.trusted_public_host = "srv1625369.tail3084c0.ts.net"
    try:
        client = TestClient(web_server.app, base_url="https://srv1625369.tail3084c0.ts.net", client=("127.0.0.1", 4321))
        response = client.get(
            "/auth/login?provider=tailscale&next=/knowledge",
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert response.headers["location"] == "/login?next=%2Fknowledge"
        assert not any(SESSION_AT_COOKIE in item for item in response.headers.get_list("set-cookie"))
    finally:
        clear_providers()
        web_server.app.state.bound_host, web_server.app.state.bound_port, web_server.app.state.auth_required = prev
        for name in ("trusted_proxy_mode", "trusted_public_host"):
            if hasattr(web_server.app.state, name):
                delattr(web_server.app.state, name)


def test_runtime_localhost_mode_keeps_real_peer_and_secure_serve_cookies():
    """Model the real Serve hop: local transport, remote XFF, HTTPS XFP."""
    provider = TailscaleAuthProvider(
        allowed_users={"operator@example.com"}, public_host="srv1625369.tail3084c0.ts.net", ttl_seconds=3600,
    )
    clear_providers()
    register_provider(provider)
    prev = (
        getattr(web_server.app.state, "bound_host", None),
        getattr(web_server.app.state, "bound_port", None),
        getattr(web_server.app.state, "auth_required", None),
        getattr(web_server.app.state, "trusted_proxy_mode", None),
        getattr(web_server.app.state, "trusted_public_host", None),
    )
    web_server.app.state.bound_host = "127.0.0.1"
    web_server.app.state.bound_port = 9119
    web_server.app.state.trusted_proxy_mode = True
    web_server.app.state.trusted_public_host = "srv1625369.tail3084c0.ts.net"
    web_server.app.state.auth_required = should_require_auth(
        "127.0.0.1", trusted_local_proxy=True,
    )
    assert web_server.app.state.auth_required is True
    try:
        client = TestClient(web_server.app, base_url="http://srv1625369.tail3084c0.ts.net", client=("127.0.0.1", 4321))
        headers = {
            "Tailscale-User-Login": "operator@example.com",
            "X-Forwarded-For": "100.78.126.50",
            "X-Forwarded-Proto": "https",
        }
        first = client.get("/knowledge", headers=headers, follow_redirects=False)
        assert first.status_code == 302
        assert first.headers["location"].startswith("/auth/login?provider=tailscale")
        login = client.get(first.headers["location"], headers=headers, follow_redirects=False)
        assert login.status_code == 302
        assert login.headers["location"] == "/knowledge"
        assert any("Secure" in item for item in login.headers.get_list("set-cookie"))
    finally:
        clear_providers()
        (web_server.app.state.bound_host,
         web_server.app.state.bound_port,
         web_server.app.state.auth_required,
         web_server.app.state.trusted_proxy_mode,
         web_server.app.state.trusted_public_host) = prev


def test_runtime_localhost_mode_rejects_forged_local_host_and_header():
    provider = TailscaleAuthProvider(
        allowed_users={"operator@example.com"}, public_host="srv1625369.tail3084c0.ts.net", ttl_seconds=3600,
    )
    clear_providers()
    register_provider(provider)
    prev = (
        getattr(web_server.app.state, "bound_host", None),
        getattr(web_server.app.state, "bound_port", None),
        getattr(web_server.app.state, "auth_required", None),
        getattr(web_server.app.state, "trusted_proxy_mode", None),
        getattr(web_server.app.state, "trusted_public_host", None),
    )
    web_server.app.state.bound_host = "127.0.0.1"
    web_server.app.state.bound_port = 9119
    web_server.app.state.trusted_proxy_mode = True
    web_server.app.state.trusted_public_host = "srv1625369.tail3084c0.ts.net"
    web_server.app.state.auth_required = True
    try:
        client = TestClient(web_server.app, base_url="http://localhost", client=("127.0.0.1", 4321))
        headers = {"Tailscale-User-Login": "operator@example.com", "X-Forwarded-Proto": "https"}
        direct = client.get("/auth/login?provider=tailscale&next=/knowledge", headers=headers, follow_redirects=False)
        assert direct.status_code == 302
        assert direct.headers["location"] == "/login?next=%2Fknowledge"
    finally:
        clear_providers()
        (web_server.app.state.bound_host,
         web_server.app.state.bound_port,
         web_server.app.state.auth_required,
         web_server.app.state.trusted_proxy_mode,
         web_server.app.state.trusted_public_host) = prev


def test_real_uvicorn_socket_preserves_loopback_peer_and_mints_secure_session():
    """Exercise the actual Serve-shaped hop over a real loopback socket."""

    class RecordingProvider(TailscaleAuthProvider):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.peers: list[str] = []

        def complete_trusted_request(self, *, request):
            self.peers.append(request.client.host if request.client else "")
            return super().complete_trusted_request(request=request)

    provider = RecordingProvider(
        allowed_users={"operator@example.com"},
        public_host="srv1625369.tail3084c0.ts.net",
        ttl_seconds=3600,
    )
    clear_providers()
    register_provider(provider)
    state_names = (
        "bound_host", "bound_port", "auth_required", "trusted_proxy_mode",
        "trusted_public_host",
    )
    previous = {name: getattr(web_server.app.state, name, None) for name in state_names}
    web_server.app.state.bound_host = "127.0.0.1"
    web_server.app.state.bound_port = 0
    web_server.app.state.auth_required = True
    web_server.app.state.trusted_proxy_mode = True
    web_server.app.state.trusted_public_host = "srv1625369.tail3084c0.ts.net"

    config = uvicorn.Config(
        web_server.app,
        host="127.0.0.1",
        port=0,
        lifespan="off",
        proxy_headers=False,
        log_level="error",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    try:
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        assert server.servers and server.servers[0].sockets
        port = server.servers[0].sockets[0].getsockname()[1]
        serve_headers = {
            "Host": "srv1625369.tail3084c0.ts.net",
            "Tailscale-User-Login": "operator@example.com",
            "X-Forwarded-For": "100.78.126.50",
            "X-Forwarded-Proto": "https",
        }
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            follow_redirects=False,
            timeout=5,
        ) as client:
            first = client.get("/knowledge", headers=serve_headers)
            assert first.status_code == 302
            assert first.headers["location"].startswith(
                "/auth/login?provider=tailscale"
            )
            login = client.get(first.headers["location"], headers=serve_headers)
            assert login.status_code == 302
            assert login.headers["location"] == "/knowledge"
            set_cookie = SimpleCookie()
            for value in login.headers.get_list("set-cookie"):
                set_cookie.load(value)
            access_cookie = next(
                morsel for morsel in set_cookie.values()
                if morsel.key.endswith(SESSION_AT_COOKIE)
            )
            refresh_cookie = next(
                morsel for morsel in set_cookie.values()
                if morsel.key.endswith(SESSION_RT_COOKIE)
            )
            assert access_cookie["secure"]
            assert refresh_cookie["secure"]
            cookie_header = "; ".join(
                f"{morsel.key}={morsel.value}"
                for morsel in set_cookie.values()
            )
            me = client.get(
                "/api/auth/me",
                headers={**serve_headers, "Cookie": cookie_header},
            )
            assert me.status_code == 200
            assert me.json()["provider"] == "tailscale"

            forged_local = client.get(
                "/auth/login?provider=tailscale&next=/knowledge",
                headers={
                    **serve_headers,
                    "Host": "localhost",
                },
            )
            assert forged_local.status_code == 302
            assert forged_local.headers["location"] == "/login?next=%2Fknowledge"
            assert not any(
                "hermes_session_at=" in value
                for value in forged_local.headers.get_list("set-cookie")
            )
        assert provider.peers
        assert set(provider.peers) == {"127.0.0.1"}
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()
        clear_providers()
        for name, value in previous.items():
            if value is None and hasattr(web_server.app.state, name):
                delattr(web_server.app.state, name)
            else:
                setattr(web_server.app.state, name, value)
