"""Canonical, authenticated, contact-safe Ad DB projection for Frank."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse

def _require_ad_db_auth(request: Request) -> None:
    # Runtime import avoids the web_server -> router import cycle.
    from hermes_cli.web_server import _require_token
    _require_token(request)


router = APIRouter(prefix="/v1/ad-db", dependencies=[Depends(_require_ad_db_auth)])
_LIMIT = 100
_MAX_OFFSET = 100_000
_HASH_PATH = re.compile(r"^sha256/[0-9a-f]{64}$")
_TEXT = re.compile(r"^[^,()\x00-\x1f\x7f]{1,120}$")
_LOCATION_RELATIONS = frozenset({"office", "service_area", "property", "copy_mention", "meta_targeting"})
_ROOT = Path("/srv/hermes/ad-db/assets")
_ADS = "id,library_id,advertiser_page_id,advertiser_page_meta_id,page_name,active_status,first_seen_at,last_seen_at,last_checked_at,ad_delivery_started_at,ad_delivery_stopped_at,ad_creation_date,ad_creative_id,format,headline,body,cta,ad_type,primary_intent,classification,display_state,ownership,locations,media"
_PROSPECTS = "prospect_type,subject_id,name,state,suburb,postcode,advertiser_page_id,page_id,page_name,platform,scan_enabled,scan_state,last_scan_started_at,last_scan_completed_at,agency,observed_ad_count"
_RUNS = "id,advertiser_page_id,scan_mode,status,started_at,completed_at,coverage_complete,pagination_exhausted,stop_reason,ads_seen,media_captured"


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _service_token() -> str:
    configured = os.environ.get("HERMES_AD_DB_SERVICE_KEY", "")
    if configured:
        return configured
    secret = os.environ.get("HERMES_AD_DB_JWT_SECRET", "")
    if not secret:
        raise HTTPException(503, "Ad DB research service is not configured")
    now = int(time.time())
    header = _b64url(b'{"alg":"HS256","typ":"JWT"}')
    claims = _b64url(
        json.dumps(
            {"role": "service_role", "iat": now, "exp": now + 60},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    message = f"{header}.{claims}"
    signature = _b64url(
        hmac.new(secret.encode("utf-8"), message.encode("ascii"), hashlib.sha256).digest()
    )
    return f"{message}.{signature}"


def _settings() -> dict[str, Any]:
    from hermes_cli.config import load_config
    config = load_config() or {}
    settings = config.get("ad_db")
    return settings if isinstance(settings, dict) else {}


def _archive_root() -> Path:
    configured = str(_settings().get("archive_root") or "").strip()
    return Path(configured).expanduser() if configured else _ROOT


def _config() -> tuple[str, dict[str, str]]:
    # The env fallback is an internal deployment/test bridge; operators
    # configure the non-secret URL under ad_db.rest_url in config.yaml.
    url = str(
        _settings().get("rest_url")
        or os.environ.get("HERMES_AD_DB_REST_URL", "")
    ).rstrip("/")
    if not url:
        raise HTTPException(503, "Ad DB research service is not configured")
    key = _service_token()
    return url, {"apikey": key, "Authorization": f"Bearer {key}", "Accept-Profile": "research"}


async def _get(view: str, select: str, params: dict[str, str]) -> list[dict[str, Any]]:
    url, headers = _config()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{url}/{view}", params={"select": select, **params}, headers=headers)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as error:
        raise HTTPException(error.response.status_code, "Ad DB query rejected") from error
    except (httpx.HTTPError, ValueError) as error:
        raise HTTPException(503, "Ad DB research service unavailable") from error
    if not isinstance(payload, list):
        raise HTTPException(502, "Ad DB returned invalid data")
    return [item for item in payload if isinstance(item, dict)]


def _query_boundary(request: Request, allowed: set[str]) -> None:
    seen: set[str] = set()
    for key, _value in request.query_params.multi_items():
        if key not in allowed or key in seen:
            raise HTTPException(400, "Unsupported or repeated Ad DB filter")
        seen.add(key)


def _text(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not _TEXT.fullmatch(value):
        raise HTTPException(400, f"Invalid {name} filter")
    return value


def _offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("ascii")
        prefix, value = raw.split(":", 1)
        offset = int(value)
    except (ValueError, UnicodeError):
        raise HTTPException(400, "Invalid Ad DB cursor") from None
    if prefix != "ad-db" or offset < 0 or offset > _MAX_OFFSET:
        raise HTTPException(400, "Invalid Ad DB cursor")
    return offset


def _cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"ad-db:{offset}".encode("ascii")).decode("ascii").rstrip("=")


def _page(rows: list[dict[str, Any]], limit: int, offset: int) -> dict[str, Any]:
    more = len(rows) > limit
    return {
        "items": rows[:limit],
        "page": {"nextCursor": _cursor(offset + limit) if more else None, "limit": limit},
    }


def _location_filter(
    state: str | None,
    suburb: str | None,
    postcode: str | None,
    relation: str | None,
) -> str | None:
    location = {
        key: value
        for key, value in (
            ("state", _text(state, "state")),
            ("suburb", _text(suburb, "suburb")),
            ("postcode", _text(postcode, "postcode")),
            ("relation", relation),
        )
        if value is not None
    }
    return "cs." + json.dumps([location], separators=(",", ":")) if location else None


def _ads(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for ad in rows:
        for media in ad.get("media") if isinstance(ad.get("media"), list) else []:
            if isinstance(media, dict) and media.get("id"):
                media["archiveUrl"] = f"/v1/ad-db/ads/{ad['id']}/media/{media['id']}"
            if isinstance(media, dict):
                media.pop("sourceUrl", None); media.pop("sourceURLs", None)
    return rows


@router.get("/ads")
async def ads(
    request: Request,
    q: str | None = None,
    agentId: UUID | None = None,
    agentName: str | None = None,
    agencyId: UUID | None = None,
    agencyName: str | None = None,
    state: str | None = None,
    suburb: str | None = None,
    postcode: str | None = None,
    locationRelation: str | None = None,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=_LIMIT),
):
    allowed = {"q", "agentId", "agentName", "agencyId", "agencyName", "state", "suburb", "postcode", "locationRelation", "cursor", "limit"}
    _query_boundary(request, allowed)
    if locationRelation is not None and locationRelation not in _LOCATION_RELATIONS:
        raise HTTPException(400, "Invalid locationRelation filter")
    offset = _offset(cursor)
    params = {"order": "last_seen_at.desc,id.desc", "limit": str(limit + 1), "offset": str(offset)}
    q = _text(q, "q")
    if q:
        params["or"] = f"(page_name.ilike.*{q}*,headline.ilike.*{q}*,body.ilike.*{q}*)"
    for key, value, exact in (
        ("ownership->agent->>id", str(agentId) if agentId else None, True),
        ("ownership->agent->>name", _text(agentName, "agentName"), False),
        ("ownership->agency->>id", str(agencyId) if agencyId else None, True),
        ("ownership->agency->>name", _text(agencyName, "agencyName"), False),
    ):
        if value:
            params[key] = f"eq.{value}" if exact else f"ilike.*{value}*"
    locations = _location_filter(state, suburb, postcode, locationRelation)
    if locations:
        params["locations"] = locations
    rows = await _get("v_ad_db_ads", _ADS, params)
    return _page(_ads(rows), limit, offset)


@router.get("/ads/{ad_id}")
async def ad(ad_id: UUID):
    rows = await _get("v_ad_db_ads", _ADS, {"id": f"eq.{ad_id}", "limit": "1"})
    if not rows:
        raise HTTPException(404, "Ad not found")
    return _ads(rows)[0]


@router.api_route("/ads/{ad_id}/media/{asset_id}", methods=["GET", "HEAD"])
async def media(ad_id: UUID, asset_id: UUID):
    rows = await _get(
        "v_ad_db_archived_media",
        "id,observed_ad_id,object_key,content_hash,byte_size,mime_type,verified_at",
        {"observed_ad_id": f"eq.{ad_id}", "id": f"eq.{asset_id}", "limit": "1"},
    )
    if not rows:
        raise HTTPException(404, "Archived media not found")
    record = rows[0]
    key = str(record.get("object_key") or "")
    try:
        byte_size = int(record.get("byte_size"))
    except (TypeError, ValueError):
        raise HTTPException(404, "Archived media not found") from None
    if not _HASH_PATH.fullmatch(key) or record.get("content_hash") != key.split("/", 1)[1] or not record.get("verified_at"):
        raise HTTPException(404, "Archived media not found")
    root = _archive_root().resolve()
    path = (root / key).resolve()
    if not path.is_file() or root not in path.parents or path.stat().st_size != byte_size:
        raise HTTPException(404, "Archived media not found")
    mime_type = str(record.get("mime_type") or "application/octet-stream")
    if not re.fullmatch(r"[\w.+-]+/[\w.+-]+", mime_type):
        mime_type = "application/octet-stream"
    return FileResponse(
        path,
        media_type=mime_type,
        headers={
            "Cache-Control": "private, max-age=31536000, immutable",
            "ETag": f'"{record["content_hash"]}"',
        },
    )


@router.get("/prospects")
async def prospects(
    request: Request,
    q: str | None = None,
    agentId: UUID | None = None,
    agentName: str | None = None,
    agencyId: UUID | None = None,
    agencyName: str | None = None,
    state: str | None = None,
    suburb: str | None = None,
    postcode: str | None = None,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=_LIMIT),
):
    allowed = {"q", "agentId", "agentName", "agencyId", "agencyName", "state", "suburb", "postcode", "cursor", "limit"}
    _query_boundary(request, allowed)
    offset = _offset(cursor)
    params = {"order": "name.asc,subject_id.asc,advertiser_page_id.asc.nullslast", "limit": str(limit + 1), "offset": str(offset)}
    logic: list[str] = []
    q = _text(q, "q")
    if q:
        logic.append(f"or(name.ilike.*{q}*,page_name.ilike.*{q}*)")
    if agentId:
        params.update({"prospect_type": "eq.agent", "subject_id": f"eq.{agentId}"})
    if agentName:
        params["name"] = f"ilike.*{_text(agentName, 'agentName')}*"
        params["prospect_type"] = "eq.agent"
    if agencyId:
        logic.append(f"or(and(prospect_type.eq.agency,subject_id.eq.{agencyId}),agency->>id.eq.{agencyId})")
    if agencyName:
        value = _text(agencyName, "agencyName")
        logic.append(f"or(and(prospect_type.eq.agency,name.ilike.*{value}*),agency->>name.ilike.*{value}*)")
    if logic:
        params["and"] = "(" + ",".join(logic) + ")"
    for key, value in (("state", state), ("suburb", suburb), ("postcode", postcode)):
        clean = _text(value, key)
        if clean:
            params[key] = f"eq.{clean}"
    rows = await _get("v_ad_db_prospects", _PROSPECTS, params)
    return _page(rows, limit, offset)


@router.get("/runs")
async def runs(
    request: Request,
    status: str | None = None,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=_LIMIT),
):
    _query_boundary(request, {"status", "cursor", "limit"})
    offset = _offset(cursor)
    params = {"order": "started_at.desc,id.desc", "limit": str(limit + 1), "offset": str(offset)}
    status = _text(status, "status")
    if status:
        params["status"] = f"eq.{status}"
    return _page(await _get("v_ad_db_runs", _RUNS, params), limit, offset)


@router.post("/runs/scan", status_code=202)
async def scan():
    raise HTTPException(503, "Ad DB collection worker is not configured")
