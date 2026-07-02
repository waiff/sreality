"""Weekly sampled parity check: stored listings.street_name_key == the Python function.

street_name_key (migration 256) is stamped at every listings.street write path from the
ONE normalizer, scraper.street.street_name_key. Two guard classes exist: the
forgot-to-stamp class fails loudly at write time (the presence CHECK, migration 264);
this job guards the STALE class — a stored key that no longer matches the function
(a normalizer edit without the required --all re-key, or a writer stamping a wrong
value). Silent drift here costs dedup recall: the --dirty scoped load matches peers by
the STORED key, so a drifted key quietly under-loads street groups (the 6h full scan
recomputes live, so the damage is latency + partial coverage, never a wrong merge —
but it should page, not hide).

Sample = the N most recent street-bearing rows (new write paths surface here first)
UNION a block-random sample across the table (long-tail drift). Any mismatch fails the
run (exit 1) -> the workflow fails -> monitor_workflow_failures records it -> the Health
page lists it. Zero new alerting plumbing.

Usage:  python -m scripts.check_street_key_parity [--recent 2500] [--random 2500]
Required: SUPABASE_DB_URL.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from scraper import db
from scraper.street import street_name_key

LOG = logging.getLogger("check_street_key_parity")

_RECENT_SQL = """
    SELECT sreality_id, street, street_name_key
    FROM listings
    WHERE street IS NOT NULL AND street <> ''
    ORDER BY sreality_id DESC
    LIMIT %(n)s
"""

# Block-sampling (TABLESAMPLE SYSTEM) is cheap and unbiased enough for a drift check —
# ~2% of blocks yields thousands of street-bearing rows on the current table; LIMIT
# trims to the requested sample. A fully uniform ORDER BY random() would seq-scan.
_RANDOM_SQL = """
    SELECT sreality_id, street, street_name_key
    FROM listings TABLESAMPLE SYSTEM (2)
    WHERE street IS NOT NULL AND street <> ''
    LIMIT %(n)s
"""


def find_mismatches(
    rows: list[tuple[int, str, str | None]],
) -> list[tuple[int, str, str | None, str | None]]:
    """(sreality_id, street, stored, recomputed) for every row whose stored key differs
    from the function. Pure — the whole decision, unit-tested."""
    out: list[tuple[int, str, str | None, str | None]] = []
    for sid, street, stored in rows:
        expected = street_name_key(street)
        if stored != expected:
            out.append((int(sid), street, stored, expected))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recent", type=int, default=2500,
                        help="Newest street-bearing rows to check (new-write-path drift).")
    parser.add_argument("--random", type=int, default=2500, dest="random_n",
                        help="Block-random street-bearing rows to check (long-tail drift).")
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
        with conn.cursor() as cur:
            cur.execute(_RECENT_SQL, {"n": args.recent})
            rows = list(cur.fetchall())
            cur.execute(_RANDOM_SQL, {"n": args.random_n})
            rows += list(cur.fetchall())

    mismatches = find_mismatches(rows)
    LOG.info("PARITY checked=%d mismatches=%d", len(rows), len(mismatches))
    if not mismatches:
        return 0
    for sid, street, stored, expected in mismatches[:20]:
        LOG.error("PARITY MISMATCH sid=%s street=%r stored=%r expected=%r",
                  sid, street, stored, expected)
    if len(mismatches) > 20:
        LOG.error("PARITY ... and %d more", len(mismatches) - 20)
    LOG.error(
        "Stored street_name_key drifted from scraper.street.street_name_key. If the "
        "normalizer was edited, run the full re-key: dispatch backfill_street_name_key.yml "
        "with all=true. Otherwise find the write path that stamped the wrong value."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
