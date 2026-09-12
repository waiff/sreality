"""Always-on realtime supervisor (realtime-scrapers Wave C-3).

A SECOND Railway service from the same Docker image (start command
`python -m scraper.realtime_worker`) that replaces cron quantization for the
latency-critical path. Settings-paced asyncio lanes (the proven
matcher/outbox pattern from api/notifications + api/notification_outbox):

- probe:     every `realtime_probe_interval_seconds` (default 180), run the
             newest-first delta probe (portal_runner.run_index_probe, Wave C-2)
             sequentially over the probe-capable portals — diff + enqueue only,
             never mark_inactive.
- drain:     every `realtime_drain_interval_seconds` (default 30), claim a
             bounded slice of the shared listing_detail_queue per source that
             has claimable rows. SKIP LOCKED makes this safe beside the GitHub
             Actions drains by construction. Sources listed in
             `realtime_drain_disabled_sources` are skipped — a per-source
             kill-switch (e.g. to freeze a portal's queue at low attempts
             during a proxy outage instead of burning them to given_up).
- images:    every `realtime_images_interval_seconds` (default 60), drain a
             `realtime_images_slice`-capped slice (default 500) of pending
             image downloads via the one existing machinery
             (scraper.main._run_image_downloads: per-host semaphore + breaker,
             active-only newest-first) — the latency lever that feeds the
             images-first publication gate in api/notifications. Coexists with
             images_fresh.yml (idempotent storage_path-IS-NULL selection);
             without R2 env vars the lane logs once and idles (the proxy-skip
             posture).
- count_probe: every `realtime_sreality_count_interval_seconds` (default 600),
             poll pagination.total per sreality (cm, ct) pair (one cheap request
             each — SrealityClient.probe_result_size). sreality's v1 API ignores
             sort params, so a newest-first probe is impossible; a count change
             beyond +-1 jitter is the cheap "something appeared/left" signal, and
             (when dispatch is opted in — a token env AND the
             realtime_sreality_count_dispatch_enabled setting) it triggers a
             targeted index_walk sooner than the */15 cron. Always records
             per-pair totals to sreality_count_probe_state (migration 270).
- maintenance: every `realtime_maintenance_interval_seconds` (default 120), one
             incremental property-maintenance pass — straggler attach + dirty-set
             recompute, via scripts.recompute_property_stats.
             run_incremental_pass (THE same implementation the GH cron runs;
             never forked). Exists because GH throttles property_maintenance.yml
             (nominal */5) to a measured 2h median / 4.1h worst — this lane is
             what actually delivers "a new/changed listing reaches properties
             (and the Browse read model) within minutes". Safe beside the GH
             cron + daily sweep: all callers serialize on the maintenance
             lease row (migration 279 — pooler-proof CAS; a session advisory
             lock strands over the transaction pooler) — a concurrent caller
             skips.
- location_resolve: every `realtime_location_resolve_interval_seconds`
             (default 15), one bounded pass of THE location resolver drain
             (location_data.resolver.drain.run — the same code
             location_resolve.yml runs), budget
             `realtime_location_resolve_max_seconds` (default 240, clamped) over
             `realtime_location_resolve_batch_size` rows (default 250, clamped),
             draining `LOCATION_RESOLVE_WORKERS` slices CONCURRENTLY (env var,
             default 4, clamped — a throughput knob, not a flag; see the
             constant block below). DARK until
             `realtime_location_resolve_enabled` is set. Safe beside
             location_resolve.yml: both take the SAME location_jobs lease row.
- heartbeat: every 30s, upsert this worker's beat + per-lane counters into
             worker_heartbeats (migration 269) — the Health-page liveness hook.

SHIPS DARK: the process exits immediately unless env REALTIME_WORKER_ENABLED=1,
so merging changes nothing until the operator creates the Railway service.
Setting any lane's interval <= 0 idles that lane (kill-switch without redeploy).

mmreality JOINED the registry 2026-09-07 with presence-verified delisting: its
queue only drained inside the 6-hourly job before, so the page checks that now
close its listings would have waited hours (it is proxied, like ceskereality,
which the worker already serves). sreality JOINED in Phase 4 of the portal-order-fidelity
program (docs/design/portal-order-fidelity.md) via its own bespoke
probe_category (the ceskereality pattern — no sort param to request, so a
per-page early-stop diff replaces it) — UNSPLIT, since the deep-pagination 422
is offset-triggered, not size-triggered, so a shallow probe never needs
sreality's district-split. Adding a portal later is one registry line.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import os
import signal
import threading
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from psycopg.types.json import Jsonb

from scraper import db, image_storage, portal_factory, portal_runner
from scraper.portal import PortalConfig, default_config, load_portal_config

LOG = logging.getLogger("scraper.realtime_worker")

ENABLE_ENV = "REALTIME_WORKER_ENABLED"
WORKER_NAME = "realtime-worker"

PROBE_PAGES = 1
PROBE_INTERVAL_DEFAULT = 180
DRAIN_INTERVAL_DEFAULT = 30
DRAIN_SLICE_DEFAULT = 200
DRAIN_MAX_SECONDS = 120.0
IMAGES_INTERVAL_DEFAULT = 60
IMAGES_SLICE_DEFAULT = 500
# Modest beside the Actions lanes' 32: the worker slice is small and the lane
# shares the CDNs with images_fresh.yml.
IMAGES_WORKERS = 8
HEARTBEAT_INTERVAL_SECONDS = 30.0
IDLE_WAIT_SECONDS = 60.0
LANE_RESTART_SECONDS = 30.0
# Ceiling on ONE lane pass. The drain lane legitimately loops eight portals at
# up to DRAIN_MAX_SECONDS each (~16 min worst case), so this sits well above any
# healthy pass and far below the nine-hour wedge it exists to bound.
LANE_PASS_TIMEOUT_SECONDS = 1800

# maintenance lane: the incremental property-maintenance pass (straggler attach
# + dirty-set recompute — scripts.recompute_property_stats.
# run_incremental_pass, THE same implementation the GH cron runs), every
# MAINTENANCE_INTERVAL_DEFAULT seconds. Exists because GH Actions throttles this
# repo's schedules to ~hourly at best — property_maintenance.yml (nominal */5)
# measured a 2h MEDIAN / 4.1h worst gap (2026-07-07 audit), so "a new listing
# reaches properties/Browse within ~5 min" was off by ~24x. Ships LIVE (the
# fix is the point); interval <= 0 idles it. Safe beside the GH cron + daily
# sweep by construction: all three serialize on the maintenance lease row
# (migration 279) inside run_incremental_pass (a concurrent caller skips).
MAINTENANCE_INTERVAL_DEFAULT = 120
MAINTENANCE_BATCH_SIZE_DEFAULT = 2000

# estimation lane (Wave 1 W1-3 / Amendment A10): drain `pending` estimation_runs
# — the agent/deterministic rent-estimate executor moved OFF the FastAPI request
# threadpool onto this worker, so a 240 s agent run can't pin a Starlette token
# and a deploy SIGTERM can't kill a paid run mid-flight (no resume, no ledger
# row). Ships DARK: the lane idles until the operator sets
# estimation_job_lane_enabled, which ALSO makes POST /estimations route rows to
# it (one flag, both halves). One run per pass keeps each pass bounded by a
# single run's wall clock; the heartbeat lane beats independently so the worker
# stays observably live through a long run. Each pass first runs the periodic
# stuck-run sweep so a run orphaned by a worker crash frees its slot.
ESTIMATION_JOB_LANE_SETTING = "estimation_job_lane_enabled"
ESTIMATION_INTERVAL_DEFAULT = 5
ESTIMATION_STUCK_MINUTES_DEFAULT = 15

# location_resolve lane (Decision 8b): THE resolver drain
# (location_data.resolver.drain.run) run from here instead of only from
# location_resolve.yml. The drain's cost is round trips, not work — ~11 registry
# round trips per listing at ~120 ms from a US GitHub runner measured 0.7
# listings/s, and GitHub fires the schedule ~7x/day, against a >100k
# dirty_locations queue. This worker sits next to the database in the EU, so the
# same code costs ~1-2 ms per trip and runs continuously. The GH lane STAYS as
# the backstop (and owns --full-sweep / --dry-run / --listing-id); the two can
# never drain at once because both take the SAME location_jobs lease row.
# Named location_resolve, never "resolve"/"drain": this worker already has a
# lane called `drain` (the portal detail drain) whose heartbeat key and
# check_worker_lane_stall offender string would become unreadable.
LOCATION_RESOLVE_LANE_SETTING = "realtime_location_resolve_enabled"
LOCATION_RESOLVE_INTERVAL_DEFAULT = 15
LOCATION_RESOLVE_MAX_SECONDS_DEFAULT = 240
# A pass must stay clear of check_worker_lane_stall's 1200 s in_flight warn (a
# healthy pass must never read as a stall) and of LANE_PASS_TIMEOUT_SECONDS (a
# healthy pass must never be abandoned). It does NOT bound the lease — see
# _location_resolve_lease_ttl.
LOCATION_RESOLVE_MAX_SECONDS_CEILING = 900
# mirrors drain.DEFAULT_BATCH; duplicated to keep location_data off the import path
LOCATION_RESOLVE_BATCH_DEFAULT = 250
# The batch size is an operator knob that the lease TTL is derived from, so it
# needs a ceiling of its own: drain.run checks its budget only BETWEEN batches,
# so an unbounded batch is an unbounded overrun past the budget (and one
# unbounded transaction).
LOCATION_RESOLVE_BATCH_CEILING = 1000
# Lease headroom over the pass budget, per listing in one batch. drain.run's
# `while` tests the budget between batches, so a pass runs `max_seconds` plus
# ONE whole batch; the lease is stamped once and never renewed, so the TTL has
# to cover that batch or the GH lane's cron can acquire the row mid-drain. 2 s
# is ~1.6x the drain's own ~1.2 s/listing target and above the 1.43 s/listing
# the GH runner measured, which is the slowest this batch has ever been seen to
# run. Kept derived rather than a flat constant precisely because batch_size is
# tunable: a flat 120 s was only ever right for one batch size.
LOCATION_RESOLVE_LEASE_SECONDS_PER_LISTING = 2
LOCATION_RESOLVE_LEASE_HEADROOM_MIN_SECONDS = 120
# How many slice loops one pass runs CONCURRENTLY (W2-a5). An ENV VAR, not an
# app_settings row and not a flag: it is a throughput parameter of the machine
# this process runs on (connections it may open, cores it has), and it belongs
# with the service's other Railway env vars rather than in the operator settings
# table the lane's cadence knobs live in.
#
# Why 4. Measured 2026-09-12 10:27Z: one loop drained ~8 listings/s (5,000 rows
# per 10 minutes) against a 448k queue, and every backend on the instance was
# waiting on DataFileRead — ~4 registry round trips per listing, each of them a
# disk wait. That is latency, not work, so the loops overlap almost perfectly
# until the instance itself saturates; 4 is the number that leaves the pooler
# and the disk headroom for the other seven lanes.
#
# The CEILING is about CONNECTIONS: every worker opens its own SESSION-mode
# connection (psycopg connections are not thread-safe) and the session pooler's
# slots are shared with every other consumer of the database.
LOCATION_RESOLVE_WORKERS_ENV = "LOCATION_RESOLVE_WORKERS"
LOCATION_RESOLVE_WORKERS_DEFAULT = 4
LOCATION_RESOLVE_WORKERS_CEILING = 8

# sreality count-probe lane (W3): sreality's v1 search API ignores every sort
# param, so its own probe (added Phase 4 of portal-order-fidelity) can only
# diff ids seen on a shallow unsplit page walk, not request a true newest-first
# ordering — this lane is a CHEAP, COMPLEMENTARY total-count signal, not made
# redundant by that probe. It polls pagination.total per (cm, ct) every
# COUNT_PROBE_INTERVAL_DEFAULT seconds; a change beyond +-COUNT_PROBE_JITTER can
# trigger a targeted index_walk sooner than the */15 cron. Interval <= 0 idles it.
COUNT_PROBE_INTERVAL_DEFAULT = 600
COUNT_PROBE_JITTER = 1  # |new-old| within this band is API noise, not a real change
COUNT_PROBE_RATE_PER_S = 2.0  # ~20 total-only requests in ~10s, politely paced

# The probe-capable portals (design doc: newest-first index order, or a bespoke
# probe_category). Stable order = polite, predictable per-pass sequencing. Also
# the set the drain lane serves — the worker only drains portals it knows how
# to build.
REALTIME_SOURCES: tuple[str, ...] = (
    "bazos", "bezrealitky", "ceskereality", "idnes",
    "maxima", "mmreality", "realitymix", "remax", "sreality",
)

# The class MAPPING lives in scraper.portal_factory, because the coverage gate
# needs the same table to ask a portal what its declared categories canonicalise
# to. The SCOPE stays here and stays narrower: the factory knows every portal,
# while this worker deliberately drains only REALTIME_SOURCES (every walked
# portal since 2026-09-07; bazos predates the uniform constructor).
# Shared mapping, local scope, so neither caller can silently widen the other.
_PORTAL_CLASSES = {
    k: v for k, v in portal_factory.PORTAL_CLASSES.items()
    if k in REALTIME_SOURCES and k != "bazos"
}
_CLIENT_CLASSES = {
    k: v for k, v in portal_factory.CLIENT_CLASSES.items()
    if k in REALTIME_SOURCES
}

# log-once-per-process guard for proxied portals skipped without SCRAPER_PROXY_URL.
_PROXY_WARNED: set[str] = set()

# log-once-per-process guard when R2 env vars are absent on the worker.
_R2_WARNED: set[str] = set()

# Count-probe walk DISPATCH is double-gated — a token env var AND the
# realtime_sreality_count_dispatch_enabled setting (default off) — so the lane
# ships dark for triggering (it still RECORDS per-pair totals for observability).
# index_walk.yml has no per-category inputs, so a trigger is a FULL walk.
DISPATCH_TOKEN_ENVS = ("WORKER_DISPATCH_TOKEN", "SCRAPE_CHAIN_TOKEN")
DISPATCH_REPO = os.environ.get("WORKER_GH_REPO", "waiff/sreality")
DISPATCH_WORKFLOW = "index_walk.yml"
DISPATCH_REF = os.environ.get("WORKER_GH_REF", "main")
# log-once-per-process guard when dispatch is enabled but no token is configured.
_DISPATCH_WARNED: set[str] = set()

# log-once-per-process guard: the resolve lane running on the TRANSACTION pooler.
_RESOLVE_SESSION_WARNED = False
# Logged on the TRANSITION into a skipped pass, not per pass: at a 15 s interval
# a 30-minute GH run would otherwise emit 120 identical lines.
_RESOLVE_LEASE_BUSY_LOGGED = False
_RESOLVE_WEDGE_LOGGED = False
# The lane's own mutual exclusion, INSIDE this process. A pass abandoned at
# LANE_PASS_TIMEOUT_SECONDS keeps running (Python cannot kill the thread) while
# its lease expires strictly earlier, so the lease alone cannot stop the next
# pass from draining beside the still-live one. Held for the whole pass; a pass
# that cannot take it does not touch the lease at all.
_RESOLVE_PASS_LOCK = threading.Lock()
# Whether the last pass came back with an empty queue — drives the drain's log
# level (see _tune_resolver_log_level).
_RESOLVE_LAST_IDLE = False


# --- settings (read per pass so operator edits take effect without restart) ---


def _read_setting(key: str) -> Any:
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
            row = cur.fetchone()
        return None if row is None else row[0]
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def _read_int(key: str, default: int) -> int:
    value = _read_setting(key)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _read_probe_interval() -> int:
    return _read_int("realtime_probe_interval_seconds", PROBE_INTERVAL_DEFAULT)


def _read_drain_interval() -> int:
    return _read_int("realtime_drain_interval_seconds", DRAIN_INTERVAL_DEFAULT)


def _read_drain_slice() -> int:
    return _read_int("realtime_drain_slice", DRAIN_SLICE_DEFAULT)


def _read_images_interval() -> int:
    return _read_int("realtime_images_interval_seconds", IMAGES_INTERVAL_DEFAULT)


def _read_images_slice() -> int:
    return _read_int("realtime_images_slice", IMAGES_SLICE_DEFAULT)


def _read_source_set(key: str) -> set[str]:
    value = _read_setting(key)
    if not isinstance(value, list):
        return set()
    return {str(v) for v in value}


def _read_disabled_sources() -> set[str]:
    return _read_source_set("realtime_probe_disabled_sources")


def _read_drain_disabled_sources() -> set[str]:
    return _read_source_set("realtime_drain_disabled_sources")


def _read_count_probe_interval() -> int:
    return _read_int(
        "realtime_sreality_count_interval_seconds", COUNT_PROBE_INTERVAL_DEFAULT)


def _read_maintenance_interval() -> int:
    return _read_int(
        "realtime_maintenance_interval_seconds", MAINTENANCE_INTERVAL_DEFAULT)


def _read_maintenance_batch_size() -> int:
    return _read_int(
        "realtime_maintenance_batch_size", MAINTENANCE_BATCH_SIZE_DEFAULT)


def _read_estimation_interval() -> int:
    # The flag gates the lane via interval<=0 (idle-not-dead, the _lane_loop
    # contract): disabled => 0 (no claim attempts at all), enabled => the
    # configured poll interval. Ships dark because the flag defaults absent.
    if not _read_flag(ESTIMATION_JOB_LANE_SETTING):
        return 0
    return _read_int(
        "realtime_estimation_interval_seconds", ESTIMATION_INTERVAL_DEFAULT)


def _read_estimation_stuck_minutes() -> int:
    return _read_int(
        "estimation_stuck_run_minutes", ESTIMATION_STUCK_MINUTES_DEFAULT)


def _read_location_resolve_interval() -> int:
    # The flag gates the lane via interval<=0 (idle-not-dead, the _lane_loop
    # contract): disabled => 0 (no lease attempt at all), enabled => the
    # configured poll interval. Ships dark because the flag defaults absent.
    if not _read_flag(LOCATION_RESOLVE_LANE_SETTING):
        return 0
    return _read_int(
        "realtime_location_resolve_interval_seconds",
        LOCATION_RESOLVE_INTERVAL_DEFAULT,
    )


def _read_location_resolve_max_seconds() -> int:
    # Clamped, not trusted: a mis-set setting must still leave a HEALTHY pass
    # below check_worker_lane_stall's 1200 s in_flight warn and below
    # LANE_PASS_TIMEOUT_SECONDS, or the lane would abandon its own normal work.
    value = _read_int(
        "realtime_location_resolve_max_seconds",
        LOCATION_RESOLVE_MAX_SECONDS_DEFAULT,
    )
    return max(1, min(value, LOCATION_RESOLVE_MAX_SECONDS_CEILING))


def _read_location_resolve_batch_size() -> int:
    value = _read_int(
        "realtime_location_resolve_batch_size", LOCATION_RESOLVE_BATCH_DEFAULT)
    return max(1, min(value, LOCATION_RESOLVE_BATCH_CEILING))


def _read_location_resolve_workers() -> int:
    """How many slice loops one pass runs concurrently. Env, clamped, never 0 —
    an unparseable value is a typo, and the lane must keep draining."""
    raw = os.environ.get(LOCATION_RESOLVE_WORKERS_ENV, "").strip()
    try:
        value = int(raw) if raw else LOCATION_RESOLVE_WORKERS_DEFAULT
    except ValueError:
        LOG.warning(
            "%s=%r is not an integer; draining with %d workers",
            LOCATION_RESOLVE_WORKERS_ENV, raw, LOCATION_RESOLVE_WORKERS_DEFAULT)
        value = LOCATION_RESOLVE_WORKERS_DEFAULT
    return max(1, min(value, LOCATION_RESOLVE_WORKERS_CEILING))


def _location_resolve_lease_ttl(max_seconds: int, batch_size: int) -> int:
    """Lease TTL for one pass: the budget plus the one batch that can start just
    under it. Both inputs are clamped, so the TTL is bounded too.

    Unchanged by the worker count: drain.run tests the budget between batches in
    EVERY loop, and the loops run concurrently, so N workers still overrun by at
    most ONE batch's wall clock — not N."""
    headroom = max(
        LOCATION_RESOLVE_LEASE_HEADROOM_MIN_SECONDS,
        batch_size * LOCATION_RESOLVE_LEASE_SECONDS_PER_LISTING,
    )
    return max_seconds + headroom


