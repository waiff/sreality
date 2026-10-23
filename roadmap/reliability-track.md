# Reliability program (applied guarantees, one voice for failure)

The reliability rebuild designed after the 2026-08-26 six-portal ingest outage. Full
detail — root-cause chain, design principles, per-wave PR breakdown, the explicit
out-of-scope list and the eight open questions for the operator — lives in
[`docs/design/reliability-program.md`](../docs/design/reliability-program.md). This
track records sequencing + shipped state only.

**North Star.** Two guarantees: (1) **every merged migration is a live, applied
guarantee** — a migration that merged but did not land must be loud within an hour, and
the oracle is the Postgres catalog, never the applied-migrations ledger and never a
fresh-container CI replay; (2) **every fleet-wide failure surfaces once, to one place,
with its root cause attached** — one event table, one bell namespace, one escalation
policy, extended and never duplicated. The constraint on *how*: build on the existing
shared framework (`portal_runner`, `verify_pipeline.py`'s `CHECKS`,
`notification_dispatches`/`channel_sends`, `migrations/`) — two tables and zero
dependencies across the whole program.

## What happened (2026-08-26)

Between ~20:00 and ~21:30 UTC six of nine portal scrapers (mmreality, ceskereality,
maxima, bezrealitky, realitymix, remax) began failing every run on
`CheckViolation: … "listings_area_basis_check"`. Migration 423's stale-guard
(`if not exists (select 1 from pg_constraint where conname = …)`) saw a pre-existing
four-token constraint of that name on `listings`, skipped, and left the table on the
wrong version while `properties` got the correct five-token one from the same file.
Migration 438 was written to fix exactly that drift — it merged 2026-08-25 17:12 UTC and
was applied 2026-08-26 22:06 UTC, **29 hours later, by the session investigating the
outage**, because migration apply and PR merge are wholly uncoupled and four independent
monitoring gaps kept the failure invisible (`scrape_runs.errors = 0` on all six portals).

## 2026-08-31 addendum

The original incident is closed, verified with data (`area_basis='plot'` 0 → 344+; one
transient portal failure in four days; OpenAI topped up, LLM pipeline healthy). Two NEW
fires filled the inbox since — full analysis in the design doc's addendum:
**(a)** the location claim-intake lane dead since 2026-08-27 (PR #1209 edited prose
inside the ceskereality contract's governed hash without a version bump; no CI gate
exists over contract immutability → W0.5a: v4 bump + lockfile rail in `test.yml`), and
**(b)** hourly acute-health false-reds from `llm_liveness`'s 4h threshold, calibrated to
the dedup-vision workload deleted 2026-08-06 (→ W0.5b: 13h default, folded into W0.3's
PR). Neither is a portal-scraper defect — the fleet is green; W1 stays queued but is not
the urgent lane. Both fires are the program's thesis in new domains: a repo-declared
truth enforced only in production, and N identical emails for one cause.

## Wave status

