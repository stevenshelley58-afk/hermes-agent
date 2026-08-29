import json
import sqlite3

import pytest

from gateway.tool_runs import (
    TOOL_MODEL_POLICY_SCHEMA,
    TOOL_RUN_COMMAND_SCHEMA,
    ToolRunError,
    ToolRunStore,
    default_ad_template_policy,
)


def command(**changes):
    value = {
        "schema": TOOL_RUN_COMMAND_SCHEMA,
        "request_id": "req-1",
        "tool_id": "ad-template-generator",
        "action": "build-template",
        "scope": {"project_id": "blockwise"},
        "payload": {"job_name": "August ads", "sources": [{"ref": "source:abc", "sha256": "a" * 64}]},
        "idempotency_key": "job-1",
        "model_policy_revision": 1,
    }
    value.update(changes)
    return value


def test_run_is_durable_and_idempotent(tmp_path):
    path = tmp_path / "tool-runs.db"
    first = ToolRunStore(str(path))
    run, created = first.create_run(command())
    assert created is True
    again, created_again = first.create_run(command(request_id="req-2"))
    assert created_again is False
    assert again["run_id"] == run["run_id"]
    first.append_event(run["run_id"], "stage.started", status="running", node_id="analyse", data={"summary": "Reading source"})
    first.update_run(run["run_id"], status="running", stage="analyse", progress=0.1)
    first.close()

    reopened = ToolRunStore(str(path))
    durable = reopened.get_run(run["run_id"])
    assert durable["status"] == "running"
    assert durable["stage"] == "analyse"
    assert [event["sequence"] for event in reopened.events(run["run_id"])] == [0, 1]
    reopened.close()


def test_replay_cursor_returns_only_missing_events(tmp_path):
    store = ToolRunStore(str(tmp_path / "events.db"))
    run, _ = store.create_run(command())
    for index in range(3):
        store.append_event(run["run_id"], "stage.progress", status="running", node_id="analyse", data={"index": index})
    replay = store.events(run["run_id"], after=1)
    assert [event["sequence"] for event in replay] == [2, 3]
    assert [event["data"]["index"] for event in replay] == [1, 2]


def test_restart_requeues_interrupted_runs_with_checkpoint(tmp_path):
    store = ToolRunStore(str(tmp_path / "recover.db"))
    run, _ = store.create_run(command())
    store.update_run(run["run_id"], status="running", stage="decompose", progress=0.25)
    recovered = store.recover_incomplete()
    assert recovered[0]["status"] == "queued"
    assert recovered[0]["stage"] == "decompose"
    assert recovered[0]["attention"] is True
    assert store.events(run["run_id"])[-1]["kind"] == "run.recovered"


def test_model_policy_revisions_are_immutable_and_run_pinned(tmp_path):
    store = ToolRunStore(str(tmp_path / "policy.db"))
    base = store.get_policy("ad-template-generator")
    assert base["revision"] == 1
    changed = default_ad_template_policy()
    changed["name"] = "Cheaper current models"
    changed["stages"]["analyse"]["primary"] = {"provider": "google", "model": "gemini-3.6-flash"}
    revision = store.create_policy("ad-template-generator", changed)
    assert revision["revision"] == 2
    run, _ = store.create_run(command(model_policy_revision=2))
    assert run["model_policy_revision"] == 2
    assert run["model_policy"]["stages"]["analyse"]["primary"]["model"] == "gemini-3.6-flash"
    assert store.get_policy("ad-template-generator", 1)["policy"]["name"] == "Sole ad-template process"


