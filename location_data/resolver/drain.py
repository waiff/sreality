"""`location_resolve_incremental` — the `dirty_locations` drain (03 §3.14.1, 00 §9).

This is the job that makes location live. Without it a newly scraped listing gets claims in
the detail drain and **never gets a resolution** — no projection row, invisible to Browse,
map, watchdog and dedup.

Batch discipline (03 §3.14.3, learned three times):

* bounded slices claimed with `FOR UPDATE SKIP LOCKED`, ONE transaction per batch, and the
  projection rebuilt in the same transaction as the resolution — that is what gives an
  operator edit read-your-writes;
* one SAVEPOINT for the optimistic slice, and a per-listing SAVEPOINT on the retry, so one
  poisonous listing still costs its own row and not the batch;
* `statement_timeout` / `lock_timeout` set with `SET LOCAL` INSIDE the batch transaction
  (outside one, on this codebase's autocommit connections, `SET LOCAL` is a silent no-op);
* a lease-row CAS on `location_jobs`, never a session advisory lock — a lock taken on one
  pooler backend and released on another strands;
* **judge the queue by OLDEST-ROW AGE, not by length** (the repo's standing rule); the run
  log prints it every batch;
* the slice is ordered `(enqueued_at, listing_id)` — a bare timestamp sort reshuffles on
  every call, because a batch enqueue shares one `now()` and the tie order is arbitrary;
* resumable by construction: the queue IS the cursor, and a failed row comes back with a
  backoff rather than blocking the slice.

Throughput discipline (measured, never assumed). The cost is POOLER ROUND TRIPS, not CPU, and
round 2 measured the constant: production run 31480587021 spent 225 ms per listing on three
statements with NO server-side work at all (SAVEPOINT, RELEASE SAVEPOINT, one DELETE by
primary key), i.e. **~75 ms of network round trip** between a GitHub-hosted runner and the
Frankfurt pooler, while the same run's registry questions cost 0.02-0.5 ms each server-side.
So the rate is `1 / (75 ms x round trips per listing)` and nothing else:

    round 1 (0.8/s):  ~16 trips — 6.5 registry misses + ~6.6 writes + 3 SAVEPOINT/DELETE
    round 2 (target): ~1 trip   — everything below is per SLICE, not per listing

Every lever is I/O-layer only: the pure core's inputs and answers are unchanged, so
deterministic replay stays bit-for-bit.

1. **`connect_session()`**, the repo's hot-loop pattern (`scraper/main.py:_run_full`): the
   SESSION-mode pooler gives a dedicated backend, so psycopg3's default `prepare_threshold`
   applies and the ~40 recurring statements are server-side prepared once instead of parsed
   and planned per listing. Falls back to the transaction pooler, loudly, without the secret.
2. **Corpus constants are loaded ONCE per run** — policies, constants, granularity ranks, the
   current registry version and the epoch — and the registry/collision views are memoized for
   the run (`resolve_db.RunCache`), because both mirrors are immutable at the pinned version.
3. **Per-slice prefetch** of everything a listing needs BEFORE it writes anything (claims,
   previous consumed inputs, `listings.property_id`, open findings): four queries per slice
   instead of four per listing. `location_disputed` is deliberately NOT prefetched — it is a
   read-your-writes read of the contradictions this run just wrote.
4. **Per-slice WARM** (`resolve_db.warm_points`): the five registry questions keyed on the
   listing's own coordinate cannot be shared between listings — that, not a too-narrow key,
   is why the run cache plateaus at ~62 % — but they can all be asked for the whole slice in
   one round trip each. Warming stores the same key/value the lazy path computes.
5. **Slice-batched writes** (`_write_slice`): compute is split from write, so the slice's
   resolutions / candidates / contradictions / dispositions / projections / property rebuilds
   are ~10 statements for 250 listings instead of ~7 each. Optimistic — one savepoint for the
   slice, and on any failure the per-listing path replays it with a SAVEPOINT each.
6. **`executemany`** for every batched writer, which psycopg pipelines into one round trip.

No `--workers`: a second connection could claim a disjoint slice safely (`FOR UPDATE SKIP
LOCKED`), but two workers holding two listings of the SAME property would race
`_rebuild_properties` — each reads the member set, then both write `property_location_current`
— and a stale read could publish the wrong winner. The property rebuild is the drain's one
cross-listing write, so the drain stays single-connection until that is designed, not bolted
on.

The slice is a bounded 250 by default rather than "as large as possible": the batch is ONE
transaction holding `FOR UPDATE` locks on its `dirty_locations` rows, and the claims-intake
enqueue (`ON CONFLICT (listing_id) DO NOTHING`) blocks on a locked row, so the slice's
duration is a latency budget for the intake. Beyond ~200 the per-batch fixed cost is already
under 1 % — a bigger slice buys nothing and lengthens that window.

CLI:  python -m location_data.resolver.drain [--max-seconds N] [--batch-size N]
                                             [--listing-id N] [--dry-run]
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg

from location_data import loader_db
from location_data.resolver import core, lease, normalize, projection, reconciler, resolve_db
from location_data.resolver.types import Claim, ResolverContext
from location_data.resolver.version import (
    POLICY_VERSION_DEFAULT,
    RECONCILER_VERSION,
    RESOLVER_VERSION,
)
from scraper import db

LOG = logging.getLogger("location_data.resolver.drain")

JOB_NAME = "location_resolve_incremental"
CONCURRENCY_GROUP = "location-resolve"
DEFAULT_BATCH = 250
DEFAULT_MAX_SECONDS = 600
BACKOFF_SECONDS = (60, 300, 900, 3600, 21600)

_CLAIM_SLICE_SQL = """
SELECT listing_id, attempts
  FROM dirty_locations
 WHERE next_eligible_at <= now()
 ORDER BY enqueued_at, listing_id
 FOR UPDATE SKIP LOCKED
 LIMIT %s
