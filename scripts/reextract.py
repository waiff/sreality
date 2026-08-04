"""Re-extract a field from ALREADY-STORED page bytes — no re-fetch, no snapshot churn.

The generic form of the ~20 one-off `backfill_*.py` scripts: when a parser silently
stopped extracting something, the pages we parsed are still in `portal_raw_pages`, so the
fix is to replay the CURRENT parser over stored bytes rather than re-crawl the portal.
That also repairs INACTIVE listings, which a re-fetch structurally cannot.

Rule compliance, by construction:
  #2  Only the field's own child rows are written — never a `listings` content column, so
      the content hash cannot change and ZERO `listing_snapshots` rows are appended.
  #3  Never touches `is_active` / `mark_inactive` / any index walk.
  #4  Not a sighting: `last_seen_at` is untouched.

SAFETY — why this only repairs ZERO-row listings. `record_images` upserts on
`(listing_id, sequence)` where sequence is the URL's position in the parsed gallery, and
refreshes the URL only `WHERE storage_path IS NULL`. If a listing already holds photos and
a re-parse yields MORE of them (idnes, where the fix recovers first-party anchors
interleaved in document order), every subsequent photo shifts position: downloaded rows
keep their old URL at a sequence the new parse means for a different photo, while
not-yet-downloaded rows get repointed. The gallery would silently reorder. So partial-loss
recovery is deliberately OUT of scope here and waits for the media contract (which gives a
stable media identity instead of a positional one). Listings with zero image rows have
nothing to collide with, so they are safe and are all this script will touch.

Keyset-paginated over `listings.id`, autocommit per listing, `--max-seconds` bounded — a
timeout or SIGKILL just resumes from the cursor on the next run.

Required env: SUPABASE_DB_URL.

    python scripts/reextract.py --source realitymix --field media --dry-run
    python scripts/reextract.py --source idnes --field media --since 2026-05-29
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Callable

from selectolax.parser import HTMLParser

from scraper import db
from scraper.idnes_parser import _gallery_urls as _idnes_gallery
from scraper.realitymix_parser import _images as _realitymix_images

LOG = logging.getLogger("reextract")

# source -> (html, source_id_native) -> ordered media URLs.
#
# Only portals whose media extraction is a NAMED function are wired. ceskereality, bazos,
# remax, maxima and mmreality build `image_urls` inline inside `parse_detail`, and lifting
# that out per portal here would be exactly the per-portal special-casing rule #21 forbids.
# Once every parser returns a `MediaExtraction`, this registry collapses into one lookup.
_MEDIA_EXTRACTORS: dict[str, Callable[[str, str], list[str]]] = {
    "realitymix": lambda html, native: _realitymix_images(html, native),
    "idnes": lambda html, _native: _idnes_gallery(HTMLParser(html)),
}

_FIELDS = ("media",)

# Newest staged detail page per candidate listing. A listing can have several staged
# pages (one per re-fetch); replaying an older one would resurrect URLs the portal has
# since rotated, so DISTINCT-ON the newest by fetched_at.
_CLAIM = """
SELECT l.id, l.source_id_native, p.html
FROM listings l
JOIN LATERAL (
    SELECT p.html
    FROM portal_raw_pages p
    WHERE p.source = l.source
      AND p.source_id_native = l.source_id_native
      AND p.page_kind = 'detail'
      AND p.html IS NOT NULL
    ORDER BY p.fetched_at DESC
    LIMIT 1
) p ON true
WHERE l.source = %(source)s
  AND l.id > %(cursor)s
  AND (%(since)s::timestamptz IS NULL OR l.first_seen_at >= %(since)s::timestamptz)
  AND NOT EXISTS (SELECT 1 FROM images i WHERE i.listing_id = l.id)
ORDER BY l.id
LIMIT %(limit)s
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, choices=sorted(_MEDIA_EXTRACTORS))
    parser.add_argument("--field", default="media", choices=_FIELDS)
    parser.add_argument("--since", default=None, help="only listings first seen on/after")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--limit", type=int, default=0, help="0 = no cap")
    parser.add_argument("--max-seconds", type=int, default=3000)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    extract = _MEDIA_EXTRACTORS[args.source]
    started = time.monotonic()
    deadline = started + args.max_seconds if args.max_seconds else None
    cursor = 0
    seen = recovered = urls_found = rows_written = still_empty = 0

    with db.connect() as conn:
        while True:
            if args.limit and seen >= args.limit:
                LOG.info("REEXTRACT limit reached at cursor=%d", cursor)
                break
            batch_size = min(args.batch_size, args.limit - seen) if args.limit else args.batch_size
            with conn.cursor() as cur:
                cur.execute(
                    _CLAIM,
                    {
                        "source": args.source,
                        "cursor": cursor,
                        "since": args.since,
                        "limit": batch_size,
                    },
                )
                batch = cur.fetchall()
            if not batch:
                break
            cursor = int(batch[-1][0])
            seen += len(batch)

            for listing_id, native, html in batch:
                urls = extract(html or "", native or "")
                if not urls:
                    still_empty += 1
                    continue
                recovered += 1
                urls_found += len(urls)
                if not args.dry_run:
                    rows_written += db.record_media(conn, int(listing_id), urls)

            LOG.info(
                "REEXTRACT progress seen=%d recovered=%d urls=%d written=%d empty=%d cursor=%d",
                seen, recovered, urls_found, rows_written, still_empty, cursor,
            )
            if deadline and time.monotonic() > deadline:
                LOG.warning("REEXTRACT time budget reached at cursor=%d; resume next run", cursor)
                break

    LOG.info(
        "REEXTRACT done source=%s field=%s seen=%d recovered=%d urls=%d written=%d "
        "still_empty=%d elapsed=%.1fs%s",
        args.source, args.field, seen, recovered, urls_found, rows_written,
        still_empty, time.monotonic() - started, " (dry-run)" if args.dry_run else "",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
