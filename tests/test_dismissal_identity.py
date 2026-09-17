"""Hermetic tests for the dismissal merge reconciler (migration 536).

Dismissals are append-only history, so a merge must LIFT a colliding active row,
never delete one, and every predicate must name its account (service-role).
These assert the SQL shape; semantics are verified out-of-band on the live DB.
"""

from __future__ import annotations

from typing import Any

from toolkit.dismissal_identity import reconcile_dismissals_on_merge


class _Cur:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((" ".join(sql.split()), params))


def _run() -> list[str]:
    cur = _Cur()
    reconcile_dismissals_on_merge(cur, retired_id=20, survivor_id=10)
    for _sql, params in cur.executed:
        assert params == {"r": 20, "s": 10}
    return [s for s, _ in cur.executed]


def test_never_deletes_a_dismissal():
    assert not any("DELETE" in s for s in _run())


def test_collision_lifts_the_retired_active_row_per_account_first():
    collide, repoint, pipeline = _run()
    assert collide.startswith("UPDATE property_dismissals r SET lifted_at = now(), lift_reason = 'merge'")
    assert "r.property_id = %(r)s AND r.lifted_at IS NULL" in collide
    assert "s.property_id = %(s)s AND s.account_id = r.account_id" in collide
    assert "s.lifted_at IS NULL" in collide
    assert repoint == (
        "UPDATE property_dismissals SET property_id = %(s)s WHERE property_id = %(r)s"
    )


def test_pipeline_card_on_the_survivor_lifts_that_accounts_dismissal():
    *_, pipeline = _run()
    assert pipeline.startswith(
        "UPDATE property_dismissals d SET lifted_at = now(), lift_reason = 'pipeline'"
    )
    assert "d.property_id = %(s)s AND d.lifted_at IS NULL" in pipeline
    assert "pp.property_id = d.property_id AND pp.account_id = d.account_id" in pipeline
