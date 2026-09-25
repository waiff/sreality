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
- location_intake_fast: every `LOCATION_INTAKE_FAST_INTERVAL_S` (default 60),
             ONE bounded pass of THE claim lane's incremental listing scan
             (location_data.claims_intake.run, the same code
             location_claims_intake.yml runs) — payload half only, no bodies
             pass, a SHORT `LOCATION_INTAKE_FAST_LAG_S` (default 120) snapshot
             lag and a `LOCATION_INTAKE_FAST_BUDGET_S` (default 45) budget,
             under its own `claims_intake.FAST_LANE` cursor. LIVE by default
             (`LOCATION_INTAKE_FAST_ENABLED=0` idles it). One lane, two
             schedules: see the constant block below.
- location_refetch: every `LOCATION_REFETCH_INTERVAL_S` (default 86400, first
             tick a few minutes after start), queue the audit page's active
             "no data" listings for ONE more detail fetch, at
             db.QUEUE_PRIORITY_VERIFY and capped at
             `LOCATION_REFETCH_CAP_PER_SOURCE` (default 500) rows per portal per
             tick. ceskereality and realitymix geocode an ad AFTER publishing it,
             so a listing fetched at discovery carried no location and nothing
             ever re-read it; the drain does the rest (a bazos category page
             raises ListingGoneError and delists instead). LIVE by default
             (`LOCATION_REFETCH_ENABLED=0` idles it).
- sold_comps: every `realtime_sold_comps_interval_seconds` (DARK: the seeded row is
             0), fetch registered sales from reas.cz for up to 5 obec cells — the
             towns where the deal pipeline holds a live card, stalest first, minus
             the cells whose newest `sold_transaction_fetches` row is younger than
             35 days (ok) or 6 hours (failed). One integer is cadence AND kill
             switch; no boolean flag, no env var, no queue. A cell attempt always
             ends in the ledger (`scraper.sold_fetch.fetch_cell` never raises), and
             an unmigrated database skips with one warning.
- autodedup: every `realtime_autodedup_interval_seconds` (DARK: the seeded row is
             0), ONE bounded pass of THE autodedup real-time shadow lane
             (autodedup.incremental_lane.run_incremental — the function
             autodedup_realtime.yml runs); the engine sizes the claim by its measured
             rate under half of a hard per-pass deadline, halved again after each
             deadline trip; a refusal or a trip counts as a failed pass. Writes only
             the `rt` shadow generation inside schema autodedup — never a production
             merge. One integer is cadence AND kill switch; the engine's own lease row
             and cursors are shared with the GH lane (and `rt_seed` takes the same
             lease), so no two of them overlap. An absent autodedup store skips with
             one warning.
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
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from scraper import (
    db, image_storage, portal_factory, portal_runner, sold_db, sold_fetch,
)
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

# location_intake_fast lane (W7-a): ONE LANE, TWO SCHEDULES.
#
# The claim lane stays one module (location_data.claims_intake). What this adds is a second
# SCHEDULE for its incremental listing scan — the same run(), the payload half only. Under
# W5 the consumers serve only RESOLVED locations, and the producer of those was an hourly
# GitHub run whose snapshot keyset stands 15 minutes behind the clock: a listing written 45 s
# after a tick waited the rest of the hour plus the lag before anything mined a claim from
# it. Measured 2026-09-13: two sreality listings first seen at 18:19:50Z had no claims and
# no verdict 27 minutes later; worst case ~75 minutes invisible in Browse.
#
# WHY THE SHORT LAG IS SAFE HERE AND NOWHERE ELSE. The lag guards a bigserial race —
# `listing_snapshots.id` is allocated at INSERT and visible at COMMIT, so a row can land
# below a keyset that has already moved past it, and a keyset never looks back. At 2 minutes
# this lane WILL occasionally skip such a row. That costs nothing because the hourly run is
# still 15 minutes back on its OWN cursor (`location_claim_batches.lane` keys the resume) and
# re-reads the same slice of the log within the hour. Mining a listing twice is free: claim
# fingerprints are ON CONFLICT DO NOTHING and the resolve enqueue is a bump (W2-a2).
#
# IT MINES BODIES TOO (W7-a2). Six of the nine portals — idnes, realitymix, bazos,
# ceskereality, remax, maxima — put a listing's location only in the stored PAGE BODY, and
# while only the hourly run mined those, their new listings stayed invisible for up to an
# hour while sreality's took 134 s. So the tick runs the JSON half FIRST (that is what a
# minute-old listing needs) and gives the bodies pass the REMAINDER of the budget, bounded
# by `LOCATION_INTAKE_FAST_BODIES_CAP` bodies. It is cheap because of the W6-b/W6-b2 cursor:
# the window starts at the id this lane's own batch row stamped, so the corpus is walked
# ONCE — the first tick after a deploy — and every tick after that returns only new bodies.
#
# ENV VARS, not app_settings rows, and LIVE by default: this is the latency fix itself, and
# the knobs are properties of the service this process runs on. `LOCATION_INTAKE_FAST_ENABLED=0`
# is the kill switch (interval 0 = idle-not-dead, the _lane_loop contract).
LOCATION_INTAKE_FAST_ENABLED_ENV = "LOCATION_INTAKE_FAST_ENABLED"
LOCATION_INTAKE_FAST_INTERVAL_ENV = "LOCATION_INTAKE_FAST_INTERVAL_S"
LOCATION_INTAKE_FAST_LAG_ENV = "LOCATION_INTAKE_FAST_LAG_S"
LOCATION_INTAKE_FAST_BUDGET_ENV = "LOCATION_INTAKE_FAST_BUDGET_S"
LOCATION_INTAKE_FAST_INTERVAL_DEFAULT = 60
LOCATION_INTAKE_FAST_LAG_DEFAULT = 120
LOCATION_INTAKE_FAST_BUDGET_DEFAULT = 45
# A HEALTHY pass must stay well under check_worker_lane_stall's 1200 s in_flight warn and
# under LANE_PASS_TIMEOUT_SECONDS, and it must finish inside its own interval or the next
# tick skips. run() checks its budget BETWEEN batches, so a pass costs the budget plus one
# batch — which is why the batch is small.
LOCATION_INTAKE_FAST_BUDGET_CEILING = 300
# Snapshot rows per batch. Nothing like the hourly lane's 10 000: a minute of fleet-wide
# change is tens to low hundreds of snapshots, and a small batch is what keeps the
# budget-plus-one-batch overrun inside the interval.
LOCATION_INTAKE_FAST_BATCH_SIZE = 2000
LOCATION_INTAKE_FAST_STATEMENT_TIMEOUT_S = 60
# Bodies MINED per tick, checked between batches (the window is never truncated — the
# bodies cursor advances to the window's max id, so a truncated window would walk past rows
# nothing comes back for). 300 is ~4x the fleet's measured page churn of 50-80 new bodies an
# HOUR, so the cap binds only while the first walk crosses a real backlog.
LOCATION_INTAKE_FAST_BODIES_CAP_ENV = "LOCATION_INTAKE_FAST_BODIES_CAP"
LOCATION_INTAKE_FAST_BODIES_CAP_DEFAULT = 300
# The R2 fetch width. The hourly runner sets 32 for its 1 500-body batches; this lane fetches
# tens of bodies beside seven other lanes on one small container. `setdefault`, so a Railway
# env var still wins.
LOCATION_INTAKE_FAST_FETCH_WORKERS = "8"
# TWO extraction processes, and ONE pool for the life of the lane. The parse is pure CPU and
# the container is small, so two is what can overlap without starving the other lanes; the
# pool is reused across ticks because a forkserver import per tick would cost more than the
# handful of bodies a tick parses. `page_readers.ExtractionPool` rebuilds it by itself when
# the contract data moves or a worker dies — and its per-body `EXTRACTION_TIMEOUT_S` is what
# keeps a pathological page from ever holding a tick (the worker is killed, the body comes
# back as its own outcome, the rest of the batch finishes on the main thread).
LOCATION_INTAKE_FAST_EXTRACTION_WORKERS = 2
_INTAKE_FAST_POOL: Any = None
# log-once-per-process guard: R2 unconfigured on this service.
_INTAKE_FAST_R2_WARNED = False
# The lane's own mutual exclusion INSIDE this process, for the same reason the resolve lane
# has one: a pass abandoned at LANE_PASS_TIMEOUT_SECONDS keeps running (Python cannot kill
# the thread), and two scans sharing one cursor would each re-read the other's window.
_INTAKE_FAST_PASS_LOCK = threading.Lock()
_INTAKE_FAST_WEDGE_LOGGED = False
# Whether the last pass opened no listing — drives the intake's log level (see
# _tune_intake_log_level). Starts True so a quiet lane is quiet from the first pass.
_INTAKE_FAST_LAST_IDLE = True
# The contract projection is a startup step, not a per-pass one (see _project_contracts_once).
_INTAKE_FAST_CONTRACTS_PROJECTED = False

