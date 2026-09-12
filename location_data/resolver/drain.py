"""`location_resolve_incremental` — the `dirty_locations` drain.

This is the job that makes location live. Without it a newly scraped listing gets claims in
the detail drain and **never gets a row** — invisible to every consumer of the location
programme.

Batch discipline (learned three times):

* bounded slices claimed with `FOR UPDATE SKIP LOCKED`, ONE transaction per batch, and the
  answer row written in the same transaction — that is what gives an operator edit
  read-your-writes;
* one SAVEPOINT for the optimistic slice, and a per-listing SAVEPOINT on the retry, so one
  poisonous listing still costs its own row and not the batch;
* `statement_timeout` / `lock_timeout` set with `SET LOCAL` INSIDE the batch transaction
  (outside one, on this codebase's autocommit connections, `SET LOCAL` is a silent no-op);
* a lease-row CAS on `location_jobs`, never a session advisory lock — a lock taken on one
  pooler backend and released on another strands;
* **judge the queue by OLDEST-ROW AGE, not by length** (the repo's standing rule);
* the slice is ordered `(enqueued_at, listing_id)` — a bare timestamp sort reshuffles on
  every call, because a batch enqueue shares one `now()` and the tie order is arbitrary;
* the queue is RE-ENTRANT: a producer's enqueue BUMPS `enqueued_at`, and every statement that
  finishes a row is bounded by the `enqueued_at` the slice claimed, so evidence that arrives
  mid-slice leaves the row queued instead of being deleted unresolved (see `_CLAIM_SLICE_SQL`);
* resumable by construction: the queue IS the cursor, and a failed row comes back with a
  backoff rather than blocking the slice;
* a failed BATCH costs one slice and not the worker (W2-a6): the transaction rolls back, the
  rows it claimed are still queued with their `enqueued_at` untouched, and the loop backs off
  and claims again — five CONSECUTIVE failures, or a lost connection, are what stop a loop.

Throughput discipline (measured, never assumed). The cost is POOLER ROUND TRIPS, not CPU:
production run 31480587021 spent 225 ms per listing on three statements with NO server-side
work at all, i.e. ~75 ms of network round trip between a GitHub-hosted runner and the
Frankfurt pooler, while the same run's registry questions cost 0.02-0.5 ms each
server-side. So the rate is `1 / (75 ms x round trips per listing)` and nothing else. The
levers are all I/O-layer: `connect_session()` for server-side prepared statements, the run
cache over the immutable mirror, a per-slice prefetch, a per-slice warm of the point-keyed
questions, and slice-batched writes.

W2-a took the biggest bite out of that number by DELETING work rather than batching it. The
write path was seven statements per slice — resolutions, candidates, contradictions,
dispositions, a read-your-writes `location_disputed` read, the listing projection and the
property rebuild — and is now ONE upsert. The prefetch was five queries and is two. The
registry warm was five point-keyed questions and is two.

**`workers` shipped with W2-a5**, and W2-a is what unblocked it. The stated reason the drain
stayed single-connection was `_rebuild_properties`: two workers holding two listings of the
SAME property would both read the member set and both write `property_location_current`, and
a stale read could publish the wrong winner. That was the drain's ONE cross-listing write and
it is gone with the table. Every remaining write is keyed on the listing the slice already
holds `FOR UPDATE`, so a second connection claiming a disjoint slice is safe by construction
— `SKIP LOCKED` is what makes the slices disjoint, and it is the same statement either way.

Concurrency is the right lever because the loop is LATENCY-bound, not CPU-bound: measured
2026-09-12 10:27Z the Railway worker drained ~8 listings/s (5,000 rows per 10 minutes)
against a 448k queue while every backend on the instance sat in `DataFileRead`. ~4 registry
round trips per listing, each waiting on a disk the drain does not own, is time N loops can
overlap and one loop cannot. What is NOT shared between them is the connection (psycopg
connections are not thread-safe — one per thread) and what IS shared is the `RunCache`, under
a lock, because name- and code-keyed answers repeat across listings and across workers.
The lease stays on the CALLER's connection and is taken exactly once: workers are one run.

CLI:  python -m location_data.resolver.drain [--max-seconds N] [--batch-size N]
                                             [--listing-id N] [--dry-run]

The CLI has no `--workers`: the GitHub lane is the backstop, it is RTT-bound against a US
runner rather than IO-bound beside the instance, and N runner connections into the session
pooler is the wrong place to spend them. The Railway worker lane passes `workers=` instead.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg

from location_data import loader_db
from location_data.claims_common import SERVED_LISTING_PREDICATE
from location_data.resolver import core, lease, projection, resolve_db
from location_data.resolver.types import Claim, ResolverContext
from location_data.resolver.version import RESOLVER_VERSION
from scraper import db

LOG = logging.getLogger("location_data.resolver.drain")

JOB_NAME = "location_resolve_incremental"
CONCURRENCY_GROUP = "location-resolve"
DEFAULT_BATCH = 250
DEFAULT_MAX_SECONDS = 600
BACKOFF_SECONDS = (60, 300, 900, 3600, 21600)

# THE QUEUE IS RE-ENTRANT (W2-a2). The slice carries each row's `enqueued_at` and every
# statement that finishes a row is bounded by the value the slice CLAIMED. A producer that
# learns something new about a listing bumps `enqueued_at` to now() (see `claims_intake`,
# `contracts.retract` and `operator_corrections`), so a bump that lands after the slice read
# its claims leaves `enqueued_at` NEWER than the claimed value and the row is NOT finished —
# it stays queued and the next slice resolves it against the claims that arrived.
#
# Without the bound this is silent, total data loss: on 2026-09-12 the nine contract bumps
# re-mined ~60k listings whose rows were already queued from the previous sweep, every
# `claim_insert` enqueue hit ON CONFLICT DO NOTHING, and the drain deleted the queue row
# after resolving them from the OLD claims. 384,500 answer rows, 135 with a town, and nothing
# left to re-enqueue them — the sweep sees a current-version row and stops.
_CLAIM_SLICE_SQL = """
SELECT listing_id, attempts, enqueued_at
  FROM dirty_locations
 WHERE next_eligible_at <= now()
 ORDER BY enqueued_at, listing_id
 FOR UPDATE SKIP LOCKED
 LIMIT %s
