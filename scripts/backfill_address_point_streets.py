"""Resolve listings.street from a trustworthy per-listing coordinate via RÚIAN.

Implements docs/design/street-coverage-ruian.md. For listings that publish NO
street text but DO carry a precise coordinate, look up the street from the local
`address_points` mirror. EXACT-MATCH ONLY — a street is assigned only when it is
unambiguous; otherwise the row stays NULL. A wrong street is worse than NULL.

The no-estimates discipline (the four design guards):

  TRUSTWORTHY COORDINATE (never a town centre):
    * CZ bbox + obec_id set;
    * NOT a shared geocode-fallback pin — fewer than `--min-share` active
      listings of the same source on the exact rounded coordinate (a real
      building coordinate is ~unique; a town/quarter geocode is shared by many);
    * sreality: reject `locality.accuracy = 'not_address'` (municipality-level);
    * bazoš EXCLUDED entirely (its detail-link coordinate is one pin per town).

  EXACT MATCH: assign only when, within `--tolerance` metres AND inside the same
  obec (obec cross-check), the address points name EXACTLY ONE distinct street.
  Zero -> no match; ≥2 -> ambiguous -> skip. The nearest point's house number
  rides along (rule-B dedup lever).

`--calibrate` reports, for listings that ALREADY have street+coord, the distance
to the nearest RÚIAN point OF THE SAME street — pick `--tolerance` from it, never
guess. street/house_number are out of the listings content hash -> no snapshot.

Usage:  python -m scripts.backfill_address_point_streets [--calibrate] [--tolerance 25]
                [--min-share 4] [--source sreality] [--limit 200000] [--dry-run]
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

LOG = logging.getLogger("backfill_address_point_streets")

# bazoš excluded (town-center link pins).
_SOURCES: tuple[str, ...] = ("sreality", "idnes", "remax", "bezrealitky", "maxima")
_CHUNK = 500
_CURSOR_MIN = -(10 ** 18)

# Geocode-fallback coordinates (shared by >= min_share active listings of one
# source). Materialized once per run; the candidate scan anti-joins it.
_FALLBACK_SQL = """
    CREATE TEMP TABLE fallback_coords ON COMMIT DROP AS
    SELECT source,
           round(st_y(geom::geometry)::numeric, 5) AS rlat,
           round(st_x(geom::geometry)::numeric, 5) AS rlon
    FROM listings
    WHERE is_active AND geom IS NOT NULL
    GROUP BY 1, 2, 3
    HAVING count(*) >= %(min_share)s
"""
_FALLBACK_IDX_SQL = "CREATE INDEX ON fallback_coords (source, rlat, rlon)"

_CANDIDATE_SQL = """
    SELECT l.sreality_id, l.obec_id,
           st_y(l.geom::geometry) AS lat, st_x(l.geom::geometry) AS lon
    FROM listings l
    WHERE l.source = %(source)s AND l.is_active AND l.street IS NULL
      AND l.obec_id IS NOT NULL AND l.geom IS NOT NULL
      AND st_y(l.geom::geometry) BETWEEN 48.0 AND 51.5
      AND st_x(l.geom::geometry) BETWEEN 12.0 AND 19.0
      AND (%(source)s <> 'sreality'
           OR l.raw_json->'locality'->>'accuracy' IS DISTINCT FROM 'not_address')
      AND NOT EXISTS (
        SELECT 1 FROM fallback_coords f
        WHERE f.source = l.source
          AND f.rlat = round(st_y(l.geom::geometry)::numeric, 5)
          AND f.rlon = round(st_x(l.geom::geometry)::numeric, 5))
      AND l.sreality_id > %(cursor)s
    ORDER BY l.sreality_id
    LIMIT %(chunk)s
