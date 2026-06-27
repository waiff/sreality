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
        (lambda s: "SELECT id, status, category_type, category_main FROM properties WHERE id IN" in s,
         [(10, "active", "prodej", "byt"), (20, "active", "prodej", "byt")]),
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
        (lambda s: "SELECT id, status, category_type, category_main FROM properties WHERE id IN" in s,
         [(10, "active", "prodej", "byt"), (20, "merged_away", "prodej", "byt")]),
    ])
    with pytest.raises(MergeError):
        merge_properties(
            conn, survivor_id=10, retired_id=20, reason="manual", source="operator",
        )
    # never re-pointed anything
    assert _find(conn.executed, "UPDATE listings SET property_id =") is None


def test_merge_rejects_sale_vs_rent_at_chokepoint():
    # The operator/cluster merge paths call merge_properties directly (bypassing
    # classify_pair); this final guard must refuse a sale↔rental merge.
    conn = _FakeConn([
        (lambda s: "SELECT id, status, category_type, category_main FROM properties WHERE id IN" in s,
         [(10, "active", "prodej", "byt"), (20, "active", "pronajem", "byt")]),
    ])
    with pytest.raises(MergeError):
        merge_properties(
            conn, survivor_id=10, retired_id=20, reason="manual", source="operator",
        )
    assert _find(conn.executed, "UPDATE listings SET property_id =") is None


def test_merge_rejects_byt_vs_dum_at_chokepoint():
    conn = _FakeConn([
        (lambda s: "SELECT id, status, category_type, category_main FROM properties WHERE id IN" in s,
         [(10, "active", "prodej", "byt"), (20, "active", "prodej", "dum")]),
    ])
    with pytest.raises(MergeError):
        merge_properties(
            conn, survivor_id=10, retired_id=20, reason="manual", source="operator",
        )


def test_merge_allows_dum_komercni_cross_type_at_chokepoint():
    # The ONE sanctioned cross-type (a house on one portal, commercial on another, same
    # building) must NOT be refused — the merge proceeds normally past the category guard.
    conn = _FakeConn([
        (lambda s: "SELECT id, status, category_type, category_main FROM properties WHERE id IN" in s,
         [(10, "active", "prodej", "dum"), (20, "active", "prodej", "komercni")]),
        (lambda s: "INSERT INTO property_merge_events" in s, [(1,), (2,)]),
    ])
    result = merge_properties(
        conn, survivor_id=10, retired_id=20, reason="manual", source="operator",
    )
    assert result["data"]["survivor_id"] == 10
    assert _find(conn.executed, "UPDATE listings SET property_id =") is not None


def test_merge_rejects_self_merge():
    conn = _FakeConn([])
    with pytest.raises(MergeError):
        merge_properties(
            conn, survivor_id=7, retired_id=7, reason="manual", source="operator",
        )
    assert conn.executed == []


def test_merge_carries_operator_state_to_survivor():
    # Property-anchored operator state follows the property onto the survivor in
    # the same transaction, so it never orphans onto the merged_away loser.
    conn = _FakeConn([
        (lambda s: "SELECT id, status, category_type, category_main FROM properties WHERE id IN" in s,
         [(10, "active", "prodej", "byt"), (20, "active", "prodej", "byt")]),
        (lambda s: "INSERT INTO property_merge_events" in s, [(1,)]),
    ])

    merge_properties(
        conn, survivor_id=10, retired_id=20, reason="manual", source="operator",
    )

    for tbl in (
        "collection_properties", "property_tags",
        "property_notes", "notification_dispatches",
    ):
        up = _find(conn.executed, f"UPDATE {tbl} SET property_id =")
        assert up is not None, f"{tbl} not re-pointed"
        assert up[1] == {"retired": 20, "survivor": 10}, tbl
    # set tables collision-collapse before re-point
    assert _find(conn.executed, "DELETE FROM notification_dispatches r") is not None
    # the carry happens BEFORE the loser is soft-retired (so no orphan window)
    idx_carry = next(
        i for i, e in enumerate(conn.executed)
        if "UPDATE property_tags SET property_id" in e[0]
    )
    idx_retire = next(
        i for i, e in enumerate(conn.executed) if "status = 'merged_away'" in e[0]
    )
    assert idx_carry < idx_retire


def test_merge_reconciles_pipeline_stage():
    # The single-valued deal-pipeline stage is reconciled (keep most-advanced)
    # in the same merge transaction, before the loser is soft-retired.
    conn = _FakeConn([
        (lambda s: "SELECT id, status, category_type, category_main FROM properties WHERE id IN" in s,
         [(10, "active", "prodej", "byt"), (20, "active", "prodej", "byt")]),
        (lambda s: "INSERT INTO property_merge_events" in s, [(1,)]),
    ])

    merge_properties(
        conn, survivor_id=10, retired_id=20, reason="manual", source="operator",
    )

    keep = _find(conn.executed, "UPDATE property_pipeline s SET stage_id = r.stage_id")
    drop = _find(conn.executed, "DELETE FROM property_pipeline WHERE property_id =")
    assert keep is not None and drop is not None
    idx_pipeline = next(
        i for i, e in enumerate(conn.executed)
        if "DELETE FROM property_pipeline WHERE property_id =" in e[0]
    )
    idx_retire = next(
        i for i, e in enumerate(conn.executed) if "status = 'merged_away'" in e[0]
    )
    assert idx_pipeline < idx_retire


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
    # the reactivated retired property's pipeline card is restored from the ledger
    assert _find(conn.executed, "INSERT INTO property_pipeline") is not None
    assert _find(conn.executed, "merge_absorb") is not None


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
