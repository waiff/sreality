"""Persistence helpers for /collections, /properties/{id}/notes, /tags.

CRUD over the property-grain curation tables (migration 202): collection
membership, tags and notes are keyed on `property_id` so operator curation
describes the real-world property and is dedup-stable (it follows the property
across merge/unmerge/split via toolkit.operator_state). Each handler is a plain
function returning a dict; the FastAPI routes in api/main.py wrap them with the
standard Depends(get_db_conn) + Depends(require_token).

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
from toolkit.property_identity import (
    resolve_active_property_id,
    resolve_active_property_ids,
)

if TYPE_CHECKING:
    pass


# --- collections -----------------------------------------------------------

# `listing_count` is kept as the field name for API/UI stability; it now counts
# member properties (the curation grain).
_COLLECTION_FULL_PROJECTION = (
    "c.id, c.name, c.description, c.created_at, c.updated_at, "
    "(SELECT count(*) FROM collection_properties cp "
    " WHERE cp.collection_id = c.id) AS listing_count, "
    "c.monitoring_enabled, c.notify_channels, c.is_system"
)


def create_collection(
    conn: "psycopg.Connection", body: s.CreateCollectionIn,
) -> dict[str, Any]:
    sql = (
        "INSERT INTO collections (name, description, monitoring_enabled, notify_channels) "
        "VALUES (%s, %s, %s, %s) "
        "RETURNING id, name, description, created_at, updated_at, "
        "          monitoring_enabled, notify_channels, is_system"
    )
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                sql,
                (body.name, body.description,
                 body.monitoring_enabled, body.notify_channels),
            )
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
    """Return the collection metadata + the property rows it holds.

    The embedded rows are the property rollups (joined server-side under the
    service role); `sreality_id` is the property's representative listing so the
    SPA can link to the detail page.
    """
    coll = _fetch_collection(conn, collection_id)
    if coll is None:
        raise HTTPException(404, "collection not found")

    properties_sql = (
        "SELECT cp.property_id, p.repr_listing_id, p.district, p.disposition, p.subtype, "
        "       p.area_m2, p.current_price_czk, p.last_seen_at, p.is_active, cp.added_at, "
        "       rl.source "
        "FROM collection_properties cp "
        "JOIN properties p ON p.id = cp.property_id "
        "LEFT JOIN listings rl ON rl.sreality_id = p.repr_listing_id "
        "WHERE cp.collection_id = %s "
        "ORDER BY cp.added_at DESC"
    )
    with conn.cursor() as cur:
        cur.execute(properties_sql, (collection_id,))
        rows = cur.fetchall()
    properties = [
        {
            "property_id":  int(r[0]),
            "sreality_id":  int(r[1]) if r[1] is not None else None,
            "district":     r[2],
            "disposition":  r[3],
            "subtype":      r[4],
            "area_m2":      float(r[5]) if r[5] is not None else None,
            "price_czk":    r[6],
            "last_seen_at": _iso(r[7]),
            "is_active":    bool(r[8]),
            "added_at":     _iso(r[9]),
            "source":       r[10],
        }
        for r in rows
    ]
    return {"collection": coll, "properties": properties}


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
    if body.monitoring_enabled is not None:
        sets.append("monitoring_enabled = %s")
        params.append(body.monitoring_enabled)
    if body.notify_channels is not None:
        sets.append("notify_channels = %s")
        params.append(body.notify_channels)
    if not sets:
        coll = _fetch_collection(conn, collection_id)
        if coll is None:
            raise HTTPException(404, "collection not found")
        return coll

    existing = _fetch_collection(conn, collection_id)
    if existing is None:
        raise HTTPException(404, "collection not found")
    if (
        body.name is not None
        and existing["is_system"]
        and body.name != existing["name"]
    ):
        raise HTTPException(409, "the system collection cannot be renamed")

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
        cur.execute(
            "SELECT is_system FROM collections WHERE id = %s", (collection_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, "collection not found")
        if row[0]:
            raise HTTPException(409, "the system collection cannot be deleted")
        cur.execute("DELETE FROM collections WHERE id = %s", (collection_id,))
    return {"deleted": True}


def add_properties_to_collection(
    conn: "psycopg.Connection",
    collection_id: int,
    body: s.AddPropertiesToCollectionIn,
) -> dict[str, Any]:
    """Insert a batch of (collection_id, property_id) rows.

    INSERT ... ON CONFLICT DO NOTHING — already-present pairs count as
    skipped, property_ids that aren't in the properties table fail the FK
    and are reported via 422. Bumps collections.updated_at on success.
    """
    if not body.property_ids:
        raise HTTPException(422, "property_ids must be non-empty")
    sql = (
        "INSERT INTO collection_properties (collection_id, property_id) "
        "SELECT %s, unnest(%s::bigint[]) "
        "ON CONFLICT (collection_id, property_id) DO NOTHING"
    )
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM collections WHERE id = %s", (collection_id,),
            )
            if cur.fetchone() is None:
                raise HTTPException(404, "collection not found")
            # Redirect merged-away property_ids to their live survivor so
            # membership never lands on a retired property (dedup-stability).
            resolved = resolve_active_property_ids(conn, body.property_ids)
            missing = [p for p in body.property_ids if p not in resolved]
            if missing:
                raise HTTPException(
                    422, f"one or more property_ids do not exist: {missing}",
                )
            survivor_ids = list({resolved[p] for p in body.property_ids})
            cur.execute(sql, (collection_id, survivor_ids))
            added = cur.rowcount
            cur.execute(
                "UPDATE collections SET updated_at = now() WHERE id = %s",
                (collection_id,),
            )
    except psycopg.errors.ForeignKeyViolation as exc:
        raise HTTPException(
            422, f"one or more property_ids do not exist: {exc}",
        )
    skipped = len(survivor_ids) - added
    return {"added": added, "skipped": skipped}


def remove_property_from_collection(
    conn: "psycopg.Connection",
    collection_id: int,
    property_id: int,
) -> dict[str, Any]:
    sql = (
        "DELETE FROM collection_properties "
        "WHERE collection_id = %s AND property_id = %s"
    )
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(sql, (collection_id, property_id))
        removed = cur.rowcount > 0
        if removed:
            cur.execute(
                "UPDATE collections SET updated_at = now() WHERE id = %s",
                (collection_id,),
            )
    return {"removed": removed}


# --- notes -----------------------------------------------------------------


def list_notes(
    conn: "psycopg.Connection", property_id: int,
) -> dict[str, Any]:
    sql = (
        "SELECT id, property_id, body, origin_listing_id, created_at "
        "FROM property_notes "
        "WHERE property_id = %s ORDER BY created_at DESC, id DESC"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (property_id,))
        rows = cur.fetchall()
    return {"data": [_to_note(r) for r in rows]}


def create_note(
    conn: "psycopg.Connection",
    property_id: int,
    body: s.CreateNoteIn,
) -> dict[str, Any]:
    # origin_listing_ref_id is the surrogate; origin_listing_id holds the legacy
    # sreality_id. Prefer a caller-supplied surrogate and derive the legacy handle
    # FROM it; only fall back to resolving legacy -> surrogate for a caller that
    # still sends the old field (post-Gate-2 that lookup returns NULL, which is
    # precisely why the surrogate has to be the driving value).
    sql = (
        "INSERT INTO property_notes "
        "  (property_id, body, origin_listing_id, origin_listing_ref_id) "
        "VALUES (%s, %s, "
        "  COALESCE(%s, (SELECT sreality_id FROM listings WHERE id = %s)), "
        "  COALESCE(%s, (SELECT id FROM listings WHERE sreality_id = %s))) "
        "RETURNING id, property_id, body, origin_listing_id, created_at"
    )
    try:
        with conn.transaction(), conn.cursor() as cur:
            pid = resolve_active_property_id(conn, property_id)
            if pid is None:
                raise HTTPException(404, "property not found")
            cur.execute(sql, (
                pid, body.body,
                body.origin_listing_id, body.origin_listing_ref_id,
                body.origin_listing_ref_id, body.origin_listing_id,
            ))
            row = cur.fetchone()
    except psycopg.errors.ForeignKeyViolation:
        raise HTTPException(404, "property not found")
    assert row is not None
    return _to_note(row)


# --- tags ------------------------------------------------------------------

_TAG_FULL_PROJECTION = (
    "t.id, t.name, t.color, t.created_at, "
    "(SELECT count(*) FROM property_tags pt WHERE pt.tag_id = t.id) "
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


def update_tag(
    conn: "psycopg.Connection",
    tag_id: int,
    body: s.UpdateTagIn,
) -> dict[str, Any]:
    """Rename and/or recolour a tag in place. Property attachments are
    untouched — the property_tags rows join by tag_id, not name."""
    sets: list[str] = []
    params: list[Any] = []
    if body.name is not None:
        sets.append("name = %s")
        params.append(body.name)
    if body.color is not None:
        sets.append("color = %s")
        params.append(body.color)
    if not sets:
        row = _fetch_tag(conn, tag_id)
        if row is None:
            raise HTTPException(404, "tag not found")
        return row
    params.append(tag_id)
    sql = f"UPDATE tags SET {', '.join(sets)} WHERE id = %s"
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(sql, params)
            if cur.rowcount == 0:
                raise HTTPException(404, "tag not found")
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, "tag name already exists")
    row = _fetch_tag(conn, tag_id)
    assert row is not None
    return row


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
    property_id: int,
    body: s.AttachTagIn,
) -> dict[str, Any]:
    """Attach a tag to a property. Idempotent (ON CONFLICT DO NOTHING)."""
    sql = (
        "INSERT INTO property_tags (property_id, tag_id) VALUES (%s, %s) "
        "ON CONFLICT (property_id, tag_id) DO NOTHING"
    )
    try:
        with conn.transaction(), conn.cursor() as cur:
            pid = resolve_active_property_id(conn, property_id)
            if pid is None:
                raise HTTPException(404, "property not found")
            cur.execute(sql, (pid, body.tag_id))
            attached = cur.rowcount > 0
    except psycopg.errors.ForeignKeyViolation as exc:
        msg = str(exc).lower()
        target = "tag" if "tag_id" in msg else "property"
        raise HTTPException(404, f"{target} not found")
    return {"attached": attached}


def detach_tag(
    conn: "psycopg.Connection",
    property_id: int,
    tag_id: int,
) -> dict[str, Any]:
    sql = (
        "DELETE FROM property_tags WHERE property_id = %s AND tag_id = %s"
    )
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(sql, (property_id, tag_id))
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


def _fetch_tag(
    conn: "psycopg.Connection", tag_id: int,
) -> dict[str, Any] | None:
    sql = f"SELECT {_TAG_FULL_PROJECTION} FROM tags t WHERE t.id = %s"
    with conn.cursor() as cur:
        cur.execute(sql, (tag_id,))
        row = cur.fetchone()
    if row is None:
        return None
    return _to_tag_full(row)


def _to_collection(row: tuple[Any, ...], *, listing_count: int) -> dict[str, Any]:
    return {
        "id":                 int(row[0]),
        "name":               row[1],
        "description":        row[2],
        "created_at":         _iso(row[3]),
        "updated_at":         _iso(row[4]),
        "listing_count":      listing_count,
        "monitoring_enabled": bool(row[5]),
        "notify_channels":    list(row[6]) if row[6] is not None else [],
        "is_system":          bool(row[7]),
    }


def _to_collection_full(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id":                 int(row[0]),
        "name":               row[1],
        "description":        row[2],
        "created_at":         _iso(row[3]),
        "updated_at":         _iso(row[4]),
        "listing_count":      int(row[5]),
        "monitoring_enabled": bool(row[6]),
        "notify_channels":    list(row[7]) if row[7] is not None else [],
        "is_system":          bool(row[8]),
    }


def _to_note(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id":                int(row[0]),
        "property_id":       int(row[1]),
        "body":              row[2],
        "origin_listing_id": int(row[3]) if row[3] is not None else None,
        "created_at":        _iso(row[4]),
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
