# Pipeline verification harness — full reference

Detail supporting the short summary in the `scraper-ops` skill's "Pipeline verification"
section. Read the skill body first; this is the per-check rationale — what each check
watches, the incident that produced it, and how its thresholds were sized — and the ladder
that turns results into incidents and system alerts. Moved out of the skill body in the W9
measure-plausibility PR (the section had grown to 82 always-loaded lines), and again in wave 1
of the Health dashboard sprint (`docs/design/health-dashboard.md`), which cut it to a summary.
Words: a status is `ok` / `warn` / `fail` / `unknown`, shown as OK / Watch / Problem / Not
measured; a check is an alarm or a gauge; a cell is one verdict inside a check (`GLOSSARY.md`
§ Health).

**Pipeline verification harness** (`scripts/verify_pipeline.py`, migration 274, PR #703; the
registry and the four statuses since migration 619) — every check is declared once in `CHECKS`
(class, host, cadence, budget, label, unit, owner page) and projected into `health_checks` (only by a run
from main: a `workflow_dispatch` from a branch, `GITHUB_REF_NAME` ≠ main, measures and stores but never
projects, so a branch's `CHECKS` cannot delete main's keys; a local run without the variable counts as main); a run
writes one `pipeline_check_results` row per check it ran, carrying `details.message`,
`details.elapsed_ms` and `details.cells` (every measured result carries its cells since Health dashboard
sprint wave 6; an unmeasured one carries an empty list). A run measures only the
checks that are DUE: newest row older than `cadence_s` less a slack of min(cadence / 10, 15 min)
(`toolkit/system_alerts.due_after_s`, the interval the recovery hold doubles), so a run that
comes a few minutes short of a full cadence after the last one does not skip a whole cadence. Only
rows written since the key's `declared_at` count, as for the page and the ladder, so a newly
registered or re-declared key is due in the run that projects it. Since wave 3 the platform alarms `jobs`, `capacity` and
`llm`, since wave 4 the portal checks `scraping`, `publishing`, `delisting` and `coverage`, since Health dashboard
sprint wave 5 the engine check `dedup`, and since wave 6 the `parsing` alarm and the quality gauges `data_quality`,
`location_town_coverage` and `text_extraction` are hosted on the worker and measured by its health lane (below); the
two keys left, `migration_drift` and `outbound_url_conformance`, are `github-actions`-hosted and measured by the middle
step of the one hourly canary, `verify_pipeline.yml` (it also takes a manual `dry_run`; its
concurrency group, `pipeline-verification`, keeps two runs from measuring a check twice); the
weekly `outbound_url_conformance` probe rides whichever run finds it due. No result reds a run
(`--exit-nonzero-on-fail` is gone since wave 2): the canary's
own assertions do, below. The URL probe keeps its own 120 s budget
(`PROBE_BUDGET_S` in `scripts/verify_outbound_urls.py`, its `Check.budget_s`): each HEAD's connect
and read timeouts are min(20 s, time left); DNS is not bounded, so one request can run past it, and
the job's headroom absorbs that. It probes its draw in an order shuffled by the ISO week, so a slow
sreality cannot starve the crawler portals; URLs it did not reach are `details.unprobed` (with
`details.budget_seconds`), never failures, and a probe that reached fewer than half of its draw is
`unknown` ("probed N of M …" in `details.skipped`). A timeout, an error or a check's own could-not-measure
path is `unknown` with its reason in `details` (`timed_out`, `errored`, `skipped`); a check the
run's budget never reached gets no row at all and stays due. No "verified nothing" path reads `warn`
any more: the last, `ppm2_median_shift`'s "compared NOTHING", went with that key in Health dashboard sprint
wave 6, whose median-shift arm reads Not measured until 6 days of its own history exist. The harness
writes no system alert: results become incidents through the ladder below, shown on Health and told
outside the app only by the canary's GitHub e-mail (no bell since 2026-10-21, ruling R2b). Since wave 2 the
pg_cron watcher `health_liveness()` (migration 620) judges whether the worker, the
ladder and every alarm are alive, and the canary checks the watcher and the ladder from GitHub
("Hosts, the watcher and the canary" below). The harness exists because the pipeline
stalled silently for two days in 2026-07 (Anthropic credit exhaustion, 38k+ failed LLM calls) and
the only alarm was a failing GH Actions cron the operator happened to miss. The broker suppression
rail (migration 401) is judged by the `dedup` check's `rulings/brokers` cell since Health dashboard
sprint wave 5 (below), with the retired `broker_merge_suppression` check's SQL moved verbatim:
active `broker_merge_suppressions` rows whose two identities share one broker — the invariant the
rail exists to hold — read Problem at the first, with no Watch tier. An operator NO (unmerge /
dismissing a `contact_bridge_review` candidate) writes a suppression row keyed on the durable identity pair; the sweep loads the active
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
Wave 3 retired eight platform keys into the three checks the worker measures (below): `llm_errors`
and `llm_burn_rate` into `llm`; `long_open_transaction` and `db_saturation` into `capacity` (the
latter's failure-rate arm into the `jobs` check's pg_cron cells); `worker_lane_stall`,
`workflow_poller_liveness` and `broker_resolution_freshness` into `jobs` (33 of the last's 34
onsets were edge flaps; `broker_maintenance` is a lane cell and `broker_region_type_stats` an
artifact cell); and `dual_write_parity` (Q6) went with the R2 dual-write it guarded. Their
thresholds and tests went with them.
Wave 4 retired seven portal keys into the four portal checks the worker measures (below):
`walk_coverage` (with `_SELF_CERTIFYING_TOTALS`, now the `Portal.total_is_count` seam) and
`walk_end_streak` into `scraping` and the `coverage` gauge; `acquisition_lag`,
`sreality_image_template` (its live CDN probe, `_fetch_sreality_probe` and the image imports) and
`property_maintenance`'s dirty arm into `publishing`; `false_delist_share` and `delist_flip_refused`
into `delisting`. `property_maintenance`'s sweep arm (the 2026-08-06 sweep death and stranded lease,
read from a stamp, never a properties scan) became the `jobs` check's `artifact/property_sweep`
cell: a completed cycle (one run, or two through `app_settings.property_sweep_cursor` since
2026-10-03) stamps the `property_sweep` derived artifact (migration 622), whose 38 h budget plus
GitHub's 18 h delivery slack keeps its Problem at 56 h, as ruled for the two-run cycle; its 52 h
Watch is gone, because an artifact cell has no Watch tier, and the sweep's own RED exit is the
faster alarm. Their thresholds, SQL and tests went with them.
Health dashboard sprint wave 5 retired `broker_merge_suppression` (hourly, on GitHub) into the
`dedup` check's `rulings/brokers` cell (every 10 min, on the worker); its threshold
(`broker_suppression_violations_fail`), its check function and its tests went with it.
Health dashboard sprint wave 6 retired nine keys into four checks on the worker (the last two sections
of this file): the regression detectors `field_fill_matrix`, `ppm2_measure_coverage`'s 7-day arm,
`ppm2_median_shift` and `location_payload_shape_drift`, with the measure of the old page's SQL
`snapshot_churn` rung, into the `parsing` alarm; the standing conditions `ppm2_basis_floor_share`,
`area_vs_usable_divergence` (with its estate-sum arm), `outbound_url_coverage` and
`ppm2_measure_coverage`'s stock arm into the `data_quality` gauge; and `text_extraction_lag` into the
`text_extraction` gauge, its wedge arm being the `jobs` check's `lane/text_extract` cell and its abort
arm the `llm` check's credit cell. `floor_convention` was deleted (Q4), and `location_town_coverage`
kept its key, moved to the worker and is counted over active ads. Their thresholds, SQL, helpers and
tests went with them; the four per-m² plausibility checks' rationale and calibration (W9 of the
measure-unification program, migration 427's `measure_plausibility_by_source`) moved into the new
sections.

The six dedup-specific checks (street/geo debt, eligibility funnel,
merge latency, engine health, merge-precision sample) went with the removed engine, along with
their `pipeline_check_thresholds` rows. The engine that merges today is judged by the `dedup`
check (Health dashboard sprint wave 5, below), which reads none of their stores (rule 15).

## Walks, queues, lanes and migrations — rationale moved out of the skill (2026-10-18)

Moved here from the skill body when wave 1 cut its section to a summary.

**The ingestion blind spot (2026-08-27).** Until then every scraper health signal compared our data
to our own data and rendered as a dot on a page, so sreality ingested ZERO new listings for nine
days without anything leaving the database. Two checks closed it, and wave 4 rebuilt both:
`acquisition_lag` read the oldest unclaimed never-fetched queue row per portal — deliberately the
QUEUE and not `listings.first_seen_at`, because a "no new rows in N hours" check needs a baseline
that the outage itself erodes (nine days of zeros makes zero the expected value) — and is now
`publishing`'s ingest arm; `walk_coverage`, collected against the portal's advertised total, was the
only comparison against EXTERNAL truth, and is now `scraping`'s missing arm and the `coverage`
gauge, which no longer certify a total the adapter derived from what it saw (remax and maxima
declare `total_is_count`). Inflow is judged against the same 24 h window 7-28 days earlier PLUS the
NEW queue rows, so a stopped drain reads as ingest's problem, never as lost discovery.

**Description reading** (field capture W7's `text_extraction_lag`; judged by the `text_extraction` gauge
since Health dashboard sprint wave 6, below) is the ingest arm's twin one layer later, computed from the
text lane's OWN statements, because the outage it replaced was a lane and its three monitors
disagreeing about who was eligible. The old key read the oldest eligible-unextracted advert, a stock it
judged as a flow (FAIL at about 3,400 h on every run while the lane was healthy); its wedge arm (rows
eligible, the lane claiming none or absent from the heartbeat) is the `jobs` check's `lane/text_extract`
cell, and its abort arm (the provider stopped the last pass; 2026-10-02..10 the lane claimed 500 a pass for
eight days and wrote nothing) the `llm` check's `<provider>/credit` cell.

