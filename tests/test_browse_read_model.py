"""Tests for the Browse read-model patch (toolkit.browse_read_model).

Hermetic: a fake conn records executed SQL (and can be told to raise) so the
DELETE+INSERT shape, the dedup/order of ids, the empty no-op, and the
must-never-abort-the-caller swallow behaviour are all exercised without a DB.

Who patches is asserted by behaviour, where each writer runs: the after-step
(`properties_changed`) in tests/test_recompute_property_stats.py, the identity writers'
`db.browse` in tests/test_property_merge_set.py and tests/test_detach_listing.py.
"""

from __future__ import annotations

from typing import Any

import psycopg

from toolkit.browse_read_model import sync_browse_list


class _Ctx:
    def __enter__(self) -> "_Ctx":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append((s, params))
        if self._conn.raise_on and self._conn.raise_on in s:
            raise psycopg.OperationalError("simulated read-model failure")


class _FakeConn:
    def __init__(self, raise_on: str | None = None) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.raise_on = raise_on

    def transaction(self) -> _Ctx:
        return _Ctx()

    def cursor(self) -> _Cur:
        return _Cur(self)


def test_emits_delete_then_insert_for_the_ids():
    conn = _FakeConn()
    sync_browse_list(conn, [10, 20])
    assert conn.executed[0][0].startswith("DELETE FROM browse_list")
    assert conn.executed[0][1] == ([10, 20],)
    # INSERT re-materializes FROM the shared projection (single column contract).
    assert "INSERT INTO browse_list SELECT * FROM browse_projection" in conn.executed[1][0]
    assert conn.executed[1][1] == ([10, 20],)


def test_dedups_and_preserves_order():
    conn = _FakeConn()
    sync_browse_list(conn, [20, 20, 10, 10])
    assert conn.executed[0][1] == ([20, 10],)


def test_noop_on_empty():
    conn = _FakeConn()
    sync_browse_list(conn, [])
    assert conn.executed == []


def test_swallows_db_error_so_the_caller_write_still_commits():
    # browse_list is a disposable cache; a patch failure must be logged and
    # swallowed, never propagated to abort the merge/link it rides with.
    conn = _FakeConn(raise_on="INSERT INTO browse_list")
    sync_browse_list(conn, [10])  # must NOT raise
