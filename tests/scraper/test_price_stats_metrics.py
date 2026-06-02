"""Hermetic tests for scraper.price_stats_metrics (CAGR + gross yield)."""

from __future__ import annotations

import pytest

from scraper.price_stats_metrics import (
    cagr_pct,
    compute_city_metrics,
    gross_yield_pct,
)


def _series(points):
    return [{"year": y, "month": m, "price": p, "active_count": a}
            for (y, m, p, a) in points]


def test_cagr_over_five_years():
    series = _series([(2020, 1, 40000, 10), (2025, 1, 60000, 8)])
    out = cagr_pct(series, window_years=5)
    # (60000/40000)^(1/5) - 1 = 8.447%
    assert out["cagr_pct"] == pytest.approx(8.45, abs=0.01)
    assert out["latest_price"] == 60000
    assert out["latest_ym"] == "2025-01"
    assert out["min_active"] == 8


def test_cagr_window_clips_to_recent_years():
    # 12 yearly points; a 5y window should anchor at 2020, not 2013.
    pts = [(y, 1, 1000 * (y - 2012), 5) for y in range(2013, 2025)]
    out = cagr_pct(_series(pts), window_years=5)
    # window start 2019 (idx end-60); end 2024.
    assert out["months"] == 6  # 2019..2024 inclusive


def test_cagr_short_history_is_none():
    series = _series([(2025, 1, 40000, 3), (2025, 6, 42000, 3)])
    assert cagr_pct(series, window_years=5)["cagr_pct"] is None  # < 1 year


def test_cagr_empty_series():
    out = cagr_pct([], window_years=5)
    assert out["cagr_pct"] is None and out["latest_price"] is None and out["months"] == 0


def test_cagr_ignores_null_and_zero_prices():
    series = _series([(2020, 1, 0, 1), (2021, 1, None, 1), (2024, 1, 50000, 4)])
    out = cagr_pct(series, window_years=5)
    assert out["latest_price"] == 50000 and out["months"] == 1  # only the real point


def test_gross_yield_cancels_units():
    # sale 80,000 Kč/m², rent 320 Kč/m²/month → 12*320/80000*100 = 4.8%
    assert gross_yield_pct(80000, 320) == pytest.approx(4.8)


def test_gross_yield_none_when_missing():
    assert gross_yield_pct(None, 320) is None
    assert gross_yield_pct(80000, None) is None
    assert gross_yield_pct(0, 320) is None


def test_compute_city_metrics_combines_both_axes():
    sale = _series([(2020, 1, 80000, 5), (2025, 1, 100000, 6)])
    rent = _series([(2020, 1, 250, 5), (2025, 1, 350, 6)])
    m = compute_city_metrics(sale, rent, window_years=5)
    assert m["sale_latest_price"] == 100000
    assert m["rent_latest_price"] == 350
    assert m["gross_yield_pct"] == pytest.approx(12 * 350 / 100000 * 100, abs=0.01)
    assert m["sale_cagr_pct"] is not None and m["rent_cagr_pct"] is not None
