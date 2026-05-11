"""Persistence helpers for /collections, /listings/{id}/notes, /tags.

CRUD over the curation tables (migrations 022-024). Each handler is a
plain function returning a dict; the FastAPI routes in api/main.py wrap
them with the standard Depends(get_db_conn) + Depends(require_token).

Pattern mirrors api/estimation_runs.py:
  - one transaction per write,
  - psycopg.errors.UniqueViolation -> HTTP 409,
  - flat dicts on the way out (no Pydantic response model).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import psycopg
from fastapi import HTTPException

from api import schemas as s

if TYPE_CHECKING:
    pass


# --- collections -----------------------------------------------------------

_COLLECTION_FULL_PROJECTION = (
    "c.id, c.name, c.description, c.created_at, c.updated_at, "
    "(SELECT count(*) FROM collection_listings cl "
    " WHERE cl.collection_id = c.id) AS listing_count"
)


def create_collection(
    conn: "psycopg.Connection", body: s.CreateCollectionIn,
) -> dict[str, Any]:
    sql = (
        "INSERT INTO collections (name, description) VALUES (%s, %s) "
        "RETURNING id, name, description, created_at, updated_at"
    )
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(sql, (body.name, body.description))
            row = cur.fetchone()
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, "collection name already exists")
    if row is None:
        raise RuntimeError("INSERT collections did not return a row")
    return _to_collection(row, listing_count=0)


def list_collections(conn: "psycopg.Connection") -> dict[str, Any]:
    sql = (
        f"SELECT {_COLLECTION_FULL_PROJECTION} FROM collections c "
        "ORDER BY c.updated_at DESC"
    )
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    data = [_to_collection_full(r) for r in rows]
    return {"data": data, "total": len(data)}


def get_collection(
    conn: "psycopg.Connection", collection_id: int,
) -> dict[str, Any]:
    """Return the collection metadata + the full listing rows it holds.

    The embedded listings are joined from the `listings` table directly
    (not `listings_public`); this endpoint runs server-side under the
    service role, so projecting the same column set the SPA's table view
    consumes is safe.
    """
    coll = _fetch_collection(conn, collection_id)
    if coll is None:
        raise HTTPException(404, "collection not found")

    listings_sql = (
        "SELECT l.sreality_id, l.district, l.disposition, l.area_m2, "
        "       l.price_czk, l.last_seen_at, l.is_active, cl.added_at "
        "FROM collection_listings cl "
        "JOIN listings l ON l.sreality_id = cl.sreality_id "
        "WHERE cl.collection_id = %s "
        "ORDER BY cl.added_at DESC"
    )
    with conn.cursor() as cur:
        cur.execute(listings_sql, (collection_id,))
        rows = cur.fetchall()
    listings = [
        {
            "sreality_id":  int(r[0]),
            "district":     r[1],
            "disposition":  r[2],
            "area_m2":      float(r[3]) if r[3] is not None else None,
            "price_czk":    r[4],
            "last_seen_at": _iso(r[5]),
            "is_active":    bool(r[6]),
            "added_at":     _iso(r[7]),
        }
        for r in rows
    ]
    return {"collection": coll, "listings": listings}


def update_collection(
    conn: "psycopg.Connection",
    collection_id: int,
    body: s.UpdateCollectionIn,
) -> dict[str, Any]:
    sets: list[str] = []
    params: list[Any] = []
    if body.name is not None:
        sets.append("name = %s")
        params.append(body.name)
    if body.description is not None:
        sets.append("description = %s")
        params.append(body.description)
    if not sets:
        coll = _fetch_collection(conn, collection_id)
        if coll is None:
            raise HTTPException(404, "collection not found")
        return coll

    sets.append("updated_at = now()")
    params.append(collection_id)
    sql = f"UPDATE collections SET {', '.join(sets)} WHERE id = %s"
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(sql, params)
            if cur.rowcount == 0:
                raise HTTPException(404, "collection not found")
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, "collection name already exists")
    coll = _fetch_collection(conn, collection_id)
    assert coll is not None
    return coll


def delete_collection(
    conn: "psycopg.Connection", collection_id: int,
) -> dict[str, Any]:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM collections WHERE id = %s", (collection_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "collection not found")
    return {"deleted": True}


def add_listings_to_collection(
    conn: "psycopg.Connection",
    collection_id: int,
    body: s.AddListingsToCollectionIn,
) -> dict[str, Any]:
    """Insert a batch of (collection_id, sreality_id) rows.

    INSERT ... ON CONFLICT DO NOTHING — already-present pairs count as
    skipped, listings that aren't in the listings table fail the FK
    and are reported via 422. Bumps collections.updated_at on success.
    """
    if not body.sreality_ids:
        raise HTTPException(422, "sreality_ids must be non-empty")
    sql = (
        "INSERT INTO collection_listings (collection_id, sreality_id) "
        "SELECT %s, unnest(%s::bigint[]) "
        "ON CONFLICT (collection_id, sreality_id) DO NOTHING"
    )
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM collections WHERE id = %s", (collection_id,),
            )
            if cur.fetchone() is None:
                raise HTTPException(404, "collection not found")
            cur.execute(sql, (collection_id, body.sreality_ids))
            added = cur.rowcount
            cur.execute(
                "UPDATE collections SET updated_at = now() WHERE id = %s",
                (collection_id,),
            )
    except psycopg.errors.ForeignKeyViolation as exc:
        raise HTTPException(
            422, f"one or more sreality_ids do not exist: {exc}",
        )
    skipped = len(body.sreality_ids) - added
    return {"added": added, "skipped": skipped}


def remove_listing_from_collection(
    conn: "psycopg.Connection",
    collection_id: int,
    sreality_id: int,
) -> dict[str, Any]:
    sql = (
        "DELETE FROM collection_listings "
        "WHERE collection_id = %s AND sreality_id = %s"
    )
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(sql, (collection_id, sreality_id))
        removed = cur.rowcount > 0
        if removed:
            cur.execute(
                "UPDATE collections SET updated_at = now() WHERE id = %s",
                (collection_id,),
            )
    return {"removed": removed}


# --- notes -----------------------------------------------------------------


def list_notes(
    conn: "psycopg.Connection", sreality_id: int,
) -> dict[str, Any]:
    sql = (
        "SELECT id, sreality_id, body, created_at FROM listing_notes "
        "WHERE sreality_id = %s ORDER BY created_at DESC, id DESC"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (sreality_id,))
        rows = cur.fetchall()
    return {"data": [_to_note(r) for r in rows]}


def create_note(
    conn: "psycopg.Connection",
    sreality_id: int,
    body: s.CreateNoteIn,
) -> dict[str, Any]:
    sql = (
        "INSERT INTO listing_notes (sreality_id, body) VALUES (%s, %s) "
        "RETURNING id, sreality_id, body, created_at"
    )
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(sql, (sreality_id, body.body))
            row = cur.fetchone()
    except psycopg.errors.ForeignKeyViolation:
        raise HTTPException(404, "listing not found")
    assert row is not None
    return _to_note(row)


# --- tags ------------------------------------------------------------------

_TAG_FULL_PROJECTION = (
    "t.id, t.name, t.color, t.created_at, "
    "(SELECT count(*) FROM listing_tags lt WHERE lt.tag_id = t.id) "
    "  AS listing_count"
)


def list_tags(conn: "psycopg.Connection") -> dict[str, Any]:
    sql = f"SELECT {_TAG_FULL_PROJECTION} FROM tags t ORDER BY lower(t.name)"
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    return {"data": [_to_tag_full(r) for r in rows]}


def create_tag(
    conn: "psycopg.Connection", body: s.CreateTagIn,
) -> dict[str, Any]:
    sql = (
        "INSERT INTO tags (name, color) VALUES (%s, %s) "
        "RETURNING id, name, color, created_at"
    )
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(sql, (body.name, body.color))
            row = cur.fetchone()
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, "tag name already exists")
    assert row is not None
    return _to_tag(row, listing_count=0)


def delete_tag(
    conn: "psycopg.Connection", tag_id: int,
) -> dict[str, Any]:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM tags WHERE id = %s", (tag_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "tag not found")
    return {"deleted": True}


def attach_tag(
    conn: "psycopg.Connection",
    sreality_id: int,
    body: s.AttachTagIn,
) -> dict[str, Any]:
    """Attach a tag to a listing. Idempotent (ON CONFLICT DO NOTHING)."""
    sql = (
        "INSERT INTO listing_tags (sreality_id, tag_id) VALUES (%s, %s) "
        "ON CONFLICT (sreality_id, tag_id) DO NOTHING"
    )
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(sql, (sreality_id, body.tag_id))
            attached = cur.rowcount > 0
    except psycopg.errors.ForeignKeyViolation as exc:
        msg = str(exc).lower()
        target = "tag" if "tag_id" in msg else "listing"
        raise HTTPException(404, f"{target} not found")
    return {"attached": attached}


def detach_tag(
    conn: "psycopg.Connection",
    sreality_id: int,
    tag_id: int,
) -> dict[str, Any]:
    sql = (
        "DELETE FROM listing_tags WHERE sreality_id = %s AND tag_id = %s"
    )
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(sql, (sreality_id, tag_id))
        detached = cur.rowcount > 0
    return {"detached": detached}


# --- helpers ---------------------------------------------------------------


def _fetch_collection(
    conn: "psycopg.Connection", collection_id: int,
) -> dict[str, Any] | None:
    sql = (
        f"SELECT {_COLLECTION_FULL_PROJECTION} FROM collections c "
        "WHERE c.id = %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (collection_id,))
        row = cur.fetchone()
    if row is None:
        return None
    return _to_collection_full(row)


def _to_collection(row: tuple[Any, ...], *, listing_count: int) -> dict[str, Any]:
    return {
        "id":            int(row[0]),
        "name":          row[1],
        "description":   row[2],
        "created_at":    _iso(row[3]),
        "updated_at":    _iso(row[4]),
        "listing_count": listing_count,
    }


def _to_collection_full(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id":            int(row[0]),
        "name":          row[1],
        "description":   row[2],
        "created_at":    _iso(row[3]),
        "updated_at":    _iso(row[4]),
        "listing_count": int(row[5]),
    }


def _to_note(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id":          int(row[0]),
        "sreality_id": int(row[1]),
        "body":        row[2],
        "created_at":  _iso(row[3]),
    }


def _to_tag(row: tuple[Any, ...], *, listing_count: int) -> dict[str, Any]:
    return {
        "id":            int(row[0]),
        "name":          row[1],
        "color":         row[2],
        "created_at":    _iso(row[3]),
        "listing_count": listing_count,
    }


def _to_tag_full(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id":            int(row[0]),
        "name":          row[1],
        "color":         row[2],
        "created_at":    _iso(row[3]),
        "listing_count": int(row[4]),
    }


def _iso(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    return v
