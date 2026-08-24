# Hydration sprint — one way to load a surface

Opened 2026-08-24 after the /pipeline board was reported as "terribly long to load" with
44 cards. Two investigations (a board autopsy, then an app-wide audit of 11 production
routes / 31 surfaces, both adversarially verified) turned that complaint into a doctrine
and a wave plan. This file is the sprint's ledger.

## North star

> **Every surface pays only for what it renders: one read whose server-side cost is
> proportional to the rows on screen, issued as early as the URL allows, and never blocked
> by a decoration — and every claim about it is sized in blocks touched, not in warm
> milliseconds.**

Four corollaries, because the evidence did not support one rule for everything:

- **A — card surfaces** (/pipeline, Browse cards, listing detail, comparables, ClipAudit):
  one cohort read for the surface's *structure*, then one shared, id-keyed, non-blocking
  enrichment layer that streams decorations in behind it. No card surface is ever gated on
  a decoration.
- **B — API-fed tables and feeds** (/brokers, /estimations, /notifications, /watchdog,
  /collections): the request graph is already right. Never add a Railway round trip, only
  delete them — every call pays a ~270–410 ms floor before any work (unpooled per-request
  psycopg, one uvicorn worker, no plan reuse). Cut *blocks touched per rendered row*
  instead, and never blank a rendered table while re-reading.
- **C — dashboards and reference reads** (/health, /costs, /datasets, city-quality
  overlays): one read per panel is already true. Bound history to what renders, precompute
  what changes daily, gate reads on whether the thing is displayed, page in parallel, and
  never poll faster than the data can change.
- **D — the substrate rule** (binds all three): cold is the default state. Size every fix
  in `EXPLAIN (ANALYZE, BUFFERS)` block counts in the PR body; warm wall-clock is not
  evidence — the same query measured 65 ms, 199 ms, 1.1 s, 3.6 s and 23.3 s in one week.
  Never measure a client wave while a maintenance job is over budget.

## Standing constraints

1. Enrichment cache keys never share a prefix with `['pipeline']` or the Browse keys —
   test-enforced. Get this wrong and every card drag refetches every decoration.
2. `security_invoker` is re-asserted in every migration touching a tenant view, backed by
   the view registry. It has silently reverted once before.
3. No browser-readable relation gains an email/phone-shaped column, in any shape,
   including a `has_*` boolean — and no widening of an existing view is written against a
   historical migration's viewdef. Always pull the live one first.
