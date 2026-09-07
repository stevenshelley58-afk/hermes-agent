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


def test_customer_projection_allowlists_nested_contact_and_provider_fields():
    row = {
        "id": AD_ID,
        "classification": "real_estate",
        "ownership": {
            "agent": {"id": AGENT_ID, "name": "Alex", "relationship": "owner", "email": "private@example.com"},
            "agency": {"id": AGENCY_ID, "name": "Harbour", "phone": "private"},
            "contact": {"email": "private@example.com"},
        },
        "locations": [{"id": "loc", "state": "WA", "suburb": "Perth", "postcode": "6000", "relation": "office", "notes": "private"}],
        "media": [{"id": ASSET_ID, "kind": "video", "mimeType": "video/mp4", "objectKey": "private", "sourceUrl": "https://cdn.example/private"}],
        "unexpected_contact": "private@example.com",
    }
    result = ad_db_api._ads([row])[0]
    assert "unexpected_contact" not in result
    assert result["ownership"]["agent"] == {"id": AGENT_ID, "name": "Alex", "relationship": "owner"}
    assert "contact" not in result["ownership"]
    assert result["locations"] == [{"id": "loc", "state": "WA", "suburb": "Perth", "postcode": "6000", "relation": "office"}]
    assert result["media"][0] == {"id": ASSET_ID, "kind": "video", "mimeType": "video/mp4", "archiveUrl": f"/v1/ad-db/ads/{AD_ID}/media/{ASSET_ID}"}
    digest = "a" * 64
    valid = ad_db_api._ads([{"id": AD_ID, "media": [{"id": ASSET_ID, "storageBucket": "ad-db", "objectKey": f"sha256/{digest}", "sha256": digest, "mimeType": "video/mp4"}]}])[0]["media"][0]
    assert valid["objectKey"] == f"sha256/{digest}" and valid["storageBucket"] == "ad-db"


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
    with patch.dict(os.environ, {"HERMES_AD_DB_JWT_SECRET": secret}, clear=True), patch.object(ad_db_api, "_settings", return_value={"rest_url": "http://127.0.0.1:8652/rest/v1"}), patch.object(ad_db_api.time, "time", return_value=1000):
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
    assert request("POST", "/v1/ad-db/runs/scan", headers=AUTH, json={"pageIds": [], "maxCredits": 51}).status_code == 422


def test_ads_apply_all_filters_and_return_bounded_cursor_page():
    rows = [{"id": AD_ID}, {"id": AD_ID}, {"id": AD_ID}]
    upstream = AsyncMock(return_value=rows)
    path = (
        "/v1/ad-db/ads?q=coast&advertiserPageId=" + AD_ID
        + "&agentId=" + AGENT_ID
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
    assert params["advertiser_page_id"] == f"eq.{AD_ID}"
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
        "/v1/ad-db/ads?agentId=not-a-uuid",
        "/v1/ad-db/ads?q=bad%0Atext",
        "/v1/ad-db/ads?cursor=bad",
    ):
        assert request("GET", path, headers=AUTH).status_code in (400, 422)


def test_prospect_and_run_filters_reach_real_projection_columns():
    upstream = AsyncMock(return_value=[])
    prospects = (
        f"/v1/ad-db/prospects?q=lane&agentId={AGENT_ID}&agentName=Alex"
        f"&agencyId={AGENCY_ID}&agencyName=Harbour&limit=5"
    )
    with patch.object(ad_db_api, "_get", new=upstream):
        assert request("GET", prospects, headers=AUTH).status_code == 200
    params = upstream.await_args.args[2]
    assert params["prospect_type"] == "eq.agency"
    assert params["subject_id"] == f"eq.{AGENCY_ID}"
    assert params["name"] == "ilike.*Harbour*"
    assert "page_name.ilike.*lane*" in params["and"]
    assert "name.ilike.*lane*" in params["and"]
    assert ad_db_api._PROSPECTS == "prospect_type,subject_id,name,state,suburb,postcode,advertiser_page_id,page_id,page_name,platform,scan_enabled,scan_state,last_scan_started_at,last_scan_completed_at,agency,observed_ad_count"

    upstream.reset_mock(return_value=True)
    upstream.return_value = []
    with patch.object(ad_db_api, "_get", new=upstream):
        assert request("GET", "/v1/ad-db/runs?status=paused&limit=5", headers=AUTH).status_code == 200
    assert upstream.await_args.args[2]["status"] == "eq.paused"
    assert upstream.await_args.args[2]["limit"] == "6"


