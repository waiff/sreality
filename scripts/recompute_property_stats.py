"""Slice 1 driver -- recompute the canonical `properties` rollup + stats.

Two phases, both idempotent:

1. Attach stragglers. Any `listings` row with `property_id IS NULL` — an
   old-code insert, or a row written by the batched detail-drain — gets its own
   singleton property here, mirroring migration 092. No cross-listing matching
   happens at this step: the old geo Tier-1 spatial probe was removed when
   grouping moved to the street+disposition dedup engine (`toolkit.dedup_engine`
   / `scripts.dedup_engine`), which runs out-of-band and owns ALL merges.

2. Recompute every property from its children. Per property:
     is_active           = bool_or(children.is_active)   (decision #3 rollup)
     source_count        = count(children)
     distinct_site_count = count(distinct children.source)
     first/last_seen_at  = min/max across children
     repr_listing_id     = the active, most-recently-seen child
     category/area/...    + current_price_czk mirror that representative child
     price_drop_count     \\
     price_rise_count      } from the union of all children's snapshots,
     max_price_drop_pct   /  ordered by scraped_at (consecutive-step deltas)
     stats_computed_at   = now()

   For today's singleton properties this reproduces exactly what the
   insert-time path (`scraper.db._ensure_property` / `_cheap_property_rollup`)
   maintains,
   plus the price-history aggregates the wrapper does not compute.

Batched by property-id range so each statement stays well under the
transaction-pooler statement timeout. autocommit=True means each batch
commits independently -- a workflow timeout preserves completed batches.

Two run modes (Phase 3 -- real-time properties):

  * --incremental (cron */5, property_maintenance.yml): attach new stragglers
    (skipping the one-time native-id backfill) + recompute ONLY the properties
    queued in `dirty_properties` by the writers. O(changes), near-real-time.
  * full (default, daily reconcile, recompute_property_stats.yml): attach +
    recompute EVERY property + reconcile childless + clear the queue. The
    self-healing backstop for anything the incremental pass missed.

Usage (typically via the workflows above):

    python -m scripts.recompute_property_stats --batch-size 2000       # full
    python -m scripts.recompute_property_stats --incremental            # dirty-set

Required env var: SUPABASE_DB_URL.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections.abc import Iterator
from typing import Any

LOG = logging.getLogger("recompute_property_stats")


_ATTACH_BACKFILL_NATIVE_ID_SQL = """
    UPDATE listings SET source_id_native = sreality_id::text
    WHERE source_id_native IS NULL
"""

_ATTACH_INSERT_SQL = """
    INSERT INTO properties (
        repr_listing_id, category_main, category_type, disposition,
        area_m2, district, locality, geom, current_price_czk,
        has_balcony, has_parking, has_lift, building_type, condition,
        ownership, furnished, terrace, cellar, garage, category_sub_cb,
        estate_area, usable_area, garden_area, parking_lots,
        is_active, first_seen_at, last_seen_at,
        source_count, distinct_site_count
    )
    SELECT
        l.sreality_id, l.category_main, l.category_type, l.disposition,
        l.area_m2, l.district, l.locality, l.geom, l.price_czk,
        l.has_balcony, l.has_parking, l.has_lift, l.building_type, l.condition,
        l.ownership, l.furnished, l.terrace, l.cellar, l.garage, l.category_sub_cb,
        l.estate_area, l.usable_area, l.garden_area, l.parking_lots,
        l.is_active, l.first_seen_at, l.last_seen_at, 1, 1
    FROM listings l
    WHERE l.property_id IS NULL
"""

_ATTACH_LINK_SQL = """
    UPDATE listings l
    SET property_id = p.id
    FROM properties p
    WHERE p.repr_listing_id = l.sreality_id
      AND l.property_id IS NULL
