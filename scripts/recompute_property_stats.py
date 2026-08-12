"""Slice 1 driver -- recompute the canonical `properties` rollup + stats.

Two phases, both idempotent:

1. Attach stragglers. Any `listings` row with `property_id IS NULL` — an
   old-code insert, or a row written by the batched detail-drain — gets its own
   singleton property here, mirroring migration 092. No cross-listing matching
   happens at this step, ever: grouping is out-of-band and, since the 2026-08
   "NEW DEDUP" cutoff, operator-ordered only (CLAUDE.md rule 15).

2. Recompute every property from its children. Per property:
     is_active           = bool_or(children.is_active)   (decision #3 rollup)
     source_count        = count(children)
     distinct_site_count = count(distinct children.source)
     first/last_seen_at  = min/max across children
     repr_listing_id     = the active, most-recently-seen child
     category/area/...    + current_price_czk mirror that representative child
     price_drop_count     \\
     price_rise_count      \\  consecutive-step deltas computed WITHIN EACH
     max_price_drop_pct     }  CHILD's own snapshot series, then summed across
     price_change_count*   /   children (+ the *_30d/_90d/_365d window counts)
     total_price_change_pct =  signed first-to-last of the REPRESENTATIVE
                               child's series only
     last_change_at      = max(children snapshots.scraped_at) -- "recently changed"
     stats_computed_at   = now()

   PRICE SERIES GRAIN (changed 2026-08; migration 173 introduced these columns).
   The window PARTITIONs by listing, NOT by property. Interleaving every child's
   snapshots into one property-level series — the original behaviour — makes a
   multi-portal property whose portals quote slightly different asking prices
   register a "change" on every single scrape, and simultaneously HIDES real
   cuts when the other portal's unchanged reading lands between two readings of
   the one that moved. Both directions were confirmed market-wide across every
   multi-source property with priced snapshots.

   `total_price_change_pct` is anchored on the representative child because
   `current_price_czk` is that same child's price: a headline price and a delta
   drawn from different series is how a card ends up describing two different
   numbers. The cost is a narrower claim — NULL when the representative alone
   has fewer than two priced snapshots, even if a sibling has a longer history.
   These columns also back Browse/Watchdog filters (`price_change_count_min`,
   `total_price_change_pct`), so cohort membership shifts after the backfill.

   For today's singleton properties this reproduces exactly what the
   insert-time path (`scraper.db._ensure_property` / `_cheap_property_rollup`)
   maintains,
   plus the price-history aggregates the wrapper does not compute.

Batched by property-id range so each statement stays well under the
transaction-pooler statement timeout. autocommit=True means each batch
commits independently -- a workflow timeout preserves completed batches.

Liveness (2026-08-06 incident): the maintenance lease is a SHORT (15 min) TTL
heartbeat-renewed every batch/slice — never a runtime-sized grant — so a
SIGKILL at any point freezes maintenance for minutes, not hours. The full
sweep also takes a --max-seconds wall-clock budget and CLEAN-STOPS at a batch
boundary when it runs out: finalize what was covered, release the lease, exit
RED (GH reports a timeout kill as `cancelled`, which alerts nobody). The
`property_maintenance` check in scripts/verify_pipeline.py watches the
resulting staleness independently.

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
import signal
import sys
import time
from collections.abc import Callable, Iterator
from typing import Any

from scraper import db

LOG = logging.getLogger("recompute_property_stats")


def _sigterm_to_systemexit(signum: int, frame: Any) -> None:
    raise SystemExit(143)


_ATTACH_BACKFILL_NATIVE_ID_SQL = """
    UPDATE listings SET source_id_native = sreality_id::text
    WHERE source_id_native IS NULL