def _read_flag(key: str) -> bool:
    value = _read_setting(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


def _read_count_dispatch_enabled() -> bool:
    return _read_flag("realtime_sreality_count_dispatch_enabled")


# --- portal construction ----------------------------------------------------


def _load_config(source: str) -> PortalConfig:
    try:
        with db.connect() as conn:
            return load_portal_config(conn, source)
    except Exception as exc:  # noqa: BLE001 - registry hiccup must not break a pass
        LOG.warning(
            "load_portal_config failed source=%s: %s; using baked-in default",
            source, exc,
        )
        return default_config(source)


def _build_portal(source: str, config: PortalConfig) -> Any:
    # Delegated: the coverage gate builds portals from the same table for the same
    # reason (to read a portal's own category canonicalisation), so the two
    # constructor exceptions live in one place.
    return portal_factory.build_portal(source, config)


def _skip_for_proxy(source: str) -> bool:
    """True when the portal REQUIRES the residential proxy and the env is unset —
    a direct request would only burn a WAF 403.

    A portal that merely prefers the proxy (`PROXY_REQUIRED = False`, i.e. the
    direct IP is throttled but functional) must still run: skipping it would
    trade slow data for no data.
    """
    mod_name, cls_name = _CLIENT_CLASSES[source]
    cls = getattr(importlib.import_module(mod_name), cls_name)
    if not getattr(cls, "USE_PROXY", False):
        return False
    if not getattr(cls, "PROXY_REQUIRED", True):
        return False
    env = getattr(cls, "PROXY_ENV", "SCRAPER_PROXY_URL")
    if os.environ.get(env):
        return False
    if source not in _PROXY_WARNED:
        _PROXY_WARNED.add(source)
        LOG.warning("%s unset; skipping proxied portal %s", env, source)
    return True


# --- lane passes (sync halves run in a thread) --------------------------------


def _run_probe_sync(source: str) -> dict[str, Any]:
    config = _load_config(source)
    portal = _build_portal(source, config)
    rc, agg = portal_runner.run_index_probe(
        portal, dry_run=False, probe_pages=PROBE_PAGES,
    )
    agg["rc"] = rc
    return agg


def _run_drain_sync(source: str, max_claims: int) -> dict[str, Any]:
    config = _load_config(source)
    portal = _build_portal(source, config)
    # run_id=None: no scrape_runs row per pass — a 30s cadence would write
    # thousands of bookkeeping rows/day (the images-only precedent: liveness
    # noise). Worker observability lives in worker_heartbeats; the listing
    # writes themselves feed Health identically (first_seen_at, queue age).
    rc, agg = portal_runner.run_detail_drain(
        portal,
        max_claims=max_claims,
        dry_run=False,
        detail_workers=config.limits.detail_workers,
        detail_rate=config.limits.detail_rate,
        max_seconds=DRAIN_MAX_SECONDS,
    )
    agg["rc"] = rc
    return agg


def _claimable_by_source() -> dict[str, int]:
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source, count(*) FROM listing_detail_queue "
                "WHERE claimed_at IS NULL AND given_up = false "
                "GROUP BY source"
            )
            return {source: int(n) for source, n in cur.fetchall()}
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def _record_pass(state: dict[str, Any], lane: str, last: dict[str, Any]) -> None:
    prev = state["lanes"].get(lane, {})
    started = prev.get("started_at")
    duration: float | None = None
    if started:
        with contextlib.suppress(ValueError):
            duration = round(
                (datetime.now(timezone.utc) - datetime.fromisoformat(started)).total_seconds(), 1
            )
    state["lanes"][lane] = {
        "last_pass_at": datetime.now(timezone.utc).isoformat(),
        "passes": int(prev.get("passes", 0)) + 1,
        "last_duration_s": duration,
        # Cleared here: a lane with started_at set in the heartbeat is a pass
        # STILL RUNNING, which is the whole point of recording it.
        "started_at": None,
        # Carried, not reset. A lane that alternates fail/succeed would otherwise
        # report zero failures whenever the most recent pass happened to work --
        # hiding exactly the flapping this instrument exists to expose.
        "failed_passes": int(prev.get("failed_passes", 0)),
        "last_failure_at": prev.get("last_failure_at"),
        "last": last,
    }


