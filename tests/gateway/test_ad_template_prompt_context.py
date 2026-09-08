import copy

import gateway.ad_template_generator_process as process
from tests.gateway.test_ad_template_generator_process import _review


def test_revision_memory_exposes_criticism_and_attempts_without_score_anchoring():
    old = {
        "iteration": 1, "discarded": True,
        "comparison": {
            "scores": {"overall": 9.99},
            "issues": [{"instruction": "obsolete-first", "targets": []}],
        },
        "refinement": {"operations": [{"op": "replace", "path": "/template/feedLayout/layers/1/geometry/y", "value": 70}]},
    }
    records = [copy.deepcopy(old) for _ in range(4)]
    records[-1]["comparison"]["issues"][0]["instruction"] = "Current label is above its button"
    records[1]["comparison"]["issues"][0]["instruction"] = "second"
    records[2]["comparison"]["issues"][0]["instruction"] = "third"
    diagnosis = {"diagnosis": "The source CTA is rectangular", "nextChanges": ["Move label, not button"], "capabilityBlockers": []}
    before = copy.deepcopy(records)
    prompt = process._review_iteration_context(records, diagnosis)
    assert "obsolete-first" not in prompt
    assert "Current label is above its button" in prompt
    assert '"value":70' in prompt
    assert "The source CTA is rectangular" in prompt
    assert "Move label, not button" in prompt
    assert "9.99" not in prompt
    assert records == before
    assert process._review_iteration_context([], None) == ""


def test_large_historical_repairs_cannot_overflow_new_comparator_context():
    prompt = process._review_iteration_context([{
        "iteration": 1,
        "refinement": {"operations": [{"value": "x" * 200_000}]},
    }], {"diagnosis": "Keep the source geometry", "nextChanges": [], "capabilityBlockers": []})
    assert len(prompt.encode()) < 25_000
    assert "Keep the source geometry" in prompt


def test_pairwise_baseline_does_not_duplicate_full_template_or_prior_scores():
    candidate = {"template": {"privateMarker": "FULL-CONTRACT-SENT-ONCE"}}
    prompt = process._pairwise_review_context(best_iteration=2, best_candidate=candidate)
    assert "BEST Feed render" in prompt
    assert "FULL-CONTRACT-SENT-ONCE" not in prompt
    assert process._pairwise_review_context(best_iteration=0, best_candidate=None) == ""


def test_fact_first_prompt_keeps_threshold_and_production_defect_gate(monkeypatch):
    monkeypatch.setattr(process, "_available_font_files", lambda: ["unused-font-list-sentinel"])
    prompt = process.review_prompt(final=True, candidate={"template": {}},
                                   reference={"sourcePlacement": "feed"}, metrics={})
    assert "unused-font-list-sentinel" not in prompt
    assert "FACT-FIRST REVIEW" in prompt
    assert "font-family identity is excluded" in prompt
    assert "minimum font sizes: Feed 24px and Story 32px" in prompt
    # Prompt revisions never relax actual acceptance: high scores cannot excuse
    # a visible CTA defect, and a single 9.79 section must still revise.
    defect = _review(accept=False)
    defect["scores"] = {key: 9.9 for key in process.SCORE_FIELDS}
    assert process.validate_review(defect)["decision"] == "revise"
    low = _review(accept=True)
    low["scores"]["details"] = 9.79
    low["issues"] = defect["issues"]
    assert process.validate_review(low)["decision"] == "revise"
