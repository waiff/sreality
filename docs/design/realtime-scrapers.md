# Real-time scrapers — the dual-lane program

Decision record for the operator-greenlit (2026-07-02) real-time program. It consciously
supersedes `multi-portal-dedup.md`'s locked decision #3 ("a property's inactive flag lags by
at most one job interval … notifications are (at most) daily"): the north star is now
minutes-grade for new listings (incl. images), delistings, cross-portal merges, and watchdog
notifications, at 100% portal coverage with zero standing health issues.

Grounded in a 19-agent investigation of `main@144f564` + production (2026-07-02): measured
baselines, per-portal delta capability (live-verified), and industry practice (a Czech
aggregator runs the same dual-lane design over 23 CZ portals at 10–20 min listing latency).
The full findings live in the session record; the decisions are here.

## Why the pipeline is slow today (verified)

Every stage is already an idempotent, resumable, newest-first drain over a Postgres queue —
the data model needs no redesign. The latency is **cron quantization on GitHub Actions**:
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
   exists server-side). **mmreality** — proxied, low-frequency only (cost).
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

**Deferred — W5b (health/SLO re-derivation):** cadence-scale the fixed thresholds
(`detail_queue_backlog` by oldest-row AGE not count — matview line ~301; `delisting_spike` as
% of portal size — line ~264), close silent-greens (image-pipeline liveness, dedup
dirty-banner staleness, drift no-baseline→warn, monitor-of-monitors on
`health_mv_refresh_stamp`), surface `worker_liveness` on the Health page, and push health reds
through the notification outbox. It's an intricate rewrite of the 396-line `scraper_health_checks`
matview (migration 214 — byte-for-byte jsonb contract) + frontend + outbox, so it needs its own
focused session, not a rushed one-shot.
