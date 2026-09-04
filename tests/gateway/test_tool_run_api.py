import asyncio
import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import gateway.ad_template_process as process
import gateway.tool_run_api as tool_run_api
from gateway.platforms.api_server import APIServerAdapter
from gateway.tool_run_api import ToolRunAPIMixin
from gateway.tool_runs import (
    AD_TEMPLATE_OPTIONAL_ROUTE,
    AD_TEMPLATE_ROUTE_ORDER,
    TOOL_RUN_COMMAND_SCHEMA,
    ToolRunStore,
    default_ad_template_policy,
)


def _visible_strings():
    return {"source": ["SOURCE"], "feed": ["FEED"], "story": ["STORY"]}


def _hierarchical_comparison(score: float, required_changes: list[str]) -> dict:
    return {
        "macro": {field: score for field in process.MACRO_FIELDS},
        "critical_regions": [{"region": "full composition", "status": "pass", "findings": []}],
        "regressions": [],
        "ranked_changes": required_changes,
        "declared_decision": "accept" if score >= 9.5 else "revise",
    }


def _source_inventory(required_changes: list[str]) -> dict:
    differences = ["Visible mismatch"] if required_changes else []
    treatment = {
        "brand_silhouette_features": ["roof"],
        "phone_badge": {"shape": "circle", "fillTreatment": "filled"},
        "mail_badge": {"shape": "circle", "fillTreatment": "filled"},
        "web_badge": {"shape": "circle", "fillTreatment": "filled"},
        "location_badge": {"shape": "absent", "fillTreatment": "absent"},
        "cta_badge": {"shape": "absent", "fillTreatment": "absent"},
    }
    return {
        "macro_regions": [
            {
                "region": region,
                "source_components": [region],
                "feed_components": [region],
                "story_components": [region],
                "source_count": 1,
                "feed_count": 1,
                "story_count": 1,
                "status": "match",
                "material": False,
                "findings": [],
                "required_change_refs": [],
            }
            for region in process.COMPARATOR_MACRO_INVENTORY_REGIONS
        ],
        "micro_checks": [
            {
                "check": check,
                "source_observation": treatment if check == "mark_badge_treatment" else f"source {check}",
                "feed_observation": treatment if check == "mark_badge_treatment" else f"feed {check}",
                "story_observation": treatment if check == "mark_badge_treatment" else f"story {check}",
                "status": "mismatch" if differences and check == "typography_spacing" else "match",
                "material": bool(differences and check == "typography_spacing"),
                "findings": differences if differences and check == "typography_spacing" else [],
                "required_change_refs": [1] if differences and check == "typography_spacing" else [],
            }
            for check in process.COMPARATOR_MICRO_INVENTORY_CHECKS
        ],
    }


def _command(source: str, *, idempotency_key: str) -> dict:
    return {
        "schema": TOOL_RUN_COMMAND_SCHEMA,
        "request_id": f"req-{idempotency_key}",
        "tool_id": "ad-template-generator",
        "action": "build-template",
        "scope": {"project_id": "blockwise"},
        "payload": {
            "brief": "Build one ad template",
            "placements": ["feed", "story"],
            "sources": [{"name": "source.png", "path": source}],
        },
        "idempotency_key": idempotency_key,
        "model_policy_revision": 1,
    }


def _valid_template() -> dict:
    def layout(placement: str, height: int) -> dict:
        return {
            "placement": placement,
            "layers": [{
                "type": "plate",
                "layerId": f"{placement}-background",
                "colourRole": "background",
                "geometry": {"x": 0, "y": 0, "width": 1080, "height": height},
                "protected": False,
            }],
            "safeZones": [{"x": 0, "y": 0, "width": 1080, "height": height}],
        }

    return {
        "schema": "blockwise.ad-template",
        "templateId": "completion-test",
        "createdAt": "2026-08-30T00:00:00+00:00",
        "feedLayout": layout("feed", 1350),
        "storyLayout": layout("story", 1920),
        "imageInputs": [],
        "textInputs": [],
        "semanticColours": {
            "background": "#FFFFFF",
            "primary": "#1A56DB",
            "secondary": "#6B7280",
            "accent": "#F59E0B",
            "mainText": "#111827",
            "inverseText": "#FFFFFF",
        },
        "assets": {},
        "fonts": [],
        "metadata": {
            "title": "Completion test",
            "description": "",
            "gallerySamples": {},
            "metaCopyDefaults": {
                "primaryText": [], "headlines": [], "descriptions": [],
                "cta": "LEARN_MORE",
            },
            "aiWritingGuidance": {"summary": "", "fields": {}},
            "publishRequirements": {
                "objective": "OUTCOME_TRAFFIC",
                "specialAdCategory": None,
                "instantForm": {"required": False, "dependency": None},
                "destination": {
                    "required": True, "kind": "website",
                    "dependency": "landing_page_url",
                },
                "requiredCtaTypes": ["LEARN_MORE"],
            },
            "replacementAssets": [],
            "realAssetRefs": [],
        },
    }


class _PreviewAgent:
    def __init__(self, callback, store, run_id, observed):
        self._callback = callback
        self._store = store
        self._run_id = run_id
        self._observed = observed
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_total_tokens = 0
        self.session_estimated_cost_usd = 0.0

    def run_conversation(self, **_kwargs):
        self._observed["user_message"] = _kwargs.get("user_message")
        self._observed["api_max_retries"] = getattr(self, "_api_max_retries", None)
        self._callback(
            "tool.started",
            "terminal",
            "grep -r 'release import compare' /srv /opt/ad-template-builder",
            {},
        )
        self._observed["stage_after_preview"] = self._store.get_run(self._run_id)["stage"]
        self._observed["stage_events_after_preview"] = [
            event["node_id"]
            for event in self._store.events(self._run_id)
            if event["kind"] == "stage.started"
        ]
        return {"final_response": "{}"}


class _ToolRunHarness(ToolRunAPIMixin):
    def __init__(self, store):
        self._tool_run_store = store
        self._tool_run_tasks = {}
        self._tool_run_agents = {}
        self._tool_run_stop_events = {}
        self._tool_run_drain_tasks = set()
        self._tool_run_shutdown = False
        self._model_name = "test-model"
        self.observed = {}
        self.system_prompts = []
        self.agent_kwargs = []
        self.preflighted = []

    def _preflight_tool_candidate(self, candidate):
        self.preflighted.append(dict(candidate))

    def _resolve_route(self, _model):
        return {}

    def _create_agent(self, **kwargs):
        self.agent_kwargs.append(kwargs)
        self.system_prompts.append(kwargs["ephemeral_system_prompt"])
        run_id = kwargs["session_id"].split(":", 1)[0]
        return _PreviewAgent(
            kwargs["tool_progress_callback"], self._tool_run_store, run_id, self.observed
        )

    def _check_auth(self, _request):
        return None


class _JsonRetryRequest:
    can_read_body = True
    content_length = 2

    def __init__(self, run_id: str, body: dict):
        self.match_info = {"run_id": run_id}
        self._body = body

    async def json(self):
        return self._body


