"""Tests for toolkit.find_distribution_outliers — hermetic.

Mocks compare_snapshots and the listing_fetch_failures cursor.
"""

from __future__ import annotations

from typing import Any

from toolkit import outliers as out


def _listing(sid: int, **fields: Any) -> dict[str, Any]:
    return {"sreality_id": sid, **fields}


def _stable_history(_conn: Any, sid: int) -> dict[str, Any]:
    return {
        "data": {
            "snapshot_count": 1,
            "price_change_pattern": "stable",
            "time_on_market_days": 1,
        },
        "metadata": {"tool": "compare_snapshots"},
    }


class _FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self._rows = rows or []
        self.executed: tuple[str, tuple[Any, ...]] | None = None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.executed = (sql, params)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakeConn:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self._cur = _FakeCursor(rows)

    def cursor(self) -> _FakeCursor:
        return self._cur


def test_too_small_sample_returns_empty_with_note():
    listings = [_listing(i, price_per_m2=100.0 + i) for i in range(3)]
    res = out.find_distribution_outliers(_FakeConn(), listings)
    d = res["data"]
    assert d["outliers"] == []
    assert d["non_outlier_ids"] == [0, 1, 2]
    assert d["median"] is None
    assert d["iqr"] is None
    assert "notes" in res["metadata"]
    assert "5" in res["metadata"]["notes"][0]


def test_single_high_outlier_flagged_statistical(monkeypatch):
    monkeypatch.setattr("toolkit.snapshots.compare_snapshots", _stable_history)
    listings = [_listing(i, price_per_m2=400.0 + i) for i in range(20)]
    listings.append(_listing(999, price_per_m2=10000.0))

    res = out.find_distribution_outliers(_FakeConn(), listings)
    d = res["data"]
    assert d["median"] is not None
    assert d["iqr"] is not None and d["iqr"] > 0
    assert len(d["outliers"]) == 1
    o = d["outliers"][0]
    assert o["sreality_id"] == 999
    assert o["value"] == 10000.0
    assert o["direction"] == "high"
    assert o["deviation_iqr_units"] > 0
    assert "statistical_outlier" in o["reasons"]


def test_low_outlier_gets_low_direction_and_negative_deviation(monkeypatch):
    monkeypatch.setattr("toolkit.snapshots.compare_snapshots", _stable_history)
    listings = [_listing(i, price_per_m2=400.0 + i) for i in range(20)]
    listings.append(_listing(999, price_per_m2=10.0))

    res = out.find_distribution_outliers(_FakeConn(), listings)
    o = res["data"]["outliers"][0]
    assert o["direction"] == "low"
    assert o["deviation_iqr_units"] < 0


def test_outlier_with_stairstep_pattern_gets_extra_reason(monkeypatch):
    def fake_compare(_conn: Any, sid: int) -> dict[str, Any]:
        if sid == 999:
            return {
                "data": {
                    "snapshot_count": 5,
                    "price_change_pattern": "stairstep_dropping",
                    "time_on_market_days": 30,
                },
                "metadata": {},
            }
        return _stable_history(_conn, sid)

    monkeypatch.setattr("toolkit.snapshots.compare_snapshots", fake_compare)
    listings = [_listing(i, price_per_m2=400.0 + i) for i in range(20)]
    listings.append(_listing(999, price_per_m2=10000.0))

    res = out.find_distribution_outliers(_FakeConn(), listings)
    o = res["data"]["outliers"][0]
    assert "statistical_outlier" in o["reasons"]
    assert "stairstep_dropping" in o["reasons"]
    assert o["history_summary"]["price_change_pattern"] == "stairstep_dropping"
    assert o["history_summary"]["snapshot_count"] == 5


def test_outlier_with_failure_row_gets_fetch_failures_reason(monkeypatch):
    monkeypatch.setattr("toolkit.snapshots.compare_snapshots", _stable_history)
    listings = [_listing(i, price_per_m2=400.0 + i) for i in range(20)]
    listings.append(_listing(999, price_per_m2=10000.0))

    conn = _FakeConn([(999, 3)])
    res = out.find_distribution_outliers(conn, listings)
    o = res["data"]["outliers"][0]
    assert "fetch_failures" in o["reasons"]
    assert o["history_summary"]["active_failure_attempts"] == 3


def test_long_time_on_market_threshold(monkeypatch):
    def fake_compare(_conn: Any, sid: int) -> dict[str, Any]:
        return {
            "data": {
                "snapshot_count": 5,
                "price_change_pattern": "stable",
                "time_on_market_days": 90,
            },
            "metadata": {},
        }

    monkeypatch.setattr("toolkit.snapshots.compare_snapshots", fake_compare)
    listings = [_listing(i, price_per_m2=400.0 + i) for i in range(20)]
    listings.append(_listing(999, price_per_m2=10000.0))

    res = out.find_distribution_outliers(_FakeConn(), listings)
    o = res["data"]["outliers"][0]
    assert "long_time_on_market" in o["reasons"]