"""

_DELETE_ROW_SQL = """
DELETE FROM dirty_locations
 WHERE listing_id = %s AND enqueued_at <= %s
"""

# `unnest` of two parallel arrays rather than `jsonb_to_recordset`: psycopg adapts a list of
# datetimes to `timestamptz[]` natively, where the jsonb form would need every timestamp
# round-tripped through a string and re-parsed.
_DELETE_ROWS_SQL = """
DELETE FROM dirty_locations d
 USING unnest(%s::bigint[], %s::timestamptz[]) AS claimed(listing_id, enqueued_at)
 WHERE d.listing_id = claimed.listing_id
   AND d.enqueued_at <= claimed.enqueued_at
"""

# Same bound on the failure stamp: a backoff written over a NEWER enqueue would push a row
# that just gained evidence behind up to six hours of `next_eligible_at`, and reset nothing
# when it finally ran.
_FAIL_ROW_SQL = """
UPDATE dirty_locations
   SET attempts = attempts + 1,
       last_error = %s,
       next_eligible_at = now() + make_interval(secs => %s)
 WHERE listing_id = %s AND enqueued_at <= %s
"""

_QUEUE_HEALTH_SQL = """
SELECT count(*), coalesce(extract(epoch from now() - min(enqueued_at)), 0)
  FROM dirty_locations
