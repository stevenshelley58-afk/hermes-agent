import importlib.util
import json
import os
import sys
import tempfile
import threading
import urllib.error
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "plugins" / "connections_agent" / "runtime.py"
SPEC = importlib.util.spec_from_file_location("connections_agent_runtime_test", MODULE_PATH)
runtime = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules["connections_agent_runtime_test"] = runtime
SPEC.loader.exec_module(runtime)


class ConnectionsRuntimeTests(unittest.TestCase):
    def setUp(self):
        self._temp_home = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_home.cleanup)
        self._home_patch = patch.object(
            runtime,
            "get_hermes_home",
            return_value=Path(self._temp_home.name),
        )
        self._home_patch.start()
        self.addCleanup(self._home_patch.stop)
        runtime._LEDGER = runtime._MutationLedger()
        runtime._RATE_LIMITER = runtime._MutationRateLimiter()

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
                    "delete", {"secret_name": "RESEND_API_KEY", "confirmation_token": "A" * 32, "provider_receipt": {"receipt_id": "a" * 32}},
                    principal="frank-vault-broker", idempotency_key="delete-key-2",
                )
                self.assertEqual(deleted["outcome"], "deleted")
                self.assertEqual(runtime.ConnectionsRuntime(self.settings()).status()["providers"][0]["state"], "setup_needed")

    def test_completed_mutation_replays_after_ledger_reinstantiation_without_raw_data(self):
        client = unittest.mock.Mock()
        client.mutate.return_value = {"id": "safe-id", "version": 3}
        body = {
            "secret_name": "RESEND_API_KEY",
            "secret_value": "restart-value-never-persisted",
        }
        with patch.object(runtime, "InfisicalClient", return_value=client):
            first = runtime.ConnectionsRuntime(self.settings()).broker_mutate(
                "create",
                body,
                principal="frank-vault-broker",
                idempotency_key="restart-create-key-0001",
            )
            runtime._LEDGER = runtime._MutationLedger()
            replay = runtime.ConnectionsRuntime(self.settings()).broker_mutate(
                "create",
                body,
                principal="frank-vault-broker",
                idempotency_key="restart-create-key-0001",
            )
        self.assertEqual(replay, first)
        client.mutate.assert_called_once()
        persisted = runtime._LEDGER._path().read_text(encoding="utf-8")
        self.assertNotIn("restart-create-key-0001", persisted)
        self.assertNotIn("restart-value-never-persisted", persisted)

    def test_reinstantiated_ledger_rejects_same_key_with_mismatched_body(self):
        client = unittest.mock.Mock()
        client.mutate.return_value = {"id": "safe-id"}
        with patch.object(runtime, "InfisicalClient", return_value=client):
            runtime.ConnectionsRuntime(self.settings()).broker_mutate(
                "rotate",
                {"secret_name": "RESEND_API_KEY", "secret_value": "first-value-never-persisted"},
                principal="frank-vault-broker",
                idempotency_key="restart-mismatch-key-0001",
            )
            runtime._LEDGER = runtime._MutationLedger()
            with self.assertRaises(runtime.ConnectionsError) as raised:
                runtime.ConnectionsRuntime(self.settings()).broker_mutate(
                    "rotate",
                    {"secret_name": "RESEND_API_KEY", "secret_value": "second-value-never-persisted"},
                    principal="frank-vault-broker",
                    idempotency_key="restart-mismatch-key-0001",
                )
        self.assertEqual(raised.exception.error_code, "idempotency_conflict")
        client.mutate.assert_called_once()
        persisted = runtime._LEDGER._path().read_text(encoding="utf-8")
        self.assertNotIn("restart-mismatch-key-0001", persisted)
        self.assertNotIn("first-value-never-persisted", persisted)
        self.assertNotIn("second-value-never-persisted", persisted)

    def test_post_provider_failure_persists_uncertain_state_and_blocks_restart_replay(self):
        client = unittest.mock.Mock()
        client.mutate.return_value = {"id": "safe-id"}
        body = {
            "secret_name": "RESEND_API_KEY",
            "secret_value": "uncertain-value-never-persisted",
        }
        with patch.object(runtime, "InfisicalClient", return_value=client), patch.object(
            runtime,
            "_record_resend_rotation",
            side_effect=OSError("simulated post-provider state failure"),
        ):
            with self.assertRaises(runtime.ConnectionsError) as first_failure:
                runtime.ConnectionsRuntime(self.settings()).broker_mutate(
                    "rotate",
                    body,
                    principal="frank-vault-broker",
                    idempotency_key="restart-uncertain-key-0001",
                )
            self.assertEqual(first_failure.exception.error_code, "idempotency_uncertain")
            runtime._LEDGER = runtime._MutationLedger()
            with self.assertRaises(runtime.ConnectionsError) as raised:
                runtime.ConnectionsRuntime(self.settings()).broker_mutate(
                    "rotate",
                    body,
                    principal="frank-vault-broker",
                    idempotency_key="restart-uncertain-key-0001",
                )
        self.assertEqual(raised.exception.error_code, "idempotency_uncertain")
        client.mutate.assert_called_once()
        persisted = runtime._LEDGER._path().read_text(encoding="utf-8")
        self.assertIn('"state":"uncertain"', persisted)
        self.assertNotIn("restart-uncertain-key-0001", persisted)
        self.assertNotIn("uncertain-value-never-persisted", persisted)

    def test_corrupt_durable_ledger_fails_closed_before_provider_mutation(self):
        path = runtime._LEDGER._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"schema":"wrong","entries":{}}', encoding="utf-8")
        client = unittest.mock.Mock()
        with patch.object(runtime, "InfisicalClient", return_value=client):
            with self.assertRaises(runtime.ConnectionsError) as raised:
                runtime.ConnectionsRuntime(self.settings()).broker_mutate(
                    "create",
                    {"secret_name": "RESEND_API_KEY", "secret_value": "must-not-run"},
                    principal="frank-vault-broker",
                    idempotency_key="corrupt-ledger-key-0001",
                )
        self.assertEqual(raised.exception.error_code, "idempotency_store_unavailable")
        client.mutate.assert_not_called()

    def test_ledger_at_capacity_preserves_completed_replay_and_conflict_before_blocking_new_key(self):
        client = unittest.mock.Mock()
        client.mutate.return_value = {"id": "safe-id"}
        body = {"secret_name": "RESEND_API_KEY", "secret_value": "capacity-value-never-persisted"}
        with patch.object(runtime._MutationLedger, "_MAX_ENTRIES", 1), patch.object(
            runtime, "InfisicalClient", return_value=client
        ):
            first = runtime.ConnectionsRuntime(self.settings()).broker_mutate(
                "create",
                body,
                principal="frank-vault-broker",
                idempotency_key="capacity-existing-key-0001",
            )
            runtime._LEDGER = runtime._MutationLedger()
            replay = runtime.ConnectionsRuntime(self.settings()).broker_mutate(
                "create",
                body,
                principal="frank-vault-broker",
                idempotency_key="capacity-existing-key-0001",
            )
            with self.assertRaises(runtime.ConnectionsError) as conflict:
                runtime.ConnectionsRuntime(self.settings()).broker_mutate(
                    "create",
                    {"secret_name": "RESEND_API_KEY", "secret_value": "different-value-never-persisted"},
                    principal="frank-vault-broker",
                    idempotency_key="capacity-existing-key-0001",
                )
            with self.assertRaises(runtime.ConnectionsError) as full:
                runtime.ConnectionsRuntime(self.settings()).broker_mutate(
                    "create",
                    body,
                    principal="frank-vault-broker",
                    idempotency_key="capacity-new-key-0002",
                )
        self.assertEqual(replay, first)
        self.assertEqual(conflict.exception.error_code, "idempotency_conflict")
        self.assertEqual(full.exception.error_code, "idempotency_store_unavailable")
        self.assertEqual(full.exception.error_category, "unavailable")
        client.mutate.assert_called_once()
        payload = json.loads(runtime._LEDGER._path().read_text(encoding="utf-8"))
        self.assertEqual(len(payload["entries"]), 1)

    def test_ledger_at_capacity_preserves_uncertain_check_and_blocks_new_key(self):
        existing_body = {"secret_name": "RESEND_API_KEY", "secret_value": "uncertain-at-cap-never-persisted"}
        with patch.object(runtime._MutationLedger, "_MAX_ENTRIES", 1):
            runtime._LEDGER.begin(
                "frank-vault-broker", "rotate", "capacity-uncertain-key-0001", existing_body
            )
            runtime._LEDGER.mark_uncertain(
                "frank-vault-broker", "rotate", "capacity-uncertain-key-0001", existing_body
            )
            runtime._LEDGER = runtime._MutationLedger()
            with self.assertRaises(runtime.ConnectionsError) as uncertain:
                runtime._LEDGER.begin(
                    "frank-vault-broker", "rotate", "capacity-uncertain-key-0001", existing_body
                )
            with self.assertRaises(runtime.ConnectionsError) as full:
                runtime._LEDGER.begin(
                    "frank-vault-broker", "rotate", "capacity-new-key-0002", existing_body
                )
        self.assertEqual(uncertain.exception.error_code, "idempotency_uncertain")
        self.assertEqual(full.exception.error_code, "idempotency_store_unavailable")
        payload = json.loads(runtime._LEDGER._path().read_text(encoding="utf-8"))
        self.assertEqual(len(payload["entries"]), 1)

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
                        "delete", {"secret_name": "RESEND_API_KEY", "confirmation_token": "A" * 32, "provider_receipt": {"receipt_id": "b" * 32}}, principal="frank-vault-broker", idempotency_key="delete-fail-1"
                    )
            self.assertEqual(runtime.ConnectionsRuntime(settings).status()["providers"][0]["state"], "error")

    def test_resend_mcp_config_is_pinned_and_exactly_filtered(self):
        config = runtime.build_resend_mcp_config("secret-never-returned")
        self.assertEqual(config["args"], ["-y", "resend-mcp@2.13.0"])
        self.assertEqual(config["tools"]["include"], ["send-email", "get-email"])
        self.assertNotIn("secret-never-returned", json.dumps({k: v for k, v in config.items() if k != "env"}))

    def test_resend_mcp_ignores_unrelated_process_wide_tools_when_resend_fails(self):
        from tools import mcp_tool

        other = SimpleNamespace(
            _tools=[SimpleNamespace(name="unrelated")],
            _registered_tool_names=["mcp__other__unrelated"],
        )
        with tempfile.TemporaryDirectory() as tmp, patch.object(runtime, "get_hermes_home", return_value=Path(tmp)), patch.object(runtime, "_resend_was_rotated", return_value=True), patch.object(runtime.InfisicalClient, "read_value", return_value="runtime-secret"), patch.object(mcp_tool, "register_mcp_servers", return_value=["mcp__other__unrelated"]), patch.object(mcp_tool, "_servers", {"other": other}):
            result = runtime.ConnectionsRuntime(self.settings()).resend_mcp_tool({})
        self.assertTrue(any(state in result for state in ("setup_needed", "error")))
        self.assertNotIn("unrelated", result)

    def test_resend_mcp_success_projects_only_attributed_allowlisted_tools(self):
        from tools import mcp_tool

        ready = threading.Event()
        ready.set()
        resend = SimpleNamespace(
            _tools=[SimpleNamespace(name="send-email"), SimpleNamespace(name="get-email"), SimpleNamespace(name="unrelated")],
            _registered_tool_names=["mcp__resend__send_email", "mcp__resend__get_email", "mcp__resend__unrelated", "mcp__other__unrelated"],
            _error=None, session=object(), _ready=ready, _task=SimpleNamespace(done=lambda: False),
        )
        with tempfile.TemporaryDirectory() as tmp, patch.object(runtime, "get_hermes_home", return_value=Path(tmp)), patch.object(runtime, "_resend_was_rotated", return_value=True), patch.object(runtime.InfisicalClient, "read_value", return_value="runtime-secret"), patch.object(mcp_tool, "register_mcp_servers", return_value=["mcp__resend__send_email", "mcp__resend__get_email", "mcp__other__unrelated"]), patch.object(mcp_tool, "_servers", {"resend": resend}):
            result = json.loads(runtime.ConnectionsRuntime(self.settings()).resend_mcp_tool({}))
        self.assertEqual(result["registered_tools"], ["send-email", "get-email"])
        self.assertEqual(result["capabilities"], ["email.send", "email.status"])
        self.assertNotIn("unrelated", json.dumps(result))

    def test_stale_resend_registry_entry_is_not_connected(self):
        from tools import mcp_tool
        for attrs in (
            {"_error": RuntimeError("dead")},
            {"session": None},
            {"_ready": threading.Event()},
            {"_task": SimpleNamespace(done=lambda: True)},
        ):
            server = SimpleNamespace(
                _tools=[SimpleNamespace(name="send-email"), SimpleNamespace(name="get-email")],
                _registered_tool_names=["mcp__resend__send_email", "mcp__resend__get_email"],
                _error=None, session=object(), _ready=threading.Event(), _task=SimpleNamespace(done=lambda: False),
            )
            server._ready.set()
            for key, value in attrs.items():
                setattr(server, key, value)
            with patch.object(mcp_tool, "_servers", {"resend": server}):
                self.assertEqual(runtime._resend_registered_tool_names(mcp_tool), [])

    def test_vault_mutation_returns_frank_secret_metadata_envelope_without_value(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(runtime, "get_hermes_home", return_value=Path(tmp)):
            client = unittest.mock.Mock()
            client.mutate.return_value = {"id": "safe-id", "version": 7, "secretValue": "must-not-cross"}
            with patch.object(runtime, "InfisicalClient", return_value=client):
                for operation, key in (("create", "envelope-create-1"), ("rotate", "envelope-rotate-1")):
                    result = runtime.ConnectionsRuntime(self.settings()).broker_mutate(
                        operation, {"secret_name": "RESEND_API_KEY", "secret_value": "runtime-only-value"},
                        principal="frank-vault-broker", idempotency_key=key,
                    )
                    self.assertEqual(result["secret"]["version"], 7)
                    self.assertNotIn("metadata", result)
                    self.assertNotIn("secretValue", json.dumps(result))
                    self.assertNotIn("runtime-only-value", json.dumps(result))

    def test_nested_sensitive_payloads_are_rejected_before_transport(self):
        with self.assertRaises(runtime.ConnectionsError):
            runtime.sanitize_action_request({"action": "create", "target": {"provider": "resend"}, "body": {"name": "Resend", "notes": {"nested": {"api_key": "secret"}}}})
        with self.assertRaises(runtime.ConnectionsError):
            runtime.sanitize_action_request({"action": "create", "target": {"provider": "resend"}, "body": {"name": "Resend", "capabilities": [{"auth": {"token": "secret"}}]}})
        with self.assertRaises(runtime.ConnectionsError):
            runtime.sanitize_action_request({"plan_id": "plan-1234", "confirmation_token": "Bearer secret-token-value"})

    def test_provider_evidence_is_not_model_supplied(self):
        for field, value in (("provider_receipt", "hermes://receipt/forged"), ("provider_outcome", "verified"), ("provider_error_code", "auth"), ("provider_error_category", "auth")):
            with self.assertRaises(runtime.ConnectionsError):
                runtime.sanitize_action_request({"plan_id": "plan-1234", field: value})

    def test_private_inspect_uses_only_bounded_dedicated_path_and_safe_projection(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        response.read.return_value = json.dumps({
            "schema": "schema://frank.connections-agent-inspect/v1",
            "connections": [{"id": "connection-1234", "provider": "resend", "name": "Resend", "status": "setup_needed", "credential_ref": "vault://resend/key", "revision": 2}],
            "attention": [{"sequence": 4, "receipt_id": "a" * 32, "correlation_id": "correlation-1234", "action": "verify", "state": "pending", "target": {"provider": "resend", "connection_id": "connection-1234"}, "result": {"connection_id": "connection-1234", "provider": "resend", "status": "setup_needed", "pending": True}}],
            "activity": [{"sequence": 5, "receipt_id": "b" * 32, "correlation_id": "correlation-5678", "action": "verify", "state": "pending", "target": {"provider": "resend", "connection_id": "connection-1234"}, "result": {"connection_id": "connection-1234", "provider": "resend", "status": "setup_needed", "pending": True}}],
        }).encode()
        response.getcode.return_value = 200
        instance = runtime.ConnectionsRuntime(self.settings())
        with patch.object(runtime._NO_REDIRECT_OPENER, "open", return_value=response) as opener:
            result = json.loads(instance.inspect_tool({"activity_limit": 7}))
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, "https://frank.invalid/api/connections/agent/inspect?activity_limit=7")
        self.assertEqual(request.get_header("X-hermes-profile"), "default")
        self.assertEqual(result["connections"][0]["credential_ref"], "vault://resend/key")
        self.assertEqual(result["activity"][0]["result"]["pending"], True)
        self.assertNotIn("secretValue", json.dumps(result))
        self.assertIn("activity_limit", instance.inspect_tool({"activity_limit": 51}))

    def test_private_inspect_fails_closed_on_unknown_envelope_or_action_fields(self):
        valid = {
            "schema": "schema://frank.connections-agent-inspect/v1",
            "connections": [], "attention": [], "activity": [],
        }
        self.assertEqual(runtime.sanitize_inspect_response(valid, activity_limit=1)["schema"], valid["schema"])
        for payload in (
            {**valid, "extra": "no"},
            {**valid, "connections": [{"id": "connection-1234", "secretValue": "no"}]},
            {**valid, "attention": [{"sequence": 1, "action": "verify", "unsafe": "no"}]},
            {**valid, "activity": [{}]},
        ):
            self.assertEqual(runtime.sanitize_inspect_response(payload, activity_limit=1)["outcome"], "failed")

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

    def test_delete_evidence_requires_exact_frank_shapes_before_infisical(self):
        client = unittest.mock.Mock()
        client.delete.return_value = {"id": "safe"}
        invalid = [
            {"confirmation_token": "short", "provider_receipt": {"receipt_id": "a" * 32}},
            {"confirmation_token": "!" * 32, "provider_receipt": {"receipt_id": "a" * 32}},
            {"confirmation_token": "A" * 32, "provider_receipt": {"receipt_id": "A" * 32}},
            {"confirmation_token": "A" * 32, "provider_receipt": {"receipt_id": "a" * 31}},
            {"confirmation_token": "A" * 32, "provider_receipt": {"receipt_id": "a" * 32, "extra": "drop"}},
        ]
        with patch.object(runtime, "InfisicalClient", return_value=client):
            for index, evidence in enumerate(invalid):
                with self.assertRaises(runtime.ConnectionsError):
                    runtime.ConnectionsRuntime(self.settings()).broker_mutate(
                        "delete", {"secret_name": "RESEND_API_KEY", **evidence},
                        principal="frank-vault-broker", idempotency_key=f"delete-invalid-{index:02d}",
                    )
        client.delete.assert_not_called()

    def test_infisical_http_failures_keep_safe_status_mapping(self):
        for status, code, category in ((401, "infisical_auth_failed", "auth"), (403, "infisical_permission_denied", "permission_denied"), (404, "infisical_not_found", "not_found"), (429, "infisical_rate_limited", "rate_limited")):
            exc = urllib.error.HTTPError("https://infisical.invalid", status, "upstream body must not leak", {}, None)
            with patch.object(runtime._NO_REDIRECT_OPENER, "open", side_effect=exc):
                with self.assertRaises(runtime.ConnectionsError) as raised:
                    runtime.InfisicalClient(self.settings()).list_metadata()
            self.assertEqual(runtime.classify_failure(raised.exception), (code, category))
            self.assertNotIn("upstream body", json.dumps(runtime.failure_payload(raised.exception)))

    def test_invalid_infisical_secret_envelope_cannot_record_success(self):
        client = runtime.InfisicalClient(self.settings())
        for payload in ({}, {"secret": {}}, {"secret": {"secretValue": "never"}}):
            with self.assertRaises(runtime.ConnectionsError):
                client._safe_secret_metadata(payload)

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

    def test_persisted_connected_state_requires_live_process_registration_or_restore(self):
        from tools import mcp_tool
        with tempfile.TemporaryDirectory() as tmp, patch.object(runtime, "get_hermes_home", return_value=Path(tmp)), patch.object(mcp_tool, "_servers", {}), patch.object(runtime, "InfisicalClient", side_effect=runtime.SetupNeeded("missing")):
            settings = self.settings()
            runtime._record_resend_connection(settings, ["send-email", "get-email"])
            status = runtime.ConnectionsRuntime(settings).status()
        self.assertEqual(status["providers"][0]["state"], "error")

    def test_successful_restart_restore_projects_only_live_resend_tools(self):
        from tools import mcp_tool
        ready = threading.Event(); ready.set()
        resend = SimpleNamespace(
            _tools=[SimpleNamespace(name="send-email"), SimpleNamespace(name="get-email"), SimpleNamespace(name="unrelated")],
            _registered_tool_names=["mcp__resend__send_email", "mcp__resend__get_email", "mcp__other__unrelated"],
            _error=None, session=object(), _ready=ready, _task=SimpleNamespace(done=lambda: False),
        )
        settings = self.settings()
        with tempfile.TemporaryDirectory() as tmp, patch.object(runtime, "get_hermes_home", return_value=Path(tmp)), patch.object(mcp_tool, "_servers", {}), patch.object(runtime.InfisicalClient, "read_value", return_value="runtime-only") as read:
            runtime._record_resend_connection(settings, ["send-email", "get-email"])
            with patch.object(mcp_tool, "register_mcp_servers", side_effect=lambda _: setattr(mcp_tool, "_servers", {"resend": resend})):
                status = runtime.ConnectionsRuntime(settings).status()
            read.assert_called_once_with("RESEND_API_KEY")
        self.assertEqual(status["providers"][0]["state"], "connected-awaiting-verification")
        self.assertNotIn("unrelated", json.dumps(status))

    def test_completion_contract_redacts_provider_error_text(self):
        with self.assertRaises(runtime.ConnectionsError):
            runtime.sanitize_action_request({"plan_id": "plan-1234", "provider_outcome": "failed"})
        with self.assertRaises(runtime.ConnectionsError):
            runtime.sanitize_action_request({"plan_id": "plan-1234", "outcome": "failed"})

        response = runtime.sanitize_action_response({
            "action": {"action": "verify", "state": "failed", "result": {
                "outcome": "failed", "provider": "resend", "provider_receipt": {"receipt_id": "a" * 32},
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
            {"action": {"action": "verify", "state": "completed", "result": {"outcome": "verified", "provider": "resend", "provider_receipt": {"receipt_id": "a" * 32}, "connection_id": "connection-1234", "status": "verified"}}, "connection": {"id": "connection-1234", "provider": "resend", "name": "Resend", "scope_kind": "global", "scope_id": "", "status": "verified", "capabilities": ["email.send"], "credential_ref": "openbao://frank/resend"}},
        ]
        with patch.object(runtime_instance, "_frank_request", side_effect=responses) as request:
            planned = runtime_instance.request_tool({"action": "plan", "request": {"action": "verify", "target": {"provider": "resend", "connection_id": "connection-1234"}, "expected_revision": 1}, "idempotency_key": "plan-verify-0001"})
            self.assertIn("plan-verify-1234", planned)
            applied = runtime_instance.request_tool({"action": "apply", "plan_id": "plan-verify-1234", "request": {"plan_id": "plan-verify-1234"}, "idempotency_key": "apply-verify-0001"})
        self.assertIn("provider evidence", applied)
        self.assertNotIn("provider_receipt", request.call_args_list[1].args[1])
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

    def test_nonsecret_environment_settings_are_not_authoritative(self):
        with patch.dict(os.environ, {"HERMES_CONNECTIONS_FRANK_URL": "https://attacker.invalid"}, clear=False), patch.object(runtime, "_config_setting_without_context", side_effect=lambda key, default: {"frank_url": "https://canonical.invalid"}.get(key, default)):
            self.assertEqual(runtime.load_settings().frank_url, "https://canonical.invalid")

    def test_plugin_init_contains_no_mojibake_markers(self):
        source = (ROOT / "plugins" / "connections_agent" / "__init__.py").read_text(encoding="utf-8")
        for marker in ("\u00f0\u0178", "\u00e2\u0153", "\u00e2"):  # common UTF-8-as-Windows-1252 artifacts
            self.assertNotIn(marker, source)


def _read_state(tmp: str) -> str:
    return (Path(tmp) / "plugin-data" / "connections-agent" / "resend-state.json").read_text(encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