"""

_ATTACH_INSERT_SQL = """
    INSERT INTO properties (
        repr_listing_id, repr_listing_ref_id, category_main, category_type, disposition,
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
        l.sreality_id, l.id, l.category_main, l.category_type, l.disposition,
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
    WHERE p.repr_listing_ref_id = l.id
      AND l.property_id IS NULL
"""

_RECOMPUTE_BATCH_SQL = """
    WITH batch AS (
      SELECT id FROM properties WHERE id >= %(lo)s AND id < %(hi)s
    ),
    -- Every child of the batch's properties, tagged with the shared per-source
    -- TRUST rank (source_trust_rank, migration 311; lower = more reliable). The
    -- golden-record CTEs below pick the best value per field in this trust order.
    kids AS (
      SELECT l.*, source_trust_rank(l.source) AS src_rank
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
        l.property_id AS pid, l.sreality_id, l.id AS listing_ref_id,
        l.category_main, l.category_type,
        l.disposition, l.price_czk,
        l.category_sub_cb, l.subtype,
        l.building_condition_level, l.apartment_condition_level, l.source
      FROM listings l
      JOIN batch b ON b.id = l.property_id
      -- Representative row = the property's DISPLAY listing (drives price,
      -- disposition, category). Active-first so a live listing represents
      -- current state (never a delisted sibling's stale price), then the shared
      -- trust order (migration 311) as the tiebreak among equally-active
      -- siblings — replacing the old bare sreality_id DESC, whose sign made the
      -- pick arbitrary across portals. Split-picker (property_identity.py) mirrors
      -- this. NB: the golden-record field CTEs above are trust-FIRST instead —
      -- a field's best-known value should come from the most trusted source even
      -- if that listing later delisted; the two goals legitimately differ.
      ORDER BY l.property_id, l.is_active DESC, source_trust_rank(l.source),
               l.last_seen_at DESC NULLS LAST, l.sreality_id DESC
    ),
    -- Group-best street (migration 183): the best non-null child street, in the
    -- shared source-trust order (migration 311 — was a bare source='sreality'
    -- boolean), then active + most recently seen. Lets place_search_text match a
    -- street even when the representative listing lacks one. LEFT-JOINed below ->
    -- NULL when no child carries a street.
    best_street AS (
      SELECT DISTINCT ON (l.property_id)
        l.property_id AS pid, l.street
      FROM listings l
      JOIN batch b ON b.id = l.property_id
      WHERE l.street IS NOT NULL AND l.street <> ''
      ORDER BY l.property_id, source_trust_rank(l.source),
               l.is_active DESC, l.last_seen_at DESC NULLS LAST, l.sreality_id DESC
    ),
    -- PER-LISTING price series. The window PARTITIONs by listing, not by
    -- property: a multi-portal property's children are independent asking-price
    -- streams, and interleaving them by scraped_at (as this did until now) makes
    -- every alternating read look like a price change. A property listed at
    -- 5.0M on one portal and 5.2M on another registered a "change" on EVERY
    -- scrape, inflating price_change_count without bound; the same interleaving
    -- also HID real changes, because a genuine cut could be masked by the other
    -- portal's unchanged price landing between the two readings. Measured
    -- market-wide over all multi-source properties with priced snapshots, this
    -- cut both ways. A change is now only ever a change WITHIN one listing.
    prices AS (
      SELECT
        l.property_id AS pid,
        s.listing_id,
        s.price_czk,
        s.scraped_at,
        row_number() OVER (
          PARTITION BY s.listing_id ORDER BY s.scraped_at, s.id
        ) AS rn
      FROM listing_snapshots s
      JOIN listings l ON l.id = s.listing_id
      JOIN batch b ON b.id = l.property_id
      WHERE s.price_czk IS NOT NULL
    ),
    steps AS (
      SELECT
        pid, listing_id, price_czk, scraped_at, rn,
        lag(price_czk) OVER (PARTITION BY listing_id ORDER BY rn) AS prev
      FROM prices
    ),
    -- Endpoints of each child's own series.
    listing_span AS (
      SELECT
        pid, listing_id,
        (array_agg(price_czk ORDER BY rn))[1]      AS first_price,
        (array_agg(price_czk ORDER BY rn DESC))[1] AS last_price,
        count(*)                                   AS price_points
      FROM prices
      GROUP BY pid, listing_id
    ),
    -- The property's headline delta is anchored on the REPRESENTATIVE child —
    -- the same listing whose price becomes properties.current_price_czk below.
    -- That coupling is the point: a delta computed over a different series than
    -- the displayed price is how a card ends up quoting a headline price and a
    -- drop that describe two different numbers. (No literal percent sign in this
    -- comment on purpose -- prose percent inside executed SQL is an
    -- `incomplete placeholder` crash in psycopg; tests/test_sql_placeholders.py
    -- guards it.) NULL when the representative has fewer than two priced
    -- snapshots -- a narrower claim than the old any-child version, but a true one.
    repr_span AS (
      SELECT ls.pid, ls.first_price, ls.last_price, ls.price_points
      FROM listing_span ls
      JOIN repr r ON r.listing_ref_id = ls.listing_id
    ),
    -- Windowed change counts (migration 173): a "change" is any consecutive
    -- pair WITHIN A CHILD where the price moved, dated by the later snapshot's
    -- scraped_at, then summed across the property's children — a change on any
    -- portal is a change for the property. The windowed counts decay as events
    -- age out, so they are only as fresh as the last recompute of the row --
    -- the daily full sweep is the bound.
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
                 THEN (prev - price_czk)::numeric / prev * 100 END)   AS max_drop_pct
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
      JOIN listings l ON l.id = s.listing_id
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
      repr_listing_ref_id = r.listing_ref_id,
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
          WHEN rs.price_points >= 2 AND rs.first_price > 0
          THEN (rs.last_price - rs.first_price)::numeric / rs.first_price * 100
      END,
      last_change_at      = coalesce(ch.last_change_at, ca.first_seen_at),
      stats_computed_at   = now()
    FROM child_agg ca
    JOIN repr r ON r.pid = ca.pid
    JOIN golden g ON g.pid = ca.pid
    JOIN best_geo bg ON bg.pid = ca.pid
    LEFT JOIN best_street bs ON bs.pid = ca.pid
    LEFT JOIN price_hist ph ON ph.pid = ca.pid
    LEFT JOIN repr_span rs ON rs.pid = ca.pid
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

