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
from pydantic import BaseModel, Field

def _require_ad_db_auth(request: Request) -> None:
    if request.method in {"GET", "HEAD"}:
        expected = os.environ.get("HERMES_AD_DB_READ_TOKEN", "")
        presented = request.headers.get("X-Hermes-Ad-Db-Read-Token", "")
        if expected and presented and hmac.compare_digest(presented, expected):
            # Customer reads intentionally exclude prospect/runs operational
            # projections. The token is scoped to ad search/detail/media only.
            if request.url.path.startswith("/v1/ad-db/prospects") or request.url.path.startswith("/v1/ad-db/runs"):
                raise HTTPException(403, "Ad DB operational projections require operator authentication")
            request.state.ad_db_scope = "customer"
            return
    # Runtime import avoids the web_server -> router import cycle.
    from hermes_cli.web_server import _require_token
    _require_token(request)


router = APIRouter(prefix="/v1/ad-db", dependencies=[Depends(_require_ad_db_auth)])
_LIMIT = 100
_MAX_OFFSET = 100_000
_HASH_PATH = re.compile(r"^sha256/[0-9a-f]{64}$")
_TEXT = re.compile(r"^[^,()*%\x00-\x1f\x7f]{1,120}$")
_LOCATION_RELATIONS = frozenset({"office", "service_area", "property", "copy_mention", "meta_targeting"})
_ROOT = Path("/srv/hermes/ad-db/assets")
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/-]{7,127}$")
_ADS = "id,library_id,advertiser_page_id,advertiser_page_meta_id,page_name,active_status,first_seen_at,last_seen_at,last_checked_at,ad_delivery_started_at,ad_delivery_stopped_at,ad_creation_date,ad_creative_id,format,headline,body,cta,ad_type,primary_intent,classification,display_state,ownership,locations,media"
_PROSPECTS = "prospect_type,subject_id,name,state,suburb,postcode,advertiser_page_id,page_id,page_name,platform,scan_enabled,scan_state,last_scan_started_at,last_scan_completed_at,agency,observed_ad_count"
_RUNS = "id,advertiser_page_id,scan_mode,status,started_at,completed_at,coverage_complete,pagination_exhausted,stop_reason,ads_seen,media_captured"
_AD_PUBLIC_FIELDS = frozenset(_ADS.split(","))
_MEDIA_PUBLIC_FIELDS = frozenset({"id", "kind", "storageBucket", "objectKey", "sha256", "byteSize", "mimeType", "width", "height", "durationMs", "archiveUrl"})
_OWNER_PUBLIC_FIELDS = frozenset({"id", "name", "relationship"})
_LOCATION_PUBLIC_FIELDS = frozenset({"id", "suburb", "state", "postcode", "relation"})


class BoundedScanRequest(BaseModel):
    """One explicit, bounded page scan; never a request to drain the queue."""

    pageIds: list[UUID] = Field(min_length=1, max_length=50)
    maxCredits: float = Field(default=25.0, gt=0, le=25)
    idempotencyKey: str = Field(min_length=8, max_length=128)


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
    # Non-secret endpoint settings are config.yaml values. Environment is
    # reserved for the service/read credentials below.
    url = str(_settings().get("rest_url") or "").rstrip("/")
    if not url:
        raise HTTPException(503, "Ad DB research service is not configured")
    key = _service_token()
    return url, {"apikey": key, "Authorization": f"Bearer {key}", "Accept-Profile": "research"}


async def _get(view: str, select: str, params: dict[str, str]) -> list[dict[str, Any]]:
    url, headers = _config()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{url}/{view}", params={"select": select, **params}, headers={**headers, "Content-Profile": "research"})
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as error:
        raise HTTPException(error.response.status_code, "Ad DB query rejected") from error
    except (httpx.HTTPError, ValueError) as error:
        raise HTTPException(503, "Ad DB research service unavailable") from error
    if not isinstance(payload, list):
        raise HTTPException(502, "Ad DB returned invalid data")
    return [item for item in payload if isinstance(item, dict)]


