import asyncio
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

from hermes_cli import ad_db_api, web_server

AD_ID = "11111111-1111-4111-8111-111111111111"
ASSET_ID = "22222222-2222-4222-8222-222222222222"
AGENT_ID = "33333333-3333-4333-8333-333333333333"
AGENCY_ID = "44444444-4444-4444-8444-444444444444"
AUTH = {web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN}


def request(method: str, path: str, **kwargs):
    async def run():
        transport = httpx.ASGITransport(app=web_server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, path, **kwargs)
    return asyncio.run(run())


def test_media_projection_never_keeps_source_url():
    result = ad_db_api._ads([{"id": AD_ID, "media": [{"id": ASSET_ID, "sourceUrl": "https://cdn.example/a", "sourceURLs": ["https://cdn.example/a"]}]}])
    media = result[0]["media"][0]
    assert media["archiveUrl"] == f"/v1/ad-db/ads/{AD_ID}/media/{ASSET_ID}"
    assert "sourceUrl" not in media
    assert "sourceURLs" not in media


def test_missing_service_credentials_fail_closed():
    with patch.dict(os.environ, {}, clear=True), patch.object(ad_db_api.time, "time", return_value=1000):
        try:
            ad_db_api._config()
        except HTTPException as error:
            assert error.status_code == 503
        else:
            raise AssertionError("missing Ad DB credentials must fail closed")


def test_short_lived_service_jwt_is_correctly_signed():
    secret = "test-secret"
    env = {"HERMES_AD_DB_REST_URL": "http://127.0.0.1:8652/rest/v1", "HERMES_AD_DB_JWT_SECRET": secret}
    with patch.dict(os.environ, env, clear=True), patch.object(ad_db_api.time, "time", return_value=1000):
        _url, headers = ad_db_api._config()
    token = headers["apikey"]
    header, claims, signature = token.split(".")
    payload = json.loads(base64.urlsafe_b64decode(claims + "=="))
    expected = hmac.new(secret.encode(), f"{header}.{claims}".encode(), hashlib.sha256).digest()
    assert hmac.compare_digest(base64.urlsafe_b64decode(signature + "=="), expected)
    assert payload == {"exp": 1060, "iat": 1000, "role": "service_role"}
    assert headers["Authorization"] == f"Bearer {token}"


def test_all_mounted_ad_db_routes_require_existing_session_boundary():
    web_server.app.state.auth_required = False
    paths = [
        ("GET", "/v1/ad-db/ads"),
        ("GET", f"/v1/ad-db/ads/{AD_ID}"),
        ("GET", f"/v1/ad-db/ads/{AD_ID}/media/{ASSET_ID}"),
        ("HEAD", f"/v1/ad-db/ads/{AD_ID}/media/{ASSET_ID}"),
        ("GET", "/v1/ad-db/prospects"),
        ("GET", "/v1/ad-db/runs"),
        ("POST", "/v1/ad-db/runs/scan"),
    ]
    for method, path in paths:
        assert request(method, path).status_code == 401
    assert request("GET", "/v1/ad-db/ads", headers={"Authorization": "Bearer attacker.jwt"}).status_code == 401
    with patch.object(ad_db_api, "_get", new=AsyncMock(return_value=[])):
        assert request("GET", "/v1/ad-db/ads", headers=AUTH).status_code == 200
    assert request("POST", "/v1/ad-db/runs/scan", headers=AUTH).status_code == 503


def test_ads_apply_all_filters_and_return_bounded_cursor_page():
    rows = [{"id": AD_ID}, {"id": AD_ID}, {"id": AD_ID}]
    upstream = AsyncMock(return_value=rows)
    path = (
        "/v1/ad-db/ads?q=coast&agentId=" + AGENT_ID
        + "&agentName=Alex&agencyId=" + AGENCY_ID
        + "&agencyName=Harbour&state=WA&suburb=Perth&postcode=6000"
        + "&locationRelation=office&limit=2"
    )
    with patch.object(ad_db_api, "_get", new=upstream):
        response = request("GET", path, headers=AUTH)
    assert response.status_code == 200
    assert len(response.json()["items"]) == 2
    cursor = response.json()["page"]["nextCursor"]
    params = upstream.await_args.args[2]
    assert params["limit"] == "3"
    assert params["offset"] == "0"
    assert params["ownership->agent->>id"] == f"eq.{AGENT_ID}"
    assert params["ownership->agency->>id"] == f"eq.{AGENCY_ID}"
    assert json.loads(params["locations"][3:]) == [{"state": "WA", "suburb": "Perth", "postcode": "6000", "relation": "office"}]
    assert "headline.ilike.*coast*" in params["or"]

    upstream.reset_mock(return_value=True)
    upstream.return_value = []
    with patch.object(ad_db_api, "_get", new=upstream):
        next_page = request("GET", f"/v1/ad-db/ads?cursor={cursor}&limit=2", headers=AUTH)
    assert next_page.status_code == 200
    assert upstream.await_args.args[2]["offset"] == "2"


