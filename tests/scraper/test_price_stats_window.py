"""Tests for the per-dataset scrape-window helpers."""

from __future__ import annotations

from scraper.price_stats_main import _dataset_window, _parse_ym


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