def _record_pass_failed(state: dict[str, Any], lane: str, duration: float) -> None:
    """A pass that raised. Counted separately from a completed one so a lane that
    is crash-looping (fails fast, forever) cannot be mistaken for one that is
    working, nor for one that is hung."""
    prev = dict(state["lanes"].get(lane, {}))
    prev["started_at"] = None
    prev["last_duration_s"] = duration
    prev["failed_passes"] = int(prev.get("failed_passes", 0)) + 1
    prev["last_failure_at"] = datetime.now(timezone.utc).isoformat()
    state["lanes"][lane] = prev


def _record_pass_start(state: dict[str, Any], lane: str) -> None:
    """Stamp that a pass BEGAN.

    Passes were previously recorded only on completion, so a lane wedged inside
    run_pass() was indistinguishable from a lane that had simply never been
    scheduled — both showed the same stale counter. That is exactly the state the
    drain lane was found in (one pass in nine hours while the images lane managed
    486), and nothing in the system could say whether it was blocked, slow, or
    idle. `_lane_loop` awaits run_pass() with no timeout and `_supervised` catches
    exceptions rather than hangs, so an unbounded await is invisible AND
    unrecoverable without a redeploy.
    """
    prev = dict(state["lanes"].get(lane, {}))
    prev["started_at"] = datetime.now(timezone.utc).isoformat()
    state["lanes"][lane] = prev


