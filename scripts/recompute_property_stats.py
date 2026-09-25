"""The property rollup -- attach stragglers, then recompute each property from its adverts.

1. Attach stragglers: every `property_id IS NULL` listing (the batched detail-drain writes them)
   is born a bare singleton (`scraper.db.NEW_SINGLETONS_SQL`, the one birth path) and recomputed
   in the same transaction. No cross-listing matching, ever (CLAUDE.md rule 15).
2. Recompute, ONE RULE PER FIELD (migration 561, decision 18). The canonical advert is rank 1 of
   `property_canonical_listings(property_id)` (active, trust, last seen, id). Every advert field
   is its own: price and ITS price history (`listing_price_steps`, migration 559: no other
   advert's steps count), area (no fallback), layout, category, subtype, source, condition with
   both derived levels (rule 14), furnished, and `repr_listing_ref_id`, through which the read
   models take place, floor, description, photos, broker and link. Every physical fact (building
   type, ownership, energy rating, amenities, estate/usable/garden area, parking) is the first
   non-empty value in the same order. Lifecycle: any advert active, min/max seen, newest snapshot.
   `repr_since` is stamped when the canonical advert changes (the price alerts start there).

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

  * --incremental (cron */5, property_maintenance.yml): attach new stragglers + recompute
    ONLY the properties queued in `dirty_properties` by the writers. O(changes).
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
from toolkit.browse_read_model import sync_browse_list

LOG = logging.getLogger("recompute_property_stats")


def _sigterm_to_systemexit(signum: int, frame: Any) -> None:
    raise SystemExit(143)


_STRAGGLERS_SQL = "SELECT id FROM listings WHERE property_id IS NULL"

_RECOMPUTE_BATCH_SQL = """
    WITH batch AS (
      SELECT id FROM properties WHERE id >= %(lo)s AND id < %(hi)s
    ),
    -- Every advert of the batch's properties in THE canonical order (migration 561); rank 1 is
    -- the canonical advert. The order is spelled there and nowhere else.
    kids AS (
      SELECT l.*, o.canonical_rank
      FROM batch b
      CROSS JOIN LATERAL property_canonical_listings(b.id) o
      JOIN listings l ON l.id = o.listing_id
    ),
    canon AS (
      SELECT * FROM kids WHERE canonical_rank = 1
    ),
    -- Lifecycle over every advert; each physical fact is the first non-empty value in the
    -- canonical order.
    child_agg AS (
      SELECT
        k.property_id              AS pid,
        bool_or(k.is_active)       AS is_active,
        count(*)                   AS source_count,
        count(distinct k.source)   AS distinct_site_count,
        min(k.first_seen_at)       AS first_seen_at,
        max(k.last_seen_at)        AS last_seen_at,
        (array_agg(k.has_lift ORDER BY k.canonical_rank) FILTER (WHERE k.has_lift IS NOT NULL))[1] AS has_lift,
        (array_agg(k.has_balcony ORDER BY k.canonical_rank) FILTER (WHERE k.has_balcony IS NOT NULL))[1] AS has_balcony,
        (array_agg(k.has_parking ORDER BY k.canonical_rank) FILTER (WHERE k.has_parking IS NOT NULL))[1] AS has_parking,
        (array_agg(k.terrace ORDER BY k.canonical_rank) FILTER (WHERE k.terrace IS NOT NULL))[1] AS terrace,
        (array_agg(k.garage ORDER BY k.canonical_rank) FILTER (WHERE k.garage IS NOT NULL))[1] AS garage,
        (array_agg(k.cellar ORDER BY k.canonical_rank) FILTER (WHERE k.cellar IS NOT NULL))[1] AS cellar,
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
    -- The canonical advert's OWN steps (`listing_price_steps`, migration 559, the one step
    -- definition the watchdog and the collection monitor read too), each dated by its own
    -- scraped_at. The windowed counts decay as events age out, so they are only as fresh as the
    -- last recompute of the row -- the daily full sweep is the bound.
    price_hist AS (
      SELECT
        c.property_id AS pid,
        count(*) FILTER (WHERE ps.price_czk < ps.prev_price_czk) AS drops,
        count(*) FILTER (WHERE ps.price_czk > ps.prev_price_czk) AS rises,
        count(*)                                                 AS changes,
        count(*) FILTER (WHERE ps.scraped_at >= now() - interval '30 days')  AS changes_30d,
        count(*) FILTER (WHERE ps.scraped_at >= now() - interval '90 days')  AS changes_90d,
        count(*) FILTER (WHERE ps.scraped_at >= now() - interval '365 days') AS changes_365d,
        max((ps.prev_price_czk - ps.price_czk)::numeric / ps.prev_price_czk * 100)
          FILTER (WHERE ps.price_czk < ps.prev_price_czk)        AS max_drop_pct
      FROM canon c
      JOIN listing_price_steps ps ON ps.listing_id = c.id
      GROUP BY c.property_id
    ),
    -- The headline delta: first-to-last of the canonical advert's own priced snapshots, the
    -- series its price is the last point of. (No literal percent sign in this comment on
    -- purpose -- prose percent inside executed SQL is an `incomplete placeholder` crash in
    -- psycopg; tests/test_sql_placeholders.py guards it.) NULL under two priced snapshots.
    canon_span AS (
      SELECT
        c.property_id AS pid,
        (array_agg(s.price_czk ORDER BY s.scraped_at, s.id))[1]           AS first_price,
        (array_agg(s.price_czk ORDER BY s.scraped_at DESC, s.id DESC))[1] AS last_price,
        count(*)                                                          AS price_points
      FROM canon c
      JOIN listing_snapshots s ON s.listing_id = c.id
      WHERE s.price_czk IS NOT NULL
      GROUP BY c.property_id
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
      distinct_site_count = r.distinct_site_count,
      first_seen_at       = r.first_seen_at,
      last_seen_at        = r.last_seen_at,
      repr_listing_id     = c.sreality_id,
      repr_since          = CASE WHEN p.repr_listing_ref_id <> c.id THEN now() ELSE p.repr_since END,
      repr_listing_ref_id = c.id,
      category_main       = c.category_main,
      category_type       = c.category_type,
      category_sub_cb     = c.category_sub_cb,
      subtype             = c.subtype,
      disposition         = c.disposition,
      area_m2             = c.area_m2,
      current_price_czk   = c.price_czk,
      price_per_m2_source_listing_id = price_per_m2_source_id(c.price_czk, c.area_m2, c.id),
      condition           = c.condition,
      building_condition_level  = c.building_condition_level,
      apartment_condition_level = c.apartment_condition_level,
      furnished           = c.furnished,
      source              = c.source,
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
      stats_computed_at   = now()
    FROM child_agg r
    JOIN canon c ON c.property_id = r.pid
    LEFT JOIN price_hist ph ON ph.pid = r.pid
    LEFT JOIN canon_span cs ON cs.pid = r.pid
    LEFT JOIN changes ch ON ch.pid = r.pid
    WHERE p.id = r.pid
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


def _attach_stragglers(conn: Any) -> int:
    """Give every property_id-NULL listing its own singleton property, recomputed at birth.
    No cross-listing matching happens here, ever (CLAUDE.md rule 15)."""
    # Birth, link and first recompute in ONE transaction: a replay after a failure (db.
    # run_resilient replays this op) finds the stragglers still unlinked, never a linked bare
    # row that Browse could show before its recompute.
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(_STRAGGLERS_SQL)
            stragglers = [int(r[0]) for r in cur.fetchall()]
        born = db.create_singleton_properties(conn, stragglers) if stragglers else []
        if born:
            _run_recompute_statement(conn, _RECOMPUTE_SCOPED_SQL, {"ids": born})
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
        # Browse reads `browse_list`, not `properties`, and pg_cron rebuilds it
        # wholesale only every 15 min — so without this a recompute reached Browse
        # a measured 11.7 min later on average (94 rebuilds / 24 h: best 2.3,
        # worst 36.6). Patching here, the one place that knows which properties
        # just changed, puts it on this lane's own cadence instead. Ordered BEFORE
        # the dirty delete so a crash between the two replays both.
        # A FAST PATH, not a guarantee, and two costs worth knowing. (i) The rebuild
        # snapshots `browse_projection` into `browse_list_next` at its START and
        # renames at its END, so a patch committed inside that window lands on the
        # doomed table and is superseded WITHOUT erroring (nothing logs): 283
        # succeeded rebuilds / 72 h, mean 237 s against a 900 s cadence = in flight
        # ~26% of wall-clock, so the seen-to-Browse gain is bimodal, not a flat
        # ~2 min. (ii) A full slice is not free: `batch_size` defaults to 2000 (the
        # GH cron passes exactly that) and the SELECT half alone is ~100 ms warm /
        # ~270 ms cold over ~23k buffers, holding ROW EXCLUSIVE on `browse_list` —
        # the one lock the rebuild's `drop table` waits behind. The live dirty depth
        # is a couple of dozen; only a post-freeze backlog claims a full slice.
        sync_browse_list(conn, ids)
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
    maintenance lane: attach new stragglers + recompute the dirty set + patch `browse_list` for exactly the
    properties it recomputed, so a change reaches Browse on this lane's cadence
    rather than at the next */15 wholesale rebuild.
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
            # attempts=2, like sweep.batch: a doomed attach reds in ~4 min instead of ~8.
            attached = step(_attach_stragglers, "sweep.attach", attempts=2)
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
            # `nonlocal conn` keeps this pointing at the last SUCCESSFULLY returned
            # connection, but run_resilient closes both the original and its
            # replacement when it exhausts its budget on a dropped socket — so the
            # release still needs a way to open one of its own.
            _release_lease(conn, holder, reconnect)

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
