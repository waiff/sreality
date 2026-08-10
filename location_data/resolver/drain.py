"""`location_resolve_incremental` — the `dirty_locations` drain (03 §3.14.1, 00 §9).

This is the job that makes location live. Without it a newly scraped listing gets claims in
the detail drain and **never gets a resolution** — no projection row, invisible to Browse,
map, watchdog and dedup.

Batch discipline (03 §3.14.3, learned three times):

* bounded slices claimed with `FOR UPDATE SKIP LOCKED`, ONE transaction per batch, and the
  projection rebuilt in the same transaction as the resolution — that is what gives an
  operator edit read-your-writes;
* a per-listing SAVEPOINT, so one poisonous listing costs its own row and not the batch;
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

Throughput discipline (measured, not assumed — the first production drain did ~3 s per
listing, i.e. ~28 h for the 34 k queue and ~23 days for the ~650 k corpus). The cost is
POOLER ROUND TRIPS — ~35 per listing: 16 registry lookups from the core and the reconciler,
plus ~19 reads and writes here — not CPU. So all four levers attack round trips, and every one
of them is I/O-layer only: the pure core's inputs and answers are unchanged, so deterministic
replay stays bit-for-bit. A warm listing now costs ~11.

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
4. **`executemany`** for the per-candidate / per-detection / per-disposition writers.

No `--workers`: a second connection could claim a disjoint slice safely (`FOR UPDATE SKIP
LOCKED`), but two workers holding two listings of the SAME property would race
`_rebuild_property` — each reads the member set, then both write `property_location_current`
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
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import psycopg

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

_PROPERTY_MEMBERS_SQL = """
SELECT listing_id, source, ST_Y(geom), ST_X(geom), granularity::text, position_source::text,
       blur_evidence::text, match_confidence::text, uncertainty_radius_m,
       radius_semantics::text, position_licence_class::text, ruian_adm_kod,
       stavebni_objekt_kod, obec_kod, cast_obce_kod, okres_kod, kraj_kod, admin_path::text,
       admin_assignment_method::text, street_name, psc, display_label, place_search_text,
       country_code, country_status::text, pin_shared_by_n, geo_blockable, render_as,
       position_quality_class
  FROM listing_location_current
 WHERE property_id = %s
 ORDER BY listing_id
"""

_BATCH_GUC = ("SET LOCAL statement_timeout = '30s'", "SET LOCAL lock_timeout = '5s'")

# `location_resolve_sweep` — the daily reconcile backstop for lost enqueues. Everything
# whose projection is missing or was built at a version tuple that is no longer current.
_FULL_SWEEP_SQL = """
INSERT INTO dirty_locations (listing_id, reason)
SELECT DISTINCT c.listing_id, 'full_sweep'
  FROM location_claims_live c
  LEFT JOIN listing_location_current p ON p.listing_id = c.listing_id
 WHERE p.listing_id IS NULL
    OR p.resolver_version <> %s
    OR p.policy_version <> %s
    OR p.registry_version_id <> %s