def _record_lane_failure(source: str, lane: str, exc: BaseException) -> None:
    """The worker's half of W3.1. Both portal lanes here call `portal_runner` DIRECTLY
    (run_id=None, deliberately — a 30s cadence would write thousands of bookkeeping
    rows/day), so they bypass `run_phase` and its crash accounting entirely: a
    CheckViolation on the latency-critical drain used to produce a log line and
    nothing else, forever. One shared function, not a second producer.

    BLOCKING — callers must `await asyncio.to_thread(...)` it, like every other DB touch
    in this file. `db.connect()` is `_connect_with_retry` (3 attempts, `time.sleep(10.0)`
    between), and `is_transient_db_error` is true for every `OperationalError`, so on a
    pooler outage a bare call sleeps ~20s per source ON THE EVENT LOOP. A drain pass
    iterates all nine sources, and the 30s heartbeat lane is a sibling coroutine on the
    same loop — i.e. the recorder would blind `worker_liveness` and `worker_lane_stall`
    during exactly the DB incident it exists to record."""
    try:
        with db.connect() as conn:
            portal_runner.record_failure_signature(conn, exc, source=source, lane=lane)
    except Exception as rec_exc:  # noqa: BLE001 - observability must never end a pass
        LOG.warning("could not record %s lane failure for %s: %r", lane, source, rec_exc)


async def _probe_pass(stop_event: asyncio.Event, state: dict[str, Any]) -> None:
    disabled: set[str] = set()
    try:
        disabled = await asyncio.to_thread(_read_disabled_sources)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("probe lane: failed to read disabled sources: %s", exc)
    totals = {"portals": 0, "new": 0, "enqueued": 0, "errors": 0, "skipped": 0}
    for source in REALTIME_SOURCES:
        if stop_event.is_set():
            break
        if source in disabled or _skip_for_proxy(source):
            totals["skipped"] += 1
            continue
        try:
            agg = await asyncio.to_thread(_run_probe_sync, source)
        except Exception as exc:  # noqa: BLE001 - one portal must not end the pass
            LOG.exception("PROBE lane source=%s failed", source)
            await asyncio.to_thread(_record_lane_failure, source, "probe", exc)
            totals["errors"] += 1
            continue
        totals["portals"] += 1
        totals["new"] += agg.get("listings_found_new", 0)
        totals["enqueued"] += agg.get("listings_enqueued", 0)
        totals["errors"] += agg.get("errors", 0)
        LOG.info(
            "PROBE lane source=%s pages=%d new=%d enqueued=%d "
            "early_stopped=%d errors=%d",
            source, agg.get("index_pages", 0), agg.get("listings_found_new", 0),
            agg.get("listings_enqueued", 0), agg.get("early_stopped", 0),
            agg.get("errors", 0),
        )
    _record_pass(state, "probe", totals)