"""

# Statement budgets, all applied with `SET LOCAL` INSIDE a transaction (outside one, on this
# codebase's autocommit connections, `SET LOCAL` is a silent no-op).
#
# The BATCH budget already existed and did its job. What WAS unguarded is everything the
# drain runs OUTSIDE a batch transaction: the per-batch queue count, the run-start constant
# load and the full-sweep INSERT. Those ran on the pooler default (no ceiling), so any one of
# them could wait indefinitely under IO pressure. Each now runs in its own bounded
# transaction.
#
# 30 s stays the batch default: a per-listing statement that has not answered in 30 s has
# already blown the budget by 25x, and a QueryCanceled here is caught by the per-listing
# SAVEPOINT and costs one row, not the batch. Overridable for a deliberately slow backfill.
BATCH_TIMEOUT_ENV = "LOCATION_RESOLVE_BATCH_TIMEOUT_S"
DEFAULT_BATCH_TIMEOUT_S = 30
# The PREFETCH gets its own, much larger budget (W2-a6). 30 s is right for a per-listing
# statement and wrong for this one: the claims read is a 250-listing `= ANY(...)` over
# `location_claims`, and on an IO-bound instance it legitimately runs past 30 s — where the
# cancel threw away the whole batch's work and the rows went back to the queue to cost the
# same again. Measured 2026-09-12: four workers each completed ONE batch and then died in the
# prefetch of their second (claimed=1000 over batches=8, i.e. four batches counted that never
# reached a write). Same `SET LOCAL` discipline, inside the batch transaction, restored to the
# per-listing budget before anything writes.
PREFETCH_TIMEOUT_ENV = "LOCATION_RESOLVE_PREFETCH_TIMEOUT_S"
DEFAULT_PREFETCH_TIMEOUT_S = 90
# One sweep window is an `INSERT ... SELECT` over a 250k-id slice of `listings` joined to the
# answer table; minutes is normal for it and only for it.
SWEEP_TIMEOUT_ENV = "LOCATION_RESOLVE_SWEEP_TIMEOUT_S"
DEFAULT_SWEEP_TIMEOUT_S = 900
# Listing-id width of one corpus-sweep window (see _SWEEP_SQL).
DEFAULT_SWEEP_WINDOW = 250_000
LOCK_TIMEOUT_S = 5

# A FAILED BATCH COSTS ONE SLICE, NEVER THE WORKER (W2-a6). The batch transaction rolls back
# on the way out, so its rows are still queued with their `enqueued_at` untouched and the next
# slice — this worker's or a sibling's — simply claims them again. What the loop must not do
# is retry instantly: the failures that happen here are pressure (a statement timeout, a
# pooler hiccup), and a hot retry loop is more of exactly what caused them.
BATCH_RETRY_BACKOFF_S = 2.0
BATCH_RETRY_BACKOFF_MAX_S = 30.0
# ...and it must not retry for ever either: five consecutive failures is no longer a blip, it
# is a condition, and the pass ends rather than spending its budget on it. The counter resets
# on any batch that lands, so an intermittent instance never accumulates its way to a stop.
MAX_CONSECUTIVE_BATCH_FAILURES = 5
# A DEAD connection is not a failed statement — every batch on it fails the same way. The
# worker reconnects ONCE (a pooler that dropped one client usually takes the next) and stops
# if that one dies too.
MAX_WORKER_RECONNECTS = 1
# sqlstates that mean THE CONNECTION rather than the statement: class 08 (connection
# exception) and the server announcing it is going away. Asked as a SQLSTATE and never as
# `isinstance(exc, OperationalError)`, because `QueryCanceled` (57014) and `LockNotAvailable`
# (55P03) are OperationalError subclasses too — the statement timeout this wave exists to
# survive would otherwise read as a dead socket and stop the worker on its first occurrence.
_CONNECTION_GONE_SQLSTATES = frozenset({"57P01", "57P02", "57P03"})


class _ConnectionLost(Exception):
    """The batch failed because the CONNECTION is gone, not because a statement was.

    Raised out of the loop so the RECONNECT POLICY lives with whoever owns the connection —
    `_run_workers` opened it and will close it — rather than in the loop that only borrows it.
    """


def _connection_lost(conn: psycopg.Connection, exc: BaseException) -> bool:
    if getattr(conn, "closed", False):
        return True
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate is None:
        # A psycopg OperationalError carrying no sqlstate came from the CLIENT side: the
        # socket closed, the pooler refused, the handshake failed. The server answered nothing.
        return isinstance(exc, psycopg.OperationalError)
    return sqlstate.startswith("08") or sqlstate in _CONNECTION_GONE_SQLSTATES


def _statement_timeout_guc(seconds: int) -> str:
    return f"SET LOCAL statement_timeout = '{int(seconds)}s'"


def _batch_guc(seconds: int) -> tuple[str, ...]:
    return (
        _statement_timeout_guc(seconds),
        f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT_S}s'",
    )


def _batch_timeout_s() -> int:
    return loader_db.env_timeout_s(BATCH_TIMEOUT_ENV, DEFAULT_BATCH_TIMEOUT_S)


def _prefetch_timeout_s() -> int:
    return loader_db.env_timeout_s(PREFETCH_TIMEOUT_ENV, DEFAULT_PREFETCH_TIMEOUT_S)


@contextlib.contextmanager
def _bounded(conn: psycopg.Connection, seconds: int) -> Iterator[psycopg.Cursor]:
    """One bounded transaction for work that would otherwise run bare on autocommit."""
    with conn.transaction():
        with conn.cursor() as cur:
            for statement in _batch_guc(seconds):
                cur.execute(statement)
            yield cur


# `location_resolve_sweep` — the daily reconcile backstop. ONE statement, driving off
# `listings`: every ACTIVE listing whose answer row is missing, or was written at a version
# tuple that is no longer current, is enqueued. Driving off `listings` sees every listing
# there is, the join to the answer table is on its primary key, and the drain writes a row
# for a claimless listing too — so coverage is `count(listing_location) = count(active)` by
# construction, and a stale row is caught the same way as a missing one.
#
# TWO version columns, not three. `policy_version` was the third and it named
# `location_field_policy` / `location_uncertainty_policy` / `location_collision_policy`,
# which W2-a deletes: policy is code now, so a policy change IS a `resolver_version` bump.
# That is what makes `resolver:v4` in `version.py` the whole cutover — this statement sees
# every row still stamped v3 and enqueues the corpus.
#
# Walked in LISTING-ID WINDOWS, never as one statement, so each window commits on its own and
# an interrupted sweep resumes by re-running (ON CONFLICT makes every window idempotent).
#
# The `NOT EXISTS (... dirty_locations ...)` pre-filter is deliberate, not a duplicate of ON
# CONFLICT: a drain slice is nearly always in flight, holding its 250 rows FOR UPDATE, and an
# INSERT ... ON CONFLICT on one of those keys must WAIT for that transaction — the sweep's
# 5 s lock_timeout then kills the window. The NOT EXISTS is an MVCC read: it sees the queued
# row and skips it without touching the lock.
#
# THE SWEEP IS THE ONE PRODUCER THAT DOES NOT BUMP (W2-a2). Every other enqueue is evidence
# arriving — new claims, a retraction, an operator edit — and bumps `enqueued_at` so an
# in-flight slice cannot finish the row. The sweep carries no evidence: bumping a queued row
# would reset a poisoned row's `attempts` backoff and move the oldest row in the queue to the
# back of it, on a statement that walks the whole corpus. NOT EXISTS + DO NOTHING, deliberately.
#
# THE RED-LINE ARM (W2-a2). Rule 25's invariant is "every active Czech listing has a town".
# The three version arms cannot express it: a row resolved without an `obec_kod` is stamped at
# the CURRENT version tuple, so the sweep sees a fresh row and walks past it for ever — which
# is exactly how 2026-09-12 left 384,365 rows red with nothing able to re-enqueue them. The
# fourth arm re-resolves them nightly until each one has a town or is determined `foreign`
# (never a default — `undetermined` is still red and still swept). It is bounded by the red
# count, not the corpus, and served by `listing_location (obec_kod, granularity)` from 501.
#
# THE DRIVING PREDICATE IS "WHAT BROWSE SERVES", NOT "WHAT IS ACTIVE" (W2-a4). `l.is_active`
# alone was the wrong scope: `browse_projection` serves `properties WHERE status = 'active'`
# — the MERGE lifecycle, not `is_active` — so a DELISTED property is still a Browse row, and
# the row it renders is its `repr_listing_ref_id` DISPLAY LISTING, which is `is_active =
# false`. Under the old predicate that listing could never be swept, never get a
# `listing_location` row, and after W3 would show no place and vanish from the map. The
# second arm brings exactly those display listings in and nothing else.
#
# The EXISTS is CORRELATED and sits under an OR, which blocks the semi-join transform, so
# Postgres evaluates it as a per-row subplan: one index probe per inactive listing WITH an
# index, one seq scan of `properties` per inactive listing WITHOUT one. Migration 505 adds
# `properties (repr_listing_ref_id) WHERE status = 'active'` — `properties` carried eleven
# indexes and not one led on the column every read model joins `listings` on.
_SWEEP_SQL = """
INSERT INTO dirty_locations (listing_id, reason)
SELECT l.id, 'full_sweep'
  FROM listings l
  LEFT JOIN listing_location p ON p.listing_id = l.id
 WHERE """ + SERVED_LISTING_PREDICATE + """
   AND (p.listing_id IS NULL
        OR p.resolver_version <> %s
        OR p.registry_version <> %s
        OR (p.obec_kod IS NULL AND p.country_status <> 'foreign'))
   AND l.id > %s AND l.id <= %s
   AND NOT EXISTS (SELECT 1 FROM dirty_locations d WHERE d.listing_id = l.id)
