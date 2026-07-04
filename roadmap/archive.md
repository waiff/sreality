> Completed work — the roadmap archive. Part of [ROADMAP.md](../ROADMAP.md).

## Done

### 2026-07: Real-time program Wave A+B — scraper correctness + SLO instrumentation

The fix-first wave of the real-time scraper program (design:
`docs/design/realtime-scrapers.md`), from a 19-agent investigation of main + prod.
Correctness bugs a faster pipeline would have amplified, plus the measurement substrate:
- **Cross-slice delisting flap killed** (#678): realitymix + ceskereality index slices
  collapsing onto one (cm, ct) (domy+chaty → dum) swept each other's rows inactive every
  6h walk (~11.5k false flips/walk; ~9k rows falsely inactive most of each day). Sweeps
  now buffer per-(cm, ct) and fire once with the union on the group's last complete
  slice; both portals gained the explicit 24h min_unseen rail their comments falsely
  claimed existed. Failure direction: over-retention, never false delisting.
- **remax rent 0% coverage fixed** (#682): queue starvation, not parsing — priceless
  index cards ("Dohodou") re-enqueued as "changed" at high priority every walk, and
  enqueue's ON CONFLICT re-stamped enqueued_at, starving the rent tail forever. None
  price = no change signal now; re-enqueue keeps the original enqueued_at (honest FIFO +
  honest queue-age metrics); drain budget 1500→2100 s.
- **idnes FX price-jitter churn killed** (#683): foreign (EUR/USD) inventory re-displays
  at a daily FX rate (94.8% of the ≥4-snapshots/week cohort), producing 47% of ALL
  snapshot rows + ~20k phantom "price changed" refetches per walk. New shared
  `price_changed()` predicate + per-portal `price_change_min_pct` limit (baked 0
  everywhere = bit-for-bit unchanged; idnes 0.005 — calibrated: 99.5% of its moves are
  <0.5%, genuine cuts ≥1%). Mortgage-CTA text stripped from stored price fields.
- **sreality snapshot-churn hash flaps stripped** (#680): quantified on a 300-pair prod
  sample — 57.3% of consecutive snapshot pairs were portal-side noise (firmy.cz
  review_count 46%, wholesale sdn.cz URL re-signs, `edited` bumps, advert_images `kind`
  flaps). Hashing now normalizes advert_images to (id, alt, order), reduces sdn_*
  attachment URLs to presence, strips premise counters/logo; image add/remove/reorder
  still snapshot.
- **Enqueue→detail-write latency ledger** (#679, migration 265): completions INSERT
  into `detail_queue_completions` in the same transaction as the queue DELETE (written/
  gone/given_up), 7-day self-pruning, + `detail_latency_recent` p50/p90 view — the
  first-hop SLI that was unmeasurable (queue rows died on completion).
- **published_at** (#685, migration 266): portal publish dates promoted to a column
  (bazos bump-date, ceskereality "Datum vložení", bezrealitky timeActivated when the
  API ever exposes it, sreality `edited` fallback) — timestamptz, hash-excluded,
  preserve-if-null. The publish→ingest latency ground truth.
- **idnes area truncation fixed** (#686): the briefed "per-m² land prices stored as
  absolute" premise was DISPROVEN against every affected staged page (stored prices
  match the portal's own absolute amounts; the sub-100k plots are genuinely cheap).
  The real defect was next door: spaced-thousands area parsing ("2 403 m²" → stored
  403) on ~8.4k active rows — parser fixed + a Kč/m² drift rail added + a
  reparse-from-staged-HTML backfill script (run post-merge).
### 2026-07: Dedup metrics hygiene — gauges decoupled from runs, geo run rows (audit PR-F)

Final slice of the 2026-07 audit program:
- **The ~9s market-gauge scan runs only where it means something** (migration 265):
  `eligible`/`flagged_*` are properties of the whole table, yet every street-pass run paid
  the full-table aggregate — including the hourly dirty drain whose own work takes
  milliseconds. Scoped runs now write NULL ("not measured"); the /dedup gauges and the
  pipeline overview read the latest FULL-scan row (`run_kind='full'`, legacy NULL-kind
  rows included), while activity stays the latest run of any lane, labeled by lane.
- **The geo lane writes run rows** (`run_kind='geo'`): it previously wrote none, so a
  chronically truncating geo scan was invisible. Its `eligible` is the geo lane's own
  count, excluded from street gauge pickers by run_kind.
- Dead-counter cleanup: `skipped_same_source` (always 0 since the Wave-3 gate removal)
  dropped from stats + logs; `cost_usd` (never written by the engine) dropped from the
  dashboard select/type — columns stay (historical rows).

### 2026-07: street_name_key guards — presence CHECK + weekly sampled parity (audit PR-E)

The write-path invariant "every listings.street write stamps street_name_key" was
enumeration-guarded only, and the enumerated class already failed once (the coord→street
resolver). Now four guards, each covering a distinct failure class:
- **Presence CHECK** (migration 264, `listings_street_key_presence`, NOT VALID→VALIDATE,
  0 violations pre-validated): a street containing any alphanumeric char must carry its
  key — the forgot-to-stamp write now fails LOUDLY at write time. Alnum-gated so it is
  exactly as strong as the Python function's own guarantee (a whitespace-only street
  legitimately keys to NULL; btrim can't see unicode whitespace, [[:alnum:]] can't
  false-fire).
- **Weekly sampled parity** (`street_key_parity.yml` → `scripts/check_street_key_parity.py`,
  2,500 newest + 2,500 block-random rows): stored key recomputed against the ONE Python
  normalizer; any drift fails the run → workflow-failure monitor → Health page. This makes
  migration 256's "parity test" claim TRUE (the audit found it referenced a test that
  didn't exist). First manual run: 0/1,200 mismatches.
- **Normalizer-edit protocol** documented at the edit site (scraper/street.py): editing the
  normalization requires the `backfill_street_name_key.yml all=true` re-key; the parity
  failure is the alarm for forgetting it. The --all re-key now skips already-correct rows
  (`IS DISTINCT FROM` — an "identical" UPDATE still writes a dead tuple under MVCC).
- Migration 264's comment corrects the record on migration 256's two overstated claims
  (append-only forbids editing 256 itself).

### 2026-07: Dedup dirty-lane throughput — probe memoization + batched pHash + dismissal consult (audit PR-B)

The audit's cost-floor fix: resolve_pair paid 2-3 SEQUENTIAL DB round-trips per candidate
pair (~0.5-0.75 s/pair from a GitHub runner to the EU pooler ≈ the whole 1200 s dirty
budget — the chronic-truncation root cause #670's per-group clear worked around).
- **`_ProbeCache` on `_RunContext`**: CLIP-completeness, floor-plan ids, and site-plan
  presence are per-LISTING facts now queried once per run instead of once per pair — a
  group of n listings costs O(n) probes, not O(n²).
- **Group-batched pHash**: `_phash_group_counts`/`_phash_group_distinctive` compute every
  cross-listing pair of a street group in ONE round trip (per exclusion-profile — byt
  excludes exteriors/renders); `run_engine` hands the group's ids to `resolve_pair`, and
  per-pair fallbacks keep standalone callers identical.
- **Dismissal treadmill fixed** (measured: 3,621 audit rows for 620 distinct pairs in 7
  days, ~5.8x): scoped runs (dirty/candidates) consult prior verdict-backed dismissals and
  SKIP a pair unless either side gained photo evidence since (`images.clip_tagged_at` >
  `reviewed_at` — recall survives new photos); engine auto-dismissals now insert a durable
  `status='dismissed'` candidate marker (66% of treadmill pairs had NO row to consult);
  `_write_pair_audit` drops records identical to one logged within 7 days (the audit logs
  decisions, not run cadence). Full scans never consult (cursor-rotated by design).

### 2026-07: Resolver streets survive refetches — preserve-if-null + provenance + geom guard (audit PR-D)

The RÚIAN coord→street resolver's fills were clobbered back to NULL by the row's next detail
refetch (`street = EXCLUDED.street`; the portal page has no street by definition of the fill).
Measured: 40% of a resolver cohort lost in 2.5 days; only ~455 of ~4.9k ever-filled still held
streets; ~4.6k active rows uniquely resolvable but streetless — direct dedup-eligibility + Browse
loss. The resolver's provenance was a raw_json marker the same refetch destroyed.

Fix (migration 263 + `scraper/db.py`):
- **One shared SET builder** (`_listing_update_set_sql`) for BOTH ingest upserts —
  `street`/`street_name_key`/`house_number` become preserve-if-null (`COALESCE(EXCLUDED.c, l.c)`);
  a page-parsed street still wins; write-path semantics can never drift between the two paths.
- **`listings.street_source`** ('parser' | 'resolver' | NULL): durable provenance. Ingest stamps
  'parser' when the page yields a street, else preserves; the resolver stamps 'resolver'. Only the
  'resolver' rows are backfilled (via the surviving raw_json marker, narrowed by
  `coord_street_attempt_version` so the jsonb probe stays tiny) — the correctness-critical
  distinction is 'resolver' vs everything-else, so legacy parser rows are deliberately left NULL
  (defined as parser-equivalent on the column comment); a bulk 210k 'parser' UPDATE deadlocked
  against the live detail drains for zero semantic gain, and organic refetches stamp 'parser' as
  pages re-yield streets.
- **Geom-change guard** in `listings_set_admin_geo` (reproduced from the LIVE definition — the
  repo's mig-222 copy predated the streetless-coord-change block): coordinates changed + source
  'resolver' → NULL the trio + provenance; the trigger's tail block then re-opens the resolver at
  the new coords. Parser streets untouched.
- Post-deploy: a `--force-rescan` resolver dispatch recovers the ~4.6k currently-resolvable rows.

### 2026-07: Dedup run observability — run_kind/truncated + cleared-based queue health (audit PR-A)

Observability slice of the 2026-07 dedup-lane audit program (lands after PR-C below — the
program's slices merged out of order as parallel sessions picked them up):
- **Run rows tell the truth** (migration 262): every `dedup_engine_runs` row now records
  `run_kind` ('full' | 'candidates' | 'dirty'), the run-level `truncated`, and a REAL
  `started_at` (previously stamped at INSERT — equal to `ended_at`, so durations were
  unrecorded). A deadline-cut FULL SCAN is now a queryable fact — the measure that proves
  PR-C's cursor is delivering whole-market cycles (and alarms if it regresses). "Latest run"
  readers order by `ended_at` (insert-order semantics preserved — a long scan's row must not
  sort below dirty runs that started after it).
- **Queue health keys on real progress, not depth**: `assessDirtyQueue` now reads
  `dirty_cleared`/`dirty_truncated` (migration 258 — written since #666 but consumed by
  nothing) — "draining" means cleared>0 in the window, and a truncated streak with zero
  cleared is a red LIVELOCK regardless of depth. The 24h TTL prune (cycle-gated since PR-C)
  shrinks depth whether or not the drain works, so the old depth-trend heuristic mislabeled
  pure eviction as "draining" (the exact 2026-07 production state). Pre-258 rows fall back
  to the depth trend.
- Drift batch: `--max-dirty` help said "N OLDEST (FIFO)" (the opposite of the #666
  newest-first claim); the module docstring still described the Wave-3-removed cross-source
  gate and the retired rule-B auto-merge; a manual `dirty=true` dispatch fell to the CLI's
  10000 claim with no `max_dirty` input (dispatch now defaults to the scheduled run's 3000).

### 2026-07: Dedup full-scan cursor — whole-market coverage + honest TTL eviction (audit PR-C)

The deep dedup-lane audit measured the 6h street full scan hitting its 4800 s deadline at ~64k of
~688k pair slots in a DETERMINISTIC order with no cursor — a head-restart every run, so ~91% of the
market's street groups were **structurally never re-scanned**, and the dirty queue's 24h TTL
eviction ("the full scan has already covered them") was **silently discarding uncovered work**.

Fix (migration 261, `dedup_scan_state` — lane-keyed so the geo lane can unify onto it):
- **Cursor rotation:** with `cursor_out` enabled (the plain scheduled full scan only), `run_engine`
  iterates groups in sorted key order resuming after `cursor_key`; each run advances the frontier;
  reaching the end of the list completes the CYCLE (stamps `last_cycle_started/completed_at`,
  resets the cursor). Whole-market coverage every ~2–3 days at current throughput. A truncated run
  that scanned nothing keeps the previous frontier (never regresses to the top).
- **Cycle-gated TTL:** `_prune_stale_dedup_dirty` additionally requires
  `marked_at < last_cycle_started_at` — a row enqueued before a completed cycle began is
  guaranteed to have had its groups scanned during that cycle. No completed cycle → evict nothing
  (safe default); the failure mode becomes VISIBLE queue growth, never silent loss.
- Dirty/candidate drains keep their own work-lists (cursor-free); shadow runs never persist state.

### 2026-07: Dedup dirty drain — per-group incremental clear (monotonic progress under any budget)

Post-redesign audit found the dirty drain STILL truncating 4/5 runs with `dirty_cleared=0`: a
3,000-property claim generates >1,600 pairs at ~0.5–0.75 s/pair (per-pair DB round-trips — pHash,
clip-completeness, plan-image lookups), which doesn't fit the 1200 s budget, and the all-or-nothing
clear kept the ENTIRE claim on truncation — so the same slice re-processed hourly (merges happened,
work repeated, queue drained only via TTL). This was the "clear per-group" defense-in-depth the
redesign deferred; the data proved it necessary.

Fix: `run_engine` gains a `resolved_property_ids` out-collector — a claimed property is RESOLVED
once EVERY street group containing it (a listing dual-keys into 'id:' + 'name:' groups) was fully
scanned this run (`for…else` completion detection; skipped oversized / filter-skipped groups count
as scanned; zero-group claimed properties resolve immediately). The dirty drain then clears
`claimed ∩ resolved` EVERY run — a truncated run clears what it finished, only the remainder
re-drains. Progress is monotonic by construction, immune to per-pair cost drift — no cap tuning.
Tests cover full-run resolve, truncated partial resolve, and the dual-key guard (one of two groups
scanned ≠ resolved). Follow-up (perf, separate): batch the per-pair DB probes set-based per group
to cut the ~0.5 s/pair floor.

### 2026-07: Floor-plan gate → contradiction veto (stop queuing ~600 obvious merges over 3D-render "plans")

Cross-portal apartment groups with 6–7 identical interior photos (pHash) were stuck in manual review
labelled "one-sided floor plan" even though both listings had plans. Root cause: the floor-plan
validation gate ran `compare_listing_floor_plans`, which correctly returns `inconclusive` when the
"plans" are 3D perspective RENDERS (not true 2D drawings, migration 245) — and
`dedup_floor_plan_inconclusive_to_review` (default on, and the setting row didn't even exist) routed
`inconclusive` to the queue, vetoing the strong pHash/visual merge over an un-readable render. 607
would-merge pairs were queued this way (198 inconclusive + 386 genuinely one-sided + 23 edge); the
`/dedup` audit also **mislabelled** every one as "one-sided" (the reason `floor_plan_review` was
rendered "jednostranný plán" regardless of the real verdict).

The gate is now a pure **contradiction veto** — it only DISMISSES on a proven `different_layout`, and
QUEUES exactly one genuinely-human case; everything it can't 2D-compare MERGES on the primary signal:

- **New `no_2d_plan` verdict** (migration 260): the compare distinguishes "≥1 side has no usable 2D
  plan (only 3D renders / illegible)" → **merge** (moot check, trust pHash/visual) from
  **`inconclusive`** = "BOTH sides HAVE usable 2D plans but still ambiguous" → **queue** (the
  operator's carve-out; a real both-2D ambiguity is a human call). Prompt rewritten (`updated_by`-
  guarded) + verdict CHECK extended + stale `inconclusive` cache swept so old render-verdicts re-run.
- **One-sided → merge** (was queue): no plan-to-plan compare is possible, so the gate can't
  contradict — the primary signal stands. `different_layout` still dismisses; unwarmed both-plan
  still defers.
- **Audit truthful**: `floor_plan_review` now reads "nejednoznačný 2D plán — k ruční kontrole" (both
  sides have 2D plans, comparison ambiguous), not "one-sided".

Backstop unchanged: `different_layout` (the only auto-dismiss) still protects dev-unit false merges;
all merges reversible/flaggable. The candidate drain re-decides the ~584 non-genuine-ambiguous queued
pairs → auto-merge.

### 2026-07: Dedup dirty lane gets its own concurrency group (real-time SLO no longer starved)

Follow-up to the real-time drain redesign: the `--dirty` lane shared ONE `dedup-engine` concurrency
group with the slow batch runs (full scan 6h / candidate 2h / geo 4×/day) at `cancel-in-progress:
false`, so a dirty run queued behind — and was often cancelled by — an 80-min full scan (several
cancelled dirty runs in the run history). That caps the "merge in minutes" SLO no matter how fast
the drain itself is. Fix: a dynamic concurrency group — the dirty cron (`45 * * * *`) runs in
`dedup-engine-dirty`, everything else stays in `dedup-engine`. Safe to run concurrently with a batch
run because `merge_properties` already row-locks both properties `FOR UPDATE` + gates on
`status='active'` (the same lock safety that already lets dedup run concurrently with
property-maintenance, a separate group) — concurrent merges serialize per-property; a redundant
re-decide is a cheap `already_merged` no-op. No new locking needed. Guard test asserts the dirty
cron maps to its own group.

### 2026-07: LLM-liveness observability — record provider FAILURES; alarm on credit exhaustion

The Anthropic account ran out of credit and every paid LLM path (dedup vision, condition scoring,
estimations, summaries) 400-ed for ~8h — while the `llm_health` monitor stayed **green**. Two gaps:
`llm_calls` recorded only SUCCESSFUL calls (a failure left zero trace), and the check only alarmed
when condition-scoring work was pending (it was quiet, so nothing fired).

- **`llm_calls.error`** (migration 259): `LLMClient.call` now records a best-effort failure row
  (zero usage/cost, `error` set) on every provider exception, then re-raises unchanged — so an
  outage is auditable. Partial index `WHERE error IS NOT NULL` for the health probe.
- **`check_llm_health`** gains a THIRD probe, independent of pending work: a credit-balance error in
  the window alarms immediately; `>= --min-failures` generic failures alarms too. Keeps working with
  no Anthropic key of its own (queries the DB). Wildcard bound in the value, not the SQL (psycopg
  `%`-safe).

### 2026-07: Dedup real-time drain redesign (the second dirty-queue stall, fixed at the foundation)

The `--dirty` drain stalled again — `dedup_dirty_properties` grew to **201k** and never drained
(the `/dedup` "Dirty queue stalled" banner) despite the FIFO-bound (#647), stall-metric (#649), and
scoped-load (#650) patches. Root causes, verified against the live DB + GH logs: (1) the enqueue was
an ENRICHMENT-progress firehose — the CLIP tag job enqueued every property whose images finished
tagging, so the market-wide backfill streamed the whole inventory through the real-time lane, **78.5%
of it un-mergeable** (no street+disposition); (2) each hourly run FIFO-claimed the same June-backfill
head, couldn't finish its street-group pair-work within the 20-min budget (aggravated by inline
floor-plan calls downloading R2 plan images then 400-ing on an out-of-credit Anthropic account),
truncated, and — the clear being gated on a non-truncated run — never advanced the head. The fix
attacks the FOUNDATION, not the symptom:

- **Enqueue = a real-time CHANGE signal** (`scraper.db._DEDUP_DIRTY_FROM_IMAGE_IDS_SQL`): gated on
  **eligibility** (property-grain `EXISTS` a street+disposition listing — the street-only drain can
  never merge one without) + **recency** (`first_seen_at` within `_DEDUP_DIRTY_RECENCY_DAYS=7`, a
  genuinely NEW arrival, not a backfill of old inventory). Cuts ~78.5% of inflow; the 6h full scan
  backstops anything dropped.
- **Claim NEWEST-first + TTL-evicted** (`_claim_dedup_dirty` `ORDER BY marked_at DESC`,
  `_prune_stale_dedup_dirty` 24h): a latency lane must serve the freshest listing first and can never
  let an un-drainable tail pin the head or grow unbounded. FIFO was the wrong order for the SLO.
- **`--floor-plan-budget 0` on the dirty cron** + `--max-dirty 3000`: no inline floor-plan vision on
  the hot path (deferred to the full scan / batch warmer), and a slice small enough to finish-and-
  clear within budget. The drain now clears every run.
- **Observability**: `dedup_engine_runs.dirty_cleared` + `dirty_truncated` (migration 258) — a run
  that clears 0 while the queue stays high is the silent livelock made visible.

Operator follow-ups: the underlying **Anthropic account was out of credit** (every LLM path down —
dedup vision, condition scoring, estimations, summaries; the `llm_health` monitor stayed green — see
the LLM-liveness observability follow-up), and a one-time cleanup of the ~157k ineligible/stale
`dedup_dirty_properties` rows lets the head jump past the backlog immediately.

### 2026-06: Dedup geo path — dedicated, paid scheduled run (single-dwelling dedup unblocked)

Houses/land/commercial (no disposition → invisible to the street engine; 229,948 active properties,
~64% of inventory) are matched by the geo path — which, though `dedup_geo_enabled=true`, had produced
**ZERO** candidates/merges in its entire history. Root cause: geo was bolted onto the street runs and
never effectively executed — on the 6h full-scan it ran *after* the street pass on the shared
`--max-seconds` (deadline-starved by the ~100K-eligible street scan), and on the candidate-drain it
inherited the street pass's apartment `restrict` (`_load_geo_eligible(restrict=apartment candidates)`
→ no single-dwelling rows). Fix: **geo is now its own scheduled run** (`dedup_engine.yml` cron
`0 3,9,15,21`, `--geo-only`, gated by `dedup_geo_enabled`) with its own budget, run **paid**
(`--max-vision-calls 300`, not `--free`) so the forensic FACADE compare auto-merges confident
cross-portal houses (different photos → pHash can't) and enqueues the ambiguous (`tier='geo'`). A
`--free` geo run would surface nothing — `--free` skips the compare AND suppresses the general
unresolved→queue enqueue.

- **Follow-ups:** extend the `dedup_batches` warmer to the geo funnel (street's 50%-off warm-cache cost
  model); a geo candidate-drain mode (O(queue)); the cross-portal coord-divergence cell-miss (a same
  house geocoded ~270 m apart on two portals lands in different geo cells — a coarser cell / radius match).

### 2026-06: Dedup dirty-drain scoped load (O(market) → O(dirty))

Closes the FIFO-bound's known follow-up: the `--dirty` drain's eligible LOAD was still
`_load_eligible(restrict=None)` — ~100K rows scanned every hourly run — because the street
NAME key was computed in Python and not stored, so the load couldn't be filtered in SQL. Now
it is **scoped to the claimed properties' street groups**:

- **`listings.street_name_key`** (migration 256) stores `scraper.street.street_name_key(street)` —
  the dedup street-group NAME key, relocated to `scraper.street` as the SINGLE home for street
  string logic (consumed live by `toolkit.dedup_engine.street_group_keys`, stamped at every
  street-write path: `scraper.db.upsert_listing`/`write_detail_batch` + the street backfills).
  Out of the content hash (no snapshot churn); a partial `(coalesce(obec_id,-1), street_name_key)`
  expression index backs the scoped lookup, with a one-shot NULL-key index for the backfill.
- **`_claimed_street_groups` + `restrict_street_groups`**: the drain reads the dirty properties'
  `street_id` + `(coalesce(obec_id,-1), street_name_key)` and the load (`_ELIGIBLE_SCOPED_SQL`)
  UNION-joins those claimed keys against `listings` as unnest-JOINs the planner **index-seeks per
  claimed key** (EXPLAIN-validated: cost ~25 vs a 100K-row scan — an `OR` of `street_id=ANY` + a
  row-comparison `IN` collapsed to one full-eligible bitmap scan, so the seek form is used instead).
  Street groups are obec-bounded (the `coalesce(.,-1)` folds the 0.4% NULL-obec rows in, so it's
  complete with no asterisk), so the scoped load carries each dirty property's existing peers while
  staying O(dirty) in BOTH load and pair-work. `only_groups_with_property_ids` still gates the
  RESOLVE, so the scoped load is a pure perf optimization under that correctness gate.
- **Single-source + drift-guarded**: one Python normalizer (NOT replicated in SQL), stamped at EVERY
  `listings.street` write path — ingest + all three bulk backfills incl. the weekly coord→street
  resolver (`backfill_address_point_streets`). A golden-case regression test pins the function + a
  write-path test asserts each backfill's UPDATE stamps the column; the ultimate backstop is the 6h
  full scan recomputing the key live (never reads the column), so a stale/missed key only delays a
  dirty-drain merge, never loses one.
- **Backfill**: `scripts/backfill_street_name_key.py` (+ `backfill_street_name_key.yml`) re-derives
  the key for existing rows from the stored `street` — no re-fetch, bulk set-based UPDATE, resumable.

### 2026-06: Dedup dirty-queue observability + stall alert

The 165K dirty-queue backlog (which crashed the drain) ran ~2 days unseen — there was no gauge.
Now every `--dirty` run records `dedup_engine_runs.dirty_queue_depth` + `dirty_claimed`
(migration 255, NULL on other run modes), and a single shared `assessDirtyQueue` helper
(`frontend/src/lib/dedupQueueHealth.ts`, unit-tested) drives both surfaces: the `/dedup` dashboard
gets a "Dirty queue" stat + a stall banner, and the Health page raises an amber (deep but draining =
transient flood) / red (high + NOT draining across recent runs = drain failing or out-paced) banner.
This is the gauge that also tells us WHEN the deferred O(market)-load fix becomes necessary.

### 2026-06: Dedup dirty-drain FIFO bound (crash fix)

The hourly `--dirty` drain was crashing (`SSL connection closed`) and the `dedup_dirty_properties`
queue had grown to 165K (44% of all properties) and wouldn't clear. Root cause: a large new-portal
image-tagging backlog (ceskereality/realitymix/mmreality) enqueued most of the market as dedup-ready,
and the dirty claim was **unbounded** — it claimed all 165K, ran a full-market load + tried to resolve
every group, never finished within the time budget, never cleared, so the queue only grew (and the
huge claim + full load dropped the pooled connection mid-run). Fix: `_claim_dedup_dirty` now claims the
**N oldest** dedup-ready properties FIFO (`--max-dirty`, default 10000) — aligning the dirty drain with
every sibling drain (recompute dirty-drain, candidate drain, detail drain all bound their per-run work).
Each bounded run completes-and-clears its slice, so a flood drains over successive runs.

- **Follow-up (DONE):** the eligible LOAD was still O(market) per dirty run — the FIFO bound capped only
  the pair-work, not the load. Closed by the stored-`street_name_key` scoped load above (migration 256).

### 2026-06: RealityMix.cz — new full scraper portal (pilot)

realitymix.cz (a Centrum.cz agency-feed AGGREGATOR, ~48k listings) added as the 5th
structured-HTML portal on the Phase-4 framework — a per-portal fetcher + parser + config,
no foundation changes. `scraper/realitymix_{client,parser,main}.py` + `portal.py` default
+ migration 250 (the `portals` row). STRUCTURED HTML like idnes/ceskereality: category from
the detail BreadcrumbList JSON-LD (the URL doesn't encode it), spec from the
`detail-information__data-item` list, precise coords + a structured street from `#print-map`
(no geocode), `supports_complete_walk=true` (per-category total, no deep-pagination cap).
`walk_category` drives `?stranka` by the page-reported total (not a pager arrow — the lesson
from ceskereality's reverted #637) with a barren-page retry. Cadence-split workflows
(index 6h + drain hourly), **dispatch-only until a GH-runner validation run is green**, then
the crons go live. Full parity: Chrome-extension overlay (`portals.ts`), on-demand URL parser
(`source_parsers/realitymix.py` + dispatcher), and broker attribution (agency/per-broker
identity from `data-fk_rk` + `/profil-realitniho-maklere/…-{id}`; `resolve_brokers` block,
identity-only — phone/email are behind a trackredir). Validated on live HTML: streets correctly
kept ("Luční"/"Hornická") vs rejected ("Jindřichov" místní část, via the morphology gate),
price NULL on "Cena na vyžádání"/"Rezervováno". The dedup engine merges the heavy sreality/idnes
overlap with no new wiring.

### 2026-06: Dedup follow-ups — floor-plan confidence floor + dead-code cleanup

Post-merge follow-ups to the fully-tagged-before-decide PR.

- **Floor-plan confidence floor** (`scripts/dedup_engine._floor_plan_image_ids`,
  `FLOOR_PLAN_MIN_CONFIDENCE = 0.50`): a CLIP `floor_plan` tag below 0.50 is dropped from the
  gate's plan set. Data-validated on the live distribution (95% of CLIP floor_plan tags ≥ 0.52,
  false positives like an idnes location-map mis-tagged at 0.36 concentrate < 0.50), so a phantom
  plan no longer creates a false "one-sided" read that queues an otherwise-mergeable pair. CLIP-only
  because only `image_clip_tags.confidence` is numeric — the LLM `image_room_classifications`
  confidence is a coarse high/medium/low text enum (a numeric floor there is a type error), left
  unfiltered.
- **Dead-code cleanup**: removed the inert `dedup_clip_only` setting + its plumbing (superseded by
  the always-on readiness gate) and the always-zero "By address" dashboard stat (rule B retired →
  no address auto-merges). Backend `auto_address` counter kept (writes 0, accurate).

### 2026-06: Bazos enrichment — model-keyed cache (sticky-miss fix, PR3 of the enrichment fix)

The description-enrichment cache `listing_description_enrichments` was UNIQUE(sreality_id,
snapshot_id): a MISS (LLM returned null / low-confidence) still wrote a row, so the listing's
latest snapshot was retired from selection FOREVER — even after a model upgrade that would now
extract the field. For stable classified ads (rare new snapshots) ~80% of cache rows had floor
still NULL, never retried. Migration 249 widens the key to `(sreality_id, snapshot_id, model)`
(mirrors `read_floor_plan` / `building_attachment_analyses`): a model upgrade re-attempts every
listing; a same-model re-run stays a no-op (no re-bill). `_select_pending`'s correlated NOT EXISTS
+ the enricher's cache-check are scoped to the current model; the INSERT uses a **targetless
`ON CONFLICT DO NOTHING`** (the cache key is the only conflictable constraint, `id` is GENERATED
ALWAYS) so the code is tolerant of the constraint swap regardless of apply-vs-deploy order.

- **Considered + rejected:** re-selecting still-NULL gap fields at the SAME model — re-bills the
  same model on the same text for marginal gain (the #606 deterministic regex already catches the
  high-precision floor cases) and risks infinite re-bill on truly text-less ads. Re-enrichment is
  the explicit, bounded model-upgrade lever instead.
- This closes the enrichment-reliability arc (PR1 #603 selection+budget, PR2 #606 shared floor,
  PR3 model-keyed cache).

### 2026-06: Dedup — fully-tagged-before-decide invariant + rule B retired

Cross-portal duplicates (near-identical photos) sat unmerged in the `/dedup` queue as false
`floor_plan_review` "one-sided" cases (77% of the backlog). Root cause: the engine decided pairs
before their images finished CLIP-tagging, so a still-pending floor plan looked absent.

- **Trigger fix** (`scraper/db.py mark_properties_dedup_dirty_for_images`): mark a property
  dedup-ready ONLY when the just-tagged listing is FULLY tagged (`NOT EXISTS` a pending stored
  image) — not after each partial batch (the bug).
- **Readiness gate** (`scripts/dedup_engine.py resolve_pair._clip_incomplete`): DEFAULT defer (when
  CLIP is the tagger) of any pair with a not-fully-tagged listing — up front, before pHash/visual —
  so the engine never decides on partial tag data. The `--dirty` drain re-decides once both sides are
  complete → a real two-sided floor-plan compare merges on MATCHING plans. Supersedes `dedup_clip_only`.
- **Rule B retired** (`toolkit/dedup_engine.classify_pair`): exact-address auto-merge removed — it was
  the only path with false merges (6.7% unmerged vs 0% for pHash/visual). Exact address is now a
  rule-C candidate flowing through pHash → visual (the `address_exact` reason kept for provenance).

### 2026-06: Bazos floor — shared `normalize_floor` + deterministic miner (PR2 of the enrichment fix)

bazos apartment `floor` coverage was 0.0% (2/13,976) vs 94–99.9% on every other portal —
the deterministic `bazos_parser` extracted area/disposition but no floor, leaving the whole
field to the (de-facto-dead) LLM enrichment. New shared `scraper/floor.py` mirrors
`scraper/street.py`: one canonical Czech-floor grammar `normalize_floor()` (ground=0:
přízemí=0, 1.patro=1, 1.NP=0, suterén=−1 — the idnes + LLM-rubric convention; sreality's
ground=1/NP stays the rule-#15 clash, converting it is a follow-up) plus a HIGH-PRECISION
free-text miner `floor_from_text()` wired into `bazos_parser.parse_detail` as a deterministic
first pass (like area/disposition). The miner fires only on explicit numeric cues and
guards the building-total trap (unit-floor NOUN '6. patře' captured; adjectival
'šestipodlažní' / 'z celkových N pater' read only as `total_floors`); the ambiguous tail
(word ordinals, mezonet, bare 'suterén') is left to the LLM. Validated on real data:
16/16 clauses correct, 0 false positives, ~50% recall at 100% precision. A shared
`is_plausible_floor()` (reject floor>total_floors / out-of-band) guards BOTH the miner and
the LLM fill (`columns_from_extraction`), and the enrichment prompt now states floor is the
unit's storey, never the building total. The LLM enrichment still fills the residual + the
other 7 gap fields (`floor` stays NULL-guarded → the deterministic value always wins).

- **Next:** route idnes/maxima's extracted floor values through `normalize_floor()` to
  retire their duplicated regexes; then tighten the dedup engine's `abs(floor diff) ≥ 2`
  convention tolerance to exact equality once sreality's ground=1 floors are converted +
  backfilled (its own PR — touches merge behaviour). Sticky-miss cache fix is the next PR.

### 2026-06: Dedup decision feedback + full auditability (`/dedup`)

The operator can now FLAG any dedup decision as incorrect and audit exactly why the engine
decided it — a labelled corpus for improving the flow, with every threshold and picture
traceable.

- **migration 248** — `dedup_decision_feedback`, **property-pair-keyed** (canonical
  `left_property_id < right_property_id`, not an audit-row id and not the drifting repr-listing
  pair) so one flag spans the Decision-history feed AND the Needs-review queue and persists across
  the pair's lifecycle without orphaning on a recompute.
  Carries `is_incorrect` + `expected_outcome` (should_merge / should_dismiss / unsure) + a free
  note. Writes via bearer-gated `POST/DELETE /dedup/feedback`; the feed gains a `flagged`-only
  filter. Shared `<DecisionFeedbackControl>` on both surfaces.
- **`toolkit/dedup_audit.build_audit_breakdown(detail)`** — a pure decision→rungs mapping
  (pHash / cosine / forensic verdict / floor-plan / address, each measured-vs-threshold,
  met/unmet/info), computed server-side from the stored factor `detail` so it renders identically
  on `list_pair_audit` + `list_candidates` and on historical rows. Each rung deep-links to the exact
  Settings knob (`settingAnchorId` → `/settings#setting-<key>`; the Settings rows carry stable
  anchors + a hash-scroll/force-open).
- **`decision_evidence`** (`GET /dedup/decision-evidence`) — the SPECIFIC pictures, resolved at
  READ time: the pHash near-identical pairs (recomputed from stored phashes with the engine's
  category exclusions), the compared plans, or the deciding room. No decision-time `detail` bloat.

### 2026-06: Property identity — category-aware dedup (P0 foundation, landed dark)

The dedup engine keys on `street + disposition`, but `disposition` is an apartment-shaped
token (`2+kk`) — structurally absent for houses/land/commercial. So ~64% of active inventory
(dum 2.3% eligible, pozemek 0%, komercni 0.2%) can never be matched, queued, or merged: the
operator's "I merge by hand but the listings count never drops" is that coverage gap (the
visible duplicates are houses/commercial the engine can't see), compounded by a singleton-at-
birth treadmill that regenerates dupes every scrape. Full diagnosis + the judged target
architecture (geo-anchored, per-category matching; durable identity to kill the treadmill;
houses auto-merge behind a same-development guard, land/commercial queue-only) is in the PR.

P0 (this PR) is behavior-neutral scaffolding for that work:
- `toolkit/dedup_engine.MatchProfile` + `profile_for(category_main)` — per-category policy as
  DATA, one profile per family (no `if category ==` branches; rule #21 posture). The `byt`
  profile reproduces today's constants exactly, so `classify_pair` is byte-identical for
  apartments and NULL-category rows (guarded by the existing suite); non-apartment profiles
  carry their intended flags (`disposition_required=False`, `geo_blocked=True`,
  `geo_auto_merge_allowed` only for `dum`, always `requires_development_guard`) but are
  unreachable by the live orchestrator — dark until P1 wires geo blocking.
- Golden-set harness: `dedup_golden_pairs` (migration 223), `scripts/build_golden_set.py`
  (positives from the 12.9k historical merges; negatives from the apartment coordinate trap —
  distinct disposition at one pin), `scripts/eval_identity.py` (per-category auto-merge
  precision / recall / false-merge-rate, the gate for any rule change). Seeded: 9,888 positives
  + 6,000 negatives. KNOWN LIMITATION: positives are apartment-biased (71 dum, 8 komercni) —
  historical merges only ever touched apartments — so non-apartment recall isn't measurable yet;
  P2's operator review of geo candidates is the labeling loop that grows those classes.

P1 (this PR) adds the **geo candidate generator** (dark/opt-in, queue-only):
- `geo_cell_key` (obec_id + 4-dp coord + category + offering — admin-scoped, NEVER raw-coord, so
  a same-coordinate collision across towns can't block together) + `classify_geo_pair`
  (deterministic, no LLM: coord/house-number/area/unit-marker contradictions → reject; strong =
  area ≤3% AND price-or-house#-match). `ListingKey` gained `lat`/`lng`/`price_czk`.
- `scripts/dedup_engine.run_geo_candidates` — a SEPARATE pass (the byt street engine is untouched)
  behind `--geo` / `--geo-only`; loads disposition-less single-dwelling listings the street pass
  can't reach, blocks by geo cell, and QUEUES candidates (tier `geo_<family>`). `geo_auto_merge`
  is hard-OFF in P1, so nothing auto-merges — it cannot false-merge. Not wired to cron yet.
- Validated against prod (set-based replica): the pass would surface **~71k candidate property
  pairs** (dum 25.8k / pozemek 22.4k / komercni 21.1k / ostatni 1.7k), ~47.7k of them "strong".
  That volume is exactly why it ships dark — `/dedup` needs a category facet + bulk-approve first.

**Next (P2+):** `/dedup` category facet + bulk-approve (the 71k can't be reviewed one-by-one) →
calibrate the strong-house bucket against the golden set → flip `dum` auto-merge on behind the
same-development guard (land/commercial stay queue-only) → durable `property_identity_keys`
signature + incremental resolver in `_attach_stragglers` to end the treadmill → score-gate Browse
merge-mode → wire the geo pass into `dedup_engine.yml`. Rewrite CLAUDE.md rule #15
(street+disposition is the byt profile, not the universal key).

### 2026-06: Delivery UI — channel opt-in + recipient config (Sprint N PR 5)

The operator-facing surface that turns the channel stack on from the app (no API/SQL).
One shared control, civic-archive native (copper chips, borders-only), reused everywhere:
- `components/DeliveryChannelsPicker.tsx` — the single "where do these alerts go" control
  (static "In-app · always" chip + Email/Telegram toggle chips; footnote links to
  Settings → Delivery and never fails silently). Used by BOTH watchdogs and collections —
  one vocabulary.
- Watchdog opt-in: a "Delivery" block in `CreateWatchdogModal` + a "Delivery" section in
  `WatchdogEdit`, wired to `notification_subscriptions.channels` (the type + create/update
  API now carry `channels`).
- Collection opt-in: `CollectionDetail`'s `MonitoringBlock` now shows the picker (wired to
  `collections.notify_channels` via the existing `updateCollection`) when monitoring is on —
  completing Sprint C's "delivery configured separately" placeholder.
- Settings → **Delivery** section: friendly recipient fields (email + Telegram chat_id) over
  the existing `app_settings` read/write (`notification_email_to` /
  `notification_telegram_chat_id`), so a non-technical operator never edits raw JSON.

UI copy is English (matches the app chrome; data stays Czech). No migration — the `channels`
field was already returned by the backend (PR 2). **Deferred:** a per-channel delivery-status
column on the Notifications feed (needs a backend feed-query join to `channel_sends` —
observability polish, separate PR).

The mobile-native channel — the abstraction's payoff: **one file + one registry line**, no
migration (the `channel_sends.channel` CHECK already allowed `'telegram'` from migration 207,
the outbox already routes the `chat_id` recipient from `app_settings.notification_telegram_chat_id`).
- `api/transports/telegram.py` — Bot API `sendMessage` (requests-only; `is_configured()` on
  `TELEGRAM_BOT_TOKEN`; HTTP / `ok:false` / network failures → `failed` SendResult, never raise).
  Registered in `_build_transports` alongside Resend. Hermetic tests (mocked HTTP).
- CLAUDE.md "Auth and secrets" now documents the Sprint-N delivery env vars
  (`RESEND_API_KEY`/`EMAIL_FROM`/`TELEGRAM_BOT_TOKEN`/`SPA_BASE_URL`/`OUTBOX_DRAIN_DISABLED`) +
  the operator-destination `app_settings` rows.

Dark until `TELEGRAM_BOT_TOKEN` is set + a watchdog/collection opts into the `telegram` channel.
**Operator go-live:** create a bot via @BotFather → `TELEGRAM_BOT_TOKEN` Railway env → DM the bot
→ set `notification_telegram_chat_id`. **Remaining:** PR 5 outreach unification; the Delivery UI
(channel toggles in WatchdogEdit + collection config + a delivery-status column on the feed).

### 2026-06: Notification delivery outbox (Sprint N PR 3 — dark until provisioned)

The delivery runtime that turns stamped `target_channels` into real sends — source-agnostic,
so it delivers BOTH watchdog and collection-monitor events (Sprint C) through one path.
- `api/notification_outbox.py` — `drain_once` (NEW un-sent `(event, channel)` pairs via the
  `notif:{dispatch}:{channel}` dedupe_key LEFT JOIN + a RETRY pass for due `failed` rows) +
  `compose_message` (channel-agnostic subject/body/deep-link from already-joined columns, 7
  change-kind labels, `SPA_BASE_URL`) + recipient resolution from `app_settings`
  (`notification_email_to` / `notification_telegram_chat_id`) + `outbox_loop` (a 2nd lifespan
  asyncio task mirroring `matcher_loop`).
- `ChannelClient` gains `configured_channels()`, a `retry()` path, and linear backoff
  (`next_attempt_at`) in `_finalize`.
- Migration 212: `app_settings` recipient endpoints + `notifications_outbox_interval_seconds`.
- Gated DARK: the outbox task only starts when a transport `is_configured()` (so it's a true
  no-op in prod until `RESEND_API_KEY`/`EMAIL_FROM` are set + redeploy); an unconfigured channel
  or unset recipient is skipped (no `failed` pile-up). Hermetic tests (compose, routing, gating,
  retry).

**Operator step to go live:** create a Resend account → set `RESEND_API_KEY` + `EMAIL_FROM` +
`SPA_BASE_URL` Railway env + SPF/DKIM/DMARC DNS, then opt a watchdog/collection into `email`.
**Next:** PR 4 — Telegram transport (`channel_sends.channel` CHECK ALTER + `BOT_TOKEN` + capture
`chat_id`); PR 5 — outreach unification; the Delivery UI (channel toggles + feed delivery-status).

### 2026-06: Collections monitoring + unified Notifications (Sprint C)

Built on PR A's unified event model. Turns the inert Collections feature into a
live "watch these properties" surface and adds the in-app notifications area.

- **Collections usable + monitored** (migration 211): ungreyed; per-collection
  `monitoring_enabled` + `notify_channels` + a protected default "monitoring"
  collection; create/edit UI + system-collection guards.
- **Collection-monitor producer** (`match_monitored_collections_once`,
  api/notifications.py): a 2nd notification producer alongside the watchdog
  matcher. Set-based emission of `collection_monitor` dispatches for monitored
  members — `price_drop`/`price_rise` (per-snapshot), `inactive`/`reactivated`
  (lifecycle), `new_source` (sibling on a new portal); own daily cadence +
  window (migration 210). `broker_change` reserved in the CHECK (migration 209)
  but not emitted — no clean change signal yet.
- **Unified Notifications feed** (`/notifications`): watchdog matches AND
  collection-monitor events from one LEFT-join endpoint (+ `unread-count` /
  `mark-all-seen`), with a **red unread nav badge**. The /watchdog page stays
  watchdog-scoped.
- **Add-to-collection** on Browse cards (adjacent to the pipeline funnel,
  rule #22), listing-detail CurationBlock, and the Chrome-extension panel.

Next (deferred, scoped): a `broker_change` signal (a resolver-stamped
`listing_broker_changed_at` or a monitor-local broker tracker) to light up the
reserved 7th kind; the channels session's outbox then carries monitor events to
email/Telegram for free (they read `target_channels`).

### 2026-06: Notification email send-plumbing (Sprint N PR 2 — ships dark)

The data + transport plumbing toward email alerts (builds on PR 1's ledger/transport).
Sends nothing yet — no outbox task, and email is dark until Resend is provisioned AND a
watchdog opts into the `email` channel; the always-on outbox is split into PR 3 so it
lands paired with live Resend provisioning rather than blind.
- Migration 208: `notification_subscriptions.channels text[] default '{}'` — per-watchdog
  delivery-channel opt-in (a delivery preference, kept OUT of `filter_spec` so Browse↔Watchdog
  filter lockstep is untouched).
- `api/transports/email_resend.py` — the Resend transport (requests-only, single api-key POST;
  `is_configured()` on `RESEND_API_KEY`+`EMAIL_FROM`; HTTP/network failures map to a `failed`
  `SendResult`, never raise). Registered in `_build_transports`. Transactional/self-notification
  scope only (Resend AUP forbids cold outreach + US data residency → outreach gets a separate EU
  vendor later).
- The matcher (`match_once` + `match_changes_once`) reads `channels` and stamps
  `notification_dispatches.target_channels` (channels minus the implicit `in_app`); subscription
  CRUD + the `/notifications/subscriptions` API carry `channels`.
- Hermetic tests: Resend transport (mocked HTTP), `target_channels` stamping on both passes,
  channels CRUD.

**Next:** PR 3 — the outbox lifespan loop (drains `notification_dispatches × target_channels`
via `ChannelClient`, gated to start only when a transport is configured) + `compose_notification_message`
+ recipient resolution + `SPA_BASE_URL` + the Delivery UI; operator provisions Resend
(`RESEND_API_KEY`/`EMAIL_FROM` + SPF/DKIM/DMARC DNS). Then PR 4 Telegram, PR 5 outreach unification.

### 2026-06: Notification channel-delivery foundation (Sprint N PR 1 — ships dark)

The delivery half of the unified-notifications work (builds on PR A's event model).
Adds the audited delivery ledger + the pluggable transport abstraction, mirroring the
`api/providers/` + `LLMClient` pattern, with **zero runtime change** (no transport
registered, no background task — an unconfigured channel raises `TransportError`).
- Migration 207: `channel_sends` — append-only, one row per send attempt to one
  external channel (the `llm_calls` of delivery). Shared by watchdog /
  collection_monitor / outreach via a `consumer` discriminator + exactly-one-origin-FK
  CHECK (`notification_id` uuid → `notification_dispatches`, or `outreach_message_id`
  → `outreach_messages`); per-(event,channel) `dedupe_key` UNIQUE; denormalized
  `source_kind`/`source_id` + `category` for telemetry; `next_attempt_at` for the
  outbox retry pass. Purely additive, unused until PR 2.
- `api/transports/base.py` (`ChannelTransport` Protocol + `RenderedMessage` /
  `SendResult` / `TransportError`), `api/channel_client.py` (claim-by-INSERT-ON-CONFLICT
  → send → finalize, never raises on a transport failure — the failed row is the audit
  trail), DI in `api/dependencies.py` (`_build_transports()` → `{}`, `get_channel_client`).
  Hermetic `ChannelClient` tests (idempotent re-claim, unconfigured-channel, transport
  exception, telemetry params).

**Next:** PR 2 — `email_resend.py` + the outbox lifespan loop (drains
`notification_dispatches × target_channels`) + `compose_notification_message` +
`SPA_BASE_URL` + Resend DNS + Delivery UI. Then PR 3 Telegram, PR 4 outreach unification.

### 2026-06: Unified notification event model (PR A — notifications + collections + channels foundation)

Foundation for two parallel sprints (multi-channel notification delivery + collections-driven
monitoring): generalize the watchdog-only `notification_dispatches` into the **unified
notification event table** that the watchdog matcher AND a forthcoming collection-monitor producer
both write, the one in-app Notifications feed reads, and the channel-delivery layer (Sprint N)
drains. Migration 206 (additive ALTERs + one operator-approved dedup-primitive swap):
- `source_kind` (`watchdog` | `collection_monitor`) + nullable `subscription_id` + `collection_id`
  FK — one event row, two producers.
- Single per-event **`dedupe_key`** replaces `UNIQUE(subscription_id, property_id, change_kind)`:
  `wd:{sub}:new:{property_id}` (once-ever) and `wd:{sub}:price_drop:{snapshot_id}`
  (**per-snapshot** — fixes the latent "fire once ever" limitation; a property that keeps cutting
  price now fires per cut, and the collection monitor inherits the same grain).
- Provenance columns (`trigger_price_czk` / `prev_price_czk` / `trigger_snapshot_id`) so "why was
  I pinged" survives latest-wins; `target_channels[]` is the producer-stamped delivery contract.
- `toolkit/operator_state.py` merge reconciler extended to the dual-source + per-snapshot collapse
  key (NULL-safe). `api/notifications.py` matchers ported (`_recent_price_drops` now per-snapshot).
- Corrected a load-bearing doc error: migration 057's "a new channel is a one-line ALTER" was
  **false** (migration 096 dropped `channel` from the dedup key); CLAUDE.md rule #16 + the design
  doc now record that delivery gets its own ledger. Shared contract:
  `docs/design/notifications-unified.md`; channel layer: `docs/design/notification-channels.md`.

**Next (the two sprints this unblocks):** Sprint N — `channel_sends` ledger + transports (Resend
email, Telegram) + outbox loop. Sprint C — ungrey collections + default "monitoring" collection +
add-to-collection (card/detail/extension) + the collection-monitor producer + the unified
in-app Notifications area + unread badge.

### 2026-06: App-wide keyset infinite scroll (Browse + Estimations + Watchdog)

Replaced offset pagination with **keyset-paginated infinite scroll** on every scrolled list
surface, built on one shared primitive. Offset was both a correctness and a latency bug here:
the default Browse lane sorts by `last_seen_at DESC` — the column the scraper bumps every cycle
— so under offset a bumped row jumps to the top, shifting every later window and silently
**duplicating one row + skipping another** mid-scroll; and `OFFSET 50000` on the 317k-active-row
`properties_public` view measured **3.7s**, over the anon 3s timeout. Keyset anchors each page to
`(sort value, property_id)` — correct under mutation, and 14× faster (worst-case unfiltered page-2
= 272ms, faster as you filter/scroll deeper). Validated end-to-end on prod: page-1 ∪ page-2 == the
global top-48 exactly (0 overlap, 0 skip), incl. the NULLS-LAST tail crossing for nullable sorts.

- **Shared primitive:** `lib/keyset.ts` (cursor + the PostgREST `.or()` predicate builder, incl.
  the two-phase NULLS-LAST boundary, emitted only for nullable columns so NOT-NULL lanes keep
  their index; 18 unit tests), `lib/useInfiniteList.ts` (React Query v5 `useInfiniteQuery` wrapper
  — flatten + dedup by stable id, `firstPage`, rows-based poll), `components/InfiniteSentinel.tsx`
  (IntersectionObserver; scroll root parameterized: cards = the overflow column, table/feeds =
  viewport), `lib/useScrollRestoration.ts` (restores the cards column's inner scrollTop on
  "open a card → Back").
- **Browse:** cards + table both keyset over `properties_public`; cohort `total` fetched once per
  filter set (head count), not per page; `?page` removed (cohort change → new key → reset to top).
- **migration 198** — denormalized `mf_gross_yield_pct` + `mf_reference_rent_czk` onto `properties`
  (the one join-sourced sort lane → now keyset-cheap; `recompute_mf_gross_yields()` mirrors them
  from the representative listing) + composite `(col, id)` keyset indexes (default lane verified
  sub-ms index scan).
- **API feeds:** `GET /estimations` + `/notifications/dispatches` gained an opaque `cursor`
  (`api/cursor.py`, ordered `(created_at|dispatched_at, id) DESC`, `next_cursor` in the response,
  `total` on first page only; legacy offset path preserved). **migration 199** indexes both.
  EstimationList + Watchdog use the shared primitive; Watchdog's mark-seen / kickoff now patch the
  row in-place (scroll-preserving) instead of invalidating the whole cache.
- **Not converted (deliberate):** Stats (aggregate), Collections / WatchdogManage / Datasets /
  Health / Dedup (already load-everything or operator review queues).
- **Next (focused follow-up PR): row-value keyset RPC.** The PostgREST `.or()` keyset is a
  BitmapOr — correct but the unfiltered page-2 worst case is ~272ms (invisible behind the 700px
  prefetch, well under the 3s budget, faster as you filter/scroll). A true row-value comparison
  `(col, id) < (X, N)` is a bounded index scan (measured **0.5ms**, flat as the table grows), but
  PostgREST can't emit it — it needs a Postgres RPC reimplementing the Browse filter set in SQL
  (a sibling of `browse_stats_properties`). Deferred to its own PR + review (a dynamic-SQL bug
  there = silent dup/skip). Also deferred: composite indexes for the Table's text/secondary sort
  lanes (district/disposition/estate_area/usable_area/parking_lots — currently ~750ms seq-scan,
  under budget) — add when one becomes hot or restrict the sortable set.

### 2026-06: Dedup vision cost — async batch warm-up lane (50% off, recall-identical)

The last recall-safe lever in the dedup-vision cost program (after the cross-source gate,
classify→Haiku, 768px downscale, and pHash-first). An async lane pre-warms the dedup engine's
classify/compare/site_plan caches through the Anthropic Message Batches API (50% off) so the
daily engine run replays unchanged over warm cache and merges for free.

- **migration 197** — `dedup_batches` + `dedup_batch_requests` (mirror condition's 098); model
  on the request (kinds mix Haiku/Sonnet), `image_ids` for the classify index→id ingest mapping.
- **Defer-and-replay, no rule duplication:** `scripts/submit_dedup_batch.py` runs the engine's
  FREE funnel (rules A/B/C + pHash + cross-source gate, reusing the same pure rules + SQL helpers)
  and enqueues only the not-yet-cached vision; `scripts/ingest_dedup_batch.py` polls, routes by
  kind to each toolkit module's persist helper (same cache rows the sync tools write), records one
  50%-discounted `llm_calls` row. Neither merges — the daily `dedup_engine.yml` replay does, over
  warm cache. A cache miss falls back to a sync call (correct, just not discounted).
- **Recall-identical:** submit enqueues `rooms_in_priority(common)[:max_room_attempts]` — the
  superset of rooms the engine's stop-at-first-High walk could reach — guarded by a golden test.
  Both-site-plan pairs defer compare behind the development-guard verdict, mirroring `_resolve_visual`.
- **`dedup_batches.yml`** — submit every 6h, ingest hourly (mode by cron), untagged (`portal: null`).
- **Cost:** the ~$640 post-lever sweep's remaining real vision halves to ~$380–430.
- **Next:** extract a shared `scripts/batch_lane.py` (submit/ingest plumbing) and retrofit the
  condition lane (rule-of-three unify); make the engine's `max_vision_calls` budget count only
  real (cache-miss) calls so warm-cache replay drains in one run instead of throttling on hits.

### 2026-06: Broker intelligence — identity foundation (phase 1 of 5)

Tie real-estate broker/agency contact data to listings so we can ask "broker X has N
listings of type T in region R" and "who has the most in R", then power a Brokers UI +
human-in-the-loop outreach (later phases). No re-scrape needed — sreality broker data is
already in `listings.raw_json->'user'` (idnes, phase 2, backfills from staged
`portal_raw_pages` HTML).

- **Two-layer identity model mirroring `listings → properties`** (migrations 185–188):
  `broker_identities` (per-source, authoritative on the portal-native id) → `brokers`
  (canonical human) and `firm_identities` → `firms` (agency, keyed on email domain), plus
  `broker_identity_contacts`, `broker_firm_memberships` (a broker can have many firms),
  the `dirty_broker_listings` queue, a reversible `broker_merge_events` ledger, and
  `broker_resolution_runs`. `listings.broker_identity_id` / `broker_firm_id` link the
  point-in-time facts. Hot public views (`listings_public`/`properties_public`) deliberately
  untouched — broker fields wire in later behind a measured migration.
- **Identity keystone (data-validated):** a contact bridges identity across sources ONLY
  if personal on both sides (frequency==1 each source) — shared role inboxes
  (`info@…`→353 brokers) and toll-free/switchboard numbers (one number→464 brokers) are
  excluded as bridges. Merges are conservative (≥2 bridges, or 1 + name match; oversized/
  transitive components queue) and reversible. Built + unit-tested now, dormant until the
  2nd source (idnes) lands.
- **Decoupled resolver** (`toolkit/broker_resolver.py` pure rules + `scripts/resolve_brokers.py`
  orchestrator + `broker_resolution.yml` */10 incremental + `resolve_brokers_full.yml` daily/
  backfill), off the scrape hot path (mirrors property-maintenance, rule #20). Firms via
  domain minus a curated free-provider list (`data/broker_free_email_domains.json` →
  `app_settings`, operator-tunable).
- **Read surface:** `broker_region_type_stats` matview (per region/okres/obec, distinct-
  property counts AT each level) + `broker_leaderboard` RPC + `brokers_public`/`firms_public`/
  `broker_firm_memberships_public` — answers both example queries under the anon 3s timeout.
- **DONE — backfill:** 12,560 brokers / 3,544 firms / 0 free-provider firms / 148k matview
  rows; leaderboard returns ranked brokers with contacts. Three backfill-path fixes shipped
  (sparse-id chunking, rollup id-batching, cross-source-scan skip + raised timeout on the
  bridge/matview statements).
- **DONE — phase 3 Brokers UI** (migration 189 `broker_geo_options` + `broker_listings_public`):
  top-nav "Brokers" → a ranked registry **leaderboard** (kraj/okres × type × offer × metric,
  URL-synced) and a **broker detail** page (contact card with tel:/mailto: for outreach,
  regional footprint, firm memberships, inventory → listing detail). Reads the public views +
  RPC via anon; civic-archive tokens.
- **DONE (code) — phase 2 idnes extraction:** `scraper/broker_idnes.py` parses the idnes
  detail contact block (account.<oid> = the per-broker key, validated against the makler-detail
  URL; name; entity-decoded email; tel: phone; agency name) into `idnes_parser`'s
  `raw_json.broker` (out of the content hash → no churn). The resolver attributes idnes from
  `raw_json->'broker'` alongside sreality's `raw_json->'user'` (source-generic rollups +
  cross-source merge follow automatically). `scripts/backfill_idnes_brokers.py` + workflow
  reparse the ~126k staged detail pages into `raw_json.broker` (resumable, no re-fetch).
  POST-MERGE: dispatch `backfill_idnes_brokers` → `resolve_brokers_full` → validate idnes
  freq distribution → add `'idnes'` to `app_settings.broker_auto_merge_sources` to activate
  the cross-source merge.
- **Next:** `toolkit/brokers.py` + FastAPI routes (programmatic/agent access — UI uses the RPC
  directly today); firm display names from idnes agency labels (deferred polish); phase 4
  outreach CRM (LLM drafts, human-in-the-loop send, GDPR opt-out); phase 5 operator
  merge-review + franchise office split.

### 2026-06: Apartment street-coverage levers (sreality locality.value + idnes/bazoš fixes)

Follow-up to the street-extractor work, after a cohort audit showed the two coverage
tables differed only by denominator (apartments vs all-categories) and surfaced the
real recoverable gaps. Principle: **only precise available data, no estimates** — a
wrong street is worse than NULL.
- **sreality `locality.value` fallback (the big win):** the parser read only the
  *structured* `locality.street`, empty on index-shape rows that carry the street in
  the free-text `locality.value` ("Street, City - Quarter"). `parse_listing` now falls
  back to that through the shared `street_from_locality` (structured always wins).
  Validated on live data: 87% of value-bearing rows recovered, **zero town-as-street
  fabrications** → sreality apartments **71.8% → ~87.6%**. Backfillable from `raw_json`
  (`backfill_portal_streets.py` gained a `sreality` source), snapshot-safe.
- **iDNES rule fixes:** the area-token guard wrongly ate `1. máje`-family streets
  (anchored it on the `m²` unit); Brno's doubled-`okres` form wrongly rejected a real
  first-segment street (only reject a *bare* `okres X` neighbour).
- **bazoš glued `ul.Výstavní`:** the keyword-anchored extractor + `clean_street` now
  handle a dotted prefix glued to the name, while still protecting real streets that
  merely start with "Ul" (Ulrychova/Ulická). Fuzzy prepositional/bare-name recall
  rules were deliberately **not** added (fabrication risk > the small gain).
- **Honest reporting:** iDNES's headline 60.5% is ~77% foreign apartments; on Czech
  apartments it is already ~87%. Report coverage foreign-excluded.
- **Built:** the coords→street source via the RÚIAN "Adresní místa" address-point ingest
  (free, offline, exact-match-only), the durable path to ~88–90% overall — see
  `docs/design/street-coverage-ruian.md` (`address_points`, `ingest_address_points`,
  `backfill_address_point_streets`, + the ingest/resolve workflows).

### 2026-06: RÚIAN resolver — deadlock fix + version-gated re-attempts (migration 222)

The weekly "resolve streets from coordinates" job started failing with
`DeadlockDetected` against the `*/15` `listings` writers (index walk / detail drain).
Root cause: the resolver ran its whole ~93k-candidate run in ONE transaction (to keep a
`fallback_coords` temp table alive on the transaction pooler), holding row-locks the
entire run. Two coupled fixes, both validated on live data:
- **No long transaction.** Materialise the town-centre reject set (verified just **8,678**
  coords) into a Python set, drop the temp table, and commit each ≤500-row write on its own
  (the proven `backfill_portal_streets` shape) under a `SET LOCAL lock_timeout` + bounded
  deadlock/lock retry. Lock-hold collapses from minutes to one sub-second statement.
- **Stop re-scanning the dead backlog.** The resolver only marked *matched* rows, so 0 of
  ~93k candidates carried an attempt marker and 100% (54% of them streetless `pozemek`)
  were re-probed against 1.57M points every week. Now every processed candidate is stamped
  with the `address_points` revision (new `address_points_revisions` provenance table +
  `listings.coord_street_attempt_version`); a no-match is re-attempted only when the dataset
  advances (monthly ingest) or its coordinate changes (the geo trigger clears the stamp).
  `pozemek` is excluded outright. Steady-state weekly runs now scan only genuinely-new rows.

### 2026-06: Reliable street across portals (street extractor + group-best place_search_text)

- **Problem (verified live):** `listings.street` powered Browse street picks + the
  street+disposition dedup engine, but only sreality (~43%) and bazos (38%, polluted)
  carried it — idnes (108k rows), maxima, remax, bezrealitky, mmreality were all 0%,
  even though every source carries the data.
- **One shared contract — `scraper/street.py`:** `clean_street` (strip `ul.`/`ulice`
  decoration, split glued bleed, drop trailing boilerplate + house number),
  `reject_as_town` (the single don't-fabricate guard: foreign coords/countries,
  "Town - Quarter", "okres X", and any candidate equal to the row's own
  obec/okres/region), and `street_from_locality` (first segment idnes/remax, last
  maxima with a morphology gate that relaxes for the safe 3-segment shape). Returns
  None whenever uncertain — a wrong street is worse than NULL.
- **Per-portal wiring:** idnes/maxima/remax mine the locality/data-address; bazos routes
  its stored value through `clean_street`; bezrealitky reads the structured
  street+houseNumber+zip; mmreality reads its `:property` JSON. `ScrapedListing` gained
  `house_number`/`zip` (DB columns already existed). All three stay out of the content
  hash → backfilling never churns snapshots.
- **Read path (migration 183):** `place_search_text` is now `coalesce(p.street, l.street)`
  — a group-best `properties.street` (best non-null child, sreality-preferred) denormalized
  by `recompute_property_stats`, so a multi-portal property matches a street even when its
  representative listing lacks one. No regression window (falls back to repr street until
  the recompute populates it).
- **Backfill** (`scripts/backfill_portal_streets.py` + dispatch workflow): re-derives the
  historical rows from already-stored data — no re-fetch, no LLM, no Mapy spend,
  snapshot-safe. Validated on live samples (idnes 63% fill, zero fabrication). Removed the
  dead expand-normalizer `toolkit/addresses.py` (the geo sweep that used it was already gone).

- **Bug:** a street-level location chip (e.g. "Pezinská" in Mladá Boleslav) matched
  only the free-text `locality` column — but bazos stores the town in `locality` and
  the street in the structured `street` column, so ~12k active bazos listings with a
  known street were invisible to street picks even when obec, bbox and deal matched.
- **Fix — one canonical match column (migration 182):** `properties_public` gained
  `place_search_text` = `concat_ws(', ', street, locality)`, the single definition of
  "the free-text place words of a row". All three chip-matching surfaces (Browse
  Map/Table via PostgREST, Browse Stats `browse_stats_properties`, the watchdog
  matcher `_build_match_clauses`) now ILIKE that ONE column wherever they consulted
  `locality` — the street-pick branch *and* the legacy no-level fallback, include and
  exclude alike, so pre-resolution saved chips behave identically. PostgREST can only
  filter view columns, so a view column is the one mechanism that centralizes the
  semantic for every surface; a future portal that lands streets in `street` is
  covered with no further work.
- The frontend chip predicate moved out of `applyFilters` into the exported pure
  `districtsFilterClause` (queries.ts) with unit tests pinning every branch; matcher
  tests now assert no branch references bare `locality`. The agent/operator-facing
  `districts` registry description was rewritten (it predated level/id chips).

### 2026-06: Sprint D — architecture unification + dedup keying + market parity

- **sreality on the shared Portal framework** (PR #439): new `scraper/sreality_main.py`
  entrypoint drives SrealityPortal through the generic portal_runner with the framework
  CLI; `index_walk.yml`/`detail_drain.yml` switched over with registry-governed limits;
  `scraper/main.py` + `scrape.yml` kept untouched as the instant-revert fallback (and the
  image phase host). Validated by branch-dispatched production walk (20 pairs, 0 errors,
  61 flips) + drain (11,691 ingested, 0 errors, clean finalize) before merge.
- **Dedup dual-keying** (PR #443): verified sreality streets are always `id:`-keyed so
  name-normalization alone could never bridge to bazos (and the legacy idnes merges came
  from the removed geo machinery); `street_group_keys` now emits id+name keys, the name
  key is stripped of street-words + house numbers (full bazos `extract_street` keyword
  family), and a new `street_id_contradiction` reject closes the cross-town channel
  dual-keying opens.
- **sreality category parity** (PR #440): all 20 category pairs walked (pozemek +
  ostatni × prodej/pronajem/drazba/podil added); condition-score selector now
  category-gated to byt/dum so land/other rows never bill the LLM (idnes pozemek rows
  had been leaking into the queue).
- **Docs + fallback cleanup** (PR #441): superseded `scrape_bazos.yml`/`scrape_idnes.yml`
  deleted; CLAUDE.md scraper-ops sections synced to the split pipeline reality.
- **Condition batch chunks 45MB** (PR #442): the API edge's upload window — not the
  256MB cap — is the real ceiling; 6-8-min 150MB uploads were getting 502'd.
- Placeholder backfill executed (274 rows backed up to
  `placeholder_backfill_backup_20260612`, price 0/1 + zero areas nulled).


### 2026-06: Scraper P0 sprint + kraj-scoped condition scoring + observability (Sprint A/B)

- **Delisting fixed** (PR #418): completeness gate 0.995 + 24h staleness rail on
  bazos/idnes/bezrealitky; bazos 12h sweep throttle removed (it starved the big
  categories). First walk flipped 6,831 stale bazos rows; stale-actives >24h: 3.
- **Snapshot churn killed** (PR #419): sreality hash strips `labels`/`labels_extended`/
  `user.image` (56% of pairs were pure volatile churn); idnes coordinate carry-forward
  ends the geocode-skip/geom-wipe oscillation (and the residual Mapy spend).
- **Image red-loop ended** (PR #415): `fl=rot,…|` prefix chains completed (rot preserved),
  400/415 classified permanent (403 stays transient), 526 + 8,505 stuck rows re-queued —
  image workflows green with errors=0.
- **mmreality retired, silent-green fixed** (PR #416 + mig 173): Cloudflare blocks GH
  runner IPs (code verified correct); cron removed, registry rows disabled
  (incl. orphaned `ceskereality`); `portal_runner` now reds the run when every category
  fails and reports walk failures as errors.
- **Condition batches unbroken** (PR #420): size-aware chunked submission under the 256MB
  Batches cap (the 5,000×61.5KB submits 413'd since Jun 4), static prompt context hoisted
  (build 50min → ~8min), condition-specific LLM liveness.
- **Kraj-scoped scoring + cross-portal reuse** (PRs #427/#425, mig 174): selector reads
  `condition_scoring_enabled_region_ids` (seeded: Středočeský, Plzeňský, Královéhradecký,
  Pardubický, Vysočina); Settings-page per-kraj toggles with unscored counts;
  `propagate_condition_levels` copies genuine scores to property siblings (3,828 reused
  on first run) with provenance + selection exclusion.
- **Observability** (PRs #428/#426/#429/#430/#431, migs 175–180): `listings.inactive_at`
  + delisting-latency check; snapshot-churn check (10-min matview); per-field NULL-drift
  check vs 6-hourly `data_quality_snapshots` captures; composed end-to-end latency check;
  image-failure breakdown matview + Health card; Health-matview staleness stamp + banner;
  failed-workflow-run recorder (30-min poller) + Health card.
- **Bazos street persistence + locality backfill** (Sprint C): `ScrapedListing.street`
  (un-hashed, like lat/lon) now rides `to_row` into `listings.street`, and the bazos
  parser surfaces its extracted street on the contract — bazos rows become eligible
  for the street+disposition dedup engine. One-off
  `backfill_bazos_street_locality.yml` re-parses staged `portal_raw_pages` HTML
  (no portal re-fetch, no geocode spend, no snapshots) to fill the ~30k active rows
  missing `street`/`locality`.
- **Price/area placeholder guards** (Sprint C "enum hygiene, price/area guards"):
  `sane_price_czk` nulls `< 2` ("1 Kč dohodou" placeholders) alongside the overflow
  cap; `sane_listing_numerics` nulls 0 m² areas; every portal main sanitizes its
  index-price compare through the same clamp so placeholder-priced listings don't
  refetch forever. Enum hygiene (status overlay, drevo, ownership) verified already
  shipped in PR #273 + backfilled — production counts are 0.

- **idnes amenity parser + remax/bezrealitky subtype** (Sprint C): idnes amenity rows are
  check-icon OR free-text ("Balkon: jih , 4 m 2"; the garage signal lives inside the
  "Parkování" value + the icon-only "Dvojgaráž" row) — every amenity field now goes
  through the truthy-field path, plus `parking_lots`, a "Stav budovy" condition fallback
  and a "Vybavení domu" furnished fallback (houses were 0% on both). remax's 2026 coarse
  "Typ nemovitosti" (7 marketing groups) erased the fine vocabulary `TYP_TO_SUBTYPE` was
  built for — subtype now derives from the detail-URL noun
  (`prodej-ubytovaciho-zarizeni` → ubytovani, …) and "Nájemní domy" lands as komercni +
  cinzovni_dum for cross-portal agreement. bezrealitky's mapping verified correct against
  live GraphQL introspection (estateType is the finest enum exposed; houseType is
  access-denied) — its remaining NULL gap is upsert lag, a raw_json backfill, not code.

- **pHash throughput unstarved**: hourly cadence (was 6-hourly, every run cancelled at
  the 30-min timeout at a measured 1.5 img/s serial), 8-worker R2 downloads with per-row
  autocommit writes kept, cap 20,000/run, active-listing images hash first so the dedup
  corroborator sees relevant photos ahead of the historical backlog.

#### Next

- Scraper program remainder: registry `portals.categories` reconciliation for sreality
  (code CATEGORIES is authoritative today); name-group scoping by `obec_id` if cross-town
  address-exact false merges appear; `scrape.yml` + legacy main.py CLI retirement once the
  framework path has a quiet week.


### 2026-06: Browse filter restructure (price-change merge, condition ranges, Other band)
- **Sidebar reorganised, no band grew taller.** The "Property" band is gone — its
  filters were listing-level anyway (furnished / ownership / amenities live on
  `listings`, mirrored to the property row; there is no building entity in the
  schema) — replaced by a **Features** band (Unit + Amenities groups). "Market
  signals" dissolved entirely: velocity moved to a new bottom **Other** band
  (together with Source portal and the viewport-vs-centre Map filter), price
  history moved up into Essentials > Price.
- **Condition levels became 1–5 ranges.** `building/apartment_condition_level_max`
  joined the registry (all agendas — comparables/estimation/watchdog get the upper
  bound too), rendering as compact paired min/max inputs inside Condition &
  material. The Stats RPC finally applies condition levels (it never had — a
  Map-vs-Stats divergence closed by migration 173).
- **Price-history filters merged + windowed** (migration 173). The per-direction
  quartet (listed-on-N-sites / cut-N-times / raised-N-times / biggest-drop) was
  retired for `price_change_count_min` + a 30/90/365-day window select (reading
  precomputed `price_change_count[_30d/_90d/_365d]` columns) and a signed
  `total_price_change_pct` (−10 = "dropped 10 %+ overall", first→last observed
  price). Recompute job fills the new columns; old keys in stored presets and
  watchdog specs are dropped silently on load.
- **"With estimates" checkbox** (Curation) — new anon-readable
  `property_estimates_public` view (property grain over successful
  `estimation_runs`); Map/Table/Cards prefilter by property-id allowlist, Stats
  via EXISTS, Browse-only by agenda design.
- `isDefault` was rewritten as a generic compare against `DEFAULT_FILTERS`
  (the hand-maintained chain had already drifted), and the three cohort fetchers
  now share one `resolveBrowsePrefilters` helper.

### 2026-06: Listing page is the primary estimation surface
- **The listing/property page now owns estimations** — the "← back to estimations" /
  "view full listing" ping-pong is gone. A new **Estimates** section renders the two
  authorities side by side: the MF Cenová-mapa reference card and **our** selected run
  (estimate, range, yield chip, confidence), then the selected run's full body (yield
  calculator, re-run / adjust, the deep-detail popup with trace + comparables +
  feedback), then an **All runs** ledger of every run on any of the property's child
  listings (`?run=ID` selects; latest is default; in-flight runs poll in place).
- **`/estimation/:id` is a fallback, not a page.** Linked runs (subject in our DB)
  redirect to `runSurfaceUrl` → the listing page's section, so old links keep working;
  the standalone page renders only **orphan runs** (pasted URLs of unscraped listings)
  through the same shared `RunBody`. The run UI itself moved from the 2 600-line
  `EstimationDetail.tsx` into `components/estimation/RunPanel.tsx` +
  `MfReferenceCard.tsx` (one MF card for listing- and run-stored breakdowns).
- **Portal links moved to the top of the listing page** — one chip per portal
  observation (active dot, price + date-range tooltip), replacing the bottom
  "Open on …" block. The MF card moved out of `ListingOverview` into the Estimates
  section. Manual estimates now sit directly below it.
- **Backend:** `GET /estimations` gained `sreality_ids` (CSV — property-grain fetch);
  list rows drop the heavyweight `source_html` (detail endpoint still returns it).
  Estimations-list rows, Browse estimate corners, the new-estimation modal, and re-run
  flows all navigate via the shared `runSurfaceUrl` helper.
- **Architecture decision recorded:** the listing stays the URL-addressable object
  (immutable id; `property_id` regroups under the dedup engine), the page carries the
  property context. No stub listings for orphan runs — the listings table stays
  scrape-only.
- **Layout pass (follow-up PR):** the page widened from `max-w-3xl` to `max-w-5xl`
  (matching Estimations / Buildings); the header became two-column — identity + price
  left, the **location map anchored top-right** (the standalone Location section is
  gone) — with the Active/Inactive pill inline next to the disposition line and
  **floor in the header meta line**. The Property/Building/Amenities grids collapsed
  into one dense facts strip + compact amenity chips (duplicate Subtype dropped), and
  the **Estimates section moved up into the old map slot** right after the
  description, via `ListingOverview`'s `estimatesSlot`.
- **Header compaction (second follow-up):** the portal chips + active-sibling alert
  moved INSIDE the header grid's left column (`ListingOverview`'s `headerExtras`) as
  one wrapping row, so the map column starts at the very top instead of below two
  stacked full-width rows; the map zoomed out two levels (14.5 → 12.5) for
  neighbourhood-scale context.

### 2026-06: On-card "Estimate" action in Browse (run + show yield in place)
- Every **apartment** card in Browse > Map now carries a small bottom-right control.
  No run yet → an **`Odhad`** button that kicks off the standard **agent rental**
  estimate for that listing. While it runs the corner shows a spinner (`Odhaduji…`);
  once it finishes it shows the run's result **in place** — **`Výnos ~ X,X %`** when the
  asking price is known, else **`Nájem ~ X Kč/měs`** — clickable through to
  `/estimation/{id}`. Distinct (copper) from the muted statistical `Výnos MF` line,
  which is a price-map reference, not an actual estimate.
- **Backend:** `POST /estimations` gained a third target input `sreality_id` (exactly one
  of `url` / `spec` / `sreality_id`) — `_match_listing_by_id` builds the target straight
  off the scraped `listings` row, no URL parse / LLM. New batch read
  `GET /estimations/latest-by-listing?sreality_ids=…` returns the latest rent run per id
  (`latest_rent_estimations_by_listing`, `DISTINCT ON`), declared before
  `/estimations/{run_id}` so the literal path isn't captured by the int route.
- **Frontend:** Browse fetches latest estimates for the visible card ids
  (`latestEstimationsByListing`), polling every 4s while any run is pending/running; the
  trigger is an agent rent estimate via `createEstimation({ sreality_id, mode:'agent' })`
  with an optimistic running state. `EstimateCorner` in `ListingCards.tsx` renders the
  four states; handlers `stopPropagation` so they don't navigate the card `<Link>` /
  toggle merge selection.

### 2026-06: iDNES geocode — skip re-geocode on refetch (stop Mapy credit burn)
- Our Mapy.cz API key was **suspended for hitting 250k credits**. Investigation traced
  the burn to `idnes_main._geocode_fallback`: ~25% of iDNES listings are "page-less"
  (no embedded `"center":[lon,lat]`) and fall back to geocoding the locality via Mapy.
  The fallback had **no cache and no "already-placed" guard**, so every coords-less page
  re-geocoded on EVERY detail refetch — and the iDNES drain runs near-continuously. The
  price-stats dataset scraper was wrongly suspected; it uses sreality's own free
  `localities/suggest`, not Mapy. (Bazos barely geocodes — it reads coords off the page
  maps link — and already had a per-run cache; sreality never geocodes.)
- **Cheap + highest-impact fix (this PR):** `IdnesPortal.connect_drain` preloads, once on
  the main thread, the set of native ids that already have a `geom`
  (`db.native_ids_with_geom`); the worker-pool `fetch_detail` skips `_geocode_fallback`
  for any id in that set. Only genuinely-new and still-missing rows geocode, so a refetch
  never re-spends a credit on a stable coordinate. Cuts the dominant ~80% of iDNES geocode
  volume (the ~82k already-placed rows) at near-zero risk — coordinates are latest-wins and
  the locality string is stable.
- **Next (the better/reusable solution, parked):** a **persistent cross-portal locality→coords
  geocode cache** (a small table keyed on a normalized locality string, with negative/miss
  caching + TTL). It collapses the residual still-missing tail (the ~7.5k iDNES rows that
  never resolve still re-geocode every refetch under the cheap fix) AND dedups across runs
  and across listings/portals — replacing bazos's per-run `_CachingGeocoder` and serving the
  on-demand `source_dispatcher`/`scraper.geocoding` path too. Until then, the cheap guard is
  the safeguard against re-suspending the new key.

### 2026-06: Bazos cadence split (fix detail-drain starvation)
- Bazos was running its index walk + detail drain in ONE GitHub-Actions job. After
  its scope was expanded from 2 to 14 nationwide sections (byt/dum/chata/restaurace/
  kancelar/prostory/sklad × prodam/pronajmu), the full index walk alone grew to
  ~1500 pages / ~50 min and consumed the entire 50-min job timeout — every scheduled
  run was **cancelled mid-flight**, so the detail drain never ran. Result: ~16k house +
  commercial ads sat enqueued-but-never-fetched (0 active in DB despite the portal
  listing thousands), orphaned queue claims never reclaimed, and "stuck" scrape_runs.
  Apartment delisting inference also drifted (active counts above the portal total)
  because the cancelled walks couldn't reliably re-fire the throttled mark_inactive.
- Fixed by **mirroring the sreality/idnes cadence split (rule #19)** — no new pattern,
  no patchwork. `bazos_main` gained `--index-only` / `--drain-only` / `--max-seconds`
  (identical to idnes). `bazos_index_walk.yml` (every 6h, 75-min timeout) runs the full
  walk + enqueue + mark_inactive; `bazos_detail_drain.yml` (hourly, `--max-seconds 2400`
  budget so it finalizes cleanly) drains the queue across all categories. The combined
  flow stays in `scrape_bazos.yml` as a dispatch-only fallback for narrow ad-hoc runs.
  The persisted queue + 30-min `reclaim_stale_claims` mean the existing ~16k backlog
  drains over ~a day of hourly runs with no data loss and no manual surgery.
- **Next:** monitor the first ~24h of drains clearing the backlog; if bazos throughput
  proves too slow (polite 0.6 req/s + per-ad geocoding), consider a second concurrent
  drain shard or a faster rate once the portal tolerates it.

### 2026-06: "Recently added / changed" Browse filters (Status section)
- Two preset "last N days" pickers (today / 3 / 7 / 14 / 30) in a new **Status**
  ControlGroup on the Browse sidebar. **Recently added** filters on `first_seen_at`
  (the preset twin of the existing `first_seen_max_days`); **recently changed**
  filters on a new precomputed `properties.last_change_at` = the newest content
  snapshot across a property's children (migration 158 — snapshots are inserted only
  on a content-hash change, so it is the last *meaningful* edit, not a re-sighting).
  A live `max(scraped_at)` would blow the anon 3 s timeout, so it is precomputed and
  maintained by `recompute_property_stats` (dirty-set + daily sweep) alongside the
  other rollups, and exposed on `properties_public`.
- Registry-driven end to end: two `single_select` BROWSE-only filters in
  `toolkit/filter_registry.py` → regenerated `filterRegistry.generated.ts` →
  Map/Table/Cards hand-coded days-ago `.gte()` predicates in `queries.ts` →
  Stats via two new `browse_stats_properties` params (migration 159). BROWSE-only,
  consistent with the other first/last-seen date filters — the watchdog matcher
  (which already fires on new/changed listings) reports them as unsupported, and the
  estimation agent keeps its own freshness knobs.
- **Next:** optionally surface "last changed N days ago" on the listing-detail header;
  consider a watchdog "fire on any content change" channel if recency-on-alerts is wanted.

### 2026-06: Chrome extension — MF rent/yield across all portals + index overlays
- The extension grew from a sreality-detail-only yield panel into a multi-portal MF
  overlay. **Detail pages** on every scraped portal (sreality, bazos, bezrealitky, idnes,
  maxima, remax, +mmreality/ceskereality best-effort) show our precomputed
  `mf_reference_rent_czk` + `mf_gross_yield_pct` ("Výnos MF") for sale apartments, with the
  comparables estimation as the deeper tool; the panel is visibly deactivated for
  non-(byt+prodej). **Index/search pages** get per-card badges (Výnos MF, or a clickable
  "Odhadnout výnos" fallback) via anchor-href scanning — no per-portal card selectors.
- New bearer-gated backend read endpoint **`POST /listings/lookup`** (`api/portal_lookup.py`)
  maps a card's on-page `(source, native id)` → our row + MF figures + latest estimate,
  batched (≤50) for one request per index page. Closes the gap that the public views expose
  only `(source, sreality_id)`. `chrome-extension/src/portals.ts` is the registry
  (host→portal, detail-URL→native-id).
- **Next:** verify mmreality/ceskereality URL→id extractors + index card selectors once those
  portals carry data; consider badging not-in-DB sale-apartment cards (needs per-portal index
  category detection); optional Path-3 public build still unbuilt.

### 2026-06: Saved filter presets on Browse
- Named, reusable Browse filter presets, surfaced as buttons next to the Browse
  headline (`PresetBar`). Click a chip to restore *all* left-panel filters
  (`loadPreset` → atomic URL write); the active preset is tracked via a `preset`
  URL param (carried by `preserveExtras`) so editing a filter marks it dirty and
  reveals an **Update** button. Save / Update / Rename / Delete go through a new
  bearer-gated CRUD (`/filter-presets`, `api/filter_presets.py`, migration 151 →
  `filter_presets` table). The save dialog (`PresetSaveModal`) asks whether to
  include the current map area; dirty-detection ignores the viewport unless the
  preset stored one (`filtersEqualForPreset`).
- **Deliberately decoupled from Watchdog**: a preset stores the saved view
  blob and is restored client-side only — it never matches server-side, so it
  can't fire a notification and carries none of the watchdog firing machinery
  (cursor / is_active / dispatches). Reuses the watchdog *CRUD pattern* (route
  shape, `api.ts` client, react-query keys), not its table.
- **Sort is captured too.** `filter_spec` is now an opaque `{ filters, sort }`
  blob (`PresetSpec` + `readPresetSpec`); loading restores both filters and the
  sort order, and changing either marks the preset dirty. Backwards-compatible —
  presets saved before this read back with the default sort. No migration /
  backend change (the API already treats `filter_spec` as an opaque blob).

### 2026-06: Watchdog feed — Portal column
- New **Portal** column in the watchdog notification feed showing the portal the
  property was last seen on (`listings.source`), as a clickable chip that opens
  the listing on that portal — the stored `source_url`, else a reconstructed
  sreality URL from the native id (`portalListingUrl`), else the in-app listing
  view. Added `l.source` / `l.source_url` to the dispatch projection +
  `WatchdogDispatch` type; clicking marks the dispatch read like the listing
  link.

### 2026-06: Exclude-a-location district filter (Browse + Watchdog)

- A district chip can now be flipped from INCLUDE to **EXCLUDE** via a per-chip
  `−`/`+` toggle in `LocationTypeahead` — the chip turns red (brick token) with a
  leading minus and **subtracts** its matches from the cohort instead of requiring
  them. `DistrictChip` gained an optional `excluded` flag
  (`frontend/src/lib/filters.ts` + the Pydantic mirror in `api/notifications.py`).
- **Consistency (rule 16)** across all four sites that encode "what a district chip
  means": Browse Map/Table/Cards (`queries.ts` builds
  `and(or(<include>), not.or(<exclude>))`), Browse Stats (`browse_stats_properties`,
  migration 146 — new `districts_excluded_filter boolean[]` param, include/exclude
  gates), and the Watchdog matcher (`_build_match_clauses` appends a `NOT (...)`
  group). The same `LocationTypeahead` renders in both Browse and Watchdog, and the
  flag round-trips through the URL (`districts_excl`) and the watchdog `filter_spec`
  JSONB with no migration on `notification_subscriptions`.
- Verified live: Praha include (10,691) + Praha exclude (42,190) = 52,881 (all
  byt/prodej) — an exact partition.

### 2026-06: Fast city-proximity filters + Min Population fix

- **Bug:** the Min Population filter returned **zero** results. It routed through
  `listings_with_city_quality` (curated-city `ST_DWithin` on centroids →
  `.in(ids)` allowlist), which exceeds the anon 3 s `statement_timeout` and falls
  back to an empty list. Broad city-quality filters were impractical for the same
  reason.
- **Fix — precomputed columns (migration 142):** replaced the per-request spatial
  RPC with indexed columns on `properties`, filtered as plain `>= value`:
  `home_obec_pop` (the listing's OWN municipality population, nearest obec polygon,
  country-wide — backs Min/Max Population for *every* listing) and
  `near_{pop,jobs,youth,overall}_{5,15}km` (MAX metric within a FIXED 5/15 km,
  **polygon-edge** distance; radius fixed, threshold dynamic; all AND-combinable).
  Population proximity uses obce ≥ 10k; index proximity the 206 curated cities
  (`pracovni_mista`/`stehovani_mladych`/`celkove_hodnoceni`).
- **Recompute:** `recompute_city_proximity()` spatial-joins each property against a
  ~215-row GiST-indexed anchor set (~2 ms/property); `recompute_city_proximity.yml`
  hourly (incremental) + `--full` after a data load. Mirrors
  `recompute_mf_gross_yields` (migration 133). Combined filter query: **~155 ms**
  (BitmapAnd over partial indexes) vs the old timeout.
- **Population for all obce (#317):** `admin_boundaries.population` now carries every
  obec (ČSÚ DataStat OBY02AT02, `scripts/load_obec_population.py`), not just the 206
  curated cities — what `home_obec_pop` + pop proximity need.
- **Consistency (rule 16):** Browse Map/Table (registry auto-dispatch), Stats
  (`browse_stats_properties`, migration 143), Watchdog
  (`_city_quality_clauses` + spec) all share the definition. Verified: Praha 1.4M;
  Kuřim sees Brno (404k) within 5 km via polygon edge; Jeseník isolated.

#### Next

- A radius toggle (5↔15 km) per proximity metric instead of two separate inputs,
  if the operator finds the doubled controls noisy.

### 2026-06: Watchdog feed — rent estimate + MF-yield column
- The per-row action is now **"Estimate rent"** and always runs a **rental**
  estimate, even for a sale listing — `kickoff_estimation_for_dispatch` forces
  `estimate_kind='rent'` and a `category_type='pronajem'` comparable cohort
  (previously it mirrored the subject's category, so a sale listing produced a
  sale-price estimate). The operator gets "what would this flat rent for", the
  input to a yield read.
- New **MF yield** column in the feed, beside the comparables-based estimation
  yield: the deterministic Ministry-of-Finance reference gross yield already
  carried on the listing (`listings.mf_gross_yield_pct`, migration 133), added
  to the dispatch projection and surfaced read-only (sale apartments only;
  "—" otherwise).

### 2026-06: Create watchdog from Browse + fix Run-estimation kickoff
- **Create watchdog from Browse.** A "+ Create watchdog" button next to the Browse
  headline saves the current filter set as a watchdog after a name-prompt dialog
  (`CreateWatchdogModal`). `filtersToWatchdogSpec` (frontend/src/lib/filters.ts)
  maps every Browse filter the matcher honours — category, dispositions, district
  chips, price / price-per-m² / MF-yield / area bounds, tri-state amenities,
  furnished/ownership/portals/condition, condition-level mins, the price-history
  mins (price-drop count, **max price-drop %**), and all city-quality predicates
  (index rules, **population min/max**, **near-city proximity**). center+radius →
  lat/lng/radius_m. Browse-only filters the watchdog matcher has no clause for
  (status, date ranges, map viewport, building material, garden area, tags) are
  reported in the dialog so the operator isn't surprised. The new watchdog appears
  in the Watchdog feed / Manage list like any other.
- **Fix: "Run estimation" on the watchdog feed did nothing.** `_insert_pending_run`
  INSERTed into `estimation_runs.category_main/category_type`, columns that don't
  exist → the endpoint 500'd and the button silently reverted (no `onError`). Moved
  category into `input_spec` jsonb (already read back by `run_pending_estimation`),
  added an `onError` alert, and a regression test asserting the INSERT never names
  the phantom columns.

### 2026-06: MF gross-yield Browse filter

- **What:** a derived `listings.mf_gross_yield_pct` (MF reference rent × 12 / asking price)
  on every sale apartment, filterable in Browse + Watchdog as a "from/to %" range. Builds on
  the MF Cenová mapa store below.
- **Compute (migration 133):** `recompute_mf_gross_yields()` set-based SQL (PIP territory →
  rent-map join → ÷ price), backfilled (31,348 rows, median ~3.5%). A `< 100 000` CZK sale-price
  floor drops placeholder / rent-magnitude prices mis-tagged `prodej` (which gave absurd %) while
  keeping genuine high-yield deals. Runs hourly (`recompute_mf_yields.yml`) + after each rent-map
  ingest.
- **Filter:** `min/max_mf_gross_yield_pct` in `filter_registry` (`_UI_AGENDAS`, float range);
  exposed on `listings_public`/`properties_public`; Map/Table auto-dispatch, Stats RPC + Watchdog
  matcher + `ComparableFilters` all carry it.

#### Next

- Watchdog yield-band alert presets; a "sort by yield" column on the Browse table.

### 2026-06: Secondary rent reference — MF Cenová mapa nájemného

- **What:** every rental estimate now carries a second, independent reference figure from the
  Ministry of Finance's quarterly *Cenová mapa nájemného* (hedonic-model reference rent per
  territory), shown alongside the comparables-based primary estimate (never overrides it).
- **Data model (migrations 131/132):** `estimation_runs.reference_rent jsonb` + history-tracked
  `rent_map_revisions` / `rent_map_values` / `rent_map_adjustments` (latest-revision-wins
  `*_public` views, the curated-cities pattern) + a materialized `rent_map_choropleth` for the map.
- **Join:** the spreadsheet's `Kód obce` IS the ČÚZK/RÚIAN code = `admin_boundaries.id` (all 7,630
  codes verified — 1,582 ku + 6,048 obec, no collision); `toolkit.rent_map.compute_reference_rent`
  resolves the subject's lat/lng to its territory by PIP and applies VK + amenity adjustments
  (novostavba variant for new builds). Read-only — not a new toolkit write exception.
- **Ingest:** stdlib XLSX parser (`zipfile`+`xml.etree`, no `openpyxl`); monthly auto-grab
  (`fetch_rent_map.yml` → `scripts.fetch_rent_map`, scrapes the MF infografika page) + manual
  upload / fetch-now from Settings (`POST /admin/rent-map/*`), `file_sha256`-deduped.
- **Surfaces:** Estimation Detail block, Chrome-extension panel line, `/estimations` +
  `/estimate_yield` payloads, and a Browse map choropleth (VK1–VK4 radio + Kraje overlay + Kč/m²
  legend, reproducing the official MF map).

#### Next

- Switchable older/novostavba toggle on the map + an as-of revision picker for historical
  comparison (the revision history is already stored).

### 2026-06: Dedup engine rebuilt — street + disposition keyed, room-aware visual

- **What:** replaced the geo-proximity matcher (the inline Tier-1 `ST_DWithin` probe in
  `scraper/db.py` and the batched spatial straggler-attach in `recompute_property_stats.py`)
  with a street + disposition keyed engine (`toolkit/dedup_engine.py` pure rules +
  `scripts/dedup_engine.py` orchestrator, `dedup_engine.yml` daily). Rules A–E: (A) only
  listings with BOTH a street and a disposition are eligible (computed inline, partial index,
  migration 127); (B) same street + house number + disposition + floor → auto-merge, 5% area
  guard; (C) same street + disposition → visual candidate unless a hard floor / >20%-area /
  house-number contradiction; (D) layered visual — ≥2 near-identical interior photos (pHash,
  facade/floor-plan excluded), else a room-aware forensic comparison (operator prompt) on like
  rooms in priority order, stop at first High; (E) the rest queue on `/dedup`.
- **New cached LLM tools** (write-allowed, toolkit rule #5): `classify_listing_images`
  (migration 128, room taxonomy) and `compare_listings_visually` (migration 129, forensic
  same-property verdict). Operator prompts seeded into `app_settings`.
- **Automation dashboard:** `dedup_engine_runs` (migration 130) + public view feed a new
  "Engine activity" section on `/dedup` — eligibility breakdown, per-run auto-merge counts by
  path (address / photos / visual) vs queued, and a trend. The review card now also shows the
  engine's visual verdict + rationale for queued pairs.
- **Retired** `dedup_sweep.py` / `dedup_sweep.yml`. Merges stay reversible (the
  `property_merge_events` ledger + one-click Undo).
- **Why:** street + disposition is the identifier the operator trusts; geo proximity merged
  the wrong things and missed cross-portal pairs that geocode differently. Same-development
  units (same street + disposition) are exactly what the room-aware visual layer disambiguates.

### 2026-05: Dedup — image-identity auto-merge + street parse + review-card UI

- **What (A):** an image-identity rung in the Tier-2 sweep (`scripts/dedup_sweep.py`)
  — near-identical cross-portal photos (pHash ≤4 or vision ≥0.9) auto-merge a pair
  *without* the tight 30 m geo demand, since two portals geocode one flat tens of
  metres apart. `corroborator='image'`, `reason='tier2_image'`, reversible like every
  auto-merge. `dedup_sweep.yml` gains a small default vision budget (50) to settle
  pairs pHash can't compare yet.
- **What (B):** parse sreality's structured street address (migration 122) — typed
  `street`/`house_number`/`zip`/`street_id` on `listings`, extracted by the parser
  from the rich `locality` shape and persisted via both write paths;
  `listings_public` exposes street + house_number. Populates for detail-fetched
  sreality rows (locality shape is mixed: index-only rows stay null). For geo/UI and
  a future exact-address rung.
- **What (C):** rebuilt the `/dedup` review card — a shared `ImageCarousel` (extracted
  from Browse cards), per-side photo sliders, named portal chips (linking to the
  portal page or our listing) replacing the bare "N sites" count, and a center ✓/✗
  comparison table (price/area/disposition/street+no/floor/district/distance) from a
  pure, unit-tested `diffCandidate`. No API change — anon public views, batched per
  card set.
- **Why:** more pairs auto-merge so the review queue stays small, and the pairs that
  do need a human decision now show *why* they might match — photos, which portals,
  and an attribute-by-attribute verdict.

### 2026-05: "Filter by portal" across agendas
A `portals` multiselect in the canonical filter registry (`toolkit/filter_registry.py`,
`agendas=_ALL_AGENDAS`, `pg_column='source'`, enum = the scraper portals from the
`portals` table) so the same filter works on Browse, Watchdog, the estimation/agent
comparable surfaces, and the Settings visibility matrix — wired the usual way
(ComparableFilters + `_shared_filter_where`, `WatchdogFilterSpec` + `_build_match_clauses`,
the four estimation/velocity input schemas, the agent override fields, and the regenerated
frontend registry → auto-dispatched `.in('source', …)` on `properties_public`). Migration
118 exposes `source` on `properties_public` (the representative listing's source — also the
Browse card's new "portál" label next to first/last-seen) and adds a `portal_filter` arg to
`browse_stats_properties` so the Stats tab honours it too. On-demand URL-parser sources
(`idnes_reality`, `remax`) are intentionally not offered — they never produce `listings`
rows. Extend `PORTAL_OPTIONS` (and regenerate) when a new ingesting portal lands.

### 2026-06: RE/MAX scraper (portal 7, pilot)
The seventh portal onto the shared Phase-4 framework — again "a fetcher + a parser +
a config row", no per-portal branches. remax-czech.cz is a national franchise
catalogue (~7,900 listings) of STRUCTURED server-rendered HTML, so `remax_parser.py`
is deterministic (no LLM): the search cards carry `data-url`/`data-price`/`data-gps`/
`data-title`, and the detail page is a `pd-detail-info__row` → `__label`/`__value`
spec block + a clean integer `data-advert-price` + per-listing `data-gps` (DMS →
decimal, CZ-bbox-guarded, no geocoding) + a `mlsf.remax-czech.cz/data//zs/{id}/`
gallery (the `_th350` thumbnail strips to the full-resolution original). Typed fields
normalise to the canonical sreality labels (`Cihlová→cihla`, `Velmi dobrý→velmi_dobry`,
`Osobní→osobni`). `RemaxClient` (`scraper/remax_client.py`) subclasses
`BasePortalClient` (HTML `Accept` + the `?sale={1,2}&stranka=N` index + the
`/reality/detail/{id}/` detail URL builders + a redirect-off-detail gone signal);
`RemaxPortal` (`scraper/remax_main.py`) implements the runner seams. Like maxima, the
index is TWO mixed agendas (sale=1 prodej / sale=2 pronájem) with no per-category URL,
so each descriptor pairs a category with its offer-type flag and `walk_category` walks
that agenda once (cached) and keeps the title-derived slice for its category (real
(cm, ct) Health-reconciliation labels); the drain re-derives each listing's category
from the detail "Typ nemovitosti" + title verb. Shipped as a **pilot**
(`supports_complete_walk=false`): remax reports a per-AGENDA total and the per-category
slice is title-derived, so a safe per-(cm,ct) completeness check isn't available — the
runner never flips listings inactive from index absence (rule #3); a gone detail still
flips that one. **Registered by CONVERTING the existing on-demand-parser `portals` row
to a scraper (migration 135)** — the LLM URL parser (`source_kind='remax'`, estimation
preview) is a separate entry point and keeps working, routed by domain in
`source_dispatcher` independent of the row's `kind`. One job runs both phases
(`scrape_remax.yml`, every 6h), drain bounded by `max_detail_per_run` + a
`--max-seconds` budget so the backlog drains over several ticks. Also **added `remax`
to `PORTAL_OPTIONS`** (it now ingests `listings` rows — the pending follow-up from the
"Filter by portal" entry above) and regenerated the frontend registry.

#### Next
- Promote to `supports_complete_walk=true` once the pilot proves stable. remax exposes
  per-category index URLs (`/reality/byty/?sale=N` with their own per-category totals),
  so a future migration could walk those for a provable per-(cm,ct) completeness check
  + delisting sweep (the idnes posture), replacing today's title-derived slice.
- Refresh the `remax_sample.html` fixture so the on-demand-parser real-fixture test lights up.

### 2026-05: M&M Reality scraper (portal 6, pilot)
The sixth portal onto the shared Phase-4 framework — again "a fetcher + a parser +
a config row," no pipeline divergence. M&M Reality is server-rendered HTML, but
**every detail page embeds a complete structured estate object** as a Vue
`:property` prop (HTML-entity-encoded JSON), so `mmreality_parser.parse_detail`
**decodes that JSON** rather than scraping markup: precise per-listing coordinates,
typed condition/construction/ownership/energy, area, floors, and images all come
from one object (no `<dl>` table, no geocoding). Typed fields are normalised to the
canonical sreality labels (`smíšená→smisena`, `velmi dobrý→velmi_dobry`,
`Družstevní→druzstevni`, `2+1`) for cross-portal filter/dedup agreement.
`MmRealityClient` (`scraper/mmreality_client.py`) subclasses `BasePortalClient`
(HTML `Accept`, `/nemovitosti/{id}/` URL builders, removed-listing redirect
signal); `MmRealityPortal` (`scraper/mmreality_main.py`) implements the runner
seams. The index is a **single mixed-category feed** (`/nemovitosti/?page=N`, no
per-category slice) and each listing's category is read from its own detail JSON,
so one config descriptor walks everything. Because a single mixed walk can't be
gated per-(category_main, category_type) the way the source-scoped `mark_inactive`
requires, it is **`supports_complete_walk=false`** (the bazos posture): the runner
never flips its listings inactive from index absence (rule #3) — delistings surface
via a gone detail fetch (immediate per-listing flip) + the "active = seen within 7
days" rule. Registered as a scraper portal (migration 117, `source='mmreality'`,
sort 35, pilot, 6h cadence). Scheduled + manual via `scrape_mmreality.yml` (combined
index-walk → detail-drain in one job, bounded by `--max-pages` / `--max-detail`; the
`--index-only`/`--drain-only` split flags exist for a cadence-split backfill).
### 2026-05: Maxima Reality crawler (portal 5, pilot)
Another portal onto the shared Phase-4 framework — a fetcher + a parser + a config
row, no pipeline divergence. Maxima is a single real-estate agency that publishes its
whole catalogue (~220 listings) as ONE server-rendered WordPress index (no JSON API,
**no per-category URL**) at `nemovitosti.maxima.cz`. `MaximaClient`
(`scraper/maxima_client.py`) subclasses `BasePortalClient` (HTML `Accept` + the
`/page/N/` index + `/nemovitosti/{id}/` detail URL builders). `maxima_parser.py` parses
the structured spec `<table>` (`th.slider_label`/`td.slider_value`), a clean `div.price`,
and precise per-listing coordinates from the embedded OpenLayers map config
(`\"center\":[lon,lat]`, backslash-escaped in the page source). Typed fields normalise to
the canonical sreality labels (`Cihlová`→`cihla`, `Osobní`→`osobni`) for cross-portal
filter agreement. Because there is no per-category URL, the **category is derived per
listing** from its native-id prefix (b=byt, d=dum, f=pozemek, g=komercni, o=ostatni) +
the title verb, so one mixed-catalogue config walks every category. `MaximaPortal`
(`scraper/maxima_main.py`) implements the runner seams and drives index-walk →
detail-drain via the one `portal_runner`. Shipped as a **pilot**
(`supports_complete_walk=false`, migration 116, `source='maxima'`, sort 26): the runner
never marks listings inactive from index-absence (a gone detail still flips that one
listing); the whole-catalogue walk IS complete, so promotion is a later migration (as
bazos got in 113). One job runs both phases (`scrape_maxima.yml`, every 6h) since the
catalogue is small.

**Follow-up (migration 120): rent agenda + per-category labels.** The first cut only
walked the default (sale) view and missed the ~34 rentals behind the buy/rent toggle
(`?af=2`), and labelled everything `null·null` (one placeholder category) so the Health
reconciliation couldn't join. Fixed by making the config descriptors per
(category_main, category_type, **af**): `walk_category` walks each agenda (sale af=1,
rent af=2) once — agenda-cached so the pages are fetched a single time — and keeps the
slice for its category. Category is derived **title-first** (`maxima_parser.category_of`,
shared by the index walk and `parse_detail`) because the rent agenda's native ids carry
prefixes the sale taxonomy (b/d/f/g/o) doesn't cover; a prefix-only derivation would
dump every rental into `ostatni` and fragment the reconciliation.

#### Next
- Promote to `supports_complete_walk=true` once the pilot proves stable. Note the
  completeness signal is per-AGENDA (maxima reports a total per af, not per category),
  so the promotion needs an agenda-level completeness check, not the per-(cm,ct) default.

### 2026-05: Per-portal operational limits in config (PR A of the Scrapers-admin track)
Made the per-portal operational limits (index/detail rate, workers, per-run caps,
image limits) operator-tunable from the DB — the foundation for a
Scrapers admin dashboard (PR B next). (The `min_completeness` knob shipped here too
but was removed shortly after — see the 2026-05 "Completeness is always 100%" entry
below; completeness is a safety invariant, not a tunable.) Migration 107 had deliberately kept these
knobs out of the registry ("per-run CLI tuning, not portal identity"); this reverses
that for the limit knobs, by operator request, since they vary a lot per portal (6
req/s JSON API vs 0.6 req/s HTML crawl) and the operator wants to tune them without a
deploy. Migration 114 adds `portals.operational_limits jsonb` (+ a `portal_limits_history`
trigger mirroring `app_settings`) and a global default layer in
`app_settings.scraper_limits_global`. `scraper/portal.py` grows a `PortalLimits`
dataclass + a deep-merge in `load_portal_config` (baked default < global < per-portal);
all four scraper mains (`main`, `idnes_main`, `bazos_main`, `bezrealitky_main`) resolve
each limit as **CLI override > per-portal DB > global DB > code default**. Seeded with
today's production values + baked code defaults matching today's argparse defaults, so
it is **zero behavior change** (production workflows still pass their CLI flags → CLI
wins). PR B adds the operator surface: `GET/PUT /admin/portals/{source}/limits` (+
`GET /admin/portals`) mirroring the `app_settings` admin pattern (writes flow through the
history trigger; server-side range validation → 400 on bad shape), and a **Scrapers**
dashboard page (`frontend/src/pages/Scrapers.tsx` + nav) with one editable card per
registry portal plus a Global-defaults card — blank field inherits the global, edits
apply on the next scrape with no redeploy. Cadence (cron) stays in code for now.

### 2026-05: Completeness is always 100% (mark-inactive safety invariant)
Removed the operator-tunable `min_completeness` scrape limit and hardcoded the
completeness bar that gates `mark_inactive` at **100%** in every complete-walk portal
(`INDEX_MIN_COMPLETENESS = 1.0` in `main` / `bazos_main` / `idnes_main` /
`bezrealitky_main`). A listing is only inferred delisted after a FULL index walk
(architectural rule #3) — never falsely delist a live listing — so this is a safety
invariant, not a knob. (The knob was never actually read by the walk anyway; it used
the module constant.) Dropped the field from `PortalLimits`, the `/admin/portals/*`
API, and the Scrapers dashboard; migration 125 strips the dead `min_completeness` key
from `scraper_limits_global` and every `portals.operational_limits`.

### 2026-05: Health dashboard — per-portal ledger
Restructured the Health page from a flat data-source grid + sreality-only global
panels into a **registry ledger**: one expandable record-card per portal, each with a
roll-up status dot (worst of its checks) and three nested disclosures — listings-by-
category reconciliation, per-pipeline scrape health checks, and pipeline schedule.
Portals **group by canonical host**, so iDNES's scraper-pilot + on-demand-parser facets
fold into one card (fixing the duplicate "iDNES Reality" tiles). Backend:
`scraper_health_checks(p_source)` is now parameterized per source (migration 111;
listings-based checks gained a source filter, so `listings_public` exposes `source`),
with a fetch-failures count fix (migration 112). Also fixed a statement-timeout
regression in `image_storage_overview()` from migration 109 — the active-listing counts
did a second 1.3M-row join; now derived from the per-category sums in one join
(migration 110). Pilots (bazos, bezrealitky) get a compact reconciliation from their
latest run; never-started scraper pilots read "idle", not false-red.

### 2026-05: iDNES Reality crawler (portal 4, pilot)
Another portal onto the shared Phase-4 framework (after bezrealitky) — proof that
a new portal is "a fetcher + a parser + a config row," no pipeline divergence.
iDNES is an **HTML crawler** (like bazos, unlike the JSON-API bezrealitky). `IdnesClient`
(`scraper/idnes_client.py`) subclasses `BasePortalClient`, adding only the HTML
`Accept` header, idnes URL builders, and removed-listing signals (404, a redirect
off `/detail/`, body markers). `idnes_parser.py` parses the structured portal —
the `<dl>` spec table, a clean price element, and precise per-listing coordinates
straight from the page map config (`"center":[lon,lat]`), so there is no geocoding
step; typed fields are normalised to the canonical sreality labels
(`panelová→panel`, `velmi dobrý stav→velmi_dobry`, `osobní→osobni`) for
cross-portal filter agreement. `IdnesPortal` (`scraper/idnes_main.py`) implements
the runner seams and drives index-walk → detail-drain via the one `portal_runner`.
**Complete-walk** (migration 111): search pages carry a result total and have no
deep-pagination cap, so — like bezrealitky, unlike bazos — `supports_complete_walk=
true` and the runner marks delisted listings inactive under the completeness guard,
source-scoped (rules #3/#15). The detail URL carries the category
(`/detail/{sale}/{cat}/…`), so the drain derives each listing's category from its
own URL — one `portals`-row config walks **many categories** (byty + domy × prodej +
pronájem). Registered as a scraper portal (migration 110, `source='idnes'`, sort 25)
parallel to bazos; the pre-existing `idnes_reality` on-demand parser row stays (the
Health card shows both badges). Because iDNES is large (~2400 index pages, ~60k
listings), the pipeline is **cadence-split like sreality** (rule #19): a full index
walk (`idnes_index_walk.yml`, `--index-only`, every 6h — completes + marks inactive +
enqueues) feeds a bounded detail drain (`idnes_detail_drain.yml`, `--drain-only`, every
2h); a combined run can't do both in one job (the full index eats the window).
`scrape_idnes.yml` is the dispatch-only combined fallback. The queue persists, so the
first ~1-2 days drain the ~60k backlog, then steady-state. Images: the drain records
URL rows; the shared `images.yml` downloads bytes to R2. Validated live: ingest +
100% property-linking + coords + categories work; the price-overflow crash is fixed.

### 2026-05: Bezrealitky scraper (portal 3 on the shared framework)
The first portal onboarded purely as a fetcher + parser + config row — proving
the Phase 4.0 framework holds with no per-portal branches in shared code.
Bezrealitky is a **JSON-API portal** (public GraphQL at `api.bezrealitky.cz`),
so it mirrors sreality, not the bazos HTML crawler: `bezrealitky_client.py` pages
`listAdverts` (index) + reads `advert(id)` (detail) over GraphQL (the shared
`BasePortalClient._request` gained POST support — backwards-compatible); the API
needs browser-like `Origin`/`Referer` headers. `bezrealitky_parser.parse_advert`
maps the advert object onto the shared `ScrapedListing`, translating bezrealitky's
enums into the **same canonical labels sreality stores** (verified against the live
table) so cross-source filtering/dedup/condition-scoring see one vocabulary; coords
come from the API's `gps` (precise, no geocoding). `BezrealitkyPortal` is
complete-walk capable (GraphQL `totalCount` + no deep-pagination cap), so unlike
bazos it marks delistings inactive under the completeness guard, **source-scoped**.
Because the detail JSON carries offerType/estateType, the drain derives category
from the response — one config walks many categories (byt/dum × prodej/pronájem to
start; `includeImports:false` = bezrealitky's own private-seller inventory). New:
`db.index_summary_native` (price-change refetch + PK resolution by
`(source, source_id_native)`), migration 110 (promote the `portals` row to
`kind='scraper'` + operational config), `scrape_bezrealitky.yml` (6-hourly + dispatch).
The on-demand LLM URL parser (`source_parsers/bezrealitky.py`, estimation preview)
is a separate entry point, unchanged.

### 2026-05: Health dashboard accuracy (post-split truth)
Made the Health page tell the truth about the index/detail-split pipeline and
fixed a cross-portal data bug it surfaced. (1) **Bazos no-progress bug:**
`db.mark_inactive` scoped only by category, so every sreality index walk swept
bazos rows (same canon categories, never in sreality's `seen_ids`) to
`is_active=false` — bazos showed 0 active. Now **source-scoped** (`db.mark_inactive`
/ `db.active_count`), enforcing rule #15; the mis-flipped rows are reactivated by a
one-off backfill after the fix deploys. (2) **Apparent "huge drift"** was the
un-drained detail-queue backlog, not data loss — the index walk collects ~100% of
sreality's listings. Migration 109 splits the old `count_reconciliation` check into
**`index_completeness`** (collected vs sreality total — did we SEE every listing) and
**`detail_queue_backlog`** (seen-but-not-fetched, via a new `listing_detail_queue_public`
view), and `detail_drain.yml`'s per-run cap rose 6000→12000 so a run uses its full
50-min window to clear deep backlogs (rate/politeness unchanged). (3) The Count-
Reconciliation panel and the 6 per-category tiles merged into **one unified per-category
table** (Active / sreality / Collected / Index% / Queue / new14d / flipped7d / failed).
(4) Recent-scrapes table caps at 15 rows with a show-all toggle. (5) **Image mirror**
gains active-listing columns + a closeable-gap bar (`image_storage_overview()` adds
`total_active`/`stored_active`) — the active gap is recoverable; inactive photos are
mostly CDN-expired. (6) The **Schedule** tile is now data-driven from
`workflowDocs.generated.ts` (all scheduled scrapes + maintenance jobs), replacing two
hardcoded, stale entries.

### 2026-05: Per-portal Health dashboard
The Health page now opens with a **Data sources** catalogue — one register
entry per portal (sreality, bazos, bezrealitky, idnes, remax), each showing
the metric that fits its kind: active-listing + scrape-run stats for the
scrapers, on-demand parse activity for the URL parsers. Backed by migration
100 (`portals` registry + `scrape_runs.source` + a `portal_health_summary()`
RPC over anon-readable aggregate views `portal_listing_counts` /
`parsed_url_activity`); adding a portal is one INSERT into `portals`, no code
change. The bazos crawler now records its own `scrape_runs` row
(`source='bazos'`, `run_type='delta'`), so the pilot surfaces on the dashboard
the moment it runs; the Recent-scrapes table gains a per-run Site column.

### Maintenance 2026-05: sreality v1 API migration
sreality rebuilt on Next.js and removed the old `/api/cs/v2/estates` API
(returned 404 from every GitHub IP). A free runner-IP probe confirmed it
was an endpoint removal, not an IP block. Rewrote `scraper/sreality_client.py`
(now `/api/v1/estates/search` + `/api/v1/estates/{id}`, offset/limit paging,
`locality_country_id=112`) and `scraper/parser.py` (new estate-object shape)
against the same row + snapshot contract — listing IDs are unchanged so
history is preserved. Updated `scraper/hashing.py` for the new volatile
fields. Reverted the temporary anti-block request-volume backoff.

### Phase 1: Scraper
Daily index + on-demand detail scrape of sreality.cz. Image mirroring to
Cloudflare R2. Failure tracking with give-up threshold. Two-mode GitHub
Actions workflow (conservative cron, opt-in aggressive bootstrap).

### Phase 1.5: Six-category coverage
`CATEGORIES` in `scraper/main.py` walks all six byt / dum / komercni ×
pronajem / prodej pairs in sequence. Per-category refetch cap so a
flooded sale category can't starve the rental walk. `category_type_cb=4`
maps to `'podil'` (fractional ownership). PRs #30, #31. Houses and
commercial listings now accumulate in the database alongside apartments.

### Phase 2: Toolkit foundation
Pure-function analytical tools over the existing schema, exposed as a
FastAPI service deployed to Railway.
- `find_comparables`: parameterised spatial+attribute search.
- `analyze_distribution`: descriptive stats over a cohort.
- `/estimate_yield`: composite endpoint with confidence and warnings.

### Phase 2.5: Freshness layer
Audit trail and on-demand verification.
- `verify_listing_freshness`: throttled re-fetch + snapshot diff.
- `compare_snapshots`: per-listing evolution analysis.
- Snapshot IDs and data-age statistics in the `/estimate_yield` response.

### Phase 3a: Neighborhood, outliers, security
- `describe_neighborhood`: dispositional/price/condition profile with
  trend.
- `find_distribution_outliers`: outlier detection with cross-referenced
  reasons.
- API auth via `API_TOKEN`.

### Phase 3b: Velocity
- `compute_market_velocity`: TOM stats and trend for a filtered cohort,
  with active/delisted/all population control.
- `compute_listing_velocity`: percentile and classification
  (fast/typical/slow/stuck) of a single listing within its peer cohort.
- Shared `_shared_filter_where` helper extracted from `find_comparables`
  so spatial+attribute filter semantics live in one place.

### Phase 4a: Spatial context — anchor amenities
- `find_anchor_amenities`: OSM POI lookup with local cache mirror in
  the `amenities` + `amenity_fetches` tables (cache-key = category +
  radius + center + TTL). Live behind the API; one of the two
  toolkit write-allowed exceptions per CLAUDE.md.

### Phase estimation-4: Generic URL parser
Cross-listed under the UI track for the full detail. Headline:
migration 020, `api/llm_client.py`, `scraper/source_dispatcher.py`,
per-source parsers (`bezrealitky`, `idnes_reality`, `remax`,
best-effort `generic`), 7-day URL cache, daily cost soft-warning.

### Phase estimation-5: URL-parser frontend
`ConfidenceIndicator`, `previewListingUrl`, `useUrlPreview`, listing
block + `force_refresh` + `cost_usd_total` surfacing on `/estimate`.
Commits `e9da41f`, `65b9967`, `d66da7e`.

### Phase 5: Statistical refinement
Two pure-Python analytical toolkit functions, both prerequisites for the
Phase 7 reasoning agent. Stdlib-only (no sklearn/numpy) per CLAUDE.md
"prefer the stdlib" rule.
- `cluster_comparables` (`toolkit/clustering.py`): k-means submarket
  detection over a listings cohort. Stateless — takes the listings
  list returned by `find_comparables` (or any compatible shape).
  Z-score normalises each axis so multi-axis runs aren't dominated by
  absolute scale, runs Lloyd's algorithm with `n_restarts` deterministic
  seeds, picks the lowest-inertia result, de-normalises centroids back
  to original units. Axes: `price_per_m2`, `price_czk`, `area_m2`,
  `distance_m`. Returns clusters sorted by size desc with per-axis
  min/median/mean/max statistics and the list of `sreality_ids` in
  each.
- `find_comparables_relaxed` (`toolkit/comparables.py`): auto-widening
  wrapper around `find_comparables` with full provenance. Runs the
  strict query first; if `result_count < min_results` walks a
  deterministic ladder of relaxations (`radius_x1.5` →
  `area_band_+0.10` → `disposition_loose` → `radius_x2` →
  `area_band_+0.20` → `disposition_any` → `drop_condition` →
  `drop_building_type` → `drop_energy_rating` → `drop_floor_band`)
  until the cohort hits `min_results` or the ladder is exhausted.
  Locality, category, price bounds, and `active_only` are never
  relaxed — they encode user intent. Each intermediate step is
  recorded in `data.relaxation_trace` with the action name, full
  filters snapshot, and resulting count. Caller can override the
  ladder.
- Two new POST endpoints `/tools/cluster_comparables` and
  `/tools/find_comparables_relaxed`, bearer-token-gated. The cluster
  endpoint takes no DB connection (stateless).
- No `estimate_yield` auto-fallback — both tools are standalone, the
  Phase 7 agent opts in. Existing deterministic estimation trace
  remains unchanged.

### Phase 6: Visual layer
Two LLM-backed analytical toolkit functions for the Phase 7 agent:
- `summarize_listing` (`toolkit/summaries.py`): structured Claude
  summary of a single listing snapshot — `headline`,
  `key_highlights`, `concerns`, `condition_assessment`,
  `target_audience`. Cached in `listing_summaries` keyed on
  `(sreality_id, snapshot_id)`; auto-invalidates when content
  changes (new snapshot → new key).
- `compare_listing_images` (`toolkit/image_similarity.py`):
  pairwise visual similarity via Claude vision, scored across six
  fixed tenant-relevant dimensions (`exterior`, `kitchen`,
  `windows_and_light`, `floor_finish`, `lighting`, `styling`) plus
  an `overall_similarity` rollup. Image bytes pulled from R2
  server-side via boto3 GetObject, base64-encoded into the vision
  payload. Cached in `listing_image_comparisons` keyed on the
  canonical-ordered pair.
- Migration 027 adds the two cache tables, extends
  `llm_calls.called_for` with `'compare_listing_images'`, and seeds
  `app_settings` with the operator-tunable system prompts and model
  IDs (`llm_summary_*`, `llm_image_compare_*`).
- New POST endpoints `/tools/summarize_listing` and
  `/tools/compare_listing_images`, bearer-token-gated.
- CLAUDE.md toolkit rule #5 grows from two to four write-allowed
  exceptions (same rationale as `find_anchor_amenities`'s OSM
  mirror: the LLM is the source of truth, we cache locally to keep
  repeat lookups fast and Anthropic-friendly).

### Phase 4b: Spatial context (tenant-perspective overlays)
Two narrow toolkit functions on top of the OSM amenity + transit
caches.
- `compute_walkability` + `compute_amenity_supply`
  (`toolkit/walkability.py`): both project the POI cohort returned
  by `find_anchor_amenities` onto a different signal. Walkability is
  a single 0-100 score driven by weighted nearest-POI distance.
  Supply is the per-category count expressed as a ratio against a
  target count, bucketed `scarce|adequate|abundant`. Two facts, two
  tools, the agent picks. Hermetic tests mock the amenity delegate
  so the math is exercised without an OSM round-trip.
- `find_comparables_along_axis` (`toolkit/transit_axis.py`):
  comparables in a corridor along a tram / subway / bus route. Two-
  stage spatial filter — first find route relations passing within
  `anchor_radius_m` of the target, then return listings within
  `corridor_m` of any of those routes. Reuses the shared comparables
  attribute filters; replaces the anchor-circle ST_DWithin with the
  corridor join. Per-listing output names the nearest line and
  distance to it.
- Migration 028 adds the `transit_lines` + `transit_line_fetches`
  cache tables (one row per relation/way pair, sha256
  bbox+transport_types cache key). The Overpass client gets a
  `fetch_routes` method that parses route relations into clean
  polylines.
- CLAUDE.md toolkit rule #5 grows from four to five write-allowed
  exceptions; architectural rule #11 is added documenting the
  transit-line mirror.
- Three new POST endpoints (`/tools/compute_walkability`,
  `/tools/compute_amenity_supply`,
  `/tools/find_comparables_along_axis`), bearer-token-gated.

### Phase 7 slice 1: The reasoning agent (provider-agnostic)
Synchronous tool-use loop that takes a target spec + filters and
returns a defensible rental estimate by iterating over a curated
toolkit subset. Writes to `estimation_runs` with `mode='agent'`,
early-INSERTs `status='running'`, finalises to `success`/`failed`.
Trace records `kind='reasoning'` per LLM turn.
- **Provider-agnostic.** `api/providers/` defines a `CompletionProvider`
  Protocol with neutral message / tool / completion types; two
  implementations ship: `AnthropicProvider` (SDK = `anthropic`) and
  `GeminiProvider` (SDK = `google-genai`). `LLMClient` is now a
  provider-agnostic audit orchestrator. Adding a third provider is
  one new file implementing the same Protocol.
- **`skills` table + history trigger.** Each skill = a bundle of
  (system prompt + allowed tools + per-provider preferred model +
  loop limits). DB-backed at runtime; on-disk
  `skills/<name>/SKILL.md` is the canonical seed (committed in git
  as documentation). Operator edits live values via the Settings
  page; every change preserved in `skills_history`.
- **Curated tool subset for slice 1:**
  `find_comparables_relaxed`, `analyze_distribution`,
  `find_distribution_outliers`, `describe_neighborhood`,
  `verify_listing_freshness` + `record_estimate` terminator.
- **Settings page** (`/settings`) edits skills and `app_settings`.
  `/admin/*` routes are bearer-gated like every other write surface
  (the SPA already sends the token). They were briefly exempt on the
  "private Railway URL is the perimeter" theory, but that URL ships in
  the public SPA bundle, so the gate was restored.
- **Loop guards:** `max_iterations`, `max_cost_usd`,
  `wall_clock_timeout_s` — all sourced from the skill row, all
  short-circuit to `status='failed'` with `error_message`.
- **Migration 029** adds the `skills` + `skills_history` tables and
  trigger, the `'agent_estimation'` `called_for` enum, the
  `llm_calls.provider` column, and seeds `rental_estimator_v1`.
- Apartment rentals only (`byt` / `pronajem`). Multi-category
  defaults stay deferred to Phase 1.5b.

### Phase B0: Building decomposition — schema + scaffolding
Persistence foundation + read endpoints for the building-paste flow.
PR #59. Full description under "Building decomposition track" below.
- Migration 035: `building_runs` parent table with full status
  lifecycle CHECK (`pending` → `extracting` → `awaiting_input` →
  `estimating` → `success` | `failed`); `business_case jsonb`
  reserved for B3; `building_run_id` (FK,
  `ON DELETE SET NULL`) + `building_unit_id` (text) columns on
  `estimation_runs`. Architectural rule #13 added to CLAUDE.md.
- `api/building_runs.py` (`create_building_run`, `get_building_run`,
  `list_building_runs`) + Pydantic schemas (`CreateBuildingIn`,
  `BuildingUnit`, `BuildingOut`). Minimal `POST /buildings` inserts
  a `status='pending'` shell so the read path can be exercised
  end-to-end before B1 lands; `GET /buildings`, `GET /buildings/{id}`
  return rows with children surfaced via a side-query on
  `estimation_runs`. All bearer-gated.
- Frontend type stubs only in `frontend/src/lib/types.ts`
  (`BuildingRun`, `BuildingUnit`, `BuildingStatus`); no pages or
  components yet — those ship with B1.

