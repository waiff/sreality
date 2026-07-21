"""Tests for the API_TOKEN bearer-token gate.

require_token fails CLOSED (Phase 0): with API_TOKEN unset every gated endpoint
returns 503 UNLESS API_AUTH_OPTIONAL=1 is set (the explicit local-dev opt-out),
so a forgotten prod secret can never silently disable auth. With API_TOKEN set,
every endpoint EXCEPT /health requires the matching Authorization: Bearer <token>
header. (conftest sets API_AUTH_OPTIONAL=1 for the suite; the fail-closed test
below deletes it to prove the 503 path.)
"""

from __future__ import annotations

from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import curation as api_curation
from api import dependencies as deps
from api import main as api_main
from api import maps as api_maps
from api import pipeline as api_pipeline
from api import tenant_pool
from scraper import url_parser as scraper_url_parser


@pytest.fixture()
def client(monkeypatch):
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: object()
    api_main.app.dependency_overrides[deps.get_sreality_client] = lambda: object()
    api_main.app.dependency_overrides[deps.get_llm_client] = lambda: object()
    # tenant-pool routes: the connection is stubbed but the route-level
    # verify_jwt is left REAL — that's the auth gate under test.
    api_main.app.dependency_overrides[tenant_pool.tenant_conn] = lambda: object()
    monkeypatch.setattr(
        tenant_pool, "resolve_account_id", lambda conn, claims: None,
    )

    def fake_find(conn, target, filters):
        return {"data": {"listings": []}, "metadata": {"tool": "find_comparables"}}

    def fake_dist(listings, field):
        return {"data": {"n": 0}, "metadata": {"tool": "analyze_distribution"}}

    def fake_verify(conn, c, sid, max_age):
        return {"data": {"sreality_id": sid}, "metadata": {"tool": "verify_listing_freshness"}}

    def fake_compare(conn, sid, since):
        return {"data": {"snapshot_count": 0}, "metadata": {"tool": "compare_snapshots"}}

    def fake_estimate(conn, target, filters, purchase_price_czk=None, **_kw):
        return {"data": {"sample_size": 0}, "metadata": {"tool": "estimate_yield"}}

    def fake_neighborhood(conn, **_kw):
        return {"data": {}, "metadata": {"tool": "describe_neighborhood"}}

    def fake_outliers(conn, listings, **_kw):
        return {"data": {"outliers": []}, "metadata": {"tool": "find_distribution_outliers"}}

    def fake_market_vel(conn, target, filters, lifecycle, trend_split_days):
        return {"data": {"cohort_size": 0}, "metadata": {"tool": "compute_market_velocity"}}

    def fake_listing_vel(conn, sreality_id, **_kw):
        return {"data": {"sreality_id": sreality_id, "found": False},
                "metadata": {"tool": "compute_listing_velocity"}}

    def fake_anchors(conn, **_kw):
        return {"data": {"categories": {}, "from_cache": {}},
                "metadata": {"tool": "find_anchor_amenities"}}

    def fake_create_run(conn, c, llm_client, body, background_tasks=None, account_id=None):
        return {"id": 1, "status": "success"}

    def fake_get_run(conn, run_id):
        return {"id": run_id, "status": "success"}

    def fake_list_runs(conn, **_kw):
        return {"data": [], "total": 0, "limit": 50, "offset": 0}

    def fake_update_scenario(conn, run_id, **_kw):
        return {"id": run_id, "status": "success"}

    def fake_parse_url(url, *, client, conn, persist=False):
        return {
            "sreality_id": 2836292428,
            "spec": {"sreality_id": 2836292428, "lat": 50.0, "lon": 14.0},
            "images": [],
            "fetched_at": "2026-05-04T10:00:00+00:00",
            "source_url": url,
            "in_database": False,
        }

    monkeypatch.setattr(api_main, "find_comparables", fake_find)
    monkeypatch.setattr(api_main, "analyze_distribution", fake_dist)
    monkeypatch.setattr(api_main, "verify_listing_freshness", fake_verify)
    monkeypatch.setattr(api_main, "compare_snapshots", fake_compare)
    monkeypatch.setattr(api_main, "estimate_yield", fake_estimate)
    monkeypatch.setattr(api_main, "describe_neighborhood", fake_neighborhood)
    monkeypatch.setattr(api_main, "find_distribution_outliers", fake_outliers)
    monkeypatch.setattr(api_main, "compute_market_velocity", fake_market_vel)
    monkeypatch.setattr(api_main, "compute_listing_velocity", fake_listing_vel)
    monkeypatch.setattr(api_main, "find_anchor_amenities", fake_anchors)
    monkeypatch.setattr(api_main, "create_estimation_run", fake_create_run)
    monkeypatch.setattr(api_main, "get_estimation_run", fake_get_run)
    monkeypatch.setattr(api_main, "list_estimation_runs", fake_list_runs)
    monkeypatch.setattr(api_main, "update_scenario", fake_update_scenario)
    monkeypatch.setattr(scraper_url_parser, "parse_sreality_url", fake_parse_url)

    # Curation endpoints — gated coverage only; functional tests live in
    # test_curation.py. Stub each handler to a constant successful dict.
    monkeypatch.setattr(
        api_curation, "create_collection",
        lambda conn, body: {"id": 1, "name": body.name, "listing_count": 0},
    )
    monkeypatch.setattr(
        api_curation, "list_collections",
        lambda conn: {"data": [], "total": 0},
    )
    monkeypatch.setattr(
        api_curation, "get_collection",
        lambda conn, cid: {"collection": {"id": cid}, "properties": []},
    )
    monkeypatch.setattr(
        api_curation, "update_collection",
        lambda conn, cid, body: {"id": cid},
    )
    monkeypatch.setattr(
        api_curation, "delete_collection",
        lambda conn, cid: {"deleted": True},
    )
    monkeypatch.setattr(
        api_curation, "add_properties_to_collection",
        lambda conn, cid, body: {"added": len(body.property_ids), "skipped": 0},
    )
    monkeypatch.setattr(
        api_curation, "remove_property_from_collection",
        lambda conn, cid, pid: {"removed": True},
    )
    monkeypatch.setattr(
        api_curation, "list_notes",
        lambda conn, pid: {"data": []},
    )
    monkeypatch.setattr(
        api_curation, "create_note",
        lambda conn, pid, body, account_id=None: {
            "id": 1, "property_id": pid, "body": body.body,
        },
    )
    monkeypatch.setattr(
        api_curation, "list_tags",
        lambda conn: {"data": []},
    )
    monkeypatch.setattr(
        api_curation, "create_tag",
        lambda conn, body: {
            "id": 1, "name": body.name, "color": body.color, "listing_count": 0,
        },
    )
    monkeypatch.setattr(
        api_curation, "delete_tag",
        lambda conn, tid: {"deleted": True},
    )
    monkeypatch.setattr(
        api_curation, "update_tag",
        lambda conn, tid, body: {
            "id": tid, "name": body.name or "hot",
            "color": body.color or "brick", "listing_count": 0,
        },
    )
    monkeypatch.setattr(
        api_curation, "attach_tag",
        lambda conn, sid, body: {"attached": True},
    )
    monkeypatch.setattr(
        api_curation, "detach_tag",
        lambda conn, sid, tid: {"detached": True},
    )
    monkeypatch.setattr(
        api_pipeline, "list_stages",
        lambda conn, *, account_id=None: {"data": []},
    )
    monkeypatch.setattr(
        api_pipeline, "add_card",
        lambda conn, body, *, account_id=None: {
            "property_id": body.property_id, "added": True,
        },
    )
    monkeypatch.setattr(
        api_pipeline, "remove_card",
        lambda conn, pid, *, account_id=None: {"removed": True},
    )
    monkeypatch.setattr(
        api_pipeline, "move_card",
        lambda conn, pid, body, *, account_id=None: {
            "property_id": pid, "stage_id": body.stage_id,
        },
    )

    monkeypatch.setattr(api_maps, "suggest", lambda *a, **kw: {"items": []})
    monkeypatch.setattr(
        api_maps,
        "resolve",
        lambda *a, **kw: {
            "kind": "unresolved",
            "label": "x",
            "lat": None,
            "lng": None,
            "polygon": None,
            "default_radius_m": 1500,
            "raw": {},
        },
    )

    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


