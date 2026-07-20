"""Backfill listings.id (the R1 surrogate, migration 312) for pre-existing rows.

Assigns 1..N in (first_seen_at, sreality_id) order — so ascending id tracks
market chronology — to every row whose id is still NULL. New rows already get a
value from the sequence default (migration 312), starting at 10,000,000, so the
two epochs never collide.

Online + restartable + worker-safe:
- A durable mapping table (`listing_id_backfill_map`) freezes the assignment once,
  so a mid-run restart never renumbers.
- Batches lock rows FOR UPDATE ... SKIP LOCKED in sreality_id (PK) order, so the
  always-on writer is never deadlocked — a row the writer holds is skipped and
  swept on a later pass. Idempotent: only ever touches `id IS NULL`.

Usage: python -m scripts.backfill_listing_surrogate_id [--batch 60000] [--dry-run]
Requires SUPABASE_DB_URL. Safe to re-run; a no-op once every row has an id.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

import psycopg

from scraper import db

log = logging.getLogger("backfill_listing_id")

_BUILD_MAP_SQL = """
    CREATE TABLE IF NOT EXISTS listing_id_backfill_map (
        sreality_id bigint PRIMARY KEY,
        new_id      bigint NOT NULL
    );
    INSERT INTO listing_id_backfill_map (sreality_id, new_id)
    SELECT sreality_id,
           row_number() OVER (ORDER BY first_seen_at, sreality_id)
    FROM listings
    WHERE id IS NULL
    ON CONFLICT (sreality_id) DO NOTHING;
"""

# One batch: lock up to N still-NULL rows (PK order, skipping any the writer
# holds) and stamp their frozen id. RETURNS the number updated.
_BATCH_SQL = """
    WITH batch AS (
        SELECT l.sreality_id, m.new_id
        FROM listings l
        JOIN listing_id_backfill_map m ON m.sreality_id = l.sreality_id
        WHERE l.id IS NULL
        ORDER BY l.sreality_id
        LIMIT %(batch)s
        FOR UPDATE OF l SKIP LOCKED
    ), upd AS (
        UPDATE listings l SET id = b.new_id
        FROM batch b WHERE l.sreality_id = b.sreality_id
        RETURNING 1
    )
    SELECT count(*) FROM upd;
"""


def _run_batch(conn: "psycopg.Connection", batch: int) -> int:
    """One batch with a bounded retry on a transient serialization/deadlock."""
    for attempt in range(5):
        try:
            with conn.cursor() as cur:
                cur.execute("SET statement_timeout = '120s'")
                cur.execute(_BATCH_SQL, {"batch": batch})
                updated = int(cur.fetchone()[0])
            conn.commit()
            return updated
        except psycopg.errors.DeadlockDetected:
            conn.rollback()
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError("batch kept deadlocking after 5 attempts")


def backfill(conn: "psycopg.Connection", batch: int, dry_run: bool) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM listings WHERE id IS NULL")
        remaining = int(cur.fetchone()[0])
    log.info("rows still NULL: %d", remaining)
    if remaining == 0:
        return 0
    if dry_run:
        log.info("dry-run: would backfill %d rows in batches of %d", remaining, batch)
        return remaining

    with conn.cursor() as cur:
        cur.execute(_BUILD_MAP_SQL)
    conn.commit()

    done = 0
    while True:
        updated = _run_batch(conn, batch)
        if updated == 0:
            break
        done += updated
        log.info("backfilled %d (this batch %d)", done, updated)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM listings WHERE id IS NULL")
        left = int(cur.fetchone()[0])
        if left == 0:
            cur.execute("DROP TABLE IF EXISTS listing_id_backfill_map")
    conn.commit()
    log.info("done: backfilled %d this run, %d still NULL", done, left)
    return done


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=int, default=60000,
                        help="Rows per batch (SKIP LOCKED; smaller = less contention).")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    with db.connect() as conn:
        backfill(conn, args.batch, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
