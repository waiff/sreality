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
    * bazoš EXCLUDED entirely (its detail-link coordinate is one pin per town);
    * `pozemek` (land) EXCLUDED — a parcel has no building street; the nearest
      address point would be a neighbour's, not the parcel's.

  EXACT MATCH: assign only when, within `--tolerance` metres AND inside the same
  obec (obec cross-check), the address points name EXACTLY ONE distinct street.
  Zero -> no match; ≥2 -> ambiguous -> skip. The nearest point's house number
  rides along (rule-B dedup lever).

Robustness + cost (the two reasons this script diverged from its first version):

  * NO long transaction. Each ≤500-row write commits on its own (the sibling
    backfill_portal_streets shape), so it never holds listings row-locks across
    the whole run. `lock_timeout` + a bounded deadlock/lock-timeout retry turn a
    collision with the */15 listings writers into a fast self-healing retry, never
    a failed run. (The first version held ONE transaction for the whole run to
    keep a temp table alive, and deadlocked against the index walk / detail drain.)
  * VERSION-GATED. Every processed candidate is stamped with the current
    address_points revision (matched AND no-match), so a permanently-unresolvable
    row leaves the pool until the dataset advances — the only event that can change
    its outcome (the monthly RÚIAN ingest bumps the revision) — or its coordinate
    changes (listings_set_admin_geo clears the stamp). Steady-state weekly runs
    then scan only genuinely-new rows instead of re-probing ~93k dead candidates
    against 1.57M points every week.

`--calibrate` reports, for listings that ALREADY have street+coord, the distance
to the nearest RÚIAN point OF THE SAME street — pick `--tolerance` from it, never
guess. street/house_number/the stamp are out of the listings content hash -> no
snapshot.

Usage:  python -m scripts.backfill_address_point_streets [--calibrate] [--tolerance 25]
                [--min-share 4] [--source sreality] [--limit 200000] [--dry-run]
                [--force-rescan]
Required: SUPABASE_DB_URL.
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import sys
import time
from typing import Any, Callable

import psycopg

from scraper import db
from scraper.street import street_name_key

LOG = logging.getLogger("backfill_address_point_streets")

# bazoš excluded (town-center link pins).
_SOURCES: tuple[str, ...] = ("sreality", "idnes", "remax", "bezrealitky", "maxima")
_CHUNK = 500
_CURSOR_MIN = -(10 ** 18)
_CALIBRATED_TOLERANCE = 15.0
_CALIBRATED_MIN_SHARE = 4
_LOCK_TIMEOUT = "5s"
_RETRY_ATTEMPTS = 5

# Geocode-fallback coordinates (rounded coords shared by >= min_share active
# listings of one source). Materialized once into a Python set; the candidate
# scan filters against it in memory — no temp table, so no long transaction.
_REJECT_SQL = """
    SELECT source,
           round(st_y(geom::geometry)::numeric, 5)::float8 AS rlat,
           round(st_x(geom::geometry)::numeric, 5)::float8 AS rlon
    FROM listings
    WHERE is_active AND geom IS NOT NULL
    GROUP BY 1, 2, 3
    HAVING count(*) >= %(min_share)s
"""

# rlat/rlon are SQL-rounded with the SAME expression as _REJECT_SQL so the
# in-memory set membership is exact (identical float8 on both sides). The scan is
# served by migration 184's partial index (source, sreality_id) WHERE street IS
# NULL; the version predicate is a cheap filter on the fetched rows.
_CANDIDATE_SQL = """
    SELECT l.sreality_id, l.obec_id,
           st_y(l.geom::geometry) AS lat, st_x(l.geom::geometry) AS lon,
           round(st_y(l.geom::geometry)::numeric, 5)::float8 AS rlat,
           round(st_x(l.geom::geometry)::numeric, 5)::float8 AS rlon
    FROM listings l
    WHERE l.source = %(source)s AND l.is_active AND l.street IS NULL
      AND l.obec_id IS NOT NULL AND l.geom IS NOT NULL
      AND l.category_main IS NOT NULL AND l.category_main <> 'pozemek'
      AND st_y(l.geom::geometry) BETWEEN 48.0 AND 51.5
      AND st_x(l.geom::geometry) BETWEEN 12.0 AND 19.0
      AND (%(source)s <> 'sreality'
           OR l.raw_json->'locality'->>'accuracy' IS DISTINCT FROM 'not_address')
      AND (%(force)s
           OR l.coord_street_attempt_version IS NULL
           OR l.coord_street_attempt_version <> %(version)s)
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

# Matched rows: set street + house + the attempt stamp, return property_id so the
# caller enqueues the parent dirty in the SAME transaction. street_name_key is set in
# lockstep with street (migration 256) — this resolver is a live street-write path, so it
# must keep the dedup street-key consistent or the --dirty scoped load would silently omit
# these rows as peers (rule #19); a resolved row always gets a definite street, so a plain
# assignment is correct.
_UPDATE_SQL = """
    UPDATE listings l
    SET street = d.street,
        street_name_key = d.street_name_key,
        house_number = COALESCE(l.house_number, d.house_number),
        street_source = 'resolver',
        coord_street_attempt_version = %(version)s,
        raw_json = l.raw_json || '{"coord_street_resolved": true}'::jsonb
    FROM (SELECT * FROM unnest(%(ids)s::bigint[], %(streets)s::text[],
                              %(name_keys)s::text[], %(houses)s::text[])
          AS t(id, street, street_name_key, house_number)) d
    WHERE l.sreality_id = d.id
    RETURNING l.property_id
