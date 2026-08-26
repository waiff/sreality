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
  the waterfall. The legacy route is unchanged only when it is entered COLD (a typed or
  bookmarked URL, where no id is known until the listing loads); reached via an in-app
  `Link` that seeds `state.listingId` — which is exactly what the estimations run-links
  do — it takes the fast path too, so "the legacy route is unchanged" is true of the URL
  form, not of every arrival at it. Both of
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
- ✅ **W3** (partial, deliberately) — collapsed `pipelineKeys.card(property_id)` into
  `pipelineKeys.members`. The per-property `card` cache was a separate `fetchPropertyPipeline`
  read that duplicated exactly what `members` already held for that property (their columns
  had already drifted out of sync once — a property badged "9" on a Browse card and "5" in
  its own listing-detail header before a prior fix re-synced them by hand). `PipelineToggle`
  (the listing-detail control) now reads the same shared `members` query every Browse/Table
  funnel already reads — one fewer network round trip on every listing-detail page load, and
  the two-reads-drift bug class can't recur since there's only one read left. `pipelineCache.ts`
  (the rule #22 chokepoint) shrank from patching/snapshotting THREE caches per write to TWO
  (`members` + `board`, which stays separate — it carries the STRUCTURAL fields `members`
  doesn't: price, place, area, is_active; **not** the photo or the broker, which are
  decorations in the `['hydration', …]` namespace and must never return to the board row).
  `revalidatePipeline` dropped its now-unused `property_id` parameter.
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
- ✅ **W6** — one broker call (migration 419). The board's broker decoration and the listing
  page's vizitka each spent TWO serialized Railway round trips on one broker line:
  `POST /brokers/by-listings` (identity, off `listing_broker_public`) and then
  `GET /brokers?ids=` (contact, off `brokers_public`) — the second unable to name its
  `broker_id`s until the first had answered. The contact pair was never anywhere else: it
  lives on `brokers`, the row `listing_broker_public` already joins and already filters to
  `status='active'`. `EXPLAIN (ANALYZE, BUFFERS)` on the 48 real board listing ids: step 1
  **518 buffers** (331 hit / 187 read, 35 rows); step 2 **207 execution + 436 planning
  buffers** — of which 108 are the same `brokers_pkey` scan step 1 had just done and 99 the
  same `firms_pkey` lookups. The widened view re-measured at **520 buffers, same plan, same
  node shape** (±2 is page-cache noise), so the second read was 100% duplicate work plus a
  second ~270–410 ms Railway floor. **Not a PII widening**: `listing_broker_public` is
  API-only under Amendment A6 — live ACL `postgres + service_role`, no `anon`, no
  `authenticated`, registered in BOTH `_BROKER_A6_SURFACES` and `_BROKER_PII_RELATIONS` — and
  `apply_pii_policy` masks on the column NAME, so both columns become `has_email`/`has_phone`
  for a non-admin the day they land, with no route change; the migration re-asserts the revoke
  explicitly rather than trusting `CREATE OR REPLACE` to preserve the ACL (`firms_public`
  reached production browser-readable by inheriting the default ACL at CREATE, invisible to
  299's grant-statement sweep until 395). Verified live: ACL unchanged, `anon`/`authenticated`
  `has_table_privilege` both false, and **0 value mismatches against `brokers_public` across
  all 524,613 rows**. Frontend: `pipelineCardBroker` takes one argument, `useListingBrokers`
  makes one call, `BrokerVizitka` lost its chained `['broker-contact', brokerId]` query
  entirely — with it went the "Načítám kontakt…" placeholder and the post-paint reflow it
  reserved width for, plus two states that only existed because contact arrived later
  ("contact read failed but identity didn't", "that read succeeded but held no row for this
  broker"). Holding the broker row now IS holding the answer. `fetchBrokersByIds` deleted
  (zero remaining callers, W3's precedent) — but its **malformed-envelope guard was moved, not
  dropped**, onto `fetchListingBrokersByIds`: with one read left, a 200 carrying an
  SPA-fallback HTML page would otherwise read as "not one card on this board has a broker",
  the exact dark state this module was repointed to end. Ride-along, in the same file the
  remaining call authenticates through: **`PyJWKClient(lifespan=3600)`** (`api/dependencies.py`
  — it was PyJWT's 300 s default, so once every five minutes the next identity-gated request
  paid a blocking outbound JWKS fetch on a single uvicorn worker before it could even look at
  the token). Rotation stays safe because `get_signing_key` falls through to
  `get_signing_keys(refresh=True)` on an unknown `kid` — verified in the installed PyJWT
  2.10.1 source, not assumed — so the lifespan bounds staleness of keys we no longer need,
  never the latency of adopting a new one. New rails: the widened row's masking asserted on
  the actual `by-listings` shape (values → flags for a non-admin, values for an admin, and
  `has_email=false` surviving as a real "unreachable" answer), and a frontend test pinning
  that identity and contact arrive on one row.
- ✅ **W9b** — `listings_public` gains `source_id_native` + `property_id` (migration 420) —
  the listing-detail chain's SERVER half, to W9a's client half. Two facts about the row the
  page was already holding were reachable only through a second relation, and the shape of
  that detour is the finding: `property_sources_public` is **a thin view over `listings`
  itself** (`where property_id is not null`), so `fetchPropertySources`' opening hop —
  `select property_id from property_sources_public where id = <this listing>` — re-read the
  very heap tuple `listings_public` had just returned, one PostgREST round trip later, to
  learn one of its own columns. Measured live: that hop is **7 execution buffers (+672
  planning)**, and the widened detail read is **7 buffers** — the same 7, the same tuple.
  The win is deliberately NOT blocks: it is one fewer request and one fewer waterfall level,
  which is corollary B's whole point (a 7-block statement and a 700-block statement cost
  nearly the same from Prague). On the legacy route — a bookmarked/shared `/listing/{id}`,
  a map popup's raw `<a href>`, the extension — the chain was `listing → sources → {MF,
  status events, pipeline funnel, canonicalization}`; all four now start one level earlier,
  off `listingQ` directly, with the sources read running alongside for the multi-portal list
  rather than in front of them. `source_id_native` also retires a genuinely fragile line: the
  legacy→canonical redirect used to find THIS listing's native id by scanning the sibling
  source list for `s.id === listing.id`, with a comment explaining that matching on
  `sreality_id` instead would make `null === null` pick the first null-sreality sibling. It
  reads its own column now. **The fast path is an ARGUMENT, never a gate** —
  `fetchPropertySources(id, knownPropertyId?)` takes it only when a NUMBER arrives, and the
  canonical route (where W9a made this fire in parallel with the listing read) passes nothing
  and resolves as before; gating it would have traded one hop for a whole waterfall level,
  undoing W9a. A NULL `property_id` means "ask", not "there is none": that is the ~5-min
  pre-attach window (rule #19), which is exactly the window `property_sources_public` cannot
  answer for either. **No PII** — this is the browser-readable view, so constraint 3 governs:
  `source_id_native` is the portal's own public advert id, already printed in every canonical
  URL the SPA links to, and `property_id` is an internal grouping surrogate. Written against
  the LIVE `pg_get_viewdef`, with 398's `null::text as broker_email/broker_phone` projections
  carried forward **verbatim** — they cannot be dropped (`CREATE OR REPLACE VIEW` can't remove
  a column and matviews depend on this view), and `_NULLED_CONTACT_COLUMNS` re-derives that
  exemption from the deparsed body every run precisely so a careless replace that restores the
  source expression fails instead of being waved through. Verified live: ACL unchanged
  (`authenticated` SELECT, `anon` dark), the two columns appended after `id` so no consumer's
  order moves, **zero** rows where `broker_email`/`broker_phone` is non-NULL, and **0
  mismatches on 20,000 rows** between the view, the base table and `property_sources_public`
  — plus **0 listings pointing at a non-active (`merged_away`) property** across the whole
  table, so the two paths cannot disagree about which side of a merge won. New rails: a
  read-budget test on `fetchPropertySources` (one relation read when the id is known, two when
  it isn't, two for a NULL, one when nothing resolves) and two `ListingDetail` cases — the
  legacy URL canonicalizing with the sources read hanging forever, and the pre-attach fallback.
  Observed in passing, **not** introduced here and left alone: the legacy route has always
  re-fetched the listing once after canonicalizing (the key carries the route form), and W9b
  makes that happen sooner rather than more often.
- ✅ **W7a** — Browse + comparables onto the shared layer. **Frontend only, no migration.**
  Browse's card photos were fetched INSIDE `fetchListingsForCards`' queryFn — `CardRow` carried
  an `images` array, and the function awaited every card's photos before returning a single row,
  so **not one card painted until all 24 carousels had landed**. Measured live on 24 real
  `browse_list` ids: **178 image rows, 178 correlated CLIP-tag lateral probes, 750 buffers,
  ~131 ms**, all of it on the paint path. Unlike W4's board read, **none of that work is
  wasteful** — the carousel renders those rows — which is exactly why the fix is to move it OFF
  the paint path rather than shrink it to a cover. Cards now paint from `browse_list` alone and
  photos arrive through `lib/hydration`'s new `useListingPhotos`. The grid does not reflow when
  they land: `ImageCarousel` always owns its `aspect-[5/4]` box, so the frame is drawn at the
  card's first paint and the photo fills it in place.
  **The subtlety that nearly shipped a regression: Browse is an INFINITE list.** `rows`
  accumulates every page loaded so far, so a single cumulative cohort key would change on every
  append and drag all the earlier pages' photos back over the wire — **O(n²)** rows read across
  n pages (~900 re-read at page 5 to learn about the 178 that are new), quietly replacing a
  blocking-but-linear read with a non-blocking quadratic one. `useListingPhotos` therefore issues
  **one query per page-sized bucket in ARRIVAL order** (`useQueries` + `combine`): appending page
  2 adds exactly one bucket and leaves bucket 1's cache entry untouched, so cost is O(n) again —
  the same rows the old per-page read fetched, minus the blocking. Arrival order, not sorted, is
  load-bearing: sorting the cohort before slicing would reshuffle every boundary on each append
  and defeat the whole thing (Browse's default sort is newest-first, so page 2's ids are typically
  *lower* than page 1's — the worst case). `combine` keeps the merged map referentially stable;
  merging outside it would mint a new Map, a new context value and a re-projection in every card
  on every unrelated render. `isPending` reports only the FIRST bucket, so a page still loading
  can't make cards already on screen claim their photos are in flight.
  **`perId` is in the cache key** — it is a client-side retention cap on the *same* server read
  (`images_public` has no per-listing LIMIT), so Browse's 50 and the comparables' 6 are different
  payloads over one cohort; leaving it out would let whichever surface asked first serve the other
  a silently truncated carousel. **`photosPerId` on the provider is opt-in and defaults to OFF**:
  the Pipeline board renders one cover and must not start pulling whole carousels merely because
  it mounts the shared provider.
  Comparables joined the layer for the modern id-space, with the frozen one quarantined beside it:
  two queries but **never two requests** — within one run's `comparables_used` the set is
  homogeneous (all surrogate post-#879/#892, or all legacy), so one id array is always empty and
  both fetchers short-circuit without touching the network. The surrogate arm gains what its
  hand-rolled key never had — `idsKey` normalisation (the old key was `cids.join(',')`:
  order-dependent and un-deduplicated, so re-sorting the comparables table was a fresh key rather
  than a cache hit), plus `keepPreviousData` and the one shared decoration `staleTime`.
  **Loader count, honestly: four hand-rolled `images_public` selects → two, not one.**
  `fetchImagesByListing` (listing detail) collapsed into `fetchImagesForListingIds` — it differed
  only in `.eq` vs `.in` and in being uncapped, so it is now a one-id call at `perId: Infinity`
  (uncapped is required: the gallery renders every photo and a cap would truncate the lightbox).
  The other two **must not** merge and the reasons are different: `listing_cover_public` is a
  different QUERY (server-side `DISTINCT ON`), and asking the multi-image read for `perId: 1` is
  precisely the 901-rows-for-44-cards pattern W4 deleted; `fetchImagesByListingIds` is keyed on
  `sreality_id` for callers whose upstream read model carries no surrogate id (/clip-audit's
  property feed, frozen pre-#879 runs), and flipping it is a silent half-swap — the id spaces
  overlap, so a `sreality_id` fed into an `IN listing_id` matches a DIFFERENT listing. Moving
  those needs a backend change to their payloads, not a rename. That reasoning is now written into
  `lib/hydration/index.ts` so the next reader doesn't re-litigate it.
  **One defect reached production and was caught by this wave's own new smoke assertion,
  fixed immediately after (#1144).** The first cut made only `photos` opt-in and left covers and
  brokers always-on — reasoning about the direction the board cared about, not the one Browse did
  — so Browse mounted the provider and silently began fetching a cover per card and a broker per
  card that **nothing on the page displays**: /browse **22 → 24 requests**, the two extra being
  `listing_cover_public` and `POST /brokers/by-listings`, found by listing the live request URLs
  rather than guessing at the delta. Asymmetric defaults are how that happens. Every decoration is
  now opt-in through one `renders={{ … }}` prop that makes each surface declare what it draws —
  the north star written as a prop signature — switched off by handing the hook an EMPTY id list,
  so its existing `ids.length > 0` gate does the work and there is no second flag to keep in step.
  A test pins it. The same run also failed this wave's new carousel assertion on a **16**-photo
  card: the check used `/[2-9]\d*$/` for "more than one photo" and 16 starts with a 1 — the app
  was right and the probe was wrong, now parsed as a number.
  New rails: the first Browse-card tests in the repo (there were none) — the grid painting with
  the photo read hanging forever, **the carousel keeping all 7 of a listing's photos rather than
  collapsing to a cover** (the ledger's explicit warning for this wave), the genuinely-no-photos
  case kept distinct from the pending one, and one cohort read keyed on the surrogate id; five
  `photoBuckets` cases pinning append-stability and arrival order; three more key-disjointness
  cases. The production smoke check gained a **Browse multi-photo-carousel assertion** — this is
  the wave that could leave the grid rendering perfectly with every photo silently gone, and every
  existing Browse assertion would still have passed.
  **← stop point 2 — pausing here for review before W7b onward.**
- ✅ **W7b** — media delivery: a stable cache key for bytes we already sent. **No migration,
  no query change** — so constraint 6's currency doesn't apply here and the honest substitute
  is bytes on the wire, measured against production. The premise held exactly. `GET /images/{key}`
  is already the right shape (a stable proxy URL 302-ing to a presigned R2 GET, so the private
  bucket streams straight to the browser), and the redirect is deliberately cached for only an
  hour so an R2 credential rotation self-heals instead of stranding browsers on a dead signed
  URL for days. But SigV4 signs off the **wall clock**: probing production twice, two seconds
  apart, returned `X-Amz-Date=…T192941Z` and `…T192943Z` with different signatures — **the target
  changes on literally every request**. The browser's HTTP cache keys on the whole URL, query
  string included, so the hourly re-mint made every already-downloaded photo unaddressable. And
  the bytes are eminently cacheable: R2 answers `Cache-Control: public, max-age=2592000` with a
  strong ETag on a **300 KB** JPEG. A 30-day cache directive that could never apply twice, across
  **10,228,162** stored images.
  The fix pins the signing timestamp to the start of the current UTC day, so one key presigns to
  a byte-identical string all day. **The 1-hour redirect cache is untouched** — that was the whole
  design constraint: the re-mint still happens hourly (rotation still self-heals within the hour),
  it just now returns the same string, so the hourly cost is one header-sized 302 and **zero image
  bytes** instead of a full re-download. A 24× reduction in re-mint boundaries, and the anchor is
  the *only* thing changed about the signature.
  **Anchoring is signed through botocore's own auth class, not reimplemented** — a four-line
  subclass overriding `_modify_request_before_signing` to overwrite `request.context['timestamp']`
  before `super()` reads it. `add_auth` stamps "now" into that context and then *every* downstream
  consumer (the `X-Amz-Date` param, the credential scope, the string-to-sign, the derived signing
  key) reads it back out, so overwriting it in that one place pins all four consistently with zero
  crypto of our own. The rail that makes this safe is an **equivalence test**: freeze botocore's
  clock to the anchor instant, ask boto3 for an ordinary presigned URL, and assert the anchored
  signer returns that byte-identical string. **It immediately failed on a real defect** — the first
  cut subclassed the generic `SigV4QueryAuth`, but boto3 resolves `s3v4-query` → `S3SigV4QueryAuth`,
  which signs the constant `UNSIGNED-PAYLOAD` instead of a body hash and skips path normalisation.
  Every field of the URL matched and *only the signature was wrong*: 253 identical leading
  characters, then a different hash. That defect is invisible to any test that checks URL shape,
  invisible to a local run without credentials, and would have 403'd **every photo in the product**
  on deploy. Signing against the reference implementation rather than against a schema is what
  caught it.
  **Bounded and reversible.** The anchor must stay well under the 7-day presign TTL — a URL minted
  in the bucket's last second was signed at its *start*, so it carries `TTL − anchor` of remaining
  life (6 days here); let the two meet and the last request of every bucket gets an already-expired
  URL. A test pins that invariant, and the route caps any override at `TTL / 2`.
  `IMAGE_PRESIGN_ANCHOR_SECONDS` is read **per request**, so `0` reverts to per-request signing
  from the Railway dashboard with no redeploy — the kill switch for the one thing that cannot be
  proven locally (that R2 honours a backdated signature). It is a well-founded assumption rather
  than a guess: a presigned URL signed 12 hours ago and one minted now with a 12-hour-old
  `X-Amz-Date` are the same artefact to the server, which is exactly what the existing 7-day
  `X-Amz-Expires` already presumes, and the 15-minute skew rule governs *header*-based SigV4, not
  query presigning. Verified end-to-end against production after deploy regardless.
  **The building-attachment half was a different bug with the same shape.**
  `GET /buildings/{id}/attachments/{aid}/raw` doesn't presign at all — it is a bearer-gated byte
  proxy (an `<img>` can't send a bearer header, so `AttachmentCard` `fetch`es it into a blob URL)
  — and it sent **no `Cache-Control` whatsoever**, outside React Query, in a bare `useEffect`. So
  every mount re-pulled the entire file, up to the 25 MB cap, over *two* hops: R2 → Railway →
  browser. The bytes are immutable by construction (the bucket key carries a per-upload uuid, an
  edit is a new row at a new key, a delete removes the row), so they take
  `private, max-age=86400, immutable` — **`private`, never `public`**: operator-only uploads behind
  a bearer token must not be stored by a shared cache. A test pins that the directive is cacheable,
  that it is never `public`, and that caching didn't widen what the route will serve (a foreign
  building id still 404s).
  **No ratchet earned, and that is the correct result** — the sweep counts app-data requests and
  explicitly excludes image bytes, so this wave moves no number on that table by construction; no
  swept route renders a building attachment either. Its evidence is bytes, and its rail is the
  behavioural check that photos still *load* (a wave that breaks image signing renders a perfect
  grid full of broken images, and every request-count assertion would still pass).
- ✅ **W10c** — /costs: the date-expression index that could not be built (migration 421).
  **The wave's premise was right and its prescription was impossible**, which is the finding.
  `llm_cost_daily_public` buckets on `l.called_at::date`, and casting a *timestamptz* to a date
  depends on the session TimeZone — so the expression is only STABLE, and Postgres flatly
  refuses to index it: `42P17: functions in index expression must be marked IMMUTABLE`
  (probed live, not recalled). There was no index to add until the expression itself was
  pinned to an explicit zone. Doing that is a **latent correctness fix in its own right**:
  which day a call was attributed to silently depended on whatever TimeZone the *reading*
  session carried, so the same row could land in different buckets for different readers.
  Verified a no-op on live data before relying on it — `(called_at)::date` vs
  `(called_at at time zone 'UTC')::date` disagree on **0 of 293,551 rows**, and the rollup
  returns an identical **93 groups / 62,362 calls / 53,842 errors / $20.7555** either way.
  **The hourly twin was the worse offender and was in scope because it is the same page.**
  `/costs` renders both; fixing only the daily half would have left the seq scan in place.
  `llm_cost_hourly_public` buckets on `date_trunc('hour', called_at)` — same STABLE problem —
  and at the 49-hour window the chart actually asks for it discarded **293,491 rows to return
  12**, i.e. it read the entire table for a two-day chart. Its rewrite has one extra
  constraint: `bucket` must stay `timestamptz`, because the SPA parses it with `new Date(…)`
  and a bare `timestamp` would be read as browser-local and shift the whole chart — so it
  truncates in UTC and labels the result UTC, keeping both the type and every value identical.
  `EXPLAIN (ANALYZE, BUFFERS)` live, at the windows the page really requests (35 days / 49
  hours), **blocks including temp, since the sort spilled to disk**:
  **daily 9,711 shared + 5,839 temp (23 MB external merge) → 10,851 shared + 0 temp**;
  **hourly 9,941 → 8 blocks (~1,240×)**, 60 rows read for 12 out. The hourly is now
  proportional to what renders, which is the north star stated literally. The daily is the
  honest, modest one — 35 days is ~21% of the table, so no index can make a fifth of a heap
  cheap; **shared blocks actually go UP** (9,711 → 10,851) and the win is that the external
  merge sort is *gone entirely* (the index delivers GROUP BY order, so GroupAggregate consumes
  the scan directly), taking 5,839 temp blocks and a 23 MB disk spill with it — net 15,550 →
  10,851 with no disk traffic. Wall clock moved 1.65 s → 0.10 s, but the same statement also
  measured **11.5 s** on one baseline run this session: corollary D, exactly, which is why
  the claim above is in blocks.
  **Deliberately NOT a covering index.** The obvious `include (cost_usd, input_tokens, …)`
  variant was built and measured: **worse** (11,529 blocks vs 10,851) and **13× larger**
  (29 MB vs 2.2 MB). An Index Only Scan is unreachable here — the planner does not match this
  expression index for index-only (confirmed by re-measuring with the `error` aggregate
  removed, and again after a `VACUUM ANALYZE` that took the table from 59.2% all-visible to
  fresh: still a plain Index Scan, identical 11,529 blocks) — and on an append-only table the
  newest heap pages, which are exactly the ones a recency query reads, are the least likely to
  be all-visible anyway. Paying for an INCLUDE that can never be used is pure write
  amplification. The lean index costs **2.2 MB** and does all the work; btree deduplication
  does the rest (`called_for`/`provider`/`model` have 10/4/9 distinct values).
  Both views stay **SECURITY DEFINER** — `reloptions` NULL, gated by `is_platform_admin()` in
  the outer WHERE. These are admin-only reporting views over a shared table, **not**
  per-account RLS views, so constraint 2's `security_invoker` would be wrong here rather than
  safer, and they are registered as `_ADMIN_ONLY_RELATIONS`, not `_TENANT_VIEWS`. Verified
  live after the migration: ACL byte-identical to before
  (`authenticated=rDxtm`, `service_role` full, **`anon` absent**), `has_table_privilege` false
  for `anon` and true for `authenticated`, `reloptions` still NULL. `create or replace view`
  preserves the ACL; the migration re-asserts it explicitly anyway, W6's precedent.
  **Left standing, measured in passing:** with the hourly aggregate down to 8 blocks, the
  `is_platform_admin()` gate itself is now the dominant cost of that read (~332 blocks at the
  Result node). Filed, not fixed — it is one `stable` call and chasing it is a different wave.
  Filed too: the daily read is still 62,362 rows aggregated to 93, so the only thing that makes
  it truly proportional is precompute (corollary C), which needs a story for *today's* partial
  bucket that a cost dashboard cannot show stale.
- ✅ **W10d** — /health: read 15 rows by reading 15 rows (migration 422). **The bound this
  target wanted turned out not to be a time window, and checking that first is the whole
  point.** `pipeline_checks_public` is a latest-row-per-`check_key` read — 15 keys out of a
  6,234-row append-only results table — and "bound the history" was the obvious reading. The
  UI does not ask for one: `fetchPipelineChecks` sends no filter at all, and
  `pipelineChecks.ts` deliberately humanizes keys from **retired** checks ("Historical rows
  from retired checks … fall through to the humanizer"), so a date window would have silently
  deleted exactly the rows that comment exists to preserve. The real bound is "one row per
  key" — which the view always meant; it was just computed the expensive way.
  **The index was already there, already used, and that is the finding.**
  `(check_key, run_at DESC)` exists and the plan was already an Index Scan + Unique with no
  sort — so unlike W10c there was no index left to add. DISTINCT ON still has to *walk* every
  index entry to find each group's boundary, and Postgres 17 has no B-tree skip scan, so the
  planner cannot do better with that SQL: it read all **6,234** entries and fetched all 6,234
  heap tuples (the `details` jsonb makes them wide) to emit 15 rows. Replaced with the classic
  **loose index scan** — a recursive CTE hops key-to-key through the index (16 descents: 15
  keys plus the terminator), then one LATERAL `limit 1` per key takes that key's newest row.
  `EXPLAIN (ANALYZE, BUFFERS)` live on the exact statement the page issues: **3,351 → 93
  blocks (~36×)**, 6,234 rows read → **15**, 5,023 ms → 47 ms. The shape matters more than the
  ratio: the old read got monotonically more expensive with **every check run ever recorded**,
  and the new one is pinned to the number of keys.
  **Semantics preserved exactly, including the tie-break — deliberately.** DISTINCT ON with
  `order by check_key, run_at desc` breaks a `(check_key, run_at)` tie arbitrarily, and so
  does `order by run_at desc limit 1`. Adding `, id desc` would make it deterministic, and it
  was measured (96 → 117 blocks, an Incremental Sort appears) — but that is a behaviour change
  smuggled into a performance migration, and live there are **0 tied pairs**, so it would buy
  nothing today. Filed as its own change if wanted. Equivalence verified live both before and
  after applying: 15 rows each way, set-compared across every column including the `details`
  jsonb, **0 rows in either direction**.
  Scope was checked rather than assumed — swept every view behind /health for the same
  `distinct on` pathology and `pipeline_checks_public` is the **only** one, so unlike W10c's
  hourly twin there is no sibling to fix. Unchanged: SECURITY DEFINER + `is_platform_admin()`
  as a One-Time Filter above the CTE (a non-admin never executes it); ACL verified byte-identical
  after the migration (`authenticated` SELECT, **`anon` dark**), `reloptions` still NULL.
  Rails: the rewrite is subtle in ways a shape assertion cannot see, so the test **executes the
  view** against the replayed schema — latest-per-key, the terminator NULL never leaking, every
  key represented exactly once, an empty table yielding no rows rather than an error, the
  single-key tightest loop, and an EXCEPT-both-ways equivalence against the `distinct on` it
  replaced — each in a transaction that always rolls back, plus a gate-is-open guard so none of
  it can pass vacuously. One more pins the *mechanism*: a revert to `distinct on` would stay
  perfectly **correct** while silently full-scanning again, which correctness tests cannot catch.
- ✅ **W10e** — /estimations OR-join: **measured, and it is a non-issue. No change shipped.**
  This entry is the deliverable. The target was flagged "code-derived, not measured", and
  measuring it is what retired it — the instruction to EXPLAIN first existed precisely so a
  plausible-looking OR wouldn't get optimised on the strength of how it reads.
  **First correction: the OR is not on /estimations.** That page's list read
  (`list_estimation_runs`) has no OR at all — it keys on `er.input_listing_id = ANY(...)`,
  keyset-paginated, and counts only on the first page. The only OR-across-join-paths in
  `api/estimation_runs.py` is in `latest_rent_estimations_by_listing`, which feeds **Browse's
  on-card estimate chip**: `ON er.input_listing_id = l.id OR (er.input_listing_id IS NULL AND
  er.input_sreality_id = l.sreality_id)`.
  **The planner does not degrade on it.** `EXPLAIN (ANALYZE, BUFFERS)` on the real 24-card
  shape, run twice — once on ids with no estimations, once on ids that all have one, because a
  conclusion drawn only from an empty result proves nothing. Both give the same plan: a Nested
  Loop over the 24 listings with a **BitmapOr that index-scans EACH arm separately**
  (`estimation_runs_input_listing_id_idx` and `estimation_runs_input_sreality_id_idx`) — not
  the seq-scan fallback an unindexable OR would force. Matching path: **188 blocks for 24
  rendered rows** (~8 per row), 35 rows through the join, `Rows Removed by Filter: 0`, 12.6 ms.
  Non-matching: 147 blocks, 0 rows. And the dominant term is not the join — it is the
  `listings` lookup (97 of 188 blocks); the 24 OR probes together cost 80.
  **The scale makes it moot regardless**: `estimation_runs` is **100 rows / 264 kB**, smaller
  than one Browse card's photo. W10a's lesson that "a single OR forces a BitmapOr, which always
  visits the heap regardless of indexing" is still true here — it visited 32 heap blocks — but
  that lesson bit on 88,762 region-grain rows, and it does not transfer to a 100-row table.
  **The second arm is provably dead today and is still correct to keep.** Zero rows have
  `input_listing_id IS NULL AND input_sreality_id IS NOT NULL`, so that branch currently matches
  nothing and could be deleted for a small planning saving. It should not be: the code comment
  documents it as the fallback for rows written before `input_listing_id` was stamped (#914),
  and NULL-`input_listing_id` rows are the late-binding case the resolver stamps once the
  listing is scraped. Deleting a documented safety net to buy nothing measurable, on the
  strength of "0 rows *today*", is the kind of change this sprint's audit exists to catch.
  **Filed, not fixed** — one real oddity surfaced: **planning costs more blocks than execution**
  (1,069 vs 188). `estimation_runs` carries **17 indexes** and `listings` is large, so the
  planner reads a lot of catalog for a query that then touches almost nothing. Every planning
  buffer was a `hit` (cached, not I/O) and it is per-statement rather than per-row, so it is not
  this wave's problem — but on a surface governed by corollary B, where the Railway floor already
  dominates, it is worth knowing that the cheapest reads are now planning-bound. An index audit
  on `estimation_runs` is its own wave if anyone wants it.
- ✅ **W8** — bundle: **first-visit JS more than halves, 607.6 → 282.9 kB gzip (−53%)**.
  Both halves of the ledger's one-line prescription were literally right, and the second one
  was the load-bearing half.
  **maplibre out of the filter-controls barrel.** `filter-controls/index.ts` re-exported
  `LocationControl`, which statically imports `maplibre-gl` (801 kB raw). A barrel is a
  static edge, so `import { MultiselectChips } from '@/components/filter-controls'` dragged
  the whole map engine along — and `FilterForm.tsx` does exactly that while **never rendering
  a map**. The barrel now exports only the `CenterRadius` *type* (erased at build time, free),
  and the two sites that genuinely draw the control — Browse's sidebar, where it renders only
  in `center_radius` mode, and `/watchdog/:id` — load it through `lazyChunk`. Both reserve the
  control's height so arriving doesn't jump the layout.
  **recharts unpinned from React — and this is the finding.** All three chart consumers
  (`PriceLineChart`, `Health`, `Costs`) were ALREADY lazy, one of them carrying the comment
  "Lazy-loaded so recharts stays out of the detail-page entry chunk". It wasn't. The cause was
  `manualChunks: { recharts: ['recharts'] }`: the object form claims a listed module **and
  every dependency nothing else has claimed**, and recharts was react/react-dom/scheduler's
  only *statically* reachable importer — so **React got folded into the chunk named
  `recharts`**. Proof, straight out of `dist/`: the entry read
  `import{r as m,a as Tr,R as Dg,b as ce}from"./recharts-DdjNNwFT.js"`, **31 of the built
  chunks** imported that same file, `react-dom` and `scheduler` strings were inside it, and the
  maplibre chunk opened with `import{c,g}from"./recharts-*.js"`. No amount of lazy-importing a
  chart could ever have helped: recharts was on every route's critical path *because React was
  inside it*. An attempt to fix it by adding `react: ['react','react-dom']` to the same map
  produced a **921-byte** react chunk — the object form matched only an ESM shim while the real
  CJS body stayed put — which is what made removing the map, rather than extending it, the
  answer. With every heavy consumer already behind `lazyChunk`, Rollup's automatic splitting
  gets it exactly right: **the entry ends with ZERO static chunk imports and index.html emits
  ZERO `modulepreload` links**, while maplibre and recharts each become ONE shared async chunk
  (verified: the map chunk is imported by all five map components, the chart chunk by all
  four chart consumers — split, not duplicated).
  Measured on real `npm run build` output, gzipped, cold first visit:
  **before** entry 239.1 + recharts 151.0 + maplibre 217.5 = **607.6 kB**;
  **after** entry alone = **282.9 kB**. The entry itself grew (239.1 → 282.9) because React
  moved into it — one file got bigger and the total still more than halved, which is why the
  honest unit here is the first-visit *set*, not any single chunk.
  **The old budget could not have caught this, in the same shape W7a's could not.** CI summed
  `index-*.js` alone and called it "what loads before any lazy import", commenting that
  maplibre and recharts "only count when their route is opened" — a claim that was false the
  whole time it was written down. It read ~239 kB and stayed green while a first visit cost
  ~608 kB. `frontend/scripts/bundle-budget.mjs` replaces it: it parses `index.html`, takes the
  entry **plus every preload plus their transitive static imports**, and fails on a gzip budget
  — and separately fails if either library is on that path, matched **by content marker, not by
  filename**, because Rollup names an auto-split chunk after whatever it hoisted (the same
  maplibre bytes shipped as `maplibre-*.js` and as `basemap-*.js` across the configs tried
  here, so a filename check would have quietly stopped matching). **Verified it actually
  fails**: rebuilt against the pre-fix `vite.config.ts` and the rail exited 1. A rail that has
  never been seen to fail is not a rail.
  No React or framework version was touched — strictly import and chunk boundaries.

## Baselines — 2026-08-24, post-W-1a/W-1b, production

Per-route app data requests (PostgREST + Railway only; basemap tiles, assets and image
bytes excluded) and settle time, measured by `npm run smoke-check:prod`:

| Route | W0 | post-W2a | post-W2b | post-W5 | post-W6 | post-W7a | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| /browse | 31 | 27 | 22 | 22 | 22 | 22 | was 9.1 s *and an HTTP 500* pre-W-1a. W7a earned **no** ratchet here and that is the honest result: the photo read moved off the paint path, it did not disappear — one request became one request. The win is that cards no longer wait for it. |
| /pipeline | 27 | 23 | 19 | 18 | **17** | 17 | time-to-first-card 1,427 → 951 ms (W1) → 412 ms (W5, killed the board's 2nd request's serialization) → **368 ms** (W6) |
| /collections | 9 | 5 | 5 | 5 | 5 | 5 | was 6-of-9 duplicated bootstrap |
| /watchdog | 10 | 6 | 6 | 6 | 6 | 6 | reference feed — shape was already correct |
| /notifications | 9 | 5 | 5 | 5 | 5 | 5 | server work is ~15 ms; the rest is transport |
| /brokers | 11 | 7 | 7 | 7 | 7 | 7 | leaderboard is server-bound, see W10a |

**Request count is not the only ratchet, and W7a is where that shows.** Two of the three waves in
this batch moved a number on this table; W7a moved none and is still the biggest structural change
of the three. What it earned instead is a *behavioural* rail — the smoke check now asserts Browse
cards hydrate a multi-photo carousel — because the failure mode this wave introduces is not "one
more request", it is "the grid renders perfectly and every photo is silently gone". A route budget
would never have seen that; it caught the covers/brokers leak within minutes of deploy. Settle
times are deliberately NOT ratcheted: /browse measured 2.2 s, 3.0 s, 5.3 s and 6.1 s across four
runs of the same build this afternoon, which is corollary D's whole point.

First-visit JS, gzipped, from real `npm run build` output (W8). This is the *set* a cold
visit fetches — entry + every `modulepreload` + their static imports — not any one chunk,
because measuring one file is exactly how the old budget missed 368 kB:

| | Entry | recharts | maplibre | First-visit total |
| --- | --- | --- | --- | --- |
| pre-W8 | 239.1 kB | 151.0 kB (preloaded) | 217.5 kB (preloaded) | **607.6 kB** |
| post-W8 | 282.9 kB | async only | async only | **282.9 kB** (−53%) |

Enforced by `frontend/scripts/bundle-budget.mjs` (blocking, 330 kB), which also fails if
either library returns to the first-visit path — matched by content, not filename.

Server-side block counts to beat (constraint 6):

| Read | Blocks | Target | Wave |
| --- | --- | --- | --- |
| `browse_list` default cohort | 15,877 → **6** | done | W-1a |
| broker leaderboard | 6,776 → **3,108** (warm) | ~500 (close, not hit — floor is a `brokers` seq scan at 55% selectivity) | done — W10a |
| board images (44 cards) | 3,995 → **788**; 901 CLIP laterals → **44** | 44 rows / 44 probes | done — W4 |
| `llm_cost_daily_public` | 9,711 shared + 5,839 temp → **10,851 shared + 0 temp** | index — hit, but the win is the sort, not the blocks | done — W10c |
| `llm_cost_hourly_public` | 9,941 → **8** (293,491 rows discarded for 12 out → 60 read) | proportional | done — W10c (same page, same defect) |
| `pipeline_checks_public` | 6,234 rows scanned for 15 → **15 for 15**; 3,351 → **93** blocks | bounded — by KEY, not by time | done — W10d |
| Browse chip OR-join (`latest_rent_estimations_by_listing`) | **188** blocks for 24 rendered rows, BitmapOr index-scans both arms, `Rows Removed by Filter: 0` | none — measured a non-issue, nothing shipped | done — W10e |

## Post-sprint audit — 2026-08-24

W2b…W5 were reviewed after the fact (5 parallel reviewers, 8 adversarial verifications, one
triage pass). **Verdict: sound, build on it.** Two adversarial passes escalated seven findings
and downgraded five; W10a was *proven* equivalent to migration 410 across seven parameter
shapes (zero row or value mismatches), W2b's exact-count arithmetic is correct at every
boundary, W3 lost exactly one field (`stage_key`, zero readers app-wide) and closed a latent
rollback hole, W9a's seed is consistent on all four URL forms, and both new views match their
migrations byte-for-byte with tenancy verified live. **Exactly one shipped behaviour
regression**, now fixed. Everything else was guardrail and record-keeping debt.

Fixed in the audit PR:

- **W10b's fail-silent choropleth** (the one regression). Numbers and polygons became separate
  reads, `growthChoropleth` drops any row whose shape is missing, and `main.tsx` wires
  `onError` on the `MutationCache` only — so a failed shapes read painted a blank map beside a
  fully correct table with nothing saying why. Pre-W10b the polygons rode in the numbers
  response, so a populated table implied a populated map. Both consumers now surface it.
- **The shapes RPC was recomputed in full per page.** PostgREST wraps an RPC in LIMIT/OFFSET,
  so `price_stat_growth_shapes(14)` re-ran its whole body for each of 5 pages: measured
  **1.4 s / 10,199 buffers per page**, re-aggregating all 180,506 observation rows every time —
  and since W2b the last four fire in parallel, i.e. four concurrent 1.4 s statements against
  an 8 s `statement_timeout`. `fetchGrowthShapes` now reads the already-materialized
  `price_stat_choropleth_public` (verified identical obec set on all 12 datasets: 43/43,
  831/831, 3,296/3,296, 4,044/4,044, 0/0), keeping the RPC as the fallback for a dataset whose
  run has not written its choropleth yet. These are the `EXPLAIN` block counts constraint 6
  required of W10b and did not get — and they are exactly where the cost had moved.
- **`pipeline_board_public` registered in `_TENANT_VIEWS` — and the registration immediately
  failed CI, on a real defect.** Isolation itself is correct (verified live: smoke-admin sees
  its 4 cards, smoke-nonadmin 0), but the replayed schema returned *permission denied for
  function publication_gate_enabled*. `pipeline_board_public` is the first
  `security_invoker` view to nest `properties_public`: that inner view is definer, so its
  table reads run as its owner, but its WHERE calls `publication_gate_enabled()` and through
  an invoker view that call is checked against the INVOKER. Migration 273 granted EXECUTE to
  `anon, authenticated`; **migration 299 PART E revoked it and nothing ever re-granted it** —
  so production only works because it still carries the pre-299 grant, i.e. prod has drifted
  from the chain and the drift is the only reason the board loads. A restore, a preview
  branch, or any environment built from migrations would serve every operator a 500 on
  /pipeline. Migration 418 re-grants it to `authenticated` only (`anon` stays dark). Safe:
  299's own comment scopes PART E to functions that could "trigger a full
  browse_list/matview rebuild [DoS] … or run the tenant seeders/backfill", and
  `publication_gate_enabled()` is a `stable`, argument-less, one-row boolean read — it was
  collateral in that sweep. **This is the strongest argument in the whole audit for the
  registry rule: the test found in one CI run what live inspection could not see at all.**
- **Four false claims corrected.** `pipelineCache.ts` and this ledger both said the board cache
  carries the *photo* — it deliberately does not, and re-adding it is the exact coupling W1
  removed; W3's title claimed "one pipeline cache" when only `card`→`members` collapsed; W9a's
  "the legacy route is unchanged" is true of the URL form but not of an in-app `Link` that
  seeds the id (which the estimations run-links do). Also `docs/architecture.md` still
  documented the deleted `PipelineCard` type and described the board as a two-read client join.
- **The route ratchets W2a earned were never actually applied** — that PR shipped only its
  comment (an editing script aborted before writing, and the commit message was trusted over
  the diff), leaving `/collections`, `/watchdog`, `/notifications` and `/brokers` at ~3× slack.

Accepted deviations — **stop tracking these as debt**:

- **W5 joining `properties_public` rather than base `properties`.** The plan was wrong, not the
  implementation: `properties` carries no `source_id_native` / `sreality_id` / `listing_id` /
  `price_czk` — `properties_public` sources them from `listings`, which is RLS-on with zero
  policies, so an invoker-mode view over the base table would have broken the canonical
  `/listing/{source}/{native}` link on every card. Only the migration header's rationale was
  wrong (it argues RLS and never mentions the publication gate).
- **W5's planned `property_pipeline` covering index** — 48 rows live, max 44 per account.
- **W3 not collapsing `board` into `members`** — they carry genuinely different payloads, and
  merging them is what would tempt someone to re-couple a decoration to the structural read.
- **W2b's `count: 'exact'` on the 18 table-backed relations** — measured 20–320 ms each.

Still open, filed rather than fixed here:

- **`listings_with_city_quality` cannot complete under the 8 s `statement_timeout`** — 11–13 s
  measured on the count-free (pre-W2b) statement shape: a seq scan of 579,049 `browse_list`
  rows with a subplan executed 281,129 times, so cost is per-row and selectivity does not help.
  Adding any city-index or near-city rule in Browse gives an error toast, not a cohort. **Not
  this sprint's doing** (W2b's count adds ~0.5 s and no second scan — PostgREST materializes
  `pgrst_source` once) and no saved preset uses those controls. The fix belongs to the RPC:
  precompute the rule match, as migration 142 already did for the population/proximity half.
- **Reads fail silently app-wide** — `main.tsx` has no `QueryCache.onError`. The W10b fix
  surfaces the two shapes consumers; a general policy needs its own PR (a blanket toast would
  double-surface on the several pages that already render their own error state).
- **The sweep has no `/listing/{id}` route**, so W9b's win (a request and a waterfall level off
  every listing-detail load) is unguarded by the ratchet — it earned no number on any swept route
  because no swept route renders it. Adding a listing step means clicking through from Browse
  rather than hard-coding a URL that goes inactive; worth its own small PR.
- `is_active: r.is_active ?? true` should become a projected tombstone defaulting to `false`;
  `broker_leaderboard` wants `, s.broker_id` in its ORDER BY (pre-existing, display-only, but
  518 brokers tie at the "Vše" option); `listing_cover_public`'s CLIP lateral is unread by its
  only consumer; `pipelineCache` and `PIPELINE_BOARD_COLS` are both untested chokepoints.

## Lane 1 complete — 2026-08-24

**All seventeen waves are shipped.** W8 closes the sprint that opened on "the /pipeline board
takes terribly long to load with 44 cards". That board now paints its first card in 368 ms
(from 1,427 ms) off 17 requests (from 27), and the doctrine that got it there held on every
surface it was pointed at afterwards.

What the last five waves add up to, and what they say about the method:

- **The prescription was wrong three times out of five, and measuring first is what caught it
  each time.** W10c's "date-expression index" could not be built at all — `timestamptz::date`
  is STABLE and Postgres refuses to index it (42P17), so the view had to be zone-pinned first,
  which turned out to be a latent correctness bug of its own. W10d's "bound the history" would
  have deleted the retired-check rows the UI deliberately renders; the bound it actually needed
  was *per key*, and the index it supposedly needed already existed and was already being used.
  W10e was a non-issue outright. Only W7b and W8 were the fix their one-line description said.
- **Three of the five shipped no user-visible change and were still worth doing**, and one
  (W10e) shipped nothing at all. A wave that ends in "measured, it's fine, here's the evidence"
  is a real outcome; the ledger entry is the deliverable.
- **Every one of the five was caught out by a test, a rail, or the planner at least once.**
  W7b's equivalence test caught a wrong SigV4 base class that would have 403'd every photo in
  the product on deploy. W10d's CI run caught a syntax error in the rail itself. W8's fix was
  invisible until `dist/` was read directly — the source said "lazy-loaded so recharts stays
  out of the entry chunk" and had been wrong for as long as it had been written.
- **The budgets that existed were measuring the wrong thing, twice, in the same shape.** W7a's
  route-request budget could not see photos vanishing; W8's bundle budget summed one file and
  called it the first visit while 368 kB arrived beside it. Both were replaced with assertions
  about *behaviour and the real artifact*, and both replacements were verified to fail before
  being trusted. **A budget nobody has watched fail is a comment, not a test.**

Where the remaining cost sits, honestly: not in query plans any more. `/costs`' hourly read is
8 blocks and its admin gate costs ~332; the Browse chip's cheapest reads are planning-bound
(1,069 planning buffers against 188 of execution). Corollary B's ~270–410 ms Railway floor and
the instance's I/O saturation — the same statement measured 97 ms and 2,083 ms on identical
block counts — now dominate everything this sprint can reach from the client. The open items
below are the honest next frontier, and the biggest of them (`browse-list-rebuild`'s root cause
and instance sizing) are the operator's call, not a wave.

## Parked, with re-entry triggers

Multi-category cohort ordering (revisit if a preset drops its district filter) · Browse map
payload 16.8 MB · instance sizing (after W-1b root-cause and W10a) · React Query persistence
· /notifications 100-row cap · repr-flip semantics (own PR, see W4) · connection pool +
gzip middleware · the unexplained client-side count abort on Browse.

---

# The Cardinality Doctrine — follow-up build, opened 2026-08-25

The hydration sprint closed with five north-star violations still standing. A 15-agent
investigation (3 architects, 2 judges, 2 adversarial critiques) produced a ruling proposal
that **corrected twelve of its own commissioned premises** before designing anything. The
headline correction: the commission assumed "only precompute fixes it", and that is true
for exactly **one** of the five. The other four are the same plan defect four times —
an expression that does not vary across the scanned rows being re-evaluated per row —
and **two of the five delete state**.

Same ledger, same rules, plus what this build's evidence earned:

- **Corollary E — the derived-state rule.** Every precomputed artifact declares, in the
  migration that creates it, *who produces it, on what cadence, and the staleness budget
  beyond which rendering it is wrong*. An artifact nobody can watch go stale is not
  precomputed, it is cached by accident.
- **Corollary F — the completeness rule.** A surface that cannot render its whole cohort
  must say so in the cohort's own terms. **A `LIMIT` without an `ORDER BY` is a sample, and
  a sample chosen by the planner is not a contract.**
- **Corollary D, rewritten.** Block counts are the currency *precisely because they are
  eviction-invariant for a fixed plan* — contention moves blocks between `hit` and `read`,
  not into or out of existence. So: (a) never quote a *timing* claim measured while a
  maintenance job runs; (b) before quoting a *block* claim, assert the plan is the one you
  designed, **by node name**; (c) check (a) with `pg_stat_activity`, never
  `cron.job_run_details` — migration 371's `SET` prefix makes pg_cron publish a provisional
  `succeeded / 'SET' / 0.1 s` row while the real work still burns I/O.
- **Standing methodology rule.** Cheap statements are measured **warm** — in-transaction
  after a warm-up statement, or from `pg_stat_statements`. A first-statement-in-a-fresh-
  backend measurement inflates by ~460 blocks, and **the Supabase MCP opens a fresh backend
  per call**. This artefact manufactured a commissioned item (see W1).

### Waves

| Wave | Content | PR | State |
| --- | --- | --- | --- |
| RN | `verify_pipeline` credit-outage matcher | [#1161](https://github.com/waiff/sreality/pull/1161) | ✅ merged |
| W0a | F1 location index + F2 autovacuum (migs 429, 430) | [#1164](https://github.com/waiff/sreality/pull/1164) | ✅ merged |
| W0b | F3 teardown — 5 objects + jobid 8 (mig 432) | [#1167](https://github.com/waiff/sreality/pull/1167) | ✅ merged |
| W1a | Item 4 — admin gate hoist, 10 policies (mig 431) | [#1166](https://github.com/waiff/sreality/pull/1166) | ✅ merged |
| W1b | Item 4 — 36 view/function wraps (uniform spelling) | — | |
| W2 | Item 1a — `_check_daily_cost` spelling | [#1163](https://github.com/waiff/sreality/pull/1163) | ✅ merged |
| W3 | Item 1b — hour rollup + watermark + F5-minimal registry (mig 437) | [#1174](https://github.com/waiff/sreality/pull/1174) | ✅ merged |
| W4 | Item 3 — broker deferred join (mig 435) | [#1171](https://github.com/waiff/sreality/pull/1171) | ✅ merged |
| W5 | Item 2 — city-quality, keyed on `obec_id` (mig 436) | [#1173](https://github.com/waiff/sreality/pull/1173) | ✅ merged |
| W6 | Item 5 — map cluster RPC | — | |
| W7 | Retirements + registry completion (destructive gate) | — | |

### W0a — the index that was 36.7% of all disk reads (migrations 429, 430)

**Not one of the five.** The platform's single largest disk consumer was never in the
ledger. `location_data/resolver/drain.py` runs every 15 minutes:

```sql
SELECT property_id, <29 columns> FROM listing_location_current
 WHERE property_id = ANY(%s::bigint[]) ORDER BY property_id, listing_id
```

`listing_location_current` carried **15 indexes and not one leading on `property_id`**.

**Baseline, `pg_stat_statements`, live 2026-08-25** — and *worse* than the proposal's own
figure, because the table grew between investigation and build:

| | calls | blocks | blocks/call | mean |
| --- | --- | --- | --- | --- |
| proposal (as commissioned) | 38,800 | 1,410,552,687 | 36,355 | — |
| **live at build time** | **39,488** | **2,737,573,584** | **69,327** | 982 ms |
| (a second registered shape) | 3,827 | 270,177,122 | 70,598 | 230 ms |

**After** — measured warm, in-transaction after a warm-up statement, 200-property batch:

```
Index Scan using llc_property_listing on listing_location_current (rows=387)
  Index Cond: (property_id = ANY (...))
  Buffers: shared hit=434
Execution Time: 1.697 ms
```

| | blocks/call |
| --- | --- |
| before | 69,327 |
| after | **434** |

**160×.** Plan shape asserted by node name per amended Corollary D: `Index Scan using
llc_property_listing`, an `Index Cond`, and **no Sort node**. The cold run of the same
statement reported the identical **434** blocks against 352 hit / 82 read — corollary D's
eviction-invariance, demonstrated rather than asserted.

**Composite and partial deliberately.** Two of the three design proposals asked for a bare
`(property_id)`, which leaves a Sort on the highest-leverage fix in the build. The second
key column serves `ORDER BY property_id, listing_id`; `property_id IS NULL` is never
queried by this shape.

**Worth naming as a pattern:** `drain.py`'s own comment shows this was written *as an
optimisation* — "Every touched property's members in ONE read… It also DE-DUPLICATES the
rebuild." Right as a request shape, wrong at the plan level. **Batching by a key you have
not indexed converts N cheap lookups into one full scan.**

**F2 — autovacuum.** Both location tables were 14.0% / 15.8% dead tuples with their last
autovacuum on **2026-08-13**, on tables rewritten every 15 minutes: at the default 0.2
scale factor the threshold sits ~137k/114k rows behind. Set to 0.02 (≈ hourly at the
current write rate). Every scan was reading ~15% dead rows straight back into F1's
eviction picture.

**How it was applied, recorded because it is not the usual pattern.** *Not* concurrently.
Both Supabase MCP paths wrap their payload in a transaction and `CREATE INDEX
CONCURRENTLY` cannot run inside one (25001); a first attempt was killed mid-build by the
120 s `statement_timeout` and left an INVALID index. It was applied instead as a **bounded
blocking build in a verified-quiet window** — no `rebuild_%` active in `pg_stat_activity`,
05:25 UTC between the `*/15` bursts, `lock_timeout='6s'`, `statement_timeout='300s'`. A
plain `CREATE INDEX` takes SHARE: it blocks writes on this table, not reads, and this
table's writers are batch jobs that retry.

**Rails.** `tests/test_location_drain_index_plan.py`, migrations lane, **errors rather than
skips** without `TEST_DATABASE_URL`. It asserts the index exists with **both** key columns
and the partial predicate, that the drain's real statement can be served by it, and that
**no Sort node** appears. It runs with `enable_seqscan=off` on purpose: CI's replayed table
is empty, where a seq scan genuinely is cheapest, so "the planner prefers it" is not a
property CI can assert — "the index can serve this exact shape, ORDER BY included" is.

**Rollback.** `DROP INDEX CONCURRENTLY llc_property_listing;` and
`ALTER TABLE … RESET (autovacuum_vacuum_scale_factor, autovacuum_analyze_scale_factor);`
on both tables.

**This is the input to STOP 1.** The ledger's open thread (a) — "why does the same rebuild
cost 10 s on a quiet day and 419 s on a bad one, same query, same rows" — has a candidate
answer that is not a planner mystery: whether the location lane is evicting `listings` and
`properties` from a 1 GB pool. The ≥24 h re-measurement decides whether an incremental
`browse_list` rebuild needs specifying at all.

### W2 — the daily-cost guard reads its own index

`_check_daily_cost` runs on **every recorded LLM call** and spelled its predicate
`called_at::date = CURRENT_DATE`. That cast is STABLE — it depends on the reading session's
`TimeZone` — so it cannot match `llm_calls_utc_day_rollup_idx` (migration 421).

| | blocks | time |
| --- | --- | --- |
| before — Parallel Seq Scan, 293,552 rows discarded for 10 | 9,665 | 3,310 ms |
| after — Bitmap Index Scan on `llm_calls_utc_day_rollup_idx`, `Index Cond` | **8** | **13.6 ms** |

**1,208×**, recorded against the proposal's projected "1,610×" — which was derived, not
measured. **It hid because the read sits inside a `try/except` that logs at DEBUG and
returns**: when it regressed the *number stayed correct* and only the cost changed. That is
why the rail asserts the plan, not the value.

Lifting the statement to a module constant `DAILY_COST_TODAY_SQL` is not cosmetic — the
SQL-correctness CI gate works by PREPARE-ing *discovered SQL constants*, and as an inline
string in a method body this statement was invisible to it (verified: it now appears in
`sql_corpus.discover()`'s 748 items).

UTC here is deliberate and scoped to matching the index. The page's *displayed* day is a
separate question with one declared zone, decided in W3.

### W1a — the admin gate, hoisted (migration 431)

**The commissioned item was a measurement artifact; the real defect was next door.** The
ledger recorded the gate at "~332 blocks". That was a once-per-backend catcache warm-up:
every Supabase-MCP call opens a fresh backend, PostgREST pools. Warm the gate costs ~2
blocks. **Item 4 as commissioned is not a north-star violation.**

The defect that *is* one, unnamed in the ledger: in all **10 tenancy policies** the gate
sits inside an `OR` with column references, which destroys the pseudoconstancy Postgres
would otherwise exploit — so the executor calls a **SECURITY DEFINER function once per
candidate row**, including on `llm_calls` at 293,551 rows.

Measured live, through the real RLS path (`set local role authenticated`):

```
before   Filter: (... OR ((account_id = '000…0'::uuid) AND is_platform_admin()))
after    InitPlan 2 -> Result
         Filter: (... OR ((account_id = '000…0'::uuid) AND (InitPlan 2).col1))
```

Semantics are bit-identical: the function is already STABLE and argument-less, so one
evaluation per statement is exactly what STABLE promises. It still reads live — **no cache,
no TTL, no memo, revocation stays instantaneous.** Row counts verified identical after.

**Three proposal premises corrected against the live catalog:**

1. **The `session_user` premise is HALF WRONG, and the memory note it told us to retire is
   right.** The proposal says the gate "keys on the JWT sub, not `session_user`", and that
   `[[admin-gate-session-user-pattern]]` predates migs 329/330 and should be corrected.
   The live function has **two arms**: claims present → `admins` keyed on JWT `sub`;
   claims **absent** → `current_setting('role') = 'none'` **and**
   `pg_roles.rolbypassrls` for `session_user`. That fallback arm is live, and the memory
   is the record *of* it. **It was not corrected.** Operational consequence worth keeping:
   psycopg, psql, pg_cron **and the Supabase MCP itself** all hit the `pg_roles` arm — so
   *every MCP measurement of this gate exercises a different branch than browser traffic
   does*.
2. **"~35 views/functions" → 36** (27 views + 9 functions), and **10 policies → 11 edit
   sites**, not 10: `manual_rental_estimates_admin_update` carries the gate in **both**
   `USING` *and* `WITH CHECK`.
3. **`ALTER POLICY`, not DROP + CREATE.** The proposal's design replayed policies and
   argued the transaction was needed because "replaying policies transiently drops them".
   `ALTER POLICY` swaps the expression in place, so there is no drop window at all — and,
   decisively, **it cannot lose `TO authenticated`**. A `DROP` + `CREATE` that omits the
   role clause silently defaults the policy to `PUBLIC`. That is privilege escalation
   inside a security replay, and it is now structurally impossible here.

**Split from the 36 cosmetic wraps, deliberately.** The views/functions call the gate
*standing alone* — already one evaluation, zero measured gain, wrapped only for uniform
spelling. Bundling them would have put **46 `ACCESS EXCLUSIVE` locks in one transaction**
across 9 continuously-written tables, and `CREATE OR REPLACE` carries hazards `ALTER
POLICY` does not (it resets view `reloptions`, and drops any function attribute not
restated — `SECURITY DEFINER`, `SET search_path`). W1a's lock set is 9 tables, held for a
catalog update, under `lock_timeout='3s'`.

**The in-migration rail earned its place on its first run.** It asserts completeness (no
bare call survives), coverage (exactly 10 policies / 11 sites), attribute stability
(permissive + role list), and that the four `*_tenant_rw` policies did **not** gain an
admin arm in `with_check`. On the first apply it **failed and rolled the whole thing
back** — the coverage count read 22 for 11 sites, because the deparser renders the wrapper
as `( SELECT is_platform_admin() AS is_platform_admin)` and the name appears twice, once as
the call and once as the alias. Production was verified untouched. Because the rail runs
*inside* the transaction, a wrong rail costs nothing and the error text is the diagnosis.

**`_admin_gate_shape.py` is deliberately NOT extended to `pg_policy`.** Its
`_GATE_OR_EVASION` rule rejects any `or … is_platform_admin` — which is the exact shape all
10 legitimate tenancy policies have, because a tenancy policy *is* an OR of "my rows" and
"platform rows". Pointing that guard at policies would force it to be weakened to pass, and
its own docstring records two earlier regex generations that were weakened and then
accepted gate-defeating forms.

**Bounded win, stated:** on `llm_calls` the gate is inside a *correlated* `EXISTS`. The
wrap hoists the gate out of the inner loop, but the `EXISTS` itself still runs per
`llm_calls` row.

Filed, not done: `procost 100` and — larger — **`proparallel = 'u'` (PARALLEL UNSAFE) on
both `is_platform_admin()` and `current_account_ids()`**, which forces every RLS-bound read
on these 9 tables to run serially. Marking them PARALLEL SAFE would likely dwarf the hoist.
Both are planner inputs and do not belong in a security replay whose whole discipline is
verbatim-with-wrapper.

### W4 — the leaderboard ranks before it hydrates (migration 435)

Today the function joins `brokers_public` to the **whole** aggregated candidate set, sorts,
and only then takes the top 100. **Between 87% and 99.2% of every joined-and-decorated row
is discarded by the LIMIT.**

| shape | before | after | factor |
| --- | --- | --- | --- |
| default byt/prodej | 3,140 | 3,378 | **0.93× — neutral, honestly worse** |
| single region chip | 22,952 | **2,479** | **9.3×** |
| multi-chip geo | 38,405 | **2,774** | **13.8×** |
| firm chip | 4,227 | **2,375** | **1.8×** |

Equivalence verified live on the default shape: **100/100 rows, full overlap, zero value
mismatches, zero inactive brokers leaked.**

**The default does not improve, and that is the honest result.** Its cost was never the
nested loops — it was two sequential scans, and resolving the active set still needs one of
them. The wins are on the chip shapes, which are the page's headline affordance.

**The production baseline is `pg_stat_statements`, not those figures:** 42 calls, **5,390
blocks/call**, mean 1,081 ms — with **65% `shared_read`** and 20,777 buffers written back.
This RPC evicts; the warm EXPLAINs are a lower bound.

**Three of the proposal's four shape figures were wrong**, from two systematic errors. Its
937-block planning number was a **cold-backend artifact** (warm: 42); its `dirtied=212 /
written=942` are hint-bit writeback noise and are **zero** warm. The region chip is
**22,952**, not 14,285 — understated by 8,667 blocks. The firm chip is 4,227, not 5,859.
Multi-chip, the page's worst shape, was never measured at all.

**The diagnosis was right but mis-weighted.** The 71,364-discarded-index-entries finding is
exact (`Rows Removed by Filter: 71423` today) — but that waste is only ~1,032 of the
default's blocks. The dominant cost was the display join.

#### The design's index was measured and REMOVED — a real reversal

The proposal specified `create index brokers_active_id_idx on brokers (id) where
status='active'`, arguing the semi-join's build side would otherwise fall back to a
1,776-block seq scan. It was built, measured, and **dropped**:

| | default | region chip |
| --- | --- | --- |
| with the index | **5,725** | 11,899 |
| without it | 3,378 | 12,529 |
| without it, `AS MATERIALIZED` | **3,378** | **2,479** |

The index's only use is an **index-only scan**, and `brokers` is **696 of 1,776 pages
all-visible — 39% — fifteen minutes after an autovacuum**, because `_BROKER_ROLLUP` updates
every active broker every 10 minutes and 60% of those updates are non-HOT. So the scan paid
**18,905 heap fetches / 4,123 blocks** where a sequential scan costs 1,776. **The visibility
map is a property of the table, not the index**, so no index can escape it. Hazard H-E was
right in kind and understated by 5×.

#### What actually fixed it: `AS MATERIALIZED`, and it is the doctrine

Written as an inlinable `IN (select ... from brokers where status='active')`, the planner
probes `brokers` **once per candidate row** on every geo shape — 3,942 loops, 11,826 blocks
on a single region chip. That is hazard H-F materialising: *the wave reproducing the very
nested loop it exists to delete.*

`AS MATERIALIZED` forces the active-broker set to resolve **once** and be hash-joined. Which
is precisely the Cardinality Doctrine's own sentence: **an expression that does not vary
across the rows being scanned is evaluated once, not once per row.** The fix was not a new
index; it was making the planner honour the invariant.

#### The part that is not an optimisation

`status='active'` moves **into** the ranking CTE. Leaving it above the LIMIT lets a
`merged_away` broker holding surviving stats rows consume a top-N slot and be discarded —
an under-filled page, and a shrunken `outreach` pool. **LIVE:** 5 such brokers exist now,
and of 19,200 merged-away brokers **717 carry a metric at or above the default cut of 26**
(44 above the all-categories cut, max 1,277). The matview refreshes only on the daily sweep
(mean gap 25.2 h, max 65.1 h) while inactivation is continuous — the two failure modes are
**correlated**.

The tiebreaker ships here too: **seven brokers tie at the default limit-100 boundary**, and
once the LIMIT is under the join an unstable sort decides *membership*, not position.

### W5 — city-quality on the obec key (migration 436)

**The commissioned framing was wrong about the defect.** The city side is 206 rows joined to
6,798. Evaluated **once** it costs 971 blocks. The entire **1,778,259**-block failure is that
it was evaluated **282,214 times** — a correlated SubPlan at ~4.4 blocks a row. Nothing was
missing except a shape the planner can execute once. **1,831×.**

**And a second, independent hard failure a SQL fix alone would not have touched:**
`resolveCityQualityPrefilter` called `fetchAllRows(..., expectMax: 100_000)` against an
allowlist of **84k–262k listing ids** for every practical threshold, so it threw
`FetchAllOverflowError` *regardless of SQL speed*. Fixing only the plan would have traded a
timeout toast for an overflow toast. **The feature was broken, not merely slow.**

**This is a KEY CHANGE, not a re-expression** — recorded plainly because the design's own
language obscured it. The live predicate never touched `admin_boundary_id`; it keyed
`curated_cities.id` against `browse_list.home_city_id`. "One SQL function owning the same
rule evaluation" is true of the *rules* and false of the *membership test*.

#### Decision #8 was taken on numbers that do not reproduce

| | proposal | re-measured |
| --- | --- | --- |
| gain | +1,979 | **+1,960**, of which **1,916 (97.8%) have NULL lat/lng** |
| genuine key disagreements | — | **+44** |
| loss | −49 "stale home_city_id corrections" | **−30 dropped** + **19 re-attributed** — two different things collapsed into one number |

The worked examples were wrong (Praha→Černošice is **1** row, not 6; Hradec Králové→Beroun is
**3**, not 4), and **the "stale Praha" characterisation is unsupported**. The real loss pattern
is *curated town vs adjacent small obec* — Znojmo→Nový Šaldorf-Sedlešovice ×5, České
Budějovice→Planá ×3, Ústí→Trmice ×3 — all 43 distinct obec ids at `level='obec'`. Those are
cases where the obec key **drops** a property from the town cohort it plausibly belongs to:
the opposite of the justification given.

**Decided by the north star, after the operator handed it back.** Option "don't swap the key"
fails outright — it leaves the overflow, so the surface still cannot render its cohort, at
34,050 blocks instead of 971. Between keeping and dropping the `lat/lng` guard the **cost is
identical**, so the cost clause does not discriminate — **Corollary F does.** That guard
exists only because the same function also served `near_city_proximity`, which this migration
retires. A listing in Brno with no coordinates is still in Brno. Keeping it would deliberately
preserve a silent under-render of the cohort for the sake of a feature being deleted in the
same statement. **The 1,916 are admitted; the −30 are filed as a data defect, not used as
justification.**

#### Rule 17 inverts, and the repair is in Python

Until now the **schema** enforced rule 17 for free: `listings` has no `home_city_id`, so a
city-quality clause on a listings-grain query died at parse with `42703` before a row was
read. Re-keying onto `l.obec_id` — a column `listings` **has** — means the identical bypass
would now plan, execute, and **silently** return an estimate narrowed by operator-curated,
revision-versioned, subjective city scores, with a `status='success'` row and a full trace.
The proposal claimed the rewrite removes this failure "structurally". **It is the reverse.**

Two facts made it urgent rather than theoretical: **nothing tested the agenda gate for these
filters** (deleting it turned nothing red), and **the old safety argument was a docstring** —
*"the listings-grain callers never set these filters, so the whole helper is inert for them"* —
an assumption about caller behaviour with no mechanism, while `_shared_filter_where` called
the helper unconditionally with no grain argument and no guard.

`_assert_no_city_quality` is that former schema rail, and it is a **raise, not an inert
branch**: rendering nothing would be exactly the silent failure W5 exists to prevent.

#### Two latent correctness bugs ride along

- **`city_index_values_public` filtered on a GLOBAL max revision.** One partial upload (a
  single corrected city at revision 3) would make the view return **33 rows instead of
  6,798** — a 99.5% collapse, every other city silently failing every rule, no error
  anywhere. Both historical uploads were full re-uploads, so it has never fired; one partial
  upload arms it. Correct spelling is latest-per-`(city_id, index_name)`, via `NOT EXISTS`
  rather than `DISTINCT ON` (which would block qual pushdown and, if ever pushed, let an
  older revision win).
- **The `city_index_rules` FilterDef description** still described `ST_DWithin` to a centroid
  with `default_radius_m`. Stale since migration 375, and now wrong twice over.

#### The most dangerous detail in the migration

`curated_cities` and `city_index_values` are **RLS-on with zero policies**. A `SECURITY
INVOKER` function reading the **base** tables returns **zero rows** for `authenticated` —
silently, not as an error — collapsing every city-quality cohort to empty. `curated_cities_matching()`
reads the `_public` views deliberately, and a rail asserts it.

Wire shape **verified against live PostgREST** rather than assumed: `RETURNS SETOF bigint`
comes back as a bare JSON array of numbers (`[584495, 554782, …]`), not an array of objects.

### W3 — the cost page stops re-aggregating its source (migration 437)

Store **one** grain (the UTC hour), derive the day at read time, serve reads as
`[closed hours from the rollup] UNION ALL [the open edge, live from llm_calls]`.

| Read | before | after | |
| --- | --- | --- | --- |
| `llm_cost_daily_public`, 35 days | 10,439 blocks / 90 rows | **47** | **222×** |
| `llm_cost_hourly_public`, 49 hours | ~1,250 (July-shape) | **27** | see caveat |

Plan confirmed by node name before the block claim was quoted (amended Corollary D):
`Index Cond` on `llm_cost_hour_rollup_prague_day_idx` — so the day predicate really does
push through the outer GROUP BY and distribute into the UNION ALL branches — a `One-Time
Filter: is_platform_admin()` above the `Append`, and the watermark resolved by **InitPlan**
in both branches rather than per row.

**The earn-test was run first and precompute won on the numbers.** A pure rewrite exists:
bounding the SPA's one-sided range flips the plan from an ordered Index Scan feeding
GroupAggregate to a Bitmap Heap Scan, 11,885 → 4,033 blocks, free. That is 2.95× and still
~200× above the rows-on-screen floor, so it does not earn its way out of state. It is also
**superseded, not additive** — after this migration no `/costs` query scans `llm_calls`
unbounded — so it was deliberately **not** also shipped, and 2.95× is not counted here.

#### Equivalence, proven rather than argued

Prague-day re-aggregation from UTC hour buckets is **exact**. Over all 293,561 rows the
rewritten daily view and a direct Prague-day aggregate of `llm_calls` produce **332 groups
each with zero rows in either symmetric difference**, cost column included; the hourly view
is likewise byte-identical to migration 421's body. `llm_cost_today_usd()` returns the same
value the page shows. Prague's offsets over 2024→2027 are exactly {+1h, +2h} with no
fractional-hour sample, so a Prague day boundary always lands on a UTC hour boundary.

#### Three defects in the commissioned design, each caught by measurement

- **`GREATEST` vs `'-infinity'` — self-contradictory.** The design said the backfill *is*
  `refresh_llm_cost_rollups('-infinity')` **and** that the window is
  `max(p_from, complete_through - 3h)`. `greatest('-infinity', …)` discards `'-infinity'`,
  so the backfill and every "full repair" would have been silent no-ops while the
  idempotency rail passed asserting nothing. Shipped as `LEAST` with an `'infinity'`
  coalesce, which additionally makes it structurally impossible to *shrink* the re-scan.
- **Double-rounding.** The design left `cost_usd numeric` unspecified as rounded or not.
  Storing the rounded hourly value and summing 24 of them is sum-of-rounds, not
  round-of-sum: measured live, that corrupts **74 of 332 daily groups**. The rollup stores
  the exact sum and each view rounds once, at the outer projection. The rail asserts the
  74 — a negative control, so it proves it is testing the thing that would break.
- **The state table as a JOIN is a single point of total failure.** An inner join to the
  singleton watermark row returns **zero rows from both views** if that row is ever missing
  — `/costs` goes blank with nothing failing. Shipped as an uncorrelated scalar subquery
  with a `'-infinity'` fallback: a missing row degrades to *today's exact numbers at
  today's cost*, and being uncorrelated it renders as the InitPlan above.

#### A registry column that could not ever be written

The design gave `derived_artifacts` a `last_error` column stamped by an
`exception when others then update …; raise;` handler, published as `has_error`.
**Verified live on this instance: a handler's write cannot survive its own re-raise** — the
re-raise unwinds the subtransaction the handler ran in, and the probe table came back empty.
So `last_error` could only ever be NULL and `has_error` could only ever read **false —
including during a total outage**. That is the same defect class as migration 432's guard
that could not fire, and a health signal that cannot fire is worse than none because the
panel renders it green. **Both columns were cut**, along with `last_started_at` (same
transaction, same fate). The durable failure record is `cron.job_run_details`; the published
signal is `last_succeeded_at` against `staleness_budget`, which stays correct *because* it
rolls back with a failed run.

#### The union's correctness is a CHECK constraint, not a habit

The split is exact only if the watermark lands on a UTC hour boundary, because
`bucket_hour = floor_hour(called_at)`. Off-boundary it silently double-counts or drops the
straddling hour, forever, with nothing failing. `date_trunc(text, timestamp)` is IMMUTABLE,
so this is enforceable — one careless `update … set complete_through = now()` is now rejected
by the database rather than by a code review.

#### Corrections to the recon that sized this wave

- **"blocks ≈ 0.13–0.17 × calls" is false.** Observed 0.042–0.203 across seven warm
  measurements, not one inside the stated band. The ratio is a property of the **plan**, not
  the data: an ordered Index Scan feeding GroupAggregate revisits heap pages at ~0.20
  blk/call; a Bitmap Heap Scan de-duplicates the page list at ~0.042, which is just the
  heap-page floor.
- **The rollup is sized against the surviving workload, not the July peak.** 71.4% of
  `llm_calls` history came from two permanently retired workloads — the three `compare_*`
  dedup feeders stopped dead 2026-08-06 at the teardown (151,249 calls), and
  `score_listing_condition` stopped 06-18 (58,436). The surviving workload's worst-ever
  35-day window is **307 hour-groups, not 2,565** — 8.4× smaller than the figure the design
  sized against. Any later retention or partitioning decision must not use that peak.
- **Migration 421's two indexes do NOT both go dead.** The design predicted zero scans for
  both. Measured: `llm_calls_utc_hour_rollup_idx` is still the access path for the hourly
  view's live branch (the SPA's `bucket` predicate pushes into it). Only
  `llm_calls_utc_day_rollup_idx` goes idle. Both were kept regardless — 4.5 MB total, and the
  revert depends on them.

Cost of the new `pg_cron` job: 96 ticks/day at ~179 blocks each (July-shape) ≈ **5
job-seconds/day**, or **0.014%** of the 34,399 s/day W0b returned. Scheduled `4,19,34,49`,
which collides with nothing on the live board — a plain `*/15` would have landed on jobid 6,
the instance's heaviest and most fragile job.

**Measurement caveat, stated because every "current" reading understates the steady state:**
the OpenAI credit outage since 2026-08-15 has driven `llm_calls` to ~59 rows/day against
~1,000/day before it. The 27-block hourly figure is measured in that state; the July-shape
comparison (~1,250 → ~70–100) is the honest one for the hourly grain. The daily 10,439 → 47
is not affected, because its cost was always in the rollup scan, not the live edge.

### W6a — the map payload, before the map read is rewritten (no migration)

Split out of W6 so the RPC's diff is only about the read shape. Three defects, all in
the client, none needing a schema change.

**Two columns nobody reads.** `subtype` is **0 of 50,000 non-NULL** in the default cohort
and appears nowhere in `ListingMap.tsx`; `tom_days` is fully populated and equally unread.
Every column in `MAP_COLS` is serialised into a GeoJSON feature property for **every pin**,
so an unread column is pure wire cost. Measured on the live cohort: **25,357,862 →
23,621,798 bytes, −1.66 MB (6.8 percent)**. (Measured with `json_build_object`, whose output
is close to but not byte-identical with PostgREST's serialiser — the delta is the claim, not
the absolute.)

**The comment that justified the cap was off by ~an order of magnitude.** `queries.ts` said
*"50k features ≈ 0.3 MB gzipped"*. The real payload is **22.66 MB raw / 453 B per row**. That
sentence was the whole stated rationale for `MAP_CAP`.

**And the cap is not the only cap.** `authenticator` carries `pgrst.db_max_rows=50000`
(migration 394, verified live), so PostgREST truncates server-side at the same number.
Deleting the client `.limit()` would not lift the truncation — it would only stop the client
knowing about it. Both have to move together, and W6's designed rail ("no cohort read applies
`.limit()` without `.order()`") would have gone **green on a read that still truncates**.

**Corollary F, applied to the copy.** The pill read `50 000 of 106 173 mapped · capped at
50 000 — refine filters`. That is arithmetically correct, which is exactly why the defect
survived: it is honest about the SIZE of what is missing and silent about its KIND. The cap
is applied with **no ORDER BY**, so the plotted pins are whatever the index scan reached first
— in practice the southernmost matches — and "capped" alone invites reading them as a
representative sample. Now: `· capped at 50 000 — an arbitrary slice, not a sample`, with the
mechanism in the title attribute, and the number derived from `MAP_CAP` so the copy cannot
drift from the constant it describes.

`capped` itself is left as `rows.length >= MAP_CAP` and **documented as undecidable at the
boundary**: PostgREST clamps at the same 50,000, so a 50,001st row is unobservable from the
client. The pill states truncation from `cohortTotal > total`, which *is* decidable, and uses
`capped` only to explain why.

### STOP 1 — the T0 baseline, captured 2026-08-25 05:37 UTC

Captured **at the moment F1 landed**, because the "before" window is the *past* 24 h and
ages out of `cron.job_run_details`. This is the row the ≥24 h re-measurement compares against.

| jobid | job | schedule | runs | failed | avg | max | 24h total |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 6 | browse-list-rebuild | `*/15` | 110 | **31 (28.2%)** | 388.3 s | 600.3 s | 42,713 s |
| 1 | refresh-health-dashboard | `*/10` | 109 | **38 (34.9%)** | 348.1 s | 900.8 s | 37,594 s |
| 8 | dedup-funnel-mv-refresh | `*/15` | 89 | 3 | 390.9 s | 600.9 s | **34,399 s** |
| 7 | browse-map-rebuild | `7,37` | 25 of 48 | 0 | 262.3 s | 582.1 s | 6,557 s |
| 3 | capture-data-quality | `30 */6` | 3 | 1 | 294.8 s | 432.7 s | 884 s |
| 5 | emit-verification-stale-alert | `10 * * * *` | 21 | 0 | 38.1 s | 600.0 s | 799 s |

**The finding the proposal did not have a number for: the scheduler is *oversubscribed*, not
at 74% of budget.** Total job wall-clock is **122,946 s inside an 86,400 s day = a 142% duty
cycle** — these jobs are not queuing politely, they are running *concurrently and contending*,
which is the mechanism behind both the 28.2% / 34.9% failure rates (jobs hitting their
`statement_timeout`, `max_s` pinned at 600.3 / 900.8) and jobid 7's **47.9% launch loss**
(25 runs against 48 scheduled).

jobid 8's share is **28.0%** of scheduler wall-clock here, against the proposal's 34.7% —
window-dependent in magnitude, stable in direction, exactly as correction #8 warns. **Always
quote the window.** W0b returns those 34,399 s/day.

Instance-wide at T0: cache hit **88.98%**, 4,610,969,944 blocks read cumulative,
**1,556.6 GB** of temp files, database **138 GB**, `stats_reset` null (counters are all-time).

Cumulative `pg_stat_statements` at T0, for the deltas the re-measurement will compute:

| statement | calls | blocks | mean |
| --- | --- | --- | --- |
| `rebuild_browse_list()` | 2,884 | 8,071,893,409 | 122,154 ms |
| `refresh_health_matviews()` | 1,333 | 2,194,833,959 | 63,079 ms |
| `rebuild_properties_map_mv()` | 467 | 1,225,479,173 | 95,948 ms |
| location drain bulk read *(F1's target)* | 39,488 | 2,737,573,584 | 982 ms |

**Scheduled, not skipped.** Per the operator's amendment, STOP 1 is report-don't-wait: the
full ≥24 h re-measurement still runs (it decides W5/W6's shape), and W1–W4 proceed meanwhile
under amended Corollary D, whose whole point is that block counts do not need a quiet
instance to be honest.

### Filed with a trigger

One line each: flag · trigger · evidence needed to close · filing wave.

- `llm_calls` carries 64 MB of index on a 76 MB heap; `called_for`/`provider` leading keys
  look unused · **trigger:** credits restored and a full traffic period captured ·
  **evidence:** `pg_stat_user_indexes` over that period · filed W2.
- `procost 100` on `is_platform_admin()` is decorative · **trigger:** the next migration
  that touches the function body · **evidence:** a before/after plan check · filed W1a.
- **`proparallel = 'u'` (PARALLEL UNSAFE) on `is_platform_admin()` AND
  `current_account_ids()`** — every RLS-bound read on the 9 tenancy tables is forced
  serial; this likely dwarfs the InitPlan hoist · **trigger:** a wave that can own a
  planner change with its own plan gate · **evidence:** parallel-plan EXPLAIN on
  `llm_calls` / `estimation_runs` before and after · filed W1a.
- The claims-absent fallback arm (`pg_roles` / `rolbypassrls`) inside a security primitive
  · **trigger:** an inventory of every raw-connection reader of a gated relation ·
  **evidence:** that inventory · filed W1a.
- **`properties.home_city_id` and `listings.obec_id` disagree on 49 rows** (30 dropped, 19
  re-attributed) — two geometry-derived columns answering the same containment question
  differently, e.g. Znojmo vs Nový Šaldorf-Sedlešovice ×5, Benešov vs Sušice ×5, Třebíč vs
  Praha ×3. **NOT "stale corrections"** — a genuine data defect · **trigger:** any work on
  `recompute_home_city` or the RÚIAN boundary set · **evidence:** which of the two columns is
  wrong per case · filed W5.
- **`NOT NULL` on `curated_cities.admin_boundary_id`** is not shippable as designed: it breaks
  `scripts/seed_curated_cities.py` (INSERTs without the column, fills it in a later pass) and
  `scripts/ingest_boundaries.py` (whose `wipe_table` depends on the FK's `ON DELETE SET NULL`).
  Enforcement ships instead as the migration's DO block + the live rail · **trigger:** an
  `ingest_boundaries` restructure to load-into-staging + swap · filed W5.
- **`llm_calls_utc_day_rollup_idx` (2,224 kB) goes to zero scans under migration 437** — the
  daily view no longer predicates a UTC day on `llm_calls`. Its sibling
  `llm_calls_utc_hour_rollup_idx` is **still live** (the hourly view's open edge uses it), so
  this is a single-index retirement, not the pair the design predicted. Deliberately NOT
  dropped in W3: the revert restores migration 421's bodies and needs it · **trigger:** W7,
  once the rollup has run unreverted for a full traffic period · **evidence:**
  `pg_stat_user_indexes.idx_scan` over that period · filed W3.
- **`derived_artifacts` covers 1 real row of 14 derived artifacts** (the public view
  publishes 3, the other two adapted from `browse_read_model_state`). The catalog-diff rail
  goes RED only for *newly created* artifacts; the 12 pre-existing matviews plus `browse_list`
  — 13, counted against the live catalog after W0b's teardown removed three — sit in
  an explicit `_W7_BACKLOG` set whose docstring says so · **trigger:** W7 · **evidence:**
  emptying that set · filed W3.