4. `pipelineCache` stays the one cache-policy chokepoint (rule #22). W3 makes it smaller;
   nothing else touches it.
5. Every PR carries its `docs/architecture.md` / skill / roadmap edit.
6. Every performance PR carries its `EXPLAIN (ANALYZE, BUFFERS)` block count.
7. No new Railway API round trip on any surface. API changes are deletions only.
8. No client wave is measured while a pg_cron maintenance job is over budget.

## Waves

### Lane 0 — hotfixes (done)

- ✅ **W-1c** — collection + tag CRUD onto the tenant pool (#1119). Ten routes were on the
  service-role connection behind the static bundle token with no account predicate; any
  token holder could read or delete another account's curation (live: 5 collections across
  5 accounts). `create_collection`/`create_tag` now stamp `account_id` explicitly — those
  two tables are top-level, so unlike their child tables no trigger fills it.
- ✅ **W-1a** — single-value list filters emit `eq`, not `in` (#1120). A ScalarArrayOp in
  the index's equality prefix cost `browse_list` its ordered scan: **15,877 buffers /
  4,452 ms → 6 buffers / 0.174 ms**; cold it exceeded the 8 s statement_timeout and Browse's
  default view answered HTTP 500. Verified live post-deploy: `op=eq`, HTTP 200, 94 ms warm.
  Same line fixed the portal-mirror lane. Three tests pin it.
- ✅ **W-1b** — `browse-list-rebuild` cadence `*/5` → `*/15` (mig 413, #1121). 201 runs at
  min 87 s / avg 270 s on a 300 s schedule = ~83 % duty cycle, 40 killed at the 600 s
  timeout. Mitigation only; root cause is I/O saturation (1 GB shared_buffers / 136 GB DB,
  every sampled backend waiting on `IO/DataFileRead`). Still open, in order: (a) why the
  same rebuild costs 10 s on a quiet day, (b) incremental rebuild off `dirty_properties`
  instead of a full CTAS, (c) instance sizing (operator's cost call).

### Lane 1 — the sprint

- ✅ **W0** — verification rail + baselines (this file). Smoke account seeded with 4
  pipeline cards (it owned none, so five of the board's six hops never ran in any automated
  check); `smoke-check.mjs` gained a /pipeline step (paints a card, time-to-first-card
  budget, priced card, columns) and a per-route budget sweep over six routes asserting **no
  5xx**, a request-count ceiling and a settle time. The 5xx assertion is the sharp one —
  Browse answered 500 for weeks with nothing noticing.
- ✅ **W1** — pipeline progressive hydration (`lib/hydration/`, frontend only, no migration).
  `fetchPipelineBoard` went from six serialized cross-origin round trips to two reads; covers
  and broker lines became independent queries in a `['hydration', …]` namespace, delivered to
  cards by context (CardFace renders twice — in-column and in the drag overlay — so props
  would drift). A stages-driven column skeleton replaced the bare "Načítání…", the header
  count shows `—` rather than a confident 0 while unknown, and the broker line reserves its
  height. Enrichment isolation is now structural, so the hand-written `.catch` swallow is
  gone; `pipelineCache` is untouched. Rails: key-namespace disjointness, a render test with
  both decorations hanging forever, and a read-budget test pinning the queryFn at two
  relations.
- ✅ **W2a** — bootstrap dedup: agendas → one query keyed on `user.id` (it keyed on the
  session *object*, and Supabase hands out a fresh one per auth event, so the pair ran 3×
  per app start outside React Query); `queryClient.clear()` on identity change (RLS-scoped
  rows from the previous account survived up to gcTime); unread badge gated on its own nav
  entry at 60 s. **Measured live: −4 requests on every route** — /collections 9→5,
  /watchdog 10→6, /notifications 9→5, /brokers 11→7, /browse 31→27, /pipeline 27→23.
- ✅ **W2b** — `fetchAllRows` exact-count termination + parallel pages; visibility-gate
  Browse's city-quality and per-card collection reads. All 18 call sites now request
  `count: 'exact'`; page 1 alone terminates the walk when the count says that's
  everything (no more terminating empty-page probe), and a full page 1 with more to
  come fires every remaining page in parallel instead of one request at a time. A short
  page 1 despite a larger count (db-max-rows clamped below `pageSize`) still falls
  through to the old sequential walk unchanged — trusting count-based page math there
  would reproduce the cap-drift bug this helper exists to prevent, the same reason a
  `pageSize + 1` probe was rejected as the mechanism. Browse's three city-quality
  queries (`citiesQuery`/`cityDefsQuery`/`cityValuesQuery`) gained `enabled: mapVisible`,
  matching the sibling `cityPolygons`/`rentMap` queries already gated in the same block —
  they feed only the map overlay. The per-card collection-membership read moved out of
  `CollectionSaveButton` (one `useQuery` per rendered card) up to `ListingCards`, gated
  on `rows.length > 0`, and is threaded down as a prop — one subscription for the whole
  grid instead of N. `EXPLAIN (ANALYZE, BUFFERS)`: `city_index_values_public` (this
  wave's largest table, 6,798 rows) costs 230 buffers for the added `count(*)`, on top of
  ~230 for the existing select — paid once per session (`staleTime: Infinity`) against a
  read that used to cost 7 sequential round trips. **Measured live: /browse 27→22,
  /pipeline 23→19** — most of the drop is the exact-count terminator saving one request
  on every exhaustive read whose whole result already fit on page 1 (curated cities,
  index definitions, pipeline members, pipeline board, collection membership, …), not
  just the two gates. /collections, /watchdog, /notifications, /brokers unchanged (this
  wave didn't touch them).
- ✅ **W9a** — listing-detail chain, client half. `sourcesQ` (`property-sources`) now keys
  and gates on `resolvedListingId ?? listingQ.data?.id` instead of waiting on the full
  listing row — on the canonical route (the id already known via seeded `Link` state or
  `natKeyQ`) it fires in parallel with `listingQ` instead of after it, cutting a level off
  the waterfall; the legacy route (no id until the listing loads) is unchanged. Both of
  `ListingDetail`'s own internal redirects (property→canonical, legacy→canonical) now seed
  `state.listingId` with the id they just resolved — `fetchPropertyReprNaturalKey` widened
  to also select `properties_public.listing_id` (already-live column, migration 343) for
  the first — so landing on the canonical URL never re-runs `natKeyQ` to re-resolve the
  natural key it was just redirected FROM/TO. `FreshnessBlock`'s post-verify invalidation
  had a dead `['snapshots', sreality_id]` call: the real key is `['snapshots',
  snapshotListingIds]` (an array of cross-source surrogate ids), so verifying freshness
  never actually refreshed the price chart; now a bare `['snapshots']` prefix, matching the
  same fix already applied one line below for `['listing']`. New test proves the parallel
  fetch directly (property-sources fires while `fetchListingById` is still an unresolved
  promise). No query-shape changes — client-only, no `EXPLAIN` evidence applicable.
- ✅ **W10a** — broker leaderboard (migration 414). The default (unfiltered) call joined
  ALL 88,762 region-grain `broker_region_type_stats` rows to `brokers`/`firms` BEFORE
  aggregating, then grouped ~89k joined rows down to ~22,666 brokers. Three fixes: (1)
  aggregate `broker_region_type_stats` down to one row per broker inside the CTE first,
  join the much smaller summed set to `brokers_public`/`firms` after; (2) a covering index
  on `(geo_level, geo_id) include (broker_id, category_main, category_type, listing_count,
  property_count, active_listing_count, active_property_count)`; (3) split the geo
  predicate's 4-way OR into a `union all` of two mutually exclusive branches — a single OR
  forces a BitmapOr, which always visits the heap regardless of indexing, so the covering
  index only paid for itself once the by-far-most-common (unfiltered) call became a plain
  single-condition scan. `EXPLAIN (ANALYZE, BUFFERS)`, warm: **6,776 → 3,108 buffers
  (2.2×), 1,690ms → 127ms (13×)**; the aggregation step alone went from a 4,278-buffer
  Bitmap Heap Scan to a 1,038-buffer Index Only Scan with `Heap Fetches: 0`. Verified live:
  top-5 brokers' summed counts match a direct reference query exactly; the explicit-filter
  branch (`union all`'s second arm) still returns correct rows. Remaining cost is a `Seq
  Scan on brokers` (1,776 buffers) filtering `status='active'` over all ~42k rows — a
  ~55%-selective filter, where Postgres correctly prefers a seq scan over an index scan, so
  this is close to the floor for the current schema; short of the original ~500-block
  estimate, still a large, honestly-measured win. Frontend: `boardQ` (the leaderboard
  query) gained `placeholderData: keepPreviousData` — every filter control changes its key,
  and without this each click blanked the ledger back to "Načítám žebříček…" instead of
  updating in place; a subtle "Aktualizuji…" hint covers the `isFetching`-without-`isLoading`
  window. New test proves the previous rows stay on screen (not the loading text) across a
  filter change. The page's other two queries (`reviewQ`, the merge-candidates badge count;
  `optionsQ`, the firm picker) have static keys that never change on filter interaction, so
  they're unaffected by this specific bug — left as-is.
- ✅ **W10b** — datasets: split the window-invariant polygon payload from the numbers
  (migration 415). `price_stat_growth()` computed `st_asgeojson()` for every obec on
  every call — including every operator drag of the `[from,to]` window — even though a
  municipality's boundary polygon never changes. Measured live on the largest dataset
  (4,044 obce): **5.86 MB of GeoJSON re-sent on every window change**, matching the
  parked memory note exactly. Split into `price_stat_growth_shapes(dataset_id)`
  (geometry only, keyed on dataset_id alone — obec universe = every obec that has EVER
  had an observation for the dataset, ignoring the window, since a shape has to cover
  any window the operator might pick) and the unchanged `price_stat_growth()` numbers
  with `geojson` dropped (the join to `admin_boundaries_public` becomes an `exists`
  check — same obec filter, zero geometry computed). `DROP FUNCTION` + `CREATE
  FUNCTION` (return columns changed, not `CREATE OR REPLACE`-compatible) with explicit
  re-grants matching the live ACL exactly (`authenticated` only, `anon` dark — this
  project's default privileges auto-GRANT on a fresh function). `growthToFeatureCollection`
  now takes the numbers rows and a separate `shapesByObec` map, joined by `obec_id`
  client-side; both call sites (`DatasetMap`, `ListingMap`/Browse's growth overlay) fetch
  shapes once with `staleTime: Infinity`, keyed on the dataset id only — never on
  `from`/`to`. New tests cover the join (including a missing-shape row skipped rather
  than crashing the map, matching the existing malformed-geometry guard).
- ✅ **W3** — one pipeline cache: collapsed `pipelineKeys.card(property_id)` into
  `pipelineKeys.members`. The per-property `card` cache was a separate `fetchPropertyPipeline`
  read that duplicated exactly what `members` already held for that property (their columns
  had already drifted out of sync once — a property badged "9" on a Browse card and "5" in
  its own listing-detail header before a prior fix re-synced them by hand). `PipelineToggle`
  (the listing-detail control) now reads the same shared `members` query every Browse/Table
  funnel already reads — one fewer network round trip on every listing-detail page load, and
  the two-reads-drift bug class can't recur since there's only one read left. `pipelineCache.ts`
  (the rule #22 chokepoint) shrank from patching/snapshotting THREE caches per write to TWO
  (`members` + `board`, which stays separate — it carries display fields `members` doesn't:
  price, photo, place). `revalidatePipeline` dropped its now-unused `property_id` parameter.
  `PipelineCard` type and `fetchPropertyPipeline` deleted (zero remaining callers). Client-only
  — no query-shape change, no `EXPLAIN` evidence applicable.
- ✅ **W4** — cover substrate: `listing_cover_public` (migration 416). The board's cover-photo
  read asked for one thumbnail per card (`perId: 1`) but `images_public` has no per-listing
  LIMIT, so PostgREST returned every image row for every listing in scope and the client
  discarded all but the first — worse, `images_public` LEFT JOINs LATERAL to
  `image_clip_tags` per row for the tag badge, so the server paid a correlated CLIP-tag
  lookup for every discarded row too. Measured live (44 real listing ids): 901 image rows,
  901 CLIP-tag lateral probes, 3,995 buffers, 380ms. `listing_cover_public` computes the ONE
  cover row per listing FIRST (`distinct on (listing_id)` over the existing
  `images_listing_id_sequence_key` index — **no new index needed**, it already provides
  `listing_id, sequence` in presorted order, so Postgres does an Incremental Sort instead of
  a full one), THEN joins the CLIP lateral only to that already-reduced set. Measured live
  (same 44 ids, warm, reproduced twice): **44 rows, 44 CLIP-tag probes, 788 buffers, 59ms**
  — a 5× buffer cut and the lateral-probe count now equals the rendered row count instead
  of ~20× it, matching the block-count target exactly (`44 rows / 44 probes`). No
  `security_invoker` (matches `images_public`, its sibling — shared market data, not
  per-account RLS-scoped); grants mirror `images_public` exactly (`anon` dark,
  `authenticated` SELECT-only). `useListingCovers` (the shared hydration hook, currently
  wired into Pipeline; Browse gets it in W7a) now calls the new `fetchListingCovers`
  instead of `fetchImagesForListingIds(ids, 1)` — the multi-image fetcher stays for its
  other callers (the card carousel, comparables) which genuinely need more than one photo.
- ✅ **W5** — `pipeline_board_public` cohort view (migration 417). The board's structural read
  was two sequential PostgREST round trips joined client-side (`property_pipeline_public`,
  then `properties_public.in('property_id', ids)` — the second unable to start until the
  first's ids landed). `pipeline_board_public` does the join server-side (LEFT JOIN on
  `property_id`), so `fetchPipelineBoard` is now one `fetchAllRows` call instead of two
  sequential requests. `security_invoker = true` — **required**, `property_pipeline` is
  account-scoped RLS same as its sibling `property_pipeline_public`, so a plain view here
  would have silently reopened the tenant boundary that table's RLS exists to enforce (the
  standing constraint 2 rail was live for exactly this). `properties_public` stays a plain
  inner view unchanged — `properties` carries a permissive `FOR SELECT TO authenticated`
  policy, not per-account, so nesting it inside an invoker-mode outer view is correct as-is.
  Grants mirror `property_pipeline_public` exactly (`anon` dark, `authenticated`
  SELECT-only). Verified live: row counts match between the new view and the old two-view
  composition exactly (48 = 48); a 5-row spot check of `property_id`/`stage_id`/`street`/
  `price_czk` matches field-for-field. `composePipelineCards` simplified from a two-array
  join to a one-array projection (the Map-based join it used to do is now the view's job);
  `pipelineBoard.test.ts` rewritten to pin ONE relation read instead of two. **← stop point
  1 — pausing here for review before W6 onward.**
- ⬜ **W6** — one broker call (contacts onto the API-only view; delete the second call from
  the board *and* listing detail).
- ⬜ **W9b** — append `source_id_native` + `property_id` to `listings_public`, written
  against the LIVE viewdef (mig 398 replaced it to close a PII hole).
- ⬜ **W7a** — Browse + comparables onto the shared layer; four image loaders → one.
  **← stop point 2**
- ⬜ **W7b** — media delivery (the hourly presign re-mint kills the browser cache key).
- ⬜ **W10c** — /costs date-expression index. ⬜ **W10d** — /health bounds. ⬜ **W10e** —
  /estimations OR-join (EXPLAIN first — that one is code-derived, not measured).
- ⬜ **W8** — bundle: maplibre out of the filter-controls barrel, recharts unpinned from
  React. Last: first-visit and post-deploy only.

## Baselines — 2026-08-24, post-W-1a/W-1b, production

Per-route app data requests (PostgREST + Railway only; basemap tiles, assets and image
bytes excluded) and settle time, measured by `npm run smoke-check:prod`:

| Route | W0 | post-W2a | post-W2b | Notes |
| --- | --- | --- | --- | --- |
| /browse | 31 | 27 | **22** | was 9.1 s *and an HTTP 500* pre-W-1a; now 4.9 s |
| /pipeline | 27 | 23 | **19** | time-to-first-card 1,427 → 951 ms (W1); rest is fetchAllRows exact-count terminators, not gating |
| /collections | 9 | **5** | 5 | was 6-of-9 duplicated bootstrap; W2b didn't touch this route |
| /watchdog | 10 | **6** | 6 | reference feed — shape was already correct |
| /notifications | 9 | **5** | 5 | server work is ~15 ms; the rest is transport |
| /brokers | 11 | **7** | 7 | leaderboard is server-bound, see W10a |

Server-side block counts to beat (constraint 6):

| Read | Blocks | Target | Wave |
| --- | --- | --- | --- |
| `browse_list` default cohort | 15,877 → **6** | done | W-1a |
| broker leaderboard | 6,776 → **3,108** (warm) | ~500 (close, not hit — floor is a `brokers` seq scan at 55% selectivity) | done — W10a |
| board images (44 cards) | 3,995 → **788**; 901 CLIP laterals → **44** | 44 rows / 44 probes | done — W4 |
| `llm_cost_daily_public` | seq scan, 231,189 rows discarded for 93 out | index | W10c |
| `pipeline_checks_public` | 6,120 rows scanned for 15 | bounded | W10d |

## Parked, with re-entry triggers

Multi-category cohort ordering (revisit if a preset drops its district filter) · Browse map
payload 16.8 MB · instance sizing (after W-1b root-cause and W10a) · React Query persistence
· /notifications 100-row cap · repr-flip semantics (own PR, see W4) · connection pool +
gzip middleware · the unexplained client-side count abort on Browse.
