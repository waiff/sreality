"""Property merge MECHANICS — the operator's curation surface, not a decision engine.

Everything here starts from a merge the operator (or another caller) has already
ORDERED: collapse this explicit set of properties, state how one property's adverts split,
list what was merged, or link properties as one asset without collapsing them.
Nothing in this module decides *whether* two properties are the same.

The one merge and the one undo live in `toolkit.property_identity` (`merge_property_set` /
`detach_listing` — the survivor rule, the asset-link carry, operator state, pipeline
reconcile, browse sync, the `property_merge_events` ledger and, for the operator, the
rulings of decision 8); the operator's split statement composes them in
`toolkit.property_split` (E919); asset links live in `toolkit.asset_identity`. This module is
the HTTP + read layer over them. Mounted under `/properties/*`, admin-gated.
"""

from __future__ import annotations

from typing import Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api import dependencies as deps
from toolkit.asset_identity import (
    AssetError,
    get_asset,
    link_properties,
    unlink_property,
)
from toolkit.property_identity import (
    MOVED,
    MergeError,
    detach_outcomes,
    listing_origins,
    merge_property_set,
    resolve_active_property_id,
)
from toolkit.property_split import REASON_MAX, SplitRefused, split_property, undo_split

router = APIRouter(prefix="/properties", tags=["properties"])


class PropertySetAction(BaseModel):
    property_ids: list[int]


class AssetLinkAction(BaseModel):
    property_ids: list[int]
    note: str | None = None


class AssetUnlinkAction(BaseModel):
    property_id: int


class SplitUndoRuling(BaseModel):
    listing_lo: int
    listing_hi: int
    verdict: str | None = None
    note: str | None = None
    reasons: list[str] = Field(default_factory=list)


class SplitUndo(BaseModel):
    """The undo a split's response issued, posted back verbatim."""

    call_id: str
    placements: dict[int, int] = Field(default_factory=dict)
    rulings: list[SplitUndoRuling] = Field(default_factory=list)


class SplitAction(BaseModel):
    """A statement (`adverts` shown, `separate` units, `keep_together`) or, alone, an `undo`."""

    adverts: list[int] | None = None
    separate: list[list[int]] = Field(default_factory=list)
    keep_together: bool | None = None
    # The operator's optional free-text reason (decision 8), kept on every ruling's note.
    reason: str | None = Field(default=None, max_length=REASON_MAX)
    confirm_retract: bool = False
    undo: SplitUndo | None = None


def _decider(claims: dict) -> str:
    decided_by = claims.get("email") or claims.get("sub")
    if not decided_by:
        raise HTTPException(status_code=403, detail="the admin identity carries no email")
    return str(decided_by)


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
              p.estate_area, p.current_price_czk,
              -- W4-a: ONE place string, the same server-composed label every
              -- serving view publishes -- not `p.district` + `p.street`, two
              -- legacy columns filled by two DIFFERENT children (best_geo and
              -- best_street) and rendered by nothing.
              location_display_label(ll.street_name, ll.house_number_cp,
                                     ll.house_number_co, ll.obec_name,
                                     ll.cast_obce_name, ll.country_code,
                                     ll.country_status) AS display_label,
              p.first_seen_at, p.last_seen_at,
              agg.sources, agg.active_count
            FROM properties p
            LEFT JOIN listing_location ll ON ll.listing_id = p.repr_listing_ref_id
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
            "display_label": r[10],
            "first_seen_at": r[11],
            "last_seen_at": r[12],
            "sources": list(r[13]) if r[13] is not None else [],
            "active_count": r[14],
        }
        for r in rows
    ]
    return {"data": data, "total": total, "returned": len(data)}


def list_merges(
    conn: psycopg.Connection, *, limit: int = 50, offset: int = 0,
) -> dict[str, Any]:
    """The merge ledger, one row per merge group (newest first)."""
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


@router.post("/merge")
def post_merge_property_set(
    body: PropertySetAction,
    conn: Any = Depends(deps.get_db_conn),
    claims: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Merge an explicit operator-chosen set of properties into its oldest record."""
    if len(set(body.property_ids)) < 2:
        raise HTTPException(status_code=400, detail="need at least two properties")
    try:
        return merge_property_set(
            conn, body.property_ids, source="operator", reason="manual_subset",
            decided_by=_decider(claims),
        )["data"]
    except MergeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/merges")
def get_merges(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """The merge ledger — one row per merge group, newest first."""
    return list_merges(conn, limit=limit, offset=offset)


@router.post("/{property_id}/split")
def post_split(
    property_id: int,
    body: SplitAction,
    conn: Any = Depends(deps.get_db_conn),
    claims: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """The operator's partition of one property's adverts, made true in one transaction (E919):
    each `separate` unit leaves as one record (back where it came from, or new), the rest stay,
    ruled one property when `keep_together`; `different` + a must-not-link across units. The
    property page's row split is `separate: [[id]], keep_together: false`. `{undo}` alone takes
    back the split whose response issued it. A refusal is `{code, message, ids}`; nothing is
    written."""
    decided_by = _decider(claims)
    try:
        if body.undo is not None:
            if body.model_fields_set - {"undo"}:
                raise HTTPException(status_code=400, detail={
                    "code": "invalid", "message": "an undo is sent alone", "ids": []})
            return undo_split(
                conn, property_id, call_id=body.undo.call_id, placements=body.undo.placements,
                rulings=[r.model_dump() for r in body.undo.rulings], decided_by=decided_by,
            )
        if body.adverts is None or body.keep_together is None:
            raise HTTPException(status_code=400, detail={
                "code": "invalid", "message": "a statement names adverts and keep_together",
                "ids": []})
        return split_property(
            conn, property_id, adverts=body.adverts, separate=body.separate,
            keep_together=body.keep_together, decided_by=decided_by, reason=body.reason,
            confirm_retract=body.confirm_retract,
        )
    except SplitRefused as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail()) from exc
    except MergeError as exc:
        raise HTTPException(status_code=409, detail={
            "code": "refused", "message": str(exc), "ids": []}) from exc


@router.get("/{property_id}/origins")
def get_origins(
    property_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Each advert's origin (where a split returns it) and the source and time of the merge
    that took it from there, all null when no standing merge moved it; what a detach would
    answer now (`detach_outcomes`), and `splittable` = that moves it."""
    survivor = resolve_active_property_id(conn, property_id)
    if survivor is None:
        raise HTTPException(status_code=404, detail=f"property {property_id} not found")
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM listings WHERE property_id = %s", (survivor,))
        ids = sorted(int(r[0]) for r in cur.fetchall())
    origins, outcomes = listing_origins(conn, ids), detach_outcomes(conn, ids)
    return {"property_id": survivor, "adverts": [
        dict(zip(("listing_id", "origin_property_id", "merge_source", "merged_at",
                  "detach_outcome", "splittable"),
                 (lid, *origins.get(lid, (None, None, None)), outcomes.get(lid),
                  outcomes.get(lid) in MOVED)))
        for lid in ids]}


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