**Lane cells** (the `jobs` check's `lane/<lane>` since wave 3; `worker_lane_stall` before it) close
the gap the watcher's `worker` cell (and `worker_liveness` before it) structurally cannot see — a
worker that is ALIVE with a wedged lane. The realtime worker
beat every 30 s for nine hours while its drain lane completed ONE pass and its images lane
completed 486; a pass was recorded only on
COMPLETION, so a hung lane and an idle lane published byte-identical state. The worker now
stamps when a pass BEGINS and the heartbeat resolves it to `in_flight_s`, and `_lane_loop`
bounds every pass with `LANE_PASS_TIMEOUT_SECONDS` (1800) — containment, not a diagnosis: it
stops one hang costing every later pass, and repeated timeouts on one lane are themselves the
diagnosis. Caveat worth knowing: a pass blocked inside `asyncio.to_thread` keeps running after
cancellation (Python cannot kill a thread), so the lane is freed but the thread is not — hence
the pass locks and the watchdog in the skill's worker section. `worker_lane_stall` judged
`in_flight_s` alone, so a lane failing every pass read OK (autodedup showed `failed_passes` 6 on
10-02 while it did); a lane cell also judges lateness, a failing spell and flapping (`jobs`, below).

**`migration_drift`** closes a different silent gap: it probes the live catalog for the objects
the newest 25 migrations declare, so a migration merged but never applied is caught in one tick
instead of the 29 h it took on 2026-08-25 (see the `database` skill). It cannot see a
merged-but-unapplied migration that only redefines existing objects (the migration-438 class,
issue #1855). Only the GitHub checkout has the files, so it stays on the canary for good, hourly
(cadence 3,600 s since wave 3, 21,600 s before); a run that finds none (the worker image has no
`migrations/` folder) is `unknown` with `details.skipped` "no migration files on this host", never
`ok`. Since wave 3 it lives in `scripts/checks/platform.py`, and its probe SQL
(`MIGRATION_OBJECT_PROBE_SQL`, `SAFE_IDENT`) in `scripts/migration_objects.py`, shared with
`scripts/apply_migration_receipt.py`. Since Health dashboard sprint wave 6 it emits a cell per migration file
in its window (the newest 25), named by its filename: OK applied, Problem when the file's objects are wholly
absent, Watch when some are, Not measured "unverifiable" when the file declares no object the probe can
check (grants, drops, data updates). OK cells are included, so a fixed file's incident recovers on a
measured OK instead of closing silently after 7 days.

**Thresholds.** Every other check's thresholds live in one dict, `THRESHOLDS`
(`scripts/verify_pipeline.py`), each entry with a one-line basis comment, passed to every check by
`main()` and by the worker's health lane. Since wave 3 nothing overrides them: `DEFAULT_THRESHOLDS`,
`load_thresholds` and its merge of `app_settings.pipeline_check_thresholds` are deleted (every
override equalled its default; migration 624 deletes the row), so a threshold changes in a PR, with
its basis. Since Health dashboard sprint wave 6 that includes the fill and vocabulary arms' numbers
(`parsing_fill_*`, `parsing_vocabulary_*`, `parsing_census_stale_days`, sized on the cohort's measured
drift and passed to
`scraper/field_census.py` as keyword arguments) and the registry limits (`location_registry_*`); only the
watcher's live as constants inside `health_liveness()` (below).

## Fill and vocabulary — fill AND validity per (portal, field) (field capture W1; judged by `parsing` since Health dashboard sprint wave 6)

Field capture W1 shipped this as the `field_fill_matrix` check; since Health dashboard sprint wave 6 it is
the `parsing` alarm's fill and vocabulary arms (`<portal>/fill/<field>`, `<portal>/vocabulary/<field>` and the
`census` cell; the `parsing` section, below), measured every 6 h on the worker. The rationale holds.

`data_quality_by_source` tests 26 fields for `IS NOT NULL`. That is blind to the two defect
shapes the field-capture program exists to fix: a value that is PRESENT and outside every
canonical option list (13 live `condition` spellings against a filter list of 6, ~14k active rows
unreachable by any Browse filter), and a cell that has read 0.0 % since the parser was written
because it reads a key the portal never emits (remax `balkon`/`lodzie`, ceskereality `vybavení`,
`počet podlaží`). `scraper/field_census.py` owns both SQL statements and both pure reductions, so
the live check and the re-bless can never measure different things.

**The matrix reads the whole active stock; only the census samples.** A census is evidence about a
payload's key space, so the newest 1,000 rows per portal answer it (~1 s). The matrix is compared
against a file blessed weeks earlier, and a SAMPLED cohort cannot be compared with itself: the
newest-1,000 slice is a time window whose category mix rotates with whatever a portal's walk
happened to cover. Measured on the shipped code before this was fixed — mmreality's window went
from a balanced mix to 73 % commercial rentals in two days, moving `cellar` 40.2 % → 8.2 % and
`disposition` 41.4 % → 6.3 % with no parser touched, twice over the fail tier. So the matrix is one
aggregate pass over every `is_active` row: measured 12.2 s for all nine portals (3.7 s in production on
2026-10-19), inside the `parsing` alarm's 180 s budget. It never expands a row into (field, value)
pairs — that form costs 32 s
over the stock; per-value counts come from `count(*) filter (where col = '<canonical value>')`
interpolated from `toolkit/filter_registry.COLUMN_CANONICAL_VALUES`, keyed by COLUMN — which is also
where the statutory `energy_rating` 'G' share per portal comes from. 'G' stays an ordinary grade: W5
measured all nine censuses and no portal marks it as the unassessed placeholder, so R11's `unassessed`
member is struck. A field with no canon is counted, not enumerated — W5 gives `price_unit` one.

**The arms are RELATIVE to a blessed baseline** (`data/field_capture/fill_baseline.json`), never
to an absolute floor, because a floor cannot see a legitimately-zero cell or a partial break
(ceskereality's `furnished` key mismatch would sit at ~2.5 %, not 0 %). Sizing is the MEASURED
drift of that cohort, never a binomial SE (the stock is the population, not a draw): comparing
each cell's fill with the same fill over the rows already active a week earlier, across 90 cells
on 2026-09-21, the worst moved 4.1 pp in a WEEK (bezrealitky `disposition` 55.0 → 50.9) and the
median under 1 pp — a 6-hourly run sees ~0.15 pp of that. A fill drop of more than 10 pp warns and of more
than 20 pp fails, both only where the baseline was ≥ 10 % filled; below that the collapse arm binds instead —
a cell with 50+ blessed filled rows keeping under a quarter of them fails. An off-canon share rising more than
5 pp warns, more than 15 pp fails (the lines are strict, at the matrix's four decimals). A cell that drifts
past a threshold honestly is a re-bless, and the message says so. The numbers belong to the cohort, not to a
taste: since Health dashboard sprint wave 6 they are `THRESHOLDS` keys (`parsing_fill_*`,
`parsing_vocabulary_*`, `parsing_census_stale_days`) carrying this basis, which `scraper/field_census.py`
receives as keyword arguments, and no setting overrides them.

**A blessed cell that stops being measured is an offender.** The spine comes from
`portals.is_enabled` and from a source having active rows, so a portal disabled during an incident
would otherwise take its 26 cells out of the denominator and leave the arm certifying the
silence — the old measure-coverage check's lesson (its denominator rail is the `data_quality` gauge's
`measure_gap` cells since wave 6), applied here as: a whole portal missing reads Problem on each of its
blessed cells, a single missing cell Watch, and each such cell's message counts the portal's blessed fields
absent from the live matrix.

**Known, not judged.** The baseline records today's zeros, so the arm is green on day one and
still names them every run, as facts, until W2's attribute contract declares a
producer for each. That list is computed from the LIVE matrix, so the run that repairs a cell is
the run that stops naming it. Boolean cells never written `false` are reported the same way —
whether silence means `false` or `unknown` is an absence
semantics the contract declares, so there is nothing yet to be right or wrong against.

**Who owns which field.** The fill arm owns the 26 `LISTING_COLUMNS` attributes. `data_quality_by_source`'s
seven probes with no attribute column (`geom`, `locality`, `street`, `property_grouped`, `source_url`, the
two condition levels) froze with its capture in Health dashboard sprint wave 6 (migration 633 unscheduled
`capture-data-quality`; wave 9 drops the view): a portal link is the `data_quality` gauge's `url/<portal>`
cell, a town the location gauge's.

**The census half** is checked in per portal under `data/field_capture/census/` and re-blessed
with `python -m scraper.field_census --bless` — a reviewed diff, like the location contract's
golden. A stale census is the known blind spot (a portal renames a key and every gate built on the
census still agrees with itself), so staleness > 30 d is the alarm's `census` cell, a Watch (wave 6
re-blessed the nine censuses of 09-21 before they turned stale on 10-22). It is
deliberately NOT a pytest: a test keyed on the calendar reds `main` on a date, on a branch that
touched nothing.

## `floor_convention` — deleted in Health dashboard sprint wave 6

Q4: 0 fails in 90 runs, the costliest check (11-35 s, a cross-portal self-join), measured against one
portal (idnes). The field-capture W8 floor heal it gated has run; its rationale is in git history (this
file at 5fa16a55) and `docs/design/field-capture/handover-autodedup-floor.md`. A storey-scale flip has
no arm now (the `parsing` section's Known limits). `scraper.attribute_contract.floor_convention`, the
parsers' floor contract, and `autodedup/floor_convention.py`, the engine's floor camps, are other
things and stay.

## The `scrape_runs` crash contract (`portal_runner.run_phase`, W0.2)

The portal checks see a walk only through its row (`scraping`, `coverage` and `delisting` judge index
runs with `ended_at` set and entries in `by_category`; `scraping` quotes an unfinished walk's start only in
`<portal>/walks`'s Problem message), so what that row records on a bad ending is the whole question. `run_phase` is the single seam (rule #21): it opens the row (`scrape_run_start`,
with `source` taken off the portal, not a per-module constant), runs the phase, and records **how it
ended**.

- **`ended_at` means the phase COMPLETED.** Only the return path calls `scrape_run_finalize`.
- A phase that raises bumps `scrape_runs.errors` by 1 via `bump_scrape_run_counts` — *additive*,
  on top of whatever the drain already committed per chunk, because a crash is one more error
  event and not a replacement aggregate — and deliberately leaves `ended_at` NULL before
  re-raising. A crashed walk is therefore never a finished walk, nor an end-reached one, even once
  the API's boot sweep stamps its `ended_at` (its `by_category` stays `[]`): its units'
  `<portal>/walks/<unit>` cells keep aging against G (§ `scraping` below), and `<portal>/walks`
  is a Problem once no walk has finished in 30 d. No check reads `errors`: the two arms a crash
  used to light, `stuck` (`ended_at is null`) and `err_pct` (`errors`) of `scraper_health_checks()`,
  have had no reader since Health wave 8 (wave 9 drops the function), and the delisting check's
  `<portal>/stuck` cell is another thing (active ads whose gone verdict waits past its limit).
- It catches `BaseException`, not `Exception`: a SIGINT or `SystemExit` out of a phase leaves the
  run just as unfinished as a `CheckViolation`, and "did not finish" must never read as green.
- Crash recording is best-effort — a failing bump can never mask the original exception.
- Since wave 3 the crash path writes no incident (the signature engine is retired): the run goes
  red, and the poller hands its reason line to the `jobs` check's `gh/<stem>` cell.

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

**1. Silence is not recovery.** `llm_errors` (retired in wave 3; the `llm` check keeps the rule)
derived `currently_failing` purely from state: `last_ok_at < last_err_at`. It used to additionally
`and` in a 90-minute staleness window
(`min_live_at`), on the theory that a lone old error with no traffic since is not a live
outage. That is backwards. The producers here have circuit breakers — `toolkit/vision_batch.py`
stops the labelling pass on the first fatal provider error, and the autodedup judge lane aborts
its pass the same way — so once an outage is *total* the traffic stops, the
last error ages out of the window, and the check reads `ok`. Measured: OpenAI was
credit-exhausted for 11 days (63,547 error rows, **zero** successful calls) and the check read
`ok` for most of it, flipping `fail` at 14:02 and `ok` at 14:58 on unchanged inputs. Because
alerting is edge-triggered, that produced **114 alerts alternating onset with a literal
"✓ Recovered: llm_errors is healthy again"** for an outage that never recovered. Any
recency-window detector downstream of a circuit breaker is sampling a duty cycle, not a state.
Generalise it: a failure is superseded only by a newer success — of the SAME account. Credit is
judged per provider: the text lane calls OpenAI up to every 5 minutes, so one comparison across
providers would let it clear an empty Anthropic account within minutes. Through OpenAI's
2026-10-02..10 credit outage the old alert said every paid LLM path was down and named no provider,
Anthropic-backed jobs included, and its rate arm read the newest call of ANY provider, so a stray
error after a top-up could hold it red for ~24 h; the `llm` check judges both per provider (below).
The worker side of the same incident: a text-lane pass the provider stopped
was booked as a success (`failed_passes: 0`) — it is now a failed pass, which the `jobs` check's
`lane/text_extract` cell reads beside the `llm` check's credit cell (`text_extraction_lag`, which failed on
that `fatal` whatever `claimed` said, where it had blamed backlog age, went in Health dashboard sprint wave 6).
The same rule, broken once more and closed in wave 1 of the Health dashboard sprint: a timed-out
check was `warn`, which `emit_transition_alerts` read as not failing, so 6 of
`text_extraction_lag`'s 7 timeouts in that outage closed its incident with a false "✓ Recovered"
(each came 6.4-9.6 h after the last `fail`, past the 6 h flap cooldown). A timeout is now
`unknown`, which the ladder never reads as a recovery.

The symmetric pathology from the same edge-triggered rule: `property_maintenance` (retired in
wave 4) was `fail`
continuously from 2026-08-20 13:08 UTC with its last alert at 11:37 UTC — six days red, six
days silent. One rule produces both — and one fix retires both: the reminder ladder in
`toolkit/system_alerts` (W3.4's `emit_transition_alerts`, replaced in wave 1 by `reconcile`; see
"The ladder" below), where all checks inherit it, never in any one check.

**2. A zero is ambiguous — name the arm.** `llm_burn_rate` (retired in wave 3 into the `llm`
check) had only upper arms ($90 warn / $150 fail), and `_record_failure` writes `cost_usd=0.0`, so
a **total outage drives 24h spend to the maximally healthy number**: it reported `ok value=0.0`
throughout the 11-day outage. It gained named arms, which the `llm` check keeps as cells:
- starvation → Problem: a (provider, `called_for`) with ≥ 5 attempts and 0 successes in the last
  hour (the old arm: a `called_for` with `attempts > 0 AND successes == 0 AND spend == 0` in 24 h).
- idle → OK: a provider with no calls in 72 h, fact `state=idle`. No check judges silence (see §4).
- spend: `fleet/spend`, the 24 h sum against Watch $130 and Problem $150.

**Evaluated per `called_for`, and that is load-bearing.** A 24h aggregate arm is defeated by a
single unrelated cheap success — verified: one cheap call of an unrelated tool held the
aggregate at $0.01 for ~24 of 30 sampled hours while the only recurring lane was completely
dead. The spend arms, parked as unreachable while 24 h spend was ~$0.01 (2026-08), are live again:
normal spend is $22-32 a day, and wave 1 moved the Watch default from $90 to the operator's $130.

**3. The acute lane must degrade, not vanish.** `run_checks` computed *all* results before
`write_results` persisted *any*, inside `llm_health.yml`'s `timeout-minutes: 5`. A timeout
therefore wrote **zero rows and fired zero alerts** — blinding `db_saturation` and
`worker_liveness` at precisely the moment DB saturation would make checks slow. Now each result
is inserted the instant its check returns, and since wave 1 the harness calls no alert function
at all: the API's ladder reads the stored rows. Budgets: `_CHECK_BUDGET_S` (45 s) per check
(`Check.budget_s`; 120 s for the outbound URL probe, 180 s for `parsing`), capped by whatever remains of
the RUN budget — one per-check budget per DUE check (`lane_budget_for`); the summed budgets are proven
to fit, with the 180 s headroom, the harness step's 6-minute limit (the poller step has 5, the job
19 with 8 minutes for setup and the assertions; 21 and 35 until Health dashboard sprint wave 6) by
`test_the_canary_job_fits_every_github_check_due_at_once`.
The full lane shared
a 120 s budget until 2026-09-22, and in 13 of the 15 runs before that 7-17 of its 25 checks were
never reached.
Enforcement is **server-side** via `SET LOCAL statement_timeout`: the connection is autocommit
and shared by every check, so a thread we cannot cancel or a signal raised mid-query would
leave it wedged for everyone downstream. Postgres cancelling its own query is the only
mechanism that returns the connection clean. **Since wave 2 the budget is a real wall clock per
check:** `_LaneBudget` (`scripts/checks/clock.py`, which the harness aliases) holds a per-check deadline, `monotonic() + min(budget_s, run time left)`,
and `_statement_timeout_clause()` gives every statement the time left to it, `max(1 s,
min(check deadline, run deadline) − now)`. Until then one timeout of the whole budget was armed
for each statement, so a check with N statements could take N × its budget (`dual_write_parity`:
p50 57 s on its 45 s). A check that has spent its budget between statements gets 1 s on the next
one, is cancelled, and is stored `unknown` with `details.timed_out`, `budget_seconds` (its
budget) and `elapsed_seconds` (what it spent). Every check that read through a raw cursor now
reads through `_fetchone` / `_fetchall` (`scripts/checks/clock.py`; SQL unchanged), so the clock binds it too; a check whose
statements must see one snapshot keeps one `conn.transaction()` and re-arms its `SET LOCAL`
before each statement. A check that raised is `unknown` with `details.errored` — "I could not
measure this" is a different claim from "this is broken", and fail-on-timeout would manufacture
a wall of false reds exactly when the operator needs to read the real one. A check the run never
reached gets **no row**: `run_checks` stops when the run budget is spent and logs the keys it
did not reach, which stay due for the next run (on the worker, its next 60 s tick). Since Health
dashboard sprint wave 6 a lane budget (the worker's 500 s pass) never starts a check whose own budget no
longer fits what is left (weighed before it starts: at most the time left plus 1 s, `_FIT_SLACK_S`, so a check
due alone, whose budget is the lane's whole budget, runs): the check waits, unwritten and still due, for the
next pass (the log line `DEFER <key>: its N s budget does not fit …`), because one started short would time
out and store an `unknown` row, which keeps it waiting a whole cadence (6 h for a gauge). Alarms run first and
every alarm due at once fits the pass (a rail), so only gauges wait; the canary's run budget is the sum of its
due checks' budgets, so it never meets the case. Due-ness reads the newest row of any status, so the wave-1
`unknown` "not run" row (`details.not_run`) made an unreached check wait a whole cadence for its next try.
Until wave 1 timeouts and not-runs were stored as `warn` (which the old ladder read as "not failing", rule 1)
and a raising check as `fail`.

**The poller cell** (`gh/poller` in `jobs` since wave 3; `workflow_poller_liveness`, W0.1, before
it) closes the matching blind spot on the other ops surface. A dead poller cannot appear in
`workflow_failures`, the table it feeds — it simply stops accumulating rows, which is
byte-identical to a quiet week. The cell keys on the age of `app_settings.workflow_failures_cursor`:
Problem past 18 h, the canary's measured 13.3 h maximum gap plus margin (the old check's 6 h / 12 h
were sized on the poller's own `*/30` cron, which the Actions throttle ran 80–256 min apart; its
46 fails 09-18..10-05 were real poller outages), and while it is stale every `gh/*` cell reads Not
measured, because their facts stopped being refreshed. Measured on the worker, it no longer rides
the run whose poll it judges. At 330–484 workflow runs a day (2026-10-15..17), even a 13 h canary
gap is ~270 runs, far inside the poller's 1,000-run page budget below.

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
traffic with it. The threshold stayed. The only recurring LLM producer left THEN was bazos
description enrichment (`enrich_bazos.yml`, itself deleted a month later — see below) —
condition scoring paused and every other LLM workflow dispatch-only — so the healthy
inter-call gap stopped being a minute and became a
cron period stretched by the Actions throttle (observed run-to-run gaps of 2.4–15.0 h over
Aug 27-30). The check has **no warn tier**, so every overshoot is a hard red: it fired
`fail value=4.195` and reddened "Monitoring: acute health (hourly)" **8 times between Aug 27
and Aug 30** while the pipeline was demonstrably healthy — 595 successful calls and 0 errors
in the trailing 24 h, $1.53 spent, last success minutes earlier. 13 h is 2× the 6 h nominal
cadence plus throttle slack, still inside one 6 h lane tick of a genuinely dead pipeline.

The transferable rule: **a threshold is a claim about a workload, so deleting the workload
invalidates the threshold.** When a producer is retired, grep the health harness for the
numbers that were sized on it — `THRESHOLDS` carries its own basis per key for exactly this
reason.

**And the second time, the check went with it.** Field-capture W0 (2026-09-21) deleted the
enrichment lane itself, leaving NO recurring LLM producer: everything left (autodedup judging,
labelling, estimations, URL parsing) is dispatch-driven or on demand. Measured over the 30 days
to 2026-09-21 the longest gap between `llm_calls` rows excluding that lane was **143.8 h**, and
`llm_liveness` had already logged 52 fails in 349 runs against a healthy pipeline. Re-sizing
13 h → 144 h would have bought a check that only speaks after six days of normal weekend quiet,
so `check_llm_liveness`, `_status_for_llm_silence` and `llm_silence_fail_hours` were all
deleted. The replacement is not a global recency threshold at all: field-capture R8 makes each
lane's health check read the lane's OWN eligibility predicate (oldest eligible-unextracted age
per source), which needs no historical baseline — the same shape as the retired
`check_acquisition_lag` (field-capture W7 shipped it as `text_extraction_lag`; the `text_extraction` gauge
since Health dashboard sprint wave 6). Beside it the
`llm` check catches "calls
are failing" per provider (from state, not recency) and "attempting and never succeeding".

---

## The ladder — one incident per failing cell, told by the API (wave 1, 2026-10-18)

Wave 1 of the Health dashboard sprint (`docs/design/health-dashboard.md` § 3 and § 6) replaced
W3.4's escalation ladder. That ladder re-derived every incident from 30 days of
`pipeline_check_results` on every lane run (`check_states`, `_collapse`,
`_resolve_truncated_onset`, `AlertPolicy`) and read anything but `fail` as healthy once a 6 h flap
cooldown had passed — shorter than the lanes' real gap, so in 30 days 34 of 88 "✓ Recovered"
alerts were false and 70 onsets followed an unmeasured row — and its anchors drifted as rows aged
out of the window. It, its `alert_*` threshold keys, the day-keyed alerts and the weekly heartbeat
are gone; the harness only measures.

**Where it runs.** `toolkit/system_alerts.reconcile(conn)` is called every 60 s by the API's
system-alerts loop (`api/system_alerts_loop.py`; `SYSTEM_ALERTS_INTERVAL_SECONDS = 60`, a
constant), started in `api/main.py`'s lifespan as the API's only background loop (the watchdog
matcher went to the worker's notifications lane in W2b) and off under `HEALTH_TELLER_DISABLED`: the
ladder stays off the worker, so a dead worker cannot silence its own alarms. A pass is one transaction under
`pg_try_advisory_xact_lock(hashtext('health_reconcile'))` — a second caller returns zeros and
writes nothing — and returns its counts (opened, reminded, recovered, closed silently), logged
when any is non-zero. Its values are module constants in `toolkit/system_alerts`, spelled once
(`ALARM_RUNGS_H`, `ALARM_WEEKLY_H`, `GAUGE_RUNG_H`, `DUE_SLACK_MAX_S`, `RECOVERY_HOLD_MIN_S`,
`UNMEASURED_CLOSE_DAYS`); no setting overrides them. The harness's due
selection imports `due_after_s` from it, so the due slack and the hold built on it cannot drift.

**What it reads.** The registry (`health_checks`: key, class, cadence, staleness budget, label,
unit), the newest `pipeline_check_results` row per registered key written since the key's
`declared_at` (the old harness stored a check's own error as `fail`, so a key is Not measured
until the new harness measures it; `pipeline_checks_public` applies the same guard, so the page and the
ladder agree), and the open cell incidents. A result's cells are the verdicts its `details.cells` list
holds. Until Health dashboard sprint wave 6 a result without them was one implicit cell named by the
check key (every wave-1 check, since explicit cells arrived with the waves that rebuilt each check); wave
6 deleted that fallback here and in the watcher's `unalerted` arm (migration 633), so a result with no
cells opens, keeps and recovers nothing. Every measured result carries its cells: `run_checks` stores a
verdict with no judged cell as Not measured and logs it as the check's defect
(`test_no_result_reaches_the_ladder_without_cells`), and the wave-6 modules' hermetic tests pin cells on every
status; the harness writes `cells: []` only for an unmeasured one. A result whose status is `unknown` is
ignored whole, and so is a result older than its key's staleness budget: it is Not measured for the ladder
too, as it is for the page's `effective_status`. A cell whose status is not `ok`, `warn` or `fail` is ignored.

**One incident per failing cell.** One `ops_incidents` row per cell, signature
`cell:<key>:<subject>`; the open-signature unique index allows one open row per cell. It is the one
incident model since wave 3: the reliability program's signature engine (`toolkit/ops_incidents.py`,
migration 462: one incident per error-text signature of a red run or a crash) is deleted with its
claim ledger and `failure_signature`'s signature tiers, and migration 621 closed its open rows
(`resolve_reason = 'retired: signature engine'`, every row whose signature does not start `cell:`).
A measured `fail` opens it (`first_seen_at` = `last_seen_at` = the result's `run_at`) or, when it
is open, advances `last_seen_at` and clears `ok_since`. A `warn` keeps an open incident open (clears
`ok_since`) and opens nothing. A
measured `ok` starts `ok_since` at the first OK, and the incident resolves
(`resolve_reason = 'measured ok'`) only on an OK measured across the hold, two cadences less the
due slack, never under 30 min (`max(2 × due_after_s(cadence), 30 min)`: a lane measures a check
up to the slack early, so two measured intervals can sum to under two cadences): the newest OK's
`run_at` must be at least the hold after the first OK; one lone OK never recovers. Two closes are
silent: an incident whose key left the registry (`retired`), and a cell unmeasured for max(7 d,
2 × cadence) — its `last_seen_at` older than that and its cell in no measured row inside it
written since the key's `declared_at` — which closes silently (`unmeasured 7d`; two weeks for the
weekly URL probe). Not measured never opens, recovers or ages an incident.

**What it records.** Since W1 of the notifications push program (2026-10-21, ruling R2b;
`docs/design/notifications-push/PROGRAM.md` § 3.8) the ladder writes no `notification_dispatches`
row and composes no message: the rhythm lives on the incident itself. Rungs count from
`first_seen_at`: an alarm reminds at 6 h, 24 h and 72 h, then weekly (R9a keeps this rhythm); a
gauge every 24 h, the operator's Q2 ruling: "ring when first read, then once a day". A rung is due
when `alerted_at` predates `first_seen_at + rung`, and only the highest due rung counts, never a
backlog. An onset sets `alert_count = 1, alerted_at = now()`; a reminder adds one to `alert_count`
and moves `alerted_at`; a measured recovery resolves the incident. Health shows the incidents and
their stamps, and the canary's `alarms_acknowledged` reads `alerted_at` (below). Until W1 each
onset, reminder and recovery was also a `system_health` row in the bell, keyed
`sys:{key}:{kind}:…`; migration 651 hides those 902 rows (the SYSTEM read arm and the admin update
policy dropped) and W6 deletes them after a backup.

**Told on Health and by the canary, nowhere else.** No system alert reaches a bell, a phone or an
inbox: wave 7's delivery readiness (the outbox and its two transports, below) was deleted
in W1, and the program's push never carries a system alert. Health's check rows carry `id=<key>` and
their cells `id=<key>:<subject>` (the subject percent-encoded; since wave 8), and because they render
after their fetch, the page scrolls to the decoded `location.hash` once its rows exist, opening the
row that hides a targeted cell, so `/health#<key>` lands on the check.

**Only acknowledgement silences.** The Acknowledge button on Health (on a check's row, shown while
one of its open incidents is unacknowledged) calls `POST /admin/health/acknowledge {"key": …}`,
which sets `ops_incidents.acknowledged_at` on that key's open cell incidents; only that stops their
reminders and the canary's e-mail, and the recovery is still recorded. Until W1 a bell read of the
notice (`notification_dispatches.seen_at`) also satisfied the canary, so "Mark all read" muted it
(audit F5).

**Idempotent by construction.** The loop re-reads the same newest rows every minute, so each step
is a no-op on repetition: `ON CONFLICT` on the open signature, `coalesce` on
`ok_since`, the `alerted_at` rung guard. `tests/toolkit/test_system_alerts.py` pins a second pass
over one state changing nothing.

**Replay rail.** `tests/toolkit/test_system_alerts_replay.py` feeds 14 days of real results
(run-length-encoded aggregates in `tests/fixtures/health_replay_14d.csv`, no ids or URLs: the repo
is public) through `reconcile`, and asserts no recovery while a key's newest row is `unknown`,
one incident per maximal fail streak, and one onset plus its rungs, nothing else, for a key that
fails the whole window. Its registry copy is host-aware since wave 3 (3 × cadence on the worker,
cadence + 18 h on GitHub), and its fixture has held the surviving keys only since wave 4, which
re-exported it with the header's SQL when the seven portal keys retired; Health dashboard sprint wave 6
deleted the 48 rows of its nine retired keys, and since the implicit cell went, the loader gives each
status-only row one cell named by its key (in the test helpers, never in production code). The worker's
twelve checks have input replays of their own (below).

**Cutover.** After the deploy the ladder sends one grouped onset per currently failing check,
alarm or gauge (today three gauges: `location_town_coverage`, `ppm2_basis_floor_share`,
`text_extraction_lag`), once, each on its first pass after the new harness has measured it. The
861 old system alerts could then be cleared with "Mark all read" (since W1 no system row is visible).

**Wave 3's cutover.** The canary's first projection from main after the merge deletes the eight
retired keys' registry rows, so the ladder closes their open cell incidents silently as `retired`
(the `llm_errors` incident open at merge included), and the new `llm` check, measured within
minutes of the worker's deploy, opens a fresh onset for an outage still under way (the two can be
open together for up to one canary gap). Legacy `sys:ops_incident:*` notices stayed in the bell
until W1 hid every system row.

