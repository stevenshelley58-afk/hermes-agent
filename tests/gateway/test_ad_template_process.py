from gateway.ad_template_process import AdTemplateProcessError, deterministic_documents, validate_final_review, validate_iterations
import pytest

def iteration(score=9.4, number=1):
    return {"iteration": number, "comparison": {"score": score, "reason": "Improve spacing"}, "decision": "accepted" if score >= 9.5 else "revise"}

def test_one_comparator_per_iteration_and_final_review_only_after_pass():
    assert validate_iterations([iteration()])[0]["comparison"]["score"] == 9.4
    with pytest.raises(AdTemplateProcessError):
        validate_iterations([{"iteration": 1, "comparison": {"score": 9.4, "reason": "x"}, "reviewers": []}])
    review = validate_final_review({"reviewers": [{"id": "reviewer-a", "score": 9.6, "reason": "good"}, {"id": "reviewer-b", "score": 9.7, "reason": "good"}]}, accepted=True)
    assert review["decision"] == "accepted"

def test_final_review_requires_two_independent_reviewers():
    with pytest.raises(AdTemplateProcessError):
        validate_final_review({"reviewers": [{"id": "same", "score": 10, "reason": "ok"}, {"id": "same", "score": 10, "reason": "ok"}]}, accepted=True)

def test_documents_are_stable():
    template = {"story": {"z": 1, "a": "x"}, "feed": {"b": 2}}
    first = deterministic_documents(template)
    assert first == deterministic_documents({"feed": {"b": 2}, "story": {"a": "x", "z": 1}})
    assert list(first) == ["feed.json", "story.json", "template.json"]
