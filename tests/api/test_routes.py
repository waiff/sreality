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
    api_main.app.dependency_overrides[deps.get_llm_client] = lambda: object()
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
            "category_main": "byt",
            "category_type": "pronajem",
        },
    )
    assert res.status_code == 200
    assert captured["target"].lat == 50.087
    assert captured["target"].disposition == "2+kk"
    assert captured["filters"].radius_m == 1500
    assert captured["filters"].max_age_days == 14
    assert captured["filters"].has_lift is True
    assert captured["filters"].category_main == "byt"
    assert captured["filters"].category_type == "pronajem"


def test_find_comparables_requires_category(client):
    """Category is required now — omitting it must 422, not silently
    default to apartments-for-rent."""
    res = client.post(
        "/tools/find_comparables",
        json={"target": {"lat": 50.0, "lng": 14.0}},
    )
    assert res.status_code == 422
    missing = {
        tuple(e["loc"]) for e in res.json()["detail"] if e["type"] == "missing"
    }
    assert ("body", "category_main") in missing
    assert ("body", "category_type") in missing


def test_find_comparables_non_byt_category_flows_through(client, monkeypatch):
    """A house-for-sale request must carry dum/prodej into the filters,
    not get overwritten by an apartment default."""
    captured = {}
    def fake(conn, target, filters):
        captured["filters"] = filters
        return {"data": {"listings": []}, "metadata": {"tool": "find_comparables"}}
    monkeypatch.setattr(api_main, "find_comparables", fake)

    res = client.post(
        "/tools/find_comparables",
        json={
            "target": {"lat": 49.2, "lng": 16.6},
            "category_main": "dum",
            "category_type": "prodej",
        },
    )
    assert res.status_code == 200
    assert captured["filters"].category_main == "dum"
    assert captured["filters"].category_type == "prodej"


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


def test_describe_neighborhood_passes_args(client, monkeypatch):
    captured = {}
    def fake(conn, *, lat, lng, radius_m, max_age_days, category_main, category_type):
        captured["lat"] = lat
        captured["lng"] = lng
        captured["radius_m"] = radius_m
        captured["max_age_days"] = max_age_days
        captured["category_main"] = category_main
        captured["category_type"] = category_type
        return {
            "data": {"active_listing_count": 0},
            "metadata": {"tool": "describe_neighborhood"},
        }
    monkeypatch.setattr(api_main, "describe_neighborhood", fake)

    res = client.post(
        "/tools/describe_neighborhood",
        json={
            "lat": 50.087, "lng": 14.42,
            "radius_m": 1500, "max_age_days": 14,
            "category_main": "byt", "category_type": "pronajem",
        },
    )
    assert res.status_code == 200
    assert captured["lat"] == 50.087
    assert captured["radius_m"] == 1500
    assert captured["max_age_days"] == 14


def test_describe_neighborhood_uses_defaults(client, monkeypatch):
    captured = {}
    def fake(conn, *, lat, lng, radius_m, max_age_days, category_main, category_type):
        captured["radius_m"] = radius_m
        captured["max_age_days"] = max_age_days
        captured["category_main"] = category_main
        captured["category_type"] = category_type
        return {"data": {}, "metadata": {"tool": "describe_neighborhood"}}
    monkeypatch.setattr(api_main, "describe_neighborhood", fake)

    res = client.post(
        "/tools/describe_neighborhood",
        json={
            "lat": 50.087, "lng": 14.42,
            "category_main": "byt", "category_type": "pronajem",
        },
    )
    assert res.status_code == 200
    assert captured["radius_m"] == 1000
    # Freshness gate is no longer implicit; callers pass max_age_days
    # explicitly when they want one.
    assert captured["max_age_days"] is None
    assert captured["category_main"] == "byt"
    assert captured["category_type"] == "pronajem"


def test_find_distribution_outliers_passes_args(client, monkeypatch):
    captured = {}
    def fake(conn, listings, *, field, iqr_multiplier, investigate_history):
        captured["listings"] = listings
        captured["field"] = field
        captured["iqr_multiplier"] = iqr_multiplier
        captured["investigate_history"] = investigate_history
        return {
            "data": {"outliers": []},
            "metadata": {"tool": "find_distribution_outliers"},
        }
    monkeypatch.setattr(api_main, "find_distribution_outliers", fake)

    res = client.post(
        "/tools/find_distribution_outliers",
        json={
            "listings": [
                {"sreality_id": 1, "price_per_m2": 400.0},
                {"sreality_id": 2, "price_per_m2": 420.0},
            ],
            "field": "price_per_m2",
            "iqr_multiplier": 2.0,
            "investigate_history": False,
        },
    )
    assert res.status_code == 200
    assert len(captured["listings"]) == 2
    assert captured["field"] == "price_per_m2"
    assert captured["iqr_multiplier"] == 2.0
    assert captured["investigate_history"] is False