async def _drain_pass(stop_event: asyncio.Event, state: dict[str, Any]) -> None:
    slice_ = DRAIN_SLICE_DEFAULT
    try:
        slice_ = await asyncio.to_thread(_read_drain_slice)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("drain lane: failed to read slice: %s", exc)
    if slice_ <= 0:
        LOG.debug("drain lane: slice<=0; skipping pass")
        return
    disabled: set[str] = set()
    try:
        disabled = await asyncio.to_thread(_read_drain_disabled_sources)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("drain lane: failed to read disabled sources: %s", exc)
    counts = await asyncio.to_thread(_claimable_by_source)
    totals = {
        "sources": 0, "new": 0, "updated": 0, "gone": 0, "errors": 0, "skipped": 0,
    }
    for source in REALTIME_SOURCES:
        if stop_event.is_set():
            break
        claimable = counts.get(source, 0)
        if claimable <= 0:
            continue
        if source in disabled or _skip_for_proxy(source):
            totals["skipped"] += 1
            continue
        try:
            agg = await asyncio.to_thread(_run_drain_sync, source, slice_)
        except Exception as exc:  # noqa: BLE001 - one portal must not end the pass
            LOG.exception("DRAIN lane source=%s failed", source)
            await asyncio.to_thread(_record_lane_failure, source, "drain", exc)
            totals["errors"] += 1
            continue
        totals["sources"] += 1
        totals["new"] += agg.get("listings_scraped_new", 0)
        totals["updated"] += agg.get("listings_updated", 0)
        totals["gone"] += agg.get("listings_inactive", 0)
        totals["errors"] += agg.get("errors", 0)
        LOG.info(
            "DRAIN lane source=%s claimable=%d new=%d updated=%d gone=%d errors=%d",
            source, claimable, agg.get("listings_scraped_new", 0),
            agg.get("listings_updated", 0), agg.get("listings_inactive", 0),
            agg.get("errors", 0),
        )
    _record_pass(state, "drain", totals)


def _run_images_sync(max_downloads: int) -> dict[str, Any]:
    # Reuse THE image machinery (per-host semaphore, breaker, active-only +
    # newest-first via db.pending_image_downloads) — never fork it. Lazy import
    # keeps scraper.main off the worker's startup path.
    from scraper.main import _run_image_downloads

    return _run_image_downloads(max_downloads, IMAGES_WORKERS, active_only=True)


async def _images_pass(stop_event: asyncio.Event, state: dict[str, Any]) -> None:
    slice_ = IMAGES_SLICE_DEFAULT
    try:
        slice_ = await asyncio.to_thread(_read_images_slice)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("images lane: failed to read slice: %s", exc)
    if slice_ <= 0:
        LOG.debug("images lane: slice<=0; skipping pass")
        return
    if not image_storage.is_configured():
        # Same posture as the proxy skip: log once and idle. The Actions image
        # lanes keep draining; setting the R2 vars enables this lane live.
        if "r2" not in _R2_WARNED:
            _R2_WARNED.add("r2")
            LOG.warning("R2 env vars unset; images lane idling")
        _record_pass(state, "images", {"downloaded": 0, "skipped_no_r2": True})
        return
    if stop_event.is_set():
        return
    agg = await asyncio.to_thread(_run_images_sync, slice_)
    totals = {
        "downloaded": agg.get("images_stored", 0),
        "stopped_suspicious": bool(agg.get("stopped_suspicious", False)),
        "cap": slice_,
    }
    LOG.info(
        "IMAGES lane downloaded=%d cap=%d stopped_suspicious=%s",
        totals["downloaded"], slice_, totals["stopped_suspicious"],
    )
    _record_pass(state, "images", totals)


# --- count-probe lane (sreality) ---------------------------------------------


def _dispatch_token() -> str | None:
    for env in DISPATCH_TOKEN_ENVS:
        tok = os.environ.get(env)
        if tok:
            return tok
    return None


def _count_probe_sync() -> dict[str, Any]:
    """One cheap pagination.total request per sreality (cm, ct) pair; diff against
    sreality_count_probe_state and upsert. Returns the pairs whose total moved beyond
    +-COUNT_PROBE_JITTER. A FIRST sighting (no prior total) is recorded but never flagged,
    so first-populating the table can't trigger a walk. No detail/enqueue: the count is the
    only cheap signal sreality's sort-blind v1 API gives."""
    from scraper.main import CATEGORIES, _build_client
    from scraper.rate_limit import RateLimiter

    limiter = RateLimiter(COUNT_PROBE_RATE_PER_S)
    prior: dict[tuple[int, int], int | None] = {}
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT category_main_cb, category_type_cb, last_total "
                "FROM sreality_count_probe_state")
            for cm, ct, total in cur.fetchall():
                prior[(int(cm), int(ct))] = None if total is None else int(total)
    finally:
        with contextlib.suppress(Exception):
            conn.close()

    changed: list[dict[str, Any]] = []
    errors = 0
    rows: list[tuple[int, int, int, bool]] = []  # cm, ct, new_total, is_changed
    for cm, ct in CATEGORIES:
        try:
            total = _build_client(cm, ct, limiter=limiter).probe_result_size()
        except Exception as exc:  # noqa: BLE001 - one category must not end the pass
            LOG.warning("COUNT-PROBE cm=%s ct=%s failed: %s", cm, ct, exc)
            errors += 1
            continue
        if total is None:
            errors += 1
            continue
        old = prior.get((cm, ct))
        is_changed = old is not None and abs(total - old) > COUNT_PROBE_JITTER
        rows.append((cm, ct, total, is_changed))
        if is_changed:
            changed.append({"cm": cm, "ct": ct, "old": old, "new": total})
    _upsert_count_state(rows)
    return {"pairs": len(rows), "changed": changed, "errors": errors}


def _upsert_count_state(rows: list[tuple[int, int, int, bool]]) -> None:
    if not rows:
        return
    conn = db.connect()  # autocommit
    try:
        with conn.cursor() as cur:
            for cm, ct, total, is_changed in rows:
                cur.execute(
                    """
                    INSERT INTO sreality_count_probe_state
                        (category_main_cb, category_type_cb, last_total,
                         last_checked_at, last_changed_at)
                    VALUES (%s, %s, %s, now(), CASE WHEN %s THEN now() ELSE NULL END)
                    ON CONFLICT (category_main_cb, category_type_cb) DO UPDATE SET
                        last_total      = EXCLUDED.last_total,
                        last_checked_at = now(),
                        last_changed_at = CASE WHEN %s THEN now()
                            ELSE sreality_count_probe_state.last_changed_at END
                    """,
                    (cm, ct, total, is_changed, is_changed),
                )
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def _seconds_since_last_sreality_index_walk() -> float | None:
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT extract(epoch FROM (now() - max(started_at))) "
                "FROM scrape_runs WHERE source = 'sreality' AND index_pages > 0")
            row = cur.fetchone()
        return float(row[0]) if row and row[0] is not None else None
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def _post_workflow_dispatch(token: str) -> bool:
    import requests

    url = (f"https://api.github.com/repos/{DISPATCH_REPO}"
           f"/actions/workflows/{DISPATCH_WORKFLOW}/dispatches")
    try:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"ref": DISPATCH_REF},
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001 - a dispatch hiccup must not kill the lane
        LOG.warning("count-probe: dispatch POST failed: %s", exc)
        return False
    if resp.status_code == 204:
        LOG.info("count-probe: dispatched %s (ref=%s)", DISPATCH_WORKFLOW, DISPATCH_REF)
        return True
    LOG.warning("count-probe: dispatch returned %s: %s",
                resp.status_code, resp.text[:200])
    return False


