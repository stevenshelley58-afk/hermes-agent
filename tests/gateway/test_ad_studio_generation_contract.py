import pytest

from gateway.tool_run_api import ToolRunAPIMixin
from gateway.tool_runs import (
    TOOL_RUN_COMMAND_SCHEMA,
    ToolRunStore,
    ToolRunError,
    validate_generation_records,
)


FEED = "a" * 64
STORY = "b" * 64
RENDER_SET = "c" * 64


def record(*, primary="reviewer.primary", strict="reviewer.strict", primary_score=9.8, strict_score=9.6):
    return {
        "iteration": 1,
        "artifacts": {"feedSha256": FEED, "storySha256": STORY, "renderSetSha256": RENDER_SET},
        "reviewers": {"primary": primary, "strict": strict},
        "scores": {
            "primaryAdSystemLikeness": primary_score,
            "strictAdSystemLikeness": strict_score,
        },
        "threshold": 9.5,
        "decision": "accepted" if primary_score >= 9.5 and strict_score >= 9.5 else "revise",
        "revisionReason": "Accepted after independent visual review.",
    }


def command():
    return {
        "schema": TOOL_RUN_COMMAND_SCHEMA,
        "request_id": "generation-request",
        "tool_id": "ad-template-generator",
        "action": "build-template",
        "scope": {"project_id": "blockwise"},
        "payload": {"sources": [{"ref": "source:one"}]},
        "idempotency_key": "generation-run",
        "model_policy_revision": 1,
    }


def test_generation_records_require_independent_review_and_current_hashes():
    validated = validate_generation_records([record()], feed_sha256=FEED, story_sha256=STORY)
    assert validated[0]["artifacts"]["renderSetSha256"] == RENDER_SET

    with pytest.raises(ToolRunError, match="independent"):
        validate_generation_records([record(strict="reviewer.primary")])
    with pytest.raises(ToolRunError, match="stale"):
        validate_generation_records([record()], feed_sha256="d" * 64, story_sha256=STORY)


def test_controller_gate_requires_dual_9_5_scores_and_current_artifacts():
    output = {"generations": [record()], "current_artifacts": {"feedSha256": FEED, "storySha256": STORY}}
    gate = ToolRunAPIMixin._validated_generation_gate(output)
    assert gate["generation"]["reviewers"] == {"primary": "reviewer.primary", "strict": "reviewer.strict"}

    low = record(strict_score=9.4)
    low_output = {"generations": [low], "current_artifacts": {"feedSha256": FEED, "storySha256": STORY}}
    with pytest.raises(ToolRunError, match="at least 9.5"):
        ToolRunAPIMixin._validated_generation_gate(low_output)


def test_generation_records_project_as_replayable_safe_events(tmp_path):
    store = ToolRunStore(str(tmp_path / "tool-runs.db"))
    run, _ = store.create_run(command())
    adapter = ToolRunAPIMixin.__new__(ToolRunAPIMixin)
    adapter._tool_run_store = store
    adapter._project_generation_events(run["run_id"], [record()])
    events = store.events(run["run_id"])
    assert [event["kind"] for event in events[-4:]] == [
        "generation.started", "generation.rendered", "generation.scored", "generation.accepted",
    ]
    scored = events[-2]["data"]
    assert scored["primary_reviewer"] != scored["strict_reviewer"]
    assert "prompt" not in scored
    store.close()


def test_declared_pipeline_contains_render_and_visual_review():
    assert ToolRunAPIMixin._tool_stage_order()[5:7] == ["render", "visual-review"]