# The budget-exhausted variant: a sweep that stops early has only recomputed
# ids below its high-water mark, so clearing the GLOBAL pre-cutoff queue would
# erase the recompute signal for unswept ids — those rows would stay stale
# until the next FULL sweep instead of being healed by the next incremental
# pass minutes later. Scope the delete to the swept range.
_CLEAR_DIRTY_SWEPT_SQL = (
    "DELETE FROM dirty_properties "
    "WHERE marked_at <= %(cutoff)s AND property_id < %(hi)s"
)

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

# Written ONLY when a walk covered every id — the O(1) liveness signal the
# `property_maintenance` health check reads. Per-row stats_computed_at cannot
# serve that role: min() over 620k properties with a listings semi-join
# measured ~3.5 min live, and a check that heavy would blow the hourly acute
# lane's own 5-min job timeout — recreating the silent-`cancelled` failure
# mode it exists to catch. A dead, killed, or chronically-incomplete sweep
# shows up here as a stale stamp within hours, however the process died.
_STAMP_SWEEP_COMPLETE_SQL = """
    INSERT INTO app_settings (key, value, updated_by)
    VALUES ('property_sweep_last_complete',
            jsonb_build_object(
                'completed_at', now(),
                'max_property_id', %(max_id)s::bigint,
                'batches', %(batches)s::int,
                'elapsed_s', %(elapsed_s)s::numeric),
            'recompute_property_stats')
    ON CONFLICT (key) DO UPDATE
      SET value = excluded.value, updated_at = now(),
          updated_by = excluded.updated_by
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


def _run_recompute_statement(conn: Any, sql: str, params: dict[str, Any]) -> None:
    """One recompute statement under the raised per-statement ceiling.

    Explicit transaction so SET LOCAL takes effect (it silently no-ops in
    autocommit); the transaction spans exactly this one statement, so the
    batch-commits-independently crash-safety property is unchanged."""
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = '{_BATCH_STATEMENT_TIMEOUT}'")
            cur.execute(sql, params)


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
    was removed when grouping moved out-of-band, and grouping is now
    operator-ordered only (CLAUDE.md rule 15). Fresh singletons are inserted already-correct (one child, no price history),
    so they need no recompute and are not enqueued dirty.
    """
    with conn.cursor() as cur:
        if not skip_native_backfill:
            cur.execute(_ATTACH_BACKFILL_NATIVE_ID_SQL)
        cur.execute(_ATTACH_INSERT_SQL)
        inserted = cur.rowcount or 0
        cur.execute(_ATTACH_LINK_SQL)
    return inserted


