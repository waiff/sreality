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
`llm_errors`, `llm_liveness`, `llm_burn_rate`, `db_saturation`, `worker_liveness`,
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
Three **per-m² plausibility** checks joined in W9 of the measure-unification program
(migration 427, view `measure_plausibility_by_source`, one read per run shared by all three;
they run on the 6-hourly `verify_pipeline.yml` lane only, NOT the hourly acute lane — a 12 s
scan of the active corpus for a slow-moving signal). They exist because
`data_quality_by_source` tests 29 fields for `IS NOT NULL` and nothing else, so it was
structurally blind to BOTH defects that program fixed: a plot area sitting in the floor-area
column and a per-m² unit price sitting in `price_czk` are 100% non-NULL. Grain is
(source, category_main, category_type) — a portal alone pools sale flats with monthly rentals,
a category alone pools a broken portal with eight healthy ones. Each has a stock arm and a
trailing-7-day arm and alarms on the worse: the stock arm indicts a defect that has been
standing for months, the fresh arm catches a regression the week it ships instead of waiting
for the corpus to churn.
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
`ppm2_median_shift` — each cell's own median area and median Kč/m² against ITSELF a week ago,
read from this check's own `pipeline_check_results.details` row from 6-14 days back (no new table).
Deliberately NOT a cross-portal comparison: measured live, portal medians legitimately differ by up
to 19x on mix (idnes' rural land is 8.5x the peer Kč/m²), so a peer arm cannot separate a bug from
a catalogue. It cannot see a defect older than its baseline — that is what the two direct detectors
are for — but it fires at 3x on any new basis flip, and it will fire when the W2 backfill heals
mmreality (6.96x on area, 7.45x on the measure). Thresholds are sized on the noisiest real weekly
move measurable today, 1.90x. No baseline is `ok` for the first week after deploy and `warn` after,
so a check that has been erroring for a fortnight cannot read as green; an empty view read (the
`is_platform_admin()` gate failing for the job's role) or an unreadable one (migration 427 not
applied yet) is `warn` naming the cause, never `ok` and never three red tiles.

The six dedup-specific checks (street/geo debt, eligibility funnel,
merge latency, engine health, merge-precision sample) went with the engine, along with their
`pipeline_check_thresholds` rows.
