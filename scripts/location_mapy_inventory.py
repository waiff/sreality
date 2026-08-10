"""Location W1 / R2: materialise the Mapy.cz five-arm affected set (04 C7.2).

Populates the immutable evidence tables of migration 385. It is a W1 INPUT, not
an output: 06 6.1.2 admits a `carry_forward` coordinate as class B only when the
row is absent from this inventory, so the first location claim must not be
written before this job has run to completion. R4 (W4) reuses the same rows as
the before/after purge ledger.

The five arms of C7.2's set A:

  1. raw_json->'coords'->>'source' IN ('geocode','carry_forward')  -- plus bazos
     'street'/'locality', which 06 6.1.2 row 5 establishes as bazos' in-parser
     Mapy geocoder (a deliberate superset of C7.2's literal list; only `link` is
     first-party on bazos).
  2. listings.geocode_attempted_at IS NOT NULL.
  3. listings.geom matches a geocode_cache coordinate -- recorded as a BOOLEAN.
     The h3 extension is not installed here, so the design's fallback applies: a
     rounded coordinate cell key plus the mandatory 3x3 neighbourhood. The
     matching happens in memory (all ~1.3k cache coordinates fit trivially) and
     NOTHING about the coordinate is written back.
  4. every geocode_cache row -> mapy_affected_cache (identity only).
  5. every property with a child in arms 1-3 -> mapy_affected_props.

NEVER PERSISTED (06 6.1.5, non-negotiable): lat, lng, matched_type, confidence,
or any key derived from a Mapy coordinate. Storing them in a quarantine table
would be the same violation under a new table name.

Complete over ALL listings, active and inactive — the licence gate and the R4
purge both key on listing_id and an inactive row's coordinate is published just
the same. The scan therefore walks the whole table by keyset; it detoasts
raw_json per row, so it is I/O-heavy and deliberately resumable: each batch is
one transaction that inserts its evidence and advances the run's high-water mark
together, and a re-run continues from there. Re-inserting an already-recorded
listing is a no-op (ON CONFLICT DO NOTHING — the only write the immutability
trigger allows). A resumed run does not revisit listings it already scanned, so
after a payload-changing refetch wave the full sweep is `--restart` (still
duplicate-free); R1 has stopped new geocodes, so that drift is bounded. A
`--restart` opens a new `restart_epoch` and resume is scoped to the highest
epoch, so a restart that dies mid-sweep resumes from its OWN mark instead of
inheriting the finished previous sweep's end-of-table mark.

Usage:  python -m scripts.location_mapy_inventory [--batch-size 20000] [--restart]
Required: SUPABASE_DB_URL.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import psycopg

from scraper import db

LOG = logging.getLogger("location_mapy_inventory")

# 06 6.1.2 ladder + row 5: 'street'/'locality' are bazos-only tokens and are Mapy
# output exactly like 'geocode'; 'carry_forward' re-persists an earlier
# coordinate with no re-validation, so it launders whatever wrote it first.
MAPY_COORDS_SOURCES = frozenset({"geocode", "carry_forward", "street", "locality"})

# Arm 3 grid. 1e-5 deg is ~1.1 m north-south and ~0.7 m east-west at 50 N; with
# the 3x3 neighbourhood the effective tolerance is ~2.5 m — far wider than the
# float round-trip through geography(Point,4326) and far tighter than any
# coincidental first-party pin.
DEFAULT_EPSILON_DEG = 1e-5

MIN_BATCH_SIZE = 10_000
MAX_BATCH_SIZE = 30_000

_REASON_MAPY = "mapy_derived_coordinate"
_REASON_UNKNOWN = "coordinate_provenance_unknown"
_REASON_PROPERTY = "child_listing_in_affected_set"

_RELATIONS = (
    "mapy_inventory_runs", "mapy_affected", "mapy_affected_cache", "mapy_affected_props",
)

_TIMEOUT_GUARD_SQL = """
    SELECT set_config('statement_timeout', %(statement_timeout)s, true),
           set_config('lock_timeout', %(lock_timeout)s, true)