"""

_RECOMPUTE_BATCH_SQL = """
    WITH batch AS (
      SELECT id FROM properties WHERE id >= %(lo)s AND id < %(hi)s
    ),
    child_agg AS (
      SELECT
        l.property_id              AS pid,
        bool_or(l.is_active)       AS is_active,
        count(*)                   AS source_count,
        count(distinct l.source)   AS distinct_site_count,
        min(l.first_seen_at)       AS first_seen_at,
        max(l.last_seen_at)        AS last_seen_at
      FROM listings l
      JOIN batch b ON b.id = l.property_id
      GROUP BY l.property_id
    ),
    repr AS (
      SELECT DISTINCT ON (l.property_id)
        l.property_id AS pid, l.sreality_id, l.category_main, l.category_type,
        l.disposition, l.area_m2, l.district, l.geom, l.price_czk,
        l.locality, l.has_balcony, l.has_parking, l.has_lift, l.building_type,
        l.condition, l.ownership, l.furnished, l.terrace, l.cellar, l.garage,
        l.category_sub_cb, l.estate_area, l.usable_area, l.garden_area,
        l.parking_lots
      FROM listings l
      JOIN batch b ON b.id = l.property_id
      ORDER BY l.property_id, l.is_active DESC, l.last_seen_at DESC NULLS LAST,
               l.sreality_id DESC
    ),
    prices AS (
      SELECT
        l.property_id AS pid,
        s.price_czk,
        row_number() OVER (
          PARTITION BY l.property_id ORDER BY s.scraped_at, s.id
        ) AS rn
      FROM listing_snapshots s
      JOIN listings l ON l.sreality_id = s.sreality_id
      JOIN batch b ON b.id = l.property_id
      WHERE s.price_czk IS NOT NULL
    ),
    steps AS (
      SELECT
        pid, price_czk,
        lag(price_czk) OVER (PARTITION BY pid ORDER BY rn) AS prev
      FROM prices
    ),
    price_hist AS (
      SELECT
        pid,
        count(*) FILTER (WHERE prev IS NOT NULL AND price_czk < prev) AS drops,
        count(*) FILTER (WHERE prev IS NOT NULL AND price_czk > prev) AS rises,
        max(CASE WHEN prev IS NOT NULL AND price_czk < prev
                 THEN (prev - price_czk)::numeric / prev * 100 END)   AS max_drop_pct
      FROM steps
      GROUP BY pid
    )
    UPDATE properties p SET
      is_active           = ca.is_active,
      source_count        = ca.source_count,
      distinct_site_count = ca.distinct_site_count,
      first_seen_at       = ca.first_seen_at,
      last_seen_at        = ca.last_seen_at,
      repr_listing_id     = r.sreality_id,
      category_main       = r.category_main,
      category_type       = r.category_type,
      disposition         = r.disposition,
      area_m2             = r.area_m2,
      district            = r.district,
      geom                = r.geom,
      current_price_czk   = r.price_czk,
      locality            = r.locality,
      has_balcony         = r.has_balcony,
      has_parking         = r.has_parking,
      has_lift            = r.has_lift,
      building_type       = r.building_type,
      condition           = r.condition,
      ownership           = r.ownership,
      furnished           = r.furnished,
      terrace             = r.terrace,
      cellar              = r.cellar,
      garage              = r.garage,
      category_sub_cb     = r.category_sub_cb,
      estate_area         = r.estate_area,
      usable_area         = r.usable_area,
      garden_area         = r.garden_area,
      parking_lots        = r.parking_lots,
      price_drop_count    = coalesce(ph.drops, 0),
      price_rise_count    = coalesce(ph.rises, 0),
      max_price_drop_pct  = ph.max_drop_pct,
      stats_computed_at   = now()
    FROM child_agg ca
    JOIN repr r ON r.pid = ca.pid
    LEFT JOIN price_hist ph ON ph.pid = ca.pid
    WHERE p.id = ca.pid
