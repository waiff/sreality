"""FastAPI routes for broker intelligence reads.

Mounted under `/brokers/*` (own prefix so it never collides with `/broker-review/*`),
admin-gated via `require_admin` — every broker-* view/function is revoked from
`anon` AND `authenticated` at the DB layer (Phase 0 Amendment A6, migration 299:
broker PII stays dark to non-admin sessions until Wave 4 ships masked columns), so
a weaker gate here would just reopen the same exposure one layer up. This is the
ONLY read path for broker data — frontend/src/lib/brokers.ts calls these routes
instead of reading the public views/RPC directly. Thin HTTP layer over
`toolkit.brokers`; every response is the standard tool envelope.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from api import dependencies as deps
from toolkit import brokers

router = APIRouter(prefix="/brokers", tags=["brokers"])


@router.get("/leaderboard")
def get_leaderboard(
    region_ids: list[int] = Query(default=[]),
    okres_ids: list[int] = Query(default=[]),
    obec_ids: list[int] = Query(default=[]),
    category_main: str | None = None,
    category_type: str | None = None,
    metric: str = "active_property_count",
    limit: int = Query(default=100, ge=1, le=2000),
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    return brokers.leaderboard(
        conn, region_ids=region_ids, okres_ids=okres_ids, obec_ids=obec_ids,
        category_main=category_main, category_type=category_type,
        metric=metric, limit=limit)


@router.get("/search")
def get_search(
    q: str = Query(min_length=1),
    limit: int = Query(default=12, ge=1, le=100),
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    return brokers.search(conn, q, limit=limit)


@router.get("/by-listing")
def get_listing_brokers_by_ids(
    listing_ids: list[int] = Query(default=[], max_length=500),
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Batched sibling of GET /brokers/by-listing/{listing_id} — one round trip
    for N cards (Pipeline board, Browse) instead of N requests."""
    return brokers.listing_brokers_by_ids(conn, listing_ids)


@router.get("/by-listing/{listing_id}")
def get_listing_broker(
    listing_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    result = brokers.listing_broker(conn, listing_id)
    if result is None:
        raise HTTPException(status_code=404, detail="listing has no attributed broker")
    return result


@router.get("/by-ids")
def get_brokers_by_ids(
    broker_ids: list[int] = Query(default=[], max_length=500),
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Batched broker-contact lookup — pairs with GET /brokers/by-listing to fill
    N cards' hover contact boxes without a per-card round trip."""
    return brokers.brokers_by_ids(conn, broker_ids)


@router.get("/{broker_id}")
def get_broker(
    broker_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    result = brokers.get_broker(conn, broker_id)
    if result is None:
        raise HTTPException(status_code=404, detail="broker not found")
    return result


@router.get("/{broker_id}/listings")
def get_broker_listings(
    broker_id: int,
    limit: int = Query(default=500, ge=1, le=2000),
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    return brokers.broker_listings(conn, broker_id, limit=limit)


@router.get("/{broker_id}/contacts")
def get_broker_contacts(
    broker_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    return brokers.broker_contacts(conn, broker_id)
