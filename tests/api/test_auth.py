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

    monkeypatch.setattr(api_main, "find_comparables", fake_find)
    monkeypatch.setattr(api_main, "analyze_distribution", fake_dist)
    monkeypatch.setattr(api_main, "verify_listing_freshness", fake_verify)
    monkeypatch.setattr(api_main, "compare_snapshots", fake_compare)
    monkeypatch.setattr(api_main, "estimate_yield", fake_estimate)
    monkeypatch.setattr(api_main, "describe_neighborhood", fake_neighborhood)
    monkeypatch.setattr(api_main, "find_distribution_outliers", fake_outliers)

    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


_FIND_BODY = {"target": {"lat": 50.0, "lng": 14.0}}
_DIST_BODY = {"listings": [], "field": "price_per_m2"}
_VERIFY_BODY = {"sreality_id": 1, "max_age_hours": 24}
_COMPARE_BODY = {"sreality_id": 1}
_ESTIMATE_BODY = {"target": {"lat": 50.0, "lng": 14.0, "area_m2": 50.0}}
_NEIGHBORHOOD_BODY = {"lat": 50.0, "lng": 14.0}
_OUTLIERS_BODY = {"listings": []}


def _gated_calls(client) -> list:
    return [
        ("/tools/find_comparables", _FIND_BODY),
        ("/tools/analyze_distribution", _DIST_BODY),
        ("/tools/verify_listing_freshness", _VERIFY_BODY),
        ("/tools/compare_snapshots", _COMPARE_BODY),
        ("/tools/describe_neighborhood", _NEIGHBORHOOD_BODY),
        ("/tools/find_distribution_outliers", _OUTLIERS_BODY),
        ("/estimate_yield", _ESTIMATE_BODY),
    ]


def test_token_unset_all_endpoints_open(client, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)

    res = client.get("/health")
    assert res.status_code == 200

    for path, body in _gated_calls(client):
        res = client.post(path, json=body)
        assert res.status_code == 200, f"{path} should be open without token"


def test_token_set_missing_header_rejects(client, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "secret-token-xyz")

    for path, body in _gated_calls(client):
        res = client.post(path, json=body)
        assert res.status_code == 401, f"{path} should reject missing token"


def test_token_set_wrong_header_rejects(client, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "secret-token-xyz")
    headers = {"Authorization": "Bearer wrong-token"}

    for path, body in _gated_calls(client):
        res = client.post(path, json=body, headers=headers)
        assert res.status_code == 401, f"{path} should reject wrong token"


def test_token_set_correct_header_passes(client, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "secret-token-xyz")
    headers = {"Authorization": "Bearer secret-token-xyz"}

    for path, body in _gated_calls(client):
        res = client.post(path, json=body, headers=headers)
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
