"""Re-extract a NON-COLUMN field from ALREADY-STORED page bytes — no re-fetch, no snapshot.

The two things a portal publishes that are not `listings` columns and therefore cannot go
through the one re-parse seam: the `images` child rows, whose identity is POSITIONAL (see
SAFETY below), and the `raw_json.broker` block the broker resolver reads. Everything typed
— every `LISTING_COLUMNS` member, `description` included — is `scripts/reparse.py`'s work
now, over a substrate declared per portal; this file kept only what that seam's write path
(one UPDATE of named columns + a `dirty_properties` enqueue) cannot express.

Rule compliance:
  #2  Neither field is in `_HASH_FIELDS` — media writes only child rows and the broker block
      rides `raw_json` — so the content hash cannot change and ZERO snapshots are appended,
      here or later. `FieldSpec.hashed` declares that per field and the module raises at
      import if a field ever joins `_HASH_FIELDS`, rather than silently downgrading the
      guarantee. (A field that DOES need the deferred snapshot belongs in the seam, which
      carries the `--allow-snapshot-deferral` gate this file no longer needs.)
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
import json
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
from scraper.remax_parser import _broker as _remax_broker
from scraper.scraped_listing import _HASH_FIELDS

LOG = logging.getLogger("reextract")


@dataclass(frozen=True)
class FieldSpec:
    """How one re-extractable field is found, selected and written.

    `hashed` is the load-bearing flag. A field inside `_HASH_FIELDS` cannot be repaired
    snapshot-free (rule #2), so it may not silently inherit this file's guarantee — it
    belongs in `scripts/reparse.py`, which carries the deferral gate.
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
    # `raw_json` is not in _HASH_FIELDS, so this is snapshot-free like media. Attribution
    # itself is NOT done here: the resolver is queue-driven and `ingest_scraped_listing`
    # only enqueues when the content hash changes — which writing raw_json does not. The
    # daily full sweep enumerates resolve_brokers._BROKER_SOURCES, so remax is picked up
    # there (or run resolve_brokers_full.yml to attribute immediately).
    "broker": FieldSpec(
        extractors={"remax": lambda html, _native: _remax_broker(HTMLParser(html))},
        missing_predicate="NOT (l.raw_json ? 'broker')",
        hashed=False,
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

# The staged detail page, one row per listing: `portal_raw_pages` is
# UNIQUE(source, source_id_native, page_kind) and latest-wins, upserted in the same
# transaction that writes the listings row, so it cannot lag the row it produced.
_CLAIM = """
SELECT l.id, l.source_id_native, p.html
FROM listings l
JOIN portal_raw_pages p
  ON p.source = l.source
 AND p.source_id_native = l.source_id_native
 AND p.page_kind = 'detail'
 AND p.html IS NOT NULL
WHERE l.source = %(source)s
  AND l.id > %(cursor)s
  AND (%(since)s::timestamptz IS NULL OR l.first_seen_at >= %(since)s::timestamptz)
  AND {missing}
ORDER BY l.id
LIMIT %(limit)s
"""

_WRITE_BROKER = """
UPDATE listings
SET raw_json = jsonb_set(coalesce(raw_json, '{}'::jsonb), '{broker}', %(value)s::jsonb)
WHERE id = %(id)s
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
                urls_found += len(value)
                if args.dry_run:
                    continue
                if args.field == "media":
                    rows_written += db.record_media(conn, int(listing_id), value)
                    continue
                with conn.cursor() as wcur:
                    wcur.execute(_WRITE_BROKER,
                                 {"value": json.dumps(value), "id": int(listing_id)})
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
