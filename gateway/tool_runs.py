"""Durable, replayable Tool-run state for API-server control surfaces.

Tool runs are deliberately separate from chat sessions.  They persist compact,
redacted operational events that a UI can replay after reconnecting without
copying prompts, credentials, or tool payloads into a second transcript.
"""

from __future__ import annotations

import copy
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


TOOL_RUN_COMMAND_SCHEMA = "schema://hermes.tool-run-command/v1"
TOOL_RUN_EVENT_SCHEMA = "schema://hermes.tool-run-event/v1"
TOOL_MODEL_POLICY_SCHEMA = "schema://hermes.tool-model-policy/v1"

_ID = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._:-]{0,127})$")
_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID = re.compile(r"^[0-9a-f]{16}$")
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_RUN_STATUSES = frozenset({
    "queued", "running", "waiting_for_approval", "blocked", "completed",
    "failed", "cancelling", "cancelled",
})
_EVENT_STATUSES = frozenset({"queued", "running", "ok", "blocked", "error", "cancelled"})
_SECRET_KEY = re.compile(r"(?:api[_-]?key|token|secret|password|credential|private[_-]?key|authorization)", re.I)
_SAFE_USAGE_KEYS = frozenset({"input_tokens", "output_tokens", "total_tokens"})
_MAX_JSON_BYTES = 512 * 1024
_MASKED_EDIT_MODELS = frozenset({
    "gemini-3.1-flash-image", "gemini-3-pro-image", "gpt-image-2",
})
_KNOWN_IMAGE_ONLY = frozenset({"gemini-3.1-flash-image", "gemini-3-pro-image", "gpt-image-2"})


class ToolRunError(ValueError):
    """Raised when a Tool-run contract or state transition is invalid."""


def _now() -> float:
    return time.time()


def _clean_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ToolRunError(f"{field} must be a bounded safe identifier")
    return value


def _safe_data(value: Any, field: str = "data", depth: int = 0) -> Any:
    if depth > 12:
        raise ToolRunError(f"{field} is too deeply nested")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > 32_000:
            raise ToolRunError(f"{field} contains an oversized string")
        return value
    if isinstance(value, list):
        if len(value) > 1_000:
            raise ToolRunError(f"{field} contains too many items")
        return [_safe_data(item, f"{field}[]", depth + 1) for item in value]
    if isinstance(value, dict):
        if len(value) > 500:
            raise ToolRunError(f"{field} contains too many fields")
        result: Dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 128:
                raise ToolRunError(f"{field} contains an invalid key")
            if _SECRET_KEY.search(key) and key not in _SAFE_USAGE_KEYS:
                raise ToolRunError(f"{field}.{key} is secret-bearing and cannot be persisted")
            result[key] = _safe_data(item, f"{field}.{key}", depth + 1)
        return result
    raise ToolRunError(f"{field} contains an unsupported value")


def _json(value: Any, field: str) -> str:
    safe = _safe_data(value, field)
    encoded = json.dumps(safe, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) > _MAX_JSON_BYTES:
        raise ToolRunError(f"{field} is too large")
    return encoded


def _loads(value: Optional[str], fallback: Any) -> Any:
    if not value:
        return copy.deepcopy(fallback)
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return copy.deepcopy(fallback)