# location_refetch lane (W8): A PAGE THAT CARRIED NO LOCATION GETS A SECOND LOOK.
#
# Two portals fill an ad's location in AFTER publishing it. Our one detail fetch ran at
# discovery — measured 2026-09-14, 56 of 63 ceskereality and 56 of 112 realitymix rows in
# the audit page's active "no data" bucket were fetched within two minutes of first
# sighting and never again — and the index card never changed afterwards, so no re-fetch
# was ever enqueued. The listing then has no claims, so it has no location, for ever.
#
# This lane is the second look, and NOTHING ELSE: it enqueues, and the machinery that
# already exists does the rest — the drain re-fetches and rewrites the page, the
# location_intake_fast lane mines the new body within a minute, the resolver answers. A
# bazos dead ad answers with the category page, which raises ListingGoneError, so the same
# fetch also delists it sooner.
#
# DAILY, AND BOUNDED PER PORTAL. The set is a few hundred rows and a portal's geocoding lag
# is minutes-to-hours, so once a day is plenty and the cap (500/portal/tick, oldest-fetched
# first) is what keeps a portal with thousands of dead ads polite: it drains over days
# instead of flooding one drain pass. Env vars, not app_settings: like the intake lane,
# these are properties of the service this process runs on, and the reader touching no
# database is what lets the lane ship LIVE (see _read_location_refetch_interval).
LOCATION_REFETCH_ENABLED_ENV = "LOCATION_REFETCH_ENABLED"
LOCATION_REFETCH_INTERVAL_ENV = "LOCATION_REFETCH_INTERVAL_S"
LOCATION_REFETCH_MIN_AGE_ENV = "LOCATION_REFETCH_MIN_AGE_S"
LOCATION_REFETCH_CAP_ENV = "LOCATION_REFETCH_CAP_PER_SOURCE"
LOCATION_REFETCH_INTERVAL_DEFAULT = 86400
# Older than this and the page is worth re-reading. Six hours, not a day: it is well past
# the portals' geocoding lag, and it means a listing discovered today gets its second look
# on TOMORROW's tick rather than waiting for the day after.
LOCATION_REFETCH_MIN_AGE_DEFAULT = 21600
LOCATION_REFETCH_CAP_DEFAULT = 500
# The first tick waits, for two reasons: a redeploy restarts every lane at once (the drain's
# first pass, the contract projection, the intake's first walk) and a daily lane has no
# reason to compete for that minute; and a deploy loop must not fire a queue-writing pass
# every time the container restarts.
LOCATION_REFETCH_FIRST_DELAY_SECONDS = 300.0
# log-once-per-process guard: the audit matview is absent (a branch database).
_LOCATION_REFETCH_VIEW_WARNED = False

# sold_comps lane (sold-comps W2): REGISTERED SALES FOR THE TOWNS THE OPERATOR IS
# ACTUALLY WORKING IN. A sale is an external fact with no account and no listing, so
# the lane has no queue: its work-list is a query (`sold_db.sold_comp_cells`) over the
# obec cells where the deal pipeline holds a live card, minus the cells whose newest
# ledger row is still fresh. The source republishes a transfer ~30 days after the
# sale, so a cell that succeeded is not worth re-asking for weeks — which makes five
# cells a pass generous rather than tight.
SOLD_COMPS_INTERVAL_SETTING = "realtime_sold_comps_interval_seconds"
SOLD_COMPS_CELL_CAP = 5
# The cheapest cell is still a ~1.9 MB SSR page against a politeness budget of one
# request per five seconds, so this lane does not race the container's first minute.
SOLD_COMPS_FIRST_DELAY_SECONDS = 300.0
# log-once-per-process guard: migration 542's store is absent (a branch database, or
# main before the apply).
_SOLD_COMPS_STORE_WARNED = False
# The lane's own mutual exclusion, INSIDE this process (the location_resolve lane's
# reason, and this lane has no lease to fall back on). A pass abandoned at
# LANE_PASS_TIMEOUT_SECONDS keeps running — Python cannot kill the thread — and it
# has written no ledger row yet, so the freshness subtraction cannot stop the next
# tick handing the SAME cells to a second walk. Under the rate limiter's 8x penalty
# factor a five-cell pass can exceed the timeout, which is exactly when a second
# walk is least welcome. Held for the whole pass.
_SOLD_COMPS_PASS_LOCK = threading.Lock()
_SOLD_COMPS_WEDGE_LOGGED = False

