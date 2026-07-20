"""Phase D of the R2 refactor: pre-flip preparation on `listings` and its children.

Three independent, deploy-order-free pieces (runbook §5 steps 2-5; step 2's
dirty_broker_listings sub-step is deliberately NOT here — see below):

  1. DROP NOT NULL on every R2_CARRIERS legacy column that is still NOT NULL today,
     derived live via the same `_legacy_column_not_null` predicate Phase B2 used to
     decide which columns got a NOT NULL CHECK on their surrogate sibling — so this
     cannot drift from that registry. (Live count: 17, not the runbook's estimated
     14 — the doc's number predates checking pg_attribute; same class of discrepancy
     Phase A's own audits kept finding, per docs/design/listing-identity-r2-pk-swap-
     runbook.md's own admission that its numbers were design-time estimates.)
  2. `estimation_cohort_entries` PK swap from (estimation_run_id, sreality_id) to
     (estimation_run_id, listing_id). Safe to do now, no writer-deploy dependency:
     Phase A4 already backfilled listing_id to 100% and Phase B2 already added a
     validated NOT NULL check + a UNIQUE(estimation_run_id, listing_id) constraint
     on it (PR #839) — but that constraint's index cannot be reused as the new PK's
     backing index (an index already owned by one constraint can't back a second;
     same reason Gate 1 pre-builds a fresh `listings_id_pk_idx` instead of reusing
     `listings_id_key`), so this builds its own dedicated index.
  3. `listings_sreality_id_uidx` / `listings_id_pk_idx`, the two indexes Gate 1 (the
     actual destructive PK swap) needs pre-built, plus `id SET NOT NULL` (instant —
     proven by the validated `listings_id_present` CHECK from migration 313, no scan).

`dirty_broker_listings` is excluded from all three: it isn't an R2_CARRIERS member
(no listing_id column exists yet at all — R2 deliberately left it to a "writer swap
at cutover" per toolkit/listing_identity.py's docstring), so unlike the above it DOES
have a deploy-order dependency (the #825 lesson: enforcing NOT NULL before the
dual-write code is live would break the ingest writer). It ships in its own
migration + code PR, backfilled and PK-swapped only after that deploy is confirmed
live — see docs/design/listing-identity-r2-pk-swap-runbook.md Progress section.

Why a script and not a migration: the two CREATE UNIQUE INDEX CONCURRENTLY calls on
`listings` (562k+ rows) are illegal inside a transaction block, same reasoning as
apply_r2_constraints.py (Phase B) and apply_r2_unique_guards.py (Phase B2), whose
lock-retry and index-state helpers this reuses. The DROP NOT NULLs and PK swap are
individually instant, but each still runs in its own retried transaction (`_with_
lock_retry`) rather than one big one, so a lock wait on one carrier can't hold up
or roll back the others.

    python -m scripts.apply_r2_phase_d_prep --dry-run
    python -m scripts.apply_r2_phase_d_prep

Requires SUPABASE_DB_URL (+ SUPABASE_DB_SESSION_URL). Safe to re-run.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import psycopg

from scraper import db
from scripts.apply_r2_constraints import _with_lock_retry
from scripts.apply_r2_unique_guards import _legacy_column_not_null
from toolkit.listing_identity import R2_CARRIERS

log = logging.getLogger("apply_r2_phase_d_prep")

_ESTIMATION_COHORT_PK_IDX = "estimation_cohort_entries_run_listing_id_pk_idx"
_LISTINGS_SREALITY_UIDX = "listings_sreality_id_uidx"
_LISTINGS_ID_PK_IDX = "listings_id_pk_idx"


def _not_null_name(table: str, col: str) -> str:
    return f"{table}_{col}_dropnn"[:63]


def _index_ready(conn: "psycopg.Connection", name: str) -> bool:
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


def _build_index_concurrently(conn: "psycopg.Connection", name: str, sql: str) -> None:
    if _index_present_but_invalid(conn, name):
        log.warning("%s: dropping INVALID index left by a killed build", name)
        with conn.cursor() as cur:
            cur.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
        conn.commit()
    if _index_ready(conn, name):
        return
    log.info("%s: building CONCURRENTLY", name)
    conn.commit()
    old_autocommit = conn.autocommit
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '60min'")
            cur.execute(sql)
    finally:
        conn.autocommit = old_autocommit
    log.info("%s: built", name)


def drop_legacy_not_nulls(conn: "psycopg.Connection", dry_run: bool) -> None:
    for carrier in R2_CARRIERS:
        table = carrier["table"]
        for legacy, _new in carrier["cols"]:
            if not _legacy_column_not_null(conn, table, legacy):
                continue
            label = f"{table}.{legacy}"
            if dry_run:
                log.info("%-50s DROP NOT NULL: todo", label)
                continue
            log.info("%s: dropping NOT NULL", label)
            _with_lock_retry(
                conn, f"ALTER TABLE {table} ALTER COLUMN {legacy} DROP NOT NULL",
                _not_null_name(table, legacy),
            )


def swap_estimation_cohort_entries_pk(conn: "psycopg.Connection", dry_run: bool) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'estimation_cohort_entries'::regclass AND contype = 'p'"
        )
        row = cur.fetchone()
    current_pk = row[0] if row else None
    already_swapped = current_pk is not None and "listing_id" in current_pk
    # A killed run may leave the table between DROP and ADD with no PK at all —
    # `current_pk is None` in that case, distinct from "still on the old PK".
    has_old_pk = current_pk is not None and "listing_id" not in current_pk

    if dry_run:
        log.info(
            "estimation_cohort_entries pk=%s idx=%s",
            "ok" if already_swapped else "todo",
            "ok" if _index_ready(conn, _ESTIMATION_COHORT_PK_IDX) else "todo",
        )
        return
    if already_swapped:
        return

    _build_index_concurrently(
        conn, _ESTIMATION_COHORT_PK_IDX,
        f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {_ESTIMATION_COHORT_PK_IDX} "
        "ON estimation_cohort_entries (estimation_run_id, listing_id)",
    )
    log.info("estimation_cohort_entries: swapping PK to (estimation_run_id, listing_id)")
    if has_old_pk:
        _with_lock_retry(
            conn,
            "ALTER TABLE estimation_cohort_entries DROP CONSTRAINT "
            "estimation_cohort_entries_pkey",
            "estimation_cohort_entries_pkey_drop",
        )
    _with_lock_retry(
        conn,
        "ALTER TABLE estimation_cohort_entries ADD CONSTRAINT estimation_cohort_entries_pkey "
        f"PRIMARY KEY USING INDEX {_ESTIMATION_COHORT_PK_IDX}",
        "estimation_cohort_entries_pkey_add",
    )
    _with_lock_retry(
        conn,
        "ALTER TABLE estimation_cohort_entries ALTER COLUMN sreality_id DROP NOT NULL",
        "estimation_cohort_entries_sreality_id_dropnn",
    )
    log.info("estimation_cohort_entries: PK swap done")


def prepare_listings_pk_swap_indexes(conn: "psycopg.Connection", dry_run: bool) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT attnotnull FROM pg_attribute WHERE attrelid = 'listings'::regclass "
            "AND attname = 'id'"
        )
        row = cur.fetchone()
    id_not_null = bool(row and row[0])

    if dry_run:
        log.info(
            "listings sreality_uidx=%s id_pk_idx=%s id_not_null=%s",
            "ok" if _index_ready(conn, _LISTINGS_SREALITY_UIDX) else "todo",
            "ok" if _index_ready(conn, _LISTINGS_ID_PK_IDX) else "todo",
            "ok" if id_not_null else "todo",
        )
        return

    _build_index_concurrently(
        conn, _LISTINGS_SREALITY_UIDX,
        f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {_LISTINGS_SREALITY_UIDX} "
        "ON listings (sreality_id)",
    )
    _build_index_concurrently(
        conn, _LISTINGS_ID_PK_IDX,
        f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {_LISTINGS_ID_PK_IDX} "
        "ON listings (id)",
    )
    if not id_not_null:
        log.info("listings.id: SET NOT NULL (instant — proven by listings_id_present CHECK)")
        _with_lock_retry(
            conn, "ALTER TABLE listings ALTER COLUMN id SET NOT NULL",
            "listings_id_setnn",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
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

    with db.connect_session() as conn:
        # PK swap first: estimation_cohort_entries.sreality_id is still part of its
        # PK until this runs, and Postgres refuses DROP NOT NULL on a PK column
        # ("column is in a primary key") — the generic loop below would hit exactly
        # that on a fresh run. Once swapped, the loop's own live NOT NULL check
        # naturally skips it (this function already drops it as its last step).
        swap_estimation_cohort_entries_pk(conn, args.dry_run)
        drop_legacy_not_nulls(conn, args.dry_run)
        prepare_listings_pk_swap_indexes(conn, args.dry_run)
    log.info("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
