"""Tests for the API_TOKEN bearer-token gate.

When API_TOKEN is unset, every endpoint works without auth (local dev).
When API_TOKEN is set, every endpoint EXCEPT /health requires the
matching Authorization: Bearer <token> header.
"""

from __future__ import annotations

from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import main as api_main
from api import maps as api_maps
from scraper import url_parser as scraper_url_parser


@pytest.fixture()
def client(monkeypatch):
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: object()
    api_main.app.dependency_overrides[deps.get_sreality_client] = lambda: object()

    def fake_find(conn, target, filters):
        return {"data": {"listings": []}, "metadata": {"tool": "find_comparables"}}

    def fake_dist(listings, field):
        return {"data": {"n": 0}, "metadata": {"tool": "analyze_distribution"}}

    def fake_verify(conn, c, sid, max_age):
        return {"data": {"sreality_id": sid}, "metadata": {"tool": "verify_listing_freshness"}}

    def fake_compare(conn, sid, since):
        return {"data": {"snapshot_count": 0}, "metadata": {"tool": "compare_snapshots"}}

    def fake_estimate(conn, target, filters, purchase_price_czk=None):
        return {"data": {"sample_size": 0}, "metadata": {"tool": "estimate_yield"}}

    def fake_neighborhood(conn, **_kw):
        return {"data": {}, "metadata": {"tool": "describe_neighborhood"}}

    def fake_outliers(conn, listings, **_kw):
        return {"data": {"outliers": []}, "metadata": {"tool": "find_distribution_outliers"}}

    def fake_market_vel(conn, target, filters, population, trend_split_days):
        return {"data": {"cohort_size": 0}, "metadata": {"tool": "compute_market_velocity"}}

    def fake_listing_vel(conn, sreality_id, **_kw):
        return {"data": {"sreality_id": sreality_id, "found": False},
                "metadata": {"tool": "compute_listing_velocity"}}

    def fake_anchors(conn, **_kw):
        return {"data": {"categories": {}, "from_cache": {}},
                "metadata": {"tool": "find_anchor_amenities"}}

    def fake_create_run(conn, c, body):
        return {"id": 1, "status": "success"}

    def fake_get_run(conn, run_id):
        return {"id": run_id, "status": "success"}

    def fake_list_runs(conn, **_kw):
        return {"data": [], "total": 0, "limit": 50, "offset": 0}

    def fake_parse_url(url, *, client, conn):
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
    monkeypatch.setattr(scraper_url_parser, "parse_sreality_url", fake_parse_url)

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


_FIND_BODY = {"target": {"lat": 50.0, "lng": 14.0}}
_DIST_BODY = {"listings": [], "field": "price_per_m2"}
_VERIFY_BODY = {"sreality_id": 1, "max_age_hours": 24}
_COMPARE_BODY = {"sreality_id": 1}
_ESTIMATE_BODY = {"target": {"lat": 50.0, "lng": 14.0, "area_m2": 50.0}}
_NEIGHBORHOOD_BODY = {"lat": 50.0, "lng": 14.0}
_OUTLIERS_BODY = {"listings": []}
_MARKET_VEL_BODY = {"target": {"lat": 50.0, "lng": 14.0}}
_LISTING_VEL_BODY = {"sreality_id": 1}
_ANCHORS_BODY = {"lat": 50.0, "lng": 14.0}
_CREATE_ESTIMATION_BODY = {"spec": {"lat": 50.0, "lng": 14.0, "area_m2": 50.0}}
_RESOLVE_BODY = {"label": "x", "lat": 50.0, "lng": 14.0}


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
        ("POST", "/estimations", _CREATE_ESTIMATION_BODY),
        ("GET", "/estimations/1", None),
        ("GET", "/estimations", None),
        ("GET", "/maps/suggest?query=foo", None),
        ("POST", "/maps/resolve", _RESOLVE_BODY),
        ("GET", "/estimations/preview?url=https://www.sreality.cz/detail/x/2836292428", None),
    ]


def _call(client, method: str, path: str, body, headers=None):
    if method == "POST":
        return client.post(path, json=body, headers=headers or {})
    return client.get(path, headers=headers or {})


def test_token_unset_all_endpoints_open(client, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)

    res = client.get("/health")
    assert res.status_code == 200

    for method, path, body in _gated_calls(client):
        res = _call(client, method, path, body)
        assert res.status_code == 200, f"{path} should be open without token"


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
    assert res.json() == {"status": "ok"}


def test_malformed_authorization_header_rejected(client, monkeypatch):
    """Plain token (no `Bearer` prefix) is treated as missing."""
    monkeypatch.setenv("API_TOKEN", "secret-token-xyz")
    headers = {"Authorization": "secret-token-xyz"}

    res = client.post("/tools/find_comparables", json=_FIND_BODY, headers=headers)
    assert res.status_code == 401
