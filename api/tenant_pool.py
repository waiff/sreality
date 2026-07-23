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
from fastapi import Depends

from api import dependencies as deps
from scraper import db

_TENANT_POOL_ENV = "TENANT_POOL_DB_URL"


def tenant_conn(
    claims: dict = Depends(deps.verify_jwt),
) -> "Iterator[psycopg.Connection]":
    """FastAPI dependency: one transaction, RLS-scoped to the caller's accounts.

    verify_jwt is a dependency of THIS function; FastAPI caches dependency
    results per request, so a route that also declares Depends(verify_jwt)
    pays no second verification.

    Dual-auth window: a legacy static-API_TOKEN caller has no Supabase `sub`,
    so under RLS it would see zero rows on every tenant table — a silent
    regression for the operator's current SPA/extension (the only production
    callers today). Legacy callers therefore stay on the unscoped service-role
    connection (today's exact behavior) until they re-auth with a real JWT.
    """
    if claims.get("legacy"):
        # Mirror get_db_conn exactly (this branch IS the service-role path):
        # db.connect adds the one-retry-on-pooler-blip + TCP keepalives + a clean
        # RuntimeError when SUPABASE_DB_URL is unset, which the bare
        # psycopg.connect(os.environ[...]) here did not (a transient hiccup
        # 500'd every /pipeline/* route, and a missing env var raised KeyError).
        conn = db.connect(
            attempts=deps._API_CONNECT_ATTEMPTS,
            retry_delay=deps._API_CONNECT_RETRY_DELAY,
        )
        try:
            yield conn
        finally:
            conn.close()
        return

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
    """The account rows are written under: the caller's own (first) account, or —
    for the legacy static-token operator — the account that claimed the legacy
    backfill (None until the operator's first signup)."""
    if claims.get("legacy"):
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT account_id FROM legacy_backfill_claim "
                    "WHERE claim_key = 'legacy_backfill_v1'"
                )
                row = cur.fetchone()
        except psycopg.errors.UndefinedTable:
            # pre-294 schema during the rollout window; the legacy service-role
            # branch is autocommit, so the failed statement poisons nothing.
            return None
        return row[0] if row else None
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
