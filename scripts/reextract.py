"""Re-extract a field from ALREADY-STORED page bytes — no re-fetch, no snapshot churn.

The generic form of the ~20 one-off `backfill_*.py` scripts: when a parser silently
stopped extracting something, the pages we parsed are still in `portal_raw_pages`, so the
fix is to replay the CURRENT parser over stored bytes rather than re-crawl the portal.
That also repairs INACTIVE listings, which a re-fetch structurally cannot.

Rule compliance:
  #2  Depends on the field, and the registry says which. An UNHASHED field (`media`) writes
      only child rows, so the content hash cannot change and ZERO snapshots are appended.
      A HASHED field (`description`, in `_HASH_FIELDS`) cannot be repaired snapshot-free:
      setting it genuinely changes the hash, so ONE snapshot per listing is appended — not
      here, but on that listing's next natural detail scrape, when the recomputed hash
      differs from the latest snapshot's. Deferred, never skipped, and spread over the
      normal cadence rather than concentrated. `--allow-snapshot-deferral` is required so
      that is an explicit choice; a mismatch between `FieldSpec.hashed` and `_HASH_FIELDS`
      raises at import.
  #3  Never touches `is_active` / `mark_inactive` / any index walk.
  #4  Not a sighting: `last_seen_at` is untouched.

Hashed fields are written with a targeted single-column UPDATE, NOT by replaying a whole
`ScrapedListing` through the scrape write path — that would rewrite every other column from
a possibly-stale stored page and could regress a price the portal has since changed.

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

from dataclasses import dataclass

from scraper import db
from scraper.idnes_parser import _gallery_urls as _idnes_gallery
from scraper.realitymix_parser import _images as _realitymix_images
from scraper.remax_parser import _description as _remax_description
from scraper.scraped_listing import _HASH_FIELDS

LOG = logging.getLogger("reextract")


@dataclass(frozen=True)
class FieldSpec:
    """How one re-extractable field is found, selected and written.

    `hashed` is the load-bearing flag. A field inside `_HASH_FIELDS` cannot be repaired
    snapshot-free (rule #2), so it may not silently inherit the media path's guarantee —
    see `--allow-snapshot-deferral`.
    """

    extractors: dict[str, Callable[[str, str], Any]]
    missing_predicate: str  # SQL fragment selecting listings that still lack the field
    hashed: bool

    def sources(self) -> list[str]:
        return sorted(self.extractors)


_FIELDS: dict[str, FieldSpec] = {
    # Only portals whose extraction is a NAMED function are wired. ceskereality, bazos,
    # maxima and mmreality build `image_urls` inline inside `parse_detail`, and lifting
    # that out per portal here would be exactly the special-casing rule #21 forbids.
    # Once every parser returns a `MediaExtraction`, these collapse into one lookup.
    "media": FieldSpec(
        extractors={
            "realitymix": lambda html, native: _realitymix_images(html, native),
            "idnes": lambda html, _native: _idnes_gallery(HTMLParser(html)),
        },
        missing_predicate="NOT EXISTS (SELECT 1 FROM images i WHERE i.listing_id = l.id)",
        hashed=False,
    ),
    "description": FieldSpec(
        extractors={"remax": lambda html, _native: _remax_description(HTMLParser(html))},
        missing_predicate="l.description IS NULL",
        hashed=True,
    ),
}

# Guard against the registry drifting out of sync with the hash contract: if a field is
# added to _HASH_FIELDS later, `hashed=False` here would silently start lying.
for _name, _spec in _FIELDS.items():
    _expected = _name in _HASH_FIELDS
    if _spec.hashed != _expected:
        raise RuntimeError(
            f"reextract field {_name!r}: hashed={_spec.hashed} but _HASH_FIELDS says {_expected}"
        )

# Newest staged detail page per candidate listing. A listing can have several staged
# pages (one per re-fetch); replaying an older one would resurrect content the portal has
# since changed, so take the newest by fetched_at.
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
  AND {missing}
ORDER BY l.id
LIMIT %(limit)s
"""

# Direct column write, deliberately NOT the scrape write path: replaying a whole
# ScrapedListing would rewrite every other column from a possibly-stale stored page
# (regressing a price that has since changed). One column, one statement.
_WRITE_DESCRIPTION = """
UPDATE listings SET description = %(value)s WHERE id = %(id)s AND description IS NULL
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--field", default="media", choices=sorted(_FIELDS))
    parser.add_argument("--since", default=None, help="only listings first seen on/after")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--limit", type=int, default=0, help="0 = no cap")
    parser.add_argument("--max-seconds", type=int, default=3000)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--allow-snapshot-deferral",
        action="store_true",
        help="required for a field in _HASH_FIELDS: acknowledges that setting it changes "
        "the content hash, so one snapshot per listing is appended on the next natural "
        "detail scrape (deferred, never skipped) — see the module docstring",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    spec = _FIELDS[args.field]
    if args.source not in spec.extractors:
        print(
            f"ERROR: --field {args.field} is not wired for --source {args.source} "
            f"(available: {', '.join(spec.sources())})",
            file=sys.stderr,
        )
        return 2
    if spec.hashed and not args.allow_snapshot_deferral and not args.dry_run:
        print(
            f"ERROR: {args.field!r} is in _HASH_FIELDS, so writing it changes the content "
            "hash and defers one snapshot per listing to its next detail scrape. Re-run "
            "with --allow-snapshot-deferral to acknowledge.",
            file=sys.stderr,
        )
        return 2

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    extract = spec.extractors[args.source]
    claim_sql = _CLAIM.format(missing=spec.missing_predicate)
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
                    claim_sql,
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
                value = extract(html or "", native or "")
                if not value:
                    still_empty += 1
                    continue
                recovered += 1
                if args.field == "media":
                    urls_found += len(value)
                    if not args.dry_run:
                        rows_written += db.record_media(conn, int(listing_id), value)
                else:
                    urls_found += len(value)
                    if not args.dry_run:
                        with conn.cursor() as wcur:
                            wcur.execute(
                                _WRITE_DESCRIPTION, {"value": value, "id": int(listing_id)}
                            )
                            rows_written += wcur.rowcount or 0

            LOG.info(
                "REEXTRACT progress seen=%d recovered=%d size=%d written=%d empty=%d cursor=%d",
                seen, recovered, urls_found, rows_written, still_empty, cursor,
            )
            if deadline and time.monotonic() > deadline:
                LOG.warning("REEXTRACT time budget reached at cursor=%d; resume next run", cursor)
                break

    LOG.info(
        "REEXTRACT done source=%s field=%s seen=%d recovered=%d size=%d written=%d "
        "still_empty=%d elapsed=%.1fs%s",
        args.source, args.field, seen, recovered, urls_found, rows_written,
        still_empty, time.monotonic() - started, " (dry-run)" if args.dry_run else "",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
