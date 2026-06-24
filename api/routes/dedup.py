"""FastAPI routes for the cross-source dedup review surface.

Mounted under `/dedup/*`, bearer-gated by the standard `require_token`
dependency — these are mutating operator actions (merge / dismiss / unmerge).

The transaction mechanics live in `toolkit.property_identity`; this router is a
thin HTTP layer over `api.property_dedup`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api import dependencies as deps
from api import property_dedup as dedup
from toolkit.asset_identity import (
    AssetError,
    get_asset,
    link_properties,
    unlink_property,
)
from toolkit.property_identity import MergeError

router = APIRouter(prefix="/dedup", tags=["dedup"])


class ClusterAction(BaseModel):
    candidate_ids: list[int]


class PropertySetAction(BaseModel):
    property_ids: list[int]


class AssetLinkAction(BaseModel):
    property_ids: list[int]
    note: str | None = None


class AssetUnlinkAction(BaseModel):
    property_id: int


@router.get("/summary")
def get_summary(
    status: str = "proposed",
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    """Cumulative review backlog + breakdown by reason (drives the dashboard)."""
    return dedup.summary(conn, status=status)


@router.get("/clip-coverage")
def get_clip_coverage(
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    """CLIP backfill progress (totals + priority tiers) for the /dedup tracker."""
    return dedup.clip_coverage(conn)


@router.get("/pipeline-overview")
def get_pipeline_overview(
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    """The top-of-page dedup funnel: per-stage count + last-24h movement."""
    return dedup.pipeline_overview(conn)


@router.get("/pipeline-timeline")
def get_pipeline_timeline(
    days: int = Query(default=14, ge=1, le=90),
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    """Daily dedup-funnel throughput (tagged / candidates / merged / dismissed)."""
    return dedup.pipeline_timeline(conn, days=days)


@router.get("/decision-images")
def get_decision_images(
    a: int,
    b: int,
    room_type: str | None = None,
    per_side: int = Query(default=4, ge=1, le=8),
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    """Photos behind a decision row — the deciding room's images for both listings
    (factor-row drill-in on Decision history / Needs-review)."""
    return dedup.decision_images(
        conn, left_sreality_id=a, right_sreality_id=b, room_type=room_type,
        per_side=per_side,
    )


@router.get("/audit")
def get_pair_audit(
    outcome: str | None = None,
    category_main: str | None = None,
    source: str | None = None,
    stage: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    """The unified Decision history feed (merged / dismissed, engine + operator).
    Filterable by property type (`category_main`), `outcome`, `source`, `stage`."""
    return dedup.list_pair_audit(
        conn, outcome=outcome, category_main=category_main, source=source,
        stage=stage, limit=limit, offset=offset,
    )


@router.get("/candidates")
def get_candidates(
    status: str | None = "proposed",
    tier: str | None = None,
    reason: str | None = None,
    verdict: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return dedup.list_candidates(
        conn, status=status, tier=tier, reason=reason, verdict=verdict,
        limit=limit, offset=offset,
    )


@router.post("/candidates/{candidate_id}/merge")
def post_merge_candidate(
    candidate_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    try:
        result = dedup.merge_candidate(conn, candidate_id)
    except MergeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="candidate not found")
    return result


@router.post("/candidates/bulk-merge")
def post_bulk_merge_candidates(
    body: ClusterAction,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    """Scoped bulk-approve: merge each given candidate as its own reversible pair.

    Per-pair tolerant (a conflicting pair is skipped, not fatal). The operator-facing
    /dedup surface sends the loaded STRONG candidates of one category here.
    """
    return dedup.bulk_merge_candidates(conn, body.candidate_ids)


@router.post("/candidates/archive-reset")
def post_archive_reset_candidates(
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    """Archive the proposed candidate queue to a backup table + clear it, so the
    engine regenerates fresh. Merges/dismissals are untouched."""
    return dedup.archive_reset_candidates(conn)


@router.post("/candidates/{candidate_id}/dismiss")
def post_dismiss_candidate(
    candidate_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    result = dedup.dismiss_candidate(conn, candidate_id)
    if result is None:
        raise HTTPException(
            status_code=404, detail="candidate not found or not proposed",
        )
    return result


@router.post("/clusters/merge")
def post_merge_cluster(
    body: ClusterAction,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    """Merge a cluster of candidates into one property under one reversible group."""
    try:
        result = dedup.merge_cluster(conn, body.candidate_ids)
    except MergeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="no candidates found")
    return result


@router.post("/properties/merge")
def post_merge_property_set(
    body: PropertySetAction,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    """Merge an explicit operator-chosen set of properties into one (subset merge)."""
    try:
        result = dedup.merge_property_set(conn, body.property_ids)
    except MergeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=400, detail="need at least two properties")
    return result


@router.post("/clusters/dismiss")
def post_dismiss_cluster(
    body: ClusterAction,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    result = dedup.dismiss_cluster(conn, body.candidate_ids)
    if result is None:
        raise HTTPException(
            status_code=404, detail="no proposed candidates found",
        )
    return result


@router.get("/merges")
def get_merges(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return dedup.list_merges(conn, limit=limit, offset=offset)


@router.post("/merges/{merge_group_id}/unmerge")
def post_unmerge(
    merge_group_id: str,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    try:
        return dedup.unmerge(conn, merge_group_id, undone_by="operator")
    except MergeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ----- asset links (same physical building, kept as separate cohorts) -------
# Unlike a merge these never collapse properties — both category facets survive.
# It is the surface for the cross-category sameness merge_properties refuses.


@router.post("/assets/link")
def post_asset_link(
    body: AssetLinkAction,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    """Link the chosen properties into one asset (same building)."""
    try:
        return link_properties(
            conn, property_ids=body.property_ids, source="operator",
            reason="manual_link", note=body.note, created_by="operator",
        )
    except AssetError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/assets/unlink")
def post_asset_unlink(
    body: AssetUnlinkAction,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    """Remove one property from its asset (dissolves the asset if <2 remain)."""
    try:
        return unlink_property(
            conn, property_id=body.property_id, reason="manual_unlink",
            created_by="operator",
        )
    except AssetError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/assets/{asset_id}")
def get_asset_route(
    asset_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    result = get_asset(conn, asset_id)
    if result is None:
        raise HTTPException(status_code=404, detail="asset not found")
    return result
