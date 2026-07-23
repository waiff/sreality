"""CRUD for /listings/{id}/manual_estimates and /manual_estimates/{id}.

Operator-recorded point-estimate rental figures attached to a listing.
Mutable rows; history trigger captures the pre-state on UPDATE and
DELETE (migration 046). Read path: SPA reads from
`manual_rental_estimates_public` with the anon key. Write path: these
bearer-gated endpoints from the FastAPI service.

Pattern mirrors api/curation.py:
  - one transaction per write,
  - plain dicts on the way out,
  - HTTPException for the standard error codes.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import psycopg
from fastapi import HTTPException

from api import schemas as s

if TYPE_CHECKING:
    pass


_COLS = (
    "id", "sreality_id", "listing_id", "rent_czk", "author", "source_kind", "notes",
    "created_at", "updated_at",
)
_SELECT = ", ".join(_COLS)


def list_manual_estimates(
    conn: "psycopg.Connection", sreality_id: int,
) -> dict[str, Any]:
    sql = (
        f"SELECT {_SELECT} FROM manual_rental_estimates "
        "WHERE sreality_id = %s ORDER BY created_at DESC, id DESC"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (sreality_id,))
        rows = cur.fetchall()
    return {"data": [_to_estimate(r) for r in rows]}


def create_manual_estimate(
    conn: "psycopg.Connection",
    sreality_id: int,
    body: s.CreateManualEstimateIn,
) -> dict[str, Any]:
    # listing_id is the surrogate mirror of sreality_id (R2 dual-write); resolved
    # inline so the caller keeps passing only the legacy id.
    sql = (
        "INSERT INTO manual_rental_estimates "
        "  (sreality_id, listing_id, rent_czk, author, source_kind, notes, updated_by) "
        "VALUES (%s, (SELECT id FROM listings WHERE sreality_id = %s), "
        "        %s, %s, %s, %s, %s) "
        f"RETURNING {_SELECT}"
    )
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(sql, (
                sreality_id, sreality_id, body.rent_czk, body.author,
                body.source_kind, body.notes, body.updated_by,
            ))
            row = cur.fetchone()
    except psycopg.errors.ForeignKeyViolation:
        raise HTTPException(404, "listing not found")
    assert row is not None
    return _to_estimate(row)


def update_manual_estimate(
    conn: "psycopg.Connection",
    estimate_id: int,
    body: s.UpdateManualEstimateIn,
) -> dict[str, Any]:
    sets: list[str] = []
    params: list[Any] = []
    if body.rent_czk is not None:
        sets.append("rent_czk = %s")
        params.append(body.rent_czk)
    if body.author is not None:
        sets.append("author = %s")
        params.append(body.author)
    if body.source_kind is not None:
        sets.append("source_kind = %s")
        params.append(body.source_kind)
    if body.notes is not None:
        sets.append("notes = %s")
        params.append(body.notes if body.notes else None)
    if not sets:
        row = _fetch_estimate(conn, estimate_id)
        if row is None:
            raise HTTPException(404, "manual estimate not found")
        return row

    sets.append("updated_at = now()")
    sets.append("updated_by = %s")
    params.append(body.updated_by)
    params.append(estimate_id)

    sql = (
        f"UPDATE manual_rental_estimates SET {', '.join(sets)} "
        f"WHERE id = %s RETURNING {_SELECT}"
    )
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, "manual estimate not found")
    return _to_estimate(row)


def delete_manual_estimate(
    conn: "psycopg.Connection", estimate_id: int,
) -> dict[str, Any]:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "DELETE FROM manual_rental_estimates WHERE id = %s",
            (estimate_id,),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "manual estimate not found")
    return {"deleted": True}


def _fetch_estimate(
    conn: "psycopg.Connection", estimate_id: int,
) -> dict[str, Any] | None:
    sql = (
        f"SELECT {_SELECT} FROM manual_rental_estimates WHERE id = %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (estimate_id,))
        row = cur.fetchone()
    return _to_estimate(row) if row is not None else None


def _to_estimate(row: tuple[Any, ...]) -> dict[str, Any]:
    # sreality_id is NULL for non-sreality-portal listings post-Gate-2-flip
    # (int(None) would crash); listing_id is the surrogate that's always present.
    return {
        "id":          int(row[0]),
        "sreality_id": int(row[1]) if row[1] is not None else None,
        "listing_id":  int(row[2]) if row[2] is not None else None,
        "rent_czk":    int(row[3]),
        "author":      row[4],
        "source_kind": row[5],
        "notes":       row[6],
        "created_at":  _iso(row[7]),
        "updated_at":  _iso(row[8]),
    }


def _iso(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    return v
