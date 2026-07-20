"""Every is_active write site must maintain listings.inactive_at (migration 175).

Flips to false stamp inactive_at = now(); reactivations clear it to NULL —
otherwise the delisting-latency health check (migration 176) reads garbage.
Hermetic: a scripted fake conn records every executed statement so the tests
assert the SQL text, same pattern as test_db_property.
"""

from __future__ import annotations

from typing import Any

from scraper import db


class _Ctx:
    def __enter__(self) -> "_Ctx":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


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

    def transaction(self) -> _Ctx:
        return _Ctx()

    def cursor(self) -> _Cur:
        return _Cur(self)


def _find(executions, needle: str) -> tuple[str, Any] | None:
    return next((e for e in executions if needle in e[0]), None)


# --- flips to false stamp the delisting moment -----------------------------


def test_mark_inactive_stamps_inactive_at():
    conn = _FakeConn()
    db.mark_inactive(conn, "byt", "prodej", {1, 2})
    flip = _find(conn.executed, "SET is_active = false")
    assert flip is not None
    assert "inactive_at = now()" in flip[0]


def test_mark_inactive_native_stamps_inactive_at():
    conn = _FakeConn()
    db.mark_inactive_native(conn, "bazos", "byt", "prodej", {"a", "b"})
    flip = _find(conn.executed, "SET is_active = false")
    assert flip is not None
    assert "inactive_at = now()" in flip[0]


def test_mark_listing_inactive_stamps_inactive_at():
    conn = _FakeConn()
    db.mark_listing_inactive(conn, 999)
    flip = _find(conn.executed, "SET is_active = false")
    assert flip is not None
    assert "inactive_at = now()" in flip[0]


def test_mark_listing_inactive_native_stamps_inactive_at():
    conn = _FakeConn()
    db.mark_listing_inactive_native(conn, "bazos", "12345")
    flip = _find(conn.executed, "SET is_active = false")
    assert flip is not None
    assert "inactive_at = now()" in flip[0]


# --- reactivations clear the stamp ------------------------------------------


def test_touch_listings_clears_inactive_at_in_both_statements():
    conn = _FakeConn()
    db.touch_listings(conn, [1, 2])
    react = _find(conn.executed, "WITH react AS")
    assert react is not None and "inactive_at = NULL" in react[0]
    bulk = _find(conn.executed, "SET last_seen_at = now(), is_active = true")
    assert bulk is not None and "inactive_at = NULL" in bulk[0]


def test_upsert_listing_clears_inactive_at_on_conflict():
    conn = _FakeConn([
        # RETURNING is (inserted, id) since the R2 dual-write: the surrogate is read
        # back in-transaction so the snapshot insert can carry it.
        (lambda s: "INSERT INTO listings" in s, [(False, 12345)]),
        (lambda s: "SELECT content_hash FROM listing_snapshots" in s, []),
    ])
    db.upsert_listing(conn, {"sreality_id": 1}, {}, "h")
    upsert = _find(conn.executed, "INSERT INTO listings")
    assert upsert is not None
    assert "is_active = true" in upsert[0]
    assert "inactive_at = NULL" in upsert[0]


def test_batch_upsert_sql_clears_inactive_at():
    assert "inactive_at = NULL" in db._BATCH_UPSERT_SQL