"""

_MATCH_SQL = """
    WITH cand(sid, obec_id, pt) AS (
      SELECT u.sid, u.obec_id,
             ST_SetSRID(ST_MakePoint(u.lon, u.lat), 4326)::geography
      FROM unnest(%(sids)s::bigint[], %(obecs)s::int[],
                  %(lats)s::float8[], %(lons)s::float8[]) AS u(sid, obec_id, lat, lon)
    )
    SELECT c.sid,
      (SELECT array_agg(DISTINCT ap.street)
         FROM address_points ap
         WHERE ap.obec_id = c.obec_id
           AND ST_DWithin(ap.geom, c.pt, %(tol)s)) AS streets,
      (SELECT ap.house_number
         FROM address_points ap
         WHERE ap.obec_id = c.obec_id
           AND ST_DWithin(ap.geom, c.pt, %(tol)s)
         ORDER BY ap.geom <-> c.pt LIMIT 1) AS nearest_house_number
    FROM cand c
"""

_UPDATE_SQL = """
    UPDATE listings l
    SET street = d.street,
        house_number = COALESCE(l.house_number, d.house_number),
        raw_json = l.raw_json || '{"coord_street_resolved": true}'::jsonb
    FROM (SELECT * FROM unnest(%(ids)s::bigint[], %(streets)s::text[],
                              %(houses)s::text[]) AS t(id, street, house_number)) d
    WHERE l.sreality_id = d.id
"""

_CALIBRATE_SQL = """
    WITH gt AS (
      SELECT l.sreality_id, l.obec_id, l.street,
             ST_SetSRID(ST_MakePoint(st_x(l.geom::geometry), st_y(l.geom::geometry)), 4326)::geography AS pt
      FROM listings l
      WHERE l.is_active AND l.street IS NOT NULL AND l.obec_id IS NOT NULL
        AND l.geom IS NOT NULL AND l.source = ANY(%(sources)s)
      ORDER BY l.sreality_id DESC
      LIMIT %(n)s
    ),
    matched AS (
      SELECT gt.sreality_id,
        (SELECT min(ST_Distance(ap.geom, gt.pt))
           FROM address_points ap
           WHERE ap.obec_id = gt.obec_id
             AND lower(ap.street) = lower(gt.street)
             AND ST_DWithin(ap.geom, gt.pt, 500)) AS dist_same_street
      FROM gt
    )
    SELECT count(*) AS n,
      count(dist_same_street) AS matched_same_street,
      round(percentile_cont(0.50) within group (order by dist_same_street)::numeric, 1) AS p50_m,
      round(percentile_cont(0.90) within group (order by dist_same_street)::numeric, 1) AS p90_m,
      round(percentile_cont(0.95) within group (order by dist_same_street)::numeric, 1) AS p95_m,
      count(*) filter (where dist_same_street <= 25) AS within_25m,
      count(*) filter (where dist_same_street <= 50) AS within_50m
    FROM matched
