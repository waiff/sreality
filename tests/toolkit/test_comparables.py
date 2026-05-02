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


def test_locality_region_id_filter():
    sql, params = build_query(
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(locality_region_id=8),
    )
    assert "l.locality_region_id = %(locality_region_id)s" in sql
    assert params["locality_region_id"] == 8


def test_amenity_booleans_three_state():
    where_none = build_query(
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(),
    )[0].split("ORDER BY")[0]
    assert "has_balcony =" not in where_none
    assert "has_lift =" not in where_none
    assert "has_parking =" not in where_none

    sql_true, params_true = build_query(
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(has_balcony=True, has_lift=True, has_parking=False),
    )
    assert "l.has_balcony = %(has_balcony)s" in sql_true
    assert "l.has_lift = %(has_lift)s" in sql_true
    assert "l.has_parking = %(has_parking)s" in sql_true
    assert params_true["has_balcony"] is True
    assert params_true["has_lift"] is True
    assert params_true["has_parking"] is False


def test_energy_rating_match_uses_any():
    sql, params = build_query(
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(energy_rating_match=["A", "B"]),
    )
    assert "l.energy_rating = ANY(%(energy_rating_match)s)" in sql
    assert params["energy_rating_match"] == ["A", "B"]


def test_total_floors_in_select_projection():
    sql, _ = build_query(
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(),
    )
    assert "l.total_floors" in sql.split("FROM listings")[0]


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


_RESULT_COLS = [
    "sreality_id", "price_czk", "area_m2", "price_per_m2",
    "disposition", "district",
    "locality_district_id", "locality_region_id",
    "floor", "total_floors",
    "building_type", "condition", "energy_rating",
    "has_balcony", "has_lift", "has_parking",
    "distance_m", "first_seen_at", "last_seen_at",
    "data_age_days", "latest_snapshot_id", "latest_snapshot_at",
    "last_freshness_check_at",
]


def _row(
    sreality_id: int,
    price_czk: int = 20000,
    area_m2: float = 50.0,
    data_age_days: int = 1,
    latest_snapshot_id: int = 100,
    last_freshness_check_at: Any = None,
):
    from datetime import datetime, timezone
    return (
        sreality_id, price_czk, area_m2,
        float(price_czk) / area_m2,
        "2+kk", "Praha 1",
        42, 8, 5, 6,
        "cihla", "novostavba", "B",
        True, True, False,
        123.4,
        datetime(2026, 5, 1, tzinfo=timezone.utc),
        datetime(2026, 5, 2, tzinfo=timezone.utc),
        data_age_days,
        latest_snapshot_id,
        datetime(2026, 5, 2, tzinfo=timezone.utc),
        last_freshness_check_at,
    )


def test_find_comparables_returns_envelope():
    from datetime import datetime, timezone
    cur = _FakeCursor([_row(1)], _RESULT_COLS)
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
    assert listing["total_floors"] == 6
    assert listing["energy_rating"] == "B"
    assert listing["has_lift"] is True
    assert listing["distance_m"] == 123.4
    assert listing["first_seen_at"].startswith("2026-05-01")
    assert res["metadata"]["data_freshness"].startswith("2026-05-02")
    assert res["metadata"]["filters_used"]["target"]["lat"] == 50.0


def test_find_comparables_exposes_freshness_fields_per_listing():
    from datetime import datetime, timezone
    cur = _FakeCursor(
        [_row(
            1,
            data_age_days=3,
            latest_snapshot_id=42,
            last_freshness_check_at=datetime(
                2026, 5, 2, 10, tzinfo=timezone.utc
            ),
        )],
        _RESULT_COLS,
    )
    conn = _FakeConn(cur)
    res = find_comparables(
        conn,  # type: ignore[arg-type]
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(),
    )
    listing = res["data"]["listings"][0]
    assert listing["data_age_days"] == 3
    assert listing["latest_snapshot_id"] == 42
    assert listing["latest_snapshot_at"].startswith("2026-05-02")
    assert listing["last_freshness_check_at"].startswith("2026-05-02")


def test_find_comparables_cohort_metadata_aggregates_ages():
    cur = _FakeCursor(
        [
            _row(1, data_age_days=1, last_freshness_check_at=None),
            _row(2, data_age_days=5, last_freshness_check_at=None),
            _row(3, data_age_days=20, last_freshness_check_at=None),
        ],
        _RESULT_COLS,
    )
    conn = _FakeConn(cur)
    res = find_comparables(
        conn,  # type: ignore[arg-type]
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(),
    )
    md = res["metadata"]
    assert md["oldest_data_age_days"] == 20
    assert md["newest_data_age_days"] == 1
    assert md["median_data_age_days"] == 5.0
    assert md["unverified_count"] == 3


def test_find_comparables_cohort_metadata_unverified_count():
    from datetime import datetime, timezone
    rows = [
        _row(1, data_age_days=1, last_freshness_check_at=None),
        _row(
            2, data_age_days=2,
            last_freshness_check_at=datetime(2026, 5, 2, tzinfo=timezone.utc),
        ),
    ]
    cur = _FakeCursor(rows, _RESULT_COLS)
    conn = _FakeConn(cur)
    res = find_comparables(
        conn,  # type: ignore[arg-type]
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(),
    )
    assert res["metadata"]["unverified_count"] == 1


def test_find_comparables_empty_db_returns_empty_listings():
    cur = _FakeCursor([], _RESULT_COLS)
    conn = _FakeConn(cur)
    res = find_comparables(
        conn,  # type: ignore[arg-type]
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(),
    )
    assert res["data"]["listings"] == []
    assert res["metadata"]["result_count"] == 0
    assert res["metadata"]["data_freshness"] is None
    assert res["metadata"]["oldest_data_age_days"] is None
    assert res["metadata"]["unverified_count"] == 0


def test_find_comparables_sql_includes_lateral_joins():
    sql, _ = build_query(
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(),
    )
    assert "LEFT JOIN LATERAL" in sql
    assert "FROM listing_snapshots" in sql
    assert "FROM listing_freshness_checks" in sql
    assert "data_age_days" in sql
