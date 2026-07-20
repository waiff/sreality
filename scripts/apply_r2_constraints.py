"""Phase B of the R2 refactor: index + FK the surrogate columns the backfill filled.

Per carrier column, in this order:
  1. CREATE INDEX CONCURRENTLY on the new column (skipped if present).
  2. ADD FOREIGN KEY (new) REFERENCES listings(id) NOT VALID — but ONLY where the
     legacy column already has an FK to listings. Which carriers those are is read
     from pg_constraint rather than hardcoded, so this cannot drift from the real
     graph: Class B ledgers (dedup_pair_audit, property_merge_events, the estimation
     family) deliberately have no FK, and they simply never grow one here.
  3. VALIDATE CONSTRAINT — the full-table check, non-blocking.

Why a script and not a plain migration: steps 1 and 3 must not run inside a
transaction (CONCURRENTLY is illegal there, and VALIDATE wants its own), and both
are far too long for the transaction pooler's ~2 minute cap on the 8.08M-row
images table. Everything here runs on the session connection, and each step is
idempotent so a killed run simply resumes.

Locking, and why each step is safe against the always-on writer:
  - CREATE INDEX CONCURRENTLY takes no blocking lock.
  - ADD ... NOT VALID takes SHARE ROW EXCLUSIVE on the child AND on listings, which
    conflicts with the writer's ordinary INSERTs. It is brief, but it must never sit
    at the head of listings' lock queue — hence a short lock_timeout and retry.
  - VALIDATE takes SHARE UPDATE EXCLUSIVE on the child and only ROW SHARE on
    listings: it blocks no writes, it is just IO.

    python -m scripts.apply_r2_constraints --dry-run
    python -m scripts.apply_r2_constraints --table images
    python -m scripts.apply_r2_constraints

Requires SUPABASE_DB_URL (+ SUPABASE_DB_SESSION_URL). Safe to re-run.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Any

import psycopg

from scraper import db
from toolkit.listing_identity import R2_CARRIERS, R2_CARRIERS_BY_TABLE

log = logging.getLogger("apply_r2_constraints")

_LOCK_TIMEOUT = "3s"
_LOCK_RETRIES = 10
_RETRY_SLEEP = 20.0


def _legacy_has_fk(conn: "psycopg.Connection", table: str, legacy: str) -> bool:
    """Does the LEGACY column carry an FK to listings? Mirrors reality instead of a
    hardcoded class list, so a carrier can never be given an FK its family never had."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM pg_constraint con
            JOIN pg_class src ON src.oid = con.conrelid
            JOIN pg_class tgt ON tgt.oid = con.confrelid
            JOIN pg_attribute att ON att.attrelid = src.oid
                                 AND att.attnum = ANY(con.conkey)
            WHERE con.contype = 'f' AND tgt.relname = 'listings'
              AND src.relname = %s AND att.attname = %s
            """,
            (table, legacy),
        )
        return int(cur.fetchone()[0]) > 0


def _index_name(table: str, col: str) -> str:
    return f"{table}_{col}_idx"[:63]


def _fk_name(table: str, col: str) -> str:
    return f"{table}_{col}_fkey"[:63]


def _exists(conn: "psycopg.Connection", sql: str, params: tuple[Any, ...]) -> bool:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone() is not None


def _index_ready(conn: "psycopg.Connection", name: str) -> bool:
    """A CONCURRENTLY build killed mid-flight leaves an INVALID index behind; treat
    that as absent so the next run drops and rebuilds it rather than trusting it."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT i.indisvalid FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
            "WHERE c.relname = %s",
            (name,),
        )
        row = cur.fetchone()
    return bool(row and row[0])


def _index_present_but_invalid(conn: "psycopg.Connection", name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT i.indisvalid FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
            "WHERE c.relname = %s",
            (name,),
        )
        row = cur.fetchone()
    return bool(row and row[0] is False)