"""

_DELETE_ROW_SQL = "DELETE FROM dirty_locations WHERE listing_id = %s"

_FAIL_ROW_SQL = """
UPDATE dirty_locations
   SET attempts = attempts + 1,
       last_error = %s,
       next_eligible_at = now() + make_interval(secs => %s)
 WHERE listing_id = %s
"""

_QUEUE_HEALTH_SQL = """
SELECT count(*), coalesce(extract(epoch from now() - min(enqueued_at)), 0)
  FROM dirty_locations
"""

_MEMBER_COLUMNS = """
       listing_id, source, ST_Y(geom), ST_X(geom), granularity::text, position_source::text,
       blur_evidence::text, match_confidence::text, uncertainty_radius_m,
       radius_semantics::text, position_licence_class::text, ruian_adm_kod,
       stavebni_objekt_kod, obec_kod, cast_obce_kod, okres_kod, kraj_kod, admin_path::text,
       admin_assignment_method::text, street_name, psc, display_label, place_search_text,
       country_code, country_status::text, pin_shared_by_n, geo_blockable, render_as,
       position_quality_class"""

# Every touched property's members in ONE read. It also DE-DUPLICATES the rebuild: the
# per-listing path rebuilt a property once per member it had in the slice, and each rebuild
# re-read the whole member set. The final row is identical either way — the rebuild is a pure
# function of the member set, and running it once at the end sees every member this slice
# rewrote instead of only those written so far.
_PROPERTY_MEMBERS_BULK_SQL = f"""
SELECT property_id, {_MEMBER_COLUMNS}
  FROM listing_location_current
 WHERE property_id = ANY(%s::bigint[])
 ORDER BY property_id, listing_id
"""

_DELETE_ROWS_SQL = "DELETE FROM dirty_locations WHERE listing_id = ANY(%s::bigint[])"

# Statement budgets, all applied with `SET LOCAL` INSIDE a transaction (outside one, on
# this codebase's autocommit connections, `SET LOCAL` is a silent no-op).
#
# The BATCH budget already existed and did its job — the 2026-08-10 stall (run 31439340945,
# 30 minutes of silence after `RESOLVE ok listing_id=93951`) was not an unguarded statement
# inside the batch. What WAS unguarded is everything the drain runs OUTSIDE a batch
# transaction: the per-batch `_QUEUE_HEALTH_SQL` count, the run-start constant loads, and
# the full-sweep INSERT. Those ran on the pooler default (no ceiling), so any one of them
# could wait indefinitely under the IO pressure four concurrent location lanes were putting
# on the instance. Each now runs in its own bounded transaction.
#
# 30 s stays the batch default rather than the loosen-to-90 s the incident write-up
# floated: a per-listing statement that has not answered in 30 s has already blown the
# ~1.2 s/listing budget by 25x, and a QueryCanceled here is caught by the per-listing
# SAVEPOINT and costs one row, not the batch. Overridable per environment for the one case
# the default cannot serve — a deliberately slow backfill on a quiet instance.
BATCH_TIMEOUT_ENV = "LOCATION_RESOLVE_BATCH_TIMEOUT_S"
DEFAULT_BATCH_TIMEOUT_S = 30
# The sweep is ONE `INSERT ... SELECT` over the whole claims corpus anti-joined against the
# projection; minutes is normal for it and only for it.
SWEEP_TIMEOUT_ENV = "LOCATION_RESOLVE_SWEEP_TIMEOUT_S"
DEFAULT_SWEEP_TIMEOUT_S = 900
# Listing-id width of one corpus-sweep window (see _FULL_SWEEP_SQL).
DEFAULT_SWEEP_WINDOW = 250_000
LOCK_TIMEOUT_S = 5


def _batch_guc(seconds: int) -> tuple[str, ...]:
    return (
        f"SET LOCAL statement_timeout = '{int(seconds)}s'",
        f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT_S}s'",
    )


def _batch_timeout_s() -> int:
    return loader_db.env_timeout_s(BATCH_TIMEOUT_ENV, DEFAULT_BATCH_TIMEOUT_S)


@contextlib.contextmanager
def _bounded(conn: psycopg.Connection, seconds: int) -> Iterator[psycopg.Cursor]:
    """One bounded transaction for work that would otherwise run bare on autocommit."""
    with conn.transaction():
        with conn.cursor() as cur:
            for statement in _batch_guc(seconds):
                cur.execute(statement)
            yield cur


# `location_resolve_sweep` — the daily reconcile backstop for lost enqueues. Everything
# whose projection is missing or was built at a version tuple that is no longer current.
#
# Walked in LISTING-ID WINDOWS, never as one statement. This has to drive off the claim
# corpus — a listing with NO projection exists nowhere else — and that corpus is what the
# W2-13 archive sweeps grow by ~150k rows an hour, so the single-statement form stopped
# fitting under the 900 s sweep ceiling on 2026-09-10 (the kraj-scoped cousin died first,
# run 34459466027; #1384 inverted that one onto the projection, which this one cannot do).
# A window bounds the work by id range: the claim index range-scans it, the anti-join
# touches only that range's rows, and each window commits on its own, so an interrupted
# sweep resumes by simply re-running (ON CONFLICT makes every window idempotent).
#
# Both statements pre-filter `NOT EXISTS (... dirty_locations ...)` rather than leaning on
# ON CONFLICT alone. Since Decision 8b a drain slice is nearly always in flight (the Railway
# lane), holding its 250 rows FOR UPDATE for the whole slice; an INSERT ... ON CONFLICT on
# one of those keys must WAIT for that transaction to learn whether the row survives, and
# the sweep's 5 s lock_timeout then kills the window. The NOT EXISTS is an MVCC read — it
# sees the queued row and skips it without touching the lock. ON CONFLICT stays as the net
# for the race between that read and the write; `_execute_window` retries that case.
_FULL_SWEEP_SQL = """
INSERT INTO dirty_locations (listing_id, reason)
SELECT DISTINCT c.listing_id, 'full_sweep'
  FROM location_claims_live c
  LEFT JOIN listing_location_current p ON p.listing_id = c.listing_id
 WHERE (p.listing_id IS NULL
        OR p.resolver_version <> %s
        OR p.policy_version <> %s
        OR p.registry_version_id <> %s)
   AND c.listing_id > %s AND c.listing_id <= %s
   AND NOT EXISTS (SELECT 1 FROM dirty_locations d WHERE d.listing_id = c.listing_id)
