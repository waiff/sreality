# Real-time scrapers — the dual-lane program

Decision record for the operator-greenlit (2026-07-02) real-time program. It consciously
supersedes the since-deleted `multi-portal-dedup.md`'s locked decision #3 ("a property's inactive flag lags by
at most one job interval … notifications are (at most) daily"): the north star is now
minutes-grade for new listings (incl. images), delistings, cross-portal merges, and watchdog
notifications, at 100% portal coverage with zero standing health issues.

Grounded in a 19-agent investigation of `main@144f564` + production (2026-07-02): measured
baselines, per-portal delta capability (live-verified), and industry practice (a Czech
aggregator runs the same dual-lane design over 23 CZ portals at 10–20 min listing latency).
The full findings live in the session record; the decisions are here.

## Why the pipeline is slow today (verified)

Every stage is already an idempotent, resumable drain over a Postgres queue — the data model
needs no redesign for LATENCY. **Correction (2026-08-04, portal-order-fidelity Phase 1/4
investigation):** "newest-first" above was an unqualified assumption, not a verified property —
`listings.first_seen_at` (what Browse's "newest first" sort reads) is stamped at detail-drain
WRITE time, and five independent mechanisms (priority-bucketed claiming, transaction-constant
`enqueued_at`, thread-pool completion-order fetch, batch-constant `now()`, concurrent drain
processes) reorder a listing between discovery and that write. See
`docs/design/portal-order-fidelity.md` for the full analysis and the fix
(`listings.discovery_seq`, migration 368). The latency claim in this doc is unaffected — cron
quantization is real and independent of ordering fidelity — but the queue was never actually
order-preserving the way this sentence implied. The latency is **cron quantization on GitHub
Actions**:
each hop (index walk → detail drain → image bytes → pHash → CLIP → dedup dirty drain →
matcher) waits for its next cron tick, and GH schedules are throttled (sreality's `*/15`
walk fires every ~61 min measured; community data shows 10–45 min delays are routine; the
20-concurrent-job cap is exceeded at peak minutes). Measured end-to-end: new listing →
Browse ~1 h (sreality) / 4–6 h (6 h portals); delisting p50 1.07 h (sreality) / 24–29 h
(24 h-rail portals) / up to 7 d (mmreality); cross-portal merge p50 12.3 h (2.7% within 1 h);
watchdog price-drop detection on a DAILY gate.

## The architecture: dual lane

**Hot lane — one always-on worker** (second Railway service from the EXISTING Docker image;
the scraper package is already importable there and the API already runs the same
settings-paced asyncio-loop pattern — matcher/outbox). It runs, as continuous loops:

1. **Newest-first delta probes, 2–5 min per portal** (live-verified capability):
   bazos/idnes/realitymix/remax — default index order IS newest-first (1–3 pages/probe);
   bezrealitky — already queries `TIMEORDER_DESC` (11 req/cycle); ceskereality — the
   `/nejnovejsi/` path slug via the existing proxy (default order is NOT newest);
   maxima — the 22-page full walk IS the probe; idnes bonus — `?s-qc[articleAge]=1`
   returns the full one-day delta (~16 pages). **sreality** — the v1 GET ignores every
   sort param (live-probed; page 1 is promotion-polluted), so it gets `pagination.total`
   count-delta probes (20 pairs ≈ 10 s) that trigger targeted category walks, plus a
   one-time HAR spike on the Next.js BFF (its react-query key proves `sort:'-date'`
   exists server-side, still unused — see the "hard case" section of
   `docs/design/portal-order-fidelity.md`). **Update (2026-08-04, portal-order-fidelity
   Phase 4):** sreality also joined `REALTIME_SOURCES` with its own bespoke
   `probe_category` (the ceskereality pattern — per-page early-stop diff, UNSPLIT since
   the 422 is offset- not size-triggered), complementing rather than replacing the
   count-probe lane. **mmreality** — proxied, low-frequency only (cost).
   Probes reuse the safe partial-walk primitive: diff + enqueue, `complete=False`, so a
   probe can never falsely delist (rule #3 machinery unchanged).
2. **Continuous detail drain** off the existing SKIP LOCKED queue (multi-runtime-safe by
   construction; stale-claim reclaim already handles crashes).
3. **Per-listing unit processing with the images-first publication gate** (operator
   decision 2026-07-02): detail write → image download (per-host semaphore + breaker
   already exist) → pHash computed INLINE on the bytes in hand (deletes the hourly
   re-download hop) → CLIP tag via a warm-model loop (~0.5 img/s steady inflow vs
   ~10 img/s/4 vCPU capacity) → dedup dirty enqueue → matcher wake. A listing is not
   surfaced as "new" (watchdog dispatch, new-listing feeds) until its first image is
   stored, with a timeout fallback for listings that genuinely have no photos.
4. **Targeted gone-probes** for watchdog/pipeline/collection properties (the per-listing
   `ListingGoneError` → immediate inactive flip already exists) — minutes-grade delisting
   for WATCHED properties; market-wide delisting stays completeness-gated on the walks.
5. **Notification producers, event-driven**: matcher woken per new-property batch (also
   fixes the cursor-vs-attach race), price-drop detection moved to write time (the drain
   already computes the price diff), sreality singleton-property creation inlined into
   `write_detail_batch` (every other portal already creates it inline).

**Cold lane — GitHub Actions keeps** the delay-tolerant heavy work it does well and for
free: full reconcile index walks (completeness + delisting evidence), image/CLIP backfills,
dedup full scans + batch lanes, monitors, CI. Self-chaining (`SCRAPE_CHAIN_TOKEN`) is
legacy backlog acceleration — do not extend it to new lanes.

**Prerequisite: shared politeness.** `RateLimiter` is per-process; a Railway lane beside
Actions walks would double-hit portals with two independent limiters. Before the probe lane
ships at portal-meaningful volume: a DB-backed rate/penalty ledger keyed on
`portals.source` (extends the migration-114 config surface) so both runtimes share one
budget and a 429/403 penalty propagates. Probes are net-polite: ~1 page/host/interval is
far below today's accepted walk volume.

**Explicitly rejected:** Playwright/headless (all 9 portals serve complete data over plain
HTTP; the mmreality/ceskereality blocker is IP reputation, which only residential egress
fixes — 10–50× per-page cost for nothing) and any new queue infrastructure (Redis etc. —
Postgres SKIP LOCKED is the industry-standard substrate and already proven here).

## SLOs (operator-accepted 2026-07-02)

Google-SRE format, measured on stage watermarks (p95, warn at ~75% of target): new listing
visible ≤ 15 min · images stored ≤ 20 min · **no publication without images** (gate above) ·
photo-sharing cross-portal merge ≤ 60 min · delisting ≤ 6 h market-wide / ≤ 30 min watched ·
notification ≤ 2 min after the property row exists. `published_at` (migration 266) and the
`detail_queue_completions` ledger (migration 265) are the measurement substrate; health
checks move from fixed batch-era thresholds to cadence/SLO-scaled ones, with reds pushed
through the existing notification outbox.

## Costs & constraints

Worker: ~$5–15/mo (Railway; same image, own service so probe load can't degrade the API).
Residential proxy: measured ~15–20 GB/mo at CURRENT cadence vs a 10 GB plan — ceskereality's
~100k proxied index pages/week dominate; plan = top-up decision + drop its full-walk cadence
once probes carry discovery. Dedup dirty-lane floor-plan budget: ≤ ~$1/day. sreality
robots.txt disallows generic crawlers (EU exposure is civil/contractual — Ryanair v PR
Aviation); the probe design keeps request volume at or below today's accepted level, which
is the defensible posture the operator accepted.

## Sequencing

Wave A+B (2026-07, shipped as PRs #678–#683 + published_at/pozemek follow-ups): correctness
fixes real-time would amplify (cross-slice delisting flap, remax rent starvation, idnes FX
churn, sreality hash flaps, idnes area truncation) + the measurement substrate. Wave C
(#688–#692, shipped): the always-on worker (`scraper/realtime_worker.py`, ships dark behind
`REALTIME_WORKER_ENABLED`) + `LedgerRateLimiter` + probe lanes + images-first gate.

**Waves W1–W5 (shipped 2026-07-04):**
- **W1** (#694) — per-source drain-disable knob (`realtime_drain_disabled_sources`), the
  proxy-outage lesson: freeze a portal's queue instead of burning it to `given_up`.
- **W2** (#695) — bounded live forensics on the `--free` lanes (`--compare-budget`, dirty 40 /
  candidates 100 / full 300), so different-photo cross-portal apartments auto-merge on the
  scheduled run (`auto_visual` was 0 for days). **DECISION: NO batch warmer** — the dedup cost
  posture is pay-at-decision-time (pHash → CLIP cosine → bounded live forensics), NOT the
  all-rooms pre-buy (structurally wasteful for a stop-at-first-High flow). `dedup_batches.yml`
  is dispatch-only; the floor-plan gate rides its OWN separate budget. (Replaces the
  now-abandoned "batch-warmer revival" this doc once listed.)
- **W3** (#696, migration 270) — sreality count-probe lane: sreality's sort-blind v1 API gets
  `pagination.total` count-delta probes that (opt-in, token+setting gated) trigger a targeted
  `index_walk`.
- **W4** — idnes/bazos street coverage: the RÚIAN resolver lever is **exhausted** at the 15 m
  tolerance (idnes coords are >15 m from address points → 0 net resolutions; bazos coords are
  the page-wide-link shared pins the resolver correctly rejects — it's excluded from the
  script's `_SOURCES`). The idnes/bazos 61%/51% ceiling is a COORD-PRECISION problem, not a
  resolver-hasn't-run gap; a real fix needs better per-listing coord extraction (out of scope).
- **W5a** (#697) — delisting rails: sreality `INDEX_MIN_COMPLETENESS` 1.0→0.995 + a new 3h
  `min_unseen_hours` rail; the 6 h portals 24h→12h.

**Location-resolve lane (Decision 8b, ships dark):** the location resolver's `dirty_locations`
drain now also runs from this worker, not only from `location_resolve.yml`. The drain's cost is
round trips, not work — ~11 registry round trips per listing at ~120 ms from a US GitHub runner
measured **0.7 listings/s**, and GitHub fires the schedule ~7x/day against a >100k queue. The
worker sits beside the database in the EU (~1–2 ms) and runs continuously. It **reuses**
`location_data.resolver.drain.run()` and `lease.held()` — no copy of the drain, the CAS or the
resolver — and takes the SAME `location_jobs` lease row (`drain.JOB_NAME`, imported never
re-spelled) on the drain's own session-pooler connection, so the worker lane and the
Actions lane can never drain at once; a busy lease is a cheap no-op (`{"acquired": false}` in
the heartbeat, logged on the transition only). The GH lane stays the backstop and keeps
`--full-sweep` / `--dry-run` / `--listing-id`. Dark until
`app_settings.realtime_location_resolve_enabled` is set; tuning keys
`realtime_location_resolve_{interval_seconds,max_seconds,batch_size}` (15 s / 240 s / 250).
Budget shape, and the two rails that make the shared lease actually exclusive:
- **The pass drains `LOCATION_RESOLVE_WORKERS` slices CONCURRENTLY (env, default 4, clamped 1–8;
  W2-a5).** An env var and not an `app_settings` row, because how many connections this process may
  open is a property of the machine rather than an operator preference. One loop measured
  **~8 listings/s** on 2026-09-12 against a 448k queue with every backend on the instance waiting on
  `DataFileRead` — latency, which N loops overlap. Each worker is a thread with its OWN session-mode
  connection (psycopg connections are not thread-safe) claiming through the same `FOR UPDATE SKIP
  LOCKED` statement, so the slices are disjoint by construction; the lease, the budget and the
  `RunCache` are shared (the cache under a lock that is never held across a registry question). A
  batch that RAISES costs its slice and nothing else (W2-a6): the transaction rolls back, its
  rows stay queued untouched, the loop backs off (2 s doubling to 30 s) and claims again, and
  only five CONSECUTIVE failures — or a lost connection, reconnected once — stop a worker. The
  prefetch runs on its own 90 s ceiling (`LOCATION_RESOLVE_PREFETCH_TIMEOUT_S`), because a
  250-listing bulk claims read legitimately outruns the 30 s a per-listing statement gets. The
  heartbeat's `workers` / `failed_batches` are how a degraded pass is told from a healthy one —
  `failed_batches`, never `failed_passes`, which means "passes that raised" one level up. The lease TTL is
  unchanged: every loop tests the budget between batches and they run concurrently, so N workers
  still overrun by at most ONE batch.
- `max_seconds` is clamped to ≤ 900 and `batch_size` to ≤ 1000, so a HEALTHY pass stays far below
  `check_worker_lane_stall`'s 1200 s `in_flight` warn and below `LANE_PASS_TIMEOUT_SECONDS`.
  Throughput does not need a long pass — the queue IS the cursor.
- **The lease TTL is `max_seconds + batch_size × 2 s` (floor 120 s), not a flat constant.**
  `lease.held` stamps `lease_expires_at` once and never renews it, and `drain.run` tests its
  budget only BETWEEN batches — so a pass runs `max_seconds` plus one whole batch, and a flat
  headroom was only ever right for one batch size. 2 s/listing is ~1.6x the drain's own
  ~1.2 s/listing target and above the 1.43 s/listing the GH runner measured. A SIGKILLed worker
  still frees the lane in ~12 minutes at the defaults rather than the lease default's hour.
- **An in-process lock, because an abandoned pass outlives its lease.** A pass abandoned at
  `LANE_PASS_TIMEOUT_SECONDS` keeps running (Python cannot kill the thread) while its lease has
  already expired, so the lease alone would let the next pass, 15 s later, drain beside it inside
  the same process. `_RESOLVE_PASS_LOCK` is taken non-blockingly for the whole pass: a pass that
  cannot take it returns `{"acquired": false, "previous_pass_running": true}` and never touches
  the lease row. The lease is what excludes the GH lane; the lock is what excludes this worker
  from itself.
- **The dark gate is fail-safe.** The lane is registered with `default_interval=0`, because
  `_lane_loop` keeps `default_interval` when the `app_settings` read RAISES and the flag lives
  inside the read that just failed. With a positive fallback one pooler blip would run a full
  drain pass while the operator believes the lane is off — including mid-`epoch_job`, the one
  moment the procedure below exists to protect.

**The realtime-worker Railway service needs `SUPABASE_DB_SESSION_URL`**
(the API service and the GH lane already have it); without it the lane still works on the
transaction pooler, several times slower, and says so once in the log and every pass in
`worker_heartbeats.details->'location_resolve'->'last'->>'session_pooler'`.

Two caveats this lane accepts rather than fixes:
- **The `location-batch` Actions concurrency group does not reach Railway** (that is the point of
  Decision 8). The worker drain can now run concurrently with the registry load, claim intake and
  Mapy inventory, which the 2026-08-10 incident deliberately serialized. Mitigations already in
  place: a bounded number of connections (`LOCATION_RESOLVE_WORKERS`, default 4 since W2-a5 — set it
  to 1 to restore the single-connection posture), batch 250, `SET LOCAL
  statement_timeout`/`lock_timeout` on every batch transaction, and
  `realtime_location_resolve_interval_seconds = 0` as the operator's instant idle
  switch before a heavy lane. A cross-runner group is explicitly out of scope here.
- **Epoch recompute overlap.** The inner `location-resolve` Actions group is what stopped
  `epoch_job` from overlapping a drain; the worker is outside it and the two lease rows do not
  exclude each other. `run()` reads `current_epoch` once per pass, so a pass in flight when a new
  epoch is minted resolves the epoch job's freshly enqueued rows against the OUTGOING epoch and
  deletes their `dirty_locations` rows — and the full sweep's stale predicate keys on
  `resolver_version`/`policy_version`/`registry_version_id`, not the epoch, so it does not
  self-heal. The 240 s budget bounds it to one pass; the procedure is: disable the flag (or set
  interval 0) → wait ≤ interval + one pass → run the epoch job → re-enable. A shared lease for the
  epoch job (or an epoch re-check between batches) is the real fix, and is a follow-up.

**Location-intake-fast lane (W7-a, ships LIVE):** the claim lane's change-driven listing scan
(`location_data.claims_intake.run`, `mode="incremental"`) also runs from this worker, every ~60 s.
Not a second lane — a second SCHEDULE for the same module. The hourly `location_claims_intake.yml`
plus its 15-minute snapshot lag left a listing written 45 s after a tick with no claims and no
verdict for up to ~75 minutes, and under W5 the consumers serve only resolved locations, so that is
~75 minutes invisible in Browse (measured 2026-09-13). The worker pass is the payload half only —
`skip_bodies=True`: no bodies-first drain, no R2 client, no `forkserver` pool — with a **2-minute**
lag, a 45 s budget and 2 000-row batches. Env knobs on the Railway service:
`LOCATION_INTAKE_FAST_{ENABLED,INTERVAL_S,LAG_S,BUDGET_S}` (`1`/60/120/45); `ENABLED=0` idles it.
- **No lease; the CURSOR is the coordination.** `location_claim_batches` resumes on `(lane, source,
  scan_mode)` and this schedule stamps `claims_intake.FAST_LANE`, so it can neither read nor advance
  the hourly run's position. That separation is also what makes the short lag safe: the lag guards a
  bigserial race (an id allocated at INSERT, visible at COMMIT, below a keyset that already moved),
  so a 2-minute window WILL skip such a row occasionally — and the hourly run, still 15 minutes back
  on its own cursor, re-reads exactly that slice within the hour. Mining twice is free (claim
  fingerprints `ON CONFLICT DO NOTHING`, the resolve enqueue a bump).
- **An in-process lock, same argument as the resolve lane.** A pass abandoned at
  `LANE_PASS_TIMEOUT_SECONDS` keeps running; two scans sharing one cursor would each re-read the
  other's window. `_INTAKE_FAST_PASS_LOCK` is taken non-blockingly for the whole pass and a tick
  that cannot take it returns `{"ran": false, "previous_pass_running": true}`.
- **It mines new page bodies too (W7-a2).** idnes, realitymix, bazos, ceskereality, remax and
  maxima carry a listing's location only in the stored detail body, so while only the hourly run
  mined bodies their new listings waited up to an hour while sreality's took 134 s. The tick runs
  the JSON half FIRST and hands the bodies pass the REMAINDER of the same 45 s
  (`bodies_budget_share=1.0`), capped at `LOCATION_INTAKE_FAST_BODIES_CAP` (300) bodies checked
  BETWEEN batches — never truncating a window, because the bodies cursor advances to the window's
  max id. R2 fetch width 8 (`setdefault`, so the service can override), extraction two forkserver
  workers wide in ONE `ExtractionPool` held for the life of the lane. Cheap because of the
  W6-b/W6-b2 cursor, which is lane-scoped like the listing keyset: the first tick after a deploy
  walks the payload keyset once and every tick after that opens only the ids above what it stamped.
  Without `R2_*` on the service the lane warns ONCE per process and mines JSON only.
- **It never inherits the full walk.** `_full_walk_handoff` looks that cursor up BY LANE and this
  schedule never runs `--mode full`, so a 45 s tick cannot pick up a ~376k-listing contract re-walk.
- **The contracts are projected from the image once at lane start** (`contracts/` is in the Docker
  image already, for `payload_norm`), idempotent per `(portal, contract_version)`. A failure is a
  WARNING, never a dead lane: the projection raises by design when a contract's governed bytes moved
  without a version bump, and `location_claims_intake.yml` is the authoritative projector.
- **Latency, detail write to Browse:** ≤2 min lag → ≤1 min tick → the resolve lane (~15 s) →
  `browse_list`'s `*/15` pg_cron rebuild. Claim and verdict in ~3–4 minutes; the read model is now
  the slowest hop, not the claim lane.

**Location-refetch lane (location simplification W8, ships LIVE):** once a day
(`LOCATION_REFETCH_INTERVAL_S`, default 86400, first tick after a
`LOCATION_REFETCH_FIRST_DELAY_SECONDS` = 300 s delay so a redeploy loop cannot turn "daily" into
"per restart"), queue the audit page's active "no data" listings for ONE more detail fetch.

- **Why.** ceskereality and realitymix geocode an ad after publishing it; our detail fetch ran at
  discovery, read empty location fields, and the index card never changed, so no re-fetch was ever
  enqueued (measured 2026-09-14: 56 of 63 ceskereality and 56 of 112 realitymix rows in that bucket
  were fetched within two minutes of first sighting and never again).
- **The audit relation IS the candidate list** — `location_pin_audit_mv` with
  `state='unresolved'` + `quality='active_no_claims'`, joined to `listings` by PK for the live
  `source_url` / `price_czk` / `is_active` (the matview is an hourly snapshot; a re-fetch must not
  be aimed by a stale URL). Rows whose newest `portal_raw_pages` / `portal_raw_payloads` record is
  younger than `LOCATION_REFETCH_MIN_AGE_S` (6 h) are skipped — re-reading a page fetched minutes
  ago only re-reads the same empty moment.
- **Bounded per portal, oldest-fetched first.** `LOCATION_REFETCH_CAP_PER_SOURCE` (500) rows per
  portal per tick, so bazos's dead ads (their page answers with the category listing →
  `ListingGoneError` → delisted by the same fetch) drain over days instead of flooding one drain
  pass. The heartbeat's `last` carries the UNCAPPED per-portal backlog beside what it queued.
- **It enqueues and nothing else.** `db.enqueue_location_refetch` writes at
  `QUEUE_PRIORITY_VERIFY` with `LEAST` on priority and `given_up=false, attempts=0` (the rows it
  most wants are the ones the drain gave up on; nothing else re-arms them). `claim_detail_batch`
  reserves a share of every claim for that class and never filters on `supports_complete_walk`, so
  every portal's drain — worker or Actions — serves them. Then the drain rewrites the page, the
  `location_intake_fast` lane mines the new body within a minute, and the resolver answers.
- **No lease, no in-process lock.** One SELECT plus one INSERT per portal; the enqueue is idempotent
  on `(source, native_id)` and never disturbs a claimed row. A missing `location_pin_audit_mv` (a
  branch database) skips the tick with ONE warning per process.
- **A daily lane is not a dead lane.** `check_worker_lane_stall` reads `in_flight_s` — a pass
  RUNNING too long — and skips a lane that is idle between passes, so no threshold changed.

**Sold-comps lane (sold-comps W2, ships DARK):** fetch registered sales from reas.cz — an external
FACT feed, not a tenth portal — for the towns the operator is actually working in. The interval is
one `app_settings` integer, `realtime_sold_comps_interval_seconds`, seeded 0 by migration 544.

- **One integer, no flag.** `_lane_loop` treats `interval <= 0` as idle-not-dead, so the setting is
  the cadence AND the kill switch. This is deliberately LESS than the estimation and
  location-resolve lanes, which each carry a boolean setting on top of an interval — that pairing
  is the variant not to copy. Registered with `default_interval=0` for the resolve lane's
  fail-safe reason, sharpened here: a settings blip must not spend another site's bandwidth.
- **The work-list is a query, not a queue** (`sold_db.sold_comp_cells`): obec cells of properties
  holding ANY account's pipeline card at a non-terminal, non-archived stage, active, `byt`/`dum`,
  with a resolved point AND an `admin_boundaries` polygon, minus the cells whose NEWEST ledger row
  is `ok` within 35 days or `failed` within 6 hours; stalest first, 5 cells a pass. The source
  republishes a transfer ~30 days after the sale, so 35 days is not a guess — asking sooner cannot
  find anything new. The boundary join is the one clause that is about the QUEUE rather than the
  work: an unboxable cell can only be skipped, a skip writes no ledger row, and `NULLS FIRST` would
  then hand back the same starved cell at the head of every pass for ever.
- **The cell is the unit, and the ledger is the run record.** The box is the obec's
  `admin_boundaries` envelope widened by 5,000 m (`sold_db.MAX_READ_RADIUS_M` = the read surface's
  largest radius). `sold_fetch.fetch_cell` NEVER raises: ok, failed or skipped, every attempt that
  has a box ends as a `sold_transaction_fetches` row — a cell that failed silently would be
  indistinguishable from a cell that holds no sales. And an `ok` row says what it COVERED: a walk
  the page cap or a non-advancing `nextPage` cut short carries `truncated: took N of M in P pages`
  in `error`, because a clean `ok` would suppress the cell for 35 days over part of an answer.
  `record_count` is what the upsert wrote, not what the pages parsed, so it cannot over-report
  against its own table when a shifting page boundary serves one transfer twice.
- **No lease, but ONE in-process lock.** The pass is one SELECT plus idempotent cell writes, so no
  lease — but a pass abandoned at `LANE_PASS_TIMEOUT_SECONDS` keeps running with no ledger row
  written yet, so freshness cannot stop the next tick re-walking its cells beside it (reachable:
  under the limiter's 8x penalty factor a five-cell pass can outlast the timeout).
  `_SOLD_COMPS_PASS_LOCK` is taken non-blocking for the whole pass — the `location_resolve`
  precedent above. A missing store (a branch database, or `main` before the apply) skips the tick
  with ONE warning per process.
- **Politeness.** One request per five seconds on the shared `portal_rate_state` ledger (no seed
  row, no `portal_configs` entry — a non-portal source needs neither), an identifying User-Agent,
  `listPerPage=100`, ONE retry rather than `portal_base`'s three (403/429 are in `RETRYABLE_STATUS`,
  and this feed's failures already come back in six hours), and a walk that follows `nextPage` alone
  under a 25-page runaway cap. Every page is SSR-computed and served `no-store`, so ~1.9 MB is real
  origin work; Praha, the one cell measured to paginate at all, is 11 pages — of its BARE envelope,
  not of the +5 km box a cell actually sends, which is why the cap records truncation instead of
  being assumed generous.

**Autodedup lane (AUTODEDUP rollout §7.3, ships DARK):** the dedup engine's real-time SHADOW pass
(`autodedup.incremental_lane.run_incremental`, the function `autodedup_realtime.yml` runs as
`python -m autodedup.lane --mode incremental` — imported, never copied) from this worker, because
GitHub fires that `*/10` schedule hours apart. Dark until `app_settings.realtime_autodedup_enabled`
is true; `realtime_autodedup_interval_seconds` (60) paces it and `0` idles it;
`realtime_autodedup_max_listings` (100, clamped 1–500) caps one claim. All three are seeded by
migration 557 (flag `false`) so /settings can flip them. The worker has no repository variable, so
it hands its flag to the engine as `enabled=True`; everything else the engine enforces binds it
exactly as it binds the workflow — the `autodedup.settings.realtime_enabled` stop button, the
unseeded skip, the storage budget, the parity gate, the pair budget.

- **Shadow only.** A pass writes the `rt` generation inside schema `autodedup` (fingerprints,
  postings, pairs, groups, cursors) and nothing else — no `public.listings` row, no `property_id`,
  no merge. Turning engine groups into production merges is a separate adapter over
  `merge_properties`; this lane neither calls nor imports it (a test drives a real pass over the
  engine's Postgres stand-in and checks every write target is `autodedup.*`).
- **One position, not two.** The engine keeps its watermark in `autodedup.scan_cursor` and its
  lease in `autodedup.rt_lease`, both keyed by name, so this lane and the workflow read and advance
  the SAME cursors under the SAME lease: whichever holds it passes, the other is a green
  `skipped: leased`. That is the location-resolve precedent (shared lease, the GH lane stays the
  backstop), not the intake's two cursors — deciding a listing twice into one generation is not free.
  `rt_seed` takes the same lease through its transaction (and refuses while a pass holds it), so no
  pass lands in a generation a seed is resetting. Switching the workflow's repository variable off
  does NOT stop this lane: `autodedup.settings.realtime_enabled = false` stops both.
- **A hard deadline, because the engine's time budget bounds the claim, not the clock.**
  `AUTODEDUP_PASS_DEADLINE_SECONDS` (1050) wraps the pass's connection: past it every statement
  except the lease release raises, so the one transaction rolls back (nothing written, no cursor
  moved), the lease is freed and the thread ends rather than outliving `LANE_PASS_TIMEOUT_SECONDS`
  with a transaction open. Deadline + one 120 s statement stays under the 1200 s stall warn, the
  lane timeout and the engine's 2100 s lease TTL. Plus the usual in-process pass lock.
- **Claims sized to fit that deadline.** The engine sizes a claim to FILL its time budget (budget ×
  its measured rate, E98), and its 900 s default is sized for the workflow's 25-minute job. The
  lane hands in `max_pass_budget_s` = half the deadline (525 s; the engine uses the smaller of that
  and `rt_pass_budget_s`). A trip rolls back without recording a rate, so after one the lane halves
  its cap AND budget for the next pass (down to one listing), back to full only after a clean pass
  claimed enough to re-measure the rate (`PASS_RATE_MIN_CLAIM`, 20). `last.backoff` shows the divisor.
- **A refusal is an error, never a crash.** The engine refuses by raising `SystemExit`; carried out
  of `asyncio.to_thread` that would stop the event loop and every lane, so the lane records it as
  `errors: 1` with the text, counts it (and a deadline trip) as a failed pass (`failed_passes`,
  `last_failure_at`, as for any lane whose pass raised) and logs it on the transition only. Heartbeat
  `details.autodedup.last` = `{ran, claimed, scored (pairs), grouped (groups written), skipped (0/1)
  + reason, errors (0/1) + refused/aborted, cap, seconds, backoff, held, retired, latency_p50_s,
  latency_p95_s, bound_by}`; an absent store (migrations 539/540) = `skipped: store_absent` + one
  warning.
- **Latency floor.** The engine ignores rows younger than its settle lag (`SETTLE_LAG_S`, 300 s,
  E73 — the changed-listing feed has no straggler sweep), and a photo-dependent merge waits for the
  pHash/CLIP producers (E93). So this lane brings decisions from hours to ~5–6 minutes; sub-minute
  needs those two moved, which is an engine decision, not a worker one.

**Deferred — W5b (health/SLO re-derivation):** cadence-scale the fixed thresholds
(`detail_queue_backlog` by oldest-row AGE not count — matview line ~301; `delisting_spike` as
% of portal size — line ~264), close silent-greens (image-pipeline liveness, dedup
dirty-banner staleness, drift no-baseline→warn, monitor-of-monitors on
`health_mv_refresh_stamp`), surface `worker_liveness` on the Health page, and push health reds
through the notification outbox. It's an intricate rewrite of the 396-line `scraper_health_checks`
matview (migration 214 — byte-for-byte jsonb contract) + frontend + outbox, so it needs its own
focused session, not a rushed one-shot.
