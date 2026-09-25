"""Hermetic tests for the single-valued pipeline merge/detach reconciler.

A merge snapshots both sides, keeps the most-advanced (terminal-aware) card on
the survivor, drops the retired; a detach that reactivates the retired property
restores its card from the snapshot. These assert the SQL shape; the real
keep/restore semantics are verified out-of-band via the Supabase MCP on temp tables.
"""

from __future__ import annotations

from typing import Any

from toolkit.pipeline_identity import (
    reconcile_pipeline_on_detach,
    reconcile_pipeline_on_merge,
)


class _Cur:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((" ".join(sql.split()), params))


def test_merge_snapshots_both_then_keeps_and_drops():
    cur = _Cur()
    reconcile_pipeline_on_merge(
        cur, retired_id=20, survivor_id=10, merge_group_id="grp",
    )
    sqls = [s for s, _ in cur.executed]
    assert len(sqls) == 4

    # (0) snapshot BOTH sides' pre-merge cards to the ledger, account carried
    assert "INSERT INTO property_pipeline_events" in sqls[0]
    assert "merge_absorb" in sqls[0]
    assert "property_id IN (%(r)s, %(s)s)" in sqls[0]
    assert "account_id" in sqls[0]
    # (1) move the retired card only if the survivor has none FOR THAT ACCOUNT
    assert "UPDATE property_pipeline SET property_id = %(s)s" in sqls[1]
    assert "NOT EXISTS" in sqls[1]
    assert "s2.account_id IS NOT DISTINCT FROM property_pipeline.account_id" in sqls[1]
    # (2) keep most-advanced, TERMINAL-AWARE (live beats closed), same account only
    assert "UPDATE property_pipeline s SET stage_id = r.stage_id" in sqls[2]
    assert "r.account_id IS NOT DISTINCT FROM s.account_id" in sqls[2]
    assert "ss.account_id IS NOT DISTINCT FROM s.account_id" in sqls[2]
    assert "rs.account_id IS NOT DISTINCT FROM r.account_id" in sqls[2]
    assert "NOT rs.is_terminal AND ss.is_terminal" in sqls[2]
    assert "rs.position > ss.position" in sqls[2]
    # (3) drop the retired card
    assert sqls[3].startswith("DELETE FROM property_pipeline WHERE property_id = %(r)s")


def test_merge_repoints_retired_to_survivor_everywhere():
    cur = _Cur()
    reconcile_pipeline_on_merge(
        cur, retired_id=20, survivor_id=10, merge_group_id="grp",
    )
    for _sql, params in cur.executed:
        assert params == {"r": 20, "s": 10, "g": "grp"}


def test_detach_restores_the_reactivated_property_and_cleans_the_absorbed_card():
    cur = _Cur()
    reconcile_pipeline_on_detach(cur, merge_group_id="grp", restored_id=20, survivor_id=10)
    sqls = [s for s, _ in cur.executed]
    assert len(sqls) == 2

    # restore THIS property's own snapshot of that merge, per (account_id, property_id);
    # bare ON CONFLICT is transition-safe across the 294->295 PK swap
    assert "INSERT INTO property_pipeline" in sqls[0]
    assert "merge_absorb" in sqls[0]
    assert "e.property_id = %(r)s" in sqls[0]
    assert "e.account_id" in sqls[0]
    assert "ON CONFLICT DO NOTHING" in sqls[0]
    # move-if-empty, per account: only a card the survivor absorbed from THIS property,
    # and only where the survivor held none of its own in that merge
    assert sqls[1].startswith("DELETE FROM property_pipeline WHERE property_id = %(s)s")
    assert "AND e.property_id = %(r)s AND e.to_stage_id IS NOT NULL" in sqls[1]
    assert "NOT EXISTS" in sqls[1]
    assert "e.account_id IS NOT DISTINCT FROM property_pipeline.account_id" in sqls[1]

    for _sql, params in cur.executed:
        assert params == {"g": "grp", "r": 20, "s": 10}


def test_detach_off_a_later_survivor_restores_but_never_cleans_it():
    """The advert left a property a LATER merge built: that property never held the card of
    the merge being undone, so only the restore runs."""
    cur = _Cur()
    reconcile_pipeline_on_detach(cur, merge_group_id="grp", restored_id=20, survivor_id=None)
    assert len(cur.executed) == 1 and "INSERT INTO property_pipeline" in cur.executed[0][0]