def _fk_state(conn: "psycopg.Connection", table: str, name: str) -> str | None:
    """None = absent, 'not_valid' = present unvalidated, 'valid' = done."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT convalidated FROM pg_constraint con "
            "JOIN pg_class c ON c.oid = con.conrelid "
            "WHERE c.relname = %s AND con.conname = %s",
            (table, name),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return "valid" if row[0] else "not_valid"


def _with_lock_retry(conn: "psycopg.Connection", sql: str, label: str) -> None:
    """Run a statement that briefly needs a strong lock on listings, retrying until
    it catches a gap between the writer's transactions.

    DeadlockDetected is retried alongside LockNotAvailable, and it is not an exotic
    case: ADD FOREIGN KEY takes SHARE ROW EXCLUSIVE on the child AND on listings,
    while the ingest path locks the same two tables in the opposite order (a new
    listing's singleton property is created inside the listings-insert transaction).
    Either side can be chosen as the deadlock victim. Both errors mean the same
    thing here — someone else held it first — so both just wait and try again.
    """
    for attempt in range(_LOCK_RETRIES):
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET lock_timeout = '{_LOCK_TIMEOUT}'")
                cur.execute(sql)
            conn.commit()
            return
        except (psycopg.errors.LockNotAvailable, psycopg.errors.DeadlockDetected) as exc:
            conn.rollback()
            log.info(
                "%s: %s (attempt %d) — retrying",
                label, type(exc).__name__, attempt + 1,
            )
            time.sleep(_RETRY_SLEEP)
    raise RuntimeError(f"{label}: could not acquire lock after {_LOCK_RETRIES} attempts")


def apply_column(
    conn: "psycopg.Connection", table: str, legacy: str, new: str, dry_run: bool,
) -> None:
    idx, fk = _index_name(table, new), _fk_name(table, new)
    wants_fk = _legacy_has_fk(conn, table, legacy)

    if dry_run:
        log.info(
            "%-34s %-22s index=%s fk=%s(%s)",
            table, new,
            "ok" if _index_ready(conn, idx) else "todo",
            "n/a" if not wants_fk else (_fk_state(conn, table, fk) or "todo"),
            "wanted" if wants_fk else "class-B",
        )
        return

    if _index_present_but_invalid(conn, idx):
        log.warning("%s: dropping INVALID index left by a killed build", idx)
        with conn.cursor() as cur:
            cur.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {idx}")
        conn.commit()

    if not _index_ready(conn, idx):
        log.info("%s: building index CONCURRENTLY", idx)
        # CONCURRENTLY cannot run inside a transaction block.
        conn.commit()
        old_autocommit = conn.autocommit
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute("SET statement_timeout = '60min'")
                cur.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {idx} ON {table} ({new})")
        finally:
            conn.autocommit = old_autocommit
        log.info("%s: built", idx)

    if not wants_fk:
        return

    state = _fk_state(conn, table, fk)
    if state is None:
        log.info("%s: adding FK NOT VALID", fk)
        _with_lock_retry(
            conn,
            f"ALTER TABLE {table} ADD CONSTRAINT {fk} "
            f"FOREIGN KEY ({new}) REFERENCES listings (id) NOT VALID",
            fk,
        )
        state = "not_valid"

    if state == "not_valid":
        log.info("%s: validating", fk)
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '60min'")
            cur.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {fk}")
        conn.commit()
        log.info("%s: validated", fk)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", default="", help="One carrier only (default: all).")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    if args.table:
        carrier = R2_CARRIERS_BY_TABLE.get(args.table)
        if carrier is None:
            print(f"ERROR: {args.table} is not an R2 carrier.", file=sys.stderr)
            return 2
        carriers = [carrier]
    else:
        carriers = R2_CARRIERS

    with db.connect_session() as conn:
        for carrier in carriers:
            for legacy, new in carrier["cols"]:
                apply_column(conn, carrier["table"], legacy, new, args.dry_run)
    log.info("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