ON CONFLICT (listing_id) DO NOTHING
"""

# The window walk's upper bound: max() over `listings`' primary key is an index probe.
_SWEEP_UPPER_BOUND_SQL = "SELECT coalesce(max(id), 0) FROM listings"


@dataclass(slots=True)
class DrainStats:
    claimed: int = 0
    resolved: int = 0
    failed: int = 0
    batches: int = 0
    fallbacks: int = 0
    # How many slice loops the run used, and how many of them ENDED EARLY (W2-a5) — a dead
    # connection, a failed queue-health read. The run continues with fewer, so the number of
    # workers alone cannot tell you what actually drained.
    workers: int = 1
    failed_passes: int = 0
    # Batches that RAISED and were retried (W2-a6) — the number that used to end the worker.
    # A batch here costs its slice and nothing else: the rows are still queued.
    failed_batches: int = 0
    # Phase timings, so the NEXT production run measures itself instead of being re-diagnosed.
    prefetch_seconds: float = 0.0
    warm_seconds: float = 0.0
    core_seconds: float = 0.0
    write_seconds: float = 0.0
    seconds: float = 0.0

    @property
    def rate(self) -> float:
        return 0.0 if self.seconds <= 0 else self.claimed / self.seconds

    def absorb(self, other: "DrainStats") -> None:
        """Fold one worker's counters into the run's.

        `seconds` is deliberately NOT summed: it is the run's WALL CLOCK, and four overlapping
        loops would report four times the elapsed time and therefore a quarter of the real
        rate. The PHASE seconds are summed and can exceed the wall clock on purpose — they say
        where the workers' time went, not how long the run took. `workers` is the run's, not a
        worker's, so it is not summed either."""
        self.claimed += other.claimed
        self.resolved += other.resolved
        self.failed += other.failed
        self.batches += other.batches
        self.fallbacks += other.fallbacks
        self.failed_passes += other.failed_passes
        self.failed_batches += other.failed_batches
        self.prefetch_seconds += other.prefetch_seconds
        self.warm_seconds += other.warm_seconds
        self.core_seconds += other.core_seconds
        self.write_seconds += other.write_seconds


@dataclass(slots=True)
class _Slice:
    """Everything the slice's listings need that can be read BEFORE any of them writes."""

    claims: dict[int, list[Claim]] = field(default_factory=dict)
    sources: dict[int, str] = field(default_factory=dict)


def _prefetch(
    conn: psycopg.Connection,
    listing_ids: list[int],
    *,
    timeout_s: int,
    restore_s: int,
) -> _Slice:
    """The slice's two bulk reads, on a budget of THEIR OWN (W2-a6, see PREFETCH_TIMEOUT_ENV).

    `SET LOCAL` inside the batch transaction the caller already opened, and restored to the
    per-listing budget in a `finally` — nothing downstream of here may write under a 90 s
    ceiling. The restore is best-effort on purpose: if it fails the transaction is already
    aborted, and the batch is about to roll back anyway."""
    with conn.cursor() as cur:
        cur.execute(_statement_timeout_guc(timeout_s))
    try:
        return _Slice(
            claims=resolve_db.load_claims_bulk(conn, listing_ids),
            sources=resolve_db.sources_bulk(conn, listing_ids),
        )
    finally:
        with contextlib.suppress(Exception):
            with conn.cursor() as cur:
                cur.execute(_statement_timeout_guc(restore_s))


def _warm(slice_: _Slice, ctx: ResolverContext, cache: resolve_db.RunCache) -> None:
    """Pre-answer the two coordinate-keyed registry questions for the whole slice.

    They are the ones the run cache can never share between listings. `warm_points` is a
    no-op on a view that is not the SQL one (the mini-mirror tests), and warming a point the
    core never asks about only costs server time — never an answer."""
    registry = getattr(ctx.registry, "_inner", None)
    if not isinstance(registry, resolve_db.SqlRegistryView):
        return
    points = sorted({
        (float(claim.lat), float(claim.lon))
        for claims in slice_.claims.values()
        for claim in claims
        if claim.lat is not None and claim.lon is not None
    })
    resolve_db.warm_points(registry, cache, points)


