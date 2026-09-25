"""Hermetic tests for THE chokepoint, `toolkit.property_identity.merge_properties`.

A scripted fake connection records every executed statement so the test can
assert the pairwise merge emitted the right SQL in the right shape. The set merge
around it (survivor rule, one recompute, rulings) is tests/test_property_merge_set.py;
the one undo is tests/test_detach_listing.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from toolkit.property_identity import AssetLinkConflict, MergeError, merge_properties


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


def test_merge_repoints_retires_and_logs_and_leaves_the_recompute_to_the_set():
    conn = _FakeConn([
        (lambda s: "SELECT id, status, category_type, category_main, asset_id FROM properties WHERE id IN" in s,
         [(10, "active", "prodej", "byt", None), (20, "active", "prodej", "byt", None)]),
        (lambda s: "INSERT INTO property_merge_events" in s, [(1,), (2,)]),
    ])

    assert merge_properties(
        conn, survivor_id=10, retired_id=20, reason="manual", source="operator",
    ) == 2
    assert _find(conn.executed, "INSERT INTO property_merge_events")[1]["group"]  # a uuid

    # children re-pointed onto the survivor
    repoint = _find(conn.executed, "UPDATE listings SET property_id =")
    assert repoint is not None and repoint[1] == (10, 20)
    # loser soft-retired, never deleted
    assert _find(conn.executed, "status = 'merged_away'") is not None
    assert _find(conn.executed, "DELETE FROM properties") is None
    # one pair of a set: the survivor is recomputed once, by merge_property_set
    assert _find(conn.executed, "WITH batch AS") is None
    assert _find(conn.executed, "browse_list") is None


def test_merge_carries_the_one_asset_link_onto_the_survivor():
    """Decision 17: the link never stays on the merged_away row (the census's old gap)."""
    conn = _FakeConn([
        (lambda s: "SELECT id, status, category_type, category_main, asset_id FROM properties WHERE id IN" in s,
         [(10, "active", "prodej", "byt", None), (20, "active", "prodej", "byt", 7)]),
        (lambda s: "INSERT INTO property_merge_events" in s, [(1,)]),
    ])
    merge_properties(conn, survivor_id=10, retired_id=20, reason="manual", source="operator")
    sql, params = _find(conn.executed, "INSERT INTO asset_membership_events")
    assert "SET asset_id = CASE WHEN id = %(survivor)s::bigint THEN %(asset)s::bigint END" in sql
    group = _find(conn.executed, "INSERT INTO property_merge_events")[1]["group"]
    assert params == {"survivor": 10, "retired": 20, "asset": 7, "reason": f"merge {group}",
                      "source": "operator"}
    idx = [e[0] for e in conn.executed]
    carry = next(i for i, e in enumerate(idx) if "asset_membership_events" in e)
    retire = next(i for i, e in enumerate(idx) if "status = 'merged_away'" in e)
    assert carry < retire


def test_merge_refuses_two_different_asset_links():
    conn = _FakeConn([
        (lambda s: "SELECT id, status, category_type, category_main, asset_id FROM properties WHERE id IN" in s,
         [(10, "active", "prodej", "byt", 7), (20, "active", "prodej", "byt", 8)]),
    ])
    with pytest.raises(AssetLinkConflict):
        merge_properties(conn, survivor_id=10, retired_id=20, reason="m", source="autodedup")
    assert _find(conn.executed, "UPDATE listings SET property_id =") is None


def test_merge_without_an_asset_link_touches_no_asset():
    conn = _FakeConn([
        (lambda s: "SELECT id, status, category_type, category_main, asset_id FROM properties WHERE id IN" in s,
         [(10, "active", "prodej", "byt", 7), (20, "active", "prodej", "byt", None)]),
        (lambda s: "INSERT INTO property_merge_events" in s, [(1,)]),
    ])
    merge_properties(conn, survivor_id=10, retired_id=20, reason="m", source="autodedup")
    assert _find(conn.executed, "asset_membership_events") is None


def test_merge_rejects_when_retired_not_active():
    conn = _FakeConn([
        (lambda s: "SELECT id, status, category_type, category_main, asset_id FROM properties WHERE id IN" in s,
         [(10, "active", "prodej", "byt", None), (20, "merged_away", "prodej", "byt", None)]),
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
        (lambda s: "SELECT id, status, category_type, category_main, asset_id FROM properties WHERE id IN" in s,
         [(10, "active", "prodej", "byt", None), (20, "active", "pronajem", "byt", None)]),
    ])
    with pytest.raises(MergeError):
        merge_properties(
            conn, survivor_id=10, retired_id=20, reason="manual", source="operator",
        )
    assert _find(conn.executed, "UPDATE listings SET property_id =") is None


def test_merge_rejects_byt_vs_dum_at_chokepoint():
    conn = _FakeConn([
        (lambda s: "SELECT id, status, category_type, category_main, asset_id FROM properties WHERE id IN" in s,
         [(10, "active", "prodej", "byt", None), (20, "active", "prodej", "dum", None)]),
    ])
    with pytest.raises(MergeError):
        merge_properties(
            conn, survivor_id=10, retired_id=20, reason="manual", source="operator",
        )


def test_merge_allows_dum_komercni_cross_type_at_chokepoint():
    # The ONE sanctioned cross-type (a house on one portal, commercial on another, same
    # building) must NOT be refused — the merge proceeds normally past the category guard.
    conn = _FakeConn([
        (lambda s: "SELECT id, status, category_type, category_main, asset_id FROM properties WHERE id IN" in s,
         [(10, "active", "prodej", "dum", None), (20, "active", "prodej", "komercni", None)]),
        (lambda s: "INSERT INTO property_merge_events" in s, [(1,), (2,)]),
    ])
    merge_properties(conn, survivor_id=10, retired_id=20, reason="manual", source="operator")
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
        (lambda s: "SELECT id, status, category_type, category_main, asset_id FROM properties WHERE id IN" in s,
         [(10, "active", "prodej", "byt", None), (20, "active", "prodej", "byt", None)]),
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
        (lambda s: "SELECT id, status, category_type, category_main, asset_id FROM properties WHERE id IN" in s,
         [(10, "active", "prodej", "byt", None), (20, "active", "prodej", "byt", None)]),
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


def test_merge_carries_dismissals_after_the_pipeline():
    # Dismissals follow the survivor in the same transaction, AFTER the pipeline
    # reconciler (a card that landed on the survivor lifts that account's
    # dismissal) and before the loser is soft-retired.
    conn = _FakeConn([
        (lambda s: "SELECT id, status, category_type, category_main, asset_id FROM properties WHERE id IN" in s,
         [(10, "active", "prodej", "byt", None), (20, "active", "prodej", "byt", None)]),
        (lambda s: "INSERT INTO property_merge_events" in s, [(1,)]),
    ])

    merge_properties(
        conn, survivor_id=10, retired_id=20, reason="manual", source="operator",
    )

    def idx(needle: str) -> int:
        return next(i for i, e in enumerate(conn.executed) if needle in e[0])

    repoint = idx("UPDATE property_dismissals SET property_id =")
    assert conn.executed[repoint][1] == {"r": 20, "s": 10}
    assert idx("DELETE FROM property_pipeline WHERE property_id =") < repoint
    assert repoint < idx("status = 'merged_away'")
    assert _find(conn.executed, "DELETE FROM property_dismissals") is None