# text_extract lane (field-capture W7): the post-publication extraction of the facts a
# prose-only advert states in its text and nowhere else. A CONSTANT interval, no
# app_settings row, no env var and no flag — the estimation lane is the cautionary case
# (its flag was never set, so it has been dark since it shipped and is absent from
# worker_heartbeats entirely, which makes it invisible to every monitor). This lane's whole
# scope is `attribute_contract.extracted_cells()` — the cells whose R7 gate the bake-off
# has OPENED. None open (the shipping state) and the pass returns before it opens a cursor,
# so the lane is live, visible and free. Five minutes puts a new listing well inside the
# lane's 20-minute SLO even after a missed pass.
TEXT_EXTRACT_INTERVAL_SECONDS = 300.0
# The sold_comps lane's reason, with money on it: a pass abandoned at
# LANE_PASS_TIMEOUT_SECONDS keeps its eight threads — and their billing — running, and the
# cache rows they have not written yet cannot stop the next tick handing the same listings
# to a second set of paid calls.
_TEXT_EXTRACT_PASS_LOCK = threading.Lock()
_TEXT_EXTRACT_WEDGE_LOGGED = False

# autodedup lane (AUTODEDUP rollout §7.3): THE real-time shadow pass of the dedup engine
# (autodedup.incremental_lane.run_incremental — the function autodedup_realtime.yml runs via
# `python -m autodedup.lane --mode incremental`, imported and never copied) from here, because
# GitHub fires that `*/10` schedule hours apart and decisions within minutes cannot ride it.
#
# SHADOW ONLY. The pass writes the `rt` generation inside schema `autodedup` (fingerprints,
# probe postings, pairs, groups, cursors) and nothing else: no `public.listings` row, no
# `property_id`, no merge. Turning engine groups into production merges is a separate adapter
# over toolkit/property_identity.merge_properties, and this lane neither calls nor imports it.
#
# ONE LANE, TWO SCHEDULES, ONE POSITION. The engine keeps its watermark in
# `autodedup.scan_cursor` and takes the `autodedup.rt_lease` row by CAS, both keyed by name
# and not by caller, so this lane and the workflow read and advance the SAME cursors under the
# SAME lease: whichever takes the lease passes, the other returns `skipped: leased`. That is
# the location_resolve precedent (a shared lease, the GH lane stays the backstop) rather than
# the intake's (two cursors): a listing decided twice into one generation is not free, and the
# engine's cursors are not per-caller. The engine's own gates bind this caller exactly as they
# bind the workflow — the `autodedup.settings.realtime_enabled` stop button, the unseeded skip,
# the storage budget, the parity gate, the pair budget.
#
# ONE integer is cadence AND kill switch (the sold_comps lane's shape): the seeded row is 0, and
# the lane is registered with default_interval=0 so a settings read that RAISES cannot wake it.
AUTODEDUP_INTERVAL_SETTING = "realtime_autodedup_interval_seconds"
# The HARD per-pass deadline, in seconds of wall clock. The engine's time budget bounds what a
# pass CLAIMS, not how long it runs, and a pass abandoned at LANE_PASS_TIMEOUT_SECONDS keeps its
# thread — and its open transaction and its lease — running. So after this deadline the pass's
# connection refuses every further statement except the lease release: the transaction rolls
# back (nothing written, no cursor moved — the engine's own refusal contract), the lease is
# freed and the thread ends. Sized so that the deadline plus one statement at the engine's
# `statement_timeout` (120 s) stays under check_worker_lane_stall's 1200 s in_flight warn,
# under LANE_PASS_TIMEOUT_SECONDS, and under the engine's LEASE_TTL_S (2100 s) — so the GH lane
# cannot take the lease while a pass here is still writing.
AUTODEDUP_PASS_DEADLINE_SECONDS = 1050.0
# The time budget the engine sizes this lane's claims by (E98: claim <= budget x the rate the
# generation measured itself at), handed in as `max_pass_budget_s`. The engine's own default
# (`PASS_BUDGET_S`, 900 s) is sized for the workflow's 25-minute timeout and would fill all but
# ~15% of this deadline. A pass stopped at the deadline rolls back without recording a rate, so
# the next one would claim the same work at the same size and trip again, holding the shared
# lease the whole time. Half the deadline leaves the pass room to run twice as slow as its
# measured rate before the deadline stops it.
AUTODEDUP_PASS_BUDGET_SECONDS = AUTODEDUP_PASS_DEADLINE_SECONDS / 2
# Longest refusal/abort text carried into the heartbeat row.
AUTODEDUP_REASON_CHARS = 300
_AUTODEDUP_PASS_LOCK = threading.Lock()
_AUTODEDUP_WEDGE_LOGGED = False
# log-once-per-process guard: the autodedup store is absent.
_AUTODEDUP_STORE_WARNED = False
# Logged on the TRANSITION, never per pass: at a 60 s interval a lease held by the GH lane or a
# standing refusal would otherwise write the same line 1,440 times a day.
_AUTODEDUP_LAST_OUTCOME: str | None = None
# The in-process back-off after a deadline trip: the time budget the engine sizes its claim by is
# divided by this, doubling per trip, down to a one-second budget (a one-listing claim at any rate
# below two listings a second). It returns to 1 only after a clean pass that claimed enough for
# the engine to re-measure its rate (`PASS_RATE_MIN_CLAIM`): a smaller pass teaches the engine
# nothing, and going back to a full claim on the same stale rate is exactly the trip that set
# it. A restart resets it.
_AUTODEDUP_BACKOFF = 1

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


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    """A clamped integer env knob. A typo is a typo, never a stopped lane: an unparseable
    value logs and falls back to the default."""
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        LOG.warning("%s=%r is not an integer; using %d", name, raw, default)
        value = default
    return max(minimum, min(value, maximum))