def default_ad_template_policy() -> Dict[str, Any]:
    """Initial recommendation; model identities remain revisioned data."""
    checked_at = "2026-08-14T03:24:31Z"
    return {
        "schema": TOOL_MODEL_POLICY_SCHEMA,
        "tool_id": "ad-template-generator",
        "name": "Recommended quality",
        "preset": "quality-first",
        "seed_revision": 2,
        "pricing_checked_at": checked_at,
        "stages": {
            "analyse": {
                "capability": "vision_structured",
                "primary": {"provider": "openai-codex", "model": "gpt-5.5"},
                "fallbacks": [{"provider": "gemini", "model": "gemini-3.6-flash"}],
                "max_attempts": 2,
                "timeout_seconds": 120,
                "max_cost_usd": 0.60,
            },
            "masked-text-cleanup": {
                "capability": "masked_image_edit",
                "primary": {"provider": "gemini", "model": "gemini-3.1-flash-image"},
                "fallbacks": [
                    {"provider": "gemini", "model": "gemini-3-pro-image"},
                    {"provider": "openai-api", "model": "gpt-image-2"},
                ],
                "max_attempts": 3,
                "timeout_seconds": 180,
                "max_cost_usd": 2.00,
            },
            "story-extend": {
                "capability": "masked_image_edit",
                "primary": {"provider": "gemini", "model": "gemini-3.1-flash-image"},
                "fallbacks": [{"provider": "openai-api", "model": "gpt-image-2"}],
                "max_attempts": 2,
                "timeout_seconds": 180,
                "max_cost_usd": 1.00,
                "optional": True,
            },
            "visual-qa": {
                "capability": "vision_structured",
                "primary": {"provider": "openai-codex", "model": "gpt-5.5"},
                "fallbacks": [{"provider": "gemini", "model": "gemini-3.6-flash"}],
                "max_attempts": 2,
                "timeout_seconds": 120,
                "max_cost_usd": 0.60,
            },
        },
        "deterministic_stages": [
            "source", "decompose", "restyle", "story-draft", "check",
            "subject-invariance", "studio-qa", "ready", "release",
        ],
    }


def _is_legacy_seed_policy(policy: Any) -> bool:
    """Recognize only the exact first-party seed that needs superseding."""
    if not isinstance(policy, dict):
        return False
    if policy.get("name") != "Recommended quality" or policy.get("preset") != "quality-first":
        return False
    stages = policy.get("stages") if isinstance(policy.get("stages"), dict) else {}
    expected = {
        "analyse": ("openai", "gpt-5.5"),
        "masked-text-cleanup": ("google", "gemini-3.1-flash-image"),
        "story-extend": ("google", "gemini-3.1-flash-image"),
        "visual-qa": ("openai", "gpt-5.5"),
    }
    for stage_id, identity in expected.items():
        stage = stages.get(stage_id) if isinstance(stages.get(stage_id), dict) else {}
        primary = stage.get("primary") if isinstance(stage.get("primary"), dict) else {}
        if (primary.get("provider"), primary.get("model")) != identity:
            return False
    return True


def validate_model_policy(policy: Any, *, tool_id: Optional[str] = None) -> Dict[str, Any]:
    if not isinstance(policy, dict) or policy.get("schema") != TOOL_MODEL_POLICY_SCHEMA:
        raise ToolRunError("invalid Tool model policy schema")
    result = _safe_data(policy, "model_policy")
    policy_tool = _clean_id(result.get("tool_id"), "model_policy.tool_id")
    if tool_id and policy_tool != tool_id:
        raise ToolRunError("model policy tool_id does not match the run")
    stages = result.get("stages")
    if not isinstance(stages, dict) or not stages:
        raise ToolRunError("model policy must declare AI stages")
    for stage_id, stage in stages.items():
        _clean_id(stage_id, "model policy stage")
        if not isinstance(stage, dict):
            raise ToolRunError(f"model policy stage {stage_id} must be an object")
        _clean_id(stage.get("capability"), f"model policy stage {stage_id}.capability")
        candidates = [stage.get("primary"), *(stage.get("fallbacks") or [])]
        if not candidates or candidates[0] is None:
            raise ToolRunError(f"model policy stage {stage_id} requires a primary model")
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise ToolRunError(f"model policy stage {stage_id} has an invalid candidate")
            _clean_id(candidate.get("provider"), "model provider")
            model = candidate.get("model")
            if not isinstance(model, str) or not model.strip() or len(model) > 200:
                raise ToolRunError("model must be a bounded non-empty string")
            model = model.strip().lower()
            declared = candidate.get("capabilities") if isinstance(candidate.get("capabilities"), list) else []
            verified = candidate.get("capability_verified") is True
            if stage.get("capability") == "masked_image_edit" and model not in _MASKED_EDIT_MODELS:
                if not verified or "masked_image_edit" not in declared:
                    raise ToolRunError(f"model {model} has not verified masked-image-edit support")
            if stage.get("capability") == "vision_structured" and model in _KNOWN_IMAGE_ONLY:
                raise ToolRunError(f"image-generation model {model} cannot perform structured vision analysis")
        attempts = stage.get("max_attempts", len(candidates))
        if not isinstance(attempts, int) or attempts < 1 or attempts > 10:
            raise ToolRunError(f"model policy stage {stage_id}.max_attempts is invalid")
        for numeric in ("timeout_seconds", "max_cost_usd"):
            value = stage.get(numeric)
            if value is not None and (not isinstance(value, (int, float)) or value < 0):
                raise ToolRunError(f"model policy stage {stage_id}.{numeric} is invalid")
    return result


