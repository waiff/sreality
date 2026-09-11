> Track file — part of [ROADMAP.md](../ROADMAP.md). After shipping, edit only this file + its index row.

## Scraper track (parallel)

Scraper-specific evolution beyond Phase 1's nightly index walk.
Independent of the analytical, UI, and map tracks.

### sreality photos: the whole frame, and provenance on every stored row (2026-09-11, in progress)
- **Shipped:** downloads moved off sreality's `res,749,562,3|shr,,20|jpg,90` (mode 3 = a 4:3 CROP;
  ~85% of photos lost their edges, one floor plan lost a whole floor) onto their `SQUARE_1800_JPG`
  template — whole frame, ≤1800px, no watermark. Their CDN is an exact-template ALLOWLIST, so a
  stored legacy/`rot` chain is NORMALISED onto the template (only `rot` survives; an unrecognised op
  is dropped rather than parked by a 400), and `frontend/src/lib/imageUrl.ts` mirrors it for the
  not-yet-downloaded fallback — pinned by shared probe vectors both suites run. Migration 496's
  provenance is now written on every download: `rendition` + decoded `stored_width`/`stored_height`.
  The image phase gained a wall-clock bound (`--image-max-seconds`, both workflows) because the count
  cap's throughput basis predates the bigger master.
  **The re-master lane now exists** (`scripts/remaster_sreality_images.py` +
  `sreality_image_remaster.yml`, 6 shards, `15 */2`): it re-downloads the ~3.55M `rendition IS NULL`
  rows at the master template and OVERWRITES each R2 object under its stored `storage_path`, stamping
  provenance + re-arming phash/CLIP through `mark_image_remastered`. sreality ROTATES image URLs when a
  listing is edited (~75% of stored URLs are dead on the longest-lived actives), so a 404/410/dead-URL
  on the stored URL re-resolves from the listing's CURRENT detail by image `order` and persists the
  fresh URL; bytes that come back 749x562 are an anomaly (never uploaded, never stamped as a master).
  Every outcome stamps the attempt clock, and retiring a row from the queue always takes two sightings
  20h apart, so one CDN blip can never claim `sreality-749-crop`. **Sequence: pilot with
  `listing_ids` (and/or `dry_run`) first, read the `REMASTER done …` line, THEN arm the cron by setting
  the `SREALITY_REMASTER_CRON` repo variable to `on`** — until it is armed the schedule is inert.
- **Next:** (1) run the re-master to completion — each shard merges its OWN sub-key (`"k/6"`) into
  `app_settings.sreality_remaster_last_complete` once it has walked its slice out AND confirmed no
  convertible row is left, so the lane is done when all six sub-keys are there; until then phash/CLIP
  are comparable only WITHIN a rendition; (2) a thumbnail rendition — the SPA/extension currently serve a
  master into every grid tile; (3) re-measure one image shard at the new template and resize
  `MAX_IMG` / the fresh lane's cap from the result; (4) one census of what stored chains actually
  carry — `SELECT DISTINCT split_part(op, ',', 1) FROM (SELECT unnest(string_to_array(split_part(
  sreality_url, 'fl=', 2), '|')) op FROM images WHERE sreality_url LIKE '%fl=%') t` — to confirm the
  kept-op allowlist (`rot`) drops nothing that carried meaning.

### Portal listing URLs — one contract, nine portals (2026-09-11, done)
- Operator-reported: the listing page's Sreality chip 404s (`rodinny-dum` vs sreality's `rodinny`).
  Root cause is ONE asymmetry — sreality was the only portal whose page URL was not a stored fact,
  so the SPA reconstructed it from display labels. North star + waves:
  `docs/design/portal-listing-url.md`.
- **W0 (this entry):** `scraper/sreality_url.py` (closed sitemap-sourced codebook, one assembler,
  two adapters, declines never raise); `parse_listing` emits `source_url`; the column rides
  `LISTING_COLUMNS` preserve-if-null on both write paths; the crawler path's bespoke post-insert
  UPDATE is gone; `ScrapedListing` refuses an empty URL; `db.detail_ref` keeps sreality's page off
  every queue; both snapshot-diff sites skip the key.
