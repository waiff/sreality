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
  New rails: the first Browse-card tests in the repo (there were none) — the grid painting with
  the photo read hanging forever, **the carousel keeping all 7 of a listing's photos rather than
  collapsing to a cover** (the ledger's explicit warning for this wave), the genuinely-no-photos
  case kept distinct from the pending one, and one cohort read keyed on the surrogate id; five
  `photoBuckets` cases pinning append-stability and arrival order; three more key-disjointness
  cases. The production smoke check gained a **Browse multi-photo-carousel assertion** — this is
  the wave that could leave the grid rendering perfectly with every photo silently gone, and every
  existing Browse assertion would still have passed.
  **← stop point 2 — pausing here for review before W7b onward.**
- ⬜ **W7b** — media delivery (the hourly presign re-mint kills the browser cache key).
- ⬜ **W10c** — /costs date-expression index. ⬜ **W10d** — /health bounds. ⬜ **W10e** —
  /estimations OR-join (EXPLAIN first — that one is code-derived, not measured).
- ⬜ **W8** — bundle: maplibre out of the filter-controls barrel, recharts unpinned from
  React. Last: first-visit and post-deploy only.

## Baselines — 2026-08-24, post-W-1a/W-1b, production

Per-route app data requests (PostgREST + Railway only; basemap tiles, assets and image
bytes excluded) and settle time, measured by `npm run smoke-check:prod`:

| Route | W0 | post-W2a | post-W2b | post-W5 | post-W6 | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| /browse | 31 | 27 | 22 | 22 | 22 | was 9.1 s *and an HTTP 500* pre-W-1a; now 2.9 s |
| /pipeline | 27 | 23 | 19 | 18 | **17** | time-to-first-card 1,427 → 951 ms (W1) → 412 ms (W5, killed the board's 2nd request's serialization) → **368 ms** (W6) |
| /collections | 9 | 5 | 5 | 5 | 5 | was 6-of-9 duplicated bootstrap |
| /watchdog | 10 | 6 | 6 | 6 | 6 | reference feed — shape was already correct |
| /notifications | 9 | 5 | 5 | 5 | 5 | server work is ~15 ms; the rest is transport |
| /brokers | 11 | 7 | 7 | 7 | 7 | leaderboard is server-bound, see W10a |

Server-side block counts to beat (constraint 6):

| Read | Blocks | Target | Wave |
| --- | --- | --- | --- |
| `browse_list` default cohort | 15,877 → **6** | done | W-1a |
| broker leaderboard | 6,776 → **3,108** (warm) | ~500 (close, not hit — floor is a `brokers` seq scan at 55% selectivity) | done — W10a |
| board images (44 cards) | 3,995 → **788**; 901 CLIP laterals → **44** | 44 rows / 44 probes | done — W4 |
| `llm_cost_daily_public` | seq scan, 231,189 rows discarded for 93 out | index | W10c |
| `pipeline_checks_public` | 6,120 rows scanned for 15 | bounded | W10d |

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

## Parked, with re-entry triggers

Multi-category cohort ordering (revisit if a preset drops its district filter) · Browse map
payload 16.8 MB · instance sizing (after W-1b root-cause and W10a) · React Query persistence
· /notifications 100-row cap · repr-flip semantics (own PR, see W4) · connection pool +
gzip middleware · the unexplained client-side count abort on Browse.
