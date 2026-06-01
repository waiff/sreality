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
    find_comparables_relaxed,
)
from toolkit.comparables import _apply_relaxation


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


def test_default_applies_no_category_filter():
    """No silent apartment-rental default: an unspecified category means
    'search every category', not 'byt/pronajem'."""
    sql, params = build_query(
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(),
    )
    assert "l.category_main" not in sql
    assert "l.category_type" not in sql
    assert "category_main" not in params
    assert "category_type" not in params


def test_explicit_category_filters_applied():
    sql, params = build_query(
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(category_main="dum", category_type="prodej"),
    )
    assert "l.category_main = %(category_main)s" in sql
    assert "l.category_type = %(category_type)s" in sql
    assert params["category_main"] == "dum"
    assert params["category_type"] == "prodej"


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


def test_price_per_m2_bounds_bind_when_set():
    sql, params = build_query(
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(min_price_per_m2=50000, max_price_per_m2=120000),
    )
    assert (
        "l.price_czk::numeric / NULLIF(l.area_m2, 0) >= %(min_price_per_m2)s"
        in sql
    )
    assert (
        "l.price_czk::numeric / NULLIF(l.area_m2, 0) <= %(max_price_per_m2)s"
        in sql
    )
    assert params["min_price_per_m2"] == 50000
    assert params["max_price_per_m2"] == 120000


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


def test_new_category_columns_in_select_projection():
    sql, _ = build_query(
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(),
    )
    select = sql.split("FROM listings")[0]
    for col in (
        "l.estate_area", "l.usable_area", "l.garden_area",
        "l.category_sub_cb",
        "l.furnished", "l.terrace", "l.cellar", "l.garage",
        "l.parking_lots", "l.ownership",
    ):
        assert col in select


def test_category_sub_cb_filter():
    sql_none, _ = build_query(
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(),
    )
    where_none = sql_none.split("ORDER BY")[0]
    assert "category_sub_cb =" not in where_none

    sql, params = build_query(
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(category_sub_cb=37),
    )
    assert "l.category_sub_cb = %(category_sub_cb)s" in sql
    assert params["category_sub_cb"] == 37


def test_furnished_and_ownership_text_filters():
    sql, params = build_query(
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(furnished="ano", ownership="osobni"),
    )
    assert "l.furnished = %(furnished)s" in sql
    assert "l.ownership = %(ownership)s" in sql
    assert params["furnished"] == "ano"
    assert params["ownership"] == "osobni"


def test_terrace_cellar_garage_three_state():
    where_none = build_query(
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(),
    )[0].split("ORDER BY")[0]
    assert "l.terrace =" not in where_none
    assert "l.cellar =" not in where_none
    assert "l.garage =" not in where_none

    sql, params = build_query(
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(terrace=True, cellar=False, garage=True),
    )
    assert "l.terrace = %(terrace)s" in sql
    assert "l.cellar = %(cellar)s" in sql
    assert "l.garage = %(garage)s" in sql
    assert params["terrace"] is True
    assert params["cellar"] is False
    assert params["garage"] is True


def test_estate_and_usable_area_bands_and_min_parking_lots():
    sql, params = build_query(
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(
            min_estate_area=200,
            max_estate_area=500,
            min_usable_area=80,
            max_usable_area=120,
            min_parking_lots=2,
        ),
    )
    assert "l.estate_area >= %(min_estate_area)s" in sql
    assert "l.estate_area <= %(max_estate_area)s" in sql
    assert "l.usable_area >= %(min_usable_area)s" in sql
    assert "l.usable_area <= %(max_usable_area)s" in sql
    assert "l.parking_lots >= %(min_parking_lots)s" in sql
    assert params["min_estate_area"] == 200
    assert params["max_estate_area"] == 500
    assert params["min_usable_area"] == 80
    assert params["max_usable_area"] == 120
    assert params["min_parking_lots"] == 2


