"""One-off backfill: persist street + locality on existing bazos listings.

Until ScrapedListing grew a `street` field, the bazos parser extracted the
street during geocoding but stored it only inside the raw_json coords
provenance — `listings.street` stayed NULL for every bazos row, making bazos
invisible to the street+disposition dedup engine. The PR #406 locality fix was
likewise never backfilled, so most active bazos rows still carry a NULL
`locality`. This re-parses each affected listing from its already-staged detail
HTML (`portal_raw_pages` — NO re-fetch of bazos) with the current parser and
UPDATEs `locality` / `street` (+ `district`, should the parser ever yield one)
in place.

No geocoding spend: the parser runs with `geocoder=None` (the no-key degrade
path), because we only want the text fields — the existing `geom` is left
untouched, so the Mapy.cz path never fires (unlike scripts.backfill_bazos_coords,
which exists precisely to re-geocode).

This deliberately writes NO snapshot (architectural rule #2 governs
source-content changes; surfacing fields we already extracted from the SAME
listing state is a data-quality fix, not a new real-world state — same posture
as the coords backfill).

Idempotent + resumable: every processed row is stamped
(`raw_json.street_locality_backfill = true`), so it drops out of the next
selection whether or not it improved; writes commit per row (autocommit). A
timeout/cancel preserves completed work; `--max-seconds` stops claiming cleanly
before the job timeout. Rerun until "pending=0".

Usage:  python -m scripts.backfill_bazos_street_locality --limit 40000
Required: SUPABASE_DB_URL.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Any

from scraper import db
from scraper.bazos_parser import parse_detail
from scraper.scraped_listing import ScrapedListing

LOG = logging.getLogger("backfill_bazos_street_locality")

# Active bazos listings missing locality or street that we haven't processed
# yet AND that still have a staged detail page. Metadata only — the (large)
# HTML is fetched per row in `_FETCH_HTML_SQL` so memory stays flat over ~30k
# rows.
_SELECT_SQL = """
    SELECT l.sreality_id, l.source_id_native, l.source_url,
           l.category_main, l.category_type, l.property_id,
           l.locality, l.street, l.district
    FROM listings l
    WHERE l.source = 'bazos' AND l.is_active
      AND (l.locality IS NULL OR l.street IS NULL)
      AND l.raw_json->>'street_locality_backfill' IS NULL
      AND EXISTS (
          SELECT 1 FROM portal_raw_pages pr
          WHERE pr.source = 'bazos' AND pr.source_id_native = l.source_id_native
            AND pr.page_kind = 'detail'
      )
    ORDER BY l.sreality_id
    LIMIT %(limit)s
"""

_FETCH_HTML_SQL = """
    SELECT html FROM portal_raw_pages
    WHERE source = 'bazos' AND source_id_native = %(native)s AND page_kind = 'detail'
    ORDER BY fetched_at DESC NULLS LAST
    LIMIT 1
"""

_COUNT_SQL = """
    SELECT count(*) FROM listings l
    WHERE l.source = 'bazos' AND l.is_active
      AND (l.locality IS NULL OR l.street IS NULL)
      AND l.raw_json->>'street_locality_backfill' IS NULL
      AND EXISTS (
          SELECT 1 FROM portal_raw_pages pr
          WHERE pr.source = 'bazos' AND pr.source_id_native = l.source_id_native
            AND pr.page_kind = 'detail'
      )
"""

# One statement per row: fill the text fields (COALESCE never NULLs out an
# existing value) and stamp the marker so the row leaves the selection. Rows
# where the parse yielded nothing still get the stamp (all params None — the
# COALESCEs are no-ops). geom is untouched, so the admin-geo trigger does not
# re-derive anything and no snapshot is written.
_UPDATE_SQL = """
    UPDATE listings
    SET locality = COALESCE(%(locality)s, locality),
        street = COALESCE(%(street)s, street),
        district = COALESCE(%(district)s, district),
        raw_json = raw_json || '{"street_locality_backfill": true}'::jsonb
    WHERE sreality_id = %(id)s
