"""Persistence for the deal pipeline (migration 205).

Phase 0 = the bookmark surface: a property is "bookmarked / interested" iff it
has a property_pipeline row, which starts at the entry stage. Stage moves (the
kanban) come in a later phase. Single-valued (one row per property per account);
writes go through the bearer-gated API, reads (membership) via
property_pipeline_public.

Account scoping (Phase 1, migrations 294/295): every public function takes the
caller's account_id and predicates with `account_id IS NOT DISTINCT FROM %s` —
explicit even under RLS, because the legacy service-role branch bypasses RLS;
NULL-safe because pre-backfill legacy rows carry account_id NULL. The card
INSERT's ON CONFLICT stays bare (no inference target) so it works against both
the (property_id) PK and the (account_id, property_id) PK migration 295 swaps in.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import datetime
from typing import Any

import psycopg
from fastapi import HTTPException

from api import schemas as s
from toolkit.property_identity import resolve_active_property_id


def list_stages(
    conn: "psycopg.Connection", *, account_id: uuid.UUID | None,
) -> dict[str, Any]:
    sql = (
        "SELECT id, key, label, position, color, is_terminal, is_entry "
        "FROM pipeline_stages WHERE archived_at IS NULL "
        "AND account_id IS NOT DISTINCT FROM %s ORDER BY position"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (account_id,))
        rows = cur.fetchall()
    return {"data": [_to_stage(r) for r in rows]}


def create_stage(
    conn: "psycopg.Connection", body: s.CreateStageIn, *,
    account_id: uuid.UUID | None,
) -> dict[str, Any]:
    """Append a new column to the right. New stages are never the entry stage."""
    _validate_color(body.color)
    with conn.transaction(), conn.cursor() as cur:
        key = _unique_key(cur, body.label, account_id)
        cur.execute(
            "SELECT coalesce(max(position), 0) + 1 FROM pipeline_stages "
            "WHERE account_id IS NOT DISTINCT FROM %s",
            (account_id,),
        )
        position = int(cur.fetchone()[0])
        cur.execute(
            "INSERT INTO pipeline_stages "
            "  (key, label, position, color, is_terminal, account_id) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "RETURNING id, key, label, position, color, is_terminal, is_entry",
            (key, body.label, position, body.color, body.is_terminal, account_id),
        )
        row = cur.fetchone()
    return _to_stage(row)


def update_stage(
    conn: "psycopg.Connection", stage_id: int, body: s.UpdateStageIn, *,
    account_id: uuid.UUID | None,
) -> dict[str, Any]:
    """Rename / recolor / retag a stage, or move the entry crown onto it."""
    _validate_color(body.color)
    if body.is_entry is False:
        raise HTTPException(
            422, "re-home the entry stage by crowning another, not by un-crowning",
        )
    with conn.transaction(), conn.cursor() as cur:
        # account-scoped (like every write in this module): the legacy
        # service-role branch bypasses RLS, so a bare `WHERE id = %s` would let
        # a legacy caller read/rename/re-flag another account's stage by id.
        cur.execute(
            "SELECT is_entry, is_terminal FROM pipeline_stages "
            "WHERE id = %s AND account_id IS NOT DISTINCT FROM %s",
            (stage_id, account_id),
        )
        cur_row = cur.fetchone()
        if cur_row is None:
            raise HTTPException(404, "stage not found")
        cur_entry, cur_terminal = bool(cur_row[0]), bool(cur_row[1])

        final_entry = body.is_entry if body.is_entry is not None else cur_entry
        final_terminal = (
            body.is_terminal if body.is_terminal is not None else cur_terminal
        )
        if final_entry and final_terminal:
            raise HTTPException(422, "the entry stage cannot also be terminal")

        if body.is_entry:  # move the single-entry crown onto this stage
            # account-scoped: a legacy service-role call must never un-crown
            # another tenant's entry stage (RLS doesn't apply on that branch).
            cur.execute(
                "UPDATE pipeline_stages SET is_entry = false, updated_at = now() "
                "WHERE is_entry AND id <> %s "
                "AND account_id IS NOT DISTINCT FROM %s",
                (stage_id, account_id),
            )

        sets: list[str] = []
        params: list[Any] = []
        if body.label is not None:
            sets += ["label = %s"]
            params += [body.label]
        if "color" in body.model_fields_set:
            sets += ["color = %s"]
            params += [body.color]
        if body.is_terminal is not None:
            sets += ["is_terminal = %s"]
            params += [body.is_terminal]
        if body.is_entry is not None:
            sets += ["is_entry = %s"]
            params += [body.is_entry]
        if sets:
            sets += ["updated_at = now()"]
            params += [stage_id, account_id]
            cur.execute(
                f"UPDATE pipeline_stages SET {', '.join(sets)} "
                "WHERE id = %s AND account_id IS NOT DISTINCT FROM %s "
                "RETURNING id, key, label, position, color, is_terminal, is_entry",
                params,
            )
            row = cur.fetchone()
        else:
            cur.execute(
                "SELECT id, key, label, position, color, is_terminal, is_entry "
                "FROM pipeline_stages WHERE id = %s AND account_id IS NOT DISTINCT FROM %s",
                (stage_id, account_id),
            )
            row = cur.fetchone()
    return _to_stage(row)


def reorder_stages(
    conn: "psycopg.Connection", body: s.ReorderStagesIn, *,
    account_id: uuid.UUID | None,
) -> dict[str, Any]:
    """Rewrite left-to-right order. `ordered_ids` must be exactly the live set."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM pipeline_stages WHERE archived_at IS NULL "
            "AND account_id IS NOT DISTINCT FROM %s",
            (account_id,),
        )
        live = {int(r[0]) for r in cur.fetchall()}
        if set(body.ordered_ids) != live or len(body.ordered_ids) != len(live):
            raise HTTPException(
                422, "ordered_ids must list every active stage exactly once",
            )
        for pos, sid in enumerate(body.ordered_ids, start=1):
            cur.execute(
                "UPDATE pipeline_stages SET position = %s, updated_at = now() "
                "WHERE id = %s",
                (pos, sid),
            )
    return list_stages(conn, account_id=account_id)


