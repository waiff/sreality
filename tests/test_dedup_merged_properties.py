"""Tests for the /dedup/merged-properties audit browse:
api.property_dedup.list_merged_properties + its WHERE builder
(_merged_property_filters). Hermetic: a scripted fake conn, no DB.
"""

from __future__ import annotations

from typing import Any

import api.property_dedup as dedup
from api.property_dedup import _merged_property_filters


# --- _merged_property_filters (pure) ---------------------------------------

def test_filters_default_floor_is_active_survivors_min_only() -> None:
    where, params = _merged_property_filters(
        min_listings=2, max_listings=None, category_main=None,
    )
    assert where.startswith("WHERE ")
    assert "p.status = 'active'" in where
    assert "p.source_count >= %(min_listings)s" in where
    assert "p.source_count <= " not in where  # no cap
    assert "category_main" not in where
    assert params == {"min_listings": 2}


def test_filters_max_binds_upper_bound() -> None:
    where, params = _merged_property_filters(
        min_listings=5, max_listings=10, category_main=None,
    )
    assert "p.source_count >= %(min_listings)s" in where
    assert "p.source_count <= %(max_listings)s" in where
    assert params == {"min_listings": 5, "max_listings": 10}


def test_filters_category_is_plain_equality_not_either_side() -> None:
    # A merged property carries ONE category_main (the survivor's) — plain
    # equality, unlike a candidate PAIR which matches on EITHER side (rule #15).
    where, params = _merged_property_filters(
        min_listings=2, max_listings=None, category_main="byt",
    )
    assert "p.category_main = %(category_main)s" in where
    assert " OR " not in where
    assert params["category_main"] == "byt"


def test_filters_blank_category_omits_the_clause() -> None:
    for empty in (None, ""):
        where, params = _merged_property_filters(
            min_listings=2, max_listings=None, category_main=empty,
        )
        assert "category_main" not in where
        assert "category_main" not in params


# --- fake conn --------------------------------------------------------------

class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append((s, params))
        if "count(*) FROM properties p" in s:
            self._rows = [(self._conn.total,)]
        elif "ORDER BY p.source_count DESC" in s:
            self._rows = list(self._conn.page_rows)
        else:
            self._rows = []

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _FakeConn:
    def __init__(self, *, total: int = 0, page_rows=None) -> None:
        self.total = total
        self.page_rows = page_rows or []
        self.executed: list[tuple[str, Any]] = []

    def cursor(self) -> _Cur:
        return _Cur(self)


def _merged_row(pid: int, source_count: int, *, active: int, sources) -> tuple[Any, ...]:
    # 16 columns matching list_merged_properties' SELECT. `distinct_site_count`
    # (col 3) is its OWN properties column, independent of the agg `sources` array
    # (col 14) — which is NULL when a property has no children.
    return (
        pid, pid * 10, source_count, len(sources or []), "byt", "prodej", "2+kk",
        68.0, None, 4_200_000, "Praha 5", "Kábrova", "2026-06-01T00:00:00Z",
        "2026-06-18T00:00:00Z", sources, active,
    )


def test_total_is_real_count_not_page_size() -> None:
    # 2 rows on the page, but the COUNT says 137 — the UI must see the true total.
    conn = _FakeConn(
        total=137,
        page_rows=[
            _merged_row(1, 6, active=4, sources=["bazos", "sreality"]),
            _merged_row(2, 5, active=5, sources=["idnes", "remax", "sreality"]),
        ],
    )
    out = dedup.list_merged_properties(conn, min_listings=5, max_listings=10, limit=2)
    assert out["total"] == 137
    assert out["returned"] == 2
    assert len(out["data"]) == 2


def test_row_shape_counts_sources_and_floats() -> None:
    conn = _FakeConn(total=1, page_rows=[
        _merged_row(7, 6, active=4, sources=["bazos", "sreality"]),
    ])
    row = dedup.list_merged_properties(conn)["data"][0]
    assert row["property_id"] == 7
    assert row["source_count"] == 6
    assert row["active_count"] == 4
    assert row["distinct_site_count"] == 2
    assert row["sources"] == ["bazos", "sreality"]
    assert row["area_m2"] == 68.0 and isinstance(row["area_m2"], float)
    assert row["estate_area"] is None
    assert row["price_czk"] == 4_200_000


def test_null_sources_becomes_empty_list() -> None:
    # array_agg over zero children is NULL — must surface as [] not None.
    conn = _FakeConn(total=1, page_rows=[_merged_row(3, 2, active=2, sources=None)])
    assert dedup.list_merged_properties(conn)["data"][0]["sources"] == []


def test_count_and_page_share_the_same_filter() -> None:
    # Regression guard (mirrors list_candidates): the COUNT and the page SELECT
    # must carry the identical WHERE, or the page total drifts from the rows.
    conn = _FakeConn(total=3, page_rows=[_merged_row(1, 8, active=8, sources=["sreality"])])
    dedup.list_merged_properties(
        conn, min_listings=5, max_listings=10, category_main="byt",
    )
    sqls = [s for s, _ in conn.executed]
    assert len(sqls) == 2  # exactly one COUNT + one page SELECT
    for s in sqls:
        assert "p.status = 'active'" in s
        assert "p.source_count >= %(min_listings)s" in s
        assert "p.source_count <= %(max_listings)s" in s
        assert "p.category_main = %(category_main)s" in s
    for _, p in conn.executed:
        assert p["min_listings"] == 5
        assert p["max_listings"] == 10
        assert p["category_main"] == "byt"


def test_page_orders_biggest_first_and_rolls_up_children() -> None:
    conn = _FakeConn(total=1, page_rows=[_merged_row(1, 9, active=9, sources=["sreality"])])
    dedup.list_merged_properties(conn)
    page_sql = next(s for s in (x for x, _ in conn.executed)
                    if "ORDER BY p.source_count DESC" in s)
    assert "ORDER BY p.source_count DESC, p.id DESC" in page_sql
    assert "LEFT JOIN LATERAL" in page_sql
    assert "count(*) FILTER (WHERE l.is_active)" in page_sql
