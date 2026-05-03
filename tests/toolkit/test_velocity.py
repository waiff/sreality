"""Hermetic tests for compute_market_velocity and compute_listing_velocity.

No DB connection. Synthetic rows fed via _FakeCursor. Cohort building
SQL is asserted via build_market_velocity_query.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from toolkit.comparables import ComparableFilters, TargetSpec
from toolkit.velocity import (
    VELOCITY_BANDS,
    build_market_velocity_query,
    compute_listing_velocity,
    compute_market_velocity,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _row(
    sid: int,
    days_first: int,
    days_last: int,
    is_active: bool,
) -> tuple[Any, ...]:
    n = _now()
    return (
        sid,
        n - timedelta(days=days_first),
        n - timedelta(days=days_last),
        is_active,
    )


# SQL shape


def test_market_velocity_query_includes_spatial_and_disposition():
    target = TargetSpec(lat=50.0, lng=14.0, disposition="2+kk")
    filters = ComparableFilters()
    sql, params = build_market_velocity_query(target, filters, "all")
    assert "ST_DWithin" in sql
    assert "l.disposition = %(disposition)s" in sql
    assert params["lat"] == 50.0 and params["lng"] == 14.0


def test_market_velocity_query_active_population_adds_clause():
    sql, _ = build_market_velocity_query(
        TargetSpec(lat=50.0, lng=14.0), ComparableFilters(), "active",
    )
    assert "l.is_active = true" in sql
    assert "l.is_active = false" not in sql


def test_market_velocity_query_delisted_population_adds_clause():
    sql, _ = build_market_velocity_query(
        TargetSpec(lat=50.0, lng=14.0), ComparableFilters(), "delisted",
    )
    assert "l.is_active = false" in sql
    assert "l.is_active = true" not in sql


def test_market_velocity_query_all_population_no_active_clause():
    sql, _ = build_market_velocity_query(
        TargetSpec(lat=50.0, lng=14.0), ComparableFilters(), "all",
    )
    where_block = sql.split("WHERE")[1]
    assert "l.is_active = true" not in where_block
    assert "l.is_active = false" not in where_block


def test_market_velocity_query_excludes_given_up_by_default():
    sql, _ = build_market_velocity_query(
        TargetSpec(lat=50.0, lng=14.0), ComparableFilters(), "all",
    )
    assert "listing_fetch_failures" in sql
    assert "given_up = true" in sql


def test_market_velocity_query_select_only_timestamps():
    sql, _ = build_market_velocity_query(
        TargetSpec(lat=50.0, lng=14.0), ComparableFilters(), "all",
    )
    select_block = sql.split("FROM")[0]
    assert "first_seen_at" in select_block
    assert "last_seen_at" in select_block
    assert "is_active" in select_block
    # No price or area in projection
    assert "price_czk" not in select_block
    assert "area_m2" not in select_block


# Hermetic _FakeCursor / _FakeConn


class _FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self._rows = rows or []
        self.executed: list[tuple[str, Any]] = []
        self._fetchone_row: tuple[Any, ...] | None = None

    def execute(self, sql: str, params: Any = ()) -> None:
        self.executed.append((sql, params))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._fetchone_row

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakeConn:
    def __init__(self, *cursors: _FakeCursor) -> None:
        self._cursors = list(cursors) if cursors else [_FakeCursor()]
        self._idx = 0

    def cursor(self) -> _FakeCursor:
        cur = self._cursors[self._idx]
        self._idx = min(self._idx + 1, len(self._cursors) - 1)
        return cur


# compute_market_velocity envelope


def test_market_velocity_envelope_basic():
    rows = [
        _row(1, 30, 5, False),    # delisted, TOM=25
        _row(2, 20, 0, True),     # active, TOM=20
        _row(3, 10, 0, True),     # active, TOM=10
        _row(4, 60, 50, False),   # delisted, TOM=10
        _row(5, 5, 0, True),      # active, TOM=5
    ]
    cur = _FakeCursor(rows)
    conn = _FakeConn(cur)
    res = compute_market_velocity(
        conn,  # type: ignore[arg-type]
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(),
        population="all",
    )
    d = res["data"]
    assert d["cohort_size"] == 5
    assert d["active_count"] == 3
    assert d["delisted_count"] == 2
    assert d["population"] == "all"
    assert d["tom_stats"]["n"] == 5
    assert d["tom_stats"]["min_days"] == 5
    assert d["tom_stats"]["max_days"] == 25
    assert d["tom_stats"]["median_days"] == 10.0


def test_market_velocity_empty_cohort_safe():
    cur = _FakeCursor([])
    conn = _FakeConn(cur)
    res = compute_market_velocity(
        conn,  # type: ignore[arg-type]
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(),
    )
    d = res["data"]
    assert d["cohort_size"] == 0
    assert d["tom_stats"]["median_days"] is None
    assert d["trend"]["recent"]["n"] == 0
    assert d["trend"]["older"]["n"] == 0
    assert "notes" in res["metadata"]


def test_market_velocity_trend_split():
    rows = [
        _row(1, 2, 0, True),     # recent (first_seen 2d ago), TOM=2
        _row(2, 4, 0, True),     # recent, TOM=4
        _row(3, 20, 0, True),    # older, TOM=20
        _row(4, 25, 0, True),    # older, TOM=25
        _row(5, 30, 0, True),    # older, TOM=30
    ]
    cur = _FakeCursor(rows)
    conn = _FakeConn(cur)
    res = compute_market_velocity(
        conn,  # type: ignore[arg-type]
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(),
        trend_split_days=7,
    )
    t = res["data"]["trend"]
    assert t["split_days"] == 7
    assert t["recent"]["n"] == 2
    assert t["recent"]["median_tom_days"] == 3.0
    assert t["older"]["n"] == 3
    assert t["older"]["median_tom_days"] == 25.0


def test_market_velocity_population_passed_through_to_metadata():
    cur = _FakeCursor([])
    conn = _FakeConn(cur)
    res = compute_market_velocity(
        conn,  # type: ignore[arg-type]
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(),
        population="active",
    )
    assert res["data"]["population"] == "active"
    assert res["metadata"]["filters_used"]["population"] == "active"


# compute_listing_velocity


def test_listing_velocity_listing_not_found():
    listing_cur = _FakeCursor([])
    conn = _FakeConn(listing_cur)
    res = compute_listing_velocity(conn, sreality_id=999)  # type: ignore[arg-type]
    assert res["data"]["found"] is False
    assert res["data"]["sreality_id"] == 999


def test_listing_velocity_classification_fast():
    """Target listing with TOM lower than 25% of peers → 'fast'."""
    n = _now()
    listing_cur = _FakeCursor()
    listing_cur._fetchone_row = (
        n - timedelta(days=2),    # first_seen: target TOM = 2
        n,                        # last_seen
        True,                     # is_active
        "2+kk",
        50.0, 14.0,               # lat, lng
    )
    peer_rows = [
        _row(i + 1, days_first=20 + i, days_last=0, is_active=True)
        for i in range(20)
    ]
    peer_cur = _FakeCursor(peer_rows)
    conn = _FakeConn(listing_cur, peer_cur)

    res = compute_listing_velocity(conn, sreality_id=999)  # type: ignore[arg-type]
    d = res["data"]
    assert d["found"] is True
    assert d["tom_days"] == 2
    assert d["cohort_size"] == 20
    assert d["tom_percentile"] is not None and d["tom_percentile"] <= 25
    assert d["classification"] == "fast"


def test_listing_velocity_classification_stuck():
    n = _now()
    listing_cur = _FakeCursor()
    listing_cur._fetchone_row = (
        n - timedelta(days=200),
        n,
        True,
        "2+kk",
        50.0, 14.0,
    )
    peer_rows = [
        _row(i + 1, days_first=10 + i, days_last=0, is_active=True)
        for i in range(20)
    ]
    peer_cur = _FakeCursor(peer_rows)
    conn = _FakeConn(listing_cur, peer_cur)

    res = compute_listing_velocity(conn, sreality_id=999)  # type: ignore[arg-type]
    d = res["data"]
    assert d["tom_days"] == 200
    assert d["tom_percentile"] >= 90
    assert d["classification"] == "stuck"


def test_listing_velocity_classification_typical():
    n = _now()
    listing_cur = _FakeCursor()
    listing_cur._fetchone_row = (
        n - timedelta(days=20),
        n,
        True,
        "2+kk",
        50.0, 14.0,
    )
    peer_rows = [
        _row(i + 1, days_first=10 + i, days_last=0, is_active=True)
        for i in range(20)
    ]
    peer_cur = _FakeCursor(peer_rows)
    conn = _FakeConn(listing_cur, peer_cur)

    res = compute_listing_velocity(conn, sreality_id=999)  # type: ignore[arg-type]
    d = res["data"]
    assert d["classification"] == "typical"


def test_listing_velocity_excludes_self_via_exclude_ids():
    """Cohort SQL must include exclude_ids = [target sreality_id]."""
    n = _now()
    listing_cur = _FakeCursor()
    listing_cur._fetchone_row = (
        n - timedelta(days=10), n, True, "2+kk", 50.0, 14.0,
    )
    peer_cur = _FakeCursor([])
    conn = _FakeConn(listing_cur, peer_cur)

    compute_listing_velocity(conn, sreality_id=42)  # type: ignore[arg-type]
    cohort_sql, cohort_params = peer_cur.executed[0]
    assert "l.sreality_id <> ALL" in cohort_sql
    assert cohort_params["exclude_ids"] == [42]


def test_listing_velocity_thresholds_in_data():
    n = _now()
    listing_cur = _FakeCursor()
    listing_cur._fetchone_row = (
        n - timedelta(days=10), n, True, "2+kk", 50.0, 14.0,
    )
    peer_rows = [_row(i + 1, 10, 0, True) for i in range(10)]
    peer_cur = _FakeCursor(peer_rows)
    conn = _FakeConn(listing_cur, peer_cur)

    res = compute_listing_velocity(conn, sreality_id=999)  # type: ignore[arg-type]
    assert res["data"]["thresholds"] == VELOCITY_BANDS


def test_listing_velocity_small_peer_cohort_emits_note():
    n = _now()
    listing_cur = _FakeCursor()
    listing_cur._fetchone_row = (
        n - timedelta(days=10), n, True, "2+kk", 50.0, 14.0,
    )
    peer_rows = [_row(i + 1, 10, 0, True) for i in range(3)]
    peer_cur = _FakeCursor(peer_rows)
    conn = _FakeConn(listing_cur, peer_cur)

    res = compute_listing_velocity(conn, sreality_id=999)  # type: ignore[arg-type]
    assert "notes" in res["metadata"]
    assert "below 5" in res["metadata"]["notes"][0]
