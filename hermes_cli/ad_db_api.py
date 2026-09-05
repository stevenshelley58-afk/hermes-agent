"""Canonical, service-gated Ad DB projection for Frank."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

router = APIRouter(prefix="/v1/ad-db")
_LIMIT = 100
_HASH_PATH = re.compile(r"^sha256/[0-9a-f]{64}$")
_ROOT = Path("/srv/hermes/ad-db/assets")
_ADS = "id,library_id,advertiser_page_id,advertiser_page_meta_id,page_name,active_status,first_seen_at,last_seen_at,last_checked_at,ad_delivery_started_at,ad_delivery_stopped_at,ad_creation_date,ad_creative_id,format,headline,body,cta,ad_type,primary_intent,classification,display_state,ownership,locations,media"
_PROSPECTS = "prospect_type,subject_id,name,state,suburb,postcode,advertiser_page_id,page_id,page_name,platform,scan_enabled,scan_state,last_scan_started_at,last_scan_completed_at,agency,observed_ad_count"
_RUNS = "id,advertiser_page_id,scan_mode,status,started_at,completed_at,coverage_complete,pagination_exhausted,stop_reason,ads_seen,media_captured"


def _config() -> tuple[str, dict[str, str]]:
    url = os.environ.get("HERMES_AD_DB_REST_URL", "").rstrip("/")
    key = os.environ.get("HERMES_AD_DB_SERVICE_KEY", "")
    if not url or not key:
        raise HTTPException(503, "Ad DB research service is not configured")
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


def _page(rows: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    return {"items": rows[:limit], "page": {"nextCursor": None, "limit": limit}}


def _ads(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for ad in rows:
        for media in ad.get("media") if isinstance(ad.get("media"), list) else []:
            if isinstance(media, dict) and media.get("id"):
                media["archiveUrl"] = f"/v1/ad-db/ads/{ad['id']}/media/{media['id']}"
            if isinstance(media, dict):
                media.pop("sourceUrl", None); media.pop("sourceURLs", None)
    return rows


@router.get("/ads")
async def ads(limit: int = Query(50, ge=1, le=_LIMIT)):
    return _page(_ads(await _get("v_ad_db_ads", _ADS, {"order": "last_seen_at.desc,id.desc", "limit": str(limit)})), limit)


@router.get("/ads/{ad_id}")
async def ad(ad_id: str):
    rows = await _get("v_ad_db_ads", _ADS, {"id": f"eq.{ad_id}", "limit": "1"})
    if not rows: raise HTTPException(404, "Ad not found")
    return _ads(rows)[0]


@router.get("/ads/{ad_id}/media/{asset_id}")
async def media(ad_id: str, asset_id: str):
    rows = await _get("v_ad_db_archived_media", "id,observed_ad_id,object_key,content_hash,byte_size,mime_type,verified_at", {"observed_ad_id": f"eq.{ad_id}", "id": f"eq.{asset_id}", "limit": "1"})
    if not rows: raise HTTPException(404, "Archived media not found")
    record = rows[0]; key = str(record.get("object_key") or "")
    if not _HASH_PATH.fullmatch(key) or record.get("content_hash") != key.split("/", 1)[1] or not record.get("verified_at"): raise HTTPException(404, "Archived media not found")
    path = (_ROOT / key).resolve()
    if not path.is_file() or _ROOT not in path.parents: raise HTTPException(404, "Archived media not found")
    if path.stat().st_size != record.get("byte_size"): raise HTTPException(404, "Archived media not found")
    return FileResponse(path, media_type=str(record.get("mime_type") or "application/octet-stream"), headers={"Cache-Control": "private, immutable", "Content-Length": str(record["byte_size"])})


@router.get("/prospects")
async def prospects(limit: int = Query(50, ge=1, le=_LIMIT)):
    return _page(await _get("v_ad_db_prospects", _PROSPECTS, {"limit": str(limit)}), limit)


@router.get("/runs")
async def runs(limit: int = Query(50, ge=1, le=_LIMIT)):
    return _page(await _get("v_ad_db_runs", _RUNS, {"order": "started_at.desc,id.desc", "limit": str(limit)}), limit)


@router.post("/runs/scan", status_code=202)
async def scan():
    raise HTTPException(503, "Ad DB collection worker is not configured")
