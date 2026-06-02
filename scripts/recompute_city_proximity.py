"""Fill properties.home_obec_pop + near_{pop,jobs,youth,overall}_{5,15}km.

Thin driver around the SQL function `recompute_city_proximity(p_full)`
(migration 142). The function does a small-anchor spatial join per property to
precompute the city-proximity columns Browse / Watchdog filter on, so those
filters are plain indexed-column predicates instead of a per-request spatial
RPC (no anon 3s statement-timeout).

Incremental by default (only properties whose `city_proximity_computed_at` is
NULL — i.e. new ones). Pass --full to rebuild every row, which is what you want
after a population load (scripts/load_obec_population) or a city-index upload,
since those shift the precomputed maxes. Requires SUPABASE_DB_URL.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import psycopg

LOG = logging.getLogger("recompute_city_proximity")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="recompute_city_proximity")
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

    # The set-based UPDATE over ~150k properties runs minutes; raise the
    # per-statement timeout for this connection so it isn't cut off.
    with psycopg.connect(dsn, prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            cur.execute("set statement_timeout = '20min'")
            cur.execute("select recompute_city_proximity(%s)", (args.full,))
            updated = cur.fetchone()[0]
        conn.commit()
    LOG.info("recompute_city_proximity(full=%s): updated %d properties", args.full, updated)
    return 0


if __name__ == "__main__":
    sys.exit(main())