"""

_RELATIONS_SQL = "SELECT to_regclass(%(name)s)"

_CACHE_SELECT_SQL = "SELECT query_key, lat, lng, resolved_at FROM geocode_cache"

_CACHE_INSERT_SQL = """
    INSERT INTO mapy_affected_cache
        (query_key, query_key_sha256, resolved_at, reason_code, inventory_run_id)
    VALUES (%(query_key)s, %(query_key_sha256)s, %(resolved_at)s, %(reason_code)s, %(run_id)s)
    ON CONFLICT (query_key) DO NOTHING
"""

_RUN_INSERT_SQL = """
    INSERT INTO mapy_inventory_runs
        (match_epsilon_deg, note, resumable, restart_epoch)
    VALUES (%(epsilon)s, %(note)s, %(resumable)s, %(restart_epoch)s)
    RETURNING id
"""

# Restart lineage. --restart opens epoch max()+1 and resume reads the high-water
# mark WITHIN the highest epoch only: a completed earlier sweep's mark is the end
# of the table, so an unscoped max() would mask an interrupted restart's own mark
# and the next dispatch would scan nothing and print `complete`.
_EPOCH_SQL = "SELECT coalesce(max(restart_epoch), 0) FROM mapy_inventory_runs"

_RESUME_SQL = """
    SELECT coalesce(max(scanned_through_listing_id), 0)
    FROM mapy_inventory_runs
    WHERE resumable AND restart_epoch = %(restart_epoch)s
"""

# The whole table, active and inactive, by keyset. raw_json is detoasted for the
# coords stamp; geom is projected to lat/lng for the in-memory arm-3 match.
_LISTING_BATCH_SQL = """
    SELECT l.id,
           l.source,
           l.raw_json->'coords'->>'source' AS coords_source,
           l.geocode_attempted_at,
           ST_Y(l.geom::geometry) AS lat,
           ST_X(l.geom::geometry) AS lng
    FROM listings l
    WHERE l.id > %(after_id)s
    ORDER BY l.id
    LIMIT %(batch_size)s
"""

_AFFECTED_INSERT_SQL = """
    INSERT INTO mapy_affected
        (listing_id, source, arm1_coords_source, coords_source,
         arm2_geocode_attempted, geocode_attempted_at, arm3_geom_matches_cache,
         reason_code, inventory_run_id)
    VALUES (%(listing_id)s, %(source)s, %(arm1_coords_source)s, %(coords_source)s,
            %(arm2_geocode_attempted)s, %(geocode_attempted_at)s,
            %(arm3_geom_matches_cache)s, %(reason_code)s, %(run_id)s)
    ON CONFLICT (listing_id) DO NOTHING
"""

_PROGRESS_SQL = """
    UPDATE mapy_inventory_runs
    SET listings_scanned = listings_scanned + %(scanned)s,
        listings_inserted = listings_inserted + %(inserted)s,
        scanned_through_listing_id = %(last_id)s
    WHERE id = %(run_id)s
"""

_PROPS_INSERT_SQL = """
    INSERT INTO mapy_affected_props (property_id, reason_code, inventory_run_id)
    SELECT DISTINCT l.property_id, %(reason_code)s::text, %(run_id)s::bigint
    FROM listings l
    JOIN mapy_affected a ON a.listing_id = l.id
    WHERE l.property_id IS NOT NULL
    ON CONFLICT (property_id) DO NOTHING
"""

_ARM_COUNTS_SQL = """
    SELECT count(*) FILTER (WHERE arm1_coords_source),
           count(*) FILTER (WHERE arm2_geocode_attempted),
           count(*) FILTER (WHERE arm3_geom_matches_cache),
           count(*)
    FROM mapy_affected
"""

_CACHE_COUNT_SQL = "SELECT count(*) FROM mapy_affected_cache"

_PROPS_COUNT_SQL = "SELECT count(*) FROM mapy_affected_props"

_FAIL_SQL = """
    UPDATE mapy_inventory_runs
    SET finished_at = now(), status = 'failed',
        note = concat_ws(' | ', note, %(note)s::text)
    WHERE id = %(run_id)s
"""

_FINISH_SQL = """
    UPDATE mapy_inventory_runs
    SET finished_at = now(),
        status = %(status)s,
        cache_rows_total = %(cache_rows_total)s,
        cache_rows_with_coordinate = %(cache_rows_with_coordinate)s,
        arm1_rows = %(arm1)s,
        arm2_rows = %(arm2)s,
        arm3_rows = %(arm3)s,
        arm4_rows = %(arm4)s,
        arm5_rows = %(arm5)s
    WHERE id = %(run_id)s