def test_find_distribution_outliers_uses_defaults(client, monkeypatch):
    captured = {}
    def fake(conn, listings, *, field, iqr_multiplier, investigate_history):
        captured["field"] = field
        captured["iqr_multiplier"] = iqr_multiplier
        captured["investigate_history"] = investigate_history
        return {"data": {"outliers": []}, "metadata": {"tool": "find_distribution_outliers"}}
    monkeypatch.setattr(api_main, "find_distribution_outliers", fake)

    res = client.post(
        "/tools/find_distribution_outliers",
        json={"listings": []},
    )
    assert res.status_code == 200
    assert captured["field"] == "price_per_m2"
    assert captured["iqr_multiplier"] == 1.5
    assert captured["investigate_history"] is True


def test_compute_market_velocity_passes_target_and_filters(client, monkeypatch):
    captured = {}
    def fake(conn, target, filters, population, trend_split_days):
        captured["target"] = target
        captured["filters"] = filters
        captured["population"] = population
        captured["trend_split_days"] = trend_split_days
        return {"data": {"cohort_size": 0}, "metadata": {"tool": "compute_market_velocity"}}
    monkeypatch.setattr(api_main, "compute_market_velocity", fake)

    res = client.post(
        "/tools/compute_market_velocity",
        json={
            "target": {"lat": 50.087, "lng": 14.42, "disposition": "2+kk"},
            "radius_m": 1500,
            "population": "active",
            "trend_split_days": 14,
            "category_main": "komercni",
            "category_type": "prodej",
        },
    )
    assert res.status_code == 200
    assert captured["target"].lat == 50.087
    assert captured["target"].disposition == "2+kk"
    assert captured["filters"].radius_m == 1500
    # Endpoint always passes active_only=False; population controls instead.
    assert captured["filters"].active_only is False
    assert captured["filters"].category_main == "komercni"
    assert captured["filters"].category_type == "prodej"
    assert captured["population"] == "active"
    assert captured["trend_split_days"] == 14


def test_compute_market_velocity_uses_defaults(client, monkeypatch):
    captured = {}
    def fake(conn, target, filters, population, trend_split_days):
        captured["population"] = population
        captured["trend_split_days"] = trend_split_days
        captured["radius_m"] = filters.radius_m
        return {"data": {}, "metadata": {"tool": "compute_market_velocity"}}
    monkeypatch.setattr(api_main, "compute_market_velocity", fake)

    res = client.post(
        "/tools/compute_market_velocity",
        json={
            "target": {"lat": 50.0, "lng": 14.0},
            "category_main": "byt", "category_type": "pronajem",
        },
    )
    assert res.status_code == 200
    assert captured["population"] == "all"
    assert captured["trend_split_days"] == 7
    assert captured["radius_m"] == 1000


def test_compute_listing_velocity_passes_args(client, monkeypatch):
    captured = {}
    def fake(conn, sreality_id, *, radius_m, disposition_match, population):
        captured["sreality_id"] = sreality_id
        captured["radius_m"] = radius_m
        captured["disposition_match"] = disposition_match
        captured["population"] = population
        return {"data": {"sreality_id": sreality_id, "found": True}, "metadata": {"tool": "compute_listing_velocity"}}
    monkeypatch.setattr(api_main, "compute_listing_velocity", fake)

    res = client.post(
        "/tools/compute_listing_velocity",
        json={
            "sreality_id": 12345,
            "radius_m": 2000,
            "disposition_match": "loose",
            "population": "delisted",
        },
    )
    assert res.status_code == 200
    assert captured["sreality_id"] == 12345
    assert captured["radius_m"] == 2000
    assert captured["disposition_match"] == "loose"
    assert captured["population"] == "delisted"