_CAT = {"category_main": "byt", "category_type": "pronajem"}
_FIND_BODY = {"target": {"lat": 50.0, "lng": 14.0}, **_CAT}
_DIST_BODY = {"listings": [], "field": "price_per_m2"}
_VERIFY_BODY = {"sreality_id": 1, "max_age_hours": 24}
_COMPARE_BODY = {"sreality_id": 1}
_ESTIMATE_BODY = {"target": {"lat": 50.0, "lng": 14.0, "area_m2": 50.0}, **_CAT}
_NEIGHBORHOOD_BODY = {"lat": 50.0, "lng": 14.0, **_CAT}
_OUTLIERS_BODY = {"listings": []}
_MARKET_VEL_BODY = {"target": {"lat": 50.0, "lng": 14.0}, **_CAT}
_LISTING_VEL_BODY = {"sreality_id": 1}
_ANCHORS_BODY = {"lat": 50.0, "lng": 14.0}
_CREATE_ESTIMATION_BODY = {"spec": {"lat": 50.0, "lng": 14.0, "area_m2": 50.0}}
_RESOLVE_BODY = {"label": "x", "lat": 50.0, "lng": 14.0}
_CREATE_COLLECTION_BODY = {"name": "test"}
_PATCH_COLLECTION_BODY = {"name": "renamed"}
_ADD_PROPERTIES_BODY = {"property_ids": [1]}
_CREATE_NOTE_BODY = {"body": "smoke"}
_CREATE_TAG_BODY = {"name": "hot", "color": "brick"}
_PATCH_TAG_BODY = {"name": "renamed", "color": "sage"}
_ATTACH_TAG_BODY = {"tag_id": 1}
_PIPELINE_CARD_BODY = {"property_id": 1}