def run(
    conn: psycopg.Connection,
    *,
    batch_size: int = DEFAULT_BATCH,
    max_seconds: int = DEFAULT_MAX_SECONDS,
    dry_run: bool = False,
    only_listing_id: int | None = None,
    workers: int = 1,
) -> DrainStats:
    """One drain run. `workers > 1` runs that many slice loops concurrently (W2-a5).

    The caller's connection stays THE run's connection: it reads the one corpus constant, it
    is where the lease the caller took lives, and it is never handed to a thread. With
    `workers=1` the loop runs on it, exactly as it always has. Above 1 it stays idle while N
    threads each open their own session-mode connection and run the SAME loop against the
    same queue — `FOR UPDATE SKIP LOCKED` hands each of them a disjoint slice."""
    workers = max(1, int(workers))
    batch_timeout_s = _batch_timeout_s()
    # The run's ONE corpus constant, read once: which registry version is current. It is
    # pinned for the run's lifetime by definition — a mirror load mints a NEW version rather
    # than editing one. Bounded as its own transaction, because a run that hangs on its first
    # statement logs nothing at all, which is the least diagnosable failure the lane has.
    with _bounded(conn, batch_timeout_s):
        registry_version_id, registry_label = resolve_db.current_registry_version(conn)
    cache = resolve_db.RunCache()
    ctx_base = _context(conn, registry_version_id, cache)
    stats = DrainStats(workers=workers)
    started = time.monotonic()
    LOG.info(
        "DRAIN start batch_size=%d max_seconds=%d statement_timeout=%ds registry_version=%s "
        "workers=%d",
        batch_size, max_seconds, batch_timeout_s, registry_label, workers,
    )

    if only_listing_id is not None:
        with conn.transaction():
            with conn.cursor() as cur:
                for statement in _batch_guc(batch_timeout_s):
                    cur.execute(statement)
            _resolve_one(
                conn, only_listing_id, ctx_base, registry_label,
                _prefetch(
                    conn, [only_listing_id],
                    timeout_s=_prefetch_timeout_s(), restore_s=batch_timeout_s,
                ),
                stats, dry_run=dry_run,
            )
            stats.claimed = stats.resolved = 1
        stats.seconds = time.monotonic() - started
        return stats

    deadline = started + max_seconds
    if workers == 1:
        _drain_loop(
            conn, ctx_base, cache, stats, registry_label=registry_label,
            batch_size=batch_size, batch_timeout_s=batch_timeout_s, deadline=deadline,
            dry_run=dry_run,
        )
    else:
        _run_workers(
            workers=workers, registry_version_id=registry_version_id,
            registry_label=registry_label, cache=cache, stats=stats, batch_size=batch_size,
            batch_timeout_s=batch_timeout_s, deadline=deadline, dry_run=dry_run,
        )
    stats.seconds = time.monotonic() - started
    LOG.info(
        "DRAIN done batches=%d claimed=%d resolved=%d failed=%d fallbacks=%d %.1fs "
        "rate=%.1f/s prefetch=%.1fs warm=%.1fs core=%.1fs write=%.1fs registry_q=%d "
        "registry_hit=%.0f%% workers=%d dead_workers=%d failed_batches=%d",
        stats.batches, stats.claimed, stats.resolved, stats.failed, stats.fallbacks,
        stats.seconds, stats.rate, stats.prefetch_seconds, stats.warm_seconds,
        stats.core_seconds, stats.write_seconds, cache.misses, 100.0 * cache.hit_rate,
        stats.workers, stats.failed_passes, stats.failed_batches,
    )
    if workers == 1:
        # Above 1 the base context asked nothing — each worker logs its OWN query report as
        # its loop ends, because a merged one hides the worker that was slow.
        LOG.info("DRAIN queries %s", _query_stats(ctx_base).report())
    LOG.info("DRAIN cache misses %s", cache.report())
    return stats


def _drain_loop(
    conn: psycopg.Connection,
    ctx: ResolverContext,
    cache: resolve_db.RunCache,
    stats: DrainStats,
    *,
    registry_label: str,
    batch_size: int,
    batch_timeout_s: int,
    deadline: float,
    dry_run: bool,
    worker: int = 0,
) -> None:
    """ONE slice loop on ONE connection: claim, prefetch, warm, compute, write, repeat until
    the budget ends or the queue comes back empty.

    `worker` is 0 for the single-connection run — the loop the drain has always had, statement
    for statement and log line for log line — and 1..N inside a thread. It changes two things
    and nothing else: the log tag, and WHO counts the queue. The health read is a `count(*)`
    over the whole of `dirty_locations`; N copies of it per batch buy N sequential scans of a
    400k-row table and one number, so only the first loop asks.

    The budget is tested BETWEEN batches, here as everywhere: a worker finishes the slice it
    holds and stops, so the run overruns by at most one batch however many workers it has (the
    slices run concurrently, so it is one batch, not N).
    """
    tag = "" if worker == 0 else f"w{worker} "
    consecutive = 0
    backoff = BATCH_RETRY_BACKOFF_S
    while time.monotonic() < deadline:
        try:
            claimed = _run_batch(
                conn, ctx, cache, stats, registry_label=registry_label,
                batch_size=batch_size, batch_timeout_s=batch_timeout_s, dry_run=dry_run,
                tag=tag, worker=worker,
            )
        except Exception as exc:  # noqa: BLE001 - one slice, not the worker
            # The batch transaction has already rolled back on the way out of the `with`, so
            # this slice's rows are queued exactly as they were — same `enqueued_at`, same
            # `attempts`, no backoff stamp (that one is per LISTING and is written by
            # `_run_slice`, which never ran). Nothing to undo, nothing to re-enqueue.
            stats.failed_batches += 1
            consecutive += 1
            LOG.warning(
                "BATCH %sfailed (%s: %s) — %d consecutive; its rows stay queued",
                tag, type(exc).__name__, exc, consecutive,
            )
            if _connection_lost(conn, exc):
                raise _ConnectionLost(f"{type(exc).__name__}: {exc}") from exc
            if consecutive >= MAX_CONSECUTIVE_BATCH_FAILURES:
                LOG.warning(
                    "DRAIN %sstopping after %d consecutive failed batches", tag, consecutive)
                return
            _backoff_sleep(backoff, deadline)
            backoff = min(backoff * 2, BATCH_RETRY_BACKOFF_MAX_S)
            continue
        if claimed is None:
            break
        consecutive = 0
        backoff = BATCH_RETRY_BACKOFF_S


