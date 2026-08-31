# Pipeline verification harness — full reference

Detail supporting the one-paragraph summary in the `scraper-ops` skill's
"Monitoring and health" section. Read the skill body first; this is the per-check
rationale — what each check watches, the incident that produced it, and how its
thresholds were sized. Moved out of the skill body in the W9 measure-plausibility PR:
the section had grown to 82 always-loaded lines and pushed `SKILL.md` past its
500-line context budget.

**Pipeline verification harness** (`scripts/verify_pipeline.py`, migration 274, PR #703) — a
scheduled job that writes one `pipeline_check_results` row per health metric (`ok`/`warn`/`fail`)
and is the origin of the notification system's third producer, `system_health` (see
`docs/architecture.md` rule #16) — a `fail` status rings the same in-app bell the SPA nav badge
polls. A `SECURITY DEFINER` dead-man-switch pg_cron function fires if the hourly job itself stops
running (the migration-136 exception-guarded pg_cron pattern). This exists because the pipeline
stalled silently for two days in 2026-07 (Anthropic credit exhaustion, 38k+ failed LLM calls) and
the only alarm was a failing GH Actions cron the operator happened to miss. The live checks are
`llm_errors`, `llm_liveness`, `llm_burn_rate`, `long_open_transaction` (6-hourly only, from
migration 437: warns when the oldest `pg_stat_activity.xact_start` passes an hour, because
`refresh_llm_cost_rollups`' 3-hour trailing re-scan only absorbs a late arrival whose
transaction was shorter than that — `called_at` defaults to now() = transaction START; the
repair is `select refresh_llm_cost_rollups('-infinity');`), `db_saturation`, `worker_liveness`,
`dual_write_parity`, `property_maintenance` (last-complete-sweep stamp age + oldest
dirty-queue row — the 2026-08-06 sweep-death/stranded-lease incident; both axes O(1) reads,
never a properties scan) and `broker_resolution_freshness` (**three** axes over
`app_settings.broker_resolution_last_complete`, `broker_resolution_runs.ended_at` and
`dirty_broker_listings` — the 2026-08-12
E2E review found the daily broker sweep truncating on its budget every day while exiting 0)
and `broker_merge_suppression` (migration 401): active `broker_merge_suppressions` rows whose two
identities share one broker — the invariant the suppression rail exists to hold. `fail` on the
first violation, no warn tier. An operator NO (unmerge / dismissing a `contact_bridge_review`
candidate) writes a suppression row keyed on the durable identity pair; the sweep loads the active
set once and it gates BOTH `decide_merges` (the pair reaches neither auto-merge nor review —
including through the oversized-component downgrade) and `_apply_merges` (a whole component that
would newly co-locate a suppressed pair is dropped and logged — the transitive chain the pure layer
cannot see; the set is re-read inside that write transaction so a NO landing mid-sweep still binds).
An explicit operator merge LIFTS the suppressions it *brings together* (never deletes, and never a
pair already co-located — that would erase evidence of a bypass); `POST
/broker-review/suppressions/{id}/lift` is the manual counterpart and `GET
/broker-review/suppressions` the ledger. Per-sweep counts land in
`broker_resolution_runs.suppressed_pairs` (pairs, not merges: the rail blocks before grading) and
the `RESOLVE full merge done … suppressed=N` line. Kill switch: `broker_auto_merge_enabled`=false.
Note the broker sweep axis measures a rotation **lap**, not one run: attribution routinely
spends its whole `--max-seconds` budget, so `resolve_brokers` carries cumulative coverage in
`app_settings.broker_sweep_cursor` (`last_id` / `lap_swept` / `lap_started_at`) and stamps
completion when the lap closes; with no lap closed yet the check ages the OPEN lap, so a
rotation that never gets round reds instead of parking on the missing-stamp warn.
The third axis exists because that lap stamp is written right after attribution, ~17-25 min
BEFORE the tail (the auto-merge step, the three rollups, the matview, candidates,
`_finalize`'s dirty-clear), so a sweep whose tail dies still leaves a minutes-old stamp while
the leaderboard and rollups silently stop: `broker_finished_{warn,fail}_hours` (30/60) age the
last full run that actually reached `ended_at`, tighter than the lap's deliberately wide 52/84
because the tail runs on every sweep. Fail sits between ONE missed night (48h + the ~2.5h
spread in when a run finishes = ~50.5h, deliberately only a warn — the skipped sweep's own red
run already emails) and TWO (~69.5h). A NULL (no finished full run on record) is skipped, not
red — only the missing LAP stamp is the deploy-day warn.
Four **per-m² plausibility** checks joined in W9 of the measure-unification program
(migration 427, view `measure_plausibility_by_source`, one read per run shared by all four;
they run on the 6-hourly `verify_pipeline.yml` lane only, NOT the hourly acute lane — a 12 s
scan of the active corpus for a slow-moving signal). They exist because
`data_quality_by_source` tests 29 fields for `IS NOT NULL` and nothing else, so it was
structurally blind to BOTH defects that program fixed: a plot area sitting in the floor-area
column and a per-m² unit price sitting in `price_czk` are 100% non-NULL. Grain is
(source, category_main, category_type) — a portal alone pools sale flats with monthly rentals,
a category alone pools a broken portal with eight healthy ones. Each has a stock arm and a
trailing-7-day arm and alarms on the worse: the stock arm indicts a defect that has been
standing for months, the fresh arm catches a regression the week it ships instead of waiting
for the corpus to churn. **Every share is a ratio over rows that HAVE the inputs**, so a cell
with nothing to measure scores no arm — and a skipped arm would otherwise be indistinguishable
from a clean one. Two rails close that: `ppm2_measure_coverage` watches the denominator, and a
check that scored NO arm at all reports `warn` with `value` null ("verified NOTHING"), never
`ok`. If you ever see all four amber at once, the measure's inputs stopped being written.
`area_vs_usable_divergence` — share of rows carrying BOTH areas that differ by >10% (the view's
material band). Only rows with both: bazos populates `usable_area` on 0 of 10 409 dum rows and
must read silent, not divergent, so the charter's literal `area_m2 IS DISTINCT FROM usable_area`
would have fired on every portal that publishes one field. `pozemek` is skipped by name — under
Option A `area_m2` IS the plot for land. Live pre-backfill: mmreality dum/prodej 99.7% (100% over
the trailing week) against 0.0% on every other portal; the milder realitymix byt convention sits
at 10.5%, which is why warn is 20% and not 5%.
`ppm2_basis_floor_share` — share of priced, area-bearing, basis-decidable rows that
`measure_price_per_m2` NULLs at its per-basis floor (rent < 1 000, sale non-land < 100 000).
An absolute LEVEL, not the "jump" the charter asked for: the unit-price masquerade never jumped,
it has been standing for the life of the portals carrying it. Live: ceskereality komercni/pronajem
20.0%, realitymix 19.0%, remax 11.3%, bazos 6.7% — against idnes and sreality at 0.5% in the same
cell, which is exactly the split between portals with and without a per-area price guard.
`ppm2_measure_coverage` — share of a cell's active rows the measure has NO INPUT for (no price,
or no positive area), skipping cells whose basis is undecidable (there an absent measure is the
specified answer). Rows the floor rejected are NOT counted — they have their inputs and
`ppm2_basis_floor_share` already indicts them. This is the axis that sees what the other three
structurally cannot: live, sreality publishes 27 174 active `pozemek` rows with `area_m2` NULL on
all of them (plot size is in `estate_area`), so those four cells produce no measure at all while
every other axis skips them and reads clean — and `data_quality_by_source` can't see it either,
grouping by (source, field) with no category grain (sreality `area_m2` reads 71.7% populated).
Severity splits by ARM: the stock arm can only **warn** (a standing, sanctioned gap nobody clears
today — warn 0.95, the five live dark cells are 0.995-1.000 and the next cell down is 0.894),
while the 7-day arm **fails** at 0.90 (that share among this week's arrivals is a parser
regression in flight; worst live scored 7d gap is 0.358). It is also the anti-silencing rail: a
portal that started writing NULL `area_m2` would make the divergence and floor axes go QUIET.
`ppm2_median_shift` — each cell's own median area and median Kč/m² against ITSELF a week ago,
read from this check's own `pipeline_check_results.details` row from 6-14 days back (no new table).
Deliberately NOT a cross-portal comparison: measured live, portal medians legitimately differ by up
to 19x on mix (idnes' rural land is 8.5x the peer Kč/m²), so a peer arm cannot separate a bug from
a catalogue. It cannot see a defect older than its baseline — that is what the two direct detectors
are for — but it fires at 3x on any new basis flip, and it will fire when the W2 backfill heals
mmreality (6.96x on area, 7.45x on the measure). Thresholds are sized on the noisiest real weekly
move measurable today, 1.90x. Each median is gated on **its own support in both weeks** — the rows
carrying that value, never `n_active`: 64 cells clear `n_active >= 200` but only 58/56 clear it on
area/Kč-m² support, and bezrealitky pozemek/prodej is 1 643 active rows with NINE areas whose spread
is 17.6x, so gating on cell size would red the tile and ring the bell on two ordinary delistings.
No baseline is `ok` for the first week after deploy and `warn` after,
so a check that has been erroring for a fortnight cannot read as green; a baseline that matched
nothing is `warn` ("compared NOTHING"), not a stable 1.00x; an empty view read (the
`is_platform_admin()` gate failing for the job's role) or an unreadable one (migration 427 not
applied yet) is `warn` naming the cause, never `ok` and never four red tiles.

The six dedup-specific checks (street/geo debt, eligibility funnel,
merge latency, engine health, merge-precision sample) went with the engine, along with their
`pipeline_check_thresholds` rows.

## The `scrape_runs` crash contract (`portal_runner.run_phase`, W0.2)

Both scraper health arms read one row, so what that row records on a bad ending is the whole
question. `run_phase` is the single seam (rule #21): it opens the row (`scrape_run_start`, with
`source` taken off the portal, not a per-module constant), runs the phase, and records **how it
ended**.

- **`ended_at` means the phase COMPLETED.** Only the return path calls `scrape_run_finalize`.
- A phase that raises bumps `scrape_runs.errors` by 1 via `bump_scrape_run_counts` — *additive*,
  on top of whatever the drain already committed per chunk, because a crash is one more error
  event and not a replacement aggregate — and deliberately leaves `ended_at` NULL before
  re-raising. A crashed run therefore lights up **both** arms of `scraper_health_checks()`:
  `stuck` (which keys on `ended_at is null`) and `err_pct` (which keys on `errors`).
- It catches `BaseException`, not `Exception`: a SIGINT or `SystemExit` out of a phase leaves the
  run just as unfinished as a `CheckViolation`, and "did not finish" must never read as green.
- Crash recording is best-effort — a failing bump can never mask the original exception.

Reading a finished row:

| `ended_at` | `errors` | meaning |
| --- | --- | --- |
| set | 0 | clean run |
| set | > 0 | finished, with N failures — the honest count |
| NULL | > 0 | **crashed** |
| NULL | 0 | SIGKILLed, or still running |

Before W0.2 this lifecycle was copy-pasted into nine `*_main.py` files and finalized from a bare
`finally`, so a crash still landed `_finalize(run_id, {}, drain=True)` — and finalize under
`bump_already_applied` never writes `errors` at all. A hard crash thus recorded `ended_at` set and
`errors = 0`, indistinguishable from a clean run and invisible to both arms; the index lane's
mirror-image bug (`_finalize` returned early on an empty agg) wrote nothing at all. That is why the
six portals that fell over on 2026-08-26 showed no error count anywhere
(`docs/design/reliability-program.md`). Never re-add a per-portal copy.

## The three self-rules (W0 of the reliability program, 2026-08-27)

`docs/design/reliability-program.md` W0.1/W0.3/W0.4. All three came out of a single finding:
**the health system was not silent during the outages it was built for — it was loudly wrong.**

**1. Silence is not recovery.** `llm_errors` derives `currently_failing` purely from state:
`last_ok_at < last_err_at`. It used to additionally `and` in a 90-minute staleness window
(`min_live_at`), on the theory that a lone old error with no traffic since is not a live
outage. That is backwards. The producers here have circuit breakers — the enrichment loop
aborts at exactly 5 consecutive errors — so once an outage is *total* the traffic stops, the
last error ages out of the window, and the check reads `ok`. Measured: OpenAI was
credit-exhausted for 11 days (63,547 error rows, **zero** successful calls) and the check read
`ok` for most of it, flipping `fail` at 14:02 and `ok` at 14:58 on unchanged inputs. Because
alerting is edge-triggered, that produced **114 alerts alternating onset with a literal
"✓ Recovered: llm_errors is healthy again"** for an outage that never recovered. Any
recency-window detector downstream of a circuit breaker is sampling a duty cycle, not a state.
Generalise it: a failure is superseded only by a newer success.

The symmetric pathology from the same edge-triggered rule: `property_maintenance` was `fail`
continuously from 2026-08-20 13:08 UTC with its last alert at 11:37 UTC — six days red, six
days silent. One rule produces both — and one fix retires both: the re-escalation ladder in
`toolkit/system_alerts.emit_transition_alerts`, where all checks inherit it (W3.4, shipped;
see "The escalation ladder" below), never in any one check.

**2. A zero is ambiguous — name the arm.** `llm_burn_rate` had only upper arms ($90 warn /
$150 fail), and `_record_failure` writes `cost_usd=0.0`, so a **total outage drives 24h spend
to the maximally healthy number**: it reported `ok value=0.0` throughout the 11-day outage. It
now carries `details.arm`:
- `starved` → `fail`: a `called_for` lane with `attempts > 0 AND successes == 0 AND spend == 0`.
- `idle` → `ok`: nothing attempted at all. Silence is `llm_liveness`'s axis, not this one.
- `runaway`/`ok`: the pre-existing spend arms.

**Evaluated per `called_for`, and that is load-bearing.** A 24h aggregate arm is defeated by a
single unrelated cheap success — verified: one `summarize_region_dispositions` call held the
aggregate at $0.01 for ~24 of 30 sampled hours while the only recurring lane was completely
dead. The upper arms are separately **parked as currently unreachable** (they were sized for
dedup-vision burn deleted 2026-08-06; 24h spend is ~$0.01), recorded rather than silently left
as arms that cannot fire.

**3. The acute lane must degrade, not vanish.** `run_checks` computed *all* results before
`write_results` persisted *any*, inside `llm_health.yml`'s `timeout-minutes: 5`. A timeout
therefore wrote **zero rows and fired zero alerts** — blinding `db_saturation` and
`worker_liveness` at precisely the moment DB saturation would make checks slow. Now each
result is inserted and alerted the instant its check returns (the transition baseline,
`latest_statuses`, is captured once before the first write, so per-check
`emit_transition_alerts` calls stay equivalent to the old batch call). Budgets:
`_CHECK_BUDGET_S` (45s) per check, capped by whatever remains of `_LANE_BUDGET_S` (**120s of
the job's 300s** — the rest is headroom for W2/W3's checks; this wave owns the number).
Enforcement is **server-side** via `SET LOCAL statement_timeout`: the connection is autocommit
and shared by every check, so a thread we cannot cancel or a signal raised mid-query would
leave it wedged for everyone downstream. Postgres cancelling its own query is the only
mechanism that returns the connection clean. An overrun is `warn` "timed out", an unreached
check is `warn` "not run" — "I could not measure this" is a different claim from "this is
broken", and fail-on-timeout would manufacture a wall of false reds exactly when the operator
needs to read the real one.

**`workflow_poller_liveness`** (W0.1) closes the matching blind spot on the other ops surface.
`record_workflow_failures.py` deliberately excludes its own runs from `workflow_failures`, so a
dead poller cannot appear in the table it feeds — it simply stops accumulating rows, which is
byte-identical to a quiet week. The check keys on the age of
`app_settings.workflow_failures_cursor`: warn > 6h, fail > 12h, sized to clear the worst
observed inter-poll gap (the cron is `*/30` but the Actions throttle really runs it 80–256 min
apart). A missing cursor row is `warn`, not `fail` — it is also the legitimate first-run state,
and a check that is red the day it ships teaches the operator to ignore every check. Registered
in the 6h lane only; promote it into `llm_health.yml`'s `--only` list after a soak.

Same wave, on the poller itself: when the page cap was hit, the cursor advanced to
`min(completions)`. Pages arrive newest-first, so hitting the cap means every run seen is
*newer* than `since` and the skipped runs are older than all of them — the next poll's
`completed_at < since` filter then dropped them **permanently**, which is why only 2 of the 6
portals that failed on 2026-08-26 were ever recorded. The in-code comment claiming it "crawls
to oldest-seen so the gap is picked up next poll" was false. The cursor now advances *only* on
a poll that reached back past `since`, and the page budget doubled to 10 pages / 1,000 runs
(≤10 Actions API requests per poll against a 1,000/hour per-repo budget).

**4. A threshold outlives the workload it was sized for** (W0.5, `llm_silence_fail_hours`
4h → 13h). `check_llm_liveness` documented its own premise: "the platform runs paid LLM
traffic continuously (dedup vision on the always-on worker) … p99 inter-call gap is ~1 min,
so the 4h default never trips in normal operation." That premise died on **2026-08-06**, when
the dedup decision engine was removed wholesale (rule 15) and took the continuous vision
traffic with it. The threshold stayed. The only recurring LLM producer left is bazos
description enrichment (`enrich_bazos.yml`) — condition scoring is paused and every other LLM
workflow is dispatch-only — so the healthy inter-call gap stopped being a minute and became a
cron period stretched by the Actions throttle (observed run-to-run gaps of 2.4–15.0 h over
Aug 27-30). The check has **no warn tier**, so every overshoot is a hard red: it fired
`fail value=4.195` and reddened "Monitoring: acute health (hourly)" **8 times between Aug 27
and Aug 30** while the pipeline was demonstrably healthy — 595 successful calls and 0 errors
in the trailing 24 h, $1.53 spent, last success minutes earlier. 13 h is 2× the 6 h nominal
cadence plus throttle slack, still inside one 6 h lane tick of a genuinely dead pipeline.

The transferable rule: **a threshold is a claim about a workload, so deleting the workload
invalidates the threshold.** When a producer is retired, grep the health harness for the
numbers that were sized on it — `DEFAULT_THRESHOLDS` carries its own rationale per key for
exactly this reason. `llm_silence_fail_hours` is **not** in the migration-274 seed and no
later migration adds it, so it resolves from the code default; if an operator ever adds it to
`app_settings.pipeline_check_thresholds`, that row wins over the code (`load_thresholds`
merges the DB over the defaults) and the deploy alone will not move it.

---

## `ops_incidents` — the failure-signature producer (W3, migration 462)

The verification harness rings the bell for the 19 checks it runs. It says nothing about the
~40 workflow failures/day that never touch a check: portal crashes, CI, backfills, LLM lanes.
That is the seam the 2026-08-26 outage fell into — the surface that could alert had nothing to
say about ingest, and `workflow_failures` (the surface that knew ingest was red) stores no
failure reason of any kind. W3 closes it WITHOUT a second bell: `ops_incidents` emits an
ordinary `system_health` `notification_dispatches` row and the shipped outbox delivers it.

**The key.** `scripts/failure_signature.py` (pure stdlib, no DB) normalizes an error into a
signature derived from the **error text only, never from `workflow_path`** — that asymmetry is
the whole mechanism, and it is why six portals failing on one `CheckViolation` are one row:

```
checkviolation|new row for relation listings violates check constraint listings_area_basis_check
check:property_maintenance|fail          # verify_pipeline's own CHECK line, one key per check
aborting|consecutive errors provider outage   # scripts that catch their own error and exit 1
step:run tests|exit:timed_out@.github/workflows/test.yml   # unreadable red — the ONE scoped key
```

Normalizer rails, each of which was a bug on the first pass: quoted identifiers survive the
digit-strip (the constraint name IS the key — a placeholder containing digits destroys it);
3-digit HTTP codes survive it too (else a 403 and a 500 collapse into `httperror|from`); only
line 1 of a psycopg message is read (line 2 is `DETAIL: Failing row contains (…)` — a whole
listing row); the exception class is matched by the dotted-module + CapWord grammar, never by
an `*Error` suffix allowlist (`CheckViolation`, `QueryCanceled`, `AdminShutdown`,
`AmbiguousFunction` and `InsufficientPrivilege` all fail that test).

**Two producers, one function.** `portal_runner.record_failure_signature(conn, exc, source=,
lane=)` is the single seam (rule 21):
- `portal_runner._record_run_crash` — the in-process chokepoint. The exception is in hand, so
  the signature is correct at t+0 with zero Actions-API cost. Writes on the SAME connection as
  the `scrape_runs.errors` bump, inside the existing best-effort try/except: a bookkeeping
  write must never mask the exception that got us here.
- `realtime_worker._record_lane_failure` — the probe and drain lanes call `portal_runner`
  directly (`run_id=None`, deliberately) and so bypass `run_phase` entirely. Before W3 a
  `CheckViolation` on the latency-critical drain produced a log line and nothing else, forever.
  **Always `await asyncio.to_thread(...)` it**, like every other DB touch in that file:
  `db.connect()` sleeps up to ~20s per source on a transient failure, and a bare call would
  stall the heartbeat lane during exactly the DB incident it exists to record.
- `record_workflow_failures.record_incidents` — the backstop, and the MAJORITY path (portal
  workflows are only ~22% of the failure corpus). For each NEWLY inserted failed run it reads
  the failed job's log and extracts the terminal error. Two budgets, both real:
  `MAX_LOG_FETCHES` per poll, and `INCIDENT_PASS_BUDGET_S` of wall clock — a fetch is up to 4
  round trips at `API_TIMEOUT_S`, so the fetch cap alone is a ~50-minute worst case inside a
  job with `timeout-minutes: 5`. The pass runs **after** `_write_cursor`, because a job kill is
  not an exception and the cursor advance is the input-coverage guarantee W3 rests on.

**One run is one failure (`ops_incident_runs`, migration 463).** Both producers see the same
Actions run — the chokepoint at t+0, the poller 80–256 minutes later — and the upsert bumps
`failure_count` unconditionally, so before 463 every portal crash counted **twice** and a LONE
crash crossed the measured onset threshold on its own. `claim_run(conn, run_id)` inserts the
run id and returns whether this caller won it; the poller claims **before** downloading, so a
run the chokepoint already recorded costs zero fetches too. That claim replaced an earlier
workflow-keyed signature-reuse map, which had no run correlation and folded an unrelated new
red (a timeout, say) into whatever incident last touched that workflow within the hour — the
real reason was then never derived and could never alert. `run_id=None` (the Railway worker)
always claims: it has no second observer. The ledger self-prunes at 30 days inside
`auto_resolve`.

**The log fetch has three hazards, all live-verified — do not simplify it.**
`/actions/jobs/{id}/logs` answers **302** to a SAS-signed Azure blob URL, and CPython's
`HTTPRedirectHandler` copies every header (`Authorization: Bearer` included) onto the redirect:
that leaks `GITHUB_TOKEN` to a third-party host *and* returns 401. So redirects are disabled and
the `Location` is re-requested **bare**. Azure **ignores suffix ranges** (`Range: bytes=-500`
came back 200 with the whole 27 KB body), so the tail is two requests: `bytes=0-0` to read the
length off `Content-Range`, then a real closed range. And the signed URL can 404 `BlobNotFound`
(retention, or logs not yet flushed) — that is a degraded incident, never a poller failure.
Never `tail` a log either: the last ~25 lines are always runner cleanup, so the extractor
anchors on the error and walks backwards.

**Alerting and closing.** Onset fires on `failure_count >= ops_incident_min_failures` (2) **or**
the signature spanning 2 distinct workflows, whichever first — the breadth arm matters because
the 2026-08-26 signature reached its 2nd workflow 8 minutes after onset while a same-workflow
2nd failure can be 164 minutes away under the Actions throttle. Exactly one dispatch per
incident (`sys:ops_incident:{id}:onset`), claimed with a conditional UPDATE before emitting so
two producers racing cannot both alert — **claim and emit share one explicit
`conn.transaction()`**, because every caller here is autocommit and a claim that committed
alone before a failed emit would set `alerted_at` with no dispatch, permanently silencing that
incident's only onset. Closing: every member workflow posting a newer
`workflow_run_health.last_success_at` (primary), `ops_incident_max_age_hours` (168 — the
backstop for retired/disabled/renamed workflows and for worker-origin incidents that have no
member workflow at all), or `toolkit.ops_incidents.resolve_incident` (manual; there is no admin
route yet). All three thresholds are scalars in `app_settings.pipeline_check_thresholds` —
`load_thresholds` merges only int/float out of that blob, so an array key would be silently
dropped and the code default would win forever.

**External delivery is still off.** `system_health_channels` is `[]`, so incidents ring the
in-app bell only. The flip is two `app_settings` rows plus a transport secret on the API
service — see the `toolkit-api` skill for `RESEND_API_KEY` / `TELEGRAM_BOT_TOKEN`;
`api/main.py` starts `outbox_loop` only when a transport `is_configured()`.

## The escalation ladder — one incident, not one edge (W3.4, 2026-08-31)

`docs/design/reliability-program.md` W3.4. Edge-triggered alerting produced two opposite
pathologies from **one rule**: `property_maintenance` was red from 2026-08-20 13:08 UTC with
its last alert at 11:37 — six days red, six days silent — while `llm_errors` oscillating on
unchanged inputs produced **114 alerts** alternating onset with a literal "✓ Recovered" for an
outage that never recovered. Both live in `toolkit/system_alerts.emit_transition_alerts`, so
both are fixed there, once, for every check — never in any one check.

**The unit of alerting is an INCIDENT, not an edge.** An incident opens at a check's first
`fail` and stays open while the check keeps failing *or* returns to `fail` within
`alert_flap_cooldown_hours` (6h). Every dedupe key for that incident is anchored on its onset
timestamp, so `ON CONFLICT (dedupe_key) DO NOTHING` gives exactly-once semantics across every
lane and cadence without any new table or any run-to-run state:

| key | when |
| --- | --- |
| `sys:{k}:onset:{incident_start}` | the first `fail` |
| `sys:{k}:reesc:{rung}:{incident_start}` | 6h / 24h / 72h, then `w1`, `w2`, … weekly forever |
| `sys:{k}:recovery:{incident_start}` | once, after the cooldown has elapsed since the last `fail` |

Three consequences worth keeping straight:

- **Only the HIGHEST due rung fires per run.** An incident that predates the ladder's deploy,
  or one whose lane was down for days, emits *one* alert (the 72h rung for a six-day red), not
  a backlog of every rung it slept through. `AlertPolicy.due_rung` owns that rule.
- **A flap re-enters the same incident.** The green half of a flap announces nothing (the
  cooldown has not elapsed), and the red half re-computes the *original* anchor, so the DB
  swallows the duplicate onset. 114 alerts become one onset and one recovery.
- **Recovery is deliberately late.** It fires on the first run that observes the check has
  held non-fail for the cooldown — `CheckState.last_run_at` is what makes "first run to
  observe it" exact, so the alert lands once and never repeats. Up to ~6h of latency on a
  *recovery* notice is the price of not shipping the flap noise; onset latency is unchanged.

**The streak comes from `pipeline_check_results`, not from migration 220.** `consecutive_failures`
and `is_chronic` (streak ≥ 3) are computed inside `public.workflow_failure_summary(int)` over
`workflow_failures` ⋈ `workflow_run_health` — the **GitHub workflow-run** domain. They are not
columns and they do not describe pipeline checks; the two domains stay separate. `check_states()`
reads the last 30 days of `pipeline_check_results` (index `..._key_run_idx (check_key, run_at desc)`)
and collapses each check to `(status, incident_started_at, last_fail_at, last_run_at, fail_runs)`.
`latest_statuses()` is now the status-only view of it.

**The anchor must never be the window edge.** For a check red LONGER than the 30-day read, every
row in the window is a `fail`, so the collapsed `incident_started_at` is just the oldest row still
inside it — a value that advances by one cadence on every run as rows age out. Every dedupe key is
built from that anchor, so `ON CONFLICT DO NOTHING` would stop suppressing anything and the ladder
would re-emit **onset on every run, forever** (hourly on the acute lane) — worse than the
pre-ladder silence it replaces. `_collapse` therefore flags `onset_truncated` when the streak runs
off the edge of the window, and `_resolve_truncated_onset` pins the real onset with one extra
query per affected check (first `fail` after the last non-`fail` preceding the window). The
cooldown is deliberately not re-applied out there — an incident that old is one long red by any
reading, and the goal is a STABLE key, not a second opinion. Residual, accepted: a green blip that
the cooldown absorbed can shift the anchor ONCE, on the run it ages out of the window. A failed or
slow resolver degrades to the window edge rather than raising.

**The policy is scalar keys, and that is load-bearing.** `load_thresholds` merges only
`isinstance(v, (int, float))` values from `app_settings.pipeline_check_thresholds`, so a JSON
*array* threshold would be dropped silently and the code default would win forever, undetectably.
Hence one key per rung: `alert_reescalate_{1,2,3}_hours`, `alert_reescalate_weekly_hours`,
`alert_flap_cooldown_hours`. A rung set to `0` is disabled. The cooldown must stay **above the
acute lane's hourly cadence** or hourly flapping rings again.

**The weekly heartbeat rides the existing `--weekly` lane** (`emit_weekly_heartbeat`, called from
`main()`), so no new schedule and no new workflow. It is the operator-facing half of a dead-man
switch: migration 274's `emit_verification_stale_alert` (pg_cron, hourly) catches a harness that
stopped writing rows from *inside* the database, but it cannot catch a harness that runs fine
while the whole delivery path is broken — a heartbeat that stops arriving can. **It is keyed
`sys:heartbeat:{ISO-week}` because `verify_pipeline.yml` appends `--weekly` on `date +%u = 1`,
i.e. to all four of Monday's 6-hourly runs** — a day-grain or run-grain key would emit 4×. The
two watchdogs share no key namespace: `sys:verification_stale:{YYYY-MM-DD}` is the pg_cron
function's alone, and a test pins that (an accidental collision would let `ON CONFLICT` swallow
the dead-man switch's insert and silence it).
