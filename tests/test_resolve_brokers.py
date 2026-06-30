"""Tests for scripts.resolve_brokers pure helpers.

Hermetic: only the keyset id-paging is exercised with a fake cursor; the SQL and
DB I/O are verified out-of-band via the Supabase MCP / the Actions full sweep.
The keyset chunker replaced an unbounded ``SELECT ... ORDER BY sreality_id`` that
crossed the pooler's 2-min statement timeout once four portals were attributed,
so these tests assert that EVERY page stays bounded (the regression guard).
"""

from __future__ import annotations

from typing import Any

from scripts.resolve_brokers import _BROKER_SOURCES, _broker_bearing_ids


class _KeysetCur:
    """Simulates a keyset scan over a fixed ascending id universe.

    Honours the ``sreality_id > :last`` lower bound and the ``LIMIT :lim`` page
    size, so the helper's pagination logic is exercised exactly as in Postgres.
    """

    def __init__(self, conn: "_KeysetConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_KeysetCur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append((s, params))
        last = params.get("last")
        lim = params["lim"]
        ge = [i for i in self._conn.universe if last is None or i > last]
        self._rows = [(i,) for i in ge[:lim]]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _KeysetConn:
    def __init__(self, universe: list[int]) -> None:
        self.universe = sorted(universe)
        self.executed: list[tuple[str, Any]] = []

    def cursor(self) -> _KeysetCur:
        return _KeysetCur(self)


def test_keyset_returns_every_id_in_order():
    universe = [-287340, -5, -1, 7, 42, 1000, 4294963276]
    conn = _KeysetConn(universe)
    assert _broker_bearing_ids(conn, page_size=2) == universe


def test_keyset_threads_last_id_across_pages():
    conn = _KeysetConn([10, 20, 30, 40, 50])
    _broker_bearing_ids(conn, page_size=2)
    lasts = [p.get("last") for _, p in conn.executed]
    # first page has no lower bound; each subsequent page resumes after the prior
    # page's last id (keyset, not OFFSET).
    assert lasts == [None, 20, 40]


def test_every_page_is_bounded_no_unbounded_scan():
    """The bug was one unbounded ``ORDER BY`` scan. Every issued statement must
    carry a LIMIT and never the old inline ``source IN (...)`` literal."""
    conn = _KeysetConn(list(range(1, 51)))
    _broker_bearing_ids(conn, page_size=10)
    for sql, params in conn.executed:
        assert "LIMIT %(lim)s" in sql
        assert "source = ANY(%(srcs)s)" in sql
        assert "source IN (" not in sql
        assert params["srcs"] == list(_BROKER_SOURCES)


def test_keyset_terminates_on_exact_multiple():
    # A full final page is followed by one empty page that stops the loop.
    conn = _KeysetConn([1, 2, 3, 4])
    assert _broker_bearing_ids(conn, page_size=2) == [1, 2, 3, 4]
    # 2 full pages + 1 empty terminator = 3 statements.
    assert len(conn.executed) == 3


def test_keyset_terminates_on_short_page():
    # A short final page stops the loop without an extra empty query.
    conn = _KeysetConn([1, 2, 3])
    assert _broker_bearing_ids(conn, page_size=2) == [1, 2, 3]
    assert len(conn.executed) == 2


def test_keyset_empty_universe_is_single_query():
    conn = _KeysetConn([])
    assert _broker_bearing_ids(conn, page_size=100) == []
    assert len(conn.executed) == 1
