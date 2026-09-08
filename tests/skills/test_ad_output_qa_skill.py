"""Packaging and reusable evidence-case contracts for the ad review skill."""
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2] / "skills" / "creative" / "ad-output-qa"


def test_skill_is_discoverable_and_references_shipped_checklist():
    text = (ROOT / "SKILL.md").read_text()
    assert text.startswith("---\nname: ad-output-qa\n")
    description = re.search(r"^description: (.*)$", text, re.M).group(1)
    assert len(description) <= 60 and description.endswith(".")
    checklist = ROOT / "references" / "review-checklist.md"
    assert "references/review-checklist.md" in text and checklist.is_file()
    assert len(checklist.read_text()) > 200
    sections = ["When to Use", "Prerequisites", "How to Run", "Quick Reference",
                "Procedure", "Pitfalls", "Verification"]
    offsets = [text.index("## " + section) for section in sections]
    assert offsets == sorted(offsets)


def test_evidence_review_cases_have_unique_identity_and_gradeable_expectations():
    data = json.loads((ROOT / "evals" / "evals.json").read_text())
    assert data["skill_name"] == "ad-output-qa"
    cases = data["evals"]
    assert len({case["id"] for case in cases}) == len(cases)
    for case in cases:
        assert case["prompt"].strip() and case["expected_output"].strip()
        assert case["expectations"] and all(case["expectations"])
        for filename in case["files"]:
            assert (ROOT / filename).is_file()
