import unittest
from pathlib import Path


SKILL = Path(__file__).resolve().parents[2] / "optional-skills" / "productivity" / "connections-agent" / "SKILL.md"


class ConnectionsSkillTests(unittest.TestCase):
    def test_skill_has_safe_frontmatter_and_runtime_contract(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        self.assertIn("name: connections-agent", text)
        self.assertIn("description: Plan and execute safe provider connection changes.", text)
        self.assertIn("profile `default`", text)
        self.assertIn("setup_needed", text)
        self.assertIn("viewSecretValue=false", text)
        self.assertIn("email.send", text)
        self.assertIn("email.status", text)
        self.assertIn("Luna", text)


if __name__ == "__main__":
    unittest.main()
