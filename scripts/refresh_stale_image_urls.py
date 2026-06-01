"""Re-open active listings whose images aren't in R2 yet for a detail re-fetch.

Portals rotate image CDN URLs over time. A listing whose photos we never downloaded
before its URLs rotated is stuck: the stored URL 404s, the frontend fallback can't load
it, and the image downloader can't fetch it either. This sweep re-enqueues such listings
into the source-generic `listing_detail_queue`; the detail drain then re-fetches them and
`db.record_images` repoints each not-yet-stored image's URL to the current one (it also
resets download_attempts and clears unavailable_reason for storage_path-NULL rows), after
which the image backfill (images.yml) can store the bytes.

Targeted + bounded: only ACTIVE listings with at least one not-yet-stored image that are
either older than --min-age (the stranded backlog the newest-first backfill never reached)
or have a confirmed-stale image (a download already 404'd → unavailable_reason
='source_unavailable'). `listings.images_refreshed_at` is a per-listing cooldown so a
genuinely-removed photo doesn't loop. Source-generic (sreality + crawler portals).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import defaultdict

from scraper import db

LOG = logging.getLogger("refresh_stale_image_urls")

_CANDIDATES_SQL = """
    SELECT l.sreality_id, l.source, l.source_id_native, l.source_url
    FROM listings l
    WHERE l.is_active
      AND (l.source = 'sreality' OR l.source_url IS NOT NULL)
      AND (l.images_refreshed_at IS NULL
           OR l.images_refreshed_at < now() - %(cooldown)s::interval)
      AND EXISTS (
            SELECT 1 FROM images i
            WHERE i.sreality_id = l.sreality_id AND i.storage_path IS NULL)
      AND (l.first_seen_at < now() - %(min_age)s::interval
           OR EXISTS (
            SELECT 1 FROM images i
            WHERE i.sreality_id = l.sreality_id
              AND i.storage_path IS NULL
              AND i.unavailable_reason = 'source_unavailable'))
    ORDER BY l.first_seen_at ASC
    LIMIT %(limit)s
"""


def enqueue_entry(
    sreality_id: int, source: str, source_id_native: str | None, source_url: str | None,
) -> tuple[str, str | None, int | None, int]:
    """One `db.enqueue_detail` entry: (native_id, detail_ref, index_price, priority).

    sreality derives its detail URL from the id (detail_ref=None); crawler portals fetch
    by URL, so detail_ref is the stored source_url. Lowest priority so the refresh never
    delays genuine new-listing detail fetches.
    """
    if source == "sreality":
        return (str(sreality_id), None, None, db.QUEUE_PRIORITY_NEW)
    return (str(source_id_native), source_url, None, db.QUEUE_PRIORITY_NEW)


def main() -> int:
    p = argparse.ArgumentParser(description="Re-enqueue stale-image active listings for a detail re-fetch.")
    p.add_argument("--limit", type=int, default=5000, help="max listings to re-enqueue this run")
    p.add_argument("--cooldown", default="14 days",
                   help="skip listings re-enqueued within this interval (per-listing cooldown)")
    p.add_argument("--min-age", default="3 days",
                   help="only sweep listings older than this, unless they have a "
                        "confirmed-stale (source_unavailable) image")
    p.add_argument("--dry-run", action="store_true", help="report counts and exit, no writes")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(_CANDIDATES_SQL,
                        {"cooldown": args.cooldown, "min_age": args.min_age, "limit": args.limit})
            rows = cur.fetchall()
        LOG.info("REFRESH candidates=%d limit=%d min_age=%s cooldown=%s",
                 len(rows), args.limit, args.min_age, args.cooldown)
        if not rows:
            return 0

        by_source: dict[str, list[tuple[str, str | None, int | None, int]]] = defaultdict(list)
        sids: list[int] = []
        for sid, source, native, url in rows:
            sids.append(sid)
            by_source[source].append(enqueue_entry(sid, source, native, url))

        if args.dry_run:
            LOG.info("DRY-RUN would re-enqueue by source: %s",
                     {s: len(e) for s, e in by_source.items()})
            return 0

        enqueued = 0
        for source, entries in by_source.items():
            enqueued += db.enqueue_detail(conn, source, entries)
        # Stamp the cooldown marker on every candidate (incl. any whose queue row was
        # already claimed, so we don't keep re-selecting them next sweep).
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE listings SET images_refreshed_at = now() WHERE sreality_id = ANY(%s)",
                (sids,),
            )
        conn.commit()
        LOG.info("REFRESH done enqueued=%d listings=%d by_source=%s",
                 enqueued, len(sids), {s: len(e) for s, e in by_source.items()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
