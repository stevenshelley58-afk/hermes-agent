"""Authenticated, fixed-scope vault broker consumed by Frank."""
from __future__ import annotations

from typing import Any

try:
    from fastapi import APIRouter, Header, HTTPException, Request
    from fastapi.responses import JSONResponse
except Exception:  # pragma: no cover - local unit tests without dashboard deps
    APIRouter = None  # type: ignore

from hermes_constants import get_hermes_home

try:
    from hermes_plugins.connections_agent.runtime import (
        BROKER_BASE_PATH,
        ConnectionsError,
        ConnectionsRuntime,
        failure_payload,
        load_settings,
    )
except ImportError:  # pragma: no cover - direct plugin-api import
    import importlib.util
    import sys
    from pathlib import Path
    _path = Path(__file__).resolve().parents[1] / "runtime.py"
    _spec = importlib.util.spec_from_file_location("connections_agent_runtime", _path)
    _mod = importlib.util.module_from_spec(_spec)
    assert _spec and _spec.loader
    sys.modules["connections_agent_runtime"] = _mod
    _spec.loader.exec_module(_mod)
    BROKER_BASE_PATH = _mod.BROKER_BASE_PATH
    ConnectionsError = _mod.ConnectionsError
    ConnectionsRuntime = _mod.ConnectionsRuntime
    failure_payload = _mod.failure_payload
    load_settings = _mod.load_settings

router = APIRouter() if APIRouter is not None else None
_runtime = ConnectionsRuntime(load_settings())


def _principal(request: Request) -> str:
    principal = getattr(getattr(request, "state", None), "token_principal", None)
    if principal is None or principal.principal != "frank-vault-broker":
        raise HTTPException(status_code=401, detail="Unauthorized")
    return principal.principal


async def _body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > 64 * 1024:
        raise HTTPException(status_code=413, detail="Request too large")
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="JSON object required") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON object required")
    return payload


def _json(payload: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers={"Cache-Control": "no-store"})


if router is not None:
    from hermes_cli.dashboard_auth.token_auth import register_token_route

    for _route in ("health", "secrets/create", "secrets/rotate", "secrets/delete", "secrets/list-metadata"):
        register_token_route(f"{BROKER_BASE_PATH}/{_route}")

    @router.get("/vault-broker/health")
    async def health(request: Request):
        _principal(request)
        return _json(_runtime.broker_health())

    @router.post("/vault-broker/secrets/list-metadata")
    async def list_metadata(request: Request):
        principal = _principal(request)
        try:
            return _json(_runtime.broker_list_metadata(principal=principal))
        except ConnectionsError as exc:
            return _json(failure_payload(exc), status=503)

    async def _mutate(request: Request, operation: str, idempotency_key: str | None):
        principal = _principal(request)
        if not idempotency_key:
            raise HTTPException(status_code=400, detail="Idempotency-Key is required")
        payload = await _body(request)
        try:
            return _json(_runtime.broker_mutate(operation, payload, principal=principal, idempotency_key=idempotency_key))
        except ConnectionsError as exc:
            status = 409 if "Idempotency-Key" in str(exc) else 400
            return _json(failure_payload(exc, provider_receipt=payload.get("provider_receipt")), status=status)

    @router.post("/vault-broker/secrets/create")
    async def create(request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        return await _mutate(request, "create", idempotency_key)

    @router.post("/vault-broker/secrets/rotate")
    async def rotate(request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        return await _mutate(request, "rotate", idempotency_key)

    @router.post("/vault-broker/secrets/delete")
    async def delete(request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        return await _mutate(request, "delete", idempotency_key)