def _env_flag(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes"}


def _read_location_intake_fast_interval() -> int:
    # The flag gates the lane via interval<=0 (idle-not-dead, the _lane_loop contract).
    # Ships LIVE — the flag defaults ON, because the lane IS the latency fix.
    if not _env_flag(LOCATION_INTAKE_FAST_ENABLED_ENV, default=True):
        return 0
    return _env_int(LOCATION_INTAKE_FAST_INTERVAL_ENV,
                    LOCATION_INTAKE_FAST_INTERVAL_DEFAULT, minimum=5, maximum=3600)


def _read_location_intake_fast_lag_seconds() -> int:
    # Floored at 0, not at 60: the rail this trades away is re-taken by the hourly lane's
    # own 15-minute cursor. Ceiled at an hour so a mis-set value idles the lane's PURPOSE
    # rather than the lane.
    return _env_int(LOCATION_INTAKE_FAST_LAG_ENV, LOCATION_INTAKE_FAST_LAG_DEFAULT,
                    minimum=0, maximum=3600)


def _read_location_intake_fast_budget() -> int:
    return _env_int(LOCATION_INTAKE_FAST_BUDGET_ENV, LOCATION_INTAKE_FAST_BUDGET_DEFAULT,
                    minimum=1, maximum=LOCATION_INTAKE_FAST_BUDGET_CEILING)


def _read_location_intake_fast_bodies_cap() -> int:
    return _env_int(LOCATION_INTAKE_FAST_BODIES_CAP_ENV,
                    LOCATION_INTAKE_FAST_BODIES_CAP_DEFAULT, minimum=1, maximum=5000)


def _read_location_refetch_interval() -> int:
    # The flag gates the lane via interval<=0 (idle-not-dead, the _lane_loop contract).
    # Ships LIVE — the lane is the fix itself, and an env var the operator must remember to
    # set on the Railway service is a fix that never runs.
    if not _env_flag(LOCATION_REFETCH_ENABLED_ENV, default=True):
        return 0
    # Floor of an hour: this lane writes to the shared queue, and nothing about it gets
    # better by running more often than the portals geocode.
    return _env_int(LOCATION_REFETCH_INTERVAL_ENV, LOCATION_REFETCH_INTERVAL_DEFAULT,
                    minimum=3600, maximum=7 * 86400)


def _read_location_refetch_min_age() -> int:
    return _env_int(LOCATION_REFETCH_MIN_AGE_ENV, LOCATION_REFETCH_MIN_AGE_DEFAULT,
                    minimum=600, maximum=30 * 86400)


def _read_location_refetch_cap() -> int:
    return _env_int(LOCATION_REFETCH_CAP_ENV, LOCATION_REFETCH_CAP_DEFAULT,
                    minimum=1, maximum=10000)


def _read_sold_comps_interval() -> int:
    # ONE integer is the cadence AND the kill switch (interval<=0 = idle-not-dead), so
    # this lane carries no `*_enabled` flag. Default 0 twice over: the seeded row ships
    # at 0, and an absent row reads as 0 — the lane is dark until the operator sets it
    # on /settings.
    return _read_int(SOLD_COMPS_INTERVAL_SETTING, 0)


def _read_autodedup_interval() -> int:
    # The sold_comps reader's contract: an absent row reads as 0, the lane dark.
    return _read_int(AUTODEDUP_INTERVAL_SETTING, 0)


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
        # Stored without an inline pHash (left for the hourly backstop); 0 in steady state.
        "phash_missed": agg.get("images_phash_missed", 0),
        "stopped_suspicious": bool(agg.get("stopped_suspicious", False)),
        "cap": slice_,
    }
    LOG.info(
        "IMAGES lane downloaded=%d phash_missed=%d cap=%d stopped_suspicious=%s",
        totals["downloaded"], totals["phash_missed"], slice_,
        totals["stopped_suspicious"],
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


def _tune_intake_log_level(*, idle: bool) -> None:
    """The intake logs a start + summary line per run and one per batch. At an hourly
    cadence that is evidence; at 60 s it is ~3k lines/day of `listings=0` burying the other
    eight lanes in the Railway log. So: full INFO while the lane is finding change, WARNING
    once a pass comes back empty. Every pass is recorded in the heartbeat either way."""
    logging.getLogger("location_data.claims_intake").setLevel(
        logging.WARNING if idle else logging.NOTSET)


def _project_contracts_once() -> bool:
    """Project contracts/portals/*.yaml into portal_contracts, ONCE per process.

    The extractor reads the DB PROJECTION of the contracts, not the files, and the hourly
    workflow re-projects from git before every run (02 §2.1.8: git is the store of record).
    This image carries contracts/ for the same reason payload_norm does, so the worker can
    do the equivalent — idempotent per (portal, contract_version), so on the steady state it
    is a handful of no-op reads at startup.

    A FAILURE IS A WARNING, NEVER A DEAD LANE. The projection raises by design when a
    contract's governed bytes changed without a version bump, and the authoritative
    projector is still the workflow: a worker that refused to start its lane over a
    condition another lane will report would trade a loud failure for a silent one.
    """
    global _INTAKE_FAST_CONTRACTS_PROJECTED
    if _INTAKE_FAST_CONTRACTS_PROJECTED:
        return True
    from location_data import contracts

    try:
        # Parsed BEFORE the connection is opened: a contract file that does not parse is a
        # deploy problem, and diagnosing it should not depend on the pooler answering.
        parsed = contracts.load_all()
        git_ref = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "railway-worker")
        with db.connect() as conn:
            for contract in parsed:
                contracts.project(conn, contract, git_ref=git_ref)
    except Exception as exc:  # noqa: BLE001 - the workflow is the authoritative projector
        LOG.warning(
            "LOCATION_INTAKE_FAST could not project the portal contracts (%s); the lane "
            "runs against whatever projection location_claims_intake.yml last loaded", exc)
        return False
    _INTAKE_FAST_CONTRACTS_PROJECTED = True
    LOG.info("LOCATION_INTAKE_FAST projected the portal contracts from the image")
    return True


def _intake_fast_pool() -> Any:
    """The lane's ONE extraction pool, built on first use and reused for the life of the
    process. `ExtractionPool` owns the rebuild rules (contract data moved, or the pool
    died); this only owns the lifetime, so a redeploy is the only thing that resets it."""
    global _INTAKE_FAST_POOL
    if _INTAKE_FAST_POOL is None:
        from location_data import page_readers

        _INTAKE_FAST_POOL = page_readers.ExtractionPool(
            LOCATION_INTAKE_FAST_EXTRACTION_WORKERS)
    return _INTAKE_FAST_POOL


def _intake_fast_r2_ready() -> bool:
    """Does this service have R2 credentials? The bodies half needs the bucket; the JSON
    half never does. A rotated or missing credential must cost the page portals their
    minute-fresh claims and NOTHING else — `claims_intake` already warns once per run and
    carries the payload half through, so this is only about saying it once per PROCESS
    rather than once a minute, for ever."""
    global _INTAKE_FAST_R2_WARNED
    ready = all(os.environ.get(name) for name in
                ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
                 "R2_BUCKET_NAME"))
    if not ready and not _INTAKE_FAST_R2_WARNED:
        _INTAKE_FAST_R2_WARNED = True
        LOG.warning(
            "LOCATION_INTAKE_FAST: R2_* is not configured on this service, so the lane "
            "mines JSON payloads only — the six page portals (idnes, realitymix, bazos, "
            "ceskereality, remax, maxima) keep waiting for the hourly run")
    return ready