**Wave 4's cutover.** The worker projects the four portal keys on its first health pass after the
deploy, but the seven retired GitHub keys leave `health_checks` only at the canary's next run from
main (median gap ~4.9 h), so both sets show on the page in between. That projection deletes their
registry rows, so the ladder closes their open cell incidents silently as `retired`
(`false_delist_share`'s, open at merge, included); the new checks open fresh onsets where they
measure Problem. Expected at ship: a `coverage` gauge Problem on realitymix's `unnominated` cell
(151 NULL-category ads no walk can nominate, the oldest last seen 112 d ago, #1853) and a Watch on
ceskereality's (96); from about 10-20, sreality's `precision` cell (its 10-18 flip cohort, #1858).
`delisting` read idnes's outflow as a 423 % spike (Watch) on 10-19 against the same hour one to four
weeks back; against the busiest 24 h within ±3 h of that hour it is about 210-240 %, under
the 300 % line. The missing and residual arms stay Not measured for each unit until its first walk
after the deploy writes `total_is_count`; from then ceskereality's `dum`/`prodej` residual reads
Problem (about −14 %: 13.7 k declared against 11.8 k held, the chaty-chalupy double count, #1854).
On a day the full property sweep holds the maintenance lease past about 40 min, `publishing`'s
visible cells read Problem for its duration (its Known limits, below).

**Wave 6's cutover.** The worker projects its four new keys on its first health pass after the deploy and
measures them in that pass (all four are due); the nine retired GitHub keys (`ppm2_median_shift`,
`ppm2_basis_floor_share`, `ppm2_measure_coverage`, `area_vs_usable_divergence`, `field_fill_matrix`,
`outbound_url_coverage`, `location_payload_shape_drift`, `text_extraction_lag` and `floor_convention`) leave
`health_checks` at the canary's next run from main, which closes their open cell incidents silently as
`retired` (`ppm2_basis_floor_share`'s and `text_extraction_lag`'s gauge incidents, open at merge,
included); both sets show on the page in between. `location_town_coverage` keeps its key but changes host,
so its `declared_at` restarts and it reads Not measured until the worker measures it; its open implicit
incident `cell:location_town_coverage:location_town_coverage` matches none of the new `<portal>/town`
subjects, so it is never reminded and closes silently (`unmeasured 7d`) within 7 days. A canary run checked
out at the old SHA that projects after the worker's first pass may move the row back to GitHub, and it
stays so until the worker's next projection (one per process). Expected at ship: `data_quality` opens a
Problem on realitymix's komerční/pronájem basis floor (11.4 % of 4,140 priced ads NULLed, lower bound 10.5
%), a real condition; `location_town_coverage` opens one on the 43 active ads with no town (ceskereality 39,
realitymix 3, mmreality 1); `parsing`'s median cells read Not measured for 6 days; idnes `has_balcony`
reads OK on the re-blessed baseline. Migration 633's apply stops `capture-data-quality`, so the old page's
`field_null_drift` rung reads "no baseline yet" about 20 h later, until wave 8 replaces the page.

## Hosts, the watcher and the canary — who watches whom (wave 2, 2026-10-18)

Wave 2 of the Health dashboard sprint (`docs/design/health-dashboard.md` § 3 and § 6) gives each
runtime the health system depends on an observer that does not run inside it. A check's host
(`Check.host`, mirrored into `health_checks.host`) is where it is measured, and each host projects
and deletes only its own registry rows:

- **`github-actions`**: 2 keys since Health dashboard sprint wave 6 (12 in wave 5, 13 in wave 4, 20 in
  wave 3, all 28 in wave 2), measured by the canary's middle step at their cadences, and they stay for
  good: `migration_drift` needs the repository checkout, and the weekly URL probe needs an address
  other than the scrapers'.
- **`pg_cron`**: `liveness`, the one watcher — a SQL function, not a `Check`, that upserts its own
  registry row.
- **`realtime-worker`**: `jobs`, `capacity` and `llm` since wave 3, `scraping`, `publishing`,
  `delisting` and the `coverage` gauge since wave 4, `dedup` since Health dashboard sprint wave 5, and
  `parsing` with the `data_quality`, `location_town_coverage` and `text_extraction` gauges since wave 6,
  measured by the worker's health lane (below).

**`run_due`.** `verify_pipeline.run_due(conn, thresholds, host, *, now, on_result=insert_result, lane_budget_s=None)`
reads which of the host's checks are due from the database (`due_checks`, so a restart re-runs
nothing), runs the alarms first and then the gauges, registry order inside each class, through
`run_checks` under the summed budgets of the due checks (`lane_budget_for`), capped by `lane_budget_s` when given (under that cap a check whose budget no longer fits waits for the next pass, since wave 6: self-rule 3), and hands each result
to `on_result` as it lands; `main()` projects the registry and calls it. The per-check wall clock,
and why an unreached check gets no row, are self-rule 3 above.

**The watcher: `public.health_liveness()`** (migration 620). A plpgsql `SECURITY DEFINER` function
with a pinned `search_path` and execute revoked from `public`, `anon` and `authenticated`,
scheduled as the pg_cron job `health-liveness` (`*/5 * * * *`) with a 60 s `statement_timeout`
set in the cron command (the migration-371 idiom: a function cannot raise its own budget). Each
call upserts its registry row (`liveness`: platform, alarm, host `pg_cron`, cadence 300 s,
staleness budget 15 min, label "Watchers", unit `min`, `declared_at` kept) and inserts exactly one
`pipeline_check_results` row. It writes no incident and no notice: the API's ladder turns its
failing cells into incidents like any check's, so they keep their reminders while the worker is
dead. Its cells judge what must not judge itself:

| cell | Problem when | basis |
| --- | --- | --- |
| `worker` | a worker's newest heartbeat is more than 10 min old (with no heartbeat row at all, `unknown`: "no worker on record") | beats come every 30 s, the worker's watchdog exits at 300 s, a deploy gap is under 2 min |
| `health_lane` | the health lane's own pass, read from its heartbeat entry (`details -> 'health'`, migration 621): no completed pass for 10 min, counted from the later of `last_pass_at` and the worker's `started_at` (the boot seed carries the last pass across a restart, so a restart is judged 10 min after boot, not in its first minute), a pass in flight for 10 min (`in_flight_s`: a pass skipped behind an abandoned one still records a pass) or failing for 10 min (`failing_since`, the message quoting `last_error`); `value` = minutes on that clock; emitted once the heartbeat carries a `health` entry, and folded into a failing `worker` cell. Never result rows (620 judged them): the lane writes one only for a due check, so a restart near a cycle's end read as a Problem | a pass is in flight at most 500 s, the lane's pass budget since Health dashboard sprint wave 6 (360 s in waves 4-5, 300 s in wave 3), 530 s with the heartbeat's 30 s lag; the lane sleeps its 60 s tick after each pass, so a full pass completes at most 560 s after the previous one, 590 s with the lag: both arms stay under the 10 min line, the 10 s left being the pass's own overrun (its connection and due read, each statement's 1 s floor, the daily prune); measured passes ran p50 1.9 s, max 23.1 s on 2026-10-19 |
| `stale:<key>` | an alarm other than `liveness` whose newest measured result (ok/warn/fail written since its `declared_at`) is past its staleness budget (`public.health_stale`); with no measured result it counts from `declared_at`, so a key whose every run is Not measured is still told; facts `key`, `host`, `last_measured_at`, `budget_s` | the budget is 3 × cadence on the worker and pg_cron, cadence + 18 h on GitHub, whose crons arrive up to ~13 h late |
| `unalerted` | a cell fails in an alarm's newest result (in budget, written since `declared_at`) and also failed in its newest measured result at least 2 cadences older, and has no open `cell:<key>:<subject>` incident; cells are read as the ladder reads them (the explicit `details.cells`; the implicit cell named by the key went in migration 633, Health dashboard sprint wave 6); one cell, `value` = the cells counted, its facts naming the keys and cells | a fail that has stood for two cadences has had every 60 s ladder pass in between to open its incident; like the ladder, it counts no stale or unmeasured result |

While a worker's cell fails, the keys hosted on it (a host is named like its worker,
`realtime-worker`) and its `health_lane` cell fold into it instead of each getting a cell, so a
dead worker is one Problem, not one per check. Every open liveness incident whose subject is
neither failing nor folded this run gets an `ok` cell ("no longer failing"), so a `stale:<key>`
measured again or an `unalerted` that got its incident recovers on a measured OK, told once,
instead of closing silently after 7 days unmeasured. The row's status is its worst cell (fail >
warn > unknown > ok), its `value` the newest worker's heartbeat age in minutes (null without one),
and `details` = `{cells, message, elapsed_ms}`, the message joining the messages of the cells at
the row's status, or "Watchers: all alive" when it is ok. Its limits are structural constants
inside the function, not tunables. The apply's post-condition calls it once and keeps that row,
so the canary and Health see liveness at once. It replaces two guards: `worker_liveness`, a
5-minute heartbeat limit that GitHub sampled about every 4.9 h (it caught an outage of length D
with a probability of about D / 4.9 h), deleted with `worker_stale_fail_minutes` — the ladder
closes any open `cell:worker_liveness:*` incident silently as `retired` once its registry row is
gone; and migration 274's dead-man job `emit-verification-stale-alert` (24 h on the newest row of
any key; it never fired), which 620 unschedules and 624 drops.

**The canary: `verify_pipeline.yml`**, "Monitoring: health canary (hourly)": cron `15 * * * *`
plus a manual dispatch whose `dry_run` input reaches the harness step only. GitHub delivers about
five of its runs a day (median gap ~4.9 h). One job — 19 minutes since Health dashboard sprint wave 6 (the poller step 5, the harness step 6 for
its budgets plus 180 s headroom, 8 for setup and the assertions; 2 budgets summing to 165 s since wave 6,
12 and 615 s in wave 5 under 35 and 21 minutes, 13 and 660 s in wave 4, 20 and 1,050 s in wave 3, 28 and
1,410 s before),
concurrency group `pipeline-verification` (never cancelled in progress), permissions
`contents: read` and `actions: read`, env `SUPABASE_DB_URL` and `GITHUB_TOKEN` — in three steps:

1. **Record failed workflow runs**: `python -m scripts.record_workflow_failures`, moved in wave 2
   from the deleted `monitor_workflow_failures.yml` with that job's 5-minute limit on the step, so
   a wedged poll cannot eat the checks' time; since wave 3 it records production runs with their
   reason lines and workflow facts ("The poller", below).
2. **Measure the GitHub-hosted checks**: `python -m scripts.verify_pipeline --host github-actions`
   (`--dry-run` when asked), `if: ${{ !cancelled() }}` under its own 6-minute limit, so a poller failure never skips the
   checks (a cancel does, and an overrun fails this step rather than the job).
3. **Assert the watchers and the teller are alive**: `python -m scripts.health_canary`,
   `if: always()`, the last step and the only one whose non-zero exit is meant to red the run. A
   poller or harness crash reds it too, as does the harness step overrunning its 6 minutes, and rightly: the
   canary did not complete.

`scripts/health_canary.py` connects with `scraper.db.connect()` and runs one bounded SQL per
assertion, each under `SET LOCAL statement_timeout = '10s'`; pure functions turn the rows into
reasons, so its tests need no database. Each failure prints one GitHub error annotation,
`::error::CANARY FAIL <name>: <reason>`, listed on the run's summary page; a clean run prints
`CANARY OK`. Exit 2 when the database does not answer (any read failing or cancelled), 1 when any
assertion fails, 0 otherwise:

| assertion | fails when | why |
| --- | --- | --- |
| `db_answers` | `select 1` cannot connect or answer within 10 s (exit 2, the error printed) | every other assertion reads the database |
| `liveness_fresh` | the newest `liveness` row is 30 min old (`LIVENESS_STALE_MIN`), or there is still none 30 min after the key was registered ("pg_cron has never written a liveness row"); skipped with a `CANARY SKIP` note while `health_checks` has no `liveness` key, so the canary can merge before migration 620 is applied | the watcher runs every 5 min: 30 min is six missed ticks |
| `failing_cells_have_incidents` | a liveness cell that has failed in every row for 15 min or more (`TELLER_GRACE_MIN`; the cells read by the ladder's own rule) has no open `cell:liveness:<subject>` incident | the ladder, the API's 60 s loop, is not running, so nothing the watcher finds reaches anyone; 15 min outlasts an API deploy |
| `alarms_acknowledged` | an alarm-class cell incident is open and unacknowledged and its latest rung (`ops_incidents.alerted_at`: the onset, or the 6 h, 24 h, 72 h or weekly reminder) is at least 15 min and at most 12 h old; the line names the key and the rung's age | system alerts reach no bell or phone (ruling R2b, 2026-10-21), so this red run's e-mail is how an alarm reaches the operator |

Since W1 (2026-10-21) the fourth assertion reads `ops_incidents` and the registry's class only: no
`channel_sends`, no `seen_at`, no dedupe key. Gauges are never asserted: a gauge's rungs show on Health once a day (Q2),
and the e-mail path is for alarms. So an unacknowledged alarm reddens every canary run from 15 min
to 12 h after its onset and after each reminder rung, and GitHub emails a red scheduled run to
whoever last changed its cron line: at the first canary run after the onset, and again after each
reminder (6 h, 24 h, 72 h, then weekly), until the incident is acknowledged on Health or the cell
recovers. This replaces
`--exit-nonzero-on-fail`, deleted with `llm_health.yml` and the 6-hourly lane: those lanes
reddened a run whenever it measured a failing alarm, read or not, and could never speak for a
check measured elsewhere — the watcher's, or the worker's.

**Who watches whom.**
- **pg_cron** (the watcher) watches the worker (its heartbeat), the ladder (`unalerted`) and every
  alarm's freshness (its `api` cell, the matcher's lease, went with the matcher in W2b, migration
  655), the canary's GitHub-hosted keys included: a
  canary that stops running opens a `stale:<key>` incident on Health once its hourly keys pass cadence + 18 h.
- **The API** (the ladder) turns what the watcher and the checks find into incidents and rungs, and
  keeps doing so for a dead worker because it does not run in the worker.
- **GitHub** (the canary) watches pg_cron (`liveness_fresh`), the ladder
  (`failing_cells_have_incidents`) and whether an alarm stands unacknowledged (`alarms_acknowledged`); its failure
  email needs neither Supabase's scheduler nor Railway.