def test_prospect_location_filter_joins_canonical_ad_projection():
    page_id = "55555555-5555-4555-8555-555555555555"
    upstream = AsyncMock(side_effect=[
        [{"advertiser_page_id": page_id, "locations": [{"relation": "office", "state": "WA", "suburb": "Perth", "postcode": "6000"}]}],
        [{"advertiser_page_id": page_id, "page_id": "101", "page_name": "One"}],
    ])
    with patch.object(ad_db_api, "_get", new=upstream):
        response = request("GET", "/v1/ad-db/prospects?state=WA&suburb=Perth&postcode=6000&locationRelation=office", headers=AUTH)
    assert response.status_code == 200
    assert upstream.await_args_list[0].args[0] == "v_ad_db_ads"
    assert upstream.await_args_list[1].args[0] == "v_ad_db_prospects"
    assert upstream.await_args_list[1].args[2]["advertiser_page_id"] == f"in.({page_id})"


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
    with patch.object(ad_db_api, "_archive_root", return_value=Path(tmp_path)), patch.object(ad_db_api, "_get", new=upstream):
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
    with patch.object(ad_db_api, "_archive_root", return_value=Path(tmp_path)), patch.object(ad_db_api, "_get", new=AsyncMock(return_value=[record])):
        assert request("GET", f"/v1/ad-db/ads/{AD_ID}/media/{ASSET_ID}", headers=AUTH).status_code == 404

def test_non_secret_endpoint_and_archive_root_prefer_config_yaml():
    config = {"ad_db": {"rest_url": "http://configured/rest/v1", "archive_root": "/srv/configured-assets"}}
    env = {"HERMES_AD_DB_REST_URL": "http://env/rest/v1", "HERMES_AD_DB_SERVICE_KEY": "test-key"}
    with patch("hermes_cli.config.load_config", return_value=config), patch.dict(os.environ, env, clear=True):
        url, _headers = ad_db_api._config()
        root = ad_db_api._archive_root()
    assert url == "http://configured/rest/v1"
    assert root == Path("/srv/configured-assets")

def test_scoped_read_token_allows_get_and_head_but_never_post_or_bearer(tmp_path):
    token_header = {"X-Hermes-Ad-Db-Read-Token": "read-token"}
    env = {"HERMES_AD_DB_READ_TOKEN": "read-token"}
    with patch.dict(os.environ, env, clear=False), patch.object(ad_db_api, "_get", new=AsyncMock(return_value=[])):
        assert request("GET", "/v1/ad-db/ads", headers=token_header).status_code == 200
        assert request("GET", "/v1/ad-db/prospects", headers=token_header).status_code == 403
        assert request("GET", "/v1/ad-db/runs", headers=token_header).status_code == 403
        assert request("GET", "/v1/ad-db/ads", headers={"X-Hermes-Ad-Db-Read-Token": "wrong"}).status_code == 401
        assert request("GET", "/v1/ad-db/ads", headers={"Authorization": "Bearer read-token"}).status_code == 401
        assert request("POST", "/v1/ad-db/runs/scan", headers=token_header).status_code == 401

    digest = hashlib.sha256(b"media").hexdigest()
    archive = tmp_path / "sha256" / digest
    archive.parent.mkdir()
    archive.write_bytes(b"media")
    record = {
        "object_key": f"sha256/{digest}",
        "content_hash": digest,
        "byte_size": 5,
        "mime_type": "video/mp4",
        "verified_at": "2026-09-05T00:00:00Z",
    }
    with (
        patch.dict(os.environ, env, clear=False),
        patch.object(ad_db_api, "_archive_root", return_value=Path(tmp_path)),
        patch.object(ad_db_api, "_get", new=AsyncMock(return_value=[record])),
    ):
        response = request(
            "HEAD",
            f"/v1/ad-db/ads/{AD_ID}/media/{ASSET_ID}",
            headers=token_header,
        )
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-length"] == "5"


