"""Phase D step 6 of the R2 refactor: drop the 19 legacy child FKs onto
`listings(sreality_id)`.

Integrity is already held by the parallel `listing_id -> listings(id)` FKs Phase B
added and validated (PR #837/#838) for the identical set of carriers. Dropping the
legacy FK is what actually frees `sreality_id` to go NULL post-Gate-2 — a NOT NULL
child FK column can never reference a NULL parent value in the first place, but a
*nullable* one still can't reference a NULL parent through an FK edge that expects a
match, so any remaining legacy FK would reject the very rows Gate 2 exists to write.

The 19 is read live from pg_constraint (`contype = 'f'` targeting `listings`, def
mentioning `sreality_id`), not hardcoded — this list happens to match the runbook's
count exactly (unlike the NOT NULL column count, which didn't — see
apply_r2_phase_d_prep.py's docstring), but deriving it live means a future carrier
added or removed can't make this script silently stale.

Each DROP is its own retried transaction (`_with_lock_retry`, reused from Phase B):
`DROP CONSTRAINT` on an FK takes ACCESS EXCLUSIVE on the child and SHARE ROW
EXCLUSIVE-equivalent on `listings`, the same contended pair as Phase B's `ADD
CONSTRAINT`, including the same ingest-vs-FK opposite-lock-order deadlock class.

FK drops are NOT gate-destructive: re-addable any time via `ADD CONSTRAINT ...
NOT VALID` -> `VALIDATE CONSTRAINT` (exactly how Phase B added the new ones), so
this can run well ahead of the actual PK-swap window per runbook §5.6's "days
before" guidance without narrowing any rollback option.

    python -m scripts.drop_r2_legacy_fks --dry-run
    python -m scripts.drop_r2_legacy_fks

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

log = logging.getLogger("drop_r2_legacy_fks")

_FIND_LEGACY_FKS_SQL = """
    SELECT con.conrelid::regclass::text, con.conname
    FROM pg_constraint con
    JOIN pg_class tgt ON tgt.oid = con.confrelid
    WHERE con.contype = 'f' AND tgt.relname = 'listings'
      AND pg_get_constraintdef(con.oid) LIKE '%%REFERENCES listings(sreality_id)%%'
    ORDER BY con.conrelid::regclass::text, con.conname
"""


def find_legacy_fks(conn: "psycopg.Connection") -> list[tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute(_FIND_LEGACY_FKS_SQL)
        return [(row[0], row[1]) for row in cur.fetchall()]


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
        fks = find_legacy_fks(conn)
        log.info("found %d legacy FK(s) to drop", len(fks))
        for table, name in fks:
            if args.dry_run:
                log.info("%-34s %-46s DROP: todo", table, name)
                continue
            log.info("%s.%s: dropping", table, name)
            _with_lock_retry(conn, f"ALTER TABLE {table} DROP CONSTRAINT {name}", name)
        if not args.dry_run:
            remaining = find_legacy_fks(conn)
            if remaining:
                log.warning("still present after run: %s", remaining)
            else:
                log.info("all legacy FKs dropped")
    log.info("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
