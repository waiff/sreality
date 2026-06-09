"""One-off backfill: re-place bazos listings stuck on the shared maps-link pin.

Before the migration-406 parser fix, every bazos listing had a NULL `locality`
(the 3-cell Lokalita row was mis-parsed), which silently disabled the text-first
street geocoder — so 94% of listings fell back to the page-wide maps-link pin and
whole towns collapsed onto a single map dot. This re-places the ones we can place
precisely: each active bazos listing whose coords came from the link
(`coords.source = 'link'`) AND that carries an extractable street is re-parsed
from its already-staged detail HTML (`portal_raw_pages` — NO re-fetch of bazos)
with the fixed parser + a cached Mapy.cz geocoder. When the re-parse yields a
precise street geocode, the listing's `geom` + `locality` are updated IN PLACE
and the parent property is enqueued dirty so Browse refreshes within ~5 min.

This deliberately writes NO snapshot (architectural rule #2 governs source-content
changes; correcting our own mis-geocoding of the SAME listing state is a
data-quality fix, not a new real-world state — same posture as the PR #405 image
backfill).

Idempotent + resumable: every processed row is stamped (`coords.reparsed = true`),
so it drops out of the next selection whether or not it improved; writes commit
per row (autocommit). A timeout/cancel preserves completed work; `--max-seconds`
stops claiming cleanly before the job timeout.

Usage:  python -m scripts.backfill_bazos_coords --limit 20000
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
from scraper.bazos_parser import parse_detail

LOG = logging.getLogger("backfill_bazos_coords")

# Active, link-pinned, street-bearing bazos listings that we haven't re-parsed
# yet, each joined to its most recent staged detail page.
_SELECT_SQL = """
    SELECT l.sreality_id, l.source_id_native, l.source_url,
           l.category_main, l.category_type, l.property_id,
           ST_AsText(l.geom) AS geom_wkt, p.html
    FROM listings l
    JOIN LATERAL (
        SELECT html FROM portal_raw_pages pr
        WHERE pr.source = 'bazos' AND pr.source_id_native = l.source_id_native
          AND pr.page_kind = 'detail'
        ORDER BY pr.fetched_at DESC NULLS LAST
        LIMIT 1
    ) p ON true
    WHERE l.source = 'bazos' AND l.is_active
      AND l.raw_json->'coords'->>'source' = 'link'
      AND l.raw_json->'coords'->>'street' IS NOT NULL
      AND l.raw_json->'coords'->>'reparsed' IS NULL
    ORDER BY l.sreality_id
    LIMIT %(limit)s
"""

_COUNT_SQL = """
    SELECT count(*) FROM listings l
    WHERE l.source = 'bazos' AND l.is_active
      AND l.raw_json->'coords'->>'source' = 'link'
      AND l.raw_json->'coords'->>'street' IS NOT NULL
      AND l.raw_json->'coords'->>'reparsed' IS NULL
"""

# Patch only coords + the marker (no geom change) — for rows that didn't improve
# or failed to parse, so they drop out of the selection without moving the point.
_STAMP_SQL = """
    UPDATE listings
    SET raw_json = jsonb_set(
        raw_json, '{coords}',
        coalesce(raw_json->'coords', '{}'::jsonb) || jsonb_build_object('reparsed', true)
    )
    WHERE sreality_id = %(id)s
"""

# An improved row: move geom (the admin-geo trigger re-derives obec/okres/region),
# set locality, and replace coords with the fresh street-geocoded provenance.
_UPDATE_SQL = """
    UPDATE listings
    SET geom = ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography,
        locality = %(locality)s,
        raw_json = jsonb_set(raw_json, '{coords}', %(coords)s::jsonb)
    WHERE sreality_id = %(id)s
"""


def _coord_close(geom_wkt: str | None, lat: float, lon: float) -> bool:
    """True when the existing point already equals the new one (~1e-5 deg ≈ 1m)."""
    if not geom_wkt or not geom_wkt.startswith("POINT("):
        return False
    try:
        ex_lon, ex_lat = (float(v) for v in geom_wkt[6:-1].split())
    except (ValueError, IndexError):
        return False
    return abs(ex_lat - lat) < 1e-5 and abs(ex_lon - lon) < 1e-5


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

    geocoder = _build_geocoder()
    if geocoder is None:
        LOG.info("BACKFILL skip: MAPY_CZ_API_KEY unset; nothing to re-place")
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

        moved = stamped = errors = 0
        dirty: list[int] = []
        for i, (sid, native, url, cmain, ctype, prop_id, geom_wkt, html) in enumerate(rows):
            if args.max_seconds and time.monotonic() - start > args.max_seconds:
                LOG.info("BACKFILL stopping: --max-seconds reached")
                break
            try:
                listing = parse_detail(
                    html, source_url=url,
                    category_main=cmain, category_type=ctype, geocoder=geocoder,
                )
            except Exception as exc:  # noqa: BLE001 - a bad staged page must not abort the run
                LOG.warning("BACKFILL parse error id=%d: %s", sid, exc)
                with conn.cursor() as cur:
                    cur.execute(_STAMP_SQL, {"id": sid})
                errors += 1
                continue

            coords = dict(listing.raw.get("coords") or {})
            coords["reparsed"] = True
            improved = (
                coords.get("source") == "street"
                and listing.lat is not None and listing.lon is not None
                and not _coord_close(geom_wkt, listing.lat, listing.lon)
            )
            with conn.cursor() as cur:
                if improved:
                    cur.execute(_UPDATE_SQL, {
                        "id": sid, "lat": listing.lat, "lon": listing.lon,
                        "locality": listing.locality, "coords": json.dumps(coords),
                    })
                    moved += 1
                    if prop_id is not None:
                        dirty.append(prop_id)
                else:
                    cur.execute(_STAMP_SQL, {"id": sid})
                    stamped += 1

            if dirty and len(dirty) >= 500:
                db.mark_properties_dirty(conn, dirty)
                dirty = []
            if (i + 1) % 200 == 0:
                LOG.info("BACKFILL progress=%d/%d moved=%d unchanged=%d errors=%d",
                         i + 1, len(rows), moved, stamped, errors)

        if dirty:
            db.mark_properties_dirty(conn, dirty)

    LOG.info("BACKFILL done processed=%d moved=%d unchanged=%d errors=%d",
             moved + stamped + errors, moved, stamped, errors)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
