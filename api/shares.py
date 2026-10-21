"""Sharing, all three kinds (migrations 610, 641, 647; docs/design/pipeline-sharing.md §3.4): grant, notify, revoke, label."""

# A grant is written on the SERVICE ROLE (browser roles hold no INSERT on any share table, nor on
# notification_dispatches), only after the route's own-account tenant gate passed (sharing never
# delegates). A revoke or a leave is a tenant DELETE that the party policy scopes.

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Callable, ContextManager

import psycopg
from fastapi import HTTPException

from api import tenant_pool
from api.notifications import ON_EVENT_CONFLICT
from api.schemas import ShareMode, ShareSide


@dataclass(frozen=True)
class CollectionShare:
    collection_id: int


@dataclass(frozen=True)
class PipelineShare:
    owner: uuid.UUID
    mode: ShareMode


@dataclass(frozen=True)
class DashboardShare:
    dashboard_id: uuid.UUID


def party_label(col: str) -> str:
    """The account's label as the caller may see it: a counterparty's email, else NULL."""
    return f"(SELECT e.email FROM share_party_emails() e WHERE e.account_id = {col})"


def delegated_stamp(relation: str, row_key: str, changed_at: str) -> str:
    """`edited_by_email, edited_at` from migration 641's one stamp definition, latest_delegated_edit()."""
    latest = f"latest_delegated_edit('{relation}', {row_key}, {changed_at})"
    return (
        f"{party_label(f'(SELECT l.actor_account_id FROM {latest} l)')} AS edited_by_email, "
        f"(SELECT l.at FROM {latest} l) AS edited_at"
    )


# account_id is the OWNER, stamped by trigger from the collection or dashboard; RETURNING it names the sender.
_INSERT_COLLECTION_SHARE_SQL = (
    "INSERT INTO collection_shares (collection_id, recipient_account_id) VALUES (%s, %s) "
    "ON CONFLICT DO NOTHING RETURNING account_id"
)
_INSERT_DASHBOARD_SHARE_SQL = (
    "INSERT INTO stats_dashboard_shares (dashboard_id, recipient_account_id) VALUES (%s, %s) "
    "ON CONFLICT DO NOTHING RETURNING account_id"
)

# A row comes back when the grant is new or its mode changed; `inserted` tells the two apart.
_UPSERT_PIPELINE_SHARE_SQL = (
    "INSERT INTO pipeline_shares AS s (account_id, recipient_account_id, mode) VALUES (%s, %s, %s) "
    "ON CONFLICT (account_id, recipient_account_id) DO UPDATE SET mode = EXCLUDED.mode "
    "WHERE s.mode <> EXCLUDED.mode "
    "RETURNING (s.xmax = 0) AS inserted"
)

# ONE notification per identity ever (EVENT_IDENTITY, migration 653): no time in it, so a revoke +
# re-share loop cannot refill the recipient's inbox. No text either (F17, R11): the bell names the
# sharer from the actor through share_party_emails() and the shared thing by its live name, so the
# address drops out once no share joins the two (api/notifications._DISPATCH_SELECT). The kind names
# what opens (migration 652), the actor is the sharer (the owner: sharing never delegates) and
# event_ref tells two shares of one kind apart (the dashboard id, the pipeline mode; NULL for a
# collection, whose id is its own column).
_INSERT_SHARE_DISPATCH_SQL = (
    "INSERT INTO notification_dispatches "
    "  (source_kind, collection_id, change_kind, account_id, actor_account_id, event_ref) "
    "VALUES ('share', %(collection_id)s::bigint, %(change_kind)s::text, %(recipient)s::uuid, "
    "        %(actor)s::uuid, %(event_ref)s::text) "
    f"{ON_EVENT_CONFLICT}"
)

_DELETE_COLLECTION_SHARE_SQL = (
    "DELETE FROM collection_shares WHERE collection_id = %s AND recipient_account_id = %s"
)
_DELETE_DASHBOARD_SHARE_SQL = (
    "DELETE FROM stats_dashboard_shares WHERE dashboard_id = %s AND recipient_account_id = %s"
)

# The path names the counterparty; the side says which of the caller's two lists it sits in, so a
# mutual pair (A shares with B and B with A) loses one grant, never both. The party policy still
# scopes both statements.
_REVOKE_MY_PIPELINE_SHARE_SQL = (
    "DELETE FROM pipeline_shares WHERE recipient_account_id = %s "
    "AND account_id IN (SELECT current_account_ids())"
)
_LEAVE_PIPELINE_SHARE_SQL = (
    "DELETE FROM pipeline_shares WHERE account_id = %s "
    "AND recipient_account_id IN (SELECT current_account_ids())"
)

_LIST_PIPELINE_SHARES_SQL = (
    f"SELECT q.mine, q.party, q.mode, {party_label('q.party')} "
    "FROM ("
    "  SELECT s.account_id IN (SELECT current_account_ids()) AS mine, "
    "    CASE WHEN s.account_id IN (SELECT current_account_ids()) "
    "         THEN s.recipient_account_id ELSE s.account_id END AS party, "
    "    s.mode, s.created_at "
    "  FROM pipeline_shares s) q "
    "ORDER BY q.created_at, q.party"
)