def _drain_dirty(
    conn: Any, batch_size: int, cutoff: Any,
    renew: Any = None,
) -> int:
    """Recompute every property queued at/before `cutoff`, scoped + batched.

    Crash-safe under autocommit: recompute then delete per batch, so an
    interrupted run simply re-recomputes (idempotent) on the next pass. Always
    terminates -- only rows with marked_at <= cutoff are claimable, the delete
    removes the claimed ones, and a row re-dirtied mid-run moves past the cutoff.

    `renew` (a zero-arg callable) is invoked once per claimed slice so a long
    drain — e.g. the backlog after a maintenance freeze, or the nine-portal
    enqueue volume post-#971 — heartbeats its 15-min lease instead of silently
    outliving it.
    """
    total = 0
    while True:
        if renew is not None:
            renew()
        with conn.cursor() as cur:
            cur.execute(_CLAIM_DIRTY_SQL, {"cutoff": cutoff, "limit": batch_size})
            claimed = cur.fetchall()
        if not claimed:
            break
        ids = [int(r[0]) for r in claimed]
        _run_recompute_statement(conn, _RECOMPUTE_SCOPED_SQL, {"ids": ids})
        with conn.cursor() as cur:
            cur.execute(_DELETE_DIRTY_SQL, {"ids": ids, "cutoff": cutoff})
        total += len(ids)
    return total


def _max_property_id(conn: Any) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(id), 0) FROM properties")
        return int(cur.fetchone()[0])


# A single-row LEASE serializes EVERY property-maintenance writer: the GH
# incremental cron, the daily full sweep, AND the realtime worker's maintenance
# lane (property_maintenance_lease, migration 279). Claimed by ONE atomic
# UPDATE ... RETURNING, so it is sound over the transaction-mode pooler —
# unlike a session advisory lock, whose lock/unlock statements can land on
# DIFFERENT pooled backends (the #716 defect: the unlock silently no-ops and
# the lock strands, skipping every future pass). Expiry self-heals a crashed
# holder. Incremental callers TRY the lease and skip when held (the next tick
# is seconds away); the daily full sweep RETRIES (bounded) until it holds it —
# the backstop must not be skipped.
#
# ONE short TTL, HEARTBEAT-renewed between statements. The full sweep used to
# take a single 3-hour grant sized to its whole runtime and release it in a
# `finally` — but a GH Actions timeout kill escalates SIGINT→SIGTERM→SIGKILL
# faster than the release round-trip, so every timeout stranded the lease and
# froze ALL property maintenance (worker lane + both crons) for hours
# (2026-08-06 incident: 5 kills in 4 days, each a multi-hour freeze). Cleanup-
# on-death cannot be relied on; cheap-death can: every writer takes the same
# 15-minute TTL and re-grants it (the `holder = %(holder)s` arm below, the
# notification matcher's sticky-holder pattern from migration 366) between
# batches, so a kill at ANY point strands the row for at most 15 minutes.
# Renewal only runs between statements on this autocommit connection, so the
# TTL must comfortably exceed one statement's worst case — recompute
# statements run under the explicit _BATCH_STATEMENT_TIMEOUT (10 min) ceiling
# — 15 min is that margin, not a renewal cadence.
_LEASE_TTL = "15 minutes"
_LEASE_RETRY_SECONDS = 10.0
# A dispatched sweep that cannot get the lease within this budget fails RED
# instead of burning its whole job retrying (observed 2026-08-06: a 30-min run
# spent 100% of its budget in _wait_lease against a dead holder's 3h grant).
# Every holder now renews a 15-min TTL, so 20 min of waiting means something
# is genuinely wrong, not merely slow.
_MAX_LEASE_WAIT_SECONDS = 1200.0

