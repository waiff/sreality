"""Backfill: geocode the standing no-coords stock of any portal from STORED locality.

The 2026-07 location audit found the drain-path geocode fallback only ever fires
INSIDE a detail refetch, so rows scraped before a portal had a fallback (or that
simply never refetch) sit with geom=NULL forever — 100% of idnes/maxima/realitymix
no-geom rows showed no attempt at all. This script works that stock down from
already-stored data: it geocodes each row's stored `locality` text via the
persistent geocode_cache (migration 288 — one Mapy credit per distinct query,
negative results cached too) and stamps EVERY processed row's
`listings.geocode_attempted_at` (placed, miss, or too-coarse), so the candidate
pool self-empties and re-runs are cheap. The mig-263 lesson applied: the stamp is
a COLUMN a refetch can't destroy, not a raw_json marker.

Placed rows get geom only — the admin-geo trigger derives obec/okres/region/ku_id
and the geo_cell_key trigger re-keys dedup blocking; property maintenance is
notified via mark_properties_dirty. locality / raw_json are NOT touched, so the
next drain reparse computes the same content hash from the same raw payload (no
snapshot churn; rule #2 governs source-content changes and none happen here).

Safe to run only because this PR also ships the universal carry-forward
(scraper.location.CoordResolver on every portal drain) + the geom
preserve-if-null upsert rail — without those, the next coords-less refetch of a
backfilled row would wipe the geom again (the pre-fix oscillation).

bazos is deliberately NOT a default source: its locality is zip+town at best and
its coords resolution needs the link-corroborating tree in
scripts/backfill_bazos_coords.py (town-pin risk).

Usage:  python -m scripts.backfill_geocode_coords --source idnes --limit 20000
Required: SUPABASE_DB_URL + MAPY_CZ_API_KEY (no Mapy key -> no-op exit 0).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from scraper import db
from scraper.location import build_geocoder, geocode_cached

LOG = logging.getLogger("backfill_geocode_coords")

DEFAULT_SOURCES = (
    "idnes", "realitymix", "maxima", "remax", "mmreality", "ceskereality",
)

# Candidate pool: active rows with usable locality text, no coords, never
# attempted (the migration-288 partial index makes this a seek). Active-only —
# an inactive row can't be refetched and gains nothing user-facing from a pin;
# flip to inactive coverage later if the inactive-geo dedup pass (P2) lands.
_SELECT_SQL = """
    SELECT l.id, l.locality, l.property_id
    FROM listings l
    WHERE l.source = ANY(%(sources)s) AND l.is_active
      AND l.geom IS NULL AND l.locality IS NOT NULL AND l.locality <> ''
      AND l.geocode_attempted_at IS NULL
    ORDER BY l.id
    LIMIT %(limit)s
"""

_COUNT_SQL = """
    SELECT count(*) FROM listings l
    WHERE l.source = ANY(%(sources)s) AND l.is_active
      AND l.geom IS NULL AND l.locality IS NOT NULL AND l.locality <> ''
      AND l.geocode_attempted_at IS NULL
"""

# Placed: geom only. The admin-geo trigger fills the hierarchy; the geo_cell_key
# trigger re-keys; raw SQL bypasses upsert_listing so no snapshot is written.
_PLACE_SQL = """
    UPDATE listings
    SET geom = ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography,
        geocode_attempted_at = now()
    WHERE id = %(id)s
"""

_STAMP_SQL = """
    UPDATE listings SET geocode_attempted_at = now() WHERE id = %(id)s
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", default=None,
                        help="Portal source(s) to process (repeatable); "
                             f"default: {', '.join(DEFAULT_SOURCES)}.")
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

    sources = list(args.source) if args.source else list(DEFAULT_SOURCES)
    geocoder = build_geocoder()  # run-memoised Mapy.cz, or None when no key is set
    if geocoder is None:
        LOG.info("BACKFILL skip: no Mapy.cz API key set; nothing to geocode")
        return 0

    start = time.monotonic()
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(_COUNT_SQL, {"sources": sources})
            pending = int(cur.fetchone()[0])
        LOG.info("BACKFILL sources=%s pending=%d limit=%d", sources, pending, args.limit)
        if args.dry_run:
            return 0

        with conn.cursor() as cur:
            cur.execute(_SELECT_SQL, {"sources": sources, "limit": args.limit})
            rows = cur.fetchall()

        placed = stamped = errors = 0
        dirty: list[int] = []
        for i, (sid, locality, prop_id) in enumerate(rows):
            if args.max_seconds and time.monotonic() - start > args.max_seconds:
                LOG.info("BACKFILL stopping: --max-seconds reached")
                break
            try:
                # Applies the skip-type + CZ-bbox policy and the persistent
                # positive/negative cache; None = nothing store-worthy.
                result = geocode_cached(conn, geocoder, locality)
            except Exception as exc:  # noqa: BLE001 - a bad query must not abort the run
                LOG.warning("BACKFILL geocode error id=%d: %s", sid, exc)
                errors += 1
                result = None
            with conn.cursor() as cur:
                if result is not None:
                    cur.execute(_PLACE_SQL, {"id": sid, "lat": result.lat, "lon": result.lng})
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