@pytest.mark.skip(reason="legacy generator policy migration is removed")
def test_legacy_seed_is_superseded_without_rewriting_revision_one(tmp_path):
    path = tmp_path / "seed-migration.db"
    store = ToolRunStore(str(path))
    legacy = default_ad_template_policy()
    legacy.pop("seed_revision", None)
    for stage_id in ("analyse", "visual-qa"):
        legacy["stages"][stage_id]["primary"]["provider"] = "openai"
        legacy["stages"][stage_id]["fallbacks"][0]["provider"] = "google"
    for stage_id in ("masked-text-cleanup", "story-extend"):
        legacy["stages"][stage_id]["primary"]["provider"] = "google"
    legacy["stages"]["masked-text-cleanup"]["fallbacks"][0]["provider"] = "google"
    legacy["stages"]["masked-text-cleanup"]["fallbacks"][1]["provider"] = "openai"
    legacy["stages"]["story-extend"]["fallbacks"][0]["provider"] = "openai"
    store._conn.execute(
        "UPDATE tool_model_policies SET policy_json=? WHERE tool_id='ad-template-generator' AND revision=1",
        (json.dumps(legacy, separators=(",", ":"), sort_keys=True),),
    )
    store._conn.commit()
    store.close()

    migrated = ToolRunStore(str(path))
    assert migrated.get_policy("ad-template-generator", 1)["policy"]["stages"]["analyse"]["primary"]["provider"] == "openai"
    current = migrated.get_policy("ad-template-generator")
    assert current["revision"] == 2
    assert current["policy"]["stages"]["analyse"]["primary"]["provider"] == "openai-codex"
    assert current["policy"]["seed_revision"] == 2


def test_one_run_override_creates_new_revision(tmp_path):
    store = ToolRunStore(str(tmp_path / "override.db"))
    override = default_ad_template_policy()
    override["name"] = "One run"
    run, _ = store.create_run(command(model_policy_revision=None, model_policy_override=override))
    assert run["model_policy_revision"] == 2
    assert store.get_policy("ad-template-generator", 2)["policy"]["name"] == "One run"
    assert store.get_policy("ad-template-generator")["revision"] == 1


def test_secret_bearing_payload_is_rejected(tmp_path):
    store = ToolRunStore(str(tmp_path / "secret.db"))
    with pytest.raises(ToolRunError, match="secret-bearing"):
        store.create_run(command(payload={"api_key": "do-not-store"}))


def test_numeric_usage_counts_are_not_mistaken_for_credentials(tmp_path):
    store = ToolRunStore(str(tmp_path / "usage.db"))
    run, _ = store.create_run(command())
    store.update_run(run["run_id"], output={"usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18}})
    assert store.get_run(run["run_id"])["output"]["usage"]["total_tokens"] == 18


def test_invalid_model_candidate_is_rejected(tmp_path):
    store = ToolRunStore(str(tmp_path / "invalid.db"))
    invalid = {
        "schema": TOOL_MODEL_POLICY_SCHEMA,
        "tool_id": "ad-template-generator",
        "name": "broken",
        "stages": {"analyse": {"capability": "vision_structured", "primary": {"provider": "openai", "model": ""}}},
    }
    with pytest.raises(ToolRunError, match="model must"):
        store.create_policy("ad-template-generator", invalid)


def test_tool_run_db_never_adds_chat_session_rows(tmp_path):
    path = tmp_path / "tool-runs.db"
    store = ToolRunStore(str(path))
    store.create_run(command())
    store.close()
    conn = sqlite3.connect(path)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "sessions" not in tables
    assert "messages" not in tables


def test_project_defaults_are_isolated_and_fall_back_to_global(tmp_path):
    store = ToolRunStore(str(tmp_path / "projects.db"))
    blockwise = default_ad_template_policy()
    blockwise["name"] = "Blockwise balanced"
    saved = store.create_policy("ad-template-generator", blockwise, project_id="blockwise")
    assert store.get_policy("ad-template-generator", project_id="blockwise")["revision"] == saved["revision"]
    assert store.get_policy("ad-template-generator", project_id="merrypaws")["revision"] == 1
    run, _ = store.create_run(command(scope={"project_id": "blockwise"}, model_policy_revision=None))
    assert run["model_policy_revision"] == saved["revision"]


@pytest.mark.skip(reason="legacy image-model capability matrix is removed")
def test_model_capability_mismatch_is_rejected(tmp_path):
    store = ToolRunStore(str(tmp_path / "capabilities.db"))
    invalid = default_ad_template_policy()
    invalid["stages"]["analyse"]["primary"] = {"provider": "openai", "model": "gpt-image-2"}
    with pytest.raises(ToolRunError, match="cannot perform structured vision"):
        store.create_policy("ad-template-generator", invalid)
    custom = default_ad_template_policy()
    custom["stages"]["masked-text-cleanup"]["primary"] = {"provider": "custom", "model": "unknown-image-model"}
    with pytest.raises(ToolRunError, match="has not verified"):
        store.create_policy("ad-template-generator", custom)
