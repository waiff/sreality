"""Backfill images.phash for cross-source dedup (multi-portal PR5).

Selects stored images without a perceptual hash, downloads the bytes from R2,
computes the dHash, and writes images.phash (signed bigint). Idempotent +
resumable: autocommit per row, so hashed images drop out of the next selection
and a timeout/cancel preserves completed work. No-op (exit 0) if R2 env vars are
missing, so a partial deploy never breaks.

Usage:  python -m scripts.compute_image_phash --limit 5000
Required: SUPABASE_DB_URL (+ R2_* for the actual download work).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from scraper import image_storage
from scraper.image_phash import compute_dhash, to_signed64

LOG = logging.getLogger("compute_image_phash")

_SELECT_SQL = """
    SELECT id, storage_path
    FROM images
    WHERE phash IS NULL AND storage_path IS NOT NULL
    ORDER BY id DESC
    LIMIT %(limit)s
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=5000, help="Max images per run.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report the pending count and exit without writing.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2
    if not image_storage.is_configured():
        LOG.info("PHASH skip: R2 env vars missing")
        return 0

    import psycopg

    r2 = image_storage.R2Client.from_env()
    hashed = errors = 0
    with psycopg.connect(db_url, autocommit=True, prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            cur.execute(_SELECT_SQL, {"limit": args.limit})
            rows = cur.fetchall()
        LOG.info("PHASH pending=%d dry_run=%s", len(rows), args.dry_run)
        if args.dry_run:
            return 0

        for image_id, key in rows:
            try:
                data = r2.download_bytes(key)
                value = to_signed64(compute_dhash(data))
            except Exception as exc:  # noqa: BLE001 - one bad image must not kill the run
                errors += 1
                LOG.warning("PHASH id=%s err: %s", image_id, exc)
                continue
            with conn.cursor() as cur:
                cur.execute("UPDATE images SET phash = %s WHERE id = %s", (value, image_id))
            hashed += 1
            if hashed % 200 == 0:
                LOG.info("PHASH progress=%d/%d errors=%d", hashed, len(rows), errors)

    LOG.info("PHASH done hashed=%d errors=%d", hashed, errors)
    return 0


if __name__ == "__main__":
    sys.exit(main())
