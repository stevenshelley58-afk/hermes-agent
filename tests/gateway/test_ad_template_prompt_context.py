import copy
import pytest
from PIL import Image

import gateway.ad_template_generator_process as process
from tests.gateway.test_ad_template_generator_process import _review
from gateway.ad_template_reusable_validation import _scenario, reusable_repair_context

def test_review_response_room_preserves_deliberate_reasoning_and_complete_findings():
    from gateway.tool_run_api import _AD_TEMPLATE_GENERATOR_ROLE_OUTPUT_TOKENS as limits
    assert limits["comparator"] == limits["review"] == 16384
    for final in (False, True):
        prompt = process.review_prompt(final=final, candidate={}, reference={"sourcePlacement": "feed"}, metrics={})
        assert "Never omit an issue to fit or inflate a score" in prompt
        assert "at most 45 words" in prompt



def test_contract_diagnosis_is_once_per_run_and_uses_the_frozen_route(tmp_path, monkeypatch):
    calls, events = [], []
    diagnosis = {"diagnosis": "Plan the whole row", "nextChanges": ["Increase story-label height to 64 and maxLines to 2"], "capabilityBlockers": []}
    def call(*args, **kwargs):
        calls.append(kwargs)
        return diagnosis
    monkeypatch.setattr(process, "_call_json", call)
    checkpoint = {}
    value = {"template": {"textInputs": [{"key": "label", "placeholder": "Label", "maxLength": 28}], "imageInputs": [], "feedLayout": {"layers": []}, "storyLayout": {"layers": []}}}
    route = {"provider": "existing", "model": "diagnostic"}
    kwargs = dict(call_agent=None, candidate=value, reasons=["reusable scenario max failed: required height 64"], paths=["source"], route=route, checkpoint=checkpoint, workspace=tmp_path, emit=lambda *args: events.append(args))
    assert process._contract_repair_diagnosis(**kwargs) == diagnosis
    # Simulate a real process restart from the durable checkpoint.
    # A successful patch writes a new stage snapshot before that restart.
    process.persist_checkpoint(tmp_path, {"candidate": value, "accepted": False})
    kwargs["checkpoint"] = process.load_checkpoint(tmp_path)
    assert process._contract_repair_diagnosis(**kwargs) == diagnosis
    assert len(calls) == 1 and calls[0]["route"] == route
    assert "Maximum editable content" in calls[0]["prompt"]
    assert "required height 64" in calls[0]["prompt"]
    assert events[-1][0] == "contract-repair.diagnosis-completed"


