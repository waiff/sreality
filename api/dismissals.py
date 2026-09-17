"""Persistence for property dismissals (migration 536).

A dismissal is a row; undo LIFTS it (never deletes, rule #3). "Dismissed for the
caller" is whatever `property_dismissals_public` shows under the caller's RLS —
plural, like every tenant read — so a lift is RLS-scoped too: undo must clear
every active row the caller can see, or the property would stay hidden.

Dismissal and the deal pipeline are mutually exclusive: a property in the
caller's pipeline cannot be dismissed, and adding one to the pipeline lifts its
dismissal (`api.pipeline.add_card`). Close a deal into a terminal stage instead.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

import psycopg
from fastapi import HTTPException

from api import schemas as s
from toolkit.property_identity import resolve_active_property_id

LiftReason = Literal["operator", "pipeline"]


def dismiss(
    conn: "psycopg.Connection", body: s.DismissPropertyIn, *,
    account_id: uuid.UUID,
) -> dict[str, Any]:
    """Dismiss a property for the caller's account. Idempotent."""
    with conn.transaction(), conn.cursor() as cur:
        pid = resolve_active_property_id(conn, body.property_id)
        if pid is None:
            raise HTTPException(422, "property not found")
        cur.execute(
            "SELECT 1 FROM property_pipeline WHERE property_id = %s LIMIT 1", (pid,),
        )
        if cur.fetchone() is not None:
            raise HTTPException(
                409, "property is in the pipeline; close the deal there instead",
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
        removed = lift_dismissals(cur, pid, reason="operator") > 0
    return {"property_id": pid, "removed": removed}


def lift_dismissals(
    cur: "psycopg.Cursor", property_id: int, *, reason: LiftReason,
) -> int:
    """Lift every active dismissal of `property_id` the caller's RLS can see."""
    cur.execute(
        "UPDATE property_dismissals SET lifted_at = now(), lift_reason = %s "
        "WHERE property_id = %s AND lifted_at IS NULL",
        (reason, property_id),
    )
    return cur.rowcount
