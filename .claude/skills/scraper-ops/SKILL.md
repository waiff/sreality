---
name: scraper-ops
description: Use when running, debugging, or extending the scrapers — triggering the per-portal index-walk/detail-drain workflows, adding a new scraper field without breaking data, refreshing per-source HTML fixtures, reading the pipeline logs (INDEX/ENQUEUE/INACTIVE/DRAIN/IMAGES line shapes), the always-on real-time worker (count-probe, live forensics, property-maintenance/dedup/geo lanes), the dedup-aware publication gate, or the pipeline verification/alerting harness. Also covers condition-scoring (currently unscheduled) and image-download workflow cadence. Triggers on: index_walk, detail_drain, gh workflow run, mark_inactive, scrape_runs, fixtures, RUN done, a new listings column, onboarding a portal, reading a scrape log, realtime_worker, publication gate, verify_pipeline, llm_burn_rate.
---

# Scraper operations

Operating the scrapers: adding a field, refreshing fixtures, triggering the workflows, and
reading the logs. The per-portal ingest architecture (what each portal is, parser
strategy, completeness posture) lives in `docs/architecture.md` § Data sources. Default
test / log helpers: `scripts/test-summary.sh` and `scripts/logs.sh <run-id> [pattern]`.

## Adding a new scraper field without breaking existing data

1. Add the column with a new numbered migration (`alter table listings add column ...`). Never
   touch `001_initial.sql`.
2. Update the parser in `scraper/parser.py` to extract the field.
3. Update the upsert in `scraper/db.py` to include the new column.
4. Backfill old rows: either leave them NULL (acceptable if the column is nullable) or run a
   one-off SQL update from the `raw_json` column, which already contains the full source
   record.

## Refreshing per-source HTML fixtures

The LLM-driven parsers (`scraper/source_parsers/`) are tested against saved listing HTML in
`tests/fixtures/source_html/`. Real listings get taken down or change layout, so every few
months the fixtures need a refresh. Don't fetch live in tests — that would burn LLM credit and
break offline runs.

Refresh (CLI, fastest): `gh workflow run fetch-fixtures.yml --ref <branch>` (add `-f`
inputs to override URLs). Or via the browser: GitHub repo → **Actions** → **Fetch + anonymize
source HTML fixtures** → **Run workflow** → pick branch / optional URLs → **Run workflow**. It
fetches each URL, runs the anonymization in `scripts/fetch_and_anonymize_fixtures.py`, and
commits the resulting `*_sample.html` files back to the same branch. The skipif tests in
`tests/scraper/test_source_parsers/test_real_fixtures.py` light up automatically once the files
exist.

Anonymization scope: phones → `+420 XXX XXX XXX`, emails → `agent@example.cz`, street numbers
(`123/45`) → `XXX/YY`. Listing prices and the surrounding HTML structure are preserved — public
data the parsers need. Agent names are too varied to scrub by regex; if a fixture leaks one,
hand-edit the file.

## How to manually trigger the scrapers