def _maybe_dispatch_index_walk(
    changed: list[dict[str, Any]], cooldown_seconds: float,
) -> dict[str, Any]:
    """Trigger ONE targeted index_walk when a count changed, IF dispatch is enabled
    (the setting AND a token env) and no sreality index walk is fresher than
    cooldown_seconds — a debounce against the */15 cron AND prior triggers (both land in
    scrape_runs as index rows). index_walk.yml has no per-category inputs, so this is a
    full walk. Returns {dispatched, reason}."""
    if not changed:
        return {"dispatched": False, "reason": "no_change"}
    if not _read_count_dispatch_enabled():
        return {"dispatched": False, "reason": "disabled"}
    token = _dispatch_token()
    if not token:
        if "token" not in _DISPATCH_WARNED:
            _DISPATCH_WARNED.add("token")
            LOG.warning(
                "count-probe: dispatch enabled but no token env (%s) set; recording only",
                "/".join(DISPATCH_TOKEN_ENVS))
        return {"dispatched": False, "reason": "no_token"}
    age = _seconds_since_last_sreality_index_walk()
    if age is not None and age < cooldown_seconds:
        return {"dispatched": False, "reason": "fresh_walk", "age": int(age)}
    ok = _post_workflow_dispatch(token)
    return {"dispatched": ok, "reason": "triggered" if ok else "dispatch_failed"}


async def _count_probe_pass(stop_event: asyncio.Event, state: dict[str, Any]) -> None:
    if stop_event.is_set():
        return
    agg = await asyncio.to_thread(_count_probe_sync)
    dispatched = False
    if agg["changed"]:
        cooldown = float(await asyncio.to_thread(_read_count_probe_interval))
        try:
            disp = await asyncio.to_thread(
                _maybe_dispatch_index_walk, agg["changed"], cooldown)
            dispatched = bool(disp.get("dispatched"))
        except Exception:  # noqa: BLE001 - a dispatch decision must not end the pass
            LOG.exception("COUNT-PROBE dispatch decision failed")
    LOG.info(
        "COUNT-PROBE pairs=%d changed=%d errors=%d dispatched=%s",
        agg["pairs"], len(agg["changed"]), agg["errors"], dispatched,
    )
    _record_pass(state, "count_probe", {
        "pairs": agg["pairs"], "changed": len(agg["changed"]),
        "errors": agg["errors"], "dispatched": dispatched,
    })


_HEARTBEAT_SQL = """
    INSERT INTO worker_heartbeats (worker, beat_at, started_at, details)
    VALUES (%(worker)s, now(), %(started_at)s, %(details)s)
    ON CONFLICT (worker) DO UPDATE SET
        beat_at    = now(),
        started_at = EXCLUDED.started_at,
        details    = EXCLUDED.details
"""


def _maintenance_sync() -> dict[str, Any]:
    """One incremental property-maintenance pass on the worker's own
    connection. Reuses THE script implementation (never forks it); the lease
    row inside run_incremental_pass makes this safe beside the GH cron and the
    daily full sweep — a concurrent caller returns skipped. Lazy import keeps
    scripts.recompute_property_stats off the worker's startup path."""
    from scripts.recompute_property_stats import run_incremental_pass

    batch_size = _read_maintenance_batch_size()
    conn = db.connect()
    try:
        return run_incremental_pass(conn, batch_size)
    finally:
        with contextlib.suppress(Exception):
            conn.close()


async def _maintenance_pass(stop_event: asyncio.Event, state: dict[str, Any]) -> None:
    if stop_event.is_set():
        return
    stats = await asyncio.to_thread(_maintenance_sync)
    last = {
        "skipped": bool(stats.get("skipped")),
        "attached": stats.get("attached", 0),
        "recomputed": stats.get("recomputed", 0),
    }
    if last["skipped"]:
        LOG.info("MAINTENANCE lane skipped (lease held by cron or daily sweep)")
    else:
        LOG.info(
            "MAINTENANCE lane attached=%d recomputed=%d",
            last["attached"], last["recomputed"],
        )
    _record_pass(state, "maintenance", last)


# estimation lane: claim ONE pending run atomically over the transaction pooler.
# FOR UPDATE SKIP LOCKED (not a session advisory lock — unsound over the pooler,
# the mig-279 lesson) flips pending->running + stamps claimed_at/worker in one
# statement; `job_payload IS NOT NULL` ensures only lane-routed rows are claimed,
# never a legacy inline row still executing in an API BackgroundTask.
_CLAIM_ESTIMATION_SQL = """
    WITH c AS (
        SELECT id FROM estimation_runs
        WHERE status = 'pending' AND job_payload IS NOT NULL
        ORDER BY created_at
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    )
    UPDATE estimation_runs r
       SET status = 'running', claimed_at = now(), worker = %(worker)s
      FROM c WHERE r.id = c.id
    RETURNING r.id, r.job_payload
"""


def _sweep_estimations_sync() -> int:
    """Periodic zombie-run sweep (A10): fail runs stuck non-terminal past the
    threshold so a run orphaned mid-execution (worker crash) frees its slot
    instead of the user polling a corpse forever. Reuses THE api implementation
    (keys `running` off coalesce(claimed_at, created_at))."""
    from api.estimation_runs import sweep_stuck_runs

    minutes = _read_estimation_stuck_minutes()
    conn = db.connect()
    try:
        return sweep_stuck_runs(conn, older_than_minutes=minutes)
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def _estimation_sync() -> dict[str, Any]:
    """Claim ONE pending run and execute it on the worker's own connection,
    reusing THE api execute_pending_run path (never forks the estimator). One run
    per pass bounds each pass by a single run's wall clock; the heartbeat lane
    beats independently so the worker stays observably alive through a 240 s run.
    Lazy imports keep api.* off the worker's startup path AND off every idle
    pass (only pulled in once a row is actually claimed). Clears job_payload at
    terminal to reclaim the (potentially large) execution snapshot."""
    conn = db.connect()
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(_CLAIM_ESTIMATION_SQL, {"worker": WORKER_NAME})
            row = cur.fetchone()
        if row is None:
            return {"claimed": 0}
        run_id, payload = int(row[0]), row[1]
        from api import dependencies as deps
        from api.estimation_runs import execute_pending_run
        from api.llm_client import LLMClient
        status = "failed"
        try:
            sreality_client = deps.get_sreality_client()
            llm_client = LLMClient(conn, providers=deps.get_providers())
            execute_pending_run(
                conn, sreality_client, llm_client, run_id, payload,
            )
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status FROM estimation_runs WHERE id = %s", (run_id,),
                )
                r = cur.fetchone()
                status = r[0] if r else "unknown"
        finally:
            with contextlib.suppress(Exception), conn.cursor() as cur:
                cur.execute(
                    "UPDATE estimation_runs SET job_payload = NULL WHERE id = %s",
                    (run_id,),
                )
        LOG.info("ESTIMATION lane ran run_id=%d status=%s", run_id, status)
        return {"claimed": 1, "run_id": run_id, "status": status}
    finally:
        with contextlib.suppress(Exception):
            conn.close()


async def _estimation_pass(stop_event: asyncio.Event, state: dict[str, Any]) -> None:
    if stop_event.is_set():
        return
    swept = 0
    try:
        swept = await asyncio.to_thread(_sweep_estimations_sync)
    except Exception:  # noqa: BLE001 - a sweep failure must not skip execution
        LOG.exception("ESTIMATION lane: stuck-run sweep failed")
    if swept:
        LOG.info("ESTIMATION lane swept %d stuck run(s)", swept)
    result = await asyncio.to_thread(_estimation_sync)
    last: dict[str, Any] = {"claimed": result.get("claimed", 0), "swept": swept}
    if "status" in result:
        last["status"] = result["status"]
    _record_pass(state, "estimation", last)