def _location_intake_fast_sync() -> dict[str, Any]:
    """One bounded pass of THE claim lane's incremental listing scan, payload half only.

    No lease: the cursor IS the coordination. `location_claim_batches` resumes on (lane,
    source, scan_mode) and this lane stamps its own lane string, so it cannot read or move
    the hourly run's position — which is also why the two schedules need no cross-process
    group. The in-process lock is what stops THIS lane overlapping itself.

    Lazy import keeps location_data (selectolax, boto3) off the worker's startup path;
    module-attribute calls keep claims_intake.run patchable in tests. No try/except around
    run(): a raise is the signal, and _lane_loop records the failed pass."""
    from location_data import claims_intake

    global _INTAKE_FAST_WEDGE_LOGGED, _INTAKE_FAST_LAST_IDLE

    if not _INTAKE_FAST_PASS_LOCK.acquire(blocking=False):
        if not _INTAKE_FAST_WEDGE_LOGGED:
            _INTAKE_FAST_WEDGE_LOGGED = True
            LOG.warning(
                "LOCATION_INTAKE_FAST lane skipped: the previous pass was abandoned and "
                "its thread is still scanning")
        return {"ran": False, "previous_pass_running": True}
    _INTAKE_FAST_WEDGE_LOGGED = False
    try:
        conn = db.connect_session()
        try:
            return _intake_fast_pass(conn)
        finally:
            with contextlib.suppress(Exception):
                conn.close()
    finally:
        _INTAKE_FAST_PASS_LOCK.release()


def _intake_fast_pass(conn: Any) -> dict[str, Any]:
    """The tick itself, on an open connection — the lock and the connection are the
    caller's. Split out so the yield below is the FIRST thing the tick does."""
    from location_data import claims_intake

    global _INTAKE_FAST_LAST_IDLE

    # THE GITHUB RUN OWNS THE LANE WHILE IT IS WALKING (W12). Both schedules write claims and
    # bump the same `dirty_locations` rows, and the hourly/full run is the one that cannot be
    # re-dispatched cheaply — a `mode=full` walk is hours of work. So the minute lane stands
    # aside while one is in flight: nothing is lost (the hourly run re-reads its own
    # 15-minute-lag slice), and the cost is that a new listing waits for that run instead of a
    # minute. One read, no lock, no write.
    yielded_to = claims_intake.running_batch_id(conn)
    if yielded_to is not None:
        LOG.info("LOCATION_INTAKE_FAST yielding: batch %d of the %s lane is still running",
                 yielded_to, claims_intake.LANE)
        return {"ran": False, "yielded_to": yielded_to}

    _project_contracts_once()
    # The lane's own fetch width, not the hourly runner's 32. `setdefault`, so the
    # Railway service can still override it without a deploy of this file.
    os.environ.setdefault("LOCATION_BODY_FETCH_WORKERS",
                          LOCATION_INTAKE_FAST_FETCH_WORKERS)
    # Says it once per PROCESS rather than once a run. Nothing branches on it: without
    # a store `drain_unmined_bodies` returns before its first query, so a missing
    # credential already costs the page half and nothing else.
    _intake_fast_r2_ready()
    budget = _read_location_intake_fast_budget()
    lag_seconds = _read_location_intake_fast_lag_seconds()
    _tune_intake_log_level(idle=_INTAKE_FAST_LAST_IDLE)
    schedule = claims_intake.Schedule(
        lane=claims_intake.FAST_LANE,
        lag_minutes=lag_seconds / 60.0,
        # THE JSON HALF FIRST. It is the half a minute-old listing needs; the bodies
        # pass takes what is left of the same 45 s.
        bodies_first=False,
        bodies_cap=_read_location_intake_fast_bodies_cap(),
        bodies_budget_share=1.0,
        # The run-end backlog `count(*)` is the hourly chain's signal. At a 60 s cadence
        # it would cost more than the drain it measures.
        backlog_readout=False,
        pool=_intake_fast_pool(),
    )
    stats = claims_intake.run(
        conn,
        mode="incremental",
        source=None,
        batch_size=LOCATION_INTAKE_FAST_BATCH_SIZE,
        max_seconds=float(budget),
        limit=None,
        start_after_id=0,
        statement_timeout=LOCATION_INTAKE_FAST_STATEMENT_TIMEOUT_S,
        dry_run=False,
        note="realtime-worker fast schedule (W7-a)",
        schedule=schedule,
    )
    _INTAKE_FAST_LAST_IDLE = not (stats["listings"] or stats["bodies_mined"])
    return {
        "ran": True,
        "listings": stats["listings"],
        "claims_inserted": stats["claims_inserted"],
        "enqueued": stats["enqueued"],
        "bodies_mined": stats["bodies_mined"],
        "bodies_complete": bool(stats["bodies_pass_complete"]),
        "seconds": round(float(stats["payload_seconds"] + stats["bodies_seconds"]), 1),
        "cursor": stats["cursor_after_id"],
        "bodies_cursor": stats["bodies_cursor_after_id"],
    }


async def _location_intake_fast_pass(
    stop_event: asyncio.Event, state: dict[str, Any],
) -> None:
    if stop_event.is_set():
        return
    last = await asyncio.to_thread(_location_intake_fast_sync)
    # Only when the pass did work — see _tune_intake_log_level for the same argument.
    if last.get("listings") or last.get("bodies_mined"):
        LOG.info(
            "LOCATION_INTAKE_FAST lane listings=%d claims=%d enqueued=%d bodies=%d "
            "complete=%s %.1fs cursor=%s bodies_cursor=%s",
            last["listings"], last["claims_inserted"], last["enqueued"],
            last["bodies_mined"], last["bodies_complete"], last["seconds"],
            last["cursor"], last["bodies_cursor"],
        )
    _record_pass(state, "location_intake_fast", last)


def _location_refetch_sync() -> dict[str, Any]:
    """One daily tick: read the audit set's active no-data rows whose page is old
    enough, and queue them for one more detail fetch at VERIFY priority.

    No lease and no in-process lock: the pass is one SELECT plus one INSERT per portal,
    the enqueue is idempotent on (source, native_id), and a second caller would at worst
    re-write rows it already wrote."""
    global _LOCATION_REFETCH_VIEW_WARNED

    started = time.monotonic()
    min_age = _read_location_refetch_min_age()
    cap = _read_location_refetch_cap()
    conn = db.connect()
    try:
        rows = db.location_refetch_candidates(
            conn, min_age_seconds=min_age, cap_per_source=cap)
        if rows is None:
            if not _LOCATION_REFETCH_VIEW_WARNED:
                _LOCATION_REFETCH_VIEW_WARNED = True
                LOG.warning(
                    "LOCATION_REFETCH: location_pin_audit_mv is absent on this database; "
                    "the lane has nothing to read and skips every tick")
            return {"ran": False, "reason": "audit_view_missing",
                    "seconds": round(time.monotonic() - started, 1)}
        by_source: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_source.setdefault(row["source"], []).append(row)
        sources: dict[str, Any] = {}
        queued_total = 0
        for source, items in sorted(by_source.items()):
            queued = db.enqueue_location_refetch(
                conn, source,
                [(it["native_id"], it["detail_ref"], it["price_czk"]) for it in items],
            )
            queued_total += queued
            sources[source] = {
                "candidates": len(items),
                "queued": queued,
                # The UNCAPPED backlog: how many this portal still owes after the cap.
                "backlog": items[0]["source_total"],
            }
        return {
            "ran": True,
            "candidates": len(rows),
            "queued": queued_total,
            "sources": sources,
            "min_age_s": min_age,
            "cap": cap,
            "seconds": round(time.monotonic() - started, 1),
        }
    finally:
        with contextlib.suppress(Exception):
            conn.close()