The sreality pipeline is **split by cadence (Phase 2)**: `index_walk.yml` ("Scraping: Sreality
index walk", cron `*/15`) feeds `detail_drain.yml` ("Scraping: Sreality detail drain", cron
`*/15`). `scrape.yml` ("Scraping: Sreality combined walk") is the **dispatch-only fallback** —
the proven combined index+detail `_run_full`, kept for instant revert (re-add its `schedule:`
cron, disable the two new ones) and ad-hoc full walks. The bazos crawl is **cadence-split**
like sreality (bazos walks 14 nationwide scopes, ~1500 index pages — a combined run starves the
drain): `bazos_index_walk.yml` ("Scraping: Bazos index walk", cron `0 */6`, full walk +
mark_inactive + enqueue) feeds `bazos_detail_drain.yml` ("Scraping: Bazos detail drain", cron
`45 * * * *`, bounded `--max-seconds`); a third job, `bazos_description_enrichment.yml`, backfills
free-text description enrichment every 3h (PR #733) — bazos's ad text needs a separate enrichment
pass the other portals' structured pages don't. Its tool (`toolkit/bazos_enrichment.py`) was
slimmed to the 8 fields it actually consumes with the LLM call's `tool_choice` FORCED (PR #768) —
the prior full-schema tool let ~27% of calls return prose instead of a tool call, which wrote no
cache row and re-billed forever; a `no_extraction` result now also caches, and the driving script
aborts (exit 1, red workflow) after 5 consecutive provider errors instead of finishing green on a
dead API key. The bezrealitky scrape is
`scrape_bezrealitky.yml` ("Scraping: Bezrealitky scraper (pilot)", every 6h + dispatch; runs
both index walk + detail drain in one job via `bezrealitky_main`). The maxima scrape is
`scrape_maxima.yml` ("Scraping: Maxima Reality scraper (pilot)", every 6h + dispatch; the
~220-listing catalogue fits both phases in one job via `maxima_main`). The mmreality scrape is
`scrape_mmreality.yml` ("Scraping: M&M Reality scraper (pilot)", cron `50 */6` + dispatch —
every request via the residential `SCRAPER_PROXY_URL` (Cloudflare 403-blocks datacenter IPs);
runs both phases in one job via `mmreality_main`, bounded by `--max-pages`/`--max-detail`). The remax
scrape is `scrape_remax.yml` ("Scraping: RE/MAX scraper (pilot)", every 6h + dispatch; runs both
phases in one job via `remax_main`, bounded by `--max-detail` + a `--max-seconds` budget so the
~7,900-listing backlog drains over several ticks). The idnes scrape is
**cadence-split** like sreality (iDNES is large — ~2400 index pages, ~60k listings — so a
combined run's full index starves the drain): `idnes_index_walk.yml` ("Scraping: iDNES Reality
index walk", `idnes_main --index-only`, cron `15 */6`, full complete-walk + mark_inactive +
enqueue) feeds `idnes_detail_drain.yml` ("Scraping: iDNES Reality detail drain", `--drain-only`,
hourly cron `30 * * * *`, bounded by a `--max-seconds` wall-clock budget; with
`SCRAPE_CHAIN_TOKEN` it re-dispatches itself while the queue has work, for near-continuous
backlog drains). There is no combined bazos/idnes fallback workflow anymore — sreality's
`scrape.yml` is the only retained combined fallback (its `_run_full` is the instant revert for
the split); for the other portals an ad-hoc combined run is `python -m scraper.<portal>_main`
locally. The dedup/properties track adds
`property_maintenance.yml` (**dirty-set incremental, cron `*/5`** — attaches new stragglers as
singletons + recomputes only changed properties; rule #20),
`recompute_property_stats.yml` (the **daily full-sweep reconcile** at 04:15 — recomputes every
property + clears the dirty queue), `dedup_engine.yml` (street+disposition dedup engine +
auto-merge; rule #15 — THREE scheduled modes, ONE `resolve_pair` decision tree (the brain),
three work-lists: a **FULL SCAN** every 6h that DISCOVERS new dups across the market; a
**CANDIDATE DRAIN** every 2h (`--candidates`) that re-decides ONLY the properties in
still-proposed `/dedup` candidates so the queue **self-clears in O(queue)** regardless of the
full scan's deadline frontier — a re-decide backoff (`engine_decision`/
`last_engine_decision_at`, migration 272, PR #701) skips a candidate whose last decision is
recent and not stale/superseded by fresh CLIP evidence, fixing a prior infinite-treadmill bug
where the same undecided pairs re-ran every cycle for no new evidence; and a **DIRTY DRAIN**
hourly at :45 (`--dirty`, Wave 4c) that
re-decides ONLY the street groups touching a just-dedup-ready property
(`dedup_dirty_properties`, migration 242) so a **new cross-portal listing merges within ~minutes**
instead of waiting hours (the watchdog-grain goal). **The enqueue is a real-time CHANGE signal,
NOT enrichment progress** (`scraper.db.mark_properties_dedup_dirty_for_images`, called by the CLIP
tag job): a property lands here only when a just-tagged listing is (a) now FULLY tagged, (b) its
property has a street+disposition listing (**eligibility** — the street-only dirty drain can never
merge one without, geo is skipped on `--dirty`), AND (c) the tagged listing is **recent**
(`first_seen_at` within `_DEDUP_DIRTY_RECENCY_DAYS`, a genuinely NEW arrival). Without (b)+(c) the
market-wide CLIP backfill / a new portal's back-catalogue floods the queue with un-mergeable /
already-full-scanned rows — the flood that stalled it twice (201k, 78.5% ineligible); anything the
gates drop is still deduped by the 6h full scan. The dirty drain's eligible LOAD is **SCOPED to
the claimed properties' street groups** (`restrict_street_groups`) — not a full-market scan — via the
**stored `listings.street_name_key`** (migration 256): `_claimed_street_groups` reads the dirty
properties' `street_id` + `(coalesce(obec_id,-1), street_name_key)`, and the load (`_ELIGIBLE_SCOPED_SQL`)
UNION-joins those claimed keys against `listings` as **unnest-JOINs the planner index-seeks PER claimed
key** (the street_id arm via migration 127's index, the name-key arm via 256's
`(coalesce(obec_id,-1), street_name_key)` partial expression index) — NOT an `OR` (validated to collapse
to one full-eligible bitmap scan). Street groups are obec-bounded (the name key is obec-scoped; a
`street_id` is one physical street), so the scoped load is **complete** — it carries each dirty
property's existing peers, so a dirty property still
re-decides against its whole group, while staying **O(dirty)** in BOTH load and pair-work. The
`only_groups_with_property_ids` filter still gates the RESOLVE to dirty-containing groups, so the
scoped load is a pure perf optimization layered under that correctness gate (no fragile SQL
street-key replay); race-free claim/clear like `dirty_properties` (rule #20). `street_name_key` is
THE single source `scraper.street.street_name_key` (also what the engine groups on live via
`street_group_keys`), stamped at every `listings.street` write path via that ONE function
(`scraper.db._set_street_name_key` at ingest + ALL the bulk street backfills: `backfill_portal_streets`
/ `backfill_bazos_street_locality` / `backfill_address_point_streets` — the weekly coord→street
resolver), out of the content hash, backfilled by `scripts.backfill_street_name_key`. Four guards hold stored == function:
golden-case tests pin the normalization, a write-path test asserts every backfill's UPDATE stamps the
column, the migration-264 presence CHECK (`listings_street_key_presence`) fails a keyless street write
LOUDLY at write time, and the weekly sampled-parity job (`street_key_parity.yml` →
`scripts/check_street_key_parity.py`) alerts via the workflow-failure monitor on any stored key
drifting from the function (a normalizer edit requires the `backfill_street_name_key.yml all=true`
re-key; the parity failure is the alarm for forgetting it). The ultimate backstop remains that the 6h
full scan recomputes the key LIVE (never reads the column), so any drift is latency-only, never a
wrong or lost merge. The claim is **NEWEST-FIRST + bounded** (`--max-dirty`,
3000 on the cron) and the queue is **TTL-bounded** (`_prune_stale_dedup_dirty`, 24h): the real-time
lane is a LATENCY optimization backstopped by the full scan, so it serves the FRESHEST dedup-ready
listing first (the "merge in minutes" SLO holds even under a transient backlog) and evicts rows
older than the TTL **that a COMPLETED full-scan cycle has provably covered** (`marked_at <
dedup_scan_state.last_cycle_started_at`, migration 261 — no completed cycle yet → evict NOTHING;
the original unconditional TTL claimed "the full scan has already covered them", which was FALSE
while the full scan head-restarted at ~9% coverage — eviction was silent work loss) — so an
un-drainable backlog can never pin the head, and eviction never discards uncovered work. **The 6h
full scan itself is CURSOR-ROTATED** (migration 261, `dedup_scan_state`): groups iterate in sorted
key order resuming after `cursor_key`, each run advances the frontier, reaching the end of the
list completes the CYCLE (stamps `last_cycle_*`, resets the cursor) — so the WHOLE market is
covered every ~2–3 days instead of the head ~9% being re-scanned forever (the tail structurally
never reached). The dirty/candidate drains have their own work-lists and stay cursor-free. (Replaced
a **FIFO** claim that let an unfinished head be re-claimed forever — FIFO is the wrong order for a
latency SLO; the full scan is the tail's backstop.) The **dirty cron runs
`--floor-plan-budget 0`**: the real-time lane pays NO inline floor-plan vision (a cold call
downloads plan images from R2 + Sonnet ~15s each; a batch of them blew the wall-clock budget →
truncate → never clear), consuming only warm verdicts and DEFERRING the rest to the 6h full scan /
the batch warmer — keeping the hot path fast so it always finishes-and-clears. **The dirty cron
runs in its OWN concurrency group** (`group: dedup-engine${{ ... '-dirty' }}`) so the slow batch
runs (full scan / candidate / geo, all in `dedup-engine`) can never starve or cancel it; safe to
run concurrently because `merge_properties` row-locks both properties `FOR UPDATE` + gates on
`status='active'` (the same lock safety that lets dedup run concurrently with property-maintenance),
so concurrent merges serialize per-property and a redundant re-decide is an `already_merged` no-op.
**The claim clears INCREMENTALLY, per completed street group** (`run_engine`'s
`resolved_property_ids` out-collector): a claimed property clears once EVERY group containing it
(a listing dual-keys into 'id:' + 'name:' groups) was fully scanned — so a deadline/pair-cap-
truncated run still clears the slice it finished and only the unprocessed remainder re-drains
(replaced an all-or-nothing clear that pinned `dirty_cleared` at 0 whenever a run truncated —
per-group clear makes progress monotonic regardless of budget).
**The per-pair cost floor itself is fixed by the run-scoped `_ProbeCache` + group-batched pHash**:
CLIP-completeness / floor-plan ids / site-plan presence are per-LISTING facts memoized for the run
(O(n) probes per group, not O(n²)), and `_phash_group_counts` computes a whole street group's pair
counts in ONE round trip per exclusion-profile (`resolve_pair(group_sids=…)`; per-pair fallback
without it). **Scoped runs also consult PRIOR verdict-backed dismissals** (`dismissed_prior` +
`_record_auto_dismissed` markers): a pair the engine already confidently dismissed is skipped
unless either side gained photo evidence since (`images.clip_tagged_at` > `reviewed_at`), and
`_write_pair_audit` refuses records identical to one logged within 7 days — the audit table logs
decisions, not run cadence (pre-fix: ~5.8x duplicate dismissal rows). Full scans never consult
(cursor-rotated: one re-decision per cycle is the designed refresh).
**Each dirty run
records `dedup_engine_runs.dirty_queue_depth` (backlog at run start) + `dirty_claimed` (its slice)**
(migration 255) **plus `dirty_cleared` + `dirty_truncated`** (migration 258, NULL on other run
modes) — `cleared==0` while `dirty_queue_depth` stays high across runs is the silent-livelock guard
the FIFO stall lacked. **EVERY run row additionally records `run_kind` ('full' | 'candidates' |
'dirty') + the run-level `truncated` + a real `started_at`** (migration 262): a chronically
deadline-cut FULL SCAN is the signal that matters most — TTL eviction hands work to the full scan,
so eviction is only safe while scans actually cover the market (the 2026-07 audit found the
pre-cursor scans silently truncating at ~9% of it; the migration-261 cursor + cycle-gated TTL fix
the coverage, and `truncated` on `run_kind='full'` rows is the alarm if it regresses). "Latest
run" readers order by `id`/`ended_at` (insert order) — NOT `started_at`, which would sort a long
scan's row below dirty runs that started after it. The `/dedup`
dashboard shows a "Dirty queue" stat + a stall banner, and the Health page raises an amber/red
banner. The shared, unit-tested `assessDirtyQueue` (`frontend/src/lib/dedupQueueHealth.ts`) is the
single source of that status for both surfaces, and it keys on **`dirty_cleared`, not depth**.
**Market gauges are decoupled from run activity** (migration 265): `eligible`/`flagged_*` are NULL on
scoped runs (the ~9s full-table aggregate only runs on full scans) — dashboards read gauges from the
latest `run_kind='full'` row and activity from the latest row of any lane; the geo pass writes its own
`run_kind='geo'` rows (its `eligible` is the geo lane's count, excluded from street gauges). Health keys:
"draining" means cleared>0 in the recent window (the 24h TTL prune shrinks depth whether or not
the drain works, so a falling depth alone proves nothing), and a truncated streak with zero
cleared is a red LIVELOCK regardless of depth (pre-258 rows fall back to the depth trend).
All three drains compose with `--free` + the floor-plan budget), `dedup_batches.yml` ("Dedup engine (vision batch warm-up)", submit every
6h + ingest hourly — pre-warms the engine's vision caches via the Anthropic Batches API at 50%
off so the daily engine run merges over warm cache for free; rule #15; the warmer submits by
`--lane street|geo|candidates`, has a wall-clock submit budget, and retries transient provider
errors within its ~75-min job window), and
`compute_image_phash.yml` (hourly pHash backfill, active-listing images first).

**CLIP tagging (`toolkit/image_tagging.py`) now persists an embedding for every TAGGED
image, not just active-listing ones** (PR #748) — closed a ~19% coverage gap that was
forcing unnecessary Sonnet vision fallback in the dedup engine; a spare-capacity repair
phase (PR #751) backfills the pre-existing tagged-but-vectorless backlog. Byt (apartment)
candidate generation gained a **geo rung** (migration 296, PR #764) — extends the
`geo_cell_key` blocking key (migration 276, see the `database` skill) to the `byt` family,
so street-less apartments (~19.3k) are now reachable via a geo-cell + disposition candidate
lane, generation-only (doesn't change the auto-merge gate, rule #15).

A unified `CoordResolver` (`scraper/location.py`, migration 288, PR #749) now backs
idnes/realitymix/maxima/remax/mmreality/ceskereality — four of those had no geocode path at
all before. See the `database` skill's "Location/geocode lifecycle" and "Street lifecycle"
entries for the caching/provenance detail; this is the portal-wiring side of the same change.

Monitor/alerting workflows watch the rest: `monitor_workflow_failures.yml` ("Monitoring: workflow
failures", cron `*/30` — records failed / timed-out / startup-failed runs into `workflow_failures`
so the Health page can list them; GitHub only emails about failed *scheduled* runs; it now
distinguishes a never-started supersession cancel from a genuine failure so cancelled-by-newer-run
doesn't inflate the failure count, and captures the run's cursor + whether it was killed by
timeout, PR #767/#738) and `llm_health.yml` ("Monitoring: LLM pipeline liveness", hourly — goes red
on ANY of: recorded `llm_calls` FAILURE rows in the window (`error IS NOT NULL`, migration 259 — a
credit-balance error alarms immediately, `>= --min-failures` generic failures otherwise), OR
`llm_calls` idle for hours while condition-scoring work is pending, OR the condition batch pipeline
stale despite fresh unrelated traffic. The failure probe is INDEPENDENT of pending work — it closes
the blind spot where a credit-exhausted account stayed green for ~8h because condition scoring
happened to be quiet. `LLMClient` records the failure row on every provider exception; the check
needs no Anthropic key of its own). Two more alerting layers were added on top: `llm_burn_rate`
(PR #739, warn threshold operator-tuned via `pipeline_check_thresholds`, currently 130 — PR #766)
watches daily LLM spend for the recurring credit-depletion pattern (see the
`llm-credit-outage-health-gap` memory if you need the incident history) — its rows land in the
same `pipeline_check_results` table the verification harness below writes to; and a broader
edge-triggered-alerts / blind-spot-detector rework (PR #732, WS4 tracks A/B/C) consolidates related
LLM alerts instead of firing one per symptom. Run any directly:
- CLI: `gh workflow run index_walk.yml --ref <branch>` (or `detail_drain.yml`, `-f` for flags).
  Watch with `gh run list --workflow=index_walk.yml` then `gh run watch`.
- Browser: GitHub repo → **Actions** → the workflow → **Run workflow** → pick branch + optional
  flags → **Run workflow**. (All sreality scraping workflows are prefixed `Scraping:`.)

**Each scrape workflow self-declares its portal with a `# portal: <source>` tag.** A one-line
comment near the top of a portal's index/drain/combined workflow (`<source>` = the
`portals.source` key, e.g. `# portal: idnes`) is parsed by `scripts/generate_workflow_docs.py`
into `WorkflowDoc.portal`, which is what the Health dashboard's per-portal "Pipeline schedule"
panel groups on — so a new portal's cron lines surface there automatically, with **no hardcoded
frontend map to keep in sync**. Tag only the actual ingest workflows (index walk / detail drain /
combined fallback); shared, source-agnostic jobs (`images.yml`, `condition_scores.yml`,
`recompute_property_stats.yml`, `dedup_engine.yml`, …) stay **untagged** (`portal: null`) and
appear in the full Settings → Workflows list rather than any single portal's schedule. As with any
workflow edit, regenerate `frontend/src/lib/workflowDocs.generated.ts` in the same commit
(`python scripts/generate_workflow_docs.py`; CI's `--check` guards drift).

**The split (architectural rule #19).** The cheap "which ads still exist" check is decoupled
from the slow "download each ad" write:
- **`index_walk.yml` (fast, frequent).** Walks the **entire** index of every category pair (no
  `--limit`), `touch_listings` bumps `last_seen_at` on still-listed ids, `mark_inactive` flips
  delisted ones (under the completeness guard), and new + price-changed ids are **enqueued** into
  `listing_detail_queue` with a priority (failure-retry > price-changed > new). No detail fetch,
  so delistings surface within minutes. Records `run_type='index'`, `index_pages>0` (what Health
  liveness keys off). Uses the **transaction pooler** (`connect()`) — bulk set-based statements,
  no per-listing loop.
- **`detail_drain.yml` (slow, async, bounded).** Claims a bounded slice of the queue
  (`--max-detail-refetches`, the workflow passes 12000), fetches details on a rate-limited pool, and writes
  them **batched** via `db.write_detail_batch` (set-based `jsonb_to_recordset`, one transaction
  per ~100 listings, ~0.1–0.2 s/listing). Uses the **session pooler** (`connect_session()`) for
  prepared statements. New listings land with `property_id` NULL and become **singletons** via
  `recompute_property_stats`'s straggler-attach (the hot write path carries no matching at all;
  grouping is the dedup engine's job, rule #15). A gone fetch flips that listing inactive +
  dequeues it; a transient error bumps
  the queue row's `attempts` (given up after 5) and stays queued. Records `run_type='detail'`,
  `index_pages=0`. The queue persists across runs, so a bounded run never loses work; a
  SIGKILLed claim is recovered by the next run's `reclaim_stale_claims`.

`mark_inactive` runs every index walk. Two safety rails make the flip safe (architectural rule
#3): (1) each per-category flip is gated on **walk completeness** — `_walk_complete` compares the
collected count against the API's `result_size` and skips the flip (logging `INACTIVE skipped`)
when the walk looks truncated; (2) a gone detail fetch (HTTP 404/410 or sreality's "tato stránka
neexistuje" body, `ListingGoneError`) flips that single listing immediately. The drain's
failure-priority replaces the old per-walk priority retry: a failed fetch keeps its queue row at
elevated priority.

**Condition scoring is currently UNSCHEDULED — an intentional pause, not a bug** (PR #730,
confirmed operator-intentional 2026-07-09; ~56k byt rows unscored is accepted). Don't
re-enable or backfill without explicit direction. The machinery is otherwise unchanged and
**batch-driven** when it does run: `condition_score_batches.yml` is the driver (Anthropic
Message Batches API, 50% cost) — `submit` (previously every 3h) puts the next slice of
unscored listings in a batch, `ingest` hourly (`35 * * * *`, still live for any in-flight
batch) polls + persists; one workflow, mode chosen by `github.event.schedule`. The
synchronous `condition_scores.yml` is a **dispatch-only fallback** — don't schedule both,
they select the same pending listings and the sync scorer doesn't skip in-flight batch rows.
The scoring model is `app_settings.llm_condition_model` (Haiku today), so batch+Haiku ≈ 25%
of the original Sonnet-sync cost. Both scrape workflows still pass `--no-condition-scoring`.
Scoring is **kraj-scoped and reuse-first** (migration 174):
the selector targets only listings whose geo-derived `region_id` is in
`app_settings.condition_scoring_enabled_region_ids` (operator-edited via the Settings page
"Hodnocení stavu — kraje" toggles; empty = paused; `region_id` NULL = parked), and
`propagate_condition_levels` copies a property's genuine score to its cross-portal siblings
(`listings.condition_levels_propagated_from` records provenance) before every submit/backfill,
so a duplicate never re-bills the LLM. `check_llm_health` mirrors the same scope.

**Images** stay decoupled across three workflows (both halves of the scrape split pass
`--no-image-downloads`; the drain's write phase only records image-URL rows — bytes land in R2
via these jobs):
- `images.yml` ("Scraping: image backlog drain (sharded)", 2-hourly) — THE deep backlog drain
  across ALL portals, horizontally **sharded into 4 parallel jobs** (each owns the
  `image_id mod 4 == shard` slice via `--image-shard k/4`), each with its own per-shard cap,
  suspicious-stop circuit-breaker, and runner IP.
- `images_fresh.yml` ("Scraping: fresh-listing image fast lane", cron `*/15` + self-chaining via
  `SCRAPE_CHAIN_TOKEN` while work remains) — drains the newest ACTIVE listings' photos first so
  a freshly-scraped card renders an image within minutes instead of waiting for the 2-hourly
  drain.
- `refresh_stale_images.yml` ("Jobs: refresh stale image URLs", every 6h) — re-enqueues active
  listings whose un-downloaded image URLs have rotated/gone stale into `listing_detail_queue`
  (low priority) so the detail drain repoints the URLs and the backfill can then store the
  bytes.

**Cadence:** `*/15` for each half, deliberately — frequent index walks surface delistings fast,
while the bounded drain keeps a steady, polite fetch volume. GitHub throttles scheduled
workflows, so effective cadence is slower; Health liveness/freshness thresholds are **per-portal
cadence-aware** (`portals.scrape_cadence_minutes`, migration 114): `scraper_health_checks` scales
liveness warn at 1.5× / fail at 3× the portal's cadence, and freshness warn at 1× / fail at 3×.
sreality's cadence (60 min, ~hourly real cadence) reproduces the original 90/180 + 60/180; the 6h
pilots (bazos/bezrealitky/idnes, cadence 360) get proportional thresholds so they aren't falsely
red between runs. Concurrency: each workflow has its own group with `cancel-in-progress: false` — a long
run is never killed mid-batch; the next tick queues behind it. Per-category mark_inactive commits
immediately after each category's walk, so even a timed-out index walk leaves a consistent
partial result.

The detail-drain writes `scrape_runs` rows too (`run_type='detail'`), but only the **index
walk** sets `index_pages>0` — so "last scrape", the liveness check, and reconciliation track
the index walk specifically, while the 24h new/updated/error counters sum across the drain's
`index_pages=0` rows too (see `scraper_health_checks()`, migration 105). The image backfill
(`--images-only`) deliberately writes NO `scrape_runs` row — recording it once polluted
liveness/reconciliation with `index_pages=0` noise.

## The real-time worker (`scraper/realtime_worker.py`)

A dark-by-default, always-on Railway service (a 2nd process from the SAME image, gated by
`REALTIME_WORKER_ENABLED`) that replaces cron quantization for the latency-critical parts of
the pipeline — the GH Actions crons above are still the throughput/completeness backbone; the
worker is the latency layer on top. Design + shipped waves: `docs/design/realtime-scrapers.md`.
Lanes shipped so far:
- **Per-source drain-disable knob** (`realtime_drain_disabled_sources`, PR #694) — the bounded
  detail drain skips sources listed here, letting a portal be pulled from the real-time lane
  without touching its GH Actions cadence.
- **Bounded live forensics** (`--compare-budget`, PR #695) — the worker can run a small,
  wall-clock-bounded slice of the forensic visual-compare step inline instead of waiting for
  the batch warmer; the warmer itself was NOT retired despite an earlier commit's title —
  PRs #725/#728/#735/#741/#757/#762 continued actively building it well after.
- **sreality count-probe lane** (migration 270, PR #696) — a lightweight per-`(category_main,
  category_type)` count check that detects a market-wide count swing faster than a full index
  walk would, feeding the completeness/delisting rails.
- **Tightened delisting rails for sreality** (PR #697) — sreality's completeness gate moved
  1.0→0.995 and its unseen-staleness window to 3h (vs 12h on the 6h-cadence portals), matching
  rule #3's two-rail design to sreality's faster real cadence.
- **Property-maintenance lane**, every 2 min (PR #716) — runs `run_incremental_pass` against
  `dirty_properties` (rule #20) far more often than the 5-min GH Actions cron. Its first cut
  serialized against the GH cron + daily sweep with a SESSION advisory lock, which is unsound
  over the transaction pooler and stranded within minutes of deploy (PR #717 fixed it with the
  lease-row CAS pattern — see the `database` skill's connection-modes section; don't reintroduce
  a session advisory lock on any pooled connection).
- **Real-time dedup lane** (PR #702) — lets a cross-portal merge complete within minutes of both
  sides landing, rather than waiting for the hourly dirty-drain cron.
- **Geo scan-state lane** (PR #715) — a geo-dedup analogue of the street dirty path, plus a geo
  cron budget and an imageless-candidate eval sweep.
- **Unified real-time geo+street dirty path** (PR #713) — the street dirty-drain and the geo
  scan-state lane were merged into one queue / one decision brain rather than two parallel dirty
  paths.

## Publication gate and pipeline verification (migrations 273–274)

**A new property is hidden from Browse, the map, Stats, the agent, and Watchdog until it has
been dedup-evaluated** — `properties.published_at` (migration 273), gated by
`dedup_publication_gate_enabled` (seeded `false`; flip only with the operator's sign-off, since
it changes what's visible market-wide). `publication_gate_enabled()` is `SECURITY DEFINER`; if
you're referencing it (or any future gate function) from a view's `WHERE`, wrap it in a scalar
subquery, not a bare call — see the `database` skill's InitPlan gotcha, migration 275, which
fixed exactly this on `properties_public` and broke Browse market-wide until it did.

**Pipeline verification harness** (`scripts/verify_pipeline.py`, migration 274, PR #703) — a
scheduled job that writes one `pipeline_check_results` row per health metric (`ok`/`warn`/`fail`)
and is the origin of the notification system's third producer, `system_health` (see
`docs/architecture.md` rule #16) — a `fail` status rings the same in-app bell the SPA nav badge
polls. A `SECURITY DEFINER` dead-man-switch pg_cron function fires if the hourly job itself stops
running (the migration-136 exception-guarded pg_cron pattern). This exists because the pipeline
stalled silently for two days in 2026-07 (Anthropic credit exhaustion, 38k+ failed LLM calls) and
the only alarm was a failing GH Actions cron the operator happened to miss.

## Reading the logs

The scheduled pipeline logs in two halves; the shared `portal_runner` emits the same line
shapes for every portal (with its own `source=`), so this reads the same for bazos/idnes/etc.

**Index walk** (`index_walk.yml` and the per-portal walks):
- `CATEGORY start cm=... ct=...` per category pair
- `INDEX offset=N estates=M total=K` per search page (offset/limit paging; sreality)
- `SPLIT cm=... ct=... result_size=N > T: walking D districts` when a sreality category exceeds
  the deep-pagination window and is walked per-district
- `PLAN unchanged=N refetch=M` per category walk (per district when split) after diffing index
  prices against the DB; `PLAN priority_retry=N` if any listings have prior failure rows
  (sreality — the other portals go straight to ENQUEUE)
- `ENQUEUE enqueued=N new=... changed=... priority=...` per category — the ids handed to the
  drain via `listing_detail_queue`
- `INACTIVE cm=... ct=... marked=N collected=M result_size=K` per category after a
  completeness-checked mark_inactive
- `INACTIVE skipped cm=... ct=...` per category whose walk looked truncated (flip suppressed)
- `RECONCILE cm=... ct=... sreality=... collected=... active=...` — portal-reported total vs
  collected vs our active DB count (drift feeds the Health page)
- `INDEX total=N pages=M enqueued=K` once at end of the walk
- `RUN done pages=N enqueued=M inactive=K errors=E`

**Detail drain** (`detail_drain.yml` and the per-portal drains):
- `DRAIN reclaimed stale claims=N` when a prior SIGKILLed run left claims behind
- `DRAIN starting source=... max_claims=... workers=W batch=B budget=Ss` once
- `DETAIL id=... gone (is_active=false)` / `DETAIL id=... error: ...` per non-ok listing
- `DRAIN flush size=N new=... updated=... unchanged=... images=...` per batched write
  (one transaction per ~100 listings)
- `DRAIN progress claimed=N new=... updated=... unchanged=... gone=... errors=... buffered=...`
  per claim chunk
- `DRAIN time budget Ss reached at claimed=N; finalizing cleanly` when `--max-seconds` stops
  the run before the job timeout
- `RATE penalize status=429|403 url=...` when the portal throttles us and the limiter widens its
  interval (auto-recovers on subsequent healthy fetches)
- `RUN done pages=0 new=... updated=... unchanged=... gone=... errors=... claimed=...`

**Image workflows** (`images.yml` / `images_fresh.yml`, `--images-only`):
- `IMAGES start cap=... workers=... active_only=... shard=... sources=...` once
- `IMAGES progress=N downloaded=... errors=... taken_down=... source_unavailable=...` every 50
- `IMAGE listing_taken_down sid=... marked=N` / `IMAGE source_unavailable id=...` per classified
  failure (an inline freshness check flips a taken-down listing inactive + bulk-marks its images)
- `IMAGES STOP suspicious ...` when the transient-failure circuit-breaker trips (exits 75; the
  next cron tick retries)
- `IMAGES done downloaded=... errors=... taken_down=... source_unavailable=... attempted=...`

The dispatch-only `scrape.yml` fallback additionally emits the legacy coupled-path lines
(`PLAN cap=N deferred=M`, `DETAIL starting refetch=N workers=W`, `DETAIL progress=N/M ...`,
`DETAIL id=... new|updated|unchanged`, `IMAGE id=... inserted=N`).

A run ending with `errors > 0` is not necessarily a failure (single-listing fetch errors are
tolerated). A run that did not emit a `RUN done` line is a real failure — check the GitHub
Actions log for a stack trace.

