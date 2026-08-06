"""Fill properties.home_city_id (+ home_city_computed_at).

Thin driver around the SQL function `recompute_home_city(p_full)` (migration
374). The function does a small-anchor spatial join per property to
precompute which curated city (if any) a property's coordinate belongs to,
so listings_with_city_quality / browse_stats_properties / Watchdog's
_city_quality_clauses can all join a plain indexed column instead of each
running a live ST_Covers/ST_DWithin containment test against all curated
cities per row (a live-scale EXPLAIN of the old inline form put the cost in
the billions).

Incremental by default (only properties whose `home_city_computed_at` is
NULL -- i.e. new ones). Pass --full to rebuild every row, which is what you
want after a curated_cities / admin_boundaries change (a city added, its
radius adjusted, or its RUIAN obec link changed). Requires SUPABASE_DB_URL.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import psycopg

LOG = logging.getLogger("recompute_home_city")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="recompute_home_city")
    p.add_argument(
        "--full", action="store_true",
        help="Rebuild every property (default: only rows not yet computed)",
    )
    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        LOG.error("SUPABASE_DB_URL is not set")
        return 2

    # The set-based UPDATE over the full table runs minutes; raise the
    # per-statement timeout for this connection so it isn't cut off.
    with psycopg.connect(dsn, prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            cur.execute("set statement_timeout = '20min'")
            cur.execute("select recompute_home_city(%s)", (args.full,))
            updated = cur.fetchone()[0]
        conn.commit()
    LOG.info("recompute_home_city(full=%s): updated %d properties", args.full, updated)
    return 0


if __name__ == "__main__":
    sys.exit(main())
