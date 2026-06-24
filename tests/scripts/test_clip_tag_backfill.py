"""_select_pending: SET-LOCAL is a literal (not a bound param) + priority→global drain.

Regression guard for the SyntaxError that stalled every clip_tag shard: `SET` is a
PostgreSQL utility statement and cannot take a bound parameter ($1), so the timeout must
be interpolated, never passed as %s.
"""

from __future__ import annotations

from typing import Any

from scripts import clip_tag_backfill as ctb


class _Cur:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append((s, params))
        if s.startswith("SET LOCAL"):
            self._rows = []
        elif isinstance(params, dict) and "region" in params:
            self._rows = list(self._conn.region_rows.get(params["region"], []))
        elif "FROM images i" in s:  # the global fallback select
            self._rows = list(self._conn.global_rows)
        else:
            self._rows = []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _Txn:
    def __enter__(self) -> "_Txn":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class _Conn:
    def __init__(self, region_rows: dict | None = None,
                 global_rows: list | None = None) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.region_rows = region_rows or {}
        self.global_rows = global_rows or []

    def transaction(self) -> "_Txn":
        return _Txn()

    def cursor(self) -> "_Cur":
        return _Cur(self)


def test_set_local_statement_timeout_is_literal_not_parameterized() -> None:
    conn = _Conn(global_rows=[(1, "k", True)])
    ctb._select_pending(conn, limit=10, shards=1, shard=0, priority_regions=[])
    set_stmts = [(s, p) for s, p in conn.executed if s.startswith("SET LOCAL")]
    assert len(set_stmts) == 1
    sql, params = set_stmts[0]
    assert params is None  # SET can't bind a param
    assert "%s" not in sql and "$1" not in sql
    assert str(ctb.SELECT_TIMEOUT_MS) in sql


def test_drains_priority_region_then_global() -> None:
    conn = _Conn(
        region_rows={19: [(1, "a", True), (2, "b", False)]},
        global_rows=[(3, "c", True)],
    )
    rows, phase = ctb._select_pending(
        conn, limit=10, shards=1, shard=0, priority_regions=[19])
    assert [r[0] for r in rows] == [1, 2, 3]  # region 19 first, then global
    assert "r19:2" in phase and "global:1" in phase


def test_priority_filling_budget_skips_global() -> None:
    conn = _Conn(
        region_rows={19: [(1, "a", True), (2, "b", True)]},
        global_rows=[(9, "x", True)],
    )
    rows, phase = ctb._select_pending(
        conn, limit=2, shards=1, shard=0, priority_regions=[19])
    assert [r[0] for r in rows] == [1, 2]
    assert "global" not in phase  # budget filled by the priority region
