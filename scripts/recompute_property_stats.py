"""The property rollup -- attach stragglers, then recompute each property from its adverts.

1. Attach stragglers: every `property_id IS NULL` listing (the batched detail-drain writes them)
   is born a bare singleton (`scraper.db.NEW_SINGLETONS_SQL`, the one birth path) and recomputed
   in the same transaction. No cross-listing matching, ever (CLAUDE.md rule 15).
2. Recompute, ONE RULE PER FIELD (docs/design/merge-sprint/PROGRAM.md MS11). The canonical advert
   is rank 1 of `property_canonical_listings(property_id)` (migration 588, MS5: active, a map
   point, earliest first seen among active / latest last seen among inactive, trust, id). Every
   advert field is its own: price, area (no fallback), layout, category, subtype, source,
   condition with both derived levels (rule 14), furnished, and `repr_listing_ref_id`, through
   which the read models take place, floor, description, photos, broker and link. The six
   amenities are a union over every advert (MS6); every other physical fact is the first
   non-empty value in the canonical order. The price figures are the canonical advert's lineage
   (MS10): its own and its same-portal predecessors' `listing_price_steps` plus one handover step
   per link, the total compounded. Lifecycle: any advert active, min/max seen, newest snapshot;
   the portal lists and one newest-advert date per offered portal (MS19). `repr_since` is
   stamped when the canonical advert changes, or when a property the full sweep reset to no ads
   gains one (the price alerts start there), and
   `city_proximity_computed_at` is cleared then and whenever it predates `repr_since`, so the
   hourly city job recomputes the figures from the canonical advert's point (an advert without
   one keeps the earlier figures until it gains one: the job reads only a point).

Batched by property-id range so each statement stays well under the
transaction-pooler statement timeout. autocommit=True means each batch
commits independently -- a workflow timeout preserves completed batches.

Liveness (2026-08-06 incident): the maintenance lease is a SHORT (15 min) TTL
heartbeat-renewed every batch/slice — never a runtime-sized grant — so a
SIGKILL at any point freezes maintenance for minutes, not hours. The full
sweep also takes a --max-seconds wall-clock budget and CLEAN-STOPS at a batch
boundary when it runs out: finalize what was covered, save a resume cursor,
release the lease. The next run continues that CYCLE from the cursor instead
of id 1 (2026-10-03: four daily runs in a row stopped near id 540k of 927k,
each restarting at 1, so the tail went days without a reconcile). The first
stop of a cycle exits 0 with a warning; a resumed run that stops again, or a
run that stops before its first batch (nothing to resume), exits RED (GH
reports a timeout kill as `cancelled`, which alerts nobody). The
`property_maintenance` check in scripts/verify_pipeline.py watches the
completion stamp independently.

Two run modes (Phase 3 -- real-time properties):

  * --incremental (cron */5, property_maintenance.yml): attach new stragglers + recompute
    ONLY the properties queued in `dirty_properties` by the writers. O(changes).
  * full (default, daily reconcile, recompute_property_stats.yml): attach +
    recompute EVERY property (one cycle, resumed across runs when the budget
    cuts it) + reconcile childless + clear the queue. The self-healing backstop
    for anything the incremental pass missed.

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
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from typing import Any

from scraper import db
from toolkit.browse_read_model import sync_browse_list
from toolkit.filter_registry import PORTAL_OPTIONS

LOG = logging.getLogger("recompute_property_stats")


def _sigterm_to_systemexit(signum: int, frame: Any) -> None:
    raise SystemExit(143)


# One attach handles at most this many births (a pass, or one turn of the full sweep's loop):
# all nine portals land new rows NULL, so a backlog after a worker freeze must not birth,
# recompute and browse-sync everything in one transaction. No ORDER BY, so the planner keeps
# listings_property_id_idx; NEW_SINGLETONS_SQL sorts the ids anyway.
STRAGGLER_BATCH = 2000
_STRAGGLERS_SQL = "SELECT id FROM listings WHERE property_id IS NULL LIMIT %(limit)s"

# MS19: one date per portal Browse offers (`PORTAL_OPTIONS`; migration 588 holds the columns), the
# first sighting of the property's newest advert there, active or not. A portal added to the
# registry fails tests/test_recompute_property_stats.py until a migration gives it its column.
# The statement below is an f-string that splices these in: a literal brace in it is doubled.
_NEWEST_AD_AT_AGG = "\n".join(
    f"        max(k.first_seen_at) FILTER (WHERE k.source = '{o.value}') AS newest_ad_at_{o.value},"
    for o in PORTAL_OPTIONS)
_NEWEST_AD_AT_SET = "\n".join(
    f"      newest_ad_at_{o.value} = r.newest_ad_at_{o.value}," for o in PORTAL_OPTIONS)

_RECOMPUTE_BATCH_SQL = f"""
    WITH batch AS (
      SELECT id FROM properties WHERE id >= %(lo)s AND id < %(hi)s
    ),
    -- Every advert of the batch's properties in THE canonical order (migration 588, MS5); rank 1
    -- is the canonical advert. The order is spelled there and nowhere else.
    kids AS (
      SELECT l.*, o.canonical_rank
      FROM batch b
      CROSS JOIN LATERAL property_canonical_listings(b.id) o
      JOIN listings l ON l.id = o.listing_id
    ),
    canon AS (
      SELECT * FROM kids WHERE canonical_rank = 1
    ),
    -- Lifecycle and the portals over every advert, active or not; the six amenities are a union
    -- (yes when any advert says yes, MS6); every other physical fact is the first non-empty value
    -- in the canonical order.
    child_agg AS (
      SELECT
        k.property_id              AS pid,
        bool_or(k.is_active)       AS is_active,
        count(*)                   AS source_count,
        min(k.first_seen_at)       AS first_seen_at,
        max(k.last_seen_at)        AS last_seen_at,
        array_agg(DISTINCT k.source ORDER BY k.source) AS all_sources,
        coalesce(array_agg(DISTINCT k.source ORDER BY k.source) FILTER (WHERE k.is_active),
                 ARRAY[]::text[]) AS active_sources,
{_NEWEST_AD_AT_AGG}
        bool_or(k.has_lift)        AS has_lift,
        bool_or(k.has_balcony)     AS has_balcony,
        bool_or(k.has_parking)     AS has_parking,
        bool_or(k.terrace)         AS terrace,
        bool_or(k.garage)          AS garage,
        bool_or(k.cellar)          AS cellar,
        (array_agg(k.usable_area ORDER BY k.canonical_rank) FILTER (WHERE k.usable_area IS NOT NULL))[1] AS usable_area,
        (array_agg(k.estate_area ORDER BY k.canonical_rank) FILTER (WHERE k.estate_area IS NOT NULL))[1] AS estate_area,
        (array_agg(k.garden_area ORDER BY k.canonical_rank) FILTER (WHERE k.garden_area IS NOT NULL))[1] AS garden_area,
        (array_agg(k.parking_lots ORDER BY k.canonical_rank) FILTER (WHERE k.parking_lots IS NOT NULL))[1] AS parking_lots,
        (array_agg(k.building_type ORDER BY k.canonical_rank) FILTER (WHERE k.building_type IS NOT NULL))[1] AS building_type,
        (array_agg(k.ownership ORDER BY k.canonical_rank) FILTER (WHERE k.ownership IS NOT NULL))[1] AS ownership,
        (array_agg(k.energy_rating ORDER BY k.canonical_rank) FILTER (WHERE k.energy_rating IS NOT NULL))[1] AS energy_rating
      FROM kids k
      GROUP BY k.property_id
    ),
    -- The canonical advert and its same-portal predecessors (MS10): each predecessor is the advert
    -- on that portal last seen latest, strictly before the later link was first seen, so adverts
    -- that ran at the same time never link; first_seen_at falls along the chain, so it ends.
    lineage AS (
      WITH RECURSIVE link AS (
        SELECT c.property_id AS pid, c.id, c.source, c.first_seen_at, 0 AS hop FROM canon c
        UNION ALL
        SELECT k.pid, pre.id, k.source, pre.first_seen_at, k.hop + 1
        FROM link k
        CROSS JOIN LATERAL (
          SELECT l.id, l.first_seen_at FROM listings l
          WHERE l.property_id = k.pid AND l.source = k.source
            AND l.last_seen_at < k.first_seen_at AND l.first_seen_at < k.first_seen_at
          ORDER BY l.last_seen_at DESC, l.id DESC
          LIMIT 1
        ) pre
      )
      SELECT * FROM link
    ),
    -- Each priced link's first and last price over its own snapshots, dated by the first.
    members AS (
      SELECT g.pid, g.hop,
        (array_agg(s.price_czk ORDER BY s.scraped_at, s.id))[1]           AS first_price,
        min(s.scraped_at)                                                 AS first_at,
        (array_agg(s.price_czk ORDER BY s.scraped_at DESC, s.id DESC))[1] AS last_price,
        count(*)                                                          AS price_points
      FROM lineage g
      JOIN listing_snapshots s ON s.listing_id = g.id
      WHERE s.price_czk IS NOT NULL
      GROUP BY g.pid, g.hop
    ),
    -- The steps the price figures count: each link's OWN steps (`listing_price_steps`, migration
    -- 559, the one step definition the alerts read too) plus one handover step into each priced
    -- link from the last price of the next older priced link, dated at the newer link's first.
    steps AS (
      SELECT g.pid, ps.scraped_at, ps.price_czk, ps.prev_price_czk
      FROM lineage g
      JOIN listing_price_steps ps ON ps.listing_id = g.id
      UNION ALL
      SELECT h.pid, h.first_at, h.first_price, h.prev_price_czk
      FROM (SELECT m.pid, m.first_at, m.first_price,
                   lead(m.last_price) OVER (PARTITION BY m.pid ORDER BY m.hop) AS prev_price_czk
            FROM members m) h
      WHERE h.first_price <> h.prev_price_czk
    ),
    -- Each step dated by its own scraped_at. The windowed counts decay as events age out, so they
    -- are only as fresh as the last recompute of the row -- the daily full sweep is the bound.
    price_hist AS (
      SELECT
        ps.pid,
        count(*) FILTER (WHERE ps.price_czk < ps.prev_price_czk) AS drops,
        count(*) FILTER (WHERE ps.price_czk > ps.prev_price_czk) AS rises,
        count(*)                                                 AS changes,
        count(*) FILTER (WHERE ps.scraped_at >= now() - interval '30 days')  AS changes_30d,
        count(*) FILTER (WHERE ps.scraped_at >= now() - interval '90 days')  AS changes_90d,
        count(*) FILTER (WHERE ps.scraped_at >= now() - interval '365 days') AS changes_365d,
        max((ps.prev_price_czk - ps.price_czk)::numeric / ps.prev_price_czk * 100)
          FILTER (WHERE ps.price_czk < ps.prev_price_czk)        AS max_drop_pct
      FROM steps ps
      GROUP BY ps.pid
    ),
    -- The headline delta, compounded over those steps: it telescopes to the oldest priced link's
    -- first price against the canonical advert's last. (No literal percent sign in this comment on
    -- purpose -- prose percent inside executed SQL is an `incomplete placeholder` crash in
    -- psycopg; tests/test_sql_placeholders.py guards it.) NULL under two priced snapshots or with
    -- an unpriced canonical advert.
    lineage_span AS (
      SELECT pid,
        (array_agg(first_price ORDER BY hop DESC))[1]     AS first_price,
        (array_agg(last_price) FILTER (WHERE hop = 0))[1] AS last_price,
        sum(price_points)                                 AS price_points
      FROM members
      GROUP BY pid
    ),
    -- Last content change = the newest snapshot of any advert (snapshots are content-change
    -- only, rule 2): the "recently changed" timestamp Browse filters on (migration 158).
    changes AS (
      SELECT l.property_id AS pid, max(s.scraped_at) AS last_change_at
      FROM listing_snapshots s
      JOIN listings l ON l.id = s.listing_id
      JOIN batch b ON b.id = l.property_id
      GROUP BY l.property_id
    )
    UPDATE properties p SET
      is_active           = r.is_active,
      source_count        = r.source_count,
      first_seen_at       = r.first_seen_at,
      last_seen_at        = r.last_seen_at,
      repr_listing_id     = c.sreality_id,
      repr_since          = CASE WHEN p.repr_listing_ref_id <> c.id OR p.source_count = 0 THEN now() ELSE p.repr_since END,
      city_proximity_computed_at = CASE WHEN p.repr_listing_ref_id <> c.id OR p.source_count = 0 OR p.city_proximity_computed_at < p.repr_since THEN NULL ELSE p.city_proximity_computed_at END,
      repr_listing_ref_id = c.id,
      category_main       = c.category_main,
      category_type       = c.category_type,
      category_sub_cb     = c.category_sub_cb,
      subtype             = c.subtype,
      disposition         = c.disposition,
      area_m2             = c.area_m2,
      current_price_czk   = c.price_czk,
      condition           = c.condition,
      building_condition_level  = c.building_condition_level,
      apartment_condition_level = c.apartment_condition_level,
      furnished           = c.furnished,
      source              = c.source,
      all_sources         = r.all_sources,
      active_sources      = r.active_sources,
      has_lift            = r.has_lift,
      has_balcony         = r.has_balcony,
      has_parking         = r.has_parking,
      terrace             = r.terrace,
      garage              = r.garage,
      cellar              = r.cellar,
      usable_area         = r.usable_area,
      estate_area         = r.estate_area,
      garden_area         = r.garden_area,
      parking_lots        = r.parking_lots,
      building_type       = r.building_type,
      ownership           = r.ownership,
      energy_rating       = r.energy_rating,
      price_drop_count    = coalesce(ph.drops, 0),
      price_rise_count    = coalesce(ph.rises, 0),
      max_price_drop_pct  = ph.max_drop_pct,
      price_change_count      = coalesce(ph.changes, 0),
      price_change_count_30d  = coalesce(ph.changes_30d, 0),
      price_change_count_90d  = coalesce(ph.changes_90d, 0),
      price_change_count_365d = coalesce(ph.changes_365d, 0),
      total_price_change_pct  = CASE
          WHEN cs.price_points >= 2 AND cs.first_price > 0
          THEN (cs.last_price - cs.first_price)::numeric / cs.first_price * 100
      END,
      last_change_at      = coalesce(ch.last_change_at, r.first_seen_at),
{_NEWEST_AD_AT_SET}
      stats_computed_at   = now()
    FROM child_agg r
    JOIN canon c ON c.property_id = r.pid
    LEFT JOIN price_hist ph ON ph.pid = r.pid
    LEFT JOIN lineage_span cs ON cs.pid = r.pid
    LEFT JOIN changes ch ON ch.pid = r.pid
    WHERE p.id = r.pid
