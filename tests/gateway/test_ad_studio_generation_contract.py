import hashlib
import json
from unittest.mock import patch

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


def test_candidate_uses_private_authoritative_trace_over_lossy_summary(tmp_path):
    run_id = "trun_authoritative_trace"
    hermes_home = tmp_path / "hermes"
    run_root = hermes_home / "tool_assets" / "ad-template-generator" / "runs" / run_id
    preview_root = run_root / "previews"
    preview_root.mkdir(parents=True)
    feed = preview_root / "meta-feed-006-feed.png"
    story = preview_root / "meta-feed-006-story.png"
    feed.write_bytes(b"feed-preview")
    story.write_bytes(b"story-preview")
    feed_hash = hashlib.sha256(feed.read_bytes()).hexdigest()
    story_hash = hashlib.sha256(story.read_bytes()).hexdigest()

    candidate = run_root / "evidence" / "template.json"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("{}", encoding="utf-8")
    accepted = record()
    accepted["artifacts"]["feedSha256"] = feed_hash
    accepted["artifacts"]["storySha256"] = story_hash
    (run_root / "generation-trace.json").write_text(json.dumps({
        "schema": "adstudio.generation-trace.v1",
        "templateId": "template-006",
        "status": "accepted",
        "generations": [accepted],
    }), encoding="utf-8")

    output = {
        "template_id": "template-006",
        "candidate_ref": str(candidate),
        "preview_refs": [str(feed), str(story)],
        "evidence_refs": {"generation": "private"},
        "generations": [{"scores": {"primaryAdSystemLikeness": None}}],
        "qa_summary": {
            "source_verified": True,
            "deterministic_check": "passed",
            "subject_invariance_gate": True,
            "release_status": "blocked_pending_human_approval",
        },
    }
    with patch("hermes_constants.get_hermes_home", return_value=hermes_home):
        prepared = ToolRunAPIMixin._prepare_candidate_output(run_id, output)

    assert prepared["generations"][0]["scores"]["primaryAdSystemLikeness"] == 9.8
    assert prepared["generationTrace"]["status"] == "accepted"
    assert prepared["generation_contract"]["validated"] is True
    assert prepared["qa"]["visual_review"] == prepared["generations"][-1]

