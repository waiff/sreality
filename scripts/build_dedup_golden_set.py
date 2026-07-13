"""Freeze a named dedup golden-set snapshot (migration 300) from `dedup_label_events`.

Benchmarks (the vision bake-off, any future free-signal replay) should read a named
`dedup_golden_sets` snapshot, never the live view — `dedup_label_events` recomputes on every
query and its labels grow every day, so two benchmark runs against the live view are not
comparable. This script freezes one snapshot; re-running with the SAME set_name is idempotent
(ON CONFLICT DO NOTHING on (set_name, label_id)) — new labels that appeared since the first
freeze are simply not added under that name, so a published set_name stays reproducible. Use a
NEW set_name (e.g. suffix the date) to capture a later cut.

Run: `python -m scripts.build_dedup_golden_set --set-name 2026-07-13-session2` (env:
SUPABASE_DB_URL). `--holdout-floor` defaults to the program's calibration boundary
(2026-07-10, see docs/design/dedup-vision-and-backlog-overhaul.md §4) — labels at or before it
are training-tainted; pass an empty string to store the set without a floor.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Any

LOG = logging.getLogger("build_dedup_golden_set")

DEFAULT_HOLDOUT_FLOOR = "2026-07-10"

_FREEZE_SQL = """
    INSERT INTO dedup_golden_sets
        (set_name, holdout_floor, label_id, left_property_id, right_property_id,
         left_listing_id, right_listing_id, is_same, label_source, category_main, tier,
         labeled_at, reason)
    SELECT %(set_name)s, %(holdout_floor)s, label_id, left_property_id, right_property_id,
           left_listing_id, right_listing_id, is_same, label_source, category_main, tier,
           labeled_at, reason
    FROM dedup_label_events
    ON CONFLICT (set_name, label_id) DO NOTHING
"""

_COUNTS_SQL = """
    SELECT label_source, is_same, count(*)
    FROM dedup_golden_sets
    WHERE set_name = %(set_name)s
    GROUP BY label_source, is_same
    ORDER BY label_source, is_same
"""


def freeze(conn: Any, *, set_name: str, holdout_floor: str | None) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(_FREEZE_SQL, {"set_name": set_name, "holdout_floor": holdout_floor})
        inserted = cur.rowcount or 0
        cur.execute(_COUNTS_SQL, {"set_name": set_name})
        by_stratum = {f"{row[0]}:{'pos' if row[1] else 'neg'}": int(row[2]) for row in cur.fetchall()}
    return {"set_name": set_name, "inserted": inserted, **by_stratum}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set-name", required=True, help="Unique name for this frozen snapshot.")
    parser.add_argument(
        "--holdout-floor", default=DEFAULT_HOLDOUT_FLOOR,
        help="Labels at/before this timestamp are calibration-tainted (default: %(default)s). "
             "Pass '' to store with no floor.",
    )
    args = parser.parse_args()
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2
    import psycopg

    with psycopg.connect(db_url, autocommit=True, prepare_threshold=None) as conn:
        stats = freeze(conn, set_name=args.set_name, holdout_floor=args.holdout_floor or None)
    LOG.info("GOLDEN SET FROZEN %s", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
