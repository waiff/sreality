"""Tests for scripts.recompute_property_stats pure helpers.

Hermetic: the id-batching arithmetic, the fake-conn execution order, and the
static validity of every SQL constant's `%`-placeholders are exercised here; the
SQL's runtime semantics + DB I/O are verified out-of-band via the Supabase MCP
after the migrations apply.
"""

from __future__ import annotations

from typing import Any

import pytest

from scripts.recompute_property_stats import (
    _attach_stragglers,
    _batch_ranges,
    _drain_dirty,
    _publish_sweep,
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


def test_attach_stragglers_singletons_only_no_spatial_link():
    """Stragglers become singletons; the old geo spatial-link step is gone.

    Matching is the out-of-band street+disposition dedup engine's job, so
    attach must NOT run any ST_DWithin probe or enqueue dirty_properties — it
    only inserts a singleton per unlinked listing and links it.
    """
    conn = _FakeConn()
    _attach_stragglers(conn)
    order = _sqls(conn)
    insert = next(i for i, s in enumerate(order) if "INSERT INTO properties" in s)
    link = next(i for i, s in enumerate(order) if "p.repr_listing_id = l.sreality_id" in s)
    assert insert < link
    assert not any("ST_DWithin" in s for s in order)
    assert not any("INSERT INTO dirty_properties" in s for s in order)


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
    # still inserts singletons even when the backfill is skipped
    assert any("INSERT INTO properties" in s for s in order)


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


def test_publish_sweep_only_touches_unpublished_ineligible():
    """The ineligible publish sweep (migration 273) publishes ONLY unpublished, active
    properties whose repr listing is eligible for NEITHER dedup pass — an
    eligible-but-unchecked property stays NULL (the engine stamps that one), and an
    already-published row is never touched. Row-level semantics run in the DB; here we
    pin the SQL shape that guarantees them + the returned rowcount."""
    from toolkit.publication import GEO_ELIGIBLE_PREDICATE, STREET_ELIGIBLE_PREDICATE

    conn = _FakeConn([
        (lambda s: "publish_reason = 'ineligible'" in s, [(1,), (2,)]),  # 2 rows published
    ])
    assert _publish_sweep(conn) == 2

    sweep = _find(conn, "publish_reason = 'ineligible'")
    assert sweep is not None
    sql = " ".join(sweep[0].split())
    assert "p.published_at IS NULL" in sql          # never re-publishes a stamped row
    assert "p.status = 'active'" in sql
    # BOTH eligibility predicates, wrapped IS NOT TRUE (NULL-safe ineligibility) so an
    # eligible repr listing keeps the property NULL for the engine to stamp.
    assert " ".join(STREET_ELIGIBLE_PREDICATE.split()) in sql
    assert " ".join(GEO_ELIGIBLE_PREDICATE.split()) in sql
    assert "IS NOT TRUE" in sql


def test_publication_predicates_parity_with_engine():
    """toolkit.publication mirrors the engine's eligibility VERBATIM (single source), so
    the ineligible sweep can never publish a property the engine WOULD dedup-check. A
    drift in either predicate fails here."""
    import scripts.dedup_engine as eng

    from toolkit.publication import GEO_ELIGIBLE_PREDICATE, STREET_ELIGIBLE_PREDICATE

    assert STREET_ELIGIBLE_PREDICATE == eng._ELIGIBILITY
    assert STREET_ELIGIBLE_PREDICATE in eng._ELIGIBLE_SQL
    assert GEO_ELIGIBLE_PREDICATE in eng._GEO_ELIGIBLE_SQL


def test_every_resolved_sql_constant_has_valid_placeholders():
    """All `*_SQL` attributes — including the `.replace()`-derived executors —
    must pass psycopg's placeholder parser.

    The fakes above record SQL without parsing it (which is why a prose `~2%` in
    `_RECOMPUTE_BATCH_SQL` once shipped green and broke property maintenance +
    every merge). This module is uniquely exposed: `_RECOMPUTE_ONE_SQL` and
    `_RECOMPUTE_SCOPED_SQL` are derived from `_RECOMPUTE_BATCH_SQL` at import
    time, so they can't be statically inspected — only validated after they
    resolve. The repo-wide AST guard (tests/test_sql_placeholders.py) covers the
    base constants; this covers the derived family that actually executes.
    """
    import scripts.recompute_property_stats as rps

    split = pytest.importorskip("psycopg._queries")._split_query
    names = [n for n in dir(rps) if n.endswith("_SQL") and isinstance(getattr(rps, n), str)]
    assert {"_RECOMPUTE_BATCH_SQL", "_RECOMPUTE_ONE_SQL", "_RECOMPUTE_SCOPED_SQL"} <= set(names)
    for name in names:
        split(getattr(rps, name).encode())  # raises ProgrammingError on a bad `%`
