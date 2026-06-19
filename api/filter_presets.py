"""Persistence for Browse saved filter presets (`filter_presets`).

A preset is a named filter set restored entirely client-side; the backend
never interprets `filter_spec` (it is the native Browse `ListingFilters`
object, stored verbatim as an opaque JSONB blob). This module is plain
psycopg I/O mirroring the subscription CRUD in `api/notifications.py`,
minus everything firing-related.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import psycopg

_COLS = "id, name, filter_spec, created_at, updated_at, position, color"

# Sentinel distinguishing "field omitted" from "set to NULL" in update_preset:
# `color=None` clears the colour, `color=_UNSET` leaves it untouched.
_UNSET: Any = object()


@dataclass
class PresetRow:
    id: str
    name: str
    filter_spec: dict[str, Any]
    created_at: str
    updated_at: str
    position: int
    color: str | None


def _row_to_preset(row: tuple[Any, ...]) -> PresetRow:
    return PresetRow(
        id=str(row[0]),
        name=row[1],
        filter_spec=row[2] or {},
        created_at=row[3].isoformat() if row[3] else "",
        updated_at=row[4].isoformat() if row[4] else "",
        position=row[5],
        color=row[6],
    )


def list_presets(conn: "psycopg.Connection") -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM filter_presets "
            "ORDER BY position ASC, created_at DESC"
        )
        rows = cur.fetchall()
    return [_row_to_preset(r).__dict__ for r in rows]


def get_preset(conn: "psycopg.Connection", preset_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_COLS} FROM filter_presets WHERE id = %s", (preset_id,))
        row = cur.fetchone()
    return _row_to_preset(row).__dict__ if row is not None else None


def create_preset(
    conn: "psycopg.Connection",
    *,
    name: str,
    filter_spec: dict[str, Any],
    color: str | None = None,
) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO filter_presets (name, filter_spec, color, position) "
            "VALUES (%s, %s::jsonb, %s, "
            "(SELECT COALESCE(MAX(position), -1) + 1 FROM filter_presets)) "
            "RETURNING id",
            (name, json.dumps(filter_spec), color),
        )
        row = cur.fetchone()
    assert row is not None
    return get_preset(conn, str(row[0])) or {}


def update_preset(
    conn: "psycopg.Connection",
    preset_id: str,
    *,
    name: str | None = None,
    filter_spec: dict[str, Any] | None = None,
    color: str | None | Any = _UNSET,
) -> dict[str, Any] | None:
    sets: list[str] = []
    params: list[Any] = []
    if name is not None:
        sets.append("name = %s")
        params.append(name)
    if filter_spec is not None:
        sets.append("filter_spec = %s::jsonb")
        params.append(json.dumps(filter_spec))
    if color is not _UNSET:
        sets.append("color = %s")
        params.append(color)
    if not sets:
        return get_preset(conn, preset_id)
    params.append(preset_id)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE filter_presets SET {', '.join(sets)} WHERE id = %s",
            params,
        )
        if cur.rowcount == 0:
            return None
    return get_preset(conn, preset_id)


def delete_preset(conn: "psycopg.Connection", preset_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM filter_presets WHERE id = %s", (preset_id,))
        return cur.rowcount > 0


def reorder_presets(
    conn: "psycopg.Connection", ids: list[str]
) -> list[dict[str, Any]]:
    """Rewrite `position` to match the given id order (0 = first). Ids not in
    the list keep their old position; the canonical list is returned."""
    if ids:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE filter_presets fp SET position = v.pos "
                "FROM (SELECT id, (ord - 1) AS pos "
                "FROM unnest(%s::uuid[]) WITH ORDINALITY AS t(id, ord)) v "
                "WHERE fp.id = v.id",
                (ids,),
            )
    return list_presets(conn)
