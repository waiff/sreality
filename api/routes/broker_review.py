"""FastAPI routes for the broker merge-review queue (Phase 5).

Mounted under `/broker-review/*` (own prefix so it never collides with the
`/brokers/{broker_id}` read routes), admin-gated via `require_admin` — mutating
operator actions (merge / dismiss / unmerge). Thin HTTP layer over
`api.broker_review`. Reversible: every merge logs to broker_merge_events.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api import broker_review as review
from api import dependencies as deps

router = APIRouter(prefix="/broker-review", tags=["broker-review"])


class MergeCandidateIn(BaseModel):
    broker_ids: list[int] | None = None  # optional subset of the proposed group


class MergeBrokersIn(BaseModel):
    broker_ids: list[int]


@router.get("/candidates")
def get_candidates(
    status: str = "proposed",
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    return review.list_candidates(conn, status=status, limit=limit, offset=offset)


@router.post("/candidates/{candidate_id}/merge")
def merge_candidate(
    candidate_id: int,
    body: MergeCandidateIn,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    try:
        result = review.merge_candidate(conn, candidate_id, broker_ids=body.broker_ids)
    except review.MergeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="candidate not found or not proposed")
    return result


@router.post("/candidates/{candidate_id}/dismiss")
def dismiss_candidate(
    candidate_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    result = review.dismiss_candidate(conn, candidate_id)
    if result is None:
        raise HTTPException(status_code=404, detail="candidate not found or not proposed")
    return result


@router.post("/merge")
def merge_brokers(
    body: MergeBrokersIn,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    try:
        return review.merge_brokers(conn, body.broker_ids)
    except review.MergeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/merges")
def list_merges(
    limit: int = Query(default=50, ge=1, le=200),
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    return review.list_recent_merges(conn, limit=limit)


@router.post("/merges/{merge_group_id}/unmerge")
def unmerge(
    merge_group_id: str,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    result = review.unmerge_group(conn, merge_group_id)
    if result is None:
        raise HTTPException(status_code=404, detail="merge group not found or already undone")
    return result
