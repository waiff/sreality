"""One-off: relocate already-ingested video rows out of `images` into `listing_videos`.

iDNES embedded video tours were ingested as cover "images" (see migration 195 +
the Layer-1 ingest filter). This moves every video row in `images` (URL path
'/video/' or a video extension) into `listing_videos`, PRESERVING storage_path so
the bytes already downloaded to R2 are KEPT (lossless move, no R2 deletion), then
deletes the `images` row so each listing's cover promotes to its first real photo.

Idempotent and re-runnable. Run AFTER the migration + Layer-1 ingest filter are
DEPLOYED, otherwise the next detail drain re-inserts the video into `images`.

    python -m scripts.backfill_listing_videos          # do the move
    python -m scripts.backfill_listing_videos --dry-run # count only
"""

from __future__ import annotations

import argparse
import logging

from scraper import db

LOG = logging.getLogger("backfill_listing_videos")

_VIDEO_PREDICATE = (
    "lower(split_part(sreality_url, '?', 1)) LIKE '%/video/%' "
    "OR lower(split_part(sreality_url, '?', 1)) ~ "
    r"'\.(mp4|mov|webm|avi|m4v|mkv|m3u8)$'"
)

_MOVE_SQL = f"""
    WITH moved AS (
        INSERT INTO listing_videos (
            sreality_id, listing_id, source_url, sequence, storage_path,
            download_attempts, last_download_attempt_at, unavailable_reason, last_error
        )
        SELECT sreality_id,
               (SELECT id FROM listings WHERE listings.sreality_id = images.sreality_id),
               sreality_url, sequence, storage_path,
               download_attempts, last_download_attempt_at, unavailable_reason, last_error
        FROM images
        WHERE id = ANY(%s)
        ON CONFLICT (sreality_id, sequence) DO UPDATE SET
            storage_path = COALESCE(listing_videos.storage_path, EXCLUDED.storage_path)
    )
    DELETE FROM images WHERE id = ANY(%s)
"""

_CHUNK = 2000


def _chunks(seq: list[int], n: int):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="count only, write nothing")
    args = ap.parse_args()

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT id FROM images WHERE {_VIDEO_PREDICATE}")
            ids = [int(r[0]) for r in cur.fetchall()]
        LOG.info("found %d video rows in images", len(ids))
        if args.dry_run or not ids:
            return

        moved = 0
        for chunk in _chunks(ids, _CHUNK):
            with conn.cursor() as cur:
                cur.execute(_MOVE_SQL, (chunk, chunk))
            conn.commit()
            moved += len(chunk)
            LOG.info("moved=%d / %d", moved, len(ids))
        LOG.info("done moved=%d", moved)


if __name__ == "__main__":
    main()