async def _post(view: str, payload: dict[str, Any], *, params: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Write only to the existing research API; this does not run a worker."""
    url, headers = _config()
    request_headers = {**headers, "Content-Type": "application/json", "Content-Profile": "research", "Prefer": "return=representation"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{url}/{view}",
                params=params or {},
                headers=request_headers,
                json=payload,
            )
        response.raise_for_status()
        result = response.json()
    except httpx.HTTPStatusError as error:
        raise HTTPException(error.response.status_code, "Ad DB queue rejected") from error
    except (httpx.HTTPError, ValueError) as error:
        raise HTTPException(503, "Ad DB research service unavailable") from error
    if not isinstance(result, list):
        raise HTTPException(502, "Ad DB returned invalid queue data")
    return [item for item in result if isinstance(item, dict)]


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
        # The select list is a first boundary; these nested JSON allowlists are
        # the second, so unexpected contact/provider fields cannot cross it.
        for key in list(ad):
            if key not in _AD_PUBLIC_FIELDS:
                ad.pop(key, None)
        ownership = ad.get("ownership")
        if isinstance(ownership, dict):
            ad["ownership"] = {
                kind: {key: value for key, value in owner.items() if key in _OWNER_PUBLIC_FIELDS}
                for kind, owner in ownership.items()
                if kind in {"agent", "agency"} and isinstance(owner, dict)
            }
        locations = ad.get("locations")
        if isinstance(locations, list):
            ad["locations"] = [
                {key: value for key, value in location.items() if key in _LOCATION_PUBLIC_FIELDS}
                for location in locations
                if isinstance(location, dict)
            ]
        media_items = ad.get("media")
        if isinstance(media_items, list):
            cleaned_media = []
            for media in media_items:
                if not isinstance(media, dict):
                    continue
                clean = {key: value for key, value in media.items() if key in _MEDIA_PUBLIC_FIELDS}
                if not (_HASH_PATH.fullmatch(str(clean.get("objectKey") or "")) and clean.get("sha256") == str(clean.get("objectKey")).split("/", 1)[1]):
                    clean.pop("objectKey", None)
                    clean.pop("sha256", None)
                if clean.get("id"):
                    clean["archiveUrl"] = f"/v1/ad-db/ads/{ad['id']}/media/{clean['id']}"
                clean.pop("sourceUrl", None)
                clean.pop("sourceURLs", None)
                cleaned_media.append(clean)
            ad["media"] = cleaned_media
    return rows


@router.get("/ads")
async def ads(
    request: Request,
    q: str | None = None,
    agentId: UUID | None = None,
    agentName: str | None = None,
    agencyId: UUID | None = None,
    agencyName: str | None = None,
    advertiserPageId: UUID | None = None,
    state: str | None = None,
    suburb: str | None = None,
    postcode: str | None = None,
    locationRelation: str | None = None,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=_LIMIT),
):
    allowed = {"q", "agentId", "agentName", "agencyId", "agencyName", "advertiserPageId", "state", "suburb", "postcode", "locationRelation", "cursor", "limit"}
    _query_boundary(request, allowed)
    if locationRelation is not None and locationRelation not in _LOCATION_RELATIONS:
        raise HTTPException(400, "Invalid locationRelation filter")
    offset = _offset(cursor)
    params = {"order": "last_seen_at.desc,id.desc", "limit": str(limit + 1), "offset": str(offset)}
    q = _text(q, "q")
    if q:
        params["or"] = f"(page_name.ilike.*{q}*,headline.ilike.*{q}*,body.ilike.*{q}*)"
    if advertiserPageId:
        params["advertiser_page_id"] = f"eq.{advertiserPageId}"
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
    locationRelation: str | None = None,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=_LIMIT),
):
    allowed = {"q", "agentId", "agentName", "agencyId", "agencyName", "state", "suburb", "postcode", "locationRelation", "cursor", "limit"}
    _query_boundary(request, allowed)
    if locationRelation is not None and locationRelation not in _LOCATION_RELATIONS:
        raise HTTPException(400, "Invalid locationRelation filter")
    offset = _offset(cursor)
    params = {"order": "name.asc,prospect_type.asc,subject_id.asc,advertiser_page_id.asc.nullslast", "limit": str(limit + 1), "offset": str(offset)}
    logic: list[str] = []
    q = _text(q, "q")
    if q:
        logic.append(f"or(name.ilike.*{q}*,page_name.ilike.*{q}*)")
    if agentId:
        params["prospect_type"] = "eq.agent"
        params["subject_id"] = f"eq.{agentId}"
    if agentName:
        params["prospect_type"] = "eq.agent"
        params["name"] = f"ilike.*{_text(agentName, 'agentName')}*"
    if agencyId:
        params["prospect_type"] = "eq.agency"
        params["subject_id"] = f"eq.{agencyId}"
    if agencyName:
        params["prospect_type"] = "eq.agency"
        params["name"] = f"ilike.*{_text(agencyName, 'agencyName')}*"
    if logic:
        params["and"] = "(" + ",".join(logic) + ")"

    # Location evidence belongs to observed ads, not to a guessed prospect
    # column. Resolve matching advertiser page IDs through the canonical ads
    # projection, then filter the real prospect view by its page-association column.
    location = _location_filter(state, suburb, postcode, locationRelation)
    if location:
        matching_ads = await _get(
            "v_ad_db_ads",
            "advertiser_page_id,locations",
            {"locations": location, "limit": "1001"},
        )
        if len(matching_ads) > 1000:
            raise HTTPException(400, "Location filter is too broad")
        matching_pages = sorted({str(row.get("advertiser_page_id")) for row in matching_ads if row.get("advertiser_page_id")})
        if not matching_pages:
            return _page([], limit, offset)
        params["advertiser_page_id"] = "in.(" + ",".join(matching_pages) + ")"
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


def _scan_limits(settings: dict[str, Any]) -> tuple[float, int] | None:
    try:
        max_credits = float(settings.get("max_credits_per_page", 25))
        max_pages = int(settings.get("max_pages_per_request", 50))
    except (TypeError, ValueError):
        return None
    if not (0 < max_credits <= 25 and 0 < max_pages <= 50):
        return None
    return max_credits, max_pages


async def _scan_readiness() -> dict[str, Any]:
    settings = _settings()
    executor = str(settings.get("scan_executor") or settings.get("executor") or "").strip()
    provider_name = str(settings.get("scan_provider") or "scrapingbee").strip().lower()
    limits = _scan_limits(settings)
    max_credits, max_pages = limits or (25.0, 50)
    base = {
        "queue": "research.work_queue",
        "execution": "blockwise-ad-collector",
        "provider": provider_name,
        "maxCreditsPerPage": max_credits,
        "maxPagesPerRequest": max_pages,
        # The research ledger is a local reservation/cap, not provider
        # allowance. The durable collector remains the sole owner of it.
        "localBudgetStatus": "executor_enforced",
    }
    if limits is None:
        return {
            **base,
            "status": "blocked",
            "executorStatus": "blocked",
            "providerStatus": "blocked",
            "quotaStatus": "unknown",
            "reason": "scan_limits_invalid",
        }
    if executor != "blockwise-ad-collector":
        return {
            **base,
            "status": "blocked",
            "executorStatus": "blocked",
            "providerStatus": "blocked",
            "quotaStatus": "unknown",
            "reason": "executor_not_configured",
        }
    if provider_name != "scrapingbee":
        return {
            **base,
            "status": "blocked",
            "executorStatus": "ready",
            "providerStatus": "blocked",
            "quotaStatus": "unknown",
            "reason": "unsupported_scan_provider",
        }

    configured = bool((os.environ.get("SCRAPINGBEE_API_KEY") or os.environ.get("HERMES_SCRAPINGBEE_API_KEY") or "").strip())
    if not configured:
        return {
            **base,
            "status": "blocked",
            "executorStatus": "ready",
            "providerStatus": "blocked",
            "quotaStatus": "unknown",
            "reason": "scrapingbee_api_key_missing",
        }

    # Provider allowance is deliberately unknown here. The existing durable
    # collector performs the authenticated ScrapingBee /usage probe and gates
    # reservation immediately before any paid request. Do not mirror that
    # provider accounting or mistake provider_credit_budgets for allowance.
    return {
        **base,
        "status": "ready",
        "executorStatus": "ready",
        "providerStatus": "configured",
        "quotaStatus": "unknown",
        "reason": "provider_usage_verified_by_executor",
    }


@router.get("/runs/readiness")
async def scan_readiness():
    _config()
    return await _scan_readiness()


@router.post("/runs/scan", status_code=202)
async def scan(body: BoundedScanRequest | None = None):
    if body is None:
        # Keep the failure explicit: a scan can never infer pages or drain the
        # existing queue.
        raise HTTPException(503, "Ad DB scan requires explicit Page IDs")
    if len(set(body.pageIds)) != len(body.pageIds):
        raise HTTPException(422, "pageIds must be distinct")
    if not _IDEMPOTENCY.fullmatch(body.idempotencyKey):
        raise HTTPException(422, "idempotencyKey has an invalid format")
    readiness = await _scan_readiness()
    if readiness["status"] != "ready":
        raise HTTPException(424, readiness)
    configured_max_pages = int(readiness["maxPagesPerRequest"])
    configured_max_credits = float(readiness["maxCreditsPerPage"])
    if len(body.pageIds) > configured_max_pages:
        raise HTTPException(422, f"At most {configured_max_pages} advertiser pages may be queued per request")
    if body.maxCredits > configured_max_credits:
        raise HTTPException(422, f"maxCredits cannot exceed the configured {configured_max_credits:g} credit cap")

    page_ids = [str(page_id) for page_id in body.pageIds]
    pages = await _get(
        "advertiser_pages",
        "id,page_id,page_name,scan_enabled,scan_state,status",
        {"id": "in.(" + ",".join(page_ids) + ")", "limit": str(len(page_ids))},
    )
    by_id = {str(row.get("id")): row for row in pages}
    missing = [page_id for page_id in page_ids if page_id not in by_id]
    if missing:
        raise HTTPException(404, "One or more advertiser pages were not found")
    # Validate the complete batch before writing any queue row, so a mixed
    # valid/paused request cannot partially start a scan.
    meta_page_ids: dict[str, str] = {}
    for page_id in page_ids:
        page = by_id[page_id]
        meta_page_id = str(page.get("page_id") or "")
        if not re.fullmatch(r"[0-9]+", meta_page_id):
            raise HTTPException(422, "Every advertiser page must have a numeric Meta Page ID")
        if page.get("scan_enabled") is False or page.get("scan_state") == "paused":
            raise HTTPException(409, "One or more advertiser pages are paused")
        meta_page_ids[page_id] = meta_page_id

    jobs: list[dict[str, Any]] = []
    for page_id in page_ids:
        meta_page_id = meta_page_ids[page_id]
        dedupe_key = f"ad-radar:{body.idempotencyKey}:{page_id}"
        existing = await _get(
            "work_queue",
            "id,status,advertiser_page_id,payload,updated_at",
            {"queue_name": "eq.research", "dedupe_key": "eq." + dedupe_key, "limit": "1"},
        )
        if existing:
            jobs.append({**existing[0], "idempotent": True})
            continue
        payload = {
            "advertiserPageId": page_id,
            "metaPageId": meta_page_id,
            "scanMode": "manual",
            "idempotencyKey": body.idempotencyKey,
            "maxCredits": body.maxCredits,
            "runCreditCap": body.maxCredits,
            "resultsLimit": 250,
        }
        try:
            created = await _post(
                "work_queue",
                {
                    "queue_name": "research",
                    "job_type": "blockwise-ad-collector",
                    "dedupe_key": dedupe_key,
                    "advertiser_page_id": page_id,
                    "priority": 4,
                    "payload": payload,
                    "status": "pending",
                    "max_attempts": 3,
                },
            )
        except HTTPException as error:
            # The partial unique index is the final race-safe idempotency
            # boundary. Re-read the winner rather than creating a second job.
            if error.status_code != 409:
                raise
            winner = await _get(
                "work_queue",
                "id,status,advertiser_page_id,payload,updated_at",
                {"queue_name": "eq.research", "dedupe_key": "eq." + dedupe_key, "limit": "1"},
            )
            if not winner:
                raise
            jobs.append({**winner[0], "idempotent": True})
            continue
        if not created:
            raise HTTPException(502, "Ad DB queue returned no job")
        jobs.append({**created[0], "idempotent": False})
    return {
        "status": "accepted",
        "runId": body.idempotencyKey,
        "queue": "research.work_queue",
        "execution": "blockwise-ad-collector",
        "maxCreditsPerPage": body.maxCredits,
        "jobs": jobs,
        "readiness": {"provider": readiness["providerStatus"], "quota": readiness["quotaStatus"]},
    }