"""

# Single-property recompute, derived from the batch SQL by narrowing the `batch`
# CTE to one id. Deriving it (rather than re-writing the body) guarantees the
# ingest birth's recompute and the batch can never drift apart.
_RECOMPUTE_ONE_SQL = _RECOMPUTE_BATCH_SQL.replace(
    "SELECT id FROM properties WHERE id >= %(lo)s AND id < %(hi)s",
    "SELECT id FROM properties WHERE id = %(pid)s",
)

# Dirty-set recompute (Phase 3), derived the same way: the batch CTE is scoped to
# an explicit id array instead of an id range, so `properties_changed` (the drain,
# the identity writers) and straggler-attach recompute exactly their properties
# with the identical body (never drifts from full).
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

# The full sweep's one dirty clear: rows queued at or before the run's start (anything dirtied
# mid-run survives for the next pass) in the id range THIS run recomputed. Never wider: ids
# above a budget stop were not recomputed, and ids below a resumed run's start were recomputed
# by an earlier run and may be dirty again -- clearing either would erase their recompute
# signal until the next cycle instead of the next incremental pass minutes later. A complete
# walk from id 1 passes [1, max_id + 1), which is the old global pre-cutoff delete for every
# property it walked: the FK makes each queued row name a property, ids are bigserial from 1
# and never deleted, and max_id is read after the cutoff. The one row the global delete also
# dropped was dirt stamped before the cutoff on a property born after max_id was read -- one
# the walk never recomputed, so keeping it is the fix, not a loss.
_CLEAR_DIRTY_SWEPT_SQL = (
    "DELETE FROM dirty_properties "
    "WHERE marked_at <= %(cutoff)s AND property_id >= %(lo)s AND property_id < %(hi)s"
)

# The batch statement starts from the ads, so a property whose ads all moved away keeps its
# last recompute, canonical ad included. A merge retires its loser (nothing serves a merged-away
# row); any other childless property is reset to "no ads". Its last advert facts stay for its
# page; with no canonical ad, the consumer rule (rule 25, read off that ad's location) keeps it
# out of Browse, the map and the Watchdog. Only a row that still differs is written, so the
# count is what this run reset. Only this reset writes `source_count` 0: the batch statement
# reads it to stamp the handover when the property gains an ad again (a NULL handle compares
# to nothing), and the R2 parity check skips it (both handles NULL on purpose).
_NO_ADS = {
    "is_active": "false", "repr_listing_ref_id": "NULL", "repr_listing_id": "NULL",
    "source_count": "0", "all_sources": "ARRAY[]::text[]", "active_sources": "ARRAY[]::text[]",
    **{f"newest_ad_at_{o.value}": "NULL" for o in PORTAL_OPTIONS},
}
_RECONCILE_CHILDLESS_SQL = f"""
    UPDATE properties p SET ({', '.join(_NO_ADS)}) = ({', '.join(_NO_ADS.values())})
    WHERE p.status = 'active'
      AND NOT EXISTS (SELECT 1 FROM listings l WHERE l.property_id = p.id)
      AND ({', '.join(f'p.{c}' for c in _NO_ADS)}) IS DISTINCT FROM ({', '.join(_NO_ADS.values())})
