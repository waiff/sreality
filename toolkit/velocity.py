"""compute_market_velocity and compute_listing_velocity: TOM analytics.

TOM (time on market) per listing:
  - active listing: now() - first_seen_at  (still going, right-censored)
  - delisted:       last_seen_at - first_seen_at  (final sojourn)

The SQL only fetches the three timestamp columns per listing; statistics
and classification happen in Python.

Active vs delisted matters interpretively: TOM-so-far on active listings
is right-censored (they haven't finished yet), while delisted TOM is
final. Mixing them via lifecycle="all" gives the broadest picture but
the agent should reason about the mix.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from toolkit.comparables import (
    ComparableFilters,
    TargetSpec,
    _lifecycle_where,
    _shared_filter_where,
)

if TYPE_CHECKING:
    import psycopg


_VelocityLifecycle = Literal["active", "delisted", "all"]
_HARD_LIMIT = 5000


VELOCITY_BANDS = {
    "fast": 25.0,    # percentile <= 25
    "slow": 75.0,    # 75 <= percentile < 90
    "stuck": 90.0,   # percentile >= 90
    # everything else is "typical"
}


def build_market_velocity_query(
    target: TargetSpec,
    filters: ComparableFilters,
    lifecycle: _VelocityLifecycle,
) -> tuple[str, dict[str, Any]]:
    """Render SQL + params. Exposed for hermetic tests."""
    where, params = _shared_filter_where(target, filters)
    # Velocity gates lifecycle without a recency window (TOM analytics want
    # the full sojourn), so no max_age_days is passed.
    life_where, life_params = _lifecycle_where(lifecycle)
    where.extend(life_where)
    params.update(life_params)

    sql = (
        "SELECT l.sreality_id, l.first_seen_at, l.last_seen_at, l.is_active\n"
        "FROM listings l\n"
        "WHERE " + "\n  AND ".join(where) + "\n"
        "ORDER BY l.first_seen_at\n"
        f"LIMIT {_HARD_LIMIT}"
    )
    return sql, params


def compute_market_velocity(
    conn: "psycopg.Connection",
    target: TargetSpec,
    filters: ComparableFilters,
    lifecycle: _VelocityLifecycle = "all",
    trend_split_days: int = 7,
) -> dict[str, Any]:
    from toolkit import _now_iso

    sql, params = build_market_velocity_query(target, filters, lifecycle)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    now = datetime.now(timezone.utc)
    cohort = [
        {
            "sreality_id": r[0],
            "first_seen_at": r[1],
            "last_seen_at": r[2],
            "is_active": r[3],
            "tom_days": _tom_days(r[1], r[2], r[3], now=now),
        }
        for r in rows
    ]

    active_count = sum(1 for c in cohort if c["is_active"])
    delisted_count = len(cohort) - active_count
    tom_values = [c["tom_days"] for c in cohort if c["tom_days"] is not None]
    tom_stats = _stats(tom_values)

    cutoff = now.timestamp() - trend_split_days * 86400
    recent_tom = [
        c["tom_days"] for c in cohort
        if c["tom_days"] is not None
        and c["first_seen_at"].timestamp() > cutoff
    ]
    older_tom = [
        c["tom_days"] for c in cohort
        if c["tom_days"] is not None
        and c["first_seen_at"].timestamp() <= cutoff
    ]

    notes: list[str] = []
    if len(cohort) < 5:
        notes.append(
            f"cohort size {len(cohort)} below 5; stats are noisy"
        )
    if active_count > 0 and delisted_count == 0 and lifecycle == "all":
        notes.append(
            "cohort contains no delisted listings; TOM is right-censored"
        )

    return {
        "data": {
            "cohort_size": len(cohort),
            "active_count": active_count,
            "delisted_count": delisted_count,
            "lifecycle": lifecycle,
            "tom_stats": tom_stats,
            "trend": {
                "split_days": trend_split_days,
                "recent": {
                    "n": len(recent_tom),
                    "median_tom_days": _median_or_none(recent_tom),
                },
                "older": {
                    "n": len(older_tom),
                    "median_tom_days": _median_or_none(older_tom),
                },
            },
        },
        "metadata": {
            "tool": "compute_market_velocity",
            "filters_used": _filters_used(target, filters, lifecycle, trend_split_days),
            "result_count": len(cohort),
            "queried_at": _now_iso(),
            "data_freshness": _max_last_seen_dt(cohort),
            **({"notes": notes} if notes else {}),
        },
    }


def compute_listing_velocity(
    conn: "psycopg.Connection",
    sreality_id: int | None = None,
    radius_m: int = 1000,
    disposition_match: Literal["exact", "loose", "any"] = "exact",
    lifecycle: _VelocityLifecycle = "all",
    *,
    listing_id: int | None = None,
) -> dict[str, Any]:
    """Percentile-rank a single listing against its peer cohort.

    Cohort is built from the listing's own lat/lng/disposition with the
    given radius. The target listing itself is excluded from the cohort
    so the percentile is "compared to peers", not "self vs self+peers".

    Addressable by either the portal-native sreality_id or the surrogate
    listing_id; the surrogate wins if both are given.
    """
    from toolkit import _now_iso

    listing = _fetch_listing_for_velocity(
        conn, sreality_id, listing_id=listing_id,
    )
    if listing is None:
        return _listing_envelope(
            sreality_id=sreality_id,
            listing_id=listing_id,
            radius_m=radius_m,
            disposition_match=disposition_match,
            lifecycle=lifecycle,
            data={"sreality_id": sreality_id, "found": False},
            queried_at=_now_iso(),
        )

    now = datetime.now(timezone.utc)
    target_tom = _tom_days(
        listing["first_seen_at"], listing["last_seen_at"],
        listing["is_active"], now=now,
    )

    if listing["lat"] is None or listing["lng"] is None:
        return _listing_envelope(
            sreality_id=sreality_id,
            listing_id=listing_id,
            radius_m=radius_m,
            disposition_match=disposition_match,
            lifecycle=lifecycle,
            data={
                "sreality_id": sreality_id,
                "found": True,
                "is_active": listing["is_active"],
                "tom_days": target_tom,
                "cohort_size": 0,
                "tom_percentile": None,
                "classification": None,
                "thresholds": dict(VELOCITY_BANDS),
            },
            queried_at=_now_iso(),
            notes=["listing has no geom; cannot build peer cohort"],
        )

    target = TargetSpec(
        lat=listing["lat"],
        lng=listing["lng"],
        disposition=listing["disposition"],
        # Exclude the subject from its own peer cohort on the id-space it was
        # addressed by. `exclude_ids` (sreality) carries a NULL guard in
        # _shared_filter_where; `exclude_listing_ids` (surrogate) is the only
        # arm that can exclude a subject with no sreality_id.
        exclude_ids=[sreality_id] if sreality_id is not None else [],
        exclude_listing_ids=[listing_id] if listing_id is not None else [],
    )
    filters = ComparableFilters(
        radius_m=radius_m,
        disposition_match=disposition_match,
        # lifecycle is left at the default (None) on the cohort filters;
        # the velocity `lifecycle` arg drives the is_active gate instead.
        # Peers must be the same category as the subject; otherwise a
        # house would be ranked against apartments. ComparableFilters no
        # longer defaults to byt/pronajem, so carry the subject's own
        # category explicitly.
        category_main=listing["category_main"],
        category_type=listing["category_type"],
    )
    sql, params = build_market_velocity_query(target, filters, lifecycle)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        peer_rows = cur.fetchall()

    peer_toms = [
        t for t in (
            _tom_days(r[1], r[2], r[3], now=now) for r in peer_rows
        ) if t is not None
    ]

    percentile = _percentile_rank(target_tom, peer_toms) if (
        target_tom is not None and peer_toms
    ) else None

    classification = _classify_velocity(percentile)

    data = {
        "sreality_id": sreality_id,
        "found": True,
        "is_active": listing["is_active"],
        "tom_days": target_tom,
        "cohort_size": len(peer_toms),
        "tom_percentile": percentile,
        "classification": classification,
        "thresholds": dict(VELOCITY_BANDS),
    }
    if len(peer_toms) < 5:
        notes = [f"peer cohort size {len(peer_toms)} below 5; classification unreliable"]
    else:
        notes = []
    return _listing_envelope(
        sreality_id=sreality_id,
        listing_id=listing_id,
        radius_m=radius_m,
        disposition_match=disposition_match,
        lifecycle=lifecycle,
        data=data,
        queried_at=_now_iso(),
        notes=notes,
    )


def _fetch_listing_for_velocity(
    conn: "psycopg.Connection",
    sreality_id: int | None = None,
    *,
    listing_id: int | None = None,
) -> dict[str, Any] | None:
    from toolkit import _listing_id_clause

    id_clause, id_val = _listing_id_clause(sreality_id, listing_id)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT first_seen_at, last_seen_at, is_active, disposition,\n"
            "  ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lng,\n"
            "  category_main, category_type\n"
            f"FROM listings WHERE {id_clause}",
            (id_val,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "first_seen_at": row[0],
        "last_seen_at": row[1],
        "is_active": row[2],
        "disposition": row[3],
        "lat": float(row[4]) if row[4] is not None else None,
        "lng": float(row[5]) if row[5] is not None else None,
        "category_main": row[6],
        "category_type": row[7],
    }


def _tom_days(
    first_seen: datetime | None,
    last_seen: datetime | None,
    is_active: bool | None,
    *,
    now: datetime,
) -> int | None:
    if first_seen is None:
        return None
    end = now if is_active else (last_seen or now)
    delta = end - first_seen
    return max(0, delta.days)


def _stats(values: list[int]) -> dict[str, Any]:
    if not values:
        return {
            "median_days": None, "p25_days": None, "p75_days": None,
            "min_days": None, "max_days": None, "n": 0,
        }
    sorted_values = sorted(values)
    return {
        "median_days": float(statistics.median(sorted_values)),
        "p25_days": _pct(sorted_values, 25),
        "p75_days": _pct(sorted_values, 75),
        "min_days": min(sorted_values),
        "max_days": max(sorted_values),
        "n": len(sorted_values),
    }


def _pct(sorted_values: list[int], p: float) -> float | None:
    n = len(sorted_values)
    if n == 0:
        return None
    if n == 1:
        return float(sorted_values[0])
    k = (n - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, n - 1)
    frac = k - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def _median_or_none(values: list[int]) -> float | None:
    return float(statistics.median(values)) if values else None


def _percentile_rank(value: int, peers: list[int]) -> float:
    """Mid-rank percentile of `value` within `peers` (0..100).

    Ties are split: a value equal to all peers gets 50, not 100. Avoids
    classifying an exactly-average TOM as 'stuck'.
    """
    n = len(peers)
    if n == 0:
        return 0.0
    lt = sum(1 for p in peers if p < value)
    eq = sum(1 for p in peers if p == value)
    return round(100.0 * (lt + 0.5 * eq) / n, 2)


def _classify_velocity(percentile: float | None) -> str | None:
    if percentile is None:
        return None
    if percentile >= VELOCITY_BANDS["stuck"]:
        return "stuck"
    if percentile >= VELOCITY_BANDS["slow"]:
        return "slow"
    if percentile <= VELOCITY_BANDS["fast"]:
        return "fast"
    return "typical"


def _max_last_seen_dt(cohort: list[dict[str, Any]]) -> str | None:
    stamps = [c["last_seen_at"] for c in cohort if c["last_seen_at"] is not None]
    return max(stamps).isoformat() if stamps else None


def _filters_used(
    target: TargetSpec,
    filters: ComparableFilters,
    lifecycle: str,
    trend_split_days: int,
) -> dict[str, Any]:
    return {
        "target": {
            "lat": target.lat,
            "lng": target.lng,
            "disposition": target.disposition,
            "area_m2": target.area_m2,
            "floor": target.floor,
            "exclude_ids": list(target.exclude_ids),
        },
        "radius_m": filters.radius_m,
        "disposition_match": filters.disposition_match,
        "area_band_pct": filters.area_band_pct,
        "floor_band": filters.floor_band,
        "portals": list(filters.portals) if filters.portals else None,
        "condition_match": (
            list(filters.condition_match) if filters.condition_match else None
        ),
        "building_type_match": (
            list(filters.building_type_match) if filters.building_type_match else None
        ),
        "energy_rating_match": (
            list(filters.energy_rating_match) if filters.energy_rating_match else None
        ),
        "has_balcony": filters.has_balcony,
        "has_lift": filters.has_lift,
        "has_parking": filters.has_parking,
        "min_price_czk": filters.min_price_czk,
        "max_price_czk": filters.max_price_czk,
        "category_main": filters.category_main,
        "category_type": filters.category_type,
        "locality_district_id": filters.locality_district_id,
        "locality_region_id": filters.locality_region_id,
        "include_unreliable": filters.include_unreliable,
        "tom_days_min": filters.tom_days_min,
        "tom_days_max": filters.tom_days_max,
        "last_seen_min_days": filters.last_seen_min_days,
        "last_seen_max_days": filters.last_seen_max_days,
        "first_seen_min_days": filters.first_seen_min_days,
        "first_seen_max_days": filters.first_seen_max_days,
        "lifecycle": lifecycle,
        "trend_split_days": trend_split_days,
    }


def _listing_envelope(
    *,
    sreality_id: int | None,
    radius_m: int,
    disposition_match: str,
    lifecycle: str,
    data: dict[str, Any],
    queried_at: str,
    listing_id: int | None = None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    filters_used: dict[str, Any] = {
        "sreality_id": sreality_id,
        "radius_m": radius_m,
        "disposition_match": disposition_match,
        "lifecycle": lifecycle,
    }
    # Echo the surrogate handle only when the caller addressed by it, so the
    # sreality_id path stays byte-identical.
    if listing_id is not None:
        data["listing_id"] = listing_id
        filters_used["listing_id"] = listing_id
    metadata: dict[str, Any] = {
        "tool": "compute_listing_velocity",
        "filters_used": filters_used,
        "result_count": data.get("cohort_size", 0),
        "queried_at": queried_at,
        "data_freshness": None,
    }
    if notes:
        metadata["notes"] = notes
    return {"data": data, "metadata": metadata}