def _write_build_retry_checkpoint(
    workspace: Path, *, iterations: int = 2, accepted_last: bool = True,
) -> None:
    (workspace / "previews").mkdir(parents=True)
    for iteration in range(1, iterations + 1):
        root = workspace / "iterations" / f"{iteration:02d}"
        root.mkdir(parents=True)
        template = _valid_template()
        template["templateId"] = f"retry-seed-{iteration}"
        (root / "artifact.json").write_text(
            json.dumps({"template": template, "assets": []}), encoding="utf-8"
        )
        for placement in ("feed", "story"):
            (workspace / "previews" / f"iteration-{iteration:02d}-{placement}.png").write_bytes(
                f"{placement}-{iteration}".encode()
            )
        accepted = accepted_last and iteration == iterations
        score = 9.7 if accepted else 9.0
        changes = [] if accepted else [
            "placement=story; layers=story-background; "
            "current={x:0,y:0,width:1080,height:1920}; "
            "target={x:0,y:0,width:1080,height:1920}; change=Continue matching"
        ]
        comparison = {
            "rubric": {field: score for field in process.RUBRIC_FIELDS},
            "reason": "Accepted comparator pixels" if accepted else "Continue matching",
            "hard_failures": [],
            "visible_strings": _visible_strings(),
            "differences": [] if accepted else ["Visible mismatch"],
            "required_changes": changes,
            "source_inventory": _source_inventory(changes),
            **_hierarchical_comparison(score, changes),
        }
        record = {
            "iteration": iteration,
            "candidate": {"template": template, "assets": [], "previews": []},
            "comparison": comparison,
            "decision": "accepted" if accepted else "revise",
        }
        process.persist_iteration_checkpoint(
            workspace,
            iteration=iteration,
            record=record,
            best_iteration=iteration,
            builder_route={"provider": "openai-codex", "model": "gpt-5.6-sol"},
            builder_escalated=True,
            previous_score=score,
            best_quality_score=score,
            low_gain_streak=0,
            feedback=json.dumps({
                "reason": comparison["reason"],
                "required_changes": changes,
                "source_invariants": {"feature_bullet_count": 6},
            }),
        )


def test_main_chat_model_never_controls_ad_template_role_selection(tmp_path):
    store = ToolRunStore(str(tmp_path / "chat-isolation.db"))
    run, _ = store.create_run(_command(str(tmp_path / "source.png"), idempotency_key="chat-isolation"))
    api = _ToolRunHarness(store)
    api._model_name = "unrelated-chat-model-a"
    first = api._tool_candidate(run, "analyse")
    api._model_name = "unrelated-chat-model-b"
    second = api._tool_candidate(run, "analyse")
    assert first == second == {"provider": "openai-codex", "model": "gpt-5.6-sol"}


def test_final_check_requeue_loads_accepted_iteration_and_existing_artifacts(tmp_path):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    store = ToolRunStore(str(tmp_path / "tool-runs.db"))
    run, _ = store.create_run(_command(str(source), idempotency_key="final-check-resume"))
    workspace = tmp_path / "run"
    (workspace / "iterations" / "05").mkdir(parents=True)
    (workspace / "previews").mkdir(parents=True)
    (workspace / "iterations" / "05" / "artifact.json").write_text(json.dumps({
        "template": _valid_template(), "assets": [],
    }), encoding="utf-8")
    for placement in ("feed", "story"):
        (workspace / "previews" / f"iteration-05-{placement}.png").write_bytes(placement.encode())

    for stale_iteration in range(1, 3):
        store.append_event(run["run_id"], "iteration.compared", node_id="compare", data={
            "iteration": stale_iteration,
            "rubric": {field: 9.0 for field in process.RUBRIC_FIELDS},
            "reason": "Stale failed generation sequence",
            "hard_failures": [],
            "visible_strings": _visible_strings(),
            "differences": ["Visible mismatch"],
            "required_changes": (required_changes := [
                "placement=feed; layers=feed-background; "
                "current={x:0,y:0,width:1080,height:1350}; "
                "target={x:0,y:0,width:1080,height:1350}; change=Continue matching the source"
            ]),
            **_hierarchical_comparison(9.0, required_changes),
            "decision": "revise",
            "preview_names": [f"iteration-{stale_iteration:02d}-feed.png", f"iteration-{stale_iteration:02d}-story.png"],
        })

    for iteration in range(1, 6):
        accepted = iteration == 5
        score = 9.7 if accepted else 9.0
        required_changes = [] if accepted else [
            "placement=feed; layers=feed-background; "
            "current={x:0,y:0,width:1080,height:1350}; "
            "target={x:0,y:0,width:1080,height:1350}; change=Continue matching the source"
        ]
        store.append_event(run["run_id"], "iteration.compared", node_id="compare", data={
            "iteration": iteration,
            "rubric": {field: score for field in process.RUBRIC_FIELDS},
            "reason": "Accepted comparison" if accepted else "Continue matching",
            "hard_failures": [],
            "visible_strings": _visible_strings(),
            "differences": [] if accepted else ["Visible mismatch"],
            "required_changes": required_changes,
            **_hierarchical_comparison(score, required_changes),
            "decision": "accepted" if accepted else "revise",
            "preview_names": [f"iteration-{iteration:02d}-feed.png", f"iteration-{iteration:02d}-story.png"],
        })

    store.update_run(run["run_id"], status="failed", stage="final-check", attention=True)
    requeued = store.requeue(run["run_id"])
    assert (requeued["status"], requeued["stage"]) == ("queued", "final-check")
    checkpoint = _ToolRunHarness(store)._final_check_checkpoint(run["run_id"], workspace)
    assert len(checkpoint["history"]) == 5
    assert checkpoint["history"][-1]["decision"] == "accepted"
    assert checkpoint["previous_score"] == 9.7
    assert checkpoint["candidate"]["template"]["templateId"] == "completion-test"
    assert checkpoint["candidate"]["render"] == {
        "feed": str((workspace / "previews" / "iteration-05-feed.png").resolve()),
        "story": str((workspace / "previews" / "iteration-05-story.png").resolve()),
    }
    assert checkpoint["resume_final_check"] is True


def test_build_restart_loads_two_iterations_and_best_for_iteration_three(tmp_path):
    store = ToolRunStore(str(tmp_path / "checkpoint.db"))
    workspace = tmp_path / "run"
    (workspace / "previews").mkdir(parents=True)
    for iteration, score in ((1, 8.4), (2, 9.1)):
        root = workspace / "iterations" / f"{iteration:02d}"
        root.mkdir(parents=True)
        (root / "artifact.json").write_text(json.dumps({
            "template": _valid_template(), "assets": [],
        }), encoding="utf-8")
        for placement in ("feed", "story"):
            (workspace / "previews" / f"iteration-{iteration:02d}-{placement}.png").write_bytes(
                placement.encode()
            )
        changes = [
            "placement=feed; layers=feed-background; "
            "current={x:0,y:0,width:1080,height:1350}; "
            "target={x:0,y:0,width:1080,height:1350}; change=Continue matching"
        ]
        comparison = {
            "rubric": {field: score for field in process.RUBRIC_FIELDS},
            "reason": "Continue matching the source",
            "hard_failures": [],
            "visible_strings": _visible_strings(),
            "differences": ["Visible mismatch"],
            "required_changes": changes,
            **_hierarchical_comparison(score, changes),
        }
        record = {
            "iteration": iteration,
            "candidate": {"template": _valid_template(), "assets": [], "previews": []},
            "comparison": comparison,
            "decision": "revise",
        }
        process.persist_iteration_checkpoint(
            workspace,
            iteration=iteration,
            record=record,
            best_iteration=iteration,
            builder_route={"provider": "openai-codex", "model": "gpt-5.6-sol"},
            builder_escalated=True,
            previous_score=score,
            low_gain_streak=0,
            feedback=(
                '{"truncated":"' + "x" * 4000
                if iteration == 2 else json.dumps({"iteration": iteration})
            ),
        )
    checkpoint = _ToolRunHarness(store)._ad_template_iteration_checkpoint(
        "trun_checkpoint", workspace, "build"
    )
    assert checkpoint is not None
    assert len(checkpoint["history"]) == 2
    assert checkpoint["best_iteration"] == 2
    assert checkpoint["candidate"]["template"]["templateId"] == "completion-test"
    assert checkpoint["resume_final_check"] is False
    assert checkpoint["previous_score"] == 9.1
    assert checkpoint["best_quality_score"] == 9.1
    recovered_feedback = json.loads(checkpoint["feedback"])
    assert recovered_feedback["best_quality_score"] == 9.1
    assert recovered_feedback["minimum_score"] == 9.1
    assert recovered_feedback["required_changes"] == changes