def test_compute_listing_velocity_uses_defaults(client, monkeypatch):
    captured = {}
    def fake(conn, sreality_id, *, radius_m, disposition_match, population):
        captured["radius_m"] = radius_m
        captured["disposition_match"] = disposition_match
        captured["population"] = population
        return {"data": {"sreality_id": sreality_id, "found": True}, "metadata": {"tool": "compute_listing_velocity"}}
    monkeypatch.setattr(api_main, "compute_listing_velocity", fake)

    res = client.post(
        "/tools/compute_listing_velocity",
        json={"sreality_id": 1},
    )
    assert res.status_code == 200
    assert captured["radius_m"] == 1000
    assert captured["disposition_match"] == "exact"
    assert captured["population"] == "all"


def test_find_anchor_amenities_passes_args(client, monkeypatch):
    captured: dict[str, Any] = {}

    def fake(conn, **kw):
        captured.update(kw)
        return {
            "data": {"categories": {"tram_stop": {"count": 0,
                                                  "nearest_distance_m": None,
                                                  "items": []}},
                     "from_cache": {"tram_stop": True}},
            "metadata": {"tool": "find_anchor_amenities"},
        }

    monkeypatch.setattr(api_main, "find_anchor_amenities", fake)

    res = client.post(
        "/tools/find_anchor_amenities",
        json={
            "lat": 50.075, "lng": 14.43, "radius_m": 750,
            "categories": ["tram_stop"], "cache_ttl_days": 14,
        },
    )

    assert res.status_code == 200
    assert captured["lat"] == 50.075
    assert captured["lng"] == 14.43
    assert captured["radius_m"] == 750
    assert captured["categories"] == ["tram_stop"]
    assert captured["cache_ttl_days"] == 14
    assert res.json()["metadata"]["tool"] == "find_anchor_amenities"


def test_find_anchor_amenities_defaults_categories_to_none(client, monkeypatch):
    """categories=None means all-of-them inside the toolkit; the API just passes through."""
    captured: dict[str, Any] = {}

    def fake(conn, **kw):
        captured.update(kw)
        return {"data": {"categories": {}, "from_cache": {}},
                "metadata": {"tool": "find_anchor_amenities"}}

    monkeypatch.setattr(api_main, "find_anchor_amenities", fake)

    res = client.post(
        "/tools/find_anchor_amenities",
        json={"lat": 50.0, "lng": 14.0},
    )
    assert res.status_code == 200
    assert captured["categories"] is None
    assert captured["radius_m"] == 1000
    assert captured["cache_ttl_days"] == 30


def test_listings_summaries_batch_returns_per_item_results(client, monkeypatch):
    """POST /listings/summaries fans out summarize_listing per item.

    Per-item failures are reported inline; one bad id never fails the whole
    request. Cache hits are honoured by the underlying tool.
    """
    from toolkit.summaries import SummarizeError

    def fake(conn, llm_client, *, sreality_id, snapshot_id=None, force_refresh=False):
        if sreality_id == 999:
            raise SummarizeError("no snapshot found for sreality_id=999")
        return {
            "data": {
                "sreality_id": sreality_id,
                "snapshot_id": snapshot_id or (sreality_id * 10),
                "summary": {
                    "headline": f"summary for {sreality_id}",
                    "key_highlights": [],
                    "concerns": [],
                    "condition_assessment": "good",
                    "target_audience": "couple",
                    "location_summary": "loc",
                    "building_summary": "bld",
                    "apartment_summary": "apt",
                },
                "model": "claude-sonnet-4-5",
                "cost_usd": 0.0042,
                "cache_hit": True,
            },
            "metadata": {"tool": "summarize_listing"},
        }

    monkeypatch.setattr(api_main, "summarize_listing", fake)

    res = client.post(
        "/listings/summaries",
        json={
            "items": [
                {"sreality_id": 111, "snapshot_id": 1110},
                {"sreality_id": 222},
                {"sreality_id": 999},
            ],
        },
    )
    assert res.status_code == 200
    data = res.json()["data"]
    assert len(data) == 3
    assert data[0]["sreality_id"] == 111
    assert data[0]["snapshot_id"] == 1110
    assert data[0]["summary"]["headline"] == "summary for 111"
    assert data[0]["error"] is None
    assert data[1]["snapshot_id"] == 2220
    assert data[2]["summary"] is None
    assert "no snapshot" in data[2]["error"]


def test_listings_summaries_batch_empty_items(client):
    res = client.post("/listings/summaries", json={"items": []})
    assert res.status_code == 200
    assert res.json() == {"data": []}


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