def _run_batch(
    conn: psycopg.Connection,
    ctx: ResolverContext,
    cache: resolve_db.RunCache,
    stats: DrainStats,
    *,
    registry_label: str,
    batch_size: int,
    batch_timeout_s: int,
    dry_run: bool,
    tag: str,
    worker: int,
) -> int | None:
    """ONE batch, in ONE transaction. Returns how many rows it claimed, or None when the queue
    handed this loop nothing — the one outcome that ends the loop rather than repeating it."""
    if worker <= 1:
        depth, oldest = _queue_health(conn, batch_timeout_s)
        LOG.info("QUEUE %sdepth=%d oldest_age_s=%.0f", tag, depth, oldest)
    batch_started = time.monotonic()
    batch_before = (stats.resolved, stats.failed)
    with conn.transaction():
        with conn.cursor() as cur:
            for statement in _batch_guc(batch_timeout_s):
                cur.execute(statement)
            cur.execute(_CLAIM_SLICE_SQL, (batch_size,))
            rows = cur.fetchall()
        if not rows:
            # SKIP LOCKED means "nothing I can have", which on a busy queue can also mean
            # "my siblings hold it all". Ending this loop is right either way: the queue
            # is the cursor and the next pass is 15 seconds away.
            LOG.info("QUEUE %sempty", tag)
            return None
        stats.batches += 1
        prefetch_started = time.monotonic()
        slice_ = _prefetch(
            conn, [int(row[0]) for row in rows],
            timeout_s=_prefetch_timeout_s(), restore_s=batch_timeout_s,
        )
        stats.prefetch_seconds += time.monotonic() - prefetch_started
        warm_started = time.monotonic()
        try:
            # Its own SAVEPOINT: the warm is an OPTIMISATION, and a statement timeout
            # inside it would otherwise abort the batch transaction and end the run.
            # Rolled back, every point simply falls through to its own lazy query.
            with conn.transaction():
                _warm(slice_, ctx, cache)
        except Exception as exc:  # noqa: BLE001 - degrade to the lazy path, never die
            LOG.warning("WARM %sfailed, resolving with per-point lookups: %s", tag, exc)
        stats.warm_seconds += time.monotonic() - warm_started
        _run_slice(
            conn, rows, ctx, registry_label, slice_, stats, dry_run=dry_run,
        )
    _log_batch(stats, cache, ctx, len(rows), batch_before,
               time.monotonic() - batch_started, tag)
    return len(rows)


def _backoff_sleep(seconds: float, deadline: float) -> None:
    """Never past the budget: a worker asleep through the deadline holds its connection doing
    nothing while the pass waits for it to join."""
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(min(seconds, remaining))