ON CONFLICT (listing_id) DO NOTHING
"""

# `location_resolve_sweep`'s second statement: the listings the engine has NEVER LOOKED AT.
# Every other enqueue presupposes a claim (intake, un-shadow, the stale sweep above drives
# off `location_claims_live`), so a listing whose payload yielded no claim — a portal whose
# page-reading entries only run on a manual sweep, a contract shadowed at intake time, an
# intake refusal — had no projection row and nothing that would ever notice (10,679 active
# rows on 2026-09-11). This drives off `listings` itself; the drain then writes a `no_input`
# row for a claimless listing, so coverage becomes `count(projection) = count(active)`.
_ORPHAN_SWEEP_SQL = """
INSERT INTO dirty_locations (listing_id, reason)
SELECT l.id, 'full_sweep'
  FROM listings l
  LEFT JOIN listing_location_current p ON p.listing_id = l.id
 WHERE l.is_active
   AND p.listing_id IS NULL
   AND l.id > %s AND l.id <= %s
   AND NOT EXISTS (SELECT 1 FROM dirty_locations d WHERE d.listing_id = l.id)
ON CONFLICT (listing_id) DO NOTHING
"""

# The window walk's upper bound. `listings.id` bounds every claim's listing_id and max()
# over its primary key is an index probe — max() over the claims VIEW would be the very
# corpus scan the windows exist to avoid.
_SWEEP_UPPER_BOUND_SQL = "SELECT coalesce(max(id), 0) FROM listings"

# The same stale set, restricted to a few kraje — a resolver bump can then be rolled out on
# a real cohort before the corpus. A listing with NO projection has no `kraj_kod` to filter
# on, so a kraj-scoped sweep cannot honestly claim it; those rows are the unscoped sweep's
# job. This drives off the PROJECTION and probes the claims with a correlated EXISTS, which
# is the whole point of the rewrite:
#
#   The first shape drove off `location_claims_live` and DISTINCT'd millions of claim rows
#   down to a listing id set before the join could discard all but two kraje. That is work
#   proportional to the CLAIM corpus for an answer proportional to the PROJECTION, and the
#   claim corpus is the thing the W2-13 archive sweeps grow by ~150k rows an hour. It went
#   from "minutes of honest work" to a `QueryCanceled` at the 900 s ceiling on 2026-09-10
#   (run 34459466027, the resolver:v2 rollout for kraje 19+27), which is a sweep that can
#   never run again rather than a sweep that is slow.
#
#   Inverted, the driving side is `listing_location_current` filtered to the kraje — and
#   `listing_id` is its PRIMARY KEY, so the DISTINCT is not merely cheaper, it is
#   unnecessary. The claim table's own `(listing_id, ...)` index serves the EXISTS probe.
#
# MATERIALIZED is an optimizer fence, deliberately: `location_claims_live` is a view over a
# view (unretracted, minus shadowed contracts), so without the fence the planner is free to
# flatten the EXISTS back into a hash semi-join over the whole claim corpus — reintroducing
# exactly the plan this rewrite exists to prevent. There is no index on `kraj_kod`; the
# filtered scan of the projection is the accepted cost and is bounded by the table, not by
# the claims.
_FULL_SWEEP_KRAJE_SQL = """
WITH stale AS MATERIALIZED (
  SELECT p.listing_id
    FROM listing_location_current p
   WHERE p.kraj_kod = ANY(%s::bigint[])
     AND (p.resolver_version <> %s
          OR p.policy_version <> %s
          OR p.registry_version_id <> %s)
)
INSERT INTO dirty_locations (listing_id, reason)
SELECT s.listing_id, 'full_sweep'
  FROM stale s
 WHERE EXISTS (SELECT 1 FROM location_claims_live c WHERE c.listing_id = s.listing_id)
   AND NOT EXISTS (SELECT 1 FROM dirty_locations d WHERE d.listing_id = s.listing_id)
ON CONFLICT (listing_id) DO NOTHING
"""


@dataclass(slots=True)
class DrainStats:
    claimed: int = 0
    resolved: int = 0
    failed: int = 0
    batches: int = 0
    fallbacks: int = 0
    # Phase timings, so the NEXT production run measures itself instead of being re-diagnosed.
    prefetch_seconds: float = 0.0
    warm_seconds: float = 0.0
    core_seconds: float = 0.0
    write_seconds: float = 0.0
    seconds: float = 0.0

    @property
    def rate(self) -> float:
        return 0.0 if self.seconds <= 0 else self.claimed / self.seconds


@dataclass(slots=True)
class _Slice:
    """Everything the slice's listings need that can be read BEFORE any of them writes."""

    claims: dict[int, list[Claim]] = field(default_factory=dict)
    previous_inputs: dict[int, tuple[str, int, str, int]] = field(default_factory=dict)
    property_ids: dict[int, int | None] = field(default_factory=dict)
    open_keys: dict[int, tuple[tuple[str, str], ...]] = field(default_factory=dict)
    sources: dict[int, str] = field(default_factory=dict)