def test_contract_diagnosis_transport_failure_is_not_repeated_but_budget_errors_propagate(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise process.AdTemplateTransportError("deadline")
    monkeypatch.setattr(process, "_call_json", fail)
    kwargs = dict(call_agent=None, candidate={"template": {"textInputs": [], "imageInputs": [], "feedLayout": {"layers": []}, "storyLayout": {"layers": []}}}, reasons=["reusable scenario max failed"], paths=[], route={"provider":"existing"}, checkpoint={}, workspace=tmp_path, emit=lambda *args: None)
    assert process._contract_repair_diagnosis(**kwargs) is None
    assert kwargs["checkpoint"]["contractRepairDiagnosisRequested"] is True
    process.persist_checkpoint(tmp_path, {"candidate": kwargs["candidate"]})
    kwargs["checkpoint"] = process.load_checkpoint(tmp_path)
    def budget(*args, **kwargs):
        raise process.AdTemplateProcessError("run cost limit exceeded")
    monkeypatch.setattr(process, "_call_json", budget)
    assert process._contract_repair_diagnosis(**kwargs) is None
    kwargs["checkpoint"] = {}
    with pytest.raises(process.AdTemplateProcessError, match="cost limit"):
        process._contract_repair_diagnosis(**kwargs)


def test_review_labels_stay_adjacent_to_their_actual_images(tmp_path):
    names = ["source.png", "source-canvas.png", "iteration-03-feed.png", "iteration-03-story.png",
             "iteration-03-feed-difference.png", "iteration-01-feed.png", "iteration-01-story.png"]
    paths = []
    for index, name in enumerate(names):
        path = tmp_path / name
        Image.new("RGB", (8, 8), (index, 0, 0)).save(path)
        paths.append(str(path))
    calls = []
    def agent(instance, message, route):
        calls.append(message)
        return {"ok": True}
    result = process._call_json(
        agent, instance="comparator-3", prompt="Review current", paths=paths,
        baseline_paths=paths[-2:], route={"provider": "test", "model": "test"},
        validate=lambda value: value, emit=lambda *args: None,
    )
    assert result == {"ok": True}
    message = calls[0]
    assert len([p for p in message if p["type"] == "image_url"]) == len(paths)
    for index, name in enumerate(names):
        label, pixels = message[1 + index * 2:3 + index * 2]
        assert name in label["text"]
        assert pixels["type"] == "image_url"
        expected = "ORIGINAL SOURCE" if index < 2 else "CURRENT CANDIDATE" if index < 4 else "DIAGNOSTIC ONLY" if index == 4 else "SAVED BASELINE ONLY"
        assert expected in label["text"]


def test_tied_draft_can_advance_only_with_nonregressing_scores_and_fewer_defects():
    best = _review(accept=False)
    best["scores"] = {key: 8.9 for key in process.SCORE_FIELDS}
    best["issues"] = [copy.deepcopy(best["issues"][0]) for _ in range(3)]
    current = copy.deepcopy(best)
    current["scores"] = {key: 9.4 for key in process.SCORE_FIELDS}
    current["issues"] = current["issues"][:1]
    assert process._tied_review_dominates(current, best)
    # Selection does not manufacture a pass or mutate either review.
    assert process.validate_review(current)["decision"] == "revise"
    before = copy.deepcopy((current, best))
    assert process._tied_review_dominates(current, best)
    assert (current, best) == before
    regressed = copy.deepcopy(current)
    regressed["scores"]["typography"] = 8.8
    assert not process._tied_review_dominates(regressed, best)
    same_defects = copy.deepcopy(current)
    same_defects["issues"] = copy.deepcopy(best["issues"])
    assert not process._tied_review_dominates(same_defects, best)
    blocker = copy.deepcopy(current)
    blocker["issues"][0]["severity"] = "blocker"
    assert not process._tied_review_dominates(blocker, best)
    effect = copy.deepcopy(current)
    effect["effects"]["masks"] = "mismatch"
    assert not process._tied_review_dominates(effect, best)


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


def test_contract_repair_receives_actual_replacement_payloads_without_mutation():
    candidate = {"template": {"textInputs": [{"key": "copy", "placeholder": "Original", "maxLength": 40}],
                              "imageInputs": [], "feedLayout": {"layers": []}, "storyLayout": {"layers": []}}}
    original = copy.deepcopy(candidate)
    context = reusable_repair_context(candidate)
    for scenario, payload in context.items():
        assert payload["copy"] == _scenario(candidate, scenario)["template"]["textInputs"][0]["placeholder"]
    prompt = process.contract_repair_prompt(candidate=candidate, reasons=["reusable scenario max failed"])
    assert context["max"]["copy"] in prompt
    assert "maxLines AND geometry" in prompt
    assert "Story minimum 32px" in prompt
    assert "lowering maxLength/maxCharacters" in prompt
    assert candidate == original


def test_review_distinguishes_unused_box_capacity_from_visible_ink():
    prompt = process.review_prompt(final=False, candidate={"template": {}}, reference={"sourcePlacement": "feed"}, metrics={})
    assert "absolute positions, not document flow" in prompt
    assert "Never shorten an invisible geometry/height" in prompt
    assert "fit-safe alternative" in prompt
    assert "9.8" in prompt


def test_best_context_does_not_duplicate_current_document():
    prompt = process._best_repair_context(
        best_candidate={"template": {"sentinel": "LARGE-CONTRACT-ALREADY-SUPPLIED"}},
        best_iteration=3, diagnosis={"diagnosis": "Keep capacity", "nextChanges": [], "capabilityBlockers": []},
        document_already_supplied=True,
    )
    assert "LARGE-CONTRACT-ALREADY-SUPPLIED" not in prompt
    assert "Keep capacity" in prompt


def test_optional_final_diagnosis_transport_failure_preserves_repair_path(monkeypatch):
    events = []
    def fail(*args, **kwargs):
        raise process.AdTemplateTransportError("diagnosis deadline exceeded")
    monkeypatch.setattr(process, "_call_json", fail)
    assert process._optional_final_diagnosis(None, emit=lambda *args: events.append(args)) is None
    assert events[0][0] == "final-repair.diagnosis-failed"
    assert events[0][2]["continuing_with_bounded_repairs"] is True


def test_optional_diagnosis_does_not_swallow_budget_or_process_errors(monkeypatch):
    def fail(*args, **kwargs):
        raise process.AdTemplateProcessError("run cost limit exceeded")
    monkeypatch.setattr(process, "_call_json", fail)
    with pytest.raises(process.AdTemplateProcessError, match="cost limit"):
        process._optional_final_diagnosis(None, emit=lambda *args: None)
