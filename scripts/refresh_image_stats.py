"""Refresh image_storage_overview_mv, the matview behind the Health "Image mirror" tile.

The tile's RPC reads this precomputed per-category rollup instead of scanning the
full images table on every browser load (migration 115). Run after the image drain
in images.yml; safe to run manually. CONCURRENTLY so anon readers never block.
"""
from __future__ import annotations

import logging
import os
import sys
import time

import psycopg

LOG = logging.getLogger("refresh_image_stats")

_MV = "image_storage_overview_mv"

_CONNECT_ATTEMPTS = 3
_CONNECT_RETRY_SLEEP_S = 10.0


def _connect(db_url: str) -> psycopg.Connection:
    # The pooler occasionally times out the handshake; one retry round-trip is
    # all it takes, so a small bounded retry beats failing the whole job.
    for attempt in range(1, _CONNECT_ATTEMPTS + 1):
        try:
            # autocommit: REFRESH ... CONCURRENTLY cannot run inside a
            # transaction block.
            return psycopg.connect(db_url, autocommit=True, prepare_threshold=None)
        except psycopg.OperationalError as exc:
            if attempt == _CONNECT_ATTEMPTS:
                raise
            LOG.warning(
                "CONNECT attempt=%d/%d failed: %s — retrying in %.0fs",
                attempt, _CONNECT_ATTEMPTS, exc, _CONNECT_RETRY_SLEEP_S,
            )
            time.sleep(_CONNECT_RETRY_SLEEP_S)
    raise AssertionError("unreachable")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    with _connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(f"refresh materialized view concurrently {_MV}")
    LOG.info("REFRESH done mv=%s", _MV)
    return 0


if __name__ == "__main__":
    sys.exit(main())
