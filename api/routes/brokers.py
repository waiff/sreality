"""FastAPI routes for broker intelligence reads.

Mounted under `/brokers/*`, bearer-gated by the standard `require_token` — broker
contacts are PII and not in the anon public views, so this is the gated server-side
path (the browser reads the non-PII subset directly from the public views). Thin
HTTP layer over `toolkit.brokers`; every response is the standard tool envelope.
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
    _: None = Depends(deps.require_token),
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
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return brokers.search(conn, q, limit=limit)


@router.get("/by-listing/{sreality_id}")
def get_listing_broker(
    sreality_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    result = brokers.listing_broker(conn, sreality_id)
    if result is None:
        raise HTTPException(status_code=404, detail="listing has no attributed broker")
    return result


@router.get("/{broker_id}")
def get_broker(
    broker_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
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
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return brokers.broker_listings(conn, broker_id, limit=limit)


@router.get("/{broker_id}/contacts")
def get_broker_contacts(
    broker_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return brokers.broker_contacts(conn, broker_id)
