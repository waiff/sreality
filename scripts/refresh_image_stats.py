"""Refresh the image-stat matviews behind the Health dashboard tiles.

image_storage_overview_mv backs the "Image mirror" tile (migration 115);
images_failure_overview_mv backs the "Image failures" card (migration 177).
Both are precomputed per-rollup so the browser never scans the full images
table. Run after the image drain in images.yml; safe to run manually.
CONCURRENTLY so anon readers never block.
"""
from __future__ import annotations

import logging
import os
import sys

import psycopg

from scraper import db

LOG = logging.getLogger("refresh_image_stats")

_MVS = ("image_storage_overview_mv", "images_failure_overview_mv")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    # db.connect adds the shared handshake-retry + TCP keepalives. Each matview
    # goes through the ONE chokepoint (migration 578): CONCURRENTLY with a
    # first-populate fallback, in-flight-refresh skip, stamped in-DB with rows +
    # duration — the try/except ladder this loop used to carry lives there now.
    with db.connect(db_url) as conn:
        for mv in _MVS:
            try:
                rows = db.refresh_matview(conn, mv)
            except psycopg.errors.UndefinedTable:
                # Deploy/migration race: the script can run before a newly
                # added matview's migration is applied. Skip, don't fail the
                # whole job — the next 2-hourly run picks it up. (An mv that
                # EXISTS but has no registry row raises instead: that is a
                # misconfiguration, not a race.)
                LOG.warning("REFRESH skipped mv=%s (does not exist yet)", mv)
                continue
            LOG.info("REFRESH done mv=%s rows=%d", mv, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
