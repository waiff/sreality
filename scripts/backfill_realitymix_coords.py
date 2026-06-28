"""One-off backfill: geocode realitymix listings that landed with no coordinates.

~28% of realitymix listings render WITHOUT a #print-map (no data-gps / data-address)
— a source-wide trait of the aggregator — so they land with geom=NULL (no map, no
admin hierarchy). The detail-drain now geocodes such rows going forward, but a row
already in the DB only re-geocodes on its NEXT refetch (a price change), which may be
never for a stable listing. This re-places the existing backlog from STORED data: it
rebuilds each row's geocode locality from its source_url town (+ the slug-recovered
street) — NO page re-fetch, no HTML re-parse — geocodes it via a cached Mapy.cz, and
updates geom + locality IN PLACE when the match is precise enough (skipping a
region/country centroid; a municipality centroid is kept, recovering obec/okres via
the admin-geo trigger).

This deliberately writes NO snapshot (architectural rule #2 governs source-content
changes; geom + locality are OUT of the content hash, so the UPDATE never appends a
snapshot — same posture as the bazos coords backfill).

Idempotent + resumable: every processed row is stamped (`coords.geocode_backfill=true`)
so it drops out of the next selection whether or not it moved; writes commit per row.
`--max-seconds` stops claiming cleanly before the job timeout.

IMPORTANT: run this only AFTER the forward carry-forward fix is deployed — otherwise
the next drain refetch of a backfilled row (its page has no coords) would wipe the
geom again. The drain's connect_drain preload of native_ids_with_geom carries
backfilled coords forward, so a backfilled row is geocoded at most once.

Usage:  python -m scripts.backfill_realitymix_coords --limit 20000
Required: SUPABASE_DB_URL + MAPY_CZ_API_KEY (no MAPY key -> no-op exit 0).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

from scraper import db
from scraper.bazos_main import _build_geocoder
from scraper.geocoding import GeocodingError
from scraper.realitymix_main import _GEOCODE_SKIP_TYPES
from scraper.realitymix_parser import _fallback_locality, _in_cz_bbox

LOG = logging.getLogger("backfill_realitymix_coords")

# Active realitymix rows with no coords we haven't tried to geocode yet. street is
# the slug-recovered street column (the precise-point input); source_url always
# carries the town. Ordered for keyset-stable resumability.
_SELECT_SQL = """
    SELECT l.sreality_id, l.source_url, l.street, l.property_id
    FROM listings l
    WHERE l.source = 'realitymix' AND l.is_active AND l.geom IS NULL
      AND l.raw_json->'coords'->>'geocode_backfill' IS NULL
    ORDER BY l.sreality_id
    LIMIT %(limit)s
"""

_COUNT_SQL = """
    SELECT count(*) FROM listings l
    WHERE l.source = 'realitymix' AND l.is_active AND l.geom IS NULL
      AND l.raw_json->'coords'->>'geocode_backfill' IS NULL
"""

# A row we couldn't place (no locality, geocode miss/too-coarse): stamp only, so it
# drops out of the next run without moving.
_STAMP_SQL = """
    UPDATE listings
    SET raw_json = jsonb_set(
        raw_json, '{coords}',
        coalesce(raw_json->'coords', '{}'::jsonb) || jsonb_build_object('geocode_backfill', true)
    )
    WHERE sreality_id = %(id)s
"""

# A placed row: set geom (the admin-geo trigger re-derives obec/okres/region), set the
# display locality, and record the geocode provenance. geom + locality are out of the
# content hash, so no snapshot is appended.
_UPDATE_SQL = """
    UPDATE listings
    SET geom = ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography,
        locality = %(locality)s,
        raw_json = jsonb_set(raw_json, '{coords}', %(coords)s::jsonb)
    WHERE sreality_id = %(id)s
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=20000,
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

    geocoder = _build_geocoder()  # cached Mapy.cz, or None when no key is set
    if geocoder is None:
        LOG.info("BACKFILL skip: no Mapy.cz API key set; nothing to geocode")
        return 0

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

        placed = stamped = errors = 0
        dirty: list[int] = []
        for i, (sid, url, street, prop_id) in enumerate(rows):
            if args.max_seconds and time.monotonic() - start > args.max_seconds:
                LOG.info("BACKFILL stopping: --max-seconds reached")
                break

            locality = _fallback_locality(url or "", street)
            result = None
            if locality:
                try:
                    result = geocoder(locality)
                except GeocodingError:
                    result = None
                except Exception as exc:  # noqa: BLE001 - a bad query must not abort the run
                    LOG.warning("BACKFILL geocode error id=%d: %s", sid, exc)
                    errors += 1

            placed_ok = (
                result is not None
                and result.matched_type not in _GEOCODE_SKIP_TYPES
                and _in_cz_bbox(result.lat, result.lng)
            )
            with conn.cursor() as cur:
                if placed_ok:
                    coords = {
                        "source": "geocode", "confidence": result.confidence,
                        "matched_type": result.matched_type, "geocode_backfill": True,
                    }
                    cur.execute(_UPDATE_SQL, {
                        "id": sid, "lat": result.lat, "lon": result.lng,
                        "locality": locality, "coords": json.dumps(coords),
                    })
                    placed += 1
                    if prop_id is not None:
                        dirty.append(prop_id)
                else:
                    cur.execute(_STAMP_SQL, {"id": sid})
                    stamped += 1

            if len(dirty) >= 500:
                db.mark_properties_dirty(conn, dirty)
                dirty = []
            if (i + 1) % 200 == 0:
                LOG.info("BACKFILL progress=%d/%d placed=%d unplaced=%d errors=%d",
                         i + 1, len(rows), placed, stamped, errors)

        if dirty:
            db.mark_properties_dirty(conn, dirty)

    LOG.info("BACKFILL done processed=%d placed=%d unplaced=%d errors=%d",
             placed + stamped, placed, stamped, errors)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