"""


def plan_update(
    listing: ScrapedListing,
    *,
    current_locality: str | None,
    current_street: str | None,
    current_district: str | None,
) -> tuple[dict[str, Any], bool]:
    """The pure per-row decision: (UPDATE params, whether anything new lands).

    A parsed value is offered only when present (COALESCE keeps the existing
    value otherwise); `improved` is True only when a parsed value fills a
    currently-NULL column — that drives the updated/skipped counters and the
    dirty-property enqueue.
    """
    params: dict[str, Any] = {
        "locality": listing.locality,
        "street": listing.street,
        "district": listing.district,
    }
    improved = (
        (listing.locality is not None and current_locality is None)
        or (listing.street is not None and current_street is None)
        or (listing.district is not None and current_district is None)
    )
    return params, improved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=40000,
                        help="Max listings processed this run.")
    parser.add_argument("--max-seconds", type=float, default=None,
                        help="Wall-clock budget; stop claiming and exit cleanly.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report the pending count and exit without writing.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    start = time.monotonic()
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(_COUNT_SQL)
            pending = int(cur.fetchone()[0])
        LOG.info("BACKFILL pending=%d limit=%d", pending, args.limit)
        if args.dry_run:
            return 0

        with conn.cursor() as cur:
            cur.execute(_SELECT_SQL, {"limit": args.limit})
            rows = cur.fetchall()

        updated = skipped = errors = 0
        dirty: list[int] = []
        for i, (sid, native, url, cmain, ctype, prop_id,
                cur_locality, cur_street, cur_district) in enumerate(rows):
            if args.max_seconds and time.monotonic() - start > args.max_seconds:
                LOG.info("BACKFILL stopping: --max-seconds reached")
                break
            with conn.cursor() as cur:
                cur.execute(_FETCH_HTML_SQL, {"native": native})
                html_row = cur.fetchone()
            if html_row is None:
                with conn.cursor() as cur:
                    cur.execute(_UPDATE_SQL, {
                        "id": sid, "locality": None, "street": None, "district": None,
                    })
                skipped += 1
                continue
            html = html_row[0]
            try:
                # geocoder=None: text fields only, zero Mapy.cz spend.
                listing = parse_detail(
                    html, source_url=url,
                    category_main=cmain, category_type=ctype, geocoder=None,
                )
            except Exception as exc:  # noqa: BLE001 - a bad staged page must not abort the run
                LOG.warning("BACKFILL parse error id=%d: %s", sid, exc)
                with conn.cursor() as cur:
                    cur.execute(_UPDATE_SQL, {
                        "id": sid, "locality": None, "street": None, "district": None,
                    })
                errors += 1
                continue

            params, improved = plan_update(
                listing, current_locality=cur_locality,
                current_street=cur_street, current_district=cur_district,
            )
            with conn.cursor() as cur:
                cur.execute(_UPDATE_SQL, {"id": sid, **params})
            if improved:
                updated += 1
                if prop_id is not None:
                    dirty.append(prop_id)
            else:
                skipped += 1

            if dirty and len(dirty) >= 500:
                db.mark_properties_dirty(conn, dirty)
                dirty = []
            if (i + 1) % 200 == 0:
                LOG.info("BACKFILL progress=%d/%d updated=%d skipped=%d errors=%d",
                         i + 1, len(rows), updated, skipped, errors)

        if dirty:
            db.mark_properties_dirty(conn, dirty)

        with conn.cursor() as cur:
            cur.execute(_COUNT_SQL)
            remaining = int(cur.fetchone()[0])

    LOG.info("BACKFILL done processed=%d updated=%d skipped=%d errors=%d pending=%d",
             updated + skipped + errors, updated, skipped, errors, remaining)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
