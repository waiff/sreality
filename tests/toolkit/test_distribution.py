"""Pure-function tests for analyze_distribution."""

from __future__ import annotations

from toolkit.distribution import analyze_distribution


def _listing(sid: int, **fields):
    return {"sreality_id": sid, **fields}


def test_empty_input():
    res = analyze_distribution([], field="price_per_m2")
    assert res["data"]["n"] == 0
    assert res["data"]["min"] is None
    assert res["metadata"]["result_count"] == 0


def test_single_element():
    res = analyze_distribution(
        [_listing(1, price_per_m2=100.0)], field="price_per_m2"
    )
    d = res["data"]
    assert d["n"] == 1
    assert d["min"] == d["max"] == d["mean"] == d["median"] == 100.0
    assert d["p25"] is None and d["stddev"] is None


def test_below_five_returns_partial_stats():
    res = analyze_distribution(
        [_listing(i, price_per_m2=v) for i, v in enumerate([10.0, 20.0, 30.0, 40.0])],
        field="price_per_m2",
    )
    d = res["data"]
    assert d["n"] == 4
    assert d["min"] == 10.0 and d["max"] == 40.0
    assert d["mean"] == 25.0 and d["median"] == 25.0
    assert d["p10"] is None


def test_all_equal_values():
    listings = [_listing(i, price_per_m2=500.0) for i in range(10)]
    res = analyze_distribution(listings, field="price_per_m2")
    d = res["data"]
    assert d["n"] == 10
    assert d["min"] == d["max"] == d["mean"] == d["median"] == 500.0
    assert d["iqr"] == 0.0
    assert d["stddev"] == 0.0


def test_known_percentiles_for_arange():
    listings = [_listing(i, price_per_m2=float(i)) for i in range(101)]
    d = analyze_distribution(listings, field="price_per_m2")["data"]
    assert d["n"] == 101
    assert d["min"] == 0.0 and d["max"] == 100.0
    assert d["median"] == 50.0
    assert d["p25"] == 25.0 and d["p75"] == 75.0
    assert d["p10"] == 10.0 and d["p90"] == 90.0
    assert d["iqr"] == 50.0


def test_outliers_flagged_by_iqr():
    listings = [_listing(i, price_per_m2=float(50 + i)) for i in range(20)]
    listings.append(_listing(999, price_per_m2=10000.0))
    res = analyze_distribution(listings, field="price_per_m2")
    assert 999 in res["data"]["outlier_ids"]
    inner = [
        sid for sid in res["data"]["outlier_ids"] if sid != 999
    ]
    assert inner == []


def test_field_selection_price_czk():
    listings = [
        _listing(i, price_czk=10000 + 100 * i, price_per_m2=200.0)
        for i in range(10)
    ]
    res = analyze_distribution(listings, field="price_czk")
    assert res["data"]["field"] == "price_czk"
    assert res["data"]["min"] == 10000
    assert res["data"]["max"] == 10900


def test_field_selection_area_m2():
    listings = [_listing(i, area_m2=float(40 + i)) for i in range(10)]
    res = analyze_distribution(listings, field="area_m2")
    assert res["data"]["field"] == "area_m2"
    assert res["data"]["min"] == 40.0
    assert res["data"]["max"] == 49.0


def test_metadata_envelope_shape():
    listings = [_listing(i, price_per_m2=100.0) for i in range(3)]
    res = analyze_distribution(listings)
    md = res["metadata"]
    assert md["tool"] == "analyze_distribution"
    assert md["filters_used"] == {"field": "price_per_m2"}
    assert md["result_count"] == 3
    assert "queried_at" in md
    assert "data_freshness" in md


def test_data_freshness_uses_max_last_seen():
    listings = [
        _listing(1, price_per_m2=100.0, last_seen_at="2026-05-01T10:00:00+00:00"),
        _listing(2, price_per_m2=110.0, last_seen_at="2026-05-02T12:00:00+00:00"),
        _listing(3, price_per_m2=120.0, last_seen_at="2026-04-30T08:00:00+00:00"),
    ]
    res = analyze_distribution(listings)
    assert res["metadata"]["data_freshness"].startswith("2026-05-02T12:00:00")


def test_field_missing_on_some_listings_drops_them():
    listings = [
        _listing(1, price_per_m2=100.0),
        _listing(2),
        _listing(3, price_per_m2=110.0),
    ]
    res = analyze_distribution(listings)
    assert res["data"]["n"] == 2
    assert res["metadata"]["result_count"] == 2