async def _location_refetch_pass(
    stop_event: asyncio.Event, state: dict[str, Any],
) -> None:
    if stop_event.is_set():
        return
    last = await asyncio.to_thread(_location_refetch_sync)
    # Once a day, so it is always logged: a lane whose whole output is one line a day
    # should leave that line even when it found nothing.
    LOG.info(
        "LOCATION_REFETCH lane candidates=%s queued=%s sources=%s %ss",
        last.get("candidates", 0), last.get("queued", 0),
        {k: v["queued"] for k, v in last.get("sources", {}).items()},
        last.get("seconds"),
    )
    _record_pass(state, "location_refetch", last)


def _sold_comps_sync() -> dict[str, Any]:
    """One tick: the stalest pipeline obec cells, fetched from the source and stored.

    No lease (the location_refetch lane's reason: one SELECT and a handful of
    idempotent cell writes), but one in-process lock — an abandoned pass has written
    no ledger row, so nothing else can stop the next tick re-walking its cells beside
    it. `fetch_cell` never raises, so one bad cell costs its own ledger row.
    """
    global _SOLD_COMPS_STORE_WARNED, _SOLD_COMPS_WEDGE_LOGGED

    started = time.monotonic()
    if not _SOLD_COMPS_PASS_LOCK.acquire(blocking=False):
        if not _SOLD_COMPS_WEDGE_LOGGED:
            _SOLD_COMPS_WEDGE_LOGGED = True
            LOG.warning(
                "SOLD_COMPS lane skipped: the previous pass was abandoned and its "
                "thread is still walking cells")
        return {"ran": False, "reason": "previous_pass_running",
                "seconds": round(time.monotonic() - started, 1)}
    try:
        conn = db.connect()
        try:
            cells = sold_db.sold_comp_cells(
                conn, sold_fetch.SOURCE, cap=SOLD_COMPS_CELL_CAP)
            if cells is None:
                if not _SOLD_COMPS_STORE_WARNED:
                    _SOLD_COMPS_STORE_WARNED = True
                    LOG.warning(
                        "SOLD_COMPS: sold_transactions is absent on this database; "
                        "the lane has nothing to write and skips every tick")
                return {"ran": False, "reason": "store_missing",
                        "seconds": round(time.monotonic() - started, 1)}
            results: list[dict[str, Any]] = []
            if cells:
                client = sold_fetch.build_client()
                results = [sold_fetch.fetch_cell(conn, client, kod) for kod in cells]
            return {
                "ran": True,
                "cells": len(results),
                "records": sum(r["records"] for r in results),
                "new": sum(r["new"] for r in results),
                "failed": sum(1 for r in results if r["status"] == "failed"),
                # A cell that could not be boxed writes no ledger row, so if one ever
                # reaches the lane again the heartbeat — not the ledger — is the only
                # place it can show. Counted so it can never read as a quiet pass.
                "skipped": sum(1 for r in results if r["status"] == "skipped"),
                "seconds": round(time.monotonic() - started, 1),
            }
        finally:
            with contextlib.suppress(Exception):
                conn.close()
    finally:
        _SOLD_COMPS_PASS_LOCK.release()


async def _sold_comps_pass(
    stop_event: asyncio.Event, state: dict[str, Any],
) -> None:
    if stop_event.is_set():
        return
    last = await asyncio.to_thread(_sold_comps_sync)
    LOG.info(
        "SOLD_COMPS lane cells=%s records=%s new=%s failed=%s skipped=%s %ss",
        last.get("cells", 0), last.get("records", 0), last.get("new", 0),
        last.get("failed", 0), last.get("skipped", 0), last.get("seconds"),
    )
    _record_pass(state, "sold_comps", last)


def _text_extract_sync() -> dict[str, Any]:
    """One bounded pass of THE post-publication text lane
    (`toolkit.description_extraction.run_pass`).

    Lazy import: the module reaches api.llm_client and the provider registry, which no
    other lane on this worker needs on the startup path. No try/except around run_pass —
    a raise is the signal, and _lane_loop records the failed pass.

    While every gate in `attribute_contract` is closed the pass returns before it opens a
    cursor, so this lane is live, visible in the heartbeat and free from the day it ships.
    """
    global _TEXT_EXTRACT_WEDGE_LOGGED

    from toolkit import description_extraction

    if not _TEXT_EXTRACT_PASS_LOCK.acquire(blocking=False):
        if not _TEXT_EXTRACT_WEDGE_LOGGED:
            _TEXT_EXTRACT_WEDGE_LOGGED = True
            LOG.warning(
                "TEXT_EXTRACT lane skipped: the previous pass was abandoned and its "
                "threads are still calling")
        return {"claimed": 0, "previous_pass_running": True}
    _TEXT_EXTRACT_WEDGE_LOGGED = False
    try:
        conn = db.connect()
        try:
            return description_extraction.run_pass(conn)
        finally:
            with contextlib.suppress(Exception):
                conn.close()
    finally:
        _TEXT_EXTRACT_PASS_LOCK.release()


async def _text_extract_pass(
    stop_event: asyncio.Event, state: dict[str, Any],
) -> None:
    if stop_event.is_set():
        return
    last = await asyncio.to_thread(_text_extract_sync)
    # Only when the pass did work: at a 5-minute cadence an unconditional line is ~290
    # "claimed=0" a day once the backlog is drained. The heartbeat records every pass.
    if last.get("claimed"):
        LOG.info(
            "TEXT-EXTRACT claimed=%s extracted=%s written=%s errors=%s $%.4f model=%s",
            last.get("claimed"), last.get("extracted"), last.get("written"),
            last.get("errors"), last.get("spent_usd", 0.0), last.get("model"),
        )
    _record_pass(state, "text_extract", last)


class _AutodedupDeadline(Exception):
    """An autodedup pass ran past AUTODEDUP_PASS_DEADLINE_SECONDS; its transaction rolls back."""


class _DeadlineCursor:
    """A cursor that refuses to START a statement once its pass is past the deadline."""

    def __init__(self, cursor: Any, guard: "_DeadlineConnection") -> None:
        self._cursor = cursor
        self._guard = guard

    def __enter__(self) -> "_DeadlineCursor":
        self._cursor.__enter__()
        return self

    def __exit__(self, *exc: Any) -> Any:
        return self._cursor.__exit__(*exc)

    def __iter__(self) -> Any:
        return iter(self._cursor)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)

    def execute(self, query: Any, *args: Any, **kwargs: Any) -> Any:
        self._guard.check(query)
        return self._cursor.execute(query, *args, **kwargs)

    def executemany(self, query: Any, *args: Any, **kwargs: Any) -> Any:
        self._guard.check(query)
        return self._cursor.executemany(query, *args, **kwargs)