def test_route_specific_query_boundary_rejects_ignored_or_invalid_inputs():
    for path in (
        "/v1/ad-db/ads?locationType=target",
        "/v1/ad-db/ads?locationRelation=invented",
        "/v1/ad-db/ads?q=a&q=b",
        "/v1/ad-db/runs?q=ignored",
        "/v1/ad-db/prospects?locationRelation=office",
        "/v1/ad-db/ads?agentId=not-a-uuid",
        "/v1/ad-db/ads?q=bad%0Atext",
        "/v1/ad-db/ads?cursor=bad",
    ):
        assert request("GET", path, headers=AUTH).status_code in (400, 422)


def test_prospect_and_run_filters_reach_postgrest():
    upstream = AsyncMock(return_value=[])
    prospects = (
        f"/v1/ad-db/prospects?q=lane&agentId={AGENT_ID}&agentName=Alex"
        f"&agencyId={AGENCY_ID}&agencyName=Harbour&state=WA&suburb=Perth&postcode=6000"
    )
    with patch.object(ad_db_api, "_get", new=upstream):
        assert request("GET", prospects, headers=AUTH).status_code == 200
    params = upstream.await_args.args[2]
    assert params["prospect_type"] == "eq.agent"
    assert params["subject_id"] == f"eq.{AGENT_ID}"
    assert params["state"] == "eq.WA"
    assert "page_name.ilike.*lane*" in params["and"]
    assert f"agency->>id.eq.{AGENCY_ID}" in params["and"]

    upstream.reset_mock(return_value=True)
    upstream.return_value = []
    with patch.object(ad_db_api, "_get", new=upstream):
        assert request("GET", "/v1/ad-db/runs?status=paused&limit=5", headers=AUTH).status_code == 200
    assert upstream.await_args.args[2]["status"] == "eq.paused"
    assert upstream.await_args.args[2]["limit"] == "6"


def test_verified_archive_stream_supports_get_range_head_and_404(tmp_path):
    digest = hashlib.sha256(b"abcdef").hexdigest()
    archive = tmp_path / "sha256" / digest
    archive.parent.mkdir()
    archive.write_bytes(b"abcdef")
    record = {
        "id": ASSET_ID,
        "observed_ad_id": AD_ID,
        "object_key": f"sha256/{digest}",
        "content_hash": digest,
        "byte_size": 6,
        "mime_type": "video/mp4",
        "verified_at": "2026-09-05T00:00:00Z",
    }
    upstream = AsyncMock(return_value=[record])
    with patch.object(ad_db_api, "_ROOT", Path(tmp_path)), patch.object(ad_db_api, "_get", new=upstream):
        full = request("GET", f"/v1/ad-db/ads/{AD_ID}/media/{ASSET_ID}", headers=AUTH)
        ranged = request("GET", f"/v1/ad-db/ads/{AD_ID}/media/{ASSET_ID}", headers={**AUTH, "Range": "bytes=1-3"})
        head = request("HEAD", f"/v1/ad-db/ads/{AD_ID}/media/{ASSET_ID}", headers=AUTH)
    assert full.status_code == 200 and full.content == b"abcdef"
    assert ranged.status_code == 206 and ranged.content == b"bcd"
    assert ranged.headers["content-range"] == "bytes 1-3/6"
    assert ranged.headers["etag"] == f'"{digest}"'
    assert ranged.headers["cache-control"].startswith("private")
    assert head.status_code == 200 and head.content == b""
    assert head.headers["content-length"] == "6"

    archive.unlink()
    with patch.object(ad_db_api, "_ROOT", Path(tmp_path)), patch.object(ad_db_api, "_get", new=AsyncMock(return_value=[record])):
        assert request("GET", f"/v1/ad-db/ads/{AD_ID}/media/{ASSET_ID}", headers=AUTH).status_code == 404

def test_non_secret_endpoint_and_archive_root_prefer_config_yaml():
    config = {"ad_db": {"rest_url": "http://configured/rest/v1", "archive_root": "/srv/configured-assets"}}
    env = {"HERMES_AD_DB_REST_URL": "http://env/rest/v1", "HERMES_AD_DB_SERVICE_KEY": "test-key"}
    with patch("hermes_cli.config.load_config", return_value=config), patch.dict(os.environ, env, clear=True):
        url, _headers = ad_db_api._config()
        root = ad_db_api._archive_root()
    assert url == "http://configured/rest/v1"
    assert root == Path("/srv/configured-assets")
