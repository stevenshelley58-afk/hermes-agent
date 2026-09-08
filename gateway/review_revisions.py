"""Durable, bounded review-message revisions for ad-template runs.

The ledger lives with Hermes' ToolRunStore.  Images and candidate JSON are
copied into the run workspace before a revision mutates the checkpoint; the
only public handle to an image is the existing authenticated artifact route.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping


class ReviewRevisionError(ValueError):
    pass


_REVISION_ID = re.compile(r"^rrev_[0-9a-f]{32}$")
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_PLACEMENTS = ("feed", "story")
_MAX_SNAPSHOT_BYTES = 20 * 1024 * 1024


def _now() -> float:
    return time.time()


def _json(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(raw.encode("utf-8")) > 4_000_000:
        raise ReviewRevisionError("review snapshot is too large")
    return raw


def _safe_revision_id(value: str) -> str:
    if not isinstance(value, str) or not _REVISION_ID.fullmatch(value):
        raise ReviewRevisionError("invalid review revision id")
    return value


def _safe_candidate(candidate: Any) -> dict:
    if not isinstance(candidate, dict) or not isinstance(candidate.get("template"), dict):
        raise ReviewRevisionError("reviewed candidate checkpoint is unavailable")
    # A detached JSON copy is intentional: later checkpoints cannot rewrite the
    # candidate that the operator actually saw.
    return copy.deepcopy(candidate)


def _regular(workspace: Path, path: Path) -> Path:
    root = workspace.resolve(strict=True)
    path.relative_to(root)
    for part in [path, *path.parents]:
        if part == root:
            break
        if part.is_symlink():
            raise ReviewRevisionError("review snapshot paths cannot be symlinks")
    if not path.is_file() or path.stat().st_size > _MAX_SNAPSHOT_BYTES:
        raise ReviewRevisionError("review snapshot is unavailable")
    return path


def _exclusive_bytes(workspace: Path, path: Path, data: bytes) -> None:
    if len(data) > _MAX_SNAPSHOT_BYTES:
        raise ReviewRevisionError("review snapshot is too large")
    path.relative_to(workspace.resolve())
    for parent in path.parents:
        if parent == workspace.resolve():
            break
        if parent.is_symlink():
            raise ReviewRevisionError("review snapshot paths cannot be symlinks")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(data)


def freeze_snapshot(workspace: Path, revision_id: str, label: str, candidate: Mapping[str, Any]) -> dict:
    """Freeze only the final production render, never a latest-iteration guess."""
    _safe_revision_id(revision_id)
    if label not in {"before", "after"}:
        raise ReviewRevisionError("invalid review snapshot label")
    workspace = workspace.resolve(strict=True)
    expected = _safe_candidate(candidate)
    final_path = _regular(workspace, workspace / "final" / "artifact.json")
    detached = _safe_candidate(json.loads(final_path.read_text()))
    for key in ("feedLayout", "storyLayout"):
        if detached["template"].get(key) != expected["template"].get(key):
            raise ReviewRevisionError("final preview does not match the reviewed candidate")
    rendered_declarations = [{key: item.get(key) for key in ("assetKey", "fileName", "mimeType")} for item in detached.get("assets", [])]
    if sorted(rendered_declarations, key=lambda item: item["assetKey"]) != sorted(expected.get("assets", []), key=lambda item: item["assetKey"]):
        raise ReviewRevisionError("final preview assets do not match the reviewed candidate")
    # Renderer artifacts embed resolved image bytes. The editable controller
    # contract intentionally permits declarations only, so preserve the exact
    # checkpoint candidate for undo, while freezing its actual final pixels.
    detached = expected
    # Validate all sources before writing any immutable output.
    frames = {}
    for placement in _PLACEMENTS:
        path = _regular(workspace, workspace / "final" / "rendered" / f"{placement}.png")
        frames[placement] = path.read_bytes()
        if not frames[placement].startswith(b"\x89PNG\r\n\x1a\n"):
            raise ReviewRevisionError("final preview is not a PNG")
    candidate_bytes = _json(detached).encode("utf-8")
    candidate_sha = hashlib.sha256(candidate_bytes).hexdigest()
    root = workspace / "review-revisions" / revision_id
    candidate_name = f"{revision_id}-{label}-candidate.json"
    previews, hashes = {}, {}
    _exclusive_bytes(workspace, root / candidate_name, candidate_bytes)
    for placement, data in frames.items():
        name = f"{revision_id}-{label}-{placement}.png"
        _exclusive_bytes(workspace, workspace / "previews" / name, data)
        previews[placement] = name
        hashes[placement] = hashlib.sha256(data).hexdigest()
    fingerprint = hashlib.sha256((candidate_sha + hashes["feed"] + hashes["story"]).encode()).hexdigest()
    return {"candidate": candidate_name, "candidate_sha256": candidate_sha,
            "previews": previews, "hashes": hashes, "candidate_hash": fingerprint}


def load_snapshot_candidate(workspace: Path, snapshot: Mapping[str, Any]) -> dict:
    name = str(snapshot.get("candidate") or "")
    match = re.fullmatch(r"(rrev_[0-9a-f]{32})-(?:before|after)-candidate\.json", name)
    if not match:
        raise ReviewRevisionError("invalid review candidate snapshot")
    workspace = workspace.resolve(strict=True)
    target = _regular(workspace, workspace / "review-revisions" / match[1] / name)
    data = target.read_bytes()
    if hashlib.sha256(data).hexdigest() != snapshot.get("candidate_sha256"):
        raise ReviewRevisionError("review candidate snapshot integrity check failed")
    return _safe_candidate(json.loads(data))


class ReviewRevisionStore:
    """SQLite-backed revision records sharing ToolRunStore's transaction lock."""

    def __init__(self, tool_run_store: Any):
        self.store = tool_run_store
        self._lock = getattr(tool_run_store, "_lock", threading.RLock())
        with self._lock:
            self.store._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS review_revisions (
                    revision_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    parent_revision_id TEXT,
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    message TEXT NOT NULL,
                    annotations_json TEXT NOT NULL,
                    candidate_hash TEXT,
                    status TEXT NOT NULL,
                    before_json TEXT NOT NULL,
                    after_json TEXT,
                    undo_of TEXT,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(run_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS review_revisions_run_revision
                    ON review_revisions(run_id, revision);
                """
            )
            self.store._conn.commit()

    def _row(self, row: Any) -> dict:
        result = dict(row)
        for key in ("before_json", "after_json", "annotations_json"):
            result[key] = json.loads(result[key]) if result.get(key) else None
        return result

    def by_idempotency(self, run_id: str, key: str) -> dict | None:
        with self._lock:
            row = self.store._conn.execute(
                "SELECT * FROM review_revisions WHERE run_id=? AND idempotency_key=?", (run_id, key)
            ).fetchone()
        return self._row(row) if row else None

    def get(self, revision_id: str) -> dict:
        _safe_revision_id(revision_id)
        with self._lock:
            row = self.store._conn.execute("SELECT * FROM review_revisions WHERE revision_id=?", (revision_id,)).fetchone()
        if not row:
            raise KeyError("review revision not found")
        return self._row(row)

    def create_pending(self, *, run_id: str, project_id: str, idempotency_key: str, message: str,
                       annotations: list, expected_revision: int, candidate_hash: str | None,
                       before: dict, revision_id: str, request_hash: str, undo_of: str | None = None) -> dict:
        if not _IDEMPOTENCY.fullmatch(idempotency_key):
            raise ReviewRevisionError("idempotency_key is invalid")
        _safe_revision_id(revision_id)
        now = _now()
        with self._lock:
            prior = self.store._conn.execute(
                "SELECT * FROM review_revisions WHERE run_id=? AND idempotency_key=?", (run_id, idempotency_key)
            ).fetchone()
            if prior:
                return self._row(prior)
            parent = self.store._conn.execute(
                "SELECT revision_id FROM review_revisions WHERE run_id=? ORDER BY revision DESC LIMIT 1", (run_id,)
            ).fetchone()
            self.store._conn.execute(
                """INSERT INTO review_revisions
                (revision_id,run_id,project_id,revision,parent_revision_id,idempotency_key,request_hash,message,annotations_json,candidate_hash,status,before_json,undo_of,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (revision_id, run_id, project_id, expected_revision + 1,
                 parent[0] if parent else None, idempotency_key, request_hash, message,
                 _json(annotations), candidate_hash, "pending", _json(before), undo_of, now, now),
            )
            self.store._conn.commit()
        return self.get(revision_id)

    def mark_failed(self, revision_id: str, error: str) -> dict:
        with self._lock:
            self.store._conn.execute(
                "UPDATE review_revisions SET status='failed',error=?,updated_at=? WHERE revision_id=? AND status='pending'",
                (str(error)[:1000], _now(), revision_id),
            )
            self.store._conn.commit()
        return self.get(revision_id)

    def finalize_after(self, workspace: Path, run_id: str, candidate: Mapping[str, Any], *, status: str = "ready_for_review") -> dict | None:
        with self._lock:
            row = self.store._conn.execute(
                "SELECT * FROM review_revisions WHERE run_id=? AND status='pending' ORDER BY revision DESC LIMIT 1", (run_id,)
            ).fetchone()
        if not row:
            return None
        current = self._row(row)
        try:
            after = freeze_snapshot(workspace, current["revision_id"], "after", candidate)
        except Exception as exc:
            return self.mark_failed(current["revision_id"], str(exc))
        with self._lock:
            self.store._conn.execute(
                "UPDATE review_revisions SET status=?,after_json=?,updated_at=? WHERE revision_id=? AND status='pending'",
                (status, _json(after), _now(), current["revision_id"]),
            )
            self.store._conn.commit()
        return self.get(current["revision_id"])

    def list(self, run_id: str, project_id: str) -> list[dict]:
        with self._lock:
            rows = self.store._conn.execute(
                "SELECT * FROM review_revisions WHERE run_id=? AND project_id=? ORDER BY created_at ASC", (run_id, project_id)
            ).fetchall()
        return [self._row(row) for row in rows]


def restore_before_candidate(workspace: Path, revision: Mapping[str, Any]) -> dict:
    """Restore only the detached candidate, forcing fresh compare/final review."""
    from gateway.ad_template_generator_process import load_checkpoint, persist_checkpoint
    candidate = load_snapshot_candidate(workspace, revision.get("before_json") or {})
    checkpoint = load_checkpoint(workspace)
    iterations = checkpoint.get("iterations") if isinstance(checkpoint.get("iterations"), list) else []
    start = max((int(item.get("iteration") or 0) for item in iterations if isinstance(item, Mapping)), default=0)
    for key in ("bestCandidate", "bestReview", "bestIteration", "finalReview", "accepted", "manualInstructions", "feedback", "reusableValidation", "recentRejects", "stallDiagnosis"):
        checkpoint.pop(key, None)
    candidate["template"].setdefault("metadata", {}).pop("generationReview", None)
    checkpoint.update(candidate=copy.deepcopy(candidate), iterations=iterations, manualStartIteration=start,
                     manualRevision=int(checkpoint.get("manualRevision") or 0) + 1,
                     cycleComparisons=0, comparisonBudgetUsed=0, layerRefinementBudgetUsed=0)
    persist_checkpoint(workspace, checkpoint)
    return checkpoint

def validate_structured_review(value: Any, *, expected_revision: Any = None, candidate_hash: Any = None) -> dict:
    if not isinstance(value, Mapping): raise ReviewRevisionError("review must be an object")
    if set(value) - {"message", "annotations"}: raise ReviewRevisionError("review contains unsupported fields")
    message = value.get("message", "")
    if not isinstance(message, str) or len(message) > 1200: raise ReviewRevisionError("review message is invalid")
    message = message.strip(); raw = value.get("annotations", [])
    if not isinstance(raw, list) or len(raw) > 8: raise ReviewRevisionError("annotations must contain no more than 8 items")
    annotations = []; total = len(message)
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != {"placement", "x", "y", "width", "height", "message"}: raise ReviewRevisionError("annotation fields are invalid")
        placement = item["placement"]
        if placement not in _PLACEMENTS: raise ReviewRevisionError("annotation placement is invalid")
        nums = []
        for key in ("x", "y", "width", "height"):
            number = item[key]
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not __import__("math").isfinite(float(number)): raise ReviewRevisionError("annotation geometry must be finite numbers")
            nums.append(float(number))
        x, y, width, height = nums
        if width <= 0 or height <= 0 or x < 0 or y < 0 or x + width > 1 or y + height > 1: raise ReviewRevisionError("annotation rectangle must be contained in the canvas")
        note = item["message"]
        if not isinstance(note, str) or (not note.strip() and not message) or len(note) > 200: raise ReviewRevisionError("annotation message is invalid")
        note = note.strip(); total += len(note)
        annotations.append({"placement": placement, "x": round(x, 6), "y": round(y, 6), "width": round(width, 6), "height": round(height, 6), "message": note})
    if not annotations and not message: raise ReviewRevisionError("review needs a message or annotation")
    if total > 4000: raise ReviewRevisionError("review instructions exceed 4,000 characters")
    if candidate_hash is not None and (not isinstance(candidate_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", candidate_hash)): raise ReviewRevisionError("candidate_hash is invalid")
    if expected_revision is not None and (isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0): raise ReviewRevisionError("expected_revision is invalid")
    return {"message": message, "annotations": annotations}

def structured_review_instructions(review: Mapping[str, Any]) -> str:
    parts = [str(review.get("message") or "").strip()]
    for item in review.get("annotations") or []:
        parts.append(f"Focus on the {item['placement']} region at x={item['x']:.6f}, y={item['y']:.6f}, width={item['width']:.6f}, height={item['height']:.6f}: {item['message']}")
    parts.append("Preserve all other layers and source-matched details outside the selected regions.")
    result = "\n".join(part for part in parts if part)
    if len(result) > 4000: raise ReviewRevisionError("review instructions exceed 4,000 characters")
    return result