| Wave | Scope | Status |
| --- | --- | --- |
| W0 stop the silent losses | Four one-file fixes, each currently *discarding* information: poller cursor loss + a poller-liveness check (W0.1), honest crash accounting at the shared `portal_runner` seam (W0.2), LLM check keyed on state not recency + a per-`called_for` starvation arm (W0.3), graceful degradation of the acute health lane under its 120s budget (W0.4) | 🟢 shipped 2026-08/09; its open follow-ups superseded by the Health dashboard sprint (2026-10-18) |
| W0.5 the second week's fires | (a) ceskereality contract v4 + contract-immutability lockfile rail in `test.yml` — un-wedges the intake lane, closes the merged≠enforced gap for portal contracts; (b) `llm_liveness` recalibrated 4h → 13h to the real (post-dedup) producer cadence | 🟡 in progress (2026-08-31, shipped as hotfix PRs ahead of wave order — the lane was down) |
| W1 survivable write path | Non-transient errors at `_flush_drain_batch` become a bounded, loud quarantine (per-item replay → existing `_drain_record_failure` → `given_up` at 5) behind a circuit breaker; `write_rejects` counter + a `drain_write_rejects` check (`ingest_freshness` largely covered in parallel by the walk sprint's `acquisition_lag` + `walk_coverage` checks, rebuilt in the Health dashboard sprint's wave 4 as the `scraping` and `publishing` checks and the `coverage` gauge) | ⚪ proposed — needs operator sign-off |
| W2 migration file as contract | `-- apply:` / `-- assert:` header tokens above a watermark (≥ 444), asserts executed inside `migrations.yml`'s existing per-file apply loop and *validated as able to fail*, plus schema-vocabulary parity and the hot-DDL doctrine rails. **W2.3's live-catalog drift check shipped in parallel as PR #1204 (`check_migration_drift`)** — remaining W2.3 scope is hardening it, not building it | ⚪ proposed — needs operator sign-off (part shipped in parallel) |
| W3 one alert, one place, cause attached | Failure signatures produced in-process at the write chokepoint (`checkviolation\|listings_area_basis_check` on all six portals), an `ops_incidents` table that exits through the **existing** `system_health` spine (no parallel channel), and the re-escalation ladder placed in `toolkit/system_alerts` where all 19 checks inherit it | 🟢 shipped 2026-08-31 — W3.1–W3.3 (migs 462+463, PR #1247) + W3.4's ladder, flap cooldown and weekly heartbeat (PR #1246); one residue, closed 2026-10-21: external alert routing (`system_health_channels` / `notification_email_to`) is deleted, and system alerts live on Health with the canary e-mail as the external alarm (ruling R2b, notifications push W1). W3.4's ladder was replaced 2026-10-18 by the Health dashboard sprint's wave 1, and the signature engine retired 2026-10-19 in its wave 3 (migration 621) |
| W4 apply becomes a job | Hot-table DDL applied out-of-band by a tested, extracted lock primitive (`attempts=1`, fail fast, escalate to a window — the retry-loop pattern is retired, not tuned); the existing PK-swap applier generalised into a dispatchable maintenance window | ⚪ proposed — needs operator sign-off + one human step (`REALTIME_WORKER_ENABLED`) |
| W5 constraint-only catalog diff | Named successor: diff CI's replayed schema against the live catalog. Catches 438 exactly, plus the ~241 migrations with no ledger row and no assert, plus the reverse drift direction W2 structurally cannot see — with zero per-migration convention | ⚪ named only, not built (open question 8) |

## W0 — shipped 2026-08/09; its open follow-ups superseded by the Health dashboard sprint (2026-10-18)

Approved 2026-08-27. Grouped into three PRs rather than the design doc's four, because
W0.1/W0.3/W0.4 all touch `scripts/verify_pipeline.py`'s check registry and stacking them
separately would only create merge-order pain:

- **Docs + track** — this file, `docs/design/reliability-program.md`, the index row.
- **W0.2 honest crash accounting** — `_run_phase`/`_finalize` lifted out of all nine
  `*_main.py` into `portal_runner` (rule #21: one shared framework, no per-portal
  special-casing), and the exception path stamps a non-zero error state instead of
  finalizing green. Nine copies → one; nine bugs → zero.
- **W0.1 + W0.3 + W0.4 — "make the health-check surface tell the truth"** — the poller
  keeps its cursor when page-capped and gains a `workflow_poller_liveness` check;
  `currently_failing` is derived from `last_ok_at < last_err_at` instead of a 90-minute
  staleness window; `llm_burn_rate` gains a per-`called_for` starvation arm; the acute
  lane persists each check as it completes under per-check and total wall-clock budgets.
  (The per-check budget bound each statement, not the check, until the Health dashboard
  sprint's wave 2 made it a deadline on 2026-10-18; that closes W0.4.)

**Risk carried into the wave:** ~~W0.3 reds the acute lane until the OpenAI account is
topped up~~ — **resolved 2026-08-30: credits topped up, pipeline verified healthy** (595
ok / 0 err calls in 24h). The remaining W0 risk is rebase drift: the walk-coverage
sprint (merged 2026-08-27..30) rewrote parts of `idnes_main.py`, `ceskereality_main.py`
and `portal_runner.py` under W0.2's feet; both W0 code PRs are being rebased onto it.

## W3 — W3.1–W3.3 shipped (2026-08-31)

Built on the corpus the design doc asked for first: 554 real failures over 14 days,
36 distinct signatures, so every threshold below is measured rather than guessed.

- **W3.1 signatures** — `scripts/failure_signature.py`, pure stdlib. The key comes from
  the error TEXT and never from `workflow_path`; the one shape that IS scoped by
  workflow is the unreadable-red fallback (unscoped, `step:|exit:1` merged 13 runs
  across 10 unrelated workflows into one meaningless mega-incident). Producers:
  `portal_runner._record_run_crash` (in-process, t+0, zero API cost) **and** the two
  `realtime_worker` lanes, which bypass `run_phase` entirely and had no failure record
  of any kind — both through one `record_failure_signature` seam, not two producers.
- **W3.2 `ops_incidents`** (migration 462) — one OPEN row per signature behind a partial
  unique index. Onset at `failure_count >= 2` **or** 2 distinct workflows, whichever
  first (the breadth arm is measurably faster: 8 min to the 2nd workflow vs up to 164
  min to a 2nd same-workflow failure under the Actions throttle). Closes on member-
  workflow success, at 168 h, or manually.
- **Review fixes, same wave (migration 463).** Two producers over one counter needed a
  correlation key and had none: the chokepoint recorded a portal crash at t+0 and the
  poller recorded the SAME concluded run hours later, so `failure_count` was ~2x reality
  and a lone crash crossed the measured onset threshold by itself. `ops_incident_runs`
  makes one Actions run at most one failure, and the poller claims before it downloads —
  which also replaces the earlier workflow-keyed signature reuse, whose lack of run
  correlation folded unrelated new reds into whatever incident last touched that workflow.
  With it: the onset claim and its emit became one transaction (autocommit connections
  were committing the claim alone, so a failed emit silenced that incident's only alert
  forever), the incident pass moved after the cursor write and gained a wall-clock budget,
  and the worker's recorder moved onto `asyncio.to_thread` — it was the one blocking DB
  call left on the event loop, worst exactly during a DB outage.
- **W3.3 exits through the existing spine** — one `system_health`
  `notification_dispatches` row per incident. **No new alert table, no parallel
  channel.** The outbox RETRY allowlist bug is fixed with it, so an ops alert now gets
  the same five delivery attempts a price drop gets instead of one.

**Corrections to the design doc, from the measurement pass.** The "~100 failures/day"
baseline counts cancellations; true `failure` volume is **39.8/day** and cancels are
concurrency-group evictions carrying no error (excluded). Portal workflows are only
**22%** of the corpus, so the log backstop is the majority path, not an optional extra.
The `listings_area_basis_check` cluster spans **8** workflows (iDNES included) and its
onset was **2026-08-24 23:09 UTC**, ~45 h earlier than the doc states. Migration 220's
`consecutive_failures`/`is_chronic` are computed over GitHub workflow runs, not
`pipeline_check_results`, so W3.4's ladder cannot consume them.

**Not flipped, on purpose:** `system_health_channels` was still `[]`: incidents rang the
in-app bell only, by ruling Q1 2026-10-18. *Superseded 2026-10-21 by ruling R2b (notifications
push W1, `docs/design/notifications-push/PROGRAM.md`): system alerts leave the bell, the readiness
is deleted, and Health + the canary e-mail are the only paths.*

## Open questions blocking W1–W4

Eight in the design doc; question 1 (OpenAI top-up) is **resolved**. Still load-bearing
for sequencing: **branch protection — `main` is currently not protected, so every CI
rail W2 adds AND the new W0.5a contract lockfile rail are advisory until that changes**,
volume tolerance (W3; alert routing was closed 2026-10-21 by ruling R2b: no routing, Health +
the canary e-mail), the two-human-step Railway procedure (W4), and
the disposition of migrations 433 and 434.

## W3.4 — shipped 2026-08-31

*Replaced 2026-10-18 by wave 1 of the Health dashboard sprint (section below): one incident per
failing cell, told by the API, instead of a ladder re-derived from 30 days of results.*

The escalation ladder, landed on its own ahead of the rest of W3 because it needs no new
table, no migration and no routing decision — it changes only *when* the existing
`system_health` bell rings.

- **The unit of alerting is an incident, not an edge.** One incident opens at the first
  `fail` and survives flaps inside a 6h cooldown; every dedupe key hangs off its onset
  timestamp, so `ON CONFLICT` gives exactly-once across both lanes with no new state.
- **6h / 24h / 72h / weekly rungs**, highest-due-only, so a six-day red says so four
  times instead of once (`property_maintenance`, 2026-08-20..26) and a six-day-old
  incident discovered on deploy emits one alert, not five.
- **Flap damping both ways**: a return to red inside the cooldown re-enters the same
  incident, and a recovery is announced only once the check has held non-fail for the
  cooldown — `llm_errors`' 114 alternating alerts collapse to one onset + one recovery.
- **A weekly heartbeat on the existing (empty) `--weekly` lane**, keyed per ISO week
  because the workflow appends `--weekly` to all four of Monday's runs. It is the half
  of the dead-man switch migration 274's in-DB `emit_verification_stale_alert` cannot
  cover: a harness that runs while delivery is silently broken.
- Policy is five scalar `pipeline_check_thresholds` keys (`load_thresholds` drops
  non-scalars, so an array would have been ignored undetectably); code defaults ship
  live, no migration.
- **Review fix — the anchor cannot be the history window's edge.** `check_states` reads
  30 days, so a check red for longer than that had every in-window row a `fail` and took
  the oldest surviving row as its onset — a value that advances one cadence per run as
  rows age out. Every dedupe key hangs off that anchor, so the ladder would have
  re-emitted **onset every run, forever** on an incident older than a month: the flood
  it exists to remove, in a new shape, and strictly worse than main's silence.
  `onset_truncated` now flags a streak that runs off the window edge and one extra query
  pins its real onset.

**Not** wired: migration 220's `consecutive_failures`/`is_chronic`, which the design doc
named. They are computed inside `workflow_failure_summary(int)` over `workflow_failures`
— the GitHub workflow-run domain, not `pipeline_check_results`. The pipeline-check streak
is read from its own table; the two domains stay separate.

## 2026-09-30 — the realtime worker cannot freeze silently (shipped with this PR)

The worker went silent 2026-09-29 23:42 → 07:22 UTC without exiting: ceskereality 403s pushed
the shared rate ledger an hour ahead, abandoned probe/drain passes leaked a thread each into the
default executor until it filled, and the heartbeat starved behind them while the loop lived on.
Fixed in one PR: a watchdog thread that `os._exit(1)`s after 300 s without a finished beat or a
pass lock held over 1 h (the heartbeat stays on the shared, now pinned, executor as its canary;
a `faulthandler` exit timer covers a stack dump stuck on a full stderr); pass locks on probe + drain,
all seven locks through one `_PassLock` whose age keeps a wedged lane in `in_flight_s`; the
worker's probe, drain and sold_comps refuse (never park on) a ledger wait over 120 s, and its
drain refuses past its deadline, while the Actions walks and drains stay unbounded; the retry
after a 403 leases one slot and a drain's lease is sized to its claims. Detail: `docs/design/realtime-scrapers.md` § Liveness.
- **Next:** a per-portal circuit breaker (stop re-probing a portal that 403s everything), Health
  alerts on `budget_refused` / `portal_rate_state.next_slot_at` running ahead, and a standing
  `faulthandler` timer for a GIL-holding stall the watchdog thread cannot see.

## 2026-10-10 — a text-lane pass the provider refused is a failed pass, and the checks name the provider (shipped with this PR)

OpenAI ran out of credit on 2026-10-02; the alarms fired but pointed the wrong way (no provider or model named, Anthropic jobs listed as down, the heartbeat booking every refused pass as a success) and nothing left the app (`system_health_channels` is `[]`). Fixed here: the refused pass is a failed pass; `text_extraction_lag` fails on the provider's refusal and names the model; `llm_errors` judges credit per provider and names the provider and model.

Next:
- ~~`verify_pipeline`: a timed-out warn counts as "not failing", closes the incident and sends a false "Recovered" (`text_extraction_lag` did it 6 times since 10-02); a timeout must keep the incident open.~~ Done 2026-10-18 (Health dashboard sprint wave 1: a timeout is `unknown`).
- ~~`llm_errors` rate arm: `currently_failing` still reads the newest call of ANY provider; for ~24 h after a top-up a stray error can turn the check red again. → Health dashboard sprint wave 3 (the llm check).~~ Done 2026-10-19 (wave 3: the `llm` check judges the error share per provider).
- ~~`worker_lane_stall` reports `failed_passes` but never alarms on it. → wave 3 (the jobs check's lane cells).~~ Done 2026-10-19 (wave 3: a lane cell judges failing spells and flapping).
- Delivery: `system_health_channels` is `[]` and no transport is configured, so no system alert has ever left the app (operator: email key + recipient + channel). → Q1 ruled 2026-10-18: bell only for now; readiness documented in Health dashboard sprint wave 7 (2026-10-19); enabling = the three steps, at the operator's word. *Superseded 2026-10-21 (R2b): Health + the canary e-mail only, the readiness deleted.*

## 2026-10-18 — Health dashboard sprint, wave 1 (shipped with this PR)

The Health page and its alarms are rebuilt as one sprint of ten waves, one draft PR each, planned
in [`docs/design/health-dashboard.md`](../docs/design/health-dashboard.md). Its north star: a red
on Health is a real, current, explained problem — measured once, judged once, shown once, told
once. The operator answered its six questions on 2026-10-18 (the rulings head its § 11). It
supersedes the open follow-ups of this program's W0 (shipped 2026-08/09) and replaces W3.4's
ladder; the `ops_incidents` signature engine retires in its wave 3.

The ten waves: (1) one vocabulary, one registry, one ladder; (2) watchers — a pg_cron liveness
check and one hourly GitHub canary; (3) platform checks and the worker's health lane, retiring the
signature engine; (4) portal checks: scraping, publishing, delisting and coverage; (5) the dedup
check; (6) the parsing alarm and the quality gauges; (7) delivery, now readiness and docs only,
because the bell stays the one channel for now (Q1; *since 2026-10-21, R2b: no bell, Health + the canary e-mail*); (8) the new Health page; (9) dropping the old
health layer — destructive, after a backup and 48 h with no reads (Q5); (10) shared admin page
parts.

What wave 1 changed:
- **One vocabulary.** Every result is `ok`, `warn`, `fail` or `unknown` (OK / Watch / Problem /
  Not measured). A check that timed out, did not run, raised, or could not measure is `unknown`
  with its reason: never OK, and it never opens, recovers or ages an incident. Migration 619 widens
  the status CHECK.
- **One registry.** `scripts/verify_pipeline.py` `CHECKS` declares each of the 29 keys once
  (class, host, cadence, label, unit, owner page) and projects them into the new `health_checks`
  table. Both GitHub lanes run only the checks that are due, so the daily sreality image probe and
  the weekly portal-link probe became cadences, and `sreality_image_canary.yml` and
  `verify_outbound_urls.yml` are deleted. Each result keeps its message and its cells. Eight keys
  are gauges (Q2): location coverage, the Kč/m² price floor, area vs usable area, walk coverage,
  deferred delist nominations, ads with no portal link, description reading and the floor
  convention; the rest are alarms.
- **One ladder.** `toolkit/system_alerts.reconcile`, run by the API every 60 s, keeps one
  `ops_incidents` row per failing cell (`cell:<key>:<subject>`), sends one grouped onset per check,
  reminds until acknowledged on Health — an alarm at 6 h, 24 h, 72 h and then weekly, a gauge once
  a day (the Q2 ruling) — and sends one recovery once an OK is measured across two cadences less
  the due slack, never under 30 min. The 30-day re-derivation, the flap cooldown, the day-keyed alerts
  and the weekly heartbeat are deleted. The GitHub lanes write no system alert; a failing alarm
  still reds either lane, so GitHub emails.
- **Read is not acknowledged.** Platform admins can now mark a system alert read (migration 619's
  policy arm; all 861 had stayed unseen), and only Acknowledge stops reminders: a minimal
  Acknowledge button on Health's pipeline-checks card (`POST /admin/health/acknowledge`) until
  wave 8 rebuilds the page.
- **One admin-gated read.** `pipeline_checks_public` serves the registry, each key's newest result,
  its effective status (Not measured once stale) and its incidents; keys outside the registry, such
  as the frozen `outbound_url_parity`, leave the page.
- The retiring signature engine is fenced: it can never close or "recover" a cell incident, and
  its poller opens no incident for a lane that exited 1 on a failing check (`check:<key>|fail` on a
  `failure` run); a timed-out, cancelled or interrupted lane still opens one.

Owed items this absorbs:
- **`docs/design/realtime-scrapers.md`'s deferred W5b** (SLO-scaled health, push alerting,
  silent-green closures; also listed in `roadmap/next.md`): replaced by the sprint instead of a
  rewrite of `scraper_health_checks_mv` — wave 1 turns silence into Not measured, wave 2 watches
  the watchers, wave 4 derives each portal's limits from its own behaviour, and wave 9 drops the
  matview.
- **From the 2026-10-10 Next list:** the timed-out warn that closed an incident with a false
  "Recovered" is fixed (a timeout is `unknown`), and the ladder no longer re-derives incidents from
  30 days of results. The `llm_errors` any-provider rate arm and `worker_lane_stall`'s unalarmed
  `failed_passes` move to wave 3 (the llm and jobs checks); delivery follows Q1 (bell only by ruling
  Q1 2026-10-18; superseded 2026-10-21 by R2b: Health + the canary e-mail only).

~~**Next:** wave 2 — `health_liveness()` on pg_cron every 5 minutes and one hourly GitHub canary
(migration 620).~~ Done 2026-10-18 (wave 2, below).

## 2026-10-18 — Health dashboard sprint, wave 2 (shipped with this PR)

Watchers: everything that measures or tells now has an observer that does not run inside it, and
GitHub's failure email is the one path to the operator that needs neither Supabase's scheduler nor
Railway. Plan: [`docs/design/health-dashboard.md`](../docs/design/health-dashboard.md) § 6, wave 2,
and its "As built" note.

What wave 2 changed:
- **The watcher.** `public.health_liveness()` (migration 620) runs on pg_cron every 5 minutes and
  writes one `liveness` result, told by the API's ladder like any check: the worker's heartbeat
  (Problem past 10 min), the worker's health lane (from wave 3), every alarm past its staleness
  budget, the API's background loops (the watchdog matcher's lease) and any measured Problem left
  without an incident. It replaces the `worker_liveness` check, a 5-minute limit GitHub sampled
  about every 4.9 hours, and the 24 h dead-man job `emit-verification-stale-alert`, which never
  fired (unscheduled; migration 643 drops its function).
- **The canary.** `verify_pipeline.yml` is the one monitoring workflow ("Monitoring: health canary
  (hourly)"): it records failed workflow runs (the poller, moved forward from wave 3, so
  `monitor_workflow_failures.yml` is deleted), measures the GitHub-hosted checks, and then
  `scripts/health_canary.py` asserts that the database answers, the watcher ran in the last 30
  min, its failing cells have incidents, and no alarm's notice has sat unread and undelivered for
  15 min while its incident is open and unacknowledged. A failing check never reds the run; the assertions do (and a crash of the poller or the harness, or
  the harness step overrunning its 21 minutes), so with
  the bell the only channel (Q1) an unread alarm reaches the operator as GitHub's failure email at
  the next canary run (*since 2026-10-21, R2b: it asserts an open, unacknowledged alarm's latest rung instead*). `llm_health.yml` and the 6-hourly lane are gone, and with them
  `--exit-nonzero-on-fail`: a failing check no longer reds a run.
- **A real time budget per check.** The harness's `run_due` measures a host's due checks, alarms
  first, and each statement's timeout is now the check's remaining budget, so a check of several
  statements can no longer take several budgets (`dual_write_parity` ran a p50 of 57 s on 45 s).
  A check that runs out of time is Not measured, with its budget and the time it spent; a check
  the run never reached writes nothing and stays due; `migration_drift` with no migration files
  is Not measured, never OK.

Owed items this closes:
- **W0.4, graceful degradation of the acute lane (2026-08):** its per-check wall-clock budget
  bound each statement rather than the check until this wave's per-check deadline; the acute lane
  itself is gone.

To check after the merge: the first red canary run's email arrives (GitHub emails a red scheduled
run to whoever last changed its cron line).

~~**Next:** wave 3 — the platform checks (jobs, capacity, llm) and the worker's health lane, which
calls `run_due` every 60 s; the poller's production filter and reason lines; the signature engine
retires (migration 621).~~ Done 2026-10-19 (wave 3, below).

## 2026-10-19 — Health dashboard sprint, wave 3 (shipped with this PR)

Platform checks and the worker's health lane: every scheduled machine on every runtime, the
database and the LLM providers are judged once, per job or provider, every 10 minutes from the
worker, and a red run carries its reason. The signature engine retires, leaving one incident
model. Plan: [`docs/design/health-dashboard.md`](../docs/design/health-dashboard.md) § 6, wave 3,
and its "As built" note.

What wave 3 changed:
- **The health lane.** A new worker lane, `health`, runs the due worker-hosted checks every
  60 s within a 300 s pass, projects the worker's registry rows on each process's first pass, and
  deletes check results older than 35 days once a day (Q5). The watcher's `health_lane` cell now
  judges it.
- **`jobs`** (every 10 min): one cell per production GitHub workflow (with the failing run's reason
  line), the GitHub poller, every pg_cron job, every worker lane (and each portal failing inside
  the probe or drain lane) and every derived artifact. It replaces `worker_lane_stall`,
  `workflow_poller_liveness` and `broker_resolution_freshness`.
- **`capacity`** (every 30 min): pg_cron run times against a reference the incident cannot erode
  (each job's lowest weekly p50 since 2026-10-17), check timeouts per host, the oldest open
  transaction, connections and the worker's memory. It replaces `db_saturation` and
  `long_open_transaction`.
- **`llm`** (every 10 min): a credit outage per provider over 72 h, error share and starvation per
  provider and caller (`called_for`), and 24 h spend. One provider's success no longer hides another's outage. It
  replaces `llm_errors` and `llm_burn_rate`. `dual_write_parity` is deleted (Q6).
- **One home for thresholds.** `THRESHOLDS` in code, each with its basis; the
  `pipeline_check_thresholds` settings row is read by nothing (migration 643 deletes it).
- **The heartbeat remembers.** Each lane reports its interval (so an off lane reads off), a
  failing spell with its last error, and, in the probe and drain lanes, each failing portal; at
  boot the worker carries its counters over from the previous heartbeat, so deploys reset nothing.
- **The poller** records production runs only (scheduled, or a push on `main`), each red with its
  reason line, and per workflow its state, whether its file is on `main` and its recent scheduled
  starts.
- **System alerts are ready for delivery**: subject from the message, link to `/health#<check>`
  (the bell stays the one channel, Q1). *Deleted 2026-10-21 (R2b, notifications push W1).*
- **Migration 621** adds the poller's columns and closes the signature engine's open incidents;
  the engine's code (`toolkit/ops_incidents.py`, the crash-path and lane writers, the poller's
  incident pass) and its signature tiers are deleted.
- **The review's corrections** (draft #1871, 2026-10-19): the worker's memory is judged only from
  a pass of the running process; an artifact built on GitHub gets GitHub's 18 h delivery slack;
  check timeouts are judged per host; a database session hidden from the checks' role makes the
  connection and open-transaction cells Not measured; a lane entry the pre-wave-3 worker wrote
  reads Not measured during the first deploy, not "never woke"; the boot seed does not carry a
  lane's interval, so a lane that never wakes reads "never woke"; a CI rail fails once the
  capacity reference date is more than 90 days old; the pg_cron queries run for real in CI; a Health link
  scrolls to its check. All twelve are in the design's As built note.

Owed items this closes:
- **From the 2026-10-10 Next list:** the `llm_errors` any-provider rate arm (now per provider) and
  `worker_lane_stall`'s unalarmed `failed_passes` (now a lane cell's failing and flapping arms).