def _gated_calls(client) -> list:
    return [
        ("POST", "/tools/find_comparables", _FIND_BODY),
        ("POST", "/tools/analyze_distribution", _DIST_BODY),
        ("POST", "/tools/verify_listing_freshness", _VERIFY_BODY),
        ("POST", "/tools/compare_snapshots", _COMPARE_BODY),
        ("POST", "/tools/describe_neighborhood", _NEIGHBORHOOD_BODY),
        ("POST", "/tools/find_distribution_outliers", _OUTLIERS_BODY),
        ("POST", "/tools/compute_market_velocity", _MARKET_VEL_BODY),
        ("POST", "/tools/compute_listing_velocity", _LISTING_VEL_BODY),
        ("POST", "/tools/find_anchor_amenities", _ANCHORS_BODY),
        ("POST", "/estimate_yield", _ESTIMATE_BODY),
        ("GET", "/estimations", None),
        ("GET", "/maps/suggest?query=foo", None),
        ("POST", "/maps/resolve", _RESOLVE_BODY),
        ("GET", "/estimations/preview?url=https://www.sreality.cz/detail/x/2836292428", None),
        ("POST",   "/collections", _CREATE_COLLECTION_BODY),
        ("GET",    "/collections/1", None),
        ("PATCH",  "/collections/1", _PATCH_COLLECTION_BODY),
        ("DELETE", "/collections/1", None),
        ("GET",    "/tags", None),
        ("POST",   "/tags", _CREATE_TAG_BODY),
        ("PATCH",  "/tags/1", _PATCH_TAG_BODY),
        ("DELETE", "/tags/1", None),
        ("POST",   "/properties/1/tags", _ATTACH_TAG_BODY),
        ("DELETE", "/properties/1/tags/1", None),
    ]