def resolve_recipient(identity_conn: "psycopg.Connection", email: str) -> uuid.UUID:
    """The confirmed account behind an email, or 422 (the same answer for unknown and unconfirmed)."""
    recipient = tenant_pool.account_for_email(identity_conn, email)
    if recipient is None:
        raise HTTPException(422, "No account uses this email.")
    return recipient


def owner_gate(conn: "psycopg.Connection", gate_sql: str, key: Any, noun: str) -> None:
    """The owner's tenant gate (an UPDATE): a row back, or a 404 before the service role opens."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(gate_sql, (key,))
        gate = cur.fetchone()
    if gate is None:
        raise HTTPException(404, f"{noun} not found")


def grant(
    open_identity_conn: Callable[[], ContextManager["psycopg.Connection"]],
    email: str,
    share: CollectionShare | PipelineShare | DashboardShare,
) -> None:
    """Write the grant and, when new (or widened to edit), its one notification; one service-role transaction."""
    with open_identity_conn() as identity_conn:
        recipient = resolve_recipient(identity_conn, email)
        with identity_conn.transaction(), identity_conn.cursor() as cur:
            # Only the share's own CHECK (not-self) is a 422; a CHECK on the dispatch is drift
            # and must page as a 500.
            try:
                if isinstance(share, CollectionShare):
                    cur.execute(_INSERT_COLLECTION_SHARE_SQL, (share.collection_id, recipient))
                elif isinstance(share, DashboardShare):
                    cur.execute(_INSERT_DASHBOARD_SHARE_SQL, (share.dashboard_id, recipient))
                else:
                    cur.execute(_UPSERT_PIPELINE_SHARE_SQL, (share.owner, recipient, share.mode))
            except psycopg.errors.CheckViolation:
                raise HTTPException(422, "That's your own account.")
            row = cur.fetchone()
            if row is None:
                return
            if isinstance(share, CollectionShare):
                cur.execute(_INSERT_SHARE_DISPATCH_SQL, _share_dispatch(
                    "shared_collection", share.collection_id, row[0], recipient, None))
            elif isinstance(share, DashboardShare):
                cur.execute(_INSERT_SHARE_DISPATCH_SQL, _share_dispatch(
                    "shared_dashboard", None, row[0], recipient, str(share.dashboard_id)))
            elif row[0] or share.mode == "edit":
                cur.execute(_INSERT_SHARE_DISPATCH_SQL, _share_dispatch(
                    "shared_pipeline", None, share.owner, recipient, share.mode))


def _share_dispatch(
    change_kind: str, collection_id: int | None, actor: uuid.UUID, recipient: uuid.UUID,
    event_ref: str | None,
) -> dict[str, Any]:
    """The share notification's row: `actor` (the sharer) is stored, never a word of text."""
    return {"change_kind": change_kind, "collection_id": collection_id, "actor": actor,
            "recipient": recipient, "event_ref": event_ref}


def revoke_collection(
    conn: "psycopg.Connection", collection_id: int, recipient: str,
) -> dict[str, Any]:
    """Revoke (owner) or leave (recipient): the party policy's USING decides which; 0 rows -> 404."""
    return _delete(conn, _DELETE_COLLECTION_SHARE_SQL, (collection_id, recipient))


def revoke_dashboard(
    conn: "psycopg.Connection", dashboard_id: str, recipient: str,
) -> dict[str, Any]:
    """Revoke (owner) or leave (recipient): the party policy's USING decides which; 0 rows -> 404."""
    return _delete(conn, _DELETE_DASHBOARD_SHARE_SQL, (dashboard_id, recipient))


def revoke_pipeline(
    conn: "psycopg.Connection",
    party: str,
    side: ShareSide,
) -> dict[str, Any]:
    """Stop sharing my board with `party`, or leave `party`'s board; 0 rows -> 404."""
    sql = _REVOKE_MY_PIPELINE_SHARE_SQL if side == "shared_with" else _LEAVE_PIPELINE_SHARE_SQL
    return _delete(conn, sql, (party,))


def _delete(conn: "psycopg.Connection", sql: str, params: tuple[Any, ...]) -> dict[str, Any]:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(sql, params)
        if cur.rowcount == 0:
            raise HTTPException(404, "share not found")
    return {"deleted": True}


def list_pipeline_shares(conn: "psycopg.Connection") -> dict[str, Any]:
    """{shared_with: my board's recipients, shared_with_me: boards shared to me}, each labelled
    through share_party_emails(); empty while acting (the party policy hides the rows)."""
    with conn.cursor() as cur:
        cur.execute(_LIST_PIPELINE_SHARES_SQL)
        rows = cur.fetchall()
    out: dict[str, list[dict[str, Any]]] = {"shared_with": [], "shared_with_me": []}
    for mine, party, mode, email in rows:
        out["shared_with" if mine else "shared_with_me"].append(
            {"account_id": str(party), "email": email, "mode": mode},
        )
    return out
