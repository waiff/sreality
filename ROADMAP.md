# Roadmap

The long-term plan for this project. Each phase builds on the previous;
tools within a phase are independent. CLAUDE.md is the authoritative
source for active rules; ROADMAP is for sequencing.

## Done

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

- Sprint C (data value, remainder): bazos dedup match-rate follow-up (street_key
  normalization vs the "ul. …"/house-number forms), remax drain follow-up (timeout fix).
- Sprint D (architecture): sreality → Portal framework, shared CLI, fallback-workflow
  deletion, CLAUDE.md scraper-section rewrite, sreality pozemek/ostatní category parity.


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

## Next

### Phase B1: Building decomposition — URL ingest + unit extractor + confirmation UI (active)

Second slice of the building-paste flow. Builds on B0's persistence:
operator pastes a `dum` (house) or `komercni` URL → backend parses
via the existing dispatcher → an LLM-vision skill reads the
description + floor plans + photos → proposes a unit list → the UI
renders an editable confirmation step → operator confirms. End of B1
the building is in `status='awaiting_input'` until the operator
submits, then advances to `estimating`. B2 picks up the per-unit
estimation fan-out from there.

Full description (including the apartment-skill-reuse note on the
B2 orchestrator step) under "Building decomposition track" below.

Headline scope:
- Migration 036: `building_unit_extractions` cache table
  + `'extract_building_units'` value on `llm_calls.called_for`
  + four `app_settings` rows for the new skill / prompt / model
  (`llm_building_extractor_system_prompt`, `llm_building_extractor_model`,
  `llm_building_extractor_max_images`, `building_default_estimator_skill`).
- New toolkit function `toolkit.building_extraction.extract_building_units`
  — write-allowed exception per toolkit rule #5; same cache pattern as
  `summarize_listing` (keyed on `(sreality_id, snapshot_id)`).
- New skill `building_unit_extractor_v1` (vision extractor, not an
  estimator) — on-disk `skills/building_unit_extractor_v1/SKILL.md`
  + migration seed `INSERT`. Allowed tools: `extract_building_units`
  + `record_building_units` terminator. Distinct from the apartment
  estimator skill — its job is structural extraction only.
- `POST /buildings/from_url` replaces B0's minimal `POST /buildings`
  as the operator-facing entry. Rejects `category_main='byt'` (those
  go through `/estimations`).
- `POST /buildings/{id}/confirm_units` accepts the operator-edited
  unit list, validates, writes to `units`, advances status to
  `estimating`. Idempotency via 409 on non-`awaiting_input` rows.
- Frontend: new `kind` toggle on `NewEstimationModal` ("apartment" /
  "building"), `BuildingUnitEditor` component for the review step,
  new `/building/:id` page (initially read-only — full rollup view
  ships with B2).

### Phase 7 slice 2: Async + full toolkit + UI mode toggle
Builds on slice 1.
- Async execution: real `status='pending'/'running'` lifecycle with
  a background worker and a polling endpoint. Removes the
  synchronous HTTP wall-clock cap.
- Expose the rest of the toolkit (`cluster_comparables`, the two
  velocity tools, the visual layer) by adding skills that whitelist
  them.
- Frontend `/estimate` gets a mode toggle (`deterministic` /
  `agent`), a provider picker (anthropic / gemini), and a skill
  picker.
- Third provider (OpenAI or Vertex AI service-account auth).
- Per-skill A/B comparison view on `/estimations`.

### Phase 7d: Agent code execution (deferred)

Let the agent build and run small ad-hoc Python when the fixed
toolkit can't express a needed calculation (e.g. a one-off
distribution fit, a custom aggregate over the comparables already
in hand, a sensitivity check the existing tools don't cover).
Scoped now, implemented later — sequenced after the manual
rental estimates work (Phase U-ME) so the simpler, contained
schema feature lands first.

