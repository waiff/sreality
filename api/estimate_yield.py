"""Composite: find_comparables + analyze_distribution + opinionated layer.

Sits outside toolkit/ deliberately. Toolkit functions return facts;
this module synthesises an estimate + confidence + warnings on top.

The endpoint does NOT call verify_listing_freshness on every comparable
(that would multiply load). It surfaces freshness statistics for the
cohort instead, so the agent can decide which comparables to verify.
"""

from __future__ import annotations

from datetime import datetime, timezone
from statistics import mean, median
from typing import TYPE_CHECKING, Any

from toolkit import (
    ComparableFilters,
    TargetSpec,
    analyze_distribution,
    find_comparables,
)

if TYPE_CHECKING:
    import psycopg


def estimate_yield(
    conn: "psycopg.Connection",
    target: TargetSpec,
    filters: ComparableFilters,
    purchase_price_czk: int | None = None,
) -> dict[str, Any]:
    cohort_res = find_comparables(conn, target, filters)
    listings = cohort_res["data"]["listings"]
    cohort_md = cohort_res["metadata"]

    if target.area_m2 is not None:
        dist = analyze_distribution(listings, field="price_per_m2")
        d = dist["data"]
        estimated, r25, r75 = _scale(d, target.area_m2)
    else:
        dist = analyze_distribution(listings, field="price_czk")
        d = dist["data"]
        estimated = _to_int(d.get("median"))
        r25 = _to_int(d.get("p25"))
        r75 = _to_int(d.get("p75"))

    sample_size = d["n"]
    gross_yield_pct = _gross_yield(estimated, purchase_price_czk)
    freshness = _freshness_block(listings)
    comparables_used = [_used_entry(l) for l in listings]
    verified_count = sum(
        1 for c in comparables_used if c["verified_during_estimate"]
    )
    confidence, warnings = _classify(
        sample_size, d, freshness, verified_count
    )

    return {
        "data": {
            "estimated_monthly_rent_czk": estimated,
            "rent_p25_czk": r25,
            "rent_p75_czk": r75,
            "sample_size": sample_size,
            "comparables_used": comparables_used,
            "data_freshness": freshness,
            "gross_yield_pct": gross_yield_pct,
            "confidence": confidence,
            "warnings": warnings,
        },
        "metadata": {
            "tool": "estimate_yield",
            "filters_used": cohort_md["filters_used"],
            "result_count": sample_size,
            "queried_at": _now_iso(),
            "data_freshness": cohort_md.get("data_freshness"),
            "underlying": {
                "find_comparables_count": cohort_md["result_count"],
                "analyze_distribution": dist["metadata"],
            },
        },
    }


def _scale(
    dist_data: dict[str, Any], area_m2: float
) -> tuple[int | None, int | None, int | None]:
    """Multiply per-m² percentiles by target area to get rent estimates."""
    median_v = dist_data.get("median")
    p25 = dist_data.get("p25")
    p75 = dist_data.get("p75")
    if median_v is None:
        return None, None, None
    estimated = int(round(median_v * area_m2))
    r25 = int(round(p25 * area_m2)) if p25 is not None else None
    r75 = int(round(p75 * area_m2)) if p75 is not None else None
    return estimated, r25, r75


def _to_int(v: Any) -> int | None:
    return int(round(v)) if v is not None else None


def _gross_yield(
    rent_czk: int | None, purchase_price_czk: int | None
) -> float | None:
    if rent_czk is None or not purchase_price_czk or purchase_price_czk <= 0:
        return None
    return round((rent_czk * 12) / purchase_price_czk * 100, 2)


def _used_entry(listing: dict[str, Any]) -> dict[str, Any]:
    return {
        "sreality_id": listing.get("sreality_id"),
        "snapshot_id": listing.get("latest_snapshot_id"),
        "snapshot_date": listing.get("latest_snapshot_at"),
        "data_age_days": listing.get("data_age_days"),
        "verified_during_estimate": (
            listing.get("last_freshness_check_at") is not None
        ),
    }


def _freshness_block(listings: list[dict[str, Any]]) -> dict[str, Any]:
    ages = [
        l["data_age_days"] for l in listings
        if isinstance(l.get("data_age_days"), int)
    ]
    if not ages:
        return {
            "oldest_data_age_days": None,
            "newest_data_age_days": None,
            "median_data_age_days": None,
            "mean_data_age_days": None,
            "stale_count": 0,
            "stale_pct": 0.0,
        }
    stale = [a for a in ages if a > 14]
    return {
        "oldest_data_age_days": max(ages),
        "newest_data_age_days": min(ages),
        "median_data_age_days": float(median(ages)),
        "mean_data_age_days": round(mean(ages), 2),
        "stale_count": len(stale),
        "stale_pct": round(100.0 * len(stale) / len(ages), 1),
    }


def _classify(
    n: int,
    dist_data: dict[str, Any],
    freshness: dict[str, Any],
    verified_count: int = 0,
) -> tuple[str, list[str]]:
    """Confidence rules — rederivable from inputs.

    high: n>=20 AND iqr/median < 0.25
    medium: n>=10 AND (iqr/median < 0.4 OR iqr unavailable)
    low: otherwise

    Then: median_age > 14 demotes one level. stale_pct > 50 forces low.
    """
    warnings: list[str] = []
    median_v = dist_data.get("median")
    iqr = dist_data.get("iqr")
    rel_iqr = (iqr / median_v) if (iqr is not None and median_v) else None

    if n >= 20 and rel_iqr is not None and rel_iqr < 0.25:
        confidence = "high"
    elif n >= 10 and (rel_iqr is None or rel_iqr < 0.4):
        confidence = "medium"
    else:
        confidence = "low"

    if n < 10:
        warnings.append(f"small sample ({n} comparables)")

    median_age = freshness.get("median_data_age_days")
    if median_age is not None and median_age > 14:
        confidence = _demote(confidence)
        warnings.append(
            f"cohort data is stale (median age {median_age:.0f} days)"
        )

    stale_pct = freshness.get("stale_pct", 0.0)
    if stale_pct > 50:
        confidence = "low"
        warnings.append(
            "more than half of comparables have not been seen in over 14 days"
        )

    oldest = freshness.get("oldest_data_age_days")
    if oldest is not None and oldest > 30:
        warnings.append(
            f"oldest comparable was last seen {oldest} days ago"
        )

    if verified_count > 0:
        warnings.append(
            f"only {verified_count} comparables have been verified during this estimate"
        )

    return confidence, warnings


def _demote(level: str) -> str:
    return {"high": "medium", "medium": "low", "low": "low"}.get(level, level)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
