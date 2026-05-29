"""Refresh image_storage_overview_mv, the matview behind the Health "Image mirror" tile.

The tile's RPC reads this precomputed per-category rollup instead of scanning the
full images table on every browser load (migration 115). Run after the image drain
in images.yml; safe to run manually. CONCURRENTLY so anon readers never block.
"""
from __future__ import annotations

import logging
import os
import sys

import psycopg

LOG = logging.getLogger("refresh_image_stats")

_MV = "image_storage_overview_mv"


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    # autocommit: REFRESH ... CONCURRENTLY cannot run inside a transaction block.
    with psycopg.connect(db_url, autocommit=True, prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            cur.execute(f"refresh materialized view concurrently {_MV}")
    LOG.info("REFRESH done mv=%s", _MV)
    return 0


if __name__ == "__main__":
    sys.exit(main())
