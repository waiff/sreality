"""FastAPI routes for the broker merge-review queue (Phase 5).

Mounted under `/broker-review/*` (own prefix so it never collides with the
`/brokers/{broker_id}` read routes), admin-gated via `require_admin` — mutating
operator actions (merge / dismiss / unmerge) plus the standing-NO ledger those
actions write (`/suppressions`, migration 401, listable and liftable). Thin HTTP
layer over `api.broker_review`. Reversible: every merge logs to broker_merge_events.

`require_admin` returns the verified JWT claims; every mutating route binds them and
threads WHO acted into the ledger. They were discarded before, so undone_by /
resolved_by are NULL on every row written to date.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api import broker_review as review
from api import dependencies as deps

router = APIRouter(prefix="/broker-review", tags=["broker-review"])


def _actor(claims: dict) -> str | None:
    """Who is acting, for the ledger's *_by columns. Email is the readable handle;
    `sub` (the Supabase user uuid) is the fallback for a token without one."""
    return claims.get("email") or claims.get("sub")


class MergeCandidateIn(BaseModel):
    broker_ids: list[int] | None = None  # optional subset of the proposed group


class MergeBrokersIn(BaseModel):
    broker_ids: list[int]


class SuppressionLiftIn(BaseModel):
    reason: str | None = None


@router.get("/candidates")
def get_candidates(
    status: str = "proposed",
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    reason: str | None = Query(default=None),
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    return review.list_candidates(conn, status=status, limit=limit, offset=offset,
                                  reason=reason)


@router.post("/candidates/{candidate_id}/merge")
def merge_candidate(
    candidate_id: int,
    body: MergeCandidateIn,
    conn: Any = Depends(deps.get_db_conn),
    claims: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    try:
        result = review.merge_candidate(conn, candidate_id, broker_ids=body.broker_ids,
                                        created_by=_actor(claims))
    except review.MergeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="candidate not found or not proposed")
    return result


@router.post("/candidates/{candidate_id}/dismiss")
def dismiss_candidate(
    candidate_id: int,
    conn: Any = Depends(deps.get_db_conn),
    claims: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    result = review.dismiss_candidate(conn, candidate_id, resolved_by=_actor(claims))
    if result is None:
        raise HTTPException(status_code=404, detail="candidate not found or not proposed")
    return result


@router.post("/merge")
def merge_brokers(
    body: MergeBrokersIn,
    conn: Any = Depends(deps.get_db_conn),
    claims: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    try:
        return review.merge_brokers(conn, body.broker_ids, created_by=_actor(claims))
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
    claims: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    result = review.unmerge_group(conn, merge_group_id, undone_by=_actor(claims))
    if result is None:
        raise HTTPException(status_code=404, detail="merge group not found or already undone")
    return result


@router.get("/suppressions")
def list_suppressions(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    include_lifted: bool = Query(default=False),
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """The standing-NO ledger (migration 401), active rows first."""
    return review.list_suppressions(conn, limit=limit, offset=offset,
                                    include_lifted=include_lifted)


@router.post("/suppressions/{suppression_id}/lift")
def lift_suppression(
    suppression_id: int,
    body: SuppressionLiftIn | None = None,
    conn: Any = Depends(deps.get_db_conn),
    claims: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Clear one standing NO. The rail is otherwise one-way: a suppression written
    against a pair that later legitimately belongs together (or one violating the
    verify_pipeline invariant) had no in-product route back."""
    try:
        result = review.lift_suppression(conn, suppression_id, lifted_by=_actor(claims),
                                         reason=body.reason if body else None)
    except review.MergeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="suppression not found")
    return result