def _jwt_gated_calls() -> list:
    """Routes on verify_jwt / tenant_pool.tenant_conn (Phase 1): fail CLOSED
    when auth is unconfigured, unlike require_token's open-when-unset
    behavior. The Wave 1 W1-1 batch (/estimations create/read/scenario,
    /collections list + /properties writes, /properties/{id}/notes) moved
    here from _gated_calls in lockstep with api/main.py."""
    return [
        ("GET",    "/pipeline/stages", None),
        ("POST",   "/pipeline/cards", _PIPELINE_CARD_BODY),
        ("PATCH",  "/pipeline/cards/1", {"stage_id": 1}),
        ("DELETE", "/pipeline/cards/1", None),
        ("POST",   "/estimations", _CREATE_ESTIMATION_BODY),
        ("GET",    "/estimations/1", None),
        ("PATCH",  "/estimations/1/scenario", {"rent_czk": 15000}),
        ("GET",    "/collections", None),
        ("POST",   "/collections/1/properties", _ADD_PROPERTIES_BODY),
        ("DELETE", "/collections/1/properties/2", None),
        ("GET",    "/properties/1/notes", None),
        ("POST",   "/properties/1/notes", _CREATE_NOTE_BODY),
    ]


def _call(client, method: str, path: str, body, headers=None):
    if method == "POST":
        return client.post(path, json=body, headers=headers or {})
    if method == "PATCH":
        return client.patch(path, json=body, headers=headers or {})
    if method == "DELETE":
        return client.delete(path, headers=headers or {})
    return client.get(path, headers=headers or {})


def test_token_unset_with_optout_all_endpoints_open(client, monkeypatch):
    """Local-dev opt-out: API_TOKEN unset + API_AUTH_OPTIONAL=1 → open."""
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("API_AUTH_OPTIONAL", "1")

    res = client.get("/health")
    assert res.status_code == 200

    for method, path, body in _gated_calls(client):
        res = _call(client, method, path, body)
        assert res.status_code == 200, f"{path} should be open with the opt-out"


def test_token_unset_without_optout_fails_closed(client, monkeypatch):
    """No API_TOKEN and no opt-out → every gated route 503 (never fail-open).
    /health stays open (it carries no require_token gate)."""
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.delenv("API_AUTH_OPTIONAL", raising=False)

    assert client.get("/health").status_code == 200

    for method, path, body in _gated_calls(client):
        res = _call(client, method, path, body)
        assert res.status_code == 503, f"{path} must fail closed when auth is unconfigured"


def test_token_set_missing_header_rejects(client, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "secret-token-xyz")

    for method, path, body in _gated_calls(client):
        res = _call(client, method, path, body)
        assert res.status_code == 401, f"{path} should reject missing token"


def test_token_set_wrong_header_rejects(client, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "secret-token-xyz")
    headers = {"Authorization": "Bearer wrong-token"}

    for method, path, body in _gated_calls(client):
        res = _call(client, method, path, body, headers=headers)
        assert res.status_code == 401, f"{path} should reject wrong token"


def test_token_set_correct_header_passes(client, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "secret-token-xyz")
    headers = {"Authorization": "Bearer secret-token-xyz"}

    for method, path, body in _gated_calls(client):
        res = _call(client, method, path, body, headers=headers)
        assert res.status_code == 200, f"{path} should pass with correct token"


def test_health_always_open(client, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "secret-token-xyz")

    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_malformed_authorization_header_rejected(client, monkeypatch):
    """Plain token (no `Bearer` prefix) is treated as missing."""
    monkeypatch.setenv("API_TOKEN", "secret-token-xyz")
    headers = {"Authorization": "secret-token-xyz"}

    res = client.post("/tools/find_comparables", json=_FIND_BODY, headers=headers)
    assert res.status_code == 401


# --- verify_jwt-gated routes (Phase 1 tenant pool) --------------------------


def test_jwt_routes_fail_closed_without_token(client, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)

    for method, path, body in _jwt_gated_calls():
        res = _call(client, method, path, body)
        assert res.status_code == 401, f"{path} must fail closed with no bearer"


def test_jwt_routes_reject_wrong_token(client, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "secret-token-xyz")
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-hs256-secret")
    headers = {"Authorization": "Bearer wrong-token"}

    for method, path, body in _jwt_gated_calls():
        res = _call(client, method, path, body, headers=headers)
        assert res.status_code == 401, f"{path} should reject a non-legacy non-JWT token"


def test_jwt_routes_accept_legacy_token(client, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "secret-token-xyz")
    headers = {"Authorization": "Bearer secret-token-xyz"}

    for method, path, body in _jwt_gated_calls():
        res = _call(client, method, path, body, headers=headers)
        assert res.status_code == 200, f"{path} should pass on the legacy dual-auth branch"
