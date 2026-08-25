"""Tests for api.estimate_yield.

Hermetic — patches find_comparables and analyze_distribution to feed
synthetic cohorts into the orchestrator and asserts the synthesis
output (estimate, freshness block, confidence, warnings).
"""

from __future__ import annotations

from typing import Any

import pytest

from api import estimate_yield as ey
from toolkit import ComparableFilters, TargetSpec
from toolkit.measures import (
    RENT_MONTHLY_CZK_M2,
    SALE_CAPITAL_CZK_M2,
    MeasureBasisError,
    cohort_basis,
)


def _listing(
    sreality_id: int,
    price_per_m2: float = 400.0,
    price_czk: int = 20000,
    area_m2: float = 50.0,
    data_age_days: int = 1,
    latest_snapshot_id: int = 100,
    last_freshness_check_at: Any = None,
    price_per_m2_basis: str | None = RENT_MONTHLY_CZK_M2,
) -> dict[str, Any]:
    return {
        "sreality_id": sreality_id,
        "price_per_m2": price_per_m2,
        # find_comparables projects the measure and its label together
        # (migration 425); a fake cohort that dropped the label would let a
        # unit-blind scaling regress silently.
        "price_per_m2_basis": price_per_m2_basis,
        "price_czk": price_czk,
        "area_m2": area_m2,
        "data_age_days": data_age_days,
        "latest_snapshot_id": latest_snapshot_id,
        "latest_snapshot_at": "2026-05-02T10:00:00+00:00",
        "last_freshness_check_at": last_freshness_check_at,
    }