def test_condition_level_min_filters_add_where_branches():
    """Both new condition-level filters should add `>= N` branches and
    bind the parameters. Absent (None) filters add no clauses — important
    because NULL rows would otherwise be excluded by an unconditional
    `IS NOT NULL` check."""
    sql, params = build_query(
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(
            building_condition_level_min=4,
            apartment_condition_level_min=3,
        ),
    )
    assert "l.building_condition_level >= %(building_condition_level_min)s" in sql
    assert "l.apartment_condition_level >= %(apartment_condition_level_min)s" in sql
    assert params["building_condition_level_min"] == 4
    assert params["apartment_condition_level_min"] == 3


def test_condition_level_filters_absent_when_none():
    sql, params = build_query(
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(),
    )
    assert "building_condition_level" not in sql
    assert "apartment_condition_level" not in sql
    assert "building_condition_level_min" not in params
    assert "apartment_condition_level_min" not in params


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


class _QueuedFakeConn:
    """Fake connection that hands out a fresh cursor per call.

    Each cursor returns the next batch of rows from `batches`, so
    find_comparables_relaxed (which calls find_comparables multiple
    times) sees a different result set on each strict/widened query.
    """

    def __init__(self, batches: list[list[tuple[Any, ...]]]) -> None:
        self._batches = list(batches)
        self.executed: list[tuple[str, dict[str, Any]]] = []

    def cursor(self) -> _FakeCursor:
        rows = self._batches.pop(0) if self._batches else []
        cur = _FakeCursor(rows, _RESULT_COLS)
        original_execute = cur.execute

        def _record(sql: str, params: dict[str, Any]) -> None:
            self.executed.append((sql, dict(params)))
            original_execute(sql, params)

        cur.execute = _record  # type: ignore[assignment]
        return cur


def test_apply_relaxation_widens_radius_off_base():
    base = ComparableFilters(radius_m=1000)
    once = _apply_relaxation(base, base, "radius_x1.5")
    assert once.radius_m == 1500
    twice = _apply_relaxation(once, base, "radius_x2")
    assert twice.radius_m == 2000


def test_apply_relaxation_widens_area_band_off_base():
    base = ComparableFilters(area_band_pct=0.20)
    once = _apply_relaxation(base, base, "area_band_+0.10")
    assert abs(once.area_band_pct - 0.30) < 1e-9
    twice = _apply_relaxation(once, base, "area_band_+0.20")
    assert abs(twice.area_band_pct - 0.40) < 1e-9


def test_apply_relaxation_loosens_disposition_match():
    base = ComparableFilters(disposition_match="exact")
    loose = _apply_relaxation(base, base, "disposition_loose")
    assert loose.disposition_match == "loose"
    any_ = _apply_relaxation(loose, base, "disposition_any")
    assert any_.disposition_match == "any"


def test_apply_relaxation_disposition_loose_skipped_when_already_any():
    base = ComparableFilters(disposition_match="any")
    out = _apply_relaxation(base, base, "disposition_loose")
    assert out.disposition_match == "any"


def test_apply_relaxation_drops_hard_filters():
    base = ComparableFilters(
        condition_match=["novostavba"],
        building_type_match=["cihla"],
        energy_rating_match=["A"],
        floor_band=2,
    )
    assert _apply_relaxation(base, base, "drop_condition").condition_match is None
    assert _apply_relaxation(base, base, "drop_building_type").building_type_match is None
    assert _apply_relaxation(base, base, "drop_energy_rating").energy_rating_match is None
    assert _apply_relaxation(base, base, "drop_floor_band").floor_band is None


def test_apply_relaxation_unknown_action_raises():
    base = ComparableFilters()
    try:
        _apply_relaxation(base, base, "expand_universe")
    except ValueError as exc:
        assert "expand_universe" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_relaxed_strict_enough_returns_no_relaxations():
    conn = _QueuedFakeConn([[_row(i) for i in range(5)]])
    res = find_comparables_relaxed(
        conn,  # type: ignore[arg-type]
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(),
        min_results=5,
    )
    assert res["metadata"]["tool"] == "find_comparables_relaxed"
    assert res["metadata"]["relaxations_applied"] == 0
    assert res["data"]["min_results_satisfied"] is True
    assert len(res["data"]["relaxation_trace"]) == 1
    assert res["data"]["relaxation_trace"][0]["action"] is None
    assert res["data"]["relaxation_trace"][0]["step"] == 0


