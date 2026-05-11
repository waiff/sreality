"""Hermetic tests for compute_walkability and compute_amenity_supply.

Both functions delegate POI lookup to find_anchor_amenities. We mock
that delegation directly so the tests stay focused on the scoring /
classification math rather than the underlying cache mechanics
(those are already covered by test_amenities.py).
"""

from __future__ import annotations

from typing import Any

import pytest

from toolkit import walkability as walk


# Validation


def test_compute_walkability_rejects_unknown_category(monkeypatch):
    _patch_amen(monkeypatch, {})
    with pytest.raises(ValueError, match="unknown categories"):
        walk.compute_walkability(
            _DummyConn(), lat=50.0, lng=14.0, radius_m=500,  # type: ignore[arg-type]
            categories=["does_not_exist"],
        )


def test_compute_amenity_supply_rejects_unknown_category(monkeypatch):
    _patch_amen(monkeypatch, {})
    with pytest.raises(ValueError, match="unknown categories"):
        walk.compute_amenity_supply(
            _DummyConn(), lat=50.0, lng=14.0, radius_m=500,  # type: ignore[arg-type]
            categories=["does_not_exist"],
        )


# Scoring math: compute_walkability


def test_walkability_score_distance_to_nearest_only(monkeypatch):
    # tram 0m -> 100, supermarket at half radius -> 50.
    # weights default to tram_stop=2.0, supermarket=1.5.
    # weighted mean = (100*2.0 + 50*1.5) / (2.0 + 1.5) = 275/3.5 = 78.57 -> 79.
    _patch_amen(monkeypatch, {
        "tram_stop":   _cat(nearest=0.0,   count=1, name="Tram A"),
        "supermarket": _cat(nearest=500.0, count=1, name="Albert"),
    })
    res = walk.compute_walkability(
        _DummyConn(), lat=0, lng=0, radius_m=1000,  # type: ignore[arg-type]
        categories=["tram_stop", "supermarket"],
    )
    assert res["data"]["walkability_score"] == 79
    by_cat = {r["category"]: r for r in res["data"]["categories"]}
    assert by_cat["tram_stop"]["category_score"] == 100
    assert by_cat["supermarket"]["category_score"] == 50


def test_walkability_missing_category_contributes_zero(monkeypatch):
    # Tram at distance 0 -> 100, metro missing -> contributes 0
    # weights tram_stop=2.0, metro_station=2.0
    # weighted mean = (100*2.0 + 0*2.0) / 4.0 = 50.
    _patch_amen(monkeypatch, {
        "tram_stop":     _cat(nearest=0.0, count=1, name="Tram A"),
        "metro_station": _cat(nearest=None, count=0),
    })
    res = walk.compute_walkability(
        _DummyConn(), lat=0, lng=0, radius_m=1000,  # type: ignore[arg-type]
        categories=["tram_stop", "metro_station"],
    )
    assert res["data"]["walkability_score"] == 50
    assert res["data"]["missing_categories"] == ["metro_station"]
    by_cat = {r["category"]: r for r in res["data"]["categories"]}
    assert by_cat["metro_station"]["category_score"] is None
    assert by_cat["metro_station"]["nearest_distance_m"] is None
    assert by_cat["metro_station"]["nearest"] is None


def test_walkability_all_missing_returns_none(monkeypatch):
    _patch_amen(monkeypatch, {
        "tram_stop":  _cat(nearest=None, count=0),
        "metro_station": _cat(nearest=None, count=0),
    })
    res = walk.compute_walkability(
        _DummyConn(), lat=0, lng=0, radius_m=1000,  # type: ignore[arg-type]
        categories=["tram_stop", "metro_station"],
    )
    assert res["data"]["walkability_score"] is None
    assert set(res["data"]["missing_categories"]) == {"tram_stop", "metro_station"}


