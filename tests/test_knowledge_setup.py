import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_cli import knowledge_setup


class KnowledgeSetupTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "knowledge.env"
        self.lock = Path(self.tempdir.name) / "setup.lock"
        self.paths = patch.multiple(
            knowledge_setup,
            KNOWLEDGE_LOCK_FILE=self.lock,
        )
        self.paths.start()

    def tearDown(self):
        self.paths.stop()
        self.tempdir.cleanup()

    def test_save_is_atomic_and_redacts_api_key_from_status(self):
        result = knowledge_setup.save_user_settings("test-api-key", self.path)
        self.assertTrue(result["configured"])
        self.assertNotIn("test-api-key", repr(result))
        self.assertEqual(result["namespace"], "project/frank")
        self.assertEqual(knowledge_setup.parse_env_file(self.path)["OPENAI_API_KEY"], "test-api-key")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_fixed_namespace_and_image_cannot_be_overridden(self):
        with self.assertRaises(knowledge_setup.KnowledgeSetupError):
            knowledge_setup.validate_env_value("HERMES_ALLOWED_NAMESPACES", "project/other")
        with self.assertRaises(knowledge_setup.KnowledgeSetupError):
            knowledge_setup.validate_env_value("NEO4J_IMAGE", "neo4j:latest")

    def test_duplicate_and_control_values_fail_closed(self):
        self.path.write_text("OPENAI_API_KEY=one\nOPENAI_API_KEY=two\n", encoding="utf-8")
        with self.assertRaises(knowledge_setup.KnowledgeSetupError):
            knowledge_setup.parse_env_file(self.path)

    def test_crlf_and_symlink_settings_fail_closed(self):
        self.path.write_bytes(b"OPENAI_API_KEY=one\r\n")
        with self.assertRaises(knowledge_setup.KnowledgeSetupError):
            knowledge_setup.parse_env_file(self.path)
        self.path.unlink()
        self.path.symlink_to(Path(self.tempdir.name) / "other")
        with self.assertRaises(knowledge_setup.KnowledgeSetupError):
            knowledge_setup.save_user_settings("one", self.path)
        self.path.unlink()
        self.path.write_text("OPENAI_API_KEY=one\nNEO4J_IMAGE=neo4j@sha256:\u0001\n", encoding="utf-8")
        with self.assertRaises(knowledge_setup.KnowledgeSetupError):
            knowledge_setup.parse_env_file(self.path)

    def test_secret_parent_must_be_private_and_real(self):
        self.path.parent.chmod(0o755)
        try:
            with self.assertRaises(knowledge_setup.KnowledgeSetupError):
                knowledge_setup.save_user_settings("one", self.path)
        finally:
            self.path.parent.chmod(0o700)
        target = Path(self.tempdir.name) / "target"
        target.mkdir(mode=0o700)
        linked_parent = Path(self.tempdir.name) / "linked"
        linked_parent.symlink_to(target, target_is_directory=True)
        with self.assertRaises(knowledge_setup.KnowledgeSetupError):
            knowledge_setup.save_user_settings("one", linked_parent / "knowledge.env")

    def test_csrf_is_single_use_and_session_bound(self):
        store = {}
        token = knowledge_setup.mint_csrf("session-a", store, 1.0)
        self.assertTrue(knowledge_setup.consume_csrf(token, "session-a", store, 2.0))
        self.assertFalse(knowledge_setup.consume_csrf(token, "session-a", store, 2.0))
        token = knowledge_setup.mint_csrf("session-a", store, 1.0)
        self.assertFalse(knowledge_setup.consume_csrf(token, "session-b", store, 2.0))


if __name__ == "__main__":
    unittest.main()