"""

# Single-property recompute, derived from the batch SQL by narrowing the `batch`
# CTE to one id. Deriving it (rather than re-writing the body) guarantees the
# inline merge recompute and the hourly batch can never drift apart.
_RECOMPUTE_ONE_SQL = _RECOMPUTE_BATCH_SQL.replace(
    "SELECT id FROM properties WHERE id >= %(lo)s AND id < %(hi)s",
    "SELECT id FROM properties WHERE id = %(pid)s",
)

# Dirty-set recompute (Phase 3), derived the same way: the batch CTE is scoped to
# an explicit id array instead of an id range, so the incremental job recomputes
# exactly the queued properties with the identical body (never drifts from full).
_RECOMPUTE_SCOPED_SQL = _RECOMPUTE_BATCH_SQL.replace(
    "SELECT id FROM properties WHERE id >= %(lo)s AND id < %(hi)s",
    "SELECT id FROM properties WHERE id = ANY(%(ids)s)",
)

# Claim a marked_at-ordered slice of the dirty queue, but only rows dirtied at or
# before a run-start cutoff. A property re-dirtied DURING the run gets a fresh
# marked_at (> cutoff, via the writers' ON CONFLICT DO UPDATE), so it is neither
# claimed here nor deleted below -- it survives for the next pass. That makes the
# working set finite + strictly shrinking, so the drain loop always terminates.
_CLAIM_DIRTY_SQL = """
    SELECT property_id, marked_at FROM dirty_properties
    WHERE marked_at <= %(cutoff)s
    ORDER BY marked_at
    LIMIT %(limit)s
"""

# Delete only the claimed ids that have NOT been re-dirtied since the cutoff.
_DELETE_DIRTY_SQL = """
    DELETE FROM dirty_properties
    WHERE property_id = ANY(%(ids)s) AND marked_at <= %(cutoff)s
"""

# Enqueue the spatially-linked stragglers so the recompute below picks them up.
# Full sweep clears the queue (it recomputed everything), but only rows that
# existed at its start -- anything dirtied mid-sweep is left for the next pass.
_CLEAR_DIRTY_SQL = "DELETE FROM dirty_properties WHERE marked_at <= %(cutoff)s"

# A merge re-points a retired property's children onto the survivor, leaving the
# loser childless. _RECOMPUTE_BATCH_SQL inner-joins listings, so a childless
# property drops out of the UPDATE and keeps stale columns -- merge_properties
# sets the loser is_active=false explicitly, but this guards the general case
# (a partially-failed merge, or any childless active property) so Browse never
# shows a ghost active dot.
_RECONCILE_CHILDLESS_SQL = """
    UPDATE properties p SET is_active = false
    WHERE p.is_active = true
      AND NOT EXISTS (SELECT 1 FROM listings l WHERE l.property_id = p.id)