def _run_workers(
    *,
    workers: int,
    registry_version_id: int,
    registry_label: str,
    cache: resolve_db.RunCache,
    stats: DrainStats,
    batch_size: int,
    batch_timeout_s: int,
    deadline: float,
    dry_run: bool,
) -> None:
    """N slice loops, N connections, ONE run cache and ONE lease (the caller's).

    Isolation is by construction, not by coordination: the slice is claimed `FOR UPDATE SKIP
    LOCKED`, so no two loops ever hold the same queue row, and since W2-a every write the
    drain makes is keyed on a listing its own slice already holds.

    A FAILED BATCH is not a failed worker (W2-a6): the loop counts it, backs off and claims
    the next slice. What ends a worker early is a LOST CONNECTION — reconnected once here,
    because this is what owns the connection, and stopped if the replacement dies too — or
    anything else escaping the loop, which counts one `failed_passes`. The others keep
    draining either way and the run reports what actually ran. Threads are daemons: a pass
    abandoned by the lane loop must not be able to keep the worker process alive at shutdown."""
    per_worker = [DrainStats() for _ in range(workers)]

    def _loop(index: int) -> None:
        mine = per_worker[index - 1]
        reconnects = 0
        while True:
            conn: psycopg.Connection | None = None
            try:
                conn = _worker_connection()
                ctx = _context(conn, registry_version_id, cache)
                _drain_loop(
                    conn, ctx, cache, mine, registry_label=registry_label,
                    batch_size=batch_size, batch_timeout_s=batch_timeout_s,
                    deadline=deadline, dry_run=dry_run, worker=index,
                )
                LOG.info("DRAIN w%d queries %s", index, _query_stats(ctx).report())
                return
            except _ConnectionLost as exc:
                reconnects += 1
                if reconnects > MAX_WORKER_RECONNECTS or time.monotonic() >= deadline:
                    mine.failed_passes += 1
                    LOG.warning(
                        "DRAIN w%d stopping after claimed=%d: connection lost (%s)",
                        index, mine.claimed, exc)
                    return
                LOG.warning(
                    "DRAIN w%d lost its connection (%s) after claimed=%d; reconnecting once",
                    index, exc, mine.claimed)
            except Exception as exc:  # noqa: BLE001 - one worker's death is not the run's
                mine.failed_passes += 1
                LOG.warning(
                    "DRAIN w%d ended early after claimed=%d: %s", index, mine.claimed, exc)
                return
            finally:
                # Runs on every exit INCLUDING the reconnect turn, so the dead connection is
                # released before a new one is opened.
                if conn is not None:
                    with contextlib.suppress(Exception):
                        conn.close()

    threads = [
        threading.Thread(
            target=_loop, args=(i,), name=f"location-drain-w{i}", daemon=True)
        for i in range(1, workers + 1)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for mine in per_worker:
        stats.absorb(mine)


def _worker_connection() -> psycopg.Connection:
    """A worker thread's OWN connection — psycopg connections are not thread-safe.

    `db.connect_session()` rather than `open_connection()`: the run's first connection (the
    caller's, the one holding the lease) has already announced the pooler mode, and one
    warning per thread per pass is the same sentence four times every fifteen seconds."""
    return db.connect_session()


def _run_slice(
    conn: psycopg.Connection,
    rows: list[tuple[Any, Any, Any]],
    ctx: ResolverContext,
    registry_label: str,
    slice_: _Slice,
    stats: DrainStats,
    *,
    dry_run: bool,
) -> None:
    """Optimistic: resolve the whole slice, then write it in two statements instead of seven
    per listing. On ANY failure the savepoint rolls the slice's writes back and the proven
    per-listing path re-runs it with a SAVEPOINT each, so a poisoned listing still costs one
    row and not the batch. The run that motivated this measured 1 failure in 2,750 listings,
    so the optimistic path is the one that matters and the fallback is the safety net."""
    stats.claimed += len(rows)
    listing_ids = [int(row[0]) for row in rows]
    # The `enqueued_at` each row was CLAIMED at — the bound on every statement that finishes
    # it, so a producer's bump mid-slice keeps the row queued.
    claimed_at = [row[2] for row in rows]
    try:
        with conn.transaction():  # SAVEPOINT for the WHOLE slice
            core_started = time.monotonic()
            resolutions = [
                item
                for listing_id in listing_ids
                if (item := _compute_one(
                    listing_id, ctx, registry_label, slice_, dry_run=dry_run,
                )) is not None
            ]
            stats.core_seconds += time.monotonic() - core_started
            if not dry_run:
                write_started = time.monotonic()
                _write_slice(conn, resolutions)
                with conn.cursor() as cur:
                    cur.execute(_DELETE_ROWS_SQL, (listing_ids, claimed_at))
                stats.write_seconds += time.monotonic() - write_started
        stats.resolved += len(rows)
        return
    except Exception as exc:  # noqa: BLE001 - fall back to per-listing isolation
        stats.fallbacks += 1
        LOG.warning(
            "SLICE batch failed (n=%d), retrying per listing with SAVEPOINTs: %s",
            len(rows), exc,
        )

    for listing_id, attempts, enqueued_at in rows:
        try:
            with conn.transaction():  # SAVEPOINT: one bad row, one bad row
                _resolve_one(
                    conn, int(listing_id), ctx, registry_label, slice_, stats,
                    dry_run=dry_run,
                )
                if not dry_run:
                    with conn.cursor() as cur:
                        cur.execute(_DELETE_ROW_SQL, (listing_id, enqueued_at))
            stats.resolved += 1
        except Exception as exc:  # noqa: BLE001 - the row must not poison the batch
            stats.failed += 1
            LOG.warning("RESOLVE failed listing_id=%s: %s", listing_id, exc)
            backoff = BACKOFF_SECONDS[min(int(attempts), len(BACKOFF_SECONDS) - 1)]
            with conn.cursor() as cur:
                cur.execute(_FAIL_ROW_SQL, (str(exc)[:500], backoff, listing_id, enqueued_at))


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
    tag: str = "",
) -> None:
    """Per-batch self-measurement: the run reports its own listings/s and where the time
    went — including WHICH query kind. `tag` names the worker (empty on the single loop, so
    the line a single-connection run writes is unchanged); the per-batch rate is that
    worker's, the cumulative phase numbers are its own too."""
    LOG.info(
        "BATCH %sn=%d ok=%d fail=%d %.1fs rate=%.1f/s cum(prefetch=%.1fs warm=%.1fs "
        "core=%.1fs write=%.1fs) registry_q=%d registry_hit=%.0f%% registry_wait=%.1fs",
        tag, n, stats.resolved - before[0], stats.failed - before[1], elapsed,
        0.0 if elapsed <= 0 else n / elapsed, stats.prefetch_seconds, stats.warm_seconds,
        stats.core_seconds, stats.write_seconds, cache.misses, 100.0 * cache.hit_rate,
        cache.seconds,
    )
    LOG.info("BATCH %squeries %s", tag, _query_stats(ctx).report())


def _context(
    conn: psycopg.Connection, registry_version_id: int, cache: resolve_db.RunCache
) -> ResolverContext:
    return ResolverContext(
        registry=resolve_db.CachedRegistryView(
            resolve_db.SqlRegistryView(conn, registry_version_id, resolve_db.QueryStats()),
            cache,
        ),
    )


def _resolve_one(
    conn: psycopg.Connection,
    listing_id: int,
    ctx: ResolverContext,
    registry_label: str,
    slice_: _Slice,
    stats: DrainStats,
    *,
    dry_run: bool,
) -> None:
    """ONE listing, compute + write — the `--listing-id` path and the slice fallback.

    It is `_write_slice` with a one-element slice, deliberately: two write paths would be two
    places for the same statement to drift apart."""
    core_started = time.monotonic()
    item = _compute_one(listing_id, ctx, registry_label, slice_, dry_run=dry_run)
    stats.core_seconds += time.monotonic() - core_started
    if item is None:
        return
    write_started = time.monotonic()
    _write_slice(conn, [item])
    stats.write_seconds += time.monotonic() - write_started


