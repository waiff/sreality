"""Tests for scripts.recompute_property_stats pure helpers.

Hermetic: only the id-batching arithmetic is exercised; the SQL and DB I/O
are verified out-of-band via the Supabase MCP after the migrations apply.
"""

from __future__ import annotations

from typing import Any

from scripts.recompute_property_stats import _attach_stragglers, _batch_ranges


def test_empty_when_no_properties():
    assert list(_batch_ranges(0, 2000)) == []


def test_invalid_batch_size_yields_nothing():
    assert list(_batch_ranges(100, 0)) == []


def test_half_open_ranges_cover_exact_multiple():
    assert list(_batch_ranges(4, 2)) == [(1, 3), (3, 5)]


def test_last_range_overshoots_to_cover_remainder():
    assert list(_batch_ranges(5, 2)) == [(1, 3), (3, 5), (5, 7)]


def test_every_id_lands_in_exactly_one_range():
    max_id, batch = 71_556, 2000
    seen = 0
    for lo, hi in _batch_ranges(max_id, batch):
        # half-open [lo, hi); count the ids in [lo, min(hi-1, max_id)]
        seen += min(hi - 1, max_id) - lo + 1
    assert seen == max_id


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self.rowcount = 0

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.executed.append(" ".join(sql.split()))
        self.rowcount = 0


class _FakeConn:
    def __init__(self) -> None:
        self.executed: list[str] = []

    def cursor(self) -> _Cur:
        return _Cur(self)


def test_attach_stragglers_spatial_link_before_singleton_insert():
    """The deferred Tier-1 match must run BEFORE the singleton insert, so a
    straggler with a single cross-source hit links instead of becoming a
    duplicate singleton."""
    conn = _FakeConn()
    _attach_stragglers(conn)
    order = conn.executed
    spatial = next(i for i, s in enumerate(order) if "ST_DWithin(p.geom, s.geom, 20)" in s)
    insert = next(i for i, s in enumerate(order) if "INSERT INTO properties" in s)
    link = next(i for i, s in enumerate(order) if "p.repr_listing_id = l.sreality_id" in s)
    assert spatial < insert < link
    # The deferred matcher keeps the inline matcher's exact gates.
    spatial_sql = order[spatial]
    assert "BETWEEN s.price_czk * 0.98 AND s.price_czk * 1.02" in spatial_sql
    assert "c.source = s.source" in spatial_sql
    assert "m.hits = 1" in spatial_sql
