# -*- coding: utf-8 -*-
#!/usr/bin/env python3
"""Queue the first fifty curated Ad Template Generator source images.

The default invocation is deliberately read-only. ``--run`` creates an
immutable batch manifest, stages verified source copies in Frank's shared
intake, and drives a single-process, resumable queue through the Tool-run API.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import math
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence


DEFAULT_ROOTS = [
    Path("/srv/frank/private/ad-template-sources/candidates-2026-08-24/01_feed_4x5_best"),
    Path("/srv/frank/private/ad-template-sources/candidates-2026-08-12/01_feed_4x5_best"),
    Path("/srv/ad-template-generator/intake"),
]
DEFAULT_ENDPOINT = "http://172.16.1.1:8642"
DEFAULT_STAGE_ROOT = Path("/srv/frank/data/window/uploads/ad-template-first50")
BATCH_SCHEMA = "hermes.ad-template-first50-batch/v1"
MANIFEST_SCHEMA = "hermes.ad-template-first50-manifest/v1"
_SOURCE_NAME = re.compile(r"^meta_(\d+)\.png$", re.IGNORECASE)
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "discarded"})
# ready_for_review is quarantined human-review work, not an active paid build;
# it is accepted only when its final evidence is present below.
_ACTIVE_STATUSES = frozenset({"queued", "running", "blocked", "cancelling", "discarding", "publishing"})
_DETAIL_STATUSES = _TERMINAL_STATUSES | {"ready_for_review", "completed", "blocked"}
_ITEM_STATUSES = frozenset({"pending", "posting", "submitted", "passed", "failed"})
_MAX_POST_ATTEMPTS = 2
_MAX_BATCH_ITEMS = 50
_MAX_COLLECTION_BYTES = 8 * 1024 * 1024
_MAX_DETAIL_BYTES = 2 * 1024 * 1024


class BatchError(RuntimeError):
    """A bounded, fail-closed queue or server-state error."""


class HttpFailure(BatchError):
    def __init__(self, message: str, *, uncertain: bool, status: int | None = None):
        super().__init__(message)
        self.uncertain = uncertain
        self.status = status


def _batch_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,47}", value):
        raise BatchError("batch-id must be 1-48 letters, numbers, dots, underscores or hyphens")
    return value


def _source_id(path: Path) -> int:
    match = _SOURCE_NAME.fullmatch(path.name)
    if not match:
        raise BatchError(f"source filename is not numeric meta_NNNN.png: {path.name}")
    return int(match.group(1))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def discover(roots: Sequence[Path] = DEFAULT_ROOTS) -> list[Path]:
    """Choose latest-root source per numeric design ID, then deduplicate SHA."""
    by_id: dict[int, Path] = {}
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for path in sorted(root.glob("meta_*.png"), key=lambda candidate: str(candidate)):
            if not path.is_file() or _SOURCE_NAME.fullmatch(path.name) is None:
                continue
            design_id = _source_id(path)
            # Roots are ordered latest first. A same-ID later-root copy is
            # never allowed to displace the curated latest source.
            by_id.setdefault(design_id, path)
    by_sha: dict[str, Path] = {}
    for design_id, path in sorted(by_id.items()):
        digest = _sha256(path)
        by_sha.setdefault(digest, path)
    return sorted(by_sha.values(), key=lambda path: (_source_id(path), str(path)))[:_MAX_BATCH_ITEMS]


def manifest(paths: Sequence[Path]) -> list[dict[str, Any]]:
    """Return the stable source manifest used by legacy callers and dry-run."""
    result = []
    for path in paths:
        resolved = Path(path).resolve(strict=True)
        result.append({
            "id": str(_source_id(resolved)),
            "name": resolved.name,
            "path": str(resolved),
            "sha256": _sha256(resolved),
        })
    return result


def _identity(item: Mapping[str, Any], batch_id: str) -> tuple[str, str]:
    source_id = str(item["id"])
    digest = str(item["sha256"])
    return (
        f"ad-template-first50-{batch_id}-{source_id}",
        f"ad-template:first50:{batch_id}:{source_id}:{digest[:16]}",
    )


def request_payload(
    item: Mapping[str, Any], project_id: str, batch_id: str = "first50-20260907",
) -> dict[str, Any]:
    batch_id = _batch_id(batch_id)
    request_id, idempotency_key = _identity(item, batch_id)
    source_path = str(item.get("staged_path") or item["path"])
    source_name = str(item.get("name") or f"meta_{item['id']}.png")
    brief = "Exact-clone Feed + Story template reconstruction."
    return {
        "schema": "schema://hermes.tool-run-command/v1",
        "request_id": request_id,
        "tool_id": "ad-template-generator",
        "action": "build-template",
        "scope": {"project_id": project_id},
        "payload": {
            "job_name": f"Ad Template Generator - meta_{item['id']}",
            "brief": brief,
            "brief_sha256": hashlib.sha256(brief.encode()).hexdigest(),
            "placements": ["feed", "story"],
            "first50_batch_id": batch_id,
            "first50_source_id": str(item["id"]),
            "first50_source_sha256": str(item["sha256"]),
            "sources": [{
                "path": source_path,
                "name": source_name,
                "media_type": "image/png",
                "sha256": str(item["sha256"]),
            }],
        },
        "idempotency_key": idempotency_key,
    }


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _append_ledger(path: Path, event: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(dict(event), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def _persist(state_path: Path, ledger_path: Path, state: dict[str, Any], event: Mapping[str, Any]) -> None:
    _atomic_json(state_path, state)
    _append_ledger(ledger_path, event)


@contextlib.contextmanager
def _batch_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _stage_sources(items: Sequence[dict[str, Any]], stage_dir: Path) -> None:
    stage_dir.mkdir(parents=True, exist_ok=True)
    for item in items:
        source = Path(item["path"]).resolve(strict=True)
        target = (stage_dir / str(item["name"])).resolve()
        target.relative_to(stage_dir.resolve())
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_file() or _sha256(target) != item["sha256"]:
                raise BatchError(f"staged source does not match immutable hash: {target}")
            continue
        fd, raw_tmp = tempfile.mkstemp(prefix=".source-", suffix=".png", dir=stage_dir)
        tmp = Path(raw_tmp)
        os.fchmod(fd, 0o644)
        try:
            with os.fdopen(fd, "wb") as handle, source.open("rb") as source_handle:
                shutil.copyfileobj(source_handle, handle)
                handle.flush()
                os.fsync(handle.fileno())
            if _sha256(tmp) != item["sha256"]:
                raise BatchError(f"source changed while staging: {source}")
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BatchError(f"invalid durable batch state: {path.name}") from exc


def _build_items(paths: Sequence[Path], stage_dir: Path, batch_id: str) -> list[dict[str, Any]]:
    items = manifest(paths)
    if len(items) != _MAX_BATCH_ITEMS:
        raise BatchError(f"expected exactly {_MAX_BATCH_ITEMS} unique numeric sources, found {len(items)}")
    for item in items:
        item["staged_path"] = str((stage_dir / item["name"]).resolve())
        request_id, idempotency_key = _identity(item, batch_id)
        item["request_id"] = request_id
        item["idempotency_key"] = idempotency_key
    return items


def _new_state(batch_id: str, items: Sequence[dict[str, Any]], project_id: str) -> dict[str, Any]:
    return {
        "schema": BATCH_SCHEMA,
        "batch_id": batch_id,
        "status": "running",
        "items": [{
            **item,
            "payload": request_payload(item, project_id, batch_id),
            "state": "pending",
            "post_attempts": 0,
            "run_id": None,
            "server_status": None,
            "last_error": None,
        } for item in items],
    }


def _validate_existing_state(state: Any, batch_id: str, items: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(state, dict) or state.get("schema") != BATCH_SCHEMA or state.get("batch_id") != batch_id:
        raise BatchError("durable batch state schema or batch identity changed")
    existing = state.get("items")
    if not isinstance(existing, list) or len(existing) != len(items):
        raise BatchError("durable batch state item count changed")
    expected = {str(item["id"]): item for item in items}
    seen: set[str] = set()
    for item in existing:
        if not isinstance(item, dict) or str(item.get("id")) in seen:
            raise BatchError("durable batch state has duplicate or invalid item")
        source_id = str(item.get("id"))
        expected_item = expected.get(source_id)
        if expected_item is None or item.get("sha256") != expected_item["sha256"] or item.get("path") != expected_item["path"]:
            raise BatchError(f"source hash/path changed for meta_{source_id}")
        if item.get("state") not in _ITEM_STATUSES:
            raise BatchError("durable batch state has an invalid item status")
        expected_request, expected_idempotency = _identity(expected_item, batch_id)
        if item.get("request_id") != expected_request or item.get("idempotency_key") != expected_idempotency:
            raise BatchError(f"stable request identity changed for meta_{source_id}")
        payload = item.get("payload")
        if not isinstance(payload, dict) or payload.get("idempotency_key") != expected_idempotency:
            raise BatchError(f"durable request intent changed for meta_{source_id}")
        seen.add(source_id)
    if seen != set(expected):
        raise BatchError("durable batch state source set changed")
    return state


class _Api:
    def __init__(self, endpoint: str, api_key: str, timeout: float):
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def request(self, method: str, path: str, body: Any = None, *, max_bytes: int = _MAX_DETAIL_BYTES) -> Any:
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = None if body is None else json.dumps(body, separators=(",", ":")).encode()
        request = urllib.request.Request(self.endpoint + path, data=payload, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    raise HttpFailure("Tool-run response exceeds the bounded size", uncertain=False)
        except urllib.error.HTTPError as exc:
            uncertain = int(exc.code) >= 500
            raise HttpFailure(f"Tool-run API returned HTTP {exc.code}", uncertain=uncertain, status=int(exc.code)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise HttpFailure("Tool-run API response was uncertain", uncertain=True) from exc
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HttpFailure("Tool-run API returned invalid JSON", uncertain=True) from exc
        if not isinstance(value, (dict, list)):
            raise HttpFailure("Tool-run API returned invalid state", uncertain=True)
        return value

    def list_runs(self) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"tool_id": "ad-template-generator", "limit": "500"})
        value = self.request("GET", f"/v1/tool-runs?{query}", max_bytes=_MAX_COLLECTION_BYTES)
        runs = value.get("data") if isinstance(value, dict) else None
        if not isinstance(runs, list) or len(runs) >= 500 or any(not isinstance(run, dict) for run in runs):
            raise BatchError("Tool-run collection state is invalid or incomplete")
        for run in runs:
            if run.get("tool_id") != "ad-template-generator" or run.get("action") != "build-template":
                raise BatchError("Tool-run collection contained an invalid ad-template run")
            if not isinstance(run.get("status"), str):
                raise BatchError("Tool-run collection contained an invalid status")
            known_statuses = _ACTIVE_STATUSES | _TERMINAL_STATUSES | {"ready_for_review"}
            if run["status"] not in known_statuses:
                raise BatchError(f"Tool-run collection contained unknown status {run['status']!r}")
        return runs

    def get_run(self, run_id: str) -> dict[str, Any]:
        value = self.request("GET", f"/v1/tool-runs/{urllib.parse.quote(run_id, safe='')}", max_bytes=_MAX_DETAIL_BYTES)
        run = value.get("run") if isinstance(value, dict) and isinstance(value.get("run"), dict) else value
        if not isinstance(run, dict) or run.get("run_id") != run_id:
            raise BatchError("Tool-run status response has an invalid identity")
        if run.get("tool_id") != "ad-template-generator" or run.get("action") != "build-template":
            raise BatchError("Tool-run status response has an invalid tool")
        if not isinstance(run.get("status"), str):
            raise BatchError("Tool-run status response has an invalid status")
        return run

    def create_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        value = self.request("POST", "/v1/tool-runs", payload, max_bytes=_MAX_DETAIL_BYTES)
        run = value.get("run") if isinstance(value, dict) and isinstance(value.get("run"), dict) else value
        if not isinstance(run, dict) or not isinstance(run.get("run_id"), str):
            raise HttpFailure("Tool-run create response has no run identity", uncertain=True)
        if run.get("tool_id") != payload["tool_id"] or run.get("action") != payload["action"]:
            raise BatchError("Tool-run create response has an invalid tool")
        return run


def _identity_match(runs: Sequence[Mapping[str, Any]], item: Mapping[str, Any]) -> dict[str, Any] | None:
    matches = [run for run in runs if run.get("idempotency_key") == item["idempotency_key"]]
    if not matches:
        matches = [run for run in runs if run.get("request_id") == item["request_id"]]
    if len(matches) > 1:
        raise BatchError(f"multiple server runs match meta_{item['id']}")
    if not matches:
        return None
    run = dict(matches[0])
    if run.get("idempotency_key") not in (None, item["idempotency_key"]) or run.get("request_id") not in (None, item["request_id"]):
        raise BatchError(f"server identity changed for meta_{item['id']}")
    payload = run.get("payload") if isinstance(run.get("payload"), dict) else {}
    if payload.get("first50_batch_id") not in (None, item.get("payload", {}).get("payload", {}).get("first50_batch_id")):
        raise BatchError(f"server batch identity changed for meta_{item['id']}")
    if payload.get("first50_source_sha256") not in (None, item["sha256"]):
        raise BatchError(f"server source hash changed for meta_{item['id']}")
    return run


def _quality_pass(run: Mapping[str, Any]) -> bool:
    output = run.get("output") if isinstance(run.get("output"), dict) else {}
    template = output.get("template")
    final = output.get("final_review")
    scores_document = output.get("scores")
    reusable = output.get("reusable_validation")
    overall_check = output.get("overall_check")
    if (
        not isinstance(template, dict)
        or not isinstance(template.get("templateId"), str)
        or not template["templateId"].strip()
        or not isinstance(final, dict)
        or final.get("decision") != "accepted"
        or overall_check != {"no_obvious_errors": True}
    ):
        return False
    comparator = scores_document.get("comparator") if isinstance(scores_document, dict) else None
    score_fields = {"overall", "geometry", "typography", "colourEffects", "imageCrop", "details"}
    gated_score_fields = score_fields - {"overall"}
    if (
        not isinstance(comparator, dict)
        or set(comparator) != score_fields
        or any(not _quality_score(comparator.get(field)) for field in gated_score_fields)
    ):
        return False
    reviewers = final.get("reviewers")
    if not isinstance(reviewers, list) or len(reviewers) != 2:
        return False
    reviewer_ids = set()
    reviewer_routes = set()
    effect_fields = {"shading", "gradients", "shadows", "transparency", "borders", "masks", "texture"}
    for reviewer in reviewers:
        if not isinstance(reviewer, dict) or reviewer.get("decision") not in {"accept", "accepted"}:
            return False
        identity = reviewer.get("id")
        route = reviewer.get("route")
        effects = reviewer.get("effects")
        reviewer_scores = reviewer.get("scores")
        if (
            not isinstance(identity, str) or not identity.strip()
            or not isinstance(route, str) or not route.strip()
            or identity in reviewer_ids or route in reviewer_routes
            or not isinstance(reviewer_scores, dict) or set(reviewer_scores) != score_fields
            or any(not _quality_score(reviewer_scores.get(field)) for field in gated_score_fields)
            or not isinstance(reviewer.get("issues"), list) or reviewer["issues"]
            or not isinstance(effects, dict) or set(effects) != effect_fields
            or any(effects[field] not in {"match", "not_present"} for field in effect_fields)
        ):
            return False
        reviewer_ids.add(identity)
        reviewer_routes.add(route)
    imported = output.get("import")
    smoke = output.get("smoke_test")
    if (
        not isinstance(imported, dict)
        or not isinstance(imported.get("template_id"), str)
        or not imported["template_id"].strip()
        or imported.get("library_status") != "quarantined"
        or not isinstance(smoke, dict)
        or smoke.get("templateId") != imported["template_id"]
        or smoke.get("status") != "passed"
    ):
        return False
    if (
        not isinstance(reusable, dict)
        or reusable.get("status") != "passed"
        or reusable.get("scenarioLimit") != 4
        or reusable.get("counts") != {"total": 4, "passed": 4, "failed": 0}
        or not isinstance(reusable.get("scenarios"), list)
        or len(reusable["scenarios"]) != 4
        or any(
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str) or not item["name"].strip()
            or not isinstance(item.get("identity"), str) or not item["identity"].strip()
            or item.get("status") != "passed"
            for item in reusable["scenarios"]
        )
        or len({item["name"] for item in reusable["scenarios"]}) != 4
    ):
        return False
    return True


def _quality_score(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        numeric = float(value)
    except (OverflowError, TypeError, ValueError):
        return False
    return math.isfinite(numeric) and 9.5 <= numeric <= 10


def _run_outcome(run: Mapping[str, Any]) -> str:
    status = run.get("status")
    if status == "blocked":
        raise BatchError(f"run {run.get('run_id')} ended in blocked")
    if status in _ACTIVE_STATUSES:
        return "active"
    if status == "ready_for_review" or status == "completed":
        if _quality_pass(run):
            return "passed"
        raise BatchError(f"run {run.get('run_id')} reached review without final evidence")
    if status in {"failed", "cancelled", "discarded"}:
        raise BatchError(f"run {run.get('run_id')} ended in {status}")
    raise BatchError(f"run {run.get('run_id')} has unknown status {status!r}")


def _record_failure(state_path: Path, ledger_path: Path, state: dict[str, Any], item: dict[str, Any], error: str) -> None:
    item["state"] = "failed"
    item["last_error"] = error[:1000]
    state["status"] = "failed"
    _persist(state_path, ledger_path, state, {"event": "batch.failed", "id": item["id"], "error": error[:1000]})


def run_batch(
    *, roots: Sequence[Path] = DEFAULT_ROOTS, state_root: Path = Path("/srv/ad-template-generator/batches"),
    batch_id: str = "first50-20260907", endpoint: str = DEFAULT_ENDPOINT,
    project_id: str = "blockwise", max_active: int = 2, stage_root: Path = DEFAULT_STAGE_ROOT,
    poll_seconds: float = 10.0, timeout: float = 20.0, api_key: str | None = None,
) -> int:
    batch_id = _batch_id(batch_id)
    if not 1 <= max_active <= 4:
        raise BatchError("max-active must be between 1 and 4")
    if poll_seconds < 0 or timeout <= 0:
        raise BatchError("poll-seconds must be non-negative and timeout must be positive")
    batch_dir = Path(state_root).resolve() / batch_id
    manifest_path = batch_dir / "manifest.json"
    state_path = batch_dir / "state.json"
    ledger_path = batch_dir / "ledger.jsonl"
    stage_dir = (Path(stage_root).resolve() / batch_id)
    with _batch_lock(batch_dir / ".lock"):
        paths = discover(roots)
        items = _build_items(paths, stage_dir, batch_id)
        if manifest_path.exists() != state_path.exists():
            raise BatchError("manifest and state must be created or restored together")
        if manifest_path.exists():
            document = _read_json(manifest_path)
            expected_manifest = {"schema": MANIFEST_SCHEMA, "batch_id": batch_id, "items": items}
            if document != expected_manifest:
                raise BatchError("immutable first50 manifest changed")
            state = _validate_existing_state(_read_json(state_path), batch_id, items)
        else:
            _stage_sources(items, stage_dir)
            document = {"schema": MANIFEST_SCHEMA, "batch_id": batch_id, "items": items}
            state = _new_state(batch_id, items, project_id)
            _atomic_json(manifest_path, document)
            _atomic_json(state_path, state)
            _append_ledger(ledger_path, {"event": "batch.initialized", "batch_id": batch_id, "count": len(items)})
        # Existing staged files are always re-hashed before any API call.
        _stage_sources(items, stage_dir)
        api = _Api(endpoint, os.getenv("HERMES_API_KEY", "") if api_key is None else api_key, timeout)
        while True:
            runs = api.list_runs()
            active_count = sum(1 for run in runs if run.get("status") in _ACTIVE_STATUSES)
            pending = []
            for item in state["items"]:
                if item["state"] == "passed":
                    continue
                was_failed = item["state"] == "failed"
                run = None
                if item.get("run_id"):
                    run = api.get_run(str(item["run_id"]))
                else:
                    run = _identity_match(runs, item)
                if run is None:
                    if was_failed:
                        raise BatchError(f"failed item meta_{item['id']} has no stable server run")
                    pending.append(item)
                    continue
                if item.get("run_id") is None and run.get("status") in _DETAIL_STATUSES:
                    run = api.get_run(str(run["run_id"]))
                item["run_id"] = run["run_id"]
                item["server_status"] = run.get("status")
                try:
                    outcome = _run_outcome(run)
                except BatchError as exc:
                    _record_failure(state_path, ledger_path, state, item, str(exc))
                    raise
                if outcome == "passed":
                    item["state"] = "passed"
                    _persist(state_path, ledger_path, state, {"event": "item.passed", "id": item["id"], "run_id": item["run_id"]})
                else:
                    item["state"] = "submitted"
                    event = "item.requeued-observed" if was_failed else "item.observed"
                    _persist(state_path, ledger_path, state, {"event": event, "id": item["id"], "run_id": item["run_id"], "status": run.get("status")})
            if all(item["state"] == "passed" for item in state["items"]):
                state["status"] = "passed"
                _persist(state_path, ledger_path, state, {"event": "batch.passed", "batch_id": batch_id})
                return 0
            pending = [item for item in state["items"] if item["state"] in {"pending", "posting"} and not item.get("run_id")]
            for stuck in pending:
                if stuck["state"] == "posting" and int(stuck.get("post_attempts", 0)) >= _MAX_POST_ATTEMPTS:
                    error = BatchError(f"uncertain create exhausted for meta_{stuck['id']}")
                    _record_failure(state_path, ledger_path, state, stuck, str(error))
                    raise error
            if active_count >= max_active or not pending:
                if poll_seconds:
                    time.sleep(poll_seconds)
                continue
            item = pending[0]
            item["state"] = "posting"
            _persist(state_path, ledger_path, state, {"event": "item.intent-persisted", "id": item["id"], "idempotency_key": item["idempotency_key"]})
            while item["post_attempts"] < _MAX_POST_ATTEMPTS:
                item["post_attempts"] += 1
                _persist(state_path, ledger_path, state, {"event": "item.post-attempt", "id": item["id"], "attempt": item["post_attempts"]})
                try:
                    run = api.create_run(item["payload"])
                except HttpFailure as exc:
                    if not exc.uncertain and exc.status is not None and exc.status < 500:
                        _record_failure(state_path, ledger_path, state, item, str(exc))
                        raise
                    try:
                        run = _identity_match(api.list_runs(), item)
                    except BatchError as reconcile_exc:
                        _record_failure(state_path, ledger_path, state, item, str(reconcile_exc))
                        raise
                    if run is None:
                        if item["post_attempts"] >= _MAX_POST_ATTEMPTS:
                            error = BatchError(f"uncertain create exhausted for meta_{item['id']}")
                            _record_failure(state_path, ledger_path, state, item, str(error))
                            raise error from exc
                        continue
                item["run_id"] = run["run_id"]
                item["state"] = "submitted"
                item["server_status"] = run.get("status")
                if run.get("status") in _DETAIL_STATUSES:
                    run = api.get_run(str(run["run_id"]))
                try:
                    outcome = _run_outcome(run)
                except BatchError as exc:
                    _record_failure(state_path, ledger_path, state, item, str(exc))
                    raise
                if outcome == "passed":
                    item["state"] = "passed"
                _persist(state_path, ledger_path, state, {"event": "item.submitted", "id": item["id"], "run_id": item["run_id"], "status": run.get("status")})
                break
            # A newly posted running item is intentionally not cancelled if a
            # later item fails; the outer loop will observe it and preserve it.


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="stage sources and submit/reconcile the durable queue")
    parser.add_argument("--root", type=Path, default=Path("/srv/ad-template-generator/batches"), help="durable batch state root")
    parser.add_argument("--source-root", action="append", type=Path, dest="source_roots", help="source root, repeat in latest-first order")
    parser.add_argument("--stage-root", type=Path, default=DEFAULT_STAGE_ROOT, help="Frank shared intake root")
    parser.add_argument("--batch-id", default="first50-20260907")
    parser.add_argument("--endpoint", default=os.getenv("HERMES_API_URL", DEFAULT_ENDPOINT))
    parser.add_argument("--project-id", default="blockwise")
    parser.add_argument("--max-active", type=int, default=2)
    parser.add_argument("--poll-seconds", type=float, default=10.0)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)
    try:
        roots = args.source_roots or DEFAULT_ROOTS
        paths = discover(roots)
        if not args.run:
            items = manifest(paths)
            if len(items) != _MAX_BATCH_ITEMS:
                raise BatchError(f"expected exactly {_MAX_BATCH_ITEMS} unique numeric sources, found {len(items)}")
            print(json.dumps({"count": len(items), "items": items}, indent=2, sort_keys=True))
            return 0
        result = run_batch(
            roots=roots, state_root=args.root, batch_id=args.batch_id,
            endpoint=args.endpoint, project_id=args.project_id,
            max_active=args.max_active, stage_root=args.stage_root,
            poll_seconds=args.poll_seconds, timeout=args.timeout,
        )
        print(json.dumps({"batch_id": args.batch_id, "status": "passed" if result == 0 else "failed"}))
        return result
    except (BatchError, OSError, ValueError) as exc:
        print(f"ad_template_first50: {str(exc)[:1000]}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