def test_long_tom_boundary_at_60_excluded(monkeypatch):
    """60 is the boundary; spec says > 60, so exactly 60 is NOT long."""
    def fake_compare(_conn: Any, sid: int) -> dict[str, Any]:
        return {
            "data": {
                "snapshot_count": 1,
                "price_change_pattern": "stable",
                "time_on_market_days": 60,
            },
            "metadata": {},
        }

    monkeypatch.setattr("toolkit.snapshots.compare_snapshots", fake_compare)
    listings = [_listing(i, price_per_m2=400.0 + i) for i in range(20)]
    listings.append(_listing(999, price_per_m2=10000.0))

    res = out.find_distribution_outliers(_FakeConn(), listings)
    o = res["data"]["outliers"][0]
    assert "long_time_on_market" not in o["reasons"]


def test_all_equal_cohort_returns_no_outliers():
    listings = [_listing(i, price_per_m2=500.0) for i in range(10)]
    res = out.find_distribution_outliers(_FakeConn(), listings)
    assert res["data"]["outliers"] == []
    assert res["data"]["iqr"] == 0.0
    assert res["data"]["non_outlier_ids"] == list(range(10))


def test_investigate_history_false_skips_compare_snapshots(monkeypatch):
    called: list[int] = []

    def fake_compare(_conn: Any, sid: int) -> dict[str, Any]:
        called.append(sid)
        return _stable_history(_conn, sid)

    monkeypatch.setattr("toolkit.snapshots.compare_snapshots", fake_compare)
    listings = [_listing(i, price_per_m2=400.0 + i) for i in range(20)]
    listings.append(_listing(999, price_per_m2=10000.0))

    res = out.find_distribution_outliers(
        _FakeConn(), listings, investigate_history=False,
    )
    assert called == []
    o = res["data"]["outliers"][0]
    assert o["reasons"] == ["statistical_outlier"]
    assert o["history_summary"] is None


def test_investigate_history_false_still_checks_failures():
    listings = [_listing(i, price_per_m2=400.0 + i) for i in range(20)]
    listings.append(_listing(999, price_per_m2=10000.0))

    conn = _FakeConn([(999, 2)])
    res = out.find_distribution_outliers(
        conn, listings, investigate_history=False,
    )
    o = res["data"]["outliers"][0]
    assert "fetch_failures" in o["reasons"]
    assert o["history_summary"] is None


def test_field_can_be_price_czk(monkeypatch):
    monkeypatch.setattr("toolkit.snapshots.compare_snapshots", _stable_history)
    listings = [_listing(i, price_czk=20000 + 100 * i) for i in range(20)]
    listings.append(_listing(999, price_czk=200000))

    res = out.find_distribution_outliers(_FakeConn(), listings, field="price_czk")
    o = res["data"]["outliers"][0]
    assert o["sreality_id"] == 999
    assert res["data"]["field"] == "price_czk"
    assert res["metadata"]["filters_used"]["field"] == "price_czk"


def test_failure_query_batched_uses_any(monkeypatch):
    monkeypatch.setattr("toolkit.snapshots.compare_snapshots", _stable_history)
    listings = [_listing(i, price_per_m2=400.0 + i) for i in range(20)]
    listings.append(_listing(998, price_per_m2=10000.0))
    listings.append(_listing(999, price_per_m2=11000.0))

    conn = _FakeConn()
    out.find_distribution_outliers(conn, listings)

    assert conn._cur.executed is not None
    sql, params = conn._cur.executed
    assert "listing_fetch_failures" in sql
    assert "sreality_id = ANY(%s)" in sql
    assert sorted(params[0]) == [998, 999]


def test_no_outliers_means_no_failure_query():
    listings = [_listing(i, price_per_m2=400.0 + i) for i in range(20)]
    conn = _FakeConn()
    res = out.find_distribution_outliers(conn, listings)
    assert res["data"]["outliers"] == []
    assert conn._cur.executed is None  # no batch lookup performed


def test_metadata_envelope_shape():
    listings = [_listing(i, price_per_m2=500.0) for i in range(10)]
    res = out.find_distribution_outliers(_FakeConn(), listings)
    md = res["metadata"]
    assert md["tool"] == "find_distribution_outliers"
    assert md["filters_used"]["field"] == "price_per_m2"
    assert md["filters_used"]["iqr_multiplier"] == 1.5
    assert md["filters_used"]["investigate_history"] is True
    assert "queried_at" in md
