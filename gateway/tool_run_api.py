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
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

from aiohttp import web

from agent.interrupt_compat import request_hard_interrupt
from agent.redact import redact_sensitive_text
from gateway.ad_template_process import AdTemplateProcessError, AdTemplateRendererRejection, AdTemplateStructuredOutputError, AdTemplateTransportError, SoleProcessOrchestrator, _compact_revision_feedback, _quality_ranking_score, _renderer_rejection_instructions, validate_artifacts, validate_builder_candidate, validate_iterations, validate_final_review, deterministic_documents, generator_prompt, run_generator_cli, THRESHOLD as AD_TEMPLATE_THRESHOLD
from gateway.tool_runs import (
    AD_TEMPLATE_ROUTE_ORDER,
    AD_TEMPLATE_OPTIONAL_ROUTE,
    TOOL_MODEL_POLICY_SCHEMA,
    ToolRunError,
    ad_template_model_catalog,
    validate_generation_records,
    validate_model_policy,
)


logger = logging.getLogger(__name__)


# Vision-capable template roles can spend more than two minutes inside a
# provider request without yielding a stream delta (the production Sol builder
# has completed at about 144 seconds).  Keep a small safety margin above that
# observed latency, while bounding a silent comparator tightly enough for the
# role-owned Sol fallback to run instead of waiting five minutes.  The helper is
# evaluated again whenever the process changes role/stage so future role floors
# can diverge without changing the watchdog contract.
AD_TEMPLATE_MIN_INACTIVITY_SECONDS = 180.0
AD_TEMPLATE_STAGE_INACTIVITY_MULTIPLIERS = {
    "build": 1.0,
    "render": 1.0,
    "compare": 1.0,
    "final-check": 1.0,
    "live": 1.0,
}


