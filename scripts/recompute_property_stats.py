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
     price_rise_count      \\
     max_price_drop_pct     } from the union of all children's snapshots,
     price_change_count*   /  ordered by scraped_at (consecutive-step deltas;
     total_price_change_pct/  the *_30d/_90d/_365d window counts + the signed
                              first-to-last total back migration 173's filters)
     last_change_at      = max(children snapshots.scraped_at) -- "recently changed"
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
        ownership, furnished, terrace, cellar, garage, category_sub_cb, subtype,
        estate_area, usable_area, garden_area, parking_lots,
        ku_id, obec_id, okres_id, region_id, obec, okres, region,
        locality_district_id, locality_region_id, source, energy_rating,
        building_condition_level, apartment_condition_level,
        is_active, first_seen_at, last_seen_at, last_change_at,
        source_count, distinct_site_count
    )
    SELECT
        l.sreality_id, l.category_main, l.category_type, l.disposition,
        l.area_m2, l.district, l.locality, l.geom, l.price_czk,
        l.has_balcony, l.has_parking, l.has_lift, l.building_type, l.condition,
        l.ownership, l.furnished, l.terrace, l.cellar, l.garage, l.category_sub_cb, l.subtype,
        l.estate_area, l.usable_area, l.garden_area, l.parking_lots,
        l.ku_id, l.obec_id, l.okres_id, l.region_id, l.obec, l.okres, l.region,
        l.locality_district_id, l.locality_region_id, l.source, l.energy_rating,
        l.building_condition_level, l.apartment_condition_level,
        l.is_active, l.first_seen_at, l.last_seen_at, l.first_seen_at, 1, 1
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
    -- Every child of the batch's properties, tagged with a per-source TRUST rank
    -- (lower = more reliable). The golden-record CTEs below pick the best value
    -- per field in this trust order, the same spirit as best_street's
    -- sreality-preferred ordering (migration 183), generalised to all fields.
    kids AS (
      SELECT l.*,
        CASE l.source
          WHEN 'sreality'     THEN 1
          WHEN 'bezrealitky'  THEN 2
          WHEN 'idnes'        THEN 3
          WHEN 'mmreality'    THEN 4
          WHEN 'remax'        THEN 5
          WHEN 'maxima'       THEN 6
          WHEN 'ceskereality' THEN 7
          WHEN 'realitymix'   THEN 8
          WHEN 'bazos'        THEN 9
          ELSE 10
        END AS src_rank
      FROM listings l
      JOIN batch b ON b.id = l.property_id
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
    -- GOLDEN RECORD (field-level survivorship). Amenity booleans use bool_or =
    -- three-valued OR-union (any reliable TRUE wins; else any explicit FALSE; else
    -- NULL) — the right rule because a portal that simply doesn't parse an amenity
    -- leaves it NULL, which the MF calc reads as "absent"; presence-wins recovers
    -- it from a sibling that did parse it (validated: of cross-child lift
    -- disagreements only ~2 percent are true-vs-false, the rest NULL-vs-known).
    -- (Keep a literal percent sign out of this comment: psycopg parses the whole
    -- query string for placeholders on every parameterized execute, so a stray
    -- one raises ProgrammingError — see tests/test_sql_placeholders.py.) Scalars
    -- take the best NON-NULL value in source-trust order via
    -- (array_agg(x ORDER BY rank) FILTER (WHERE x IS NOT NULL))[1].
    golden AS (
      SELECT
        k.property_id AS pid,
        bool_or(k.has_lift)     AS has_lift,
        bool_or(k.has_balcony)  AS has_balcony,
        bool_or(k.has_parking)  AS has_parking,
        bool_or(k.terrace)      AS terrace,
        bool_or(k.garage)       AS garage,
        bool_or(k.cellar)       AS cellar,
        (array_agg(k.area_m2 ORDER BY k.src_rank, k.is_active DESC,
            k.last_seen_at DESC NULLS LAST, k.sreality_id DESC)
            FILTER (WHERE k.area_m2 IS NOT NULL))[1]      AS area_m2,
        (array_agg(k.usable_area ORDER BY k.src_rank, k.is_active DESC,
            k.last_seen_at DESC NULLS LAST, k.sreality_id DESC)
            FILTER (WHERE k.usable_area IS NOT NULL))[1]  AS usable_area,
        (array_agg(k.estate_area ORDER BY k.src_rank, k.is_active DESC,
            k.last_seen_at DESC NULLS LAST, k.sreality_id DESC)
            FILTER (WHERE k.estate_area IS NOT NULL))[1]  AS estate_area,
        (array_agg(k.garden_area ORDER BY k.src_rank, k.is_active DESC,
            k.last_seen_at DESC NULLS LAST, k.sreality_id DESC)
            FILTER (WHERE k.garden_area IS NOT NULL))[1]  AS garden_area,
        (array_agg(k.parking_lots ORDER BY k.src_rank, k.is_active DESC,
            k.last_seen_at DESC NULLS LAST, k.sreality_id DESC)
            FILTER (WHERE k.parking_lots IS NOT NULL))[1] AS parking_lots,
        (array_agg(k.building_type ORDER BY k.src_rank, k.is_active DESC,
            k.last_seen_at DESC NULLS LAST, k.sreality_id DESC)
            FILTER (WHERE k.building_type IS NOT NULL))[1] AS building_type,
        (array_agg(k.condition ORDER BY k.src_rank, k.is_active DESC,
            k.last_seen_at DESC NULLS LAST, k.sreality_id DESC)
            FILTER (WHERE k.condition IS NOT NULL))[1]    AS condition,
        (array_agg(k.ownership ORDER BY k.src_rank, k.is_active DESC,
            k.last_seen_at DESC NULLS LAST, k.sreality_id DESC)
            FILTER (WHERE k.ownership IS NOT NULL))[1]    AS ownership,
        (array_agg(k.furnished ORDER BY k.src_rank, k.is_active DESC,
            k.last_seen_at DESC NULLS LAST, k.sreality_id DESC)
            FILTER (WHERE k.furnished IS NOT NULL))[1]    AS furnished,
        (array_agg(k.energy_rating ORDER BY k.src_rank, k.is_active DESC,
            k.last_seen_at DESC NULLS LAST, k.sreality_id DESC)
            FILTER (WHERE k.energy_rating IS NOT NULL))[1] AS energy_rating
      FROM kids k
      GROUP BY k.property_id
    ),
    -- Geom + admin territory (incl. the MF rent-map join key ku_id) from the best
    -- CZ-located child: a child WITH a Czech territory (obec_id NOT NULL) wins over
    -- a foreign/uncoded one, then by source trust + recency. Keeps geom and every
    -- territory field consistent (one child), and prefers a CZ coordinate so a
    -- merged property whose repr happens to carry an off/foreign point still
    -- resolves its MF territory. Falls back to the best child overall (NULL
    -- territory) for a genuinely foreign property.
    best_geo AS (
      SELECT DISTINCT ON (k.property_id)
        k.property_id AS pid, k.geom, k.locality, k.district,
        k.ku_id, k.obec_id, k.okres_id, k.region_id, k.obec, k.okres, k.region,
        k.locality_district_id, k.locality_region_id
      FROM kids k
      ORDER BY k.property_id, (k.obec_id IS NOT NULL) DESC, k.src_rank,
               k.is_active DESC, k.last_seen_at DESC NULLS LAST, k.sreality_id DESC
    ),
    repr AS (
      SELECT DISTINCT ON (l.property_id)
        l.property_id AS pid, l.sreality_id, l.category_main, l.category_type,
        l.disposition, l.price_czk,
        l.category_sub_cb, l.subtype,
        l.building_condition_level, l.apartment_condition_level, l.source
      FROM listings l
      JOIN batch b ON b.id = l.property_id
      ORDER BY l.property_id, l.is_active DESC, l.last_seen_at DESC NULLS LAST,
               l.sreality_id DESC
    ),
    -- Group-best street (migration 183): the best non-null child street,
    -- sreality-preferred (structured + most reliable), then active + most
    -- recently seen. Lets place_search_text match a street even when the
    -- representative listing lacks one. LEFT-JOINed below -> NULL when no child
    -- carries a street.
    best_street AS (
      SELECT DISTINCT ON (l.property_id)
        l.property_id AS pid, l.street
      FROM listings l
      JOIN batch b ON b.id = l.property_id
      WHERE l.street IS NOT NULL AND l.street <> ''
      ORDER BY l.property_id, (l.source = 'sreality') DESC,
               l.is_active DESC, l.last_seen_at DESC NULLS LAST, l.sreality_id DESC
    ),
    prices AS (
      SELECT
        l.property_id AS pid,
        s.price_czk,
        s.scraped_at,
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
        pid, price_czk, scraped_at, rn,
        lag(price_czk) OVER (PARTITION BY pid ORDER BY rn) AS prev
      FROM prices
    ),
    -- Windowed change counts (migration 173): a "change" is any consecutive
    -- pair where the price moved, dated by the later snapshot's scraped_at.
    -- The windowed counts decay as events age out, so they are only as fresh
    -- as the last recompute of the row -- the daily full sweep is the bound.
    price_hist AS (
      SELECT
        pid,
        count(*) FILTER (WHERE prev IS NOT NULL AND price_czk < prev) AS drops,
        count(*) FILTER (WHERE prev IS NOT NULL AND price_czk > prev) AS rises,
        count(*) FILTER (WHERE prev IS NOT NULL AND price_czk <> prev) AS changes,
        count(*) FILTER (WHERE prev IS NOT NULL AND price_czk <> prev
                         AND scraped_at >= now() - interval '30 days')  AS changes_30d,
        count(*) FILTER (WHERE prev IS NOT NULL AND price_czk <> prev
                         AND scraped_at >= now() - interval '90 days')  AS changes_90d,
        count(*) FILTER (WHERE prev IS NOT NULL AND price_czk <> prev
                         AND scraped_at >= now() - interval '365 days') AS changes_365d,
        max(CASE WHEN prev IS NOT NULL AND price_czk < prev
                 THEN (prev - price_czk)::numeric / prev * 100 END)   AS max_drop_pct,
        (array_agg(price_czk ORDER BY rn))[1]      AS first_price,
        (array_agg(price_czk ORDER BY rn DESC))[1] AS last_price,
        count(*)                                   AS price_points
      FROM steps
      GROUP BY pid
    ),
    -- Last content change = newest snapshot across all children. Snapshots are
    -- inserted only on a content-hash change (rule #2), so this is the "recently
    -- changed" timestamp the Browse filter reads (exposed via properties_public,
    -- migration 158). Includes price-less snapshots (any field change), so it is
    -- a separate CTE from `prices` above (which filters price_czk IS NOT NULL).
    changes AS (
      SELECT l.property_id AS pid, max(s.scraped_at) AS last_change_at
      FROM listing_snapshots s
      JOIN listings l ON l.sreality_id = s.sreality_id
      JOIN batch b ON b.id = l.property_id
      GROUP BY l.property_id
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
      area_m2             = g.area_m2,
      district            = bg.district,
      geom                = bg.geom,
      current_price_czk   = r.price_czk,
      locality            = bg.locality,
      street              = bs.street,
      has_balcony         = g.has_balcony,
      has_parking         = g.has_parking,
      has_lift            = g.has_lift,
      building_type       = g.building_type,
      condition           = g.condition,
      ownership           = g.ownership,
      furnished           = g.furnished,
      terrace             = g.terrace,
      cellar              = g.cellar,
      garage              = g.garage,
      category_sub_cb     = r.category_sub_cb,
      subtype             = r.subtype,
      estate_area         = g.estate_area,
      usable_area         = g.usable_area,
      garden_area         = g.garden_area,
      parking_lots        = g.parking_lots,
      ku_id                     = bg.ku_id,
      region_id                 = bg.region_id,
      okres_id                  = bg.okres_id,
      obec_id                   = bg.obec_id,
      obec                      = bg.obec,
      okres                     = bg.okres,
      region                    = bg.region,
      building_condition_level  = r.building_condition_level,
      apartment_condition_level = r.apartment_condition_level,
      energy_rating             = g.energy_rating,
      source                    = r.source,
      locality_district_id      = bg.locality_district_id,
      locality_region_id        = bg.locality_region_id,
      price_drop_count    = coalesce(ph.drops, 0),
      price_rise_count    = coalesce(ph.rises, 0),
      max_price_drop_pct  = ph.max_drop_pct,
      price_change_count      = coalesce(ph.changes, 0),
      price_change_count_30d  = coalesce(ph.changes_30d, 0),
      price_change_count_90d  = coalesce(ph.changes_90d, 0),
      price_change_count_365d = coalesce(ph.changes_365d, 0),
      total_price_change_pct  = CASE
          WHEN ph.price_points >= 2 AND ph.first_price > 0
          THEN (ph.last_price - ph.first_price)::numeric / ph.first_price * 100
      END,
      last_change_at      = coalesce(ch.last_change_at, ca.first_seen_at),
      stats_computed_at   = now()
    FROM child_agg ca
    JOIN repr r ON r.pid = ca.pid
    JOIN golden g ON g.pid = ca.pid
    JOIN best_geo bg ON bg.pid = ca.pid
    LEFT JOIN best_street bs ON bs.pid = ca.pid
    LEFT JOIN price_hist ph ON ph.pid = ca.pid
    LEFT JOIN changes ch ON ch.pid = ca.pid
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


def recompute_mf_one(conn: Any, property_id: int) -> None:
    """Refresh ONE property's MF reference rent/yield from its golden record.

    Pairs with recompute_one: rebuild the golden columns, then recompute MF on
    them so a merge/unmerge survivor is never one mf-recompute cycle stale.
    Calls the same recompute_property_mf() DB function the hourly job uses.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT public.recompute_property_mf(ARRAY[%s]::bigint[])",
            (property_id,),
        )


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
