"""FastAPI routes for the cross-source dedup review surface.

Mounted under `/dedup/*`, bearer-gated by the standard `require_token`
dependency — these are mutating operator actions (merge / dismiss / unmerge),
not a configuration surface, so no `/admin/*`-style exemption applies.

The transaction mechanics live in `toolkit.property_identity`; this router is a
thin HTTP layer over `api.property_dedup`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from api import dependencies as deps
from api import property_dedup as dedup
from toolkit.property_identity import MergeError

router = APIRouter(prefix="/dedup", tags=["dedup"])


@router.get("/candidates")
def get_candidates(
    status: str | None = "proposed",
    tier: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return dedup.list_candidates(
        conn, status=status, tier=tier, limit=limit, offset=offset,
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