def validate_command(command: Any) -> Dict[str, Any]:
    if not isinstance(command, dict) or command.get("schema") != TOOL_RUN_COMMAND_SCHEMA:
        raise ToolRunError("invalid Tool-run command schema")
    allowed = {
        "schema", "request_id", "tool_id", "action", "scope", "payload",
        "idempotency_key", "model_policy_revision", "model_policy_override",
    }
    if set(command) - allowed:
        raise ToolRunError("Tool-run command contains unsupported fields")
    result = _safe_data(command, "command")
    _clean_id(result.get("request_id"), "request_id")
    tool_id = _clean_id(result.get("tool_id"), "tool_id")
    _clean_id(result.get("action"), "action")
    _clean_id(result.get("idempotency_key"), "idempotency_key")
    scope = result.get("scope")
    if not isinstance(scope, dict) or not isinstance(scope.get("project_id"), str) or not scope["project_id"].strip():
        raise ToolRunError("command scope.project_id is required")
    if not isinstance(result.get("payload"), dict):
        raise ToolRunError("command payload must be an object")
    revision = result.get("model_policy_revision")
    if revision is not None and (not isinstance(revision, int) or revision < 1):
        raise ToolRunError("model_policy_revision must be a positive integer")
    override = result.get("model_policy_override")
    if override is not None:
        validate_model_policy(override, tool_id=tool_id)
    return result