@dataclass(slots=True)
class _Computed:
    """One listing's resolution and everything derived from it, BEFORE anything is written.

    Splitting compute from write is what lets the writes be batched across the slice — and
    it is also what makes the fallback cheap: a slice that has to retry per listing replays
    these, it does not re-resolve."""

    listing_id: int
    resolution: Any
    detections: list[Any]
    open_before: list[str]
    property_id: int | None
    cluster: Any
    threshold_n: int
    inputs_changed: bool


def _prefetch(conn: psycopg.Connection, listing_ids: list[int]) -> _Slice:
    return _Slice(
        claims=resolve_db.load_claims_bulk(conn, listing_ids),
        previous_inputs=resolve_db.previous_consumed_inputs_bulk(conn, listing_ids),
        property_ids=resolve_db.property_ids_bulk(conn, listing_ids),
        open_keys=resolve_db.open_dedupe_keys_bulk(conn, listing_ids),
        sources=resolve_db.sources_bulk(conn, listing_ids),
    )


def _warm(slice_: _Slice, ctx: ResolverContext, cache: resolve_db.RunCache) -> None:
    """Pre-answer the five coordinate-keyed registry questions for the whole slice.

    They are the ones the run cache can never share between listings, and they were 5 of the
    ~16 round trips a listing cost. `warm_points` is a no-op on a view that is not the SQL
    one (the mini-mirror tests), and warming a point the core never asks about only costs
    server time — never an answer."""
    registry = getattr(ctx.registry, "_inner", None)
    collision = getattr(ctx.collision, "_inner", None)
    if not isinstance(registry, resolve_db.SqlRegistryView):
        return
    if not isinstance(collision, resolve_db.SqlCollisionEvidence):
        return
    points = sorted({
        (claim.source, float(claim.lat), float(claim.lon))
        for claims in slice_.claims.values()
        for claim in claims
        if claim.lat is not None and claim.lon is not None
    })
    resolve_db.warm_points(registry, collision, cache, points)


def run(
    conn: psycopg.Connection,
    *,
    batch_size: int = DEFAULT_BATCH,
    max_seconds: int = DEFAULT_MAX_SECONDS,
    policy_version: str = POLICY_VERSION_DEFAULT,
    dry_run: bool = False,
    only_listing_id: int | None = None,
) -> DrainStats:
    batch_timeout_s = _batch_timeout_s()
    # Corpus constants: read ONCE for the run, never per listing. Everything here is either
    # pinned by version (registry, epoch) or a small operator-curated table, so a re-read per
    # listing would buy nothing and cost a round trip each. Bounded as one transaction —
    # these are the run's FIRST statements, and a run that hangs here never logs anything at
    # all, which is the least diagnosable failure the lane has.
    with _bounded(conn, batch_timeout_s):
        registry_version_id, registry_label = resolve_db.current_registry_version(conn)
        epoch_id = resolve_db.current_epoch(conn)
    if epoch_id is None:
        raise RuntimeError(
            "no pin_cluster_epochs row: mint one with `python -m location_data.resolver."
            "epoch_job` first — collision_epoch_id is NOT NULL and part of the identity"
        )
    cache = resolve_db.RunCache()
    with _bounded(conn, batch_timeout_s):
        ctx_base = _context(conn, registry_version_id, epoch_id, policy_version, cache)
    stats = DrainStats()
    started = time.monotonic()
    LOG.info(
        "DRAIN start batch_size=%d max_seconds=%d statement_timeout=%ds registry_version=%s",
        batch_size, max_seconds, batch_timeout_s, registry_label,
    )

    if only_listing_id is not None:
        with conn.transaction():
            with conn.cursor() as cur:
                for statement in _batch_guc(batch_timeout_s):
                    cur.execute(statement)
            _resolve_one(
                conn, only_listing_id, ctx_base, registry_version_id, registry_label,
                epoch_id, policy_version, _prefetch(conn, [only_listing_id]), stats,
                dry_run=dry_run,
            )
            stats.claimed = stats.resolved = 1
        stats.seconds = time.monotonic() - started
        return stats

    while time.monotonic() - started < max_seconds:
        depth, oldest = _queue_health(conn, batch_timeout_s)
        LOG.info("QUEUE depth=%d oldest_age_s=%.0f", depth, oldest)
        batch_started = time.monotonic()
        batch_before = (stats.resolved, stats.failed)
        with conn.transaction():
            with conn.cursor() as cur:
                for statement in _batch_guc(batch_timeout_s):
                    cur.execute(statement)
                cur.execute(_CLAIM_SLICE_SQL, (batch_size,))
                rows = cur.fetchall()
            if not rows:
                LOG.info("QUEUE empty")
                break
            stats.batches += 1
            prefetch_started = time.monotonic()
            slice_ = _prefetch(conn, [int(listing_id) for listing_id, _ in rows])
            stats.prefetch_seconds += time.monotonic() - prefetch_started
            warm_started = time.monotonic()
            try:
                # Its own SAVEPOINT: the warm is an OPTIMISATION, and a statement timeout
                # inside it would otherwise abort the batch transaction and end the run.
                # Rolled back, every point simply falls through to its own lazy query.
                with conn.transaction():
                    _warm(slice_, ctx_base, cache)
            except Exception as exc:  # noqa: BLE001 - degrade to the lazy path, never die
                LOG.warning("WARM failed, resolving with per-point lookups: %s", exc)
            stats.warm_seconds += time.monotonic() - warm_started
            _run_slice(
                conn, rows, ctx_base, registry_version_id, registry_label, epoch_id,
                policy_version, slice_, stats, dry_run=dry_run,
            )
        _log_batch(stats, cache, ctx_base, len(rows), batch_before,
                   time.monotonic() - batch_started)
    stats.seconds = time.monotonic() - started
    LOG.info(
        "DRAIN done batches=%d claimed=%d resolved=%d failed=%d fallbacks=%d %.1fs "
        "rate=%.1f/s prefetch=%.1fs warm=%.1fs core=%.1fs write=%.1fs registry_q=%d "
        "registry_hit=%.0f%%",
        stats.batches, stats.claimed, stats.resolved, stats.failed, stats.fallbacks,
        stats.seconds, stats.rate, stats.prefetch_seconds, stats.warm_seconds,
        stats.core_seconds, stats.write_seconds, cache.misses, 100.0 * cache.hit_rate,
    )
    LOG.info("DRAIN queries %s", _query_stats(ctx_base).report())
    LOG.info("DRAIN cache misses %s", cache.report())
    return stats