# Ceiling for --max-seconds. The workflow's timeout-minutes (130) is sized as
# budget + one in-flight batch + prelude + finalize headroom FOR THIS
# CEILING; an unclamped dispatch input above it would let the runner
# SIGKILL a healthy sweep before its clean-stop fires — a silent `cancelled`,
# the exact mode this script exists to eliminate. Raising the ceiling means
# raising timeout-minutes in the same change.
#
# 6000s (100 min), raised from 4200s: the six sweeps to 2026-08-10 measured
# 3502/3777/4044/3890/3626s — 83-96% of the old 4200s ceiling, i.e. one bad day
# from clean-stopping RED with the corpus still growing (~2k properties/day).
# 6000s puts the observed worst case (4044s) at ~67% and leaves a full 30 min of
# growth headroom. Sized against that regime deliberately: the 2026-08-12 sweep
# finished in 508s (avg batch 1.6s vs 11-13s) after the 08-11 Supabase restart,
# but one post-restart datapoint is not a new baseline.
_MAX_BUDGET_SECONDS = 6000.0

# Per-recompute-statement ceiling, applied via SET LOCAL inside an explicit
# transaction (the repo's layered-timeout pattern; SET LOCAL no-ops without
# conn.transaction() on an autocommit connection). The pooler's ~2-min default
# proved too tight for the post-#971 batch SQL on deep-history batches: the
# 2026-08-06 10:09Z run's FIRST batch (ids 1-2001, the oldest sreality
# listings) was killed at ~3.5 min while later-id batches run in seconds.
# MUST stay comfortably under _LEASE_TTL (15 min): renewal only fires between
# statements, so one statement's worst case is the longest possible renewal
# gap.
_BATCH_STATEMENT_TIMEOUT = "10min"

# Retry budget for ONE recompute batch (db.run_resilient defaults to 4). A batch
# runs under the 10-min ceiling above, so four attempts could burn 40 minutes
# inside a single loop iteration — the deadline is only checked at a batch
# BOUNDARY, so that would sail past the workflow's outer timeout and re-create
# the silent `cancelled` this script exists to eliminate. Two attempts keeps the
# useful part of the budget (a dropped connection or a passing lock wait replays
# once) and bounds the in-flight worst case at 2 x _BATCH_STATEMENT_TIMEOUT,
# which is what the workflow's timeout-minutes is sized for. A range that times
# out twice is the poisoned range the error log names, not a blip.
_BATCH_RESILIENT_ATTEMPTS = 2

_TRY_LEASE_SQL = """
    UPDATE property_maintenance_lease
       SET holder = %(holder)s, expires_at = now() + %(lease)s::interval
     WHERE id = 1
       AND (holder IS NULL OR expires_at < now() OR holder = %(holder)s)
    RETURNING 1
"""

_RELEASE_LEASE_SQL = """
    UPDATE property_maintenance_lease
       SET holder = NULL, expires_at = NULL
     WHERE id = 1 AND holder = %(holder)s
"""


def _new_holder(kind: str) -> str:
    import uuid

    return f"{kind}:{uuid.uuid4()}"