"""

# Written ONLY when a cycle covered every id (in one run, or across runs through the cursor
# below) — the O(1) liveness signal the
# `property_maintenance` health check reads. Per-row stats_computed_at cannot
# serve that role: min() over 620k properties with a listings semi-join
# measured ~3.5 min live, and a check that heavy would blow the hourly acute
# lane's own 5-min job timeout — recreating the silent-`cancelled` failure
# mode it exists to catch. A dead, killed, or chronically-incomplete sweep
# shows up here as a stale stamp within hours, however the process died.
# `batches` and `elapsed_s` are the finishing run's; `runs` and `cycle_started_at` the cycle's.
_STAMP_SWEEP_COMPLETE_SQL = """
    INSERT INTO app_settings (key, value, updated_by)
    VALUES ('property_sweep_last_complete',
            jsonb_build_object(
                'completed_at', now(),
                'max_property_id', %(max_id)s::bigint,
                'batches', %(batches)s::int,
                'elapsed_s', %(elapsed_s)s::numeric,
                'runs', %(runs)s::int,
                'cycle_started_at', %(cycle_started_at)s::timestamptz),
            'recompute_property_stats')
    ON CONFLICT (key) DO UPDATE
      SET value = excluded.value, updated_at = now(),
          updated_by = excluded.updated_by
