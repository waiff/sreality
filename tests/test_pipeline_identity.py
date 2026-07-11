"""Hermetic tests for the single-valued pipeline merge/unmerge reconciler.

A merge snapshots both sides, keeps the most-advanced (terminal-aware) card on
the survivor, drops the retired; an unmerge restores the reactivated retired
property's card from the snapshot. These assert the SQL shape; the real
keep/restore semantics are verified out-of-band via the Supabase MCP on temp tables.
"""

from __future__ import annotations

from typing import Any

from toolkit.pipeline_identity import (
    reconcile_pipeline_on_merge,
    reconcile_pipeline_on_unmerge,
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


def test_unmerge_restores_retired_and_cleans_moved_survivor_card():
    cur = _Cur()
    reconcile_pipeline_on_unmerge(cur, merge_group_id="grp", survivor_id=10)
    sqls = [s for s, _ in cur.executed]
    assert len(sqls) == 2

    # restore the retired (non-survivor) snapshot onto its now-active property,
    # per (account_id, property_id); bare ON CONFLICT is transition-safe across
    # the 294→295 PK swap
    assert "INSERT INTO property_pipeline" in sqls[0]
    assert "merge_absorb" in sqls[0]
    assert "e.property_id <> %(s)s" in sqls[0]
    assert "e.account_id" in sqls[0]
    assert "ON CONFLICT DO NOTHING" in sqls[0]
    assert "status = 'active'" in sqls[0]
    # move-if-empty cleanup: drop the survivor's absorbed card iff it had no
    # snapshot, per account
    assert sqls[1].startswith("DELETE FROM property_pipeline WHERE property_id = %(s)s")
    assert "NOT EXISTS" in sqls[1]
    assert "e.account_id IS NOT DISTINCT FROM property_pipeline.account_id" in sqls[1]

    for _sql, params in cur.executed:
        assert params == {"g": "grp", "s": 10}