def _try_lease(conn: Any, holder: str, lease: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(_TRY_LEASE_SQL, {"holder": holder, "lease": lease})
        return cur.fetchone() is not None


def _renew_lease(conn: Any, holder: str) -> None:
    """Heartbeat: re-grant our own lease, pushing expiry out one TTL.

    Raises if the re-grant misses — that means the lease expired mid-work and
    ANOTHER writer took it, so continuing would run two recomputes concurrently.
    That is idempotent-safe but wasteful, and it means this process stalled for
    >15 min on a single statement, which is itself worth a red run.
    """
    if not _try_lease(conn, holder, _LEASE_TTL):
        raise RuntimeError(
            "maintenance lease lost mid-work (expired and re-claimed by another "
            "writer) — aborting rather than recomputing concurrently"
        )


def _wait_lease(
    conn: Any, holder: str, lease: str,
    max_wait_seconds: float = _MAX_LEASE_WAIT_SECONDS,
) -> None:
    # Wall-clock anchored: the CAS round trips themselves can be slow exactly
    # when this path runs (degraded DB), and counting only the sleeps would
    # let the wait silently outgrow the job budget.
    entered = time.monotonic()
    while not _try_lease(conn, holder, lease):
        waited = time.monotonic() - entered
        if waited >= max_wait_seconds:
            raise RuntimeError(
                f"maintenance lease still held after {waited:.0f}s of waiting — "
                "failing RED instead of burning the job budget; every holder "
                "renews a 15-min TTL, so this indicates a real fault"
            )
        LOG.info("MAINTENANCE lease held by another writer; retrying in %.0fs",
                 _LEASE_RETRY_SECONDS)
        time.sleep(_LEASE_RETRY_SECONDS)


def _release_lease(conn: Any, holder: str) -> None:
    """Best-effort release. Callers run this from a `finally:`, so a raise here (a
    dead connection makes even `conn.cursor()` throw) would replace the real failure
    with a crash-during-cleanup. The lease's correctness never rested on the release
    landing: the holder-guarded CAS plus the short renewed TTL are the guarantee, and
    a missed release costs at most one TTL of frozen maintenance."""
    try:
        with conn.cursor() as cur:
            cur.execute(_RELEASE_LEASE_SQL, {"holder": holder})
    except Exception:  # noqa: BLE001 - best-effort; the TTL is the real guarantee
        LOG.warning("MAINTENANCE: lease release failed (holder=%s) — self-heals when "
                    "the %s TTL expires", holder, _LEASE_TTL)


def run_incremental_pass(conn: Any, batch_size: int = 2000) -> dict[str, Any]:
    """ONE incremental property-maintenance pass — THE shared implementation
    behind the GH cron (property_maintenance.yml) and the realtime worker's
    maintenance lane: attach new stragglers (skip the legacy native-id
    backfill) + recompute the dirty set.
    Serialized by the maintenance lease; a caller that
    finds the lease held returns {"skipped": True} — the concurrent pass is
    doing the same work, and the next tick is seconds away. A pass normally
    runs seconds; the 15-minute lease is a wide margin, and its expiry
    self-heals a crashed holder.
    """
    holder = _new_holder("incremental")
    if not _try_lease(conn, holder, _LEASE_TTL):
        return {"skipped": True, "attached": 0, "recomputed": 0}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT now()")
            cutoff = cur.fetchone()[0]
        attached = _attach_stragglers(conn, skip_native_backfill=True)
        recomputed = _drain_dirty(
            conn, batch_size, cutoff,
            renew=lambda: _renew_lease(conn, holder),
        )
        return {"skipped": False, "attached": attached, "recomputed": recomputed}
    finally:
        _release_lease(conn, holder)


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
    parser.add_argument(
        "--max-seconds", type=float, default=6000.0,
        help="Full-sweep wall-clock budget (default 6000). On exhaustion the "
             "sweep clean-stops at a batch boundary, finalizes only what it "
             "covered, releases the lease, and exits RED (1) — a visible "
             "failure instead of a silent timeout-minutes `cancelled` kill. "
             f"Clamped to {int(_MAX_BUDGET_SECONDS)}s: the workflow's "
             "timeout-minutes backstop is sized for that ceiling, and a "
             "larger budget would let the runner SIGKILL the job before the "
             "clean-stop fires (raising both requires editing the yml).",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    # SIGTERM (the middle step of GH's SIGINT→SIGTERM→SIGKILL cancel ladder)
    # defaults to instant death — no finally, lease stranded. Route it through
    # SystemExit so the release path gets its chance; the short renewed TTL is
    # the guarantee for when even this loses the race.
    signal.signal(signal.SIGTERM, _sigterm_to_systemexit)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.batch_size < 1:
        print("ERROR: --batch-size must be >= 1.", file=sys.stderr)
        return 2

    # Explicit check before db.connect(): database_url() would raise a bare
    # RuntimeError instead of this friendly message + exit 2.
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    mode = "incremental" if args.incremental else "full"
    LOG.info(
        "RECOMPUTE config mode=%s batch_size=%d dry_run=%s",
        mode, args.batch_size, args.dry_run,
    )

    def reconnect() -> Any:
        return db.connect(db_url)

    started_at = time.monotonic()
    # db.connect() instead of a bare psycopg.connect(): same autocommit +
    # prepare_threshold=None, PLUS TCP keepalives and a 3-attempt handshake retry.
    # The full sweep holds this ONE connection for up to the whole budget, so a
    # pooler recycle or a Supabase restart (2026-08-11, an AdminShutdown 338s in)
    # used to kill the run outright.
    with db.connect(db_url) as conn:
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
        # Shared implementation with the realtime worker's maintenance lane.
        if args.incremental:
            stats = run_incremental_pass(conn, args.batch_size)
            elapsed = time.monotonic() - started_at
            if stats["skipped"]:
                LOG.info(
                    "RECOMPUTE incremental skipped: another maintenance pass "
                    "holds the lock (worker lane or daily sweep)",
                )
                return 0
            LOG.info(
                "RECOMPUTE incremental done attached=%d recomputed=%d elapsed=%.1fs",
                stats["attached"], stats["recomputed"], elapsed,
            )
            return 0

        # The full sweep RETRIES for the lease (it is the daily backstop and
        # must not be skipped) — but boundedly, failing RED rather than burning
        # the whole job against a stuck holder. Acquisition happens INSIDE the
        # try: a kill between grant and try-entry used to strand a fresh lease
        # with zero work done; the holder-guarded release no-ops when the wait
        # never succeeded, so this ordering is safe.
        holder = _new_holder("full")
        incomplete_at: int | None = None

        def step(op: Callable[[Any], Any], label: str,
                 attempts: int | None = None) -> Any:
            """db.run_resilient with the conn rebinding its docstring demands (it may
            hand back a FRESH connection after a pooler drop). Every op below is
            idempotent — recompute statements are pure latest-wins recomputes and the
            dirty-clear / stamp are keyed writes, so a replay re-commits identically."""
            nonlocal conn
            budget = {} if attempts is None else {"attempts": attempts}
            result, conn = db.run_resilient(
                conn, op, reconnect=reconnect, label=label, **budget)
            return result

        try:
            # Lease ACQUISITION stays unwrapped: its CAS/backoff semantics are its
            # own, and nothing has been done yet when it fails.
            _wait_lease(conn, holder, _LEASE_TTL)
            attached = step(_attach_stragglers, "sweep.attach")
            LOG.info("RECOMPUTE stragglers attached=%d", attached)

            budget = min(args.max_seconds, _MAX_BUDGET_SECONDS)
            if budget < args.max_seconds:
                LOG.warning(
                    "RECOMPUTE --max-seconds %.0f clamped to %.0f — the "
                    "workflow's timeout-minutes backstop is sized for this "
                    "ceiling; raise both together in the yml",
                    args.max_seconds, budget,
                )
            deadline = started_at + budget
            max_id = step(_max_property_id, "sweep.max_id")
            total_batches = -(-max_id // args.batch_size) if max_id else 0
            batches = 0
            for lo, hi in _batch_ranges(max_id, args.batch_size):
                # Budget clean-stop (the detail drains' --max-seconds pattern):
                # stop batching with enough headroom left to finalize + release,
                # instead of being SIGKILLed mid-statement by timeout-minutes.
                batch_started = time.monotonic()
                if batch_started >= deadline:
                    incomplete_at = lo
                    break
                # Renewal (not acquisition) through step() too: it is a sticky-holder
                # CAS, idempotent on retry, and on a dead connection the raw call
                # raises BEFORE the wrapped batch below can reconnect. A genuine
                # lost lease still raises RuntimeError, which run_resilient re-raises
                # immediately (not an OperationalError -> not transient).
                step(lambda c: _renew_lease(c, holder), "sweep.renew")
                try:
                    step(lambda c: _run_recompute_statement(
                        c, _RECOMPUTE_BATCH_SQL, {"lo": lo, "hi": hi}), "sweep.batch",
                        attempts=_BATCH_RESILIENT_ATTEMPTS)
                except Exception:
                    # Name the poisoned range before dying — the 10:09Z run's
                    # log showed only a bare QueryCanceled with no way to tell
                    # WHICH ids need investigating.
                    LOG.error(
                        "RECOMPUTE batch=%d-%d FAILED after %.1fs",
                        lo, hi, time.monotonic() - batch_started,
                    )
                    raise
                batches += 1
                # One line per batch, deliberately: ~311 lines/day buys the
                # per-range cost profile that diagnosing the post-#971 SQL
                # (and any future creep toward the budget) depends on.
                LOG.info(
                    "RECOMPUTE batch=%d-%d %.1fs (%d/%d)",
                    lo, hi, time.monotonic() - batch_started,
                    batches, total_batches,
                )

            # The finalize block is wrapped too: a drop here would throw away an
            # otherwise-complete sweep's completion stamp and red the health check.
            if incomplete_at is None:
                reconciled = step(_reconcile_childless, "sweep.reconcile")
                if reconciled:
                    LOG.info(
                        "RECOMPUTE reconciled childless=%d (set is_active=false)",
                        reconciled,
                    )

                def _finalize(c: Any) -> None:
                    # The full sweep recomputed every property, so clear the dirt
                    # that existed at its start; anything dirtied mid-sweep survives
                    # for the next incremental pass.
                    with c.cursor() as cur:
                        cur.execute(_CLEAR_DIRTY_SQL, {"cutoff": cutoff})
                    # Completion stamp — the health check's O(1) liveness signal.
                    # Complete walks only: an incomplete sweep leaving the stamp
                    # stale IS the alarm condition.
                    with c.cursor() as cur:
                        cur.execute(_STAMP_SWEEP_COMPLETE_SQL, {
                            "max_id": max_id, "batches": batches,
                            "elapsed_s": round(time.monotonic() - started_at, 1),
                        })

                step(_finalize, "sweep.finalize")
            else:
                # Only ids < incomplete_at were recomputed — clear their dirt
                # only, and skip _reconcile_childless (next complete sweep runs
                # it; its targets are near-zero in practice).
                def _clear_swept(c: Any) -> None:
                    with c.cursor() as cur:
                        cur.execute(
                            _CLEAR_DIRTY_SWEPT_SQL,
                            {"cutoff": cutoff, "hi": incomplete_at},
                        )

                step(_clear_swept, "sweep.clear_swept")
        finally:
            _release_lease(conn, holder)

    elapsed = time.monotonic() - started_at
    if incomplete_at is not None:
        # RED on purpose: an incomplete reconcile is a broken contract, not a
        # partial success — GH only emails on scheduled-run FAILURES (a
        # timeout kill lands as `cancelled` and alerts nobody, which is how
        # 5 dead sweeps went unnoticed for 4 days). The id tail above
        # `incomplete_at` keeps its pre-sweep stats until a sweep finishes;
        # the `property_maintenance` health check tracks that staleness.
        LOG.error(
            "RECOMPUTE budget exhausted after %.0fs: swept ids<%d of %d "
            "(%d/%d batches); exiting RED — investigate per-batch cost first "
            "(see the progress logs); raising the budget past %.0fs requires "
            "editing BOTH --max-seconds and the workflow's timeout-minutes",
            elapsed, incomplete_at, max_id, batches, total_batches,
            _MAX_BUDGET_SECONDS,
        )
        return 1
    LOG.info(
        "RECOMPUTE done max_property_id=%d batches=%d avg_batch_s=%.1f elapsed=%.1fs",
        max_id, batches, elapsed / batches if batches else 0.0, elapsed,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