def _tune_resolver_log_level(*, idle: bool) -> None:
    """The drain logs five INFO lines per run (DRAIN start / QUEUE depth / QUEUE
    empty / DRAIN done / DRAIN queries). That is ~7 runs a day on the GH lane and
    a run every `realtime_location_resolve_interval_seconds` here, i.e. ~29k
    lines/day burying the other seven lanes in the Railway log once the queue is
    drained. So: full INFO while there is a backlog, where those numbers are the
    diagnosis, and WARNING once a pass comes back empty. Slice and per-listing
    failures are WARNING and always pass through."""
    logging.getLogger("location_data.resolver.drain").setLevel(
        logging.WARNING if idle else logging.NOTSET)


def _location_resolve_sync() -> dict[str, Any]:
    """One bounded pass of THE resolver drain (location_data.resolver.drain.run)
    under THE shared location_jobs lease, so this lane and location_resolve.yml
    can never drain at once — plus an in-process lock, because an abandoned pass
    outlives its lease. Lazy import keeps location_data off the worker's startup
    path; module-attribute calls (drain.run / lease.held) keep both patchable in
    tests. No try/except around run(): a raise is the signal — lease.held stamps
    last_outcome='failed' and re-raises, and _lane_loop records the failed
    pass."""
    from location_data.resolver import drain, lease

    global _RESOLVE_SESSION_WARNED, _RESOLVE_LEASE_BUSY_LOGGED
    global _RESOLVE_WEDGE_LOGGED, _RESOLVE_LAST_IDLE

    session_pooler = bool(os.environ.get("SUPABASE_DB_SESSION_URL"))
    if not _RESOLVE_PASS_LOCK.acquire(blocking=False):
        # The previous pass was abandoned at LANE_PASS_TIMEOUT_SECONDS and its
        # thread is still inside drain.run. Its lease has already expired (the
        # TTL is deliberately shorter, so a DEAD worker frees the lane), so the
        # lease cannot stop us here — this lock is what does. Never touch the
        # lease on this path: acquiring it would hand a second drain the row.
        if not _RESOLVE_WEDGE_LOGGED:
            _RESOLVE_WEDGE_LOGGED = True
            LOG.warning(
                "LOCATION_RESOLVE lane skipped: the previous pass was abandoned "
                "and its thread is still draining"
            )
        return {
            "acquired": False,
            "session_pooler": session_pooler,
            "previous_pass_running": True,
        }
    _RESOLVE_WEDGE_LOGGED = False
    try:
        max_seconds = _read_location_resolve_max_seconds()
        batch_size = _read_location_resolve_batch_size()
        if not session_pooler and not _RESOLVE_SESSION_WARNED:
            _RESOLVE_SESSION_WARNED = True
            LOG.warning(
                "LOCATION_RESOLVE lane running on the TRANSACTION pooler: set "
                "SUPABASE_DB_SESSION_URL on the realtime-worker service for "
                "prepared-statement throughput"
            )
        _tune_resolver_log_level(idle=_RESOLVE_LAST_IDLE)
        # db.connect_session() IS what drain.open_connection() returns; called
        # directly because the wrapper's one extra behaviour is an unconditional
        # per-call WARNING about the transaction pooler — right for a lane that
        # runs ~7x/day, ~5.7k lines/day here. The warning above says the same
        # thing (with the service to fix it named) once per process instead.
        conn = db.connect_session()
        try:
            # The lease MUST be taken on the drain's own connection (drain.main
            # does the same); JOB_NAME/CONCURRENCY_GROUP are imported, never
            # re-spelled — a typo'd literal is a SILENT loss of mutual
            # exclusion. cadence/runner only ever land on a fresh DB
            # (_UPSERT_JOB_SQL is ON CONFLICT DO NOTHING); runtime attribution
            # comes from lease_holder, so never "fix" location_jobs.runner by
            # writing to it.
            with lease.held(
                conn,
                drain.JOB_NAME,
                cadence="15 minutes",
                concurrency_group=drain.CONCURRENCY_GROUP,
                runner="railway-realtime-worker",
                ttl_seconds=_location_resolve_lease_ttl(max_seconds, batch_size),
            ) as acquired:
                if not acquired:
                    if not _RESOLVE_LEASE_BUSY_LOGGED:
                        _RESOLVE_LEASE_BUSY_LOGGED = True
                        # Never says WHY: _ACQUIRE_SQL carries `AND enabled`, so
                        # a lease held by location_resolve.yml and an operator's
                        # `location_jobs.enabled = false` are the same answer
                        # here, and guessing between them misdirects whoever is
                        # asking why the queue is not draining.
                        LOG.info(
                            "LOCATION_RESOLVE lane skipped: the %s lease was not "
                            "acquired", drain.JOB_NAME,
                        )
                    return {"acquired": False, "session_pooler": session_pooler}
                _RESOLVE_LEASE_BUSY_LOGGED = False
                stats = drain.run(
                    conn, batch_size=batch_size, max_seconds=max_seconds,
                    workers=_read_location_resolve_workers())
            _RESOLVE_LAST_IDLE = stats.claimed == 0
            return {
                "acquired": True,
                "session_pooler": session_pooler,
                "claimed": stats.claimed,
                "resolved": stats.resolved,
                "failed": stats.failed,
                "batches": stats.batches,
                "fallbacks": stats.fallbacks,
                # What the pass actually ran with, never what it was configured
                # with: `rate` divided by `workers` is the per-loop number the
                # next tuning decision needs. `failed_batches` counts BATCHES
                # that raised and were retried (W2-a6) — never worker threads
                # ending their loop, and deliberately NOT spelled
                # `failed_passes`: that name already means something else one
                # level up (the lane's own count of passes that raised), and
                # reading four of them per healthy pass is what made this lane
                # look broken when it was working.
                "workers": stats.workers,
                "failed_batches": stats.failed_batches,
                "seconds": round(stats.seconds, 1),
                "rate": round(stats.rate, 2),
            }
        finally:
            with contextlib.suppress(Exception):
                conn.close()
    finally:
        _RESOLVE_PASS_LOCK.release()


async def _location_resolve_pass(
    stop_event: asyncio.Event, state: dict[str, Any],
) -> None:
    if stop_event.is_set():
        return
    last = await asyncio.to_thread(_location_resolve_sync)
    # Only when the pass did work: at a 15 s cadence an unconditional summary is
    # 5.7k lines/day of "claimed=0" once the queue is drained. The heartbeat
    # records every pass either way.
    if last.get("claimed"):
        LOG.info(
            "LOCATION_RESOLVE lane claimed=%d resolved=%d failed=%d %.1fs rate=%.2f/s "
            "workers=%d",
            last["claimed"], last["resolved"], last["failed"],
            last["seconds"], last["rate"], last["workers"],
        )
    _record_pass(state, "location_resolve", last)


