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
shared framework (`portal_runner`, `verify_pipeline.py`'s `_CHECKS`,
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
| W0 stop the silent losses | Four one-file fixes, each currently *discarding* information: poller cursor loss + a poller-liveness check (W0.1), honest crash accounting at the shared `portal_runner` seam (W0.2), LLM check keyed on state not recency + a per-`called_for` starvation arm (W0.3), graceful degradation of the acute health lane under its 120s budget (W0.4) | 🟡 in progress (approved 2026-08-27) |
| W0.5 the second week's fires | (a) ceskereality contract v4 + contract-immutability lockfile rail in `test.yml` — un-wedges the intake lane, closes the merged≠enforced gap for portal contracts; (b) `llm_liveness` recalibrated 4h → 13h to the real (post-dedup) producer cadence | 🟡 in progress (2026-08-31, shipped as hotfix PRs ahead of wave order — the lane was down) |
| W1 survivable write path | Non-transient errors at `_flush_drain_batch` become a bounded, loud quarantine (per-item replay → existing `_drain_record_failure` → `given_up` at 5) behind a circuit breaker; `write_rejects` counter + a `drain_write_rejects` check (`ingest_freshness` largely covered in parallel by the walk sprint's `acquisition_lag` + `walk_coverage` checks) | ⚪ proposed — needs operator sign-off |
| W2 migration file as contract | `-- apply:` / `-- assert:` header tokens above a watermark (≥ 444), asserts executed inside `migrations.yml`'s existing per-file apply loop and *validated as able to fail*, plus schema-vocabulary parity and the hot-DDL doctrine rails. **W2.3's live-catalog drift check shipped in parallel as PR #1204 (`check_migration_drift`)** — remaining W2.3 scope is hardening it, not building it | ⚪ proposed — needs operator sign-off (part shipped in parallel) |
| W3 one alert, one place, cause attached | Failure signatures produced in-process at the write chokepoint (`checkviolation\|listings_area_basis_check` on all six portals), an `ops_incidents` table that exits through the **existing** `system_health` spine (no parallel channel), and the re-escalation ladder placed in `toolkit/system_alerts` where all 19 checks inherit it | 🟡 W3.1–W3.3 shipped (migration 462), routing not flipped; W3.4 ladder outstanding |
| W4 apply becomes a job | Hot-table DDL applied out-of-band by a tested, extracted lock primitive (`attempts=1`, fail fast, escalate to a window — the retry-loop pattern is retired, not tuned); the existing PK-swap applier generalised into a dispatchable maintenance window | ⚪ proposed — needs operator sign-off + one human step (`REALTIME_WORKER_ENABLED`) |
| W5 constraint-only catalog diff | Named successor: diff CI's replayed schema against the live catalog. Catches 438 exactly, plus the ~241 migrations with no ledger row and no assert, plus the reverse drift direction W2 structurally cannot see — with zero per-migration convention | ⚪ named only, not built (open question 8) |

## W0 — in progress

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

**Not flipped, on purpose:** `system_health_channels` is still `[]`. Incidents ring the
in-app bell only until the operator chooses routing.

## Open questions blocking W1–W4

Eight in the design doc; question 1 (OpenAI top-up) is **resolved**. Still load-bearing
for sequencing: **branch protection — `main` is currently not protected, so every CI
rail W2 adds AND the new W0.5a contract lockfile rail are advisory until that changes**,
alert routing + volume tolerance (W3), the two-human-step Railway procedure (W4), and
the disposition of migrations 433 and 434.
