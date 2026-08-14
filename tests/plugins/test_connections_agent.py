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
            self.assertEqual(status["providers"][0]["state"], "ready")
            self.assertNotIn("secretValue", json.dumps(status))

    def test_delete_requires_frank_confirmation_and_provider_receipt(self):
        client = unittest.mock.Mock()
        client.delete.return_value = {"id": "safe"}
        with patch.object(runtime, "InfisicalClient", return_value=client):
            with self.assertRaises(runtime.ConnectionsError):
                runtime.ConnectionsRuntime(self.settings()).broker_mutate(
                    "delete", {"secret_name": "RESEND_API_KEY"}, principal="frank-vault-broker", idempotency_key="delete-key-1"
                )

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


if __name__ == "__main__":
    unittest.main()
