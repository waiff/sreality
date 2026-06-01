"""Recompute listings.mf_gross_yield_pct for every sale apartment.

Thin wrapper over the set-based `recompute_mf_gross_yields()` SQL function
(migration 133): resolves each sale apartment's MF reference rent by
point-in-polygon against admin_boundaries, divides annual rent by asking
price, and writes the two derived columns. Cheap + idempotent (only changed
rows are written), so it runs on a schedule AND after each rent-map ingest.

    python -m scripts.recompute_mf_yields
"""

from __future__ import annotations

import logging
import sys

from scraper.db import connect

LOG = logging.getLogger("recompute_mf_yields")


def recompute(conn) -> int:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT recompute_mf_gross_yields()")
        (n,) = cur.fetchone()
    return int(n)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with connect() as conn:
        n = recompute(conn)
    LOG.info("MF yields recomputed: %d rows changed", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