"""

# The resume cursor (2026-10-03: four daily runs in a row stopped on budget near id 540k of
# 927k and each restarted at id 1, so the id tail went days without a reconcile). A budget stop
# saves where the next run continues the cycle; the run that completes it deletes the row in
# the stamp's transaction. A cycle that began more than _CURSOR_MAX_AGE ago starts over at id 1
# instead, or the stamp would vouch for a reconcile whose lower half is days old. The age is
# judged when a run resumes, not when it stamps, so a stamp's oldest recompute can be
# _CURSOR_MAX_AGE plus that run's own length (~2h: budget + one in-flight batch) old; the
# stamp's `cycle_started_at` records it.
_CURSOR_MAX_AGE = "36 hours"

_READ_SWEEP_CURSOR_SQL = """
    SELECT (value->>'next_lo')::bigint, (value->>'runs')::int, value->>'cycle_started_at',
           (value->>'cycle_started_at')::timestamptz > now() - %(max_age)s::interval
      FROM app_settings WHERE key = 'property_sweep_cursor'
"""

_SAVE_SWEEP_CURSOR_SQL = """
    INSERT INTO app_settings (key, value, updated_by)
    VALUES ('property_sweep_cursor',
            jsonb_build_object(
                'next_lo', %(next_lo)s::bigint,
                'cycle_started_at', %(cycle_started_at)s::timestamptz,
                'runs', %(runs)s::int),
            'recompute_property_stats')
    ON CONFLICT (key) DO UPDATE
      SET value = excluded.value, updated_at = now(),
          updated_by = excluded.updated_by
