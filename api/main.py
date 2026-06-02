"""FastAPI service exposing the analytical toolkit + estimate_yield.

Routes return the standard toolkit envelope verbatim. No bespoke
response shaping; the agent layer consumes the dicts directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from datetime import timedelta
from typing import Any, AsyncIterator, Literal

from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api import curation
from api import price_stats as price_stats_module
from api import manual_estimates as me
from api import dependencies as deps
from api import maps
from api import schemas as s
from api import skills as skills_module
from api.agent import AGENT_TOOLS
from api.estimate_yield import estimate_yield
from api.building_runs import (
    assert_editable_for_attachments,
    confirm_units,
    create_building_run,
    create_building_run_from_url,
    get_building_run,
    list_building_runs,
    re_extract,
    sweep_stuck_buildings,
    update_building_inputs,
)
from api import attachments as attachments_module
from api import feedback as feedback_module
from api import refiner as refiner_module
from api.estimation_runs import (
    create_estimation_run,
    get_estimation_run,
    get_trace_payload,
    list_estimation_runs,
    preview_estimation,
    sweep_stuck_runs,
    update_scenario,
)
from api import notifications as nf_module
from api.routes.admin import router as admin_router
from api.routes.dedup import router as dedup_router
from api.routes.images import router as images_router
from api.routes.notifications import router as notifications_router
from scraper import image_storage
from scraper.db import sweep_stuck_scrape_runs
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
    summarize_region_dispositions,
    verify_listing_freshness,
)
from toolkit.image_similarity import ImageCompareError
from toolkit.region_annotations import RegionAnnotationError
from toolkit.rent_map import compute_reference_rent
from toolkit.summaries import SummarizeError

@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI) -> "AsyncIterator[None]":
    """Spawn the Watchdog matcher loop alongside the request handler.

    On startup we also sweep any estimation_runs / building_runs left
    in a non-terminal status by a server crash mid-background-task,
    flipping rows older than 10 minutes to 'failed' with a clear
    error_message so the operator isn't left looking at a forever-
    pending row. scrape_runs hard-killed at the GitHub-Actions job
    timeout (which can't self-finalize) are likewise stamped with an
    ended_at so they stop reading as 'stuck' on the Health page.

    The loop opens its own per-pass DB connection and respects the
    `notifications_matcher_interval_seconds` knob in `app_settings`.
    Setting that key to 0 keeps the task alive but idle, so an
    operator can disable matching without redeploying.

    Disabled entirely when the env var `NOTIFICATIONS_MATCHER_DISABLED`
    is set — useful for tests / one-off CLI invocations that import
    api.main but don't want a background task chattering.
    """
    if not os.environ.get("STUCK_ROW_SWEEP_DISABLED"):
        try:
            with deps.open_background_conn() as conn:
                est = sweep_stuck_runs(conn)
                bld = sweep_stuck_buildings(conn)
                scr = sweep_stuck_scrape_runs(conn)
            if est or bld or scr:
                logging.info(
                    "stuck-row sweep on startup: %s estimation_runs, "
                    "%s building_runs, %s scrape_runs",
                    est, bld, scr,
                )
        except Exception:
            logging.exception("stuck-row sweep failed on startup")

    # The /images/{key} route presigns R2 to serve listing photos. If R2 isn't
    # configured ON THIS SERVICE (the scraper's R2 secrets live in GitHub
    # Actions, a different runtime), every photo 503s and the whole UI looks
    # imageless while the DB still reports them "stored". Shout it at boot so
    # the misconfig is never silent again (see CLAUDE.md "Image storage (R2)").
    if not image_storage.is_configured():
        logging.warning(
            "R2 image storage is NOT configured on this API service "
            "(need R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / "
            "R2_BUCKET_NAME) — GET /images/{key} will return 503 and listing "
            "photos will not display."
        )

    stop_event: asyncio.Event = asyncio.Event()
    task: asyncio.Task[None] | None = None
    if not os.environ.get("NOTIFICATIONS_MATCHER_DISABLED"):
        task = asyncio.create_task(
            nf_module.matcher_loop(stop_event), name="notifications-matcher",
        )
    try:
        yield
    finally:
        if task is not None:
            stop_event.set()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, timeout=10.0)


app = FastAPI(title="sreality toolkit API", version="0.3.0", lifespan=_lifespan)

_cors_origins = [
    o.strip()
    for o in os.environ.get("CORS_ALLOW_ORIGINS", "").split(",")
    if o.strip()
]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: "Request", exc: Exception) -> "JSONResponse":
    logging.exception("Unhandled exception on %s %s", request.method, request.url.path)
    origin = request.headers.get("origin")
    headers: dict[str, str] = {}
    if origin and origin in _cors_origins:
        headers["access-control-allow-origin"] = origin
        headers["vary"] = "Origin"
    return JSONResponse(
        status_code=500,
        content={"detail": f"{type(exc).__name__}: {exc}"},
        headers=headers,
    )

# Skill validation needs to know the registered agent tools and the
# registered provider names. Populate at import time so PUT /admin/skills/*
# rejects bogus tool / provider names with a clear 400.
skills_module.AGENT_TOOL_NAMES = set(AGENT_TOOLS.keys())
skills_module.PROVIDER_NAMES = set(deps.get_providers().keys())

# /admin/* is exempted from the API_TOKEN bearer gate per the slice-1
# Settings-page design (CLAUDE.md "Auth and secrets" + rule #8). The
# private Railway URL is the security perimeter for these routes.
app.include_router(admin_router)
# /notifications/* (Watchdog feed + subscription CRUD) goes through
# the standard bearer gate — operator content, not configuration.
app.include_router(notifications_router)
# /dedup/* (cross-source merge review: list candidates, merge/dismiss/unmerge)
# — mutating operator actions, standard bearer gate.
app.include_router(dedup_router)
# /images/* redirects a listing-photo key to a presigned R2 URL. Public (like
# /health) — an <img> tag can't send a bearer header and these are public
# photos; the key regex keeps it scoped to listing images only.
app.include_router(images_router)


@app.get("/health")
def health() -> dict[str, str]:
    # `image_storage` lets the operator (or a monitor) see at a glance whether
    # this service can serve /images/{key}; "unconfigured" means every listing
    # photo 503s even though the bytes are in R2. Stays a flat string map so
    # Railway's healthcheck keeps treating any 200 as healthy.
    return {
        "status": "ok",
        "image_storage": (
            "configured" if image_storage.is_configured() else "unconfigured"
        ),
    }


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
        portals=body.portals,
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
        building_condition_level_min=body.building_condition_level_min,
        apartment_condition_level_min=body.apartment_condition_level_min,
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


@app.post("/listings/summaries")
def post_listings_summaries(
    body: s.SummarizeListingsBatchIn,
    conn: Any = Depends(deps.get_db_conn),
    llm_client: Any = Depends(deps.get_llm_client),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    """Batch summarize_listing for the Estimate page comparables table.

    Sequential under the hood — cache hits are sub-millisecond and
    the typical batch is 5 comparables. Per-item failures are reported
    inline; a single bad id never fails the whole request.
    """
    out: list[dict[str, Any]] = []
    for item in body.items:
        try:
            result = summarize_listing(
                conn, llm_client,
                sreality_id=item.sreality_id,
                snapshot_id=item.snapshot_id,
            )
            data = result["data"]
            out.append({
                "sreality_id": item.sreality_id,
                "snapshot_id": data.get("snapshot_id"),
                "summary": data.get("summary"),
                "error": None,
            })
        except SummarizeError as exc:
            out.append({
                "sreality_id": item.sreality_id,
                "snapshot_id": item.snapshot_id,
                "summary": None,
                "error": str(exc),
            })
        except Exception as exc:  # noqa: BLE001 — per-item isolation
            out.append({
                "sreality_id": item.sreality_id,
                "snapshot_id": item.snapshot_id,
                "summary": None,
                "error": f"{type(exc).__name__}: {exc}",
            })
    return {"data": out}


@app.post("/tools/summarize_region_dispositions")
def post_summarize_region_dispositions(
    body: s.SummarizeRegionDispositionsIn,
    conn: Any = Depends(deps.get_db_conn),
    llm_client: Any = Depends(deps.get_llm_client),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    """Natural-language annotations for the Browse > Stats box plots.

    Cached per (region, calendar day): the first viewer of a region today
    pays for the LLM call; everyone else hits the cache.
    """
    try:
        return summarize_region_dispositions(
            conn, llm_client,
            region_key=body.region_key,
            dispositions=[d.model_dump() for d in body.dispositions],
            ppm2_overall=body.ppm2_overall,
            region_label=body.region_label,
            force_refresh=body.force_refresh,
        )
    except RegionAnnotationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


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
    background_tasks: BackgroundTasks,
    conn: Any = Depends(deps.get_db_conn),
    client: Any = Depends(deps.get_sreality_client),
    llm_client: Any = Depends(deps.get_llm_client),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return create_estimation_run(
        conn, client, llm_client, body, background_tasks=background_tasks,
    )


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


@app.patch("/estimations/{run_id}/scenario")
def patch_estimation_scenario(
    run_id: int,
    body: s.ScenarioUpdateIn,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    """Persist the operator's yield-scenario overrides.

    Shared by the SPA's YieldBlock and the Chrome extension's yield
    panel; latest-wins. A body with all three numbers null clears the
    column back to NULL — render defaults again.
    """
    row = update_scenario(
        conn, run_id,
        rent_czk=body.rent_czk,
        fond_per_m2_czk=body.fond_per_m2_czk,
        price_czk=body.price_czk,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="estimation run not found")
    return row


@app.get("/estimations/{run_id}/trace/{step_n}/payload")
def get_estimation_trace_payload(
    run_id: int,
    step_n: int,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    payload = get_trace_payload(conn, run_id, step_n)
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail="trace payload not found for this run/step",
        )
    return payload


@app.get("/estimations/{run_id}/feedback")
def list_estimation_feedback(
    run_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    if get_estimation_run(conn, run_id) is None:
        raise HTTPException(status_code=404, detail="estimation run not found")
    return {"data": feedback_module.list_feedback_for_run(conn, run_id)}


@app.post("/estimations/{run_id}/feedback")
def post_estimation_feedback(
    run_id: int,
    body: s.CreateFeedbackIn,
    conn: Any = Depends(deps.get_db_conn),
    llm_client: Any = Depends(deps.get_llm_client),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    """Persist operator feedback on a run.

    When `kick_off_refinement=true` we fire the slice C refiner
    synchronously and return the resulting (feedback, refinement)
    pair; otherwise the row sits in `submitted` for a later batch
    run. Refiner failures persist as `status='failed'` on the
    feedback row — the operator still sees their note in the UI.
    """
    run = get_estimation_run(conn, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="estimation run not found")

    initial_status = "refining" if body.kick_off_refinement else "submitted"
    row = feedback_module.insert_feedback(
        conn,
        estimation_run_id=run_id,
        feedback_text=body.feedback_text,
        initial_status=initial_status,
    )

    refinement: dict[str, Any] | None = None
    if body.kick_off_refinement:
        from api.refiner import run_refinement
        refinement, terminal_status = run_refinement(
            conn, llm_client, feedback=row, run=run,
        )
        feedback_module.update_feedback_status(
            conn,
            row["id"],
            status=terminal_status,
            refinement_id=refinement["id"] if refinement else None,
        )
        row["status"] = terminal_status
        if refinement:
            row["refinement_id"] = refinement["id"]
    return {"feedback": row, "refinement": refinement}


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


@app.post("/buildings")
def post_buildings(
    body: s.CreateBuildingIn,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return create_building_run(conn, body)


@app.post("/buildings/from_url")
def post_buildings_from_url(
    body: s.CreateBuildingFromUrlIn,
    background_tasks: BackgroundTasks,
    conn: Any = Depends(deps.get_db_conn),
    sreality_client: Any = Depends(deps.get_sreality_client),
    llm_client: Any = Depends(deps.get_llm_client),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return create_building_run_from_url(
        conn, sreality_client, llm_client, body,
        background_tasks=background_tasks,
    )


@app.post("/buildings/{building_id}/confirm_units")
def post_buildings_confirm_units(
    building_id: int,
    body: s.ConfirmBuildingUnitsIn,
    background_tasks: BackgroundTasks,
    conn: Any = Depends(deps.get_db_conn),
    sreality_client: Any = Depends(deps.get_sreality_client),
    llm_client: Any = Depends(deps.get_llm_client),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return confirm_units(
        conn, building_id, body,
        sreality_client=sreality_client,
        llm_client=llm_client,
        background_tasks=background_tasks,
    )


@app.post("/buildings/{building_id}/re_extract")
def post_buildings_re_extract(
    building_id: int,
    conn: Any = Depends(deps.get_db_conn),
    llm_client: Any = Depends(deps.get_llm_client),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return re_extract(conn, llm_client, building_id)


@app.get("/buildings")
def get_buildings(
    source: str | None = None,
    status: s.BuildingStatus | None = None,
    sreality_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return list_building_runs(
        conn,
        source=source,
        status=status,
        sreality_id=sreality_id,
        limit=limit,
        offset=offset,
    )


@app.get("/buildings/{building_id}")
def get_building(
    building_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    row = get_building_run(conn, building_id)
    if row is None:
        raise HTTPException(status_code=404, detail="building run not found")
    return row


@app.patch("/buildings/{building_id}/inputs")
def patch_building_inputs(
    building_id: int,
    body: s.UpdateBuildingInputsIn,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return update_building_inputs(conn, building_id, body)


@app.post("/buildings/{building_id}/attachments")
def post_building_attachment(
    building_id: int,
    file: UploadFile = File(...),
    source: str = "ui",
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    row = assert_editable_for_attachments(conn, building_id)
    uploaded_by = source if source in ("ui", "api", "clickup") else None
    return attachments_module.insert_attachment(
        conn,
        building_run_id=row["id"],
        file=file,
        uploaded_by=uploaded_by,
    )


@app.get("/buildings/{building_id}/attachments")
def list_building_attachments(
    building_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    # 404 if the parent doesn't exist; otherwise return rows (may be empty).
    if get_building_run(conn, building_id) is None:
        raise HTTPException(status_code=404, detail="building run not found")
    return {"data": attachments_module.list_attachments(conn, building_id)}


@app.delete("/buildings/{building_id}/attachments/{attachment_id}")
def delete_building_attachment(
    building_id: int,
    attachment_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    assert_editable_for_attachments(conn, building_id)
    attachments_module.delete_attachment(conn, building_id, attachment_id)
    return {"ok": True}


@app.get("/buildings/{building_id}/attachments/{attachment_id}/raw")
def get_building_attachment_raw(
    building_id: int,
    attachment_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> Response:
    """Bearer-gated thumbnail proxy. The frontend uses this to render
    attachment previews without exposing R2 credentials."""
    row = attachments_module.fetch_attachment(conn, attachment_id)
    if row is None or row["building_run_id"] != building_id:
        raise HTTPException(status_code=404, detail="attachment not found")
    data, mime, _filename = attachments_module.download_attachment_bytes(
        conn, attachment_id,
    )
    return Response(content=data, media_type=mime)


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
        portals=body.portals,
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
        building_condition_level_min=body.building_condition_level_min,
        apartment_condition_level_min=body.apartment_condition_level_min,
    )
    result = estimate_yield(
        conn, target, filters, body.purchase_price_czk,
        estimate_kind=body.estimate_kind,
        expected_monthly_rent_czk=body.expected_monthly_rent_czk,
    )
    # Secondary MF Cenová mapa reference (rent only). Best-effort: the
    # amenity filters double as the subject's attributes for this ad-hoc
    # surface (the /estimations flow reads the real subject listing).
    if body.estimate_kind == "rent":
        reference_rent = compute_reference_rent(
            conn,
            lat=target.lat, lng=target.lng, area_m2=target.area_m2,
            disposition=target.disposition,
            amenities={
                "balcony": body.has_balcony is True,
                "terrace": body.terrace is True,
                "furnished": body.furnished == "ano",
                "garage": body.garage is True,
                "elevator": body.has_lift is True,
                "other_material": False,
            },
            is_novostavba=False,
        )
        if reference_rent is not None:
            result["data"]["reference_rent"] = reference_rent
    return result


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


# --- price-stats datasets (ceny-nemovitosti) -------------------------------

@app.post("/price-stats/datasets")
def post_price_stat_dataset(
    body: s.PriceStatDatasetIn,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return price_stats_module.create_dataset(conn, body)


@app.get("/price-stats/datasets")
def get_price_stat_datasets(
    include_inactive: bool = False,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return price_stats_module.list_datasets(conn, include_inactive=include_inactive)


@app.patch("/price-stats/datasets/{dataset_id}")
def patch_price_stat_dataset(
    dataset_id: int,
    body: s.PriceStatDatasetUpdateIn,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return price_stats_module.update_dataset(conn, dataset_id, body)


@app.delete("/price-stats/datasets/{dataset_id}")
def delete_price_stat_dataset(
    dataset_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return price_stats_module.deactivate_dataset(conn, dataset_id)


@app.get("/price-stats/datasets/{dataset_id}/summary")
def get_price_stat_summary(
    dataset_id: int,
    window_years: int = 5,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return price_stats_module.dataset_summary(conn, dataset_id, window_years)


@app.get("/price-stats/datasets/{dataset_id}/city-metrics")
def get_price_stat_city_metrics(
    dataset_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return price_stats_module.dataset_city_metrics(conn, dataset_id)


@app.get("/price-stats/datasets/{dataset_id}/cities/{entity_type}/{entity_id}/series")
def get_price_stat_city_series(
    dataset_id: int,
    entity_type: str,
    entity_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return price_stats_module.dataset_city_series(conn, dataset_id, entity_type, entity_id)


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


@app.patch("/tags/{tag_id}")
def patch_tag(
    tag_id: int,
    body: s.UpdateTagIn,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return curation.update_tag(conn, tag_id, body)


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


# --- Skill refinements (Phase AI slice C) ---------------------------------

@app.get("/skill-refinements/{refinement_id}")
def get_skill_refinement(
    refinement_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    row = refiner_module.get_refinement(conn, refinement_id)
    if row is None:
        raise HTTPException(
            status_code=404, detail="skill refinement not found",
        )
    return row


@app.post("/skill-refinements/{refinement_id}/decision")
def decide_skill_refinement(
    refinement_id: int,
    body: s.RefinementDecisionIn,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    """Apply or dismiss a proposed refinement.

    Applying writes through `skills.update_skill`, which triggers
    the `skills_history` trigger from migration 029, so the prior
    prompt is preserved automatically. Dismiss is a status-only
    flip on the refinement and its parent feedback row.
    """
    try:
        if body.decision == "apply":
            return refiner_module.apply_refinement(conn, refinement_id)
        return refiner_module.dismiss_refinement(conn, refinement_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# --- manual rental estimates --------------------------------------------
# Phase U-ME: point-estimate rental figures attached to a listing.
# Reads are also exposed via the manual_rental_estimates_public view
# (anon select grant from migration 046) for the SPA; these bearer-gated
# endpoints carry the write path and a token-gated read for direct API
# callers.


@app.get("/listings/{sreality_id}/manual_estimates")
def get_listing_manual_estimates(
    sreality_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return me.list_manual_estimates(conn, sreality_id)


@app.post("/listings/{sreality_id}/manual_estimates")
def post_listing_manual_estimate(
    sreality_id: int,
    body: s.CreateManualEstimateIn,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return me.create_manual_estimate(conn, sreality_id, body)


@app.patch("/manual_estimates/{estimate_id}")
def patch_manual_estimate(
    estimate_id: int,
    body: s.UpdateManualEstimateIn,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return me.update_manual_estimate(conn, estimate_id, body)


@app.delete("/manual_estimates/{estimate_id}")
def delete_manual_estimate(
    estimate_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return me.delete_manual_estimate(conn, estimate_id)


@app.post("/tools/get_manual_rental_estimates")
def post_get_manual_rental_estimates(
    body: s.GetManualRentalEstimatesIn,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    from toolkit.manual_estimates import get_manual_rental_estimates
    return get_manual_rental_estimates(conn, body.sreality_id)


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
        portals=body.portals,
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
        building_condition_level_min=body.building_condition_level_min,
        apartment_condition_level_min=body.apartment_condition_level_min,
    )
    return target, filters
