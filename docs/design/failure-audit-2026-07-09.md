# Production failure & alerting audit — root causes + implementation plan

> **RESCUE + STATUS UPDATE (2026-07-14).** This is a HISTORICAL 2026-07-09 audit snapshot,
> rescued into git (it had existed only in an untracked working copy). Do NOT treat it as
> current — re-verify before acting on any item. Known progress since it was written:
> **RC1** (Anthropic credit) — detection improved by the `llm_burn_rate` early-warning check
> (#739); prevention (auto-reload/spend cap) still operator-level and unbuilt.
> **RC2** (candidate-drain `make_interval(float)` crash) — **RESOLVED by #735** (float-safe
> backoff interval; the audit predates the fix and says "no fix PR exists").
> **RC7** (workflow-failure recorder gaps) — partially addressed by #738.
> RC3 (DB saturation), RC4 (cron throttle), RC5 (property-stats reconcile), RC6 (alerting
> unification) remain open / partially tracked in memories + roadmap — re-audit before acting.

**Date:** 2026-07-09. **Status:** analysis complete, implementation not started.
**Evidence base:** 17-agent verified audit (every root-cause claim below was independently
re-verified against primary evidence — GH run logs, `file:line` reads, live DB queries — and is
CONFIRMED unless explicitly marked otherwise). 112 failed Actions runs since 2026-07-02 across
18 workflows, plus all 13 `system_health` bell rows, were classified; nothing was left uncovered.

This document is the implementation spec. Each workstream lists concrete files, tables,
acceptance criteria, and validation steps. PRs are sized one-purpose each per CLAUDE.md.

---

## 1. Root causes (7, deduplicated across all failure classes)

### RC1 — Anthropic credit exhaustion (RESOLVED; second-order damage remains)
~42,800 failed LLM calls in three windows: **Jul 3 17:50 → Jul 5 12:54** (~39.5k), **Jul 7
09:10–21:38** (511 — NOT credit: the new Railway worker dedup lane ran ~12.5h without
`ANTHROPIC_API_KEY`), **Jul 8 04:11–05:58** (2.9k). Clean since Jul 8 07:00 UTC.
- **The architecture held: zero poison writes.** No failure path writes a terminal negative
  state (vision caches persist only successes; auto-dismiss is guarded by an all-rooms-verdicted
  check; the floor-plan gate defers on failure). Merge throughput dipped ~70% and fully self-healed.
- **Second-order damage (see WS3/WS5):** dedup review queue exploded ~390 → 15,218 proposed
  candidates; publication-gate latency p95 hit 62h (recovered to 13.3h, no stuck backlog).
- **Prevention is operator-level:** enable auto-reload + billing alerts on the Anthropic console
  (the mig-259 credit alarm detects, it cannot prevent).

### RC2 — `make_interval(hours => float)` crash: candidate-drain lane 100% dead since Jul 5  — ✅ RESOLVED by #735 (float-safe backoff interval)
PR #701 (merged 2026-07-05 13:10) added `_proposed_candidate_property_ids` executing
`... < now() - make_interval(hours => %(backoff_h)s)` at `scripts/dedup_engine.py:356-368` with a
**float** bind (setting `dedup_candidate_redecide_hours`, registered `type='float'` default 24 in
`toolkit/dedup_settings.py:62`). Postgres has no `make_interval(hours => double precision)` →
`UndefinedFunction` ~3s into **every** `--candidates` run for 4 days (roughly half to two-thirds
of the 44 failed dedup runs). `dedup_engine_runs` has zero `run_kind='candidates'` rows after
Jul 5 12:28. Still on origin/main; no fix PR exists. **This — not the credit outage — is the live
driver of the review-queue explosion** (queue grew 13,285 → 14,781 between Jul 8 14:46 and Jul 9
04:21, i.e. after credit was restored). The unmerged PR #665 SQL-correctness gate would have
caught this class at CI time.

