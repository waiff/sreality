"""FastAPI routes for broker intelligence reads.

Mounted under `/brokers/*` on real Supabase-Auth identity (`verify_jwt`), NOT the
static shared secret: that token is inlined into the SPA bundle and extractable
from it by design, so `require_token` here meant anyone holding the public bundle
could pull 2000 brokers' unmasked email + phone off the leaderboard (D1/D2 of the
2026-08-12 broker E2E review). Any logged-in user now reads the broker graph with
contact PII replaced by has_email / has_phone flags — `apply_pii_policy`, applied
to every envelope so a widened view can't leak through an un-guarded route — and
only an admin gets the values. The dedicated contacts endpoint is `require_admin`.

Thin HTTP layer over `toolkit.brokers`; every response is the standard envelope.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api import dependencies as deps
from toolkit import brokers

router = APIRouter(prefix="/brokers", tags=["brokers"])


class ListingIdsIn(BaseModel):
    listing_ids: list[int] = Field(default_factory=list, max_length=brokers.MAX_BATCH)


def _is_admin(claims: dict[str, Any]) -> bool:
    meta = claims.get("app_metadata") or {}
    return claims.get("is_admin") is True or meta.get("is_admin") is True


def _policy(envelope: dict[str, Any], claims: dict[str, Any]) -> dict[str, Any]:
    return brokers.apply_pii_policy(envelope, include_pii=_is_admin(claims))


@router.get("")
def get_brokers_by_ids(
    ids: list[int] = Query(default=[], max_length=brokers.MAX_BATCH),
    conn: Any = Depends(deps.get_db_conn),
    claims: dict = Depends(deps.verify_jwt),
) -> dict[str, Any]:
    return _policy(brokers.brokers_by_ids(conn, ids), claims)


@router.get("/leaderboard")
def get_leaderboard(
    region_ids: list[int] = Query(default=[]),
    okres_ids: list[int] = Query(default=[]),
    obec_ids: list[int] = Query(default=[]),
    category_main: str | None = None,
    category_type: str | None = None,
    metric: str = "active_property_count",
    limit: int = Query(default=100, ge=1, le=2000),
    firm_ids: list[int] = Query(default=[]),
    conn: Any = Depends(deps.get_db_conn),
    claims: dict = Depends(deps.verify_jwt),
) -> dict[str, Any]:
    return _policy(brokers.leaderboard(
        conn, region_ids=region_ids, okres_ids=okres_ids, obec_ids=obec_ids,
        category_main=category_main, category_type=category_type,
        metric=metric, limit=limit, firm_ids=firm_ids), claims)


@router.get("/firm-options")
def get_firm_options(
    q: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    conn: Any = Depends(deps.get_db_conn),
    claims: dict = Depends(deps.verify_jwt),
) -> dict[str, Any]:
    return _policy(brokers.firm_options(conn, q=q, limit=limit), claims)


@router.get("/search")
def get_search(
    q: str = Query(min_length=1),
    limit: int = Query(default=12, ge=1, le=100),
    conn: Any = Depends(deps.get_db_conn),
    claims: dict = Depends(deps.verify_jwt),
) -> dict[str, Any]:
    # The only read whose PREDICATE is caller-supplied, so the policy has to reach
    # the query itself: masking the projection alone left the ILIKE matching text
    # the response redacted, i.e. a guess-confirming oracle over it.
    admin = _is_admin(claims)
    return _policy(brokers.search(conn, q, limit=limit, include_pii=admin), claims)


@router.get("/geo-options")
def get_geo_options(
    geo_level: Literal["region", "okres"] | None = Query(default=None),
    conn: Any = Depends(deps.get_db_conn),
    claims: dict = Depends(deps.verify_jwt),
) -> dict[str, Any]:
    return _policy(brokers.geo_options(conn, geo_level=geo_level), claims)


@router.get("/by-listing")
def get_listing_broker_by_query(
    sreality_id: int | None = Query(default=None),
    listing_id: int | None = Query(default=None),
    conn: Any = Depends(deps.get_db_conn),
    claims: dict = Depends(deps.verify_jwt),
) -> dict[str, Any]:
    return _by_listing(conn, sreality_id, listing_id, claims)


@router.get("/by-listing/{sreality_id}")
def get_listing_broker(
    sreality_id: int,
    listing_id: int | None = Query(default=None),
    conn: Any = Depends(deps.get_db_conn),
    claims: dict = Depends(deps.verify_jwt),
) -> dict[str, Any]:
    return _by_listing(conn, sreality_id, listing_id, claims)


@router.post("/by-listings")
def post_listing_brokers(
    body: ListingIdsIn,
    conn: Any = Depends(deps.get_db_conn),
    claims: dict = Depends(deps.verify_jwt),
) -> dict[str, Any]:
    return _policy(brokers.listing_brokers(conn, body.listing_ids), claims)


@router.get("/{broker_id}")
def get_broker(
    broker_id: int,
    conn: Any = Depends(deps.get_db_conn),
    claims: dict = Depends(deps.verify_jwt),
) -> dict[str, Any]:
    result = brokers.get_broker(conn, broker_id)
    if result is None:
        raise HTTPException(status_code=404, detail="broker not found")
    return _policy(result, claims)


@router.get("/{broker_id}/listings")
def get_broker_listings(
    broker_id: int,
    limit: int = Query(default=500, ge=1, le=2000),
    conn: Any = Depends(deps.get_db_conn),
    claims: dict = Depends(deps.verify_jwt),
) -> dict[str, Any]:
    return _policy(brokers.broker_listings(conn, broker_id, limit=limit), claims)


@router.get("/{broker_id}/contacts")
def get_broker_contacts(
    broker_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    return brokers.broker_contacts(conn, broker_id)


def _by_listing(conn: Any, sreality_id: int | None, listing_id: int | None,
                claims: dict[str, Any]) -> dict[str, Any]:
    try:
        result = brokers.listing_broker(conn, sreality_id, listing_id=listing_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="listing has no attributed broker")
    return _policy(result, claims)