"""

# Everyone else we looked at this run (town-centre pins, ambiguous, no-match):
# stamp only, so they leave the candidate pool until the dataset advances.
_STAMP_SQL = """
    UPDATE listings
    SET coord_street_attempt_version = %(version)s
    WHERE sreality_id = ANY(%(ids)s::bigint[])
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


def partition_matches(
    results: list[tuple[int, list[str] | None, str | None]],
) -> tuple[list[tuple[int, str, str | None]], list[int], dict[str, int]]:
    """Split MATCH_SQL rows into street updates vs stamp-only ids (+ counts).

    Pure: a unique street -> an update; zero or ≥2 distinct streets -> stamp-only
    (no-match / ambiguous). Order-preserving so callers' arrays stay aligned.
    """
    updates: list[tuple[int, str, str | None]] = []
    other_ids: list[int] = []
    matched = ambiguous = nomatch = 0
    for sid, street_arr, house in results:
        if street_arr and len(street_arr) == 1:
            updates.append((sid, street_arr[0], house))
            matched += 1
        else:
            other_ids.append(sid)
            if street_arr:
                ambiguous += 1
            else:
                nomatch += 1
    return updates, other_ids, {"matched": matched, "ambiguous": ambiguous, "nomatch": nomatch}


def _load_reject_set(conn: Any, min_share: int) -> set[tuple[str, float, float]]:
    with conn.cursor() as cur:
        cur.execute(_REJECT_SQL, {"min_share": min_share})
        reject = {(src, rlat, rlon) for src, rlat, rlon in cur.fetchall()}
    LOG.info("RESOLVE reject_coords loaded=%d (min_share=%d)", len(reject), min_share)
    return reject


def _current_version(conn: Any) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT max(revision) FROM address_points_revisions")
        row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _execute_with_retry(conn: Any, run: Callable[[Any], Any], what: str, id_range: str) -> Any:
    """Run `run(conn)` inside a short transaction, retrying on a deadlock /
    lock-timeout. On exhaustion: log the id range and return None so the run
    continues (idempotent — street IS NULL re-selects the batch next run).

    lock_timeout is SET LOCAL inside the transaction, not at connect time: on the
    transaction-mode pooler a session-level SET would not reliably carry across
    transactions, so a per-transaction local scope is the only reliable place.
    """
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
                return run(conn)
        except (psycopg.errors.DeadlockDetected, psycopg.errors.LockNotAvailable) as exc:
            if attempt == _RETRY_ATTEMPTS - 1:
                LOG.warning("RESOLVE %s gave up after %d attempts on %s: %s",
                            what, _RETRY_ATTEMPTS, id_range, exc.__class__.__name__)
                return None
            time.sleep(min(2.0, 0.1 * (2 ** attempt)) * (0.5 + random.random()))
    return None


def _apply_matches(conn: Any, updates: list[tuple[int, str, str | None]], version: int) -> None:
    ids = [u[0] for u in updates]
    streets = [u[1] for u in updates]
    name_keys = [street_name_key(u[1]) for u in updates]
    houses = [u[2] for u in updates]

    def run(c: Any) -> None:
        with c.cursor() as cur:
            cur.execute(_UPDATE_SQL, {"ids": ids, "streets": streets,
                                      "name_keys": name_keys, "houses": houses,
                                      "version": version})
            prop_ids = [r[0] for r in cur.fetchall() if r[0] is not None]
        if prop_ids:
            db.mark_properties_dirty(c, prop_ids)

    _execute_with_retry(conn, run, "match-write", f"{ids[0]}..{ids[-1]}")


def _stamp(conn: Any, ids: list[int], version: int) -> None:
    if not ids:
        return

    def run(c: Any) -> None:
        with c.cursor() as cur:
            cur.execute(_STAMP_SQL, {"ids": ids, "version": version})

    _execute_with_retry(conn, run, "stamp", f"{ids[0]}..{ids[-1]}")


def calibrate(conn: Any, n: int) -> None:
    with conn.cursor() as cur:
        cur.execute(_CALIBRATE_SQL, {"sources": list(_SOURCES), "n": n})
        row = cur.fetchone()
    LOG.info("CALIBRATE n=%s matched_same_street=%s p50=%sm p90=%sm p95=%sm within_25m=%s within_50m=%s",
             *row)


