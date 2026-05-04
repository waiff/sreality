"""FastAPI service exposing the analytical toolkit + estimate_yield.

Routes return the standard toolkit envelope verbatim. No bespoke
response shaping; the agent layer consumes the dicts directly.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query

from api import dependencies as deps
from api import maps
from api import schemas as s
from api.estimate_yield import estimate_yield
from api.estimation_runs import (
    create_estimation_run,
    get_estimation_run,
    list_estimation_runs,
    preview_estimation,
)
from scraper.source_dispatcher import ParseError
from toolkit import (
    ComparableFilters,
    TargetSpec,
    analyze_distribution,
    compare_snapshots,
    compute_listing_velocity,
    compute_market_velocity,
    describe_neighborhood,
    find_anchor_amenities,
    find_comparables,
    find_distribution_outliers,
    verify_listing_freshness,
)

app = FastAPI(title="sreality toolkit API", version="0.2.5")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/maps/suggest")
def get_maps_suggest(
    query: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(default=10, ge=1, le=20),
    lang: str = Query(default="cs", min_length=2, max_length=8),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return maps.suggest(query, limit=limit, lang=lang)


@app.post("/maps/resolve")
def post_maps_resolve(
    body: s.ResolveLocationIn,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return maps.resolve(
        conn,
        label=body.label,
        lat=body.lat,
        lng=body.lng,
        type_=body.type,
        regional_structure=body.regional_structure,
        raw=body.raw,
    )


@app.post("/tools/find_comparables")
def post_find_comparables(
    body: s.FindComparablesIn,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    target, filters = _build_comparables_inputs(body)
    return find_comparables(conn, target, filters)


@app.post("/tools/analyze_distribution")
def post_analyze_distribution(
    body: s.AnalyzeDistributionIn,
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return analyze_distribution(body.listings, field=body.field)


@app.post("/tools/verify_listing_freshness")
def post_verify_listing_freshness(
    body: s.VerifyFreshnessIn,
    conn: Any = Depends(deps.get_db_conn),
    client: Any = Depends(deps.get_sreality_client),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return verify_listing_freshness(
        conn, client, body.sreality_id, body.max_age_hours,
    )


@app.post("/tools/compare_snapshots")
def post_compare_snapshots(
    body: s.CompareSnapshotsIn,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    since = (
        timedelta(days=body.since_days)
        if body.since_days is not None
        else None
    )
    return compare_snapshots(conn, body.sreality_id, since)


@app.post("/tools/describe_neighborhood")
def post_describe_neighborhood(
    body: s.DescribeNeighborhoodIn,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return describe_neighborhood(
        conn,
        lat=body.lat,
        lng=body.lng,
        radius_m=body.radius_m,
        max_age_days=body.max_age_days,
        category_main=body.category_main,
        category_type=body.category_type,
    )


@app.post("/tools/find_distribution_outliers")
def post_find_distribution_outliers(
    body: s.FindDistributionOutliersIn,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return find_distribution_outliers(
        conn,
        body.listings,
        field=body.field,
        iqr_multiplier=body.iqr_multiplier,
        investigate_history=body.investigate_history,
    )


@app.post("/tools/compute_market_velocity")
def post_compute_market_velocity(
    body: s.ComputeMarketVelocityIn,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
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
        active_only=False,
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
    return compute_market_velocity(
        conn, target, filters,
        population=body.population,
        trend_split_days=body.trend_split_days,
    )


@app.post("/tools/find_anchor_amenities")
def post_find_anchor_amenities(
    body: s.FindAnchorAmenitiesIn,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return find_anchor_amenities(
        conn,
        lat=body.lat,
        lng=body.lng,
        radius_m=body.radius_m,
        categories=body.categories,
        cache_ttl_days=body.cache_ttl_days,
    )


@app.post("/tools/compute_listing_velocity")
def post_compute_listing_velocity(
    body: s.ComputeListingVelocityIn,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return compute_listing_velocity(
        conn,
        body.sreality_id,
        radius_m=body.radius_m,
        disposition_match=body.disposition_match,
        population=body.population,
    )


@app.post("/estimations/preview")
def post_estimations_preview(
    body: s.PreviewEstimationIn,
    conn: Any = Depends(deps.get_db_conn),
    client: Any = Depends(deps.get_sreality_client),
    llm_client: Any = Depends(deps.get_llm_client),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    try:
        return preview_estimation(conn, client, llm_client, body)
    except ParseError as exc:
        raise HTTPException(status_code=502, detail=f"parse failed: {exc}")
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"upstream error: {type(exc).__name__}: {exc}",
        )


@app.post("/estimations")
def post_estimations(
    body: s.CreateEstimationIn,
    conn: Any = Depends(deps.get_db_conn),
    client: Any = Depends(deps.get_sreality_client),
    llm_client: Any = Depends(deps.get_llm_client),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return create_estimation_run(conn, client, llm_client, body)


@app.get("/estimations/preview")
def get_estimation_preview(
    url: str = Query(..., description="A sreality.cz detail URL"),
    conn: Any = Depends(deps.get_db_conn),
    client: Any = Depends(deps.get_sreality_client),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    """Scrape a sreality URL and return its parsed spec without persisting.

    Read-only. Lets the UI present the scraped fields for review/edit
    before the user commits to POST /estimations.
    """
    import requests

    from scraper.url_parser import parse_sreality_url

    try:
        parsed = parse_sreality_url(url, client=client, conn=conn)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except requests.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Could not fetch listing from sreality.cz: {exc}",
        ) from exc

    spec = parsed["spec"]
    return {
        "url":         parsed["source_url"],
        "sreality_id": int(parsed["sreality_id"]),
        "in_database": bool(parsed["in_database"]),
        "fetched_at":  parsed["fetched_at"],
        "spec": {
            "lat":         spec.get("lat"),
            "lng":         spec.get("lon"),
            "area_m2":     spec.get("area_m2"),
            "disposition": spec.get("disposition"),
            "floor":       spec.get("floor"),
            "exclude_ids": [],
        },
        "listing": {
            "price_czk":            spec.get("price_czk"),
            "price_unit":           spec.get("price_unit"),
            "category_main":        spec.get("category_main"),
            "category_type":        spec.get("category_type"),
            "locality":             spec.get("locality"),
            "district":             spec.get("district"),
            "locality_district_id": spec.get("locality_district_id"),
            "locality_region_id":   spec.get("locality_region_id"),
            "total_floors":         spec.get("total_floors"),
            "has_balcony":          spec.get("has_balcony"),
            "has_lift":             spec.get("has_lift"),
            "has_parking":          spec.get("has_parking"),
            "building_type":        spec.get("building_type"),
            "condition":            spec.get("condition"),
            "energy_rating":        spec.get("energy_rating"),
            "image_count":          len(parsed.get("images") or []),
        },
    }


@app.get("/estimations/{run_id}")
def get_estimation(
    run_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    row = get_estimation_run(conn, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="estimation run not found")
    return row


@app.get("/estimations")
def list_estimations(
    source: str | None = None,
    status: Literal["pending", "running", "success", "failed"] | None = None,
    sreality_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return list_estimation_runs(
        conn,
        source=source,
        status=status,
        sreality_id=sreality_id,
        limit=limit,
        offset=offset,
    )


@app.post("/estimate_yield")
def post_estimate_yield(
    body: s.EstimateYieldIn,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
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