def test_restart_keeps_best_quality_separate_from_latest_regressing_score(tmp_path):
    store = ToolRunStore(str(tmp_path / "checkpoint-best-score.db"))
    workspace = tmp_path / "run"
    (workspace / "previews").mkdir(parents=True)
    changes = [
        "placement=story; layers=story-background; "
        "current={x:0,y:0,width:1080,height:1920}; "
        "target={x:0,y:0,width:1080,height:1920}; change=Restore the native Story hierarchy"
    ]
    for iteration, quality, regressions in (
        (1, 8.7, []),
        (2, 8.9, ["Story reverted to the Feed topology"]),
    ):
        template = _valid_template()
        template["templateId"] = f"candidate-{iteration}"
        root = workspace / "iterations" / f"{iteration:02d}"
        root.mkdir(parents=True)
        (root / "artifact.json").write_text(
            json.dumps({"template": template, "assets": []}), encoding="utf-8"
        )
        for placement in ("feed", "story"):
            (workspace / "previews" / f"iteration-{iteration:02d}-{placement}.png").write_bytes(
                placement.encode()
            )
        comparison = {
            "rubric": {field: quality for field in process.RUBRIC_FIELDS},
            "reason": "Continue from the immutable best",
            "hard_failures": [],
            "visible_strings": _visible_strings(),
            "differences": ["Visible mismatch"],
            "required_changes": changes,
            "macro": {field: quality for field in process.MACRO_FIELDS},
            "critical_regions": [{"region": "full composition", "status": "pass", "findings": []}],
            "regressions": regressions,
            "ranked_changes": changes,
            "declared_decision": "revise",
        }
        process.persist_iteration_checkpoint(
            workspace,
            iteration=iteration,
            record={
                "iteration": iteration,
                "candidate": {"template": template, "assets": [], "previews": []},
                "comparison": comparison,
                "decision": "revise",
            },
            best_iteration=1,
            builder_route={"provider": "openai-codex", "model": "gpt-5.6-sol"},
            builder_escalated=True,
            previous_score=quality,
            best_quality_score=8.7,
            low_gain_streak=0,
            feedback=json.dumps({
                "best_quality_score": 8.7,
                "best_review": comparison if iteration == 1 else {},
                "current_review": comparison,
            }),
        )

    checkpoint = _ToolRunHarness(store)._ad_template_iteration_checkpoint(
        "trun_best_score", workspace, "build"
    )
    assert checkpoint is not None
    assert checkpoint["best_iteration"] == 1
    assert checkpoint["candidate"]["template"]["templateId"] == "candidate-1"
    assert checkpoint["previous_score"] == 8.9
    assert checkpoint["best_quality_score"] == 8.7


def test_final_check_requeue_rebuilds_after_interrupted_accepted_artifact(tmp_path):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    store = ToolRunStore(str(tmp_path / "tool-runs.db"))
    run, _ = store.create_run(_command(str(source), idempotency_key="final-check-corrupt"))
    workspace = tmp_path / "run"
    (workspace / "iterations" / "04").mkdir(parents=True)
    (workspace / "iterations" / "05").mkdir(parents=True)
    (workspace / "previews").mkdir(parents=True)
    (workspace / "iterations" / "04" / "artifact.json").write_text(json.dumps({
        "template": _valid_template(), "assets": [],
    }), encoding="utf-8")
    # This is the exact crash residue observed in production after a gateway
    # restart interrupted final review: the ledger says accepted, while the
    # latest artifact and previews are empty.
    (workspace / "iterations" / "05" / "artifact.json").write_bytes(b"")
    for placement in ("feed", "story"):
        (workspace / "previews" / f"iteration-04-{placement}.png").write_bytes(
            placement.encode()
        )
        (workspace / "previews" / f"iteration-05-{placement}.png").write_bytes(b"")

    for iteration in range(1, 6):
        accepted = iteration == 5
        score = 9.7 if accepted else 9.0
        store.append_event(run["run_id"], "iteration.compared", node_id="compare", data={
            "iteration": iteration,
            "rubric": {field: score for field in process.RUBRIC_FIELDS},
            "reason": "Accepted comparison" if accepted else "Continue matching",
            "hard_failures": [],
            "visible_strings": _visible_strings(),
            "differences": [] if accepted else ["Visible mismatch"],
            "required_changes": (required_changes := [] if accepted else [
                "placement=feed; layers=feed-background; "
                "current={x:0,y:0,width:1080,height:1350}; "
                "target={x:0,y:0,width:1080,height:1350}; change=Continue matching"
            ]),
            **_hierarchical_comparison(score, required_changes),
            "decision": "accepted" if accepted else "revise",
            "preview_names": [
                f"iteration-{iteration:02d}-feed.png",
                f"iteration-{iteration:02d}-story.png",
            ],
        })

    store.update_run(run["run_id"], status="failed", stage="final-check", attention=True)
    checkpoint = _ToolRunHarness(store)._final_check_checkpoint(run["run_id"], workspace)

    assert checkpoint["resume_final_check"] is False
    assert checkpoint["history"][-1]["final_review_failed"] is True
    assert checkpoint["candidate"]["template"]["templateId"] == "completion-test"
    assert checkpoint["candidate"]["template_path"] == str(
        (workspace / "iterations" / "04" / "artifact.json").resolve()
    )


class _StructuredRoleAgent:
    def __init__(self, instance_id: str, template: dict, messages: list):
        self._instance_id = instance_id
        self._template = template
        self._messages = messages
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_total_tokens = 0
        self.session_estimated_cost_usd = 0.0

    def run_conversation(self, **_kwargs):
        self._messages.append(_kwargs.get("user_message"))
        if self._instance_id.startswith("builder-"):
            payload = {"template": self._template, "assets": []}
        else:
            payload = {
                "reason": "All visible requirements pass",
                "differences": [],
                "required_changes": [],
                "hard_failures": [],
                "visible_strings": _visible_strings(),
                "rubric": {field: 9.7 for field in process.RUBRIC_FIELDS},
            }
            if self._instance_id.startswith("comparator-"):
                payload.update({
                    "macro": {field: 9.7 for field in process.MACRO_FIELDS},
                    "critical_regions": [{
                        "region": "full composition", "status": "pass", "findings": [],
                    }],
                    "regressions": [],
                    "ranked_changes": [],
                    "decision": "accept",
                })
        return {"final_response": json.dumps(payload)}


class _RealOrchestratorHarness(_ToolRunHarness):
    def __init__(self, store, template):
        super().__init__(store)
        self._template = template
        self.role_calls = []
        self.role_messages = []

    def _create_agent(self, **kwargs):
        self.agent_kwargs.append(kwargs)
        instance_id = kwargs["session_id"].split(":", 2)[1]
        self.role_calls.append(instance_id)
        return _StructuredRoleAgent(instance_id, self._template, self.role_messages)