def _lane_snapshot(lanes: dict[str, Any]) -> dict[str, Any]:
    """The lane state as written to the heartbeat, with elapsed time resolved.

    `in_flight_s` is computed HERE rather than left to the reader: it is the one
    number that separates "this lane is working" from "this lane is wedged", and
    a value only derivable by arithmetic over an ISO string is a value nothing
    will alarm on.
    """
    now = datetime.now(timezone.utc)
    out: dict[str, Any] = {}
    for lane, info in lanes.items():
        entry = dict(info)
        started = entry.get("started_at")
        elapsed: float | None = None
        if started:
            with contextlib.suppress(ValueError):
                elapsed = round((now - datetime.fromisoformat(started)).total_seconds(), 1)
        entry["in_flight_s"] = elapsed
        out[lane] = entry
    return out


def _beat_sync(state: dict[str, Any]) -> None:
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(_HEARTBEAT_SQL, {
                "worker": WORKER_NAME,
                "started_at": state["started_at"],
                "details": Jsonb(_lane_snapshot(state["lanes"])),
            })
    finally:
        with contextlib.suppress(Exception):
            conn.close()


async def _heartbeat_pass(state: dict[str, Any]) -> None:
    await asyncio.to_thread(_beat_sync, state)


# --- the supervisor -----------------------------------------------------------


async def _lane_loop(
    name: str,
    stop_event: asyncio.Event,
    read_interval: Callable[[], float],
    run_pass: Callable[[], Awaitable[None]],
    state: dict[str, Any],
    *,
    default_interval: float,
    idle_seconds: float = IDLE_WAIT_SECONDS,
) -> None:
    """One forever-lane: re-read the interval each pass (live app_settings
    edits apply on the next wake), interval<=0 = idle-not-dead, per-pass
    try/except, clean stop_event exit. Mirrors notifications.matcher_loop."""
    LOG.info("%s lane starting", name)
    while not stop_event.is_set():
        interval = default_interval
        try:
            interval = float(await asyncio.to_thread(read_interval))
        except Exception as exc:  # noqa: BLE001
            LOG.warning("%s lane: failed to read interval: %s", name, exc)

        if interval <= 0:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=idle_seconds)
            except asyncio.TimeoutError:
                continue
            else:
                break

        _record_pass_start(state, name)
        started = time.monotonic()
        try:
            # CONTAINMENT, not a diagnosis. run_pass() was awaited unbounded, so a
            # single wedged call froze its lane until the next redeploy -- the
            # drain lane managed ONE pass in nine hours while images managed 486.
            # The timeout does not say WHY a pass hangs; it stops one hang from
            # costing every subsequent pass. The instrumentation above is what
            # will say why.
            #
            # Honest limit: a pass that is blocked inside asyncio.to_thread keeps
            # running after the cancellation -- Python cannot kill a thread. This
            # frees the LANE, and a leak shows up as repeated timeouts on the same
            # lane, which is itself the diagnosis we currently lack.
            await asyncio.wait_for(run_pass(), timeout=LANE_PASS_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            LOG.error(
                "%s lane pass exceeded %ss and was abandoned; the lane continues "
                "(a thread blocked inside it may still be running)",
                name, LANE_PASS_TIMEOUT_SECONDS,
            )
            _record_pass_failed(state, name, round(time.monotonic() - started, 1))
        except Exception:  # noqa: BLE001 - a pass failure never kills the lane
            LOG.exception("%s lane pass failed", name)
            # A pass that raised never reaches _record_pass, so clear the
            # in-flight stamp here or a failing lane reads as a hung one forever.
            _record_pass_failed(state, name, round(time.monotonic() - started, 1))

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
        else:
            break
    LOG.info("%s lane stopped", name)


async def _supervised(
    name: str,
    lane: Callable[[], Awaitable[None]],
    stop_event: asyncio.Event,
) -> None:
    """Belt on top of the per-pass try/except: a lane-loop bug restarts the
    lane after a pause instead of leaving it silently dead until redeploy."""
    while True:
        try:
            await lane()
            return
        except Exception:  # noqa: BLE001 - a lane crash never kills the process
            LOG.exception(
                "%s lane crashed; restarting in %ss", name, LANE_RESTART_SECONDS,
            )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=LANE_RESTART_SECONDS)
            return
        except asyncio.TimeoutError:
            continue


def _new_state() -> dict[str, Any]:
    return {"started_at": datetime.now(timezone.utc), "lanes": {}}


async def _amain() -> int:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # Railway sends SIGTERM on redeploy; finish the current pass, then exit.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    state = _new_state()
    LOG.info(
        "realtime worker starting sources=%s probe_pages=%d",
        ",".join(REALTIME_SOURCES), PROBE_PAGES,
    )
    lanes: list[tuple[str, Callable[[], Awaitable[None]]]] = [
        ("probe", lambda: _lane_loop(
            "probe", stop_event, _read_probe_interval,
            lambda: _probe_pass(stop_event, state),
            state,
            default_interval=PROBE_INTERVAL_DEFAULT)),
        ("drain", lambda: _lane_loop(
            "drain", stop_event, _read_drain_interval,
            lambda: _drain_pass(stop_event, state),
            state,
            default_interval=DRAIN_INTERVAL_DEFAULT)),
        ("images", lambda: _lane_loop(
            "images", stop_event, _read_images_interval,
            lambda: _images_pass(stop_event, state),
            state,
            default_interval=IMAGES_INTERVAL_DEFAULT)),
        ("count_probe", lambda: _lane_loop(
            "count_probe", stop_event, _read_count_probe_interval,
            lambda: _count_probe_pass(stop_event, state),
            state,
            default_interval=COUNT_PROBE_INTERVAL_DEFAULT)),
        ("maintenance", lambda: _lane_loop(
            "maintenance", stop_event, _read_maintenance_interval,
            lambda: _maintenance_pass(stop_event, state),
            state,
            default_interval=MAINTENANCE_INTERVAL_DEFAULT)),
        ("estimation", lambda: _lane_loop(
            "estimation", stop_event, _read_estimation_interval,
            lambda: _estimation_pass(stop_event, state),
            state,
            default_interval=ESTIMATION_INTERVAL_DEFAULT)),
        # default_interval=0 is the FAIL-SAFE, not a default: _lane_loop keeps
        # this value when the app_settings read RAISES, and the flag lives
        # inside the read that just failed. A positive fallback would turn a
        # dark, deliberately-idled lane ON for one pass on any pooler blip —
        # e.g. mid-migration or mid-registry-load, which is exactly when it must
        # not drain. (It named `epoch_job` until W2-a deleted the epoch engine.)
        ("location_resolve", lambda: _lane_loop(
            "location_resolve", stop_event, _read_location_resolve_interval,
            lambda: _location_resolve_pass(stop_event, state),
            state,
            default_interval=0)),
        ("heartbeat", lambda: _lane_loop(
            "heartbeat", stop_event, lambda: HEARTBEAT_INTERVAL_SECONDS,
            lambda: _heartbeat_pass(state),
            state,
            default_interval=HEARTBEAT_INTERVAL_SECONDS)),
    ]
    tasks = [
        asyncio.create_task(_supervised(name, fn, stop_event), name=f"realtime-{name}")
        for name, fn in lanes
    ]
    await asyncio.gather(*tasks)
    LOG.info("realtime worker stopped")
    return 0


def _enabled() -> bool:
    return os.environ.get(ENABLE_ENV, "").strip().lower() in {"1", "true", "yes"}


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not _enabled():
        LOG.info(
            "realtime worker disabled (%s != 1); exiting — set the env var on "
            "the Railway service to enable", ENABLE_ENV,
        )
        return 0
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