### RC3 — Chronic DB saturation vs the 2-minute statement_timeout ceiling (CRITICAL, systemic)
Eight "different" failing workflows share one identical mode: `QueryCanceled` ("canceling
statement due to statement timeout") in eight different scripts. The server default
statement_timeout is 2min (confirmed via SHOW; service_role has no override) and many job
queries chronically run 10–110s — one notch below the ceiling — so any DB slowdown reds the
fleet at once. pg_cron's own ledger proves the DB was the bottleneck: browse_list 5-min rebuild
avg 27s→84s with up to 7/12 timeouts/hour; `refresh_health_matviews()` failing 70–100% of its
10-min runs. **Onset is chronic from Jul 7**, not a one-night episode (health refresh already
failed 25/144 on Jul 7). Standing-load producers that changed that day: browse_list 5-min full
rebuild (migs 276-278, deployed Jul 7 ~19:50), worker property-maintenance lane every 2min
(#716, merged Jul 7 20:47), plus the pre-existing 10-min health-matview refresh — all competing
full-scan producers on a 512MB-shared_buffers instance.
**Unproven attribution (validate before tuning):** which producer dominates is correlation-only;
Supabase instance CPU/disk-IO graphs for Jul 7–9 must be pulled (dashboard-only, operator
click-path) before choosing between slimming load vs upsizing the instance.
Also chronic near/over-ceiling statements flagged in `pg_stat_statements`: broker
contact-frequency rollup mean 354.6s (survives only under the full sweep's timeout lift),
`touch_listings` UPDATE mean 35.3s ×515 calls/day (index-walk path, NOT wrapped in
`run_resilient` — next slowdown reds index walks).

### RC4 — GH Actions cron throttle invalidated every cron-offset ordering assumption
The designed "property sweep 04:15 → dedup 04:45" ordering now runs head-on (throttle shifts
sub-hourly crons 1.5–4h). Three **unserialized bulk writers on `properties`** — property-stats
sweep (GH daily), city-proximity recompute (GH hourly), worker maintenance lane (every 2min) —
produced the first `DeadlockDetected` on Jul 9 08:04; only the first shares the
`sreality-property-maintenance` concurrency group. Cron offsets must be treated as decorative;
serialization must move in-DB (mig-279 lease CAS) or into the worker.

### RC5 — Property-stats daily full reconcile dead since Jul 3 (rule-#20 backstop absent)
Runtime grew 16m52s (Jul 2) → 28m34s (Jul 3) → over the workflow's `timeout-minutes: 30`
(Jul 4–7: killed as **"cancelled"**, which `scripts/record_workflow_failures.py:31`
`ALERT_CONCLUSIONS` excludes → four days of deaths invisible on the Health page), then Jul 8–9 a
single 2,000-property batch blew the 2-min statement_timeout (the script, unlike its sibling
`recompute_city_proximity.py:47`, never raises it). The sweep is O(all properties) = 497,757
rows / 249 batches, restarts at batch 1 every day (no cursor), so tail id-ranges may **never**
be reconciled under a hard cap. Measured drift so far: 83 properties `is_active=true` with zero
active children (falsely active in Browse/Watchdog/stats; filter: `merged_into IS NULL`).

### RC6 — Alerting is level-triggered over trailing windows with per-UTC-day dedupe
The bell (mig 274, `emit_system_alert` → `notification_dispatches`, dedupe `sys:{check}:{day}`)
has produced 13 system_health rows all-time, **0 ever seen** — the operator's real channel is
the GH red-run email from `check_llm_health.py` exit-1. Defects, each verified:
- **Stale re-alarms:** `check_llm_errors` (verify_pipeline, 24h trailing window) fired "every
  paid LLM path is down" **~22.4h after recovery** (Jul 9 04:21) and ~16h stale on Jul 6.
- **Same-day suppression of real incidents:** `sys:llm_errors:2026-07-08` was consumed at 03:50
  by a *stale* alarm (echoing the Jul 7 key incident); when the real Jul 8 credit burst hit
  04:00–05:59, three later verify_pipeline runs recorded `fail` with credit_errors=2866 but the
  bell was an ON-CONFLICT no-op. The stale detector *masked* the fresh outage.
- **Duplicate detection:** `check_llm_health` (hourly, 4h window) and verify_pipeline
  `check_llm_errors` (6-hourly, 24h window) detect the same provider failures under two bell keys.
- **Structurally-red checks:** `engine_health` fails on "no full-scan cycle within 30h" but the
  street cycle (cursor since mig 261) has NEVER completed — at ~2 effective runs/day vs ~12–14k
  groups a cycle takes ~2 weeks; the check has been red every day of its existence, and its
  message ("new cross-portal listings are not being de-duplicated on time") is false — new
  listings flow through the healthy worker dirty lane within minutes. `merge_latency` measures
  backlog age (p95 61 days) not engine latency → permanently amber.
- **No dependency suppression, no recovery events, no live-state predicate.**
- **`record_workflow_failures.py` doubly broken:** `LOOKBACK_MINUTES=40` vs actual 1.5–4h
  throttle gaps silently drops most red runs (13 liveness reds → 2 ledger rows), and
  `ALERT_CONCLUSIONS` excludes `cancelled` (RC5's four invisible days).
- **The inversion (the headline):** the week's only critical *systemic* incident — RC3 DB
  saturation — produced **zero** rows on any of ~10 health surfaces, while the already-resolved
  LLM outage rang ≥5 surfaces for 4 days. Two confirmed blind spots have no detector at all:
  DB health (pg_cron failure rate) and realtime-worker liveness (`worker_heartbeats` is written
  every ~30s and read by nothing).

### RC7 — Zombie machinery and phantom-shipped PRs
- **Vision A/B harness** (`validate_vision_models.yml` + `scripts/validate_vision_models.py`,
  dispatch-only): 18/18 lifetime runs red (by bug then by design). Bugs fixed+merged today
  (#726, #727). It then produced its first-ever verdicts: **Haiku@768 compare recall 20% → DO NOT
  ADOPT** (classify 96.5% pass) and **Sonnet@1568 recall 88.3% → DO NOT ADOPT** (run 29004960758,
  operator-dispatched 08:30 today — the harness is in ACTIVE use; do not delete yet, fix it: see
  WS0-4). Defects: exit-1 on a designed verdict pollutes the failure feed; `timeout-minutes: 30`
  cannot fit its own default inputs (200/40); recurring double-dispatch (all four June dispatches
  + today's 07:01 came in pairs seconds apart) with `cancel-in-progress: false`.
- **CLIP re-tag campaign** fully drained but still scheduled — every run ×4 shards burns a heavy
  anti-join scan to discover zero work, and reds when the DB is slow.
- **Condition scoring silently paused since Jun 18** (empty region list in app_settings): the
  workflow runs green (once red, inside the saturation window) while doing nothing. Coverage of
  new active byt inventory collapsed **74.8% → 6.7% scored** (55,995 of 60,037 first_seen ≥
  Jun 18 have NULL `apartment_condition_level`) — ~11k listings missing scores they'd normally
  get, silently dropped by rule-14 condition filters in Browse.
- **Actions :45 dedup dirty-drain cron** now largely redundant next to the worker dirty lane
  (459 runs/2d, queue 98 rows / 0.5h max age).
- **Dead code:** `worker_liveness` view (zero consumers), `recent_workflow_failures` RPC +
  `fetchRecentWorkflowFailures` (superseded by `workflow_failure_summary`).
- **Phantom-shipped PRs** (open on GitHub, memory/plans treat as landed): **#663**
  (db.connect handshake retry — `scraper/db.py:307` still has none, part of the fleet-red
  mechanism), **#665** (SQL-correctness CI gate — would have caught RC2), **#664** (geo
  progressive coverage — likely superseded by #713/#715, close explicitly). Plus stale branches
  (#648, #397, #254, 3 ancient `claude/*`).

### Noise (no action)
Jul-5 CI reds = dev-branch iteration merged green (#705); Jul-8 migration-smoke red = unguarded
`cron.schedule` in mig 282, fixed pre-merge in 2min (#721) — but the pg_cron guard foot-gun has
bitten twice → WS6. M&M Jul 6 = portal 403, self-healed. Jun-27 smoke red on main = Docker pull
infra flake (recovered from `gh run view` annotations after log expiry). Batch-warmup Jul 4 =
unretried Anthropic 502 at submit; script since rewritten by #725 (add submit retry only if the
warmer is re-enabled in WS3).

---

## 2. What is healthy — do not touch
- **Real-time dedup dirty lane (worker):** ~6,500 auto-merges/2d, queue fresh. The "engine
  stalled" alert is about the *backstop* full-scan, not real-time flow.
- **The dedup decision tree's failure posture:** outage wrote no false dismissals/merges —
  positive evidence the recall-locked design works.
- **CI + migration smoke on main:** green streaks; the branch reds are the safety net working.
- **Snapshots/history/delisting rails:** uninvolved in any failure class.

---

## 3. Workstreams

Ordering rationale: WS0 stops active bleeding (one-liners + unschedules); WS1 needs the operator
evidence pull first and unblocks the fleet; WS2/WS3 restore the two dead pipelines; WS4 is the
architecture piece and should land after WS1-3 so its new checks watch already-fixed systems;
WS5/WS6 are recovery + guards.

### WS0 — Immediate triage (day 1; 4 small PRs + 2 operator actions)

**WS0-1. Fix the candidate-drain crash (RC2).** `scripts/dedup_engine.py:356-368`: replace
`make_interval(hours => %(backoff_h)s)` with `(%(backoff_h)s * interval '1 hour')` (keeps
fractional-hour support; do NOT cast ::int). Add a regression test that PREPAREs or executes the
query against the real test schema (the `_FakeConn` gap is documented — an execution-path test
is required, not a string assert). Acceptance: next scheduled `--candidates` run completes;
`dedup_engine_runs` gains `run_kind='candidates'` rows; `property_identity_candidates`
status='proposed' count starts falling. The drain is O(due) with budgets (compare 100 /
floor-plan 300 per 2h run) — the 15.2k backlog drains over days; WS3-2 decides whether to blitz.

**WS0-2. Unschedule the drained CLIP re-tag campaign.** NULL/unset
`app_settings.clip_taxonomy_retag_after` (script exits clean at the "no campaign" guard) AND
comment out the cron in its workflow YAML (keep `workflow_dispatch` for the next taxonomy
change). Acceptance: no scheduled runs; zero shard scans.

**WS0-3. Surface the condition-scoring pause (decision gate → WS5-1).** Operator question:
was emptying the region list ~Jun 18 intentional cost control? Regardless: make pauses LOUD —
in the scoring script, when the region gate yields zero work because the region list is empty,
write a `pipeline_check_results` row `status='paused'` (see WS4-1 states) instead of silent
green. Do not re-enable regions without the operator's cost sign-off.

**WS0-4. Vision harness: fix, don't delete (it's in active use).**
`.github/workflows/validate_vision_models.yml` + `scripts/validate_vision_models.py`:
(a) exit 0 when the harness RAN correctly — the ADOPT / DO-NOT-ADOPT verdict goes in the job
summary (`$GITHUB_STEP_SUMMARY`) and log, not the exit code (a designed verdict is not a
workflow failure); keep exit-1 only for harness malfunctions (INCONCLUSIVE, crash).
(b) defaults → the proven 100 pairs / 30 min-class limits; `timeout-minutes: 90`.
(c) `concurrency: cancel-in-progress: true` (kills the double-dispatch waste).
(d) cap pairs-per-listing in sampling (verified correlated-sample bias: single listings dominate
the missed set). Record both verdicts (Haiku@768: 20% recall DO-NOT-ADOPT; Sonnet@1568: 88.3%
DO-NOT-ADOPT) in `roadmap/dedup` track + `docs/design/clip-visual-embeddings.md` in the same PR.
When the operator's model-migration program concludes, retire the workflow in a cleanup PR.

**WS0-5 (operator).** Anthropic console: enable auto-reload / billing alerts (RC1 prevention).
**WS0-6 (operator).** Pull Supabase CPU + disk-IO graphs for Jul 7 00:00 → Jul 9 06:00 (needed
by WS1-1; not readable via MCP). Click-path: Supabase dashboard → project → Reports → Database.

### WS1 — DB capacity & statement-timeout discipline (fixes RC3 + RC4 fleet-wide)

**WS1-1. Attribute the saturation before tuning (gate for WS1-3).** With WS0-6 graphs,
correlate CPU/IO against the three standing producers (browse_list 5-min rebuild, worker 2-min
maintenance lane, 10-min `refresh_health_matviews`) using `cron.job_run_details` timings and
worker heartbeat timestamps. Decision output: slim which producer(s), and/or upsize the instance.
Do not skip: four dimensions' fixes each targeted a *different unproven suspect*.

**WS1-2. One shared job-session helper; kill bespoke connection blocks.** Add to `scraper/db.py`
(or `toolkit/db_jobs.py`) a single `job_connect(db_url, *, statement_timeout: str, ...)` (or
context manager) that: connects with `prepare_threshold=None`, sets a per-session
`statement_timeout` sized for batch analytics (explicit per caller, e.g. '10min'/'20min'),
and wraps in the existing `run_resilient` retry-on-OperationalError semantics where idempotent.
Adopt in (each currently near/over ceiling on a bare 2-min connection):
- `scripts/dedup_engine.py:3105` (engine session; also see WS3-4 keyset option),
- broker incremental `_run_incremental` — wrap `_IDENTITY_ROLLUP`/`_BROKER_ROLLUP` with
  `SET LOCAL statement_timeout` exactly like the #661 full-sweep fix (bounded, e.g. 10min),
- iDNES/RealityMix drain `native_ids_with_geom` preload — route through `run_resilient` (pure
  SELECT, idempotent) and scope the scan (only ids in the claimed batch, or a per-source partial
  index on `geom IS NOT NULL`),
- one-shot jobs: MF yields, stale-image-URLs, bazos enrichment, condition propagate,
  city-proximity (already sets 20min at line 47 — migrate to the helper for uniformity),
- `touch_listings` bulk UPDATE (mean 35.3s — wire through resilience before it crosses 2min).
Rule to encode in CLAUDE.md afterwards: **no batch job runs analytics statements on a bare
default-timeout connection; all go through the helper.**

**WS1-3. Slim the standing load (informed by WS1-1).** Candidates, in expected-yield order:
- `refresh_health_matviews()` (the #1 DB consumer, 70–100% timeout rate — currently wasted
  work amplifying the slowdown): make single-pass/cheaper (extend #547), lower cadence to
  30–60min via pg_cron reschedule, raise its internal timeout so a run *completes* instead of
  burning-and-discarding. NOTE: it feeds the live /health page (mig 219 + `Health.tsx`) — it is
  NOT deletable unless the health surface is redesigned (WS4 may subsume parts of it; decide
  there, not here).
- browse_list 5-min rebuild: give its pg_cron session a raised statement_timeout (internal job,
  not an anon query); if WS1-1 fingers it, make it dirty-driven/incremental or lengthen cadence
  (documented worst-case staleness 10-15min). Its failures cluster inside dedup-geo windows —
  re-measure after WS1-4.
- Stagger pg_cron schedules away from each other and from the 04:15 sweep window.

**WS1-4. Serialize the three bulk `properties` writers (RC4).** Extend the mig-279
`property_maintenance_lease` CAS to cover: the dedup engine's properties-stamping/merge phase,
the city-proximity bulk UPDATE, and the property-stats sweep batches (worker lane already holds
it). Cron offsets are decorative — delete the YAML header comments claiming ordering. The Jul 9
deadlock is the first symptom of this class; expect recurrence on any unserialized pair.
Alternative for city-proximity: fold its incremental fill into the worker's
`run_incremental_pass` (already lease-serialized), retiring the hourly GH workflow and keeping
`--full` dispatch-only (population/city-index loads only). Prefer this — one less throttled cron.

### WS2 — Property-stats full reconcile rebuild (RC5)

**WS2-1. Make the sweep resumable and timeout-safe.** `scripts/recompute_property_stats.py`
(the daily `--full` path): (a) use the WS1-2 helper with an explicit statement_timeout;
(b) persist a sweep cursor (`app_settings` key or a one-row state table) after each committed
batch so a killed run resumes instead of restarting at id 1; (c) per-batch INFO logging (current
LOG.debug is invisible in Actions); (d) either raise `timeout-minutes` to fit measured runtime
+50% or — preferred — move the full reconcile into the realtime worker as a low-frequency lane
(no 30-min wall, no cron throttle, lease already held), keeping the GH workflow dispatch-only.
Acceptance: one full sweep completes end-to-end; the 83 falsely-active properties get corrected
by `_reconcile_childless` + the sweep tail; `_CLEAR_DIRTY_SQL` executes.

**WS2-2. Backstop assertion.** After the first successful sweep, add the drift metric to
verify_pipeline (WS4): count of active properties with zero active children — gauge, warn >0
sustained. This is the direct measure of "the rule-#20 backstop is working".

### WS3 — Dedup throughput + review-queue remediation

**WS3-1. Geo lane economics (root of the queue explosion).** Verified mechanism: the geo cron
branch (`dedup_engine.yml:219`) passes NO `--compare-budget` → every geo run since Jul 5 spent
$0.00 with 0–24 vision calls; unresolved pairs become `visual_inconclusive` review cards
(14,179 since Jul 4) — the queue grows ~800–2,000/day even on healthy days, and after #715 the
growth driver shifted to the worker dirty lane (1,642 queued by 08:45 Jul 9). Fix policy, not
symptoms: geo pairs that exhaust budget should **DEFER** (stay dirty / re-scan later) like the
street lane's free posture — not enqueue operator review cards; give the geo lane an explicit
paid budget consistent with the funnel design; consider enabling the #725 targeted batch warmer
(`dedup_batch_warmer_enabled` currently false) — if so, first add submit-retry to the batch
submit path (`api/providers/anthropic.py:134` flush is unretried; Jul 4 502 killed a run).

**WS3-2. One-off backlog remediation (operator cost decision).** The 15.2k proposed cards are
not reviewable by hand. Options: (a) bulk re-queue the Jul 4–9 `visual_inconclusive` geo cohort
(~14.8k) back to the engine for re-decision now that credit is restored — prefer batch API
(50% cost); (b) let the fixed candidate drain (WS0-1) chew it at 100 compares/2h (~weeks).
Recommend (a) for the cohort, (b) for the residue. Requires a spend estimate before dispatch.

**WS3-3. Retire the Actions :45 dirty-drain cron.** Worker owns the dirty lane. Before deleting:
confirm the worker lane's paid budgets (compare 40 / floor-plan 25) cover what the cron's did;
keep `--dirty` reachable via `workflow_dispatch` as manual fallback; optionally gate a fallback
cron on `worker_heartbeats` staleness (but WS4-3 adds the proper worker-down alert).

**WS3-4. Full-scan throughput + cycle semantics.** The street cycle takes ~2 weeks at current
throughput (~2 effective GH runs/day × 385–900 groups vs ~12–14k groups); geo is ~a year
(post-restore pace 87–105 groups/run — outage-era paces were 30× inflated by cache-only
scanning; do not size from them). Substantive fix: move street+geo full scans onto the worker
(budget-bounded lane residency, escapes the GH throttle) — per the established worker-lane
pattern; order the geo scan by expected yield (cells containing fresh cross-portal listings
first) so debt that matters drains first. Also make `_load_eligible`/`_load_geo_eligible`
keyset-paginate from `dedup_scan_state.cursor_key` instead of SELECT+ORDER over the whole market
every run (kills the 124s timeout class at its root; WS1-2's raised timeout is the stopgap).
Define what a "cycle" means for the 92k-cell geo space BEFORE wiring any alert to it (WS4-2).
Split lane naming in workflow runs (name runs by lane) so failure counts stop conflating lanes.

### WS4 — Alerting unification (RC6) — one state store, transition-based, dependency-aware

Design principles (industry-standard alerting semantics, scoped to a single-operator platform):
alert on **state transitions** with hysteresis, keep **one source of truth for check state**,
separate **detection cadence** from **notification policy**, **auto-resolve**, **suppress
downstream** alerts while a root alert is active, and **every scheduled pipeline must prove
"did work OR knows why not"**.

**WS4-1. Single check-state store + transition emitter.** `pipeline_check_results` (exists,
history per check_key) becomes the SoT. New small module (e.g. `toolkit/alerting.py`):
after verify_pipeline writes its rows, compare the latest two rows per check_key —
- `ok→fail`: emit ONE bell row, `dedupe_key = sys:{check}:{incident_start_iso}` (replaces the
  per-UTC-day key — fixes both the daily re-alarm AND the same-day-suppression bug);
- `fail→ok`: emit one informational recovery row (auto-resolves the incident in the feed);
- continuous red: re-ring only as deliberate escalation after N days (config, default 3);
- add `status='paused'` as a first-class state (WS0-3) rendered distinctly.
Migration: extend the check-state table only if needed (status enum); no new alert tables.
SPA: render system_health bell rows with live status by joining `pipeline_checks_public` on
check_key (no schema change); the feed then answers "is this still happening?".

**WS4-2. One detector per failure domain; recovery-aware predicates.**
- **LLM provider outage:** owned by the hourly liveness probe ONLY. Fold `check_llm_health.py`'s
  probes (pending-gated liveness + credit detection — its silent-death checks are unique, keep
  them) into verify_pipeline as checks, run hourly via the existing llm_health.yml as
  `verify_pipeline --only llm_liveness`, keeping the intentional **exit-1 GH email** (the one
  channel the operator demonstrably reads; belt-and-braces for when the DB/bell path is broken).
  Delete `scripts/check_llm_health.py` after folding. Predicate must be **live-state**: alarm
  only when `max(called_at) FILTER (error)` > `max(called_at) FILTER (success)` (healthy traffic
  auto-clears within minutes) — never a bare trailing-window count. Demote verify_pipeline's
  `check_llm_errors` to a metrics-only state row (no bell) — its 24h window caused every stale
  alarm; note the Jul-6 nuance: intermittent outages must still be caught, which the hourly
  live-state probe does by cadence, not by window width.
- **engine_health re-spec:** split "stalled" from "slow". Fail = cursor stagnation
  (`dedup_scan_state.updated_at` stale > Xh, or `scan_groups_scanned` ≈ 0 over trailing 24-48h)
  — this also catches the real 12h street-cursor stall the old check accidentally overlapped;
  warn (no bell) = cycle older than a threshold derived from designed cycle time (days), with a
  cycle-ETA gauge. Fix the message: the market-wide BACKSTOP is behind; real-time dedup is
  unaffected. Geo gets its own check only after WS3-4 defines geo-cycle semantics.
- **merge_latency:** restrict the sample to pairs first seen inside the window; keep backlog age
  as a separate value-only gauge (stops the permanent amber).
- **Dependency suppression:** evaluate the provider check first; while red, downstream checks
  (engine cycle, condition liveness, per-tool error rates) still write state rows but with a
  `suppressed_by` marker and no bell. Note: engine_health is NOT downstream of the credit check
  by default (its red predates and outlives outages) — suppression keys must be explicit per
  check, not blanket.
**WS4-3. Two new detectors for the confirmed blind spots** (without these the redesign only
de-noises; the real incidents stay dark):
- **DB health:** failure rate over `cron.job_run_details` (e.g. >30% timeouts across pg_cron
  jobs in 1h) and/or fleet QueryCanceled count — this is the check that would have caught RC3
  on Jul 7 instead of surfacing as 10 unrelated red workflows.
- **Worker liveness:** `worker_heartbeats` staleness (>5min = fail) — currently written every
  30s, read by nothing; the worker owns all latency-critical loops. Include per-lane env
  preflight: a paid lane running without `ANTHROPIC_API_KEY` heartbeats a distinct degraded
  state (the Jul-7 12.5h keyless incident becomes a 5-minute alarm).
- **Work-done gauges** ("runs-green-but-does-nothing" class): every scheduled pipeline records
  rows-processed; zero-work runs against nonzero pending → warn (catches the condition-scoring
  pause, drained campaigns, silent-green image monitor — the old memory item).

**WS4-4. Fix or fold `record_workflow_failures.py`.** One PR: replace `LOOKBACK_MINUTES=40`
with a high-water-mark cursor (last processed run completion timestamp in `app_settings`) and
add `cancelled` to `ALERT_CONCLUSIONS` (timeout-minutes kills currently vanish). Keep it as the
Health-page workflow feed; it is not an alerting path (WS4-1 owns alerting).

**WS4-5. Removals (after WS4-1/2 land):** `check_llm_health.py` (folded), verify_pipeline's
`check_llm_errors` bell path (metrics-only), `worker_liveness` view (replaced by the real
worker check), `recent_workflow_failures` RPC + `fetchRecentWorkflowFailures` (unused,
superseded by `workflow_failure_summary`). Document in the PR: exactly one check keeps the
exit-1 email channel (the hourly liveness probe); verify_pipeline is bell-only by policy.

### WS5 — Data-quality recovery (data-science lens)

**WS5-1. Condition-scoring restart + backfill (needs WS0-3 operator decision).** If re-enabled:
size the backfill against the region gate, not "everything" (baseline coverage was 74.8%, never
~100%). Cohort: ~56k unscored active byt first_seen ≥ Jun 18; batch API (the existing
`condition scoring (batch API)` path) at Haiku pricing. Deliver a cost estimate before running.
Add the WS4-3 work-done gauge so a future pause is visible within a day.

**WS5-2. Bazos enrichment: backlog truth + selection cost.** REFUTED claim to not build on:
"enrichment is stalled" — it took ONE timeout (inside the 04:15 contention window). The real
finding: the backlog **never drained** — 16,583 pending vs ~2k ok/day (cost-cap $20/run), a
permanent multi-week lag. Two decisions: (a) operator: raise the per-run cost cap temporarily
to drain, or accept the lag; (b) engineering (only if the timeout recurs): make pending-selection
O(pending) — stamp a latest-snapshot-enriched flag at write time (the mig-105/106 dirty-set
pattern) or store `latest_snapshot_id` on listings to kill the correlated MAX anti-join.

**WS5-3. Size the duplicate double-counting skew (one query, before trusting stats).** The
15.2k review backlog + never-completed street/geo cycles mean unmerged cross-portal duplicates
inflate Browse counts, price stats, and MF-yield golden records. Run one SELECT: proposed pairs
where both properties are active & published, grouped by category/surface; report the skew % on
browse_stats and MF yields. If material (>1-2% on a used surface), note it on the affected
surfaces until WS3-2 drains the backlog. Add `publication-latency p95` as a gauge (check:
warn > 24h) — it measured 62h during the outage and is coupled to candidate-drain health.

**WS5-4. Debt gauges stay value-only until throughput exists.** street_debt 30,488 /
geo_debt 97,651 suspect pairs: do NOT wire alerts to these before WS3-4 raises throughput —
an alert nobody can action is the RC6 pattern again.

### WS6 — Process guards & repo hygiene

**WS6-1. Merge the phantom PRs (or close explicitly).** #665 SQL-correctness gate: rebase,
verify green with `TEST_DATABASE_URL`, merge — it PREPAREs discovered SQL against the replayed
schema and catches the RC2 class in CI. #663 db.connect handshake retry: rebase+merge —
`scraper/db.py:307` still crashes any job on a pooler handshake blip before `run_resilient`
engages (part of the fleet-red mechanism). #664: close as superseded by #713/#715 with a
comment. Sweep #648/#397/#254 + 3 `claude/*` branches: close-or-supersede. Then correct the
memory files that mark these shipped.

**WS6-2. pg_cron migration guard.** The unguarded `cron.schedule` foot-gun bit twice (Jun 1,
Jul 8). Add to the migration smoke-test a static check: any migration containing `cron.schedule`
must wrap it in the established mig-136 guard pattern (`IF EXISTS (SELECT FROM pg_extension
WHERE extname='pg_cron')`). Fail the smoke-test with a pointed message otherwise.

**WS6-3. CLAUDE.md/docs updates (same PRs as the work):** the WS1-2 "no bare-connection batch
jobs" rule; "cron offsets are decorative — serialize in-DB" under rule 20/21 context; alerting
policy one-pager (one email channel, bell is transition-based) in `docs/architecture.md`.

---

## 4. Removals ledger (explicit, per operator mandate)
| Item | Action | When |
|---|---|---|
| CLIP re-tag campaign schedule | unschedule (keep dispatch) | WS0-2 |
| Actions :45 dedup dirty cron | delete after budget parity check | WS3-3 |
| `scripts/check_llm_health.py` | fold into verify_pipeline, delete | WS4-2 |
| verify_pipeline `check_llm_errors` bell | demote to metrics-only | WS4-2 |
| `worker_liveness` view | drop (replaced by real worker check) | WS4-5 |
| `recent_workflow_failures` RPC + frontend fetch | drop (unused) | WS4-5 |
| city-proximity hourly GH workflow | retire if folded into worker lane | WS1-4 |
| vision A/B harness | retire only when the model-migration program concludes | post-WS0-4 |
| PR #664 + stale branches | close explicitly | WS6-1 |

## 5. Operator decisions needed (blocking the marked items)
1. **Condition scoring** (WS0-3/WS5-1): was the Jun-18 pause intentional? Re-enable scope +
   backfill budget (~56k listings, Haiku batch pricing — estimate before running).
2. **DB capacity** (WS1-1/WS1-3): after the graphs — slim load vs upsize the Supabase instance.
3. **Review-queue blitz** (WS3-2): approve batch-API spend to re-decide the ~14.8k cohort.
4. **Bazos enrichment** (WS5-2): temporarily raise the $20/run cost cap to drain 16.6k backlog?
5. **Anthropic billing** (WS0-5): auto-reload on.

## 6. Assumption-validation notes (things this audit corrected — do not rebuild on them)
- "Bazos enrichment is stalled" — REFUTED (single timeout; the real issue is the never-drained
  cost-capped backlog).
- "Browse_list rebuild caused the timeout spike" — UNPROVEN; #716 worker lane merged 1h later is
  an equal suspect; WS1-1 gates any tuning.
- "The dedup engine is stalled" — FALSE as stated (real-time lane healthy); but the street
  cursor DID stall for 12h (Jul 8 20:36 →) — WS4-2's stagnation predicate covers the true part.
- "Geo cycle ≈ months" — optimistic; ~a year at post-restore pace (outage paces were cache-only
  inflated 30×).
- Memory files claiming #663/#664/#665 shipped — wrong; they are open PRs (WS6-1 corrects).
- `engine_health` red ≠ outage fan-out — it predates and outlives the credit windows; do not
  blanket-suppress it under the provider check.
- The 83 falsely-active properties reproduce only with `merged_into IS NULL` (121 without).
- Jul-7 LLM window was a missing `ANTHROPIC_API_KEY` on the Railway worker, not credit; why the
  key was absent for ~12.5h is still unestablished (check Railway env change history) — the
  WS4-3 preflight makes recurrence a 5-minute alarm either way.