def _patch(monkeypatch, listings, dist_data=None, dist_md=None):
    def fake_find(conn, target, filters):
        return {
            "data": {"listings": listings},
            "metadata": {
                "tool": "find_comparables",
                "result_count": len(listings),
                "filters_used": {"target": {"lat": target.lat}},
                "data_freshness": "2026-05-02T10:00:00+00:00",
            },
        }
    monkeypatch.setattr(ey, "find_comparables", fake_find)

    def fake_dist(_listings, field):
        # Mirror the real tool: the basis is READ off the rows that back the
        # percentiles, never invented here.
        contributing = [l for l in _listings if l.get(field) is not None]
        basis = cohort_basis(contributing) if field == "price_per_m2" else None
        if dist_data is not None:
            data = {**dist_data}
            data.setdefault("basis", basis)
            return {"data": data, "metadata": dist_md or {}}
        # default: pretend stats are computed from input
        values = [l.get(field) for l in _listings if l.get(field) is not None]
        n = len(values)
        if not values:
            return {
                "data": {
                    "n": 0, "field": field, "min": None, "max": None,
                    "mean": None, "median": None,
                    "p10": None, "p25": None, "p75": None, "p90": None,
                    "stddev": None, "iqr": None, "outlier_ids": [],
                    "basis": basis,
                },
                "metadata": {},
            }
        return {
            "data": {
                "n": n, "field": field, "basis": basis,
                "min": min(values), "max": max(values),
                "mean": sum(values) / n, "median": sorted(values)[n // 2],
                "p10": values[0], "p25": sorted(values)[max(0, n // 4)],
                "p75": sorted(values)[min(n - 1, 3 * n // 4)], "p90": values[-1],
                "stddev": 0.0, "iqr": 50.0, "outlier_ids": [],
            },
            "metadata": {"tool": "analyze_distribution"},
        }
    monkeypatch.setattr(ey, "analyze_distribution", fake_dist)


def test_basic_estimate_with_area_scales_per_m2(monkeypatch):
    listings = [_listing(i, price_per_m2=400.0 + i) for i in range(20)]
    _patch(
        monkeypatch, listings,
        dist_data={
            "n": 20, "field": "price_per_m2",
            "median": 410.0, "p25": 405.0, "p75": 415.0,
            "iqr": 10.0,
            "min": 400.0, "max": 420.0,
            "mean": 410.0, "stddev": 5.0,
            "p10": 401.0, "p90": 419.0, "outlier_ids": [],
        },
    )

    res = ey.estimate_yield(
        conn=None,
        target=TargetSpec(lat=50.0, lng=14.0, area_m2=50.0),
        filters=ComparableFilters(),
        purchase_price_czk=5_000_000,
    )
    d = res["data"]
    assert d["estimated_monthly_rent_czk"] == 20500   # 410 * 50
    assert d["rent_p25_czk"] == 20250                 # 405 * 50
    assert d["rent_p75_czk"] == 20750                 # 415 * 50
    assert d["sample_size"] == 20
    # gross yield = 20500 * 12 / 5_000_000 * 100 = 4.92
    assert d["gross_yield_pct"] == 4.92


def test_high_confidence_with_tight_cluster(monkeypatch):
    listings = [_listing(i) for i in range(25)]
    _patch(
        monkeypatch, listings,
        dist_data={
            "n": 25, "field": "price_per_m2",
            "median": 400.0, "p25": 395.0, "p75": 405.0,
            "iqr": 10.0,
            "min": 380.0, "max": 420.0, "mean": 400.0,
            "stddev": 5.0, "p10": 390.0, "p90": 410.0,
            "outlier_ids": [],
        },
    )
    res = ey.estimate_yield(
        conn=None,
        target=TargetSpec(lat=50.0, lng=14.0, area_m2=50.0),
        filters=ComparableFilters(),
    )
    assert res["data"]["confidence"] == "high"


def test_small_sample_warns_and_low_confidence(monkeypatch):
    listings = [_listing(i) for i in range(3)]
    _patch(
        monkeypatch, listings,
        dist_data={
            "n": 3, "field": "price_per_m2",
            "median": 400.0, "p25": None, "p75": None,
            "iqr": None,
            "min": 380.0, "max": 420.0, "mean": 400.0,
            "stddev": None, "p10": None, "p90": None,
            "outlier_ids": [],
        },
    )
    res = ey.estimate_yield(
        conn=None,
        target=TargetSpec(lat=50.0, lng=14.0, area_m2=50.0),
        filters=ComparableFilters(),
    )
    d = res["data"]
    assert d["confidence"] == "low"
    assert any("small sample" in w for w in d["warnings"])


def test_stale_cohort_demotes_confidence(monkeypatch):
    listings = [
        _listing(i, data_age_days=20) for i in range(25)
    ]
    _patch(
        monkeypatch, listings,
        dist_data={
            "n": 25, "field": "price_per_m2",
            "median": 400.0, "p25": 395.0, "p75": 405.0,
            "iqr": 10.0, "min": 380.0, "max": 420.0,
            "mean": 400.0, "stddev": 5.0, "p10": 390.0, "p90": 410.0,
            "outlier_ids": [],
        },
    )
    res = ey.estimate_yield(
        conn=None,
        target=TargetSpec(lat=50.0, lng=14.0, area_m2=50.0),
        filters=ComparableFilters(),
    )
    d = res["data"]
    # Would have been "high"; >14 day median demotes one level + >50% stale forces low.
    assert d["confidence"] == "low"
    assert any("stale" in w for w in d["warnings"])
    assert d["data_freshness"]["stale_pct"] == 100.0
    assert d["data_freshness"]["median_data_age_days"] == 20.0


def test_oldest_above_30_days_emits_warning(monkeypatch):
    listings = [_listing(i, data_age_days=10) for i in range(10)]
    listings.append(_listing(99, data_age_days=45))
    _patch(monkeypatch, listings)

    res = ey.estimate_yield(
        conn=None,
        target=TargetSpec(lat=50.0, lng=14.0, area_m2=50.0),
        filters=ComparableFilters(),
    )
    assert any("45 days ago" in w for w in res["data"]["warnings"])


def test_comparables_used_carries_snapshot_ids(monkeypatch):
    listings = [
        _listing(
            1, latest_snapshot_id=42,
            last_freshness_check_at="2026-05-02T10:00:00+00:00",
        ),
        _listing(2, latest_snapshot_id=99),
    ]
    _patch(monkeypatch, listings)

    res = ey.estimate_yield(
        conn=None,
        target=TargetSpec(lat=50.0, lng=14.0, area_m2=50.0),
        filters=ComparableFilters(),
    )
    used = res["data"]["comparables_used"]
    assert used[0]["sreality_id"] == 1
    assert used[0]["snapshot_id"] == 42
    assert used[0]["verified_during_estimate"] is True
    assert used[1]["sreality_id"] == 2
    assert used[1]["snapshot_id"] == 99
    assert used[1]["verified_during_estimate"] is False
    # any verified_during_estimate=true triggers an informational warning
    assert any(
        "1 comparables have been verified" in w
        for w in res["data"]["warnings"]
    )


def test_no_verified_comparables_means_no_verified_warning(monkeypatch):
    listings = [_listing(i) for i in range(20)]  # all unverified
    _patch(monkeypatch, listings)
    res = ey.estimate_yield(
        conn=None,
        target=TargetSpec(lat=50.0, lng=14.0, area_m2=50.0),
        filters=ComparableFilters(),
    )
    assert all(
        "verified during this estimate" not in w
        for w in res["data"]["warnings"]
    )


def test_verified_warning_counts_correctly(monkeypatch):
    listings = [
        _listing(
            i,
            last_freshness_check_at=(
                "2026-05-02T10:00:00+00:00" if i < 3 else None
            ),
        )
        for i in range(20)
    ]
    _patch(monkeypatch, listings)
    res = ey.estimate_yield(
        conn=None,
        target=TargetSpec(lat=50.0, lng=14.0, area_m2=50.0),
        filters=ComparableFilters(),
    )
    assert any(
        "3 comparables have been verified" in w
        for w in res["data"]["warnings"]
    )


def test_no_purchase_price_means_no_yield(monkeypatch):
    listings = [_listing(i) for i in range(20)]
    _patch(
        monkeypatch, listings,
        dist_data={
            "n": 20, "field": "price_per_m2",
            "median": 400.0, "p25": 395.0, "p75": 405.0,
            "iqr": 10.0, "min": 380.0, "max": 420.0,
            "mean": 400.0, "stddev": 5.0, "p10": 390.0, "p90": 410.0,
            "outlier_ids": [],
        },
    )
    res = ey.estimate_yield(
        conn=None,
        target=TargetSpec(lat=50.0, lng=14.0, area_m2=50.0),
        filters=ComparableFilters(),
        purchase_price_czk=None,
    )
    assert res["data"]["gross_yield_pct"] is None


def test_empty_cohort_returns_nulls(monkeypatch):
    _patch(monkeypatch, [])
    res = ey.estimate_yield(
        conn=None,
        target=TargetSpec(lat=50.0, lng=14.0, area_m2=50.0),
        filters=ComparableFilters(),
    )
    d = res["data"]
    assert d["estimated_monthly_rent_czk"] is None
    assert d["sample_size"] == 0
    assert d["confidence"] == "low"
    assert d["data_freshness"]["oldest_data_age_days"] is None


def test_no_target_area_uses_price_czk_directly(monkeypatch):
    listings = [_listing(i, price_czk=20000 + 100 * i) for i in range(10)]
    _patch(
        monkeypatch, listings,
        dist_data={
            "n": 10, "field": "price_czk",
            "median": 20500.0, "p25": 20200.0, "p75": 20800.0,
            "iqr": 600.0, "min": 20000.0, "max": 20900.0,
            "mean": 20500.0, "stddev": 100.0, "p10": 20100.0,
            "p90": 20800.0, "outlier_ids": [],
        },
    )
    res = ey.estimate_yield(
        conn=None,
        target=TargetSpec(lat=50.0, lng=14.0),  # no area
        filters=ComparableFilters(),
    )
    assert res["data"]["estimated_monthly_rent_czk"] == 20500


def test_route_wired_via_app(monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    TestClient = pytest.importorskip("fastapi.testclient").TestClient

    from api import dependencies as deps
    from api import main as api_main

    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: object()

    captured: dict[str, Any] = {}
    def fake(
        conn, target, filters, purchase_price_czk=None,
        *, estimate_kind="rent", expected_monthly_rent_czk=None,
        trace_recorder=None,
    ):
        captured["target"] = target
        captured["filters"] = filters
        captured["price"] = purchase_price_czk
        captured["kind"] = estimate_kind
        captured["expected_rent"] = expected_monthly_rent_czk
        return {"data": {"sample_size": 0}, "metadata": {"tool": "estimate_yield"}}
    monkeypatch.setattr(api_main, "estimate_yield", fake)

    client = TestClient(api_main.app)
    res = client.post(
        "/estimate_yield",
        json={
            "target": {"lat": 50.087, "lng": 14.42, "area_m2": 50.0,
                       "disposition": "2+kk"},
            "purchase_price_czk": 5_000_000,
            "radius_m": 1500,
            "category_main": "byt",
        },
    )
    api_main.app.dependency_overrides.clear()
    assert res.status_code == 200
    assert captured["target"].lat == 50.087
    assert captured["price"] == 5_000_000
    assert captured["kind"] == "rent"
    # category_type follows estimate_kind (rent -> pronajem) when omitted.
    assert captured["filters"].category_main == "byt"
    assert captured["filters"].category_type == "pronajem"


def test_sale_kind_emits_sale_keys_and_reverse_yield(monkeypatch):
    listings = [
        _listing(i, price_per_m2=120_000.0,
                 price_per_m2_basis=SALE_CAPITAL_CZK_M2)
        for i in range(20)
    ]
    _patch(
        monkeypatch, listings,
        dist_data={
            "n": 20, "field": "price_per_m2",
            "median": 120_000.0, "p25": 115_000.0, "p75": 125_000.0,
            "iqr": 10_000.0,
            "min": 110_000.0, "max": 130_000.0,
            "mean": 120_000.0, "stddev": 4_000.0,
            "p10": 112_000.0, "p90": 128_000.0, "outlier_ids": [],
        },
    )
    res = ey.estimate_yield(
        conn=None,
        target=TargetSpec(lat=50.0, lng=14.0, area_m2=50.0),
        filters=ComparableFilters(),
        estimate_kind="sale",
        expected_monthly_rent_czk=25_000,
    )
    d = res["data"]
    assert d["estimate_kind"] == "sale"
    assert d["estimated_sale_price_czk"] == 6_000_000   # 120000 * 50
    assert d["sale_p25_czk"] == 5_750_000
    assert d["sale_p75_czk"] == 6_250_000
    # Rent fields are explicitly null in sale mode.
    assert d["estimated_monthly_rent_czk"] is None
    assert d["rent_p25_czk"] is None
    # Reverse yield: 25000 * 12 / 6_000_000 * 100 = 5.0
    assert d["gross_yield_pct"] == 5.0


def test_sale_kind_without_expected_rent_skips_yield(monkeypatch):
    listings = [
        _listing(i, price_per_m2=120_000.0,
                 price_per_m2_basis=SALE_CAPITAL_CZK_M2)
        for i in range(20)
    ]
    _patch(
        monkeypatch, listings,
        dist_data={
            "n": 20, "field": "price_per_m2",
            "median": 120_000.0, "p25": 115_000.0, "p75": 125_000.0,
            "iqr": 10_000.0,
            "min": 110_000.0, "max": 130_000.0,
            "mean": 120_000.0, "stddev": 4_000.0,
            "p10": 112_000.0, "p90": 128_000.0, "outlier_ids": [],
        },
    )
    res = ey.estimate_yield(
        conn=None,
        target=TargetSpec(lat=50.0, lng=14.0, area_m2=50.0),
        filters=ComparableFilters(),
        estimate_kind="sale",
    )
    assert res["data"]["gross_yield_pct"] is None
    assert res["data"]["estimated_sale_price_czk"] == 6_000_000


def test_rent_kind_default_unchanged_signature(monkeypatch):
    """Calling estimate_yield without estimate_kind keeps legacy behaviour."""
    listings = [_listing(i) for i in range(20)]
    _patch(
        monkeypatch, listings,
        dist_data={
            "n": 20, "field": "price_per_m2",
            "median": 400.0, "p25": 395.0, "p75": 405.0,
            "iqr": 10.0, "min": 380.0, "max": 420.0,
            "mean": 400.0, "stddev": 5.0, "p10": 390.0, "p90": 410.0,
            "outlier_ids": [],
        },
    )
    res = ey.estimate_yield(
        conn=None,
        target=TargetSpec(lat=50.0, lng=14.0, area_m2=50.0),
        filters=ComparableFilters(),
    )
    d = res["data"]
    assert d["estimate_kind"] == "rent"
    assert d["estimated_monthly_rent_czk"] is not None
    assert d["estimated_sale_price_czk"] is None


# --- the basis gate on _scale ---------------------------------------------
# `median × area` is the same multiplication for a monthly Kč/m² and a capital
# Kč/m²; only the basis decides what the product is. These pin that the gate
# fires rather than let the pipeline emit a confidently wrong headline number.


def test_scaling_a_mixed_cohort_raises(monkeypatch):
    """Rule 22 puts a sale+rent cohort one click away; it has no single unit."""
    listings = [
        _listing(i, price_per_m2=400.0,
                 price_per_m2_basis=(
                     RENT_MONTHLY_CZK_M2 if i % 2 else SALE_CAPITAL_CZK_M2
                 ))
        for i in range(20)
    ]
    _patch(monkeypatch, listings)
    with pytest.raises(MeasureBasisError) as exc:
        ey.estimate_yield(
            conn=None,
            target=TargetSpec(lat=50.0, lng=14.0, area_m2=50.0),
            filters=ComparableFilters(),
        )
    assert "mixed" in str(exc.value)


def test_scaling_an_unlabelled_cohort_raises(monkeypatch):
    """Absent labels degrade to 'unknown' — never to a default of sale."""
    listings = [
        _listing(i, price_per_m2=400.0, price_per_m2_basis=None)
        for i in range(20)
    ]
    _patch(monkeypatch, listings)
    with pytest.raises(MeasureBasisError):
        ey.estimate_yield(
            conn=None,
            target=TargetSpec(lat=50.0, lng=14.0, area_m2=50.0),
            filters=ComparableFilters(),
        )


def test_scaling_a_rent_cohort_into_a_sale_price_raises(monkeypatch):
    """A monthly Kč/m² times an area is a monthly rent, never a sale price."""
    listings = [
        _listing(i, price_per_m2=400.0, price_per_m2_basis=RENT_MONTHLY_CZK_M2)
        for i in range(20)
    ]
    _patch(monkeypatch, listings)
    with pytest.raises(MeasureBasisError):
        ey.estimate_yield(
            conn=None,
            target=TargetSpec(lat=50.0, lng=14.0, area_m2=50.0),
            filters=ComparableFilters(),
            estimate_kind="sale",
            expected_monthly_rent_czk=25_000,
        )


def test_price_czk_branch_is_never_basis_gated(monkeypatch):
    """No target area -> Kč percentiles, which need no per-m² basis at all.

    This branch must NOT be made to fail: an unlabelled cohort is fine here
    because nothing per-m² is being multiplied.
    """
    listings = [
        _listing(i, price_czk=20_000 + i, price_per_m2_basis=None)
        for i in range(20)
    ]
    _patch(monkeypatch, listings)
    res = ey.estimate_yield(
        conn=None,
        target=TargetSpec(lat=50.0, lng=14.0),
        filters=ComparableFilters(),
    )
    assert res["data"]["estimated_monthly_rent_czk"] is not None
    assert res["metadata"]["price_per_m2_basis"] is None
    assert res["metadata"]["price_per_m2_unit"] is None


def test_empty_cohort_is_a_gap_not_an_error(monkeypatch):
    """"No comparables" must stay "no comparables", not become a basis error."""
    _patch(monkeypatch, [])
    res = ey.estimate_yield(
        conn=None,
        target=TargetSpec(lat=50.0, lng=14.0, area_m2=50.0),
        filters=ComparableFilters(),
    )
    assert res["data"]["estimated_monthly_rent_czk"] is None
    assert res["data"]["sample_size"] == 0


def test_metadata_names_the_unit_of_the_scaled_estimate(monkeypatch):
    listings = [_listing(i, price_per_m2=400.0 + i) for i in range(20)]
    _patch(monkeypatch, listings)
    res = ey.estimate_yield(
        conn=None,
        target=TargetSpec(lat=50.0, lng=14.0, area_m2=50.0),
        filters=ComparableFilters(),
    )
    assert res["metadata"]["price_per_m2_basis"] == RENT_MONTHLY_CZK_M2
    assert res["metadata"]["price_per_m2_unit"] == "Kč/m²/měs"
