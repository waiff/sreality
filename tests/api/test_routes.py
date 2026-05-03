"""API route tests using FastAPI's TestClient.

Hermetic — overrides the DB-conn and SrealityClient dependencies, and
patches the underlying toolkit functions so no real DB or HTTP is hit.
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
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


def test_health_endpoint(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_find_comparables_passes_target_and_filters(client, monkeypatch):
    captured = {}
    def fake(conn, target, filters):
        captured["target"] = target
        captured["filters"] = filters
        return {"data": {"listings": []}, "metadata": {"tool": "find_comparables"}}
    monkeypatch.setattr(api_main, "find_comparables", fake)

    res = client.post(
        "/tools/find_comparables",
        json={
            "target": {"lat": 50.087, "lng": 14.42, "disposition": "2+kk"},
            "radius_m": 1500,
            "max_age_days": 14,
            "has_lift": True,
        },
    )
    assert res.status_code == 200
    assert captured["target"].lat == 50.087
    assert captured["target"].disposition == "2+kk"
    assert captured["filters"].radius_m == 1500
    assert captured["filters"].max_age_days == 14
    assert captured["filters"].has_lift is True


def test_analyze_distribution_passes_listings(client, monkeypatch):
    captured = {}
    def fake(listings, field):
        captured["listings"] = listings
        captured["field"] = field
        return {"data": {"n": len(listings)}, "metadata": {"tool": "analyze_distribution"}}
    monkeypatch.setattr(api_main, "analyze_distribution", fake)

    res = client.post(
        "/tools/analyze_distribution",
        json={
            "listings": [
                {"sreality_id": 1, "price_per_m2": 400.0},
                {"sreality_id": 2, "price_per_m2": 420.0},
            ],
            "field": "price_per_m2",
        },
    )
    assert res.status_code == 200
    assert captured["field"] == "price_per_m2"
    assert len(captured["listings"]) == 2


def test_verify_listing_freshness_passes_args(client, monkeypatch):
    captured = {}
    def fake(conn, c, sid, max_age):
        captured["sid"] = sid
        captured["max_age"] = max_age
        return {
            "data": {"sreality_id": sid, "outcome": "cached"},
            "metadata": {"tool": "verify_listing_freshness"},
        }
    monkeypatch.setattr(api_main, "verify_listing_freshness", fake)

    res = client.post(
        "/tools/verify_listing_freshness",
        json={"sreality_id": 12345, "max_age_hours": 6},
    )
    assert res.status_code == 200
    assert captured["sid"] == 12345
    assert captured["max_age"] == 6


def test_compare_snapshots_converts_since_days_to_timedelta(client, monkeypatch):
    captured = {}
    def fake(conn, sid, since):
        captured["sid"] = sid
        captured["since"] = since
        return {"data": {"snapshot_count": 0}, "metadata": {"tool": "compare_snapshots"}}
    monkeypatch.setattr(api_main, "compare_snapshots", fake)

    res = client.post(
        "/tools/compare_snapshots",
        json={"sreality_id": 7, "since_days": 14},
    )
    assert res.status_code == 200
    assert captured["sid"] == 7
    assert captured["since"].days == 14


def test_compare_snapshots_none_since_passes_none(client, monkeypatch):
    captured = {}
    def fake(conn, sid, since):
        captured["since"] = since
        return {"data": {"snapshot_count": 0}, "metadata": {"tool": "compare_snapshots"}}
    monkeypatch.setattr(api_main, "compare_snapshots", fake)

    res = client.post(
        "/tools/compare_snapshots",
        json={"sreality_id": 7},
    )
    assert res.status_code == 200
    assert captured["since"] is None


def test_invalid_body_returns_422(client):
    res = client.post(
        "/tools/find_comparables",
        json={"target": {"lat": "not a number", "lng": 14.0}},
    )
    assert res.status_code == 422


def test_unsupported_disposition_match_rejected(client):
    res = client.post(
        "/tools/find_comparables",
        json={
            "target": {"lat": 50.0, "lng": 14.0},
            "disposition_match": "wibble",
        },
    )
    assert res.status_code == 422