ON CONFLICT (listing_id) DO NOTHING
"""


@dataclass(slots=True)
class DrainStats:
    claimed: int = 0
    resolved: int = 0
    failed: int = 0
    batches: int = 0
    # Phase timings, so the NEXT production run measures itself instead of being re-diagnosed.
    prefetch_seconds: float = 0.0
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


def _prefetch(conn: psycopg.Connection, listing_ids: list[int]) -> _Slice:
    return _Slice(
        claims=resolve_db.load_claims_bulk(conn, listing_ids),
        previous_inputs=resolve_db.previous_consumed_inputs_bulk(conn, listing_ids),
        property_ids=resolve_db.property_ids_bulk(conn, listing_ids),
        open_keys=resolve_db.open_dedupe_keys_bulk(conn, listing_ids),
    )


def run(
    conn: psycopg.Connection,
    *,
    batch_size: int = DEFAULT_BATCH,
    max_seconds: int = DEFAULT_MAX_SECONDS,
    policy_version: str = POLICY_VERSION_DEFAULT,
    dry_run: bool = False,
    only_listing_id: int | None = None,
) -> DrainStats:
    # Corpus constants: read ONCE for the run, never per listing. Everything here is either
    # pinned by version (registry, epoch) or a small operator-curated table, so a re-read per
    # listing would buy nothing and cost a round trip each.
    registry_version_id, registry_label = resolve_db.current_registry_version(conn)
    epoch_id = resolve_db.current_epoch(conn)
    if epoch_id is None:
        raise RuntimeError(
            "no pin_cluster_epochs row: mint one with `python -m location_data.resolver."
            "epoch_job` first — collision_epoch_id is NOT NULL and part of the identity"
        )
    cache = resolve_db.RunCache()
    ctx_base = _context(conn, registry_version_id, epoch_id, policy_version, cache)
    stats = DrainStats()
    started = time.monotonic()

    if only_listing_id is not None:
        with conn.transaction():
            _resolve_one(
                conn, only_listing_id, ctx_base, registry_version_id, registry_label,
                epoch_id, policy_version, _prefetch(conn, [only_listing_id]), stats,
                dry_run=dry_run,
            )
            stats.claimed = stats.resolved = 1
        stats.seconds = time.monotonic() - started
        return stats

    while time.monotonic() - started < max_seconds:
        depth, oldest = _queue_health(conn)
        LOG.info("QUEUE depth=%d oldest_age_s=%.0f", depth, oldest)
        batch_started = time.monotonic()
        batch_before = (stats.resolved, stats.failed)
        with conn.transaction():
            with conn.cursor() as cur:
                for statement in _BATCH_GUC:
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
            for listing_id, attempts in rows:
                stats.claimed += 1
                try:
                    with conn.transaction():  # SAVEPOINT: one bad row, one bad row
                        _resolve_one(
                            conn, int(listing_id), ctx_base, registry_version_id,
                            registry_label, epoch_id, policy_version, slice_, stats,
                            dry_run=dry_run,
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
        _log_batch(stats, cache, len(rows), batch_before, time.monotonic() - batch_started)
    stats.seconds = time.monotonic() - started
    LOG.info(
        "DRAIN done batches=%d claimed=%d resolved=%d failed=%d %.1fs rate=%.1f/s "
        "prefetch=%.1fs core=%.1fs write=%.1fs registry_q=%d registry_hit=%.0f%%",
        stats.batches, stats.claimed, stats.resolved, stats.failed, stats.seconds,
        stats.rate, stats.prefetch_seconds, stats.core_seconds, stats.write_seconds,
        cache.misses, 100.0 * cache.hit_rate,
    )
    return stats


def _log_batch(
    stats: DrainStats,
    cache: resolve_db.RunCache,
    n: int,
    before: tuple[int, int],
    elapsed: float,
) -> None:
    """Per-batch self-measurement: the next production run reports its own listings/s and
    where the time went, so nobody has to re-derive it from log timestamps."""
    LOG.info(
        "BATCH n=%d ok=%d fail=%d %.1fs rate=%.1f/s cum(prefetch=%.1fs core=%.1fs "
        "write=%.1fs) registry_q=%d registry_hit=%.0f%% registry_wait=%.1fs",
        n, stats.resolved - before[0], stats.failed - before[1], elapsed,
        0.0 if elapsed <= 0 else n / elapsed, stats.prefetch_seconds, stats.core_seconds,
        stats.write_seconds, cache.misses, 100.0 * cache.hit_rate, cache.seconds,
    )


def _context(
    conn: psycopg.Connection,
    registry_version_id: int,
    epoch_id: int,
    policy_version: str,
    cache: resolve_db.RunCache,
) -> ResolverContext:
    return ResolverContext(
        registry=resolve_db.CachedRegistryView(
            resolve_db.SqlRegistryView(conn, registry_version_id), cache
        ),
        constants=resolve_db.load_constants(conn),
        field_policy=resolve_db.load_field_policy(conn, policy_version),
        uncertainty_policy=resolve_db.load_uncertainty_policy(conn, policy_version),
        collision_policy=resolve_db.load_collision_policy(conn, policy_version),
        collision=resolve_db.CachedCollisionEvidence(
            resolve_db.SqlCollisionEvidence(conn, epoch_id), cache
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
    claims = slice_.claims.get(listing_id, [])
    if not claims:
        LOG.info("RESOLVE skip listing_id=%s reason=no_claims", listing_id)
        return
    core_started = time.monotonic()
    resolution = core.resolve(
        claims,
        ctx,
        resolver_version=RESOLVER_VERSION,
        registry_version_id=registry_version_id,
        policy_version=policy_version,
        collision_epoch_id=epoch_id,
    )
    stats.core_seconds += time.monotonic() - core_started
    if dry_run:
        LOG.info(
            "RESOLVE dry listing_id=%s status=%s granularity=%s hash=%s",
            listing_id, resolution.status, resolution.precision.granularity,
            resolution.content_hash[:12],
        )
        return

    # Prefetched with the slice, which is strictly BEFORE this run rewrote any projection:
    # the row still points at the resolution this listing was last served from, which is what
    # `inputs_changed` compares against.
    previous_inputs = slice_.previous_inputs.get(listing_id)

    write_started = time.monotonic()
    resolution_id = resolve_db.write_resolution(conn, resolution)
    resolve_db.write_candidates(conn, resolution_id, resolution)
    stats.write_seconds += time.monotonic() - write_started

    core_started = time.monotonic()
    detections, evaluated_rules = reconciler.run_with_coverage(
        resolution, claims, normalize.normalize_all(claims), registry=ctx.registry
    )
    stats.core_seconds += time.monotonic() - core_started
    open_before = resolve_db.filter_open_keys(
        slice_.open_keys.get(listing_id, ()), rules=evaluated_rules
    )
    property_id = slice_.property_ids.get(listing_id)

    write_started = time.monotonic()
    resolve_db.write_contradictions(
        conn, detections, reconciler_version=RECONCILER_VERSION,
        resolver_version=RESOLVER_VERSION, registry_version_id=registry_version_id,
        property_id=property_id,
    )
    resolve_db.append_auto_close(
        conn,
        reconciler.auto_close(
            open_before, detections, inputs_changed=_inputs_changed(previous_inputs, resolution)
        ),
    )
    stats.write_seconds += time.monotonic() - write_started

    cluster = (
        ctx.collision.for_point(resolution.source, resolution.position.lat, resolution.position.lon)
        if ctx.collision and resolution.position.lat is not None
        else None
    )
    threshold = ctx.collision_threshold(resolution.source, resolution.admin.obec_kod).threshold_n
    write_started = time.monotonic()
    row = projection.build_listing_row(
        resolution,
        property_id=property_id,
        resolution_id=resolution_id,
        registry_version_label=registry_label,
        rank=ctx.granularity_rank,
        cluster=cluster,
        threshold_n=threshold,
        # NOT prefetched with the slice: this reads back the contradictions THIS listing just
        # wrote a few statements ago, so a batch-start snapshot would miss a fresh major.
        location_disputed=resolve_db.location_disputed(conn, listing_id),
    )
    resolve_db.upsert_listing_projection(conn, row)
    if property_id is not None:
        _rebuild_property(conn, property_id, ctx)
    stats.write_seconds += time.monotonic() - write_started
    LOG.info(
        "RESOLVE ok listing_id=%s status=%s granularity=%s blockable=%s blocked_fields=%s",
        listing_id, resolution.status, row["granularity"], row["geo_blockable"],
        ",".join(resolution.survivorship_blocked) or "-",
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


def _rebuild_property(conn: psycopg.Connection, property_id: int, ctx: ResolverContext) -> None:
    columns = (
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
    with conn.cursor() as cur:
        cur.execute(_PROPERTY_MEMBERS_SQL, (property_id,))
        members: list[dict[str, Any]] = [dict(zip(columns, row)) for row in cur.fetchall()]
    row = projection.build_property_row(property_id, members, rank=ctx.granularity_rank)
    if row is not None:
        resolve_db.upsert_property_projection(conn, row)


def _queue_health(conn: psycopg.Connection) -> tuple[int, float]:
    with conn.cursor() as cur:
        cur.execute(_QUEUE_HEALTH_SQL)
        depth, oldest = cur.fetchone() or (0, 0)
    return int(depth), float(oldest)


def enqueue_full_sweep(conn: psycopg.Connection, *, policy_version: str) -> int:
    """`location_resolve_sweep`: the backstop for lost enqueues. The incremental lane stays
    the primary path — this only re-enqueues what a version bump or a dropped enqueue left
    behind."""
    registry_version_id, _ = resolve_db.current_registry_version(conn)
    with conn.cursor() as cur:
        cur.execute(_FULL_SWEEP_SQL, (RESOLVER_VERSION, policy_version, registry_version_id))
        enqueued = cur.rowcount
    LOG.info("SWEEP enqueued=%d", enqueued)
    return enqueued


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
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with open_connection() as conn:
        with lease.held(
            conn,
            JOB_NAME,
            cadence="15 minutes",
            concurrency_group=CONCURRENCY_GROUP,
        ) as acquired:
            if not acquired:
                LOG.info("DRAIN skipped: another run holds the %s lease", JOB_NAME)
                return 0
            if args.full_sweep and not args.dry_run:
                enqueue_full_sweep(conn, policy_version=args.policy_version)
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