def test_walkability_nearest_payload_round_trip(monkeypatch):
    _patch_amen(monkeypatch, {
        "tram_stop": _cat(
            nearest=120.5, count=2, name="Anděl", source_id="node/42",
            lat=50.075, lng=14.43,
        ),
    })
    res = walk.compute_walkability(
        _DummyConn(), lat=50.0, lng=14.4, radius_m=1000,  # type: ignore[arg-type]
        categories=["tram_stop"],
    )
    nearest = res["data"]["categories"][0]["nearest"]
    assert nearest == {
        "source_id": "node/42",
        "name":      "Anděl",
        "lat":       50.075,
        "lng":       14.43,
    }


def test_walkability_custom_weights(monkeypatch):
    # supermarket at 250m -> 75, park at 250m -> 75
    # custom weights supermarket=3.0, park=1.0
    # mean = (75*3 + 75*1) / 4 = 75.
    _patch_amen(monkeypatch, {
        "supermarket": _cat(nearest=250.0, count=1),
        "park":        _cat(nearest=250.0, count=1),
    })
    res = walk.compute_walkability(
        _DummyConn(), lat=0, lng=0, radius_m=1000,  # type: ignore[arg-type]
        categories=["supermarket", "park"],
        weights={"supermarket": 3.0, "park": 1.0},
    )
    assert res["data"]["walkability_score"] == 75


def test_walkability_envelope_shape(monkeypatch):
    _patch_amen(monkeypatch, {
        "tram_stop": _cat(nearest=100.0, count=1),
    }, data_freshness="2026-05-10T12:00:00+00:00")
    res = walk.compute_walkability(
        _DummyConn(), lat=50.0, lng=14.0, radius_m=800,  # type: ignore[arg-type]
        categories=["tram_stop"],
    )
    md = res["metadata"]
    assert md["tool"] == "compute_walkability"
    assert md["result_count"] == 1
    assert md["data_freshness"] == "2026-05-10T12:00:00+00:00"
    assert md["filters_used"]["radius_m"] == 800
    assert md["filters_used"]["categories"] == ["tram_stop"]
    assert md["filters_used"]["weights"] == {"tram_stop": 2.0}


def test_walkability_defaults_use_module_categories(monkeypatch):
    _patch_amen(monkeypatch, {
        c: _cat(nearest=None, count=0) for c in walk._DEFAULT_CATEGORIES
    })
    res = walk.compute_walkability(
        _DummyConn(), lat=0, lng=0, radius_m=1000,  # type: ignore[arg-type]
    )
    assert (
        res["metadata"]["filters_used"]["categories"]
        == list(walk._DEFAULT_CATEGORIES)
    )


# Classification math: compute_amenity_supply


def test_supply_classifies_scarce_adequate_abundant(monkeypatch):
    # target_count defaults: metro=1, pharmacy=1, supermarket=1.
    _patch_amen(monkeypatch, {
        "metro_station": _cat(nearest=None, count=0),   # 0/1=0 -> scarce
        "pharmacy":      _cat(nearest=300.0, count=1),  # 1/1=1.0 -> adequate
        "supermarket":   _cat(nearest=200.0, count=3),  # 3/1=3.0 -> abundant
    })
    res = walk.compute_amenity_supply(
        _DummyConn(), lat=0, lng=0, radius_m=1000,  # type: ignore[arg-type]
        categories=["metro_station", "pharmacy", "supermarket"],
    )
    by_cat = {r["category"]: r for r in res["data"]["categories"]}
    assert by_cat["metro_station"]["adequacy"]   == "scarce"
    assert by_cat["pharmacy"]["adequacy"]        == "adequate"
    assert by_cat["supermarket"]["adequacy"]     == "abundant"
    assert by_cat["supermarket"]["supply_ratio"] == 3.0
    assert res["data"]["summary"] == {
        "scarce":   ["metro_station"],
        "adequate": ["pharmacy"],
        "abundant": ["supermarket"],
    }