Operator-confirmed approach:
- Self-hosted sandboxed subprocess on the Railway container.
  No third-party sandbox (rules out e2b), no provider-hosted
  code-exec beta (rules out Anthropic's `code_execution_20250522`
  and Gemini's native `code_execution`). Cross-provider neutral
  per CLAUDE.md.
- Sandbox primitives still to design when the phase starts:
  `subprocess.run` with `preexec_fn` setting `rlimit_as` /
  `rlimit_cpu`, env scrub, no network egress, per-call tmpdir,
  wall-clock timeout. Or `RestrictedPython` for a pure-Python
  whitelist. Decision is part of the phase, not this stub.
- New `agent_code_executions` audit table keyed on
  `estimation_runs.id` so every code block, its stdout, stderr,
  duration, and result is auditable alongside the existing
  trace.
- Wired as a new `computation_v1` skill rather than a flag on
  `rental_estimator_v1` — keeps the safety boundary explicit
  and avoids broadening the existing skill's allowed_tools by
  a category-change rather than a per-tool addition.
- Trace integration: each execution emits a `step.kind =
  'code_execution'` entry alongside today's `tool_call`,
  `computation`, and `reasoning` kinds. `TRACE_SCHEMA_VERSION`
  in `api/estimation_runs.py` bumps when this lands.
- Open questions deferred to the phase: pre-populated namespace
  shape (pandas-ready vs pure dicts), whether the agent can
  reference earlier tool outputs by name, soft-cost cap per run.

### Phase QUAL: Qualitative city data + population overlay (in progress)

Operator-curated qualitative indexes (employment, safety, services,
amenities, etc. — 33 metrics from `data/obce_v_datech_2025.csv`) for
206 Czech cities, attached to the geo data already on each listing,
plus an authoritative population column sourced from ČSÚ. Both
surfaces feed the Browse filters and the U2.7 notification
subscriptions so an alert can fire when "listing in a city with
employment-index > 5 and population > 20 000" or on a compound
proximity rule ("within 5 km of a city with safety-index > 6,
services-index > X and population > 20 000") — combinable with the
standard listing facets (floor area, disposition, price, price per
m², etc.). Browse map also renders matching cities as a separate pin
overlay that can be heatmap-color-coded by any chosen index.

**What's shipped** (this commit):

- **Schema** (migrations 078 + 079): `curated_cities`,
  `city_index_revisions`, `city_index_values`, `city_index_definitions`,
  `city_population`, plus the three `*_public` views and the
  `listings_with_city_quality(p_index_rules, p_pop_min, p_pop_max,
  p_proximity)` RPC. Anon SELECT on the views and EXECUTE on the
  RPC; SECURITY INVOKER throughout.
- **Backend**: three new filter defs in `toolkit/filter_registry.py`
  (`city_index_rules`, `min/max_city_population`,
  `near_city_proximity`), gated to BROWSE + WATCHDOG agendas only so
  the estimation agent / comparables tool stay unaware.
  `toolkit/comparables._shared_filter_where` and
  `api/notifications._build_match_clauses` both render the new
  clauses by delegating to the shared `_city_quality_clauses` helper
  — Browse and Watchdog stay in lockstep.
- **Browse data path**: `frontend/src/lib/queries.ts` resolves the
  city-quality sreality_id allowlist via the new RPC and AND's it
  alongside the existing tag prefilter — same composition pattern as
  `listings_with_tags`. Map / Table / Cards all honour the new
  predicate without touching the existing PostgREST fast path.
- **Filter UI**: new "City quality" `<ControlGroup>` in
  `Filters.tsx` with the `CityIndexRulesPicker` custom widget
  (dropdown grouped by category × threshold input, repeatable) plus
  range inputs for min / max city population.
- **Map overlay**: `ListingMap.tsx` renders the curated city set as
  a separate GeoJSON layer above the listing dots, with bottom-left
  controls for "Show cities" toggle + "Color by:" dropdown + gradient
  legend. Heatmap paint expression `red(0)→amber(5)→green(10)` matches
  the data's 0–10 index range. Click → popup with city name, kraj,
  population, and every index value (highlighted index pinned to the
  top). **Cities draw as their real municipality boundary polygons**
  (migration 139's `curated_city_polygons_public` — RÚIAN obec geometry
  simplified to GeoJSON, anon-read), not fixed-radius circles: a
  translucent same-tone fill + a thicker conditional-coloured border,
  and **the selected index figure is labelled at each shape's
  centroid** (`city-fill` / `city-outline` / `city-label` layers). A
  city with no boundary falls back to a radius circle.
- **Values fetch un-truncated**: `fetchCityIndexValues` now pages
  through `city_index_values_public` in 1,000-row chunks. PostgREST
  hard-caps responses at the project's `db-max-rows` (1,000), which the
  old `.range(0, 49999)` could not lift — so only the first ~32 cities'
  values reached the browser and every other city (Dobříš included)
  showed em-dashes / a grey, value-less shape. Mirrors the same fix
  `fetchRentMapChoropleth` already used.
- **Obec re-link correction** (migration 140): the polygons exposed 6
  curated cities that migration 081's name-walk had linked to the WRONG
  obec — a larger, differently-named neighbour (Šlapanice→Brno,
  Odry→Ostrava, Hranice/Jeseník→Olomouc, Chrudim→České Lhotice,
  Mělník→Úžice), which drew a giant blob AND mis-scoped their
  `ST_Covers` city-quality filter. Re-linked by exact obec name match
  (tie-broken by nearest centroid); pure spatial containment was unsafe
  because several Mapy.cz centroids land just outside the town in a tiny
  neighbour. 202 correct links + the 20 cities with no same-name obec
  are untouched.
- **Tooling**: `scripts/seed_curated_cities.py` reads the operator
  CSV, geocodes each (Město, Kraj) pair via Mapy.cz, writes to the
  DB. Per-city radius is derived from the Mapy.cz bbox (clamped
  2–25 km). Operator triggers via
  `.github/workflows/seed_curated_cities.yml` (Mapy.cz + Supabase
  secrets, geocode cache committed back to the branch for offline
  reruns). Seed is idempotent: curated_cities upsert by `(name,
  kraj_name)`, definitions upsert by `index_name`, each run appends
  a new `city_index_revisions` row.

**Next-commit follow-ups also shipped**:

- **Watchdog editor surfaces the city-quality section.** The picker
  + 3 numeric fields now render in `WatchdogEdit.tsx` via the same
  custom-widget wire-up Browse uses. `WatchdogFilterSpec` gained
  the four matching fields (`city_index_rules`,
  `min_city_population`, `max_city_population`,
  `near_city_proximity`) and `DEFAULT_WATCHDOG_FILTER_SPEC` gets
  the matching nulls. Picker wire shape unified on snake_case
  `{index_name, op, value}` so the same operator output flows
  unchanged to the Browse RPC, Watchdog matcher, and the new Stats
  RPC.
- **Stats tab honours city-quality filters.** Migration 080 extends
  `browse_stats` to 44 params with the same four city-quality
  inputs the listings RPC accepts. Same EXISTS / NOT EXISTS
  semantics. Aliased the outer SELECT (`from listings_public l`)
  to avoid the bare-`lat`/`lng` ambiguity inside the EXISTS, which
  silently turned the geographic filter into a no-op on the first
  draft. `fetchBrowseStats` now passes the four params; Stats and
  Map/Table can never disagree on a city-quality cohort again.
- **Population fetcher**: `scripts/fetch_population_wikidata.py`
  queries Wikidata's public SPARQL endpoint for every Czech
  municipality's `population (P1082) @ point in time (P585)`,
  matches by `(name, kraj)` against the curated list, and writes
  `data/csu_population_2024.csv` — the file the seed script
  already loads on present. Wired through
  `.github/workflows/refresh_population.yml` for operator-triggered
  refresh; no DB access required (the workflow just regenerates
  the committed CSV).
- **Population source switched to the official ČSÚ DataStat file.**
  The Wikidata fetcher is now a fallback; the preferred source is the
  official export "Počet obyvatel v obcích k 1. 1." (download from
  https://data.csu.gov.cz/datastat/data/VYBER/OBY02AT02). The operator
  commits the downloaded JSON-stat file to `data/csu_population.json`;
  `scripts/csu_population.py` parses it (takes the latest year, derives
  each municipality's kraj from the JSON-stat `child` map, drops the
  kraj-level aggregates) and `scripts/seed_curated_cities.py` matches
  municipalities to curated cities by `(name, kraj)`
  (diacritics-insensitive) and upserts `city_population`. The seed
  prefers the JSON and falls back to the legacy CSV when it's absent.
- **Price-per-m² filter everywhere.** Two new `FilterDef`s in
  `toolkit/filter_registry.py` (`min/max_price_per_m2`,
  `pg_column='price_per_m2'`, all-agenda) make per-m² bounds a
  first-class registry primitive. Toolkit comparables, the Watchdog
  matcher, `EstimationFilters` / `WatchdogFilterSpec` on the TS side,
  Browse URL state, the Filters.tsx Price control, and Stats all
  honour it via one consistent expression
  (`price_czk::numeric / NULLIF(area_m2, 0)`). Migration 083 extends
  `browse_stats` to 46 params so the Stats tab stays aligned with
  Map / Table whenever a per-m² bound is set. The PostgREST direct
  paths get this for free because `listings_public` already exposes
  `price_per_m2` as a computed column.
- **15-minute lightweight delta scrape.** New
  `.github/workflows/scrape_delta.yml` (cron `*/15 * * * *`,
  `--limit 200`, image / condition phases skipped) walks the first
  ~3 index pages of each of the 6 category pairs every 15 minutes so
  a newly-listed sreality property reaches the Watchdog feed within
  minutes instead of within a day. The nightly `scrape.yml` still
  owns `mark_inactive` per architectural rule #3 — the partial walk
  here can never falsely flip a live listing inactive thanks to the
  `--limit`-set guard in `scraper/main.py:main`. Concurrency-group
  drops overlapping runs rather than queueing them.
  _(Superseded by Scraper-track Phase 1.6: this job now does a complete
  walk every tick and runs `mark_inactive` itself.)_
- **Watchdog feed polling decoupled from estimation polling.**
  `frontend/src/pages/Watchdog.tsx` switches from an unconditional
  5-second `refetchInterval` to a two-tier callback: 30 s for the
  dispatches feed (matcher ticks every 5 min, so 30 s gives plenty
  of resolution at 1/6 the request volume), bumped to 5 s only when
  any visible row carries a non-terminal estimation status. Drops
  back the moment estimation completes.

**Still next** (separate slice):

- **`/cities` admin page**: in-app uploader for next year's CSV
  (preview + confirm two-step). Today's flow goes through the
  GitHub Action.
- **Per-city `default_radius_m` rework.** The current value comes
  from each city's Mapy.cz bbox half-diagonal (clamped 2–25 km).
  This works for small towns but is too tight for major cities —
  e.g. Brno comes out at 2 km, well short of Brno's actual
  built-up footprint, so a Brno-rule + Brno listing pair can miss
  unless the listing sits within 2 km of the centroid. The right
  fix is a population-weighted radius (`r ≈ k·sqrt(pop / density)`,
  clamped 2–25 km) once `city_population` is seeded by the
  Wikidata fetcher above. Manual overrides via direct UPDATE on
  `curated_cities.default_radius_m` work today.
- **Prague gap.** The operator's source CSV omits Prague
  (instead carries Prague-Východ / Prague-Západ as suburban
  okres entries). City-quality filters therefore don't activate
  for any Prague-bbox listing today. Adding a manual Prague row
  to `curated_cities` (centroid 14.43,50.07 / radius 18 km) and
  seeding its 33 index values from a separate source is the
  cleanest fix; deferred until the operator decides whether they
  want Prague-as-one or Prague-broken-into-districts.
- **Operator decision still open**: whether to expose the `op`
  operator (currently locked to `>=` in the picker) or keep it
  simple.

Headline scope:
- Migration: `cities(city_id, name, csu_code, geom geography(point,
  4326), centroid_admin_polygon geography(multipolygon, 4326) NULL,
  ...)` — canonical reference table. `city_id` resolved via the Czech
  Statistical Office (ČSÚ) municipality code so successive uploads
  align cleanly.
- Migration: `city_indexes(city_id, source_revision, uploaded_at,
  uploaded_by, raw_row jsonb)` + `city_index_values(city_id,
  source_revision, index_name, value numeric)` — long-form so a new
  index column on next upload doesn't need a schema migration. Append
  -only via `source_revision`; the latest revision is the default
  query target, prior revisions stay auditable.
- Migration: `city_population(city_id, as_of_year, population,
  source)` — one row per (city, year) so historical analysis stays
  possible without breaking the latest-wins norm elsewhere.
- Migration: extend `listings` with `nearest_city_id` (FK to
  `cities`, nullable, backfilled from `geom`) so a per-listing filter
  on city quality avoids a per-query spatial join. Trigger updates it
  on insert / coordinate change.
- Population source: pick one canonical feed during scope review
  (ČSÚ open data, Wikidata SPARQL, or the OSM `admin_centre` tag) so
  numbers don't drift between surfaces.
- Spreadsheet ingest: `POST /admin/cities/indexes/upload`
  (bearer-gated) accepts the operator's CSV / XLSX, validates the
  column set, resolves city rows by ČSÚ code (with a name-fallback
  preview for unmatched rows), writes a fresh `source_revision`,
  returns row-level errors. FastAPI parses — never the browser. Same
  upload-then-confirm pattern as building-unit extraction (Phase B1).
- Browse filters: extend `Filters.tsx` with a "City quality" section
  that enumerates available index names from the latest
  `source_revision` so the UI updates automatically when an upload
  adds an index. A new compound proximity filter ("within X km of a
  city matching Y") is the headline new primitive — backed by a
  single PostGIS query (`ST_DWithin` against the matching cities'
  geoms) rather than UI-side iteration. Reuses the existing
  `_shared_filter_where` helper so the matcher and Browse can never
  disagree on what a filter means.
- Notification integration: the saved-filter spec on
  `notification_subscriptions` (Phase U2.7) extends to accept the new
  city-quality and proximity predicates. The dispatch matcher reuses
  the same SQL builder — one shared definition of "matches."
- Operator surface: `/cities` admin page lists the registered
  cities, current population, and latest index values for sanity-
  checking the most recent upload.

**Open questions (operator to decide before implementation starts)**

- **Canonical city identity.** Match cities by ČSÚ municipality code
  (`obec_kod`), Wikidata Q-id, or our own slug? ČSÚ recommended —
  stable Czech-statistics identifier, joins cleanly to most public
  datasets.
- **Geo definition of "in city X".** Centroid + radius (sloppy at
  city edges, cheap), nearest-city assignment (cheap, defensible),
  or polygon containment using ČSÚ admin polygons (correct, adds a
  one-off shapefile import). Nearest-city via `nearest_city_id`
  recommended as the default; polygon containment can be added later
  without invalidating data.
- **Index schema shape.** Long-form `(city_id, index_name, value)`
  (flexible — recommended) vs fixed columns (cleaner SQL, every new
  index needs a migration). Long-form lets the Browse UI enumerate
  indexes from data rather than schema, matching the
  `app_settings`-style discipline.
- **Population cadence.** Bulk-load once from a static dataset
  (cheaper, drifts) or refresh annually via the same upload endpoint
  (matches the index-upload workflow, no scheduled worker)? Annual-
  upload recommended.
- **Filter UI complexity ceiling.** "Within X km of a city with
  markers A>n, B>m and population>k" is a nested predicate. Cap the
  UI grammar at max-depth-1 nested rules (one outer city criterion +
  one optional proximity criterion); deeper compound rules go via a
  free-form JSON expression on power-user subscriptions only.
- **Snapshot vs live for index values.** If a new `source_revision`
  arrives between a notification being saved and its first dispatch,
  does the dispatch use the spec's revision-as-of-saved or the
  current one? Current-revision recommended (matches the
  latest-wins norm); the alternative is heavier and rarely useful.

**Out of scope for this phase**

- Per-user index overrides (every operator sees the same index
  values; single-operator identity model still applies).
- Automated scraping of index data — input is a hand-curated
  spreadsheet, not an automated feed.
- LLM- or ML-derived quality scores. The indexes are operator-
  supplied facts; any reasoning on top happens at the agent layer
  per toolkit rule #1.
- City-quality features beyond Browse filter / notification matching
  (e.g. ranking the agent's comparables by city quality, surfacing a
  quality badge on Listing Detail, applying city-quality predicates
  to estimation cohorts) — natural follow-ups, not gated by this
  phase. Phase QUAL deliberately does **not** touch the estimation
  agent, the building decomposition flow, or any other surface; its
  scope is the Browse filter primitives and the U2.7 notification /
  watchdog spec.

## UI track (parallel, independent of analytical phases)

A browser UI is now a recognized territory rather than a future "maybe."
This track runs in parallel with the analytical phases above; the
toolkit is what makes the UI worth building, but the UI doesn't gate
toolkit work.

### Phase U0: Foundation (done)
- `frontend/` folder with README declaring conventions.
- Migration 008 creates `*_public` views and grants `SELECT` to the
  `anon` role; sensitive columns (`raw_json`, `geom`, hashes, error
  messages) are never exposed.
- CLAUDE.md "Territories" section defines the boundary between the
  Python backend and the future frontend.

### Phase U1a: Database browser (done)
Read-only Vite + React + TS SPA over the `*_public` views with the
`anon` key. Deployed to Railway as a second service alongside the
FastAPI backend. Civic-archive visual direction (laid-paper canvas,
oxidised-copper accent, Fraunces / Inter / JetBrains Mono, tabular
numerals, Czech locale formatting).
- **Browse**: filter sidebar (district typeahead, disposition multi-toggle,
  dual-handle price + area sliders, tri-state status, last-seen-within,
  has-balcony/lift/parking) → Map / Table / Stats tabs. Filter and
  sort state in URL params; bookmarkable, refresh-survives.
- **Listing detail** (`/listing/:sreality_id`): hero, mini-map, key
  facts, snapshot timeline strip (the product's signature visual
  vocabulary), per-snapshot diff table, freshness check log,
  outbound link to sreality.cz.
- **Region**: district multiselect or radius-from-pin definition; live
  aggregates (count, p25/median/p75 price + price/m², per-disposition
  median table), 90-day active-per-day chart, 12-week new-listings bar,
  median time-on-market for delisted listings.
- **Health**: operator dashboard. Last-scrape recency (with
  36-hour stale banner), active count + Δ vs 7 days ago, new-listings
  14-day chart, snapshot-density buckets, freshness checks 24h by
  outcome, fetch-failures table. Per-scrape audit added in
  migration 086 (`scrape_runs` + `recent_scrape_runs` /
  `image_storage_overview` RPCs) — Recharts time-series of
  scraped-new / inactive / images-stored over the last 14 days,
  expandable per-run table broken out by category pair, image-mirror
  progress (stored / total), and a static cron schedule card.
- Migrations 011 (`browse_stats`), 012 (`region_stats` +
  `region_active_by_day`), 013 (`health_summary`), 014 (`browse_stats`
  inactive-only filter), 021 (`region_stats` `ppm2_box` extension).
- Browse-2 add-ons (done): `LocationSearchBox` + Mapy.cz suggest /
  resolve proxy (`/maps/suggest`, `/maps/resolve`),
  `DispositionBoxPlots` on the Region page.

### Phase U1b: Estimation backend (done)
- `estimation_runs` table (migration 010): persistent record of every
  estimation, regardless of trigger. Schema reserves `mode='agent'`
  and `status='pending'/'running'` for U4 without forcing today's
  code to write twice.
- `scraper.url_parser`: turns a sreality URL into a parsed spec by
  reusing `scraper.parser`.
- `/estimations` endpoints: POST creates a run (URL or spec), GET-by-id
  reads one, GET lists with filters and pagination.
- Trace format v1: tool calls + computations recorded with
  `output_summary` only (full data in dedicated columns).

### Phase estimation-4: Generic URL parser (done)
- Migration 020: `parsed_url_cache`, `llm_calls`, `app_settings` +
  `app_settings_history`, plus `source_kind` /
  `parse_confidence` / `parse_confidence_per_field` / `source_html`
  columns on `estimation_runs`.
- `api.llm_client.LLMClient`: wraps the Anthropic SDK, audits every
  call to `llm_calls`, computes USD cost from a per-model price
  table, and emits a one-time daily-cost soft warning at
  `LLM_DAILY_COST_WARN_USD` (default $5).
- `scraper.geocoding.geocode`: Mapy.cz forward geocoding with
  type-based confidence (regional.address → high; street → medium;
  city centroid → low) and a CLI verification helper.
- `scraper.source_dispatcher`: classifies a URL by domain and routes
  to either the deterministic sreality flow or the LLM-driven
  per-source parsers (bezrealitky, idnes_reality, remax,
  best-effort generic). Cache lookup on canonicalised URL hash with
  7-day TTL.
- `POST /estimations/preview`: parse any allowlisted URL through the
  LLM-driven dispatcher and return spec + provenance without
  persisting a run. Coexists with the U2-frontend's existing
  `GET /estimations/preview` (sreality-only, read-only); the POST
  version is the path forward for non-sreality sources.
- `POST /estimations`: now routes through the dispatcher and
  populates the four new audit columns; parse failures persist a
  `failed` row with the error message.

### Phase U2: Estimation flow (done)
End-to-end browser flow over the U1b backend.
- `/estimate`: two-step form (paste URL or pick listing → review and
  edit spec → submit). Pre-fills from `/estimations/preview`; on
  submit POSTs `CreateEstimationIn` to the FastAPI service. URL-origin
  runs send `url` + a minimal `spec_overrides` diff so the server
  records the original `input_url` for traceability.
- `/estimations`: list view of past runs with source/status filters,
  URL-state-driven pagination, links to detail.
- `/estimation/:id`: complete display — rent range strip,
  confidence/source pills, warnings block (failed runs render
  `error_message` and a truncated trace, no range), input recap,
  trace timeline, comparables table sorted by data age, re-run
  button (POSTs new run with `parent_run_id` set).
- `Timeline` component: dispatches on `step.kind` via a renderer
  map (`tool_call` / `computation` / `reasoning`). Today renders
  the deterministic 4-step trace; the same component will render
  the U4 agent's longer traces without rework. Smart default
  expansion (last step + steps over 500 ms).

### browse-2: Region search + box plots (done)
- Mapy.cz suggest / resolve proxy endpoints (`api/maps.py`) — bearer-
  gated, 5-min in-process TTL cache on suggest, admin_boundaries-aware
  polygon resolution that auto-degrades to point + radius when the
  table is missing or empty.
- Region page rebuilt around a single `LocationSearchBox`: typing a
  street address, neighbourhood, or kraj returns ranked Mapy.cz
  suggestions and resolves to either a polygon (when admin_boundaries
  ships) or a point + radius. Browse-1's district / radius pickers
  remain reachable under an "Advanced" disclosure for legacy
  bookmarks and direct radius drag-and-drop.
- Per-disposition price-per-m² box plots (custom SVG) replace
  browse-1's median-only summary table. Tukey 1.5×IQR whiskers
  clipped to min/max, copper median line, no outlier dots, no
  per-disposition colour-coding. A numeric table beneath the SVG
  preserves precise readouts.
- Migration 021 (`021_region_stats_box.sql`) extends `region_stats`
  with a per-disposition `ppm2_box` field; existing fields preserved
  for backwards compatibility.

### Phase estimation-5: URL-parser frontend (done)
- `ConfidenceIndicator` component + per-field confidence surface on
  the review step.
- `previewListingUrl` + `useUrlPreview` React hook: drives the
  paste-URL step against `POST /estimations/preview`.
- Listing-block render on the URL step; `force_refresh` to bypass
  the 7-day cache; `cost_usd_total` rolled up from `llm_calls`.
- Commits `e9da41f`, `65b9967`, `d66da7e`. PR #29.

### Phase U-BV: Browse velocity, card badges, filter overhaul (done)
- Migration 052 promotes "turned in" (TOM = days on market) to a
  first-class column on `listings_public`. Same definition as
  `toolkit/velocity._tom_days`: `now() - first_seen_at` for active
  rows, `last_seen_at - first_seen_at` for delisted. SQL and Python
  now share one authoritative computation.
- Migration 053 redoes `browse_stats` with a new filter surface:
  `tom_days_min/max`, `last_seen_min/max_days` and
  `first_seen_min/max_days` (both replacing the old preset
  `seen_within_days_filter`), `building_type_filter text[]`.
  Implicit `active_only=true` default dropped — Browse no longer
  hides delisted listings unless asked.
- Toolkit `ComparableFilters` grows the same six filter fields
  (`tom_days_min/max`, `last_seen_min/max_days`,
  `first_seen_min/max_days`) and flips defaults so no implicit
  freshness gate fires. The deterministic estimator's
  `_DEFAULT_ACTIVE_ONLY` and per-kind `max_age_days` are gone with
  it. Velocity logic is unchanged; the new filter fields flow
  through `_shared_filter_where` for free.
- API: `FindComparablesIn`, `EstimateYieldIn`,
  `ComputeMarketVelocityIn`, `DescribeNeighborhoodIn`, and
  `CreateEstimationIn` all grow the six new optional filters; the
  deterministic `_build_filters` plumbs them through. Agent's
  `base_filters` carry them per-run without per-tool schema bloat.
- Frontend: `ListingFilters` adds `tomDaysMin/Max`,
  `lastSeenMinDays/MaxDays`, `firstSeenMinDays/MaxDays`,
  `buildingMaterial`. `applyFilters` plumbs the days-ago ranges
  against `last_seen_at` / `first_seen_at` and the TOM range against
  `tom_days`. The four-bucket Building material picker (Cihla /
  Panel / Smíšená / Ostatní) maps "Ostatní" to the five remaining
  sreality values. Default `status` is now `'any'`.
- Filter panel regrouped: Category / Location / Disposition / Price /
  Size / Status & velocity / Building / Amenities / Curation.
  ControlGroup legend bumped (0.82rem, ink-primary, semibold) so it
  visually outweighs the smaller Section labels (0.62rem,
  ink-tertiary). Redundant inner labels dropped on singleton groups.
- Browse cards now stack four metadata badges down the right margin:
  status (sage `Aktivní` / brick `Neaktivní`), first-seen (`od 5. 5.`),
  last-seen (`viděno 8. 5.`), and the copper TOM pill
  (`94 dní`, Czech plural). Re-uses the existing token palette and
  borders-only depth strategy; no new design tokens.
- Migration 061 enriches `browse_stats` with a
  `price_quartile_velocity` field: the filtered cohort is split into
  four equal-size price buckets via `ntile(4)` and each bucket reports
  its `tom_days` distribution alongside its price range. Stacks on
  top of 060's expanded signature — DROP-then-CREATE because the
  function body grows a new CTE; the parameter list is unchanged from
  060. The Stats tab renders this as a fourth Card ("Turnover by
  price quartile") with horizontal box plots reusing the
  `DispositionBoxPlots` SVG idiom. Active vs. delisted semantics of
  the per-bucket TOM follow the user's status filter — no per-bucket
  active/inactive split is computed.
- Migration 062 adds `mean` to each bucket's `tom_box` so the
  price/velocity signal isn't lost when `tom_days` is integer-clumped.
  With a 14-day scrape window the five-number summary collapses to
  identical medians across all four buckets even though means differ
  monotonically (active 2+kk byt/pronajem: 8.9 / 9.0 / 9.4 / 9.9 days).
  Frontend renders the mean as a copper dot on the box plot and a new
  MEAN column in the numeric table; the caption now names the
  integer-flooring caveat explicitly.
- Migration 063 replaces the four-equal-bucket
  `price_quartile_velocity` with a seven-band percentile split
  `price_band_velocity`: p0–p10, p10–p25, p25–p45, p45–p55, p55–p75,
  p75–p90, p90–p100. Narrower bands at the tails and around the
  median, wider through the body, so the chart surfaces tail-vs-body
  differences that an equal-quartile split would mask. The new
  payload also reports `pct_share` per band (actual share of priced
  cohort, since ties at percentile cuts make bucket sizes drift from
  their nominal 10/15/20/10/20/15/10). Active 2+kk byt/pronajem shows
  the body bands clustered at mean ≈ 9.2d while the priciest decile
  jumps to 10.5d. Frontend rewrite: `PriceQuartileVelocity` →
  `PriceBandVelocity`, seven rows on the y-axis with percentile +
  price-range + n + share labels; Card heading and caption updated
  accordingly.

### Phase U2.5: Freshness write-path (done)
- "Ověřit aktuálnost" (Verify freshness) button on Listing Detail's
  freshness-checks section. Calls the bearer-token-gated
  `POST /tools/verify_listing_freshness` on demand via the existing
  `request()` auth path (no new auth mechanism) — `max_age_hours: 0`
  so an operator click always forces a real re-fetch rather than the
  throttle's `cached` short-circuit.
- `verifyListingFreshness` wrapper + `VerifyFreshnessResult` /
  `FreshnessOutcome` types in `frontend/src/lib/api.ts`.
- Pending state ("Ověřuji…" / "Re-fetching the listing from the
  source…") and a result line that maps the outcome
  (`unchanged` / `updated` / `gone` / `fetch_error` / `cached`) to a
  human message + the existing `OutcomeChip`. On success it
  invalidates the `listing`, `snapshots`, and `freshness` queries so
  the timeline strip and the check log refetch immediately.
- The audit log table (`listing_freshness_checks`) and the wrapped
  `scraper.freshness.freshness_check` already existed from Phase 2.5;
  this phase added only the frontend affordance. Backed by a
  `FreshnessBlock` component test (the full live e2e needs production
  secrets).

### Phase U-Nav: Unified browse → detail navigation (next)

Today the top nav exposes `Listing` and (historically) `Estimate` as
top-level destinations. That conflates two distinct UX roles:
**list pages** (Browse, Estimations, Collections) are entry points,
**detail pages** (Listing, Estimation, Building, Collection item) are
drill-downs from those lists. The standalone `/listing` entry with no
id resolves to an empty shell and the entry only exists to satisfy
the menu link — a tell that the IA is wrong. This phase collapses
detail pages back into their parent flows and adds an explicit
"where am I, how do I get back" affordance.

**Scope:**
- **Remove from menu:** `Listing` link (currently `frontend/src/components/Shell.tsx:10`)
  and the `path: 'listing'` (no-id) route in `frontend/src/routes.tsx`.
  `Estimate` is already gone — the modal-trigger CTA in the top bar
  replaces it. Menu becomes: Browse, Region, Estimations, Collections,
  Health.
- **Detail pages stay reachable only via drill-down** from their
  parent list. `/listing/:sreality_id`, `/estimation/:id`,
  `/building/:id`, `/collection/:id` are unchanged as URLs; they
  just no longer have a nav entry.
- **Breadcrumbs + back affordance** on every detail page. Renders the
  parent context (e.g. "Browse / Praha 6 — 2+kk apartments / Listing
  detail") and preserves the parent's filter/sort/page state on
  click. Mechanism: when a list-page link navigates to a detail, it
  stashes the current URL (with all query params) into router
  state; the breadcrumb's "back" link reads from that state and
  falls back to the bare list URL if state is missing
  (deep-link / refresh case).
- **URL hierarchy** — pick one of the patterns below during the
  design kickoff. Not a foregone conclusion which is right for this
  app; documenting the trade-offs so the operator can choose.

**Proposed UX patterns (to discuss before any code lands):**

1. **Breadcrumb + flat URLs (recommended starting point).**
   Keep today's flat routes (`/browse`, `/listing/:id`) and add a
   breadcrumb strip + a sticky "← Back to results" link that
   restores the parent's filter state from router state. Pattern
   used by Airbnb, Zillow, GitHub's issue/PR detail pages.
   Cheap to ship, no migration of bookmarked URLs, breadcrumb
   degrades gracefully on deep-link / refresh (still shows
   "Browse" as parent, just without the specific filter context).
2. **Nested URLs reflecting drill-down.**
   `/browse/listing/:sreality_id`, `/estimations/:id`. The URL is
   the breadcrumb — back button = strip last segment. Pattern used
   by Linear, Notion, most file-tree UIs. Stronger sense of
   hierarchy; downside is the same listing reached from Collections
   would live at `/collections/:cid/listing/:sreality_id`, forcing
   either route duplication or a canonical-detail-URL convention.
   Bookmark migration needed (redirects from `/listing/:id`).
3. **Detail-as-overlay (modal / side sheet).**
   Clicking a listing in the Browse table opens an overlay over the
   filtered list rather than navigating away. Pattern used by Gmail,
   Linear's command-K previews, Booking.com's room picker. Best
   when users browse → preview → browse repeatedly. Trade-off: the
   detail loses real-estate, deep-linking requires a parallel
   full-page route anyway, and the snapshot timeline (the product's
   signature element) wants the full page.

Realistic combo: pattern 1 across the board, with pattern 3 as a
future enhancement on Browse → Listing once the breadcrumb is in
place and we know which detail interactions stay shallow.

**Out of scope for this phase:** changing the snapshot timeline,
adding new detail-page content, restyling list pages. Pure IA +
navigation work.

### Phase U3: Toolkit-backed views (later)
Surfacing `describe_neighborhood`, `find_distribution_outliers`, and
the velocity tools through the UI. Auth-gated; specific shape decided
when U1 + U2 are live.

## Map track (parallel)

Geographic drill-down beyond the existing district facet. Independent
of the analytical and UI phases; runs alongside them.

### map-1: typed locality IDs
- **Part A (done):** inspection of `raw_json.recommendations_data`
  confirmed 100% coverage on `locality_municipality_id`,
  `locality_quarter_id`, and `locality_ward_id` across active
  listings. Cardinality and naming notes captured in commit
  `d663233`.
- **Part B (proposed):** migration 016 promotes those three IDs to
  typed columns, sanitising sreality's `-1` sentinel to `NULL`.
  Parser + scraper write-path landed in commit `d663233`; migration
  is committed in `migrations/016_locality_ids_extended.sql`.
  Confirm-and-mark-done item: verify whether Part B has actually
  been applied to the production database (auto-status block above
  should show migration 016 applied if so) and update this entry
  accordingly.
- **Part C (done):** backfill from `raw_json` for existing rows;
  exposed via `listings_public`.
- **Part D (done):** spatial join scaffolding for ČÚZK / RÚIAN
  polygons. Migration 017 (`admin_boundaries`),
  `scripts/ingest_boundaries.py`,
  `.github/workflows/ingest_boundaries.yml`. Bridge table populated
  by the ingest workflow.

## Scraper track (parallel)

Scraper-specific evolution beyond Phase 1's nightly index walk.
Independent of the analytical, UI, and map tracks.

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
> **Design locked (2026-05-25): see
> [`docs/design/multi-portal-dedup.md`](docs/design/multi-portal-dedup.md).**
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

## Dedup + canonical listing track (parallel)

Today the `listings` table is effectively a mirror of sreality.cz
keyed on `sreality_id`. As multi-portal ingestion (Scraper Phase 2)
brings bezrealitky / idnes / remax / maxima / etc. into the same
table, "the same property" will start showing up multiple times —
both within a single run (cross-portal collision) and across runs
(taken down and relisted under a new broker after expiring). This
track is the work to identify those duplicates and present one
canonical listing per real-world property.

**Directional architectural shift surfaced by the operator.** The
`listings` table evolves from "mirror of sreality" to "mirror of
every observed property across all sources, deduplicated."
Architectural rules #1 (append-only migrations), #2 (snapshot on
content change), and #3 (never delete listings) all carry over —
applied at the canonical level rather than the per-source level.
The migration is significant; this track plans the path but does
not commit to it without an operator decision on the canonical
shape (see D1 below).

> **Design locked (2026-05-25): see
> [`docs/design/multi-portal-dedup.md`](docs/design/multi-portal-dedup.md).**
> The operator's expanded requirements (cross-portal price-history
> chart, link history, all-sources-inactive lifecycle, "listed on 3+
> sites" / "price dropped 10%+" filters, daily property-change
> notifications) outgrew Shape A. We now adopt **Shape B** (a thin
> `properties` parent + existing `listings` as per-source children) and
> treat D1 + D2 + Scraper Phase 2 as **one feature** shipped in six
> independently-signed-off slices (0 foundation → 5 image tier). The
> design doc is the source of truth; the Shape-A-as-default text in the
> D1/D2 subsections below is **superseded** and kept for history. Each
> slice still needs the per-slice operator sign-off listed in the doc
> before its migration lands.
>
> **Progress:** Slices 0, 1, and 2a are **built and applied**. Slice 0
> (migrations 091+092 + scraper wrapper) + Slice 1 (migrations 093+094, the
> recompute job + hourly workflow, Browse Map/Table/Cards on
> `properties_public`). Slice 2a (migration 095) denormalised the filter
> columns onto `properties` so `browse_stats_properties` is perf-equivalent to
> the listing-grain RPC, repointed the Stats tab to it, and added the four
> derived filters (`distinct_site_count_min`, `price_drop_count_min`,
> `price_rise_count_min`, `max_price_drop_pct_min`) through the registry into
> Browse (Map/Table/Cards + Stats). Slice 2b (migration 096) moved
> notifications to the property grain (dispatch once per real property, not per
> portal listing), added a second matcher (`match_changes_once`) that fires
> `price_drop` change-events for properties dropping in the lookback window,
> and surfaced the four derived filters in Watchdog. Slice 3a (migration 097)
> built the portal-agnostic insert-time Tier-1 matcher: a geo+price+area probe
> (`ST_DWithin 20m`, price ±2%, area ±1m², same-source excluded) that attaches
> a new listing to a near-matching property, creates a singleton on no match,
> or enqueues a `property_identity_candidates` row on ambiguity — plus the
> `ScrapedListing` contract + a negative synthetic-id sequence for non-sreality
> rows. It's inert for today's sreality-only data (verified). Slice 3b
> (migration 098) shipped the first portal scraper (operator chose **bazos**):
> `scraper/bazos_parser.py` (deterministic selectolax HTML→`ScrapedListing`),
> `scraper/bazos_client.py` (adaptive-throttle fetch reusing `RateLimiter`),
> `scraper/bazos_main.py` (index→detail→stage-in-`portal_raw_pages`→parse→
> `ingest_scraped_listing`, no `mark_inactive` on a partial walk), and the
> manual `scrape_bazos.yml` workflow. `portal_raw_pages` decouples fetch from
> parse so pages re-parse without re-fetching.
>
> **Complete (2026-05-28).** The remaining slices shipped: the merge/unmerge core
> + review API (migration 100, `toolkit/property_identity.py`, `/dedup/*`), the
> Tier-2 fuzzy sweep + auto-merge classifier (`scripts/dedup_sweep.py`,
> `dedup_sweep.yml`), the `/dedup` operator review UI, the Listing Detail
> cross-source price chart + "listed on N sites" panel, the image pHash tier
> (migration 102, `scraper/image_phash.py`, `compute_image_phash.yml`), and region
> stats on the property grain (migration 103). Auto-merge is conservative —
> only ≤30m + an independent corroborator (near-exact address, low-Hamming pHash,
> or vision); everything else queues. Every merge is reversible via `unmerge_group`.
> The bazos pilot is now **scheduled (every 6h)** and lands data after three pilot
> fixes (return-the-PK for image attribution; cast geom params so null-coord rows
> insert; extract coords from the page-wide maps link). Cross-source matching is
> geo-based, so it lights up as bazos coordinates accumulate; the sweep already
> produces real bazos↔sreality candidate pairs. Next portals (bezrealitky / idnes)
> reuse the same `ScrapedListing` → `ingest_scraped_listing` framework.

### Phase D1: Strict cross-source dedup (proposed — superseded by the design doc above)

Catch the obvious duplicates: the same listing observed on two
portals at once, or the same source-listing re-fetched under a
slightly different URL. This is a precondition for Scraper Phase 2
— without it, multi-portal ingestion multiplies every listing by
the number of portals it appears on. Also a precondition for Phase
U2.7's "notify once per real property" guarantee.

**Canonical shape (operator decision required before this phase
starts)**

Two viable shapes. Both preserve all existing snapshot history and
respect architectural rules #1 / #2 / #3.

- **Shape A — single canonical table, per-source observations as
  history.** Keep `listings` as the canonical row (one per real
  property). Existing `sreality_id` becomes one of many possible
  `source_id_native` values. New companion table
  `listing_source_observations(listing_id, source,
  source_id_native, source_url, first_seen_at, last_seen_at)`
  records every source that has surfaced this listing. Existing
  `listing_snapshots` gains a `source` column so per-source
  content drift is still visible in the diff timeline. Lowest
  migration cost; downstream queries (`find_comparables`,
  `browse_stats`, RPCs, frontend) keep working with minimal
  changes. **Recommended default.**
- **Shape B — two-table model: `properties` + `listings`.** New
  canonical `properties` table; existing `listings` becomes per-
  source observations linked back via `property_id`. Cleaner
  separation of concerns, but every downstream query has to learn
  the join. Tens of files touch this; the visible payoff is small
  if Shape A's denormalised approach already handles the same use
  cases. Reopen when Shape A's limits show up in production.

**Matcher (insert-time, has to be cheap)**

- **Tier 1 — exact canonicalised URL.** Lower-case scheme + host,
  strip query, strip trailing slash, sha256. Hash match against an
  existing canonical row → append a new
  `listing_source_observations` row and a snapshot if content
  differs; do not insert a new canonical row.
- **Tier 2 — (lat, lng, price_czk, area_m2) within tolerance.**
  `ST_DWithin` within ~20 m, price within ±2%, area within
  ±1 m². High precision; catches "same listing surfaced on two
  portals simultaneously."
- **Tier 3 — agent phone / email when exposed.** Same
  (phone, area, district) triple within 30 days = likely the same
  listing relisted by the same agent. Lower precision; auto-merge
  gated on at least one more matching marker.
- **Ambiguous tier.** Anything that matches at lower confidence
  goes to a new `listing_duplicate_candidates` queue for operator
  review. Default to "no merge" rather than "guess merge."

**Migration scope**

- New numbered migration co-authored with Scraper Phase 2's
  `source` / `source_url` / `source_id_native` columns (single
  migration touching the same surface).
- Shape-A path: add `listing_source_observations` +
  `listing_duplicate_candidates`; add `source` to
  `listing_snapshots`. Backfill: every existing row gets one
  `listing_source_observations` entry with
  `source='sreality', source_id_native=sreality_id::text`. No
  data loss.
- `_shared_filter_where` learns to filter by source via the new
  observations join (read path stays on `listings`).

**Notification feature link (Phase U2.7)**

Phase U2.7's `notification_dispatches` table currently keys on
`sreality_id`. Once D1 ships the canonical id is the dedup key, so
a single property surfaced on bezrealitky AND sreality fires one
notification instead of two. The U2.7 schema gets a one-line
update at D1 land time: `sreality_id` → `listing_id` referencing
the canonical row. Same `(subscription_id, listing_id)` uniqueness
guarantee, just at the right grain.

### Phase D2: Fuzzy property identity (proposed)

Catch the harder case: a listing taken down and relisted weeks
later with different wording, different broker, possibly different
photos. Markers per the operator's brief (everything else — price,
broker, URL, listing copy — is allowed to vary):

- **Address** (street name + house number when present; full
  address is the highest-precision signal).
- **City / district / cadastral area.**
- **Floor** (when known).
- **Disposition + area triangulation.** A 51 m² 1+1 and a 50 m²
  2+kk are likely the same flat — relisted with a different
  disposition label. Use a tight equivalence map across nearby
  dispositions (`1+1 ≈ 2+kk`, `2+1 ≈ 3+kk`, etc.) combined with a
  ±10% area band.
- **Image similarity.** Two-tier to keep cost down:
  - Cheap first pass: perceptual hash (`pHash` / `aHash`) on the
    hero image via Pillow. Catches re-uploads of the same photo
    with minor recompression / resizing.
  - Vision tier for the ambiguous: reuse
    `compare_listing_images` from Phase 6 (Claude vision).
    Higher cost; only invoked when the cheap markers say "maybe."

**Matcher (background sweep, NOT insert-time)**

D1's matcher runs at insert time and has to be cheap. D2 is
heavier (image fetches, sometimes vision calls); runs as a
periodic background sweep over recently-inactive listings against
currently-active listings, surfaces candidates, never auto-merges
without operator review. Precision over recall.

- New table `property_identity_candidates(left_listing_id,
  right_listing_id, confidence, markers_matched jsonb,
  suggested_at, status, reviewed_at, reviewed_action)` — append-
  only audit of every candidate the sweep proposes. Status:
  `proposed` → `merged` | `dismissed`. Operator reviews on a new
  `/dedup/candidates` page (frontend).
- On `merged`: the older listing's snapshots are re-pointed at the
  canonical row, both `listing_source_observations` entries
  collapse onto the canonical id. Architectural rule #3 (never
  delete) holds — merged listings keep their history, the
  canonical row just gains it.
- Sweep cadence: weekly is plenty; relisted-after-expired patterns
  unfold on a multi-week timescale, not minutes.

**Address normalisation**

Czech addresses arrive in a variety of formats (street + descriptive
number + orientation number, street + house number, P.O. box). A
normalisation helper lives in a new `toolkit/addresses.py` —
canonicalises whitespace, strips diacritics for fuzzy comparison
only (display form keeps them), parses out descriptive vs.
orientation numbers, returns a stable comparison key. Hermetic
tests against a fixture set of real Czech address strings.

**Open questions (operator to decide before D2 starts)**

- **Conservative vs. aggressive merging.** Default is conservative
  (queue, operator approves). Aggressive auto-merge above a
  confidence threshold is tempting for scale but bakes in
  irreversible false positives.
- **Image-tier model.** `compare_listing_images` is already there
  but is materially expensive per pair (~$0.05). For D2's volume
  a cheaper dedicated image-similarity model may be needed; pick
  when the cohort size makes the bill visible. pHash alone may
  cover most cases.
- **What "merged" actually means in the UI.** Browse should show
  one row per canonical property by default (default-on toggle to
  "show all source observations" for power use); Listing Detail
  shows all source observations on a tab. Confirm before
  implementation.

**Out of scope for D1 + D2**

- Cross-property dedup beyond same-property identification (e.g.
  identifying neighbouring units that are part of the same
  building — that's the Building decomposition track's job).
- Automatic re-merging when a previously-dismissed candidate
  re-surfaces with new markers — manual re-trigger for now.
- The Shape-B full architectural split (`properties` parent table
  + per-source `listings` child). Reopen once Shape A's limits
  show up in production.

## Operator workflow track (parallel)

User-facing features that don't fit the analytical, estimation, UI,
map, or scraper tracks. Operator-scoped (single shared identity, no
per-user accounts — matches today's bearer-token model).

### Phase U2.6: Collections + tags + notes (done)
Operator watchlists, freeform coloured tags, and per-listing journal
notes — end-to-end.
- Migrations 022 (`collections` + `collection_listings`), 023
  (`listing_notes`), 024 (`tags` + `listing_tags`, palette pinned
  to eight named colours by CHECK), 025 (`*_public` views +
  `listings_with_tags(tag_ids)` RPC with AND-semantics, capped at
  5000 rows).
- API: `api/curation.py` exposes CRUD over `/collections`,
  `/listings/{id}/notes`, and `/tags`; routes wired in `api/main.py`
  around line 612+. All bearer-gated per CLAUDE.md toolkit rule #8.
  Tag colour mirrored in `api/schemas.TagColor` (eight-name Literal).
- Frontend:
  - `/collections` index with inline new-collection form, listing
    counts, soft-delete with confirm.
  - `/collection/:id` detail with rename/description edit, delete,
    and a slim member-listings table reusing the Browse/ListingTable
    visual language (sreality_id link, district / disposition / area
    / price / last seen / status / added_at + remove button).
  - `ListingDetail` gains a `CurationBlock` sitting between
    KeyFactsBlock and TimestampsBlock: every collection rendered as a
    toggle (✓/+ chip), tag chips with an autocomplete picker that
    can create a new tag inline (eight-colour palette), and a
    collapsing notes journal (textarea + chronological list).
  - Browse `Filters.tsx` Curation group exposes a tags facet —
    AND-semantics, delegates to the `listings_with_tags` RPC.
- New tokens: `--color-tag-{copper,sage,brick,ochre,slate,plum,teal,sand}`
  + `-soft` pair, scoped at the bottom of `globals.css` per the
  "new tokens by domain-name" rule; the four pre-existing semantic
  colours alias their global token, the four new ones (slate, plum,
  teal, sand) ship with new swatches and light/dark variants.
- Future hook (out of scope, but the schema supports it): the agent
  reads collections as seed examples — "estimate this listing using
  only comparables from collection X."
- Follow-ups landed:
  - Migration 033 adds `tag_ids bigint[]` to `browse_stats` with the
    same AND-semantics as `listings_with_tags`. The Browse Stats
    tab now agrees with Map / Table when the operator filters by
    tag.
  - `PATCH /tags/{tag_id}` (`api/curation.update_tag` +
    `api/schemas.UpdateTagIn`) supports in-place rename + recolour;
    listing attachments are preserved because `listing_tags` joins
    by `tag_id`, not by name. A shared `TagEditPopover` wires
    rename / recolour / delete into both tag pickers — the
    CurationBlock matches list and the Browse Filters "Add" rows.

### Phase U-ME: Manual rental estimates (next)

Capture operator-judgement rent figures as first-class data and
make them visible to both humans (a panel on Listing Detail) and
the agent (a new toolkit tool, consulted by
`rental_estimator_v1` before `record_estimate`). Bridges the gap
between an operator's broker quote / portfolio benchmark / gut
number and the agent's defensible distribution — today that
private signal only lives in `listing_notes` free text where
neither side can use it as a number.

Shape locked with the operator:
- Point estimate (`rent_czk` integer, CHECK 1000–1000000), not
  a range. Simpler than mirroring the estimator's p25/p75 and
  matches how operators actually write the number down.
- One row per estimate; many per listing. Mutable rows with
  full audit history on UPDATE and DELETE via a trigger (same
  pattern as `app_settings_history` in migration 020).
- Free-text `author` + `source_kind` CHECK ∈
  `broker / gut / external_comp / portfolio / other` + optional
  `notes` (≤4000 chars).

Scope:
- Migration 046: `manual_rental_estimates` (FK
  `sreality_id`, the fields above, `created_at`, `updated_at`,
  `updated_by`) + `manual_rental_estimates_history` (append-only,
  `change_kind` ∈ `update / delete`) + BEFORE UPDATE / AFTER
  DELETE trigger. `manual_rental_estimates_public` view with
  anon select grant (same pattern as `listing_notes_public` in
  migration 025). (Originally drafted as 043; renumbered after
  main's Phase AI slice A claimed slot 043 with
  `043_estimation_trace_payloads.sql`.)
- API: new `api/manual_estimates.py` exposing CRUD over
  `/listings/{id}/manual_estimates` (GET + POST) and
  `/manual_estimates/{id}` (PATCH + DELETE). All bearer-gated
  per CLAUDE.md toolkit rule #8. Pydantic schemas appended to
  `api/schemas.py`.
- Toolkit: `toolkit/manual_estimates.py:get_manual_rental_estimates(conn,
  sreality_id)` returns the standard `{data, metadata}` envelope
  with `data.estimates` (empty list when none exist).
  POST `/tools/get_manual_rental_estimates` route in
  `api/main.py`.
- Agent: handler + `_ToolDef` entry registered in
  `api/agent.py:_build_tool_registry()` so the tool is callable
  by name from the agent loop. Provider-agnostic — no changes
  needed in `api/providers/`.
- Migration 047: `UPDATE skills` for `rental_estimator_v1` *and*
  `rental_estimator_full_v1` (the sibling skill added by main's
  PR #77 — both want the new tool). Appends
  `get_manual_rental_estimates` to `allowed_tools` (idempotent
  guard via `not (allowed_tools @> ...)`), same shape as
  migration 045's `read_floor_plan` add. The `system_prompt`
  update that inserts the "CONSULT MANUAL ESTIMATES" step into
  each skill's instructions is *not* part of this migration —
  per migration 045's precedent, prompt edits are an operator
  action via the Settings UI so we never overwrite hand-edits.
  The on-disk `SKILL.md` files carry the inline numbered step
  as canonical documentation. (Originally drafted as 046; bumped
  alongside the 043 → 046 rename above.)
- Frontend: `ManualEstimatesBlock` slotted into
  `frontend/src/pages/ListingDetail.tsx` after `CurationBlock`
  (manual estimates are operator-curated like tags/notes;
  same shelf is the natural home). Reads via
  `manual_rental_estimates_public` with the anon key; writes go
  through the bearer-gated API endpoints. Wrappers in
  `frontend/src/lib/api.ts` (`listManualEstimates`,
  `createManualEstimate`, `updateManualEstimate`,
  `deleteManualEstimate`). No design-token changes.

Out of scope for this phase:
- Manual estimates on sales / commercial listings (the field
  is named `rent_czk` and CHECK-bounded; a future migration
  generalises).
- The agent's ad-hoc Python code execution capability — that's
  Phase 7d above, deferred.

### Phase U2.7: New-listing notifications — in-app slice (shipped)

In-app slice landed: saved-filter "Watchdog" surface in the SPA, a
background matcher loop in the FastAPI service, and per-row
estimation kickoff that runs deterministically in the background and
surfaces the yield once it lands. Email / SMS / push remain deferred
(see open questions below). Cron cadence is still nightly; the
operator can call `POST /notifications/matcher/run` from the UI's
"Run matcher now" button to trigger an immediate evaluation against
any newly-scraped listings.

**What shipped**

- Schema: migrations `056_notification_subscriptions.sql`,
  `057_notification_dispatches.sql`, `058_notifications_app_settings.sql`.
  Dispatches carry a nullable `estimation_run_id` FK so the
  operator-triggered yield calculation links back to the
  estimation row that lives on the existing `/estimation/:id` page.
- Backend: `api/notifications.py` owns the `WatchdogFilterSpec`
  Pydantic model, the SQL-clause renderer (mirrors
  `_shared_filter_where` semantics), and the matcher loop spawned via
  FastAPI's lifespan context manager. `api/routes/notifications.py`
  exposes the standard bearer-gated CRUD + dispatch endpoints. The
  matcher reads its cadence and the watermark from `app_settings`
  rows seeded by 058 so the operator can tune both without a
  redeploy.
- Frontend: new `Watchdog` nav tab, `/watchdog` feed page,
  `/watchdog/manage` list, `/watchdog/new` and `/watchdog/:id/edit`
  filter editor. Notification rows expose the listing, disposition,
  price, when it fired, the watchdog name, an "estimation" column
  that streams the yield once the background task completes, and a
  per-row "Run estimation" button.

**What's deferred**

- Email / SMS / push channels. `notification_dispatches.channel` is
  CHECK-bounded to `'in_app'` only; a future migration adds the new
  enum values and the dispatch worker grows a fan-out branch.
- 5-minute scrape cadence (Shape A from the original proposal).
  Today's nightly cron still applies; the matcher loop honestly
  surfaces "no fresh listings" between scrapes. A new
  `.github/workflows/scrape_probe.yml` is a separate slice.
- Per-user identity (one shared operator stays the model).

**Original brief (kept below as the design rationale)**



Two cross-cutting pieces have to land together: a notification
backend + UI for managing subscriptions, and a scraper cadence
change so the underlying data refreshes more often than nightly.

**Notification surface**

- Migration: `notification_subscriptions` (one row per saved filter
  spec, columns mirroring the Browse filter sidebar — district /
  disposition / price range / area range / has-balcony / has-parking
  / category_main / category_type / tag_ids, plus `is_active`,
  `name`, `created_at`, `updated_at`). One operator identity today
  so no `user_id` column yet — see open questions below.
- Migration: `notification_dispatches(subscription_id, sreality_id,
  dispatched_at, channel, status, error_message)` — append-only
  audit + dedup guard so a (subscription, listing) pair never
  re-fires even if the matcher re-runs. **Cross-link to Dedup
  track Phase D1:** once D1 ships, the dedup key changes from
  `sreality_id` to the canonical `listing_id`, so a property
  surfaced on multiple portals fires one notification rather than
  one-per-portal. This is a single-column rename on
  `notification_dispatches`; no functional change to the dispatch
  worker beyond reading from the canonical row.
- API: new `/notifications/*` routes (CRUD on subscriptions, list of
  recent dispatches, manual "test send" for a subscription). Bearer-
  gated; browser writes flow through here, never direct Postgres.
- Frontend `/notifications` page: list / create / edit / delete
  subscriptions, reusing the Browse `Filters.tsx` components so the
  filter spec stays canonical across surfaces. A "matches today"
  counter per subscription drives intuition before the operator
  enables alerts.
- Listing Detail gets a "notify on listings like this" affordance
  that pre-fills a new subscription from the listing's facets.

**Dispatch worker**

- New scheduled job (GitHub Actions cron, or Railway scheduled
  function — pick alongside the cadence decision below). Every run:
  1. Find listings inserted into `listings` since the previous
     successful dispatch run. Driven by `listings.first_seen_at` (or
     `created_at` if cleaner). Anti-join against
     `notification_dispatches` to skip anything already fired.
  2. For each active subscription, run the filter spec against that
     window. Reuse `_shared_filter_where` so the matcher and Browse
     can never disagree on what a filter means.
  3. Fan out emails (one message per (subscription, listing) match,
     or one digest per subscription per run — pick during scope
     review). Write a row to `notification_dispatches` per send.
- Email provider: one of SendGrid / Postmark / Mailgun / SES (see
  open questions). Provider credentials are env-only, never
  inlined into the browser bundle. Architectural rule #1 (append-
  only migrations), #2 (snapshot-on-change), #3 (no deletes),
  #4 (last_seen_at semantics) all preserved — this feature is
  read-mostly over the listings tables and writes only to the new
  notification tables.

**Scraper cadence change (cross-cutting, required)**

Current nightly cron surfaces new listings ~24h late, which makes
the alert feature feel useless. Operator proposal: run the scraper
every five minutes. Naive translation of the six-category nightly
walk to a 5-min cron is too aggressive — 288 full runs/day would
hammer sreality and blow the GitHub Actions minute budget. Two
viable shapes to choose between:

- **Shape A — light "new-listings probe":** a new entry point that
  walks only the first 1-2 index pages per category sorted by
  newest, no detail refetch of existing listings, no
  `mark_inactive` call (architectural rule #3 already forbids
  inferring inactivity from a partial walk — the existing
  `mark_inactive` skip-when-`--limit` branch lights up here). The
  full nightly walk stays untouched, preserving snapshot density
  and inactive bookkeeping. Recommended default.
- **Shape B — lower-footprint full walk on a tighter cron:** keep
  one cron, drop per-run cost, accept that inactive inference still
  only runs in the nightly job. Higher risk of rate-limiting and
  minute-budget pressure; only worth doing if shape A leaves
  meaningful new listings undetected.

Both shapes preserve the snapshot-on-change discipline (rule #2)
and the is_active-after-complete-walk rule (rule #3). Both reuse
the existing `listing_fetch_failures` queue so a probe that fails
to fetch a fresh listing doesn't drop it on the floor.

**Open questions (operator to decide before B1-equivalent work
starts)**

- **Channels.** Start with email only, or include SMS / push from
  the outset? Email-first is the assumption above.
- **Email provider.** SendGrid, Postmark, Mailgun, or AWS SES?
  Affects pricing model, env-var surface, and template tooling. No
  current dependency, so this is a fresh pick — same discipline as
  CLAUDE.md's "no new dependencies without justification" rule.
- **Cadence.** Is 5 minutes the firm target, or is 15-30 minutes
  acceptable? Lower cadence relaxes rate-limit and minute-budget
  pressure. Affects shape A vs. shape B above.
- **Per-user identity.** Today's model is one shared operator
  (`API_TOKEN` bearer, shared `anon` key). Multi-recipient
  notifications are the first real argument for opening per-user
  accounts — explicitly out of scope today. Default for this phase:
  stay single-operator, send all alerts to one configured address
  in env. Reopen identity work as a separate phase if a second
  recipient is needed.
- **Digest vs. per-listing.** One email per match (chatty, fast) or
  one digest per subscription per run (quieter, slight latency
  cost)? Affects `notification_dispatches` shape — current schema
  draft supports both.

**Out of scope for this phase**

- Per-user accounts / authentication (one shared operator stays the
  identity model; see open question above).
- SMS / push notifications (email-first).
- "AI-curated" alerts where the Phase 7 agent picks listings the
  operator might like — that's a later layer on top of this
  scaffolding.
- Re-notification on snapshot change (price drop, status change).
  Listed as a "next" follow-up once new-listing alerts ship.

## Building decomposition track (parallel)

The "paste a whole-building listing" workflow. Operator drops a
`rodinný dům` URL into the same paste field they use for apartments
today; the system reads description + floor-plan images, proposes
the apartment units inside the building (including potential ones
like an unconverted attic), the operator confirms / edits the unit
list and the per-unit condition, the agent fans out one rent + one
sale estimate per unit, results are grouped and summed at the
building level, and a spreadsheet-style business-case overlay
computes the development P&L (acquisition + reno + new build + soft
costs + VAT in/out + debt service → EBIT / EBT / MOIC / IRR /
yield-on-cost).

Reference business case: `model_Kralupska.xlsx` (operator-supplied,
2026-05-12). Six blocks: Assumptions, Floor Schedule, Unit
Schedule, Cost Stack (with VAT splits), Revenue & P&L, Returns.

### Phase B0: Schema + scaffolding (done)

Pure plumbing. No agent changes, no UI changes beyond type stubs.
Shipped in PR #59.
- Migration 035 (`035_building_runs.sql`): new `building_runs`
  parent table; `building_run_id` (FK) + `building_unit_id` (text)
  columns on `estimation_runs`. Status lifecycle: `pending` →
  `extracting` → `awaiting_input` → `estimating` → `success` |
  `failed`. The `awaiting_input` pause is the human-in-the-loop gate
  that distinguishes the building flow from today's single-shot
  estimation_runs flow. Per CLAUDE.md architectural rule #13.
- `api/building_runs.py` module: `create_building_run`,
  `get_building_run`, `list_building_runs`. Children are surfaced
  on the detail response via a side-query on `estimation_runs`.
- API endpoints: `POST /buildings` (minimal shell — `{source,
  input_url?}` → `status='pending'`), `GET /buildings`,
  `GET /buildings/{id}`. All bearer-gated.
- Pydantic schemas: `CreateBuildingIn`, `BuildingUnit` (the JSONB
  unit record schema, used by B1 onwards), `BuildingRunOut` shape
  documented via `_BUILDING_COLUMNS`.
- Frontend type stubs in `frontend/src/lib/types.ts` (`BuildingRun`,
  `BuildingUnit`, `BuildingStatus`). No new pages or components.
- Tests: hermetic CRUD tests in `tests/api/test_buildings.py`
  modeled on the `_State`-style fakes from `test_estimations.py`.

### Phase B1: URL ingest + unit extractor + confirmation UI (done)

Builds on B0's persistence. The output of B1 is a `building_runs`
row sitting in `status='awaiting_input'` (extractor ran,
`units_proposal` populated, ready for the operator's review) which
transitions to `estimating` on confirmation. Per-unit fan-out
lands in B2; B1 stops at the human-in-the-loop gate.

**Data + migration**

- Migration 036 (`036_building_unit_extractions.sql`):
  - New cache table `building_unit_extractions` keyed on
    `(sreality_id, snapshot_id)` — same shape as `listing_summaries`
    (migration 027): `extracted_at`, `model`, `units jsonb`,
    `building jsonb`, `confidence text`, `warnings jsonb`,
    `cost_usd numeric`. New snapshot auto-invalidates by virtue of
    the PK including `snapshot_id`. RLS enabled, no policies (read
    through API).
  - `'extract_building_units'` added to `llm_calls.called_for`
    CHECK constraint so the audit trail tags vision calls
    consistently.
  - Four `app_settings` seeds:
    `llm_building_extractor_system_prompt` (the canonical prompt
    body, mirrors the on-disk SKILL.md),
    `llm_building_extractor_model` (`claude-sonnet-4-5` by default,
    operator-tunable via `/settings`),
    `llm_building_extractor_max_images` (default `8` — enough to
    cover hero + floor plans + interior on a typical sreality `dum`
    listing without ballooning the token bill), and
    `building_default_estimator_skill` (default
    `rental_estimator_v1`, used by the B2 orchestrator — see the
    "apartment skill reuse" note on B2's orchestrator step).
  - Every prior value preserved via the existing
    `app_settings_history` trigger (migration 020).

**Toolkit function**

- `toolkit.building_extraction.extract_building_units(
  sreality_id, snapshot_id, max_images=8, force_refresh=False) ->
  envelope`. Write-allowed exception per CLAUDE.md toolkit rule #5
  (LLM is the source of truth; cache locally so the
  inevitable B1→B2 round-trip and any later re-extraction don't
  re-bill). Same envelope contract as every other toolkit function
  (`{data, metadata}`). The `data` payload is the structured unit
  proposal:
  ```python
  {
    "units": [
      {"id": "u1", "floor": 1, "area_m2": 72, "disposition": "3+kk",
       "condition": "good", "notes": "...", "is_potential": false},
      ...
    ],
    "building": {"floor_count": 4, "year_built": 1932,
                 "condition": "good", "total_area_m2": 320,
                 "construction_type": "brick"},
    "confidence": "high|medium|low",
    "warnings": [...],
  }
  ```
- Pulls description text from the latest snapshot's parsed fields
  (already on `listing_snapshots.raw_json`) and up to `max_images`
  images from R2 via boto3 `GetObject`, base64-encoded into the
  Claude vision payload — same pattern as `compare_listing_images`
  in `toolkit.image_similarity`.
- Calls log to `llm_calls` with
  `called_for='extract_building_units'`, the building_run_id (when
  invoked through the API), token / cost columns populated.
- Cohort floor: if the listing has no images in R2 (image-download
  phase hasn't caught up yet, or `R2_*` env vars missing), the
  function falls back to description-only and stamps
  `confidence='low'` + a warning. Never crashes the building flow.

**Skill — and why it is NOT the apartment estimator**

- New skill `building_unit_extractor_v1`:
  - On-disk seed: `skills/building_unit_extractor_v1/SKILL.md`
    (canonical content + frontmatter, mirroring
    `skills/rental_estimator_v1/SKILL.md`).
  - Migration 036 seed `INSERT` into `skills` table (same pattern
    as migration 029's `rental_estimator_v1` seed, migration 032's
    `rental_estimator_full_v1` seed). Operator edits live values
    via `/settings`; `skills_history` trigger preserves every
    prior version (per Phase 7 slice 1).
  - Allowed tools: `extract_building_units` (the toolkit wrapper
    above) + `record_building_units` (the terminator — same shape
    contract as `record_estimate`, validated server-side).
  - Preferred model: anthropic = `claude-sonnet-4-5`, gemini =
    `gemini-2.5-pro` (vision-capable on both providers).
  - Limits: `max_iterations: 4`, `max_cost_usd: 0.30`,
    `wall_clock_timeout_s: 90`. Lower than the estimator's caps
    because extraction is a one-shot vision call, not an iterative
    cohort search.
  - System prompt teaches the model to: (a) read the description
    text first to anchor on stated unit count + total area,
    (b) cross-check against floor plans, (c) emit one entry per
    discrete unit including potential ones (e.g. an unconverted
    attic worth flagging `is_potential=true`), (d) populate
    `condition` from the provided photos when the text is silent,
    (e) terminate with `record_building_units`.
  - **This is an extractor skill, not an estimator skill.** Per-unit
    rent / sale estimation in B2 reuses the existing
    `rental_estimator_v1` / `rental_estimator_full_v1` skill (see
    the apartment-skill-reuse note on B2's orchestrator step) so
    that an apartment estimated inside a building is computed
    exactly the same way as a standalone apartment estimation, and
    any improvement to the apartment estimator skill rolls into the
    building flow automatically.

**API endpoints**

- `POST /buildings/from_url` — operator-facing entry, replaces
  B0's minimal `POST /buildings` shell:
  1. Routes the input URL through
     `scraper.source_dispatcher.parse_listing_url` (reused as-is —
     same cache, same per-source parsers, same audit trail in
     `parsed_url_cache` and `llm_calls`).
  2. Validates the parse: `category_main` must be `'dum'` or
     `'komercni'`. A `byt` URL returns HTTP 400 with a hint to use
     `/estimations` instead — apartments don't decompose.
  3. Inserts `building_runs` row in `status='pending'` with all
     `input_*` + `source_*` + `subject_summary` columns populated
     from the parse output. (The `subject_summary.building` sub-
     object will be overwritten by the extractor's `building`
     field in step 5.)
  4. Transitions `status` to `'extracting'` and runs the
     extractor synchronously (v1; Phase 7 slice 2's async lifecycle
     will retrofit polling later). On extractor failure, transitions
     to `status='failed'` with `error_message` set; the row IS
     the audit trail, same discipline as estimation_runs.
  5. On success, writes the extractor output to `units_proposal`
     (append-only after this point) and to `subject_summary` (which
     keeps the operator-visible "what we know about the building"
     blob in one place), transitions to `status='awaiting_input'`,
     returns the row. Total latency ~10-30 s on a typical
     `dum` listing — within the 90s skill timeout.
- `POST /buildings/{id}/confirm_units` — the human-in-the-loop gate:
  1. Accepts the operator-edited unit list (the
     `record_building_units` envelope's `units` array). Rejects if
     `status != 'awaiting_input'` (HTTP 409 — building already in
     a later state).
  2. Validates each entry's shape via the existing `BuildingUnit`
     Pydantic schema from B0 (`id`, `floor`, `area_m2`,
     `disposition`, `condition`, `notes`, `is_potential`).
  3. Writes the confirmed list to `units` (mutable until estimation
     starts in B2, after which B2 freezes it).
  4. Transitions `status` to `'estimating'`. B2's orchestrator
     picks up from there. For B1's scope we stop here — a building
     in `estimating` with no child runs is a valid intermediate
     state.
- `POST /buildings/{id}/re_extract` — re-run the extractor against
  the current snapshot (forces cache miss via `force_refresh=True`).
  Only valid while the building is in `awaiting_input`; returns 409
  otherwise. Useful when a new snapshot lands between paste and
  confirmation and the operator wants the extractor to see it.
- B0's old minimal `POST /buildings` is removed — every operator-
  facing creation goes through `from_url` from B1 onward.

**Frontend**

- `NewEstimationModal` grows a `kind` toggle ("Apartment" /
  "Building"), defaulting to apartment so existing flows stay
  unchanged. Pasting a URL with `kind='building'` routes the
  request to `/buildings/from_url` instead of `/estimations`.
- Step 2 of the building flow renders a new `BuildingUnitEditor`
  component: a table of unit rows (floor / area / disposition /
  condition / notes / `is_potential` checkbox), add / remove
  buttons, plus a building summary header (year built, floor count,
  total m², construction type). Each editable field maps 1:1 to a
  `BuildingUnit` field. Submitting POSTs to
  `/buildings/{id}/confirm_units`.
- New `/building/:id` route — initial read-only view of a building
  row. For B1 it renders: subject summary block, current status
  badge (with a CTA for `awaiting_input` rows that opens the
  `BuildingUnitEditor` in confirm mode), units list (proposal or
  confirmed), warnings block, link back to the source URL. The
  full rollup view + per-unit estimate strips ship with B2.
- The Estimations list page (`/estimations`) is unchanged — building
  rows live on `/buildings` (a new list page) so the two
  conceptually-different things don't blend. `/buildings` is a slim
  table modeled on `/estimations` (source / status / created_at /
  unit count / link). The shared `EstimationsListPage` filter +
  pagination conventions apply.

**Tests**

- `tests/toolkit/test_building_extraction.py`: hermetic test that
  stubs the Claude vision call with a saved fixture response and
  exercises the cache hit / miss branches, plus the fallback path
  when R2 is unreachable.
- `tests/api/test_buildings_b1.py`: integration tests for
  `POST /buildings/from_url` (parse success, `byt` rejection,
  extractor failure, cache hit) and
  `POST /buildings/{id}/confirm_units` (happy path, status guard,
  schema validation). Modeled on the `_State` fakes from
  `tests/api/test_estimations.py`; no real LLM, no real DB.
- `tests/skills/test_building_unit_extractor_v1.py`: validates the
  SKILL.md frontmatter + migration seed are in sync (same pattern
  as the existing `rental_estimator_v1` test).
- Frontend: `BuildingUnitEditor.test.tsx` snapshot + interaction
  test for add / remove / edit / submit; `BuildingPage.test.tsx`
  for the `awaiting_input` CTA branch.

**Out of scope for B1 (deferred to B2 / later)**

- Per-unit rent / sale estimation fan-out — that's the B2
  orchestrator's job, which reuses the existing apartment
  estimator skill (see B2 below).
- Building rollup totals — same.
- The Excel-style business case tab — B3.
- Async / polling lifecycle — Phase 7 slice 2.
- Multi-portal (bezrealitky / idnes / remax) building paste — the
  source_dispatcher already routes those URLs, but per-source
  building parsers may need extra fields beyond what the apartment
  flow exercises; defer until a real bezrealitky `dum` URL surfaces
  in operator testing.

### Phase B2: Per-unit fan-out + building rollup view (done)

Takes the flow from B1's confirmation gate through to per-unit
estimates + a building-level rollup. No new migration: migration
035 already carried all six `total_rent/sale_p25/p50/p75_czk`
columns.

- **Orchestrator** in `api/building_runs.py`: `confirm_units`
  flips the row to `estimating` and hands off to
  `_run_building_estimations`, which fans out one rent + one sale
  `estimation_runs` child per confirmed unit, each linked back via
  `building_run_id` + `building_unit_id`. It is a fan-out +
  synchronous watcher, **not** a new LLM loop — each child runs
  through the existing `create_estimation_run` plumbing
  (`background_tasks=None`), so when the loop returns every child
  is terminal and the rollup is exact. Runs as a BackgroundTask
  from the endpoint (handler returns the `estimating` row; the
  detail page polls); runs inline when called without
  `background_tasks` (tests).
  - **Reuse of the apartment estimator skill.** Rent children run
    in **agent mode** under
    `app_settings.building_default_estimator_skill` (seeded by
    migration 036 to `rental_estimator_v1`, operator-tunable via
    `/settings`), so a unit inside a building is estimated exactly
    like a standalone apartment and any skill improvement rolls in
    for free. Children pass `category_main='byt'`,
    `category_type='pronajem'`/`'prodej'`, plus `area_m2` +
    `disposition` from the confirmed unit and `lat`/`lng` from the
    parent parse. **Sale children run in deterministic mode** until
    a sale-specific skill ships; the orchestrator already reads an
    optional `building_sale_estimator_skill` setting (absent today
    → deterministic), so wiring a sale skill later needs no code or
    migration change.
- **Rollup**: `_finalise_building` runs once every child is
  terminal and `_rollup_totals` sums the **successful** children
  into `total_rent/sale_p25/p50/p75_czk`. P50 is a straight sum;
  P25 / P75 sum the per-unit IQR endpoints. A percentile with no
  contributing unit stays NULL rather than reading as a misleading
  zero. The building lands `success` if any child succeeded, else
  `failed`. `sweep_stuck_buildings` now also recovers an orphaned
  `estimating` row (server restart mid-fan-out) to `failed`.
- **Frontend**: `/building/:id` grows a "Building totals" section
  (rent + sale `RangeStrip`s) and a per-unit card list — each unit
  shows its rent + sale estimate strip (reusing `RangeStrip`, the
  same strip `/estimation/:id` uses) and a "View estimate →" link
  to the child estimation. The read-only proposal table stays as
  the pre-fan-out / no-children fallback.
- **Out of scope (carried forward)**: sale-side estimator skill
  (sale children stay deterministic until `sale_estimator_v1`
  ships); the Excel-style business case overlay (B3).

### Phase B3: Business case tab

- Storage: `building_runs.business_case` JSONB (column exists from
  B0). Holds assumptions + floor schedule + unit-schedule overrides
  + computed outputs. JSONB grain because the spreadsheet is
  non-tabular and operator-tunable.
- Math engine: `api/business_case.py` — pure-Python port of the
  `model_Kralupska.xlsx` formulas (~30 lines of Excel logic).
  Stdlib only. Inputs from the column above + the unit list +
  the latest rollup totals; outputs EBIT / EBT / MOIC / IRR /
  yield-on-cost + the per-row breakdowns.
- API: `PUT /buildings/{id}/business_case` (idempotent save +
  recompute); the GET returns the persisted state.
- Frontend: an Excel-like grid as a new tab on the building page.
  Option A: hand-rolled `<table>` + per-cell `<input>`, save-on-blur
  to the PUT. Option B: an off-the-shelf grid (Handsontable
  Community / `react-spreadsheet`) — needs operator approval for the
  new dep. Default recommendation is A on the strength of "no new
  deps without justification"; revisit if the hand-rolled grid
  proves too rigid.

## Skill refinement track (parallel)

Closing the loop on the Phase 7 agent: today the operator can edit a
skill's system prompt via `/settings`, but there is no structured way
to learn from a specific estimation that went well or badly. This
track adds (a) deeper trace inspection so the operator can actually
see *why* the agent picked the comparables it picked, and (b) a
feedback-driven prompt refinement loop where the operator's written
critique of a specific run gets fed back into the skill that produced
it.

### Phase AI: Feedback-driven skill refinement (active)

Sliced into three independent PRs along the data-flow boundary:
slice A captures full tool-call payloads alongside the existing
bounded trace; slice B adds operator feedback capture; slice C
drives the actual refiner skill. Each slice is independently
useful.

#### Slice A: Trace inspection enrichment (done)

Migration 043 lands the side-table foundation; PR1 of three.

- Migration 043: `estimation_trace_payloads(estimation_run_id,
  step_n, full_output jsonb, captured_at)`, PK on the pair.
  ON DELETE CASCADE so payloads track the parent run. RLS enabled,
  no policies — service-role only; the frontend reads via the
  bearer-gated endpoint below. 30-day retention documented in
  CLAUDE.md (architectural rule #9 prose); no automated pruner,
  manual SQL when the table grows.
- `TraceRecorder.set_full_output(...)` + `iter_payloads()` +
  top-level `flush_trace_payloads(conn, run_id, recorder)`. The
  recorder accumulates `(step_n, full_output)` pairs in memory;
  flush executes a single `executemany` INSERT after the parent
  `estimation_runs` row is persisted. `ON CONFLICT DO NOTHING`
  makes retry double-flush a no-op.
- Wired into:
  - `estimate_yield` (deterministic path): captures the full
    `find_comparables` cohort and `analyze_distribution` result.
  - `agent.run_agent_estimation`: captures every tool-call result
    in the loop, plus the terminator input and unknown-tool
    diagnostics. Exception paths leave the payload unset by
    design (failed tool calls have nothing to drill into).
  - All three persist sites: `create_estimation_run` success path,
    `_persist_failed_run`, and `_run_agent_path` (both finalise
    branches) call `flush_trace_payloads` after the row exists.
- `GET /estimations/{id}/trace/{n}/payload` (bearer-gated) returns
  `{step_n, full_output, captured_at}`, 404 when absent.
- Frontend `Timeline.tsx`: `tool_call` step bodies render a
  "Show full payload" expander that lazily calls the new
  `useTracePayload(runId, stepN, enabled)` hook (added to
  `frontend/src/lib/queries.ts`). `EstimationDetail` threads the
  run id into `<Timeline runId={run.id} />`; previews and other
  callers without a persisted run continue to render without the
  expander.
- Hermetic unit tests on `set_full_output` / `iter_payloads`:
  computation/reasoning steps never produce payload rows;
  numbering on payload rows lines up with the trace step `n`.

Past-run drill-down is one-directional in time: the writer only
captures payloads for runs executed *after* slice A shipped.
Pre-existing `estimation_runs` rows lose the drill-down ability;
the trace summary stays intact.

#### Slice A.1: Audit follow-ups (done)

Operator-driven adjustments to the trace surface uncovered the
moment slice A landed on /estimation/17:

- Migration 048 — `estimation_runs.comparables_excluded jsonb` and
  a string-replace UPDATE on both rental skill prompts inserting a
  required `comparable_decisions` bullet on the `record_estimate`
  arguments list. Applied via MCP; `skills_history` preserves the
  prior prompts.
- `record_estimate` schema (api/agent.py) accepts
  `comparable_decisions: [{sreality_id, decision, reason}]`. The
  agent's terminator step now records `n_comparables_included` /
  `n_comparables_excluded` in its bounded summary; the full
  decisions list lives in the slice A side-table.
- `_finalise` joins inclusion reasons onto each `comparables_used`
  entry (new optional `reason` field) and emits a parallel
  `comparables_excluded` list — both persisted on the run row.
- Agent loop emits a `skill_choice` computation step before the
  first LLM turn recording skill name, description, provider,
  model, limits, and tool whitelist — answers "why was this skill
  used" in the audit.
- Agent summary line wording: `after N iters` → `after N LLM
  turns` for clarity about what the counter represents.
- Frontend: `ComparableUsed.reason` + new `ComparableExcluded`
  type, "Why kept" column on the comparables table, "Considered
  and set aside" panel below it, and Mode / Skill / Model rows in
  the Inputs recap (pulled from the trace's `skill_choice` step).
- Hermetic tests on `_normalise_decisions` (malformed entries
  dropped, not raised) and on the agent trace shape (skill_choice
  always first).

These were follow-ups, not new slices — same PR.

#### Slice B: Feedback capture (done)

Migration 049 + the API surface land the operator's free-text
feedback as a first-class object linked back to the run:

- Migration 049 (applied via MCP): `estimation_feedback(id,
  estimation_run_id, feedback_text, submitted_at, status,
  refinement_id)` with a CHECK enum on `status` covering the full
  lifecycle (`submitted | refining | proposed | applied |
  dismissed | failed`). FK on the run cascades; RLS enabled, no
  policies — service-role only.
- `api/feedback.py` insert/get/list/update-status helpers (mirrors
  the small storage modules elsewhere in `api/`).
- `POST /estimations/{id}/feedback` accepts
  `{feedback_text, kick_off_refinement=true}` and either stashes
  the row (`status='submitted'`) or fires slice C inline
  (`status='refining'` → terminal status set by the refiner).
  `GET /estimations/{id}/feedback` returns the run's history,
  newest first.
- Frontend `FeedbackBlock` on `/estimation/:id`: composer with
  textarea + "Run the refiner now" checkbox, history list with a
  per-row status badge (FeedbackStatus), and per-row "View
  proposed change" expander that lazy-loads the slice C
  refinement.

#### Slice C: Refinement loop (done)

Same-skill, suggest-then-confirm. Prompt-only edits (operator's
choices in the slice-B/C kickoff).

- Migration 050 (applied via MCP):
  - `skill_refinements(id, skill_name FK skills, original_prompt,
    proposed_prompt, refiner_explanation, source_feedback_id FK
    estimation_feedback, status, created_at, applied_at)` —
    proposal lifecycle is `proposed → applied | dismissed`.
  - FK from `estimation_feedback.refinement_id` →
    `skill_refinements(id)`, `ON DELETE SET NULL`.
  - `llm_calls.called_for` CHECK extended with `refine_skill`.
  - `app_settings.llm_skill_refiner_system_prompt` +
    `llm_skill_refiner_model` seeds (operator can edit live).
  - `skills.skill_refiner_v1` seed row with prompt-only tool
    whitelist (`["record_skill_refinement"]`) and tight limits
    (`max_iterations=2`, `max_cost_usd=0.40`,
    `wall_clock_timeout_s=60`).
- `skills/skill_refiner_v1/SKILL.md` canonical docs.
- `api/refiner.py` — single-pass LLM call, parses the run's
  `skill_choice` trace step to discover which skill produced it
  (deterministic / pre-slice-A.1 runs report a soft 'failed'
  status), assembles the refiner user message from the original
  prompt + feedback + compacted trace, calls
  `LLMClient.call(called_for='refine_skill')`, and persists the
  proposal. Helpers `apply_refinement` / `dismiss_refinement` flip
  both the refinement and its parent feedback row; applying goes
  through `skills.update_skill` so the existing `skills_history`
  trigger from migration 029 preserves the prior prompt.
- `GET /skill-refinements/{id}` and
  `POST /skill-refinements/{id}/decision` (apply | dismiss),
  bearer-gated.
- Frontend: `RefinementProposal` renders the refiner's explanation,
  a line-based prompt diff (green = added, red = removed), and
  Apply / Dismiss buttons when the proposal is still in `proposed`
  state. Diff is computed client-side from `original_prompt` and
  `proposed_prompt`.
- Hermetic tests (`tests/api/test_refiner.py`) cover the pure
  helpers: `_pick_skill_name_from_run`,
  `_build_refiner_user_message`, `_compact_steps`.

**Caveats:**

- Refiner can only act on agent-mode runs (deterministic runs
  have no skill to refine). The lifecycle handles this by setting
  the feedback's status to `failed` and never producing a
  refinement row.
- Past-run feedback works if the run has a `skill_choice` step in
  its trace (i.e. ran under slice A.1 or later). Older agent runs
  return `failed` with the same handling.
- `auto_apply_refinements` flag is intentionally NOT implemented —
  operator chose suggest-then-confirm; auto-apply can be a Phase
  AI follow-up if same-session iteration feels slow.

#### Slice C.1: Skill consolidation (done)

Operator follow-up: two active rental skills (`rental_estimator_v1`
and `rental_estimator_full_v1`) was confusing UX. Decision: keep
only the full skill active, treat the older one as history. The
refiner pipeline updates the full skill in place going forward —
`skills_history` is the per-skill audit trail, no new sibling
skill rows.

- Migration 051 (applied): `skills.archived_at timestamptz`. Same
  column on `skills_history` so snapshots preserve the archival
  state. `rental_estimator_v1.archived_at` set to now().
- Backend default: `CreateEstimationIn.skill` flipped from
  `"rental_estimator_v1"` to `"rental_estimator_full_v1"`. Existing
  estimations referencing v1 in their trace still load v1 (load_skill
  doesn't filter by archival); only new estimations and the default
  picker are gated.
- `list_skills(conn, include_archived=False)` is the new shape;
  `GET /admin/skills?include_archived=true` exposes archived rows.
  Frontend `Skill.archived_at` + a "Show archived skills" toggle on
  the Settings page. Archived cards render with a muted background
  and a small `archived` tag.

The "skill picker on the new-estimation modal" was scoped out —
the operator's mental model is now: one canonical rental skill,
refined in place via slice C, with history per-skill in
`skills_history`. Past runs that ran under v1 keep their trace's
`skill_choice` step pointing at v1; the row is still there for them
to load.

#### Slice D: Multi-pass estimation strategy + confidence revision (proposed)

Two related changes to the estimator skill's behaviour, separate
from the refiner loop but in the same family of "make the agent's
reasoning loop richer." Independent of slices A–C; can ship in
parallel.

**Multi-pass strategy**

Today's `rental_estimator_v1` runs a single pass: pick filters,
fetch comparables, compute the distribution, emit a point estimate.
The revised flow runs three explicit iterations:

1. **Reconnaissance.** Inspect the available sample for the
   candidate filter spec — how many comparables exist, how
   dispersed they are, whether obvious gaps (only top-floor flats,
   only renovated units, etc.) constrain inference. Output: one or
   more declared benchmarking strategies and the reasoning behind
   each. A "strategy" here is a concrete plan ("widen radius to
   1.5 km and trim 10/90", "find the two best-matched units and
   anchor on those", "split the cohort by floor band and average").
2. **Execution.** Run each declared strategy end to end (gather
   the cohort it implies, compute its estimate, capture its
   confidence inputs). Strategies that fall through — e.g.
   "find two near-identical units" returns zero — fail open and
   don't gate the run.
3. **Adjudication.** Compare the strategies' results and pick the
   one the agent judges most reliable. The chosen strategy's
   estimate is the run's primary result; the others are recorded
   as alternates in the trace so the operator can inspect what
   was considered and why it was rejected.

Iterate from step 1 until the chosen strategy reaches at least
medium confidence per the revised score below, bounded by the
skill's `max_iterations` and `max_cost_usd` so a stubborn run
can't spend unbounded LLM credit. Hitting the bound returns the
best-so-far estimate with the confidence label it actually
earned (no rounding up).

Trace shape: each iteration emits a `reasoning` step explaining
the chosen strategy followed by the `tool_call` steps it needs.
`TRACE_SCHEMA_VERSION` in `api/estimation_runs.py` bumps when
this lands. Alternate-strategy summaries (estimate, confidence,
reason for not picking) live in a new `alternate_strategies`
array on the trace summary, kept small per architectural rule #9
— full cohorts go to `estimation_trace_payloads` (Slice A) so
each step's `output_summary` stays a summary.

**Confidence revision**

Today's confidence label is dominated by sample size. The revised
calculation factors in:

- **Quality of fit.** Two near-identical comparables (same
  disposition, same micro-location, same floor band, same
  condition, similar age) can warrant higher confidence than
  fifty loose matches. Today this signal is implicit in the IQR;
  it becomes an explicit input — a per-comparable "match score"
  derived from facet overlap with the subject listing, with the
  cohort's mean/min match score feeding the confidence calc.
- **Sample size.** Still relevant — a single comparable is fragile
  no matter how well-matched. The new formula doesn't drop sample
  size, it stops letting sample size alone determine the label.
- **Cross-strategy agreement.** When two independent strategies
  (e.g. "narrow filter" vs. "broad filter trimmed to outliers")
  produce estimates within a configurable epsilon (default ~5 %),
  confidence rises; wide disagreement lowers it. Only emitted when
  at least two strategies survived adjudication.
- **Freshness.** Already captured in metadata, but factor it into
  the label so a cohort full of stale comparables can't masquerade
  as high-confidence.

Labels stay `low | medium | high`. The label-shape change is
coordinated across `api/schemas.py`, `toolkit/comparables.py`,
the agent skill's prompt, and the `/estimation/:id` UI — capture
as one migration when the score components persisted on
`estimation_runs` change shape. `estimation_runs.confidence_score`
gets a sibling `confidence_breakdown jsonb` so the operator can
see which signal drove the label.

**Skill / prompt impact**

- `rental_estimator_v1` system prompt is rewritten to teach the
  three-iteration flow and the new strategy vocabulary.
  `app_settings_history` (migration 020) preserves the prior
  prompt automatically when the operator writes through
  `PUT /admin/skills/{name}`. Bump the seed `INSERT` migration's
  comment to flag the format change; do **not** edit the original
  migration (architectural rule #1).
- Building decomposition (Phase B2) fans out per-unit through the
  same apartment estimator skill, so Slice D's iteration changes
  propagate automatically — no separate building-level rework. The
  building rollup view continues to aggregate the per-unit estimates
  as they are produced; no new logic at the building tier.

**Out of scope for this slice**

- Auto-tuning the strategy mix from past runs — that's Slice C's
  refiner once it has data to learn from.
- Operator-visible per-strategy A/B at the skill level — Phase 7
  slice 2 covers that for skills as a whole.
- New strategies beyond what the prompt enumerates. The skill
  picks from a written list; expanding the list is a prompt edit,
  not a code change.

---

#### Original phase brief (pre-slicing)

**Trace inspection enrichment**

The existing trace already records every tool call's parameters and
an `output_summary` per architectural rule #9 (capping row size at
single-digit kilobytes regardless of cohort size). What's missing is
the ability to drill from a tool call's row in the timeline into the
full payload it returned — concretely, the operator wants to see
"this `find_comparables_relaxed` call with these filters returned 42
listings; the 8 that ended up in `comparables_used` were picked
because of this reasoning step; here are the 34 that didn't make
the cut and why."

- Trace step rows in the UI already render `filters_used` from the
  tool's metadata envelope (per toolkit rule #2). What they don't
  render: the listings that came back from the call but weren't
  selected. Two viable shapes:
  - **Shape A — payload side-table.** New table
    `estimation_trace_payloads(estimation_run_id, step_n,
    full_output jsonb, captured_at)` written at trace-finalisation
    time. Architectural rule #9 stays intact (the trace JSONB on
    `estimation_runs` keeps only the summary); this is a separate,
    lazily-loaded record. UI fetches `/estimations/{id}/trace/{n}/payload`
    on click-to-expand. Recommended default.
  - **Shape B — on-demand re-execution.** No new storage; the UI
    re-runs the tool with the recorded params. Cheaper at write
    time but breaks the freshness contract — the listings table
    moves under the agent's feet, so the operator sees a different
    cohort than the run actually used. Bad for the audit story.
- The Timeline component (`frontend/src/components/Timeline.tsx`)
  already dispatches on `step.kind`; this is a new render mode on
  `tool_call` steps that exposes the expandable payload view +
  per-listing "did this make `comparables_used`? if not, why?"
  annotation.
- The "why" annotation per non-selected listing comes from the
  reasoning step that immediately follows the tool call (per the
  Phase 7 slice 1 trace shape — reasoning kind is emitted per LLM
  turn). The UI surfaces the relevant slice of that reasoning
  alongside the listings table.

**Feedback capture**

- Migration: `estimation_feedback(id, estimation_run_id, feedback_text,
  submitted_at, status, refinement_id)` — one row per operator
  feedback submission, linked back to the run. `status` lifecycle:
  `submitted` → `refining` → `proposed` | `applied` | `dismissed` |
  `failed`. Append-only (architectural rule #1 spirit even though
  this is operational data, not history).
- API: `POST /estimations/{id}/feedback` accepts
  `{feedback_text: str, kick_off_refinement: bool = true}` and
  inserts a row. Bearer-gated. Defaults to immediately kicking off
  the refinement loop so the operator gets a same-session proposal;
  setting the flag false stores the feedback without spending LLM
  credit.
- Frontend: a "Provide feedback" button sits alongside the existing
  "Re-run" button on `/estimation/:id`. Click opens a modal with a
  textarea + submit. Past feedback for a run renders inline (one
  block per submission, status badge, link to the proposed
  refinement when applicable).

**Refinement loop**

- New skill `skill_refiner_v1` (on-disk seed
  `skills/skill_refiner_v1/SKILL.md` + migration seed `INSERT`,
  same pattern as `rental_estimator_v1` per Phase 7 slice 1). Input
  context: the original skill (system prompt + allowed tools +
  preferred model + limits, sourced fresh from the `skills` row at
  refinement time), the full estimation trace including the new
  trace payloads, and the operator's feedback text. Output: a
  proposed updated `system_prompt` (and optionally an updated
  `allowed_tools` whitelist when the feedback says "stop using
  tool X" or "you should have used tool Y"), plus a one-paragraph
  explanation of what the refiner changed and why.
- Limits: `max_iterations: 2`, `max_cost_usd: 0.40`,
  `wall_clock_timeout_s: 60`. The refiner is a single reasoning
  pass over a fully-materialised context, not an iterative tool-
  use loop, so limits sit lower than the estimator's.
- Calls log to `llm_calls` with `called_for='refine_skill'` (new
  value on the CHECK constraint via the same migration).

**Apply vs. suggest — the safety-critical choice**

- **Default: suggest-then-confirm.** The refiner writes the proposed
  new prompt to a new staging table `skill_refinements(id, skill_id,
  original_prompt, proposed_prompt, proposed_allowed_tools,
  refiner_explanation, source_feedback_id, status, created_at,
  applied_at)`. Status: `proposed` → `applied` | `dismissed`. The
  operator reviews the diff on `/settings/skills/{name}/refinements`
  and clicks Apply (which writes through `PUT /admin/skills/{name}`,
  letting the existing `skills_history` trigger from migration 029
  preserve the prior value automatically) or Dismiss.
- **Optional: auto-apply.** Operator can flag a skill as
  `auto_apply_refinements: true` via `/settings`. Useful for early
  iteration when the operator wants tight loops; risky in the long
  run because LLM-written prompt edits will drift the skill's
  behaviour silently. Strongly recommend leaving this off in
  production.
- Either path goes through `skills_history` for full audit and
  rollback, same discipline as `app_settings_history` (migration
  020).

**Open questions (operator to decide before implementation starts)**

- **Payload retention.** How long do we keep `estimation_trace_payloads`
  rows? Forever bloats the table (a single run's payload can be
  hundreds of KB); 30 days mirrors `listing_freshness_checks` and
  is the recommended default. Old rows just remove the
  drill-down ability — the trace summary stays intact.
- **Refinement scope.** Does the refiner update the *same* skill the
  run used, or fork to a new `_v2`/`_vN` skill so the original stays
  pristine for A/B comparison? Forking is heavier but matches how
  the Phase 7 slice 2 A/B view assumes multiple skill variants.
- **Allowed-tools edits.** Should the refiner be allowed to change
  the tool whitelist, or only the system prompt? Prompt-only is
  simpler and harder to break things with; tool-whitelist edits
  unlock real behaviour change but need stricter validation
  (refusing to whitelist a tool that doesn't exist, etc.).
- **Feedback batching.** Apply each feedback submission individually
  (chatty, fast iteration, more LLM cost), or accumulate N
  submissions and refine once over the bundle (cheaper, slower
  iteration)? Default: per-submission, behind the
  `kick_off_refinement` flag so the operator can batch manually.
- **Default model for the refiner.** A capable model (Claude Opus,
  GPT-4o, Gemini Pro) is worth the cost here — it's writing prompts
  that drive every subsequent estimation. Lock to a specific model
  via `app_settings.llm_skill_refiner_model` so the operator can
  swap without redeploying.

**Out of scope for Phase AI**

- Automated regression testing of refined skills (re-running the
  refined skill against a fixture set of past estimations to check
  for behaviour drift) — that's a follow-up phase once the basic
  loop is in place.
- Multi-operator feedback aggregation — today's single-operator
  identity model applies (same as Phase U2.7).
- Cross-skill refinement ("learning from the rental skill should
  improve the sale skill") — out of scope; each skill is refined
  in isolation against its own runs.

## Summarize track (parallel)

LLM-derived natural-language summaries over the data the user is
already viewing. Distinct from the `browse-*` track (UI primitives
and navigation) and from the future `agent-*` track (multi-tool
reasoning). Powered by the Claude API via the FastAPI service —
the browser never holds an Anthropic key.

### summarize-1: Annotated distribution charts (done)
- One- to two-sentence natural-language annotation per per-disposition
  Kč/m² box plot in Browse > Stats (the box plots browse-2 shipped
  under the `DispositionBoxPlots` component). Generated server-side by
  `toolkit.region_annotations.summarize_region_dispositions` from the
  same `ppm2_box` payload that drives the chart, with the cohort-wide
  percentiles + all per-disposition box stats as cross-disposition
  context. Annotations are facts about the distribution — never a price
  recommendation (toolkit rule #1).
- Cached per `(region, calendar day)` in `region_disposition_annotations`
  (migration 104) so repeat browser sessions don't re-bill: the first
  viewer of a region today pays for the Claude call, everyone else hits
  the cache; the next day's first view regenerates. `region_key` is the
  SPA's deterministic serialization of the active filter set.
- Wired the same LLM-tool way as `summarize_listing`: operator-tunable
  system prompt + model in `app_settings`
  (`llm_region_annotation_system_prompt` / `_model`, default
  `claude-sonnet-4-5`), audited in `llm_calls` under
  `called_for='summarize_region_dispositions'`, exposed via the
  bearer-gated `POST /tools/summarize_region_dispositions`. The SPA
  renders the annotations under the box plots; the browser never holds
  an Anthropic key.
- Was the track entry-point for Phase 6's `summarize_listing` and
  `compare_listing_images` — all three now sit in the same family.

## Out of scope until explicitly opened
- ClickUp integration.
- MCP server wrapping the toolkit (for ad-hoc chat with the data).
- Public read API beyond the bearer-token gate.
- Per-user identity / accounts in the UI (the `anon` key is shared and
  read-only; the FastAPI token is shared and gated).

## Data preconditions
- Velocity tools (Phase 3b) work today (1 snapshot per listing is enough
  for TOM math).
- Outlier history-pattern detection (Phase 3a) becomes more useful as
  snapshot density grows past ~1.5/listing average.
- Cluster detection (Phase 5) needs neighborhoods with 30+ comparables
  to be meaningful; sparse rural areas will return single-cluster
  results.