def _compute_one(
    listing_id: int,
    ctx: ResolverContext,
    registry_label: str,
    slice_: _Slice,
    *,
    dry_run: bool,
) -> Any | None:
    """The PURE half of a listing: the four steps. Writes nothing, so the whole slice can be
    computed before the first statement goes out."""
    claims = slice_.claims.get(listing_id, [])
    if not claims:
        # A queued listing with no live claim is an ANSWER, not a skip. The row states
        # "nothing to go on" (granularity unknown, no position, country undetermined), so
        # coverage is measurable and the nightly sweep does not re-enqueue it.
        LOG.info("RESOLVE no_claims listing_id=%s", listing_id)
    resolution = core.resolve(
        claims,
        ctx,
        resolver_version=RESOLVER_VERSION,
        registry_version=registry_label,
        listing_id=listing_id,
        source=slice_.sources.get(listing_id, "unknown"),
    )
    if dry_run:
        LOG.info(
            "RESOLVE dry listing_id=%s granularity=%s confidence=%s obec=%s disputed=%s",
            listing_id, resolution.granularity, resolution.match_confidence,
            resolution.obec_kod, resolution.disputed or "-",
        )
        return None
    return resolution


def _write_slice(conn: psycopg.Connection, resolutions: list[Any]) -> None:
    """ONE statement for the whole slice. There is nothing to order any more: the
    resolutions, the candidate set, the contradiction ledger, the disposition log, the
    read-your-writes `location_disputed` read and the property rollup are all gone, and with
    them the write ORDER that used to carry meaning."""
    if not resolutions:
        return
    resolve_db.upsert_listing_locations_bulk(
        conn, [projection.build_listing_row(r) for r in resolutions]
    )
    for resolution in resolutions:
        LOG.info(
            "RESOLVE ok listing_id=%s granularity=%s confidence=%s country=%s disputed=%s",
            resolution.listing_id, resolution.granularity, resolution.match_confidence,
            resolution.country_status, resolution.disputed or "-",
        )


def _queue_health(conn: psycopg.Connection, timeout_s: int) -> tuple[int, float]:
    """Bounded: this runs BETWEEN batches, i.e. on the autocommit connection where the batch
    transaction's `SET LOCAL` no longer applies. It is an observability read — the drain must
    never be unable to start a batch because counting the queue hung."""
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
    conn: psycopg.Connection, *, window: int = DEFAULT_SWEEP_WINDOW,
) -> int:
    """`location_resolve_sweep`: the backstop for lost enqueues, and the mechanism a version
    bump rides. The incremental lane stays the primary path — this re-enqueues what a
    `resolver_version` bump, a registry reload, a dropped enqueue or a claimless listing left
    behind, plus every active Czech listing still without a town, in listing-id windows (see
    `_SWEEP_SQL`). Its scope is what BROWSE SERVES — active listings plus the display listing
    of every active property, delisted ones included."""
    if window <= 0:
        raise ValueError("sweep window must be positive")
    seconds = loader_db.env_timeout_s(SWEEP_TIMEOUT_ENV, DEFAULT_SWEEP_TIMEOUT_S)
    _, registry_label = resolve_db.current_registry_version(conn)
    with _bounded(conn, seconds) as cur:
        cur.execute(_SWEEP_UPPER_BOUND_SQL)
        row = cur.fetchone()
        upper = int(row[0]) if row else 0
    enqueued = windows = after = 0
    while after < upper:
        hi = min(after + window, upper)
        n = _execute_window(
            conn, seconds, _SWEEP_SQL, (RESOLVER_VERSION, registry_label, after, hi),
        )
        enqueued += n
        windows += 1
        LOG.info("SWEEP window=(%d,%d] enqueued=%d", after, hi, n)
        after = hi
    LOG.info("SWEEP enqueued=%d windows=%d width=%d timeout=%ds", enqueued, windows, window, seconds)
    return enqueued


def open_connection() -> psycopg.Connection:
    """The hot loop wants a SESSION-mode connection so its recurring statements get
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
    parser = argparse.ArgumentParser(
        description="drain dirty_locations (bind -> fill -> grade -> check)"
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH)
    parser.add_argument("--max-seconds", type=int, default=DEFAULT_MAX_SECONDS)
    parser.add_argument("--listing-id", type=int, default=None)
    parser.add_argument(
        "--full-sweep", action="store_true",
        help="first enqueue every active listing whose answer row is missing or stale",
    )
    parser.add_argument(
        "--sweep-window", type=int, default=DEFAULT_SWEEP_WINDOW,
        help="full-sweep only: listing-id width of one bounded window",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with open_connection() as conn:
        # The sweep ENQUEUES before the lease is even attempted. The lease guards the DRAIN
        # (claim, resolve, write); the enqueue is an idempotent insert whose only exclusion
        # need — the archive sweep's bulk writes into the same table — is the `location-batch`
        # Actions group this mode runs in. The Railway lane holds the drain lease ~94% of the
        # time, so an enqueue gated behind it ran on 2026-09-10 as a 20-second "DRAIN skipped"
        # success that enqueued nothing (run 34482389394).
        if args.full_sweep and not args.dry_run:
            enqueue_full_sweep(conn, window=args.sweep_window)
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
                dry_run=args.dry_run,
                only_listing_id=args.listing_id,
            )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