class _ExplicitEventOrchestrator:
    def __init__(self, *, call_agent, emit, **_kwargs):
        self.call_agent = call_agent
        self.emit = emit

    def run(self, *, routes, **_kwargs):
        policy = default_ad_template_policy()
        expected_stage_ids = [*AD_TEMPLATE_ROUTE_ORDER, AD_TEMPLATE_OPTIONAL_ROUTE]
        assert [
            (route["provider"], route["model"])
            for route in routes
        ] == [
            (
                policy["stages"][stage_id]["primary"]["provider"],
                policy["stages"][stage_id]["primary"]["model"],
            )
            for stage_id in expected_stage_ids
        ]
        # A generic role activity preview happens before any orchestrator
        # lifecycle evidence. It must remain ordinary source-stage activity.
        self.call_agent(
            "builder-1",
            [{"type": "text", "text": "complete contract"}],
            "openai-codex/gpt-5.6-sol",
        )
        for kind, node, data in (
            ("stage.started", "build", {}),
            ("iteration.started", "build", {}),
            ("iteration.rendered", "render", {}),
            ("iteration.compared", "compare", {}),
            ("builder.escalated", "build", {
                "iteration": 2,
                "from_provider": "openai-codex",
                "from_model": "gpt-5.6-luna",
                "to_provider": "openai-codex",
                "to_model": "gpt-5.6-sol",
                "reason": "insufficient_improvement",
                "previous_score": 8.2,
                "score": 8.3,
            }),
            ("final-review.started", "final-check", {}),
            ("final-review.completed", "final-check", {}),
            ("template.imported", "live", {}),
        ):
            self.emit(kind, node, data)
        return {
            "template": {},
            "iterations": [],
            "final_review": {},
            "previews": [],
            "documents": {},
            "import": {"template_id": "tpl-test", "status": "imported"},
            "process": "only-ad-template-process",
        }