def _run_slice(
    conn: psycopg.Connection,
    rows: list[tuple[Any, Any]],
    ctx: ResolverContext,
    registry_version_id: int,
    registry_label: str,
    epoch_id: int,
    policy_version: str,
    slice_: _Slice,
    stats: DrainStats,
    *,
    dry_run: bool,
) -> None:
    """Optimistic: resolve the whole slice, then write it in ~12 statements instead of ~7 per
    listing. On ANY failure the savepoint rolls the slice's writes back and the proven
    per-listing path re-runs it with a SAVEPOINT each, so a poisoned listing still costs one
    row and not the batch — it just costs the slice one extra pass. The run that motivated
    this measured 1 failure in 2,750 listings, so the optimistic path is the one that matters
    and the fallback is the safety net, not the design."""
    stats.claimed += len(rows)
    listing_ids = [int(listing_id) for listing_id, _ in rows]
    try:
        with conn.transaction():  # SAVEPOINT for the WHOLE slice
            core_started = time.monotonic()
            computed = [
                item
                for listing_id in listing_ids
                if (item := _compute_one(
                    listing_id, ctx, registry_version_id, epoch_id, policy_version,
                    slice_, dry_run=dry_run,
                )) is not None
            ]
            stats.core_seconds += time.monotonic() - core_started
            if not dry_run:
                write_started = time.monotonic()
                _write_slice(conn, computed, ctx, registry_version_id, registry_label)
                with conn.cursor() as cur:
                    cur.execute(_DELETE_ROWS_SQL, (listing_ids,))
                stats.write_seconds += time.monotonic() - write_started
        stats.resolved += len(rows)
        return
    except Exception as exc:  # noqa: BLE001 - fall back to per-listing isolation
        stats.fallbacks += 1
        LOG.warning(
            "SLICE batch failed (n=%d), retrying per listing with SAVEPOINTs: %s",
            len(rows), exc,
        )

    for listing_id, attempts in rows:
        try:
            with conn.transaction():  # SAVEPOINT: one bad row, one bad row
                _resolve_one(
                    conn, int(listing_id), ctx, registry_version_id, registry_label,
                    epoch_id, policy_version, slice_, stats, dry_run=dry_run,
                )
                if not dry_run:
                    with conn.cursor() as cur:
                        cur.execute(_DELETE_ROW_SQL, (listing_id,))
            stats.resolved += 1
        except Exception as exc:  # noqa: BLE001 - the row must not poison the batch
            stats.failed += 1
            LOG.warning("RESOLVE failed listing_id=%s: %s", listing_id, exc)
            backoff = BACKOFF_SECONDS[min(int(attempts), len(BACKOFF_SECONDS) - 1)]
            with conn.cursor() as cur:
                cur.execute(_FAIL_ROW_SQL, (str(exc)[:500], backoff, listing_id))


def _query_stats(ctx: ResolverContext) -> resolve_db.QueryStats:
    inner = getattr(ctx.registry, "_inner", None)
    stats = getattr(inner, "stats", None)
    return stats if isinstance(stats, resolve_db.QueryStats) else resolve_db.QueryStats()


def _log_batch(
    stats: DrainStats,
    cache: resolve_db.RunCache,
    ctx: ResolverContext,
    n: int,
    before: tuple[int, int],
    elapsed: float,
) -> None:
    """Per-batch self-measurement: the next production run reports its own listings/s and
    where the time went — including WHICH query kind, which round 1's aggregate-only line
    could not say."""
    LOG.info(
        "BATCH n=%d ok=%d fail=%d %.1fs rate=%.1f/s cum(prefetch=%.1fs warm=%.1fs "
        "core=%.1fs write=%.1fs) registry_q=%d registry_hit=%.0f%% registry_wait=%.1fs",
        n, stats.resolved - before[0], stats.failed - before[1], elapsed,
        0.0 if elapsed <= 0 else n / elapsed, stats.prefetch_seconds, stats.warm_seconds,
        stats.core_seconds, stats.write_seconds, cache.misses, 100.0 * cache.hit_rate,
        cache.seconds,
    )
    LOG.info("BATCH queries %s", _query_stats(ctx).report())


