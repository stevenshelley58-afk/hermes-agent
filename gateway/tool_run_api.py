"""HTTP and execution mixin for durable Tool runs.

The API server owns authentication and agent construction.  This mixin keeps
the Tool-job contract at the platform edge instead of adding another model
tool to every Hermes conversation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

from aiohttp import web

from agent.interrupt_compat import request_hard_interrupt
from agent.redact import redact_sensitive_text
from gateway.ad_template_process import AdTemplateProcessError, AdTemplateStructuredOutputError, SoleProcessOrchestrator, validate_artifacts, validate_iterations, validate_final_review, deterministic_documents, generator_prompt, THRESHOLD as AD_TEMPLATE_THRESHOLD
from gateway.tool_runs import (
    AD_TEMPLATE_ROUTE_ORDER,
    TOOL_MODEL_POLICY_SCHEMA,
    ToolRunError,
    validate_generation_records,
    validate_model_policy,
)


logger = logging.getLogger(__name__)


# Vision-capable template roles can spend several minutes inside a provider
# request without yielding a stream delta.  Keep the watchdog finite, but do
# not let the legacy 120-second policy mistake a healthy slow call for a hung
# generator.  Explicit policies may lengthen this bound, never shorten it.
AD_TEMPLATE_MIN_INACTIVITY_SECONDS = 300.0


def _ad_template_inactivity_timeout(route_settings: Dict[str, Any]) -> float:
    configured = max(1.0, float(route_settings.get("timeout_seconds") or 120))
    return max(configured, AD_TEMPLATE_MIN_INACTIVITY_SECONDS)


def _error(message: str, code: str) -> Dict[str, Any]:
    return {"error": {"message": message, "type": "invalid_request_error", "code": code}}


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class ToolRunAPIMixin:
    """Durable Tool-run endpoints mixed into ``APIServerAdapter``."""

    @staticmethod
    def _tool_stage_order() -> List[str]:
        return list(("source", "build", "render", "compare", "final-check", "live"))

    @staticmethod
    def _isolated_tool_role_prompt() -> str:
        return (
            "You are one isolated builder, comparator, or final-review role in "
            "Hermes' sole ad-template process. The user message contains the "
            "complete role contract and every required image input; reason only "
            "from that prompt and those attached images. Do not inspect, list, "
            "search, discover, or read repositories or the filesystem, and do not "
            "use terminal, read_file, search_files, or broad shell searches. In "
            "particular, never traverse /srv or the retired "
            "/opt/ad-template-builder tree. The SoleProcessOrchestrator alone "
            "executes the renderer and Blockwise import; do not run either. Return "
            "exactly one requested JSON object with no prose or code fences."
        )

    @staticmethod
    def _tool_stage_from_process_event(event_kind: Any, node: Any) -> str | None:
        """Project only the orchestrator's explicit event contract into lifecycle state."""
        kind = str(event_kind or "")
        stage = str(node or "")
        if kind == "stage.started":
            return stage if stage in ToolRunAPIMixin._tool_stage_order() else None
        expected = {
            "iteration.started": "build",
            "iteration.rendered": "render",
            "iteration.compared": "compare",
            "iteration.revised": "build",
            "builder.escalated": "build",
            "final-review.started": "final-check",
            "final-review.retried": "final-check",
            "final-review.completed": "final-check",
            "template.imported": "live",
        }.get(kind)
        return stage if expected == stage else None

    @staticmethod
    def _canonical_tool_stage(stage: Any) -> str:
        value = str(stage or "").strip().lower().replace("_", "-").replace(" ", "-")
        aliases = {
            "source": "source",
            "analyse": "build", "analyze": "build", "build": "build",
            "decompose": "build", "restyle": "build", "story-draft": "build",
            "render": "render",
            "compare": "compare", "qa": "compare", "visual-review": "compare",
            "check": "compare", "subject-invariance": "compare", "studio-qa": "compare",
            "final-review": "final-check", "final-check": "final-check", "ready": "final-check",
            "import": "live", "release": "live", "live": "live",
        }
        return aliases.get(value, "source")

    @staticmethod
    def _preview_placement(name: str) -> str | None:
        """Return the placement encoded by the final token of a preview stem."""
        stem = Path(str(name)).stem.lower()
        match = re.search(r"(?:^|[-_.])(feed|story)$", stem)
        return match.group(1) if match else None

    @staticmethod
    def _ingest_tool_sources(command: Dict[str, Any]) -> Dict[str, Any]:
        """Stage the source only for the duration of this run; no vault or hash."""
        payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
        sources = payload.get("sources") if isinstance(payload.get("sources"), list) else []
        if not sources:
            raise ToolRunError("at least one source is required")
        try:
            from hermes_constants import get_hermes_home
            staging_root = (get_hermes_home() / "tool_runs" / "staging").resolve()
        except Exception as exc:
            raise ToolRunError("Hermes run workspace is unavailable") from exc
        shared_root = Path(os.environ.get("FRANK_SHARED_UPLOAD_ROOT", "/srv/frank/data/window/uploads")).resolve()
        allowed = {".avif", ".bmp", ".gif", ".heic", ".heif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
        result = json.loads(json.dumps(command))
        staged = []
        for item in sources:
            if not isinstance(item, dict):
                raise ToolRunError("source must be an object")
            raw_path = item.get("path")
            if not raw_path:
                raise ToolRunError("source image path is required")
            source = Path(str(raw_path)).resolve(strict=True)
            try:
                source.relative_to(shared_root)
            except ValueError as exc:
                raise ToolRunError("source path is outside the Frank upload mount") from exc
            if not source.is_file() or source.suffix.lower() not in allowed:
                raise ToolRunError("source is not a supported image")
            target = staging_root / f"source-{uuid.uuid4().hex}{source.suffix.lower()}"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            staged.append({"name": str(item.get("name") or source.name), "path": str(target), "size": source.stat().st_size})
        result["payload"]["sources"] = staged
        return result

    @staticmethod
    def _copy_source_preview(workspace: Path, source_file: Path) -> Path:
        """Persist the run input behind the same authenticated artifact route as renders."""
        suffix = source_file.suffix.lower() if source_file.suffix else ".png"
        target = workspace / "previews" / f"source{suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_file, target)
        return target

    @staticmethod
    def _durable_tool_source(workspace: Path, raw_source: str) -> Path:
        """Resolve a staged source, or the exact persisted preview on same-run retry."""
        source_file = Path(str(raw_source)).expanduser().resolve()
        if source_file.is_file():
            return ToolRunAPIMixin._copy_source_preview(workspace, source_file)

        preview_root = (workspace / "previews").resolve()
        allowed = {
            ".avif", ".bmp", ".gif", ".heic", ".heif", ".jpeg", ".jpg",
            ".png", ".tif", ".tiff", ".webp",
        }
        candidates = []
        if preview_root.is_dir():
            for candidate in preview_root.glob("source.*"):
                try:
                    resolved = candidate.resolve(strict=True)
                    resolved.relative_to(preview_root)
                except (OSError, ValueError):
                    continue
                if (
                    resolved.is_file()
                    and not candidate.is_symlink()
                    and resolved.suffix.lower() in allowed
                ):
                    candidates.append(resolved)
        if len(candidates) != 1:
            raise RuntimeError("durable run source preview is unavailable")
        return candidates[0]

    def _final_check_checkpoint(self, run_id: str, workspace: Path) -> Dict[str, Any]:
        """Load the last accepted render without rebuilding it on final-check retry."""
        records: List[Dict[str, Any]] = []
        for event in self._tool_run_store.events(run_id, limit=5000):
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            if event.get("kind") == "iteration.compared" and data.get("iteration") == 1:
                records = []
            if event.get("kind") == "final-review.completed" and data.get("decision") == "revise" and records:
                records[-1]["final_review_failed"] = True
            if event.get("kind") != "iteration.compared":
                continue
            comparison = {
                key: data.get(key)
                for key in ("rubric", "reason", "hard_failures", "differences", "required_changes")
            }
            preview_names = data.get("preview_names") if isinstance(data.get("preview_names"), list) else []
            records.append({
                "iteration": data.get("iteration"),
                "comparison": comparison,
                "decision": data.get("decision"),
                "candidate": {
                    "previews": [
                        {
                            "name": str(name),
                            "placement": "feed" if str(name).endswith("-feed.png") else "story",
                        }
                        for name in preview_names
                    ],
                },
            })
        history = validate_iterations(records)
        if history[-1]["decision"] != "accepted":
            raise RuntimeError("final-check retry has no accepted comparator checkpoint")
        workspace_root = workspace.resolve()

        def load_candidate(iteration: int) -> Dict[str, Any]:
            artifact_path = (
                workspace / "iterations" / f"{iteration:02d}" / "artifact.json"
            ).resolve()
            try:
                artifact_path.relative_to(workspace_root)
                artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError("accepted final-check artifact is unavailable") from exc
            if not isinstance(artifact, dict) or set(artifact) != {"template", "assets"}:
                raise RuntimeError("accepted final-check artifact is invalid")
            previews = []
            render = {}
            for placement in ("feed", "story"):
                preview = (
                    workspace / "previews" / f"iteration-{iteration:02d}-{placement}.png"
                ).resolve()
                try:
                    preview.relative_to(workspace_root)
                except ValueError as exc:
                    raise RuntimeError(
                        "accepted final-check preview escapes the run workspace"
                    ) from exc
                render[placement] = str(preview)
                previews.append(
                    {"name": preview.name, "path": str(preview), "placement": placement}
                )
            candidate = {
                **artifact,
                "previews": previews,
                "render": render,
                "template_path": str(artifact_path),
            }
            try:
                validate_artifacts(candidate, workspace)
            except (AdTemplateProcessError, OSError, ValueError) as exc:
                raise RuntimeError("accepted final-check artifact is unavailable") from exc
            return candidate

        accepted_iteration = int(history[-1]["iteration"])
        try:
            candidate = load_candidate(accepted_iteration)
            resume_final_check = True
        except RuntimeError as accepted_error:
            # A gateway/process interruption can occur after the comparator has
            # accepted an iteration but while the final review is running.  An
            # interrupted non-atomic writer historically left the latest
            # artifact/previews empty.  Never review or import that candidate;
            # rebuild the next iteration from the newest intact predecessor and
            # require the comparator and both final reviewers to pass again.
            candidate = None
            for iteration in range(accepted_iteration - 1, 0, -1):
                try:
                    candidate = load_candidate(iteration)
                    break
                except RuntimeError:
                    continue
            if candidate is None:
                raise accepted_error
            history[-1]["final_review_failed"] = True
            resume_final_check = False
        return {
            "candidate": candidate,
            "history": history,
            "previous_score": history[-1]["comparison"]["score"],
            "resume_final_check": resume_final_check,
        }

    @staticmethod
    def _tool_json_output(value: Any, *, process_result: bool = False) -> Dict[str, Any]:
        if process_result:
            required = {
                "template", "iterations", "final_review", "previews",
                "documents", "import", "process",
            }
            if (
                not isinstance(value, dict)
                or value.get("process") != "only-ad-template-process"
                or not required.issubset(value)
            ):
                raise RuntimeError("Sole ad-template process returned an invalid result")
            # The executable orchestrator already returns a structured object;
            # its nested evidence is validated immediately afterward by
            # _prepare_candidate_output. Keep the default agent-transport parser
            # strict so arbitrary direct dictionaries are never accepted here.
            return dict(value)
        text = ""
        if isinstance(value, dict):
            text = str(value.get("final_response") or value.get("output") or "")
        elif value is not None:
            text = str(value)
        text = redact_sensitive_text(text, force=True).strip()
        if text.startswith("```") and text.endswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except (TypeError, json.JSONDecodeError):
            # Some agent transports append a local verifier note after the JSON
            # response.  Decode one complete object, but never persist the
            # trailing transport text as public Tool output.
            start = text.find("{")
            if start >= 0:
                try:
                    parsed, _ = json.JSONDecoder().raw_decode(text[start:])
                    if isinstance(parsed, dict):
                        return parsed
                except (TypeError, json.JSONDecodeError):
                    pass
        raise AdTemplateStructuredOutputError(
            "Builder did not return one structured JSON result"
        )

    @staticmethod
    def _prepare_candidate_output(run_id: str, output: Dict[str, Any]) -> Dict[str, Any]:
        required = {"template", "iterations", "final_review", "previews", "documents", "import"}
        missing = sorted(key for key in required if key not in output)
        if missing:
            raise RuntimeError(f"Generator result is incomplete: {', '.join(missing)}")
        iterations = validate_iterations(output.get("iterations"))
        final_review = validate_final_review(output.get("final_review"), accepted=iterations[-1]["decision"] == "accepted")
        if final_review.get("decision") != "accepted":
            raise RuntimeError("final reviewers did not pass")
        docs = deterministic_documents(output.get("template"))
        imported = output.get("import")
        if not isinstance(imported, dict) or not imported.get("template_id") or not imported.get("status"):
            raise RuntimeError("Blockwise import receipt is missing")
        # The complete layered template and deterministic documents are already
        # stored in the run artifact and imported into Blockwise. Persist only
        # a bounded control-plane projection here: real templates routinely
        # exceed the generic 32k per-string Tool-run ledger limit.
        template = output.get("template") or {}
        metadata = template.get("metadata") if isinstance(template.get("metadata"), dict) else {}
        artifact_ref = output.get("template_path")
        result = {key: output.get(key) for key in (
            "previews", "template_path", "render_path", "import", "process",
            "usage", "cost", "builder_escalated", "builder_route",
            "model_policy_revision",
        )}
        result["template"] = {
            "schema": template.get("schema"),
            "templateId": template.get("templateId"),
            "title": metadata.get("title"),
            "artifact": artifact_ref,
        }
        result["documents"] = {
            name: {"bytes": len(value.encode("utf-8")), "artifact": artifact_ref}
            for name, value in docs.items()
        }
        result["iterations"] = [
            {
                "iteration": item["iteration"],
                "decision": item["decision"],
                "score": item["comparison"]["score"],
                "minimum_score": item["comparison"]["minimum_score"],
                "rubric": item["comparison"]["rubric"],
                "final_review_failed": bool(item.get("final_review_failed")),
            }
            for item in iterations
        ]
        result["final_review"] = {
            "decision": final_review["decision"],
            "threshold": final_review["threshold"],
            "reviewers": [
                {
                    "id": item["id"],
                    "route": item["route"],
                    "score": item["score"],
                    "minimum_score": item["minimum_score"],
                    "rubric": item["rubric"],
                }
                for item in final_review["reviewers"]
            ],
        }
        result["process"] = "only-ad-template-process"
        return result

    @staticmethod
    def _validated_generation_gate(output: Any) -> Dict[str, Any]:
        if not isinstance(output, dict):
            raise ToolRunError("generator output is required")
        iterations = validate_iterations(output.get("iterations"))
        return validate_final_review(output.get("final_review"), accepted=iterations[-1]["decision"] == "accepted")

    def _project_generation_events(self, run_id: str, records: List[Dict[str, Any]]) -> None:
        # The executable orchestrator persists each real comparator and reviewer
        # event as it happens. Never reconstruct or synthesize process truth.
        return None

    def _tool_candidate(self, run: Dict[str, Any], stage: str) -> Dict[str, str]:
        stages = (run.get("model_policy") or {}).get("stages") or {}
        selected = stages.get(stage) or stages.get("analyse") or {}
        primary = selected.get("primary") or {}
        return {
            "provider": str(primary.get("provider") or "").strip(),
            "model": str(primary.get("model") or self._model_name).strip(),
        }

    def _tool_candidates(self, run: Dict[str, Any], stage: str) -> tuple[List[Dict[str, str]], Dict[str, Any]]:
        stages = (run.get("model_policy") or {}).get("stages") or {}
        settings = stages.get(stage) or stages.get("analyse") or {}
        raw = [settings.get("primary"), *(settings.get("fallbacks") or [])]
        candidates = [
            {"provider": str(item.get("provider") or "").strip(), "model": str(item.get("model") or "").strip()}
            for item in raw if isinstance(item, dict) and item.get("model")
        ]
        return candidates[: int(settings.get("max_attempts") or len(candidates) or 1)], settings

    def _tool_prompt(self, run: Dict[str, Any], *, finalize: bool = False) -> str:
        payload = run.get("payload") or {}
        sources = payload.get("sources") or []
        source = (sources[0] or {}).get("path") if isinstance(sources[0], dict) else ""
        return generator_prompt(run_id=run["run_id"],
            project_id=str((run.get("scope") or {}).get("project_id") or ""),
            brief=str(payload.get("brief") or ""), placements=payload.get("placements") or [],
            source=str(source or ""))

    def _drain_tool_future(self, future: "asyncio.Future[Any]") -> None:
        """Consume a stopped executor future, including any late exception."""
        async def consume() -> None:
            try:
                await asyncio.shield(future)
            except BaseException:
                # The Tool run already records the redacted failure. Retrieving
                # the late exception here prevents an unhandled-future warning.
                pass

        task = asyncio.create_task(consume())
        drains = getattr(self, "_tool_run_drain_tasks", None)
        if drains is None:
            drains = set()
            self._tool_run_drain_tasks = drains
        drains.add(task)
        task.add_done_callback(drains.discard)
        background = getattr(self, "_background_tasks", None)
        if background is not None:
            background.add(task)
            task.add_done_callback(background.discard)

    def _start_tool_task(self, run_id: str, *, finalize: bool = False) -> None:
        task = self._tool_run_tasks.get(run_id)
        if task is not None and not task.done():
            return
        task = asyncio.create_task(self._execute_tool_run(run_id, finalize=finalize))
        self._tool_run_tasks[run_id] = task
        try:
            self._background_tasks.add(task)
        except TypeError:
            pass
        if hasattr(task, "add_done_callback"):
            task.add_done_callback(self._background_tasks.discard)

    async def _execute_tool_run(self, run_id: str, *, finalize: bool = False) -> None:
        current_stage = "source"
        stop_events = getattr(self, "_tool_run_stop_events", None)
        if stop_events is None:
            stop_events = {}
            self._tool_run_stop_events = stop_events
        stop_event = threading.Event()
        stop_events[run_id] = stop_event
        try:
            run = self._tool_run_store.get_run(run_id)
            run["model_policy"] = validate_model_policy(
                run.get("model_policy"), tool_id=str(run.get("tool_id") or ""),
            )
            current_stage = self._canonical_tool_stage(run.get("stage"))
            self._tool_run_store.update_run(
                run_id, status="running", stage=current_stage,
                progress=max(0.02, float(run.get("progress") or 0)),
            )
            self._tool_run_store.append_event(
                run_id, "stage.started", status="running", node_id=current_stage,
                data={"summary": "Starting sole ad-template process"},
            )
            route_stage = "analyse"
            candidates, route_settings = self._tool_candidates(run, route_stage)
            if not candidates:
                raise RuntimeError(f"No compatible model configured for {route_stage}")
            loop = asyncio.get_running_loop()
            reported_cost = 0.0
            cost_limit = float(route_settings.get("max_cost_usd") or 0)
            activity_sequence = 0
            last_activity_at = time.monotonic()

            def mark_activity() -> None:
                nonlocal activity_sequence, last_activity_at
                activity_sequence += 1
                last_activity_at = time.monotonic()

            def progress_callback(event_type: str, tool_name: str = None, preview: str = None, args=None, **kwargs):
                nonlocal reported_cost
                normalized_event = {
                    "generation-started": "iteration.started",
                    "generation-rendered": "iteration.rendered",
                    "generation-scored": "iteration.compared",
                    "generation-revision-requested": "iteration.revised",
                    "generation-accepted": "iteration.compared",
                }.get(str(event_type), str(event_type))
                process_events = {"iteration.started", "iteration.rendered", "iteration.compared", "iteration.revised", "final-review.started", "final-review.completed"}
                if event_type not in {"tool.started", "tool.completed", "subagent.start", "subagent.complete"} and normalized_event not in process_events:
                    return
                if normalized_event in process_events:
                    mark_activity()
                    safe_data = {key: kwargs[key] for key in ("iteration", "score", "reason", "decision") if kwargs.get(key) is not None}
                    self._tool_run_store.append_event(
                        run_id, normalized_event, status="error" if kwargs.get("is_error") else "running",
                        node_id="compare" if normalized_event.startswith("iteration.") else "final-check", data=safe_data,
                    )
                    return
                mark_activity()
                data: Dict[str, Any] = {}
                if tool_name:
                    data["tool"] = str(tool_name)[:160]
                # Tool previews can contain command text, private filesystem paths,
                # prompt fragments, or provider payloads.  They are useful inside
                # Hermes while deriving activity, but are never part of Frank's
                # durable operator event contract.
                if event_type == "tool.completed":
                    data["duration_seconds"] = round(float(kwargs.get("duration", 0) or 0), 3)
                    data["error"] = bool(kwargs.get("is_error"))
                for key in ("status", "model", "cost_usd", "input_tokens", "output_tokens"):
                    if kwargs.get(key) is not None:
                        data[key] = kwargs[key]
                if kwargs.get("cost_usd") is not None:
                    reported_cost = max(reported_cost, float(kwargs["cost_usd"]))
                    if cost_limit and reported_cost > cost_limit:
                        raise RuntimeError(f"{route_stage} exceeded its ${cost_limit:.2f} cost limit")
                self._tool_run_store.append_event(
                    run_id, event_type, status="error" if data.get("error") else "running",
                    node_id=current_stage, data=data,
                )

            def role_heartbeat(*_args, **_kwargs) -> None:
                mark_activity()

            def run_sync(_candidate: Dict[str, str]):
                from hermes_constants import get_hermes_home
                workspace = (get_hermes_home() / "tool_runs" / "ad-template-generator" / run_id).resolve()
                workspace.mkdir(parents=True, exist_ok=True)
                usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "estimated_cost_usd": 0.0}
                run_cost_limit = float(os.environ.get("AD_TEMPLATE_MAX_RUN_COST_USD", "10.0"))

                def should_stop() -> bool:
                    if stop_event.is_set():
                        return True
                    try:
                        return bool(self._tool_run_store.get_run(run_id).get("cancel_requested"))
                    except Exception:
                        # Side effects fail closed if cancellation state can no
                        # longer be read from the durable ledger.
                        return True

                def call_agent(instance_id: str, prompt: Any, route_name: str):
                    provider, model = route_name.split("/", 1)
                    route = self._resolve_route(model)
                    agent = self._create_agent(
                        ephemeral_system_prompt=self._isolated_tool_role_prompt(),
                        session_id=f"{run_id}:{instance_id}:{uuid.uuid4().hex}",
                        tool_progress_callback=progress_callback,
                        requested_model=model, requested_provider=provider, route=route,
                        persistence_disabled=True,
                        enabled_toolsets_override=[],
                        stream_delta_callback=role_heartbeat,
                        reasoning_callback=role_heartbeat,
                    )
                    active = self._tool_run_agents.setdefault(run_id, {})
                    active[instance_id] = agent
                    try:
                        result = agent.run_conversation(user_message=prompt, conversation_history=[], task_id=f"{run_id}:{instance_id}")
                        usage["input_tokens"] += getattr(agent, "session_prompt_tokens", 0) or 0
                        usage["output_tokens"] += getattr(agent, "session_completion_tokens", 0) or 0
                        usage["total_tokens"] += getattr(agent, "session_total_tokens", 0) or 0
                        usage["estimated_cost_usd"] += float(getattr(agent, "session_estimated_cost_usd", 0.0) or 0.0)
                        if run_cost_limit > 0 and usage["estimated_cost_usd"] > run_cost_limit:
                            raise RuntimeError(f"sole ad-template process exceeded whole-run cost limit {run_cost_limit:.2f}")
                        return self._tool_json_output(result)
                    finally:
                        active.pop(instance_id, None)
                        if not active:
                            self._tool_run_agents.pop(run_id, None)
                routes = []
                for role in AD_TEMPLATE_ROUTE_ORDER:
                    stage_candidates, _ = self._tool_candidates(run, role)
                    if not stage_candidates:
                        raise RuntimeError(f"No route configured for {role}")
                    routes.append(stage_candidates[0])
                route_names = [f"{item['provider']}/{item['model']}" for item in routes]
                def emit(kind: str, node: str, data: Dict[str, Any]):
                    nonlocal current_stage
                    event_stage = self._tool_stage_from_process_event(kind, node)
                    if event_stage is not None and event_stage != current_stage:
                        current_stage = event_stage
                        order = self._tool_stage_order()
                        progress = min(0.90, max(0.03, order.index(event_stage) / len(order)))
                        self._tool_run_store.update_run(run_id, stage=event_stage, progress=progress)
                        if kind != "stage.started":
                            self._tool_run_store.append_event(
                                run_id, "stage.started", status="running", node_id=event_stage,
                                data={"summary": f"Started {event_stage.replace('-', ' ')}"},
                            )
                            mark_activity()
                    self._tool_run_store.append_event(run_id, kind, status="ok" if kind.endswith(("completed", "compared", "imported")) else "running", node_id=node, data=data)
                    mark_activity()
                payload = run.get("payload") or {}
                source_items = payload.get("sources") or []
                source = str((source_items[0] or {}).get("path") if source_items and isinstance(source_items[0], dict) else "")
                if not source:
                    raise RuntimeError("source image is missing")
                source = str(self._durable_tool_source(workspace, source))
                checkpoint = self._final_check_checkpoint(run_id, workspace) if current_stage == "final-check" else None
                result = SoleProcessOrchestrator(
                    call_agent=call_agent, workspace=workspace, run_id=run_id,
                    project_id=str((run.get("scope") or {}).get("project_id") or ""), emit=emit,
                    should_stop=should_stop,
                ).run(
                    source=source, brief=str(payload.get("brief") or ""),
                    placements=payload.get("placements") or [], routes=routes,
                    require_quality_route=True,
                    resume_final_check=bool((checkpoint or {}).get("resume_final_check")),
                    revision_candidate=(checkpoint or {}).get("candidate"),
                    history=(checkpoint or {}).get("history"),
                    total_iterations=len((checkpoint or {}).get("history") or []),
                    previous_score=(checkpoint or {}).get("previous_score"),
                )
                return result, usage

            result = usage = None
            failures = []
            configured_timeout = max(
                1.0, float(route_settings.get("timeout_seconds") or 120)
            )
            timeout = _ad_template_inactivity_timeout(route_settings)
            for attempt, candidate in enumerate(candidates, start=1):
                self._tool_run_store.append_event(
                    run_id, "provider.attempt", status="running", node_id=route_stage,
                    data={
                        "attempt": attempt,
                        "provider": candidate["provider"],
                        "model": candidate["model"],
                        "configured_timeout_seconds": configured_timeout,
                        "timeout_seconds": timeout,
                    },
                )
                future = None
                try:
                    future = loop.run_in_executor(None, run_sync, candidate)
                    observed_activity = activity_sequence
                    # Anchor the inactivity deadline to the instant activity
                    # happened, not the next polling instant. Otherwise a
                    # heartbeat just after a poll can grant nearly two full
                    # inactivity windows to a silent role.
                    deadline = last_activity_at + timeout
                    while True:
                        if activity_sequence != observed_activity:
                            observed_activity = activity_sequence
                            deadline = last_activity_at + timeout
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise asyncio.TimeoutError(
                                f"{current_stage} made no recorded progress for {timeout:.0f} seconds"
                            )
                        try:
                            result, usage = await asyncio.wait_for(
                                asyncio.shield(future), timeout=min(1.0, remaining)
                            )
                            break
                        except asyncio.TimeoutError:
                            if future.done():
                                result, usage = await future
                                break
                    break
                except asyncio.CancelledError:
                    stop_event.set()
                    running_agents = self._tool_run_agents.get(run_id)
                    if running_agents is not None:
                        agents = running_agents.values() if isinstance(running_agents, dict) else [running_agents]
                        for active_agent in list(agents):
                            request_hard_interrupt(active_agent, "api_server_tool_run_cancel")
                    if future is not None:
                        self._drain_tool_future(future)
                    raise
                except Exception as exc:
                    running_agents = self._tool_run_agents.get(run_id)
                    if isinstance(exc, asyncio.TimeoutError):
                        stop_event.set()
                        if running_agents is not None:
                            agents = running_agents.values() if isinstance(running_agents, dict) else [running_agents]
                            for active_agent in list(agents):
                                request_hard_interrupt(active_agent, "api_server_tool_run_stage_timeout")
                        if future is not None:
                            self._drain_tool_future(future)
                    failure = redact_sensitive_text(str(exc), force=True)[:600]
                    failures.append(failure)
                    if attempt < len(candidates):
                        next_candidate = candidates[attempt]
                        self._tool_run_store.update_run(run_id, attention=True)
                        self._tool_run_store.append_event(
                            run_id, "provider.fallback", status="blocked", node_id=route_stage,
                            data={"attempt": attempt, "from_model": candidate["model"], "to_model": next_candidate["model"], "reason": failure},
                        )
            if result is None:
                raise RuntimeError(f"All configured {route_stage} model attempts failed: {'; '.join(failures)}")
            refreshed = self._tool_run_store.get_run(run_id)
            if refreshed.get("cancel_requested"):
                self._tool_run_store.update_run(run_id, status="cancelled", attention=True)
                self._tool_run_store.append_event(run_id, "run.cancelled", status="cancelled", node_id=current_stage, data={})
                return
            if isinstance(result, dict) and result.get("failed"):
                raise RuntimeError(redact_sensitive_text(str(result.get("error") or "Tool execution failed"), force=True))
            output = self._tool_json_output(result, process_result=True)
            output["usage"] = usage
            builder_cost = output.get("cost")
            output["cost"] = dict(builder_cost) if isinstance(builder_cost, dict) else ({"builder_reported": builder_cost} if builder_cost is not None else {})
            output["cost"]["reported_usd"] = usage.get("estimated_cost_usd") or reported_cost
            output["cost"]["estimated_usd"] = usage.get("estimated_cost_usd", 0.0)
            output["model_policy_revision"] = refreshed["model_policy_revision"]
            output = self._prepare_candidate_output(run_id, output)
            self._project_generation_events(run_id, output["iterations"])
            self._tool_run_store.update_run(
                run_id, status="completed", stage="live", progress=1,
                output=output, attention=False,
            )
            self._tool_run_store.append_event(
                run_id, "template.imported", status="ok", node_id="live",
                data=dict(output["import"]),
            )
        except asyncio.CancelledError:
            try:
                if getattr(self, "_tool_run_shutdown", False):
                    self._tool_run_store.append_event(
                        run_id, "run.interrupted", status="blocked", node_id=current_stage,
                        data={"reason": "gateway-shutdown", "will_resume": True},
                    )
                else:
                    self._tool_run_store.update_run(run_id, status="cancelled", attention=True)
                    self._tool_run_store.append_event(run_id, "run.cancelled", status="cancelled", node_id=current_stage, data={})
            except Exception:
                pass
            raise
        except Exception as exc:
            logger.exception("durable Tool run failed: %s", run_id)
            error = redact_sensitive_text(str(exc), force=True)
            try:
                self._tool_run_store.update_run(run_id, status="failed", error=error, attention=True)
                self._tool_run_store.append_event(run_id, "run.failed", status="error", node_id=current_stage, data={"error": error[:2000]})
            except Exception:
                pass
        finally:
            try:
                completed = self._tool_run_store.get_run(run_id)
                for source in (completed.get("payload") or {}).get("sources") or []:
                    raw = source.get("path") if isinstance(source, dict) else ""
                    candidate = Path(str(raw))
                    if "tool_runs" in candidate.parts and "staging" in candidate.parts:
                        candidate.unlink(missing_ok=True)
            except (KeyError, OSError):
                pass
            stop_event.set()
            if stop_events.get(run_id) is stop_event:
                stop_events.pop(run_id, None)
            self._tool_run_agents.pop(run_id, None)
            self._tool_run_tasks.pop(run_id, None)

    async def _handle_create_tool_run(self, request: web.Request) -> web.Response:
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        limited = self._concurrency_limited_response()
        if limited is not None:
            return limited
        try:
            body = await request.json()
            command = body.get("command") if isinstance(body, dict) and "command" in body else body
            command = await asyncio.to_thread(self._ingest_tool_sources, command)
            run, created = self._tool_run_store.create_run(command)
            if run["tool_id"] != "ad-template-generator" or run["action"] != "build-template":
                if created:
                    self._tool_run_store.update_run(run["run_id"], status="failed", error="No registered Tool executor", attention=True)
                return web.json_response(_error("No registered Tool executor", "tool_executor_missing"), status=422)
            if created:
                self._start_tool_task(run["run_id"])
            return web.json_response(self._tool_run_store.get_run(run["run_id"]), status=202 if created else 200)
        except (ToolRunError, ValueError, TypeError) as exc:
            return web.json_response(_error(str(exc), "invalid_tool_run"), status=400)

    async def _handle_list_tool_runs(self, request: web.Request) -> web.Response:
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        try:
            runs = self._tool_run_store.list_runs(
                tool_id=request.query.get("tool_id") or None,
                project_id=request.query.get("project_id") or None,
                limit=int(request.query.get("limit", "100")),
            )
            return web.json_response({"object": "list", "data": runs})
        except (ToolRunError, ValueError) as exc:
            return web.json_response(_error(str(exc), "invalid_tool_run_query"), status=400)

    async def _handle_get_tool_run(self, request: web.Request) -> web.Response:
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        try:
            return web.json_response(self._tool_run_store.get_run(request.match_info["run_id"]))
        except KeyError as exc:
            return web.json_response(_error(str(exc), "tool_run_not_found"), status=404)

    async def _handle_download_tool_run(self, request: web.Request) -> web.StreamResponse:
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        try:
            run = self._tool_run_store.get_run(request.match_info["run_id"])
            if run.get("status") != "completed":
                raise ToolRunError("template is not ready")
            output = run.get("output") if isinstance(run.get("output"), dict) else {}
            raw_path = output.get("template_path")
            if not raw_path:
                raise ToolRunError("deterministic template file is unavailable")
            from hermes_constants import get_hermes_home
            root = (get_hermes_home() / "tool_runs").resolve()
            target = Path(str(raw_path)).resolve(strict=True)
            target.relative_to(root)
            if target.suffix.lower() not in {".json", ".svg", ".png", ".webp"} or not target.is_file():
                raise ToolRunError("deterministic template file is invalid")
            return web.FileResponse(target, headers={"Content-Disposition": f'attachment; filename="{target.name}"'})
        except KeyError as exc:
            return web.json_response(_error(str(exc), "tool_run_not_found"), status=404)
        except (ToolRunError, OSError, ValueError) as exc:
            return web.json_response(_error(str(exc), "tool_pack_unavailable"), status=409)

    async def _handle_tool_run_artifact(self, request: web.Request) -> web.StreamResponse:
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        run_id = request.match_info["run_id"]
        name = request.match_info["name"]
        try:
            self._tool_run_store.get_run(run_id)
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", name):
                raise ToolRunError("invalid artifact name")
            from hermes_constants import get_hermes_home
            root = (
                get_hermes_home() / "tool_runs" / "ad-template-generator" /
                run_id / "previews"
            ).resolve()
            target = (root / name).resolve(strict=True)
            target.relative_to(root)
            if target.is_symlink() or not target.is_file() or target.suffix.lower() not in {".avif", ".bmp", ".gif", ".heic", ".heif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp", ".svg"}:
                raise ToolRunError("preview artifact is unavailable")
            return web.FileResponse(target, headers={"Cache-Control": "private, no-store"})
        except KeyError as exc:
            return web.json_response(_error(str(exc), "tool_run_not_found"), status=404)
        except (ToolRunError, OSError, ValueError) as exc:
            return web.json_response(_error(str(exc), "tool_artifact_unavailable"), status=404)

    async def _handle_tool_run_events(self, request: web.Request) -> web.StreamResponse:
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        run_id = request.match_info["run_id"]
        try:
            self._tool_run_store.get_run(run_id)
            cursor = int(request.query.get("after") or request.headers.get("Last-Event-ID") or "-1")
            if cursor < -1:
                raise ValueError("invalid event cursor")
        except KeyError as exc:
            return web.json_response(_error(str(exc), "tool_run_not_found"), status=404)
        except (ToolRunError, ValueError) as exc:
            return web.json_response(_error(str(exc), "invalid_event_cursor"), status=400)
        response = web.StreamResponse(status=200, headers={
            "Content-Type": "text/event-stream", "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        })
        await response.prepare(request)
        idle = 0
        try:
            while True:
                events = self._tool_run_store.events(run_id, after=cursor)
                for event in events:
                    cursor = event["sequence"]
                    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                    await response.write(f"id: {cursor}\nevent: {event['kind']}\ndata: {payload}\n\n".encode())
                    idle = 0
                run = self._tool_run_store.get_run(run_id)
                if run["status"] in {"completed", "failed", "cancelled"} and not events:
                    break
                if not events:
                    idle += 1
                    if idle >= 30:
                        await response.write(b": keepalive\n\n")
                        idle = 0
                await asyncio.sleep(0.5)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return response

    async def _handle_list_tool_policies(self, request: web.Request) -> web.Response:
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        try:
            return web.json_response({"object": "list", "data": self._tool_run_store.list_policies(
                request.match_info["tool_id"], project_id=request.query.get("project_id") or None,
            )})
        except ToolRunError as exc:
            return web.json_response(_error(str(exc), "invalid_tool_id"), status=400)

    async def _handle_create_tool_policy(self, request: web.Request) -> web.Response:
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        try:
            body = await request.json()
            policy = body.get("policy") if isinstance(body, dict) and "policy" in body else body
            project_id = body.get("project_id", "") if isinstance(body, dict) else ""
            return web.json_response(self._tool_run_store.create_policy(
                request.match_info["tool_id"], policy, project_id=str(project_id or ""),
            ), status=201)
        except (ToolRunError, ValueError, TypeError) as exc:
            return web.json_response(_error(str(exc), "invalid_model_policy"), status=400)

    async def _handle_tool_run_models(self, request: web.Request) -> web.Response:
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        try:
            from hermes_cli.inventory import build_model_options_payload, load_picker_context
            payload = await asyncio.to_thread(lambda: build_model_options_payload(load_picker_context(), include_unconfigured=True, refresh=False))
            payload["policy_schema"] = TOOL_MODEL_POLICY_SCHEMA
            providers = payload.get("providers") if isinstance(payload.get("providers"), list) else []
            readiness = {
                str(item.get("slug") or ""): bool(item.get("authenticated"))
                for item in providers if isinstance(item, dict)
            }
            checked_at = "2026-08-14T03:24:31Z"

            def capability(provider: str, model: str, name: str) -> Dict[str, Any]:
                return {
                    "provider": provider,
                    "model": model,
                    "capabilities": [name],
                    "available": readiness.get(provider, False),
                    "credential_ready": readiness.get(provider, False),
                    "estimated_price": None,
                    "price_checked_at": checked_at,
                    "pricing_stale": True,
                }

            payload["ad_studio_capabilities"] = [
                capability("deepseek", "deepseek-v4-flash-vision-exp", "vision_structured"),
                capability("openai-codex", "gpt-5.6-luna", "vision_structured"),
                capability("gemini", "gemini-3.1-flash-image", "masked_image_edit"),
                capability("gemini", "gemini-3-pro-image", "masked_image_edit"),
                capability("openai-api", "gpt-image-2", "masked_image_edit"),
            ]
            return web.json_response(payload)
        except Exception:
            logger.exception("failed to list Tool models")
            return web.json_response(_error("Failed to list Tool models", "tool_models_failed"), status=500)

    async def _handle_tool_run_model_change(self, request: web.Request) -> web.Response:
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        try:
            body = await request.json()
            policy = body.get("policy") if isinstance(body, dict) else None
            return web.json_response(self._tool_run_store.replace_remaining_policy(request.match_info["run_id"], policy))
        except KeyError as exc:
            return web.json_response(_error(str(exc), "tool_run_not_found"), status=404)
        except (ToolRunError, ValueError, TypeError) as exc:
            return web.json_response(_error(str(exc), "invalid_model_policy"), status=400)

    async def _handle_retry_tool_run(self, request: web.Request) -> web.Response:
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        run_id = request.match_info["run_id"]
        try:
            run = self._tool_run_store.get_run(run_id)
            if run["status"] not in {"failed", "cancelled", "blocked"}:
                raise ToolRunError("only failed, cancelled, or blocked Tool runs can be retried")
            run = self._tool_run_store.requeue(run_id)
            self._start_tool_task(run_id, finalize=run.get("stage") == "release")
            return web.json_response(run, status=202)
        except KeyError as exc:
            return web.json_response(_error(str(exc), "tool_run_not_found"), status=404)
        except ToolRunError as exc:
            return web.json_response(_error(str(exc), "invalid_tool_retry"), status=400)

    async def _handle_cancel_tool_run(self, request: web.Request) -> web.Response:
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        run_id = request.match_info["run_id"]
        try:
            run = self._tool_run_store.request_cancel(run_id)
            stop_event = getattr(self, "_tool_run_stop_events", {}).get(run_id)
            if stop_event is not None:
                stop_event.set()
            agents = self._tool_run_agents.get(run_id)
            if agents is not None:
                values = agents.values() if isinstance(agents, dict) else [agents]
                for active_agent in list(values):
                    request_hard_interrupt(active_agent, "api_server_tool_run_cancel")
            task = self._tool_run_tasks.get(run_id)
            if task is not None and not task.done():
                task.cancel()
            elif run["status"] not in {"completed", "failed", "cancelled"}:
                self._tool_run_store.update_run(run_id, status="cancelled", attention=True)
                self._tool_run_store.append_event(run_id, "run.cancelled", status="cancelled", node_id=run.get("stage"), data={})
            return web.json_response(self._tool_run_store.get_run(run_id), status=202)
        except KeyError as exc:
            return web.json_response(_error(str(exc), "tool_run_not_found"), status=404)
