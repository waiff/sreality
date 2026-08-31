# Reliability Program: Applied Guarantees, One Voice for Failure

**Status:** W0 approved 2026-08-27 and in progress; W1–W4 proposed, pending operator sign-off on the open questions below
**Home:** `docs/design/reliability-program.md` + new `roadmap/reliability-track.md` (precedent: `docs/design/realtime-scrapers.md`, `docs/design/new-dedup/PROGRAM.md`)
**Occasioned by:** the 2026-08-26 six-portal ingest outage and its ten-email fan-out

---

## North Star

Two guarantees, and everything below exists only to serve one of them.

1. **Every merged migration is a live, applied guarantee.** A migration that merged but did not land must be loud within an hour, not discovered 29 hours later by a scraper fleet falling over. The oracle is the Postgres catalog — never the applied-migrations ledger, never a fresh-container replay.
2. **Every fleet-wide failure surfaces once, to one place, with its root cause attached.** Never N unrelated-looking emails for one bug. One event table, one bell namespace, one escalation policy — extended, never duplicated.

And a constraint on *how*: low maintenance means **fewer hand-copied patterns and more small, boring, reusable tools built ON the existing shared framework** — `portal_runner`, `scripts/verify_pipeline.py`'s `_CHECKS`, `notification_dispatches`/`channel_sends`, the `migrations/` directory and its CI replay loop. This program adds **two tables and zero dependencies**. Every other change is a predicate, a counter, a text rail, or a function lifted from nine copies into one.

---

## What happened (2026-08-26)

Between ~20:00 and ~21:30 UTC, six of nine portal scrapers (mmreality, ceskereality, maxima, bezrealitky, realitymix, remax) began failing every run with the same exception:

```
psycopg.errors.CheckViolation: new row for relation "listings"
violates check constraint "listings_area_basis_check"
```

**Root cause chain.** `scraper.area.derive_headline_area` returns `(value, 'plot')` for every `category_main='pozemek'` row carrying an area measure. `area_basis` sits in `db.LISTING_COLUMNS`, is not in `_PRESERVE_IF_NULL_COLUMNS`, and both write paths build their `ON CONFLICT SET` from the same `_listing_update_set_sql()` — so `area_basis = EXCLUDED.area_basis` on literally every detail write on every portal. Migration 423 added the column plus a five-token CHECK to *both* `listings` and `properties`, guarded by `if not exists (select 1 from pg_constraint where conname = ...)`. A stale four-token constraint of that exact name already existed on `listings` from earlier dev; the guard saw the name, skipped, and left `listings` on the wrong version while `properties` got the right one from the same file. Migration 438 was written to fix precisely that drift. It merged 2026-08-25 17:12 UTC and was applied 2026-08-26 22:06 UTC — **29 hours later, by the session investigating the outage.**

Exposure: 55,994 active `pozemek` rows carrying a non-NULL `area_m2`. sreality is the only structurally immune portal (it puts the parcel in `estate_area`). **idnes is not immune — at 24,335 rows it is the largest exposure**; it simply had not reached a land row in that window. bazos survived on one row of luck.

**Four systemic gaps, each independently flagged as patchwork by its own investigation.**

**Patchwork flag 1 — migration apply is not a mechanism.** There is no apply workflow, no apply script, no runbook step, no ordering enforcement, no reconciliation. The real order is **apply-then-merge during authoring**: 437, 439, 440, 441, 442 and 443 all carry ledger timestamps 20 minutes to 11 hours *before* their own merge commit. 438 is the sole inversion — which reframes the incident entirely. 438's apply was not forgotten; it was **attempted, lost the lock race its own `DO` block raises on, and the PR merged anyway**, because apply-success and merge-success are uncoupled and the PR body (ending "Full suite: 4,969 passed") has no slot for apply status. This class has recurred at least four documented times (migrations 025/026/052 headers; 337/338/339 in the PK-swap runbook) and each time was answered with a better comment. A second unapplied migration is live right now (433) — but deliberately, and nothing can tell the two cases apart.

**Patchwork flag 2 — hot-table DDL is tribal knowledge, hand-copied.** Nine independent implementations of "take a strong lock on a hot table without head-blocking production" — eight in `scripts/`, one inlined as PL/pgSQL in 438 — with mutually contradictory constants (3s/40/5.0s, 3s/10/20.0s, 5s, 6s/10/3.0s: total budgets from ~30s to 200s). Zero tests over any retry or lock path. The authoritative guidance — "a short `lock_timeout` never queues, so you never win"; "`authenticated` has an 8s statement_timeout, so any head-block beyond that turns SPA reads into HTTP 500s"; "exclude autovacuum, it is auto-cancelled" — exists **only in a Claude memory file**, not in CLAUDE.md, the `database` skill, `docs/architecture.md`, or any comment a future author would encounter. The MCP session runs `lock_timeout='0'` (unbounded), so protection is entirely opt-in prose; across ~74 lock-aware migrations five different values are used.