- **W1:** `scripts/reconcile_source_url.py` + `reconcile_source_url.yml` (dispatch, dry-run default):
  a primary-key walk over NARROW typed columns (never `raw_json`), the same assembler as ingest,
  canonical string or nothing; counters that gate the write (unknown-code histogram — a recent
  unknown code suspends writes; structural id check; `would_clear` before `--clear`), a pre-flight
  `--conformance N` HEAD gate, `--report-check` → `outbound_url_parity`. Rails lifted into
  `scripts/backfill_support.py` (rebuild-gap wait + lock-fight retry) so `backfill_area_basis`
  shares them.
- **W3 (rails):** mig 495 adds a `source_url` row to `data_quality_by_source` (free 6-hourly
  per-portal trend via the existing pg_cron capture); `outbound_url_coverage` in `verify_pipeline`
  (ABSOLUTE active-NULL count per source, warn 50 / fail 500 — a new sreality code silently
  producing NULLs would never move a share); `outbound_url_parity` = the reconciler's weekly dry
  run with `--report-check` (Mon 05:41 UTC; the only detector that reaches the inactive archive);
  `outbound_url_conformance` = `scripts/verify_outbound_urls.py` weekly (Tue 05:17 UTC; ~44 HEADs,
  rotating quarter of the codebook per ISO week, concentration thresholds).
