"""Stats dashboards (migration 647, docs/design/saved-dashboards.md): the saved set, its shares, the working board."""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable, ContextManager

import psycopg
from fastapi import APIRouter, Depends

from api import dependencies as deps
from api import saved_sets
from api import schemas as s
from api import shares
from api import tenant_pool

# The one "is this row the caller's own" predicate: dashboards never act (pipeline-sharing.md §3.7).
_IS_MINE_SQL = "t.account_id IN (SELECT current_account_ids())"

DASHBOARDS = saved_sets.SavedSetKind(
    table="stats_dashboards",
    noun="dashboard",
    id_param="dashboard_id",
    blob_columns=("charts", "filter_spec"),
    create_in=s.CreateStatsDashboardIn,
    update_in=s.UpdateStatsDashboardIn,
    nullable=frozenset({"filter_spec", "color"}),
    projection_extra=(
        ("owner_email", f"CASE WHEN {_IS_MINE_SQL} THEN NULL ELSE {shares.party_label('t.account_id')} END"),
        ("shared_with", "(SELECT coalesce(json_agg(json_build_object("
                        "'account_id', s.recipient_account_id, "
                        f"'email', {shares.party_label('s.recipient_account_id')}) ORDER BY s.created_at), "
                        "'[]'::json) FROM stats_dashboard_shares s WHERE s.dashboard_id = t.id)"),
    ),
    row_scope="name IS NOT NULL",
    # Own dashboards first, then those shared with the caller (the SPA splits the list the same way).
    order_by=f"({_IS_MINE_SQL}) DESC, t.position ASC, t.created_at DESC",
    own_rows=_IS_MINE_SQL,
)
# Every statement the kind runs, for the CI PREPARE sweep (tests/sql_corpus import-resolves `*_SQL`).
DASHBOARD_SQL = saved_sets.statements(DASHBOARDS)

# The ownership gate names the caller's OWN accounts and a NAMED dashboard: a recipient sees the row but
# matches nothing here, and the working board is never shared (647's trigger refuses it as well).
_SHARE_GATE_SQL = (
    "UPDATE stats_dashboards SET updated_at = now() "
    "WHERE id = %s AND account_id IN (SELECT current_account_ids()) AND name IS NOT NULL RETURNING name"
)

# RLS alone scopes the read (tenancy shape 1); one membership per user makes it the PUT's row, and a second
# membership would need the write's account resolution here.
_WORKING_BOARD_SQL = (
    "SELECT id, charts, updated_at FROM stats_dashboards WHERE name IS NULL ORDER BY updated_at DESC LIMIT 1"
)

_SAVE_WORKING_BOARD_SQL = (
    "INSERT INTO stats_dashboards (account_id, charts, updated_at) VALUES (%s, %s::jsonb, now()) "
    "ON CONFLICT (account_id) WHERE name IS NULL DO UPDATE "
    "SET charts = EXCLUDED.charts, updated_at = EXCLUDED.updated_at "
    "RETURNING updated_at"
)

router = APIRouter(tags=["stats-dashboards"])
router.include_router(saved_sets.saved_set_router(DASHBOARDS, "/stats/dashboards"))


def working_board(conn: psycopg.Connection) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(_WORKING_BOARD_SQL)
        row = cur.fetchone()
    if row is None:
        return None
    return {"id": str(row[0]), "charts": row[1], "updated_at": row[2].isoformat()}


def save_working_board(
    conn: psycopg.Connection, charts: list[dict[str, Any]], *, account_id: uuid.UUID,
) -> dict[str, Any]:
    """Replace the account's working board with `charts` (inserted on the first save)."""
    with conn.cursor() as cur:
        cur.execute(_SAVE_WORKING_BOARD_SQL, (account_id, json.dumps(charts)))
        row = cur.fetchone()
    assert row is not None
    return {"charts": charts, "updated_at": row[0].isoformat()}


def share_dashboard(
    conn: psycopg.Connection,
    open_identity_conn: Callable[[], ContextManager[psycopg.Connection]],
    dashboard_id: uuid.UUID,
    body: s.ShareDashboardIn,
) -> dict[str, Any]:
    """Share one named dashboard read-only with the account behind a confirmed email (owner only)."""
    shares.owner_gate(conn, _SHARE_GATE_SQL, dashboard_id, "dashboard")
    shares.grant(open_identity_conn, body.email, shares.DashboardShare(dashboard_id))
    return saved_sets.row_or_404(DASHBOARDS, saved_sets.get(conn, DASHBOARDS, str(dashboard_id)))


@router.post("/stats/dashboards/{dashboard_id}/shares")
def post_dashboard_share(
    dashboard_id: uuid.UUID,
    body: s.ShareDashboardIn,
    conn: Any = Depends(tenant_pool.tenant_conn),
    _own: uuid.UUID = Depends(tenant_pool.require_account_id),  # sharing never delegates: 403 under X-Act-As
) -> dict[str, Any]:
    return share_dashboard(conn, deps.open_background_conn, dashboard_id, body)


@router.delete("/stats/dashboards/{dashboard_id}/shares/{account_id}")
def delete_dashboard_share(
    dashboard_id: uuid.UUID,
    account_id: uuid.UUID,
    conn: Any = Depends(tenant_pool.tenant_conn),
) -> dict[str, Any]:
    """Revoke (owner) or leave (recipient): the party policy decides which; 0 rows -> 404."""
    return shares.revoke_dashboard(conn, str(dashboard_id), str(account_id))


@router.get("/stats/dashboard")
def get_stats_dashboard(conn: Any = Depends(tenant_pool.tenant_conn)) -> dict[str, Any] | None:
    """The caller's working board, or null (the SPA then shows its default charts)."""
    return working_board(conn)


@router.put("/stats/dashboard")
def put_stats_dashboard(
    body: s.StatsDashboardIn,
    conn: Any = Depends(tenant_pool.tenant_conn),
    account_id: uuid.UUID = Depends(tenant_pool.require_account_id),
) -> dict[str, Any]:
    """Replace the caller's working board; every chart was validated by the stats registry (422)."""
    return save_working_board(conn, body.charts, account_id=account_id)