**Patchwork flag 3 — two ops-observability surfaces that do not know the other exists.** `workflow_failures` / `workflow_run_health` (poller → Health card) knows *which* workflows are red and never *why* — it stores `run_id, workflow_name, workflow_path, conclusion, run_started_at, html_url` and no failure reason at all. `pipeline_check_results` / `system_health` (verify_pipeline → bell) can alert and has an edge-triggering discipline, but none of its 14 checks observes scrapers. **The incident fell exactly in the seam**: the surface that could alert had nothing to say about ingest, and the surface that knew ingest was red has no alerting path. Meanwhile external delivery is 95% shipped and 0% switched on (`system_health_channels = []`; 249 system_health alerts in 30 days, zero delivered externally), and the last 5% contains a bug — the outbox RETRY pass hardcodes `cs.consumer IN ('watchdog','collection_monitor')`, so ops alerts get one attempt where price-drop alerts get five.

**Patchwork flag 4 — the write path has no non-transient error handling at all.** `run_resilient` is a good mechanism for *transient* errors with a documented idempotency contract. For non-transient ones there is nothing: `CheckViolation` is an `IntegrityError`, `is_transient_db_error` matches only `OperationalError`, so it re-raises on attempt 1 and the drain dies. Worse than "one bad row kills its batch": the crash path never reaches `fail_detail`, so the poison row's `listing_detail_queue.attempts` never bumps, `given_up` never trips, `reclaim_stale_claims` releases it unchanged, and `claim_detail_batch` orders `priority DESC, enqueued_at` — **the same poison cohort is re-claimed, re-fetched (an impolite wasted HTTP request), and re-crashes forever.** A permanent wedge with no self-healing path. And it was invisible in the database: `_run_phase`'s `finally` calls `_finalize(run_id, {}, drain=True)`, which stamps `ended_at` and leaves `errors` untouched — **`scrape_runs.errors = 0` for every one of the six portals GitHub emailed about.** Nine `*_main.py` files carry that same copy-pasted bug.

**Also observed in the window, and instructive.** OpenAI has been credit-exhausted since 2026-08-15 07:47 UTC — 11 days, 63,547 error rows, zero successful calls — and `llm_errors` read `ok` for most of it, because `currently_failing` `and`s in an unconditional 90-minute staleness gate. Same outage, `fail` at 14:02:08 and `ok` at 14:58:15 with `value=1.0` at both samples. Since edges ring the bell, that produced **114 alert rows alternating onset and a literal "✓ Recovered: llm_errors is healthy again"** for an outage that never recovered. `llm_burn_rate` reported `ok value=0.0` throughout: it has only upper arms, and `_record_failure` writes `cost_usd=0.0`, so a total outage drives spend to the *maximally healthy* number. The thing that actually reached the operator was an incidental "Run failed" email from a script exiting 1. **The purpose-built health system was not silent — it was loudly wrong.**

The symmetric failure on the same surface: `property_maintenance` has been `fail` continuously since 2026-08-20 13:08 UTC and its last alert was 2026-08-20 11:37 UTC. Six days red, six days silent. One edge-triggered rule produces both pathologies simultaneously.

---

## Addendum — the second week's fires (2026-08-31)

Four days on, the original incident is **closed and verified with data, not green runs**:
`area_basis='plot'` went 0 → 344+ rows across mmreality/ceskereality/bezrealitky within
hours of the constraint fix, and the portal fleet has had exactly **one** failure since
2026-08-27 (a transient mmreality 403 through the residential proxy, recovered next run).
OpenAI credits were topped up ~2026-08-30; the LLM pipeline is measurably healthy again
(595 ok / 0 err calls, $1.53 spend in the trailing 24h).

The operator's inbox, however, kept filling — from two NEW sources, and the pattern is
the program's thesis restated:

**Fire 1 — the location claim-intake lane, dead since 2026-08-27 22:32 UTC (14
consecutive failures).** PR #1209 (the walk-coverage sprint's ceskereality kraj
partition) edited two *prose* fields in `contracts/portals/ceskereality.yaml`
(`fetch.robots_note`, one `extractions[].notes`). Both sit inside the bytes
`contract_sha256` governs (everything minus `persistence:`/`shadow:`, W2a-3e), the
version was not bumped, and `project()` — correctly — refuses the mismatch at startup,
killing intake for **all** portals, not just ceskereality. There is **no CI gate** over
contract immutability: the invariant is enforced only in production, only after merge,
only by the failing job itself. That is the migration-438 shape again in a different
domain: *a repo-declared truth whose sole enforcement point is production runtime*.
Fix shipped as **W0.5a**: contract v4 bump (the doctrine's own remedy, 02 §2.1.8 — the
prose is now the accurate operational record, so reverting it would be the dishonest
fix) plus a **lockfile rail** — `contracts/portals/contracts.lock.json` mapping each
portal to its (version, governed-hash), verified by a test that imports the loader's own
`contract_body_hash` (no second parser), so an edit-without-bump fails `test.yml`
pre-merge.

**Fire 2 — the acute-health monitor false-redding hourly (8 failures).**
`check_llm_liveness` fails at 4h of LLM silence. Its docstring's own justification —
"dedup vision on the always-on worker … p99 inter-call gap ~1 min, so the 4h default
never trips in normal operation" — describes a workload **deleted 2026-08-06** (rule 15).
The only recurring producer left is the enrichment cron: nominal 6h, observed 7h+ under
Actions throttle. A 4h threshold against a ≥6h producer is a guaranteed periodic
false-red — principle 2's exact failure mode (an arm calibrated to a dead premise).
Fix shipped as **W0.5b**, folded into W0.3's PR: default raised to 13h (2× nominal
cadence + throttle slack), docstring rewritten to name the real producer.