def resolve_source(conn: Any, source: str, tol: float, limit: int, version: int,
                   force: bool, reject: set[tuple[str, float, float]],
                   deadline: float | None, dry_run: bool) -> dict[str, int]:
    cursor = _CURSOR_MIN
    matched = ambiguous = nomatch = town_pins = stamped = processed = 0
    while processed < limit:
        if deadline is not None and time.monotonic() > deadline:
            LOG.info("RESOLVE source=%s stopping: --max-seconds reached", source)
            break
        chunk_size = min(_CHUNK, limit - processed)
        with conn.cursor() as cur:
            cur.execute(_CANDIDATE_SQL, {"source": source, "cursor": cursor,
                                         "chunk": chunk_size, "version": version,
                                         "force": force})
            cands = cur.fetchall()
        if not cands:
            break
        cursor = cands[-1][0]
        processed += len(cands)

        to_match = [c for c in cands if (source, c[4], c[5]) not in reject]
        town_pin_ids = [c[0] for c in cands if (source, c[4], c[5]) in reject]
        town_pins += len(town_pin_ids)

        updates: list[tuple[int, str, str | None]] = []
        other_ids: list[int] = []
        if to_match:
            with conn.cursor() as cur:
                cur.execute(_MATCH_SQL, {"sids": [c[0] for c in to_match],
                                         "obecs": [c[1] for c in to_match],
                                         "lats": [c[2] for c in to_match],
                                         "lons": [c[3] for c in to_match], "tol": tol})
                results = cur.fetchall()
            updates, other_ids, counts = partition_matches(results)
            matched += counts["matched"]
            ambiguous += counts["ambiguous"]
            nomatch += counts["nomatch"]

        if not dry_run:
            if updates:
                _apply_matches(conn, updates, version)
            stamp_ids = sorted(town_pin_ids + other_ids)
            _stamp(conn, stamp_ids, version)
            stamped += len(updates) + len(stamp_ids)

        LOG.info("RESOLVE source=%s processed=%d matched=%d ambiguous=%d nomatch=%d town_pins=%d",
                 source, processed, matched, ambiguous, nomatch, town_pins)
    LOG.info("RESOLVE source=%s done matched=%d ambiguous=%d nomatch=%d town_pins=%d stamped=%d processed=%d",
             source, matched, ambiguous, nomatch, town_pins, stamped, processed)
    return {"matched": matched, "ambiguous": ambiguous, "nomatch": nomatch,
            "town_pins": town_pins, "stamped": stamped}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibrate", action="store_true",
                        help="Report same-street distance distribution and exit (pick --tolerance).")
    parser.add_argument("--tolerance", type=float, default=_CALIBRATED_TOLERANCE,
                        help="Max metres from coordinate to the RÚIAN point (exact-match guard). "
                             "15 m calibrated on ground truth = 99.2% precision; looser drops it (20 m → 97.6%).")
    parser.add_argument("--min-share", type=int, default=_CALIBRATED_MIN_SHARE,
                        help="A coord shared by >= this many active listings is a town-center pin (rejected).")
    parser.add_argument("--source", choices=_SOURCES, default=None)
    parser.add_argument("--limit", type=int, default=200000,
                        help="Max candidates processed per source this run.")
    parser.add_argument("--max-seconds", type=float, default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="Match but write nothing (report matched/ambiguous/nomatch).")
    parser.add_argument("--force-rescan", action="store_true",
                        help="Re-attempt every candidate regardless of its stamp (use after tuning).")
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

    # A run that deviates from the calibrated defaults is re-testing the guards,
    # so it must bypass the stamp or it would silently no-op on exactly the rows
    # it means to re-evaluate.
    tuned = args.tolerance != _CALIBRATED_TOLERANCE or args.min_share != _CALIBRATED_MIN_SHARE
    force = args.force_rescan or tuned

    with db.connect() as conn:
        if args.calibrate:
            calibrate(conn, args.calibrate_n)
            return 0
        version = _current_version(conn)
        reject = _load_reject_set(conn, args.min_share)
        LOG.info("RESOLVE start version=%d tol=%.0fm force=%s sources=%s",
                 version, args.tolerance, force, ",".join(sources))
        totals = {"matched": 0, "ambiguous": 0, "nomatch": 0, "town_pins": 0, "stamped": 0}
        for source in sources:
            if deadline is not None and time.monotonic() > deadline:
                break
            res = resolve_source(conn, source, args.tolerance, args.limit, version,
                                 force, reject, deadline, args.dry_run)
            for k in totals:
                totals[k] += res[k]
    LOG.info("RESOLVE all-done matched=%d ambiguous=%d nomatch=%d town_pins=%d stamped=%d",
             totals["matched"], totals["ambiguous"], totals["nomatch"],
             totals["town_pins"], totals["stamped"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