"""


def recompute_one(conn: Any, property_id: int) -> None:
    """Recompute one property's rollup + stats using the batch job's exact SQL.

    No transaction wrapper, so it nests inside a caller's open transaction
    (e.g. the inline survivor recompute in toolkit.property_identity.merge_properties).
    """
    with conn.cursor() as cur:
        cur.execute(_RECOMPUTE_ONE_SQL, {"pid": property_id})


def _reconcile_childless(conn: Any) -> int:
    with conn.cursor() as cur:
        cur.execute(_RECONCILE_CHILDLESS_SQL)
        return cur.rowcount or 0


def _batch_ranges(max_id: int, batch_size: int) -> Iterator[tuple[int, int]]:
    """Yield half-open [lo, hi) id ranges covering 1..max_id inclusive."""
    if max_id < 1 or batch_size < 1:
        return
    for lo in range(1, max_id + 1, batch_size):
        yield lo, lo + batch_size


def _attach_stragglers(conn: Any, *, skip_native_backfill: bool = False) -> int:
    """Give every property_id-NULL listing its own singleton property.

    The native-id backfill is a one-time legacy fix that scans the whole listings
    table, so the */5 incremental pass skips it (daily full mode runs it). No
    cross-listing matching happens here anymore: the old geo Tier-1 spatial link
    was removed when grouping moved to the street+disposition dedup engine
    (`toolkit.dedup_engine` / `scripts.dedup_engine`), which runs out-of-band.
    Fresh singletons are inserted already-correct (one child, no price history),
    so they need no recompute and are not enqueued dirty.
    """
    with conn.cursor() as cur:
        if not skip_native_backfill:
            cur.execute(_ATTACH_BACKFILL_NATIVE_ID_SQL)
        cur.execute(_ATTACH_INSERT_SQL)
        inserted = cur.rowcount or 0
        cur.execute(_ATTACH_LINK_SQL)
    return inserted


def _drain_dirty(conn: Any, batch_size: int, cutoff: Any) -> int:
    """Recompute every property queued at/before `cutoff`, scoped + batched.

    Crash-safe under autocommit: recompute then delete per batch, so an
    interrupted run simply re-recomputes (idempotent) on the next pass. Always
    terminates -- only rows with marked_at <= cutoff are claimable, the delete
    removes the claimed ones, and a row re-dirtied mid-run moves past the cutoff.
    """
    total = 0
    while True:
        with conn.cursor() as cur:
            cur.execute(_CLAIM_DIRTY_SQL, {"cutoff": cutoff, "limit": batch_size})
            claimed = cur.fetchall()
        if not claimed:
            break
        ids = [int(r[0]) for r in claimed]
        with conn.cursor() as cur:
            cur.execute(_RECOMPUTE_SCOPED_SQL, {"ids": ids})
        with conn.cursor() as cur:
            cur.execute(_DELETE_DIRTY_SQL, {"ids": ids, "cutoff": cutoff})
        total += len(ids)
    return total


def _max_property_id(conn: Any) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(id), 0) FROM properties")
        return int(cur.fetchone()[0])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--batch-size", type=int, default=2000,
        help="Properties recomputed per statement (default 2000).",
    )
    parser.add_argument(
        "--incremental", action="store_true",
        help="Dirty-set mode: attach new stragglers (skip the legacy native-id "
             "backfill) + recompute only queued properties. Default is the full "
             "sweep over every property (the daily reconcile backstop).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report straggler + dirty + property counts and exit without writing.",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.batch_size < 1:
        print("ERROR: --batch-size must be >= 1.", file=sys.stderr)
        return 2

    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    import psycopg

    mode = "incremental" if args.incremental else "full"
    LOG.info(
        "RECOMPUTE config mode=%s batch_size=%d dry_run=%s",
        mode, args.batch_size, args.dry_run,
    )

    started_at = time.monotonic()
    with psycopg.connect(db_url, autocommit=True, prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT now()")
            cutoff = cur.fetchone()[0]

        if args.dry_run:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM listings WHERE property_id IS NULL")
                stragglers = int(cur.fetchone()[0])
                cur.execute("SELECT count(*) FROM dirty_properties")
                dirty = int(cur.fetchone()[0])
                cur.execute("SELECT count(*) FROM properties")
                properties = int(cur.fetchone()[0])
            LOG.info(
                "RECOMPUTE dry-run mode=%s stragglers=%d dirty=%d properties=%d; exit",
                mode, stragglers, dirty, properties,
            )
            return 0

        # Incremental: attach new stragglers, then recompute only the queued
        # (dirty) properties. The full-table sweep is the daily reconcile.
        if args.incremental:
            attached = _attach_stragglers(conn, skip_native_backfill=True)
            recomputed = _drain_dirty(conn, args.batch_size, cutoff)
            elapsed = time.monotonic() - started_at
            LOG.info(
                "RECOMPUTE incremental done attached=%d recomputed=%d elapsed=%.1fs",
                attached, recomputed, elapsed,
            )
            return 0

        attached = _attach_stragglers(conn)
        LOG.info("RECOMPUTE stragglers attached=%d", attached)

        max_id = _max_property_id(conn)
        batches = 0
        for lo, hi in _batch_ranges(max_id, args.batch_size):
            with conn.cursor() as cur:
                cur.execute(_RECOMPUTE_BATCH_SQL, {"lo": lo, "hi": hi})
            batches += 1
            LOG.debug("RECOMPUTE batch=%d-%d done", lo, hi)

        reconciled = _reconcile_childless(conn)
        if reconciled:
            LOG.info(
                "RECOMPUTE reconciled childless=%d (set is_active=false)", reconciled,
            )

        # The full sweep recomputed every property, so clear the dirt that
        # existed at its start; anything dirtied mid-sweep survives for the next
        # incremental pass.
        with conn.cursor() as cur:
            cur.execute(_CLEAR_DIRTY_SQL, {"cutoff": cutoff})

    elapsed = time.monotonic() - started_at
    LOG.info(
        "RECOMPUTE done max_property_id=%d batches=%d elapsed=%.1fs",
        max_id, batches, elapsed,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