"""


def calibrate(conn: Any, n: int) -> None:
    with conn.cursor() as cur:
        cur.execute(_CALIBRATE_SQL, {"sources": list(_SOURCES), "n": n})
        row = cur.fetchone()
    LOG.info("CALIBRATE n=%s matched_same_street=%s p50=%sm p90=%sm p95=%sm within_25m=%s within_50m=%s",
             *row)


def resolve_source(conn: Any, source: str, tol: float, limit: int,
                   deadline: float | None, dry_run: bool) -> dict[str, int]:
    cursor = _CURSOR_MIN
    matched = ambiguous = nomatch = processed = 0
    while processed < limit:
        if deadline is not None and time.monotonic() > deadline:
            LOG.info("RESOLVE source=%s stopping: --max-seconds reached", source)
            break
        chunk_size = min(_CHUNK, limit - processed)
        with conn.cursor() as cur:
            cur.execute(_CANDIDATE_SQL, {"source": source, "cursor": cursor, "chunk": chunk_size})
            cands = cur.fetchall()
        if not cands:
            break
        cursor = cands[-1][0]
        processed += len(cands)
        sids = [c[0] for c in cands]
        obecs = [c[1] for c in cands]
        lats = [c[2] for c in cands]
        lons = [c[3] for c in cands]
        with conn.cursor() as cur:
            cur.execute(_MATCH_SQL, {"sids": sids, "obecs": obecs,
                                     "lats": lats, "lons": lons, "tol": tol})
            results = cur.fetchall()
        ids: list[int] = []
        streets: list[str] = []
        houses: list[str | None] = []
        for sid, street_arr, house in results:
            if street_arr and len(street_arr) == 1:
                ids.append(sid)
                streets.append(street_arr[0])
                houses.append(house)
                matched += 1
            elif street_arr and len(street_arr) > 1:
                ambiguous += 1
            else:
                nomatch += 1
        if ids and not dry_run:
            with conn.cursor() as cur:
                cur.execute(_UPDATE_SQL, {"ids": ids, "streets": streets, "houses": houses})
            dirty = _dirty_property_ids(conn, ids)
            if dirty:
                db.mark_properties_dirty(conn, dirty)
        LOG.info("RESOLVE source=%s processed=%d matched=%d ambiguous=%d nomatch=%d",
                 source, processed, matched, ambiguous, nomatch)
    LOG.info("RESOLVE source=%s done matched=%d ambiguous=%d nomatch=%d processed=%d",
             source, matched, ambiguous, nomatch, processed)
    return {"matched": matched, "ambiguous": ambiguous, "nomatch": nomatch}


def _dirty_property_ids(conn: Any, sids: list[int]) -> list[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT property_id FROM listings WHERE sreality_id = ANY(%s) AND property_id IS NOT NULL",
                    (sids,))
        return [r[0] for r in cur.fetchall()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibrate", action="store_true",
                        help="Report same-street distance distribution and exit (pick --tolerance).")
    parser.add_argument("--tolerance", type=float, default=15.0,
                        help="Max metres from coordinate to the RÚIAN point (exact-match guard). "
                             "15 m calibrated on ground truth = 99.2% precision; looser drops it (20 m → 97.6%).")
    parser.add_argument("--min-share", type=int, default=4,
                        help="A coord shared by >= this many active listings is a town-center pin (rejected).")
    parser.add_argument("--source", choices=_SOURCES, default=None)
    parser.add_argument("--limit", type=int, default=200000,
                        help="Max candidates processed per source this run.")
    parser.add_argument("--max-seconds", type=float, default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="Match but write nothing (report matched/ambiguous/nomatch).")
    parser.add_argument("--calibrate-n", type=int, default=20000)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    sources = (args.source,) if args.source else _SOURCES
    start = time.monotonic()
    deadline = start + args.max_seconds if args.max_seconds else None

    # ONE transaction: the TEMP fallback_coords must survive across statements,
    # and on the transaction pooler a temp table does NOT survive a commit. The
    # trustworthy-coord set is small (~13k), so one transaction is cheap; the run
    # is fully re-runnable (street IS NULL + the resolved marker make it idempotent).
    with db.connect() as conn:
        conn.autocommit = False
        if args.calibrate:
            calibrate(conn, args.calibrate_n)
            conn.rollback()
            return 0
        with conn.cursor() as cur:
            cur.execute(_FALLBACK_SQL, {"min_share": args.min_share})
            cur.execute(_FALLBACK_IDX_SQL)
        LOG.info("RESOLVE fallback_coords built (min_share=%d); tol=%.0fm", args.min_share, args.tolerance)
        totals = {"matched": 0, "ambiguous": 0, "nomatch": 0}
        for source in sources:
            if deadline is not None and time.monotonic() > deadline:
                break
            res = resolve_source(conn, source, args.tolerance, args.limit, deadline, args.dry_run)
            for k in totals:
                totals[k] += res[k]
        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()
    LOG.info("RESOLVE all-done matched=%d ambiguous=%d nomatch=%d",
             totals["matched"], totals["ambiguous"], totals["nomatch"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
