"""Tests for toolkit.compare_snapshots.

Hermetic — fakes the conn cursor to serve canned snapshot rows.
Re-parses real JSON via scraper.parser.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from toolkit.snapshots import (
    _build_field_changes,
    _classify_pattern,
    _price_change_stats,
    compare_snapshots,
)
from scraper import parser as parser_module


_FIXTURE = Path(__file__).parent.parent / "fixtures" / "sample_listing.json"


def _load_raw() -> dict[str, Any]:
    return json.loads(_FIXTURE.read_text())


def _ts(days_ago: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days_ago)


def _trajectory(prices: list[int | None]) -> list[dict[str, Any]]:
    return [
        {
            "snapshot_id": i,
            "at": datetime(2026, 5, i + 1, tzinfo=timezone.utc).isoformat(),
            "price_czk": p,
        }
        for i, p in enumerate(prices)
    ]


# pattern classifier — table-driven
def test_pattern_stable_zero_changes():
    assert _classify_pattern(_trajectory([20000, 20000, 20000])) == "stable"


def test_pattern_stable_single_snapshot():
    assert _classify_pattern(_trajectory([20000])) == "stable"


def test_pattern_single_drop():
    assert _classify_pattern(_trajectory([20000, 18000])) == "single_drop"


def test_pattern_single_rise_maps_to_rising():
    assert _classify_pattern(_trajectory([18000, 20000])) == "rising"


def test_pattern_stairstep_dropping():
    assert _classify_pattern(_trajectory([20000, 19000, 18000])) == "stairstep_dropping"


def test_pattern_rising_two_steps():
    assert _classify_pattern(_trajectory([18000, 19000, 20000])) == "rising"


def test_pattern_volatile_mixed():
    assert _classify_pattern(_trajectory([20000, 18000, 19000])) == "volatile"


def test_pattern_handles_none_prices():
    assert _classify_pattern(_trajectory([None, 20000, 20000])) == "stable"
    # Non-adjacent comparison is intentionally skipped — the None pair
    # interrupts the run, so this counts as zero changes (stable).
    assert _classify_pattern(_trajectory([20000, None, 18000])) == "stable"


# price change stats
def test_price_change_stats_simple_drop():
    count, total = _price_change_stats(_trajectory([20000, 18000]))
    assert count == 1 and total == -2000


def test_price_change_stats_no_change():
    count, total = _price_change_stats(_trajectory([20000, 20000, 20000]))
    assert count == 0 and total == 0


def test_price_change_stats_total_is_last_minus_first():
    count, total = _price_change_stats(_trajectory([20000, 19000, 18000, 18000]))
    assert count == 2
    assert total == -2000


# field changes — using the real fixture
def test_field_changes_emits_one_row_per_changed_field():
    raw_a = _load_raw()
    raw_b = copy.deepcopy(raw_a)
    raw_b["price_czk"] = {**raw_b["price_czk"], "value_raw": 22500}

    snaps = [
        {"id": 1, "scraped_at": _ts(2), "price_czk": 16900, "raw_json": raw_a},
        {"id": 2, "scraped_at": _ts(1), "price_czk": 22500, "raw_json": raw_b},
    ]
    changes = _build_field_changes(snaps, parser_module)
    fields = [c["field"] for c in changes]
    assert "price_czk" in fields
    price_change = next(c for c in changes if c["field"] == "price_czk")
    assert price_change["from"] == 16900
    assert price_change["to"] == 22500
    assert price_change["snapshot_id"] == 2


def test_field_changes_image_url_diff_emits_images_field():
    raw_a = _load_raw()
    raw_b = copy.deepcopy(raw_a)
    images = ((raw_b.get("_embedded") or {}).get("images")) or []
    if images:
        images[0]["_links"]["view"]["href"] = (
            images[0]["_links"]["view"]["href"] + "?v=2"
        )

    snaps = [
        {"id": 1, "scraped_at": _ts(2), "price_czk": 16900, "raw_json": raw_a},
        {"id": 2, "scraped_at": _ts(1), "price_czk": 16900, "raw_json": raw_b},
    ]
    changes = _build_field_changes(snaps, parser_module)
    assert any(c["field"] == "images" for c in changes)


def test_field_changes_skips_sreality_id_lon_lat():
    raw_a = _load_raw()
    raw_b = copy.deepcopy(raw_a)
    raw_b["map"] = {**(raw_b.get("map") or {}), "lon": 99.9, "lat": 99.9}
    snaps = [
        {"id": 1, "scraped_at": _ts(2), "price_czk": 16900, "raw_json": raw_a},
        {"id": 2, "scraped_at": _ts(1), "price_czk": 16900, "raw_json": raw_b},
    ]
    changes = _build_field_changes(snaps, parser_module)
    fields = {c["field"] for c in changes}
    assert "lon" not in fields
    assert "lat" not in fields
    assert "sreality_id" not in fields


def test_field_changes_returns_empty_for_single_snapshot():
    raw = _load_raw()
    snaps = [
        {"id": 1, "scraped_at": _ts(1), "price_czk": 16900, "raw_json": raw},
    ]
    assert _build_field_changes(snaps, parser_module) == []


# end-to-end via fake conn
class _FakeCursor:
    def __init__(self, responses: list[list[tuple]]):
        self._responses = responses
        self._idx = 0
        self._last: list[tuple] = []
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return None
    def execute(self, sql: str, params: tuple = ()) -> None:
        self._last = self._responses[self._idx]
        self._idx += 1
    def fetchall(self) -> list[tuple]:
        return self._last
    def fetchone(self):
        return self._last[0] if self._last else None


class _FakeConn:
    def __init__(self, responses: list[list[tuple]]):
        self._cur = _FakeCursor(responses)
    def cursor(self) -> _FakeCursor:
        return self._cur


def test_compare_snapshots_envelope_with_two_snapshots():
    raw_a = _load_raw()
    raw_b = copy.deepcopy(raw_a)
    raw_b["price_czk"] = {**raw_b["price_czk"], "value_raw": 18500}

    snap_rows = [
        (1, _ts(3), 16900, raw_a),
        (2, _ts(1), 18500, raw_b),
    ]
    listing_row = [(_ts(10),)]

    conn = _FakeConn([snap_rows, listing_row])
    res = compare_snapshots(conn, sreality_id=2836292428)  # type: ignore[arg-type]

    d = res["data"]
    assert d["sreality_id"] == 2836292428
    assert d["snapshot_count"] == 2
    assert d["price_change_count"] == 1
    assert d["price_change_total_czk"] == 1600
    assert d["price_change_pattern"] == "rising"
    assert len(d["price_trajectory"]) == 2
    assert d["price_trajectory"][0]["snapshot_id"] == 1
    assert d["price_trajectory"][1]["snapshot_id"] == 2
    assert d["time_on_market_days"] >= 9  # listing first_seen_at = 10 days ago

    md = res["metadata"]
    assert md["tool"] == "compare_snapshots"
    assert md["filters_used"]["sreality_id"] == 2836292428
    assert md["result_count"] == 2

    assert any(fc["field"] == "price_czk" for fc in d["field_changes"])


def test_compare_snapshots_empty_for_unknown_listing():
    conn = _FakeConn([[], []])  # no snapshots, no listing row
    res = compare_snapshots(conn, sreality_id=999)  # type: ignore[arg-type]
    d = res["data"]
    assert d["snapshot_count"] == 0
    assert d["first_snapshot_at"] is None
    assert d["last_snapshot_at"] is None
    assert d["price_trajectory"] == []
    assert d["field_changes"] == []
    assert d["price_change_pattern"] == "stable"
    assert d["time_on_market_days"] == 0


def test_compare_snapshots_since_filter_passes_seconds():
    raw_a = _load_raw()
    snap_rows = [(1, _ts(2), 16900, raw_a)]
    listing_row = [(_ts(5),)]
    conn = _FakeConn([snap_rows, listing_row])

    res = compare_snapshots(
        conn,  # type: ignore[arg-type]
        sreality_id=1,
        since=timedelta(days=7),
    )
    assert res["metadata"]["filters_used"]["since_days"] == 7