"""

_DELETE_SWEEP_CURSOR_SQL = "DELETE FROM app_settings WHERE key = 'property_sweep_cursor'"


def recompute_one(conn: Any, property_id: int) -> None:
    """Recompute one property's rollup + stats using the batch job's exact SQL.

    No transaction wrapper, so it nests inside a caller's open transaction. Its one caller is
    the ingest birth (`scraper.db._ensure_property`); the identity writers and the dirty drain
    use `properties_changed`.
    """
    with conn.cursor() as cur:
        cur.execute(_RECOMPUTE_ONE_SQL, {"pid": property_id})


# Every advert of these properties that a broker is attributed to, queued for the broker drain
# (Broker Unify W3): `brokers.property_count` / `active_property_count` key on the advert's
# property, so a recompute, a merge or a detach changes them. Stamped clock_timestamp(), not
# now(): this runs inside the writer's (or the slice's) transaction, whose now() is its START.
# A broker pass whose cutoff falls inside that transaction would otherwise read the pre-move
# listings.property_id and then, once we commit, its `marked_at <= cutoff` dequeue would still
# match our re-stamp and drop it — that broker's counts stale until the daily sweep. Statement
# time narrows the window to the gap between this statement and the commit.
_MIRROR_BROKER_DIRTY_SQL = (
    "INSERT INTO dirty_broker_listings (listing_id, marked_at) "
    "SELECT l.id, clock_timestamp() FROM listings l "
    "WHERE l.property_id = ANY(%(ids)s) AND l.broker_identity_id IS NOT NULL "
    "ON CONFLICT (listing_id) DO UPDATE SET marked_at = clock_timestamp()"
)


def properties_changed(conn: Any, property_ids: Iterable[int]) -> None:
    """Make what is derived from these properties true now, inside the caller's transaction: the
    rollup (the sweep's own body, scoped), their Browse rows, and their attributed adverts queued
    for the broker drain. THE after-step of the dirty drain and of both identity writers
    (`toolkit.property_identity`); it opens no transaction, sets no ceiling and never enqueues
    `dirty_properties` (the drain calls it on ids it is about to dequeue).

    The order is load-bearing: `browse_projection` reads the recomputed `properties` row, and the
    broker mirror selects adverts by their property AFTER the writer moved them. The Browse patch
    is best-effort (`sync_browse_list` unwinds only itself) and a fast path, not a guarantee:
    without it a recompute waited a measured mean 11.7 min (worst 36.6) for the */15 wholesale
    rebuild, and a patch committed while a rebuild is in flight (~26% of wall-clock) lands on the
    doomed table and is superseded silently. A full drain slice (2000 ids, only after a backlog;
    the live depth is a couple of dozen) costs ~100-270 ms of ROW EXCLUSIVE on `browse_list`, the
    lock the rebuild's drop waits behind. That is the uncontended cost. The rebuild's drop runs
    with lock_timeout 0, so it queues behind any in-flight `browse_list` reader (up to 120 s on
    the API role), and the patch queues behind it while the caller still holds these
    properties' row locks; a merge or split of one of them waits too. The drain bounds that wait
    (`_DRAIN_LOCK_TIMEOUT`); the identity writers inherit their caller's lock_timeout.
    """
    ids = sorted({int(p) for p in property_ids if p is not None})
    if not ids:
        return
    with conn.cursor() as cur:
        cur.execute(_RECOMPUTE_SCOPED_SQL, {"ids": ids})
    sync_browse_list(conn, ids)
    with conn.cursor() as cur:
        cur.execute(_MIRROR_BROKER_DIRTY_SQL, {"ids": ids})


@contextmanager
def _bounded(conn: Any) -> Iterator[None]:
    """A transaction under the batch ceiling (SET LOCAL no-ops in autocommit)."""
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = '{_BATCH_STATEMENT_TIMEOUT}'")
        yield


def _run_recompute_statement(conn: Any, sql: str, params: dict[str, Any]) -> None:
    """One recompute statement in its own `_bounded` transaction, so each batch still commits
    independently (a killed sweep keeps every finished batch)."""
    with _bounded(conn), conn.cursor() as cur:
        cur.execute(sql, params)


def _reconcile_childless(conn: Any) -> int:
    with conn.cursor() as cur:
        cur.execute(_RECONCILE_CHILDLESS_SQL)
        return cur.rowcount or 0


def _batch_ranges(max_id: int, batch_size: int, start: int = 1) -> Iterator[tuple[int, int]]:
    """Yield half-open [lo, hi) id ranges covering start..max_id inclusive."""
    if max_id < start or batch_size < 1:
        return
    for lo in range(start, max_id + 1, batch_size):
        yield lo, lo + batch_size


def _attach_stragglers(conn: Any, limit: int = STRAGGLER_BATCH) -> int:
    """Give up to `limit` property_id-NULL listings their own singleton, recomputed and browse-synced at birth.
    No cross-listing matching happens here, ever (CLAUDE.md rule 15)."""
    # Birth, link and first recompute in ONE transaction: a replay after a failure (db.
    # run_resilient replays this op) finds the stragglers still unlinked, never a linked bare
    # row that Browse could show before its recompute. The browse patch is best-effort in its
    # own savepoint (sync_browse_list), so it can never abort the birth.
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(_STRAGGLERS_SQL, {"limit": limit})
            stragglers = [int(r[0]) for r in cur.fetchall()]
        born = db.create_singleton_properties(conn, stragglers) if stragglers else []
        if born:
            _run_recompute_statement(conn, _RECOMPUTE_SCOPED_SQL, {"ids": born})
            sync_browse_list(conn, born)
    return len(born)


def _bind_pending_estimation_listing_ids(conn: Any, *, limit: int = 5000) -> int:
    """Late-binding identity resolution for estimation_runs. Returns rows stamped.

    An estimation submitted for a sreality URL the scraper has not reached yet
    lands input_sreality_id NOT NULL + input_listing_id NULL (the insert's COALESCE
    subquery finds no listing). Every estimation read path now keys solely on the
    surrogate input_listing_id, so such a run belongs to no listing page until this
    stamps it. This is the same "a listing just became visible" tick that attaches
    stragglers, which is exactly the event that makes the NULL resolvable.

    Lives here, not in api/estimation_runs.py, on purpose: property_maintenance.yml
    installs base extras only (`pip install -e .`, no [api]), so importing that
    fastapi-dependent module would ImportError the whole cron.

    Rule 12 (runs are immutable) is respected — this resolves IDENTITY, never a
    result. It only ever fills a NULL: the `input_listing_id IS NULL` guard is
    repeated in the UPDATE's own WHERE, not just the CTE, so the stamp is one-way
    and idempotent under concurrency and can never overwrite an already-bound run.

    Deliberately NOT fuzzy. listings_sreality_id_uidx makes the subquery provably
    single-valued, so a multi-match cannot be represented on this arm; a run that
    stays ambiguous keeps its NULL rather than being attributed by an arbitrary
    tie-break, and there is no input_url arm. A wrong attribution silently credits
    a paid estimate to the wrong flat — strictly worse than staying unattached
    (the principle the street extractor already follows).
    """
    with conn.cursor() as cur:
        cur.execute(
            "WITH cand AS ("
            "  SELECT er.id AS run_id,"
            "         (SELECT l.id FROM listings l"
            "           WHERE l.sreality_id = er.input_sreality_id) AS listing_id"
            "    FROM estimation_runs er"
            "   WHERE er.input_listing_id IS NULL"
            "     AND er.input_sreality_id IS NOT NULL"
            "   ORDER BY er.id"
            "   LIMIT %(limit)s"
            ") "
            "UPDATE estimation_runs er "
            "   SET input_listing_id = cand.listing_id "
            "  FROM cand "
            " WHERE er.id = cand.run_id "
            "   AND cand.listing_id IS NOT NULL "
            "   AND er.input_listing_id IS NULL",
            {"limit": int(limit)},
        )
        return cur.rowcount or 0


# The drain slice's ceiling on any one lock wait. Its Browse patch can queue behind a */15
# rebuild's swap, holding the slice's `properties` row locks (properties_changed's docstring),
# and the slice is mostly recently-dirtied properties — the ones the autodedup lane (5 s
# lock_timeout) and a split (5 s, busy 409) are likeliest to touch. A patch that times out
# unwinds only its savepoint and the next rebuild carries the row; a recompute that times out
# rolls the slice back and the next pass replays it. Drain only: the full sweep and the
# straggler attach share `_bounded` and keep the session default.
_DRAIN_LOCK_TIMEOUT = "5s"


def _drain_dirty(
    conn: Any, batch_size: int, cutoff: Any,
    renew: Any = None,
) -> int:
    """Bring every property queued at/before `cutoff` current, one claimed slice at a time.

    Each slice runs `properties_changed` (recompute, Browse patch, broker queue) in ONE
    `_bounded` transaction under `_DRAIN_LOCK_TIMEOUT`, then dequeues: a crash between the two
    replays the slice, and every statement is idempotent. Always terminates -- only rows with
    marked_at <= cutoff are claimable, the delete removes the claimed ones, and a row re-dirtied
    mid-run moves past the cutoff.

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
        with _bounded(conn):
            with conn.cursor() as cur:
                cur.execute(f"SET LOCAL lock_timeout = '{_DRAIN_LOCK_TIMEOUT}'")
            properties_changed(conn, ids)
        with conn.cursor() as cur:
            cur.execute(_DELETE_DIRTY_SQL, {"ids": ids, "cutoff": cutoff})
        total += len(ids)
    return total


def _max_property_id(conn: Any) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(id), 0) FROM properties")
        return int(cur.fetchone()[0])


