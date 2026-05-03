"""Hermetic tests for describe_neighborhood — assert SQL shape and envelope.

No DB connection. Synthetic rows fed through a _FakeCursor.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from toolkit.neighborhoods import build_query, describe_neighborhood


# SQL shape


def test_query_uses_st_dwithin_and_radius_param():
    sql, params = build_query(
        lat=50.087, lng=14.42, radius_m=1000, max_age_days=30,
        category_main="byt", category_type="pronajem",
    )
    assert "ST_DWithin(" in sql
    assert "%(radius_m)s" in sql
    assert params["lat"] == 50.087 and params["lng"] == 14.42
    assert params["radius_m"] == 1000
    assert params["max_age_days"] == 30


def test_query_uses_active_filter_pattern():
    sql, _ = build_query(
        lat=50.0, lng=14.0, radius_m=1000, max_age_days=7,
        category_main=None, category_type=None,
    )
    assert "is_active = true" in sql
    assert "make_interval(days => %(max_age_days)s)" in sql


def test_query_excludes_given_up_failures_by_default():
    sql, _ = build_query(
        lat=50.0, lng=14.0, radius_m=1000, max_age_days=7,
        category_main=None, category_type=None,
    )
    assert "listing_fetch_failures" in sql
    assert "given_up = true" in sql


def test_query_default_category_filters_apply_when_set():
    sql, params = build_query(
        lat=50.0, lng=14.0, radius_m=1000, max_age_days=7,
        category_main="byt", category_type="pronajem",
    )
    assert "l.category_main = %(category_main)s" in sql
    assert "l.category_type = %(category_type)s" in sql
    assert params["category_main"] == "byt"
    assert params["category_type"] == "pronajem"


def test_query_omits_category_clauses_when_none():
    sql, params = build_query(
        lat=50.0, lng=14.0, radius_m=1000, max_age_days=7,
        category_main=None, category_type=None,
    )
    assert "category_main" not in sql
    assert "category_type" not in sql
    assert "category_main" not in params
    assert "category_type" not in params


def test_query_has_disposition_building_condition_groupby_via_ctes():
    sql, _ = build_query(
        lat=50.0, lng=14.0, radius_m=1000, max_age_days=7,
        category_main=None, category_type=None,
    )
    assert "disposition_mix AS" in sql
    assert "building_mix AS" in sql
    assert "condition_mix AS" in sql
    assert "GROUP BY 1" in sql


def test_query_treats_null_as_unknown_via_coalesce():
    sql, _ = build_query(
        lat=50.0, lng=14.0, radius_m=1000, max_age_days=7,
        category_main=None, category_type=None,
    )
    assert "coalesce(disposition, 'unknown')" in sql
    assert "coalesce(building_type, 'unknown')" in sql
    assert "coalesce(condition, 'unknown')" in sql


def test_query_excludes_dispositions_below_n_5():
    sql, _ = build_query(
        lat=50.0, lng=14.0, radius_m=1000, max_age_days=7,
        category_main=None, category_type=None,
    )
    assert "HAVING count(*) >= 5" in sql


def test_query_uses_percentile_cont_for_price_stats():
    sql, _ = build_query(
        lat=50.0, lng=14.0, radius_m=1000, max_age_days=7,
        category_main=None, category_type=None,
    )
    assert "percentile_cont(0.5) WITHIN GROUP (ORDER BY price_czk)" in sql
    assert "percentile_cont(0.25) WITHIN GROUP" in sql
    assert "percentile_cont(0.75) WITHIN GROUP" in sql


def test_query_trend_counts_use_first_and_last_seen():
    sql, _ = build_query(
        lat=50.0, lng=14.0, radius_m=1000, max_age_days=7,
        category_main=None, category_type=None,
    )
    assert "first_seen_at > now() - interval '7 days'" in sql
    assert "first_seen_at > now() - interval '30 days'" in sql
    assert "NOT is_active" in sql
    assert "last_seen_at > now() - interval '7 days'" in sql


def test_query_user_values_never_string_interpolated():
    sql, _ = build_query(
        lat=50.0, lng=14.0, radius_m=1000, max_age_days=7,
        category_main="'; drop table listings; --",
        category_type="'; drop table listings; --",
    )
    assert "drop table" not in sql.lower()


# Envelope shaping with synthetic rows


_RESULT_COLS = [
    "active_count",
    "disposition_counts",
    "building_counts",
    "condition_counts",
    "price_stats_list",
    "new_7d",
    "new_30d",
    "inactive_7d",
    "inactive_30d",
    "oldest_data_age_days",
    "median_data_age_days",
    "max_last_seen",
]


class _FakeCursor:
    def __init__(self, row: tuple[Any, ...] | None, cols: list[str]) -> None:
        self._row = row
        self._cols = cols
        self.executed: tuple[str, dict[str, Any]] | None = None
        self.description = [(c,) for c in cols] if cols else None

    def execute(self, sql: str, params: dict[str, Any]) -> None:
        self.executed = (sql, params)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakeConn:
    def __init__(self, cur: _FakeCursor) -> None:
        self._cur = cur

    def cursor(self) -> _FakeCursor:
        return self._cur


def test_envelope_shape_with_typical_response():
    row = (
        100,
        {"1+kk": 12, "2+kk": 31, "3+kk": 50, "unknown": 7},
        {"panel": 40, "cihla": 55, "unknown": 5},
        {"novostavba": 18, "po rekonstrukci": 40, "unknown": 42},
        [
            {
                "disposition": "2+kk",
                "n": 23,
                "median_price_czk": 18000.0,
                "median_price_per_m2": 380.0,
                "p25_price_per_m2": 340.0,
                "p75_price_per_m2": 420.0,
                "median_area_m2": 50.0,
            },
            {
                "disposition": "3+kk",
                "n": 30,
                "median_price_czk": 24000.0,
                "median_price_per_m2": 360.0,
                "p25_price_per_m2": 330.0,
                "p75_price_per_m2": 400.0,
                "median_area_m2": 70.0,
            },
        ],
        7, 25, 2, 8,
        14, 3.0,
        datetime(2026, 5, 2, 13, 0, tzinfo=timezone.utc),
    )
    cur = _FakeCursor(row, _RESULT_COLS)
    conn = _FakeConn(cur)
    res = describe_neighborhood(
        conn, lat=50.087, lng=14.42, radius_m=1000,  # type: ignore[arg-type]
    )

    d = res["data"]
    assert d["center"] == {"lat": 50.087, "lng": 14.42}
    assert d["radius_m"] == 1000
    assert d["active_listing_count"] == 100
    # density = 100 / (pi * 1.0**2) ≈ 31.83
    assert 31 < d["active_listings_per_km2"] < 32

    assert d["disposition_mix"] == {
        "1+kk": 0.12, "2+kk": 0.31, "3+kk": 0.5, "unknown": 0.07,
    }
    assert "unknown" in d["building_type_mix"]
    assert d["building_type_mix"]["unknown"] == 0.05

    ps = d["price_stats_by_disposition"]
    assert "2+kk" in ps and "3+kk" in ps
    assert ps["2+kk"]["n"] == 23
    assert ps["2+kk"]["median_price_czk"] == 18000
    assert ps["2+kk"]["p25_price_per_m2"] == 340.0

    assert d["trend"] == {
        "new_listings_last_7_days": 7,
        "new_listings_last_30_days": 25,
        "becoming_inactive_last_7_days": 2,
        "becoming_inactive_last_30_days": 8,
    }

    assert d["data_age"]["oldest_data_age_days"] == 14
    assert d["data_age"]["median_data_age_days"] == 3.0

    md = res["metadata"]
    assert md["tool"] == "describe_neighborhood"
    assert md["filters_used"]["category_main"] == "byt"
    assert md["filters_used"]["category_type"] == "pronajem"
    assert md["result_count"] == 100
    assert md["data_freshness"].startswith("2026-05-02")
    assert "notes" not in md


def test_empty_area_returns_coherent_zeros():
    row = (
        0, {}, {}, {},
        [],
        0, 0, 0, 0,
        None, None,
        None,
    )
    cur = _FakeCursor(row, _RESULT_COLS)
    conn = _FakeConn(cur)
    res = describe_neighborhood(
        conn, lat=50.0, lng=14.0, radius_m=500,  # type: ignore[arg-type]
    )

    d = res["data"]
    assert d["active_listing_count"] == 0
    assert d["active_listings_per_km2"] == 0.0
    assert d["disposition_mix"] == {}
    assert d["building_type_mix"] == {}
    assert d["condition_mix"] == {}
    assert d["price_stats_by_disposition"] == {}
    assert d["trend"]["new_listings_last_7_days"] == 0
    assert d["data_age"]["oldest_data_age_days"] is None
    assert d["data_age"]["median_data_age_days"] is None
    assert res["metadata"]["data_freshness"] is None
    assert res["metadata"]["result_count"] == 0


def test_unknown_building_type_appears_in_mix():
    row = (
        10,
        {"1+kk": 10},
        {"unknown": 4, "panel": 6},
        {"unknown": 10},
        [],
        0, 0, 0, 0,
        1, 1.0,
        datetime(2026, 5, 2, tzinfo=timezone.utc),
    )
    cur = _FakeCursor(row, _RESULT_COLS)
    conn = _FakeConn(cur)
    res = describe_neighborhood(
        conn, lat=50.0, lng=14.0,  # type: ignore[arg-type]
    )
    bt = res["data"]["building_type_mix"]
    assert bt == {"unknown": 0.4, "panel": 0.6}
    cond = res["data"]["condition_mix"]
    assert cond == {"unknown": 1.0}


def test_hard_cap_note_in_metadata_when_exceeded():
    row = (
        6000,
        {"1+kk": 6000},
        {"panel": 6000},
        {"unknown": 6000},
        [],
        50, 200, 5, 25,
        2, 1.0,
        datetime(2026, 5, 2, tzinfo=timezone.utc),
    )
    cur = _FakeCursor(row, _RESULT_COLS)
    conn = _FakeConn(cur)
    res = describe_neighborhood(
        conn, lat=50.0, lng=14.0, radius_m=5000,  # type: ignore[arg-type]
    )
    md = res["metadata"]
    assert "notes" in md
    assert any("6000" in n and "5000" in n for n in md["notes"])


def test_executes_against_cursor_with_expected_params():
    row = (0, {}, {}, {}, [], 0, 0, 0, 0, None, None, None)
    cur = _FakeCursor(row, _RESULT_COLS)
    conn = _FakeConn(cur)
    describe_neighborhood(
        conn, lat=50.087, lng=14.42, radius_m=1500,  # type: ignore[arg-type]
        max_age_days=14,
    )
    assert cur.executed is not None
    sql, params = cur.executed
    assert "ST_DWithin" in sql
    assert params["lat"] == 50.087
    assert params["lng"] == 14.42
    assert params["radius_m"] == 1500
    assert params["max_age_days"] == 14
    assert params["category_main"] == "byt"
    assert params["category_type"] == "pronajem"