class ToolRunStore:
    """Thread-safe SQLite ledger for runs, model policies, and ordered events."""

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            try:
                from hermes_constants import get_hermes_home
                db_path = str(get_hermes_home() / "state.db")
            except Exception:
                db_path = ":memory:"
        self._db_path = None if db_path == ":memory:" else str(db_path)
        self._lock = threading.RLock()
        if self._db_path:
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        except Exception as exc:
            raise ToolRunError("durable Tool-run data plane is unavailable") from exc
        self._conn.row_factory = sqlite3.Row
        try:
            from hermes_state import apply_wal_with_fallback
            apply_wal_with_fallback(self._conn, db_label="state.db")
        except Exception:
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_schema()
        self._tighten_permissions()
        self.ensure_default_policy("ad-template-generator", default_ad_template_policy())

    def _create_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tool_model_policies (
                    tool_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    project_id TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    is_default INTEGER NOT NULL DEFAULT 0,
                    policy_json TEXT NOT NULL,
                    PRIMARY KEY (tool_id, revision)
                );
                CREATE TABLE IF NOT EXISTS tool_runs (
                    run_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    tool_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    scope_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    policy_revision INTEGER NOT NULL,
                    policy_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT,
                    progress REAL NOT NULL DEFAULT 0,
                    trace_id TEXT NOT NULL,
                    output_json TEXT,
                    error TEXT,
                    attention INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    started_at REAL,
                    completed_at REAL,
                    UNIQUE (tool_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS tool_runs_scope_updated
                    ON tool_runs(tool_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS tool_run_events (
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    schema TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    node_id TEXT,
                    trace_id TEXT NOT NULL,
                    span_id TEXT,
                    data_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, sequence),
                    FOREIGN KEY (run_id) REFERENCES tool_runs(run_id) ON DELETE CASCADE
                );
                """
            )
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(tool_model_policies)")}
            if "is_default" not in columns:
                self._conn.execute("ALTER TABLE tool_model_policies ADD COLUMN is_default INTEGER NOT NULL DEFAULT 0")
                self._conn.execute(
                    """UPDATE tool_model_policies SET is_default=1
                       WHERE (tool_id,revision) IN (
                           SELECT tool_id,MAX(revision) FROM tool_model_policies GROUP BY tool_id
                       )"""
                )
            if "project_id" not in columns:
                self._conn.execute("ALTER TABLE tool_model_policies ADD COLUMN project_id TEXT NOT NULL DEFAULT ''")
            self._conn.commit()

    def _tighten_permissions(self) -> None:
        if not self._db_path:
            return
        for raw in (self._db_path, f"{self._db_path}-wal", f"{self._db_path}-shm"):
            try:
                path = Path(raw)
                if path.exists():
                    path.chmod(0o600)
            except OSError:
                pass

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def ensure_default_policy(self, tool_id: str, policy: Dict[str, Any]) -> None:
        tool_id = _clean_id(tool_id, "tool_id")
        policy = validate_model_policy(policy, tool_id=tool_id)
        with self._lock:
            exists = self._conn.execute(
                "SELECT revision,policy_json FROM tool_model_policies "
                "WHERE tool_id=? AND project_id='' AND is_default=1 ORDER BY revision DESC LIMIT 1", (tool_id,)
            ).fetchone()
            if exists is None:
                self._conn.execute(
                    "INSERT INTO tool_model_policies(tool_id,revision,project_id,created_at,is_default,policy_json) VALUES(?,?,?,?,?,?)",
                    (tool_id, 1, "", _now(), 1, _json(policy, "model_policy")),
                )
                self._conn.commit()
            elif tool_id == "ad-template-generator" and _is_legacy_seed_policy(_loads(exists["policy_json"], {})):
                # Keep revision 1 immutable for reproducibility, and make the
                # corrected executable provider slugs the future default.
                revision = int(self._conn.execute(
                    "SELECT COALESCE(MAX(revision),0)+1 FROM tool_model_policies WHERE tool_id=?", (tool_id,)
                ).fetchone()[0])
                self._conn.execute(
                    "UPDATE tool_model_policies SET is_default=0 WHERE tool_id=? AND project_id=''", (tool_id,)
                )
                self._conn.execute(
                    "INSERT INTO tool_model_policies(tool_id,revision,project_id,created_at,is_default,policy_json) VALUES(?,?,?,?,?,?)",
                    (tool_id, revision, "", _now(), 1, _json(policy, "model_policy")),
                )
                self._conn.commit()

    def list_policies(self, tool_id: str, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        tool_id = _clean_id(tool_id, "tool_id")
        project_id = str(project_id or "").strip()
        with self._lock:
            if project_id:
                rows = self._conn.execute(
                    "SELECT revision,project_id,created_at,is_default,policy_json FROM tool_model_policies WHERE tool_id=? AND project_id=? ORDER BY revision DESC",
                    (tool_id, project_id),
                ).fetchall()
                if not rows:
                    rows = self._conn.execute(
                        "SELECT revision,project_id,created_at,is_default,policy_json FROM tool_model_policies WHERE tool_id=? AND project_id='' ORDER BY revision DESC",
                        (tool_id,),
                    ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT revision,project_id,created_at,is_default,policy_json FROM tool_model_policies WHERE tool_id=? ORDER BY revision DESC",
                    (tool_id,),
                ).fetchall()
        return [
            {"tool_id": tool_id, "project_id": row["project_id"], "revision": row["revision"], "created_at": row["created_at"], "is_default": bool(row["is_default"]), "policy": _loads(row["policy_json"], {})}
            for row in rows
        ]

    def get_policy(self, tool_id: str, revision: Optional[int] = None, *, project_id: Optional[str] = None) -> Dict[str, Any]:
        tool_id = _clean_id(tool_id, "tool_id")
        project_id = str(project_id or "").strip()
        query = "SELECT revision,project_id,created_at,is_default,policy_json FROM tool_model_policies WHERE tool_id=?"
        params: List[Any] = [tool_id]
        if revision is None:
            query += " AND project_id=? AND is_default=1 ORDER BY revision DESC LIMIT 1"
            params.append(project_id)
        else:
            if not isinstance(revision, int) or revision < 1:
                raise ToolRunError("policy revision must be positive")
            query += " AND revision=?"
            params.append(revision)
        with self._lock:
            row = self._conn.execute(query, tuple(params)).fetchone()
            if row is None and revision is None and project_id:
                row = self._conn.execute(
                    "SELECT revision,project_id,created_at,is_default,policy_json FROM tool_model_policies WHERE tool_id=? AND project_id='' AND is_default=1 ORDER BY revision DESC LIMIT 1",
                    (tool_id,),
                ).fetchone()
        if row is None:
            raise KeyError(f"model policy not found: {tool_id}@{revision or 'latest'}")
        return {"tool_id": tool_id, "project_id": row["project_id"], "revision": row["revision"], "created_at": row["created_at"], "is_default": bool(row["is_default"]), "policy": _loads(row["policy_json"], {})}

    def create_policy(self, tool_id: str, policy: Dict[str, Any], *, make_default: bool = True, project_id: str = "") -> Dict[str, Any]:
        tool_id = _clean_id(tool_id, "tool_id")
        project_id = str(project_id or "").strip()
        if project_id:
            _clean_id(project_id, "project_id")
        policy = validate_model_policy(policy, tool_id=tool_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(revision),0)+1 FROM tool_model_policies WHERE tool_id=?", (tool_id,)
            ).fetchone()
            revision = int(row[0])
            created_at = _now()
            if make_default:
                self._conn.execute("UPDATE tool_model_policies SET is_default=0 WHERE tool_id=? AND project_id=?", (tool_id, project_id))
            self._conn.execute(
                "INSERT INTO tool_model_policies(tool_id,revision,project_id,created_at,is_default,policy_json) VALUES(?,?,?,?,?,?)",
                (tool_id, revision, project_id, created_at, int(bool(make_default)), _json(policy, "model_policy")),
            )
            self._conn.commit()
        return {"tool_id": tool_id, "project_id": project_id, "revision": revision, "created_at": created_at, "is_default": bool(make_default), "policy": copy.deepcopy(policy)}

    def create_run(self, command: Dict[str, Any]) -> tuple[Dict[str, Any], bool]:
        command = validate_command(command)
        tool_id = command["tool_id"]
        with self._lock:
            existing = self._conn.execute(
                "SELECT run_id FROM tool_runs WHERE tool_id=? AND idempotency_key=?",
                (tool_id, command["idempotency_key"]),
            ).fetchone()
            if existing is not None:
                return self.get_run(existing["run_id"]), False
            project_id = str((command.get("scope") or {}).get("project_id") or "")
            override = command.get("model_policy_override")
            if override is not None:
                policy_record = self.create_policy(tool_id, override, make_default=False, project_id=project_id)
            else:
                policy_record = self.get_policy(tool_id, command.get("model_policy_revision"), project_id=project_id)
            run_id = f"trun_{uuid.uuid4().hex}"
            trace_id = uuid.uuid4().hex
            now = _now()
            self._conn.execute(
                """INSERT INTO tool_runs(
                    run_id,request_id,tool_id,action,idempotency_key,scope_json,payload_json,
                    policy_revision,policy_json,status,stage,progress,trace_id,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, command["request_id"], tool_id, command["action"], command["idempotency_key"],
                    _json(command["scope"], "scope"), _json(command["payload"], "payload"),
                    policy_record["revision"], _json(policy_record["policy"], "model_policy"),
                    "queued", "source", 0.0, trace_id, now, now,
                ),
            )
            self._conn.commit()
        self.append_event(run_id, "command.accepted", status="queued", node_id="source", data={"policy_revision": policy_record["revision"]})
        return self.get_run(run_id), True

    def _row_to_run(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "object": "hermes.tool_run",
            "run_id": row["run_id"],
            "request_id": row["request_id"],
            "tool_id": row["tool_id"],
            "action": row["action"],
            "idempotency_key": row["idempotency_key"],
            "scope": _loads(row["scope_json"], {}),
            "payload": _loads(row["payload_json"], {}),
            "model_policy_revision": row["policy_revision"],
            "model_policy": _loads(row["policy_json"], {}),
            "status": row["status"],
            "stage": row["stage"],
            "progress": row["progress"],
            "trace_id": row["trace_id"],
            "output": _loads(row["output_json"], None),
            "error": row["error"],
            "attention": bool(row["attention"]),
            "cancel_requested": bool(row["cancel_requested"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
        }

    def get_run(self, run_id: str) -> Dict[str, Any]:
        _clean_id(run_id, "run_id")
        with self._lock:
            row = self._conn.execute("SELECT * FROM tool_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"Tool run not found: {run_id}")
        return self._row_to_run(row)

    def list_runs(self, *, tool_id: Optional[str] = None, project_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        if not isinstance(limit, int) or limit < 1 or limit > 500:
            raise ToolRunError("limit must be between 1 and 500")
        clauses: List[str] = []
        params: List[Any] = []
        if tool_id:
            clauses.append("tool_id=?")
            params.append(_clean_id(tool_id, "tool_id"))
        query = "SELECT * FROM tool_runs"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, tuple(params)).fetchall()
        runs = [self._row_to_run(row) for row in rows]
        if project_id:
            runs = [item for item in runs if item["scope"].get("project_id") == project_id]
        return runs

    def append_event(
        self,
        run_id: str,
        kind: str,
        *,
        status: str = "ok",
        node_id: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
        span_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        _clean_id(run_id, "run_id")
        _clean_id(kind, "event kind")
        if status not in _EVENT_STATUSES:
            raise ToolRunError("unsupported event status")
        if node_id is not None:
            _clean_id(node_id, "node_id")
        if span_id is not None and not _SPAN_ID.fullmatch(span_id):
            raise ToolRunError("span_id must be lowercase W3C 16-hex")
        data_json = _json(data or {}, "event.data")
        timestamp = _now()
        with self._lock:
            run = self._conn.execute("SELECT trace_id FROM tool_runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(f"Tool run not found: {run_id}")
            sequence = int(self._conn.execute(
                "SELECT COALESCE(MAX(sequence),-1)+1 FROM tool_run_events WHERE run_id=?", (run_id,)
            ).fetchone()[0])
            self._conn.execute(
                """INSERT INTO tool_run_events(
                    run_id,sequence,schema,kind,status,timestamp,node_id,trace_id,span_id,data_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (run_id, sequence, TOOL_RUN_EVENT_SCHEMA, kind, status, timestamp, node_id, run["trace_id"], span_id, data_json),
            )
            self._conn.execute("UPDATE tool_runs SET updated_at=? WHERE run_id=?", (timestamp, run_id))
            self._conn.commit()
        return {
            "schema": TOOL_RUN_EVENT_SCHEMA,
            "run_id": run_id,
            "sequence": sequence,
            "kind": kind,
            "status": status,
            "timestamp": timestamp,
            "node_id": node_id,
            "trace_id": run["trace_id"],
            "span_id": span_id,
            "data": _loads(data_json, {}),
        }

    def events(self, run_id: str, *, after: int = -1, limit: int = 1000) -> List[Dict[str, Any]]:
        _clean_id(run_id, "run_id")
        if not isinstance(after, int) or after < -1:
            raise ToolRunError("event cursor is invalid")
        if not isinstance(limit, int) or limit < 1 or limit > 5000:
            raise ToolRunError("event limit is invalid")
        with self._lock:
            rows = self._conn.execute(
                """SELECT sequence,schema,kind,status,timestamp,node_id,trace_id,span_id,data_json
                   FROM tool_run_events WHERE run_id=? AND sequence>? ORDER BY sequence ASC LIMIT ?""",
                (run_id, after, limit),
            ).fetchall()
        return [
            {
                "schema": row["schema"], "run_id": run_id, "sequence": row["sequence"],
                "kind": row["kind"], "status": row["status"], "timestamp": row["timestamp"],
                "node_id": row["node_id"], "trace_id": row["trace_id"], "span_id": row["span_id"],
                "data": _loads(row["data_json"], {}),
            }
            for row in rows
        ]

    def update_run(self, run_id: str, *, status: Optional[str] = None, stage: Optional[str] = None,
                   progress: Optional[float] = None, output: Any = None, error: Optional[str] = None,
                   attention: Optional[bool] = None) -> Dict[str, Any]:
        current = self.get_run(run_id)
        updates: Dict[str, Any] = {"updated_at": _now()}
        if status is not None:
            if status not in _RUN_STATUSES:
                raise ToolRunError("unsupported run status")
            if current["status"] in _TERMINAL_STATUSES and status != current["status"]:
                raise ToolRunError("terminal Tool runs cannot transition")
            updates["status"] = status
            if status == "running" and current["started_at"] is None:
                updates["started_at"] = _now()
            if status in _TERMINAL_STATUSES:
                updates["completed_at"] = _now()
        if stage is not None:
            updates["stage"] = _clean_id(stage, "stage")
        if progress is not None:
            if not isinstance(progress, (int, float)) or progress < 0 or progress > 1:
                raise ToolRunError("progress must be between 0 and 1")
            updates["progress"] = float(progress)
        if output is not None:
            updates["output_json"] = _json(output, "output")
        if error is not None:
            updates["error"] = str(error)[:4000]
        if attention is not None:
            updates["attention"] = int(bool(attention))
        columns = ",".join(f"{key}=?" for key in updates)
        with self._lock:
            self._conn.execute(f"UPDATE tool_runs SET {columns} WHERE run_id=?", (*updates.values(), run_id))
            self._conn.commit()
        return self.get_run(run_id)

    def request_cancel(self, run_id: str) -> Dict[str, Any]:
        run = self.get_run(run_id)
        if run["status"] in _TERMINAL_STATUSES:
            return run
        with self._lock:
            self._conn.execute(
                "UPDATE tool_runs SET cancel_requested=1,status='cancelling',updated_at=? WHERE run_id=?",
                (_now(), run_id),
            )
            self._conn.commit()
        self.append_event(run_id, "command.cancel-requested", status="running", node_id=run.get("stage"), data={})
        return self.get_run(run_id)

    def requeue(self, run_id: str, *, stage: Optional[str] = None) -> Dict[str, Any]:
        run = self.get_run(run_id)
        next_stage = _clean_id(stage or run.get("stage") or "source", "stage")
        with self._lock:
            self._conn.execute(
                """UPDATE tool_runs SET status='queued',stage=?,cancel_requested=0,
                   attention=0,error=NULL,completed_at=NULL,updated_at=? WHERE run_id=?""",
                (next_stage, _now(), run_id),
            )
            self._conn.commit()
        self.append_event(
            run_id, "command.queued", status="queued", node_id=next_stage,
            data={"resume_from": next_stage},
        )
        return self.get_run(run_id)

    def replace_remaining_policy(self, run_id: str, policy: Dict[str, Any]) -> Dict[str, Any]:
        run = self.get_run(run_id)
        if run["status"] in _TERMINAL_STATUSES:
            raise ToolRunError("completed Tool runs cannot change model policy")
        policy = validate_model_policy(policy, tool_id=run["tool_id"])
        pipeline = ["source", "analyse", "decompose", "restyle", "story-draft", "check", "subject-invariance", "studio-qa", "ready", "release"]
        stage_starts = {"analyse": "analyse", "masked-text-cleanup": "restyle", "story-extend": "story-draft", "visual-qa": "check"}
        current_index = pipeline.index(run.get("stage")) if run.get("stage") in pipeline else 0
        merged = copy.deepcopy(policy)
        changed: List[str] = []
        for stage_id, candidate in policy.get("stages", {}).items():
            start_index = pipeline.index(stage_starts.get(stage_id, "source"))
            if start_index <= current_index:
                merged["stages"][stage_id] = copy.deepcopy((run.get("model_policy") or {}).get("stages", {}).get(stage_id, candidate))
            else:
                changed.append(stage_id)
        if not changed:
            raise ToolRunError("no unstarted AI stages remain")
        policy_record = self.create_policy(
            run["tool_id"], merged, make_default=False,
            project_id=str((run.get("scope") or {}).get("project_id") or ""),
        )
        with self._lock:
            self._conn.execute(
                "UPDATE tool_runs SET policy_revision=?,policy_json=?,updated_at=? WHERE run_id=?",
                (policy_record["revision"], _json(policy_record["policy"], "model_policy"), _now(), run_id),
            )
            self._conn.commit()
        self.append_event(
            run_id, "model-policy.changed", status="ok", node_id=run.get("stage"),
            data={"policy_revision": policy_record["revision"], "applies_to": "remaining-stages", "changed_stages": changed},
        )
        return self.get_run(run_id)

    def recover_incomplete(self) -> List[Dict[str, Any]]:
        """Return interrupted runs to the queue without erasing checkpoints."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id,stage FROM tool_runs WHERE status IN ('running','cancelling') ORDER BY created_at"
            ).fetchall()
            self._conn.execute(
                """UPDATE tool_runs SET status='queued',cancel_requested=0,attention=1,updated_at=?
                   WHERE status IN ('running','cancelling')""",
                (_now(),),
            )
            self._conn.commit()
        recovered: List[Dict[str, Any]] = []
        for row in rows:
            self.append_event(
                row["run_id"], "run.recovered", status="queued", node_id=row["stage"],
                data={"reason": "gateway-restart", "resume_from": row["stage"]},
            )
            recovered.append(self.get_run(row["run_id"]))
        return recovered
