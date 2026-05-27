"""Operator-facing dedup review: list candidate pairs, merge, dismiss, unmerge.

Thin DB layer over `property_identity_candidates` + `property_merge_events`. The
actual merge/unmerge transaction mechanics live in `toolkit.property_identity`
(shared with the Tier-2 sweep); this module is the API's read + orchestration
surface. Mounted under `/dedup/*` (see `api.routes.dedup`).
"""

from __future__ import annotations

from typing import Any

import psycopg

from toolkit.property_identity import MergeError, merge_properties, unmerge_group

# A property side rendered on a review card. Built in SQL so geom -> lat/lng and
# the display fields come straight off the canonical row.
_PROP_SIDE_SQL = """
  jsonb_build_object(
    'property_id',         {p}.id,
    'status',              {p}.status,
    'sreality_id',         {p}.repr_listing_id,
    'price_czk',           {p}.current_price_czk,
    'area_m2',             {p}.area_m2,
    'disposition',         {p}.disposition,
    'district',            {p}.district,
    'category_main',       {p}.category_main,
    'category_type',       {p}.category_type,
    'source_count',        {p}.source_count,
    'distinct_site_count', {p}.distinct_site_count,
    'first_seen_at',       {p}.first_seen_at,
    'lat',                 ST_Y({p}.geom::geometry),
    'lng',                 ST_X({p}.geom::geometry)
  )
"""


def list_candidates(
    conn: psycopg.Connection,
    *,
    status: str | None = "proposed",
    tier: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    clauses: list[str] = []
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if status is not None:
        clauses.append("c.status = %(status)s")
        params["status"] = status
    if tier is not None:
        clauses.append("c.tier = %(tier)s")
        params["tier"] = tier
    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    sql = f"""
        SELECT
          c.id, c.tier, c.status, c.confidence, c.markers_matched,
          c.auto_merged, c.merge_group_id::text, c.created_at, c.reviewed_at,
          {_PROP_SIDE_SQL.format(p="l")} AS left_property,
          {_PROP_SIDE_SQL.format(p="r")} AS right_property
        FROM property_identity_candidates c
        JOIN properties l ON l.id = c.left_property_id
        JOIN properties r ON r.id = c.right_property_id
        {where_sql}
        ORDER BY c.created_at DESC, c.id DESC
        LIMIT %(limit)s OFFSET %(offset)s
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    data = [
        {
            "id": r[0],
            "tier": r[1],
            "status": r[2],
            "confidence": float(r[3]) if r[3] is not None else None,
            "markers_matched": r[4],
            "auto_merged": r[5],
            "merge_group_id": r[6],
            "created_at": r[7],
            "reviewed_at": r[8],
            "left_property": r[9],
            "right_property": r[10],
        }
        for r in rows
    ]
    return {"data": data, "total": len(data)}


def merge_candidate(
    conn: psycopg.Connection, candidate_id: int,
) -> dict[str, Any] | None:
    """Merge a proposed candidate (survivor = the older property). None = 404."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT left_property_id, right_property_id, status, tier, "
            "confidence, markers_matched "
            "FROM property_identity_candidates WHERE id = %s",
            (candidate_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    left, right, status, _tier, confidence, markers = row
    if status != "proposed":
        raise MergeError(
            f"candidate {candidate_id} is not proposed (status={status})"
        )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM properties WHERE id IN (%s, %s) "
            "ORDER BY first_seen_at ASC, id ASC LIMIT 1",
            (left, right),
        )
        survivor = int(cur.fetchone()[0])
    retired = int(right) if survivor == int(left) else int(left)

    return merge_properties(
        conn,
        survivor_id=survivor,
        retired_id=retired,
        reason="manual",
        source="operator",
        confidence=float(confidence) if confidence is not None else None,
        markers=markers,
    )


def dismiss_candidate(
    conn: psycopg.Connection, candidate_id: int,
) -> dict[str, Any] | None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE property_identity_candidates "
            "SET status = 'dismissed', reviewed_at = now(), "
            "    reviewed_action = 'operator' "
            "WHERE id = %s AND status = 'proposed' RETURNING id",
            (candidate_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"id": candidate_id, "status": "dismissed"}


def list_merges(
    conn: psycopg.Connection, *, limit: int = 50, offset: int = 0,
) -> dict[str, Any]:
    sql = """
        SELECT
          merge_group_id::text,
          min(created_at)                       AS merged_at,
          max(survivor_property_id)             AS survivor_property_id,
          count(distinct retired_property_id)   AS retired_count,
          count(*)                              AS listings_moved,
          max(source)                           AS source,
          max(reason)                           AS reason,
          bool_and(undone_at IS NOT NULL)       AS fully_undone
        FROM property_merge_events
        GROUP BY merge_group_id
        ORDER BY min(created_at) DESC
        LIMIT %(limit)s OFFSET %(offset)s
    """
    with conn.cursor() as cur:
        cur.execute(sql, {"limit": limit, "offset": offset})
        rows = cur.fetchall()
    data = [
        {
            "merge_group_id": r[0],
            "merged_at": r[1],
            "survivor_property_id": r[2],
            "retired_count": r[3],
            "listings_moved": r[4],
            "source": r[5],
            "reason": r[6],
            "fully_undone": r[7],
        }
        for r in rows
    ]
    return {"data": data, "total": len(data)}


def unmerge(
    conn: psycopg.Connection, merge_group_id: str, *, undone_by: str,
) -> dict[str, Any]:
    """Reverse a merge group. Raises MergeError if it has no active events."""
    return unmerge_group(conn, merge_group_id=merge_group_id, undone_by=undone_by)
