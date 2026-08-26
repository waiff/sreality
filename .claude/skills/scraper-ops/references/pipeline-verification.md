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
days silent. One rule produces both. The re-escalation ladder that fixes it belongs in
`toolkit/system_alerts.emit_transition_alerts`, where all checks inherit it (W3.4), not in any
one check.

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
