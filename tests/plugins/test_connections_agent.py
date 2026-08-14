import importlib.util
import json
import os
import sys
import tempfile
import urllib.error
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "plugins" / "connections_agent" / "runtime.py"
SPEC = importlib.util.spec_from_file_location("connections_agent_runtime_test", MODULE_PATH)
runtime = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules["connections_agent_runtime_test"] = runtime
SPEC.loader.exec_module(runtime)


class ConnectionsRuntimeTests(unittest.TestCase):
    def settings(self, **overrides):
        values = dict(
            enabled=True,
            frank_url="https://frank.invalid",
            infisical_url="https://infisical.invalid",
            project_id="project",
            environment="dev",
            secret_path="/connections",
            resend_secret_name="RESEND_API_KEY",
            agent_key="a" * 40,
            broker_key="b" * 40,
            infisical_token="token",
        )
        values.update(overrides)
        return runtime.ConnectionsSettings(**values)

    def test_status_stays_setup_needed_until_local_rotation_receipt(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(runtime, "get_hermes_home", return_value=Path(tmp)):
            status = runtime.ConnectionsRuntime(self.settings()).status()
            self.assertEqual(status["providers"][0]["state"], "setup_needed")
            runtime._record_resend_rotation(self.settings(), {"id": "safe"})
            status = runtime.ConnectionsRuntime(self.settings()).status()
            self.assertEqual(status["providers"][0]["state"], "configured")
            self.assertNotIn("secretValue", json.dumps(status))

    def test_health_is_probe_backed_and_fail_closed(self):
        missing = runtime.ConnectionsRuntime(self.settings(infisical_url="", project_id="", infisical_token=""))
        missing_health = missing.broker_health()
        self.assertFalse(missing_health["infisical"]["configured"])
        self.assertEqual(missing_health["status"], "setup_needed")
        self.assertNotEqual(missing_health.get("outcome"), "verified")

        with patch.object(runtime.InfisicalClient, "list_metadata", side_effect=runtime.ConnectionsError("Infisical CE request failed (GET)")):
            failed = runtime.ConnectionsRuntime(self.settings()).broker_health()
        self.assertEqual(failed["state"], "error")
        self.assertEqual(failed["status"], "unavailable")
        self.assertFalse(failed["infisical"]["verified"])
        self.assertNotEqual(failed.get("outcome"), "verified")

        with patch.object(runtime.InfisicalClient, "list_metadata", return_value=[]):
            verified = runtime.ConnectionsRuntime(self.settings()).broker_health()
        self.assertEqual(verified["outcome"], "verified")
        self.assertEqual(verified["status"], "verified")
        self.assertTrue(verified["infisical"]["verified"])

    def test_create_delete_lifecycle_is_truthful_and_never_stores_values(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(runtime, "get_hermes_home", return_value=Path(tmp)):
            client = unittest.mock.Mock()
            client.mutate.return_value = {"id": "safe"}
            client.delete.return_value = {"id": "safe-delete"}
            with patch.object(runtime, "InfisicalClient", return_value=client):
                created = runtime.ConnectionsRuntime(self.settings()).broker_mutate(
                    "create", {"secret_name": "RESEND_API_KEY", "secret_value": "create-value-never-stored"},
                    principal="frank-vault-broker", idempotency_key="create-key-1",
                )
                self.assertEqual(created["outcome"], "created")
                configured = runtime.ConnectionsRuntime(self.settings()).status()
                self.assertEqual(configured["providers"][0]["state"], "configured")
                self.assertNotIn("create-value-never-stored", _read_state(tmp))
                deleted = runtime.ConnectionsRuntime(self.settings()).broker_mutate(
                    "delete", {"secret_name": "RESEND_API_KEY", "confirmation_token": "confirmation-1234", "provider_receipt": {"receipt_id": "receipt-delete"}},
                    principal="frank-vault-broker", idempotency_key="delete-key-2",
                )
                self.assertEqual(deleted["outcome"], "deleted")
                self.assertEqual(runtime.ConnectionsRuntime(self.settings()).status()["providers"][0]["state"], "setup_needed")

    def test_failed_create_and_delete_leave_prior_truth_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(runtime, "get_hermes_home", return_value=Path(tmp)):
            settings = self.settings()
            runtime._record_resend_create(settings, {"id": "safe"})
            client = unittest.mock.Mock()
            client.mutate.side_effect = runtime.ConnectionsError("Infisical CE request failed (POST)")
            with patch.object(runtime, "InfisicalClient", return_value=client):
                with self.assertRaises(runtime.ConnectionsError):
                    runtime.ConnectionsRuntime(settings).broker_mutate(
                        "create", {"secret_name": "RESEND_API_KEY", "secret_value": "failed-create"}, principal="frank-vault-broker", idempotency_key="create-fail-1"
                    )
            self.assertEqual(runtime.ConnectionsRuntime(settings).status()["providers"][0]["state"], "configured")

            runtime._record_resend_connection(settings, ["send-email"])
            client.delete.side_effect = runtime.ConnectionsError("Infisical CE request failed (DELETE)")
            with patch.object(runtime, "InfisicalClient", return_value=client):
                with self.assertRaises(runtime.ConnectionsError):
                    runtime.ConnectionsRuntime(settings).broker_mutate(
                        "delete", {"secret_name": "RESEND_API_KEY", "confirmation_token": "confirmation-1234", "provider_receipt": {"receipt_id": "receipt-delete-fail"}}, principal="frank-vault-broker", idempotency_key="delete-fail-1"
                    )
            self.assertEqual(runtime.ConnectionsRuntime(settings).status()["providers"][0]["state"], "connected-awaiting-verification")

    def test_resend_mcp_config_is_pinned_and_exactly_filtered(self):
        config = runtime.build_resend_mcp_config("secret-never-returned")
        self.assertEqual(config["args"], ["-y", "resend-mcp@2.13.0"])
        self.assertEqual(config["tools"]["include"], ["send-email", "get-email"])
        self.assertNotIn("secret-never-returned", json.dumps({k: v for k, v in config.items() if k != "env"}))

    def test_nested_sensitive_payloads_are_rejected_before_transport(self):
        with self.assertRaises(runtime.ConnectionsError):
            runtime.sanitize_action_request({"action": "create", "target": {"provider": "resend"}, "body": {"name": "Resend", "notes": {"nested": {"api_key": "secret"}}}})
        with self.assertRaises(runtime.ConnectionsError):
            runtime.sanitize_action_request({"action": "create", "target": {"provider": "resend"}, "body": {"name": "Resend", "capabilities": [{"auth": {"token": "secret"}}]}})
        with self.assertRaises(runtime.ConnectionsError):
            runtime.sanitize_action_request({"plan_id": "plan-1234", "confirmation_token": "Bearer secret-token-value"})

    def test_plan_schema_accepts_frank_connection_metadata_for_create_and_update(self):
        create = runtime.sanitize_action_request({
            "action": "create", "target": {"provider": "resend", "project": "connections"},
            "body": {
                "provider": "resend", "name": "Resend", "scope_kind": "global", "status": "connected",
                "connection_ref": "resend://default", "credential_ref": "openbao://frank/resend",
                "capabilities": ["email.send", "email.status"], "notes": "safe metadata",
            }, "expected_revision": 0,
        })
        self.assertEqual(create["action"], "create")
        self.assertEqual(create["body"]["capabilities"], ["email.send", "email.status"])
        update = runtime.sanitize_action_request({
            "action": "update", "target": {"provider": "resend", "connection_id": "connection-1234"},
            "body": {"name": "Resend updated", "scope_kind": "project", "scope_id": "demo", "status": "connected", "capabilities": ["email.send"]},
            "expected_revision": 2,
        })
        self.assertEqual(update["target"]["connection_id"], "connection-1234")
        self.assertEqual(update["expected_revision"], 2)

    def test_delete_requires_frank_confirmation_and_provider_receipt(self):
        client = unittest.mock.Mock()
        client.delete.return_value = {"id": "safe"}
        with patch.object(runtime, "InfisicalClient", return_value=client):
            with self.assertRaises(runtime.ConnectionsError):
                runtime.ConnectionsRuntime(self.settings()).broker_mutate(
                    "delete", {"secret_name": "RESEND_API_KEY"}, principal="frank-vault-broker", idempotency_key="delete-key-1"
                )

    def test_broker_mutations_emit_action_specific_outcomes_without_values(self):
        client = unittest.mock.Mock()
        client.mutate.return_value = {"id": "safe"}
        with patch.object(runtime, "InfisicalClient", return_value=client), patch.object(runtime, "_record_resend_rotation"):
            result = runtime.ConnectionsRuntime(self.settings()).broker_mutate(
                "rotate", {"secret_name": "RESEND_API_KEY", "secret_value": "new-key-never-returned"},
                principal="frank-vault-broker", idempotency_key="rotate-key-1",
            )
        self.assertEqual(result["outcome"], "updated")
        self.assertNotIn("new-key-never-returned", json.dumps(result))

    def test_token_provider_uses_constant_time_match_and_fixed_principal(self):
        provider = runtime.ConnectionsTokenProvider(secret="b" * 40)
        principal = provider.verify_token(token="b" * 40)
        self.assertEqual(principal.principal, "frank-vault-broker")
        self.assertIsNone(provider.verify_token(token="c" * 40))

    def test_resend_activation_is_blocked_before_new_rotation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(runtime, "get_hermes_home", return_value=Path(tmp)):
            with patch.object(runtime, "InfisicalClient") as client_cls:
                result = runtime.ConnectionsRuntime(self.settings()).resend_mcp_tool({})
                self.assertIn("setup_needed", result)
                client_cls.assert_not_called()

    def test_completion_contract_redacts_provider_error_text(self):
        request = runtime.sanitize_action_request({
            "plan_id": "plan-1234",
            "provider_outcome": "failed",
            "provider_receipt": "receipt-1234",
            "provider_error_code": "provider_rejected",
            "provider_error_category": "unavailable",
        })
        self.assertEqual(request["provider_outcome"], "failed")
        self.assertEqual(request["provider_receipt"], "receipt-1234")
        self.assertEqual(request["provider_error_category"], "unavailable")
        self.assertNotIn("error", request)
        with self.assertRaises(runtime.ConnectionsError):
            runtime.sanitize_action_request({"plan_id": "plan-1234", "outcome": "failed"})

        response = runtime.sanitize_action_response({
            "action": {"action": "verify", "state": "failed", "result": {
                "outcome": "failed", "provider": "resend", "provider_receipt": "receipt-1234",
                "error_code": "provider_rejected", "error_category": "unavailable", "message": "Authorization: Bearer secret",
            }, "unsafe": "drop"},
            "connection": {"id": "connection-1234", "provider": "resend", "status": "error", "secretValue": "drop"},
            "unsafe": "drop",
        }, action="apply")
        self.assertEqual(response["action"]["result"]["outcome"], "failed")
        self.assertEqual(response["action"]["result"]["error_category"], "unavailable")
        self.assertNotIn("message", json.dumps(response))
        self.assertNotIn("secretValue", json.dumps(response))

    def test_apply_requires_plan_id_and_new_idempotency_key(self):
        runtime_instance = runtime.ConnectionsRuntime(self.settings())
        plan_response = {"plan": {"plan_id": "plan-1234", "action": "verify", "state": "planned"}, "action": {"action": "verify", "state": "running"}}
        with patch.object(runtime_instance, "_frank_request", return_value=plan_response):
            runtime_instance.request_tool({"action": "plan", "request": {"action": "verify", "target": {"connection_id": "connection-1234"}}, "idempotency_key": "plan-key-1"})
        same_key = runtime_instance.request_tool({"action": "apply", "plan_id": "plan-1234", "request": {"plan_id": "plan-1234"}, "idempotency_key": "plan-key-1"})
        self.assertIn("new idempotency_key", same_key)
        missing_plan = runtime_instance.request_tool({"action": "apply", "request": {"plan_id": "short"}, "idempotency_key": "apply-key-1"})
        self.assertIn("plan_id", missing_plan)

    def test_mocked_frank_plan_then_apply_preserves_nested_contract(self):
        runtime_instance = runtime.ConnectionsRuntime(self.settings())
        responses = [
            {"plan": {"plan_id": "plan-verify-1234", "action": "verify", "state": "planned", "target": {"provider": "resend", "connection_id": "connection-1234"}}, "action": {"action": "verify", "state": "running"}},
            {"action": {"action": "verify", "state": "completed", "result": {"outcome": "verified", "provider": "resend", "provider_receipt": "hermes://receipt/verify-1234", "connection_id": "connection-1234", "status": "verified"}}, "connection": {"id": "connection-1234", "provider": "resend", "name": "Resend", "scope_kind": "global", "scope_id": "", "status": "verified", "capabilities": ["email.send"], "credential_ref": "openbao://frank/resend"}},
        ]
        with patch.object(runtime_instance, "_frank_request", side_effect=responses) as request:
            planned = runtime_instance.request_tool({"action": "plan", "request": {"action": "verify", "target": {"provider": "resend", "connection_id": "connection-1234"}, "expected_revision": 1}, "idempotency_key": "plan-verify-0001"})
            self.assertIn("plan-verify-1234", planned)
            applied = runtime_instance.request_tool({"action": "apply", "plan_id": "plan-verify-1234", "request": {"plan_id": "plan-verify-1234", "provider_receipt": "hermes://receipt/verify-1234", "provider_outcome": "verified"}, "idempotency_key": "apply-verify-0001"})
        self.assertIn("verified", applied)
        self.assertEqual(request.call_args_list[1].args[1]["provider_outcome"], "verified")
        self.assertNotIn("profile", request.call_args_list[0].args[1])

    def test_authenticated_requests_reject_same_and_cross_host_redirects(self):
        for location in ("https://infisical.invalid/api/v4/secrets", "https://attacker.invalid/steal"):
            error = urllib.error.HTTPError(location, 302, "redirect", {"Location": location}, None)
            with patch.object(runtime._NO_REDIRECT_OPENER, "open", side_effect=error) as opener:
                with self.assertRaises(runtime.ConnectionsError):
                    runtime.InfisicalClient(self.settings())._request("GET", "https://infisical.invalid/api/v4/secrets")
                self.assertEqual(opener.call_count, 1)
                self.assertEqual(opener.call_args.args[0].get_header("Authorization"), "Bearer token")
            error = urllib.error.HTTPError(location, 302, "redirect", {"Location": location}, None)
            with patch.object(runtime._NO_REDIRECT_OPENER, "open", side_effect=error) as opener:
                with self.assertRaises(runtime.ConnectionsError):
                    runtime.ConnectionsRuntime(self.settings())._frank_request("https://frank.invalid/api/connections/agent/plan", {"action": "discover"}, "frank-key-0001")
                self.assertEqual(opener.call_count, 1)
                self.assertEqual(opener.call_args.args[0].get_header("Authorization"), "Bearer " + ("a" * 40))

    def test_infisical_universal_auth_is_in_memory_and_refreshes_once_after_401(self):
        settings = self.settings(infisical_token="", infisical_client_id="client-id", infisical_client_secret="client-secret", infisical_organization_slug="org")

        def response(payload):
            item = MagicMock()
            item.__enter__.return_value = item
            item.__exit__.return_value = False
            item.read.return_value = json.dumps(payload).encode("utf-8")
            item.getcode.return_value = 200
            return item

        first_login = response({"accessToken": "access-token-one", "expiresIn": 3600, "accessTokenMaxTTL": 3600, "tokenType": "Bearer"})
        second_login = response({"accessToken": "access-token-two", "expiresIn": 3600, "accessTokenMaxTTL": 3600, "tokenType": "Bearer"})
        metadata = response({"secrets": []})
        unauthorized = urllib.error.HTTPError("https://infisical.invalid/api/v4/secrets", 401, "unauthorized", {}, None)
        with patch.object(runtime._NO_REDIRECT_OPENER, "open", side_effect=[first_login, metadata]):
            client = runtime.InfisicalClient(settings)
            self.assertEqual(client.list_metadata(), [])
        third_login = response({"accessToken": "access-token-three", "expiresIn": 3600, "accessTokenMaxTTL": 3600, "tokenType": "Bearer"})
        metadata_again = response({"secrets": []})
        with patch.object(runtime._NO_REDIRECT_OPENER, "open", side_effect=[first_login, unauthorized, second_login, metadata, third_login, metadata_again]) as opener:
            client = runtime.InfisicalClient(settings)
            self.assertEqual(client.list_metadata(), [])
            client._token_expires_at = 0
            self.assertEqual(client.list_metadata(), [])
            self.assertEqual(opener.call_count, 6)
            login_request = opener.call_args_list[0].args[0]
            self.assertNotIn("access-token-one", login_request.data.decode("utf-8"))
            metadata_request = opener.call_args_list[1].args[0]
            refreshed_request = opener.call_args_list[3].args[0]
            self.assertEqual(metadata_request.get_header("Authorization"), "Bearer access-token-one")
            self.assertEqual(refreshed_request.get_header("Authorization"), "Bearer access-token-two")
            self.assertEqual(json.loads(login_request.data.decode("utf-8"))["clientId"], "client-id")

    def test_only_canonical_vault_broker_key_is_loaded(self):
        with patch.dict(os.environ, {"HERMES_VAULT_BROKER_KEY": "canonical-key", "HERMES_CONNECTIONS_BROKER_KEY": "alias-key"}, clear=False):
            settings = runtime.load_settings()
        self.assertEqual(settings.broker_key, "canonical-key")

    def test_plugin_api_uses_authoritative_env_scope_without_ctx_or_secret_status(self):
        api_path = ROOT / "plugins" / "connections_agent" / "dashboard" / "plugin_api.py"
        env = {
            "HERMES_CONNECTIONS_ENABLED": "true",
            "HERMES_CONNECTIONS_FRANK_URL": "https://frank.example.invalid",
            "HERMES_CONNECTIONS_INFISICAL_URL": "https://infisical.example.invalid",
            "HERMES_CONNECTIONS_INFISICAL_PROJECT_ID": "project-fixed",
            "HERMES_CONNECTIONS_INFISICAL_ENVIRONMENT": "production",
            "HERMES_CONNECTIONS_INFISICAL_SECRET_PATH": "/connections",
            "HERMES_CONNECTIONS_RESEND_SECRET_NAME": "RESEND_API_KEY",
            "HERMES_CONNECTIONS_AGENT_KEY": "agent-secret-never-status",
            "HERMES_VAULT_BROKER_KEY": "broker-secret-never-status",
            "HERMES_CONNECTIONS_INFISICAL_TOKEN": "infisical-secret-never-status",
        }
        module_name = "connections_api_env_test"
        with patch.dict(os.environ, env, clear=False):
            api_spec = importlib.util.spec_from_file_location(module_name, api_path)
            api_module = importlib.util.module_from_spec(api_spec)
            assert api_spec and api_spec.loader
            sys.modules[module_name] = api_module
            api_spec.loader.exec_module(api_module)
            settings = api_module._runtime.settings
            self.assertEqual(settings.project_id, "project-fixed")
            self.assertEqual(settings.environment, "production")
            self.assertEqual(settings.secret_path, "/connections")
            status = api_module._runtime.status()
            status_text = json.dumps(status)
            self.assertNotIn("agent-secret-never-status", status_text)
            self.assertNotIn("broker-secret-never-status", status_text)
            self.assertNotIn("infisical-secret-never-status", status_text)

    def test_plugin_init_contains_no_mojibake_markers(self):
        source = (ROOT / "plugins" / "connections_agent" / "__init__.py").read_text(encoding="utf-8")
        for marker in ("\u00f0\u0178", "\u00e2\u0153", "\u00e2"):  # common UTF-8-as-Windows-1252 artifacts
            self.assertNotIn(marker, source)


def _read_state(tmp: str) -> str:
    return (Path(tmp) / "plugin-data" / "connections-agent" / "resend-state.json").read_text(encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
