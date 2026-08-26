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

## Wave status

| Wave | Scope | Status |
| --- | --- | --- |
| W0 stop the silent losses | Four one-file fixes, each currently *discarding* information: poller cursor loss + a poller-liveness check (W0.1), honest crash accounting at the shared `portal_runner` seam (W0.2), LLM check keyed on state not recency + a per-`called_for` starvation arm (W0.3), graceful degradation of the acute health lane under its 120s budget (W0.4) | 🟡 in progress (approved 2026-08-27) |
| W1 survivable write path | Non-transient errors at `_flush_drain_batch` become a bounded, loud quarantine (per-item replay → existing `_drain_record_failure` → `given_up` at 5) behind a circuit breaker; `write_rejects` counter + two new ingest `_CHECKS` entries | ⚪ proposed — needs operator sign-off |
| W2 migration file as contract | `-- apply:` / `-- assert:` header tokens above a watermark (≥ 444), asserts executed inside `migrations.yml`'s existing per-file apply loop and *validated as able to fail*, a live-catalog `check_migration_drift`, plus schema-vocabulary parity and the hot-DDL doctrine rails | ⚪ proposed — needs operator sign-off |
| W3 one alert, one place, cause attached | Failure signatures produced in-process at the write chokepoint (`checkviolation\|listings_area_basis_check` on all six portals), an `ops_incidents` table that exits through the **existing** `system_health` spine (no parallel channel), and the re-escalation ladder placed in `toolkit/system_alerts` where all 14 checks inherit it | ⚪ proposed — needs operator sign-off |
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

**Risk carried into the wave:** W0.3 correctly reds the acute lane on merge and
`--exit-nonzero-on-fail` emails hourly until the OpenAI account is topped up (open
question 1). Recovery latency after a top-up is the *producer's* cadence — expect a
bounded red tail of up to ~6h, not minutes. A bounded red tail beats a permanent false
green.

## Open questions blocking W1–W4

Eight, listed in full in the design doc. The load-bearing ones for sequencing: the
OpenAI top-up (gates W0.3's landing), **branch protection — `main` is currently not
protected, so every CI rail W2 adds is advisory until that changes**, alert routing +
volume tolerance (W3), the two-human-step Railway procedure (W4), and the disposition of
migrations 433 and 434.
