"""Hermetic tests for the property merge/unmerge core (toolkit.property_identity).

A scripted fake connection records every executed statement so the test can
assert the merge/unmerge transaction emitted the right SQL in the right shape.
The spatial/recompute SQL itself is verified out-of-band via the Supabase MCP;
here we only check control flow + the statements the functions emit.
"""

from __future__ import annotations

from typing import Any

import pytest

from toolkit.property_identity import MergeError, merge_properties, unmerge_group


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
    def __init__(self, script: list[tuple[Any, list[tuple[Any, ...]]]]) -> None:
        self.script = script
        self.executed: list[tuple[str, Any]] = []

    def transaction(self) -> _Ctx:
        return _Ctx()

    def cursor(self) -> _Cur:
        return _Cur(self)


def _find(executions, needle: str) -> tuple[str, Any] | None:
    return next((e for e in executions if needle in e[0]), None)


# --- merge_properties -----------------------------------------------------


def test_merge_repoints_retires_logs_and_recomputes():
    conn = _FakeConn([
        (lambda s: "SELECT id, status FROM properties WHERE id IN" in s,
         [(10, "active"), (20, "active")]),
        (lambda s: "INSERT INTO property_merge_events" in s, [(1,), (2,)]),
    ])

    result = merge_properties(
        conn, survivor_id=10, retired_id=20, reason="manual", source="operator",
    )

    assert result["data"]["survivor_id"] == 10
    assert result["data"]["retired_id"] == 20
    assert result["data"]["listings_moved"] == 2
    assert result["data"]["merge_group_id"]  # a uuid was generated

    # children re-pointed onto the survivor
    repoint = _find(conn.executed, "UPDATE listings SET property_id =")
    assert repoint is not None and repoint[1] == (10, 20)
    # loser soft-retired, never deleted
    assert _find(conn.executed, "status = 'merged_away'") is not None
    assert _find(conn.executed, "DELETE FROM properties") is None
    # candidate marked merged + survivor recomputed inline
    assert _find(conn.executed, "property_identity_candidates") is not None
    assert _find(conn.executed, "WITH batch AS") is not None


def test_merge_rejects_when_retired_not_active():
    conn = _FakeConn([
        (lambda s: "SELECT id, status FROM properties WHERE id IN" in s,
         [(10, "active"), (20, "merged_away")]),
    ])
    with pytest.raises(MergeError):
        merge_properties(
            conn, survivor_id=10, retired_id=20, reason="manual", source="operator",
        )
    # never re-pointed anything
    assert _find(conn.executed, "UPDATE listings SET property_id =") is None


def test_merge_rejects_self_merge():
    conn = _FakeConn([])
    with pytest.raises(MergeError):
        merge_properties(
            conn, survivor_id=7, retired_id=7, reason="manual", source="operator",
        )
    assert conn.executed == []


# --- unmerge_group --------------------------------------------------------


def test_unmerge_replays_ledger_and_reactivates():
    conn = _FakeConn([
        (lambda s: "FROM property_merge_events WHERE merge_group_id" in s,
         [(10, 20, 1001), (10, 20, 1002)]),
        (lambda s: "UPDATE listings SET property_id = %s WHERE sreality_id" in s,
         [(1,)]),  # each replay re-points exactly one child
    ])

    result = unmerge_group(conn, merge_group_id="grp", undone_by="operator")

    assert result["data"]["survivor_id"] == 10
    assert result["data"]["retired_ids"] == [20]
    assert result["data"]["listings_moved_back"] == 2
    assert result["data"]["conflicts"] == []
    # retired reactivated, events stamped undone, candidate re-opened, recompute ran
    assert _find(conn.executed, "status = 'active'") is not None
    assert _find(conn.executed, "undone_at = now()") is not None
    assert _find(conn.executed, "SET status = 'proposed'") is not None
    assert _find(conn.executed, "WITH batch AS") is not None


def test_unmerge_conflict_when_child_repointed_elsewhere():
    conn = _FakeConn([
        (lambda s: "FROM property_merge_events WHERE merge_group_id" in s,
         [(10, 20, 1001)]),
        # re-point UPDATE matches nothing (child no longer on survivor) -> rowcount 0
    ])

    result = unmerge_group(conn, merge_group_id="grp", undone_by="operator")

    assert result["data"]["listings_moved_back"] == 0
    assert result["data"]["conflicts"] == [1001]


def test_unmerge_raises_when_no_active_events():
    conn = _FakeConn([])
    with pytest.raises(MergeError):
        unmerge_group(conn, merge_group_id="grp", undone_by="operator")
