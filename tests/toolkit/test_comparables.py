"""Hermetic tests for find_comparables — assert SQL shape and bound params.

These tests don't touch a DB. A live-DB version lives in
test_comparables_live.py and is gated on SUPABASE_DB_URL.
"""

from __future__ import annotations

from typing import Any

from toolkit.comparables import (
    ComparableFilters,
    TargetSpec,
    build_query,
    find_comparables,
)


def test_minimal_query_only_spatial():
    target = TargetSpec(lat=50.0, lng=14.0)
    filters = ComparableFilters(
        active_only=False,
        category_main=None,
        category_type=None,
        include_unreliable=True,
    )
    sql, params = build_query(target, filters)
    assert "ST_DWithin" in sql
    assert "ST_Distance" in sql
    assert "ORDER BY distance_m" in sql
    assert "LIMIT 500" in sql
    assert params["lat"] == 50.0
    assert params["lng"] == 14.0
    assert params["radius_m"] == 1000


def test_active_only_adds_recency_filter():
    sql, params = build_query(
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(
            active_only=True,
            max_age_days=14,
            category_main=None,
            category_type=None,
            include_unreliable=True,
        ),
    )
    assert "l.is_active = true" in sql
    assert "make_interval(days => %(max_age_days)s)" in sql
    assert params["max_age_days"] == 14


def test_default_excludes_unreliable():
    sql, _ = build_query(
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(category_main=None, category_type=None),
    )
    assert "listing_fetch_failures" in sql
    assert "given_up = true" in sql


def test_include_unreliable_flag_drops_failures_clause():
    sql, _ = build_query(
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(
            include_unreliable=True,
            category_main=None,
            category_type=None,
        ),
    )
    assert "listing_fetch_failures" not in sql


def test_default_filters_apartments_for_rent():
    sql, params = build_query(
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(),
    )
    assert "l.category_main = %(category_main)s" in sql
    assert "l.category_type = %(category_type)s" in sql
    assert params["category_main"] == "byt"
    assert params["category_type"] == "pronajem"


def test_disposition_exact_filter():
    target = TargetSpec(lat=50.0, lng=14.0, disposition="2+kk")
    sql, params = build_query(
        target,
        ComparableFilters(disposition_match="exact"),
    )
    assert "l.disposition = %(disposition)s" in sql
    assert params["disposition"] == "2+kk"


def test_disposition_loose_expands_to_set():
    target = TargetSpec(lat=50.0, lng=14.0, disposition="2+kk")
    sql, params = build_query(
        target,
        ComparableFilters(disposition_match="loose"),
    )
    assert "l.disposition = ANY(%(disposition_loose)s)" in sql
    assert set(params["disposition_loose"]) == {"2+kk", "2+1"}


def test_disposition_any_skips_filter():
    target = TargetSpec(lat=50.0, lng=14.0, disposition="2+kk")
    sql, _ = build_query(
        target,
        ComparableFilters(disposition_match="any"),
    )
    where_clause = sql.split("ORDER BY")[0]
    assert "l.disposition = " not in where_clause
    assert "l.disposition = ANY" not in where_clause


def test_area_band_applies_only_when_target_has_area():
    sql, params = build_query(
        TargetSpec(lat=50.0, lng=14.0, area_m2=50.0),
        ComparableFilters(area_band_pct=0.20),
    )
    assert "l.area_m2 BETWEEN %(area_min)s AND %(area_max)s" in sql
    assert params["area_min"] == 40.0
    assert params["area_max"] == 60.0


def test_area_band_skipped_when_target_lacks_area():
    sql, params = build_query(
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(area_band_pct=0.20),
    )
    assert "l.area_m2 BETWEEN" not in sql
    assert "area_min" not in params


def test_floor_band_requires_target_floor():
    sql_no, _ = build_query(
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(floor_band=2),
    )
    assert "l.floor BETWEEN" not in sql_no

    sql_yes, params = build_query(
        TargetSpec(lat=50.0, lng=14.0, floor=5),
        ComparableFilters(floor_band=2),
    )
    assert "l.floor BETWEEN %(floor_min)s AND %(floor_max)s" in sql_yes
    assert params["floor_min"] == 3 and params["floor_max"] == 7


