"""Persistence for the deal pipeline (migration 205).

Phase 0 = the bookmark surface: a property is "bookmarked / interested" iff it
has a property_pipeline row, which starts at the entry stage. Stage moves (the
kanban) come in a later phase. Single-valued (one row per property); writes go
through the bearer-gated API, reads (membership) via property_pipeline_public.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg
from fastapi import HTTPException

from api import schemas as s


def list_stages(conn: "psycopg.Connection") -> dict[str, Any]:
    sql = (
        "SELECT id, key, label, position, color, is_terminal, is_entry "
        "FROM pipeline_stages WHERE archived_at IS NULL ORDER BY position"
    )
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    return {"data": [_to_stage(r) for r in rows]}


def add_card(
    conn: "psycopg.Connection", body: s.AddPipelineCardIn,
) -> dict[str, Any]:
    """Bookmark a property: insert a card at the entry stage. Idempotent."""
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM pipeline_stages WHERE is_entry LIMIT 1",
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(500, "no entry stage configured")
            entry_stage_id = int(row[0])
            cur.execute(
                "SELECT coalesce(max(board_position), 0) + 1 "
                "FROM property_pipeline WHERE stage_id = %s",
                (entry_stage_id,),
            )
            next_pos = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO property_pipeline (property_id, stage_id, board_position) "
                "VALUES (%s, %s, %s) ON CONFLICT (property_id) DO NOTHING "
                "RETURNING property_id",
                (body.property_id, entry_stage_id, next_pos),
            )
            added = cur.fetchone() is not None
            if added:
                cur.execute(
                    "INSERT INTO property_pipeline_events "
                    "  (property_id, to_stage_id, reason) VALUES (%s, %s, 'operator')",
                    (body.property_id, entry_stage_id),
                )
    except psycopg.errors.ForeignKeyViolation:
        raise HTTPException(422, "property not found")
    card = _fetch_card(conn, body.property_id)
    if card is None:
        raise RuntimeError("pipeline card vanished after insert")
    return {**card, "added": added}


def remove_card(
    conn: "psycopg.Connection", property_id: int,
) -> dict[str, Any]:
    """Un-bookmark: drop the card, logging its prior stage to the ledger."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "SELECT stage_id FROM property_pipeline WHERE property_id = %s",
            (property_id,),
        )
        row = cur.fetchone()
        if row is None:
            return {"removed": False}
        from_stage_id = int(row[0])
        cur.execute(
            "DELETE FROM property_pipeline WHERE property_id = %s", (property_id,),
        )
        cur.execute(
            "INSERT INTO property_pipeline_events "
            "  (property_id, from_stage_id, reason) VALUES (%s, %s, 'operator')",
            (property_id, from_stage_id),
        )
    return {"removed": True}


# --- helpers ---------------------------------------------------------------


def _fetch_card(
    conn: "psycopg.Connection", property_id: int,
) -> dict[str, Any] | None:
    sql = (
        "SELECT pp.property_id, pp.stage_id, ps.key, ps.label, pp.board_position, "
        "       pp.note, pp.entered_stage_at, pp.added_at "
        "FROM property_pipeline pp JOIN pipeline_stages ps ON ps.id = pp.stage_id "
        "WHERE pp.property_id = %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (property_id,))
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "property_id":      int(row[0]),
        "stage_id":         int(row[1]),
        "stage_key":        row[2],
        "stage_label":      row[3],
        "board_position":   float(row[4]) if row[4] is not None else None,
        "note":             row[5],
        "entered_stage_at": _iso(row[6]),
        "added_at":         _iso(row[7]),
    }


def _to_stage(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id":          int(row[0]),
        "key":         row[1],
        "label":       row[2],
        "position":    int(row[3]),
        "color":       row[4],
        "is_terminal": bool(row[5]),
        "is_entry":    bool(row[6]),
    }


def _iso(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    return v