"""


# ---------------------------------------------------------------- pure predicates

def cell_key(lat: float, lng: float, epsilon_deg: float = DEFAULT_EPSILON_DEG) -> tuple[int, int]:
    """Rounded spatial cell for a coordinate (the h3 fallback of the design)."""
    return (round(lat / epsilon_deg), round(lng / epsilon_deg))


def build_cache_cells(
    rows: list[tuple[Any, ...]], epsilon_deg: float = DEFAULT_EPSILON_DEG,
) -> set[tuple[int, int]]:
    """Cells of every POSITIVE geocode_cache row. Negative rows (lat/lng NULL)
    hold no coordinate, so they can never make arm 3 true — they still land in
    mapy_affected_cache as arm 4."""
    cells: set[tuple[int, int]] = set()
    for _query_key, lat, lng, _resolved_at in rows:
        if lat is None or lng is None:
            continue
        cells.add(cell_key(float(lat), float(lng), epsilon_deg))
    return cells


def geom_matches_cache(
    lat: float | None,
    lng: float | None,
    cells: set[tuple[int, int]],
    epsilon_deg: float = DEFAULT_EPSILON_DEG,
) -> bool:
    """Arm 3, with the mandatory 3x3 neighbourhood expansion so a coordinate that
    rounds across a cell boundary still matches."""
    if lat is None or lng is None or not cells:
        return False
    cx, cy = cell_key(float(lat), float(lng), epsilon_deg)
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            if (cx + dx, cy + dy) in cells:
                return True
    return False


def reason_code_for(arm1: bool, arm3: bool) -> str:
    """06 6.1.5's two class-E codes. Arms 1 and 3 name a coordinate that IS Mapy
    output; an attempt-only row (arm 2 alone) records that a geocode ran against
    the listing with success and failure indistinguishable (06 6.1.3), which is
    exactly 'coordinate_provenance_unknown'."""
    return _REASON_MAPY if (arm1 or arm3) else _REASON_UNKNOWN


def evidence_for_listing(
    listing_id: int,
    source: str,
    coords_source: str | None,
    geocode_attempted_at: datetime | None,
    lat: float | None,
    lng: float | None,
    cells: set[tuple[int, int]],
    epsilon_deg: float = DEFAULT_EPSILON_DEG,
) -> dict[str, Any] | None:
    """One mapy_affected row, or None when the listing is in no arm."""
    arm1 = coords_source in MAPY_COORDS_SOURCES
    arm2 = geocode_attempted_at is not None
    arm3 = geom_matches_cache(lat, lng, cells, epsilon_deg)
    if not (arm1 or arm2 or arm3):
        return None
    return {
        "listing_id": listing_id,
        "source": source,
        "arm1_coords_source": arm1,
        "coords_source": coords_source if arm1 else None,
        "arm2_geocode_attempted": arm2,
        "geocode_attempted_at": geocode_attempted_at,
        "arm3_geom_matches_cache": arm3,
        "reason_code": reason_code_for(arm1, arm3),
    }


def cache_identity_row(
    query_key: str, resolved_at: datetime, run_id: int, has_coordinate: bool,
) -> dict[str, Any]:
    """Arm 4 row: identity + reason, never the cached coordinate (06 6.1.4).

    A negative-cache row (lat/lng NULL) is a Mapy query that returned nothing, so
    it never held a Mapy-derived coordinate; within 06 6.1.5's closed two-code
    vocabulary the honest label is 'coordinate_provenance_unknown'. It is still
    arm-4 evidence — the query itself was made — and still excluded from arm 3,
    which needs a coordinate to match against.
    """
    return {
        "query_key": query_key,
        "query_key_sha256": hashlib.sha256(query_key.encode("utf-8")).hexdigest(),
        "resolved_at": resolved_at,
        "reason_code": _REASON_MAPY if has_coordinate else _REASON_UNKNOWN,
        "run_id": run_id,
    }


# ---------------------------------------------------------------- db plumbing

@contextmanager
def guarded(
    conn: psycopg.Connection, statement_timeout_s: int, lock_timeout_s: int = 5,
) -> Iterator[psycopg.Cursor]:
    """One transaction with LOCAL timeouts.

    connect() is autocommit and points at the transaction-mode pooler, where a
    session-level SET can land on a different backend than the statement it was
    meant to guard — so the guard has to be transaction-local, which also makes
    each batch's evidence + high-water mark advance atomically.
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(_TIMEOUT_GUARD_SQL, {
                "statement_timeout": f"{statement_timeout_s}s",
                "lock_timeout": f"{lock_timeout_s}s",
            })
            yield cur


