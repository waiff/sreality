"""Tests for the per-dataset scrape-window helpers."""

from __future__ import annotations

from scraper.price_stats_main import _dataset_window, _downsample, _parse_ym


def _months(*pairs):
    return [{"year": y, "month": m, "price": 100} for (y, m) in pairs]


def test_downsample_monthly_is_identity():
    ms = _months((2020, 1), (2020, 2), (2020, 3))
    assert _downsample(ms, "monthly") == ms
    assert _downsample(ms, None) == ms


def test_downsample_quarterly_keeps_period_end():
    ms = _months(*[(2020, m) for m in range(1, 13)])
    kept = [(r["year"], r["month"]) for r in _downsample(ms, "quarterly")]
    assert kept == [(2020, 3), (2020, 6), (2020, 9), (2020, 12)]


def test_downsample_annual_keeps_last_available_per_year():
    ms = _months((2020, 5), (2020, 11), (2021, 2), (2021, 12), (2022, 4))
    kept = [(r["year"], r["month"]) for r in _downsample(ms, "annual")]
    assert kept == [(2020, 11), (2021, 12), (2022, 4)]  # last present month each year


def test_downsample_semiannual():
    ms = _months(*[(2021, m) for m in range(1, 13)])
    kept = [(r["year"], r["month"]) for r in _downsample(ms, "semiannual")]
    assert kept == [(2021, 6), (2021, 12)]


def test_parse_ym():
    assert _parse_ym("2018-03") == (2018, 3)
    assert _parse_ym("2026-12") == (2026, 12)
    assert _parse_ym(None) is None
    assert _parse_ym("") is None
    assert _parse_ym("garbage") is None


def test_dataset_window_uses_dataset_values():
    win = _dataset_window(
        {"start_ym": "2016-01", "end_ym": "2020-12"}, (2015, 1), (2026, 6)
    )
    assert win == ((2016, 1), (2020, 12))


def test_dataset_window_falls_back_to_defaults():
    assert _dataset_window({}, (2015, 1), (2026, 6)) == ((2015, 1), (2026, 6))
    # partial: only start set
    win = _dataset_window({"start_ym": "2019-06"}, (2015, 1), (2026, 6))
    assert win == ((2019, 6), (2026, 6))
