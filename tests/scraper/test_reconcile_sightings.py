"""The index walk's ONE sighting diff (rules #3/#4/#19): `portal_runner.reconcile_sightings`
plus the two db reads it and the drain dry-run lean on. Hermetic: the db functions are
patched through `portal_runner.db`, the same module object every adapter's `db` is."""

from __future__ import annotations

from typing import Any

from scraper import db


class _Cursor:
    def __init__(self, rows_for: Any) -> None:
        self.rows_for = rows_for
        self.executed: list[tuple[str, Any]] = []
        self._rows: list[tuple] = []

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))
        self._rows = self.rows_for(sql, params)

    def fetchall(self) -> list[tuple]:
        return self._rows


class _CursorConn:
    def __init__(self, rows_for: Any) -> None:
        self.cur = _Cursor(rows_for)

    def cursor(self) -> _Cursor:
        return self.cur


# --- db.index_summary_native / db.claimable_counts ---------------------------


def test_index_summary_native_chunks_at_5000() -> None:
    def rows_for(_sql: str, params: Any) -> list[tuple]:
        _source, ids = params
        return [(n, int(n), None, 100, None) for n in ids if int(n) % 1000 == 0]

    conn = _CursorConn(rows_for)
    ids = [str(i) for i in range(12_001)] + ["0", "5000", "12000"]
    out = db.index_summary_native(conn, "ceskereality", ids)

    executed = conn.cur.executed
    assert len(executed) == 3
    assert [len(p[1]) for _, p in executed] == [5000, 5000, 2001]
    assert all(p[0] == "ceskereality" for _, p in executed)
    assert sorted(out, key=int) == [str(i) for i in range(0, 12_001, 1000)]
    assert out["5000"] == {"id": 5000, "sreality_id": None, "price_czk": 100, "last_seen_at": None}


def test_index_summary_native_empty_makes_no_statement() -> None:
    conn = _CursorConn(lambda *_a: [])
    assert db.index_summary_native(conn, "remax", []) == {}
    assert conn.cur.executed == []


def test_claimable_counts_all_sources_and_one() -> None:
    def rows_for(_sql: str, params: Any) -> list[tuple]:
        if params is None:
            return [("bazos", 3), ("remax", 7)]
        return [(params[0], 4)]

    conn = _CursorConn(rows_for)
    assert db.claimable_counts(conn) == {"bazos": 3, "remax": 7}
    assert db.claimable_counts(conn, "idnes") == {"idnes": 4}
    (all_sql, all_params), (one_sql, one_params) = conn.cur.executed
    assert all_params is None and "GROUP BY source" in all_sql
    assert one_params == ("idnes",) and "source = %s" in one_sql