class _DeadlineConnection:
    """The autodedup pass's connection, with a wall-clock deadline on every statement.

    Past the deadline the next statement raises instead of running, so the engine's one
    transaction rolls back on its way out (nothing written, no cursor moved — its own refusal
    contract) and the thread ends, instead of outliving LANE_PASS_TIMEOUT_SECONDS with a
    transaction and a lease open. `exempt` statements still run after it: the lease release,
    the one statement a stopped pass must still send. Everything else is the real connection
    (transaction(), close(), info) untouched."""

    def __init__(self, conn: Any, deadline: float, *, exempt: tuple[Any, ...] = (),
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._conn = conn
        self._deadline = deadline
        self._exempt = exempt
        self._clock = clock
        self.tripped = False

    def check(self, query: Any) -> None:
        # A tuple, not a set: `in` compares by equality, so an unhashable query cannot raise.
        if query in self._exempt:
            return
        if self._clock() >= self._deadline:
            self.tripped = True
            raise _AutodedupDeadline(
                f"autodedup pass stopped at its {AUTODEDUP_PASS_DEADLINE_SECONDS:.0f} s "
                "deadline; a transaction still open rolls back, so nothing half-written "
                "survives and no cursor moves past undecided work")

    def cursor(self, *args: Any, **kwargs: Any) -> _DeadlineCursor:
        return _DeadlineCursor(self._conn.cursor(*args, **kwargs), self)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def _autodedup_store_present(conn: Any) -> bool:
    from autodedup.incremental_sql import RT_STORE_PRESENT_SQL

    with conn.cursor() as cur:
        cur.execute(RT_STORE_PRESENT_SQL)
        rows = cur.fetchall()
    return bool(rows and rows[0][0])


def _autodedup_outcome(
    started: float,
    *,
    summary: dict[str, Any] | None = None,
    skipped: str | None = None,
    refused: str | None = None,
    deadline: bool = False,
) -> dict[str, Any]:
    """The heartbeat's `last` for one tick — the same keys on every path, so a skipping or
    refusing lane never reads as a quiet one: `scored` pairs, `grouped` groups written,
    `skipped` 0/1 with its `reason`, `errors` 0/1 with the refusal or abort text."""
    last: dict[str, Any] = {
        "ran": False, "claimed": 0, "scored": 0, "grouped": 0, "skipped": 0, "errors": 0,
        "seconds": round(time.monotonic() - started, 1),
        # The divisor of the NEXT pass's time budget: above 1 the lane is backing off a deadline.
        "backoff": _AUTODEDUP_BACKOFF,
    }
    if skipped is not None:
        last.update(skipped=1, reason=skipped)
    elif refused is not None:
        last.update(errors=1, refused=refused[:AUTODEDUP_REASON_CHARS],
                    deadline_exceeded=deadline)
    elif summary is not None and summary.get("skipped"):
        # The engine's own green skips: `leased` (the GH lane is passing), `unseeded` (no
        # generation to pass over yet), `dark` (the autodedup.settings stop button).
        last.update(skipped=1, reason=str(summary["skipped"]),
                    detail=str(summary.get("reason") or "")[:AUTODEDUP_REASON_CHARS])
    elif summary is not None:
        counts = summary.get("counts") or {}
        latency = summary.get("latency_s") or {}
        aborted = str(summary.get("aborted") or "")
        last.update(
            ran=True,
            claimed=int(counts.get("claimed") or 0),
            scored=int(counts.get("pairs_scored") or 0),
            grouped=int(counts.get("clusters_written") or 0),
            held=int(counts.get("held") or 0),
            retired=int(counts.get("retired") or 0),
            # A pair set that would not fit even a one-listing claim: the pass wrote nothing,
            # and the engine calls that a block worth an operator's eye.
            errors=1 if aborted else 0,
            latency_p50_s=latency.get("p50"),
            latency_p95_s=latency.get("p95"),
            bound_by=(summary.get("claim_bound") or {}).get("bound_by"),
        )
        if aborted:
            last["aborted"] = aborted[:AUTODEDUP_REASON_CHARS]
    return last


def _autodedup_sync() -> dict[str, Any]:
    """One bounded pass of THE autodedup real-time shadow lane
    (`autodedup.incremental_lane.run_incremental`), on one connection behind the deadline.

    Lazy import keeps autodedup off the worker's startup path and off every dark wake. The
    engine REFUSES by raising SystemExit — a storage budget, a parity breach, a scope it cannot
    walk, a missing migration — and a SystemExit that reached the event loop would stop the
    whole worker, so a refusal is caught HERE and recorded as an error, never re-raised. Any
    other exception is the signal, and _lane_loop records the failed pass.
    """
    global _AUTODEDUP_WEDGE_LOGGED, _AUTODEDUP_STORE_WARNED, _AUTODEDUP_BACKOFF

    started = time.monotonic()
    if not _AUTODEDUP_PASS_LOCK.acquire(blocking=False):
        # The previous pass was abandoned at LANE_PASS_TIMEOUT_SECONDS and its thread still
        # holds its connection. The deadline makes that a short window, not a wedge.
        if not _AUTODEDUP_WEDGE_LOGGED:
            _AUTODEDUP_WEDGE_LOGGED = True
            LOG.warning(
                "AUTODEDUP lane skipped: the previous pass was abandoned and its thread is "
                "still running")
        return _autodedup_outcome(started, skipped="previous_pass_running")
    _AUTODEDUP_WEDGE_LOGGED = False
    try:
        from autodedup import incremental_lane
        from autodedup.incremental_sql import RT_LEASE_RELEASE_SQL

        backoff = _AUTODEDUP_BACKOFF
        budget = AUTODEDUP_PASS_BUDGET_SECONDS / backoff
        conn = db.connect()
        try:
            if not _autodedup_store_present(conn):
                if not _AUTODEDUP_STORE_WARNED:
                    _AUTODEDUP_STORE_WARNED = True
                    LOG.warning(
                        "AUTODEDUP: the autodedup real-time store (migrations 539/540) is "
                        "absent on this database; the lane skips every tick")
                return _autodedup_outcome(started, skipped="store_absent")
            _AUTODEDUP_STORE_WARNED = False
            guarded = _DeadlineConnection(
                conn, started + AUTODEDUP_PASS_DEADLINE_SECONDS,
                exempt=(RT_LEASE_RELEASE_SQL,))
            # The pass writes its summary file where the workflow uploads one; here nobody
            # reads it (the heartbeat is the readout), so it lives for the pass and no longer.
            with tempfile.TemporaryDirectory(prefix="autodedup-rt-") as out_dir:
                try:
                    # No argument at all: a pass runs under the scope and scorer its
                    # generation was SEEDED with (a differing one is a re-scope the engine
                    # refuses), and the claim is the engine's own — its measured rate times
                    # this budget. `enabled=True`: the interval above 0 is the lane's switch.
                    summary = incremental_lane.run_incremental(
                        lambda: guarded, {}, Path(out_dir),
                        enabled=True, max_pass_budget_s=budget)
                except SystemExit as exc:
                    return _autodedup_outcome(started, refused=str(exc))
                except _AutodedupDeadline as exc:
                    _AUTODEDUP_BACKOFF = min(backoff * 2, int(AUTODEDUP_PASS_BUDGET_SECONDS))
                    return _autodedup_outcome(started, refused=str(exc), deadline=True)
            last = _autodedup_outcome(started, summary=summary)
            if (backoff > 1 and last["ran"] and not last["errors"]
                    and last["claimed"] >= incremental_lane.PASS_RATE_MIN_CLAIM):
                _AUTODEDUP_BACKOFF = 1
                last["backoff"] = 1
            return last
        finally:
            # run_incremental closes the connection it was handed; a second close is a
            # no-op, and this one covers every path that never got that far.
            with contextlib.suppress(Exception):
                conn.close()
    finally:
        _AUTODEDUP_PASS_LOCK.release()


def _autodedup_note(last: dict[str, Any]) -> None:
    """Log the lane's OUTCOME on a change, never per pass."""
    global _AUTODEDUP_LAST_OUTCOME

    if last.get("errors"):
        key = "error:" + str(last.get("refused") or last.get("aborted") or "")
    elif last.get("skipped"):
        key = "skipped:" + str(last.get("reason"))
    else:
        key = "ran"
    if key == _AUTODEDUP_LAST_OUTCOME:
        return
    _AUTODEDUP_LAST_OUTCOME = key
    if last.get("errors"):
        LOG.warning("AUTODEDUP lane pass stopped: %s",
                    last.get("refused") or last.get("aborted"))
    elif last.get("skipped"):
        LOG.info("AUTODEDUP lane skipping: %s %s", last.get("reason"), last.get("detail") or "")
    else:
        LOG.info("AUTODEDUP lane passing")


async def _autodedup_pass(stop_event: asyncio.Event, state: dict[str, Any]) -> None:
    if stop_event.is_set():
        return
    last = await asyncio.to_thread(_autodedup_sync)
    _autodedup_note(last)
    # Only when the pass decided something: at a 60 s cadence an unconditional line is
    # ~1,440 "claimed=0" a day. The heartbeat records every pass either way.
    if last.get("claimed"):
        LOG.info(
            "AUTODEDUP lane claimed=%d scored=%d grouped=%d held=%d retired=%d "
            "p50=%ss p95=%ss bound_by=%s %.1fs",
            last["claimed"], last["scored"], last["grouped"], last.get("held", 0),
            last.get("retired", 0), last.get("latency_p50_s"), last.get("latency_p95_s"),
            last.get("bound_by"), last["seconds"],
        )
    if last.get("refused"):
        # A refusal or a deadline trip is a pass that RAISED; the lane caught it only so a
        # SystemExit could not stop the event loop. So it is counted where every lane's raised
        # passes are (`failed_passes`, `last_failure_at`), with the text kept in `last`.
        _record_pass_failed(state, "autodedup", float(last["seconds"]))
        state["lanes"]["autodedup"]["last"] = last
        return
    _record_pass(state, "autodedup", last)


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
    first_delay_seconds: float = 0.0,
) -> None:
    """One forever-lane: re-read the interval each pass (live app_settings
    edits apply on the next wake), interval<=0 = idle-not-dead, per-pass
    try/except, clean stop_event exit. Mirrors notifications.matcher_loop."""
    LOG.info("%s lane starting", name)
    if first_delay_seconds > 0:
        # A lane that ticks once a day (location_refetch) does not run its pass in the
        # same second the container boots: every other lane is starting too, and a deploy
        # loop would otherwise turn "daily" into "per redeploy".
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=first_delay_seconds)
        except asyncio.TimeoutError:
            pass
        else:
            LOG.info("%s lane stopped", name)
            return
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
        # default_interval is the lane's OWN cadence here, not 0: this reader touches no
        # database (env vars only), so it cannot fail the way the app_settings readers can,
        # and the lane ships LIVE.
        ("location_intake_fast", lambda: _lane_loop(
            "location_intake_fast", stop_event, _read_location_intake_fast_interval,
            lambda: _location_intake_fast_pass(stop_event, state),
            state,
            default_interval=LOCATION_INTAKE_FAST_INTERVAL_DEFAULT)),
        # Daily, and it does NOT alarm as dead between ticks: check_worker_lane_stall
        # reads `in_flight_s` (a pass RUNNING too long), never the gap between passes, so
        # an idle lane is simply skipped by the check.
        ("location_refetch", lambda: _lane_loop(
            "location_refetch", stop_event, _read_location_refetch_interval,
            lambda: _location_refetch_pass(stop_event, state),
            state,
            default_interval=LOCATION_REFETCH_INTERVAL_DEFAULT,
            first_delay_seconds=LOCATION_REFETCH_FIRST_DELAY_SECONDS)),
        # default_interval=0 for the location_resolve lane's reason above: the
        # interval IS the kill switch, so a settings-read failure must not wake a
        # lane the operator deliberately left dark — and this one spends an external
        # site's bandwidth when it wakes.
        ("sold_comps", lambda: _lane_loop(
            "sold_comps", stop_event, _read_sold_comps_interval,
            lambda: _sold_comps_pass(stop_event, state),
            state,
            default_interval=0,
            first_delay_seconds=SOLD_COMPS_FIRST_DELAY_SECONDS)),
        # A CONSTANT interval, like the heartbeat lane's: no app_settings read, therefore
        # no failure mode and no fail-safe-0 requirement. What governs this lane is the
        # attribute contract, and a lane with no gated `text` cell simply claims nothing.
        ("text_extract", lambda: _lane_loop(
            "text_extract", stop_event, lambda: TEXT_EXTRACT_INTERVAL_SECONDS,
            lambda: _text_extract_pass(stop_event, state),
            state,
            default_interval=TEXT_EXTRACT_INTERVAL_SECONDS)),
        # default_interval=0 for the sold_comps lane's reason above: the interval IS the kill
        # switch, so a settings-read failure must not wake a lane the operator left dark.
        ("autodedup", lambda: _lane_loop(
            "autodedup", stop_event, _read_autodedup_interval,
            lambda: _autodedup_pass(stop_event, state),
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
