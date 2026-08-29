"""HTTP and execution mixin for durable Tool runs.

The API server owns authentication and agent construction.  This mixin keeps
the Tool-job contract at the platform edge instead of adding another model
tool to every Hermes conversation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List

from aiohttp import web

from agent.interrupt_compat import request_hard_interrupt
from agent.redact import redact_sensitive_text
from gateway.tool_runs import (
    GENERATION_LIKENESS_THRESHOLD,
    TOOL_MODEL_POLICY_SCHEMA,
    ToolRunError,
    validate_generation_records,
)


logger = logging.getLogger(__name__)


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
        return [
            "source", "analyse", "decompose", "restyle", "story-draft",
            "render", "visual-review", "check", "subject-invariance",
            "studio-qa", "ready", "release",
        ]

    @staticmethod
    def _preview_placement(name: str) -> str | None:
        """Return the placement encoded by the final token of a preview stem."""
        stem = Path(str(name)).stem.lower()
        match = re.search(r"(?:^|[-_.])(feed|story)$", stem)
        return match.group(1) if match else None

    @staticmethod
    def _ingest_tool_sources(command: Dict[str, Any]) -> Dict[str, Any]:
        """Copy Frank staging files into Hermes' private, content-addressed store."""
        payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
        sources = payload.get("sources") if isinstance(payload.get("sources"), list) else []
        if not sources:
            raise ToolRunError("at least one source is required")
        try:
            from hermes_constants import get_hermes_home
            asset_root = (get_hermes_home() / "tool_assets" / "ad-template-generator").resolve()
        except Exception as exc:
            raise ToolRunError("Hermes private asset store is unavailable") from exc
        shared_root = Path(os.environ.get("FRANK_SHARED_UPLOAD_ROOT", "/srv/frank/data/window/uploads")).resolve()
        allowed_extensions = {".avif", ".bmp", ".gif", ".heic", ".heif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
        result = json.loads(json.dumps(command))
        ingested = []
        for item in sources:
            if not isinstance(item, dict):
                raise ToolRunError("source must be an object")
            raw_path = item.get("path")
            if not raw_path:
                ingested.append(item)
                continue
            candidate_path = Path(str(raw_path))
            if candidate_path.is_symlink():
                raise ToolRunError("source symlinks are not accepted")
            try:
                source = candidate_path.resolve(strict=True)
            except OSError as exc:
                raise ToolRunError("source staging file is unavailable") from exc
            try:
                source.relative_to(shared_root)
            except ValueError as exc:
                raise ToolRunError("source path is outside the Frank staging mount") from exc
            relative = source.relative_to(shared_root)
            if any(part.startswith(".") for part in relative.parts):
                raise ToolRunError("hidden source files are not accepted")
            if not source.is_file() or source.suffix.lower() not in allowed_extensions:
                raise ToolRunError("source is not a supported regular image")
            size = source.stat().st_size
            if size < 1 or size > 250 * 1024 * 1024:
                raise ToolRunError("source exceeds the private ingestion limit")
            digest_builder = hashlib.sha256()
            with source.open("rb") as source_stream:
                head = source_stream.read(32)
                digest_builder.update(head)
                for chunk in iter(lambda: source_stream.read(1024 * 1024), b""):
                    digest_builder.update(chunk)
            image_header = (
                head.startswith(b"\x89PNG\r\n\x1a\n") or head.startswith(b"\xff\xd8\xff") or
                head.startswith((b"GIF87a", b"GIF89a", b"BM", b"II*\x00", b"MM\x00*")) or
                (head.startswith(b"RIFF") and head[8:12] == b"WEBP") or b"ftypavif" in head or
                b"ftypheic" in head or b"ftypheif" in head or b"ftypmif1" in head
            )
            if not image_header:
                raise ToolRunError("source bytes are not a supported image format")
            digest = digest_builder.hexdigest()
            declared = str(item.get("sha256") or "").lower()
            if declared and declared != digest:
                raise ToolRunError("source hash did not match the uploaded bytes")
            target_dir = asset_root / digest[:2]
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / f"{digest}{source.suffix.lower()}"
            if not target.exists():
                shutil.copyfile(source, target)
                target.chmod(0o600)
            private = dict(item)
            private["path"] = str(target)
            private["sha256"] = digest
            private["ref"] = f"source:{digest}"
            private["ingested"] = True
            ingested.append(private)
        result["payload"]["sources"] = ingested
        return result

    @staticmethod
    def _tool_json_output(value: Any) -> Dict[str, Any]:
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
        raise RuntimeError("Builder did not return one structured JSON result")

    @staticmethod
    def _prepare_candidate_output(run_id: str, output: Dict[str, Any]) -> Dict[str, Any]:
        """Validate pre-release evidence and expose only opaque preview URLs."""
        required = {"template_id", "candidate_ref", "preview_refs", "evidence_refs", "qa_summary"}
        missing = sorted(key for key in required if not output.get(key))
        if missing:
            raise RuntimeError(f"Builder candidate is incomplete: {', '.join(missing)}")
        qa = output.get("qa_summary")
        if not isinstance(qa, dict):
            raise RuntimeError("Builder candidate QA evidence is invalid")
        if (
            qa.get("source_verified") is not True
            or qa.get("deterministic_check") != "passed"
            or qa.get("subject_invariance_gate") is not True
            or qa.get("release_status") != "blocked_pending_human_approval"
        ):
            raise RuntimeError("Builder candidate did not pass every automated pre-release gate")
        try:
            from hermes_constants import get_hermes_home
            hermes_home = get_hermes_home()
            run_root = (
                hermes_home / "tool_assets" / "ad-template-generator" / "runs" / run_id
            ).resolve()
            checkpoint_root = (
                hermes_home / "tool_checkpoints" / "ad-template-generator" / run_id
            ).resolve()
            preview_root = (
                hermes_home / "tool_assets" / "ad-template-generator" /
                "runs" / run_id / "previews"
            ).resolve()
        except Exception as exc:
            raise RuntimeError("Hermes private preview store is unavailable") from exc
        try:
            candidate = Path(str(output["candidate_ref"])).resolve(strict=True)
            if not any(_path_is_relative_to(candidate, root) for root in (run_root, checkpoint_root)):
                raise ValueError("outside private run roots")
        except (OSError, ValueError) as exc:
            raise RuntimeError("Builder candidate is outside the private run store") from exc
        if candidate.is_symlink() or not candidate.is_file() or candidate.suffix.lower() != ".json":
            raise RuntimeError("Builder candidate is not an immutable JSON document")
        preview_refs = output.get("preview_refs")
        if not isinstance(preview_refs, list) or not preview_refs:
            raise RuntimeError("Builder candidate has no reviewable previews")
        previews = []
        for raw_path in preview_refs:
            try:
                target = Path(str(raw_path)).resolve(strict=True)
                target.relative_to(preview_root)
            except (OSError, ValueError) as exc:
                raise RuntimeError("Builder preview is outside the private run store") from exc
            if target.is_symlink() or not target.is_file() or target.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                raise RuntimeError("Builder preview is not a supported immutable image")
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            previews.append({
                "name": target.name,
                "sha256": digest,
                "url": f"/api/ad-studio/runs/{run_id}/artifacts/{target.name}",
            })
        result = dict(output)
        result["previews"] = previews
        # The deterministic builder writes the complete trace beneath the
        # run's private root. Treat that file as authoritative: the agent's
        # compact final response is a lossy transport summary and must not be
        # able to replace valid numeric scores with prose, nulls, or stale data.
        trace = None
        trace_path = run_root / "generation-trace.json"
        if trace_path.exists():
            if trace_path.is_symlink() or not trace_path.is_file():
                raise RuntimeError("generation trace is not an immutable private JSON document")
            try:
                trace = json.loads(trace_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("generation trace is unreadable or invalid") from exc
            if not isinstance(trace, dict):
                raise RuntimeError("generation trace must be a JSON object")
            result["generationTrace"] = trace
            generation_value = trace.get("generations")
        else:
            generation_value = result.get("generations")
            trace = result.get("generationTrace") or result.get("generation_trace")
        if generation_value is None and isinstance(trace, dict):
            generation_value = trace.get("generations")
        if generation_value is not None:
            placements = [ToolRunAPIMixin._preview_placement(item["name"]) for item in previews]
            if any(placement is None for placement in placements):
                raise RuntimeError("every preview must have an unambiguous Feed or Story placement suffix")
            feed_hashes = [item["sha256"] for item, placement in zip(previews, placements) if placement == "feed"]
            story_hashes = [item["sha256"] for item, placement in zip(previews, placements) if placement == "story"]
            current = result.get("current_artifacts") or result.get("artifact_hashes") or {}
            declared_feed = current.get("feedSha256") or current.get("feed_sha256") if isinstance(current, dict) else None
            declared_story = current.get("storySha256") or current.get("story_sha256") if isinstance(current, dict) else None
            if len(feed_hashes) == 1 and declared_feed and str(declared_feed).lower() != feed_hashes[0]:
                raise RuntimeError("declared current Feed hash does not match preview bytes")
            if len(story_hashes) == 1 and declared_story and str(declared_story).lower() != story_hashes[0]:
                raise RuntimeError("declared current Story hash does not match preview bytes")
            feed_hash = str(feed_hashes[0] if len(feed_hashes) == 1 else (declared_feed or "")).lower()
            story_hash = str(story_hashes[0] if len(story_hashes) == 1 else (declared_story or "")).lower()
            if not feed_hash or not story_hash:
                raise RuntimeError("generation records must bind one current Feed and Story artifact hash")
            records = validate_generation_records(
                generation_value,
                feed_sha256=feed_hash,
                story_sha256=story_hash,
            )
            if isinstance(trace, dict):
                if trace.get("templateId") not in (None, result.get("template_id")):
                    raise RuntimeError("generation trace template id does not match candidate")
                if trace.get("generations") is not None and trace.get("generations") != records:
                    raise RuntimeError("generation trace and generation records disagree")
            result["generations"] = records
            result["generation_contract"] = {
                "schema": "adstudio.generation-trace.v1",
                "validated": True,
                "approval_blocked_until_accepted": True,
                "current_artifacts": {"feedSha256": feed_hash, "storySha256": story_hash},
            }
        else:
            # Historical runs remain readable, but cannot cross the approval
            # boundary because they have no authoritative scored generation.
            result["generation_contract"] = {
                "schema": "adstudio.generation-trace.v1",
                "validated": False,
                "legacy_missing": True,
                "approval_blocked_until_accepted": True,
            }
        qa_projection = {
            "source_verified": True,
            "deterministic_check": "passed",
            "subject_invariance_passed": True,
            "release_blocked_pending_approval": True,
        }
        if result.get("generations"):
            qa_projection["visual_review"] = result["generations"][-1]
        result["qa"] = qa_projection
        return result

    @staticmethod
    def _validated_generation_gate(output: Any) -> Dict[str, Any]:
        """Return the accepted generation only when it is current and dual-reviewed."""
        if not isinstance(output, dict):
            raise ToolRunError("approval requires a validated generation contract")
        records = output.get("generations")
        trace = output.get("generationTrace") or output.get("generation_trace")
        if records is None and isinstance(trace, dict):
            records = trace.get("generations")
        current = output.get("current_artifacts") or output.get("artifact_hashes")
        if not isinstance(current, dict):
            current = (output.get("generation_contract") or {}).get("current_artifacts")
        feed_hash = str((current or {}).get("feedSha256") or (current or {}).get("feed_sha256") or "").lower()
        story_hash = str((current or {}).get("storySha256") or (current or {}).get("story_sha256") or "").lower()
        if not feed_hash or not story_hash:
            raise ToolRunError("approval requires current Feed and Story artifact hashes")
        if isinstance(trace, dict) and trace.get("status") not in (None, "accepted"):
            raise ToolRunError("approval requires an accepted generation trace")
        validated = validate_generation_records(records, feed_sha256=feed_hash or None, story_sha256=story_hash or None)
        last = validated[-1]
        scores = last.get("scores") if isinstance(last.get("scores"), dict) else last
        reviewers = last.get("reviewers") if isinstance(last.get("reviewers"), dict) else last
        primary = float(scores.get("primaryAdSystemLikeness", scores.get("primary_ad_system_likeness", scores.get("primary_score"))))
        strict = float(scores.get("strictAdSystemLikeness", scores.get("strict_ad_system_likeness", scores.get("strict_score"))))
        if last.get("decision") != "accepted" or primary < GENERATION_LIKENESS_THRESHOLD or strict < GENERATION_LIKENESS_THRESHOLD:
            raise ToolRunError("approval requires both independent likeness scores to be at least 9.5")
        if reviewers.get("primary") == reviewers.get("strict"):
            raise ToolRunError("approval requires independent reviewer identities")
        artifacts = last.get("artifacts") if isinstance(last.get("artifacts"), dict) else last
        if feed_hash and artifacts.get("feedSha256", artifacts.get("feed_sha256")) != feed_hash:
            raise ToolRunError("approval generation Feed hash is stale")
        if story_hash and artifacts.get("storySha256", artifacts.get("story_sha256")) != story_hash:
            raise ToolRunError("approval generation Story hash is stale")
        return {"generation": last, "feedSha256": artifacts.get("feedSha256", artifacts.get("feed_sha256")), "storySha256": artifacts.get("storySha256", artifacts.get("story_sha256"))}

    def _project_generation_events(self, run_id: str, records: List[Dict[str, Any]]) -> None:
        """Project validated generation records into Hermes' one event ledger."""
        existing = {
            (event["kind"], event.get("data", {}).get("iteration"))
            for event in self._tool_run_store.events(run_id)
            if event["kind"].startswith("generation.")
        }
        for record in records:
            iteration = record["iteration"]
            artifacts = record.get("artifacts") or {}
            reviewers = record.get("reviewers") or {}
            scores = record.get("scores") or {}
            common = {
                "iteration": iteration,
                "feed_sha256": artifacts.get("feedSha256", artifacts.get("feed_sha256")),
                "story_sha256": artifacts.get("storySha256", artifacts.get("story_sha256")),
                "render_set_sha256": artifacts.get("renderSetSha256", artifacts.get("render_set_sha256")),
            }
            events = [
                ("generation.started", {"iteration": iteration}),
                ("generation.rendered", common),
                ("generation.scored", {
                    **common,
                    "primary_reviewer": reviewers.get("primary", reviewers.get("primaryReviewer")),
                    "strict_reviewer": reviewers.get("strict", reviewers.get("strictReviewer")),
                    "primary_score": scores.get("primaryAdSystemLikeness", scores.get("primary_ad_system_likeness")),
                    "strict_score": scores.get("strictAdSystemLikeness", scores.get("strict_ad_system_likeness")),
                    "threshold": GENERATION_LIKENESS_THRESHOLD,
                }),
            ]
            decision_kind = "generation.accepted" if record.get("decision") == "accepted" else "generation.revision-requested"
            events.append((decision_kind, {"iteration": iteration, "revision_reason": str(record.get("revisionReason", record.get("revision_reason", "")))[:1000]}))
            for kind, data in events:
                if (kind, iteration) in existing:
                    continue
                self._tool_run_store.append_event(run_id, kind, status="ok", node_id="visual-review", data=data)

    @staticmethod
    def _validate_release_artifact(output: Dict[str, Any]) -> None:
        """Bind a release receipt to the exact private artifact bytes."""
        try:
            from hermes_constants import get_hermes_home
            release_root = (get_hermes_home() / "tool_releases" / "ad-template-generator").resolve()
        except Exception as exc:
            raise RuntimeError("Hermes private release store is unavailable") from exc
        raw_path = output.get("template_pack_path")
        if not raw_path:
            raise RuntimeError("Builder did not return the private TemplatePack path")
        candidate = Path(str(raw_path))
        if candidate.is_symlink():
            raise RuntimeError("TemplatePack symlinks are not accepted")
        try:
            artifact = candidate.resolve(strict=True)
            artifact.relative_to(release_root)
        except (OSError, ValueError) as exc:
            # Finalizer models can redact an otherwise valid private path in
            # their structured response (for example, ``private:``). Recover
            # deterministically from the release identifier instead of making
            # release correctness depend on model phrasing. The recovered
            # artifact remains confined to the private store and is still
            # bound below to the returned checksum and signature receipt.
            release_id = str(output.get("release_id") or "")
            if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,198}[A-Za-z0-9])?", release_id):
                raise RuntimeError("TemplatePack is outside the private release store") from exc
            candidate = release_root / release_id / "pack.bundle.json"
            try:
                if candidate.is_symlink():
                    raise ValueError("symlink")
                artifact = candidate.resolve(strict=True)
                artifact.relative_to(release_root)
            except (OSError, ValueError) as recovery_exc:
                raise RuntimeError("TemplatePack is outside the private release store") from recovery_exc
        if not artifact.is_file() or artifact.suffix.lower() not in {".json", ".zip"}:
            raise RuntimeError("TemplatePack is not a supported immutable artifact")
        digest = hashlib.sha256()
        with artifact.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != str(output.get("sha256") or "").lower():
            raise RuntimeError("TemplatePack checksum does not match the released bytes")
        if artifact.suffix.lower() == ".json":
            try:
                pack = json.loads(artifact.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("TemplatePack JSON is unreadable") from exc
            integrity = pack.get("integrity") if isinstance(pack, dict) else None
            if not isinstance(integrity, dict) or not isinstance(integrity.get("signature"), dict):
                raise RuntimeError("TemplatePack does not contain its signature receipt")
            if integrity.get("signature") != output.get("signature"):
                raise RuntimeError("TemplatePack signature receipt does not match the released artifact")

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

    def _tool_stage_from_preview(self, preview: Any, current: str) -> str:
        haystack = str(preview or "").lower()
        for stage in self._tool_stage_order():
            for variant in (stage, stage.replace("-", "_"), stage.replace("-", " ")):
                if re.search(rf"(?:^|[^a-z]){re.escape(variant)}(?:[^a-z]|$)", haystack):
                    return stage
        return current

    def _tool_prompt(self, run: Dict[str, Any], *, finalize: bool) -> str:
        payload = run.get("payload") or {}
        sources = []
        for item in payload.get("sources") or []:
            if isinstance(item, dict):
                ref = item.get("path") or item.get("ref") or item.get("url")
                if ref:
                    sources.append(f"- {ref}")
        if finalize:
            return (
                "Finalize an approved Ad Template Generator Tool run. The operator confirmed the Studio candidate "
                "at 100% zoom. Re-open its checkpointed workspace, rerun every release-blocking check, and issue "
                "only an immutable sanitized signed TemplatePack. Return one JSON object with release_id, "
                "template_pack_ref, template_pack_path, sha256, signature, compatibility, qa, and trace_ref. Store "
                "the pack beneath Hermes home/tool_releases/ad-template-generator; the path remains private. If a gate is stale or "
                "failing return failed=true and error. Never expose sources, prompts, credentials, reviewer identity, "
                "or private paths.\n\n"
                f"Run: {run['run_id']}\nProject: {(run.get('scope') or {}).get('project_id')}\n"
                f"Candidate: {json.dumps(run.get('output') or {}, ensure_ascii=False)}"
            )
        model_lines = []
        for stage_id, stage in ((run.get("model_policy") or {}).get("stages") or {}).items():
            primary = (stage or {}).get("primary") or {}
            fallbacks = (stage or {}).get("fallbacks") or []
            chain = [f"{primary.get('provider')}/{primary.get('model')}"]
            chain.extend(f"{item.get('provider')}/{item.get('model')}" for item in fallbacks if isinstance(item, dict))
            model_lines.append(f"- {stage_id}: {' -> '.join(chain)}")
        return (
            "Execute the canonical Ad Template Generator v2 process as a private durable Tool job, not a chat. "
            "Read and follow the installed adstudio-template-builder-v2 skill and project authority. One source "
            "produces at most one layered template. No image model may paint a whole ad; image models may operate "
            "only inside declared masked regions. Run source, analyse, decompose, restyle, story-draft, check, "
            "render, visual-review, check, subject-invariance, and prepare the Studio QA candidate. Use deterministic VPS commands and the "
            "canonical renderer wherever the pipeline declares a deterministic stage. Checkpoint every artifact. "
            "Treat /opt/ad-template-builder and every Git checkout as read-only authority: never create, edit, or "
            "delete repository files. Write candidate documents, previews, and evidence only beneath this run's "
            "Hermes tool_assets or tool_checkpoints directories. A candidate path outside those private roots is rejected. "
            "Stop before release for human approval. Return one compact JSON object with template_id, candidate_ref, "
            "preview_refs, evidence_refs, generations, qa_summary, cost, and attention items. Every generation must "
            "contain Feed/Story/render-set hashes, two different reviewer IDs, numeric likeness scores, a decision "
            "and revision reason. Never return raw prompts, source "
            "bytes, credentials, or hidden reasoning.\n\n"
            f"Tool run: {run['run_id']}\nRequest: {run['request_id']}\n"
            f"Resume from checkpointed stage: {run.get('stage') or 'source'}. Do not rerun earlier valid checkpoints.\n"
            f"Project: {(run.get('scope') or {}).get('project_id')}\n"
            f"Job: {payload.get('job_name') or run['run_id']}\n"
            f"Placements: {json.dumps(payload.get('placements') or [], ensure_ascii=False)}\n"
            f"Brief: {str(payload.get('brief') or '')[:4000]}\n"
            f"Sources:\n{chr(10).join(sources)}\n"
            f"Pinned model-policy revision: {run['model_policy_revision']}\n"
            f"AI routes:\n{chr(10).join(model_lines)}"
        )

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
        current_stage = "release" if finalize else "source"
        try:
            run = self._tool_run_store.get_run(run_id)
            current_stage = "release" if finalize else (run.get("stage") or "source")
            self._tool_run_store.update_run(
                run_id, status="running", stage=current_stage,
                progress=0.92 if finalize else max(0.02, float(run.get("progress") or 0)),
            )
            self._tool_run_store.append_event(
                run_id, "stage.started", status="running", node_id=current_stage,
                data={"summary": "Final release checks" if finalize else "Preparing private source evidence"},
            )
            route_stage = "visual-qa" if finalize else "analyse"
            candidates, route_settings = self._tool_candidates(run, route_stage)
            if not candidates:
                raise RuntimeError(f"No compatible model configured for {route_stage}")
            loop = asyncio.get_running_loop()
            reported_cost = 0.0
            cost_limit = float(route_settings.get("max_cost_usd") or 0)
            activity_sequence = 0

            def progress_callback(event_type: str, tool_name: str = None, preview: str = None, args=None, **kwargs):
                nonlocal current_stage, reported_cost, activity_sequence
                next_stage = self._tool_stage_from_preview(preview, current_stage)
                if next_stage != current_stage:
                    current_stage = next_stage
                    activity_sequence += 1
                    order = self._tool_stage_order()
                    progress = min(0.90, max(0.03, order.index(next_stage) / len(order)))
                    self._tool_run_store.update_run(run_id, stage=next_stage, progress=progress)
                    self._tool_run_store.append_event(
                        run_id, "stage.started", status="running", node_id=next_stage,
                        data={"summary": f"Started {next_stage.replace('-', ' ')}"},
                    )
                normalized_event = {
                    "generation-started": "generation.started",
                    "generation-rendered": "generation.rendered",
                    "generation-scored": "generation.scored",
                    "generation-revision-requested": "generation.revision-requested",
                    "generation-accepted": "generation.accepted",
                    "generation-failed": "generation.failed",
                }.get(str(event_type), str(event_type))
                generation_events = {
                    "generation.started", "generation.rendered", "generation.scored",
                    "generation.revision-requested", "generation.accepted", "generation.failed",
                }
                if event_type not in {"tool.started", "tool.completed", "subagent.start", "subagent.complete"} and normalized_event not in generation_events:
                    return
                if normalized_event.startswith("generation."):
                    activity_sequence += 1
                    safe_data = {
                        key: kwargs[key] for key in (
                            "iteration", "primary_score", "strict_score",
                            "primary_reviewer", "strict_reviewer",
                            "feed_sha256", "story_sha256", "render_set_sha256",
                        ) if kwargs.get(key) is not None
                    }
                    self._tool_run_store.append_event(
                        run_id, normalized_event, status="error" if kwargs.get("is_error") else "running",
                        node_id="visual-review", data=safe_data,
                    )
                    return
                activity_sequence += 1
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

            def run_sync(candidate: Dict[str, str]):
                route = self._resolve_route(candidate["model"])
                agent = self._create_agent(
                    ephemeral_system_prompt=(
                        "You are executing a private durable Frank Tool job on the VPS. Operational progress is "
                        "visible to the operator. Follow installed skills and project authority exactly."
                    ),
                    session_id=run_id,
                    tool_progress_callback=progress_callback,
                    requested_model=candidate["model"],
                    requested_provider=candidate["provider"],
                    route=route,
                    persistence_disabled=True,
                )
                self._tool_run_agents[run_id] = agent
                result = agent.run_conversation(
                    user_message=self._tool_prompt(run, finalize=finalize),
                    conversation_history=[],
                    task_id=run_id,
                )
                usage = {
                    "input_tokens": getattr(agent, "session_prompt_tokens", 0) or 0,
                    "output_tokens": getattr(agent, "session_completion_tokens", 0) or 0,
                    "total_tokens": getattr(agent, "session_total_tokens", 0) or 0,
                }
                return result, usage

            result = usage = None
            failures = []
            timeout = max(1.0, float(route_settings.get("timeout_seconds") or 120))
            for attempt, candidate in enumerate(candidates, start=1):
                self._tool_run_store.append_event(
                    run_id, "provider.attempt", status="running", node_id=route_stage,
                    data={"attempt": attempt, "provider": candidate["provider"], "model": candidate["model"], "timeout_seconds": timeout},
                )
                try:
                    future = loop.run_in_executor(None, run_sync, candidate)
                    observed_activity = activity_sequence
                    # The route timeout is an inactivity limit for the current
                    # model/tool stage, not a deadline for the whole pipeline.
                    # Every durable stage/tool event resets it; otherwise a
                    # healthy multi-stage VPS build is killed at 120 seconds.
                    deadline = loop.time() + timeout
                    while True:
                        remaining = deadline - loop.time()
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
                            if activity_sequence != observed_activity:
                                observed_activity = activity_sequence
                                deadline = loop.time() + timeout
                    break
                except Exception as exc:
                    running_agent = self._tool_run_agents.get(run_id)
                    if isinstance(exc, asyncio.TimeoutError) and running_agent is not None:
                        request_hard_interrupt(running_agent, source="api_server_tool_run_stage_timeout")
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
            output = self._tool_json_output(result)
            output["usage"] = usage
            builder_cost = output.get("cost")
            output["cost"] = dict(builder_cost) if isinstance(builder_cost, dict) else ({"builder_reported": builder_cost} if builder_cost is not None else {})
            output["cost"]["reported_usd"] = reported_cost
            output["model_policy_revision"] = refreshed["model_policy_revision"]
            if finalize:
                prior_output = refreshed.get("output") if isinstance(refreshed.get("output"), dict) else {}
                self._validated_generation_gate(prior_output)
                # Preserve the accepted candidate history in the release
                # receipt even when the finalizer returns only pack metadata.
                if prior_output.get("generations") is not None:
                    output["generations"] = prior_output["generations"]
                if prior_output.get("generationTrace") is not None:
                    output["generationTrace"] = prior_output["generationTrace"]
                if output.get("failed"):
                    raise RuntimeError(str(output.get("error") or "Release checks failed"))
                required_release = {"release_id", "template_pack_ref", "template_pack_path", "sha256", "signature", "compatibility", "qa", "trace_ref"}
                missing = sorted(key for key in required_release if not output.get(key))
                if missing:
                    raise RuntimeError(f"Builder returned an incomplete TemplatePack release: {', '.join(missing)}")
                if not re.fullmatch(r"[0-9a-f]{64}", str(output.get("sha256") or "")):
                    raise RuntimeError("Builder returned an invalid TemplatePack checksum")
                self._validate_release_artifact(output)
                qa = output.get("qa") if isinstance(output.get("qa"), dict) else {}
                if qa.get("all_gates_passed") is not True or qa.get("subject_invariance_passed") is not True or qa.get("source_identity_leakage") != 0:
                    raise RuntimeError("Builder release evidence did not pass every mandatory QA gate")
                self._tool_run_store.update_run(
                    run_id, status="completed", stage="release", progress=1,
                    output=output, attention=False,
                )
                self._tool_run_store.append_event(
                    run_id, "release.published", status="ok", node_id="release",
                    data={key: output.get(key) for key in ("release_id", "template_pack_ref", "sha256", "compatibility") if output.get(key) is not None},
                )
            else:
                output = self._prepare_candidate_output(run_id, output)
                if output.get("generations"):
                    self._project_generation_events(run_id, output["generations"])
                self._tool_run_store.update_run(
                    run_id, status="waiting_for_approval", stage="studio-qa", progress=0.9,
                    output=output, attention=True,
                )
                self._tool_run_store.append_event(
                    run_id, "approval.requested", status="blocked", node_id="studio-qa",
                    data={"gate": "studio-qa-100-percent", "choices": ["approve", "reject"]},
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
                raise ToolRunError("TemplatePack is not released")
            output = run.get("output") if isinstance(run.get("output"), dict) else {}
            raw_path = output.get("template_pack_path")
            if not raw_path:
                raise ToolRunError("released TemplatePack file is unavailable")
            from hermes_constants import get_hermes_home
            root = (get_hermes_home() / "tool_releases" / "ad-template-generator").resolve()
            target = Path(str(raw_path)).resolve(strict=True)
            target.relative_to(root)
            if target.suffix.lower() not in {".zip", ".json"} or not target.is_file():
                raise ToolRunError("released TemplatePack file is invalid")
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
                get_hermes_home() / "tool_assets" / "ad-template-generator" /
                "runs" / run_id / "previews"
            ).resolve()
            target = (root / name).resolve(strict=True)
            target.relative_to(root)
            if target.is_symlink() or not target.is_file() or target.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
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
                capability("openai-codex", "gpt-5.5", "vision_structured"),
                capability("gemini", "gemini-3.6-flash", "vision_structured"),
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

    async def _handle_tool_run_approval(self, request: web.Request) -> web.Response:
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        run_id = request.match_info["run_id"]
        try:
            body = await request.json()
            run = self._tool_run_store.get_run(run_id)
            if run["status"] != "waiting_for_approval":
                raise ToolRunError("Tool run is not waiting for Studio QA")
            decision = str(body.get("decision") or "").lower()
            if decision == "reject":
                reason = redact_sensitive_text(str(body.get("reason") or "Studio QA rejected"), force=True)
                self._tool_run_store.update_run(run_id, status="failed", error=reason, attention=True)
                self._tool_run_store.append_event(run_id, "approval.rejected", status="error", node_id="studio-qa", data={"reason": reason[:1000]})
                return web.json_response(self._tool_run_store.get_run(run_id))
            if decision != "approve" or body.get("confirm_100_percent") is not True:
                raise ToolRunError("approval requires decision=approve and confirm_100_percent=true")
            self._validated_generation_gate(run.get("output"))
            self._tool_run_store.append_event(run_id, "approval.approved", status="ok", node_id="studio-qa", data={"gate": "studio-qa-100-percent"})
            self._tool_run_store.requeue(run_id, stage="release")
            self._start_tool_task(run_id, finalize=True)
            return web.json_response(self._tool_run_store.get_run(run_id), status=202)
        except KeyError as exc:
            return web.json_response(_error(str(exc), "tool_run_not_found"), status=404)
        except (ToolRunError, ValueError, TypeError) as exc:
            return web.json_response(_error(str(exc), "invalid_tool_approval"), status=400)

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
            agent = self._tool_run_agents.get(run_id)
            if agent is not None:
                request_hard_interrupt(agent, source="api_server_tool_run_cancel")
            task = self._tool_run_tasks.get(run_id)
            if task is not None and not task.done():
                task.cancel()
            elif run["status"] not in {"completed", "failed", "cancelled"}:
                self._tool_run_store.update_run(run_id, status="cancelled", attention=True)
                self._tool_run_store.append_event(run_id, "run.cancelled", status="cancelled", node_id=run.get("stage"), data={})
            return web.json_response(self._tool_run_store.get_run(run_id), status=202)
        except KeyError as exc:
            return web.json_response(_error(str(exc), "tool_run_not_found"), status=404)
