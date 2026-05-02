"""FastAPI service exposing the analytical toolkit + estimate_yield.

Routes return the standard toolkit envelope verbatim. No bespoke
response shaping; the agent layer consumes the dicts directly.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import Depends, FastAPI

from api import dependencies as deps
from api import schemas as s
from api.estimate_yield import estimate_yield
from toolkit import (
    ComparableFilters,
    TargetSpec,
    analyze_distribution,
    compare_snapshots,
    find_comparables,
    verify_listing_freshness,
)

app = FastAPI(title="sreality toolkit API", version="0.2.5")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/tools/find_comparables")
def post_find_comparables(
    body: s.FindComparablesIn,
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    target, filters = _build_comparables_inputs(body)
    return find_comparables(conn, target, filters)


@app.post("/tools/analyze_distribution")
def post_analyze_distribution(
    body: s.AnalyzeDistributionIn,
) -> dict[str, Any]:
    return analyze_distribution(body.listings, field=body.field)


@app.post("/tools/verify_listing_freshness")
def post_verify_listing_freshness(
    body: s.VerifyFreshnessIn,
    conn: Any = Depends(deps.get_db_conn),
    client: Any = Depends(deps.get_sreality_client),
) -> dict[str, Any]:
    return verify_listing_freshness(
        conn, client, body.sreality_id, body.max_age_hours,
    )


@app.post("/tools/compare_snapshots")
def post_compare_snapshots(
    body: s.CompareSnapshotsIn,
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    since = (
        timedelta(days=body.since_days)
        if body.since_days is not None
        else None
    )
    return compare_snapshots(conn, body.sreality_id, since)


@app.post("/estimate_yield")
def post_estimate_yield(
    body: s.EstimateYieldIn,
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    target = TargetSpec(
        lat=body.target.lat,
        lng=body.target.lng,
        area_m2=body.target.area_m2,
        disposition=body.target.disposition,
        floor=body.target.floor,
        exclude_ids=list(body.target.exclude_ids),
    )
    filters = ComparableFilters(
        radius_m=body.radius_m,
        area_band_pct=body.area_band_pct,
        disposition_match=body.disposition_match,
        max_age_days=body.max_age_days,
        floor_band=body.floor_band,
        locality_district_id=body.locality_district_id,
    )
    return estimate_yield(conn, target, filters, body.purchase_price_czk)


def _build_comparables_inputs(
    body: s.FindComparablesIn,
) -> tuple[TargetSpec, ComparableFilters]:
    target = TargetSpec(
        lat=body.target.lat,
        lng=body.target.lng,
        area_m2=body.target.area_m2,
        disposition=body.target.disposition,
        floor=body.target.floor,
        exclude_ids=list(body.target.exclude_ids),
    )
    filters = ComparableFilters(
        radius_m=body.radius_m,
        area_band_pct=body.area_band_pct,
        disposition_match=body.disposition_match,
        max_age_days=body.max_age_days,
        active_only=body.active_only,
        floor_band=body.floor_band,
        condition_match=body.condition_match,
        building_type_match=body.building_type_match,
        energy_rating_match=body.energy_rating_match,
        has_balcony=body.has_balcony,
        has_lift=body.has_lift,
        has_parking=body.has_parking,
        min_price_czk=body.min_price_czk,
        max_price_czk=body.max_price_czk,
        category_main=body.category_main,
        category_type=body.category_type,
        locality_district_id=body.locality_district_id,
        locality_region_id=body.locality_region_id,
        include_unreliable=body.include_unreliable,
    )
    return target, filters
