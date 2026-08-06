"""Property merge MECHANICS — the operator's curation surface, not a decision engine.

Everything here starts from a merge the operator (or another caller) has already
ORDERED: collapse this explicit set of properties, list what was merged, undo a
group, browse the results, or link properties as one asset without collapsing them.
Nothing in this module decides *whether* two properties are the same.

The transaction mechanics live in `toolkit.property_identity` (`merge_properties` /
`unmerge_group` — operator state re-pointing, pipeline reconcile, browse sync, the
`property_merge_events` ledger) and `toolkit.asset_identity`; this module is the
HTTP + read layer over them. Mounted under `/properties/*`, admin-gated.
"""

from __future__ import annotations

from typing import Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api import dependencies as deps
from toolkit.asset_identity import (
    AssetError,
    get_asset,
    link_properties,
    unlink_property,
)
from toolkit.property_identity import MergeError, merge_properties, unmerge_group

router = APIRouter(prefix="/properties", tags=["properties"])


class PropertySetAction(BaseModel):
    property_ids: list[int]


class AssetLinkAction(BaseModel):
    property_ids: list[int]
    note: str | None = None


class AssetUnlinkAction(BaseModel):
    property_id: int


def merge_property_set(
    conn: psycopg.Connection, property_ids: list[int],
) -> dict[str, Any] | None:
    """Merge an explicit SET of properties into one (the operator-checked subset).

    Takes the property ids the operator ticked — "merge exactly these" — with no
    reference to any proposal or candidate edge. The oldest is the survivor; every
    other merges into it under one reversible group. None = nothing to do (fewer
    than two distinct ids).
    """
    ids = sorted({int(p) for p in property_ids})
    if len(ids) < 2:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM properties WHERE id = ANY(%s) AND status = 'active' "
            "ORDER BY first_seen_at ASC, id ASC",
            (ids,),
        )
        active = [int(r[0]) for r in cur.fetchall()]
    if len(active) < 2:
        raise MergeError("fewer than two active properties in the selection")
    survivor, retired_ids = active[0], active[1:]

    # One outer transaction so the subset merge is ATOMIC: each merge_properties
    # nests as a savepoint, so a later refusal (e.g. the category guard) rolls
    # the whole set back instead of committing a partial merge.
    group: str | None = None
    moved = 0
    with conn.transaction():
        for retired in retired_ids:
            result = merge_properties(
                conn, survivor_id=survivor, retired_id=retired,
                reason="manual_subset", source="operator", merge_group_id=group,
            )
            group = result["data"]["merge_group_id"]
            moved += int(result["data"]["listings_moved"])

    return {
        "merge_group_id": group,
        "survivor_id": survivor,
        "retired_ids": retired_ids,
        "listings_moved": moved,
    }


def _merged_property_filters(
    *,
    min_listings: int,
    max_listings: int | None,
    category_main: str | None,
) -> tuple[str, dict[str, Any]]:
    """Shared WHERE for list_merged_properties + its COUNT, so the page total
    can never drift from the page rows. Only live survivors (`status='active'`):
    a `merged_away` loser's children have already repointed to its survivor, so
    its `source_count` is stale."""
    clauses = ["p.status = 'active'", "p.source_count >= %(min_listings)s"]
    params: dict[str, Any] = {"min_listings": min_listings}
    if max_listings is not None:
        clauses.append("p.source_count <= %(max_listings)s")
        params["max_listings"] = max_listings
    if category_main:
        # A property carries ONE category_main (the survivor's) — plain equality.
        clauses.append("p.category_main = %(category_main)s")
        params["category_main"] = category_main
    return "WHERE " + " AND ".join(clauses), params


