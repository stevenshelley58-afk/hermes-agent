import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


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
        self.assertNotEqual(missing_health.get("outcome"), "verified")

        with patch.object(runtime.InfisicalClient, "list_metadata", side_effect=runtime.ConnectionsError("Infisical CE request failed (GET)")):
            failed = runtime.ConnectionsRuntime(self.settings()).broker_health()
        self.assertEqual(failed["state"], "error")
        self.assertFalse(failed["infisical"]["verified"])
        self.assertNotEqual(failed.get("outcome"), "verified")

        with patch.object(runtime.InfisicalClient, "list_metadata", return_value=[]):
            verified = runtime.ConnectionsRuntime(self.settings()).broker_health()
        self.assertEqual(verified["outcome"], "verified")
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
            runtime.sanitize_action_request({"connection_id": "c1", "metadata": {"nested": {"api_key": "secret"}}})
        with self.assertRaises(runtime.ConnectionsError):
            runtime.sanitize_action_request({"connection_id": "c1", "metadata": [{"auth": {"token": "secret"}}]})
        with self.assertRaises(runtime.ConnectionsError):
            runtime.sanitize_action_request({"connection_id": "c1", "capability": "email.send", "provider": "resend", "operation": "send", "profile": "default", "confirmation_token": "Bearer secret-token-value"})

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
            "connection_id": "c1",
            "outcome": "failed",
            "provider_receipt": {"receipt_id": "receipt-1234"},
            "provider_error_code": "provider_rejected",
            "provider_error_category": "unavailable",
        })
        self.assertEqual(request["outcome"], "failed")
        self.assertEqual(request["provider_receipt"], {"receipt_id": "receipt-1234"})
        self.assertEqual(request["provider_error_category"], "unavailable")
        self.assertNotIn("error", request)
        self.assertNotIn("raw_error", request["provider_receipt"])

        response = runtime.sanitize_action_response({
            "outcome": "failed",
            "provider": "resend",
            "provider_receipt": {"receipt_id": "receipt-1234", "message": "secret"},
            "provider_error_code": "not-allowlisted",
            "provider_error_category": "not-allowlisted",
            "error": "Authorization: Bearer secret",
        }, action="apply")
        self.assertEqual(response["outcome"], "failed")
        self.assertEqual(response["error_code"], "unknown_error")
        self.assertEqual(response["error_category"], "unknown")
        self.assertNotEqual(response["provider_receipt"], {"receipt_id": "receipt-1234"})
        self.assertNotIn("error", response)
        self.assertEqual(set(response), {"schema", "outcome", "provider_receipt", "error_code", "error_category"})

    def test_apply_requires_plan_id_and_new_idempotency_key(self):
        runtime_instance = runtime.ConnectionsRuntime(self.settings())
        with patch.object(runtime_instance, "_frank_request", return_value={"plan_id": "plan-1234"}):
            runtime_instance.request_tool({"action": "plan", "request": {"connection_id": "c1", "profile": "default"}, "idempotency_key": "plan-key-1"})
        same_key = runtime_instance.request_tool({"action": "apply", "plan_id": "plan-1234", "request": {"connection_id": "c1", "profile": "default"}, "idempotency_key": "plan-key-1"})
        self.assertIn("new idempotency_key", same_key)
        missing_plan = runtime_instance.request_tool({"action": "apply", "request": {"connection_id": "c1", "profile": "default"}, "idempotency_key": "apply-key-1"})
        self.assertIn("plan_id", missing_plan)

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
            "HERMES_CONNECTIONS_BROKER_KEY": "broker-secret-never-status",
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
