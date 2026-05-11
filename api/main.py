"""FastAPI service exposing the analytical toolkit + estimate_yield.

Routes return the standard toolkit envelope verbatim. No bespoke
response shaping; the agent layer consumes the dicts directly.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query

from api import curation
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
    cluster_comparables,
    compare_listing_images,
    compare_snapshots,
    compute_amenity_supply,
    compute_listing_velocity,
    compute_market_velocity,
    compute_walkability,
    describe_neighborhood,
    find_anchor_amenities,
    find_comparables,
    find_comparables_along_axis,
    find_comparables_relaxed,
    find_distribution_outliers,
    summarize_listing,
    verify_listing_freshness,
)
from toolkit.image_similarity import ImageCompareError
from toolkit.summaries import SummarizeError

app = FastAPI(title="sreality toolkit API", version="0.3.0")


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


@app.post("/tools/find_comparables_relaxed")
def post_find_comparables_relaxed(
    body: s.FindComparablesRelaxedIn,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    target, filters = _build_comparables_inputs(body)
    return find_comparables_relaxed(
        conn, target, filters,
        min_results=body.min_results,
        relaxation_ladder=body.relaxation_ladder,
    )


@app.post("/tools/find_comparables_along_axis")
def post_find_comparables_along_axis(
    body: s.FindComparablesAlongAxisIn,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    target, filters = _build_comparables_inputs(body)
    return find_comparables_along_axis(
        conn, target, filters,
        transport_types=body.transport_types,
        anchor_radius_m=body.anchor_radius_m,
        corridor_m=body.corridor_m,
        cache_ttl_days=body.cache_ttl_days,
    )


@app.post("/tools/analyze_distribution")
def post_analyze_distribution(
    body: s.AnalyzeDistributionIn,
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return analyze_distribution(body.listings, field=body.field)


@app.post("/tools/cluster_comparables")
def post_cluster_comparables(
    body: s.ClusterComparablesIn,
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return cluster_comparables(
        body.listings,
        axes=body.axes,
        n_clusters=body.n_clusters,
        seed=body.seed,
        n_restarts=body.n_restarts,
    )


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
        category_sub_cb=body.category_sub_cb,
        furnished=body.furnished,
        terrace=body.terrace,
        cellar=body.cellar,
        garage=body.garage,
        ownership=body.ownership,
        min_estate_area=body.min_estate_area,
        max_estate_area=body.max_estate_area,
        min_usable_area=body.min_usable_area,
        max_usable_area=body.max_usable_area,
        min_parking_lots=body.min_parking_lots,
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


@app.post("/tools/compute_walkability")
def post_compute_walkability(
    body: s.ComputeWalkabilityIn,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return compute_walkability(
        conn,
        lat=body.lat,
        lng=body.lng,
        radius_m=body.radius_m,
        categories=body.categories,
        weights=body.weights,
        cache_ttl_days=body.cache_ttl_days,
    )


@app.post("/tools/compute_amenity_supply")
def post_compute_amenity_supply(
    body: s.ComputeAmenitySupplyIn,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return compute_amenity_supply(
        conn,
        lat=body.lat,
        lng=body.lng,
        radius_m=body.radius_m,
        categories=body.categories,
        target_counts=body.target_counts,
        cache_ttl_days=body.cache_ttl_days,
    )


@app.post("/tools/summarize_listing")
def post_summarize_listing(
    body: s.SummarizeListingIn,
    conn: Any = Depends(deps.get_db_conn),
    llm_client: Any = Depends(deps.get_llm_client),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    try:
        return summarize_listing(
            conn, llm_client,
            sreality_id=body.sreality_id,
            snapshot_id=body.snapshot_id,
            force_refresh=body.force_refresh,
        )
    except SummarizeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/tools/compare_listing_images")
def post_compare_listing_images(
    body: s.CompareListingImagesIn,
    conn: Any = Depends(deps.get_db_conn),
    llm_client: Any = Depends(deps.get_llm_client),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    try:
        return compare_listing_images(
            conn, llm_client,
            sreality_id_a=body.sreality_id_a,
            sreality_id_b=body.sreality_id_b,
            n_images=body.n_images,
            force_refresh=body.force_refresh,
        )
    except ImageCompareError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
    source_kind: Literal[
        "sreality", "bezrealitky", "idnes_reality", "remax", "unsupported"
    ] | None = None,
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
        source_kind=source_kind,
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
        category_sub_cb=body.category_sub_cb,
        furnished=body.furnished,
        terrace=body.terrace,
        cellar=body.cellar,
        garage=body.garage,
        ownership=body.ownership,
        min_estate_area=body.min_estate_area,
        max_estate_area=body.max_estate_area,
        min_usable_area=body.min_usable_area,
        max_usable_area=body.max_usable_area,
        min_parking_lots=body.min_parking_lots,
    )
    return estimate_yield(conn, target, filters, body.purchase_price_czk)


# --- curation -------------------------------------------------------------
# Operator-curated lists of listings, append-only notes, and free-form
# coloured tags. Reads also flow through the *_public views in
# migration 025 (used by the SPA via the anon key); writes always
# come through these endpoints.


@app.post("/collections")
def post_create_collection(
    body: s.CreateCollectionIn,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return curation.create_collection(conn, body)


@app.get("/collections")
def get_list_collections(
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return curation.list_collections(conn)


@app.get("/collections/{collection_id}")
def get_collection(
    collection_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return curation.get_collection(conn, collection_id)


@app.patch("/collections/{collection_id}")
def patch_collection(
    collection_id: int,
    body: s.UpdateCollectionIn,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return curation.update_collection(conn, collection_id, body)


@app.delete("/collections/{collection_id}")
def delete_collection(
    collection_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return curation.delete_collection(conn, collection_id)


@app.post("/collections/{collection_id}/listings")
def post_collection_listings(
    collection_id: int,
    body: s.AddListingsToCollectionIn,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return curation.add_listings_to_collection(conn, collection_id, body)


@app.delete("/collections/{collection_id}/listings/{sreality_id}")
def delete_collection_listing(
    collection_id: int,
    sreality_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return curation.remove_listing_from_collection(
        conn, collection_id, sreality_id,
    )


@app.get("/listings/{sreality_id}/notes")
def get_listing_notes(
    sreality_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return curation.list_notes(conn, sreality_id)


@app.post("/listings/{sreality_id}/notes")
def post_listing_note(
    sreality_id: int,
    body: s.CreateNoteIn,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return curation.create_note(conn, sreality_id, body)


@app.get("/tags")
def get_tags(
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return curation.list_tags(conn)


@app.post("/tags")
def post_tag(
    body: s.CreateTagIn,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return curation.create_tag(conn, body)


@app.delete("/tags/{tag_id}")
def delete_tag(
    tag_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return curation.delete_tag(conn, tag_id)


@app.post("/listings/{sreality_id}/tags")
def post_attach_tag(
    sreality_id: int,
    body: s.AttachTagIn,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return curation.attach_tag(conn, sreality_id, body)


@app.delete("/listings/{sreality_id}/tags/{tag_id}")
def delete_listing_tag(
    sreality_id: int,
    tag_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return curation.detach_tag(conn, sreality_id, tag_id)


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
        category_sub_cb=body.category_sub_cb,
        furnished=body.furnished,
        terrace=body.terrace,
        cellar=body.cellar,
        garage=body.garage,
        ownership=body.ownership,
        min_estate_area=body.min_estate_area,
        max_estate_area=body.max_estate_area,
        min_usable_area=body.min_usable_area,
        max_usable_area=body.max_usable_area,
        min_parking_lots=body.min_parking_lots,
    )
    return target, filters