- **The worker** (its health lane, since wave 3) runs the platform checks: `jobs` watches every
  production GitHub workflow (through the canary's poller, whose cursor is its `gh/poller` cell),
  every pg_cron job (`health-liveness` included), every worker lane and every derived artifact.
  Since wave 4 it also runs the portal checks (`scraping`, `publishing`, `delisting`, `coverage`),
  since Health dashboard sprint wave 5 the engine check (`dedup`), and since wave 6 the `parsing` alarm
  and the quality gauges. The watcher's `health_lane` cell watches the lane.

**Known gap.** Since migration 655 (notifications W2b) the watcher judges no API state: the
matcher's lease, its only API signal, went with the matcher. A dead API (no teller) shows only
through `unalerted`, two cadences after a cell fails with no incident, and through the canary,
whose third assertion emails a failing liveness cell that finds no incident.

## Delivery readiness — deleted (notifications push program W1, 2026-10-21)

Health dashboard sprint wave 7 (2026-10-19) kept system alerts in the bell (ruling Q1, 2026-10-18)
with two transports (a chat bot and Resend email), the outbox and a three-step switch-on ready. Ruling
R2b (2026-10-21, `docs/design/notifications-push/PROGRAM.md` § 3.8 and § 6) reversed Q1: system
alerts leave the bell, and W1 deleted the transports, the outbox, Settings › Delivery's recipient
fields and the switch-on steps (git history keeps them); nothing is left to switch on. The ladder's
incidents show on Health, and the one external path is the canary's red run, which GitHub e-mails
(`alarms_acknowledged`, above) and which needs neither Supabase's scheduler nor Railway. The
program's iPhone push (W4) carries watchdog, collection-monitor and share events only.

## The health lane — the worker measures its own checks (wave 3, 2026-10-19)

Wave 3 of the Health dashboard sprint (`docs/design/health-dashboard.md` § 6 and its "As built"
note) gives the worker its first checks. The lane `health` in `scraper/realtime_worker.py`,
registered in `LANES` after `text_extract`, wakes every `HEALTH_TICK_SECONDS` (60 s, a constant
like `text_extract`'s: no settings row and no kill switch, so a brake is a deploy) and runs one pass
at a time under `_HEALTH_PASS_LOCK`. Each pass, `_health_sync()` runs in a thread on its own
`db.connect()`:

- on the process's first pass, `verify_pipeline.project_registry(conn, "realtime-worker", now)`, so
  every deploy re-projects the worker's keys and deletes its undeclared ones, touching no other
  host's rows;
- every pass, `verify_pipeline.run_due(conn, THRESHOLDS, "realtime-worker", now=now,
  lane_budget_s=HEALTH_PASS_BUDGET_SECONDS)` (500 s since Health dashboard sprint wave 6: its seven 45 s alarms,
  `parsing`'s 180 s and, since notifications push W4, `notifications`' 5 s, 500 s in all, always fit — the
  next worker alarm needs the pass or a budget reworked — and a gauge whose budget no longer fits what is left waits
  for the next pass; 360 s in waves 4-5, 300 s in wave 3), which measures the due worker-hosted checks,
  each on its own clock (self-rule 3);
- once per 24 h in each process, the 35-day prune the operator ruled (Q5): `delete from
  pipeline_check_results where run_at < now() - interval '35 days'`, 10,000 rows at a time by
  `ctid`, until none is left.

Its heartbeat `last` is `{"measured": [keys], "pruned": n}`. The worker imports
`scripts.verify_pipeline` inside the pass (the image carries `scripts/`; `migrations/` is
dockerignored, which only `migration_drift` reads, and that check stays on GitHub);
`verify_pipeline.main()` takes `--host github-actions` only, and the worker never calls it. The
worker's keys carry the 45 s per-check budget (180 s for `parsing`) and a staleness budget of 3 × cadence
(30 min for `jobs`, `llm`, `scraping`, `publishing`, `delisting` and `dedup`, 90 min for `capacity`, 3 h for
the `coverage` and `text_extraction` gauges, 18 h for `parsing` and the `data_quality` and
`location_town_coverage` gauges), so the watcher's `stale:<key>` tells a stopped worker alarm within 5 min of that
(it covers alarms only: a stopped gauge just reads Not measured on the page), its `health_lane` cell (no completed
pass, a pass in flight or a failing spell for 10 min; a 500 s pass stays under the in-flight line, its row
above) tells a stopped lane, and the lane is a `lane/health` job cell like any other. The image carries
`data/` too, so the `parsing` alarm's fill arm reads its blessed baseline there.

## `jobs` — is every scheduled machine running and succeeding? (wave 3)

`check_jobs` (`scripts/checks/platform.py`; platform alarm on the worker, cadence 600 s, label
"Jobs", unit "cells") gives one job cell per scheduled machine on every runtime, with the subjects
`gh/<workflow file stem>`, `gh/poller`, `cron/<jobname>`, `lane/<lane>`, `lane/<lane>/<portal>` (its
`portal` field set; on the notifications lane the key is a step, `lane/notifications/<step>`) and
`artifact/<name>`, so the ladder opens one incident per failing job, each
shown on Health with its reason. It replaces
`worker_lane_stall`, `workflow_poller_liveness`, `broker_resolution_freshness` and
`db_saturation`'s failure-rate arm (above).

**GitHub workflows**, from the poller's tables (below). A cell exists for every
`workflow_run_health` row with `on_main = true` — a workflow whose file left `main` has none, and a
row the poller has not stamped yet (NULL) has none yet — and only a production run is a job's record
(event `schedule`, or `push` on `main`; a row written before wave 3 has `event` NULL and never
counts). A workflow whose `state` is not `active` (`disabled_manually`, …) reads OK with the fact
`state`. The canary has no cell: GitHub emails its red runs.

| reads | when | basis |
| --- | --- | --- |
| Problem | the newest production run failed and the workflow's median start gap is at least 24 h (`jobs_gh_long_cycle_h`); Not measured until 2 scheduled starts are recorded, one to two cycles for a weekly or monthly job, because `recent_starts` accumulates with no bootstrap | one red loses a whole cycle: `location_registry_load`'s single 09-01 timeout left ~91.6 % of katastr polygons missing for weeks |
| Problem | 3 consecutive production reds since `last_success_at` (`jobs_gh_streak_fail`) | in migration 462's corpus 35 of 63 red streaks were a lone failure; `recompute_mf_yields`' 3rd red came at ~15:00 on 09-13 |
| Problem | the time since `last_success_at` exceeds 3 × the p90 of the workflow's own start gaps (`jobs_gh_gap_p90_multiple`); it needs 10 `recent_starts` (`jobs_gh_gap_min_starts`), and until then this arm is Not measured inside the cell's facts, never a verdict; the time is counted to the poller's cursor, not to now: the cursor (the newest completion a whole poll has seen) is how far the workflow's runs are known | each workflow is judged by its own rhythm, not by one global clock |
| Watch | a lone red | main-branch CI goes red after ~10 % of merges |

`recent_starts` holds scheduled starts only ("The poller", below): a push has no cadence to be
overdue against. So for a workflow that only runs on push, the long-cycle and overdue arms are Not
measured, a lone red is Watch and 3 reds in a row are a Problem; its push runs still keep its
`last_success_at`, `state` and `on_main`, and its reds still count.

A failing cell's message carries the run's reason line and its link. The four stale streaks the
investigation found get no cell: `recompute_mf_yields`, `autodedup_realtime` and
`validate_vision_models` left `main`, and `dinov3_embed_backfill` runs on dispatch only.
**`gh/poller`** is Problem when the poller's cursor is older than 18 h (`jobs_poller_stale_h`; its
basis is "The poller cell", above), and while it is, every `gh/*` cell reads Not measured.

**pg_cron jobs.** A `cron/<jobname>` cell for every `cron.job` row, the watcher's own
`health-liveness` included (an inactive job reads OK with the fact `active=false`), judged over
finished runs only for the failure arms (`end_time IS NOT NULL`; the lateness arm counts every start):

| reads | when |
| --- | --- |
| Problem | 2 of the last 3 runs failed (`jobs_cron_fail_of_3`) |
| Problem | no run started for 3 × the job's median start gap over 7 days (`jobs_cron_gap_multiple`); it needs 5 starts (`jobs_cron_gap_min_starts`), and until then this arm is Not measured |
| Watch | any failure in 24 h |

When the `cron` tables cannot be read, or `cron.job` reads empty, the pg_cron arm is Not measured
with the reason, never OK: pg_cron's row security returns zero rows to the wrong role, so an empty
read is a failed measurement, not a quiet scheduler. No migration grants anything for it; the
worker's role already reads the cron tables, and the check reads them defensively.

**Worker lanes**, from the newest `worker_heartbeats` row: its `details` per lane, and the worker's
uptime from `started_at`. One `lane/<lane>` cell per lane carries the WORST of its conditions, each
named in `facts.conditions`; the `heartbeat` lane has no cell (the watcher judges the beat), and
`health` is judged like any lane. Since Health dashboard sprint wave 5 a last pass that aborted
reads Watch and, when a second pass aborts, Problem (the table); a clean pass recovers it. An
abort is a completed pass to the heartbeat (`failed_passes` never counts it: the engine's lane
livelocked for hours on 2026-10-19 while its cell read OK). The heartbeat replaces `last` when a
pass completes or is refused, so one abort is read again while the next pass runs long or raises:
a re-read keeps a Problem only the abort condition itself read (`facts.conditions` holds "fail: its
last pass aborted…"), never one the cell read for another reason. Only `last.aborted` is read, and
only as a non-empty reason string, which no lane but the engine's writes (`pair_budget`,
`deadline`; fact `last_aborted`); its shutdown aborts, `stopping` (the worker is shutting down) and
`lease_lost` (its lease was released under the pass), are no livelock and are not read (the check
imports both from `autodedup.incremental_lane`). A refusal is a failed pass already, judged by the
failing rule; the other lanes' `last.errors` count per-item failures inside completed passes;
`text_extract`'s boolean `aborted` marks a pass its budget bounded; and a failed re-cut
(`last.recut_failed`) has no arm.

| reads | when |
| --- | --- |
| OK, `off` | `interval_s` ≤ 0, or the newest pass's `last.skipped == "slice<=0"` (what a drain or images pass with a slice ≤ 0 records; any other `skipped`, autodedup's included, is not off) |
| Problem, never woke | `interval_s` is None and the worker has been up more than 15 min (`jobs_lane_boot_grace_s`) |
| Not measured | the lane's entry has no `interval_s` key at all, which only a worker from before wave 3 writes (the seeded None, the key present and null, is "never woke"); this guards the first deploy, while the old process's beat can still be the newest row |
| Watch, then Problem, missing | a lane in `LANES` has no heartbeat entry at all (notifications W2b): the beat is from an older process beating beside the new one (a deploy overlap); with the API's matcher gone, a worker without the `notifications` lane would leave the producers unrun and no cell would say so. Watch the first time (this worker's health lane can read its predecessor's row before its own first beat), Problem when the previous measurement read it missing too (fact `missing`); the `heartbeat` lane has no cell, missing or not. It reads another process's beat, never the checker's own: a worker wholly on old code runs the old check too, and shows only in Railway's per-service commit status |
| Problem, late | `now − last_pass_at` > max(3 × `interval_s`, `interval_s` + 2 × `last_duration_s`, 15 min) (`jobs_lane_floor_s`), once the worker has been up longer than that bound; a lane with no pass yet counts from the worker's start; a lane with no pass in this process counts from the worker's boot, or from this check's previous measurement when the lane read off then (switched on after boot) |
| Problem, failing | `failing_since` older than max(3 × `interval_s`, 15 min), or set at all when `interval_s` ≥ 1 h (`jobs_lane_daily_interval_s`; a daily lane passes about once per boot) |
| Problem, in flight | a pass running more than 1,800 s (`jobs_lane_inflight_fail_s`, `LANE_PASS_TIMEOUT_SECONDS`) |
| Problem, aborted again | the last pass aborted (`last.aborted`, a non-empty reason string other than the engine's shutdown aborts `stopping` and `lease_lost`), this check's previous measurement read an abort too, and the lane's `passes` has advanced since: a second pass aborted (the heartbeat replaces `last` when a pass completes or is refused, so one abort read twice while the next pass runs or raises stays Watch); or, with `passes` not advanced, the previous measurement's abort condition itself read Problem: the re-read keeps it ("its last pass aborted: pair_budget, after an earlier aborted pass"), while a Problem the cell read for another reason (late, failing) leaves one abort a Watch condition |
| Watch | a pass running more than 1,200 s (`jobs_lane_inflight_warn_s`); the newest pass failed (`failing_since` set, not yet old); flapping: since this check's previous result, at least half of the lane's attempts failed (`jobs_lane_flap_share`, 0.5), that is `failed_passes` grew at least as much as `passes`, which counts completed passes only, over at least 3 attempts (`jobs_lane_flap_min_passes`); the last pass aborted (`last.aborted`, a non-empty reason string: the engine's `pair_budget` or `deadline`) |
| no cell | the worker has been up under 15 min and the lane has not passed in this process yet |

A portal failing inside the probe or drain lane has its own cell, `lane/<lane>/<portal>`, from
`portal_errors`, and so does a step failing inside the notifications lane (notifications W2a:
`portal_errors` keyed by step, `watchdog_new` / `price` / `monitor`, so the cell is
`lane/notifications/<step>` with the step as its `portal`; red iff a statement raised, never for a
stored spec the matcher refuses, which `last` counts as `subscriptions_invalid`), and inside the push
lane (notifications push W4: `key` / `fan_out` / `claim` / `send`, each error the exception's CLASS
name only — `send` reads "N of M devices failed: <classes>" — because a push endpoint is a secret
capability URL and a psycopg or requests message can carry one; a transport retry is no error, the
`notifications` check judges outcomes; `last.deadline_stopped` = the pass reached its 90 s claim budget and
left the remaining devices to their lease, `last.stopped` = the shutdown did): Problem once its
`failing_since` is older than max(3 × the lane's interval, 15 min), Watch before. Basis: the lane bounds rest on 7 days of Railway logs read at build; the only
stored episode is autodedup's 10-02 stall, when the lane showed `failed_passes` 6 while
`worker_lane_stall` read OK.

**What the heartbeat carries** (`scraper/realtime_worker.py`, wave 3). `_amain` registers every
lane from the module constant `LANES` — `probe`, `drain`, `images`, `count_probe`, `maintenance`,
`broker_maintenance`, `estimation`, `location_resolve`, `location_intake_fast`, `location_refetch`,
`sold_comps`, `text_extract`, `health`, `autodedup`, `notifications`, `push`, `heartbeat` — seeds each with `interval_s`
None before any task starts, and asserts that the names it registers equal `LANES`; nothing imports
the worker to learn the list (the jobs check mirrors it as `scripts/checks/platform.WORKER_LANES`, a test pins the two). `_lane_loop` writes `interval_s` on every wake, the idle branch
included, so an off lane reads off, not absent. Per lane: `interval_s`, `passes`, `failed_passes`,
`last_pass_at`, `last_duration_s`, `started_at`, `in_flight_s`, `last`, `failing_since`,
`last_error` (at most 300 characters) and, on the probe, drain, notifications and push lanes,
`portal_errors` (`{portal or step: {failing_since, last_error}}`); `last_failure_at` is gone. A pass fails exactly where it
failed before — a raise (`last_error` = `<Type>: <message>`), the 1,800 s timeout (`pass exceeded
1800 s`), `text_extract`'s `fatal`, autodedup's refusal — and nothing new counts as a failure:
`_record_pass_failed` sets `failing_since` once and overwrites `last_error`; `_record_pass` clears
both and carries `interval_s`, `portal_errors` and every key it does not set itself. The heartbeat
lane records its own pass (`{"beat": true}`) like any lane, so no reader special-cases it. A probe
or drain pass reports the portals it attempted and those that raised (a probe portal whose every
category failed counts as raised, `every category failed`): a success deletes that portal's entry,
a raise sets or keeps it, and a portal not attempted keeps it unless the lane's disabled-sources
setting now holds it, which deletes it. `_record_lane_failure`, a blocking incident write per
failing portal, is gone. **At boot**, before the first beat, `_seed_from_previous_beat(state)` reads
this worker's previous `worker_heartbeats.details` in one bounded attempt (`connect_timeout` 10, no
retry; a miss seeds nothing and logs once) and copies, per lane in `LANES`, every key except
`started_at`, `in_flight_s`, `interval_s` and `last_failure_at`, so about 11 deploys a day reset no
counter, failing spell or portal error, and a lane that never wakes in the new process reads
"never woke", not late against its old interval.

**Derived artifacts.** An `artifact/<name>` cell for every `derived_artifacts` row: Problem past its
`staleness_budget` (`public.health_stale(last_succeeded_at, staleness_budget)`), except that a row
whose `host` is `github-actions` is stale only past the budget plus `GITHUB_DELIVERY_SLACK_S`
(18 h), because GitHub delivers a cron late by hours (54 of the canary's last 65 scheduled gaps
exceeded 6 h: median 7.2 h, longest 13.5 h, 09-28..10-18); Not measured ("never succeeded") while `last_succeeded_at` is NULL. Facts: `producer`, `host`,
`cadence`, `last_succeeded_at`, `last_rows`, `last_duration_ms`, `budget_s`, `slack_s` and
`is_serving` (a fact, never a verdict). Each budget is at least 3 cadences (`browse_list` 45 min on
`*/15`) except `property_sweep` (wave 4, migration 622, the daily full sweep's completed cycle):
38 h on a daily cron, so with the slack it reads Problem at 56 h, the retired `property_maintenance`
sweep arm's fail line, and its 52 h Watch has no equivalent; 622 seeded it from
`app_settings.property_sweep_last_complete`, so its verdict is continuous across the deploy. Every
live row was inside its budget at build.

## The poller — production runs, reason lines and workflow facts (wave 3)

`scripts/record_workflow_failures.py`, the canary's first step, writes the facts the `jobs` check
judges for GitHub; it judges nothing itself.

**Production runs only.** A job's record is its production runs: event `schedule`, or `push` on
`main`. Pull-request, dispatch and branch runs are attended and stay in Settings › GitHub Actions,
so a workflow that dispatches itself is recorded by its cron run only: its chained hops are
dispatch runs. The canary (`MONITOR_WORKFLOW_NAME`, matched by its display name) is skipped for
failures and successes alike, so its own runs never reach the tables; GitHub's email is their path.
A poll whose `GITHUB_REF_NAME` is set and is not `main` (a dispatch from a branch) writes nothing
and says so in one log line, the registry projection's rule. The cursor
(`app_settings.workflow_failures_cursor`), the cancelled gate (a never-started supersession cancel
is not a failure) and `_advance_cursor` are unchanged.

**What it writes.** For every production run it sees, the workflow's `workflow_run_health` row is
upserted, born on a failure too: `last_success_at` by `greatest()` from its successes; `state` from
one `GET /repos/{repo}/actions/workflows` per poll (`active`, `disabled_manually`, …); `on_main`,
whether the workflow file is in the checkout; and `recent_starts`, the stored array merged with the
SCHEDULED starts this poll saw (event `schedule`, every conclusion; a push has no cadence to be
overdue against), newest 20 kept. It accumulates with no history bootstrap, so the start-gap arm is
Not measured until a workflow has 10 scheduled starts, about two weeks for a daily job, and the
long-cycle arm until it has 2. Each new failure goes into `workflow_failures` with `event`,
`head_branch` and `reason`, the reason line of its failed job's log tail (`LOG_TAIL_BYTES`),
fetched for newly inserted rows within `MAX_LOG_FETCHES` (25 logs) and `REASON_PASS_BUDGET_S`
(90 s) per poll; a row past either budget keeps `reason` NULL and is not retried, so its cell shows
the run link without a reason. Migration 621 added those three columns and `state`, `on_main` and
`recent_starts`.

**The reason line.** `failure_signature.reason_line(text)`, at most 300 characters (None when the
log has none), is the one line that explains a red run: (1) the LAST `CHECK <key> status=fail`
line; else (2) the last exception line — `<CapWord>Error: …`, `<CapWord>Exception: …` or a
traceback's final line — outside `##[group]` … `##[endgroup]` blocks and not starting with runner
boilerplate (Image, Version, Commit, Region, Cloud, Contents, Metadata, Packages, Actions, `Note:
switching to`); else (3) the last `##[error]` line. The Actions timestamp prefix is stripped and the
rest of the line kept. The signature engine's exception-first tier took the last CapWord-colon line
anywhere, so runner setup lines such as `Metadata: read` beat every CHECK line: it never produced a
single CHECK signature. The fixture `tests/fixtures/workflow_job_116299577551.txt` (a 2026-10-15
monitoring run's job log, scrubbed of tokens) pins that it yields its `CHECK llm_errors
status=fail` line, not `Metadata: read`. Since wave 2 only the canary prints CHECK lines, and the
poller skips it, so for today's workflows the reason is usually tier 2's exception.

**The log fetch has three hazards, all live-verified — do not simplify it.**
`/actions/jobs/{id}/logs` answers **302** to a SAS-signed Azure blob URL, and CPython's
`HTTPRedirectHandler` copies every header (`Authorization: Bearer` included) onto the redirect:
that leaks `GITHUB_TOKEN` to a third-party host *and* returns 401. So redirects are disabled and
the `Location` is re-requested **bare**. Azure **ignores suffix ranges** (`Range: bytes=-500`
came back 200 with the whole 27 KB body), so the tail is two requests: `bytes=0-0` to read the
length off `Content-Range`, then a real closed range. And the signed URL can 404 `BlobNotFound`
(retention, or logs not yet flushed) — that row keeps `reason` NULL, never a poller failure.
Never `tail` a log either: the last ~25 lines are always runner cleanup, so the extractor
anchors on the error and walks backwards.

## `capacity` — is the database, and the worker's memory, keeping up? (wave 3)

`check_capacity` (`scripts/checks/platform.py`; platform alarm on the worker, cadence 1,800 s,
label "Capacity", unit "cells"):

| cell | measures | Watch | Problem |
| --- | --- | --- | --- |
| `cron/<jobname>` | the 24 h p50 and p95 duration of its finished runs, against its reference, its interval (the median start gap) and its `statement_timeout` (parsed from the `set statement_timeout='NNNs'` prefix of `cron.job.command`; with none, that arm is not judged) | p50 ≥ 3 × reference and ≥ 30 s, or p95 ≥ 50 % of the interval | p50 ≥ 4 × reference and ≥ 60 s, or p95 ≥ 80 % of the interval or of the statement_timeout |
| `check_timeouts/<host>` | one cell per host that measures through the harness (`github-actions`, `realtime-worker`): the share of the last 24 h's `pipeline_check_results` rows of that host's keys with `details ? 'timed_out'`, so one host's timeouts are not averaged into the other's; the watcher's `pg_cron`-hosted key is left out, and a host with no result in 24 h is Not measured | > 5 % | > 20 % |
| `open_transaction` | the oldest `xact_start` in `pg_stat_activity`, autovacuum and the check's own backend excluded; Not measured while the view hides a session (below) | > 60 min | > 6 h |
| `connections` | client connections against `max_connections`; Not measured while the view hides a session (below) | > 70 % | > 90 % |
| `worker_memory` | the autodedup lane's `last.peak_rss_mb` over its `memory_limit_mb`, from a pass of this process only: Not measured ("no autodedup pass since the worker booted"; facts `last_pass_at` and `boot`) while the lane's `last_pass_at` is NULL or older than the heartbeat's `started_at`, because the boot seed carries `last` across a restart (the watcher's `health_lane` cell and the `jobs` check's lane cells read it), so until then it is the dead process's peak | > 0.8 | > 0.9 |
| `db_restart` | `pg_postmaster_start_time()`, a fact | — | — (always OK) |

**A hidden session is not a quiet one.** `pg_stat_activity` shows a session the reading role may not
inspect with its `backend_type` NULL, so `connections` and `open_transaction` also count those
rows, and while there is any, both cells are Not measured ("pg_stat_activity hides other sessions
from this role"): a count or an oldest transaction over the visible sessions alone reads low and
passes.

**The reference cannot be eroded by the incident.** A job's reference is its lowest weekly p50 of
finished runs since `THRESHOLDS["CAPACITY_REFERENCE_FROM"]`, a blessed date: 2026-10-17, the first
full day on the 2XL instance (p50 41-100 s). Before 7 days of post-reference history
(`capacity_reference_min_days`) the relative arm is Not measured; re-blessing after a deliberate
change is a dated PR that moves the date, and a rail in `tests/scripts/test_verify_pipeline.py`
fails once the date is more than 90 days old, so that PR is forced, not remembered. Why not a trailing baseline: `browse-list-rebuild`'s
daily p50 ran 151-277 s from 08-17 against a lowest weekly p50 of ~9.9 s (ratio 15-28) and hit
1,800 s timeouts 09-26..10-03, while a trailing 28-day baseline rose from 25.7 s (08-18) to 49.5 s
(08-31), 176.6 s (09-15) and 242 s (10-09), so it would have read "recovered" by early September.
A known, stated limit: a cron tick that skips itself is indistinguishable from a run and biases p50
down, the reference with it, which only makes the relative arm stricter.

**The other cells' basis.** Check timeouts ran up to ~12 % a day during the saturation and 0 since
10-10, measured on the GitHub population (the only host before wave 3); the worker's cell uses the
same limits. The oldest open transaction peaked at 18.3 min in the saturation month; it matters
beyond load because `refresh_llm_cost_rollups`' 3-hour trailing re-scan absorbs only a late arrival
whose transaction was shorter than that (`called_at` defaults to now(), the transaction START; the
repair is `select refresh_llm_cost_rollups('-infinity');`). Connections read 29 of 380 and the
worker's memory 7.4 of 22.9 GB at build. It replaces `db_saturation` (a failure rate only, OK on
all 61 runs of the saturation week) and `long_open_transaction` (never non-OK in 196 runs, with no
fail tier).

## `llm` — are the providers serving, and is spend sane? (wave 3)

`check_llm` (`scripts/checks/platform.py`; platform alarm on the worker, cadence 600 s, label "LLM
providers", unit "cells"), over `llm_calls` (24-72 h, ~2 s):

| cell | Problem | Watch |
| --- | --- | --- |
| `<provider>/credit` | the newest error matching the credit patterns `llm_errors` used, quoted verbatim, is newer than that provider's own newest success, over a 72 h lookback (`llm_lookback_h`); an authentication failure does not count | — |
| `<provider>/<called_for>` | at least 5 attempts and 0 successes in the last hour (`llm_starved_min_attempts`) | a 24 h error share of at least 20 % over at least 20 calls (`llm_error_share_warn`, `llm_error_share_min_calls`) while the provider's newest call is an error |
| `fleet/spend` | 24 h `cost_usd` above $150 (`llm_spend_24h_fail_usd`) | above $130 (`llm_spend_24h_warn_usd`) |

A provider with no calls in 72 h has one cell, OK, with the fact `state=idle`. Basis: 5 of 5 onsets
of the old checks since 08-27 were real; normal spend is $22-32 a day. The 72 h lookback keeps a
credit outage red after its callers back off (self-rule 1: a circuit breaker stops the traffic, and
a recency window then reads the silence as recovery); judging per provider means one provider's
success no longer masks another's outage; starvation per (provider, `called_for`) means one cheap
success elsewhere cannot hold an aggregate healthy (self-rule 2). It replaces `llm_errors` (an
any-provider rate gate over a 24 h window) and `llm_burn_rate` with its starved and idle arms, and
spells the spend once (the code default was $90 against the settings row's $130 until wave 1).
`text_extraction_lag`'s abort arm, a third detector of the same outage, went with that key in Health
dashboard sprint wave 6. The alarms still tell a provider's credit outage twice, by its
`<provider>/credit` cell and by the `jobs` check's `lane/text_extract` cell, because the text lane's
provider-fatal pass is a failed pass, and the `text_extraction` gauge's `stock` cell reads it as 0
readings while rows wait (stated, accepted).

**Replay rails.** `tests/scripts/test_checks_platform_replay.py` replays aggregate INPUTS exported
read-only from production — `tests/fixtures/health_jobs_14d.csv`, `health_capacity_cron_8w.csv`
(weekly p50, p95 and start gap per pg_cron job from 2026-08-10, a week before the 08-17
saturation) and `health_llm_14d.csv` (hourly calls, errors, credit errors and cost per provider
and `called_for`), each with its refresh SQL in the test's header — through the checks and the
ladder. The incident-shaped windows fire: the 09-13 `recompute_mf_yields` reds, the 10-02 and
10-18 OpenAI outages, and the 08-17 → 10-10 `browse-list-rebuild` saturation, replayed on a
reference date overridden to 08-10 (the blessed 10-17 postdates it): its incident opens inside that
reference week through the p95 arm, before the relative arm can judge, and the sticky reference
holds it red to the end. The quiet windows are what the data allows: a full fortnight for llm
(09-14..09-27); 8 days for pg_cron (10-11..10-18, after the saturation's tail); one week for
capacity (from 10-12), with the relative arm Not measured on the blessed reference; none for GitHub
workflows, because only each workflow's newest success was stored before wave 3. The worker lanes
and the derived artifacts keep no history, so only their hermetic tests judge them.
`tests/scripts/test_checks_platform.py` pins every rule above on fake connections, which answer a
statement by a fragment of its text, so the pg_cron SQL also runs for real: in CI's replay database
(no pg_cron) a rolled-back transaction creates a stub `cron` schema (`job`, `job_run_details`),
seeds runs and executes `_CRON_JOBS_SQL`, `_CAPACITY_CRON_SQL` and `_CAPACITY_REFERENCE_SQL`
through psycopg with their real parameters, asserting that only finished runs count, that the
7-day window counts back from the job's newest start, "2 of the last 3" and whole-week reference
buckets; the artifact read runs there too, with the GitHub delivery slack the check sends. That
rail, `tests/test_platform_cron_sql_live.py`, is one `migrations.yml` step after 621's
(`TEST_DATABASE_URL`, `DB_RAILS_REQUIRED=1`), skipped locally.

## `scraping` — is every portal still finding and fetching its ads? (wave 4)

`check_scraping` (`scripts/checks/scraping.py`; an alarm on the worker, cadence 600 s, label
"Scraping", unit "cells") judges four arms per portal, two of them per unit. Its words
(`GLOSSARY.md` § Collecting ads): a **walk** is one `scrape_runs` row with `run_type = 'index'`,
FINISHED when `ended_at` is set and `by_category` is not empty (a crashed or killed walk keeps `[]`
and is stamped later by the API's boot sweep: never finished); a **unit** is one `(source,
category_main, category_type)` label of `by_category`, which REACHED THE END in a walk when every
entry of that label has `walk_reached_end = true` (a unit absent from a walk was not walked in it);
**G** is the portal's median start-to-start gap of its finished walks over 14 d (the open gap never
counts; with fewer than 10 gaps, the slowest portal's G).

| cell | measures | Watch | Problem |
| --- | --- | --- | --- |
| `<portal>/walks/<cm>-<ct>` | hours since the START of the newest finished walk in which the unit reached the end | above 3 G (`scraping_walk_watch_gaps`) | above 6 G (`scraping_walk_fail_gaps`), or a unit walked in the last 30 d (`scraping_walk_recent_days`) with no end-reached walk in 14 d (`scraping_walk_stale_days`); a portal short of 10 gaps (`scraping_walk_fallback_min_gaps`) is scored on the slowest portal's G |
| `<portal>/walks` | for every portal of `claims_common.SOURCES`, on every pass: the hours since the START of its newest finished walk, with the units its walks of the last 30 d recorded (facts `finished_at`, `units`) | never: its units' own cells judge them | no walk in 30 d recorded a unit (no walk at all, or none finished), so a portal never drops out: it keeps its inflow, fetch-error and unnominated cells, and this cell's incident recovers on a measured OK |
| `<portal>/inflow` | ads discovered in the trailing 24 h — ads first seen in it, plus NEW-priority queue rows enqueued in it that have no listings row — against the median of the same 24 h 7, 14, 21 and 28 d earlier (ads only: those queue rows are gone by then) | below 50 % (`scraping_inflow_warn_share`), or above 300 % (`scraping_inflow_spike_share`) | below 20 % (`scraping_inflow_fail_share`) |
| `<portal>/fetch_errors` | of the ids attempted in 24 h (terminal `detail_queue_completions` plus live queue rows with `attempts > 0` claimed or enqueued in the window), the share with a failed attempt, judged on its Wilson lower bound | above 2 % (`scraping_fetch_err_warn`) | above 10 % (`scraping_fetch_err_fail`) |
| `<portal>/missing/<cm>-<ct>` | at the unit's newest end-reached walk, (Σ declared − Σ collected) / Σ declared over its entries | above 2 % (`scraping_missing_warn`) | above 5 % (`scraping_missing_fail`) |

Minimum evidence: inflow needs a baseline of 20 a day (`scraping_inflow_min_baseline`), else it
compares 7-day windows, and under 20 per 7 d it is Not measured; fetch errors need 200 ids
(`scraping_fetch_err_min_ids`), else 7 d, else Not measured, and ids given up 3 or more times in 7 d
are named in the message, not excluded; missing needs a declared total of 500
(`scraping_missing_min_declared`), and an entry whose `total_is_count` is true ("declared is a count
of what was seen": remax, maxima) or absent ("not yet recorded by this walk's adapter": every entry
written before wave 4) is Not measured, never 0 %. Its facts carry `declared_is_upper_bound`
(ceskereality's national total includes ads filed under no kraj, about 0.8 % of its rentals, under
the Watch line). One convention for every wave-4 share judged on a Wilson lower bound (fetch errors
here; publishing's visible, stock and photos; delisting's precision): the cell's `value` is the raw
share in percent (what happened), `facts.lower_bound` the bound it was judged on, and its message
names both.

Basis (the design's § 4): measured G is 7.15-7.33 h on the crawlers and 4.42 h on sreality, so a
unit reads Problem after about 43-44 h and 26.5 h — the operator's three/six-walk ruling of 10-13 in
time; over 14 days no unit crossed 6 G (the worst were transient Watch: bazos byt/pronajem 32.1 h,
idnes komercni/pronajem 26.7 h). Inflow's weekday medians are bazos 1,618, sreality 1,456, idnes
1,564 and ceskereality 807 a day; same-window baselines absorb the 30-45 % weekend dip, and an
hourly backtest found 0 Problems in 14 d. A sustained partial drop erodes the baseline within 2-3
weeks; the `coverage` gauge, judged against each portal's own declared totals, owns that case. The
worst normal fetch-error day is 0.18 % (bazos); sreality's old 25 % false FAIL was one poison id
(615387212, HTTP 409, #1851), here 1 of ~6,060 ids, 0.02 %. Missing: the latest walks collected
99.96-100 % of declared and no incident is on record; it catches a parser that drops cards on a walk
that still reaches the end.

Known limits: missing is judged per unit, not per walk scope as the design said, because entries
carry no scope key and labels repeat (bazos writes 22 entries over 10 labels; ceskereality and
realitymix list `dum` twice); a walk that stops between two scopes of one label reads as reached
when every scope it walked reached the end (a scope it never started leaves no entry); a dispatched
`--max-pages` / `--limit` / bazos region run finishes as an index walk, which shortens G and never
reaches the end (`scrape_runs` records no run arguments); and no per-attempt stamp exists, so the
fetch-error denominator approximates "attempted in 24 h". It replaces `walk_end_streak` and
`walk_coverage` (with `_SELF_CERTIFYING_TOTALS`, now the `Portal.total_is_count` seam); the old
page's SQL checks on the same questions keep rendering until wave 9.

## `publishing` — do new ads reach Browse quickly, with their photos? (wave 4)

`check_publishing` (`scripts/checks/publishing.py`; an alarm on the worker, cadence 600 s, label
"Publishing", unit "cells"):

| cell | measures | Watch | Problem |
| --- | --- | --- | --- |
| `<portal>/ingest` | age (from `enqueued_at`) of the oldest NEW-priority queue row, claimed or not, not given up, with no listings row for `(source, native_id)` — so `refresh_stale_image_urls`' known ads at that priority do not count | above 3 h (`publishing_queue_warn_h`) | above 6 h (`publishing_queue_fail_h`) |
| `<portal>/changes` | the same for the oldest CHANGED or FAILURE row, not given up | above 3 h | above 6 h |
| `<portal>/visible` | of active ads first written 30 min-24 h ago, the share whose property is not in `browse_list`, judged on its Wilson lower bound, split by the stage that blocks it: no property (`property_id` NULL), property not active (`status <> 'active'`), location not served (the property's canonical ad fails the consumer rule, `claims_common.served_location_predicate`, re-keyed onto that ad as `browse_projection` judges it), else not yet in `browse_list`; a property born after the newest successful rebuild started (`derived_artifacts.last_succeeded_at` of `browse_list`), or whose canonical ad's location was resolved or re-resolved after it (`listing_location.resolved_at`, rewritten on every re-resolve: the resolver never patches `browse_list`), is shown as waiting for the next rebuild, a fact, not a blocker | above 2 % (`publishing_visible_warn`) | above 10 % (`publishing_visible_fail`) |
| `<portal>/stock`, `fleet/stock` | of properties with `status = 'active' AND is_active`, the share missing from `browse_list`, by the same stages; a portal cell counts the properties with an active ad on that portal (counted from listings' `source`, `is_active` and `property_id` columns), the fleet cell all of them | above 0.1 % (`publishing_stock_warn`) | Wilson lower bound above 1 % (`publishing_stock_fail`) |
| `<portal>/photos` | of the photos of ads first written 1-24 h ago, excluding `unavailable_reason = 'listing_taken_down'`: not stored (`storage_path IS NULL`: pending, throttled, given up, `source_unavailable`, `not_an_image`), judged on its Wilson lower bound; and narrow, `stored_width` under 600 px (`publishing_photos_min_px`) among stored photos with a width | not stored above 2 % (`publishing_photos_unstored_warn`); narrow above 10 % (`publishing_photos_narrow_warn`) | not stored above 10 % (`publishing_photos_unstored_fail`); narrow above 50 % (`publishing_photos_narrow_fail`) |
| `fleet/dirty` | age of the oldest `dirty_properties.marked_at` | above 4 h (`publishing_dirty_warn_h`) | above 8 h (`publishing_dirty_fail_h`) |

Minimum evidence: visible needs 50 ads (`publishing_visible_min_n`), else a 72 h window, else Not
measured; the stock Problem needs 200 properties (`publishing_stock_min_n`); photos need 200 photos
(`publishing_photos_min_n`). The stock read is ONE short statement, because `browse_list`'s rebuild
swaps the table in behind its readers. Photo facts: the top `last_error` reason and the count of new
ads with no image rows, which the arm cannot judge (its denominator is photos, so a parser that
stops emitting galleries scores nothing: the realitymix 19-day blackout class). The active stock's
stored share is not computed: an 8M-row scan every 10 min; the image panels keep it until wave 8.

Basis: the 7-day maximum waits were 119-122 min for NEW (bazos politeness bursts on 10-12, 10-13 and
10-16, traced to the rate-ledger overflow, #1852) and 106 min for CHANGED, so the bazos 10-13 burst
(about 1,110 ads waiting at once, the oldest 119 min) stays green — queued ads are ingest's, never
visible's. Visible: 2 of 2,374 were not visible on 10-17. Stock: 20 of 362,883 active properties
live (0.0055 %: ceskereality 18, realitymix 1, mmreality 1); the 09-14 regression (Browse ~350k →
45,810 rows) reads ~87 %, the 09-17 one (29,545 bazos ads without a location) Problem on bazos.
Photos: 30,136 of 30,137 stored at build (1 `source_unavailable`), 0-2.8 % narrow (medians
1,199-1,600 px); the iDNES throttle took new ads with given-up photos from 5 % to 52 %, and a
thumbnail-template regression drives narrow towards 100 %. A CDN that refuses our transform parks
the photo `source_unavailable` (a 400 is terminal), so not-stored sees from our own rows the failure
the retired `sreality_image_template` probe looked for from outside, and narrow sees a downgrade
below 600 px (a thumbnail template: a 200 with a smaller rendition). Dirty: the 7-day maximum is 3.4
h, mid-sweep; the 08-20 outage held dirty rows for 6 d (09-15's 165 h was the sweep's age, which
`jobs`' `artifact/property_sweep` cell judges; the dirty queue stayed under 1 h that day, so the
dirty arm alone would have missed it); a rail in `tests/scripts/test_checks_publishing.py` keeps the
Watch above the full sweep's lease hold (`recompute_property_stats._MAX_BUDGET_SECONDS`, 6,000 s),
the guard the deleted `test_property_dirty_warn_clears_a_full_length_sweep` used to be.

Known limits: `listing_taken_down` can only be set for sreality, so a crawler ad made inactive
within 24 h leaves photos pending or `source_unavailable`, which count as not stored; a failed
`browse_list` rebuild only grows the "waiting for the next rebuild" fact on visible and stock, never
a blocker, so it is told once, by the `jobs` check's `artifact/browse_list` cell; narrow's floor is
an absolute 600 px, so a downgrade that stays above it (the one on record, the 749 px CROPPED frames
`docs/architecture.md` describes under Photos) reads 0 % narrow (owed: judge the stored width
against the declared source width); a change's age can include time its row spent at VERIFY, because
a promotion keeps `enqueued_at`. Births no longer wait for the daily full property sweep: it holds
the maintenance lease for up to its 6,000 s budget (`recompute_property_stats._MAX_BUDGET_SECONDS`)
while the worker's 30 s maintenance lane skips, so since 2026-10-20 it attaches stragglers after
every batch under that lease (a failed attach is logged and the sweep goes on), and a new ad waits
one batch at most. Before, it attached only at its start, and on a sweep day longer than about
40 min visible read Problem for the sweep's duration: the 28-day replay holds five such onsets on
four days (ceskereality and sreality 10-02, ceskereality 10-05, sreality 10-08, bezrealitky 10-09;
new ads held 41-100 min, each closed within 80 min). Those ads really were out of Browse past the
15-min target, so the onsets stay disclosed, not silenced; they age out of the replay by 11-06.
It replaces `acquisition_lag` (unclaimed NEW rows only, 6 h
/ 24 h), `property_maintenance`'s dirty arm (2.5 h / 3 h; its sweep arm is `jobs`'
`artifact/property_sweep` cell) and `sreality_image_template` with its live CDN probe.

## `delisting` — do ads that left their portal go inactive on time, and correctly? (wave 4)

`check_delisting` (`scripts/checks/delisting.py`; an alarm on the worker, cadence 600 s, label
"Delisting", unit "cells"). Its limits come from `scraper/delist_policy.py`'s `GONE_DWELL` (12 h)
and `RECHECK_THROTTLE` (24 h: `db.presence_candidates` re-nominates no ad with a completion in the
last 24 h; named in wave 4, unchanged), whose larger is the **effective dwell**, 24 h today
(`docs/architecture.md` rule #3):

| cell | measures | Watch | Problem |
| --- | --- | --- | --- |
| `<portal>/outflow` | ads made inactive in the trailing 24 h (`listings.inactive_at`) against the four weeks 7, 14, 21 and 28 d earlier, each read at every 24 h window ending within ±3 h (`delisting_outflow_align_h`) of this hour: `baseline` is the median of each week's quietest window, `baseline_busy` the median of each week's busiest | below 50 % of `baseline` (`delisting_outflow_warn_share`), or above 300 % of `baseline_busy` (`delisting_outflow_spike_share`) and at least 500 (`delisting_outflow_spike_min`) | below 20 % of `baseline` (`delisting_outflow_fail_share`) |
| `<portal>/stuck` | active ads whose first unrefuted gone verdict — the earliest `gone` completion newer than the ad's `last_seen_at`, found per ad before counting (`db.gone_evidence`'s rule) — is older than the effective dwell + 2 × the portal's p90 walk-start gap (finished walks, 14 d) + 1 h (`delisting_stuck_slack_h`) | 25 or more (`delisting_stuck_warn`) | 250 or more (`delisting_stuck_fail`) |
| `<portal>/verify_backlog` | age of the oldest VERIFY-priority queue row, claimed or not, not given up | above 6 h (`delisting_verify_warn_h`) | above 24 h (`delisting_verify_fail_h`) |
| `<portal>/deferrals` | the flip cap's `verify_deferred`, summed per finished walk | 2 consecutive walks in which a unit reached the end, each with deferrals (`delisting_deferral_walks`) | never: the cap is a throttle by design |
| `<portal>/precision` | of the `flipped` completions 48-72 h old, the share whose ad is active again (`listings.is_active` now), judged on its Wilson lower bound | above 10 % (`delisting_precision_warn`) | above 15 % (`delisting_precision_fail`) |

Minimum evidence: outflow needs a `baseline` of 50 a day (`delisting_outflow_min_baseline`), else
Not measured — maxima is unscored by its volume, never by its name, and because the floor is judged
on the quietest windows, remax and mmreality read Not measured about 68 % and 50 % of hours (45 %
each on fixed windows; judging the floor on the busiest median is an open ruling); precision needs 50 flips
(`delisting_precision_min_n`), else the cohort is pooled over 2-7 d. Outflow reads all its windows
in one statement: one `listings_inactive_at_idx` range per lag (the 24 h widened by ±3 h), binned by
the hour, then summed into the windows; a range per window made the planner seq-scan `listings`
(4.6 s against 0.12 s, 10-19).
The precision cohort reads a plain `completed_at` range over the pruned 7-day ledger with no portal
predicate: the planner reads it whole (about 200k rows, a parallel seq scan, ~0.3 s) more cheaply
than per portal through `detail_queue_completions_source_completed_idx`, which it prices about three
times higher; the gone index is partial on `outcome = 'gone'`, so it cannot serve the `flipped`
rows.

Basis: on 10-10 all nine portals made 0 ads inactive (no `inactive_at` stamp from 2026-10-09 21:39Z
to 10-11 06:04Z) against medians of 400-1,900 a day. A crawler's flips land in bursts at its walk
times, so the same hour one to four weeks back reads a walk that starts two hours late as a dip: on
2026-10-05 realitymix's midday walk started at 14:48 instead of 12:54 and one of its Sunday walks
was missing, and the same-hour comparison read 17 % for one hour, a normal day told as a Problem and
a recovery. Hence each week's quietest and busiest window within ±3 h of this hour: a dip is
judged against the quietest, a spike against the busiest. Revivals clear `inactive_at` 48-72 h after
a flip, so the baseline windows lose up to ~7 % of their flips (sreality; ≤ 5 % elsewhere) that the
current window still holds, and the ratio reads high by that margin. Stuck: none at build, the
oldest pending verdict 27.2 h; first verdict to inactive runs at p90 27.8-35.2 h, under limits of
38.9 h (sreality) and 43.5-47.5 h (crawlers). Verify backlog: the 7-day maximum is 203 min.
Deferrals: the 10-13/15 ceskereality catch-up deferred nominations in two runs (2 walks, then 3) and cleared itself each time. Precision: the
post-dwell steady state is sreality ~7 % (lower bound ~6 %) and at most 4.9 % (≤ 2.1 %) elsewhere;
the blink cohorts read 26-49 %, and the pre-hysteresis ledger, where one verdict flipped, put
sreality at 35 %, mmreality 15 %, remax 11 %, bezrealitky 7 % and idnes 6 %. The fixed 48-72 h
horizon removes the right-censoring that kept `false_delist_share` non-OK on 23 of its 24 runs. When
precision rings, read the flipped rows' pages (`detail_queue_completions` → `listings.source_url`)
before touching the dwell. At ship, sreality's 10-18 flip cohort reads ~17.5 % active again (lower
bound ~15.5 %), so its precision cell may open Problem on 10-20/21: the blink tail that the 24 h
rule (#1858) addresses, told truthfully, not suppressed.

Known limits: the stuck limit uses the portal-level walk gap, which understates portals whose units
rotate (idnes walks stalest-first; ceskereality and realitymix nominate per slice group; remax and
maxima per agenda); deferrals are judged per portal, because an agenda portal files an agenda's
deferrals on its first descriptor and a sibling-buffer portal on a group's last, and a walk counts
there when any unit reached the end (coverage's portal-level walk needs every unit to: each answers
its own question); outflow compares windows, not walks, so a walk that runs further off its baseline
weeks' timing than the alignment absorbs can still read as a dip; location-refetch rows share the
VERIFY priority and count in the backlog; "active again" includes probe and refetch revivals; and a
verdict older than the ledger's 7 days is pruned, so stuck can never see it. It replaces
`false_delist_share` (a mixed 7-day window) and `delist_flip_refused` (7 days of
`delist_flip_refusals`, a table nothing reads after it); the old page's `delisting_latency` and
`delisting_spike` matview checks rendered until wave 8 retired the page (wave 9 drops the matview).

## `coverage` — do we hold what each portal shows? (wave 4, gauge)

`check_coverage` (`scripts/checks/scraping.py`, beside the scraping alarm because it reads the same
walk ledgers; a GAUGE on the worker, cadence 3,600 s, label "Coverage", unit "cells"): a standing
condition, told once when it first reads Problem and then once a day.

| cell | measures | Watch | Problem |
| --- | --- | --- | --- |
| `<portal>/residual/<cm>-<ct>` | at the unit's newest end-reached walk, R = (H − P − D) / D: D the Σ declared of its entries, H its active ads now, P the active ads with a pending unrefuted gone verdict (in dwell) | R beyond ±2 % (`coverage_residual_warn`) | R beyond ±5 % (`coverage_residual_fail`) |
| `<portal>/unnominated` | active ads whose `last_seen_at` is before the start of their unit's newest end-reached walk (an ad with no unit: the portal's newest walk in which every entry reached the end), with no queue row and no completion newer than the ad's last sighting, less that walk's deferrals; a portal with no end-reached walk in 30 d reads Not measured | 25 or more (`coverage_unnominated_warn`) | 100 or more (`coverage_unnominated_fail`) |

Residual needs D ≥ 500 (`coverage_residual_min_declared`), and an entry whose `total_is_count` is
true or absent is Not measured, as in scraping's missing arm. Unnominated counts from the ad's last
sighting, not from that walk's start: an ad checked inside the 24 h recheck throttle before the walk
is in flight, not stuck (counted from the walk's start, 2026-10-19 read about 3,600 ads already
being rechecked: Problem on five portals). Each cell's facts carry `trend` and `worse_7d`, the shared
gauge trend (the quality gauges section, below); until Health dashboard sprint wave 6 they were `trend_30d`
(this check's own value 7 and 30 days ago) and `worse_than_7d` (the value grew), and the series restarted at
that deploy.

Basis: with in-dwell ads subtracted R is 0-0.15 % on idnes, sreality and bazos (held +1,708 / +1,405
/ +1,362 against in-dwell 1,600 / 1,251 / 1,371); ceskereality's houses (`dum`/`prodej`) read about
−14 % from the chaty-chalupy double count (#1854: 13.7 k declared against 11.8 k held), a Problem
every day of the replay and, live, from that unit's first walk after the deploy; ceskereality
byt/prodej's 27-day hole read +37 % (13,373 held against 9,771 declared). Unnominated: the
investigation counted 422 NULL-category ads on 10-18 (ceskereality 124, realitymix 298, the oldest
unseen 110 d: the ads no walk can nominate, #1853); on 10-19 the cell read 151 on realitymix
(Problem, the oldest last seen 112 d ago) and 96 on ceskereality (Watch), while bazos's ~9 in-flight
ads stay OK; the outcome definition counts the NULL-category ads without naming a portal. It
replaces `walk_coverage`'s gap arm; on the old page the SQL `stale_active` check (all of its rows
were NULL-category ads) and the category-trends panels rendered until wave 8 retired the page.

**Replay rails (wave 4).** Each portal check replays its INPUTS
(`tests/scripts/test_checks_{scraping,publishing,delisting}_replay.py`, coverage with scraping) from
fixtures under `tests/fixtures/health_*.csv`, exported read-only from production with the refresh
SQL in each test's header: walks, missing, residual, inflow and outflow from `scrape_runs` and
`listings`, where 28 days exist; fetch errors, stuck, the verify backlog and precision from
`detail_queue_completions` and the queue, where only 7 days exist, so that export ran first, before
the 10-13 bazos burst and the 10-12/13 cohorts aged out. Where no store keeps history — the 08-17
starvation, the 09-14 Browse drop, the 09-17 bazos location loss, the iDNES photo throttle, a 30 %
revived cohort — the incident-shaped fixture is SYNTHETIC, and its test says so with the figures it
reproduces; the 10-10 zero-flip day is real. What each replay opens, arm by arm, is listed in the
design's wave-4 As built note: the quiet stretches the data holds open nothing (the outflow replay's
are shorter than 14 days: its real stalls leave no onset-free fortnight), visible opens five
Problems on four sweep days (publishing's Known limits), coverage's residual also opens on three
single days of sreality byt/pronajem (09-21, 09-28 and 10-01) and on eleven units on 10-14/15 (for those the
replay can set aside only the in-dwell count of 10-19, no store keeping an as-of count), the inflow
replay's 09-28/29 onsets for idnes and mmreality are an artefact of its missing queue history (live,
the arm adds the NEW rows still queued, so a stopped drain is publishing's ingest, past 6 h), and
the bazos 10-13 burst stays green. Three seeded, rolled-back live tests
(`tests/test_checks_{scraping,publishing,delisting}_sql_live.py`, one step of CI's migrations lane)
run each module's statements with the check's own parameters, because the replays model the SQL in
Python and PREPARE checks only names and types. A rule-21 rail
(`tests/scripts/test_checks_rule21.py`) walks every `scripts/checks/*.py` and fails when a string
literal outside a docstring holds a portal name as a whole word (any case), the value of
`portal_runner.DECLARED_TOTAL_KEY` or the identifier `sreality_id`: so a check imports the portal
list and the declared-total key rather than spell them, never joins on `sreality_id`, and says
"maximum values" where English would say "maxima". It imports the queue priorities,
`delist_policy`'s constants and the served-location rule by convention: numbers and SQL the rail
cannot see.

## `dedup` — is the engine deciding and merging, and does production honour your rulings? (Health dashboard sprint wave 5)

`check_dedup` (`scripts/checks/dedup.py`; an alarm on the worker, cadence 600 s, label "Engine", unit
"cells", owner page `/autodedup/progress`, after `coverage` in `CHECKS`). Its words (`GLOSSARY.md`
§ Changing what belongs together): the **engine** merges only inside the **apply scope**
(`app_settings.autodedup_apply_scope`), decides into the **live generation**
(`autodedup.incremental.GENERATION`), pauses its merges while it **builds** an area it was just
seeded on (`autodedup.settings` `rt_bootstrap:rt`), and runs one pass, seed or batch run at a time
under the **lease** (`autodedup.rt_lease`). An ad's **decision** is its `autodedup.rt_fp` row:
`first_decided_at` is stamped at the start of the pass that committed it and kept on every later
refresh. An **engine merge** is a live `property_merge_events` row whose `source` is
`autodedup.apply.MERGE_SOURCE`, an **operator merge** one whose `source` is `'operator'`; a split's
birth rows (`property_identity.BIRTH_REASONS`) are not merges, and the joins of a split by letters
are operator merges. A raised, late or stuck pass stays the `jobs` check's `lane/autodedup` cell;
this check judges what the engine's own stores say.

**The state is a fact, never a status.** Every cell carries `facts.state`. In precedence order:
**off** (`realtime_autodedup_interval_seconds` absent, unparseable or ≤ 0, the key imported from
`autodedup.apply`), **building** (`rt_bootstrap:rt` true under `incremental.setting_flag`),
**merging** (the apply scope admits a live merge, parsed with `apply.Scope`) and **idle**
(otherwise: the engine decides, and merges nothing). An arm the state makes irrelevant reads OK
with the state and the reason in its message, never omitted, because an omitted subject would leave
its incident open for 7 days.

| cell | measures | Watch | Problem |
| --- | --- | --- | --- |
| `state` | `<state> since <ago>`; facts `state`, `state_since`, `interval_s`, `scope_blocks`, `seed_version`, `newest_merge_age_s`, `last_commit_age_s` (the `rt_new` scan cursor's `updated_at`; a committed pass rewrites it only when its new-feed window held rows, so on a corpus with no new ad at all it ages while passes still commit), and merge groups in 24 h and 7 d, `merges_24h_engine`, `merges_7d_engine`, `merges_24h_operator`, `merges_7d_operator` | never | never |
| `stream` | while the lane is on, whether the live stream is live: not when the area is unseeded (no `rt_calibration` row), its seed version (`rt_seed_version:rt`) is not `incremental.SEED_VERSION` (production merges stop silently: this is their only signal), or the store is absent (`RT_STORE_PRESENT_SQL`); the message names which | any of the three | never |
| `build` | while building: first decisions per hour in the last 3 h (`dedup_build_window_h`), the window clipped at the build's start (`rt_bootstrap:rt` `updated_at`) and at the last brake lift (the newest `app_settings_history` row of the interval key whose stored OLD value is 0); the backlog (`RT_SCOPE_BACKLOG_SQL`, bound as the lane binds it) and ETA = backlog / rate, a lower bound; facts `blocks_total` and `blocks_walked` (`RT_SCOPE_SCAN_SEEN_SQL`) | the ETA above 7 d (`dedup_build_eta_warn_d`) | 0 decisions in a clipped window at least 3 h long, with backlog > 0 and the lane not braked |
| `merge_pause` | while building: hours since the newest engine merge, measured from the build's start (operator merges go on during a build and do not count) | above 48 h (`dedup_pause_warn_h`) | never (Q3) |
| `decision_lag` | while merging or idle: of the ads in the engine's area first seen 1-24 h ago (the lane's own new-feed statement, `RT_NEW_LISTINGS_SQL`, imported whole, with `RT_FP_READ_SQL` for their decisions in place of the contract's `RT_KNOWN_SQL`: the same predicate plus the first-decision stamp), the share with no decision, judged on its Wilson lower bound; Not measured within 24 h of a brake lift, a build's end, a seed or an engine change, unless the lane has stalled, or a Problem this arm read is still open and the cohort is clean, which then reads OK through the excuse (below); facts `n`, `undecided`, `share`, `lower_bound`, and `p50_min` / `p90_min` arrival to decision over the decided part, and while a Problem is open `open_reading`, `opened_at` and `recovering_since` | above 1 % (`dedup_lag_warn`) | above 10 % (`dedup_lag_fail`) |
| `lease` | the `rt_lease` row (`RT_LEASE_READ_SQL`, imported); facts `holder_kind` (`pass`, `seed`, `dispatch`, `other` or `none`), `remaining_s` and, for an ended lease, `past_expiry_s`, `untaken_s`, `untaken_since` and `ended_by` (release or expiry) | a live lease held by anything but the live worker, after a 120 s deploy grace: a predecessor's pass (a pass holder whose start, its third field, is before the worker's `worker_heartbeats.started_at`), a seed, a batch run or an unknown holder | while merging or idle and seeded, no pass has taken the lease for the lane's interval + 10 min (`dedup_lease_fail_past_expiry_min`; 11 min at the 60 s interval), counted from the latest of its expiry, the last brake lift and the build's end: the lane stopped taking it |
| `rulings/ads` | pairs whose standing negative ruling (the newest `autodedup.verdicts` row per pair in `ui_sql.NEGATIVE_VERDICTS`, or a `must_not_link` row of any source) has both ads on one property now while either ad carries a live engine merge newer than the ruling, timed at the start of its current negative spell; facts `breaches`, `negatives` (standing pairs), `together_now` and `set_rulings_unjudged` | never | 1 or more (`dedup_rulings_fail`) |
| `rulings/brokers` | active `broker_merge_suppressions` whose two identities share a broker (the retired check's SQL, verbatim); facts `active`, `lifted`, `violations` | never | 1 or more (`dedup_rulings_fail`) |
| `splits` | of the engine's merge groups created 7-14 d ago (a matured cohort: an operator had a week to notice), the share with a member an operator split off (`undone_by` not the engine's own `mode=unapply`, `UNAPPLY_BY_PREFIX`), judged on its Wilson lower bound; facts `groups`, `split`, `share`, `lower_bound` and `unapplied` (the engine's own unapply, a fact) | above 2 % (`dedup_splits_warn`) with at least 100 groups (`dedup_splits_min_groups`) | never |

Each cell's `value` is the number it is judged on: decisions per hour (`build`), hours
(`merge_pause`), the raw undecided share in percent (`decision_lag`), `remaining_s` or, once it has
ended, the minutes the lease has gone untaken on the Problem's clock (`lease`), breaches and
violations (`rulings/*`), the raw split share in percent (`splits`), none for `state` and `stream`;
the shares follow wave 4's convention, the bound in `facts.lower_bound` and in the message.
`together_now` counts ruled-apart pairs sharing a property by any path, read from
`listings.property_id` alone (no ledger read): the operator's pending splits, a fact, never a
status. The `splits` message reads "N of M groups the engine merged 7-14 d ago were split by an
operator (x %; lower bound y %)".

Minimum evidence: `decision_lag` needs 20 ads (`dedup_lag_min_n`). It reads Not measured while the
lane is off or building ("building") and, with the reason, when a brake lift, a build's end, a seed
(`rt_seed_version:rt` `updated_at`) or an engine change (`rt_engine:rt` `since`: E960 re-decides the
old engine's groups within 12 h, and arrivals wait) sits inside the window's 24 h, because brakes,
builds, seeds and engine changes are not lag. Two rules reach through the four excuses, the stall
rule always and the recovery rule only for an OK. The stall rule: once no pass has committed a new
ad for an hour, counted from the latest of the `rt_new` stamp, the last brake lift and the build's
end, a lane that stopped deciding is judged whatever changed, and the message names the excuses it
overrides. The recovery rule exists because Not measured is inert on the ladder, which closes an
incident only on a second stored OK at least 1,800 s after the first, measured on `run_at`: an
incident the excuses hid would otherwise wait up to a day for them to age out. A Problem opens the
reading (`open_reading`, with `opened_at` the Problem's time, unchanged while it is open); a Watch
opens nothing, as it opens no incident. While the reading is open and an excuse holds (the lane has
not stalled), the cohort is read anyway: a clean one reads OK, its message ending "judged through
the brake of the last 24 h so the open reading can recover", and one that would read Watch or
Problem reads the excuse's Not measured message as before, because the excuses' backlogs are not
lag; a reading judged through an excuse is therefore OK or Not measured, never a new onset or a
reminder. With no excuse the arm is judged as usual. Every OK sets `recovering_since` when it is
unset, a Watch or a Problem clears it (the ladder resets its own clock on any measured non-OK), and a
Not measured reading carries it with `open_reading` and `opened_at`. The reading closes, and the arm follows the excuses again, when the previous stored
reading was an OK whose `run_at` lies at least 2,400 s after `recovering_since` (`_RECOVERY_S`: the
ladder's 1,800 s hold plus one 600 s cadence, measured time as the ladder's own recovery is, so a
gap with no stored reading never expires it), when `opened_at` is older than 7 days (the ladder's
unmeasured close), or when a seed or a build start follows `opened_at` (a new generation: the
opening reading's cohort is gone).
`splits` needs 100 groups, else Not measured. `build` reads OK "waiting for the walk of N blocks"
when the backlog is 0 but blocks are still unwalked.

Basis (the design's § 4, re-measured on 2026-10-19 where it had moved). Build: the Prague build
(seeded 10-16 16:27Z, done 10-18 about 20:10Z) decided 6,659-16,275 ads an hour in its first wave
(the 60 blocks the seed walked) and 1,771-7,654 in its second (median about 3,000; the design's band
was 3,000-7,600), its largest ETA 50.7 h; the 10-02 statement-timeout stall decided nothing for
about 27 h, braked for about 22 h of it. From 10-16 22:00Z to 10-17 16:00Z the build decided 1,583
ads in 18 h (the design's ~90/h) because the seed's walk had spent the day's 60 block scans
(`MAX_ENTER_SCANS_PER_DAY`): the other 60 blocks, about 95k ads, waited for the walk of 10-17
16:28Z, and as they had no snapshot rows the backlog read about 0, so the ETA was about 0, not the
design's ~30 d Watch. The cell reads that wait as OK "waiting for the walk of 60 blocks" and its
facts name the blocks; the design's "64,432 of 155,801" was read after that walk.
`RT_SCOPE_BACKLOG_SQL` ran 101 ms on 10-19 (the design's 48 ms was a smaller store's). Merge pause:
the Prague build paused the engine's merges 52.3 h (10-16 16:01Z → 10-18 20:17Z; the design's 29 h
was read mid-build), so Watch for its last ~4 h, and the S1-widened build of 10-10 → 10-13 about 80
h, Watch for about 32 h. Decision lag: on 10-19 at 08:32Z, 41 of 414 such ads were undecided (9.9 %,
lower bound 7.4 %), all newer than the `rt_new` cursor (no decision pass had committed since
07:55Z), and at 09:00Z, an hour past the last commit, the stall rule judged 55 of 421 (13.1 %, lower
bound 10.2 %): Problem; arrival to decision over the decided 373 was p50 10.8 min, p90 55.9 min, not
the design's 5.9 / 8.0 min, with the 10-18/19 merge-window brake and the slow passes after 04:25Z
inside the window. An as-of replay every 2 h from the durable stamps read 2.5-4.7 % (lower bound) at
the end of that 5 h brake, which the brake exclusion keeps Not measured. Lease: a pass takes it for
40 min (`LEASE_TTL_S`), a seed or batch run for 5 h (`DISPATCH_TTL_S`); every deploy shows the old
container's lease to the new worker for about a minute, until the old container's SIGTERM path
releases it (E941), hence the grace; the 8-10 strands on record (10-03 to 10-12) each ended by the
TTL or a hand release and were re-taken within about a minute: Watch at most, never Problem. A brake
leaves the lease as old as itself (14 lifts in 28 d), so Problem counts from the latest of the
expiry, the last lift and the build's end, and a strand a database restart leaves under the live
process (10-10 05:57Z) reads OK until the next deploy: no store tells its dead pass from a working
one. Rulings: 765 standing negative pair rulings, the same 765 pairs as `must_not_link`, and 0
breaches; one ruled-apart pair shares a property, held there by the removed engine's merges
(`together_now`). The engine's two false merges on record (10-01 and 10-03) were ruled after they
merged, so they are not breaches; from 10-19 08:18Z E969 relaxes the one-storey fact a "different"
ruling rests on, and this cell is its safety net. `broker_merge_suppressions` has never held a row,
and the retired check read OK on 61 of 61 runs in 7 d. Splits: on 10-19, 0 of the 149 engine groups
created 7-14 d earlier had been split; all-time 7 of 8,319 (0.08 %, lower bound 0.04 %), all by an
operator, none by `mode=unapply`, and 0 of the 6,693 created in the last 7 d. The design's "7 of
1,626 = 0.43 %" counted the same 7 over the smaller store of its day. At Prague volume, about 6,700
groups a week, the 2 % bound needs about 2.35 % split (157 groups); the operator accepted about 1.3
% false merges for the Prague rollout.

Known limits: builds before 10-16, the 10-02 stall and every lease strand left no durable trace (a
fresh seed deletes the generation's decisions, and `rt_lease` is one row), so none is replayed but
the 10-10 strand, as a SYNTHETIC replay (the hermetic tests hold their Problem shapes);
`RT_SCOPE_BACKLOG_SQL` is blind to blocks not yet walked and to the part of a block past the walk's
20,000-ad slice (`ENTER_SLICE`), the ETA is a lower bound (a pass costs more as the store grows), and
the active/inactive split of the backlog § 4 asked for is dropped (0.35 s and 663k buffers at a
build's start); set-level negative rulings that name no ads (3 today) cannot be judged
(`set_rulings_unjudged`); a pass that aborts on its pair budget or deadline is a completed pass to the
heartbeat (`failed_passes` stays 0): since Health dashboard sprint wave 5 `jobs` reads it from
`last.aborted` (Watch once, Problem when a second pass aborts), and this check sees it as
`last_commit_age_s` at once and as decision lag after an hour without a commit; a failed re-cut
(`last.recut_failed`) has no arm, and neither have E939's "a reconcile wait past its bound" and
"daily-cap skips" (`reconcile_waiting`, `reconcile_stale` and `reconcile_deferred` live in the
heartbeat's per-pass `last` only); `decision_lag` is blind most hours at today's tempo of sittings
(96 of the 169 hourly readings to 10-19 09:00Z fell within 24 h of a brake lift: 7 lifts in 7 d, 14
in 28 d), before the Prague build, its end and the engine changes are counted; a lease stranded by
a database restart under the live process reads OK until the next deploy; the lease holders'
`rt_seed:` and `dispatch:` prefixes and a pass holder's shape are copied from the engine, which
spells them inline, and pinned by tests to its writers (an `rt_lease` constant is owed to the E96x
stack), and `autodedup.incremental_lane` is imported at load beside `autodedup.apply`; under the
held E964, building and merging overlap, and the merge-pause arm then needs the per-block rail (owed
to that PR); `app_settings.updated_at` is not stamped by its writers, so every age comes from
`app_settings_history` (which stores the OLD value at `replaced_at`) or the engine's own stamps; and
until wave 8 the page shows one row per check, so its unit renders "Ncells" and the cells live in
`pipeline_checks_public.details`. Rule 15: the check reads only the live generation's stores and
the `property_merge_events` rows whose `source` is the engine's or `'operator'`, by a positive
filter (the merge window's id floor, which fixes an id boundary and reads no row's content, is the
one ledger read without one); the removed engine's merges are the retire tool's business
(`mode=unapply`, `retire_legacy=1`), never a Health arm.

It replaces `broker_merge_suppression` (its SQL is the `rulings/brokers` cell's); the `verify_pipeline`
judgement of the lane that `docs/design/autodedup/PROGRAM.md` E939 and E941 owed (CUTOFF C7 had
deleted the removed engine's six checks); the heartbeat's `latency_p50_s` / `latency_p95_s`, deleted
with their log fields (they measured seed age: arrival to decision is `decision_lag`'s `p50_min` /
`p90_min`, from the stores); the heartbeat's `dirty` read as a backlog (a neighbourhood); and the
engine progress page's `MODE = 'SHADOW'` chip as the de-facto status.

**Replay rails (Health dashboard sprint wave 5).** `tests/scripts/test_checks_dedup_replay.py`
replays the check's inputs hourly over 28 days (2026-09-22 00:00Z to 10-19 09:00Z, 658 ticks)
through the real ladder from fixtures under `tests/fixtures/health_dedup_*.csv`, exported read-only
from production with each export's SQL and query time in the test's header: the Prague build's
hourly first decisions from 10-16 16:27Z (the only build any store keeps until the next fresh seed
deletes it) with its walk waves and backlog, the decision-lag cohorts per hour since 10-16, merges
per hour by source and splits per day over 28 d, the brakes and apply-scope changes from
`app_settings_history`, rulings per day, and the commits per 10 min of 10-19. It opens one incident:
`decision_lag` at 10-19 09:00Z (55 of 421 undecided, 13.1 %, lower bound 10.2 %, 65 min without a
commit). The only other reading past OK is the Prague build's merge pause, Watch at 10-18
17:00-20:00Z (48.5-51.5 h), never told; the build arm reads OK "waiting for the walk of 60 blocks"
for 19 h and its largest ETA is 50.7 h, `splits` is worst at 6 of 487 (lower bound 0.6 %) and
`rulings/ads` holds 0 breaches with 246 to 765 rulings standing. The lease, rulings and pre-seed
decision-lag arms see constant or empty inputs there (no lease row, the export's breaches on every
tick, no arrival before the seed), so their quiet is weak evidence. The 10-02 stall and the builds
before 10-16 are not replayed (later fresh seeds wiped their decisions; the hermetic tests hold the
build arm's Problem shape); the one SYNTHETIC replay, and its test says so, is the 10-10 lease
strand at 10-minute ticks: Watch at 06:10-06:30Z, never Problem.
`tests/test_checks_dedup_sql_live.py` (a step of CI's migrations lane) executes the check's
statements, seeded and rolled back: a "different" pair joined by an engine merge after its ruling is
a breach, the same with `source = 'auto'` is not (the executed rule-15 proof), and so is a pair
merged before its ruling. `tests/scripts/test_checks_dedup_rule15.py` walks the module's string
constants and the text of every statement it sends and fails on `'auto'` as a source literal, a `<>
'auto'` filter, a generation other than the imported `GENERATION`, or an import of
`autodedup.legacy_retire`; the rule-21 rail runs over it unchanged.

To check after the merge: `health_checks` holds the eight `realtime-worker` keys, `dedup` among them
(label "Engine"); within 10 minutes of the worker's deploy Health shows its row (a dot, the label,
the value and the time; the cells render in wave 8); the `broker_merge_suppression` row leaves at the
canary's first run from `main`; and the engine progress page says "The engine merges in production
inside the apply scope; live lane health is on Health.", its link landing on the row.

## `notifications` — does every push leave? (notifications push W4, 2026-10-21)

`scripts/checks/notifications.py`, a platform alarm on the worker every 10 minutes, owner page
`/notifications`, a 5 s budget (four reads over the 24 h event window and the push ledger, a few
hundred rows). Its statements live beside the pass they judge, in `api/push_delivery.py`, and are
imported inside the check: the registry must import with the base dependencies on GitHub (test.yml).

| Cell | Problem / Watch | Why this line |
|---|---|---|
| `push/stall` | Problem: a `pending` or `failed` delivery whose `next_attempt_at` passed more than 10 min ago (`notifications_stall_min`), its device not on hold | the lane ticks every 15 s and a claim's lease is 2 min, so a healthy row is never due past ~2.5 min; a terminal failure waits at `'infinity'` and never counts; a device Apple throttled (`push_subscriptions.hold_until`, a 429's Retry-After, up to an hour) keeps its rows due and unclaimed by design, with their attempts handed back; a row a dead process claimed 5 times is failed `lease_expired` by `EXHAUSTED_SQL`; not judged while the lane is braked (nothing is claimed then, so every row is due by design; `push/unconfigured` says so once) |
| `push/recall` | Problem: a VISIBLE unseen event of the last 24 h, more than 5 min old (`notifications_recall_grace_min`), whose account has a live device registered before it, with no `notification_deliveries` row for that pair | fan-out runs every pass (15 s), so a missing row after 5 min is a lost push (the F2/F10 class); not judged while the lane is braked, which `push/unconfigured` says |
| `push/failures` | Problem: 3 or more terminal failures (`status = 'failed'`, `next_attempt_at = 'infinity'`) created in 24 h (`notifications_failures_fail`); the message names Apple's reason words | one `BadDeviceToken` is one device's churn; three in a day is a key, payload or JWT fault |
| `push/unconfigured` | Watch: the push lane's newest pass found no `VAPID_PRIVATE_KEY` (`last.unconfigured`) while a live device exists | the brake is deliberate, so never Problem; Not measured when the worker's heartbeat is stale or missing (the watcher's `worker` cell judges that) |

The three-strike rule (iOS removes an origin's subscriptions after silent pushes) has no server
signal: prevented in `sw.js` (always one notification), never measured here. Gauges (open rate,
latency, skip mix, churn by `end_reason`) are design-doc SQL, reviewed after 7 days (PROGRAM.md § 3.9).

## `parsing` — are new ads still read the way they were? (Health dashboard sprint wave 6)

`check_parsing` (`scripts/checks/quality.py`; an alarm on the worker, domain `portals`, cadence 21,600 s,
budget 180 s, label "Parsing", unit "cells", owner page `/scrapers`). The regression detectors of field
capture and the measure program, folded into one per-portal alarm: a field's fill dropping, new ads losing
their per-m² measure, a jump in a stock median, an unreadable location payload, a re-snapshot storm. Every
subject is emitted on every pass, OK cells included (about 540, 541 on 10-19), so a cell recovers on a
measured OK, while the measure view is readable: when it is not, the measures and median subjects (the view's
rows) cannot be named, and `details.not_measured` and the message name both arms with the cause. The check's
message is its worst cell's, and a Watch never rings.

| cell | measures | Watch | Problem |
| --- | --- | --- | --- |
| `<portal>/fill/<field>` | the field's fill over the portal's active ads (`field_census`'s matrix statement, on the check's clock) against the blessed baseline (the fill and vocabulary section, above) | a drop of more than 10 pp from a baseline at least 10 % filled; one blessed cell no longer measured; or no active ad carrying a field the attribute contract names a producer for | a drop of more than 20 pp; fewer than a quarter of the blessed filled ads where 50 or more were blessed; or the whole portal no longer measured |
| `<portal>/vocabulary/<field>` | the field's off-canon share (canon: `toolkit/filter_registry.COLUMN_CANONICAL_VALUES`) against the baseline | a rise of more than 5 pp | a rise of more than 15 pp |
| `census` | the age of the checked-in key censuses (`data/field_capture/census/`) | older than 30 d | never |
| `<portal>/measures/<cm>-<ct>` | the gap, the share of ads with no per-m² measure (no price, or no positive area), among the cell's active ads first seen in 7 d, judged on its Wilson lower bound against the same cell's stock gap (`measure_plausibility_by_source`) | the bound above the stock gap by more than 0.10 (`parsing_measures_warn`) | by more than 0.25 (`parsing_measures_fail`) |
| `<portal>/median/<cm>-<ct>` | the active stock's median Kč/m² (`measure_price_per_m2`) against this check's own median 6-14 d ago; ratio = max(now / then, then / now); Not measured for the first 6 days, after a change of basis, and when either median is 0 | above 2× (`parsing_median_warn`) | above 3× (`parsing_median_fail`) |
| `<portal>/payload` | among the portal's active ads first seen in 48 h, the share whose stored payload the location contract cannot read (`claims_common.PAYLOAD_SHAPE_PREDICATES`) | above 3 % (`parsing_payload_warn`) | above 10 % (`parsing_payload_fail`) |
| `<portal>/churn` | re-snapshots (a listing's second and later snapshots) written in 24 h per active ad, against the portal's own 28-day median daily ratio; facts the ratio and the median | above 3× (`parsing_churn_warn_x`) | above 6× (`parsing_churn_fail_x`) |

The fill and vocabulary numbers are `THRESHOLDS` keys `parsing_fill_*`, `parsing_vocabulary_*` and
`parsing_census_stale_days`, passed to `field_census` by name. Minimum evidence, below which a cell is Not
measured: 50 new ads for the measures cell (`parsing_measures_min_n`), 200 ads carrying the measure in both
readings for a median (`parsing_median_min_n`) and 30 for the payload cell (`parsing_payload_min_n`). A churn
cell under 500 re-snapshots in 24 h (`parsing_churn_min_snapshots`) reads OK ("under the 500 a storm needs"):
the quiet day was counted, so it is measured, and churn is Not measured only for a portal with no active ad.
maxima's handful of arrivals (2 in 48 h and 21 a week on 10-19) keeps its measures cells Not measured; it has
no payload cell, because its contract reads the page.

**Fill and vocabulary.** The cohort, the sizing and the "known, not judged" rules are the fill and
vocabulary section's, above. The matrix read 3.7 s in production on 10-19. Its one Problem onset, 2026-09-22
05:06Z (bazos `condition` and `energy` filled 133 → 33), was the deleted description lane's residue being
overwritten by refetches; it cleared on that day's re-bless, not on a fix. The goldens were re-blessed again
in this wave, read-only against production (10-19 13:53Z), by the bless path's own parts rather than
`python -m scraper.field_census --bless`, which had no database credentials here: the module's statements ran
through the Supabase MCP, each md5-matched to the module constant it came from, and their rows went through
the module's own reducers and writers. The nine censuses of 09-21 would have read stale from 2026-10-22
19:20Z, and idnes `has_balcony`'s 26.8 % → 13.7 % is wave 4's definition change (terraces out, #1567)
converging as refetches overwrite about 17,845 terrace-only ads, not a regression. The worker image carries
`data/field_capture/` (the Dockerfile copies `data/`).

**Measures of new ads.** The legacy `ppm2_measure_coverage` judged the gap itself: its stock arm read Watch at
0.95 (a standing, sanctioned gap) and its 7-day arm Problem at 0.90. The design's carry floor (Watch below
0.95, Problem below 0.90) turned those numbers upside down and would have rung 13 Problem and 6 Watch at ship
on standing gaps (19 of the 32 cells with 200 new ads on 10-19; bazos's classified ads carry no area on about
a fifth of its arrivals), while the legacy rule needs 6.3 d of a total collapse and never sees a partial one
(remax's ~44 % of pages with no price path, fixed 2026-08-05). Judging the 7-day gap's lower bound against the
cell's stock gap cancels the gaps both share, so only a change in new ads reads. The stock gap itself is the
`data_quality` gauge's `measure_gap` cell. The arm is also the anti-silencing rail: a portal that started
writing NULL areas would make the floor and divergence shares go quiet, and this cell reads it first.

**Median shift.** Each cell's own median against itself a week back, never a peer: portal medians legitimately
differ by up to 19× on mix (idnes's rural land is 8.5× the peer Kč/m²). Over the active STOCK, never arrivals:
on a two-day burst of cheap land (bazos pozemek/prodej, 10-12/13: 2,534 ads at 23-28 Kč/m²) the arrival
cohort's weekly median fell 810 → 61 Kč/m² (13.3×) while the stock moved 1.49×. Since 09-26 no stock median
moved more than 1.49× in a week; every move of 2× or more in the retained 35 days came in 09-19..09-25, when
the 09-17 W19/W21 heals moved five per-m² medians 3.2-6.9× (W19's spaced thousands on the land of bazos,
~800 → 117 Kč/m², ceskereality, realitymix and remax; W21's mmreality plot on its houses for sale). The
2× / 3× lines were sized on the noisiest real weekly move of 2026-08 (1.90×), and each median is gated on its
own support, the ads carrying the value, never the cell's size (bezrealitky pozemek/prodej once held 1,643
active ads with nine areas spread 17.6×). Only the per-m² median is compared (the old check compared the area
median too). The baseline is the facts (`median`, `price_per_m2_basis`, `n`) of this check's own cells 6-14 d
ago, and a cell whose basis changed since is Not measured, as is one whose median is 0 now or then (land's
per-m² price is rounded to 2 dp with no floor, and a shift is the ratio of two positive medians);
`ppm2_median_shift`'s rows, which kept their medians under `details.cells` as an object, are never bridged, so
the arm is Not measured for its first 6 days. Each median carries `price_per_m2_basis` and names its unit
through `toolkit.measures.unit_label(basis)` (a rent cell reads Kč/m²/měs: rule 23). A heal or a re-bless
reads as a shift and must be acknowledged: no heal ledger exists yet.

**Payload shape.** Three portals' location contracts read the stored payload: sreality's API JSON
(`locality`), bezrealitky's GraphQL answer (`ruianId`) and mmreality's embedded JSON. The other six read the
archived page and have no payload cell; a page-markup change there reaches Health through the fill arm and the
location gauge. `location_data/claims_common.PAYLOAD_SHAPE_PREDICATES` holds one NULL-free SQL predicate per
portal over `l.raw_json` (TRUE = readable: sreality's `locality` an object carrying a post-cutover key,
bezrealitky's `ruianId` key present, mmreality's `municipality` and `point` keys present), spelled from the
same key lists as the Python reader beside it (`sreality_payload_shape`), and `PAYLOAD_SHAPE_REMEDIES` the
remedy per portal; the check wraps each predicate in `coalesce(…, false)`, so a truncated payload never counts
as readable, and binds the portal name, so it names none (rule 21). "Readable" means the shape new ads are
expected in: the contract still reads sreality's legacy shape through its fallback paths, but a new ad
arriving in it is a regression. Why 48 h: a new ad gets its detail fetch within the hour, so after a
portal-side change the share climbs as elapsed / window: one 6-hourly pass into 48 h reads 12.5 % and crosses
10 %, where a 7-day window would sit at 3.6 % for about 1.4 days. No unreadable arrival in 28 d (35,127
sreality and bezrealitky arrivals, 10-19); mmreality's predicate is new and has no history.
`tests/location_data/test_payload_shape_parity_live.py`, in CI's migrations lane, executes each SQL predicate
on fixture payloads and asserts it agrees with its Python twin: the "parity-tested" this section used to claim
had been false since location W1-a (5d3fc621) deleted that test.

**Churn.** A volatile hashed field re-snapshots the corpus and fans out watchdog price-drop keys. The absolute
lines of the old page's SQL rung (0.5 / 1.5 snapshots per active ad per 24 h, migration 354) sat above every
storm on record: idnes's FX price jitter wrote 95.5k snapshots a week, 47 % of all, until #683 on 2026-07-02,
about 0.12-0.14 per active ad against a 0.03 normal, and the 28-day daily maximum on 10-19 was 0.100 (idnes,
10-01). So the arm counts re-snapshots only (a listing's first snapshot is its arrival) and judges each portal
against its own 28-day median daily ratio, with at least 500 re-snapshots in the 24 h, under which the cell
reads OK, so a small portal's quiet day cannot ring.

**Known limits.** The stock arms are slow: a field that goes NULL on every new write reaches the fill arm's
10 pp Watch in 1.8-23 d (2-6 % of a portal's stock is rewritten a day; the slow end is a field 27 % filled), and
the collapse arm needs 23-69 d. The median arm is blind for 6 days and reads a heal or a re-bless as a shift.
No parsing-family check ever caught a parser regression in flight: every real onset of the legacy checks was
a heal, a clean-up or a standing defect, and people found every in-flight regression on record, after 13.7 h
to ~131 d; these thresholds are calibrated on heals, standing defects and the measured series, not on a true
positive. A churn storm present from a portal's first day is its own median and stays invisible. The payload
arm reads new ads only, so existing ads rewritten from a wrong page (mmreality's substitute cards, 09-07) pass
it. Media (photos per new ad: realitymix's new ads had none for 19 days, 07-16 to 08-04) and value anomalies
(placeholders such as idnes floor = 20, sentinels such as sreality zip = −1, and a storey-scale flip such as
sreality's old-API rows, which `floor_convention` watched until Q4 deleted it) have no arm: filed, not built.

It replaces `field_fill_matrix`, `ppm2_measure_coverage`'s 7-day arm, `ppm2_median_shift` and
`location_payload_shape_drift` with `SREALITY_SHAPE_CASE_SQL` and `_LOCATION_SHAPE_REMEDY` (portal literals in
shared code); and on the old page, until wave 8 retired it, the SQL `snapshot_churn` and `field_null_drift` rungs (wave 9 drops them).

## The quality gauges — `data_quality`, `location_town_coverage`, `text_extraction` (Health dashboard sprint wave 6)

Three gauges on the worker, in `scripts/checks/quality.py` beside `parsing`, domain `data_quality`, each with
the 45 s budget: standing conditions with an owner, told once when a cell first reads Problem and then once a
day until it recovers or is acknowledged (Q2); a Watch sends nothing. Every subject is emitted on every pass,
OK cells included, while the measure view is readable: when it is not, `data_quality`'s floor and measure_gap
subjects (the view's rows) cannot be named, its divergence and estate-sum cells read Not measured naming the
cause, and `details.not_measured` and the message name the view.

**Trends.** Every gauge cell carries `trend` and `worse_7d` (`scripts/checks/common.trend`, which `coverage`
uses too since this wave): `trend` is the cell's [day, value, status] for each of the last 30 Europe/Prague
days, rolled from the newest measured previous result that carries the subject, so a Not measured run never
resets it, with today's point replacing the day's earlier one; `worse_7d` is true when today's status is worse
than the point dated a week back, and None without one. Wave 8's page draws the series and counts the gauges
worse than last week. Alarms carry no trend. A new subject, or one renamed, starts a fresh series.

### `data_quality` — the value quality of each portal's ads

`check_data_quality` (cadence 21,600 s, label "Data quality", unit "cells", owner page `/scrapers`, the
per-portal page, because no page renders value quality yet). It reads the per-m² plausibility view
`measure_plausibility_by_source` (migrations 427 and 534; active ads, grain (portal, category, deal)) through
the pass memo it shares with `parsing`, and `listings.source_url`.

| cell | measures | Watch | Problem |
| --- | --- | --- | --- |
| `floor/<portal>/<cm>-<ct>` | the share of priced, area-bearing, basis-decidable ads that `measure_price_per_m2` NULLs at its per-basis floor (rent under 1,000, sale under 100,000 outside land), over the stock and over ads first seen in 7 d, on the Wilson lower bound | above 5 % (`data_quality_floor_warn`) | above 10 % (`data_quality_floor_fail`) |
| `divergence/<portal>` | per category and deal, land skipped, over the stock and over ads first seen in 7 d: the share of ads carrying both areas whose `area_m2` and `usable_area` differ by more than 10 %, on the lower bound; the worst decides | above 20 % (`data_quality_divergence_warn`) | above 40 % (`data_quality_divergence_fail`) |
| `estate_sum/<portal>` | per category and deal, land skipped: the share of three-area ads whose `estate_area` equals `area_m2` + `usable_area` within 1 % (a figure the page computed, not measured), on the lower bound; the worst decides | above 5 % (`data_quality_estate_sum_warn`) | above 10 % (`data_quality_estate_sum_fail`) |
| `url/<portal>` | active ads with no portal link (`source_url` NULL), an absolute count; fact: those first written in 7 d | above 50 (`data_quality_url_warn`) | above 500 (`data_quality_url_fail`) |
| `measure_gap/<portal>/<cm>-<ct>` | the stock's share of ads with no per-m² measure (no price, or no positive area) where the basis is decidable, on the lower bound, with at least 200 ads (`data_quality_measure_gap_min_n`) | above 95 % (`data_quality_measure_gap_warn`) | never |

The floor, divergence and estate-sum shares need 100 ads each (`data_quality_*_min_n`). **The view.**
`data_quality_by_source` tested fields for `IS NOT NULL` only, so it was blind to both defects the measure
program fixed: a plot area in the floor-area column and a per-m² unit price in `price_czk` are 100 % non-NULL.
The grain is (portal, category, deal): a portal alone pools sale flats with monthly rents, a category alone
pools a broken portal with eight healthy ones. Every share is a ratio over ads that HAVE the inputs, so a cell
with nothing to measure scores nothing and would read like a clean one: the `measure_gap` cells watch the
denominator (sreality's 27,174 active land ads with no headline area read 100 % until W17's plot-area heal on
09-15), and an empty or unreadable view is Not measured, naming its cause. The read measured 3.45 s warm on
10-19.

**Basis floor.** An absolute level, not a jump: the unit-price masquerade (a price per m² stored as the price)
never jumped, it stood for the life of the portals carrying it. Sized on 2026-08-25: ceskereality
komerční/pronájem 20.0 %, realitymix 19.0 %, remax 11.3 %, bazos 6.7 %, against idnes and sreality at 0.5 % in
the same cell, the split between portals with and without a per-area price guard. On 10-19 it reads Problem on
realitymix komerční/pronájem (11.4 % of 4,140, lower bound 10.5 %), a real data condition and the expected
ship-time red, not re-sized to hide it; Watch on realitymix pozemek/pronájem (13.7 % of 131, lower bound 8.9
%), remax komerční/pronájem (9.6 % of 281, 6.7 %) and bazos komerční/pronájem (6.3 % of 2,787, 5.5 %; 7.3 % of
its 455 new ads, 5.2 %).

**Area against usable area.** Only ads with both areas count: bazos fills `usable_area` on 0 of 10,409 houses
and must read silent, not divergent; land is skipped by name, because its `area_m2` IS the plot. Before W1's
heal mmreality houses read 99.7 % against 0.0 % elsewhere, and realitymix flats' milder convention 10.5 %,
which is why Watch is 20 % and not 5 %; on 10-19 nothing crossed (worst 0.1 % of 1,190, remax byt/pronájem). The
estate-sum share is a weak fingerprint of mmreality's old shape (its `estate_area` was parcel plus usable area
while its headline was the usable area): its top two cells read 5.5 % of 55 and 2.6 % of 1,123 over a
background under 1.4 %, so 5 % / 10 % is a forward guard sized on a real ranking; on 10-19 the worst was 1.5 %
of 875 (sreality dům/pronájem, lower bound 0.9 %).

**Ads with no portal link.** Absolute counts, never a share of ~400k active ads: a URL-codebook miss moves a
count within a day. 11 on 10-19, all sreality, none first seen in 7 d.

It replaces `ppm2_basis_floor_share`, `area_vs_usable_divergence`, `outbound_url_coverage`,
`ppm2_measure_coverage`'s stock arm and `floor_convention` (deleted, Q4), and `data_quality_by_source` with the
`capture-data-quality` job: migration 633 unschedules the job (24 failures in 30 d, statement timeouts and "job
startup timeout", each ringing the `jobs` check's cron cell for a job this gauge supersedes), and
`data_quality_snapshots` stays as frozen history until wave 9 (Q5).

### `location_town_coverage` — rule 25's invariant over active ads

`check_location_town_coverage` (cadence 21,600 s, label "Location coverage", unit "cells", owner page
`/location-quality`; on GitHub until this wave).

| cell | measures | Watch | Problem |
| --- | --- | --- | --- |
| `<portal>/town` | among the portal's ACTIVE ads first written more than 24 h ago (`l.is_active and l.first_seen_at < now() - NEW_AD_H`), those with no `listing_location` row or non-foreign with no town (`obec_kod`); a Czech address-grain row written by the current `RESOLVER_VERSION` with a town but no `katastr_kod` (KÚ geometry missing at the registry version), outside the towns whose KÚ the registry load recorded as degenerate, is a Problem condition kept out of the value (a row with no town counts once, as no town); facts `hidden` (the ads the consumer rule hides), the no-row and no-town counts and the town share | never | 1 or more: red until zero (rule 25) |
| `registry` | the RÚIAN registry's current version and any staged load (`registry_versions`) | never | no current version, a load staged for more than 24 h (`location_registry_max_staged_hours`), or a current version older than 40 d (`location_registry_max_age_days`) |

Rule 25's invariant: every served listing has a row and every active Czech listing has a town; foreign is a
determination, never a default. Since this wave it is judged over ACTIVE ads first written more than 24 h ago
(Q2), reversing W15's whole-corpus count, under which the check never read OK in 132 runs: on 10-19
the whole corpus held 13,246 such ads, 13,192 of them inactive ads whose pages are gone. `NEW_AD_H` (24) lives
in `scripts/checks/common.py` and publishing's visible arm imports it too, so an ad leaves publishing's window
as it enters this one, and the resolver's lag inside an ad's first day stays publishing's to judge (on 10-19,
21 active ads under 24 h old had no town, resolved within minutes). The lane still covers every listing (W15):
only the gauge's population moved. The inactive stock is not read each run: on 10-19 at 13:09Z, 13,192 inactive
ads had no town, plus 3 inactive sreality address-grain rows with no KÚ, a dated fact that Location Quality
owns. Live then: 43 active ads (ceskereality 39, realitymix 3, mmreality 1; 23 of them older than 30 d, none
last resolved more than 7 d ago), 0 with no row and 0 address-grain rows with no KÚ; the statement ran 3.1 s
with the served predicate's hidden count (0.62 s without it), against the whole-corpus statement's 6.5 s. The
registry: `ruian:2026-09-30`, loaded 10-01, 19 d old, nothing staged, so a missed November load reads Problem
from 11-10 (41 d; the line is strict). Problem, not Watch: a stalled load is a failure the `jobs` check cannot
see (a superseded run is cancelled, not failed), and a gauge's Watch opens no incident. The per-portal series
changes meaning at this deploy, as it did on 2026-09-14.

Known limits: a gauge already reading Problem cannot announce a resolver regression (the 2026-09-14 sweep that
left 595,816 ads undetermined and the 09-17 loss of 29,545 bazos locations each deepened an open red), so
publishing's location arms are that detector; a text-lane stall shows twice for bazos, whose town comes from
the text lane's reading unless the ad carries a map-link pin: here after 24 h, and as publishing's
`location_not_served` stage.

### `text_extraction` — are new ads' descriptions read promptly?

`check_text_extraction` (cadence 3,600 s, label "Description reading", unit "cells", owner page `/costs`).

| cell | measures | Watch | Problem |
| --- | --- | --- | --- |
| `flow/<portal>` | for each portal in the text lane's scope, the share of ads with a description first written 1-24 h ago that have no reading at the current `SCHEMA_VERSION`, on the Wilson lower bound over at least 30 of them (`text_extraction_flow_min_n`, else Not measured); facts the given-up ads and p95 minutes to the first reading at any version of the current `SCHEMA_VERSION`, beside the lane's 20 min `SLO_MINUTES` | above 5 % (`text_extraction_flow_warn`) | above 25 % (`text_extraction_flow_fail`) |
| `stock` | rows waiting, active and inactive (the archive is re-read once per `SCHEMA_VERSION`), readings in 24 h, and ETA = waiting / readings a day, a lower bound | the ETA above 14 d (`text_extraction_eta_warn_d`), or 0 readings in 24 h while only inactive rows wait | 0 readings in 24 h while active rows wait |

With the lane off (no model set, or no portal in its scope) every cell reads OK with the state `off`. Both
statements are the lane's own (`toolkit/description_extraction`), imported, never copied, and so are its scope
and model reads, all on the check's clock: the stock counts through the lane's ONE eligibility predicate,
while the flow counts the scope's arrivals (active ads with a description) and never goes through that
predicate, so a predicate that selects nothing cannot hide its own arrivals (the 07-22 → 09-21 dead lane
reached 223 of ~50k bazos ads while its workflow read green). An ad the lane gave up on after `GIVE_UP_AFTER`
(5) failed readings stays unread in the flow and is named as a fact. The old key judged the archive's age as a
flow and read FAIL (3,450.8 h on 10-19) on every run while the lane was healthy. p95 counts to the first
reading at any version of the current schema, so a version bump is not a stall: counted to the first reading
at the current version, the arrivals of 10-12 to 10-14, re-read after the 10-14 bump, put the 7-day p95 at
3,521 min. The flow needs 30 arrivals because 2 unread of a small portal's 2 have a lower bound of 34 %, a
Problem (bazos, the lane's one portal today, had 707 on 10-19). Stated, not fixed: both reads date a reading
by its cache row's `created_at`, which a retry that succeeds leaves at its first, failed attempt, so the p95
understates and the readings in 24 h can miss a late success. Live on 10-19: flow 0 of 707 bazos arrivals
unread, p95 49.2 min; stock 55,285 waiting (54 active, 55,231 the archive re-read) at 12,790 readings a day,
an ETA of 4.32 d. The old key's 35 retained days hold 22 runs with no reading in 24 h while rows waited, all
in the provider-credit outage (10-03 to 10-12; the lane read nothing for 248.8 h), and 4 with an ETA past 14 d
around it.

A provider outage is told three times, stated and accepted: by `llm`'s `<provider>/credit` cell, by `jobs`'
`lane/text_extract` cell (a pass the provider stopped is a failed pass) and here, first as the flow's unread
new ads and then as the stock's 0 readings (the replay opens the bazos flow cell at 10-02 15:00Z and the stock
at 10-03 17:00Z); a lane that raises or stops is the `jobs` lane cell's. It replaces `text_extraction_lag` (a
stock read as a flow), whose wedge arm is the `jobs` lane cell and whose abort arm is the `llm` credit cell.

### `migration_drift` and `outbound_url_conformance` — explicit cells (wave 6)

Both stay on GitHub. `migration_drift` emits a cell per migration file in its window (under "Walks, queues,
lanes and migrations", above). `outbound_url_conformance` (`scripts/verify_outbound_urls.py`; domain
`data_quality` since this wave, `scraping` before) emits a cell per portal of `claims_common.SOURCES`: Problem
when one of its strata with at least `outbound_url_conformance_cell_min` samples is more than
`outbound_url_conformance_cell_fail_share` non-conforming (a URL-shape defect fails a whole stratum, where a
delisting not yet caught up with is one scattered 404), OK otherwise, any scattered failure named. An HTTP 403
or 405 is the portal refusing the HEAD (a bot wall, or a server that allows no HEAD) and judges nothing: a
portal whose every link was refused, or of which nothing was drawn, is Not measured. One `sample` cell
carries the overall share, Watch when its Wilson lower bound exceeds 5 %
(`outbound_url_conformance_warn_share`), because a crawler portal's one sample a week can never reach
`cell_min` and its 1-of-1 bound would read 21 %. The probe now resolves a relative `Location` header against
the request URL before reading its segments: two of the six failures of 10-18 were sreality redirects to a
relative `/detail/…` path. That week read 6 of 44 (13.6 %, lower bound 6.4 %); with the fix, and the two 403
walls refused, it is 2 of 42 (lower bound 1.3 %): OK. The module sits outside `scripts/checks/`, so the
rule-21 rail does not cover its sreality codebook rotation; its header says so, and the Portal-seam refactor
is filed (#1860).

**Replay rails (Health dashboard sprint wave 6).** `tests/scripts/test_checks_quality_replay.py` feeds the
four checks' inputs through the real ladder from fixtures under `tests/fixtures/health_quality_*.csv`,
exported read-only from production (counts only: the repo is public), each export's SQL and time in the test's
header. `pipeline_check_results` keeps 35 days (Q5), so the fixtures come from that window (the legacy checks'
runs and stored medians) and from durable stores: churn per portal per day over 28 d from `listing_snapshots`,
the 7-day and stock gaps over 28 d and the weekly stock medians over 6 weeks, as the measure view computes them, the active no-town counts per portal (current only:
`listing_location` keeps no history), and arrivals and readings per day over 28 d from the text lane's
tables. The legacy rows' harness artifacts stored as `warn` (`not_run`, `timed_out`, `skipped`) are filtered
out. Where no durable input exists (fill per day, payload shape per day) the replay is SYNTHETIC and says so.
What the replays open (tests/scripts/test_checks_quality_replay.py, 22 tests; 09-22..10-19, parsing 6-hourly
at 111 ticks and 541 subjects, text hourly): parsing opens TWO incidents in 28 days, both idnes churn — 09-24
18:00Z to 09-26 12:00Z (peak 9.0×) and 10-01 12:00Z to 10-04 06:00Z (peak 11.9×, 8,935 re-snapshots against a
median day of 754), the FX-conversion re-snapshot waves of #683 (price moves of ~0.57 % just past the 0.5 %
re-enqueue tolerance on exchange-rate days): a real churn class, kept as the expected recurring red until it
is fixed at the source; 10-17 peaks at 5.9× (Watch). measures 0 Watch (worst 9.1 pp); medians Not measured
until 09-28 06:00Z then OK (worst 1.10×); fill, vocabulary and payload SYNTHETIC and OK. Synthetic: a collapse
of new ads' measures reads Watch at 24 h and Problem at 2.25 d; a 4× storm Watch only (opens nothing); a 7×
storm Problem for one tick (recovered after 30 h). Heals: the 09-17 W19/W21 heals moved five median cells
(3.2-6.9×), four of which the legacy key's single cell had hidden. text_extraction opens four incidents, all
real lane stops: the W1 cutover (46.1 h without a reading, 09-22) and the credit outage (flow 10-02 15:00Z to
10-12 20:00Z; stock 10-03 17:00Z to 10-13 17:00Z). data_quality's one Problem is the day-one realitymix
komerční/pronájem floor (473 of 4,141 priced ads in the export, LB 10.5 %); the location gauge reads the 43
ads as Problem and recovers on a synthetic zero. The seeded, rolled-back live rail
`tests/test_checks_quality_sql_live.py`, in CI's migrations lane, executes every statement the four checks
send, the payload predicates and the location gauge's 24 h edge with publishing included.
`tests/scripts/test_checks_parsing.py` and `tests/scripts/test_checks_gauges.py` pin every rule above on fake
connections, with the retired checks' fixtures as fold-equivalence inputs: each arm now judged on the Wilson
lower bound is pinned separately, and where a legacy arm used `>=`, the change to the shared strict `>` is
pinned at the boundary.

To check after the merge: `health_checks` holds twelve `realtime-worker` keys and two `github-actions` keys;
within minutes of the worker's deploy Health shows the `parsing`, `data_quality`, `location_town_coverage` and
`text_extraction` rows (their cells render in wave 8); the nine retired rows leave at the canary's first run
from `main`; after migration 633's apply `cron.job` holds no `capture-data-quality`; and `data_quality` reads
Problem on realitymix's komerční/pronájem floor cell, as expected.