- **Closed 2026-09-11:** migs 494 + 495 applied (hosted Supabase MCP), W2 #1403 + W3 #1405 merged,
  Railway rolled out, read-only Playwright check on production: the reported listing's chip is the
  exact canonical and a merged property's two sreality siblings (property 60, `rodinny` + `chalupa`)
  each link their OWN page. W1 write pass: 227,292 rows filled (103,885 active / 123,407 inactive),
  4,076 declined (`sub_cb_null` 4,073), zero unknown codes; the conformance gate BLOCKED the first
  attempt (street held under a NULL provenance stamp → #1406) and passed 40/40 after. Rails seeded:
  coverage ok (20 active sreality rows without a URL, warn at 50), conformance ok 2.27 % (1/44 = an
  mmreality page answering 403 to HEAD — a portal quirk, not a URL defect; a crawler-portal HEAD
  refusal is a known soft spot of the probe). **Filed, not built:**
  the SPA's `api.ts` source registry is missing ceskereality/realitymix, BUT `estimation_runs.
  source_kind`'s CHECK (mig 020) lists neither either — a URL of those portals pasted into the
  estimation flow may violate the constraint; needs its own investigation (inbound URL→portal
  registries, four copies), not a 4-line SPA patch.

### The nomination gate went STRUCTURAL — counts stopped vetoing (2026-09-08, done)
- The 5th element of `walk_category` now means **"the walk reached the portal's end"**
  (`portal.walk_reached_end` over a shared `StopReason` / `PORTAL_ENDS` / `OUR_STOPS`
  vocabulary), not "the counts reconciled". The numeric per-slice AND could never pass on a
  live index: one 87-row Karlovarský slice collecting 86 vetoed all 20,964 rows of
  ceskereality's houses-for-sale, which nominated nothing for days.
- All nine portals reclassified their exits, with two mandatory disciplines: **items-first**
  (a blocked HTTP 200 must not exit through the real last page's branch) and the **barren
  rule** (an empty page is ours until a re-fetch corroborates it). sreality's swallowed HTTP
  422 deep-pagination refusal is now exposed as `cap_hit` and counts as our stop.
- `walk_coverage` is unchanged and still drives ceskereality's descent, idnes's resample and
  sreality's national fallback — as a logged `COVERAGE` alarm, never a gate. Both facts land
  in `scrape_runs.by_category` (`walk_reached_end` + `walk_coverage`, no migration) and
  `verify_pipeline`'s coverage check reports `categories_nominating` beside the gap.
- bazos CI budget: `timeout-minutes` 75 → 130 with `--max-seconds 6600`. Since migration 488
  (22 scopes, ~2,400 pages) GitHub was cancelling the walk at 75 min, so the tail scopes never
  walked and the `scrape_run` was never finalized.
- **Next:** watch the first `COVERAGE` warnings per portal (a forged terminator shows up there
  first) and the drain volume from ceskereality's newly-unblocked categories.

### Presence-verified delisting — rule #3 rewritten (2026-09-07, done)
- Index absence NOMINATES, the page DECIDES: a complete category walk queues every active
  row it did not see for a detail fetch at the lowest priority; a positive gone signal flips
  it, a live page refreshes it. One mechanism for all nine portals (shared runner default +
  overrides for bazos subtypes, ceskereality/realitymix sibling slices, remax/maxima agendas).
- Retired: the staleness rail, ceskereality's national cross-check (region-less listings are
  nominated instead), the latching refusal. `delist_flip_cap` now throttles nominations.
- mmreality joined the always-on worker (its queue drains in minutes, not hours). Migration
  482 adds `portal_category_drift`, the coverage alarm fed by the optional `live_categories`
  seam (mmreality implements it; ceskereality exposes no machine-readable category list).
- **Next:** watch the first walks' `VERIFY` lines and `delist_flip_refusals` deferrals; the
  ceskereality backlog (~40k) drains at 10% of each category per walk.

### mmreality: ten per-type indexes, proved against the portal's own count (2026-09-06, done)
- The bare `/nemovitosti/` feed the walk paged since 2026-05 was **prodej only** (its own
  SSR `metadata.count` equals the prodej total) — the 1,518 rentals were never scraped —
  and every per-type URL declares a count. So "single mixed index, no total", the reason
  the portal was parked on `supports_complete_walk=false`, was wrong on both halves.
- Walk is now one category per (sale type × property type), ten in all, each proved by
  the shared `walk_is_complete` arithmetic, written to the slice ledger (one row per
  category), swept by the category-scoped `mark_inactive_native` behind the 12 h rail.
  Migration 481 set the ten descriptors; the coverage gate flips the flag from evidence.
- **Next:** watch `portal_coverage_gate` for mmreality's three-cycle streak (~a day).
  Its ~3,300 stale rows (27% of active) will then hit the flip cap — verify a sample by
  fetch and release with a bounded override, exactly as idnes.

### Phase 1.5: Six-category coverage (done)
Cross-listed under top-level Done above. Headline: all six byt / dum
/ komercni × pronajem / prodej pairs walked nightly with per-category
refetch cap.

### Phase 1.6: Unified 15-min cadence + immediate delisting (done)
The 15-min `scrape_delta.yml` was promoted from a `--limit 200` partial
walk to a **complete** index walk every tick, so both new listings and
delistings reflect within minutes instead of within a day. `mark_inactive`
now runs every tick, made safe by two rails: a walk-completeness guard
(`_walk_complete` compares collected vs the API's `result_size`, skipping
the flip on a truncated walk) and gone-detection on the detail fetch
(`ListingGoneError` on 404/410 or sreality's "tato stránka neexistuje"
body flips the single listing inactive + clears its fetch-failure row
instead of accumulating failures). Detail/image work is capped per tick;
deferred work drains next tick (failure-priority + newest-first image
ordering, so a run's fresh photos download inline). The nightly `scrape.yml`
became the thin deep run: condition scoring + deep image-backlog drain +
high-cap detail catch-up. Runs are labelled via `--run-type` so the Health
page keeps the frequent ticks (`delta`) distinct from the nightly (`full`).
Operational watch-items: ~10–15× the prior GitHub Actions minutes, and
continuous full walks raise sreality rate-limit/IP-block exposure.

### Phase 1.7: Parallel detail fetches behind a global rate limiter (done)
Detail fetching was serial at 1.5s/request (~0.67 req/s) — the engine's
throughput bottleneck, and the reason the 15-min tick's detail budget is
small. Now a small `ThreadPoolExecutor` does the network I/O concurrently
while the main thread serialises DB writes against the single (not
thread-safe) psycopg connection — the same pattern the image phase already
uses. A hand-rolled, stdlib-only `scraper/rate_limit.RateLimiter` (shared
across all per-category clients + workers) caps the *aggregate* request
rate so concurrency hides per-request latency without raising the politeness
ceiling; it auto-backs-off on HTTP 429/403 (`RATE penalize` log line) and
decays back when sreality is quiet. New `--detail-workers` / `--detail-rate`
knobs (defaults 4 workers @ 2 req/s) are wired into both scrape workflows.
No new dependency (pure `threading`); the index walk and DB writes stay
serial by design. `get_detail`'s serial 1.5s self-throttle is retained for
the no-limiter callers (`freshness`, `--detail-only`).

### Phase 1.8: Single hourly pipeline + decoupled scoring (done)
Collapsed the two-tier scrape into **one** hourly workflow. The
former `scrape_delta.yml` (hourly walk) and `scrape.yml` (nightly deep
run) were folded into a single `scrape.yml` "Scraping: Sreality hourly
run" (cron `0 * * * *`, `run_type='full'`): complete index walk +
`mark_inactive` + capped detail refetch (4000 global / 1200 per category)
+ active-image drain, at a moderate concurrency bump (8 detail workers
@ 6 req/s, up from 4 @ 2). `scrape_delta.yml` deleted. Condition scoring
moved OUT of the scrape into its own decoupled hourly workflow
`condition_scores.yml` (cron `30 * * * *`, repurposed from the manual
backfill, wrapping `scripts/backfill_condition_scores.py`) so the LLM
phase can never slow the walk; its selection is portal-agnostic, ready to
score future scrapers. `images.yml` repurposed as the deep backlog drain
that also reaches inactive/historical images (the hourly run covers
active). Liveness (migration 090) keys off any `index_pages>0` walk, not
`run_type`, so no schema change was needed.

### Phase 1.8b: Async condition scoring via the Batch API (code shipped; migration pending)
Optional second scoring backend on the Anthropic **Message Batches API**
(50% cheaper, async). `score_listing_condition` was split into a shared
`build_scoring_request` (one request builder, so the cached system+tools
prefix is identical across sync and batch) and `persist_scoring_result`
(the cache row + guarded `listings.*` UPDATE). `AnthropicProvider` gained
`submit_batch` / `poll_batch` / `iter_batch_results` (+ neutral
`BatchStatus` / `BatchResultItem` types). Two scripts —
`submit_condition_batch.py` (build + submit a batch, dedup against
in-flight requests) and `ingest_condition_batch.py` (poll, persist
results idempotently, record `llm_calls` at the discounted cost) — drive
the new `condition_score_batches.yml` workflow (dispatch-only:
`submit` / `ingest` modes). Tracking tables are migration **098**
(`condition_score_batches`, `condition_score_batch_requests`). **Pending:**
apply migration 098 + confirm a manual submit→ingest round-trip before
enabling a scheduled `ingest`; the synchronous `condition_scores.yml`
stays the default steady-state path.

### Phase 1.9: Prepared statements for the hot write loop (done)
First phase of the scaling roadmap
(`~/.claude/plans/the-health-page-is-functional-moore.md`, the low-risk
write-throughput quick win). `scraper/db.py` gained `connect_session()`,
which points the scraper's long-lived detail-write connection
(`_run_full`) at a new `SUPABASE_DB_SESSION_URL` (Supabase Session-mode
pooler, port 5432) **without** `prepare_threshold=None`, so the repeated
upsert + spatial SQL gets server-side prepared once and reused across the
run instead of re-planned per listing. The session pooler gives each
client a dedicated backend, so prepared statements are safe there (no
`DuplicatePreparedStatement`). Everything else — scrape_run bookkeeping,
bazos, images, recompute, API, scripts — stays on `connect()` (Transaction
pooler, 6543). When `SUPABASE_DB_SESSION_URL` is unset, `connect_session()`
falls back to `connect()`, so nothing breaks where the secret isn't set.
Plus a small fairness tweak: `_rotated_categories` rotates the category
processing order each run (offset = run hour) so the per-run detail-refetch
budget — consumed in category order — no longer permanently starves the same
trailing categories. Next in the scaling roadmap: **Phase 2 — split the
fast index walk from the slow batched detail-drain.**

### Phase 3.0: Real-time properties — dirty-set incremental recompute (done)
The third scaling-roadmap unlock: the `properties` rollup goes near-real-time
and **O(changes)** instead of a full-table recompute. Previously
`recompute_property_stats` recomputed *every* property every 30 min, so a
new/edited/delisted listing lagged up to that interval and the job wouldn't
scale to 5–10 portals.
- **`dirty_properties` queue (migration 106).** The writers that change a
  property's children enqueue its `property_id` with a cheap set-based
  `INSERT ... ON CONFLICT DO UPDATE SET marked_at`: `write_detail_batch` (a
  content change → new snapshot, via the snapshot insert's `RETURNING`),
  `mark_inactive` / `mark_listing_inactive` (delisting), and `touch_listings`
  (a re-sighting that reactivates a listing — no snapshot, so captured via a
  CTE). New listings (`property_id` NULL) are left to straggler-attach.
- **`property_maintenance.yml`** (`recompute_property_stats --incremental`,
  cron `*/5`): attaches new stragglers (the batched **Tier-1 matcher**,
  skipping the one-time native-id backfill) + recomputes **only** the queued
  properties — the full recompute SQL scoped to `id = ANY(...)`. So properties
  reflect changes within ~5 min and the job is O(changes).
- **Race-free + terminating drain:** claims rows dirtied at/before a run
  cutoff, recomputes, deletes only those untouched since (a mid-run re-dirty
  bumps `marked_at` past the cutoff → preserved for the next pass).
- **Daily full sweep** (`recompute_property_stats.yml`, no `--incremental`,
  04:15 UTC) recomputes everything + clears the queue — the self-healing
  backstop, so a missed enqueue reconciles within 24h. Tier-2 fuzzy dedup
  (`dedup_sweep.py`) unchanged. Both maintenance jobs share the
  `sreality-property-maintenance` concurrency group.
- Accepted lag: a byte-identical reactivation (no snapshot) waits for the
  daily sweep — rare, documented. Architectural rule #20.
- **Liveness hardening (2026-08-06 incident).** The sweep outgrew its 30-min
  `timeout-minutes` (runtime doubled in ~3 weeks); each kill reported as
  `cancelled` (never `failure` — no email) and stranded the old 3-hour lease
  grant, freezing the worker lane + both crons for hours; 5 dead sweeps in
  4 days went unnoticed and ~38k live properties in the id tail went stale.
  Fix: the maintenance lease is ONE 15-min TTL heartbeat-renewed every
  batch/slice (the mig-366 sticky-holder pattern; a SIGKILL now strands
  minutes, not hours), acquisition inside the try, `_wait_lease` bounded
  (fail RED, not a 30-min retry burn), the full sweep clean-stops at
  `--max-seconds` (default 6000, `timeout-minutes: 130` backstop — raised from
  3600/75 in 2026-08 after six sweeps measured 83-96% of the old ceiling) and exits
  RED with a swept-range-scoped dirty clear, and a `property_maintenance`
  check in `verify_pipeline` (hourly acute lane) alarms on the
  `property_sweep_last_complete` stamp age (written on complete walks only —
  O(1); the per-row staleness scan measured ~3.5 min live and would have
  blown the acute lane's own 5-min timeout) + oldest dirty-queue row age.
  Known follow-ups: (1) the batch UPDATE rewrites all ~515k live rows daily
  with no changed-only guard — the suspected driver of the runtime growth
  (bloat feedback); measure and add an `IS DISTINCT FROM` guard as its own
  change; (2) repeated budget exhaustion restarts from id 1 each day (no
  resume cursor), so a chronically over-budget sweep starves the high-id
  tail — loud (daily RED + stamp-age fail) but not self-healing; add a
  cursor only if (1) doesn't restore headroom.

### Phase 4.0: Portal framework — one pipeline for every portal (done)
The fourth scaling-roadmap unlock: collapse sreality + bazos onto ONE shared
framework so a new portal is a fetcher + parser + config row, with no per-portal
branches in shared code. The lean/modular guardrail before onboarding portals 3+.
- **`BasePortalClient`** (`scraper/portal_base.py`): the HTTP machinery every
  portal shares — session/headers, `RateLimiter` pacing + 429/403 penalize,
  retry/backoff, `ListingGoneError` on 404/410. `SrealityClient` / `BazosClient`
  subclass it and keep only the `Accept` header, URL building, and body markers.
- **`PortalConfig`** (`scraper/portal.py`) backed by the `portals` registry's new
  operational columns (`supports_complete_walk`, `categories`, `split_threshold`,
  migration 107), with a baked-in default fallback.
- **Source-generic queue** (migration 108): `listing_detail_queue` re-keyed from
  `sreality_id` to `(source, native_id)` + `detail_ref`, so every portal enqueues
  into the one queue and the one drain claims from it. Backward-compatible
  re-key (sreality_id stays a unique index).
- **`portal_runner`** (`scraper/portal_runner.py`): one `run_index_walk` + one
  `run_detail_drain`, parameterized by a `Portal`. `SrealityPortal` /
  `BazosPortal` implement the seams; the entrypoints are thin delegators.
  sreality stays byte-identical (the district-split is the one sanctioned hook);
  bazos joins the queue/drain model (partial walks → never marks inactive).
- Architectural rules #19 (shared split) + #21 (the framework + modularity).
  Pilot scope: bazos is single-category (the queue doesn't carry the category
  parse_detail needs); multi-category bazos would encode it — deferred.
After Phase 4 the limiter is each portal's polite fetch rate, not the DB or
pipeline divergence — the healthy place to be. **Validated by onboarding
bezrealitky (portal 3)** as a pure fetcher + parser + config row — a JSON-API
portal that, because its detail JSON carries the category, walks many categories
through the unchanged queue/drain (the multi-category limitation is per-portal,
not a framework one). See the dated entry at the top of ## Done.

### Phase 2.0: Cadence split — index-walk / batched detail-drain (done)
The structural unlock from the scaling roadmap
(`~/.claude/plans/the-health-page-is-functional-moore.md`). The single
combined scrape is split into two cadence-matched jobs joined by a queue:
- **`index_walk.yml`** (`scraper.main --index-only`, cron `*/15`,
  `run_type='index'`) walks the full index, `touch_listings` +
  `mark_inactive` under the completeness guard, and **enqueues** new /
  price-changed ids into `listing_detail_queue` (migration 105) with a
  priority (failure-retry > price-changed > new). No detail fetch — delistings
  surface within minutes. Transaction pooler.
- **`detail_drain.yml`** (`--drain-only`, cron `*/15`, `run_type='detail'`)
  claims a bounded slice (`FOR UPDATE SKIP LOCKED`), fetches on a rate-limited
  pool, and writes **batched** via `db.write_detail_batch` — set-based
  `jsonb_to_recordset` (fixed-shape SQL so the session pooler still prepares
  it), one transaction per ~100 listings, snapshot-on-change preserved by an
  `IS DISTINCT FROM` anti-join. Target ~0.1–0.2 s/listing. Session pooler.
- **Tier-1 matcher deferred** off the hot path: the drain inserts with
  `property_id` NULL; the straggler-attach runs the same spatial match
  set-based. (Phase 3 moved that attach to a `*/5` incremental pass, cutting
  the brand-new-listing Browse read-lag from ≤30 min to ~5 min.)
- `scrape.yml`'s combined `_run_full` retained as the **dispatch-only revert
  fallback** (re-add its cron to roll back; no code change).
- Migration 105 also widens `scrape_runs.run_type` to admit `index`/`detail`
  and redefines `scraper_health_checks()` so liveness/reconciliation stay
  scoped to the index walk while the 24h counters also see the drain's
  `index_pages=0` rows. Architectural rule #19.

### Phase 1.5b: Multi-category UI defaults (done)
The data was always broad (all six byt/dum/komercni ×
pronajem/prodej pairs), but the analytical and estimation surfaces
silently defaulted `category_main='byt'` / `category_type='pronajem'`,
so house and commercial estimations couldn't be driven cleanly. Done:
- `toolkit/comparables.py` — `ComparableFilters.category_main` /
  `category_type` now default to `None` ("search every category"), not
  `byt`/`pronajem`. The silent apartment-rental default is gone; the
  `None` semantic (no category clause) was already supported and tested.
- `toolkit/velocity.py` — `compute_listing_velocity` was the one
  internal caller relying on the old default; it now reads the
  subject's own `category_main`/`category_type` and ranks it against
  same-category peers (a house no longer ranks against apartments).
- `toolkit/neighborhoods.py` — `describe_neighborhood`'s function
  default aligned to `None` for consistency.
- `api/schemas.py` — `category_main`/`category_type` are now **required**
  (Pydantic 422 on omission, pass `null` for "all categories") on
  `FindComparablesIn`, `DescribeNeighborhoodIn`, `ComputeMarketVelocityIn`.
  `EstimateYieldIn` requires `category_main` and keeps `category_type`
  following `estimate_kind` — the same smart pattern `CreateEstimationIn`
  already used (left unchanged).
- `frontend/src/components/NewEstimationModal.tsx` (the current estimation
  entry point; the old `EstimateForm.tsx`/`UrlScrapeStep.tsx` are gone) —
  a "Property type" selector (Apartment / House / Commercial) sits beside
  the Rent/Sale toggle; both plumb `category_main` + `category_type`
  explicitly into the estimation request, and the placeholder URL + help
  copy follow the chosen category.
Unblocks end-to-end house and commercial estimations over data that
already existed in the database.

### Phase 2: Multi-portal ingestion (later, larger)
> **Design locked (2026-05-25): see `docs/design/multi-portal-dedup.md`** (deleted in the
> 2026-08 "NEW DEDUP" cutoff; git history only — the ingestion half of that doc shipped, the
> dedup half was removed).
> Multi-portal ingestion is now unified with the Dedup track into one
> sliced feature. Chosen target portals: **bezrealitky, bazos,
> reality.idnes**. Ingestion arrives in Slice 3 (after the Shape-B
> property foundation + property-grain read/notification slices). The
> scope notes below are background; the doc is the source of truth.

Today's non-sreality flow is *parse on demand* via
`source_dispatcher` (LLM call per URL, cached 7 days). To make
bezrealitky / idnes / remax / maxima comparables (and other portals
as the operator opens them) show up in `find_comparables`, those
portals need to land in the `listings` table itself. **Hard
dependency: the Dedup track's Phase D1 must ship first.** Without
strict cross-source dedup, multi-portal ingestion multiplies every
listing by the number of portals it appears on, which breaks
`find_comparables`, `browse_stats`, and the notification dispatch
fan-out alike. Scope:
- Per-source index walker analogous to `scraper/sreality_client.py`.
  Most of these portals don't expose a public JSON API, so HTML
  pagination / playwright will be in scope; bot-detection is more
  aggressive than sreality.
- Reuse `parse_listing_url` for detail pages, with aggressive
  caching and a per-source rate limit.
- New `listings` columns: `source` (default `'sreality'`),
  `source_url`, `source_id_native`. New numbered migration. The
  same migration that adds these columns is co-authored with
  Phase D1's canonical shape — they touch the same surface.
- Update `_shared_filter_where` so toolkit queries can filter by
  source.
- Frontend Browse: source multi-toggle.
- Open question: trust LLM-parsed data in the deterministic
  comparable pool, or keep portals as a separate cohort visible
  behind a `source != 'sreality'` badge until visual + heuristic
  validation matures? Default recommendation is the latter; agent
  (Phase 7) opts cross-portal cohorts in once it can validate
  them.