def test_condition_and_building_lists_use_any():
    sql, params = build_query(
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(
            condition_match=["novostavba", "po rekonstrukci"],
            building_type_match=["cihla", "panel"],
        ),
    )
    assert "l.condition = ANY(%(condition_match)s)" in sql
    assert "l.building_type = ANY(%(building_type_match)s)" in sql
    assert params["condition_match"] == ["novostavba", "po rekonstrukci"]
    assert params["building_type_match"] == ["cihla", "panel"]


def test_price_bounds_bind_when_set():
    sql, params = build_query(
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(min_price_czk=10000, max_price_czk=30000),
    )
    assert "l.price_czk >= %(min_price_czk)s" in sql
    assert "l.price_czk <= %(max_price_czk)s" in sql
    assert params["min_price_czk"] == 10000
    assert params["max_price_czk"] == 30000


def test_locality_district_id_filter():
    sql, params = build_query(
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(locality_district_id=42),
    )
    assert "l.locality_district_id = %(locality_district_id)s" in sql
    assert params["locality_district_id"] == 42


def test_exclude_ids_uses_array_not_equal():
    sql, params = build_query(
        TargetSpec(lat=50.0, lng=14.0, exclude_ids=[1, 2, 3]),
        ComparableFilters(),
    )
    assert "l.sreality_id <> ALL(%(exclude_ids)s)" in sql
    assert params["exclude_ids"] == [1, 2, 3]


def test_user_values_never_string_interpolated():
    sql, _ = build_query(
        TargetSpec(
            lat=50.0,
            lng=14.0,
            disposition="'; drop table listings; --",
            area_m2=42.0,
        ),
        ComparableFilters(
            condition_match=["'; drop table listings; --"],
            min_price_czk=42,
            locality_district_id=99,
        ),
    )
    assert "drop table" not in sql.lower()


class _FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]], cols: list[str]) -> None:
        self._rows = rows
        self._cols = cols
        self.executed: tuple[str, dict[str, Any]] | None = None
        self.description = [(c,) for c in cols] if cols else None

    def execute(self, sql: str, params: dict[str, Any]) -> None:
        self.executed = (sql, params)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakeConn:
    def __init__(self, cur: _FakeCursor) -> None:
        self._cur = cur

    def cursor(self) -> _FakeCursor:
        return self._cur


def test_find_comparables_returns_envelope():
    cols = [
        "sreality_id", "price_czk", "area_m2", "price_per_m2",
        "disposition", "district", "locality_district_id",
        "floor", "building_type", "condition",
        "distance_m", "first_seen_at", "last_seen_at",
    ]
    from datetime import datetime, timezone
    rows = [
        (
            1, 20000, 50.0, 400.0, "2+kk", "Praha 1", 42, 5,
            "cihla", "novostavba", 123.4,
            datetime(2026, 5, 1, tzinfo=timezone.utc),
            datetime(2026, 5, 2, tzinfo=timezone.utc),
        ),
    ]
    cur = _FakeCursor(rows, cols)
    conn = _FakeConn(cur)
    res = find_comparables(
        conn,  # type: ignore[arg-type]
        TargetSpec(lat=50.0, lng=14.0, area_m2=50.0, disposition="2+kk"),
        ComparableFilters(),
    )
    assert res["metadata"]["tool"] == "find_comparables"
    assert res["metadata"]["result_count"] == 1
    listing = res["data"]["listings"][0]
    assert listing["sreality_id"] == 1
    assert listing["distance_m"] == 123.4
    assert listing["first_seen_at"].startswith("2026-05-01")
    assert res["metadata"]["data_freshness"].startswith("2026-05-02")
    assert res["metadata"]["filters_used"]["target"]["lat"] == 50.0


def test_find_comparables_empty_db_returns_empty_listings():
    cur = _FakeCursor([], [
        "sreality_id", "price_czk", "area_m2", "price_per_m2",
        "disposition", "district", "locality_district_id",
        "floor", "building_type", "condition", "distance_m",
        "first_seen_at", "last_seen_at",
    ])
    conn = _FakeConn(cur)
    res = find_comparables(
        conn,  # type: ignore[arg-type]
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(),
    )
    assert res["data"]["listings"] == []
    assert res["metadata"]["result_count"] == 0
    assert res["metadata"]["data_freshness"] is None