- **From wave 2:** the poller's dead ladder fence goes with its rewrite; `MAX_LOG_FETCHES` stays
  at 25 a poll, and a red past the budget keeps no reason (below).

Owed and stated (also in the design's "As built" note):
- The start-gap arm of a GitHub cell is Not measured until the workflow has 10 recorded scheduled
  starts (about two weeks for a daily job), and the long-cycle arm until it has 2 (one to two
  cycles for a weekly or monthly job): the starts accumulate from wave 3 on, with no history
  import. Only a scheduled run records a start (a push has no cadence), so a workflow that only
  runs on push has neither arm: a lone red is Watch, 3 reds a Problem. The overdue arm is judged
  as of the poller's cursor, not now.
- A provider's credit outage is told twice until wave 6: by the `llm` check's credit cell and by
  the text lane's `jobs` cell, because a pass the provider stopped is a failed pass.
- A workflow that dispatches itself is recorded by its cron run only: its chained hops are
  dispatch runs, outside the production filter.
- The replay rails' quiet windows are what the data allows: 8 days for pg_cron, one week for
  capacity (its relative arm Not measured), a fortnight for the LLM providers, none for GitHub
  workflows (only each workflow's newest success was stored before wave 3).
- A red run past the poller's reason budget (25 logs or 90 s a poll) keeps no reason line and is
  not retried.
- A pg_cron tick that skips itself counts as a run and pulls the median down; the sticky
  reference only makes the comparison stricter.
- Read by nothing after this wave, for migration 643 to drop: `app_settings.broker_resolution_last_complete`
  (still written by `scripts/resolve_brokers.py`; brokers territory) and the `dual_write_watermark`
  table.
- At merge the retired keys' open incidents close silently at the canary's first run from `main`,
  and the new `llm` check opens a fresh onset for any outage still under way.
- Wave 2's rulings (the `api` cell's 26 min, the NULL lease, the red held through a recovery) are
  unchanged.

To check after the merge: the worker's `health` lane passes in the heartbeat, `health_checks` holds
the three `realtime-worker` keys, and Health shows their rows within 10 minutes of the deploy (the
page renders one row per check; their cells render in wave 8: corrected in Health dashboard sprint
wave 5).

~~**Next:** wave 4 — the portal checks (scraping, publishing, delisting and the coverage gauge),
built on ledgers that see the worker's work (migration 622).~~ Done 2026-10-19 (wave 4, below).

## 2026-10-19 — Health dashboard sprint, wave 4 (shipped with this PR)

Portal checks: each portal's discovery, publishing, delisting and holdings are judged by four
checks the worker measures every 10 minutes (the holdings gauge hourly), built on ledgers that see
the worker's work, with limits taken from the delisting policy's constants and each portal's own
walk rhythm; Browse's whole stock is watched, not only its newest ads. Plan:
[`docs/design/health-dashboard.md`](../docs/design/health-dashboard.md) § 6, wave 4, and its "As
built" note; reference: the scraper-ops skill's `references/pipeline-verification.md`.

What wave 4 changed:
- **`scraping`** (every 10 min): per portal and unit (a property type and deal, such as flats for
  sale), the hours since the newest walk that reached the portal's end, against 3 and 6 times the
  portal's own median walk gap; new ads found in the last 24 h against the same 24 h one to four
  weeks earlier; the share of detail fetches that failed; and, per unit, the share of the portal's
  declared ads the walk did not collect. It replaces `walk_coverage` and `walk_end_streak`.
- **`publishing`** (every 10 min): how long new and changed ads wait in the queue; the share of
  new ads, and of the whole active stock, missing from Browse, split by the stage that blocks them
  (no property yet, property not active, location not served, not yet in Browse); new photos not
  stored or stored too small; the oldest property waiting for its recompute. It replaces
  `acquisition_lag`, `property_maintenance`'s dirty-queue arm and the sreality image-template
  probe.
- **`delisting`** (every 10 min): ads made inactive in the last 24 h against each of the four weeks back, read at
  every 24 h window within ±3 h of this hour (a drop against each week's quietest window, a spike
  against its busiest; the 10-10 day on which the whole fleet made none inactive now fails); gone
  verdicts that should have closed their ad by now; the oldest presence check waiting; walks whose
  checks the flip cap deferred; and the share of ads made inactive 2-3 days ago that are active
  again. It replaces `false_delist_share` and `delist_flip_refused`.
- **`coverage`** (hourly, a gauge): per unit, the ads we hold against the ads the portal declares,
  less those waiting out their dwell; per portal, the active ads no walk can nominate. With it the
  `coverage_gate` workflow retires (it had evaluated nothing since 2026-09-13), and so do the
  ceskereality and mmreality slice writes only it read.
- **The seams the checks read.** remax and maxima now declare that their "total" is the count of
  what they saw (`total_is_count`), so their missing and residual arms read Not measured instead
  of a 0 % gap by construction; the declared-total key is spelled once
  (`portal_runner.DECLARED_TOTAL_KEY`); `delist_policy.RECHECK_THROTTLE` names the 24 h re-check
  exclusion that makes the effective dwell 24 h, with nothing changed (the one-rule fix is #1858).
  No check module names a portal (a CI rail).
- **The daily full sweep** stamps the `property_sweep` derived artifact (migration 622), judged by
  the `jobs` check, instead of an `app_settings` row: still Problem at 56 h (a 38 h budget plus
  GitHub's 18 h delivery slack); the old 52 h Watch is gone, because an artifact cell has none.
- **The health lane's pass** is capped at 360 s (8 × 45 s: the worker's seven checks now, wave 5's
  dedup the eighth); the canary measures 13 GitHub keys (660 s of budgets).

Owed items this closes:
- **`docs/design/realtime-scrapers.md`'s deferred W5b**, its wave-4 share: each portal's limits
  now come from its own behaviour (its walk gap, same-window baselines, the effective dwell), not
  a hand-set cadence.
- **From the investigation:** the 10-10 zero-flip day (no alarm then, the outflow arm now); a
  portal whose walks stop no longer drops out: every portal gets its cells, and one with no
  finished walk in 30 d reads a `<portal>/walks` Problem and keeps its inflow, fetch-error and
  unnominated cells; sreality's "25 % errors" (one poison id, #1851) reads 0.02 %; remax and maxima
  no longer read 100 % coverage by construction; and the W1 row's `ingest_freshness` (above) is the
  inflow and ingest arms.

Owed and stated (also in the design's "As built" note):
- The missing and residual arms are Not measured for every unit until its first walk after the
  deploy writes `total_is_count`, and for remax and maxima for good (their total is what they
  saw).
- The fetch-error share approximates "attempted in 24 h" (the queue keeps no per-attempt time);
  the stuck limit uses the portal-level walk gap, which understates portals whose units rotate;
  deferrals are judged per portal; the photos arm cannot judge ads with no photo rows (a fact
  counts them).
- At ship the `coverage` gauge opens a Problem on realitymix's unnominated ads (151 NULL-category
  ads, the oldest last seen 112 d ago, #1853) and reads Watch on ceskereality's (96); from the
  first walk after the deploy that records `total_is_count` for ceskereality's `dum`/`prodej`, that
  unit's residual reads Problem too (about −14 %: 13.7 k declared against 11.8 k held, the
  chaty-chalupy double count, #1854). `delisting` read Watch on idnes's outflow on 10-19 (a 423 %
  spike against the same hour one to four weeks back; against the busiest 24 h within ±3 h
  of that hour it is about 210-240 %, under the 300 % line), and sreality's precision cell may read
  Problem on 10-20/21 (its 10-18 flip cohort, ~17.5 % active again): the blink tail #1858
  addresses, told, not suppressed.
- Closed 2026-10-20 (`feature/link-ingest`): a full property sweep holding the maintenance lease
  over about 40 min turned `publishing`'s visible cell Problem (new ads waited for their property,
  the sweep attaching only at its start); it now attaches after every batch under that lease, and
  the worker's maintenance lane runs every 30 s.
- Left for wave 9: the 150 ceskereality and mmreality rows of `portal_index_slices` and
  `portal_coverage_gate` (read by nothing), `portals.supports_complete_walk` (no writer now; the
  old Health page's portal posture still reads it) and `delist_flip_refusals` (still written, read
  by nothing); only `portal_coverage_gate` is on wave 9's list so far. Wave 9 as built: only
  `portal_coverage_gate` went (migration 643); the slice rows (unjudged), `supports_complete_walk`
  (still read, by the Scrapers page, not the old Health page) and `delist_flip_refusals` (still
  written) stay, and the scraper track owns them.
- The old page's matview checks (`delisting_latency`, `delisting_spike` and the rest) kept
  rendering until wave 8 retired the page; migration 643 (wave 9) dropped their matviews.
- At merge the seven retired keys' open incidents close silently at the canary's first run from
  `main` (`false_delist_share`'s included), and the new checks open fresh onsets where they
  measure Problem.

To check after the merge: `health_checks` holds the seven `realtime-worker` keys, Health shows the
portal checks' rows within 10 minutes of the deploy (coverage's within the hour; their cells render
in wave 8: corrected in Health dashboard sprint wave 5), and `derived_artifacts` holds
`property_sweep`, stamped by the next completed sweep.

~~**Next:** wave 5 — the dedup check (`scripts/checks/dedup.py`), judged from durable stores, and
the AutodedupProgress copy that still says "shadow".~~ Done 2026-10-19 (Health dashboard sprint wave
5, below).

## 2026-10-19 — Health dashboard sprint, wave 5 (shipped with this PR)

The engine check: the engine's state, its progress, how quickly it decides new ads and whether
production honours your no-merge rulings are judged every 10 minutes from the worker, from the
engine's own durable stores and its merges alone. The engine's progress page stops saying
"shadow". Plan: [`docs/design/health-dashboard.md`](../docs/design/health-dashboard.md) § 6, wave
5, and its "As built" note; reference: the scraper-ops skill's
`references/pipeline-verification.md`.

What Health dashboard sprint wave 5 changed:
- **`dedup`** (label "Engine", every 10 min): the engine's state, off (its interval is 0), building
  (deciding a newly seeded area for the first time, merges paused), merging (the apply scope admits
  a merge) or idle, is a fact on every cell, never a verdict. Its cells: whether the live stream is
  live (an out-of-date seed version stops production merges with no other signal); a build's
  decisions per hour and its time to finish, a lower bound; how long merges have paused in a build
  (Watch past 48 h, never a ring, Q3); the share of new ads in the engine's area still undecided
  1-24 h after they arrived; a lease held by anything but the live worker, or one no pass takes any
  more; pairs you ruled different that an engine merge joined after your ruling, and active broker
  suppressions whose identities share a broker; and the share of the engine's merges 7-14 days old
  that an operator split (Watch only).
- **Rule 15 holds by construction.** The check reads only the live engine's stores and merges by
  the engine or an operator, by a positive filter; a CI rail fails if it names the removed engine's
  source, and a live CI test proves a removed-engine merge is no breach.
- **`broker_merge_suppression` retires** into the rulings cell for brokers (its SQL moved verbatim,
  its threshold and tests with it).
- **The heartbeat's `latency_p50_s` / `latency_p95_s` go**, with their log fields: they measured
  seed age, not latency; arrival to decision is now read from the stores.
- **The engine's progress page** says "The engine merges in production inside the apply scope; live
  lane health is on Health." (Q4), "Health" linking to the check, in place of its `SHADOW` chip and
  the "four small blocks … merges nothing" lede; the groups page loses the same false claim.
- **The registry**: 20 keys, 8 on the worker (8 × 45 s fill the 360 s pass exactly) and 12 on
  GitHub (615 s of budgets).

Owed items this closes:
- **AUTODEDUP E939 / E941** (`roadmap/autodedup.md`): the `verify_pipeline` judgement of the lane,
  delivered as this check, from the stores rather than the heartbeat.
- **The design's § 2.6** "Dedup: no check at all", and the progress page's false "merges nothing".

Owed and stated (also in the design's "As built" note):
- Builds before 10-16, the 10-02 stall and every lease strand left no durable trace, so none is
  replayed but the 10-10 strand, as a synthetic replay; the hermetic tests hold their Problem
  shapes. The 28-day replay opens one incident, the decision lag of 10-19 09:00Z; its lease and
  rulings cells there see empty or constant inputs.
- The build backlog cannot see blocks not yet walked (the facts name them), so the time to finish
  is a lower bound; the backlog's split into active and inactive ads is dropped (0.35 s a read).
- Set-level negative rulings that name no ads (3 today) cannot be judged.
- A pass that aborts is a completed pass to the heartbeat; since Health dashboard sprint wave 5
  `jobs` reads a last pass that aborted (`last.aborted`, the engine lane's reason) as Watch, and as
  Problem when a second pass aborts; a refusal was a failed pass already. The engine check sees the
  stall as `last_commit_age_s` at once and as decision lag after an hour without a commit. A failed
  re-cut, and E939's reconcile waits and daily-cap skips, have no cell.
- The decision-lag cell is Not measured most hours at today's tempo of sittings: 96 of the 169
  hours to 10-19 09:00Z fell within 24 h of a brake lift (7 lifts in 7 days).
- Under the held E964 building and merging overlap, and the merge-pause cell needs that PR's
  per-block rail; the engine's own `PassResult.latency_s` is left to the E961-E964 stack.
- `app_settings.updated_at` is not stamped by its writers, so every age comes from
  `app_settings_history` or the engine's own stamps.
- The first real incident: from 07:55Z on 10-19 the lane committed nothing (passes aborting on their
  pair budget, `failed_passes` 0). The engine's fix (E970) is its own program's. The decision-lag
  cell read it as Problem at 09:00Z (55 of 421, lower bound 10.2 %): a brake lift, a build's end, a
  seed or an engine change inside its 24 h stops excusing the lag once no pass has committed a new
  ad for an hour (the stall rule). While a Problem it read is open (`open_reading`, `opened_at`), a
  clean reading under those excuses reads OK and any other Not measured, so the incident can close
  and an excuse's own backlog never rings; the reading closes once a stored OK lies 2,400 s of
  measured time after its first OK (`recovering_since`), 7 days after the Problem, or at a seed or
  a build start after it.
- At merge the `broker_merge_suppression` row leaves at the canary's first run from `main`; until
  then both rows show, and the new row's unit reads "Ncells" until wave 8.

To check after the merge: `health_checks` holds the eight `realtime-worker` keys with `dedup`
(label "Engine"), Health shows its row within 10 minutes of the deploy (the page renders one row per
check; the cells are in `pipeline_checks_public.details` and render in wave 8), and the engine's
progress page carries the new sentence with its link landing on the row.

~~**Next:** wave 6 — the parsing alarm and the quality gauges (`scripts/checks/quality.py`): parser
regressions ring as one per-portal alarm, standing data-quality conditions become gauges with a
30-day trend, and no legacy key remains.~~ Done 2026-10-19 (Health dashboard sprint wave 6, below).

## 2026-10-19 — Health dashboard sprint, wave 6 (shipped with this PR)

The parsing alarm and the quality gauges: parser regressions ring as one per-portal alarm, standing
data-quality conditions are gauges with a 30-day trend, rule 25 is judged over active ads, every
check emits its own cells and no legacy key remains. Plan:
[`docs/design/health-dashboard.md`](../docs/design/health-dashboard.md) § 6, wave 6, and its "As
built" note; reference: the scraper-ops skill's `references/pipeline-verification.md`.

What Health dashboard sprint wave 6 changed:
- **`parsing`** (label "Parsing", an alarm every 6 h on the worker): per portal, a field's fill or
  vocabulary drifting from the blessed baseline (re-blessed in this wave), new ads losing their per-m²
  measure against the same portal's stock, a jump in a stock median (Not measured for its first 6
  days), a location payload the contract cannot read (sreality, bezrealitky and, new, mmreality, through
  predicates `location_data` exports), and a re-snapshot storm against the portal's own 28-day median.
- **`data_quality`** (label "Data quality", a gauge every 6 h): the share of prices the per-m² floor
  NULLs, area against usable area, ads with no portal link and each portal's standing per-m² gaps. It
  reads Problem on day one (realitymix komerční/pronájem: 11.4 % of 4,140 priced ads, lower bound
  10.5 %), a real condition told once and then daily until acknowledged.
- **`location_town_coverage`** (a gauge, now on the worker): rule 25 over ACTIVE ads first written more
  than 24 h ago, 43 on 10-19 against 13,246 over the whole corpus (operator ruling Q2); a stalled RÚIAN
  registry load stays a Problem.
- **`text_extraction`** (label "Description reading", an hourly gauge): new ads' descriptions read
  within a day, and the archive re-read's ETA (4.3 d on 10-19), replacing a check that read FAIL on
  every run.
- **Every check emits its own cells.** The implicit cell is gone from the ladder and, through migration
  633, from the watcher; `migration_drift` has a cell per migration file and the portal-link probe a
  cell per portal, and the probe no longer reads a relative redirect or a bot wall as a broken link.
- **Gauge trends**: each gauge cell carries 30 days of daily points and whether it is worse than a week
  ago; `coverage` moved onto the same facts (`trend`, `worse_7d`).
- **The health lane's pass** is 500 s, and a gauge it cannot fit waits for the next pass; the canary's
  job is 19 min and its harness step 6 min.
- **The registry**: 14 keys, 12 on the worker and 2 on GitHub (165 s of budgets). Nine keys retire: the
  eight legacy checks and `floor_convention` (Q4). Migration 633 also unschedules `capture-data-quality`
  (24 failures in 30 d), and `data_quality_snapshots` stays as frozen history (Q5; wave 9 kept it).

Owed items this closes, and one correction:
- The design's "every check emits explicit cells" and "no legacy key remains".
- The payload-shape parity test the location program claimed, lost in location W1-a (5d3fc621), is
  restored as a live CI rail.
- The wave-3 note above that a provider's credit outage is told twice "until wave 6" was never right:
  the alarms still tell it twice (the `llm` credit cell and the text lane's `jobs` cell), and wave 6
  adds the `text_extraction` gauge as a third, first through its flow cells' unread new ads and then
  through its stock cell's 0 readings, stated and accepted.

Owed and stated (also in the design's "As built" note):
- The stock arms are slow: a field NULLed on every new write needs 1.8-23 d to read Watch. The median
  arm is blind for its first 6 days and reads a heal or a re-bless as a shift (no heal ledger yet).
- No parsing-family check has ever caught a parser regression in flight; the thresholds rest on heals,
  standing defects and the measured series.
- Media (photos per new ad) and value anomalies (placeholders, sentinels and, with `floor_convention`
  gone, a storey-scale flip) stay unwatched: filed, not built.
- A text-lane stall shows twice for bazos (publishing and the location gauge), and the location gauge
  cannot announce a resolver regression while it already reads Problem (publishing does).
- The 35-day prune bounds every replay; where no durable input exists the replay is synthetic.
- The text gauge dates a retried reading at its first attempt, so its p95 understates and its
  readings in 24 h can miss a late success.
- Coverage's trend facts are renamed (`trend`, `worse_7d`); wave 8 reads the new names.
- At merge the nine retired keys leave at the canary's first run from `main`, and the old
  whole-corpus location incident closes silently within 7 days.

To check after the merge: `health_checks` holds twelve `realtime-worker` keys and two `github-actions`
keys; Health shows the three new rows and the re-hosted location row within minutes of the deploy
(their cells render in wave 8); after migration 633's apply `cron.job` holds no
`capture-data-quality`; `data_quality` reads Problem on realitymix's komerční/pronájem floor cell, as
expected.

~~**Next:** wave 7 — delivery readiness: with the bell the only channel (Q1), the docs and click-by-click
steps that switch email or Telegram on later; no channel is switched on.~~ Done 2026-10-19 (Health
dashboard sprint wave 7, below).

## 2026-10-19 — Health dashboard sprint, wave 7 (shipped with this PR)

*Superseded 2026-10-21 (ruling R2b, notifications push W1): this readiness was deleted; system alerts
live on Health, and the canary e-mail is the external alarm.*

Delivery readiness, docs only: by ruling Q1 (2026-10-18) the bell stays the one channel for now, and
the system is kept ready for Telegram and email. Plan:
[`docs/design/health-dashboard.md`](../docs/design/health-dashboard.md) § 6, wave 7, and its "As
built" note; reference: the scraper-ops skill's `references/pipeline-verification.md` § Delivery
readiness.

What Health dashboard sprint wave 7 changed:
- **The enabling path, written down** for later, click by click, in three steps: (1) on Railway, the
  API service's Variables: `TELEGRAM_BOT_TOKEN`, or `RESEND_API_KEY` and `EMAIL_FROM`, with
  `SPA_BASE_URL` checked for the link to Health, then Deploy; (2) the recipient in Settings ›
  Delivery (Telegram chat ID or Email recipient); (3) a migration written that day with the next free
  number: the channel list and one test system alert, "Delivery test: Health alerts now reach you here",
  confirmed when it arrives.
- **No migration.** The design's 623 is not written: merged under Q1 it would sit on main unapplied,
  and its data-only statements are invisible to `migration_drift` (it reads such a file
  "unverifiable"). The enabling migration takes the next free number on the day you give the word
  (main reached 634 during the sprint), and wave 9's destructive one likewise when it comes.
- **Verified, not changed:** since wave 2 the canary has counted an alarm's notice as handled when it
  was sent or delivered, as it counts one read in the bell, and its tests pin it; nothing has ever
  been sent (`channel_sends` held 0 rows on 10-19), and no watchdog or collection is opted into email
  or Telegram, so switching on sends nothing old.
- **The bell-only statements** here, in the toolkit-api skill and in the media-integrity design now
  name the ruling and point to the steps; that design's 2026-08-04 ruling is re-affirmed (2026-10-18),
  not superseded.
- The flaky wave-4 rail `test_the_producers_stamp_lands_on_the_row` (a 1 µs miss on CI) is fixed, and the
  canary's channel-status pins are added (sent and delivered count as handled; queued, failed, bounced
  and suppressed do not).

~~Owed: the enabling migration, at your word (the three steps).~~ Moot since 2026-10-21 (R2b). GitHub's failure email stays either
way: it is the path that needs neither Supabase's scheduler nor Railway.

To check after the merge: nothing changes in production; `system_health_channels` still reads `[]`
and the bell rings as before.

~~**Next:** wave 8 — the Health page: one screen, at 1,280 px and on the phone, shows what is wrong,
where and why, and links to the page that owns the fix.~~ Done 2026-10-19 (Health dashboard sprint
wave 8, below).

## 2026-10-19 — Health dashboard sprint, wave 8 (shipped with this PR)

The Health page, rebuilt on one read: what is wrong first, the portals as one matrix, and a link to
the page that owns each fix. Plan: [`docs/design/health-dashboard.md`](../docs/design/health-dashboard.md)
§ 5 and § 6, wave 8, and its "As built" note.

What Health dashboard sprint wave 8 changed:
- **One read** of `pipeline_checks_public`, every 5 minutes while the tab is visible and on focus, in
  place of 17 requests a minute; the browser holds no threshold.
- **The page**: a header from the watchers; five counts (open alarm incidents, Problems, Watch, Not
  measured, gauges worse than last week); four sections in a fixed order (Portals, Engine, Data
  quality, Platform), rows worst first; Portals opens with the matrix, portals × the five portal
  checks, each cell that check's worst measured verdict for that portal; each check row has its status
  word and dot, value, message, worst cells, trend, owner page link and Acknowledge. Not measured and
  stale never read OK; an empty or failed read is an error with Retry, never "all clear".
- **Ways in**: Health is the first Admin entry and an admin-only row in the phone's account sheet; each
  system alert in Notifications gains an "Open Health" link to its check (the feed now carries the
  check's key).
- **The registry, normalised in code (no migration)**: the five portal checks share the domain
  `portals`, and owner pages are filled (LLM providers and Description reading → LLM costs, the portal
  checks and Data quality → Scrapers, Jobs → Settings).
- **Deleted**: the old page (2,431 lines) with every panel the plan's Removed list names, its two helper
  libraries and their tests, 11 health fetchers, 29 health types and the ten 60-second polls; the 14
  `# portal:` workflow tags with the generator code that read them (Settings › GitHub Actions stays the
  workflow catalogue).

Owed and stated (also in the design's "As built" note):
- ~~"Alerts reach" and the delivery facts wait for delivery's enabling migration (wave 7's three steps)~~ moot since 2026-10-21 (R2b):
  the watcher writes no delivery facts, so the strip has five tiles, not six.
- The legacy `outbound_url_conformance` row has no cells until its next weekly run (~10-25).
- The phone reflow is pinned by class strings only (jsdom cannot evaluate a media query); the browser
  check after the deploy proves it.
- A row that goes stale can read measured for up to one poll (5 min): the view judges staleness when
  it is read.
- ink-3 small text (Stat labels and hints, Panel titles) measures 3.9:1 light and 4.3:1 dark, under
  AA's 4.5:1 for small text: a design-token ruling for the operator, not changed here.
- The ONE PIN plan's W2 SPA step (`docs/design/one-pin/PROGRAM.md:346`) points at what this wave
  deleted: `derivedArtifacts.ts` and its test, and the old `Health.tsx`'s comment at :89-93 (those
  lines are other code now). Told to its session 2026-10-19 (it adds the note in its own PR #1896); not edited here.

To check after the merge: Railway's vite status reads success; the worker re-projects the registry on
its first health pass after its deploy (the four portal checks then read domain `portals`); then, read
only, the page in both explicit themes at 1,280 px and on the phone, and a system alert's "Open Health"
link landing on its check.

~~**Next:** wave 9 — drop the old health layer: its 8 matviews, 11 functions and 12 views stop costing
database time, after a backup and 48 h with no reads of them (destructive, Q5).~~ Done as Health green
sprint wave 6 (below).


## 2026-10-20 — Health dashboard sprint, wave 10a (shipped with this PR)

Wave 10, the shared admin page parts, is split in two: 10a, this PR, moves the admin pages onto the
shared panel and error banner where nothing on screen changes, plus the one change Q4 already ruled;
10b, a second draft PR, holds the other moves that change how a page looks; you ruled on 2026-10-20: all of it, with a red dot beside the label where a number was brick.
Plan: [`docs/design/health-dashboard.md`](../docs/design/health-dashboard.md) § 6, wave 10, and its
"As built" note. Wave 10a goes ahead of wave 9, which keeps its gate (wave 8's Next, above).

What Health dashboard sprint wave 10a changed:
- **One panel**: the cards of LLM costs, NEW DEDUP · Audit poloh and the NEW DEDUP dashboard were
  copies of the shared `Panel` and now draw it, the same on screen. Location quality's cards lose their
  header bar to match them, as Q4 ruled (their titles a little wider and lighter, the content 4 px
  further in). Screen readers now list each card as a region named by its title.
- **One error banner**: the settings pages' own banner copy goes; its 8 places draw the shared
  `ErrorBanner` through a new `flush` option that keeps each where it sat, and they now announce as
  alerts.
- **Dead parts out**: Panel's unused `as` option (LLM costs' card titles become h2, the same on
  screen) and dead code in the touched files.
- The plan's "status maps → StatusDot" item is struck: the two colour maps left are run and plan
  states, which a status dot must not draw.

Ruled 2026-10-20, all of it (wave 10b, the next PR; the exact figures are in the design's "As built" note):
- **The stat tiles** on LLM costs, Location quality and AUTODEDUP · Progress: coloured numbers turn
  ink (copper "Projected / month" and "With a town"; brick failed calls, disputes and stale registry),
  labels and hints change size or tone, and the Progress tiles get about 17–20 px shorter.
- **The three brick numbers** (LLM costs' failed calls, Location quality's disputes and stale
  registry): a small brick dot beside the label, with the word "Problem" for screen readers.
- **The loading placeholders** on LLM costs and Location quality stop pulsing, and their tiles change
  corner and border slightly.
- **The error banners** on LLM costs and Location quality change margin and padding; LLM costs' also its
  corners and title weight; Location quality's gain a "Failed:" title and a softer border.

Owed and stated (also in the design's "As built" note):
- LLM costs and Settings have no page test, so the nested-control check does not run on them.
- With the API down, Settings can announce up to six alerts at once.
- The settings pages still import their banner through SectionChrome, now a thin wrapper over the
  shared one; moving their 8 call sites onto the shared import would delete it (wave 10b did, below).
- Admin pages the plan did not list keep their own panel-shaped boxes (AUTODEDUP · Pair, NEW DEDUP ·
  Candidates and Labeling, Settings' rent map, Outreach), and Datasets its banner-like box.

To check after the merge: Railway's vite status reads success; then, read only, LLM costs, Location
quality, Audit poloh, NEW DEDUP, Settings and NEW DEDUP · Settings in both explicit themes: each as
before, except Location quality's cards without their header bar.

**Next:** wave 10b (ruled 2026-10-20; the list above), then wave 9 at its gate.

## 2026-10-20 — Health dashboard sprint, wave 10b (shipped with this PR)

The second half of wave 10: the admin page parts that change how a page looks now draw the shared
tile, loading placeholder and error banner, as you ruled on 2026-10-20: all of it, a small brick dot
beside the label where a number was brick, and the copper numbers simply turn plain. Nothing else
changes on screen. Plan: [`docs/design/health-dashboard.md`](../docs/design/health-dashboard.md) § 6,
wave 10, and its "Wave 10b, as built" note. It merges after wave 10a (#1905).

What Health dashboard sprint wave 10b changed on screen:
- **LLM costs**: the six figures at the top are the shared tile. "Projected / month" is plain ink (it
  was copper). "Errors · 7 d" is plain ink (it was brick) with a small brick dot beside its label while
  any call failed in the last 7 days; screen readers hear "Problem". The two small notes under the
  figures are a shade darker and easier to read. The loading placeholders stop pulsing, and their six
  tiles get slightly rounder corners and a slightly stronger border. The "Failed to load cost data"
  banner sits 8 px lower, with 4 px less padding a side, tighter corners and a slightly bolder title.
- **Location quality**: its 13 figures are the shared tile: labels a hair smaller, five small notes a
  shade darker. "With a town" is plain ink (it was copper); "Disputed" and "On an older registry" are
  plain ink (they were brick) with the brick dot while any dispute is counted, and while more than half
  the rows are on an older registry. The loading placeholders stop pulsing (the same tile change as on
  LLM costs). Its four error banners gain a "Failed:" title, sit 24 px below what comes before them, are
  8 px taller and get a softer border.
- **AUTODEDUP · Progress**: its four figures are the shared tile, about 17–20 px shorter, with smaller
  corners, tighter padding, a fainter border and slightly different label lettering.
- **Settings and NEW DEDUP · Settings**: nothing on screen. Their 8 error banners now use the shared
  banner directly (still `flush`, so each sits where it sat), and SectionChrome's thin wrapper is gone:
  one banner, one way to import it.
- **Screen readers**: the five error banners on LLM costs and Location quality now announce themselves
  as alerts when they appear.

Owed and stated (also in the design's "Wave 10b, as built" note):
- With the API down, Settings can still mount up to six alert banners at once (10a's limit).
- LLM costs and Settings still have no page test, so the nested-control check does not run on them,
  and no test watches LLM costs' tiles, dot, placeholders or banner.
- No test draws the three new dots (Location quality's page test fails its overview read on purpose):
  the shared tile's own test and the browser check after the merge cover them.
- The dot borrows Health's word "Problem" for figures no Health check judges, as ruled: LLM costs'
  failed calls (Health's `llm` check judges error share and spend its own way, so the two can disagree)
  and Location quality's disputes and stale registry.
- LLM costs' "Errors 7 d" table column still shows a count in brick and a zero in faint grey (too faint
  for data): a separate ruling.
- Admin pages the plan did not list keep their own panel-shaped boxes (AUTODEDUP · Pair, NEW DEDUP ·
  Candidates and Labeling, Settings' rent map, Outreach), and Datasets its banner-like box.

To check after the merge: Railway's vite status reads success; then, read only, LLM costs, Location
quality, AUTODEDUP · Progress, Settings and NEW DEDUP · Settings in both explicit themes at 1,280 px:
the plain numbers, the dots (only where a failed call, a dispute or a stale share is there to show),
the shorter Progress tiles and, while a page loads, the still placeholders; Settings and NEW DEDUP ·
Settings as before.

**Next:** wave 9 at its gate (wave 8's Next, above); wave 10 is done.

## 2026-10-22 — Health green sprint, wave 1 (shipped with this PR)

The first wave of the Health green sprint: the Health page measures the truth. Plan:
[`docs/design/health-dashboard.md`](../docs/design/health-dashboard.md) § 14 (north star: a red leaves
the page only through a root fix that leaves less behind) and its "Wave 1 as built" note.

What wave 1 changed:
- **pg_cron stops freezing** (migration 658). The Browse list rebuild ran a dead `drop table if exists
  browse_list_next`; its NOTICE froze pg_cron's one launcher for the whole ~68 s rebuild, four times an
  hour, so every job started alongside recorded a ~67 s run (the capacity Problem on
  `cron/health-liveness`, "112 % of statement_timeout", and a canary leg). The statement is gone and
  nothing else in the function changed.
- **ceskereality's houses are counted once** (migration 659, #1854). The `chaty-chalupy` scope re-listed
  ads the `rodinne-domy` walk already sees, so dum/prodej's declared total held ~2,000 houses twice
  (residual −13.6 %; truly +1.8 %).
- **One definition of "an ad awaiting nomination"** (`db.nominable_predicate`), shared by the walks,
  `web`'s walk and the coverage check: the sreality, bazos and idnes `unnominated` warns read 0.
  ceskereality (95) and realitymix (159) stay red: their NULL-category ads, wave 2.
- **A dropped or renamed cell closes at once** as `retired`: the ghost `location_town_coverage`
  incident closes on the first pass. A cell the check lists as Not measured still waits a week.
- **One verdict, one cell**: a unit whose declared total cannot be judged shows once (coverage's
  residual), not twice: 48 fewer Not-measured cells.
- Two skill lines: a pg_cron command emits no NOTICE before its last statement; dispatch a new
  portal's first walk at deploy.

To check: 658 and 659 applied through `apply_migration.yml` before the merge; then, at :00 and :15, no
interim `SET` row for the rebuild and liveness under 1 s, and the capacity cell OK about 23 h later; the
next ceskereality walk logs `RECONCILE cm=dum ct=prodej` declaring ~11.6 k and its residual reads OK;
the three `unnominated` cells OK at the next coverage pass; the ghost incident resolved `retired`.

**Next:** W2, rule 3 made total (the 430 NULL-label ads nominable, the parsers fixed, the stored rows
healed); W3, producers (idnes's daily EUR churn, the per-m² masquerade); W4, location producers (14 of
the 38 town gaps); W5, the rulings Q1–Q3 applied; W6, the legacy layer (migration 643, on or after
2026-10-23 13:00Z).

## 2026-10-22 — Health green sprint, wave 2 (shipped with this PR)

Rule 3 made total (issue #1853). A sighting is label-blind (`reconcile_sightings` touches by id) but a
unit's nomination was label-bound, so an active ad whose label no walked unit covers was never
page-checked once it left the index: 703 NULL-label ads on 2026-10-22, 430 of them unclosable since June
(ceskereality's 129 `/drazby/` auctions, realitymix's 301 ads whose breadcrumb is truncated, 277 of them
"Prodej chaty/rekreačního objektu"). Health read them as `ceskereality/unnominated` 94 and
`realitymix/unnominated` 157.

What wave 2 changed:
- **The parsers emit the canonical label.** ceskereality maps `/drazby/` to `drazba` (sreality's and
  mmreality's word) and its parser reads the category off its own URL, so `scripts/reparse.py`
  re-derives it; realitymix reads the family from the structured h1 before the broker-written slug
  when the breadcrumb is truncated; remax labels a "prostor" title komercni when nothing above names it
  (the 273 "Komerční prostory" warehouses: nominable already, invisible to every category filter).
- **The runner owns nomination scopes.** A unit's scope is its two labels unless the portal declares
  `presence_scope` (bazos: the section's subtype; remax, maxima: the agenda); units sharing a scope
  nominate once, on the last of them, with their union, and a unit's active count is the same scope's.
  Deleted: the two slice-buffer copies (ceskereality, realitymix) and `note_empty_slice`, bazos's
  override and its `active_count`, `web`'s own query, remax's and maxima's agenda queries (an agenda's
  union is its whole walk) and `db.active_count`'s own subtype clause.
- **Orphans are nominated.** A walk whose every unit reached the end, over the portal's whole agenda,
  also nominates the active ads in no walked scope (`db.orphan_candidates`, NULL labels included)
  against every id it saw, under the same flip cap and dwell (`VERIFY orphans …`), recorded as one more
  `by_category` entry with NULL labels (an indexless unit's shape): the checks count its deferrals, and
  a failed orphan query is a run error and an entry short of the end. `web`'s whole agenda is that
  scope. A bazos dispatch narrowed to one section is `narrowed` and proves no orphan; a registry that
  maps no bazos section fails the run (rc 2) instead of walking a stand-in one.
- **A rail:** `tests/test_nomination_scope_rail_live.py` (CI migrations lane) builds every portal from
  its replayed `portals` row, seeds one active ad per label and requires each to come back from exactly
  one nomination query.

To check: the next ceskereality and realitymix walks whose units all reached the end log
`VERIFY orphans candidates=N`, and each portal gains one cell, `walks/orphans` (hours since its last walk
in which every unit reached the end; web's `walks/none-none` is renamed so); the orphans' entry declares no
total, so it gets no residual; after the reparse heal (the PR body's runbook, after the Prague read) and
two gone verdicts at least 24 h apart, `coverage ceskereality/unnominated` and `realitymix/unnominated`
read 0 within ~2 days and their `no_walked_category` 0. Not here: the multi-ad properties the heal
leaves holding prodej beside drazba (a clash checked only at merge) — the operator splits them.

**Next:** W3, producers (idnes's daily EUR churn, the per-m² masquerade); W4, location producers; W5, the
rulings Q1–Q3 applied; W6, the legacy layer (migration 643).
## 2026-10-22 — Health green sprint, wave 5a (shipped with this PR)

The `delisting` precision arm judges a flip at the age a false delist must show by. Plan:
[`docs/design/health-dashboard.md`](../docs/design/health-dashboard.md) § 14 (Q2, settled by construction,
no ruling).

- **The defect.** The arm read the flips 48–72 h old, pooled over 2–7 d under 50. Rule 3 makes an
  end-reached walk a census, so an ad still listed when we closed it is sighted again within 2 × the
  portal's p90 walk gap + 1 h (the stuck limit less the dwell); one first sighted later was missing from a
  walk that reached the end after its flip: a re-list. On 10-22 idnes read Problem at 106 of 407 (26 %):
  104 were whole agency portfolios flipped one morning and back 2–3 days later under the same ids.
- **The change.** Each portal's cohort is the day of flips from that age (`_sighted_s`, one bound shared with
  the stuck limit; idnes 19–43 h, sreality 14–38 h at 10-22's gaps). The pooled fallback and the fixed
  48–72 h constant are deleted: under 50 flips the cell is Not measured. mmreality's new-price re-lists stop
  counting (Q2b).
- **Live, 16:32Z, the final SQL:** idnes 11 of 1,270 (0.9 %), sreality 144 of 1,684 (8.6 %, lower bound
  7.3 %), mmreality 6 of 127, remax 4 of 70, realitymix 6 of 463, bezrealitky 1 of 135, bazos 0 of 1,316,
  ceskereality 0 of 751, all OK; maxima and web Not measured. 0.11 s, one index range per portal.

To check: after the deploy, the idnes precision incident closes on a measured OK, verified on the page.- **Wave 5b (2026-10-23, shipped with the next PR):** `jobs lane/<name>` read "late" once after every brake
  lift because `_lane`'s switch-on clock only applied when the lane had no pass at all; a pass from before
  the pause won. The reference is now the later of the last pass and the previous measurement when the lane
  read off then (autodedup, 15:54Z: a 5 h brake, incident 1005 opened on a healthy first pass of 117 merges).

## 2026-10-22 — Health green sprint, wave 4 (shipped with this PR)

Location producers: the claim lane sees the page the drain just fetched. Plan:
[`docs/design/health-dashboard.md`](../docs/design/health-dashboard.md) § 14 and its "Wave 4 as built" note.

What wave 4 changed:
- **The payload archive appends every changed body.** Its 7-day per-listing floor (W2a-7) discarded a
  refetched body inside the window, so the town the second fetch found never reached the claim lane
  (≥ 1,133 refusals a day). Reversed W2a-7 decision; ~1.1k more R2 PUTs a day.
- **The refetch lane selects rule 25's invariant** (`location_steps.town_missing_sql()`: active, not
  foreign, no town), the set the `location_town_coverage` check judges, instead of the pin-audit
  matview's `active_no_claims` bucket, which missed ads holding only a kraj or okres claim.

To check: `PAYLOAD stats appended=` in the worker log; the next `LOCATION_REFETCH lane` line queues ~26;
`ceskereality/town` 34 → ~23 within ~1 h of the refetch tick; `realitymix/town` 3 → 0 or a gone verdict.

**Next:** W5 applies the rulings (Q3: the 24 towns the portal withholds; the text lane's scope).
## 2026-10-22 — Health green sprint, wave 3a (shipped with this PR)

idnes states a foreign ad's price in its own currency ("Cena v EUR 298 000 €") and shows it converted
to Kč at the day's rate, and the crawlers' content hash read the Kč. Whenever the rate drifted past the
0.5 % index tolerance (measured against the STORED Kč, so the drift accumulates), the whole foreign
stock re-snapshotted at once: 8,468 re-snapshots in 24 h on 10-21/22, from 6,064 Spanish, 245 Croatian
and 94 Italian ads (1,421 Czech), only `params.cena` and `price_text` differing; seven waves of 4–10k
since 08-28 — a rule-2 violation feeding `listing_snapshots` and `dirty_properties`. Fixed here: the
idnes parser reads the stated price into `price_native` (never a column; kept in `raw_json` as the
evidence), and `ScrapedListing.hash_doc` reads it IN PLACE OF `price_czk` when present — one rule in
the shared hash, no portal branch. A Kč-priced ad's hash input is byte-identical to before (a golden
test pins the digest computed on the base commit); a foreign ad's hash changes once, by design.
`price_czk` still updates latest-wins on every fetch, unhashed (rule 20's exception: the property
rollup catches up at the daily sweep). Deleted: idnes's own `--price-change-min-pct` CLI override, which
no workflow passed; the shared `operational_limits` row is the one override channel, as for every portal.
- **Next:** the tolerance still re-enqueues the foreign stock on each crossing (a refetch wave of
  ~6.4k detail fetches, now snapshot-free). Each foreign ad re-snapshots ONCE at its first detail fetch
  after the deploy (the new hash), mostly inside the first crossing: about one ordinary wave, so
  `parsing idnes/churn` may read red once more. The crossing after it must read ~0 foreign
  re-snapshots, and the churn incident closes on that measured OK.

## 2026-10-23 — Health green sprint, wave 6: the old health layer goes (shipped with this PR)

Health dashboard sprint wave 9 (#1910), parked since 10-20, rebased onto main as the green sprint's wave 6: the
second engine behind the old Health page, which nothing has read since wave 8 rebuilt the page on one view. Plan:
[`docs/design/health-dashboard.md`](../docs/design/health-dashboard.md) § 6, wave 9, its "As built" note, and § 14's
"Wave 6 as built".

What it changes:
- **Dropped from the database** (migration 643, destructive, one transaction): 8 matviews, 12 views, 11 functions,
  4 tables (`ops_incident_runs` 611 rows, the retired check results 1,822, `portal_coverage_gate` 153,
  `dual_write_watermark` 22), the 5 columns of `ops_incidents` only the retired signature engine used, the `*/10`
  pg_cron job `refresh-health-dashboard`, 8 of the 18 rows of the derived-artifacts registry and 3 settings rows.
  The refresh cost 14.8 h of database time over its last measured 7 days (to 10-20).
- **`portals.scrape_cadence_minutes` goes in its own migration, 663**, applied right after 643: the web portal's
  migration 649 (applied before 643, replayed after it) inserts a row naming that column, and #1939's 650 must replay
  after 643, so no one number fits both.
- **Their writers, deleted in the same PR**: the images workflow's `refresh-stats` job with
  `scripts/refresh_image_stats.py`; the `active_db` and `walk_coverage` keys each walk wrote into
  `scrape_runs.by_category`, for the units and the orphans' entry (no Health check reads either; the coverage verdict
  is still logged as `COVERAGE`, and the RECONCILE and `VERIFY orphans` lines keep their `active=` counts); the
  broker sweep's `broker_resolution_last_complete` stamp; the per-m² census entry for a dropped matview; and the test
  rails that listed the old objects.
- **Kept on purpose**: `ops_incidents_open_signature_uq`, the index every new cell incident needs;
  `data_quality_snapshots`, frozen history (Q5); and `portals.supports_complete_walk` (the Scrapers page still reads
  it), `delist_flip_refusals` (still written) and the 150 unread ceskereality and mmreality slice rows, which stay
  with the scraper track.
- **Delivered in the database skill's order, not 627's**: the code still on `main` survives the drop (the image
  refresh catches the missing matview and skips; the by_category keys are JSON; only the broker stamp would return
  one unread row), so after the backup (label `health-legacy-643`) and a second 24 h zero-reads check, 643 and then
  663 are applied from this branch (on or after 2026-10-23 13:00Z, Q5), and the PR merges after, before the next
  daily broker lap (04:35 UTC).

Owed and stated (also in the design's notes):
- The backup holds no function, grant or job: the 11 function bodies stay in git (six byte for byte, five equal to
  their last migration once whitespace and comments are stripped; their live md5s, re-read 2026-10-23, are in 643's
  header), and there is no revert script, as for 618 and 627.
- `ops_incidents_open_recent_idx` (migration 462) was not on the list and stays; the 132 resolved signature-engine
  incidents stay as history (the backup keeps their dropped columns).

To check after the apply: the post-apply SELECTs in the PR (every dropped object gone, the cron job gone, the
registry at 10, `ops_incidents_open_signature_uq` present, no open non-cell incident); then, on Health's Platform
section, Jobs and Capacity list no `cron/refresh-health-dashboard` cell, Jobs no `artifact/` cell for the eight, and
Migration drift reads 643 and 663 Not measured (a drop declares nothing it can probe).

**Next:** the green sprint's remaining waves (W3 producers, W4 location producers, W5 rulings).
