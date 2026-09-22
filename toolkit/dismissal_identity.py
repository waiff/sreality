"""Carry property dismissals across a property merge (migration 536).

`property_dismissals` is append-only — undo lifts a row, nothing deletes one — so
the generic `toolkit.operator_state` registry (which collapses a colliding row by
DELETING it) cannot carry it. This runs inside `merge_properties`' service-role
transaction, AFTER the pipeline reconciler, so every predicate names its account
explicitly (tenancy doctrine, shape 3).

A dismissal says the operator doesn't want the real-world property, so the
survivor inherits it from either side. Unmerge/split are best-effort, like the
registry tables: the rows stay on the survivor, with their history intact.
"""

from __future__ import annotations

import psycopg


def reconcile_dismissals_on_merge(
    cur: psycopg.Cursor, *, retired_id: int, survivor_id: int
) -> None:
    """Re-point every dismissal (active and lifted) retired -> survivor."""
    params = {"r": retired_id, "s": survivor_id}

    # One active row per (property, account): where both sides hold one, the
    # survivor's stands and the retired's is lifted, not deleted.
    cur.execute(
        "UPDATE property_dismissals r SET lifted_at = now(), lift_reason = 'merge' "
        "WHERE r.property_id = %(r)s AND r.lifted_at IS NULL "
        "AND EXISTS (SELECT 1 FROM property_dismissals s "
        "  WHERE s.property_id = %(s)s AND s.account_id = r.account_id "
        "  AND s.lifted_at IS NULL)",
        params,
    )
    cur.execute(
        "UPDATE property_dismissals SET property_id = %(s)s WHERE property_id = %(r)s",
        params,
    )
    # A LIVE deal and a dismissal never coexist for one account, whichever side
    # each fact came from; a card closed into a terminal stage keeps it (the same
    # rule `api.dismissals` states on the tenant connection, here per account).
    cur.execute(
        "UPDATE property_dismissals d SET lifted_at = now(), lift_reason = 'pipeline' "
        "WHERE d.property_id = %(s)s AND d.lifted_at IS NULL "
        "AND EXISTS (SELECT 1 FROM property_pipeline pp "
        "  JOIN pipeline_stages ps ON ps.id = pp.stage_id "
        "  WHERE pp.property_id = d.property_id AND pp.account_id = d.account_id "
        "  AND NOT ps.is_terminal)",
        params,
    )