@pytest.mark.asyncio
async def test_generic_terminal_preview_never_advances_or_emits_stage(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    source = tmp_path / "source.png"
    source.write_bytes(b"source-pixels")
    store = ToolRunStore(str(tmp_path / "tool-runs.db"))
    run, _ = store.create_run(_command(str(source), idempotency_key="stage-truth"))
    api = _ToolRunHarness(store)
    api._resolve_route = lambda _model: pytest.fail(
        "frozen Tool routes must not consult mutable chat model routes"
    )
    monkeypatch.setattr(tool_run_api, "SoleProcessOrchestrator", _ExplicitEventOrchestrator)
    monkeypatch.setattr(api, "_prepare_candidate_output", lambda _run_id, output: output)

    await api._execute_tool_run(run["run_id"])

    assert api.observed["stage_after_preview"] == "source"
    assert api.observed["stage_events_after_preview"] == ["source"]
    events = store.events(run["run_id"])
    assert [
        event["node_id"] for event in events if event["kind"] == "stage.started"
    ] == ["source", "build", "render", "compare", "build", "final-check", "live"]
    escalation = next(event for event in events if event["kind"] == "builder.escalated")
    assert escalation["node_id"] == "build"
    assert escalation["status"] == "running"
    assert escalation["data"] == {
        "iteration": 2,
        "from_provider": "openai-codex",
        "from_model": "gpt-5.6-luna",
        "to_provider": "openai-codex",
        "to_model": "gpt-5.6-sol",
        "reason": "insufficient_improvement",
        "previous_score": 8.2,
        "score": 8.3,
    }
    terminal_event = next(event for event in events if event["kind"] == "tool.started")
    assert terminal_event["node_id"] == "source"
    completed = store.get_run(run["run_id"])
    assert (completed["status"], completed["stage"], completed["progress"]) == (
        "completed",
        "live",
        1.0,
    )
    assert api.system_prompts == [ToolRunAPIMixin._isolated_tool_role_prompt()]
    assert api.agent_kwargs[0]["enabled_toolsets_override"] == []
    assert api.agent_kwargs[0]["persistence_disabled"] is True
    assert api.agent_kwargs[0]["confirmed_runtime_lock"] is True
    assert callable(api.agent_kwargs[0]["stream_delta_callback"])
    assert callable(api.agent_kwargs[0]["reasoning_callback"])
    assert "thinking_callback" not in api.agent_kwargs[0]
    assert api.observed["api_max_retries"] == 1
    assert api.agent_kwargs[0]["route"] is None
    policy = default_ad_template_policy()
    expected_preflight = []
    for stage_id in [*AD_TEMPLATE_ROUTE_ORDER, AD_TEMPLATE_OPTIONAL_ROUTE]:
        candidate = policy["stages"][stage_id]["primary"]
        route = {"provider": candidate["provider"], "model": candidate["model"]}
        if route not in expected_preflight:
            expected_preflight.append(route)
    assert api.preflighted == expected_preflight


@pytest.mark.asyncio
async def test_unknown_frozen_provider_fails_preflight_before_any_model_work(
    tmp_path, monkeypatch,
):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    source = tmp_path / "source.png"
    source.write_bytes(b"source-pixels")
    store = ToolRunStore(str(tmp_path / "provider-preflight.db"))
    run, _ = store.create_run(
        _command(str(source), idempotency_key="provider-preflight")
    )
    api = _ToolRunHarness(store)

    def reject_provider(_candidate):
        raise RuntimeError("Unknown provider 'custom:venice'")

    api._preflight_tool_candidate = reject_provider

    await api._execute_tool_run(run["run_id"])

    failed = store.get_run(run["run_id"])
    assert failed["status"] == "failed"
    assert "Unknown provider" in failed["error"]
    assert api.agent_kwargs == []
    assert not any(
        event["kind"] == "stage.started" for event in store.events(run["run_id"])
    )


@pytest.mark.asyncio
async def test_execution_rejects_stale_pinned_policy_before_agent(tmp_path):
    store = ToolRunStore(str(tmp_path / "stale-execution.db"))
    run, _ = store.create_run(
        _command("source.png", idempotency_key="stale-execution")
    )
    stale = default_ad_template_policy()
    stale["seed_revision"] = 3
    stale["stages"]["analyse"]["primary"]["model"] = "deepseek-v4-flash"
    store._conn.execute(
        "UPDATE tool_runs SET policy_json=? WHERE run_id=?",
        (json.dumps(stale, separators=(",", ":"), sort_keys=True), run["run_id"]),
    )
    store._conn.commit()
    api = _ToolRunHarness(store)

    await api._execute_tool_run(run["run_id"])

    failed = store.get_run(run["run_id"])
    assert failed["status"] == "failed"
    assert "audited" in failed["error"]
    assert api.agent_kwargs == []


def test_direct_orchestrator_result_does_not_weaken_generic_agent_parser():
    result = {
        "template": {},
        "iterations": [],
        "final_review": {},
        "previews": [],
        "documents": {},
        "import": {},
        "process": "only-ad-template-process",
    }
    with pytest.raises(RuntimeError, match="structured JSON"):
        ToolRunAPIMixin._tool_json_output(result)
    assert ToolRunAPIMixin._tool_json_output(result, process_result=True) == result


def test_model_transport_exhaustion_is_distinct_from_structured_output_failure():
    with pytest.raises(process.AdTemplateTransportError, match="transport"):
        ToolRunAPIMixin._tool_json_output({
            "final_response": "API call failed after 1 retries: Codex TTFB timeout",
        })


def test_completion_projection_keeps_large_layered_documents_out_of_the_ledger(tmp_path):
    template = _valid_template()
    template["metadata"]["description"] = "layered-template-content-" * 2_000
    evidence = {
        "reason": "The source and both placements match",
        "differences": [],
        "required_changes": [],
        "hard_failures": [],
        "visible_strings": _visible_strings(),
        "rubric": {field: 9.7 for field in process.RUBRIC_FIELDS},
        "macro": {field: 9.7 for field in process.MACRO_FIELDS},
        "critical_regions": [{"region": "full composition", "status": "pass", "findings": []}],
        "regressions": [],
        "ranked_changes": [],
        "decision": "accept",
    }
    output = {
        "template": template,
        "iterations": [{
            "iteration": 1,
            "comparison": evidence,
            "decision": "accepted",
        }],
        "final_review": {"reviewers": [
            {"id": "reviewer-a", "route": "route-a", **evidence},
            {"id": "reviewer-b", "route": "route-b", **evidence},
        ]},
        "previews": [],
        "documents": process.deterministic_documents(template),
        "template_path": "/run/artifact.json",
        "render_path": "/run/feed.png",
        "import": {"template_id": "completion-test", "status": "imported"},
        "process": "only-ad-template-process",
    }

    projected = ToolRunAPIMixin._prepare_candidate_output("run", output)

    assert projected["template"] == {
        "schema": "blockwise.ad-template",
        "templateId": "completion-test",
        "title": "Completion test",
        "artifact": "/run/artifact.json",
    }
    assert projected["documents"]["template.json"]["bytes"] > 32_000
    assert all(not isinstance(value, str) for value in projected["documents"].values())
    store = ToolRunStore(str(tmp_path / "compact-output.db"))
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    run, _ = store.create_run(_command(str(source), idempotency_key="compact-output"))
    stored = store.update_run(run["run_id"], status="completed", output=projected)
    assert stored["output"]["import"]["template_id"] == "completion-test"


@pytest.mark.asyncio
async def test_retry_reuses_persisted_source_after_staging_cleanup(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    staging = home / "tool_runs" / "staging"
    staging.mkdir(parents=True)
    source = staging / "source.png"
    source.write_bytes(b"source-pixels")
    monkeypatch.setenv("HERMES_HOME", str(home))
    store = ToolRunStore(str(tmp_path / "retry-source.db"))
    run, _ = store.create_run(
        _command(str(source), idempotency_key="retry-durable-source")
    )
    api = _ToolRunHarness(store)
    seen_sources = []

    class _FailOnce:
        def __init__(self, **_kwargs):
            pass

        def run(self, *, source, **_kwargs):
            seen_sources.append(source)
            assert Path(source).read_bytes() == b"source-pixels"
            raise process.AdTemplateProcessError("intentional first-run failure")

    monkeypatch.setattr(tool_run_api, "SoleProcessOrchestrator", _FailOnce)
    await api._execute_tool_run(run["run_id"])

    assert store.get_run(run["run_id"])["status"] == "failed"
    assert not source.exists()
    durable = (
        home
        / "tool_runs"
        / "ad-template-generator"
        / run["run_id"]
        / "previews"
        / "source.png"
    )
    assert durable.read_bytes() == b"source-pixels"

    class _SucceedOnRetry:
        def __init__(self, **_kwargs):
            pass

        def run(self, *, source, **_kwargs):
            seen_sources.append(source)
            assert Path(source).read_bytes() == b"source-pixels"
            return {
                "template": {},
                "iterations": [],
                "final_review": {},
                "previews": [],
                "documents": {},
                "import": {"template_id": "tpl-retried", "status": "imported"},
                "process": "only-ad-template-process",
            }

    monkeypatch.setattr(tool_run_api, "SoleProcessOrchestrator", _SucceedOnRetry)
    monkeypatch.setattr(api, "_prepare_candidate_output", lambda _run_id, output: output)
    started = []
    monkeypatch.setattr(
        api,
        "_start_tool_task",
        lambda run_id, finalize=False: started.append((run_id, finalize)),
    )
    response = await api._handle_retry_tool_run(
        SimpleNamespace(match_info={"run_id": run["run_id"]})
    )
    assert response.status == 202
    assert started == [(run["run_id"], False)]

    await api._execute_tool_run(run["run_id"])

    completed = store.get_run(run["run_id"])
    assert completed["status"] == "completed"
    assert completed["output"]["import"]["template_id"] == "tpl-retried"
    assert seen_sources == [str(durable), str(durable)]
    assert "command.queued" in {
        event["kind"] for event in store.events(run["run_id"])
    }


@pytest.mark.asyncio
async def test_terminal_final_failure_build_from_best_is_restart_safe_and_repairs_renderer(
    tmp_path, monkeypatch,
):
    home = tmp_path / "hermes-home"
    source = tmp_path / "source.png"
    source.write_bytes(b"exact-source-pixels")
    monkeypatch.setenv("HERMES_HOME", str(home))
    command = _command(str(source), idempotency_key="build-from-best")
    command["payload"]["sources"][0]["size"] = source.stat().st_size
    command["payload"]["brief"] = "Immutable full brief tail sentinel ADDRESS_BOUNDS_ONLY"
    store = ToolRunStore(str(tmp_path / "build-from-best.db"))
    run, _ = store.create_run(command)
    workspace = home / "tool_runs" / "ad-template-generator" / run["run_id"]
    _write_build_retry_checkpoint(workspace)
    (workspace / "previews" / "source.png").write_bytes(source.read_bytes())
    store.append_event(
        run["run_id"], "final-review.retried", status="running", node_id="final-check",
        data={
            "reviewer": "final-reviewer-b",
            "attempt": 3,
            "reason": "final-review-b returned no structured JSON",
        },
    )
    store.update_run(
        run["run_id"], status="failed", stage="final-check",
        error="final-review-b exhausted invalid structured JSON retries",
    )
    renderer_reason = (
        "story text layer story-address painted bounds exceed geometry by 4px on right"
    )
    monkeypatch.setattr(
        tool_run_api,
        "run_generator_cli",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            process.AdTemplateRendererRejection(renderer_reason)
        ),
    )
    api = _ToolRunHarness(store)
    started = []
    monkeypatch.setattr(
        api, "_start_tool_task",
        lambda run_id, finalize=False: started.append((run_id, finalize)),
    )

    response = await api._handle_retry_tool_run(
        _JsonRetryRequest(run["run_id"], {"mode": "build-from-best"})
    )

    assert response.status == 202
    assert started == [(run["run_id"], False)]
    queued = store.get_run(run["run_id"])
    assert queued["status"] == "queued"
    assert queued["stage"] == "build"
    assert queued["payload"] == run["payload"]
    assert queued["model_policy_revision"] == run["model_policy_revision"]
    events = store.events(run["run_id"])
    intent = [event for event in events if event["kind"] == "command.queued"][-1]
    assert intent["data"]["retry_mode"] == "build-from-best"
    assert intent["data"]["seed_iteration"] == 2
    assert intent["data"]["history_length"] == 2
    assert intent["data"]["iteration_budget_extension"] == 1
    assert intent["data"]["renderer_reasons"] == [renderer_reason]
    assert "final-review-b exhausted invalid structured JSON retries" in intent["data"]["seed_feedback"]
    assert renderer_reason in intent["data"]["seed_feedback"]

    observed = {}

    class _ResumeFromBest:
        def __init__(self, **_kwargs):
            pass

        def run(self, **kwargs):
            observed.update(kwargs)
            return {
                "template": {}, "iterations": [], "final_review": {},
                "previews": [], "documents": {},
                "import": {"template_id": "tpl-after-repair", "status": "imported"},
                "process": "only-ad-template-process",
            }

    restarted_api = _ToolRunHarness(store)
    monkeypatch.setattr(tool_run_api, "SoleProcessOrchestrator", _ResumeFromBest)
    monkeypatch.setattr(
        restarted_api, "_prepare_candidate_output", lambda _run_id, output: output,
    )
    await restarted_api._execute_tool_run(run["run_id"])

    assert observed["resume_final_check"] is False
    assert observed["total_iterations"] == 2
    assert observed["best_iteration"] == 2
    assert observed["revision_candidate"]["template"]["templateId"] == "retry-seed-2"
    assert observed["iteration_budget_extension"] == 1
    assert renderer_reason in observed["feedback"]
    assert "final-review-b exhausted invalid structured JSON retries" in observed["feedback"]


@pytest.mark.asyncio
async def test_build_from_best_concurrent_requests_queue_once_atomically(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    store = ToolRunStore(str(tmp_path / "concurrent-retry.db"))
    run, _ = store.create_run(_command(str(source), idempotency_key="concurrent-retry"))
    store.update_run(run["run_id"], status="failed", stage="final-check", error="failed")
    api = _ToolRunHarness(store)
    started = []
    monkeypatch.setattr(api, "_start_tool_task", lambda *args, **kwargs: started.append(args))
    monkeypatch.setattr(
        api,
        "_validate_build_from_best_retry",
        lambda _run: ({}, {
            "retry_mode": "build-from-best",
            "seed_iteration": 2,
            "history_length": 2,
            "iteration_budget_extension": 1,
            "seed_feedback": "{}",
        }),
    )

    responses = await asyncio.gather(*[
        api._handle_retry_tool_run(
            _JsonRetryRequest(run["run_id"], {"mode": "build-from-best"})
        )
        for _ in range(2)
    ])

    assert sorted(response.status for response in responses) == [202, 400]
    queued = [
        event for event in store.events(run["run_id"])
        if event["kind"] == "command.queued"
    ]
    assert len(queued) == 1
    assert queued[0]["data"]["retry_mode"] == "build-from-best"
    assert len(started) == 1


@pytest.mark.asyncio
async def test_build_from_best_rejects_completed_import_without_mutation(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    store = ToolRunStore(str(tmp_path / "completed-retry.db"))
    run, _ = store.create_run(_command(str(source), idempotency_key="completed-retry"))
    store.update_run(
        run["run_id"], status="completed", stage="live",
        output={"import": {"template_id": "already-imported"}},
    )
    api = _ToolRunHarness(store)
    before = store.get_run(run["run_id"])
    before_events = store.events(run["run_id"])
    monkeypatch.setattr(
        api, "_validate_build_from_best_retry",
        lambda _run: pytest.fail("completed runs must fail before validation"),
    )
    monkeypatch.setattr(
        api, "_start_tool_task",
        lambda *_args, **_kwargs: pytest.fail("completed run must not start"),
    )

    response = await api._handle_retry_tool_run(
        _JsonRetryRequest(run["run_id"], {"mode": "build-from-best"})
    )

    assert response.status == 400
    assert store.get_run(run["run_id"]) == before
    assert store.events(run["run_id"]) == before_events


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["source", "best-preview"])
async def test_build_from_best_fails_closed_when_persisted_seed_is_incomplete(
    tmp_path, monkeypatch, missing,
):
    home = tmp_path / "hermes-home"
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    monkeypatch.setenv("HERMES_HOME", str(home))
    store = ToolRunStore(str(tmp_path / f"missing-{missing}.db"))
    run, _ = store.create_run(_command(str(source), idempotency_key=f"missing-{missing}"))
    workspace = home / "tool_runs" / "ad-template-generator" / run["run_id"]
    _write_build_retry_checkpoint(workspace)
    (workspace / "previews" / "source.png").write_bytes(source.read_bytes())
    if missing == "source":
        source.unlink()
        (workspace / "previews" / "source.png").unlink()
    else:
        (workspace / "previews" / "iteration-02-feed.png").unlink()
    store.update_run(run["run_id"], status="failed", stage="final-check", error="failed")
    api = _ToolRunHarness(store)
    before_events = store.events(run["run_id"])
    monkeypatch.setattr(
        tool_run_api, "run_generator_cli",
        lambda *_args, **_kwargs: pytest.fail("incomplete seed must fail before renderer"),
    )

    response = await api._handle_retry_tool_run(
        _JsonRetryRequest(run["run_id"], {"mode": "build-from-best"})
    )

    assert response.status == 400
    assert store.get_run(run["run_id"])["status"] == "failed"
    assert store.events(run["run_id"]) == before_events


@pytest.mark.asyncio
async def test_ad_template_retry_preserves_submitted_model_profile_snapshot(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(b"source-pixels")
    store = ToolRunStore(str(tmp_path / "retry-policy.db"))
    current_policy = default_ad_template_policy()
    for _ in range(6):
        store.create_policy("ad-template-generator", current_policy, make_default=False)
    stale_policy = json.loads(json.dumps(current_policy))
    stale_policy["seed_revision"] = 8
    stale_record = store.create_policy(
        "ad-template-generator", stale_policy, make_default=False
    )
    current_record = store.create_policy(
        "ad-template-generator", current_policy, make_default=True
    )
    assert stale_record["revision"] == 8
    assert current_record["revision"] == 9

    command = _command(str(source), idempotency_key="retry-stale-policy")
    command["model_policy_revision"] = 8
    run, _ = store.create_run(command)
    checkpoint = {"template_id": "candidate-at-final-check", "iteration": 12}
    store.update_run(
        run["run_id"], status="failed", stage="final-check", output=checkpoint,
        error="review schema rejected",
    )
    before_default = store.get_policy("ad-template-generator")
    before = store.get_run(run["run_id"])
    started = []
    api = _ToolRunHarness(store)
    monkeypatch.setattr(
        api,
        "_start_tool_task",
        lambda run_id, finalize=False: started.append((run_id, finalize)),
    )

    response = await api._handle_retry_tool_run(
        SimpleNamespace(match_info={"run_id": run["run_id"]})
    )

    assert response.status == 202
    retried = store.get_run(run["run_id"])
    assert retried["model_policy_revision"] == 8
    assert retried["model_policy"] == stale_policy
    assert retried["scope"] == before["scope"]
    assert retried["payload"] == before["payload"]
    assert retried["output"] == checkpoint
    assert retried["stage"] == "final-check"
    assert started == [(run["run_id"], False)]
    assert store.get_policy("ad-template-generator") == before_default
    queued = [event for event in store.events(run["run_id"]) if event["kind"] == "command.queued"]
    assert "policy_upgraded" not in queued[-1]["data"]


@pytest.mark.asyncio
async def test_real_orchestrator_successful_import_reaches_completed(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    source = tmp_path / "source.png"
    source.write_bytes(b"source-pixels")
    template = _valid_template()
    store = ToolRunStore(str(tmp_path / "completion.db"))
    run, _ = store.create_run(_command(str(source), idempotency_key="completion"))
    api = _RealOrchestratorHarness(store, template)

    def render(candidate, workspace: Path):
        rendered = workspace / "rendered"
        rendered.mkdir(parents=True, exist_ok=True)
        feed = rendered / "feed.png"
        story = rendered / "story.png"
        feed.write_bytes(b"feed-pixels")
        story.write_bytes(b"story-pixels")
        artifact = workspace / "artifact.json"
        artifact.write_text(json.dumps(candidate), encoding="utf-8")
        return {
            **candidate,
            "previews": [
                {"name": feed.name, "path": str(feed), "placement": "feed"},
                {"name": story.name, "path": str(story), "placement": "story"},
            ],
            "render": {"feed": str(feed), "story": str(story)},
            "receipt": {"outputs": {"feed": {}, "story": {}}},
            "template_path": str(artifact),
        }

    monkeypatch.setattr(process, "run_generator_cli", render)
    monkeypatch.setattr(
        process,
        "import_template",
        lambda _output, *, run_id, project_id: {
            "template_id": f"tpl-{run_id[-8:]}",
            "status": "imported",
            "project_id": project_id,
        },
    )
    assert tool_run_api.SoleProcessOrchestrator is process.SoleProcessOrchestrator

    await api._execute_tool_run(run["run_id"])

    completed = store.get_run(run["run_id"])
    assert completed["status"] == "completed"
    assert completed["stage"] == "live"
    assert completed["error"] is None
    assert completed["output"]["process"] == "only-ad-template-process"
    assert completed["output"]["import"]["status"] == "imported"
    assert len(api.role_calls) == 4
    assert api.role_calls[0].startswith("builder-")
    assert api.role_calls[1].startswith("comparator-")
    assert all(name.startswith("final-reviewer-") for name in api.role_calls[2:])
    assert not any(event["kind"] == "run.failed" for event in store.events(run["run_id"]))
    assert all(kwargs["enabled_toolsets_override"] == [] for kwargs in api.agent_kwargs)
    builder_message = api.role_messages[0]
    assert builder_message[0]["type"] == "text"
    assert builder_message[1]["type"] == "image_url"
    assert builder_message[1]["image_url"]["url"].startswith("data:image/png;base64,")
    comparator_message = api.role_messages[1]
    assert comparator_message[0]["type"] == "text"
    assert [part["type"] for part in comparator_message[1:]] == [
        "image_url", "image_url", "image_url",
    ]


@pytest.mark.asyncio
async def test_initial_builder_format_recovery_persists_events_and_all_role_costs(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    source = tmp_path / "source.png"
    source.write_bytes(b"source-pixels")
    template = _valid_template()
    store = ToolRunStore(str(tmp_path / "format-recovery.db"))
    run, _ = store.create_run(
        _command(str(source), idempotency_key="format-recovery")
    )

    class _Agent:
        def __init__(self, owner, instance_id):
            self.owner = owner
            self.instance_id = instance_id
            self.session_prompt_tokens = 10
            self.session_completion_tokens = 5
            self.session_total_tokens = 15
            self.session_estimated_cost_usd = 0.05

        def run_conversation(self, **_kwargs):
            self.owner.calls.append(self.instance_id)
            if self.instance_id.startswith("builder-"):
                self.owner.builder_calls += 1
                if self.owner.builder_calls == 1:
                    return {"final_response": "not-json"}
                payload = {"template": template, "assets": []}
            else:
                payload = {
                    "reason": "All visible requirements pass",
                    "differences": [],
                    "required_changes": [],
                    "hard_failures": [],
                    "visible_strings": _visible_strings(),
                    "rubric": {
                        field: 9.7 for field in process.RUBRIC_FIELDS
                    },
                }
                if self.instance_id.startswith("comparator-"):
                    payload.update({
                        "macro": {field: 9.7 for field in process.MACRO_FIELDS},
                        "critical_regions": [{
                            "region": "full composition", "status": "pass", "findings": [],
                        }],
                        "regressions": [],
                        "ranked_changes": [],
                        "decision": "accept",
                    })
            return {"final_response": json.dumps(payload)}

    class _Harness(_ToolRunHarness):
        def __init__(self, run_store):
            super().__init__(run_store)
            self.calls = []
            self.builder_calls = 0

        def _create_agent(self, **kwargs):
            self.agent_kwargs.append(kwargs)
            instance_id = kwargs["session_id"].split(":", 2)[1]
            return _Agent(self, instance_id)

    api = _Harness(store)

    def render(candidate, workspace: Path):
        rendered = workspace / "rendered"
        rendered.mkdir(parents=True, exist_ok=True)
        feed = rendered / "feed.png"
        story = rendered / "story.png"
        feed.write_bytes(b"feed-pixels")
        story.write_bytes(b"story-pixels")
        artifact = workspace / "artifact.json"
        artifact.write_text(json.dumps(candidate), encoding="utf-8")
        return {
            **candidate,
            "previews": [
                {"name": feed.name, "path": str(feed), "placement": "feed"},
                {"name": story.name, "path": str(story), "placement": "story"},
            ],
            "render": {"feed": str(feed), "story": str(story)},
            "receipt": {"outputs": {"feed": {}, "story": {}}},
            "template_path": str(artifact),
        }

    monkeypatch.setattr(process, "run_generator_cli", render)
    monkeypatch.setattr(
        process,
        "import_template",
        lambda _output, *, run_id, project_id: {
            "template_id": "tpl-format-recovery",
            "status": "imported",
            "project_id": project_id,
        },
    )

    await api._execute_tool_run(run["run_id"])

    completed = store.get_run(run["run_id"])
    assert completed["status"] == "completed"
    assert api.calls[:3] == [
        "builder-1",
        "builder-1-output-retry-1",
        "comparator-1",
    ]
    assert len([name for name in api.calls if name.startswith("comparator-")]) == 1
    assert len([name for name in api.calls if name.startswith("final-reviewer-")]) == 2
    assert completed["output"]["usage"] == {
        "input_tokens": 50,
        "output_tokens": 25,
        "total_tokens": 75,
        "estimated_cost_usd": pytest.approx(0.25),
    }
    assert completed["output"]["cost"]["reported_usd"] == pytest.approx(0.25)
    events = store.events(run["run_id"])
    assert len([event for event in events if event["kind"] == "builder.output-retry"]) == 1
    assert not any(event["kind"] == "builder.escalated" for event in events)
    assert len([event for event in events if event["kind"] == "iteration.compared"]) == 1
    assert len([event for event in events if event["kind"] == "final-review.started"]) == 2
    assert len([event for event in events if event["kind"] == "final-review.completed"]) == 1


def test_isolated_role_prompt_forbids_discovery_and_orchestrator_work():
    prompt = ToolRunAPIMixin._isolated_tool_role_prompt().lower()
    assert "complete role contract" in prompt
    assert "attached images" in prompt
    assert "do not inspect" in prompt and "repositories or the filesystem" in prompt
    assert "terminal" in prompt and "broad shell searches" in prompt
    assert "/srv" in prompt and "/opt/ad-template-builder" in prompt
    assert "orchestrator alone" in prompt and "renderer" in prompt and "import" in prompt
    assert "exactly one requested json object" in prompt


class _InterruptibleRole:
    def __init__(self):
        self.messages = []

    def hard_interrupt(self, message=None):
        self.messages.append(message)


@pytest.mark.asyncio
async def test_cancel_without_live_task_transitions_and_stops_active_role(tmp_path):
    store = ToolRunStore(str(tmp_path / "cancel.db"))
    run, _ = store.create_run(_command("source.png", idempotency_key="cancel"))
    store.update_run(run["run_id"], status="running")
    api = _ToolRunHarness(store)
    role = _InterruptibleRole()
    api._tool_run_agents[run["run_id"]] = {"builder-1": role}
    cancelling_statuses = []
    request_cancel = store.request_cancel

    def tracked_request_cancel(run_id):
        result = request_cancel(run_id)
        cancelling_statuses.append(result["status"])
        return result

    store.request_cancel = tracked_request_cancel
    request = SimpleNamespace(match_info={"run_id": run["run_id"]})

    response = await api._handle_cancel_tool_run(request)

    assert response.status == 202
    assert json.loads(response.body)["status"] == "cancelled"
    assert cancelling_statuses == ["cancelling"]
    assert store.get_run(run["run_id"])["status"] == "cancelled"
    assert role.messages == ["api_server_tool_run_cancel"]
    assert [event["kind"] for event in store.events(run["run_id"])[-2:]] == [
        "command.cancel-requested",
        "run.cancelled",
    ]


@pytest.mark.asyncio
async def test_disconnect_stops_active_tool_role_with_current_interrupt_contract():
    role = _InterruptibleRole()
    adapter = SimpleNamespace(
        name="api_server",
        _mark_disconnected=lambda: None,
        _tool_run_shutdown=False,
        _tool_run_agents={"run": {"builder-1": role}},
        _tool_run_tasks={},
        _tool_run_store=None,
        _response_store=None,
        _site=None,
        _runner=None,
        _close_cached_session_dbs=lambda: None,
        _app=object(),
    )

    await APIServerAdapter.disconnect(adapter)

    assert role.messages == ["api_server_tool_run_shutdown"]
    assert adapter._tool_run_shutdown is True
    assert adapter._tool_run_agents == {}
    assert adapter._app is None

def _one_second_candidates(_run, stage):
    routes = {
        "analyse": {
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
        },
        "compare": {
            "provider": "openai-codex",
            "model": "gpt-5.6-luna",
        },
        "final-review-a": {
            "provider": "openai-codex",
            "model": "gpt-5.6-luna",
        },
        "final-review-b": {
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
        },
        "quality-escalation": {
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
        },
    }
    return [routes[stage]], {
        "timeout_seconds": 1,
        "max_attempts": 1,
        "max_cost_usd": 1,
    }


class _OneRoleOrchestrator:
    def __init__(self, *, call_agent, emit, should_stop, **_kwargs):
        self.call_agent = call_agent
        self.emit = emit
        self.should_stop = should_stop

    def run(self, *, source, **_kwargs):
        payload = self.call_agent(
            "builder-1",
            process.vision_message("inspect", [source]),
            "openai-codex/gpt-5.6-luna",
        )
        if self.should_stop():
            raise process.AdTemplateProcessError(
                "sole ad-template process was cancelled"
            )
        return {
            "template": payload,
            "iterations": [],
            "final_review": {},
            "previews": [],
            "documents": {},
            "import": {"template_id": "tpl-heartbeat", "status": "imported"},
            "process": "only-ad-template-process",
        }


class _TimedRoleAgent:
    def __init__(self, behavior, kwargs):
        self._behavior = behavior
        self.kwargs = kwargs
        self.interrupts = []
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_total_tokens = 0
        self.session_estimated_cost_usd = 0.0

    def run_conversation(self, **kwargs):
        return self._behavior(self, kwargs)

    def hard_interrupt(self, message=None):
        self.interrupts.append(message)


class _TimedRoleHarness(_ToolRunHarness):
    def __init__(self, store, behavior):
        super().__init__(store)
        self._behavior = behavior
        self.created_agents = []

    def _create_agent(self, **kwargs):
        self.agent_kwargs.append(kwargs)
        agent = _TimedRoleAgent(self._behavior, kwargs)
        self.created_agents.append(agent)
        return agent


def test_ad_template_inactivity_timeout_has_finite_slow_call_allowance():
    assert tool_run_api._ad_template_inactivity_timeout(
        {"timeout_seconds": 120}, stage="compare"
    ) == 180.0
    assert tool_run_api._ad_template_inactivity_timeout(
        {"timeout_seconds": 600}, stage="build"
    ) == 600.0
    assert tool_run_api._ad_template_inactivity_timeout(
        {}, stage="final-check"
    ) == 180.0


@pytest.mark.asyncio
async def test_slow_silent_role_outlives_short_policy_but_remains_bounded(
    tmp_path, monkeypatch
):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(tool_run_api, "AD_TEMPLATE_MIN_INACTIVITY_SECONDS", 2.0)
    source = tmp_path / "source.png"
    source.write_bytes(b"source-pixels")
    store = ToolRunStore(str(tmp_path / "slow-silent-role.db"))
    run, _ = store.create_run(_command(str(source), idempotency_key="slow-silent-role"))

    def slow_but_healthy(_agent, _kwargs):
        assert _agent._try_recover_primary_transport(
            RuntimeError("transport timeout"), retry_count=1, max_retries=1
        ) is False
        time.sleep(1.2)
        return {"final_response": "{}"}

    api = _TimedRoleHarness(store, slow_but_healthy)
    monkeypatch.setattr(api, "_tool_candidates", _one_second_candidates)
    monkeypatch.setattr(tool_run_api, "SoleProcessOrchestrator", _OneRoleOrchestrator)
    monkeypatch.setattr(api, "_prepare_candidate_output", lambda _run_id, output: output)

    await api._execute_tool_run(run["run_id"])

    completed = store.get_run(run["run_id"])
    assert completed["status"] == "completed"
    assert api.created_agents[0].interrupts == []
    attempt = next(
        event for event in store.events(run["run_id"])
        if event["kind"] == "provider.attempt"
    )
    assert attempt["data"]["configured_timeout_seconds"] == 1.0
    assert attempt["data"]["timeout_seconds"] == 2.0


@pytest.mark.asyncio
async def test_stream_heartbeat_prevents_false_inactivity_timeout(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    source = tmp_path / "source.png"
    source.write_bytes(b"source-pixels")
    store = ToolRunStore(str(tmp_path / "heartbeat.db"))
    run, _ = store.create_run(_command(str(source), idempotency_key="heartbeat"))

    def streamed(agent, _kwargs):
        for _ in range(6):
            time.sleep(0.22)
            agent.kwargs["stream_delta_callback"]("chunk")
        return {"final_response": "{}"}

    api = _TimedRoleHarness(store, streamed)
    monkeypatch.setattr(api, "_tool_candidates", _one_second_candidates)
    monkeypatch.setattr(tool_run_api, "SoleProcessOrchestrator", _OneRoleOrchestrator)
    monkeypatch.setattr(api, "_prepare_candidate_output", lambda _run_id, output: output)

    await api._execute_tool_run(run["run_id"])

    completed = store.get_run(run["run_id"])
    assert completed["status"] == "completed"
    assert api.created_agents[0].interrupts == []


@pytest.mark.asyncio
async def test_silent_timeout_interrupts_drains_and_consumes_late_exception(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(tool_run_api, "AD_TEMPLATE_MIN_INACTIVITY_SECONDS", 1.0)
    source = tmp_path / "source.png"
    source.write_bytes(b"source-pixels")
    store = ToolRunStore(str(tmp_path / "silent-timeout.db"))
    run, _ = store.create_run(_command(str(source), idempotency_key="silent-timeout"))
    finished = {"value": False}

    def late_failure(_agent, _kwargs):
        time.sleep(1.2)
        finished["value"] = True
        raise RuntimeError("late role exception")

    api = _TimedRoleHarness(store, late_failure)
    monkeypatch.setattr(api, "_tool_candidates", _one_second_candidates)
    monkeypatch.setattr(tool_run_api, "SoleProcessOrchestrator", _OneRoleOrchestrator)

    await api._execute_tool_run(run["run_id"])
    assert store.get_run(run["run_id"])["status"] == "failed"
    assert api.created_agents[0].interrupts == [
        "api_server_tool_run_stage_timeout"
    ]
    await asyncio.sleep(0.35)
    assert finished["value"] is True
    assert api._tool_run_drain_tasks == set()


@pytest.mark.asyncio
async def test_late_timed_out_builder_cannot_render_or_import(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(tool_run_api, "AD_TEMPLATE_MIN_INACTIVITY_SECONDS", 1.0)
    source = tmp_path / "source.png"
    source.write_bytes(b"source-pixels")
    store = ToolRunStore(str(tmp_path / "late-builder.db"))
    run, _ = store.create_run(_command(str(source), idempotency_key="late-builder"))
    rendered = []
    imported = []

    def late_builder(_agent, _kwargs):
        time.sleep(1.2)
        return {"final_response": json.dumps({"template": {}, "assets": []})}

    api = _TimedRoleHarness(store, late_builder)
    monkeypatch.setattr(api, "_tool_candidates", _one_second_candidates)
    monkeypatch.setattr(process, "run_generator_cli", lambda *args: rendered.append(args))
    monkeypatch.setattr(process, "import_template", lambda *args, **kwargs: imported.append((args, kwargs)))

    await api._execute_tool_run(run["run_id"])
    assert store.get_run(run["run_id"])["status"] == "failed"
    await asyncio.sleep(0.35)
    assert api.created_agents[0].interrupts == [
        "api_server_tool_run_stage_timeout"
    ]
    assert api._tool_run_drain_tasks == set()
    assert rendered == []
    assert imported == []
