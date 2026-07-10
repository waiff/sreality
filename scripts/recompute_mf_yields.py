"""Recompute listings.mf_gross_yield_pct for every sale apartment.

Thin wrapper over the set-based `recompute_mf_gross_yields()` SQL function
(migrations 133/134/222): joins each sale apartment's precomputed MF rent-map
territory (listings.ku_id / obec_id, filled at write time by the admin-geo
trigger -- NO per-run point-in-polygon since migration 222), divides annual
reference rent by asking price, and writes the derived columns. Idempotent
(only changed rows are written via an `is distinct from` guard), so it runs on
a schedule AND after each rent-map ingest.

    python -m scripts.recompute_mf_yields
"""

from __future__ import annotations

import logging
import sys

from psycopg import errors

from scraper.db import connect

LOG = logging.getLogger("recompute_mf_yields")


def recompute(conn) -> int:
    with conn.transaction(), conn.cursor() as cur:
        # Belt-and-suspenders: the rewrite (migration 222) makes this a bounded
        # arithmetic pass, but a one-off plan regression must never let the
        # pooler's ~120s default cut it off mid-UPDATE (the city-proximity job
        # sets the same guard).
        cur.execute("SET LOCAL statement_timeout = '10min'")
        cur.execute("SELECT recompute_mf_gross_yields()")
        (n,) = cur.fetchone()
    return int(n)


def recompute_with_retry(conn) -> int:
    """One retry on deadlock: the bulk listings UPDATE can lose a lock-order race
    against a concurrent bulk writer (first seen 2026-07-10, vs the every-2-min
    worker maintenance lane). The victim's transaction is rolled back and the
    function is idempotent (is-distinct-from write guard), so a single rerun on a
    settled snapshot is safe and almost always clean."""
    try:
        return recompute(conn)
    except errors.DeadlockDetected:
        LOG.warning("MF yields recompute deadlocked (concurrent bulk writer); retrying once")
        return recompute(conn)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with connect() as conn:
        n = recompute_with_retry(conn)
    LOG.info("MF yields recomputed: %d rows changed", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
