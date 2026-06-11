"""FastAPI routes for the Watchdog (new-listing notification) surface.

Mounted under `/notifications/*`. Bearer-gated by the standard
`require_token` dependency, like every other write surface.

Two flavours of writes touch this router:

- Subscription CRUD (`POST/PUT/DELETE /notifications/subscriptions/*`)
  is straight psycopg I/O.
- The "Run estimation" action on a dispatch row goes through
  `BackgroundTasks`. The endpoint INSERTs a `pending` estimation_runs
  row synchronously, returns immediately, then `run_pending_estimation`
  finishes the deterministic estimate in the background. The frontend
  polls the dispatch (or the linked estimation_run row) until the
  status flips to a terminal value.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api import dependencies as deps
from api import notifications as nf
from api.notifications import WatchdogFilterSpec

if TYPE_CHECKING:
    import psycopg

router = APIRouter(prefix="/notifications", tags=["notifications"])


# --- request bodies -------------------------------------------------------


class CreateSubscriptionIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    filter_spec: WatchdogFilterSpec
    is_active: bool = True


class UpdateSubscriptionIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    filter_spec: WatchdogFilterSpec | None = None
    is_active: bool | None = None


# --- subscriptions --------------------------------------------------------


@router.get("/subscriptions")
def get_subscriptions(
    include_inactive: bool = True,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    rows = nf.list_subscriptions(conn, include_inactive=include_inactive)
    return {"data": rows, "total": len(rows)}


@router.post("/subscriptions")
def post_subscription(
    body: CreateSubscriptionIn,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return nf.create_subscription(
        conn,
        name=body.name,
        filter_spec=body.filter_spec,
        is_active=body.is_active,
    )


@router.get("/subscriptions/{subscription_id}")
def get_subscription(
    subscription_id: str,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    row = nf.get_subscription(conn, subscription_id)
    if row is None:
        raise HTTPException(status_code=404, detail="subscription not found")
    return row


@router.put("/subscriptions/{subscription_id}")
def put_subscription(
    subscription_id: str,
    body: UpdateSubscriptionIn,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    row = nf.update_subscription(
        conn,
        subscription_id,
        name=body.name,
        filter_spec=body.filter_spec,
        is_active=body.is_active,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="subscription not found")
    return row


@router.delete("/subscriptions/{subscription_id}")
def delete_subscription(
    subscription_id: str,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    if not nf.delete_subscription(conn, subscription_id):
        raise HTTPException(status_code=404, detail="subscription not found")
    return {"deleted": True}


# --- dispatches (the feed) ------------------------------------------------


@router.get("/dispatches")
def get_dispatches(
    subscription_id: str | None = None,
    seen: Literal["all", "seen", "unseen"] = "all",
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    return nf.list_dispatches(
        conn,
        subscription_id=subscription_id,
        seen=seen,
        limit=limit,
        offset=offset,
    )


@router.post("/dispatches/{dispatch_id}/mark-seen")
def post_mark_seen(
    dispatch_id: str,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    row = nf.mark_dispatch_seen(conn, dispatch_id)
    if row is None:
        raise HTTPException(status_code=404, detail="dispatch not found")
    return row


@router.post("/dispatches/{dispatch_id}/estimate")
def post_kickoff_estimate(
    dispatch_id: str,
    background_tasks: BackgroundTasks,
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    """Kick off a deterministic estimation in the background.

    Synchronously: validate the dispatch exists, build the target spec
    from `listings`, INSERT a `pending` `estimation_runs` row, link it
    on the dispatch.

    Background: `run_pending_estimation(run_id)` runs the actual
    `estimate_yield` against a fresh DB connection, then UPDATEs the
    run row to its terminal status. The frontend polls
    `GET /notifications/dispatches?…` (or the run row directly) for
    completion.

    If the dispatch already has a run linked we surface the existing
    row untouched — the operator can click again on a failed run via
    the standard rerun affordance, not via this endpoint.
    """
    dispatch, run_id = nf.kickoff_estimation_for_dispatch(conn, dispatch_id)
    if not dispatch:
        raise HTTPException(status_code=404, detail="dispatch not found")
    if run_id is not None:
        background_tasks.add_task(nf.run_pending_estimation, run_id)
    return dispatch


# --- matcher utility (manual run + status) --------------------------------


@router.post("/matcher/run")
def post_matcher_run(
    conn: Any = Depends(deps.get_db_conn),
    _: None = Depends(deps.require_token),
) -> dict[str, Any]:
    """Synchronously execute one matcher pass (both new-listing and
    property-change matchers).

    Useful for testing a freshly created subscription without waiting
    for the next scheduler tick — the periodic loop continues to run
    on its own clock. Idempotent against the (subscription_id,
    property_id, change_kind) UNIQUE constraint.
    """
    stats = nf.match_once(conn)
    change_stats = nf.match_changes_once(conn)
    return {"data": stats, "change_data": change_stats}
