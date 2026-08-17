"""Hermes-owned setup contract for the Frank knowledge stack."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import tempfile
from typing import Mapping

KNOWLEDGE_SECRET_FILE = Path("/srv/hermes/secrets/knowledge.env")
KNOWLEDGE_LOCK_FILE = Path("/srv/hermes/secrets/.knowledge-setup.lock")
APPROVED_FRANK_RECEIPT = Path("/var/lib/frank/release/approved-sha")
APPROVED_FRANK_HELPER_RECEIPT = Path("/var/lib/frank/release/frank-knowledge-helper.sha256")
APPROVED_FRANK_HELPER_SHA256 = "6fdbeb5b0cac9be1d01251adda5a2789702ce3a918d6ce09ccf3833b405343a0"
DEPLOY_HELPER = Path("/usr/local/sbin/frank-knowledge-deploy")
# Check / retry is intentionally the same idempotent Frank-owned action: its
# final deploy check is the only authoritative health result.
CHECK_HELPER = DEPLOY_HELPER
FRANK_REPOSITORY = Path("/projects/frank")
APPROVED_FRANK_SHA = "32a8b2d15bbf517f9977a962ae236a2582fc6701"
KNOWLEDGE_NAMESPACE = "project/frank"
NEO4J_RECOMMENDED_VERSION = "Neo4j 5.26 Community"
NEO4J_IMAGE = (
    "neo4j@sha256:"
    "d9dd3dc7d1c78fa959191ff02dbdcbefadceaf83eee23428fb92a58cac8ad3fe"
)

_IMAGE_RE = re.compile(r"^neo4j@sha256:[0-9a-f]{64}$")
_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_VALUE_RE = re.compile(r"^[^\x00-\x08\x0b\x0c\x0e-\x1f\x7f\r\n]*$")
_USER_KEYS = frozenset({
    "OPENAI_API_KEY", "NEO4J_IMAGE", "HERMES_ALLOWED_NAMESPACES",
    "FRANK_KNOWLEDGE_ALLOWED_PROJECTS",
})
_GENERATED_KEYS = frozenset({
    "HERMES_GRAPHITI_PROVIDER_TOKEN", "FRANK_KNOWLEDGE_PROJECTION_TOKEN",
    "NEO4J_PASSWORD",
})
_ALL_KEYS = _USER_KEYS | _GENERATED_KEYS
_CSRF_SECRET = secrets.token_bytes(32)


class KnowledgeSetupError(ValueError):
    """A safe, user-facing setup validation error."""


def validate_api_key(value: str) -> str:
    value = value.strip()
    if not value or not _VALUE_RE.fullmatch(value):
        raise KnowledgeSetupError("Enter a valid OpenAI API key.")
    return value


def validate_env_value(key: str, value: str) -> str:
    if key not in _ALL_KEYS or not _KEY_RE.fullmatch(key):
        raise KnowledgeSetupError("Unsupported knowledge setting.")
    if not isinstance(value, str) or not _VALUE_RE.fullmatch(value) or not value:
        raise KnowledgeSetupError("Knowledge settings must be single-line values.")
    if key == "NEO4J_IMAGE" and (value != NEO4J_IMAGE or not _IMAGE_RE.fullmatch(value)):
        raise KnowledgeSetupError("The recommended Neo4j image is fixed to an immutable release.")
    if key in {"HERMES_ALLOWED_NAMESPACES", "FRANK_KNOWLEDGE_ALLOWED_PROJECTS"} and value != KNOWLEDGE_NAMESPACE:
        raise KnowledgeSetupError("Frank knowledge is fixed to the project/frank namespace.")
    return value


def parse_env_file(path: Path = KNOWLEDGE_SECRET_FILE) -> dict[str, str]:
    """Read the small, strict knowledge env contract without exposing values."""
    if path.is_symlink() or not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            raw = stream.read()
    except (OSError, UnicodeError) as exc:
        raise KnowledgeSetupError("Knowledge settings are unavailable.") from exc
    values: dict[str, str] = {}
    for line_number, line in enumerate(raw.splitlines(keepends=True), 1):
        if line.endswith("\n"):
            line = line[:-1]
        if line.endswith("\r"):
            raise KnowledgeSetupError("Knowledge settings must use LF line endings.")
        if not line:
            continue
        if "=" not in line:
            raise KnowledgeSetupError(f"Knowledge settings are malformed (line {line_number}).")
        key, value = line.split("=", 1)
        if not _KEY_RE.fullmatch(key) or key not in _ALL_KEYS or key in values:
            raise KnowledgeSetupError("Knowledge settings contain an unsupported or duplicate key.")
        if not _VALUE_RE.fullmatch(value):
            raise KnowledgeSetupError("Knowledge settings contain a control character.")
        values[key] = value
    return values


def _lock_file():
    KNOWLEDGE_LOCK_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = KNOWLEDGE_LOCK_FILE.open("a+", encoding="utf-8")
    os.chmod(KNOWLEDGE_LOCK_FILE, 0o600)
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def _atomic_write(values: Mapping[str, str], path: Path = KNOWLEDGE_SECRET_FILE) -> None:
    if path.is_symlink():
        raise KnowledgeSetupError("Knowledge settings cannot be a symlink.")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".knowledge-", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            for key in sorted(values):
                stream.write(f"{key}={values[key]}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        os.chmod(path, 0o600)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def save_user_settings(api_key: str | None, path: Path = KNOWLEDGE_SECRET_FILE) -> dict[str, object]:
    """Atomically save only user-controlled keys; generated values are preserved."""
    with _lock_file():
        values = parse_env_file(path)
        if api_key is not None:
            values["OPENAI_API_KEY"] = validate_api_key(api_key)
        values["NEO4J_IMAGE"] = NEO4J_IMAGE
        values["HERMES_ALLOWED_NAMESPACES"] = KNOWLEDGE_NAMESPACE
        values["FRANK_KNOWLEDGE_ALLOWED_PROJECTS"] = KNOWLEDGE_NAMESPACE
        for key, value in values.items():
            validate_env_value(key, value)
        _atomic_write(values, path)
    return status(path)


def status(path: Path = KNOWLEDGE_SECRET_FILE) -> dict[str, object]:
    try:
        values = parse_env_file(path)
    except KnowledgeSetupError as exc:
        return {
            "configured": False, "api_key_set": False,
            "namespace": KNOWLEDGE_NAMESPACE, "allowed_project": KNOWLEDGE_NAMESPACE,
            "neo4j_version": NEO4J_RECOMMENDED_VERSION, "neo4j_image_pinned": False,
            "provider_ready": False, "projection_ready": False,
            "status": "error", "message": str(exc),
        }
    api_key_set = bool(values.get("OPENAI_API_KEY"))
    image_pinned = values.get("NEO4J_IMAGE") == NEO4J_IMAGE
    namespace_ok = values.get("HERMES_ALLOWED_NAMESPACES") == KNOWLEDGE_NAMESPACE
    project_ok = values.get("FRANK_KNOWLEDGE_ALLOWED_PROJECTS") == KNOWLEDGE_NAMESPACE
    configured = api_key_set and image_pinned and namespace_ok and project_ok
    return {
        "configured": configured, "api_key_set": api_key_set,
        "namespace": KNOWLEDGE_NAMESPACE, "allowed_project": KNOWLEDGE_NAMESPACE,
        "neo4j_version": NEO4J_RECOMMENDED_VERSION, "neo4j_image_pinned": image_pinned,
        "provider_ready": False, "projection_ready": False,
        "status": "ready" if configured else "needs_setup",
        "message": "Ready to start the knowledge stack." if configured else "Enter the OpenAI API key to continue.",
    }


def csrf_binding(request, *, loopback_token: str = "") -> str:
    session = getattr(request.state, "session", None)
    if session is not None:
        raw = "|".join(str(item) for item in (
            session.provider, session.user_id, session.expires_at, session.access_token,
        ))
    else:
        raw = f"loopback:{loopback_token}"
    return hmac.new(_CSRF_SECRET, raw.encode(), hashlib.sha256).hexdigest()


def mint_csrf(binding: str, store: dict[str, tuple[str, float]], now: float) -> str:
    token = secrets.token_urlsafe(32)
    store[token] = (binding, now + 600.0)
    return token


def consume_csrf(token: str, binding: str, store: dict[str, tuple[str, float]], now: float) -> bool:
    entry = store.pop(token, None)
    if entry is None:
        return False
    stored_binding, expires = entry
    return expires >= now and hmac.compare_digest(stored_binding, binding)