def test_scan_enqueues_distinct_pages_on_existing_research_queue():
    page_one = "55555555-5555-4555-8555-555555555555"
    page_two = "66666666-6666-4666-8666-666666666666"
    pages = [
        {"id": page_one, "page_id": "101", "page_name": "One", "scan_enabled": True, "scan_state": "needs_first_fill", "status": "candidate"},
        {"id": page_two, "page_id": "202", "page_name": "Two", "scan_enabled": True, "scan_state": "healthy", "status": "resolved_collectable"},
    ]
    async def get(view, _select, params):
        if view == "provider_credit_budgets":
            return [{"max_credits": 25, "reserved_credits": 0, "spent_credits": 0}]
        if view == "advertiser_pages":
            assert params["id"] == f"in.({page_one},{page_two})"
            return pages
        assert view == "work_queue"
        return []
    created = AsyncMock(side_effect=[
        [{"id": "77777777-7777-4777-8777-777777777777", "status": "pending"}],
        [{"id": "88888888-8888-4888-8888-888888888888", "status": "pending"}],
    ])
    env = {"SCRAPINGBEE_API_KEY": "configured"}
    with patch.dict(os.environ, env, clear=False), patch.object(ad_db_api, "_settings", return_value={"scan_executor": "blockwise-ad-collector"}), patch.object(ad_db_api, "_get", side_effect=get), patch.object(ad_db_api, "_post", new=created):
        response = request("POST", "/v1/ad-db/runs/scan", headers=AUTH, json={
            "pageIds": [page_one, page_two],
            "maxCredits": 20,
            "idempotencyKey": "frank-scan-2026-01",
        })
    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "accepted"
    assert payload["maxCreditsPerPage"] == 20
    assert [job["idempotent"] for job in payload["jobs"]] == [False, False]
    assert [call.args[0] for call in created.await_args_list] == ["work_queue", "work_queue"]
    first = created.await_args_list[0].args[1]
    assert first["advertiser_page_id"] == page_one
    assert first["payload"]["metaPageId"] == "101"
    assert first["payload"]["runCreditCap"] == 20
    assert first["job_type"] == "blockwise-ad-collector"


def test_scan_is_idempotent_against_existing_queue_jobs():
    page_id = "55555555-5555-4555-8555-555555555555"
    async def get(view, _select, _params):
        if view == "provider_credit_budgets":
            return [{"max_credits": 25, "reserved_credits": 0, "spent_credits": 0}]
        if view == "advertiser_pages":
            return [{"id": page_id, "page_id": "101", "scan_enabled": True, "scan_state": "healthy", "status": "candidate"}]
        return [{"id": "77777777-7777-4777-8777-777777777777", "status": "claimed", "advertiser_page_id": page_id}]
    post = AsyncMock()
    env = {"SCRAPINGBEE_API_KEY": "configured"}
    with patch.dict(os.environ, env, clear=False), patch.object(ad_db_api, "_settings", return_value={"scan_executor": "blockwise-ad-collector"}), patch.object(ad_db_api, "_get", side_effect=get), patch.object(ad_db_api, "_post", new=post):
        response = request("POST", "/v1/ad-db/runs/scan", headers=AUTH, json={
            "pageIds": [page_id], "idempotencyKey": "frank-scan-2026-02",
        })
    assert response.status_code == 202
    assert response.json()["jobs"][0]["idempotent"] is True
    post.assert_not_awaited()