def test_supply_classification_boundaries(monkeypatch):
    # Exact thresholds: 0.5 -> adequate (inclusive lower), 1.5 -> abundant.
    _patch_amen(monkeypatch, {
        "tram_stop":  _cat(nearest=100.0, count=3),  # target=3 -> 1.0 adequate
        "bus_stop":   _cat(nearest=100.0, count=1),  # target=3 -> 0.33 scarce
        "convenience": _cat(nearest=100.0, count=3), # target=2 -> 1.5 abundant
    })
    res = walk.compute_amenity_supply(
        _DummyConn(), lat=0, lng=0, radius_m=1000,  # type: ignore[arg-type]
        categories=["tram_stop", "bus_stop", "convenience"],
    )
    by_cat = {r["category"]: r["adequacy"] for r in res["data"]["categories"]}
    assert by_cat == {
        "tram_stop":  "adequate",
        "bus_stop":   "scarce",
        "convenience": "abundant",
    }


def test_supply_custom_target_counts(monkeypatch):
    _patch_amen(monkeypatch, {
        "park": _cat(nearest=400.0, count=2),  # target=5 -> 0.4 scarce
    })
    res = walk.compute_amenity_supply(
        _DummyConn(), lat=0, lng=0, radius_m=1000,  # type: ignore[arg-type]
        categories=["park"],
        target_counts={"park": 5},
    )
    row = res["data"]["categories"][0]
    assert row["target_count"] == 5
    assert row["supply_ratio"] == 0.4
    assert row["adequacy"]     == "scarce"


def test_supply_envelope_shape(monkeypatch):
    _patch_amen(monkeypatch, {
        "tram_stop": _cat(nearest=100.0, count=2),
    }, data_freshness="2026-05-10T12:00:00+00:00")
    res = walk.compute_amenity_supply(
        _DummyConn(), lat=50.0, lng=14.0, radius_m=500,  # type: ignore[arg-type]
        categories=["tram_stop"],
    )
    md = res["metadata"]
    assert md["tool"] == "compute_amenity_supply"
    assert md["result_count"] == 1
    assert md["data_freshness"] == "2026-05-10T12:00:00+00:00"
    assert md["filters_used"]["radius_m"] == 500
    assert md["filters_used"]["target_counts"] == {"tram_stop": 3}


def test_supply_zero_target_is_normalised_to_one(monkeypatch):
    # If a caller passes target_count=0, we don't divide by zero —
    # treat it as 1 (one POI is enough).
    _patch_amen(monkeypatch, {
        "tram_stop": _cat(nearest=100.0, count=2),
    })
    res = walk.compute_amenity_supply(
        _DummyConn(), lat=0, lng=0, radius_m=1000,  # type: ignore[arg-type]
        categories=["tram_stop"],
        target_counts={"tram_stop": 0},
    )
    row = res["data"]["categories"][0]
    assert row["target_count"] == 1
    assert row["supply_ratio"] == 2.0


# Helpers


class _DummyConn:
    pass


def _cat(
    *,
    nearest: float | None,
    count: int,
    name: str = "X",
    source_id: str = "node/1",
    lat: float = 50.0,
    lng: float = 14.0,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    if nearest is not None:
        items.append({
            "source_id": source_id,
            "name":      name,
            "lat":       lat,
            "lng":       lng,
            "distance_m": nearest,
            "fetched_at": None,
        })
    return {
        "count":              count,
        "nearest_distance_m": nearest,
        "items":              items,
    }


def _patch_amen(
    monkeypatch,
    by_category: dict[str, dict[str, Any]],
    *,
    data_freshness: str | None = None,
) -> None:
    def _fake(conn, lat, lng, radius_m, categories=None, cache_ttl_days=30,
              overpass_client=None):
        return {
            "data": {
                "center":     {"lat": lat, "lng": lng},
                "radius_m":   radius_m,
                "categories": by_category,
                "from_cache": {c: True for c in by_category},
            },
            "metadata": {
                "tool":           "find_anchor_amenities",
                "filters_used":   {},
                "result_count":   sum(
                    int(c.get("count") or 0) for c in by_category.values()
                ),
                "queried_at":     "2026-05-11T00:00:00+00:00",
                "data_freshness": data_freshness,
            },
        }

    monkeypatch.setattr(walk, "find_anchor_amenities", _fake)