**What this changes in the program — and what it does not.**
- It **answers "would W1+ have helped?" honestly: no.** W1 hardens the portal write path,
  which is currently green. Neither fire is a portal-scraper defect. W1 stays queued as
  designed — the next poison-row event remains a matter of time — but it is not the
  urgent lane.
- It **generalizes W2's principle** beyond migrations: the program's first guarantee
  should read "every merged *declaration* is a live, enforced guarantee" — migrations
  (W2), portal contracts (W0.5a's lockfile, shipped early because the lane was down),
  and code-side vocabularies (W2.4's parity check) are three instances of one class.
- It **strengthens W3's evidence base**: 14 identical `ContractError` emails over three
  days, plus 8 identical liveness false-reds, with nothing grouping either cluster — the
  ten-email symptom reproduced twice in one week. W3 remains the highest-leverage
  operator-facing wave.
- **Timeliness note, out of scope here:** mmreality data ran ~7.3h stale on a 6h-cadence
  portal overnight — GitHub Actions cron throttle jitter (runs observed 80–256 min late
  fleet-wide). That is the standing problem the realtime-worker program owns; this
  program does not fork into scheduling.

---

## Design principles

1. **Assert against the catalog, never the ledger, never a replay.** The ledger is not an oracle: 195 of 470 rows carry no `NNN` prefix; one file routinely produces N rows; 424–427 are applied with *no ledger row at all*; 434 has two ledger rows and no repo file. A naive existence check fires ~6 false alarms per 20 migrations against 1 true positive. And CI is structurally blind by construction — on a fresh replay 423's guard finds nothing and creates the *correct* constraint, so the broken and fixed production states produce **byte-identical green runs**. Only the live catalog can see this.
2. **A guard that cannot fire is worse than no guard.** This is not our phrase — it is `tests/test_migration_catalog_guards.py`'s own docstring, written after migration 432's `to_regproc` guard turned out to be unfirable. Any assert we add must be *validated as able to fail*, not merely present.
3. **A permanently-red check trains the operator to ignore every check.** `verify_pipeline.py`'s own comments say this. Parked migrations, first observations, and budget exhaustion must be expressible as something other than `fail`.
4. **Extend the single chokepoint; never fragment it back (rule 21).** One bad token broke six portals because there is one write path — that is a *feature*. The fix belongs at that one seam, not as nine per-portal `try/except`s. Same for run accounting: lift `_run_phase`/`_finalize` into `portal_runner` rather than fixing nine copies.
5. **One alert spine (rule 16, corrected).** Migration 274 already widened `notification_dispatches`' source CHECK and `channel_sends.consumer` to a third producer, `system_health`, and `api/notification_outbox.compose_message()` already has its branch. Rule 16's "two producers" text is stale; the settled architecture is one spine with a producer discriminator. **We do not build a second one.**
6. **Migrations stay append-only, declarative records (rule 1).** Nothing here edits a merged file. New obligations attach via structured header comments on *new* files above a watermark, or via an append-only sidecar. The lock choreography moves *out* of migration files into one tested tool — which strengthens rule 1 rather than bending it.
7. **Silence is not recovery.** A failure is superseded only by a newer success, never by elapsed time. This single rule fixes the LLM false-green, and it generalizes: as soon as a producer has a circuit breaker (the enrichment loop aborts at exactly 5 consecutive errors), any recency-window detector downstream is sampling a duty cycle, not a state.
8. **Stdlib only (rule 7).** No Sentry, no PagerDuty, no Supabase CLI, no new HTTP or DB library. Everything below is `re`, `psycopg`, `requests`, and SQL.

---

## Program waves

Five waves, ~17 PRs. W0 and W1 are independently valuable and should ship regardless of the program's fate. W2–W4 build on them.

### W0 — Stop the silent losses (4 PRs, small, low risk)

**Goal:** four one-file fixes, each of which is *currently discarding information*. No new tables, no new surfaces, no new concepts. This is the wave that makes every later measurement trustworthy.

| # | Change |
|---|---|
| **W0.1** | **Poller cursor loss.** `scripts/record_workflow_failures.py`: when `MAX_PAGES=5` is hit, `new_cursor = min(completions)` — and because the cap was hit, every completion seen is *newer* than `since`, so the cursor moves **forward past the uncovered window**. Those runs are then filtered out next poll by `completed_at < since` and dropped **permanently**. The in-code comment ("crawling to oldest-seen … picked up next poll") is false. Fix: leave the cursor at `since` when capped and raise the page budget; log the shortfall. Add a `workflow_poller_liveness` check keyed on `app_settings.workflow_failures_cursor` age — the poller excludes its own runs from the table it feeds, so a dead poller is currently invisible. |
| **W0.2** | **Honest crash accounting.** Lift `_run_phase`/`_finalize` out of all nine `*_main.py` into `portal_runner`, and make the exception path stamp a non-zero error state instead of finalizing green. Nine copies → one; nine bugs → zero. |
| **W0.3** | **LLM state, not LLM recency.** In `check_llm_errors`, drop `min_live_at` from the `currently_failing` derivation and set it from `last_ok_at < last_err_at`. Move the derivation into the file's pure-status section so it becomes unit-testable — today's tests pass `currently_failing` in as a literal and pin the arms while never exercising the code that was wrong. Add a starvation arm to `llm_burn_rate`, evaluated **per `called_for`** (mirroring `_status_for_llm_errors`'s existing `per_called_for` offender idiom): `attempts > 0 AND successes == 0 AND spend == 0` → `fail`; `attempts == 0` → `ok`, flagged idle (silence is `llm_liveness`'s axis). A 24h-aggregate arm would be defeated by a single unrelated $0.01 success — verified: one `summarize_region_dispositions` call held `llm_burn_rate` at 0.01 for ~24 of 30 sampled hours during a total outage of the only recurring lane. Carry `details.arm` (`starved`/`runaway`/`idle`) so `value=0.0` on a red is legible. Fix the operator copy in the same diff — it currently names Anthropic and "dedup vision", a subsystem deleted 2026-08-06 (rule 15). |
| **W0.4** | **Make the acute lane degrade gracefully.** `run_checks` computes *all* results before `write_results` persists *any*, inside a job with `timeout-minutes: 5`. A timeout therefore writes zero rows and fires zero alerts — blinding `db_saturation` and `worker_liveness` at exactly the moment DB saturation would make checks slow. Persist per check as it completes, give each check a wall-clock budget that returns `warn` rather than running long, and set an explicit lane budget (**120s of the 300s job timeout**) that any new check must fit. This wave owns that number; W2 and W3 spend against it. |

**Why not patchwork:** each of these deletes a duplicated or wrong mechanism rather than adding one. W0.2 removes eight copies. W0.3 removes a magic constant whose justifying workload was deleted three weeks ago. W0.4 fixes a failure mode that three separate proposals were about to make worse by piling onto the same job.

**Risk:** W0.3 reds the acute lane on merge and `--exit-nonzero-on-fail` emails hourly until the OpenAI account is topped up. **Land W0.3 with the top-up.** Recovery latency after a top-up is the *producer's* cadence — the enrichment cron is nominally 3-hourly and observed at up to 6h under Actions throttle — so expect a bounded red tail of up to ~6h, not "minutes". A bounded red tail beats a permanent false green.

### W1 — The write path becomes survivable (3 PRs + 1 additive migration, medium, medium risk)

**Goal:** one bad row becomes a bounded, loud quarantine instead of a permanent fleet wedge. All at the **one shared seam**, `portal_runner._flush_drain_batch`.

- On a **non-transient** error from `run_resilient` (transients are already handled), replay the buffer item-by-item via `portal.write_details(c, [item])`. Safe for all nine — the upserts are idempotent, which is the same property `run_resilient` already depends on, and it works for sreality's atomic `write_detail_batch` too. Successes complete normally; each rejecting item goes to the **existing** `_drain_record_failure`, which bumps `listing_detail_queue.attempts` and trips `given_up` at 5. That alone converts a permanent wedge into a self-limiting quarantine.
- **Circuit breaker — this is what makes isolation safe.** If the per-item replay rejects more than a small fraction of the batch (>20% or >5 items), do **not** quarantine: re-raise and let the run go red. A whole-batch rejection is a schema or code break, not bad rows; quarantining it would silently retire 30k+ queue rows to `given_up` within five runs. (The queue is at 37,028 rows today, up from the 30,632 migration 438 recorded — it is growing.)
- **Rejects are loud, never silent.** One additive migration adds a `write_rejects` counter to `scrape_runs`/`bump_scrape_run_counts`. The contract becomes: *the row is not written, and the system says so within the hour* — never a silent skip. This respects the toolkit's facts-not-opinions rule.
- **Two new `_CHECKS` entries** (the registry has 14, none of which observes ingest at all): `ingest_freshness` (per-source detail-drain progress / write freshness — six portals stopped writing and nothing could notice) and `drain_write_rejects` (non-zero rejects in the last N hours).
- **Reject records carry the exception class and constraint name.** This is W3's primary signature source, captured at t+0 at the chokepoint, with no log download.

**Why not patchwork:** the fetch path already has exactly the mechanism the write path lacks (`_drain_record_failure` → attempts bump → `given_up` at 5). We are wiring an existing mechanism to a second caller, not inventing one — and doing it once for nine portals rather than nine times.

**Risk:** the breaker threshold is a judgement call. Set it conservatively (fail loud) and tune from `write_rejects` data.

### W2 — The migration file becomes a contract (4 PRs, small–medium, low risk)

**Goal:** intent becomes machine-readable, asserts become executable, and the doctrine that currently lives in a memory file becomes a rail.

**W2.1 — Intent as data.** Every migration numbered ≥ **444** (mirroring the existing `GRANDFATHER_MAX = 304` watermark idiom in `tests/test_migration_numbers.py`) carries:

```sql
-- apply: required
-- assert: select convalidated from pg_constraint where conname = 'listings_area_basis_check'
-- assert: select pg_get_constraintdef(oid) like '%''plot''%' from pg_constraint
--         where conname = 'listings_area_basis_check'
```

`required` is the **implicit default**; the single documented escape hatch is `-- apply: skip — <reason>`. We deliberately reject the proposed three-token taxonomy (`required` / `awaiting-signoff` / `never-production`): its two motivating cases both fail on inspection. 348's end state (`listings_pkey` on `id`) *is* true in production, so it passes as `required` with no special token. 433 is the one genuinely parked case — and it sits at 433, below any watermark, so the machinery would never touch it. Rule 7's "no new dependencies without justification" applies to vocabulary too; add a third state when a second real case appears.

The offline shape rail **extends existing files** (`test_migration_catalog_guards.py` for assert shape — literally the "can this guard fire" file; `test_migration_numbers.py` for token presence) and imports the existing `_statements`/`_strip_comments` helpers rather than adding a sixth migration-text parser. An append-only sidecar `migrations/asserts/NNN.assert.sql` may attach an assert to an already-merged file — safe, because the CI loop globs `migrations/*.sql` and will not descend (the `migrations/reverts/` subdirectory already proves this). **Scoped explicitly: a sidecar exists to attach an assert to a migration under active drift suspicion, never as a backfill project.** We are not retro-asserting 478 files.

**W2.2 — Assert power is validated, not assumed.** This is the blocker the reviewers were right about: a shape rail requiring "one token, ≥1 assert, select-only, names a catalog relation" is satisfied in full by `select to_regclass('listings') is not null`, which can never be false — and 423's own plausible assert ("the constraint exists") was **true in the broken state**. Fix, and it is nearly free because the loop already exists. `migrations.yml` applies files one at a time:

```bash
for f in $(ls migrations/*.sql | sort); do psql -v ON_ERROR_STOP=1 -q -f "$f"; done
```

Run each `required` file's asserts **inside that loop, immediately after its apply, and require TRUE**. This is universally valid and catches typos, wrong catalog names, and asserts that can never be true. An opt-in `-- assert-before-false:` arm additionally proves the assert *discriminates* — opt-in rather than mandatory precisely because 438 could not satisfy it: on a fresh replay the constraint already has `'plot'` from 423, so its before-state is legitimately true.

**W2.3 — The live catalog check.** One new `check_migration_drift` in `_CHECKS`. Execution model, corrected: `verify_pipeline` imports `scraper.db.connect`, which is **autocommit** — so `SET LOCAL statement_timeout` outside a transaction is a no-op that raises only a WARNING, which is exactly the silent-no-op class this repo keeps writing rails against. Each assert therefore runs inside `with conn.transaction():` with `set local statement_timeout='3s'` and `set local transaction_read_only = on` — read-only enforced by the **engine**, with the select-only text match kept only as a lint (`select some_volatile_fn()` passes a text match). A total wall-clock budget from W0.4's allocation returns `warn` with "budget exhausted, N of K asserted" rather than running long. A first failing observation is `warn`; only a second consecutive one is `fail` — which absorbs the merge→apply window (normally negative under apply-then-merge, but up to an hour for any session that merges first) using `latest_statuses`, which already reads the prior run. Do **not** reach for git timestamps: `actions/checkout@v6` defaults to `fetch-depth: 1`. **Ship into the 6h full lane, soak for a week, then add to the hourly `--only` list.**

**W2.4 — Two riders on the same parser.** (a) A `schema_vocabulary_parity` check: `scraper/area.py` declares `AREA_BASES` as a frozenset and it is referenced in exactly two places, both tests, both asserting the code against *itself*. Diff declared code-side vocabularies against the live constraint's token set — **this would have gone red the hour 423 was applied**, with no dependence on anyone remembering 438. (b) The hot-DDL doctrine rails, since they live in the same header territory: assert that a migration touching a hot table sets a `lock_timeout` at all (today it is convention — 60×`5s`, 10×`3s`, 3×`6s`, 2×`8s`, 1×`30s`, and the MCP session default is `0`, i.e. unbounded); forbid `8s` and above on hot tables, since it equals `authenticated`'s `statement_timeout` and converts SPA reads from waits into HTTP 500s; and forbid stating a lock mode in prose unless it matches the real one — **migrations 314 and 350 both claim `ADD CONSTRAINT ... CHECK ... NOT VALID` takes "a brief SHARE ROW EXCLUSIVE lock". It does not; that is the `ADD FOREIGN KEY` mode. `ADD CHECK` takes ACCESS EXCLUSIVE**, so under the one-transaction wrap those two held a *reader*-blocking lock through a full heap scan on `listings` and `images`.

**A correction this program owes the record.** Earlier drafts claimed nine migrations were "INERT as applied" because the MCP wraps multi-statement payloads in one transaction (proven empirically: `now()` lagged `clock_timestamp()` by exactly 3.01s across three statements). The catalog does not support the strong reading — **exactly one constraint in the public schema is unvalidated**, on a dead table from the removed dedup engine. So "inert" means the VALIDATE ran under the wrong (ACCESS EXCLUSIVE) lock: an **availability** problem, not a validity one. The mis-documented set is roughly `276_*`, `311_*`, `314_*`, `350_*`, `411_*`, `438_*`; migration 264 is a **counter-example** that already states the hazard correctly ("NOT VALID + VALIDATE in ONE migration … does NOT get the two-transaction benefit"), and 301 targets non-hot tables. Cite these by **filename** — duplicate migration *numbers* exist on main (two `276_*`, two `301_*`).

**Scope limit, stated plainly:** this design detects "repo says X, production lacks X" only. It cannot see "production has X that no repo file creates" — 434 is a live instance (two ledger rows, no file). That direction is only reachable by the named successor below.

**Named successor, deferred not hidden.** CI already builds a complete, correct schema from the migration chain on every `migrations/**` push. A **constraint-only catalog diff** of that replayed schema against production would catch 438 exactly, plus the ~241 migrations with no ledger row and no assert, plus the reverse direction — with zero per-migration convention and zero hand-written asserts. It is tractable (684 public constraints: 201 CHECK, 217 FK, 75 UNIQUE). The honest counter is that a full diff has a bad cold-start noise profile and would be permanently amber for weeks — the exact trap principle 3 warns about. **W2 is therefore a bridge with a named successor, not the terminal answer.** Note the intent token has *more* value there (433 is applied in the CI replay but not in prod, i.e. a permanent expected-diff entry).

### W3 — One alert, one place, cause attached (5 PRs + 1 additive migration, medium–large, medium risk)

**Goal:** the ten emails become one, and that one carries the `CheckViolation` text.

**W3.1 — Failure signatures, from the chokepoint first.** `scripts/failure_signature.py` (pure, stdlib `re`) normalizes an exception into a workflow-independent key: strip digits, UUIDs, timestamps, paths; keep quoted identifiers; lowercase; truncate. `psycopg.errors.CheckViolation: … "listings_area_basis_check"` → `checkviolation|listings_area_basis_check` **on all six portals, because the signature is derived only from the error text and never from `workflow_path`.** That asymmetry is the whole mechanism.

Two producers, in priority order:
- **Primary: in-process, at the write chokepoint.** W1 already has the exception in hand inside `_flush_drain_batch`. This produces the correct signature at t+0 for all six portals, with zero log downloads and zero Actions-API cost. It is the only shape faithful to rule 21, and the only one with any chance of beating GitHub's ~1-minute email.
- **Backstop: the poller's log tail**, for reds with no Python exception (timeouts, `startup_failure`, cancels). Fallback key is `step:{name}|exit:{code}` **scoped by `workflow_path`**, so unreadable reds cannot merge into a meaningless mega-incident. Two hazards must be handled and were absent from the proposal: `/actions/jobs/{id}/logs` returns a 302 to a signed blob URL, and `urllib.request.urlopen` follows it **carrying the `Authorization: Bearer` header to a third-party host** — disable redirect-following, re-request the `Location` bare, and cap bytes read (a detail-drain log is megabytes, inside a 5-minute job).

**W3.2 — `ops_incidents` (one new table).** One open row per signature (partial unique index on `signature WHERE resolved_at IS NULL`), accumulating `failure_count`, `workflow_paths[]`, a sample run URL, and the log excerpt. Auto-resolve when every member workflow has `workflow_run_health.last_success_at > last_seen_at` — that table already exists and already holds exactly this. **Plus a manual resolve path and a max-age auto-close**, because a workflow that is retired, disabled, moved (which forks `workflow_path`), or simply unscheduled will never post a success, so without them its incident escalates forever — a machine for manufacturing the exact fatigue this wave exists to end.

**W3.3 — It exits through the existing spine. We reject the parallel one.** The reviewed proposal wanted `ops_incident_alerts` + `ops_alert_email` + `toolkit/ops_alerts.py`. **Dropped**, for a reason worth stating in full: migration 274 already widened `notification_dispatches`' source CHECK to a third arm (`source_kind='system_health'` with both `subscription_id` and `collection_id` NULL), already widened `channel_sends.consumer` to include `'system_health'`, already seeded `app_settings.system_health_channels`, and `api/notification_outbox.compose_message()` already has an explicit `system_health` branch rendering the stored message verbatim under "Systémové upozornění". `toolkit/system_alerts.py`'s own docstring states the intent: *"reusing the whole existing Notifications surface instead of a parallel alerting path."* The bell **already means CI health** — 14 checks ring it. A new spine would duplicate a working ledger, recipient resolver, claim→send→terminal-status flow, and transport wiring; it would be a second bell namespace, which is precisely the shape the WS4 rebuild removed ("one detection codepath, one bell namespace"); and it would re-introduce an import-hygiene hazard, since `monitor_workflow_failures.yml` installs base deps only and `api/transports/` is one import away from a silent break in a lane with no test. So: `ops_incidents` **emits a `system_health` dispatch row**, and the shipped outbox delivers it.

Required with it: **fix the outbox RETRY allowlist** (`api/notification_outbox.py:240` — the entire delta between "ops alerting exists" and "ops alerting is reliable"), and flip `system_health_channels` + `notification_email_to`. Update **rule 16** in the same PR per the same-PR-doc-update rule: three producers, and the `system_health` arm is not property-grain.

**W3.4 — Escalation policy moves to where every producer inherits it.** "Red for six days, silent for six" is a property of `toolkit/system_alerts.emit_transition_alerts`, which governs all 14 checks — not of ops incidents. Putting a ladder inside `ops_incidents` would leave `property_maintenance` (the cited example) broken. So the **6h / 24h / 72h / weekly re-escalation ladder lands in `system_alerts`**, once, for everybody. Two existing signals get wired in rather than rebuilt: `migration 220`'s `consecutive_failures` + `is_chronic` (streak ≥ 3) already computes chronic-vs-transient with no alerting attached, and migration 274 already ships a `verification_stale` self-watchdog. Use `verify_pipeline`'s existing (currently empty) `--weekly` lane for the heartbeat rather than inventing one.

**W3.5 — Keep GitHub's per-run email on. Record this as a decision, not a deferral.** `llm_health.yml`'s own header calls the exit-1 email "the belt-and-braces channel for when the in-app bell path itself is down" — it is the only channel independent of Supabase, and during the LLM outage it was the *only* thing that worked. The reviewed proposal gated its removal on "the new path alerting at or before the GitHub email", which **cannot pass**: the poller's `*/30` cron actually runs 80–256 minutes apart under Actions throttle. The honest, achievable goal is the one that matters anyway: **one alert instead of ten, with the exception text attached.**

**Success measures**, split so an input failure cannot be mistaken for a clustering failure:
- (a) **Input coverage:** after W0.1, every red in a replayed window appears in `workflow_failures`. Today only 2 of the 6 affected portals were ever recorded — no normalizer can cluster six workflows out of two rows.
- (b) **Clustering:** the 2026-08-26 window replays to exactly one incident spanning the workflows present in the input.
- (c) **Volume:** incidents/day in the low single digits against a ~100 failures/day baseline (peak 149 on 08-25, across 9–18 distinct workflows). **Commit to onset thresholds only after a W0 measurement pass** publishes the signature histogram over the recorded corpus — `failure_count >= 2` is nearly free in practice, since one poll pass covers 80–256 minutes and a continuously-failing `*/30` workflow contributes 3–8 failures per pass.
- (d) **End-to-end recovery signal for the original incident:** `select count(*) from listings where area_basis='plot'` > 0. It was still 0 after the constraint was fixed — green workflow runs are not proof the fleet recovered.

### W4 — Apply becomes a job, not a side effect of authoring (3 PRs, medium, medium risk + one human step)

**Goal:** a lost lock race becomes a retryable job with its own escalation, instead of a silently-dropped intention.

**The finding that sets the shape:** every hot-table `ALTER` in this repo holds ACCESS EXCLUSIVE for **milliseconds** — a scan across every hot-table ALTER in `migrations/` returns only 3 scan-forcing forms. The problem has never been lock *hold*; it is lock *acquisition*. And a short `lock_timeout` plus retry is the one strategy structurally guaranteed to lose under continuous traffic: you abandon a FIFO queue before reaching its head, rejoin at the back, and add queue pressure for every other waiter. **223 lost races across five escalating loops is the predicted outcome, not bad luck.** So we retire the pattern; we do not tune it.

- **W4.1 — Doctrine, stated once, in the `database` skill and CLAUDE.md.** Hot-table DDL is applied **out-of-band by the script**; the migration file carries the idempotent replay form for CI and the append-only record. This convention already exists unnamed in-repo (migration 429: *"The statement below is also the replay/CI record"*; 313: *"On the LIVE database these same end-states were reached online, out-of-band"*). It changes the standing "applied via the Supabase MCP" instruction, so `database/SKILL.md` and rule 1's prose update **in the same PR**. Corollary rule: **`VALIDATE` always goes in its own apply call**, because the MCP wraps a payload in one transaction while CI's `psql -f` (no `--single-transaction`) commits per statement — production and CI have different apply semantics for the repo's single most important online-DDL technique. That split is expressed by consuming W2.1's header token (e.g. `-- apply: split-calls`), **not by inventing a second header convention** — W2 owns the format, W4 consumes it. Also: `CREATE INDEX CONCURRENTLY` is impossible through `apply_migration` (25001), and `scripts/apply_r2_constraints.py` already solves it by flipping `conn.autocommit` on a session connection; that belongs in the doctrine too.
- **W4.2 — One tested primitive, extracted not invented.** `scripts/apply_r2_constraints.py:126` `_with_lock_retry` already retries `LockNotAvailable` **and** `DeadlockDetected` under a per-attempt `lock_timeout`, already raises on exhaustion, and already documents *why* deadlock is routine here (ADD FK locks child-then-`listings`; the ingest path locks the reverse, so either side can be the victim — a hazard 438's hand-written `DO` block silently regressed by catching only the first). The honest delta is: promote it to a shared module, add the two commit semantics the `database` skill already distinguishes (`run_each_committed` vs `run_as_one_transaction`), add a `lock_holders()` diagnostic, add tests. **Default `attempts=1`: the tool's job is to try, fail fast, and escalate to a window — not to grind.** A 2s×5 loop would be *shorter* than the 6s×10 that already lost, and would collide with W2.4's ≤2s hot-table ceiling; naming it a "retry loop" at all would re-import the belief we just disproved. `lock_holders()` queries `pg_locks ⋈ pg_stat_activity` for any non-autovacuum holder of the target relation (autovacuum is auto-cancelled), reusing `verify_pipeline`'s existing `_LONG_OPEN_TXN_TOP_SQL` idiom rather than a fresh style. This replaces 438's `query like '%rebuild\_%'` guess, which is **blind to pg_cron jobid 1** — `refresh_health_matviews`, `*/10`, avg 173.7s / max 900.1s, refreshing five matviews that read `listings`, the largest recurring cron holder — and which tests the wrong population entirely, since cron occupies only 45.9% of wall-clock while the dominant blockers are app connections it cannot see.
- **W4.3 — Generalize the window that already works.** `scripts/apply_listings_pk_swap.py` + `.github/workflows/apply_listings_pk_swap.yml` is a complete 361-line applier: `--preflight` / `--window --confirm` / `--resume-cron` / `--rollback`, worker-heartbeat and in-flight-run gates, cron resumed in a `finally`, run from Actions on a session connection. Parameterize it by migration number. **The unlock: `cron.alter_job(job_id, active)` already works in-repo** (`_set_cron_active`, line 181) — the standing belief that pg_cron cannot be paused programmatically, which drove the entire retry-loop design, is false. That belief is also asserted in a stale comment at `tests/test_cron_statement_timeout_guard.py:30` ("no `cron.alter_job` usage exists as of this writing"); correct it in the same PR. With jobs 1/6/7 pausable and the worker gated, a real maintenance window is one dispatch. A failed window escalates through W3's `ops_incidents`, not a bare Actions email.

**Be honest about the ceiling:** the window needs `REALTIME_WORKER_ENABLED=false` on Railway, which an agent cannot set — the script's own docstring says so. W4.3 is a two-human-step procedure. That is acceptable precisely because W4.1 keeps it rare.

**Why W4 is last despite being closest to root cause:** W2 converts a failed apply from 29 hours of silence into ≤1 hour of noise, which removes the urgency *and* gives W4's window tool a real gate to test against.

---

## Explicitly out of scope for this program

- **The Supabase CLI / `supabase db push`.** New dependency (rule 7), a rename of all 478 files to `<timestamp>_name.sql` (rule 1), and its ledger-diff-then-replay model is documented **in this repo** as unsafe here: the PK-swap runbook warns `db push` would see 337/338/339 as pending and re-apply them, and 339 is not a no-op against already-swapped production. The same hazard now covers 424–427 (applied, unledgered) and 433/348 (must never run).
- **Auto-applying migrations from CI to production.** Requires production DB credentials in CI and re-imports the not-idempotent-replay hazard. W4's window is dispatch-only and human-gated.
- **A parallel ops channel** — no `ops_incident_alerts`, no `ops_alert_email`, no Sentry/PagerDuty/Slack integration. One spine.
- **Retro-asserting historical migrations.** The sidecar is for a file under active drift suspicion, never a backfill project.
- **Editing, renumbering, or reverting merged migrations**, including the duplicate numbers below `GRANDFATHER_MAX = 304` and the two live duplicates (`276_*`, `301_*`).
- **Per-portal error branches.** Every fix in W1 and W3.1 lands at the shared seam or not at all (rule 21).
- **Recalibrating `llm_burn_rate`'s upper tiers.** The $90/$150 arms were sized for dedup-vision burn deleted 2026-08-06 and are currently unreachable (24h spend ~$0.01). **Explicitly parked, with the reason recorded**, pending real burn growth — rather than silently left as two arms that cannot fire in a check whose whole point is that arms must be able to fire.
- **Structured provider-error classification.** The proposed switch from prose substrings to the `"type": "insufficient_quota"` / HTTP 429 tokens is *not* load-bearing for this incident — `%no credits remaining%` already matches, and the stated justification was wrong twice (SQL `LIKE`'s `_` is a wildcard that makes a pattern *more* permissive, not less; and `credit_balance_exhausted` does not appear in the stored string). It has real residual value on a different argument — a provider's structured error `type` is an API contract while its prose is not — so it rides as a small optional PR in W3, or waits.
- **The reverse drift direction** (production DDL with no repo file, e.g. 434). Named as the constraint-diff successor's job; W2 cannot see it and the doc must not imply otherwise.
- **Anything touching the removed dedup engine** (rule 15).

---

## Open questions for the operator

1. **OpenAI credit.** The account has been dry since 2026-08-15. W0.3 makes the check correctly red on merge, and `--exit-nonzero-on-fail` will email hourly until it is topped up. Do you want to top up first, or land W0.3 and accept the emails?
2. **Branch protection.** `gh api repos/waiff/sreality/branches/main/protection` returns **404 — branch not protected**. Every CI rail in W2 (and `migrations.yml` itself, whose own header says "advisory until added to branch protection") is non-blocking until this changes. Should this program turn it on, or is that a separate decision?
3. **Alert routing.** Which channels for `system_health_channels` — email, Telegram, both? Which inbox for `notification_email_to` — the same one GitHub already emails, or a separate ops address so ops mail and market mail are routed apart?
4. **W4's human step.** Setting `REALTIME_WORKER_ENABLED=false` on Railway is outside agent reach. Confirm the two-step procedure (you flip it, the dispatch runs, you flip it back), or provide a lever the workflow can drive.
5. **Migration 433.** `public.browse_stats` is still present in production with the exact 46-argument signature 433 targets. Is it still awaiting sign-off, or should it now be applied (or the file retired)? It is the only genuine `apply: skip` case, and it sits below the watermark either way.
6. **Migration 434.** Two ledger rows, no repo file — prod-only DDL. Capture it retroactively as a documentation file (the 025/026/052 precedent), or leave it for the constraint-diff successor?
7. **Alert volume tolerance.** What is an acceptable steady-state alerts/day before we tighten W3's onset thresholds? The baseline is ~100 raw failures/day and rising; "low single digits" is our target, not a measured promise.
8. **The successor.** Do you want the constraint-only catalog diff (W5, named but not built) scheduled now, or held until W2 has run for a month and we can see how much hand-written assert accumulates?