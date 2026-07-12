"""Hermetic tests for split_property_to_singletons (toolkit.property_identity).

A scripted fake connection records every executed statement so the test can
assert the re-singletonize transaction emitted the right SQL in the right shape.
The recompute SQL itself is verified out-of-band via the Supabase MCP; here we
only check control flow + the statements the function emits.
"""

from __future__ import annotations

from typing import Any, Callable

import pytest

from toolkit.property_identity import MergeError, split_property_to_singletons


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
                r = rows() if callable(rows) else rows
                self._rows = list(r)
                self.rowcount = len(self._rows)
                return
        self._rows = []
        self.rowcount = 0

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _FakeConn:
    def __init__(self, script: list[tuple[Callable[[str], bool], Any]]) -> None:
        self.script = script
        self.executed: list[tuple[str, Any]] = []

    def transaction(self) -> _Ctx:
        return _Ctx()

    def cursor(self) -> _Cur:
        return _Cur(self)


def _find(executions, needle: str) -> tuple[str, Any] | None:
    return next((e for e in executions if needle in e[0]), None)


def _find_all(executions, needle: str) -> list[tuple[str, Any]]:
    return [e for e in executions if needle in e[0]]


def _id_sequence(*ids: int) -> Callable[[], list[tuple[int]]]:
    it = iter(ids)
    return lambda: [(next(it),)]


def test_split_detaches_all_but_anchor_and_recomputes():
    conn = _FakeConn([
        (lambda s: "FROM properties WHERE id = %s FOR UPDATE" in s, [(50, "active")]),
        (lambda s: "SELECT sreality_id FROM listings WHERE property_id" in s,
         [(1001,), (1002,), (1003,)]),
        (lambda s: "INSERT INTO properties" in s, _id_sequence(9001, 9002)),
        (lambda s: "WITH batch AS" in s, []),
    ])

    res = split_property_to_singletons(conn, property_id=50)

    d = res["data"]
    assert d["property_id"] == 50
    # First child in repr order stays; the rest detach onto fresh singletons.
    assert d["anchor_listing_id"] == 1001
    assert d["detached_listing_ids"] == [1002, 1003]
    assert d["new_property_ids"] == [9001, 9002]

    # one INSERT + one re-point per detached child, in order
    assert len(_find_all(conn.executed, "INSERT INTO properties")) == 2
    repoints = _find_all(conn.executed, "UPDATE listings SET property_id =")
    assert [e[1] for e in repoints] == [(9001, 1002), (9002, 1003)]
    # survivor recomputed inline; no DURABLE row is ever deleted (rule #3 —
    # history is sacred). The only DELETE is the disposable browse_list read-model
    # cache patch (toolkit.browse_read_model), rebuilt wholesale every 5 min.
    assert _find(conn.executed, "WITH batch AS") is not None
    assert _find(conn.executed, "DELETE FROM properties") is None
    assert _find(conn.executed, "DELETE FROM listings") is None


def test_split_is_noop_for_single_child():
    conn = _FakeConn([
        (lambda s: "FROM properties WHERE id = %s FOR UPDATE" in s, [(50, "active")]),
        (lambda s: "SELECT sreality_id FROM listings WHERE property_id" in s, [(1001,)]),
    ])

    res = split_property_to_singletons(conn, property_id=50)

    assert res["data"]["anchor_listing_id"] == 1001
    assert res["data"]["detached_listing_ids"] == []
    assert res["data"]["new_property_ids"] == []
    # nothing detached, no new property, no recompute
    assert _find(conn.executed, "INSERT INTO properties") is None
    assert _find(conn.executed, "UPDATE listings SET property_id =") is None
    assert _find(conn.executed, "WITH batch AS") is None


def test_split_raises_when_property_not_active():
    conn = _FakeConn([
        (lambda s: "FROM properties WHERE id = %s FOR UPDATE" in s,
         [(50, "merged_away")]),
    ])
    with pytest.raises(MergeError):
        split_property_to_singletons(conn, property_id=50)
    assert _find(conn.executed, "UPDATE listings SET property_id =") is None


def test_split_raises_when_property_not_found():
    conn = _FakeConn([
        (lambda s: "FROM properties WHERE id = %s FOR UPDATE" in s, []),
    ])
    with pytest.raises(MergeError):
        split_property_to_singletons(conn, property_id=50)
    assert _find(conn.executed, "INSERT INTO properties") is None
