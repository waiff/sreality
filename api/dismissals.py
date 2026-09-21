"""Persistence for property dismissals (migration 536).

A dismissal is a row; undo LIFTS it (never deletes, rule #3). "Dismissed for the
caller" is whatever `property_dismissals_public` shows under the caller's RLS —
plural, like every tenant read — so a lift is RLS-scoped too: undo must clear
every active row the caller can see, or the property would stay hidden.

A LIVE deal and a dismissal never coexist: a property whose card sits at a
non-terminal stage cannot be dismissed, and any pipeline write that leaves a card
live lifts the dismissal (`lift_dismissals_of_live_deals`). A deal closed into a
terminal stage is history, not pursuit — "Passed" is exactly "reviewed it, didn't
like it" — so it can be dismissed and keep its card; re-opening it lifts again.
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
from fastapi import HTTPException

from api import schemas as s
from toolkit.property_identity import resolve_active_property_id

def dismiss(
    conn: "psycopg.Connection", body: s.DismissPropertyIn, *,
    account_id: uuid.UUID,
) -> dict[str, Any]:
    """Dismiss a property for the caller's account. Idempotent."""
    with conn.transaction(), conn.cursor() as cur:
        pid = resolve_active_property_id(conn, body.property_id)
        if pid is None:
            raise HTTPException(422, "property not found")
        cur.execute(_LIVE_DEAL_SQL, (pid,))
        if cur.fetchone() is not None:
            raise HTTPException(
                409, "property is a live deal; close it into a terminal stage first",
            )
        cur.execute(
            "INSERT INTO property_dismissals (account_id, property_id) "
            "VALUES (%s, %s) "
            "ON CONFLICT (property_id, account_id) WHERE lifted_at IS NULL DO NOTHING",
            (account_id, pid),
        )
        added = cur.rowcount == 1
    return {"property_id": pid, "added": added}


def undismiss(conn: "psycopg.Connection", property_id: int) -> dict[str, Any]:
    """Undo: lift the caller's active dismissals of a property (RLS-scoped)."""
    with conn.transaction(), conn.cursor() as cur:
        pid = resolve_active_property_id(conn, property_id)
        if pid is None:
            return {"property_id": property_id, "removed": False}
        cur.execute(
            "UPDATE property_dismissals SET lifted_at = now(), lift_reason = 'operator' "
            "WHERE property_id = %s AND lifted_at IS NULL",
            (pid,),
        )
        removed = cur.rowcount > 0
    return {"property_id": pid, "removed": removed}


# "Live" = a card at a non-terminal stage. RLS-scoped like every tenant read: the
# caller's dismissal yields to the caller's live deal (the merge reconciler states
# the same rule per account, on the service-role connection).
_LIVE_DEAL_SQL = (
    "SELECT 1 FROM property_pipeline pp "
    "JOIN pipeline_stages ps ON ps.id = pp.stage_id "
    "WHERE pp.property_id = %s AND NOT ps.is_terminal LIMIT 1"
)

_LIFT_LIVE_DEALS_SQL = (
    "UPDATE property_dismissals d SET lifted_at = now(), lift_reason = 'pipeline' "
    "WHERE d.property_id = ANY(%s) AND d.lifted_at IS NULL "
    "AND EXISTS (SELECT 1 FROM property_pipeline pp "
    "  JOIN pipeline_stages ps ON ps.id = pp.stage_id "
    "  WHERE pp.property_id = d.property_id AND NOT ps.is_terminal)"
)


def lift_dismissals_of_live_deals(
    cur: "psycopg.Cursor", property_ids: list[int],
) -> int:
    """Lift the caller's dismissals of whichever of these properties now hold a live card.

    Decided from the data, not from the caller's idea of what just changed: every
    pipeline write that can leave a card live (add, a stage move, a stage re-opened)
    calls this afterwards, and a card still at a terminal stage keeps its dismissal.
    """
    cur.execute(_LIFT_LIVE_DEALS_SQL, (list(property_ids),))
    return cur.rowcount