def _read_sweep_cursor(conn: Any) -> tuple[Any, ...] | None:
    """The saved cursor as (next_lo, runs, cycle_started_at, younger than _CURSOR_MAX_AGE)."""
    import psycopg

    try:
        with conn.cursor() as cur:
            cur.execute(_READ_SWEEP_CURSOR_SQL, {"max_age": _CURSOR_MAX_AGE})
            return cur.fetchone()
    except psycopg.DataError as exc:
        # The Settings page edits any app_settings row as raw JSON, and a value the casts reject
        # is a DataError run_resilient never retries: it would red every run until someone fixed
        # the row by hand. Like resolve_brokers._sweep_state, treat it as no cursor instead.
        LOG.warning("RECOMPUTE property_sweep_cursor is unreadable (%s): treated as absent, so "
                    "a fresh cycle starts at id 1 and its first save replaces the row", exc)
        return None


def _resume_point(saved: tuple[Any, ...] | None, max_id: int) -> tuple[int, int, Any] | None:
    """(next_lo, runs, cycle_started_at) if the saved cursor can continue its cycle, else None."""
    if saved is None:
        return None
    next_lo, runs, cycle_started_at, young = saved
    if not young or next_lo is None or not (1 < next_lo <= max_id):
        return None
    return int(next_lo), int(runs or 0), cycle_started_at


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
# MUST stay comfortably under _LEASE_TTL (15 min): renewal fires as the first
# statement of each batch attempt, so ONE statement's worst case is the longest
# possible renewal gap. (Per ATTEMPT, deliberately — see
# _BATCH_RESILIENT_ATTEMPTS: renewing only once per loop iteration would let a
# retried batch outlive its own lease.)
_BATCH_STATEMENT_TIMEOUT = "10min"

# Retry budget for ONE recompute batch (db.run_resilient defaults to 4). A batch
# runs under the 10-min ceiling above, so four attempts could burn 40 minutes
# inside a single loop iteration — the deadline is only checked at a batch
# BOUNDARY, so that would sail past the workflow's outer timeout and re-create
# the silent `cancelled` this script exists to eliminate. Two attempts keeps the
# useful part of the budget (a dropped connection or a passing lock wait replays
# once) and bounds the in-flight worst case at 2 x _BATCH_STATEMENT_TIMEOUT,
# which is what the workflow's timeout-minutes is sized for. A range that times
# out twice is the poisoned range the error log names, not a blip. 20 min also
# exceeds _LEASE_TTL, which is why the lease renewal runs INSIDE the retried op
# (main()'s _renew_and_recompute) rather than once per loop iteration.
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


def _release_lease_cas(conn: Any, holder: str) -> None:
    with conn.cursor() as cur:
        cur.execute(_RELEASE_LEASE_SQL, {"holder": holder})


def _release_lease(conn: Any, holder: str,
                   reconnect: Callable[[], Any] | None = None) -> None:
    """Best-effort release, with ONE reconnect fallback. Callers run this from a
    `finally:`, so a raise here (a dead connection makes even `conn.cursor()` throw)
    would replace the real failure with a crash-during-cleanup. The lease's
    correctness never rested on the release landing: the holder-guarded CAS plus the
    short renewed TTL are the guarantee, and a missed release costs at most one TTL
    of frozen maintenance.

    The fallback covers the case `step`'s `nonlocal conn` narrows but cannot remove:
    when run_resilient exhausts its budget on a DROPPED connection it closes both the
    original and its replacement, so there is no live handle left to release on. One
    fresh connection, one holder-guarded CAS, close it again."""
    exc: BaseException | None = None
    try:
        _release_lease_cas(conn, holder)
        return
    except Exception as e:  # noqa: BLE001 - best-effort; the TTL is the real guarantee
        exc = e
    if reconnect is not None:
        fresh: Any = None
        try:
            fresh = reconnect()
            _release_lease_cas(fresh, holder)
            return
        except Exception as e:  # noqa: BLE001 - still best-effort
            exc = e
        finally:
            if fresh is not None:
                try:
                    fresh.close()
                except Exception:  # noqa: BLE001
                    pass
    # exc_info=exc, not True: we are outside the except block here, so sys.exc_info()
    # would be empty and the traceback lost.
    LOG.warning("MAINTENANCE: lease release failed (holder=%s) — self-heals when "
                "the %s TTL expires", holder, _LEASE_TTL, exc_info=exc)


