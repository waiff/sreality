"""Refresh the Browse map's read-optimized materialized view.

`properties_map_mv` (migration 254) is the cold-robust source for the Browse map
feed: a clean, read-only copy of properties_public's map columns that stays
all-visible and cached, so the up-to-50k-point scan never trips the anon 3s
statement_timeout the way the churned live `properties` table did. It only
reflects new state when refreshed, so a scheduled REFRESH keeps the map fresh.

CONCURRENTLY (allowed because of the unique index on property_id) means anon map
reads see the old snapshot during the refresh instead of being blocked. The
follow-up VACUUM reclaims the diff's dead tuples and re-sets the visibility map.
Both must run OUTSIDE a transaction block, hence autocommit.

    python -m scripts.refresh_map_mv

Required env: SUPABASE_DB_URL (same as every other job).
"""

from __future__ import annotations

import logging

from scraper.db import connect

LOG = logging.getLogger("refresh_map_mv")


def refresh(conn) -> None:
    conn.autocommit = True
    with conn.cursor() as cur:
        # A refresh of ~333k rows is seconds, but never let the pooler's ~120s
        # default cut it off mid-rebuild.
        cur.execute("SET statement_timeout = '10min'")
        cur.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY properties_map_mv")
        cur.execute("VACUUM (ANALYZE) properties_map_mv")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with connect() as conn:
        refresh(conn)
    LOG.info("properties_map_mv refreshed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