def archive_stage(
    conn: "psycopg.Connection", stage_id: int, *,
    account_id: uuid.UUID | None,
) -> dict[str, Any]:
    """Soft-retire a stage. Refused if it is the entry stage or still holds cards."""
    with conn.transaction(), conn.cursor() as cur:
        # account-scoped throughout (the legacy service-role branch bypasses
        # RLS): otherwise a legacy caller could archive another account's stage
        # by id, and the cards-check would leak (via 409-vs-success) whether a
        # foreign stage holds cards.
        cur.execute(
            "SELECT is_entry, archived_at FROM pipeline_stages "
            "WHERE id = %s AND account_id IS NOT DISTINCT FROM %s",
            (stage_id, account_id),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, "stage not found")
        if bool(row[0]):
            raise HTTPException(409, "crown another stage as entry before archiving this one")
        if row[1] is not None:
            return {"archived": False, "stage_id": stage_id}
        cur.execute(
            "SELECT 1 FROM property_pipeline WHERE stage_id = %s "
            "AND account_id IS NOT DISTINCT FROM %s LIMIT 1",
            (stage_id, account_id),
        )
        if cur.fetchone() is not None:
            raise HTTPException(409, "stage still holds cards; move them first")
        cur.execute(
            "UPDATE pipeline_stages SET archived_at = now(), updated_at = now() "
            "WHERE id = %s AND account_id IS NOT DISTINCT FROM %s",
            (stage_id, account_id),
        )
    return {"archived": True, "stage_id": stage_id}