def _context(
    conn: psycopg.Connection,
    registry_version_id: int,
    epoch_id: int,
    policy_version: str,
    cache: resolve_db.RunCache,
) -> ResolverContext:
    query_stats = resolve_db.QueryStats()
    return ResolverContext(
        registry=resolve_db.CachedRegistryView(
            resolve_db.SqlRegistryView(conn, registry_version_id, query_stats), cache
        ),
        constants=resolve_db.load_constants(conn),
        field_policy=resolve_db.load_field_policy(conn, policy_version),
        uncertainty_policy=resolve_db.load_uncertainty_policy(conn, policy_version),
        collision_policy=resolve_db.load_collision_policy(conn, policy_version),
        collision=resolve_db.CachedCollisionEvidence(
            resolve_db.SqlCollisionEvidence(conn, epoch_id, query_stats), cache
        ),
        granularity_rank=resolve_db.load_granularity_rank(conn),
    )


def _resolve_one(
    conn: psycopg.Connection,
    listing_id: int,
    ctx: ResolverContext,
    registry_version_id: int,
    registry_label: str,
    epoch_id: int,
    policy_version: str,
    slice_: _Slice,
    stats: DrainStats,
    *,
    dry_run: bool,
) -> None:
    """ONE listing, compute + write — the `--listing-id` path and the slice fallback.

    It is `_write_slice` with a one-element slice, deliberately: two write paths would be two
    places for the ordering the projection depends on to drift apart."""
    core_started = time.monotonic()
    item = _compute_one(
        listing_id, ctx, registry_version_id, epoch_id, policy_version, slice_,
        dry_run=dry_run,
    )
    stats.core_seconds += time.monotonic() - core_started
    if item is None:
        return
    write_started = time.monotonic()
    _write_slice(conn, [item], ctx, registry_version_id, registry_label)
    stats.write_seconds += time.monotonic() - write_started


def _compute_one(
    listing_id: int,
    ctx: ResolverContext,
    registry_version_id: int,
    epoch_id: int,
    policy_version: str,
    slice_: _Slice,
    *,
    dry_run: bool,
) -> _Computed | None:
    """The PURE half of a listing: S1-S9 plus everything derived from them. Writes nothing,
    so the whole slice can be computed before the first statement goes out."""
    claims = slice_.claims.get(listing_id, [])
    if not claims:
        # A queued listing with no live claim is an ANSWER, not a skip. Until 2026-09-11 it
        # was dropped from the queue and either kept a stale row (re-shadow, retraction) or
        # never got one at all — the 10,679 active orphans the audit measured. A `no_input`
        # resolution states "nothing to go on" (granularity unknown, no position), so
        # coverage is measurable and the orphan sweep does not re-enqueue it every night.
        LOG.info("RESOLVE no_input listing_id=%s reason=no_claims", listing_id)
    resolution = core.resolve(
        claims,
        ctx,
        resolver_version=RESOLVER_VERSION,
        registry_version_id=registry_version_id,
        policy_version=policy_version,
        collision_epoch_id=epoch_id,
        listing_id=listing_id,
        source=slice_.sources.get(listing_id, "unknown"),
    )
    if dry_run:
        LOG.info(
            "RESOLVE dry listing_id=%s status=%s granularity=%s hash=%s",
            listing_id, resolution.status, resolution.precision.granularity,
            resolution.content_hash[:12],
        )
        return None
    detections, evaluated_rules = reconciler.run_with_coverage(
        resolution, claims, normalize.normalize_all(claims), registry=ctx.registry
    )
    cluster = (
        ctx.collision.for_point(resolution.source, resolution.position.lat, resolution.position.lon)
        if ctx.collision and resolution.position.lat is not None
        else None
    )
    return _Computed(
        listing_id=listing_id,
        resolution=resolution,
        detections=list(detections),
        open_before=resolve_db.filter_open_keys(
            slice_.open_keys.get(listing_id, ()), rules=evaluated_rules
        ),
        property_id=slice_.property_ids.get(listing_id),
        cluster=cluster,
        threshold_n=ctx.collision_threshold(
            resolution.source, resolution.admin.obec_kod
        ).threshold_n,
        # Prefetched with the slice, which is strictly BEFORE this run rewrote any projection.
        inputs_changed=_inputs_changed(slice_.previous_inputs.get(listing_id), resolution),
    )


def _write_slice(
    conn: psycopg.Connection,
    computed: list[_Computed],
    ctx: ResolverContext,
    registry_version_id: int,
    registry_label: str,
) -> None:
    """The slice's ~7-statements-per-listing collapsed into ~10 for the whole slice.

    The ORDER is the part that carries meaning, and it is the per-listing order unchanged:
    resolutions before candidates (candidates need the id), contradictions and auto-closes
    before `location_disputed` (that read is a read of what this run just wrote, and a
    slice-start snapshot would serve a projection denying a major finding this very run
    raised), listing projections before the property rebuild (the rebuild reads them back).
    """
    if not computed:
        return
    resolutions = [item.resolution for item in computed]
    ids = resolve_db.write_resolutions_bulk(conn, resolutions)
    resolve_db.write_candidates_bulk(
        conn, [(ids[item.listing_id], item.resolution) for item in computed]
    )
    resolve_db.write_contradictions_bulk(
        conn,
        [(item.detections, item.property_id) for item in computed],
        reconciler_version=RECONCILER_VERSION,
        resolver_version=RESOLVER_VERSION,
        registry_version_id=registry_version_id,
    )
    resolve_db.append_auto_close(
        conn,
        [
            close
            for item in computed
            for close in reconciler.auto_close(
                item.open_before, item.detections, inputs_changed=item.inputs_changed
            )
        ],
    )
    disputed = resolve_db.location_disputed_bulk(conn, [item.listing_id for item in computed])
    rows = [
        projection.build_listing_row(
            item.resolution,
            property_id=item.property_id,
            resolution_id=ids[item.listing_id],
            registry_version_label=registry_label,
            rank=ctx.granularity_rank,
            cluster=item.cluster,
            threshold_n=item.threshold_n,
            location_disputed=item.listing_id in disputed,
        )
        for item in computed
    ]
    resolve_db.upsert_listing_projections_bulk(conn, rows)
    _rebuild_properties(
        conn, sorted({item.property_id for item in computed if item.property_id is not None}), ctx
    )
    for item, row in zip(computed, rows):
        LOG.info(
            "RESOLVE ok listing_id=%s status=%s granularity=%s blockable=%s blocked_fields=%s",
            item.listing_id, item.resolution.status, row["granularity"], row["geo_blockable"],
            ",".join(item.resolution.survivorship_blocked) or "-",
        )


