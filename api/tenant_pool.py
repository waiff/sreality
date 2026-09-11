"""Per-request tenant-scoped DB transaction (Phase 1 Amendment A1).

ONE explicit transaction per request, autocommit off: SET LOCAL ROLE +
the verified JWT claims are issued first, and the SAME transaction serves the
route handler's reads AND writes (a SET LOCAL evaporates when its transaction
ends, so a post-commit read-back on another transaction would run claim-less
and RLS would hide the row just written). Distinct from
api.dependencies.get_db_conn (service-role, autocommit, unscoped) — this is
the RLS-enforced path every per-account route must use: verify_jwt alone is
authentication, not authorization.

The pool role (migration 293) is LOGIN + NOINHERIT with zero direct grants;
data access exists only under the explicit `SET LOCAL ROLE authenticated`,
so a code path that forgets the switch fails closed, never leaks.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator

import psycopg
from fastapi import Depends, HTTPException

from api import dependencies as deps

_TENANT_POOL_ENV = "TENANT_POOL_DB_URL"


def tenant_conn(
    claims: dict = Depends(deps.verify_jwt),
) -> "Iterator[psycopg.Connection]":
    """FastAPI dependency: one transaction, RLS-scoped to the caller's accounts.

    verify_jwt is a dependency of THIS function; FastAPI caches dependency
    results per request, so a route that also declares Depends(verify_jwt)
    pays no second verification.

    There is NO fallback connection: every caller is a real Supabase JWT and
    gets the RLS-scoped tenant-pool transaction. An unconfigured pool must
    fail loudly here rather than silently degrade to an unscoped connection.
    """
    dsn = os.environ.get(_TENANT_POOL_ENV)
    if not dsn:
        raise RuntimeError(f"{_TENANT_POOL_ENV} not configured")

    # prepare_threshold=None: the transaction-mode pooler rebinds physical
    # backends between transactions (scraper/db.py's standing rationale) —
    # which is also why the role/claims MUST be re-issued inside every
    # transaction rather than set once per connection.
    conn = psycopg.connect(dsn, autocommit=False, prepare_threshold=None)
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SET LOCAL ROLE authenticated")
                # set_config(), NOT "SET LOCAL ... = %s": SET takes only a
                # literal (a bind parameter is a syntax error), and f-string
                # interpolation would be an injection surface — claims carry
                # attacker-shaped strings from the caller's own JWT.
                cur.execute(
                    "SELECT set_config('request.jwt.claims', %s, true)",
                    (json.dumps(claims),),
                )
            yield conn
        # conn.transaction() commits on clean resumption, rolls back on the
        # route's exception — reads and writes share this one block.
    finally:
        conn.close()


def resolve_account_id(conn: psycopg.Connection, claims: dict) -> uuid.UUID | None:
    """The caller's own (first) account, or None when the user has no membership."""
    with conn.cursor() as cur:
        # ORDER BY for a deterministic pick: account_members has only a composite
        # PK, so a user with >1 membership (already legal — team/multi-account is
        # anticipated) would otherwise resolve to a per-request-arbitrary account,
        # making every pipeline write nondeterministically scoped. Stable oldest-
        # membership-wins until a real primary-account concept exists.
        cur.execute(
            "SELECT account_id FROM account_members WHERE user_id = %s "
            "ORDER BY created_at, account_id LIMIT 1",
            (claims["sub"],),
        )
        row = cur.fetchone()
    return row[0] if row else None


def require_account_id(
    conn: psycopg.Connection = Depends(tenant_conn),
    claims: dict = Depends(deps.verify_jwt),
) -> uuid.UUID:
    """The ONE account a write is scoped to, resolved once at the route edge.

    A caller with no membership is a loud 400, never a silently empty 200 or an
    opaque 500: every account-predicated write column is NOT NULL (migrations
    290/295) and RLS's WITH CHECK can only validate the account a row claims, not
    choose one — so a route that forwards None writes a row RLS is guaranteed to
    reject, or predicates a DELETE/UPDATE that matches nothing.

    Lives in tenant_pool, not api.dependencies: this module imports that one
    (tenant_conn's verify_jwt default), so Depends(tenant_conn) over there is a
    circular import — and a lazily-imported wrapper would be a SECOND callable,
    which FastAPI caches separately, splitting a route's reads and writes across
    two tenant transactions (the Amendment A1 boundary).
    """
    account_id = resolve_account_id(conn, claims)
    if account_id is None:
        raise HTTPException(status_code=400, detail="no account for caller")
    return account_id