def list_merged_properties(
    conn: psycopg.Connection,
    *,
    min_listings: int = 2,
    max_listings: int | None = None,
    category_main: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Already-merged properties (survivors) whose child-listing count
    (`source_count` — every listing ever grouped under the property, active or
    delisted) is in [min_listings, max_listings]. The audit view for spotting
    over-merges — biggest groups first. Reads the base `properties` table
    (service role), so it sees rows the `*_public` views hide. The per-property
    portal list + active count come from a LATERAL over the children."""
    where_sql, params = _merged_property_filters(
        min_listings=min_listings,
        max_listings=max_listings,
        category_main=category_main,
    )
    with conn.cursor() as cur:
        # Real total for THIS filter (the page is capped at `limit`), sharing the
        # exact WHERE with the page SELECT so they can never disagree.
        cur.execute(f"SELECT count(*) FROM properties p {where_sql}", params)
        total = int(cur.fetchone()[0])

        cur.execute(
            f"""
            SELECT
              p.id, p.repr_listing_id, p.source_count, p.distinct_site_count,
              p.category_main, p.category_type, p.disposition, p.area_m2,
              p.estate_area, p.current_price_czk, p.district, p.street,
              p.first_seen_at, p.last_seen_at,
              agg.sources, agg.active_count
            FROM properties p
            LEFT JOIN LATERAL (
              SELECT array_agg(DISTINCT l.source ORDER BY l.source) AS sources,
                     count(*) FILTER (WHERE l.is_active)            AS active_count
              FROM listings l WHERE l.property_id = p.id
            ) agg ON true
            {where_sql}
            ORDER BY p.source_count DESC, p.id DESC
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            {**params, "limit": limit, "offset": offset},
        )
        rows = cur.fetchall()

    data = [
        {
            "property_id": r[0],
            "sreality_id": r[1],
            "source_count": r[2],
            "distinct_site_count": r[3],
            "category_main": r[4],
            "category_type": r[5],
            "disposition": r[6],
            "area_m2": float(r[7]) if r[7] is not None else None,
            "estate_area": float(r[8]) if r[8] is not None else None,
            "price_czk": r[9],
            "district": r[10],
            "street": r[11],
            "first_seen_at": r[12],
            "last_seen_at": r[13],
            "sources": list(r[14]) if r[14] is not None else [],
            "active_count": r[15],
        }
        for r in rows
    ]
    return {"data": data, "total": total, "returned": len(data)}


def list_merges(
    conn: psycopg.Connection, *, limit: int = 50, offset: int = 0,
) -> dict[str, Any]:
    """The merge ledger, one row per reversible group (newest first)."""
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


@router.post("/merge")
def post_merge_property_set(
    body: PropertySetAction,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Merge an explicit operator-chosen set of properties into one (subset merge)."""
    try:
        result = merge_property_set(conn, body.property_ids)
    except MergeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=400, detail="need at least two properties")
    return result


@router.get("/merges")
def get_merges(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """The merge ledger — one row per reversible group, newest first."""
    return list_merges(conn, limit=limit, offset=offset)


@router.post("/merges/{merge_group_id}/unmerge")
def post_unmerge(
    merge_group_id: str,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Undo one merge group (reversible curation, never a delete)."""
    try:
        return unmerge(conn, merge_group_id, undone_by="operator")
    except MergeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/merged")
def get_merged_properties(
    min_listings: int = Query(default=2, ge=1),
    max_listings: int | None = Query(default=None, ge=1),
    category_main: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Browse the RESULTS of merging: active properties whose child-listing count
    (`source_count`) is in [min_listings, max_listings], biggest groups first —
    the operator's over-merge audit. `category_main` narrows by property type."""
    return list_merged_properties(
        conn,
        min_listings=min_listings,
        max_listings=max_listings,
        category_main=category_main,
        limit=limit,
        offset=offset,
    )


# ----- asset links (same physical building, kept as separate cohorts) -------
# Unlike a merge these never collapse properties — both category facets survive.
# It is the surface for the cross-category sameness merge_properties refuses.


@router.post("/assets/link")
def post_asset_link(
    body: AssetLinkAction,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Link the chosen properties into one asset (same building)."""
    try:
        return link_properties(
            conn, property_ids=body.property_ids, source="operator",
            reason="manual_link", note=body.note, created_by="operator",
        )
    except AssetError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/assets/unlink")
def post_asset_unlink(
    body: AssetUnlinkAction,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Remove one property from its asset (dissolves the asset if <2 remain)."""
    try:
        return unlink_property(
            conn, property_id=body.property_id, reason="manual_unlink",
            created_by="operator",
        )
    except AssetError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/assets/{asset_id}")
def get_asset_route(
    asset_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """One asset link group and its member properties."""
    result = get_asset(conn, asset_id)
    if result is None:
        raise HTTPException(status_code=404, detail="asset not found")
    return result