def missing_relations(conn: psycopg.Connection) -> list[str]:
    missing: list[str] = []
    with conn.cursor() as cur:
        for name in _RELATIONS:
            cur.execute(_RELATIONS_SQL, {"name": name})
            if cur.fetchone()[0] is None:
                missing.append(name)
    return missing


def _inserted(cur: psycopg.Cursor, attempted: int) -> int:
    return cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else attempted


def record_failure(run_id: int, exc: BaseException) -> None:
    """Stamp the run 'failed' on a FRESH connection.

    Whatever broke the sweep may have taken the connection with it (aborted
    transaction, dead socket), and this stamp must never mask the original
    exception — so it gets its own connection and swallows its own errors.
    """
    try:
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(_FAIL_SQL, {
                    "note": f"{type(exc).__name__}: {exc}"[:500], "run_id": run_id,
                })
    except Exception:
        LOG.exception("INVENTORY could not stamp run=%s as failed", run_id)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=20_000,
                        help=f"Listings per batch ({MIN_BATCH_SIZE}-{MAX_BATCH_SIZE}).")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max listings scanned this run (default: the whole table).")
    parser.add_argument("--max-seconds", type=float, default=None,
                        help="Wall-clock budget; stops between batches and exits cleanly.")
    parser.add_argument("--start-after-id", type=int, default=None,
                        help="Explicit keyset start; overrides the resume high-water mark.")
    parser.add_argument("--restart", action="store_true",
                        help="Open a new restart epoch and rescan from the first listing "
                             "(still never duplicates); an interrupted restart resumes.")
    parser.add_argument("--epsilon-deg", type=float, default=DEFAULT_EPSILON_DEG,
                        help="Arm-3 cell size in degrees (3x3 neighbourhood on top).")
    parser.add_argument("--statement-timeout", type=int, default=600,
                        help="Per-statement timeout in seconds.")
    parser.add_argument("--note", default=None, help="Free-text note stamped on the run row.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Scan and report the arm counts without writing anything.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2
    batch_size = max(MIN_BATCH_SIZE, min(MAX_BATCH_SIZE, args.batch_size))

    start = time.monotonic()
    with db.connect() as conn:
        missing = missing_relations(conn)
        if missing:
            print(f"ERROR: migration 385 is not applied; missing {', '.join(missing)}",
                  file=sys.stderr)
            return 2

        with conn.cursor() as cur:
            cur.execute(_CACHE_SELECT_SQL)
            cache_rows = cur.fetchall()
        cells = build_cache_cells(cache_rows, args.epsilon_deg)
        LOG.info("INVENTORY cache rows=%d with_coordinate=%d cells=%d",
                 len(cache_rows), sum(1 for r in cache_rows if r[1] is not None), len(cells))

        run_id: int | None = None
        after_id = 0
        with conn.cursor() as cur:
            cur.execute(_EPOCH_SQL)
            epoch = int(cur.fetchone()[0])
        # --restart opens a new epoch; resume then reads that epoch's own mark, so
        # an interrupted restart is resumable and a finished earlier sweep cannot
        # mask it. A dry run reads nothing and opens nothing.
        if args.restart and not args.dry_run:
            epoch += 1
        if args.start_after_id is not None:
            after_id = args.start_after_id
        elif not (args.restart or args.dry_run):
            with conn.cursor() as cur:
                cur.execute(_RESUME_SQL, {"restart_epoch": epoch})
                after_id = int(cur.fetchone()[0])

        if not args.dry_run:
            with guarded(conn, args.statement_timeout) as cur:
                cur.execute(_RUN_INSERT_SQL, {
                    "epsilon": args.epsilon_deg, "note": args.note,
                    "resumable": args.start_after_id is None,
                    "restart_epoch": epoch,
                })
                run_id = int(cur.fetchone()[0])
                # Arm 4 first: the cache is the evidence R4 destroys when it drops
                # geocode_cache, so it is recorded before anything else runs.
                if cache_rows:
                    cur.executemany(_CACHE_INSERT_SQL, [
                        cache_identity_row(query_key, resolved_at, run_id,
                                           lat is not None and lng is not None)
                        for query_key, lat, lng, resolved_at in cache_rows
                    ])
            LOG.info("INVENTORY run=%d epoch=%d resuming after listing_id=%d batch=%d",
                     run_id, epoch, after_id, batch_size)

        scanned = inserted = props = 0
        arm1 = arm2 = arm3 = 0
        stopped_early = False
        try:
            while True:
                if args.limit is not None and scanned >= args.limit:
                    stopped_early = True
                    break
                if args.max_seconds and time.monotonic() - start > args.max_seconds:
                    LOG.info("INVENTORY stopping: --max-seconds reached")
                    stopped_early = True
                    break
                size = batch_size
                if args.limit is not None:
                    size = min(size, args.limit - scanned)

                with guarded(conn, args.statement_timeout) as cur:
                    cur.execute(_LISTING_BATCH_SQL, {"after_id": after_id, "batch_size": size})
                    rows = cur.fetchall()
                    if not rows:
                        break
                    evidence: list[dict[str, Any]] = []
                    for listing_id, source, coords_source, attempted_at, lat, lng in rows:
                        row = evidence_for_listing(
                            listing_id, source, coords_source, attempted_at,
                            lat, lng, cells, args.epsilon_deg)
                        if row is None:
                            continue
                        arm1 += int(row["arm1_coords_source"])
                        arm2 += int(row["arm2_geocode_attempted"])
                        arm3 += int(row["arm3_geom_matches_cache"])
                        evidence.append(row)
                    after_id = int(rows[-1][0])
                    scanned += len(rows)
                    batch_inserted = len(evidence)
                    if evidence and run_id is not None:
                        cur.executemany(_AFFECTED_INSERT_SQL,
                                        [{**row, "run_id": run_id} for row in evidence])
                        batch_inserted = _inserted(cur, len(evidence))
                    inserted += batch_inserted
                    if run_id is not None:
                        cur.execute(_PROGRESS_SQL, {
                            "scanned": len(rows), "inserted": batch_inserted,
                            "last_id": after_id, "run_id": run_id,
                        })
                LOG.info("INVENTORY progress scanned=%d hit=%d through_id=%d",
                         scanned, inserted, after_id)

            # Arm 5 runs over the whole table, not just this run's rows: the
            # closure of a listing recorded by an earlier partial run is still
            # missing until some run computes it.
            if run_id is not None:
                with guarded(conn, args.statement_timeout) as cur:
                    cur.execute(_PROPS_INSERT_SQL,
                                {"reason_code": _REASON_PROPERTY, "run_id": run_id})
                    props = _inserted(cur, 0)
        except Exception as exc:
            if run_id is not None:
                record_failure(run_id, exc)
            raise

        with conn.cursor() as cur:
            cur.execute(_ARM_COUNTS_SQL)
            t_arm1, t_arm2, t_arm3, t_total = cur.fetchone()
            cur.execute(_CACHE_COUNT_SQL)
            t_cache = int(cur.fetchone()[0])
            cur.execute(_PROPS_COUNT_SQL)
            t_props = int(cur.fetchone()[0])

        if run_id is not None:
            with guarded(conn, args.statement_timeout) as cur:
                cur.execute(_FINISH_SQL, {
                    "status": "stopped" if stopped_early else "completed",
                    "cache_rows_total": len(cache_rows),
                    "cache_rows_with_coordinate": sum(1 for r in cache_rows if r[1] is not None),
                    "arm1": t_arm1, "arm2": t_arm2, "arm3": t_arm3,
                    "arm4": t_cache, "arm5": t_props, "run_id": run_id,
                })

    print(f"INVENTORY {'dry-run ' if args.dry_run else ''}done run={run_id} "
          f"scanned={scanned} new_listing_rows={inserted} new_property_rows={props} "
          f"{'stopped_early' if stopped_early else 'complete'}")
    print(f"INVENTORY this-run arms: arm1_coords_source={arm1} "
          f"arm2_geocode_attempted={arm2} arm3_geom_matches_cache={arm3}")
    print(f"INVENTORY totals: arm1={t_arm1} arm2={t_arm2} arm3={t_arm3} "
          f"arm4_cache={t_cache} arm5_properties={t_props} listings_total={t_total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