def _inputs_changed(
    previous: tuple[str, int, str, int] | None, resolution: Any
) -> bool:
    """00 §8.2: auto-close fires only when the CONSUMED inputs actually changed. With no
    previous projection there is nothing to compare, so nothing is closed — a finding is
    never retired on the strength of an assumption."""
    if previous is None:
        return False
    return previous != (
        resolution.claim_set_hash,
        resolution.registry_version_id,
        resolution.policy_version,
        resolution.collision_epoch_id,
    )


_MEMBER_FIELDS = (
    "listing_id", "source", "lat", "lon", "granularity", "position_source",
    "blur_evidence", "match_confidence", "uncertainty_radius_m", "radius_semantics",
    "position_licence_class", "ruian_adm_kod", "stavebni_objekt_kod", "obec_kod",
    "cast_obce_kod", "okres_kod", "kraj_kod", "admin_path", "admin_assignment_method",
    "street_name", "psc", "display_label", "place_search_text", "country_code",
    "country_status", "pin_shared_by_n", "geo_blockable", "render_as",
    # the property winner is chosen partly on this (projection._precision_key); reading
    # it as NULL for every member flattened the quality term to a constant.
    "position_quality_class",
)


def _rebuild_properties(
    conn: psycopg.Connection, property_ids: list[int], ctx: ResolverContext
) -> None:
    """Every property the slice touched, in two statements. `build_property_row` is a pure
    function of the member set, so one rebuild per property at the end of the slice produces
    exactly the row the per-listing path's last rebuild did."""
    if not property_ids:
        return
    grouped: dict[int, list[dict[str, Any]]] = {pid: [] for pid in property_ids}
    with conn.cursor() as cur:
        cur.execute(_PROPERTY_MEMBERS_BULK_SQL, (property_ids,))
        for row in cur.fetchall():
            grouped.setdefault(int(row[0]), []).append(dict(zip(_MEMBER_FIELDS, row[1:])))
    rows = [
        row
        for property_id in property_ids
        if (row := projection.build_property_row(
            property_id, grouped.get(property_id, []), rank=ctx.granularity_rank
        )) is not None
    ]
    resolve_db.upsert_property_projections_bulk(conn, rows)


def _queue_health(conn: psycopg.Connection, timeout_s: int) -> tuple[int, float]:
    """Bounded: this runs BETWEEN batches, i.e. on the autocommit connection where the
    batch transaction's `SET LOCAL` no longer applies. It is an observability read — the
    drain must never be unable to start a batch because counting the queue hung."""
    with _bounded(conn, timeout_s) as cur:
        cur.execute(_QUEUE_HEALTH_SQL)
        depth, oldest = cur.fetchone() or (0, 0)
    return int(depth), float(oldest)


SWEEP_WINDOW_ATTEMPTS = 3
SWEEP_WINDOW_RETRY_S = 2.0


def _execute_window(
    conn: psycopg.Connection, seconds: int, sql: str, params: tuple[Any, ...],
) -> int:
    """One bounded sweep statement, retried on a lock wait the NOT EXISTS pre-filter could
    not prevent (a row queued between our read and our write and immediately claimed)."""
    for attempt in range(1, SWEEP_WINDOW_ATTEMPTS + 1):
        try:
            with _bounded(conn, seconds) as cur:
                cur.execute(sql, params)
                return max(cur.rowcount, 0)
        except psycopg.errors.LockNotAvailable:
            if attempt == SWEEP_WINDOW_ATTEMPTS:
                raise
            LOG.warning("SWEEP window hit a lock wait (attempt %d); retrying", attempt)
            time.sleep(SWEEP_WINDOW_RETRY_S)
    raise AssertionError("unreachable")


