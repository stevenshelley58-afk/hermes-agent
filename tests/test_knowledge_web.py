import tempfile
import unittest
import time
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_cli import knowledge_setup, web_server
from fastapi import HTTPException


class _Request:
    def __init__(self, headers):
        self.headers = headers
        self.state = SimpleNamespace(session=None)


class KnowledgeWebSecurityTests(unittest.TestCase):
    def _request(self, *, csrf="", origin="https://hermes.example", host="hermes.example", fetch_site="same-origin", idem="knowledge-test-idempotency-1"):
        return _Request({
            "origin": origin,
            "host": host,
            "sec-fetch-site": fetch_site,
            "X-Hermes-CSRF": csrf,
            "Idempotency-Key": idem,
        })

    def test_origin_and_fetch_site_are_exact(self):
        self.assertTrue(web_server._knowledge_origin_is_same(self._request()))
        self.assertFalse(web_server._knowledge_origin_is_same(self._request(origin="null")))
        self.assertFalse(web_server._knowledge_origin_is_same(self._request(fetch_site="cross-site")))
        self.assertFalse(web_server._knowledge_origin_is_same(self._request(fetch_site="")))
        self.assertFalse(web_server._knowledge_origin_is_same(self._request(host="other.example")))

    def test_csrf_is_session_bound_single_use_and_idempotent(self):
        web_server._KNOWLEDGE_CSRF_TOKENS.clear()
        web_server._KNOWLEDGE_IDEMPOTENCY.clear()
        request = self._request()
        now = time.monotonic()
        token = knowledge_setup.mint_csrf(
            web_server._knowledge_csrf_token(request),
            web_server._KNOWLEDGE_CSRF_TOKENS,
            now,
        )
        request.headers["X-Hermes-CSRF"] = token
        with patch.object(web_server, "_require_token"):
            web_server._knowledge_mutation_guard(request)
            with self.assertRaises(HTTPException) as replay:
                web_server._knowledge_mutation_guard(request)
        self.assertEqual(replay.exception.status_code, 403)

    def test_wrong_release_receipt_fails_before_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "approved-sha"
            receipt.write_text("wrong\n", encoding="ascii")
            with patch.object(knowledge_setup, "APPROVED_FRANK_RECEIPT", receipt):
                with patch("hermes_cli.web_server.subprocess.run") as run:
                    self.assertFalse(web_server._run_knowledge_helper(Path("/usr/bin/true"), timeout=1))
                    run.assert_not_called()

    def test_helper_contract_has_no_user_arguments_or_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "approved-sha"
            receipt.write_text(knowledge_setup.APPROVED_FRANK_SHA + "\n", encoding="ascii")
            helper_receipt = Path(directory) / "frank-knowledge-helper.sha256"
            true_path = Path("/usr/bin/true")
            digest = hashlib.sha256(true_path.read_bytes()).hexdigest()
            helper_receipt.write_text(digest + "\n", encoding="ascii")
            with patch.object(knowledge_setup, "APPROVED_FRANK_RECEIPT", receipt), \
                 patch.object(knowledge_setup, "APPROVED_FRANK_HELPER_RECEIPT", helper_receipt), \
                 patch.object(knowledge_setup, "DEPLOY_HELPER", true_path), \
                 patch.object(knowledge_setup, "APPROVED_FRANK_HELPER_SHA256", digest):
                with patch("hermes_cli.web_server.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
                    self.assertTrue(web_server._run_knowledge_helper(true_path, timeout=1))
                    args = run.call_args.args[0]
                    self.assertEqual(args, ["sudo", "-n", "/usr/bin/true"])
                    env = run.call_args.kwargs["env"]
                    self.assertEqual(set(env), {"PATH", "LANG", "HERMES_HOME"})
                    self.assertIs(run.call_args.kwargs["stdout"], web_server.subprocess.DEVNULL)
                    self.assertIs(run.call_args.kwargs["stderr"], web_server.subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
