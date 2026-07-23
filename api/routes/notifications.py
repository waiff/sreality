"""FastAPI routes for the Watchdog (new-listing notification) surface.

Mounted under `/notifications/*`.

The user-facing surface — subscription CRUD, the dispatch feed, unread-count,
mark-seen/mark-all-seen — runs on the **tenant pool** (`tenant_pool.tenant_conn`,
RLS-scoped by the caller's JWT claims), so every read/write is account-isolated
by the policies migrations 290/292 put on `notification_subscriptions` /
`notification_dispatches`. A legacy static-`API_TOKEN` caller (the operator's SPA
today) has no Supabase `sub`, so `tenant_conn` routes it to the unscoped
service-role connection — behaviour-preserving until the SPA/extension send real
user JWTs, at which point RLS becomes a live boundary. `verify_jwt` (which
`tenant_conn` depends on) accepts BOTH the JWT and the legacy token, so no route
loses the operator.

Two routes deliberately stay on the service-role connection + `require_token`:
- `POST /dispatches/{id}/estimate` kicks off an estimation that reads the shared
  `listings` table (RLS deny-all for `authenticated`, the A5 shape) and INSERTs an
  `estimation_runs` row whose own `account_id` stamping is Wave-1 metering scope —
  moving it correctly needs the two-connection split `POST /listings/lookup` uses,
  a separate follow-up.
- `POST /matcher/run` runs the platform-wide producer passes (service-role by
  design, rule #16); the row-level trigger stamps each dispatch's `account_id`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api import dependencies as deps
from api import notifications as nf
from api import tenant_pool
from api.notifications import WatchdogFilterSpec

if TYPE_CHECKING:
    import psycopg

router = APIRouter(prefix="/notifications", tags=["notifications"])


# --- request bodies -------------------------------------------------------


class CreateSubscriptionIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    filter_spec: WatchdogFilterSpec
    is_active: bool = True
    # Non-in_app delivery channels (e.g. ['email']). in_app is always implicit.
    channels: list[str] = Field(default_factory=list)


class UpdateSubscriptionIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    filter_spec: WatchdogFilterSpec | None = None
    is_active: bool | None = None
    channels: list[str] | None = None


# --- subscriptions --------------------------------------------------------


@router.get("/subscriptions")
def get_subscriptions(
    include_inactive: bool = True,
    conn: Any = Depends(tenant_pool.tenant_conn),
) -> dict[str, Any]:
    rows = nf.list_subscriptions(conn, include_inactive=include_inactive)
    return {"data": rows, "total": len(rows)}


@router.post("/subscriptions")
def post_subscription(
    body: CreateSubscriptionIn,
    conn: Any = Depends(tenant_pool.tenant_conn),
    claims: dict = Depends(deps.verify_jwt),
) -> dict[str, Any]:
    account_id = tenant_pool.resolve_account_id(conn, claims)
    if account_id is None:
        # No resolvable account (a JWT with no membership, or the legacy operator
        # before their first signup claimed the backfill) — the account_id column
        # is NOT NULL (migration 364), so refuse rather than 500 on the insert.
        raise HTTPException(status_code=400, detail="no account for caller")
    return nf.create_subscription(
        conn,
        name=body.name,
        filter_spec=body.filter_spec,
        is_active=body.is_active,
        channels=body.channels,
        account_id=account_id,
    )


@router.get("/subscriptions/{subscription_id}")
def get_subscription(
    subscription_id: str,
    conn: Any = Depends(tenant_pool.tenant_conn),
) -> dict[str, Any]:
    row = nf.get_subscription(conn, subscription_id)
    if row is None:
        raise HTTPException(status_code=404, detail="subscription not found")
    return row


@router.put("/subscriptions/{subscription_id}")
def put_subscription(
    subscription_id: str,
    body: UpdateSubscriptionIn,
    conn: Any = Depends(tenant_pool.tenant_conn),
) -> dict[str, Any]:
    row = nf.update_subscription(
        conn,
        subscription_id,
        name=body.name,
        filter_spec=body.filter_spec,
        is_active=body.is_active,
        channels=body.channels,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="subscription not found")
    return row


@router.delete("/subscriptions/{subscription_id}")
def delete_subscription(
    subscription_id: str,
    conn: Any = Depends(tenant_pool.tenant_conn),
) -> dict[str, Any]:
    if not nf.delete_subscription(conn, subscription_id):
        raise HTTPException(status_code=404, detail="subscription not found")
    return {"deleted": True}


# --- dispatches (the feed) ------------------------------------------------


@router.get("/dispatches")
def get_dispatches(
    subscription_id: str | None = None,
    collection_id: int | None = None,
    source_kind: Literal["watchdog", "collection_monitor", "system_health", "all"] = "all",
    seen: Literal["all", "seen", "unseen"] = "all",
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    cursor: str | None = Query(default=None, description="Keyset cursor (next_cursor)"),
    conn: Any = Depends(tenant_pool.tenant_conn),
) -> dict[str, Any]:
    try:
        return nf.list_dispatches(
            conn,
            subscription_id=subscription_id,
            collection_id=collection_id,
            source_kind=source_kind,
            seen=seen,
            limit=limit,
            offset=offset,
            cursor=cursor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/unread-count")
def get_unread_count(
    source_kind: Literal["watchdog", "collection_monitor", "system_health", "all"] = "all",
    conn: Any = Depends(tenant_pool.tenant_conn),
) -> dict[str, int]:
    """Unseen dispatch counts for the nav badge.

    Returns `{watchdog, collection_monitor, total, unread_count}` — `unread_count`
    is the total (or the scoped count when `source_kind` is set). RLS scopes the
    count to the caller's own account.
    """
    return nf.get_unread_count(conn, source_kind=source_kind)


@router.post("/mark-all-seen")
def post_mark_all_seen(
    source_kind: Literal["watchdog", "collection_monitor", "system_health", "all"] = "all",
    conn: Any = Depends(tenant_pool.tenant_conn),
) -> dict[str, int]:
    """Mark every unseen dispatch (optionally scoped to a source) as seen.

    The UPDATE is account-blind SQL; the tenant pool's RLS UPDATE policy scopes it
    to the caller's own dispatches.
    """
    return {"updated": nf.mark_all_seen(conn, source_kind=source_kind)}


@router.post("/dispatches/{dispatch_id}/mark-seen")
def post_mark_seen(
    dispatch_id: str,
    conn: Any = Depends(tenant_pool.tenant_conn),
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

    Stays on the service-role connection: it reads the shared `listings`
    table (RLS deny-all for `authenticated`, the A5 shape) and stamps an
    `estimation_runs` row whose per-account `account_id` is Wave-1 metering
    scope — the correct tenant-pool move needs the two-connection split
    `POST /listings/lookup` uses (a follow-up).
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

    Service-role by design: the producers scan the whole market (rule #16),
    and the row-level trigger stamps each dispatch's `account_id`.
    """
    stats = nf.match_once(conn)
    change_stats = nf.match_changes_once(conn)
    monitor_stats = nf.match_monitored_collections_once(conn)
    return {
        "data": stats,
        "change_data": change_stats,
        "monitor_data": monitor_stats,
    }