def run_incremental_pass(conn: Any, batch_size: int = 2000) -> dict[str, Any]:
    """ONE incremental property-maintenance pass — THE shared implementation
    behind the GH cron (property_maintenance.yml) and the realtime worker's
    maintenance lane: attach new stragglers, then bring the dirty set current through
    `properties_changed` (recompute, `browse_list` patch, broker queue), so a change reaches
    Browse on this lane's cadence rather than at the next */15 wholesale rebuild.
    Serialized by the maintenance lease; a caller that
    finds the lease held returns {"skipped": True} — the concurrent pass is
    doing the same work, and the next tick is seconds away. A pass normally
    runs seconds; the 15-minute lease is a wide margin, and its expiry
    self-heals a crashed holder.
    """
    holder = _new_holder("incremental")
    if not _try_lease(conn, holder, _LEASE_TTL):
        return {
            "skipped": True, "attached": 0,
            "estimations_bound": 0, "recomputed": 0,
        }
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT now()")
            cutoff = cur.fetchone()[0]
        attached = _attach_stragglers(conn)
        bound = _bind_pending_estimation_listing_ids(conn)
        recomputed = _drain_dirty(
            conn, batch_size, cutoff,
            renew=lambda: _renew_lease(conn, holder),
        )
        return {
            "skipped": False, "attached": attached,
            "estimations_bound": bound, "recomputed": recomputed,
        }
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
        help="Dirty-set mode: attach new stragglers + recompute only queued "
             "properties. Default is the full "
             "sweep over every property (the daily reconcile backstop).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report straggler + dirty + property counts and the resume cursor, "
             "then exit without writing.",
    )
    parser.add_argument(
        "--max-seconds", type=float, default=6000.0,
        help="Full-sweep wall-clock budget (default 6000). On exhaustion the "
             "sweep clean-stops at a batch boundary, finalizes only what it "
             "covered, saves where the next run resumes the cycle, and releases "
             "the lease; a resumed run that runs out again, or a run that runs out "
             "before its first batch, exits RED (1) — a "
             "visible failure instead of a silent timeout-minutes `cancelled` kill. "
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

        def step(op: Callable[[Any], Any], label: str,
                 attempts: int | None = None) -> Any:
            """db.run_resilient with the conn rebinding its docstring demands (it may
            hand back a FRESH connection after a pooler drop). Every op below is
            idempotent — recompute statements are pure latest-wins recomputes and the
            dirty-clear, cursor and stamp are keyed writes, so a replay re-commits
            identically."""
            nonlocal conn
            budget = {} if attempts is None else {"attempts": attempts}
            result, conn = db.run_resilient(
                conn, op, reconnect=reconnect, label=label, **budget)
            return result

        if args.dry_run:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM listings WHERE property_id IS NULL")
                stragglers = int(cur.fetchone()[0])
                cur.execute("SELECT count(*) FROM dirty_properties")
                dirty = int(cur.fetchone()[0])
                cur.execute("SELECT count(*) FROM properties")
                properties = int(cur.fetchone()[0])
            saved = step(_read_sweep_cursor, "sweep.cursor")
            LOG.info(
                "RECOMPUTE dry-run mode=%s stragglers=%d dirty=%d properties=%d "
                "cursor(next_lo, runs, cycle_started_at, younger than %s)=%s; exit",
                mode, stragglers, dirty, properties, _CURSOR_MAX_AGE, saved,
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

        try:
            # Lease ACQUISITION stays unwrapped: its CAS/backoff semantics are its
            # own, and nothing has been done yet when it fails.
            _wait_lease(conn, holder, _LEASE_TTL)
            # attempts=2, like sweep.batch: a doomed attach reds in ~4 min instead of ~8.
            # Bounded batches until one comes back short: each is its own transaction.
            attached = 0
            while True:
                batch = step(_attach_stragglers, "sweep.attach", attempts=2)
                attached += batch
                if batch < STRAGGLER_BATCH:
                    break
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
            saved = step(_read_sweep_cursor, "sweep.cursor")
            resume = _resume_point(saved, max_id)
            if resume is not None:
                start_lo, runs, cycle_started_at = resume
                LOG.info(
                    "RECOMPUTE resuming the cycle started %s at id %d (run %d of it)",
                    cycle_started_at, start_lo, runs + 1,
                )
            else:
                if saved is not None:
                    LOG.warning(
                        "RECOMPUTE ignoring the saved cursor %s (cycle older than %s, "
                        "or next_lo outside 2-%d); a fresh cycle starts at id 1",
                        saved, _CURSOR_MAX_AGE, max_id,
                    )
                # A fresh cycle starts when this run did: its cutoff, by the database clock.
                start_lo, runs, cycle_started_at = 1, 0, cutoff
            ranges = list(_batch_ranges(max_id, args.batch_size, start_lo))
            total_batches = len(ranges)
            batches = 0
            for lo, hi in ranges:
                # Budget clean-stop (the detail drains' --max-seconds pattern):
                # stop batching with enough headroom left to finalize + release,
                # instead of being SIGKILLed mid-statement by timeout-minutes.
                batch_started = time.monotonic()
                if batch_started >= deadline:
                    incomplete_at = lo
                    break
                # Renewal is the FIRST statement of the retried op, not a
                # separate step before it. _BATCH_RESILIENT_ATTEMPTS lets one
                # batch occupy up to 2 x _BATCH_STATEMENT_TIMEOUT (20 min),
                # which would blow past the 15-min _LEASE_TTL and break this
                # module's invariant that one statement's worst case is the
                # longest possible renewal gap — another writer would take the
                # lease mid-batch and the next renewal would red the sweep.
                # Re-asserting per attempt also re-takes the lease on the FRESH
                # connection after a pooler drop. Idempotent (sticky-holder CAS);
                # a genuinely lost lease raises RuntimeError, which run_resilient
                # re-raises immediately (not an OperationalError -> not transient).
                def _renew_and_recompute(c: Any, lo: int = lo, hi: int = hi) -> None:
                    _renew_lease(c, holder)
                    _run_recompute_statement(
                        c, _RECOMPUTE_BATCH_SQL, {"lo": lo, "hi": hi})

                try:
                    step(_renew_and_recompute, "sweep.batch",
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
                        "RECOMPUTE reconciled childless=%d (reset to no ads)",
                        reconciled,
                    )

                def _finalize(c: Any) -> None:
                    # This run recomputed [start_lo, max_id]: clear that range's dirt,
                    # stamp the completed cycle (complete cycles only — an incomplete one
                    # leaving the stamp stale IS the alarm condition), and delete the
                    # cursor in the same transaction, so a stamp never coexists with it.
                    with c.transaction(), c.cursor() as cur:
                        cur.execute(_CLEAR_DIRTY_SWEPT_SQL, {
                            "cutoff": cutoff, "lo": start_lo, "hi": max_id + 1})
                        cur.execute(_STAMP_SWEEP_COMPLETE_SQL, {
                            "max_id": max_id, "batches": batches,
                            "elapsed_s": round(time.monotonic() - started_at, 1),
                            "runs": runs + 1, "cycle_started_at": cycle_started_at,
                        })
                        cur.execute(_DELETE_SWEEP_CURSOR_SQL)

                step(_finalize, "sweep.finalize")
            else:
                # Only [start_lo, incomplete_at) was recomputed: clear its dirt only, save
                # where the next run resumes, and leave _reconcile_childless to the run
                # that completes the cycle (its targets are near-zero in practice).
                def _stop(c: Any) -> None:
                    with c.transaction(), c.cursor() as cur:
                        cur.execute(_CLEAR_DIRTY_SWEPT_SQL, {
                            "cutoff": cutoff, "lo": start_lo, "hi": incomplete_at})
                        cur.execute(_SAVE_SWEEP_CURSOR_SQL, {
                            "next_lo": incomplete_at,
                            "cycle_started_at": cycle_started_at, "runs": runs + 1})

                # A fresh run stopped before batch 1 swept nothing to clear, and a cursor at
                # id 1 is never resumed (_resume_point needs 1 < next_lo): save nothing.
                if incomplete_at > 1:
                    step(_stop, "sweep.stop")
        finally:
            # `nonlocal conn` keeps this pointing at the last SUCCESSFULLY returned
            # connection, but run_resilient closes both the original and its
            # replacement when it exhausts its budget on a dropped socket — so the
            # release still needs a way to open one of its own.
            _release_lease(conn, holder, reconnect)

    elapsed = time.monotonic() - started_at
    if incomplete_at == 1:
        # The budget clock starts before the lease wait and the straggler attach, so a backlog
        # can spend it all before batch 1. Nothing was recomputed and no cursor saved, so there
        # is no cycle for the next run to continue: RED, or every such day would exit green
        # having reconciled nothing.
        LOG.error(
            "RECOMPUTE budget exhausted after %.0fs before the first batch (the lease wait and "
            "the straggler attach above ran first): nothing recomputed and no cursor saved, so "
            "the next run starts a fresh cycle at id 1; exiting RED",
            elapsed,
        )
        return 1
    if incomplete_at is not None and resume is None:
        # One continuation is tolerated: the cursor is saved and the next run finishes the
        # cycle from it, stamping it complete like any other.
        LOG.warning(
            "RECOMPUTE budget exhausted after %.0fs: swept ids %d to %d of %d (%d/%d batches); "
            "the next run resumes this cycle at id %d",
            elapsed, start_lo, incomplete_at - 1, max_id, batches, total_batches, incomplete_at,
        )
        return 0
    if incomplete_at is not None:
        # RED on purpose: a resumed run that runs out again means the cycle needs three or
        # more runs — genuinely too slow, not a partial success. GH only emails on
        # scheduled-run FAILURES (a timeout kill lands as `cancelled` and alerts nobody,
        # which is how 5 dead sweeps went unnoticed for 4 days). The id tail above
        # `incomplete_at` keeps its pre-sweep stats until a cycle completes; the
        # `property_maintenance` health check tracks that staleness.
        LOG.error(
            "RECOMPUTE budget exhausted after %.0fs: swept ids %d to %d of %d (%d/%d batches) "
            "in run %d of the cycle started %s, so the cycle needs three or more runs; "
            "cursor saved at id %d (resumed by a run within %s of the cycle's start, else "
            "the next run starts over at id 1); exiting RED — investigate per-batch cost "
            "first (see the progress logs); raising the budget past %.0fs requires editing "
            "BOTH --max-seconds and the workflow's timeout-minutes",
            elapsed, start_lo, incomplete_at - 1, max_id, batches, total_batches,
            runs + 1, cycle_started_at, incomplete_at, _CURSOR_MAX_AGE,
            _MAX_BUDGET_SECONDS,
        )
        return 1
    LOG.info(
        "RECOMPUTE done max_property_id=%d from_id=%d cycle_runs=%d batches=%d "
        "avg_batch_s=%.1f elapsed=%.1fs",
        max_id, start_lo, runs + 1, batches, elapsed / batches if batches else 0.0,
        elapsed,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