def test_scan_reports_provider_blocked_without_enqueue():
    page_id = "55555555-5555-4555-8555-555555555555"
    get = AsyncMock()
    post = AsyncMock()
    with patch.dict(os.environ, {}, clear=False), patch.object(ad_db_api, "_settings", return_value={"scan_executor": "blockwise-ad-collector"}), patch.object(ad_db_api, "_get", new=get), patch.object(ad_db_api, "_post", new=post):
        response = request("POST", "/v1/ad-db/runs/scan", headers=AUTH, json={
            "pageIds": [page_id], "idempotencyKey": "frank-scan-2026-03",
        })
    assert response.status_code == 424
    assert response.json()["detail"]["reason"] == "scrapingbee_api_key_missing"
    get.assert_not_awaited()
    post.assert_not_awaited()


def test_readiness_does_not_mislabel_local_budget_as_provider_quota():
    with patch.dict(os.environ, {"SCRAPINGBEE_API_KEY": "configured"}, clear=False), patch.object(
        ad_db_api, "_settings", return_value={"scan_executor": "blockwise-ad-collector"}
    ):
        readiness = asyncio.run(ad_db_api._scan_readiness())
    assert readiness["status"] == "ready"
    assert readiness["providerStatus"] == "configured"
    assert readiness["quotaStatus"] == "unknown"
    assert readiness["localBudgetStatus"] == "executor_enforced"
    assert readiness["reason"] == "provider_usage_verified_by_executor"


def test_scan_enforces_configured_page_and_credit_caps_before_queue_write():
    page_id = "55555555-5555-4555-8555-555555555555"
    post = AsyncMock()
    with patch.dict(os.environ, {"SCRAPINGBEE_API_KEY": "configured"}, clear=False), patch.object(
        ad_db_api,
        "_settings",
        return_value={
            "scan_executor": "blockwise-ad-collector",
            "max_credits_per_page": 10,
            "max_pages_per_request": 1,
        },
    ), patch.object(ad_db_api, "_get", new=AsyncMock()), patch.object(ad_db_api, "_post", new=post):
        too_many_pages = request("POST", "/v1/ad-db/runs/scan", headers=AUTH, json={
            "pageIds": [page_id, "66666666-6666-4666-8666-666666666666"],
            "idempotencyKey": "frank-scan-caps-pages",
        })
        too_many_credits = request("POST", "/v1/ad-db/runs/scan", headers=AUTH, json={
            "pageIds": [page_id],
            "maxCredits": 11,
            "idempotencyKey": "frank-scan-caps-credit",
        })
    assert too_many_pages.status_code == 422
    assert too_many_credits.status_code == 422
    post.assert_not_awaited()


def test_customer_token_cannot_read_scan_readiness():
    with patch.dict(os.environ, {"HERMES_AD_DB_READ_TOKEN": "read-token"}, clear=False):
        response = request("GET", "/v1/ad-db/runs/readiness", headers={"X-Hermes-Ad-Db-Read-Token": "read-token"})
    assert response.status_code == 403


def test_research_writes_select_content_profile():
    class Response:
        def raise_for_status(self):
            return None
        def json(self):
            return [{"id": "77777777-7777-4777-8777-777777777777"}]

    class Client:
        def __init__(self):
            self.kwargs = None
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            return None
        async def post(self, *_args, **kwargs):
            self.kwargs = kwargs
            return Response()

    client = Client()
    with patch.object(ad_db_api, "_config", return_value=("http://research/rest/v1", {"apikey": "dedicated", "Authorization": "Bearer dedicated", "Accept-Profile": "research"})), patch.object(ad_db_api.httpx, "AsyncClient", return_value=client):
        result = asyncio.run(ad_db_api._post("work_queue", {"job_type": "blockwise-ad-collector"}))
    assert result[0]["id"].startswith("7777")
    assert client.kwargs["headers"]["Content-Profile"] == "research"
