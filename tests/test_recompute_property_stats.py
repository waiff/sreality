"""Tests for scripts.recompute_property_stats pure helpers.

Hermetic: only the id-batching arithmetic is exercised; the SQL and DB I/O
are verified out-of-band via the Supabase MCP after the migrations apply.
"""

from __future__ import annotations

from typing import Any

from scripts.recompute_property_stats import (
    _attach_stragglers,
    _batch_ranges,
    _drain_dirty,
)


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
        self._rows: list[tuple[Any, ...]] = []
        self.rowcount = 0

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append((s, params))
        for predicate, rows in self._conn.script:
            if predicate(s):
                self._rows = list(rows)
                self.rowcount = len(rows)
                return
        self._rows = []
        self.rowcount = 0

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _FakeConn:
    def __init__(self, script: list[tuple[Any, list[tuple[Any, ...]]]] | None = None) -> None:
        self.script = script or []
        self.executed: list[tuple[str, Any]] = []

    def cursor(self) -> _Cur:
        return _Cur(self)


def _sqls(conn: _FakeConn) -> list[str]:
    return [e[0] for e in conn.executed]


def _find(conn: _FakeConn, needle: str) -> tuple[str, Any] | None:
    return next((e for e in conn.executed if needle in e[0]), None)


def test_attach_stragglers_spatial_link_before_singleton_insert():
    """The deferred Tier-1 match must run BEFORE the singleton insert, so a
    straggler with a single cross-source hit links instead of becoming a
    duplicate singleton."""
    conn = _FakeConn()
    _attach_stragglers(conn)
    order = _sqls(conn)
    spatial = next(i for i, s in enumerate(order) if "ST_DWithin(p.geom, s.geom, 20)" in s)
    insert = next(i for i, s in enumerate(order) if "INSERT INTO properties" in s)
    link = next(i for i, s in enumerate(order) if "p.repr_listing_id = l.sreality_id" in s)
    assert spatial < insert < link
    # The deferred matcher keeps the inline matcher's exact gates.
    spatial_sql = order[spatial]
    assert "BETWEEN s.price_czk * 0.98 AND s.price_czk * 1.02" in spatial_sql
    assert "c.source = s.source" in spatial_sql
    assert "m.hits = 1" in spatial_sql


def test_attach_stragglers_full_runs_native_id_backfill():
    conn = _FakeConn()
    _attach_stragglers(conn)
    assert any("source_id_native = sreality_id::text" in s for s in _sqls(conn))


def test_attach_stragglers_incremental_skips_native_id_backfill():
    """The */5 incremental pass must not scan the whole listings table for the
    one-time native-id backfill; the daily full sweep handles it."""
    conn = _FakeConn()
    _attach_stragglers(conn, skip_native_backfill=True)
    order = _sqls(conn)
    assert not any("source_id_native = sreality_id::text" in s for s in order)
    assert any("ST_DWithin(p.geom, s.geom, 20)" in s for s in order)  # link still runs


def test_attach_stragglers_enqueues_spatially_linked_properties():
    """A straggler that links to an existing property dirties that property."""
    conn = _FakeConn([
        (lambda s: "ST_DWithin(p.geom, s.geom, 20)" in s, [(7,), (7,), (9,)]),
    ])
    _attach_stragglers(conn, skip_native_backfill=True)
    enq = _find(conn, "INSERT INTO dirty_properties")
    assert enq is not None
    sql, params = enq
    assert "unnest(%(ids)s::bigint[])" in sql
    assert "ON CONFLICT (property_id) DO UPDATE SET marked_at = now()" in sql
    assert params == {"ids": [7, 7, 9]}


def test_attach_stragglers_no_enqueue_when_no_links():
    conn = _FakeConn()  # spatial link returns nothing
    _attach_stragglers(conn, skip_native_backfill=True)
    assert _find(conn, "INSERT INTO dirty_properties") is None


class _DrainCur:
    def __init__(self, conn: "_DrainConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_DrainCur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append((s, params))
        if s.startswith("DELETE FROM dirty_properties"):
            self._conn.deleted.append((params["ids"], params["cutoff"]))
            self._rows = []
        elif "SELECT property_id, marked_at FROM dirty_properties" in s:
            self._rows = self._conn.batches.pop(0) if self._conn.batches else []
        elif "WITH batch AS" in s:  # scoped recompute
            self._conn.recomputed.append(params["ids"])
            self._rows = []
        else:
            self._rows = []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _DrainConn:
    def __init__(self, batches: list[list[tuple[Any, ...]]]) -> None:
        self.batches = list(batches)
        self.executed: list[tuple[str, Any]] = []
        self.recomputed: list[list[int]] = []
        self.deleted: list[tuple[list[int], Any]] = []

    def cursor(self) -> _DrainCur:
        return _DrainCur(self)


def test_drain_dirty_recomputes_each_batch_then_terminates():
    conn = _DrainConn([[(7, "t1"), (8, "t1")], [(9, "t2")], []])
    total = _drain_dirty(conn, batch_size=2, cutoff="CUTOFF")
    assert total == 3
    assert conn.recomputed == [[7, 8], [9]]
    # deletes are scoped to the claimed ids and the run cutoff
    assert conn.deleted == [([7, 8], "CUTOFF"), ([9], "CUTOFF")]


def test_drain_dirty_empty_queue_is_noop():
    conn = _DrainConn([[]])
    assert _drain_dirty(conn, 100, "C") == 0
    assert conn.recomputed == []
    assert conn.deleted == []
