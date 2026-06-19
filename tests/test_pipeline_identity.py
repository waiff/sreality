"""Hermetic tests for the single-valued pipeline merge reconciler.

A merge keeps the most-advanced pipeline card on the survivor (the generic
operator-state re-point can't, because property_pipeline's PK is property_id).
These assert the SQL shape; the real keep-most-advanced semantics are verified
out-of-band via the Supabase MCP on temp tables.
"""

from __future__ import annotations

from typing import Any

from toolkit.pipeline_identity import reconcile_pipeline_on_merge


class _Cur:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((" ".join(sql.split()), params))


def test_reconcile_emits_move_log_keep_delete_in_order():
    cur = _Cur()
    reconcile_pipeline_on_merge(
        cur, retired_id=20, survivor_id=10, merge_group_id="grp",
    )
    sqls = [s for s, _ in cur.executed]
    assert len(sqls) == 4

    # (1) move the retired card only if the survivor has none
    assert "UPDATE property_pipeline SET property_id = %(s)s" in sqls[0]
    assert "NOT EXISTS" in sqls[0]
    # (2) log the retired card as absorbed (for a future lossless unmerge)
    assert "INSERT INTO property_pipeline_events" in sqls[1]
    assert "merge_absorb" in sqls[1]
    # (3) keep whichever stage is most-advanced on the survivor
    assert "UPDATE property_pipeline s SET stage_id = r.stage_id" in sqls[2]
    assert "rs.position > ss.position" in sqls[2]
    # (4) drop the retired card
    assert sqls[3].startswith("DELETE FROM property_pipeline WHERE property_id = %(r)s")


def test_reconcile_repoints_retired_to_survivor_everywhere():
    cur = _Cur()
    reconcile_pipeline_on_merge(
        cur, retired_id=20, survivor_id=10, merge_group_id="grp",
    )
    for _sql, params in cur.executed:
        assert params == {"r": 20, "s": 10, "g": "grp"}