def enqueue_full_sweep(
    conn: psycopg.Connection,
    *,
    policy_version: str,
    kraje: Sequence[int] | None = None,
    window: int = DEFAULT_SWEEP_WINDOW,
) -> int:
    """`location_resolve_sweep`: the backstop for lost enqueues. The incremental lane stays
    the primary path — this only re-enqueues what a version bump or a dropped enqueue left
    behind.

    Every statement here gets the sweep budget rather than the batch ceiling — but never
    the whole corpus in one statement; see `_FULL_SWEEP_SQL` for why it is windowed.

    `kraje` scopes the stale set to those `kraj_kod`s, which is how a resolver bump gets a
    side-by-side cohort before the corpus-wide run; that shape drives off the projection
    and is one bounded statement.
    """
    seconds = loader_db.env_timeout_s(SWEEP_TIMEOUT_ENV, DEFAULT_SWEEP_TIMEOUT_S)
    registry_version_id, _ = resolve_db.current_registry_version(conn)
    if kraje:
        with _bounded(conn, seconds) as cur:
            cur.execute(
                _FULL_SWEEP_KRAJE_SQL,
                (list(kraje), RESOLVER_VERSION, policy_version, registry_version_id),
            )
            enqueued = cur.rowcount
        LOG.info(
            "SWEEP enqueued=%d timeout=%ds kraje=%s",
            enqueued, seconds, ",".join(str(k) for k in kraje),
        )
        return enqueued
    if window <= 0:
        raise ValueError("sweep window must be positive")
    with conn.cursor() as cur:
        cur.execute(_SWEEP_UPPER_BOUND_SQL)
        row = cur.fetchone()
        upper = int(row[0]) if row else 0
    enqueued = 0
    after = 0
    windows = 0
    while after < upper:
        hi = min(after + window, upper)
        n = _execute_window(
            conn, seconds, _FULL_SWEEP_SQL,
            (RESOLVER_VERSION, policy_version, registry_version_id, after, hi),
        )
        enqueued += n
        windows += 1
        LOG.info("SWEEP window=(%d,%d] enqueued=%d", after, hi, n)
        after = hi
    LOG.info(
        "SWEEP enqueued=%d windows=%d width=%d timeout=%ds kraje=all",
        enqueued, windows, window, seconds,
    )
    return enqueued


def enqueue_orphan_sweep(conn: psycopg.Connection, *, window: int = DEFAULT_SWEEP_WINDOW) -> int:
    """Enqueue every ACTIVE listing with no projection row at all — see `_ORPHAN_SWEEP_SQL`.
    Windowed and bounded exactly like the stale sweep; idempotent, so re-running is safe."""
    if window <= 0:
        raise ValueError("sweep window must be positive")
    seconds = loader_db.env_timeout_s(SWEEP_TIMEOUT_ENV, DEFAULT_SWEEP_TIMEOUT_S)
    with conn.cursor() as cur:
        cur.execute(_SWEEP_UPPER_BOUND_SQL)
        row = cur.fetchone()
        upper = int(row[0]) if row else 0
    enqueued = windows = after = 0
    while after < upper:
        hi = min(after + window, upper)
        n = _execute_window(conn, seconds, _ORPHAN_SWEEP_SQL, (after, hi))
        enqueued += n
        windows += 1
        LOG.info("ORPHAN SWEEP window=(%d,%d] enqueued=%d", after, hi, n)
        after = hi
    LOG.info("ORPHAN SWEEP enqueued=%d windows=%d width=%d timeout=%ds", enqueued, windows, window, seconds)
    return enqueued


def _parse_kraje(raw: str) -> tuple[int, ...]:
    return tuple(int(part) for part in raw.replace(",", " ").split())


def open_connection() -> psycopg.Connection:
    """The hot loop wants a SESSION-mode connection so its ~40 recurring statements get
    server-side prepared (`scraper/main.py:_run_full` is the same pattern). The transaction
    pooler still WORKS — `connect_session()` falls back to it — it is just several times
    slower per listing, so the fallback is announced rather than silent."""
    if not os.environ.get("SUPABASE_DB_SESSION_URL"):
        LOG.warning(
            "SUPABASE_DB_SESSION_URL unset: falling back to the TRANSACTION pooler, where "
            "prepare_threshold=None forces every statement to be re-parsed per listing"
        )
    return db.connect_session()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="drain dirty_locations (S1-S9 + projection)")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH)
    parser.add_argument("--max-seconds", type=int, default=DEFAULT_MAX_SECONDS)
    parser.add_argument("--policy-version", default=POLICY_VERSION_DEFAULT)
    parser.add_argument("--listing-id", type=int, default=None)
    parser.add_argument("--full-sweep", action="store_true", help="enqueue the stale set first")
    parser.add_argument(
        "--orphan-sweep", action="store_true",
        help="also enqueue every active listing that has no projection row at all",
    )
    parser.add_argument(
        "--kraje", default="", help="full-sweep only: comma-separated kraj_kod scope"
    )
    parser.add_argument(
        "--sweep-window", type=int, default=DEFAULT_SWEEP_WINDOW,
        help="corpus-wide full-sweep only: listing-id width of one bounded window",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with open_connection() as conn:
        # The sweep ENQUEUES before the lease is even attempted. The lease guards the
        # DRAIN (claim, resolve, write); the enqueue is an idempotent insert whose only
        # exclusion need — the archive sweep's bulk writes into the same table — is the
        # `location-batch` Actions group this mode runs in. Since Decision 8b the Railway
        # lane holds the drain lease ~94% of the time, so an enqueue gated behind it ran on
        # 2026-09-10 as a 20-second "DRAIN skipped" success that enqueued nothing (run
        # 34482389394). Enqueue first; then whoever holds the lease drains it.
        if args.full_sweep and not args.dry_run:
            enqueue_full_sweep(
                conn,
                policy_version=args.policy_version,
                kraje=_parse_kraje(args.kraje),
                window=args.sweep_window,
            )
        if args.orphan_sweep and not args.dry_run:
            enqueue_orphan_sweep(conn, window=args.sweep_window)
        with lease.held(
            conn,
            JOB_NAME,
            cadence="15 minutes",
            concurrency_group=CONCURRENCY_GROUP,
        ) as acquired:
            if not acquired:
                LOG.info(
                    "DRAIN skipped: another run holds the %s lease%s", JOB_NAME,
                    " (the sweep above is enqueued; that holder drains it)"
                    if args.full_sweep and not args.dry_run else "",
                )
                return 0
            run(
                conn,
                batch_size=args.batch_size,
                max_seconds=args.max_seconds,
                policy_version=args.policy_version,
                dry_run=args.dry_run,
                only_listing_id=args.listing_id,
            )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