def add_card(
    conn: "psycopg.Connection", body: s.AddPipelineCardIn, *,
    account_id: uuid.UUID | None,
) -> dict[str, Any]:
    """Bookmark a property: insert a card at the entry stage. Idempotent.

    A stale property_id (cached by the extension, or from the 5-min browse_list)
    may have been merged away since; resolve it to the live survivor so the card
    never orphans onto a retired property.
    """
    try:
        with conn.transaction(), conn.cursor() as cur:
            pid = resolve_active_property_id(conn, body.property_id)
            if pid is None:
                raise HTTPException(422, "property not found")
            cur.execute(
                "SELECT id FROM pipeline_stages "
                "WHERE is_entry AND account_id IS NOT DISTINCT FROM %s LIMIT 1",
                (account_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(500, "no entry stage configured")
            entry_stage_id = int(row[0])
            # Lock the stage row first so two members of the same account
            # bookmarking into the entry stage concurrently can't both read the
            # same max() and land on the same board_position — the row lock
            # serializes them within each request's one tenant-pool transaction
            # (Amendment A1), no advisory lock needed (those are unsound over the
            # transaction pooler; mig-279 lesson).
            cur.execute("SELECT 1 FROM pipeline_stages WHERE id = %s FOR UPDATE", (entry_stage_id,))
            # board_position is per (account, stage): scope the max by account so
            # the query is served by mig-357's (account_id, stage_id,
            # board_position) index and two accounts sharing a stage id can't
            # interleave positions.
            cur.execute(
                "SELECT coalesce(max(board_position), 0) + 1 "
                "FROM property_pipeline "
                "WHERE account_id IS NOT DISTINCT FROM %s AND stage_id = %s",
                (account_id, entry_stage_id),
            )
            next_pos = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO property_pipeline "
                "  (property_id, stage_id, board_position, account_id) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING "
                "RETURNING property_id",
                (pid, entry_stage_id, next_pos, account_id),
            )
            added = cur.fetchone() is not None
            if added:
                cur.execute(
                    "INSERT INTO property_pipeline_events "
                    "  (property_id, to_stage_id, reason, account_id) "
                    "VALUES (%s, %s, 'operator', %s)",
                    (pid, entry_stage_id, account_id),
                )
    except psycopg.errors.ForeignKeyViolation:
        # Reachable only from the property_pipeline INSERT's property/account FK
        # (a stale/merged-away property, or an unknown account) — genuinely
        # "property not found". The events INSERT that follows reuses the same
        # pid/stage/account the card INSERT already validated, so it can never be
        # the first FK violation and be misattributed here.
        raise HTTPException(422, "property not found")
    card = _fetch_card(conn, pid, account_id)
    if card is None:
        raise RuntimeError("pipeline card vanished after insert")
    return {**card, "added": added}


def remove_card(
    conn: "psycopg.Connection", property_id: int, *,
    account_id: uuid.UUID | None,
) -> dict[str, Any]:
    """Un-bookmark: drop the card, logging its prior stage to the ledger."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "SELECT stage_id FROM property_pipeline "
            "WHERE property_id = %s AND account_id IS NOT DISTINCT FROM %s",
            (property_id, account_id),
        )
        row = cur.fetchone()
        if row is None:
            return {"removed": False}
        from_stage_id = int(row[0])
        cur.execute(
            "DELETE FROM property_pipeline "
            "WHERE property_id = %s AND account_id IS NOT DISTINCT FROM %s",
            (property_id, account_id),
        )
        cur.execute(
            "INSERT INTO property_pipeline_events "
            "  (property_id, from_stage_id, reason, account_id) "
            "VALUES (%s, %s, 'operator', %s)",
            (property_id, from_stage_id, account_id),
        )
    return {"removed": True}


def move_card(
    conn: "psycopg.Connection", property_id: int, body: s.MoveCardIn, *,
    account_id: uuid.UUID | None,
) -> dict[str, Any]:
    """Move a card to another stage and/or reorder it within a stage.

    A stage change stamps `entered_stage_at` and logs a `moved` event; a pure
    within-stage reorder (only `board_position`) is not deal history, so it logs
    nothing.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "SELECT stage_id FROM property_pipeline WHERE property_id = %s "
            "AND account_id IS NOT DISTINCT FROM %s FOR UPDATE",
            (property_id, account_id),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, "pipeline card not found")
        from_stage_id = int(row[0])
        stage_changed = body.stage_id is not None and body.stage_id != from_stage_id

        sets: list[str] = []
        params: list[Any] = []
        if body.stage_id is not None:
            sets.append("stage_id = %s")
            params.append(body.stage_id)
            if stage_changed:
                sets.append("entered_stage_at = now()")
        if body.board_position is not None:
            sets.append("board_position = %s")
            params.append(body.board_position)
        if sets:
            sets.append("updated_at = now()")
            params += [property_id, account_id]
            try:
                cur.execute(
                    f"UPDATE property_pipeline SET {', '.join(sets)} "
                    "WHERE property_id = %s AND account_id IS NOT DISTINCT FROM %s",
                    params,
                )
            except psycopg.errors.ForeignKeyViolation:
                raise HTTPException(422, "stage not found")
            if stage_changed:
                cur.execute(
                    "INSERT INTO property_pipeline_events "
                    "  (property_id, from_stage_id, to_stage_id, reason, account_id) "
                    "VALUES (%s, %s, %s, 'operator', %s)",
                    (property_id, from_stage_id, body.stage_id, account_id),
                )
    card = _fetch_card(conn, property_id, account_id)
    assert card is not None
    return card


# --- helpers ---------------------------------------------------------------


def _validate_color(color: str | None) -> None:
    if color is not None and color not in s.PIPELINE_STAGE_COLORS:
        raise HTTPException(422, f"invalid color; pick one of {s.PIPELINE_STAGE_COLORS}")


def _slugify(label: str) -> str:
    norm = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "_", norm.lower()).strip("_")
    return slug or "stage"


def _unique_key(
    cur: "psycopg.Cursor", label: str, account_id: uuid.UUID | None,
) -> str:
    base = _slugify(label)
    cur.execute(
        "SELECT lower(key) FROM pipeline_stages "
        "WHERE account_id IS NOT DISTINCT FROM %s",
        (account_id,),
    )
    taken = {r[0] for r in cur.fetchall()}
    if base not in taken:
        return base
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


def _fetch_card(
    conn: "psycopg.Connection", property_id: int, account_id: uuid.UUID | None,
) -> dict[str, Any] | None:
    sql = (
        "SELECT pp.property_id, pp.stage_id, ps.key, ps.label, pp.board_position, "
        "       pp.note, pp.entered_stage_at, pp.added_at "
        "FROM property_pipeline pp JOIN pipeline_stages ps ON ps.id = pp.stage_id "
        "WHERE pp.property_id = %s AND pp.account_id IS NOT DISTINCT FROM %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (property_id, account_id))
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
