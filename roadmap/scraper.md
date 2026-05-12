# Scraper track

Scraper-specific evolution beyond Phase 1's nightly index walk.
Independent of the analytical, UI, and map tracks.

## Done

### Phase 1.5: Six-category coverage
Cross-listed under the analytical track. Headline: all six byt / dum
/ komercni × pronajem / prodej pairs walked nightly with per-category
refetch cap.

## Next

### Phase 1.5b: Multi-category UI defaults (follow-up)
The data is now broad, but the analytical and estimation surfaces
still hardcode `category_main='byt'` / `category_type='pronajem'` as
defaults. Work to do:
- `toolkit/comparables.py` — promote `category_main` /
  `category_type` from defaulted parameters to required-with-clear-
  error or first-class request fields (lines 57-58 today).
- `api/schemas.py` — same on `FindComparablesIn`,
  `DescribeNeighborhoodIn`, `ComputeMarketVelocityIn`,
  `CreateEstimationIn`, `EstimateYieldIn`.
- `frontend/src/components/EstimateForm.tsx` — replace "Apartment"
  hard-coded labelling with a category/type selector.
- `frontend/src/components/UrlScrapeStep.tsx` — placeholder URL +
  copy reflect the chosen category.
Small, high leverage; unblocks end-to-end house and commercial
estimations using data that already exists in the database.

### Phase 2: Multi-portal ingestion (later, larger)
Today's non-sreality flow is *parse on demand* via
`source_dispatcher` (LLM call per URL, cached 7 days). To make
bezrealitky / idnes / remax comparables show up in
`find_comparables`, those portals need to land in the `listings`
table itself. Scope:
- Per-source index walker analogous to `scraper/sreality_client.py`.
  Most of these portals don't expose a public JSON API, so HTML
  pagination / playwright will be in scope; bot-detection is more
  aggressive than sreality.
- Reuse `parse_listing_url` for detail pages, with aggressive
  caching and a per-source rate limit.
- New `listings` columns: `source` (default `'sreality'`),
  `source_url`, `source_id_native`. New numbered migration.
- Update `_shared_filter_where` so toolkit queries can filter by
  source.
- Frontend Browse: source multi-toggle.
- Open question: trust LLM-parsed data in the deterministic
  comparable pool, or keep portals as a separate cohort visible
  behind a `source != 'sreality'` badge until visual + heuristic
  validation matures? Default recommendation is the latter; agent
  (Phase 7) opts cross-portal cohorts in once it can validate
  them.
