"""verify_listing_freshness: on-demand re-fetch with throttle window.

Wraps scraper.freshness.freshness_check. Returns cached data if the
listing's effective age is below max_age_hours; otherwise triggers a
real refetch and returns its outcome.

This is the explicit exception to the "toolkit is read-only" rule —
verify_listing_freshness may trigger writes via the wrapped
freshness_check (snapshot insert, listings.is_active flip, audit log).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import psycopg

    from scraper.sreality_client import SrealityClient


_LISTING_COLS: tuple[str, ...] = (
    "sreality_id", "first_seen_at", "last_seen_at", "is_active",
    "category_main", "category_type", "price_czk", "price_unit",
    "area_m2", "disposition", "locality", "district",
    "locality_district_id", "locality_region_id",
    "floor", "total_floors",
    "has_balcony", "has_parking", "has_lift",
    "building_type", "condition", "energy_rating",
)


def verify_listing_freshness(
    conn: "psycopg.Connection",
    client: "SrealityClient",
    sreality_id: int | None = None,
    max_age_hours: int = 24,
    *,
    listing_id: int | None = None,
) -> dict[str, Any]:
    from toolkit import _now_iso

    if sreality_id is None and listing_id is None:
        raise ValueError(
            "verify_listing_freshness requires a sreality_id or listing_id"
        )

    listing = _fetch_listing(conn, sreality_id=sreality_id, listing_id=listing_id)
    # The refetch (sreality.cz) and listing_freshness_checks are sreality-native
    # — the latter has no listing_id column — so key the rest of the flow on the
    # subject's own sreality_id: the input when given, else the resolved row's.
    # `sid` is separate from the original selector so the re-fetch below keeps
    # addressing the row the caller named (a NULL-sreality row stays reachable).
    sid = (
        sreality_id if sreality_id is not None
        else (listing["sreality_id"] if listing else None)
    )
    last_check_at = _fetch_last_check_at(conn, sid)
    last_seen_at = listing["last_seen_at"] if listing else None
    age_hours = _effective_age_hours(last_seen_at, last_check_at)

    if (
        listing is not None
        and age_hours is not None
        and age_hours < max_age_hours
    ):
        return _envelope(
            sid, max_age_hours,
            outcome="cached", verified=False, cached=True,
            age_hours=age_hours,
            what_changed=[],
            snapshot_id=_fetch_latest_snapshot_id(conn, sid),
            current=_serialize_listing(listing),
            data_freshness_iso=_iso(last_seen_at),
            queried_at=_now_iso(),
        )

    from scraper import freshness as scraper_freshness
    res = scraper_freshness.freshness_check(conn, client, sid)
    current = _fetch_listing(conn, sreality_id=sreality_id, listing_id=listing_id)

    if res["outcome"] == "fetch_error":
        post_age = age_hours
        data_freshness_iso = _iso(last_seen_at)
    else:
        post_age = 0.0
        data_freshness_iso = res["checked_at"]

    return _envelope(
        sid, max_age_hours,
        outcome=res["outcome"],
        verified=True, cached=False,
        age_hours=post_age,
        what_changed=res["what_changed"],
        snapshot_id=res["snapshot_id"],
        current=_serialize_listing(current) if current else None,
        data_freshness_iso=data_freshness_iso,
        queried_at=_now_iso(),
    )


def _envelope(
    sreality_id: int,
    max_age_hours: int,
    *,
    outcome: str,
    verified: bool,
    cached: bool,
    age_hours: float | None,
    what_changed: list[str],
    snapshot_id: int | None,
    current: dict[str, Any] | None,
    data_freshness_iso: str | None,
    queried_at: str,
) -> dict[str, Any]:
    return {
        "data": {
            "sreality_id": sreality_id,
            "outcome": outcome,
            "verified": verified,
            "cached": cached,
            "age_hours": age_hours,
            "what_changed": what_changed,
            "snapshot_id": snapshot_id,
            "current": current,
        },
        "metadata": {
            "tool": "verify_listing_freshness",
            "filters_used": {
                "sreality_id": sreality_id,
                "max_age_hours": max_age_hours,
            },
            "result_count": 1,
            "queried_at": queried_at,
            "data_freshness": data_freshness_iso,
        },
    }


def _fetch_listing(
    conn: "psycopg.Connection",
    sreality_id: int | None = None,
    *,
    listing_id: int | None = None,
) -> dict[str, Any] | None:
    from toolkit import _listing_id_clause

    id_clause, id_val = _listing_id_clause(sreality_id, listing_id)
    cols_sql = ", ".join(_LISTING_COLS)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {cols_sql} FROM listings WHERE {id_clause}",
            (id_val,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return dict(zip(_LISTING_COLS, row))


def _fetch_last_check_at(
    conn: "psycopg.Connection", sreality_id: int
) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT checked_at FROM listing_freshness_checks
            WHERE sreality_id = %s
            ORDER BY checked_at DESC
            LIMIT 1
            """,
            (sreality_id,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _fetch_latest_snapshot_id(
    conn: "psycopg.Connection", sreality_id: int
) -> int | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM listing_snapshots
            WHERE sreality_id = %s
            ORDER BY scraped_at DESC LIMIT 1
            """,
            (sreality_id,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _effective_age_hours(
    last_seen_at: datetime | None,
    last_check_at: datetime | None,
) -> float | None:
    candidates = [t for t in (last_seen_at, last_check_at) if t is not None]
    if not candidates:
        return None
    most_recent = max(candidates)
    return (datetime.now(timezone.utc) - most_recent).total_seconds() / 3600


def _serialize_listing(listing: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in listing.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, Decimal):
            out[k] = float(v)
        else:
            out[k] = v
    return out


def _iso(t: datetime | None) -> str | None:
    return t.isoformat() if isinstance(t, datetime) else None
