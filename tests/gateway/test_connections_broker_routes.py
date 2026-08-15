"""Contract tests for the private Connections broker on the 8642 API seam."""

import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from plugins.connections_agent.runtime import ConnectionsError, ConnectionsSettings, failure_payload


def _settings():
    return ConnectionsSettings(
        enabled=True,
        frank_url="http://frank.invalid",
        infisical_url="http://infisical.invalid",
        project_id="fixed-project",
        environment="production",
        secret_path="/hermes",
        resend_secret_name="RESEND_API_KEY",
        agent_key="a" * 40,
        broker_key="b" * 40,
        infisical_token="token",
    )


def _app(adapter):
    app = web.Application()
    app.router.add_get(
        "/api/plugins/connections-agent/vault-broker/health",
        adapter._handle_connections_health,
    )
    app.router.add_post(
        "/api/plugins/connections-agent/vault-broker/secrets/list-metadata",
        adapter._handle_connections_list_metadata,
    )
    return app


class ConnectionsBrokerRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_broker_route_is_private_cached_and_nonblocking(self):
        adapter = APIServerAdapter(PlatformConfig(enabled=True))
        calls = []

        async def fake_to_thread(fn, *args, **kwargs):
            calls.append(fn.__name__)
            return {"schema": "test", "ok": True}

        headers = {"Authorization": "Bearer " + "b" * 40, "X-Hermes-Profile": "default"}
        with patch("plugins.connections_agent.runtime.load_settings", return_value=_settings()), patch("gateway.platforms.api_server.asyncio.to_thread", side_effect=fake_to_thread):
            async with TestClient(TestServer(_app(adapter))) as client:
                response = await client.get("/api/plugins/connections-agent/vault-broker/health", headers=headers)
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Cache-Control"], "no-store, no-cache, must-revalidate")
                self.assertEqual(response.headers["Pragma"], "no-cache")
                self.assertEqual(calls, ["broker_health"])
                unauthorized = await client.get("/api/plugins/connections-agent/vault-broker/health", headers={"X-Hermes-Profile": "default"})
                self.assertEqual(unauthorized.status, 401)
                self.assertEqual(unauthorized.headers["Pragma"], "no-cache")


    async def test_list_metadata_validates_bounded_json_before_runtime(self):
        adapter = APIServerAdapter(PlatformConfig(enabled=True))
        headers = {"Authorization": "Bearer " + "b" * 40, "X-Hermes-Profile": "default"}
        with patch("plugins.connections_agent.runtime.load_settings", return_value=_settings()):
            async with TestClient(TestServer(_app(adapter))) as client:
                response = await client.post(
                    "/api/plugins/connections-agent/vault-broker/secrets/list-metadata",
                    data=b"x" * (64 * 1024 + 1), headers=headers,
                )
                self.assertEqual(response.status, 413)
                self.assertEqual(response.headers["Cache-Control"], "no-store, no-cache, must-revalidate")
                malformed = await client.post(
                    "/api/plugins/connections-agent/vault-broker/secrets/list-metadata",
                    data=b"[]", headers=headers,
                )
                self.assertEqual(malformed.status, 400)
                self.assertEqual(malformed.headers["Pragma"], "no-cache")


    def test_main_api_route_table_contains_only_private_broker_paths(self):
        adapter = APIServerAdapter(PlatformConfig(enabled=True))
        paths = {(method, path) for method, path, _handler in adapter._http_route_table()}
        self.assertIn(("GET", "/api/plugins/connections-agent/vault-broker/health"), paths)
        self.assertIn(("POST", "/api/plugins/connections-agent/vault-broker/secrets/list-metadata"), paths)
        self.assertIn(("POST", "/api/plugins/connections-agent/vault-broker/secrets/create"), paths)
        self.assertIn(("POST", "/api/plugins/connections-agent/vault-broker/secrets/rotate"), paths)
        self.assertIn(("POST", "/api/plugins/connections-agent/vault-broker/secrets/delete"), paths)

    def test_infisical_failure_categories_keep_safe_http_statuses(self):
        adapter = APIServerAdapter(PlatformConfig(enabled=True))
        for category, status in (("auth", 401), ("permission_denied", 403), ("not_found", 404), ("rate_limited", 429), ("unavailable", 503), ("timeout", 503)):
            exc = ConnectionsError("upstream body must not cross", error_code="infisical_unavailable", error_category=category)
            self.assertEqual(adapter._connections_error_status(exc), status)
            self.assertNotIn("upstream body", str(failure_payload(exc)))