def test_relaxed_stops_at_first_step_that_satisfies():
    conn = _QueuedFakeConn([
        [_row(1)],
        [_row(i) for i in range(8)],
    ])
    res = find_comparables_relaxed(
        conn,  # type: ignore[arg-type]
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(radius_m=1000),
        min_results=5,
    )
    assert res["metadata"]["relaxations_applied"] == 1
    trace = res["data"]["relaxation_trace"]
    assert len(trace) == 2
    assert trace[0]["action"] is None
    assert trace[0]["result_count"] == 1
    assert trace[1]["action"] == "radius_x1.5"
    assert trace[1]["result_count"] == 8
    assert trace[1]["filters_snapshot"]["radius_m"] == 1500
    assert res["metadata"]["result_count"] == 8
    assert res["data"]["min_results_satisfied"] is True


def test_relaxed_exhausts_ladder_when_data_never_sufficient():
    batches = [[_row(1)] for _ in range(11)]
    conn = _QueuedFakeConn(batches)
    res = find_comparables_relaxed(
        conn,  # type: ignore[arg-type]
        TargetSpec(lat=50.0, lng=14.0, disposition="2+kk"),
        ComparableFilters(),
        min_results=5,
    )
    assert res["metadata"]["relaxations_applied"] == 10
    assert len(res["data"]["relaxation_trace"]) == 11
    assert res["data"]["min_results_satisfied"] is False
    actions = [s["action"] for s in res["data"]["relaxation_trace"]]
    assert actions[0] is None
    assert actions[1] == "radius_x1.5"
    assert actions[-1] == "drop_floor_band"


def test_relaxed_custom_ladder_overrides_default_order():
    batches = [[_row(1)], [_row(1)], [_row(i) for i in range(6)]]
    conn = _QueuedFakeConn(batches)
    res = find_comparables_relaxed(
        conn,  # type: ignore[arg-type]
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(),
        min_results=5,
        relaxation_ladder=["disposition_any", "drop_condition", "radius_x2"],
    )
    actions = [s["action"] for s in res["data"]["relaxation_trace"]]
    assert actions == [None, "disposition_any", "drop_condition"]
    assert res["metadata"]["relaxations_applied"] == 2
    assert res["metadata"]["result_count"] == 6


def test_relaxed_envelope_carries_cohort_freshness_stats():
    conn = _QueuedFakeConn([
        [_row(i, data_age_days=2) for i in range(6)],
    ])
    res = find_comparables_relaxed(
        conn,  # type: ignore[arg-type]
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(),
        min_results=5,
    )
    md = res["metadata"]
    assert md["oldest_data_age_days"] == 2
    assert md["newest_data_age_days"] == 2
    assert md["median_data_age_days"] == 2.0
    assert md["min_results"] == 5
    assert md["unverified_count"] == 6


def test_relaxed_final_filters_used_reflects_last_action():
    conn = _QueuedFakeConn([
        [_row(1)],
        [_row(i) for i in range(6)],
    ])
    res = find_comparables_relaxed(
        conn,  # type: ignore[arg-type]
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(radius_m=800),
        min_results=5,
    )
    assert res["metadata"]["filters_used"]["radius_m"] == 1200


def test_mf_gross_yield_pct_bounds_applied():
    sql, params = build_query(
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(min_mf_gross_yield_pct=4.0, max_mf_gross_yield_pct=6.0),
    )
    assert "l.mf_gross_yield_pct >= %(min_mf_gross_yield_pct)s" in sql
    assert "l.mf_gross_yield_pct <= %(max_mf_gross_yield_pct)s" in sql
    assert params["min_mf_gross_yield_pct"] == 4.0
    assert params["max_mf_gross_yield_pct"] == 6.0


def test_mf_gross_yield_pct_absent_when_unset():
    sql, params = build_query(TargetSpec(lat=50.0, lng=14.0), ComparableFilters())
    assert "mf_gross_yield_pct" not in sql
    assert "min_mf_gross_yield_pct" not in params