def _ad_template_inactivity_timeout(
    route_settings: Dict[str, Any], *, stage: str | None = None,
) -> float:
    configured = max(1.0, float(route_settings.get("timeout_seconds") or 120))
    stage_floor = AD_TEMPLATE_MIN_INACTIVITY_SECONDS * (
        AD_TEMPLATE_STAGE_INACTIVITY_MULTIPLIERS.get(str(stage or ""), 1.0)
    )
    return max(configured, stage_floor)


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
                for key in (
                    "rubric", "reason", "hard_failures", "visible_strings", "differences",
                    "required_changes", "ranked_changes", "macro", "critical_regions",
                    "regressions", "declared_decision",
                )
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
            "best_quality_score": _quality_ranking_score(
                history[-1]["comparison"]
            ),
            "resume_final_check": resume_final_check,
        }

    @staticmethod
    def _load_ad_template_iteration_candidate(
        workspace: Path, iteration: int
    ) -> Dict[str, Any]:
        workspace_root = workspace.resolve()
        artifact_path = (
            workspace / "iterations" / f"{iteration:02d}" / "artifact.json"
        ).resolve()
        try:
            artifact_path.relative_to(workspace_root)
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("persisted iteration artifact is unavailable") from exc
        if not isinstance(artifact, dict) or set(artifact) != {"template", "assets"}:
            raise RuntimeError("persisted iteration artifact is invalid")
        previews = []
        render = {}
        for placement in ("feed", "story"):
            preview = (
                workspace / "previews" / f"iteration-{iteration:02d}-{placement}.png"
            ).resolve()
            try:
                preview.relative_to(workspace_root)
            except ValueError as exc:
                raise RuntimeError("persisted iteration preview escapes run workspace") from exc
            render[placement] = str(preview)
            previews.append({
                "name": preview.name,
                "path": str(preview),
                "placement": placement,
            })
        candidate = {
            **artifact,
            "previews": previews,
            "render": render,
            "template_path": str(artifact_path),
        }
        try:
            validate_artifacts(candidate, workspace)
        except (AdTemplateProcessError, OSError, ValueError) as exc:
            raise RuntimeError("persisted iteration candidate is unavailable") from exc
        return candidate

    def _ad_template_iteration_checkpoint(
        self, run_id: str, workspace: Path, current_stage: str
    ) -> Dict[str, Any] | None:
        """Load the last complete append-only boundary for restart continuation."""
        records: List[Dict[str, Any]] = []
        state: Dict[str, Any] | None = None
        for iteration in range(1, 61):
            path = workspace / "iterations" / f"{iteration:02d}" / "checkpoint.json"
            if not path.is_file():
                break
            try:
                checkpoint = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("persisted iteration checkpoint is unreadable") from exc
            if (
                not isinstance(checkpoint, dict)
                or checkpoint.get("schema") != "hermes.ad-template-iteration-checkpoint.v1"
                or checkpoint.get("iteration") != iteration
                or not isinstance(checkpoint.get("record"), dict)
            ):
                raise RuntimeError("persisted iteration checkpoint is invalid")
            records.append(checkpoint["record"])
            state = checkpoint
        if not records or state is None:
            if current_stage == "final-check":
                return self._final_check_checkpoint(run_id, workspace)
            return None
        history = validate_iterations(records)
        best_iteration = int(state.get("best_iteration") or 0)
        if not 1 <= best_iteration <= len(history):
            raise RuntimeError("persisted best iteration is invalid")
        candidate = self._load_ad_template_iteration_candidate(workspace, best_iteration)
        last = history[-1]
        resume_final_check = (
            current_stage == "final-check"
            and last["decision"] == "accepted"
            and not last.get("final_review_failed")
        )
        selected_route = state.get("builder_route")
        if not isinstance(selected_route, dict):
            raise RuntimeError("persisted builder route is invalid")
        best_quality_score = state.get("best_quality_score")
        if not isinstance(best_quality_score, (int, float)):
            # Checkpoints written before best_quality_score existed carried
            # only the latest render score. Recompute from the persisted best
            # review rather than assigning a regressing render's score to the
            # immutable best candidate.
            best_quality_score = _quality_ranking_score(
                history[best_iteration - 1]["comparison"]
            )
        raw_feedback = str(state.get("feedback") or "")
        try:
            decoded_feedback = json.loads(raw_feedback) if raw_feedback else {}
        except json.JSONDecodeError:
            decoded_feedback = {
                "instruction": "Revise the immutable best-so-far candidate; do not continue from a regressing render.",
                "best_quality_score": best_quality_score,
                "best_review": history[best_iteration - 1]["comparison"],
                "current_review": last["comparison"],
            }
        retry_intent = self._build_from_best_retry_intent(run_id, current_stage)
        if retry_intent is not None:
            if (
                retry_intent.get("seed_iteration") != best_iteration
                or retry_intent.get("history_length") != len(history)
            ):
                raise RuntimeError("build-from-best retry seed no longer matches its checkpoint")
            decoded_feedback = retry_intent.get("seed_feedback")
            if not isinstance(decoded_feedback, str) or not decoded_feedback:
                raise RuntimeError("build-from-best retry has no persisted terminal feedback")
        return {
            "candidate": candidate,
            "history": history,
            "previous_score": state.get("previous_score"),
            "best_quality_score": float(best_quality_score),
            "resume_final_check": resume_final_check,
            "best_iteration": best_iteration,
            "selected_builder_route": selected_route,
            "builder_escalated": bool(state.get("builder_escalated")),
            "low_gain_streak": int(state.get("low_gain_streak") or 0),
            "feedback": _compact_revision_feedback(decoded_feedback),
            "iteration_budget_extension": (
                int(retry_intent.get("iteration_budget_extension") or 0)
                if retry_intent is not None else 0
            ),
        }

    def _build_from_best_retry_intent(
        self, run_id: str, current_stage: str
    ) -> Dict[str, Any] | None:
        if current_stage != "build":
            return None
        for event in reversed(self._tool_run_store.events(run_id, limit=5000)):
            if event.get("kind") != "command.queued":
                continue
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            return data if data.get("retry_mode") == "build-from-best" else None
        return None

    def _terminal_failure_seed_feedback(
        self,
        run: Dict[str, Any],
        checkpoint: Dict[str, Any],
        renderer_instruction: str,
    ) -> str:
        raw_feedback = checkpoint.get("feedback")
        try:
            base = json.loads(raw_feedback) if isinstance(raw_feedback, str) else {}
        except json.JSONDecodeError:
            base = {}
        if not isinstance(base, dict):
            base = {}
        terminal_reasons: List[str] = []
        final_hard_failures: List[str] = []
        final_required_changes: List[str] = []
        final_events: List[Dict[str, Any]] = []
        for event in self._tool_run_store.events(str(run["run_id"]), limit=5000):
            if event.get("kind") == "iteration.compared":
                final_events = []
                continue
            if str(event.get("kind") or "").startswith("final-review."):
                final_events.append(event)
        for event in final_events[-16:]:
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            reason = data.get("reason")
            if reason:
                terminal_reasons.append(redact_sensitive_text(str(reason), force=True)[:600])
            reviewers = data.get("reviewers") if isinstance(data.get("reviewers"), list) else []
            for reviewer in reviewers[:2]:
                if not isinstance(reviewer, dict):
                    continue
                final_hard_failures.extend(
                    str(item)[:600] for item in (reviewer.get("hard_failures") or [])[:6]
                )
                final_required_changes.extend(
                    str(item)[:1200] for item in (reviewer.get("required_changes") or [])[:6]
                )
                if reviewer.get("reason"):
                    terminal_reasons.append(str(reviewer["reason"])[:600])
        if run.get("error"):
            terminal_reasons.append(
                redact_sensitive_text(str(run["error"]), force=True)[:600]
            )
        terminal_reason = "; ".join(dict.fromkeys(terminal_reasons))[:1200]
        current_review = dict(base)
        current_review["hard_failures"] = list(dict.fromkeys([
            *final_hard_failures,
            *list(base.get("hard_failures") or []),
            *([f"Terminal final review did not pass: {terminal_reason}"] if terminal_reason else []),
            renderer_instruction,
        ]))
        current_review["required_changes"] = list(dict.fromkeys([
            renderer_instruction,
            *final_required_changes,
            *list(base.get("required_changes") or []),
        ]))
        current_review["reason"] = terminal_reason or str(base.get("reason") or "")
        return _compact_revision_feedback({
            "instruction": (
                "Resume at build from the validated immutable best after terminal final-review "
                "failure; preserve every source invariant and apply the current renderer fix."
            ),
            "best_quality_score": checkpoint.get("best_quality_score"),
            "best_review": current_review,
            "current_review": current_review,
            "source_invariants": base.get("source_invariants"),
        })

    @staticmethod
    def _checkpoint_builder_candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
        """Project a rendered checkpoint back to the exact builder contract."""
        raw_assets = candidate.get("assets")
        if not isinstance(raw_assets, list):
            raise ToolRunError("persisted best artifact has invalid assets")
        assets = []
        for item in raw_assets:
            if not isinstance(item, dict):
                raise ToolRunError("persisted best artifact has invalid assets")
            assets.append({
                key: item.get(key)
                for key in ("assetKey", "fileName", "mimeType")
            })
        projected = {"template": candidate.get("template"), "assets": assets}
        try:
            return validate_builder_candidate(projected)
        except AdTemplateProcessError as exc:
            raise ToolRunError("persisted best artifact does not match the current builder contract") from exc

    def _validate_build_from_best_retry(
        self, run: Dict[str, Any]
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        """Fail closed before requeuing a terminal run at its persisted best."""
        if (
            run.get("tool_id") != "ad-template-generator"
            or run.get("action") != "build-template"
        ):
            raise ToolRunError("build-from-best retry is only supported for ad-template builds")
        payload = run.get("payload") if isinstance(run.get("payload"), dict) else {}
        if payload.get("placements") != ["feed", "story"]:
            raise ToolRunError("build-from-best retry requires the original Feed and Story contract")
        sources = payload.get("sources") if isinstance(payload.get("sources"), list) else []
        if len(sources) != 1 or not isinstance(sources[0], dict):
            raise ToolRunError("build-from-best retry requires exactly one persisted source")
        brief = payload.get("brief")
        if not isinstance(brief, str) or not brief.strip():
            raise ToolRunError("build-from-best retry requires the original immutable brief")

        try:
            from hermes_constants import get_hermes_home
            workspace = (
                get_hermes_home()
                / "tool_runs"
                / "ad-template-generator"
                / str(run["run_id"])
            ).resolve()
            source = self._durable_tool_source(workspace, str(sources[0].get("path") or ""))
        except (KeyError, OSError, RuntimeError, ValueError) as exc:
            raise ToolRunError("persisted source contract is unavailable") from exc
        expected_size = sources[0].get("size")
        if isinstance(expected_size, int) and expected_size >= 0 and source.stat().st_size != expected_size:
            raise ToolRunError("persisted source contract does not match the submitted source")
        try:
            checkpoint = self._ad_template_iteration_checkpoint(
                str(run["run_id"]), workspace, "build"
            )
        except (AdTemplateProcessError, OSError, RuntimeError, ValueError) as exc:
            raise ToolRunError("validated best checkpoint is unavailable") from exc
        if (
            not isinstance(checkpoint, dict)
            or not isinstance(checkpoint.get("candidate"), dict)
            or not isinstance(checkpoint.get("history"), list)
            or not checkpoint.get("history")
            or not isinstance(checkpoint.get("best_iteration"), int)
        ):
            raise ToolRunError("validated best checkpoint is unavailable")
        candidate = self._checkpoint_builder_candidate(checkpoint["candidate"])

        renderer_outcome = "renderable"
        renderer_reasons: List[str] = []
        try:
            with tempfile.TemporaryDirectory(prefix="retry-renderer-preflight-") as scratch:
                run_generator_cli(candidate, Path(scratch))
        except AdTemplateRendererRejection as exc:
            # A bounded deterministic rejection is a compatible current renderer
            # contract. The resumed builder receives it through the normal repair
            # path; no canonical run artifact is changed by this preflight.
            renderer_outcome = "deterministic-rejection"
            renderer_reasons = list(exc.reasons)
        except (AdTemplateProcessError, OSError, RuntimeError, ValueError) as exc:
            raise ToolRunError("current renderer contract cannot validate the persisted best artifact") from exc

        renderer_instruction = "Preserve the validated immutable best exactly."
        if renderer_reasons:
            try:
                renderer_instruction = _renderer_rejection_instructions(
                    candidate, renderer_reasons
                )[0]
            except AdTemplateProcessError as exc:
                raise ToolRunError("current renderer rejection cannot seed a bounded repair") from exc
        seed_feedback = self._terminal_failure_seed_feedback(
            run, checkpoint, renderer_instruction
        )

        return checkpoint, {
            "retry_mode": "build-from-best",
            "seed_iteration": checkpoint["best_iteration"],
            "history_length": len(checkpoint["history"]),
            "iteration_budget_extension": 1,
            "seed_feedback": seed_feedback,
            "source_verified": True,
            "renderer_validation": renderer_outcome,
            "renderer_reasons": renderer_reasons,
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
        if re.search(
            r"(?:API call failed after\s+\d+\s+(?:retries|attempts?)|"
            r"Codex stream produced no bytes within TTFB cutoff|"
            r"Operation interrupted during API call|request timed out)",
            text,
            flags=re.IGNORECASE,
        ):
            raise AdTemplateTransportError("model role transport attempt exhausted")
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
            "model": str(primary.get("model") or "").strip(),
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

    @staticmethod
    def _preflight_tool_candidate(candidate: Dict[str, str]) -> None:
        """Resolve one frozen provider/model pair without consulting chat state."""
        provider = str(candidate.get("provider") or "").strip()
        model = str(candidate.get("model") or "").strip()
        if not provider or not model:
            raise RuntimeError("Frozen ad-template model route is incomplete")
        from gateway.platforms.api_server import _resolve_request_runtime_agent_kwargs

        runtime = _resolve_request_runtime_agent_kwargs(provider, target_model=model)
        resolved_provider = str(runtime.get("provider") or "").strip()
        if resolved_provider != provider:
            raise RuntimeError(
                f"Frozen ad-template provider resolved as {resolved_provider or 'none'}, expected {provider}"
            )

    def _frozen_tool_route_plan(
        self, run: Dict[str, Any]
    ) -> Dict[str, tuple[List[Dict[str, str]], Dict[str, Any]]]:
        """Snapshot and preflight every route that this run can execute."""
        roles = list(AD_TEMPLATE_ROUTE_ORDER)
        stages = (run.get("model_policy") or {}).get("stages") or {}
        if AD_TEMPLATE_OPTIONAL_ROUTE in stages:
            roles.append(AD_TEMPLATE_OPTIONAL_ROUTE)
        plan: Dict[str, tuple[List[Dict[str, str]], Dict[str, Any]]] = {}
        role_pairs: Dict[tuple[str, str], List[str]] = {}
        for role in roles:
            candidates, settings = self._tool_candidates(run, role)
            if not candidates:
                raise RuntimeError(f"No route configured for {role}")
            frozen_candidates = [dict(item) for item in candidates]
            plan[role] = (frozen_candidates, dict(settings))
            for candidate in frozen_candidates:
                pair = (candidate["provider"], candidate["model"])
                role_pairs.setdefault(pair, []).append(role)
        for (provider, model), route_roles in role_pairs.items():
            self._preflight_tool_candidate({"provider": provider, "model": model})
            self._tool_run_store.append_event(
                run["run_id"], "provider.preflight", status="ok", node_id="source",
                data={"provider": provider, "model": model, "roles": route_roles},
            )
        return plan

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
            route_plan = self._frozen_tool_route_plan(run)
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
            candidates, route_settings = route_plan[route_stage]
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
                    agent = self._create_agent(
                        ephemeral_system_prompt=self._isolated_tool_role_prompt(),
                        session_id=f"{run_id}:{instance_id}:{uuid.uuid4().hex}",
                        tool_progress_callback=progress_callback,
                        requested_model=model, requested_provider=provider, route=None,
                        confirmed_runtime_lock=True,
                        persistence_disabled=True,
                        enabled_toolsets_override=[],
                        stream_delta_callback=role_heartbeat,
                        reasoning_callback=role_heartbeat,
                    )
                    # The durable role policy owns retry/fallback. Disable the
                    # generic agent's nested three-attempt loop so one stalled
                    # vision route cannot multiply every orchestrator retry.
                    agent._api_max_retries = 1
                    # A durable Tool role must also skip the generic Agent's
                    # one-time primary-transport rebuild. The orchestrator owns
                    # the frozen role fallback (for example Luna comparator ->
                    # Sol quality escalation); silently rebuilding Luna here
                    # previously doubled a 120-second TTFB failure before that
                    # fallback could run.
                    agent._try_recover_primary_transport = (
                        lambda *_args, **_kwargs: False
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
                routes = [
                    dict(route_plan[role][0][0]) for role in AD_TEMPLATE_ROUTE_ORDER
                ]
                # A configured analyse fallback must actually replace the
                # builder route on retry; the previous implementation ignored
                # ``_candidate`` and silently repeated the failed primary.
                routes[0] = dict(_candidate)
                if AD_TEMPLATE_OPTIONAL_ROUTE in route_plan:
                    routes.append(dict(route_plan[AD_TEMPLATE_OPTIONAL_ROUTE][0][0]))
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
                checkpoint = self._ad_template_iteration_checkpoint(
                    run_id, workspace, current_stage
                )
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
                    best_quality_score=(checkpoint or {}).get("best_quality_score"),
                    best_iteration=(checkpoint or {}).get("best_iteration"),
                    selected_builder_route=(checkpoint or {}).get("selected_builder_route"),
                    builder_escalated=bool((checkpoint or {}).get("builder_escalated")),
                    low_gain_streak=int((checkpoint or {}).get("low_gain_streak") or 0),
                    feedback=str((checkpoint or {}).get("feedback") or ""),
                    iteration_budget_extension=int(
                        (checkpoint or {}).get("iteration_budget_extension") or 0
                    ),
                )
                return result, usage

            result = usage = None
            failures = []
            configured_timeout = max(
                1.0, float(route_settings.get("timeout_seconds") or 120)
            )
            timeout = _ad_template_inactivity_timeout(
                route_settings, stage=current_stage
            )
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
                            timeout = _ad_template_inactivity_timeout(
                                route_settings, stage=current_stage
                            )
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
                raise RuntimeError(
                    f"Sole ad-template process failed during {current_stage}: "
                    f"{'; '.join(failures)}"
                )
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
                self._tool_run_store.resolve_executor_interruption(
                    run_id,
                    reason="gateway-shutdown"
                    if getattr(self, "_tool_run_shutdown", False)
                    else "executor-interrupted",
                )
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
                {
                    **item,
                    "available": readiness.get(str(item["provider"]), False),
                    "credential_ready": readiness.get(str(item["provider"]), False),
                    "estimated_price": None,
                    "price_checked_at": checked_at,
                    "pricing_stale": True,
                }
                for item in ad_template_model_catalog()
            ] + [
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
            body: Dict[str, Any] = {}
            if callable(getattr(request, "json", None)) and bool(
                getattr(request, "can_read_body", False)
            ):
                try:
                    parsed = await request.json()
                except (json.JSONDecodeError, TypeError, ValueError, web.HTTPException) as exc:
                    if getattr(request, "content_length", None) not in (None, 0):
                        raise ToolRunError("retry body must be valid JSON") from exc
                    parsed = {}
                if not isinstance(parsed, dict):
                    raise ToolRunError("retry body must be an object")
                body = parsed
            unknown = set(body) - {"mode"}
            if unknown:
                raise ToolRunError("retry body contains unsupported fields")
            mode = body.get("mode")
            if mode not in (None, "build-from-best"):
                raise ToolRunError("unsupported Tool retry mode")
            run = self._tool_run_store.get_run(run_id)
            if run["status"] not in {"failed", "cancelled", "blocked"}:
                raise ToolRunError("only failed, cancelled, or blocked Tool runs can be retried")
            if mode == "build-from-best":
                if run["status"] not in {"failed", "cancelled"}:
                    raise ToolRunError(
                        "build-from-best retry requires a failed or cancelled terminal Tool run"
                    )
                identity = {
                    key: run.get(key)
                    for key in (
                        "run_id", "tool_id", "action", "scope", "payload",
                        "model_policy_revision", "model_policy",
                    )
                }
                _checkpoint, evidence = await asyncio.to_thread(
                    self._validate_build_from_best_retry, run
                )
                current = self._tool_run_store.get_run(run_id)
                if current["status"] not in {"failed", "cancelled", "blocked"} or any(
                    current.get(key) != value for key, value in identity.items()
                ):
                    raise ToolRunError("Tool run changed while validating build-from-best retry")
                run = self._tool_run_store.requeue(
                    run_id,
                    stage="build",
                    expected_statuses={"failed", "cancelled"},
                    event_data=evidence,
                )
            else:
                run = self._tool_run_store.requeue(
                    run_id,
                    expected_statuses={"failed", "cancelled", "blocked"},
                )
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
