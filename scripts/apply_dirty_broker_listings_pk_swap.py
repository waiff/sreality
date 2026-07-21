"""R2 Phase D — finalize dirty_broker_listings's PK swap (sreality_id -> listing_id).

Migration 336 shipped the nullable listing_id dual-write column + writer code;
this was deliberately deferred until the writer deploy was confirmed fully live,
per the #825 lesson (a constraint added before every writer honors it breaks the
still-lagging ones). A live check found the fleet had fully rolled over — 6+ hours
post-336-merge, zero rows anywhere in dirty_broker_listings had a NULL listing_id,
including rows written by the GH-Actions-cron portals (subject to the SHA-freeze
gotcha, which can lag a merge by up to a full cadence cycle).

This must run BEFORE the matching writer-code PR (retargets both ON CONFLICT
sites to listing_id) deploys, or the new code's ON CONFLICT (listing_id) has no
unique index to infer from yet.

    python -m scripts.apply_dirty_broker_listings_pk_swap --dry-run
    python -m scripts.apply_dirty_broker_listings_pk_swap

Requires SUPABASE_DB_URL (+ SUPABASE_DB_SESSION_URL). Safe to re-run.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from scraper import db
from scripts.apply_r2_constraints import _with_lock_retry

log = logging.getLogger("apply_dirty_broker_listings_pk_swap")

_PK_INDEX = "dirty_broker_listings_listing_id_key"
_LEGACY_PLAIN_INDEX = "dirty_broker_listings_listing_id_idx"


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
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM dirty_broker_listings WHERE listing_id IS NULL")
            null_count = int(cur.fetchone()[0])
            cur.execute(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'dirty_broker_listings'::regclass AND contype = 'p'"
            )
            row = cur.fetchone()
        current_pk = row[0] if row else None
        already_swapped = current_pk is not None and "listing_id" in current_pk

        if args.dry_run:
            log.info(
                "dirty_broker_listings null_listing_id=%d pk=%s",
                null_count, "ok" if already_swapped else "todo",
            )
            return 0

        if already_swapped:
            log.info("already swapped, nothing to do")
            return 0

        if null_count:
            log.info("backfilling %d straggler row(s)", null_count)
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE dirty_broker_listings d SET listing_id = l.id "
                    "FROM listings l WHERE l.sreality_id = d.sreality_id "
                    "AND d.listing_id IS NULL"
                )
            conn.commit()

        _with_lock_retry(
            conn,
            "ALTER TABLE dirty_broker_listings ALTER COLUMN listing_id SET NOT NULL",
            "dirty_broker_listings_listing_id_setnn",
        )
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {_PK_INDEX} "
                "ON dirty_broker_listings (listing_id)"
            )
            cur.execute(f"DROP INDEX IF EXISTS {_LEGACY_PLAIN_INDEX}")
        conn.commit()
        _with_lock_retry(
            conn,
            "ALTER TABLE dirty_broker_listings DROP CONSTRAINT IF EXISTS "
            "dirty_broker_listings_pkey",
            "dirty_broker_listings_pkey_drop",
        )
        _with_lock_retry(
            conn,
            "ALTER TABLE dirty_broker_listings ADD CONSTRAINT dirty_broker_listings_pkey "
            f"PRIMARY KEY USING INDEX {_PK_INDEX}",
            "dirty_broker_listings_pkey_add",
        )
        _with_lock_retry(
            conn,
            "ALTER TABLE dirty_broker_listings ALTER COLUMN sreality_id DROP NOT NULL",
            "dirty_broker_listings_sreality_id_dropnn",
        )
    log.info("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
