# Per-m² measure unification — program charter

**North star: one measure, one definition, one label.**
Every per-m² figure the platform computes or renders — in SQL, in Python, in the SPA, in
the Chrome extension — resolves from a single named measure carrying its own numerator,
denominator, unit and validity bounds. No consumer re-derives the formula; no surface
renders the number without its basis label.

**Scope tests.** A change is in scope if it passes at least one: T1 collapses a duplicate
definition · T2 makes a consumer basis-aware · T3 makes the measure trustworthy at the
source. Passing none = scope creep (§5 records what that excluded and why).

**Live evidence** measured against production 2026-08-24 via `properties_map_mv`:
mmreality house `area_m2` median 905 m² vs 145–161 elsewhere, Kč/m² 5 699 vs ~46 000, on
3 311 rows. ~3 000 active properties priced under 1 000 Kč, ~75 of them first seen in the
trailing 7 days — the unit-price masquerade is ONGOING, so a backfill without a rail
refills. Land Kč/m² spreads 387→6 377 across portals. Land `estate_area` coverage is
~100% on sreality/idnes/bezrealitky/maxima but 0.1% / 1.2% / 3.1% on realitymix /
mmreality / remax — the measurement that decided the fork in §2.2.

**STATUS: COMPLETE — W1–W9 all shipped (2026-08-25).** The rail is live: required-argument
signatures, the offline CI census (`tests/test_measure_registry_census.py` +
`toolkit.measures.REGISTERED_SITES`) and `FilterDef.basis`. **Three items remain OPEN and are
the operator's**, tracked in [roadmap/measure-track.md](../../roadmap/measure-track.md): the two
W2 backfill WRITE passes (mmreality dum/prodej still reads a median 5 701 Kč/m² on a 905.0 m²
median area, live 2026-08-25), the `area_basis` stamp's coverage (24.1% of listings, and **zero**
rows carry the `plot` token — gate on `category_main` as well, never on `area_basis` alone), and
`drop function public.browse_stats(…)` (approval + pg_dump).

**Branch:** `feature/ppm2-measure-registry` (verified current with `origin/main`, zero commits ahead).
**Next free migration number: 423** — verified: local and `origin/main` both top out at `422_pipeline_checks_public_loose_index_scan.sql`. Every "migration 405" and "migration 169" in the upstream lens material is wrong and would collide with merged files. Numbers below are claimed **in merge order**; a wave that merges out of order takes the then-next free number and never edits a merged file.

---

## 1. THE DEDUPLICATED CONSUMER INVENTORY — **64 sites**

Same file+expression reported by two lenses = one row. Expressions counted individually would be ~90; **64 is the number of code locations that must change**, and it is the completeness contract for "all surfaces".

### §1 — SQL formula definitions (9)
| # | Site | Today |
|---|---|---|
| 1 | `migrations/420:106` `listings_public.price_per_m2` | `price_czk::numeric / area_m2::numeric` — **UNROUNDED**, listing grain |
| 2 | `migrations/398:216` `properties_public.price_per_m2` | `round(p.current_price_czk::numeric / p.area_m2, 2)` — property grain |
| 3 | `migrations/375:291` `browse_projection.price_per_m2` | `round(current_price_czk::numeric / area_m2, 2)` → feeds `browse_list` **and** `properties_map_mv` via `rebuild_*` (376) |
| 4 | `migrations/370:154` `listing_feed_public.price_per_m2` | `l.price_czk::numeric / l.area_m2::numeric` — **UNROUNDED**, listing grain |
| 5 | `migrations/378:127-128` `browse_stats_properties` filter | recomputes inline over `browse_list`, ignoring the stored rounded column in the same row (×2) |
| 6 | `migrations/378:224` `ppm2_pct` | inline (×3) |
| 7 | `migrations/378:225` `disposition_dist` | inline (×7) |
| 8 | `migrations/103:60-85` `region_stats` ppm2 CTE | inline (×6). **Signature has no `category_main`/`category_type` at all** — sale flats, monthly rentals, houses and land pool into one Kč/m² distribution unconditionally. Worst live basis failure in the schema. |
| 9 | `migrations/083` `browse_stats` | orphan; zero callers in `api/`, `toolkit/`, `frontend/src/`, `scripts/`; no later DROP |

### §2 — Python-emitted SQL that bypasses every view (5) — **all 9 expressions grep-verified**
| # | Site |
|---|---|
| 10 | `toolkit/comparables.py:427,432` — the rule-16 shared matcher's bounds |
| 11 | `toolkit/comparables.py:627` — projection; source of `estimation_cohort_entries.price_per_m2` |
| 12 | `toolkit/transit_axis.py:313` |
| 13 | `toolkit/neighborhoods.py:103,106,109` — median/p25/p75, a **fourth** distinct validity bound set, fed straight into an LLM prompt |
| 14 | `api/notifications.py:330,335` — the watchdog matcher. Rule 16 says Watchdog and Browse share one definition of "matches"; today they share it only because two people typed the same string, over different grains and different rounding |

### §3 — Client-side re-derivations of the formula (6)
| # | Site |
|---|---|
| 15 | `frontend/src/lib/format.ts:96-102` `fmtPricePerM2(price, area)` — **the** SPA definition, 6 callers |
| 16 | `frontend/src/components/region/DispositionBoxPlots.tsx:30` `fmtPpm2` (private NBSP + Intl) |
| 17 | `frontend/src/components/estimation/MfReferenceCard.tsx:39` `perM2` (monthly rent, bare `Kč/m²`, ASCII space) |
| 18 | `frontend/src/components/ListingMap.tsx:315` inline `Kč/m²` (monthly MF reference rent) |
| 19 | `frontend/src/pages/Datasets.tsx:74-75` `fmtPerM2` — emits **no unit at all** |
| 20 | `chrome-extension/src/content.ts:1108` `mf_reference_rent_czk / area` — **property-grain numerator ÷ listing-grain denominator** (`api/portal_lookup.py:82` vs `:76`); wrong for any merged group even when the area basis is right |

### §4 — Render sites that display the measure (12)
| # | Site |
|---|---|
| 21 | `ListingTable.tsx:280` value + `:44` header + `:261,266` Area/Lot |
| 22 | `ListingCards.tsx:658` value + `:873-874` sort presets + `:510-511` local `isRent` |
| 23 | `listing-detail/ListingOverview.tsx:114` (absolute price gets `/ price_unit`; the Kč/m² one line up gets nothing) |
| 24 | `estimation/ComparableModal.tsx:105` |
| 25 | `OriginPropertyPanel.tsx:44` |
| 26 | `estimation/RunPanel.tsx:1252` |
| 27 | `estimation/RunPanel.tsx:627` fond suffix `Kč/m²` — a **monthly** service charge under the capital label |
| 28 | `BrowseStats.tsx:47-51` + `:62-76` (`unit="Kč / m²"`, basis-blind) |
| 29 | `DispositionBoxPlots.tsx:133` aria, `:162-167` bare tick numbers, `:176` axis literal |
| 30 | `ListingMap.tsx:1738` legend `Nájemné (Kč/m²)` |
| 31 | `chrome-extension/src/content.ts:1118`, `:1245`, `:1149` |
| 32 | `Datasets.tsx:114,118` — **correct precedent** (`Sale Kč/m²` / `Rent Kč/m²/mo`); source from the shared map so it cannot drift |

### §5 — Column-list / contract sites that must carry the measure + basis (9)
| # | Site |
|---|---|
| 33 | `queries.ts` `TABLE_COLS` — verified: no `category_main`, no `category_type`, no `price_per_m2`. The one render lane with **no** basis available today |
| 34 | `queries.ts` `CARD_COLS` — has both category cols; needs `price_per_m2` |
| 35 | `queries.ts` `MAP_COLS` — needs `category_main,category_type` |
| 36 | `queries.ts` `DETAIL_COLS` — has both category cols; needs `price_per_m2` |
| 37 | `api/portal_lookup.py:62` `_LISTING_COLS` + `_MARKET_SQL` (constant is `_LISTING_COLS`, **not** `_LOOKUP_COLUMNS`; both category cols already present) |
| 38 | `migrations/417:19` `pipeline_board_public` — omits `price_per_m2` **and** `category_type` |
| 39 | `toolkit/filter_registry.py:1178,1197` + `frontend/src/lib/filterRegistry.generated.ts:1702,1732` |
| 40 | `api/agent.py:161,180` tool-schema enums (no description, no unit) |
| 41 | `api/schemas.py:256-282` `Ppm2BoxIn`/`SummarizeRegionDispositionsIn` + `frontend/src/lib/api.ts:377-386` |

### §6 — Stored per-m² values with no basis (6)
| # | Site |
|---|---|
| 42 | `migrations/053:54` `estimation_cohort_entries.price_per_m2 double precision` |
| 43 | `migrations/144:89` `price_stat_observations.price` — a **rate** stored under an absolute-price name, anon-exposed |
| 44 | `migrations/145:27` `price_stat_city_metrics.sale_latest_price` / `rent_latest_price` — units live only in file-header prose |
| 45 | `migrations/132:46,61` `rent_map_values.ref_rent_per_m2`, `ref_rent_novostavba_per_m2`, `rent_map_adjustments.czk_per_m2` + producer `api/rent_map.py:112-128` — the only per-m² columns whose **name** states the unit; period is still implicit |
| 46 | `fond_per_m2_czk` — `api/schemas.py:418`, `api/estimation_runs.py:1145-1167`, `api/main.py:835`, `RunPanel.tsx:422`, `content.ts:36` (default `10` duplicated across two territories) |
| 47 | `listings.price_unit` — the **numerator's** basis carrier, written by all nine portals in four spellings for two concepts (`měsíc`/`za mesic`, `celkem`/`za nemovitost`), rendered raw at `ListingOverview.tsx:115` |

### §7 — "Gross yield": four live definitions of one ratio (4)
| # | Site |
|---|---|
| 48 | `migrations/257:88` `recompute_property_mf` (property grain) + `:197` `recompute_mf_gross_yields` (listing grain) |
| 49 | `migrations/415:108` `price_stat_growth` — **unrounded** |
| 50 | `scraper/price_stats_metrics.py:73-81` — **rounded 2dp**, writes `price_stat_city_metrics.gross_yield_pct`. Same municipality, two answers |
| 51 | `migrations/149:43` `price_stat_series` — header states in writing that "the frontend derives … 12·rent/sale gross yield". A direct north-star violation |

### §8 — Input quality (T3) (6)
| # | Site |
|---|---|
| 52 | `scraper/mmreality_parser.py:447` `totalArea or usableArea` — **verified verbatim**; `:474` already sets `estate_area` from `landArea/plotArea` |
| 53 | `scraper/db.py:226,229` `MIN_PRICE_CZK = 2` — **verified**; catches "1 Kč dohodou", sails past 136/176/379 Kč unit prices |
| 54 | `ceskereality:176-192`, `realitymix:190-204`, `bazos:188-198`, `maxima:217-233` `_parse_price` — no per-area guard |
| 55 | `idnes_parser.py:152-158,269` `_PRICE_PER_M2_RE` + `remax_parser.py:326-341` `_PER_AREA_MARKERS` — **two divergent guards**, not one un-generalised guard |
| 56 | `scripts/recompute_property_stats.py:183` / `:367` / `:370` — **verified**: `area_m2 = g.area_m2` (golden record, `source_trust_rank`) while `current_price_czk = r.price_czk` and `category_main/type = r.*` (representative child) |
| 57 | `scraper/source_parsers/common.py:69-78` — LLM parse schema; already declares "užitná preferred over celková" (the canonical basis), and locks `price_unit` to a two-member vocabulary with no per-m² member |

### §9 — LLM-prompt surfaces (3)
| # | Site |
|---|---|
| 58 | `migrations/104:96-97` — a live `app_settings` prompt instructing the model **"Do not assume which — describe the numbers as given."** The north-star failure written into production data as a design rule |
| 59 | `toolkit/region_annotations.py:235,239` — hardcodes `(Kč/m²)` into the prompt |
| 60 | `skills/rental_estimator_full_v1/SKILL.md:89,203,243` + `skills/rental_estimator_v1/SKILL.md:60` (git copy **and** the live `skills` row) |

### §10 — Analytical consumers that must carry the label (4)
| # | Site |
|---|---|
| 61 | `toolkit/outliers.py:24-32` — default field `price_per_m2` |
| 62 | `toolkit/distribution.py:14-19` — default field `price_per_m2` |
| 63 | `toolkit/clustering.py:18,34` — default axis `price_per_m2` |
| 64 | `api/estimate_yield.py:76`, `:210-222` `_scale`, `:229-234` `_gross_yield` |

**TOTAL: 64 sites.** Waves 1–9 below touch all 64 and nothing else; the rail in W8 makes a 65th impossible.

---

## 2. PR #397 — VERDICT AND THE ARCHITECTURAL FORK

### 2.1 PR #397: **SUPERSEDE AND CLOSE. Salvage two files by copy; drop one commit entirely.**

Branch `feature/area-m2-basis`, tip `44e38e68`, open since 2026-06-05, 7 merge conflicts against `origin/main`.

**Cherry-pick by copy (`git show 44e38e68:<path>`), never rebase:**
- `scraper/area.py` — the `usable → floor → total → fallback` precedence. It is right, and it is independently corroborated by `scraper/source_parsers/common.py:70`, which already tells the LLM parser "užitná plocha preferred over celková". The two ingest paths that both write `listings.area_m2` disagree **in writing**; mmreality is the side that is wrong.
- `tests/scraper/test_area.py` — 6 of its 7 cases verbatim; invert the seventh.

**Discard:**
- `migrations/169_area_basis.sql` — number long taken; and its step 2 infers basis from `usable_area`, itself polluted on four portals, which would mislabel the ~3,300 mmreality rows `'usable'`. Rewrite from scratch.
- Its land rule (`pozemek → (None, None)`). See the fork.
- **Commit `f247ea68` wholesale** — dropping `min/max_usable_area` from the registry, API, watchdog and SPA. It deletes a *different* measure. It silently widens every stored watchdog spec (`api/notifications.py:183` documents Pydantic `extra='ignore'` as the mechanism) and every saved filter preset; it leaves permanently-NULL dangling arguments in the live `browse_stats_properties` signature (verified: `usable_area_min_filter`/`usable_area_max_filter` survive into `375:446` and `378:48`); and it breaks `Timeline.tsx`'s display of archived runs, a rule-12 immutable-originals violation. Its own commit message ("No behaviour change — `area_m2` == the usable area for apartments") is contradicted by the FilterDef it deletes, which says "Distinct from `area_m2` … Often smaller."
- Its ROADMAP block recording unshipped work as Done.

**Also missing from #397 and therefore from any revive:** `ceskereality_parser.py` and `realitymix_parser.py` (both added after the branch), and `realitymix` inverts the precedence (total-first).

### 2.2 THE FORK: **OPTION A. Decided. This gates every wave.**

> **`area_m2` stays polymorphic. The MEASURE resolves its basis from `(category_main, category_type)` at read time, through one named function. We additionally ship `listings.area_basis` as a PROVENANCE STAMP — an observation of which physical area the column already holds, never a constraint that changes its value.**

**Why Option A satisfies the north star and Option B does not.** The north star requires that the *measure* carry its numerator, denominator, unit and bounds. It does not require the *column* to be one physical thing. A named measure `land_capital_czk_m2` whose denominator is documented as "plot area" is fully compliant. Option B relocates land's denominator; it does **not** by itself name a single measure, because "usable vs floor vs total" remains a real distinction *within* buildings (realitymix is total-first; idnes collapses three labels into one string before parsing). Option B therefore still needs a basis label — it just pays a large data-loss price on the way to needing it.

**What Option B costs — every consumer that loses a value when land `area_m2` goes NULL:**

1. **`bazos` land loses its only area number.** Verified: `grep -c estate_area scraper/bazos_parser.py` = **0**, while `bazos_parser.py:47-49` maps `pozemky|pozemek|zahrada` to `category_main='pozemek'`. Option B's docstring says "the plot is read from `estate_area` instead". There is no `estate_area`. This is deletion, not relocation.
2. **`bezrealitky` land is unverified.** No bezrealitky fixture exists in `tests/fixtures/portal_html/`; the `location_w2` set is PII-scrubbed and area-free. #397 asserts `surfaceLand` carries the plot in an inline comment with no evidence. If it is `surface`, bazos's failure repeats.
3. **~24.8k spurious snapshots.** `area_m2` is in `_HASH_FIELDS`. NULLing land area appends a `listing_snapshots` row per land listing for a non-event — rule-2 churn with zero information content.
4. **Land Kč/m² empties on every surface at once**: rows 1–8 of the inventory all return NULL for land; the Browse "Price/m²" sort silently drops every land row; `region_stats` loses land entirely.
5. **`api/estimate_yield.py:76` degrades silently.** `field = "price_per_m2" if target.area_m2 is not None else "price_czk"` — every land yield estimation switches from per-m² comparison to raw absolute-price comparison, with no flag, no trace entry, no operator signal. A silent basis switch is strictly worse than a visible gap.
6. **`tests/scraper/test_idnes_parser.py:371-382`** — the one test in the repo pinning the polymorphic contract (`area_m2 == estate_area == 1074.0` for a pozemek) goes red and must be rewritten, deleting the only evidence we have that land area is captured at all.
7. **`properties.area_m2`** golden record and `pipeline_board_public` inherit the NULL through `recompute_property_stats`.

**Is that cost payable? No.** It destroys a denominator that live data says works (96–100% `estate_area` agreement on sreality/idnes/ceskereality/bezrealitky), on four portals, to buy a naming property the measure function delivers for free. And the argument that "the area slider is dwelling-shaped so land doesn't belong in `area_m2`" is void: `min_area_m2`'s constraint is `max=300` — the slider cannot express a land filter under *either* option.

**Concretely, Option A means:**
- `public.measure_price_per_m2_basis(category_main, category_type)` → `'sale_capital_czk_m2'` | `'rent_monthly_czk_m2'` | `'land_capital_czk_m2'` | `NULL`.
- `listings.area_basis text CHECK IN ('usable','floor','total','plot','unknown')` — set by the shared `derive_headline_area`, land yields `'plot'` with the value intact, bazos yields `'unknown'`. Additive; **not** in `_HASH_FIELDS` (precedent: `scraper/scraped_listing.py`'s header establishes exactly this for street/house_number/zip/published_at).
- The basis is **never** derived from `listings.price_unit` — `price_unit` is a four-spelling duplicate of `category_type`, not a per-area unit.

---

## 3. WAVES

Every wave = one PR = one purpose. Migration numbers are claimed at merge time in this order.

### W1 — Truth at the source: headline area + per-basis price floor
**Scope test: T3.** No migration.
**Files:** `scraper/mmreality_parser.py:447` (`usableArea or totalArea`; leave `:474` alone — it already writes the real plot); **new** `scraper/area.py` (from #397, land branch inverted to `(value, 'plot')`); route all nine parsers through it — `parser.py:215`, `bezrealitky:182`, `idnes:576-580` (feed usable/floor/total as **separate** params; the pre-parse collapse is what destroys the basis), `mmreality:447`, `remax:679`, `ceskereality:536-538`, `realitymix:571-579`, `maxima:417-421`, `bazos:679`; **new** `scraper/price_text.py` collapsing `idnes_parser.py:152-158` (anchored-after-amount regex) **and** `remax_parser.py:326-341` (substring markers) into one `is_per_area_price()`, then insert it in ceskereality/realitymix/bazos/maxima `_parse_price`; `scraper/db.py` — a **sibling** `plausible_price_czk(price, *, category_type)` next to `sane_price_czk` (do not fold it in: the existing function's contract is pure column-range clamping and it carries an import-time `assert set(_NUMERIC_ABS_MAX) == {numeric LISTING_COLUMNS}`), floors: sale non-land ≥ 100 000, rent ≥ 1 000, land none, area ≥ 5 m²; `scraper/scraped_listing.py` (+`area_basis` to `_LISTING_FIELDS`, **not** `_HASH_FIELDS`); `scraper/db.py` `LISTING_COLUMNS`/`_LISTING_COLUMN_PGTYPE`; `scraper/source_parsers/common.py:69-78` (schema prose only — the live copy is edited through the Settings UI, never re-seeded by migration).
**Migration 423:** `alter table listings add column if not exists area_basis text;` + CHECK + `alter table properties add column if not exists area_basis text;` + `comment on column listings.price_unit` (four legacy spellings, a duplicate of `category_type`, **never** use it to decide a per-m² basis).
**Proves it correct:** new `tests/scraper/test_price_unit_masquerade.py`, parametrized across nine parsers, seeded from `test_idnes_parser.py:403-412` (all six positives incl. the spaced `18 500 Kč / m²`, **and both negatives**: `14 160 Kč/měsíc` → 14160, `4 990 000 Kč (4 008 Kč/m²)` → 4990000) plus `test_remax_parser.py:350-355` (`7 759 CZK/ za m2` → None); new mmreality **house** fixture `totalArea='905', usableArea='130'` asserting `area_m2 == 130.0` **and** `estate_area` untouched (today's fixture sets both keys to `"54"`, so it passes under either precedence); `test_idnes_parser.py:371-382` stays **green unchanged** — that is the Option-A acceptance test; salvaged `tests/scraper/test_area.py` with the land case inverted plus a bazos-shaped land case (fallback only → `(value,'plot')`, proving nothing is deleted); existing `test_detail_queue.py` `sane_price_czk` cases stay green via the no-category default.
**Unblocks:** everything. The measure is arithmetic on garbage until this lands.

### W2 — Heal the stored damage
**Scope test: T3.** No migration. **Runs after W1 merges + one drain cycle.**
**Files:** **new** `scripts/backfill_mmreality_areas.py` and `scripts/backfill_unit_price_masquerade.py`, both cloned from `scripts/backfill_idnes_areas.py` (the working precedent: re-parses staged state, writes **no** snapshot — "correcting our own mis-parse of the SAME staged state is a data-quality fix" — idempotent via a `raw_json.area_reparse_v2` marker, resumable). mmreality can read `raw_json` directly (`dict(obj)` at `:435`), so it needs no `portal_raw_pages` read. Then `select recompute_property_stats(...)` over affected `property_id`s.
**Proves it correct:** `tests/scripts/test_backfill_mmreality_areas.py` at `derive()` level (shape: `tests/scripts/test_backfill_portal_streets.py`); mandatory `--dry-run` count printed and recorded in the PR body before the write run.
**Parallel with W3.** Strictly after W1.

### W3 — One property-grain measure: coherent numerator and denominator
**Scope test: T1+T3.** **This is the deepest defect and it must precede W4.**
**Files:** `scripts/recompute_property_stats.py:183-191` (roll `area_m2` and `usable_area` from the **same** child, not independently) and `:362-372` (`area_m2 = coalesce(r.area_m2, g.area_m2)` — the denominator comes from the representative child whose price is already the numerator; golden record only as fallback).
**Migration 424:** `alter table properties add column if not exists price_per_m2_source_listing_id bigint;` + `comment on function public.source_trust_rank` recording that it governs the per-m² **denominator** (mmreality sits at rank 4, above five portals — that is how a plot area escapes the listing grain into merged properties). **Do not reorder the ranks** to work around a parser bug: that is a per-portal branch in shared code by another name and it silently changes survivorship for ~30 other fields.
**Proves it correct:** a unit test over the rollup CTE with a two-child fixture (mmreality area, sreality price) asserting the pair comes from one row and `price_per_m2_source_listing_id` is stamped; a live verification `SELECT count(*)` of properties where area-provenance ≠ price-provenance, which must be 0 after the sweep.
**Unblocks W4.** Without it, unifying the formula across five views unifies the spelling and not the measure — `recompute_property_stats.py`'s own comment at `:308` makes exactly this argument for the price *delta* and then violates it for the ratio.

### W4 — THE KEYSTONE: the named measure in SQL
**Scope test: T1+T2.** **Migration 425.** Strictly after W3.

Contents, in order:
1. `create function public.measure_price_per_m2(p_price numeric, p_area numeric, p_category_main text, p_category_type text) returns numeric language sql immutable parallel safe` — **single-expression body, NO `SET search_path`** (a `SET` clause blocks planner inlining and would turn every predicate into a full scan). Returns `round(p_price / p_area, 2)`; NULL when area ≤ 0/NULL, price NULL, or price below the per-basis floor. Grant to `anon, authenticated, service_role`. Copy the declaration style of `source_trust_rank` (`311:26-45`) — the repo's existing proof that this shape inlines and is safe to expose to `anon`.
2. `create function public.measure_price_per_m2_basis(text, text) returns text` — the four-token vocabulary; the rent token is spelled `rent_monthly_czk_m2`, identical to what `rent_map_values` will be commented with, so the two systems share one word.
3. **Re-emit VERBATIM, changing only the ppm2 line and appending `price_per_m2_basis`:** `listings_public` (from 420), `properties_public` (from 398 — note the first argument here is `p.current_price_czk`), `browse_projection` (from 375), `listing_feed_public` (from 370, keeping every `l.` qualification 370 added).
4. `browse_stats_properties` — re-emit **378's** body (not 374's, not 375's): add `l.price_per_m2, l.category_main, l.category_type` to the `filtered` select at `:57`; replace `:127-128` with `l.price_per_m2 >= / <= …`; replace all ten inline divisions at `:224-225` with plain `price_per_m2`; emit `'ppm2_basis'` — the cohort's single basis, or the literal `'mixed'`. Mixed is reachable in one click: `category_type_filter` is nullable by rule 22.
5. `region_stats` — DROP+CREATE inside one transaction with two appended defaulted params `category_main_filter text[] default null, category_type_filter text default null`; add `price_per_m2` to `filtered`; emit `ppm2_basis`. `region_active_by_day` in the same file is untouched.
6. `pipeline_board_public` — re-emit 417 adding `p.price_per_m2`, `p.price_per_m2_basis`, `p.category_type`; register it in `tests/test_tenant_isolation_live.py::_TENANT_VIEWS` (417 skipped this and 418 had to clean up).
7. `drop function if exists browse_stats(<full 083 signature>);` — **destructive, pause for operator OK + pg_dump.** Zero callers verified.
8. `comment on column` for `price_stat_observations.price`, `price_stat_city_metrics.{sale_latest_price,rent_latest_price,gross_yield_pct}`, `rent_map_values.{ref_rent_per_m2,ref_rent_novostavba_per_m2}`, `rent_map_adjustments.czk_per_m2`, using `scraper/price_stats_metrics.py:73-81`'s docstring sentence ("per-m² cancels, so units don't matter; rent is Kč/m²/month, sale is Kč/m²") as the canonical wording. Round `415:108` to 2dp so `price_stat_growth` and the stored `price_stat_city_metrics.gross_yield_pct` stop disagreeing. `price_stat_series` gains `gross_yield_pct` + `price_basis` (keeping existing columns so the SPA migrates in its own PR).
9. **Ends with** `select rebuild_browse_list(); select rebuild_properties_map_mv();` — inline, the ordering 363/375 already use. Do **not** hand-retype those function bodies (376 exists because 371 retyped one and silently regressed the anon grant plus three covering indexes). Never name migration 254 as an edit target: it is dead history for `properties_map_mv` since 277.

**HARD CONSTRAINT, put it in the migration header:** the function returns `round(x, 2)`. `migrations/200`'s header documents why — the SPA keyset cursor sends `price_per_m2.eq.<float64>` as its equal-value tiebreaker and an ~18-digit numeric does not round-trip through a JS Number, silently skipping rows at the page seam. Any future session that "unifies on the unrounded form" breaks Browse pagination. **Corollary benefit:** `listing_feed_public` flips from unrounded to rounded, which closes that row-drop on the single-portal Browse lane where it is live today (`effectiveSort` passes `price_per_m2` through untouched).

**Proves it correct:** new `tests/test_measure_price_per_m2.py` in the replayed-schema suite — one seeded row must return **byte-identical** `price_per_m2` from `listings_public`, `properties_public`, `browse_projection`, `listing_feed_public`, `browse_stats_properties` and `pipeline_board_public`; plus an `EXPLAIN` assertion in the `migrations.yml` replay DB that `properties_public` keyset paging and the `browse_list` covering indexes (283/376) still plan the same way.

### W5 — Python and API call sites onto the named measure
**Scope test: T1+T2. Migration 426.** After W4.
**Files:** **new** `toolkit/measures.py` — `per_m2_sql(alias)`, `per_m2_basis_sql(alias)`, the `Ppm2Basis` vocabulary, per-basis floors, unit labels; it is the module `api/estimate_yield.py:1-24`'s docstring gets promoted into. Then rows 10–14, 37, 40, 41, 61–64: `comparables.py:427,432,627` **plus** adding `l.category_main, l.category_type, l.price_unit` and `price_per_m2_basis` to the projection at `:626-645` (today it selects none of them — **every** "read the basis off the listing dicts" action downstream is blocked on this), `:783` coercion tuple, `:711-716` `_filters_used`; `transit_axis.py:313,418`; `neighborhoods.py:103-116,133-135`; `notifications.py:330,335` + the `:135` spec comment; `outliers/distribution/clustering` thread the basis into their envelopes (**never assert** — `POST /tools/analyze_distribution` takes client-supplied listings that need carry no basis, so degrade to `basis='unknown'`, never guess, never default to sale); `estimate_yield.py:76,210-222,229-234` (`_scale` gains `*, basis: str` and hard-fails on mismatch; the `price_czk`-percentile else-branch at `:99-101` must **not** be made to fail); `agent.py:161,180,1373-1377,1682-1716,1015-1057`; `portal_lookup.py` `_LISTING_COLS`+`_MARKET_SQL` gain `price_per_m2`, `price_per_m2_basis`, `area_basis` and `mf_reference_rent_per_m2_czk` **computed at the same grain as its numerator**; `region_annotations.py:235,239` takes the basis and renders `Kč/m²` / `Kč/m²/měs` / `Kč/m² pozemku`; a new `app_settings` revision superseding `migrations/104:96-97` that **receives** the basis and refuses to characterise a `'mixed'` cohort (additive via `app_settings_history`); both `skills/rental_estimator*/SKILL.md` + the live `skills` rows via `PUT /admin/skills/{name}` (verify the live row against the file first — it is operator-editable and may have drifted).
**Migration 426:** `alter table estimation_cohort_entries add column if not exists price_per_m2_basis text;` — additive, nullable; historical rows stay NULL = "basis unknown, pre-426". Do **not** recompute stored `price_per_m2` on existing rows (rules 8 and 12).
**Proves it correct — and this is load-bearing:** the SQL-correctness gate **does not cover any of these statements.** Verified by running discovery: 760 items, of which zero contain the per-m² expression; `comparables.py` → 0 items, `neighborhoods.py` → 0 items, `transit_axis.py`'s 4 items are at lines 185/212/240/354, not the corridor CTE at 313. All five are built by in-function concatenation into a local variable. So: promote each rendered statement to a module-level `*_SQL` constant the corpus can resolve, **or** add a direct test that `PREPARE`s `build_query()`'s output against the replayed schema. Additionally: rewrite `tests/toolkit/test_comparables.py:227-235` to assert against `per_m2_sql('l')` instead of a fourth hand-typed copy; **new** test asserting `api/notifications._build_match_clauses` emits the byte-identical clause `_shared_filter_where` produces (rule 16, currently unenforced — `grep -c price_per_m2 tests/api/test_notifications.py` = 0); extend `tests/api/test_portal_lookup.py`'s `_mk_market_row` builder with the new keys (the failure is a `KeyError` at `portal_lookup.py:197`, not an assertion).
**PR body must state:** the matcher now NULLs rows below the per-basis floor, so a saved alert with a Kč/m² bound stops matching a 136 Kč commercial rental. That is the intent, but it changes what a stored subscription fires on.

### W6 — Frontend: one formatter, one label, basis on every surface
**Scope test: T1+T2.** No migration. After W4. **Parallel with W7 and W9.**
**Files:** **new** `frontend/src/lib/measure.ts` (not `format.ts` — `filters.ts:7` already imports `format.ts` and a basis helper there creates a cycle): `export type Ppm2Basis = 'sale'|'rent'|'land'|'mixed'`, `ppm2Basis(categoryMain, categoryType)`, `ppm2BasisOfCohort(filters)`, and one `PPM2_UNIT` map whose Czech strings are lifted verbatim from `growthChoropleth.ts:188-189` (`'Nájem Kč/m²/měs'` / `'Cena Kč/m²'` — the repo's existing correct implementation; do **not** design fresh labels, and do **not** "fix" `buildHoverData`, which knowingly cancels two per-m² denominators and converts the period). Then: `format.ts:96-102` signature → `fmtPricePerM2(value: number|null, basis: Ppm2Basis)`; `fmtArea` gains `areaKind?: 'usable'|'plot'`; **delete** rows 16, 17, 19 (three private formatters); `queries.ts` rows 33–36 + `TableRow`/`CardRow`/`MapRow`/`ListingPublic`; the six callers and all twelve label sites (rows 21–32); `api.ts:377-386` + `BrowseExperience.tsx:586-598` send `basis` and **skip the annotation query entirely when `'mixed'`**; collapse the three parallel MF reference-rent interfaces in `types.ts:20-42`/`648-670`/`MfReferenceCard.tsx:20-30` into one; `Ppm2Box` (`types.ts:190-198`) gains a required sibling `basis` on every consumer; `Timeline.tsx:50-87` `SelectionRoundFilters` gains the two ppm² keys **before** `FILTER_ROWS:326-376` can reference them (it is typed `key: keyof SelectionRoundFilters`); `WatchdogManage.tsx:282-341` `summariseFilter` gains the missing ppm² branch.
**Do NOT touch:** `filters.ts:1162-1167` `pipelineViewFilters` — it is correct per rule 22 and is the canonical producer of the `'mixed'` state; and do not scope a `regionKeyFromFilters` cache-key change: `cat` and `deal` are already in the key, so the sale/rent collision does not exist.
**Proves it correct:** `frontend/src/lib/format.test.ts` **already exists** (two describes) — **extend** it: one case per basis with a distinct suffix, `'mixed'` never renders a number, U+00A0 preserved; new `measure.test.ts` pinning `ppm2BasisOfCohort(pipelineViewFilters()) === 'mixed'`; extend `keyset.test.ts` to pin `category_main`+`category_type` present in both COLS constants; extend `queries.test.ts` to pin that `effectiveSort` does **not** remap `price_per_m2` in portal-mirror mode (that pass-through is what W4's rounding fix depends on); extend `growthChoropleth.test.ts` to assert the two canonical labels — today the repo's one correct basis label has zero test coverage.
**Free riders, fix in the same commit, never counted as scope:** `BrowseStats.tsx:43` `unit="Kč / mo"` hardcoded on the absolute-price card; `ListingTable.tsx:269-277` missing the rental `/měs`.

### W7 — Chrome extension: read the server's measure, name the month
**Scope test: T2.** No migration. After W5. **Parallel with W6.**
**Files:** `content.ts:1108` — delete the client-side division and render the server's `mf_reference_rent_per_m2_czk` (this also fixes the property-grain ÷ listing-grain mismatch, which is wrong for merged groups independent of any basis question); `:1118` → `Kč/m²·měs`; `:1245` fond suffix → `Kč/m²·měs`; `:1149` area suffixed with its basis (`905 m² (pozemek)`); `:345` `subjectArea` returns `{value, basis}` — the fallback chain has **two** sources (`state.run.input_spec` and `state.listing`), so both `types.ts:38` and `:156` must carry it; `:369`/`:383` gate the fond multiplication on basis (today a 10 Kč/m²/month fond × a 905 m² plot manufactures ~9 050 Kč/month, subtracted from rent in the yield numerator — it can drive the yield negative); `:36` `DEFAULT_FOND_CZK_PER_M2` reads a server-served default; `types.ts:8,22,156` **additive fields only** (`api.ts:52-72` sends no version header and has a fixed endpoint set — there is zero version negotiation with a shipped bundle, so a redefinition is unrecoverable); `api.ts:133` stops hardcoding `estimate_kind:'rent'` for every category (it currently fires an apartment monthly-Kč/m² table against houses and land).
**Proves it correct:** the W8 census guard scans `content.ts` as text. **Do not add vitest + jsdom to a third territory** to check a string literal — that is the sprint's largest rule-7 ask and it buys nothing the offline guard does not.

**AS BUILT** — five corrections to the plan above, each found by reading the code the plan describes:
- **The month suffix is `Kč/m²/měs`, not `Kč/m²·měs`.** `Kč/m²/měs` is what `toolkit/measures.PPM2_UNIT_CS['rent_monthly_czk_m2']` and `growthChoropleth.ts:188` already spell, and it is one of the four literals W8's census arm looks for. Inventing a second spelling in a third territory is the exact duplication this sprint exists to end. `content.ts` now contains **one** per-m² unit literal in total (`CZK_PER_M2_MONTH`, one census site); every prose mention of a bare unit or a price-over-area quotient was reworded so the text scan has nothing else to register.
- **`input_spec` carries no basis and cannot be made to without leaving W7.** `estimation_runs.input_spec` is `_match_listing_by_url` / `_match_listing_by_id`'s `{lat, lng, area_m2, disposition, floor, exclude_ids}` (`api/estimation_runs.py:1528,1574`) or the LLM parser's spec — no area stamp in any arm. `types.ts:38` was therefore **not** given a phantom field. `subjectArea` returns `{value, basis}` with the basis read off `state.listing.area_basis` (the lookup's real stamp) for both arms, and honestly `null` for a subject not in our DB. **Residual:** the fond gate is authoritative only for subjects we have. Closing the unknown-subject arm means stamping an area basis into `target_spec` in `_build_resolution` — a separate, additive change to the estimation-run resolution path.
- **`DEFAULT_FOND_CZK_PER_M2 = 10` was not duplicated in `api/schemas.py`** — that file carried the number only as prose in `ScenarioUpdateIn`'s docstring. The live duplicate is `frontend/src/components/estimation/RunPanel.tsx:428`. Fixed by making the server the definition: `api/schemas.DEFAULT_FOND_CZK_PER_M2` is now a real constant, the docstring names it instead of restating it, and `POST /listings/lookup` serves `fond_per_m2_czk_default` **per subject** — `null` where the denominator is a parcel, so the "no fond on a plot" decision is made once, server-side, and the extension holds no copy of the rate. It is deliberately **not** gated on `measure_price_per_m2_basis`: that resolves rent-first, so a plot to let labels `rent_monthly_czk_m2` and would slip a land-basis test; `area_basis` and `category_main` are the two stamps that name the denominator. (`RunPanel.tsx` still holds its literal — W6's to collapse onto the same served value.)
- **The negative-yield path is narrower than stated, and the whole estimation arm is LATENT.** A found `pozemek` row already fails `isSaleApt`, which hides the MF and estimation blocks entirely, so the fond × plot product is reachable only for a listing **not in our DB** on one of the six portals with no `saleApartmentHint` (bazos, bezrealitky, maxima, remax, mmreality, realitymix). The fond gate shipped anyway — it is correct, and it becomes load-bearing the moment that category gate widens. The same gate makes the `POST /estimations` change **inert on this surface today**, and the plan's premise ("it currently fires an apartment monthly-Kč/m² table against houses and land") was never true *here*: `renderEstimation` runs only when `state.isSaleApt !== false` (`content.ts:725`), so every reachable call is either a found byt+prodej (→ `rent` + `byt`) or a subject we don't have (→ `rent`, no category) — byte-for-byte what the old hardcoded body sent. `estimateKindFor` can never return `sale` and `category_main` can never be anything but `byt` until that gate widens. The code shipped because it is correct then, not because it changes anything now; `CreateEstimationIn.category_main` defaulting to **`"byt"`** is why `estimate_kind` alone would not have been enough. **Residual:** widening the gate needs a second look at `defaultRent`, which would seed a byt+`pronajem` subject's rent from `mf_reference_rent_czk` (the MF *reference*) while its actual asking rent sits in `price_czk`.
- **"Does a fond apply" must be answered ONCE, and the first cut answered it twice.** The server withholds the rate on `area_basis == 'plot'` **OR** `category_main == 'pozemek'`; the client's `fondApplies` re-derived it from `area_basis` alone, and migration 423 has no backfill — a land row not yet re-scraped is `area_basis` NULL with `category_main = 'pozemek'`, exactly the row `tests/api/test_portal_lookup.py` enshrines, where the two rules disagree and the panel drops the yield entirely. `fondApplies` now reads the served answer (`fond_per_m2_czk_default !== null`) and `area_basis` only words the hint. Relatedly, `PortalListing.fond_per_m2_czk_default` is **optional**: absent (an API predating W7 — `build-extension.yml` uploads a reviewable `dist/` on every PR, so that pairing is normal) is not the same answer as a served `null`, and collapsing them with `?? null` blanked the field and returned no yield for every subject. `LEGACY_FOND_FALLBACK_CZK_PER_M2` covers only the absent case. Where no fond applies the input is now disabled and `bodyFromState` never PATCHes the rate — `estimation_runs.scenario` is one value shared with `RunPanel.tsx`, which has no plot guard, so persisting an inert rate would give one run two yields.

### W8 — THE PERMANENT RAIL *(installs the mechanism; must be last)*
**Scope test: T1.** No migration.

**First, in this same PR:** `CLAUDE.md` is **exactly 300/300** lines and `.github/scripts/docs-budget-check.sh` fails on `n > 300`. Reclaim ≥ 10 lines by moving rule-15 and rule-22 prose into `docs/architecture.md` **before** adding rule #23, or `docs-budget.yml` goes red on the commit that states the sprint's own conclusion.

The rail is three interlocking parts. No one part is sufficient.

**(a) Required-argument signatures — makes a unit-blind call impossible to write.**
`toolkit/measures.per_m2_sql(alias) -> str` is the only way to get the SQL fragment; there is no zero-arg variant to fall back to. `format.ts`'s `fmtPricePerM2(value: Ppm2Basis-bearing, basis: Ppm2Basis)` makes the old `(price, area)` call a **type error** — enforced for free by `npx tsc --noEmit`, already a blocking step at `.github/workflows/test.yml:63-71`. This is what stops the *next* developer, before CI is even involved.

**(b) `tests/test_measure_registry_census.py` + `toolkit/measures.REGISTERED_SITES` — the CI gate.**
Offline Python, no DB, no new dependency, runs on every push via the existing `pytest -q` (no YAML edit needed — pytest auto-collects). It scans `scraper/ toolkit/ api/ scripts/ frontend/src/ chrome-extension/src/` **and** the effective (highest-numbered) SQL definition of each database object, for two patterns:
   - a price-over-area division, **excluding** `ruian_*` and `area_km2`/`area_ha` (`location_data/ruian_boundaries.py` and `migrations/381` use `area_m2` for polygon area — a name collision, not a measure; and a naive regex false-positives on `neighborhoods.py:181`'s `active_count / area_km2` density);
   - a per-m² **unit literal** (`Kč/m²`, `CZK/m²`, `Kč/m2`, `Kč/m²/měs`) — this arm is what catches `scraper/price_stats_metrics.py:81`'s `12.0 * rent_per_m2_month / sale_per_m2`, which contains no `area` identifier at all and which a division-only regex misses entirely.

   Every hit must appear in `REGISTERED_SITES`; every registered measure must declare numerator, denominator, unit and validity bounds. It fails loudly on a 65th site.

**(c) `FilterDef.basis` + the codegen check — the label's home.**
`toolkit/filter_registry.py:133` already has `unit: str | None` (27 uses, **zero** test coverage). Add `basis` beside it, set `'depends_on_category'` on rows 39, fix the two descriptions to name the measure instead of restating `price_czk / area_m2`, fix the sale-scaled `max=500000 / step=1000` as agent-facing metadata (they reach agents via `GET /admin/filter-schema`; they are inert in the SPA, which renders `range_inputs`, not a slider). Add `test_every_pg_backed_numeric_filter_declares_a_unit` and `test_per_m2_filters_declare_a_basis`. **Regenerate `frontend/src/lib/filterRegistry.generated.ts` with `python -m scripts.generate_filter_registry` in the same commit** or `test.yml:36`'s `--check` reds the build.

**Also here:** `CLAUDE.md` rule #23 — "Every per-m² figure resolves from `measure_price_per_m2` / `measure_price_per_m2_basis`; no consumer re-derives the formula and no surface renders the number without its basis label." Plus a `docs/architecture.md` § entry.
**Proves it correct:** the census must be GREEN against the post-W7 tree with a registry listing exactly the 64 sites; then add a bare `price_czk / area_m2` in a scratch file and confirm RED, and remove it. Also fix the stale comment at `test.yml:46-48` ("no jsdom yet") — `frontend/vite.config.ts:29` sets `environment: 'jsdom'`.

**SHIPPED. Six corrections to the plan above.**

1. **"A registry listing exactly the 64 sites" was the wrong shape, and would have been a lie.**
   The 64 are the *consumer inventory* — the code locations the program had to CHANGE. What the
   census counts is what survives AFTERWARDS, and the two sets barely overlap: most of the 64 are
   gone (they now call the measure and spell nothing), while the census legitimately finds things
   that were never on the list at all — pinning tests, prompt strings, a `jsonb` health `detail`
   listing five column names. The registry as built declares **25 site-arms / 72 occurrences**,
   keyed on `(file-or-database-object, arm)` with an exact COUNT rather than line numbers: line
   numbers rot on every edit above them, a count moves only when the population does. Each entry
   carries a `kind` (`defines` / `calls` / `labels` / `guards` / `prose` / `debt`) and a `why`.
   Above it sit three `MEASURES` — `ppm2`, `fond_per_m2`, `gross_yield_pct` — each declaring
   numerator, denominator, unit and validity bounds, which is the "every registered measure
   declares…" half of the gate.
2. **Comments are stripped; string literals and docstrings are not.** This line is the whole
   design, and it is forced by the plan's own example: `price_stats_metrics.py`'s two units live
   in a DOCSTRING, so a scanner that stripped all prose would miss the site the second arm exists
   to catch. Scanning comments too would have registered ~30 explanatory `/* … */` blocks in the
   SPA and made a reworded comment a CI failure. The rule that separates them: a comment is prose
   ABOUT the code; a string is something the program can EMIT, and a unit inside one is a label a
   user or a model will read. One consequence, documented at the registry: the `why` texts
   deliberately DESCRIBE units rather than spelling them, so editing a justification cannot move
   its own file's count.
3. **Both regexes had to be rewritten for catastrophic backtracking.** The first draft's
   `\s*(?:…| |\s)*` around the unit slash, and three consecutive `\s*` separated by optional
   groups in the division arm, took the six-tree scan to **53 seconds** (5 s on a single 2 000-line
   module). One star per gap over non-overlapping alternatives: **2.5 s**. A gate nobody will wait
   for is a gate that gets switched off.
4. **"Effective SQL definition" needs DROP tracking, not just the highest number.**
   `properties_map_mv`'s newest `create` is migration 273's basis-blind one — dead history, since
   425 drops it and `rebuild_properties_map_mv()` rebuilds it from `browse_projection`. Without
   the drop pass the census would have been permanently red on a definition that does not exist.
   A migration that drops-then-recreates in the same file (083, 425) is a redefinition, so the
   comparison is strictly `drop_num > create_num`.
5. **`test_every_pg_backed_numeric_filter_declares_a_unit` cannot pass as literally written.**
   18 of the 32 column-backed numeric filters carry no unit, and 7 of those genuinely have none
   (three identifiers, four 1..5 condition ranks). Inventing eleven agent-facing unit strings in a
   per-m² wave is unreviewed metadata churn. Shipped instead: an explicit
   `UNITLESS_NUMERIC_FILTERS` set, so *silence* is illegal while "this has no unit" stays a legal,
   recorded answer — and the test also fails on a stale id in the set, and on a filter that is in
   both. The charter's `max=500000 / step=1000` rescaling was NOT done: those constraints are
   correct for a sale cohort and only the basis makes them ambiguous, which `basis` now states.
6. **The sweep had missed one of the twelve W6 label sites — a real, live defect.**
   `RunPanel.tsx`'s "Fond oprav + SVJ" input (charter site #27) still carried `suffix="Kč/m²"` on
   a MONTHLY charge — off by twelve, and contradicted by the Chrome extension, where W7 renders
   the same field with `CZK_PER_M2_MONTH`. Found by the census, fixed here: it reads
   `PPM2_UNIT.rent` from the shared map. This is the census's first catch, before it had ever run
   in CI.

**Red/green proof, both ways.** GREEN against the post-W7 tree with the registry above. RED on a
scratch file carrying `price_czk / area_m2` and `"Kč/m²"` — both arms fire, printing file, line,
source text and instructions. RED again on an extra unit literal added to an already-registered
file, as `COUNT MOVED`, printing the registered justification to re-read before bumping the
number. Part (a) proven the same way: making one `@ts-expect-error` call valid turns the
directive itself into a TS2578 build failure.

**HARDENED after adversarial review — the first draft's rail was narrower than its own claim.**
Six changes, five of them because a probe walked straight through the gate:

1. **The division arm now resolves whole OPERANDS, not identifiers.** The draft matched a bare or
   dotted name on each side, so `r["price_czk"] / r["area_m2"]` — the dominant row-access idiom in
   this repo, twenty-odd live occurrences across `toolkit/snapshots.py`, `api/notifications.py`,
   `scraper/db.py` and six portal mains — was invisible, as were `sum(price_czk) / sum(area_m2)`,
   `coalesce(price_czk, 0) / area_m2` and `price // area_m2`. The aggregate form is not exotic: it
   is the natural shape of a new region/obec stats RPC, i.e. the same class of site as
   `region_stats`, this program's worst find. Both sides are now resolved by a bracket-balanced
   walk outward from the operator, bounded at 160 characters (a miss beats a runaway false
   positive), `//` counts, and `_AREA_EXCLUDED` applies to the DENOMINATOR only — applying it to
   the numerator exempted `ruian_price_czk / area_m2`, which is a real re-derivation.
2. **Every migration statement is scanned, not only the five `create` forms.** The draft kept only
   statements matching `create [or replace] (materialized view|view|function|procedure|table)`, so
   `alter table … add column ppm2 generated always as (price_czk / area_m2) stored`, DML
   backfills, `create index ((price_czk / area_m2))` and `comment on column … 'CZK/m2'` were
   never scanned at all. The generated-column case is the worst outcome this charter contemplates:
   a persisted, unfloored, basis-blind second definition that every downstream consumer would then
   legitimately read as a plain column. And the `comment on` case is not hypothetical — migration
   425 § 7 puts the canonical unit strings into the catalog itself. The supersede logic still
   decides which OBJECT DEFINITIONS are live; everything else executes once and is scanned
   unconditionally. Registry grew by four SQL entries (migrations 104, 425 ×2, 426).
3. **A third arm: consuming the vocabulary is a census event.** Both value arms are spelling
   filters, and W8 itself teaches developers to IMPORT the label rather than spell it — so a probe
   that imported `PPM2_UNIT_CS` and computed `num / den` on a cohort with no basis resolution
   spelled nothing and named nothing, and passed green. `vocab` registers every file reading
   `PPM2_UNIT` / `PPM2_UNIT_CS` / `PPM2_VALUE_LABEL` / `PPM2_BASIS_TOKEN`, one hit per FILE
   (counting occurrences would red the build when a component reads the map twice instead of
   once — churn on a correct edit is how a gate gets switched off). Thirteen entries.
4. **The three "twin" vocabularies were compared, and they already disagreed.**
   `PPM2_UNIT.land` was a byte-for-byte copy of `PPM2_UNIT.sale` while `PPM2_UNIT_CS`, the SPA's
   own `PPM2_VALUE_LABEL.land` and `fmtArea(n, 'plot')` all said *pozemku* — one measure, two
   labels, in the two modules the registry calls twins, and `format.test.ts` pinned the wrong one
   because it asserted only `rent != sale`. The census counts occurrences and is value-BLIND by
   construction, so it could never have seen this. Two value-comparing tests now pin
   `PPM2_UNIT` and the extension's `CZK_PER_M2_MONTH` against `PPM2_UNIT_CS` basis-for-basis; the
   extension matters most, since that territory has no test job at all.
5. **`browse_stats` was registered as inert debt while it was still REACHABLE.**
   `has_function_privilege('authenticated', 'public.browse_stats', 'EXECUTE')` was true on
   production. Registering a reachable re-derivation as inert is the one thing the census must not
   do. Migration 428 revokes the grant — additive, autonomous, reversible with one `grant` — and
   the debt entry now states the truth: on disk and in the catalog, not reachable; the DROP still
   waits on the operator.
6. **Two tests were pinned to things that rot.** `test_the_effective_sql_definition_is_the_newest_undropped_one`
   asserted `listings_public` resolves to a migration whose FILENAME starts `425_`; that view has
   been replaced eighteen times, so the next legitimate replacement would have reddened CI blaming
   the wrong migration — for exactly the change rule #23 asks for. The filename assertion is gone;
   the semantic one (the live definition calls the measure) stays, and the supersede mechanics are
   covered against synthetic migration text in a `tmp_path`. And
   `test_every_pg_backed_numeric_filter_declares_a_unit` scoped itself with `and f.pg_column`,
   cutting the guarded population from 47 to 32 — where the two it hid were precisely the two with
   no declaration. Scope removed; `floor_band` and `price_change_count_min` declared.

**The census now names its own blind spots**, in the module docstring and in
`docs/architecture.md` § rule 23, rather than claiming it "reds on the next one, whatever it is".
That claim was false, and it was written into the two documents every future session is told to
trust — worse than no rail, because a green run reads as proof. What remains uncovered, on
purpose and in writing: closed-vocabulary spelling (`price_czk / sqm`, `amount / area_m2`, a unit
assembled at runtime), a division routed through a helper, and the fact that the SQL half is a
census of `migrations/` **on disk, not of the database** — dynamic DDL inside plpgsql (migrations
283/299/371/376) and the `property_sources_mv` drift are unregisterable and unseen. Registry as
hardened: **43 site-arms / 102 occurrences**, ~1.1 s.

### W9 — The plausibility gate
**Scope test: T3. Migration 427.** After W4. **Parallel with W6/W7.**
**Files:** `scripts/verify_pipeline.py` `_CHECKS` gains three keys (four as built) — `ppm2_median_shift` (per source × category_main × category_type, fail on an order-of-magnitude week-over-week median move), `ppm2_basis_floor_share` (fail when the share NULLed by the basis floor jumps), `area_vs_usable_divergence` (the direct mmreality detector: `area_m2 IS DISTINCT FROM usable_area` on a source where they should agree).
**Migration 427:** `create view measure_plausibility_by_source` — per (source, category_main, category_type): median `area_m2`, median `measure_price_per_m2`, share failing the basis floor, share where area diverges from usable.
**Why here and not the other health system:** `data_quality_by_source` (`318:43`) tests 29 fields for `IS NOT NULL` only — both live bugs produce 100% non-NULL values, so every health surface is structurally blind to them. And **do not** re-emit `scraper_health_checks_mv` to add a tile: its ~14 checks are hardcoded `jsonb_build_object` literals inside a ~300-line pg_cron-refreshed matview (`354:151-450`). `pipeline_check_results` (`274:27`, written from Python, exposed at `422:49`) is the zero-DDL home and it pages the operator through the existing alert path.
**Proves it correct:** unit tests over the check functions with synthetic rows; a manual run against prod recording the pre-W1 and post-W2 medians in the PR body.

**SHIPPED. Three corrections to the plan above, all forced by measuring production before
building (all figures live 2026-08-25, pre-W2-backfill; migration 427's header carries the
full tables).**

1. **`area_m2 IS DISTINCT FROM usable_area` is not the mmreality detector — twice over.**
   `usable_area` is NULL on 10 409 of 10 409 bazos dum/prodej rows, and `IS DISTINCT FROM`
   is TRUE against NULL, so the literal predicate scores a portal that simply does not
   publish the field at 100% divergent. It is also exact: realitymix byt diverges on 21.9%
   of its pairs at a *median relative difference of 9.6%* — a balcony convention, not a
   different quantity. Shipped: only rows carrying BOTH areas are counted (`n_area_pairs`;
   no pairs ⇒ NULL, never 0 and never 100), and only above a 10% relative band. That
   separates mmreality dum/prodej — **99.7% divergent, median relative difference 85.0%,
   `area_m2` 905.0 against `usable_area` 130.0 on the same 3 588 rows** — from every other
   portal's dum/prodej at **0.0%**. `pozemek` is skipped in the check: under Option A
   `area_m2` IS the plot for land, so divergence there is the correct answer.

2. **The floor share had to be an absolute LEVEL, not a "jump".** The unit-price masquerade
   never jumped — it has been standing for the life of the four portals carrying it, so a
   week-over-week detector is blind to it by construction. The level indicts it precisely:
   **ceskereality komercni/pronajem 20.0% (1 133 of 5 656), realitymix 19.0%, realitymix
   pozemek/pronajem 15.7%, remax 11.3%, bazos 6.7% — against idnes 0.5% and sreality 0.5%
   in the same cell**, which is exactly the split between the portals with and without a
   per-area price guard (charter rows 54/55). Fail 10%, warn 5%. **This check is RED on the
   day it deploys and that is correct**; it goes green when W2's backfill runs.

3. **A cross-portal "peer" arm is not viable and was not built.** Measured across every cell
   with ≥200 rows and ≥3 peers, a cell's median against its peers': realitymix ostatni/prodej
   **19.4x** on area (a junk-drawer category), idnes pozemek/prodej **8.5x** on Kč/m² (rural
   plot mix, 455 vs 3 849), mmreality dum/prodej **8.1x** (the bug). The defect does not
   separate from the mix at any threshold. `ppm2_median_shift` therefore compares a cell only
   against ITSELF a week ago, reading the baseline from its own `pipeline_check_results.details`
   row 6-14 days back (no new table). It **cannot** catch a defect older than its baseline —
   that is what corrections 1 and 2 are for — but it fires at 3.0x on a new basis flip, and it
   will fire on the W2 heal (**6.96x** on area, **7.45x** on the measure). Sized against the
   noisiest weekly move measurable today: 1.47x on area / 1.90x on Kč/m² across 21 new-arrival
   cohorts, a strictly noisier series than the stock medians it compares.

**Also shipped:** both share checks carry a trailing-7-day arm beside the stock arm and alarm
on the worse (mmreality is **100.0% divergent over the 113 pairs first seen in the last week** —
a regression is ~100% of what arrived since it shipped but only churn-fraction of the stock, so
without the fresh arm detection latency is weeks); the checks share ONE read of the view
per run (12 s sequential scan, cleared per run); an empty read is `warn`, never `ok`, so the
`is_platform_admin()` gate failing for the job's role cannot look like a clean bill of health;
and they run on the **6-hourly** `verify_pipeline.yml` lane only, not the hourly acute lane.

**4. A fourth check, `ppm2_measure_coverage`, and the fail-open rail behind it (review).**
Corrections 1-3 all measure a RATIO OVER ROWS THAT HAVE THE INPUTS, which makes all three blind
in the same direction: a cell with nothing to measure scores no arm, and a skipped arm is
indistinguishable from a clean one. Blank the measurable content of the real 100 production cells
— the per-m² measure 100% dead platform-wide, which is what a later vocabulary or category
migration would cause — and the three checks as first written returned `ok` with *"Per-m² medians
stable week-over-week (worst move 1.00x across 64 cells)"*. Nothing had been compared; `worst`
simply never left its initial value, and `len(snapshot)` counted cells over the row floor rather
than cells compared. This is live production state today, not a hypothetical: **sreality publishes
27 174 active `pozemek` rows with `area_m2` NULL on 27 174 of them** (the plot size is in
`estate_area`, which the measure does not read), so four cells covering ~7% of the active corpus
produce no measure at all and all three axes called them healthy. `data_quality_by_source` cannot
see it either — it groups by (source, field) with no category grain, so sreality's `area_m2`
reads 71.7% populated, mild patchiness rather than a category at zero. Shipped: the view publishes
the DENOMINATORS (`n_area_valued`, `n_ppm2_valued`, `n_active_7d`, `measure_input_gap_share`
+`_7d`); `ppm2_measure_coverage` alarms on them (stock arm warn-only at 0.95 — the five live dark
cells are 0.995-1.000, the next cell down is 0.894 — and the 7-day arm **fails** at 0.90, where
the worst live scored value is 0.358, because that share among this week's arrivals is a
regression in flight); rows the floor rejected are excluded from it, so one defect is never billed
to two tiles; and every check now returns the number of arms it actually scored and reports
`warn` with `value` null rather than `ok` when that number is zero. This is also the anti-silencing
rail: had mmreality's parser regressed to writing NULL instead of the plot area, `n_area_pairs`
would have fallen to 0 and today's 99.7% divergence FAIL would have flipped to a green skip.

**5. `ppm2_median_shift` gates each median on ITS OWN support, in both weeks (review).** It first
gated on `n_active`, while the statistic it compares is a median over only the rows carrying the
value. Live, **64 cells clear `n_active >= 200` but only 58 clear it on area support and 56 on
Kč/m² support** — and `bezrealitky/pozemek/prodej` is 1 643 active rows with **nine** areas, whose
Kč/m² spread across those nine is 17.6x (1 002.94 to 17 644.67). Two ordinary delistings move that
median past the 3.0x fail with no parser change and no backfill, and because `emit_transition_alerts`
fires on every re-entry into `fail`, an oscillating thin cell rings the bell again each time. The
snapshot now carries `n_area` / `n_ppm2` beside each median and both weeks are gated on them.


### Ordering and parallelism

```
W1 ──┬──> W2 ──┐
     │          │
W3 ──┴──────────┴──> W4 ──┬──> W5 ──> W7 ──┐
 (W1 ∥ W3: scraper/ vs    │                 │
  scripts/, disjoint)     ├──> W6 ──────────┤──> W8 (rail, LAST)
                          └──> W9 ──────────┘
```
- **Strict:** W1→W2. W3→W4 (property-grain coherence before the shared function). W4→{W5,W6,W7,W9}. W5→W7. Everything→W8.
- **Parallel:** W1 ∥ W3. W2 ∥ W3. W5 ∥ W6 ∥ W9 (Python/API vs `frontend/src/` vs `scripts/`+migration — disjoint). W6 ∥ W7 (different territories).
- **No wave ships a surface a later wave contradicts:** W4 fixes the value before W6 renders it; W6 renders the basis before W8 forbids rendering without one; W1 fixes the input before W9 alarms on it.

---

## 4. THE PERMANENT RAIL — stated once, concretely

**A ninth unit-blind call site is made impossible by three mechanisms installed together in W8:**

| Mechanism | Concretely | Catches |
|---|---|---|
| **Required-argument signature** | `toolkit/measures.per_m2_sql(alias)` has no zero-arg fallback; `fmtPricePerM2(value, basis)` makes the old two-number call a **TypeScript compile error** under the existing `tsc --noEmit` step | The developer, at the keyboard, in the SPA and the toolkit |
| **CI census gate** | `tests/test_measure_registry_census.py` — offline Python, no DB, no new dependency, on every push. Scans six source trees **and** the effective SQL definition per object, for a price/area division **and** a per-m² unit literal. Fails unless every hit is in `toolkit/measures.REGISTERED_SITES`, and unless every registered measure declares numerator, denominator, unit, bounds | The three territories the type system cannot reach: migrations, the Chrome extension, and Python-emitted SQL strings — which the existing SQL corpus provably does not see |
| **Registry + codegen check** | `FilterDef.basis` beside the existing `FilterDef.unit`, with `--check` on `filterRegistry.generated.ts` already blocking in `test.yml:36` | The agent-facing and SPA-facing *label* drifting from the SQL |

---

## 5. EXPLICITLY EXCLUDED — passes none of T1/T2/T3

1. `migrations/257:197` `recompute_mf_gross_yields` listing-grain twin — collapsing two **grains** of one already-basis-aware measure; merge/unmerge blast radius; not a duplicate *definition*.
2. `migrations/381:189` `ruian_admin_unit_geometries.area_m2` — cadastral polygon area; no price divided by it.
3. `location_data/ruian_boundaries.py:287,313,330` — same name collision. The census must exclude `ruian_*`.
4. `toolkit/floor_plan.py:59,84` — per-room LLM areas; never become a stored denominator.
5. `migrations/070:135` / `076:200` rental-estimator prompts — rent-only by construction; the basis is pinned by the agenda; they re-derive no formula.
6. `api/schemas.py:99-100`, `:143` — pure request defaults; the fix belongs in the toolkit output envelope.
7. `frontend/src/lib/types.ts:728-729` `EstimationFilters.min/max_price_per_m2` — verified dead three ways: declared, never written, never read.
8. `BrowseStats.tsx:41-45` `unit="Kč / mo"` — a mislabelled **absolute** price. Real bug, one-line free rider on the basis prop; must not justify sprint scope.
9. `ListingTable.tsx:269-277` missing rental `/měs` — same: absolute price, free rider.
10. `api/schemas.py:418` adding `ge=0` to `fond_per_m2_czk` — unrelated validation hardening.
11. `keyset.ts:217-228` `withKeysetColumns` — a no-op; the `Set` dedupe absorbs the new columns.
12. `filters.ts:1162-1167` `pipelineViewFilters` — correct per rule 22. Its `'mixed'` cohort is intended. Test it; do not change it.
13. `_region_hash` / `regionKeyFromFilters` cache collision — **refuted.** `cat` and `deal` are already in the serialized key; all three deal states hash distinctly. Do not touch.
14. `growthChoropleth.ts` `buildHoverData` — already the most basis-correct code in the SPA (it knowingly cancels two per-m² denominators and converts month→annual). Do not "fix"; copy.
15. `price_unit` normalisation into one typed enum across nine portals — genuinely T2, but `price_unit` is in `_HASH_FIELDS`, so write-time normalisation churns a snapshot on every listing, and its per-m² job is already done by `measure_price_per_m2_basis`. Follow-up program, not a wave. (Only the `COMMENT` and the LLM-schema line ride along, in W4/W1.)
16. A chrome-extension vitest runner + `build-extension.yml` wiring — a new devDependency in a third territory (rule 7) to check a literal the offline census already catches.
17. Renaming `price_stat_city_metrics.sale_latest_price` / `rent_latest_price` / `price_stat_observations.price` — anon-granted public surfaces (`149:49` grants `price_stat_series` to `anon`); a rename breaks the SPA's direct read. Comment now; rename never or much later.
18. Widening `listings.area_m2 numeric(7,1)` to match `properties.area_m2`'s bare `numeric` — a real asymmetry, recorded; no per-m² consumer changes.
19. A client-side bounds check inside `fmtPricePerM2` — would **hide** the source defect. Bounds live in the measure function and at the write boundary.
20. Reordering `source_trust_rank` to route around the mmreality parser bug — a per-portal branch in shared code by another name; silently changes survivorship for ~30 other fields.
21. Adding a per-m² tile to `scraper_health_checks_mv` — a ~300-line DROP+CREATE of a pg_cron matview for marginal gain; `pipeline_check_results` costs zero DDL.
22. **PR #397 commit `f247ea68`** (drop `min/max_usable_area` across registry/API/watchdog/SPA) — deletes a *different* measure; silently widens stored watchdog specs and saved presets; breaks archived-run display (rule 12).
23. PR #397's ROADMAP block recording unshipped work as Done; and, per repo rules, ROADMAP edits belong in `roadmap/<track>.md`, not the index.
24. `realtime_worker` freshness lag (price rewritten in ~30 s, `mf_gross_yield_pct` hourly) — a freshness gap, not a basis or definition problem.
25. Adding `CHECK` constraints to `listings.area_m2` / `price_czk` — could abort a live drain on already-stored values. Clamp-to-NULL at the write boundary instead (the existing `sane_listing_numerics` pattern).
26. Editing `migrations/254` for `properties_map_mv` — dead history since 277; would violate append-only **and** not change the live object.
27. `chrome-extension/src/index_overlay.ts:140` — already gated to `byt`+`prodej`, renders a %, not a per-m².

---

## 6. RISKS

**Data / rollback shaped**
1. **Visible value change:** `listings_public` and `listing_feed_public` flip unrounded → `round(…,2)`. Haléř-level, affects Listing Detail and the single-portal Browse lane. It is simultaneously the **fix** for `migration 200`'s documented keyset row-drop, which is live on the portal-mirror lane today (mechanism CONFIRMED from `keyset.ts:185-188` + `effectiveSort`; frequency unmeasured). Must be in the W4 PR body.
2. **Watchdog semantics change (W5):** the measure NULLs rows below the per-basis floor, so a saved alert stops matching a 136 Kč "commercial rental". Intended, but operator-visible. Ship with a pre/post count of affected `notification_dispatches`.
3. **`drop function browse_stats(...)` is destructive** — operator OK + `pg_dump` per the database gate. Zero callers verified by grep across `api/`, `toolkit/`, `frontend/src/`, `scripts/`; 083's own text is the restore script.
4. **`region_stats` DROP+CREATE** — appended params carry defaults so callers keep compiling, but the function is briefly absent. Wrap in one transaction.
5. **W2 backfill touches ~3,300 rows against append-only history.** Follow `scripts/backfill_idnes_areas.py` exactly: no snapshot written (correcting our own mis-parse of the same staged state), idempotent via a `raw_json` marker, dry-run count first. The next successful refetch appends exactly one genuine snapshot per healed row — bounded and self-limiting under rule 2.

**Design assumptions the live data may contradict**
6. **bezrealitky land is UNVERIFIED.** No fixture exists (`tests/fixtures/portal_html/` holds only idnes, mmreality, realitymix, remax; the `location_w2` set is PII-scrubbed and area-free). #397 asserts `surfaceLand` in an inline comment with no evidence. This does not block Option A — we never NULL land — but W9's plausibility view must not assume it. Run a live `SELECT` before trusting it.
7. **`maxima:491` and `ceskereality:598` read `'plocha pozemku'`, a label `remax_parser.py:740` reads and its fixture does not contain.** `estate_area` is asserted in only 3 of 9 portal test files. Six portals' plot capture is unproven, not fine. W1 should assert `estate_area` on all nine and file new area-bearing fixtures where none exist.
8. **Every row count in the established context is unverified in this worktree** (no `SUPABASE_DB_URL`): ~3,300 mmreality rows, ~3,000 sub-1000 Kč properties, the 16× land spread, the 905 vs 130 m² medians. Re-run before sizing W2 and before fixing the per-basis floors.
9. **Production has drifted from the migration chain** — `migration 418`'s own header says so. Before writing W4, re-confirm each object's live body with `pg_get_viewdef` / `pg_get_functiondef`; re-emitting a stale body is exactly how 371 regressed 283's anon grant and three covering indexes.

**Mechanism / CI**
10. **Planner regression risk:** a function call in a `WHERE` does not match an inline-expression index. Verified there is **no** per-m² expression index, generated column or constraint anywhere in 422 migrations, so nothing is invalidated — but the safety depends entirely on the function being `IMMUTABLE PARALLEL SAFE`, single-expression, **with no `SET search_path`**. A `SET` clause blocks inlining and turns every predicate into a full scan. `EXPLAIN` in the `migrations.yml` replay DB before merging W4.
11. **Rebuild cadence:** `browse_list` republishes every 15 min (mig 413), `properties_map_mv` every 30, both by blue-green DROP+CREATE. W4 must call both `rebuild_*` functions inline or Browse serves a stale-shaped table until the next cron.
12. **The SQL corpus covers none of the Python-emitted per-m² SQL** (verified: 0 discovered items in `comparables.py` and `neighborhoods.py`). W5 must arrange PREPARE coverage explicitly; a plan that leans on automatic coverage leans on nothing.
13. **`CLAUDE.md` is 300/300.** Rule #23 cannot land until lines are reclaimed in the same PR, or the blocking `docs-budget` job goes red.
14. **`main` is not branch-protected** (standing repo note) — a red PR *can* merge and auto-deploy. Confirm green before every merge in this sprint; the destructive step in W4 makes that non-optional.
---

## 7. OPEN ITEMS AFTER W1–W9 — the close-out

Three items survived the nine waves. Status lives here; the numbers and the live readings live in
[roadmap/measure-track.md](../../roadmap/measure-track.md) § "What remains OPEN".

### Item 1 — the W2 backfill write passes

**Root cause of the stall, found on the first live dispatch: W2 shipped both scripts without their
runners.** `scripts/backfill_idnes_areas.py`, the precedent both were cloned from, has
`backfill_idnes_areas.yml`; these two had nothing, so the only way to run them was a local shell
holding `SUPABASE_DB_URL` — a credential that lives in GitHub Actions. Both now have a dispatch-only
workflow, inverted from the house style so that `write` (not `dry_run`) is the input and the default
is to report.

**And neither script could actually run.** `listings` carries 9.1 GB of TOAST, so any statement that
touches `raw_json` across a large row set runs against the cluster's 120 s `statement_timeout`.
`backfill_mmreality_areas` put its resume marker in the `WHERE`, which forces a detoast of all 11,218
candidates before a `LIMIT` can apply — it died on the opening `count(*)`, 121 s in, never reaching
the select. `backfill_unit_price_masquerade` kept the marker out of the predicate but projected
`raw_json` for 5,000 rows per statement, and was cancelled on page 8 with 35,000 of ceskereality's
70,560 rows examined. Both are now **two statements per page** — ids only, then payload by primary
key — so the page bounds the detoast rather than the planner's choice. The derivation functions
(`derive()`, `changed_columns()`, `decide()`, `price_text_from_fragment()`) are untouched: only the
access path changed.

### Item 2 — the `area_basis` backfill

**THE FORK, decided: a script calling `derive_headline_area`, not a forward migration.**

The stamp is a claim about a value already stored, so a backfill must PROVE which arm of the
resolver won — never infer it, and never guess. `scripts/backfill_area_basis.py` therefore feeds the
one measure the stored columns prove was the winner to `scraper.area.derive_headline_area` and
writes back whatever it returns. The precedence and the five-token vocabulary keep exactly ONE
definition; what the script adds is only the *inference of which input won*, which is new knowledge
about an existing row rather than a second copy of the derivation. This is the same reasoning as
W4's: the north star constrains the definition, not the mechanism that observes it.

A forward migration was rejected on two independent grounds. It would have restated the resolver's
branch order as a second definition — the exact thing rule #23 exists to prevent. And it could not
have batched: a single `UPDATE` over ~242k rows of an 11 GB table does not fit the 120 s
`statement_timeout`, and wrapping it in a `do $$ loop $$` holds one transaction across the whole
sweep. The repo's `backfill_*.py` pattern already solves both.

**What is provable, and what is refused.** Three proofs cover ~241,900 of the 459,896 stampable rows
and take `plot` from **0 to 71,353**. The refusal is the load-bearing half: idnes and ceskereality
store a *collapsed* `usable_area`, so `area_m2 = usable_area` there proves only that one of three
labels won, and stamping `usable` would fabricate provenance on ~183k rows. Those stay NULL until
someone re-parses `portal_raw_pages` — a re-parse project, not a stamp. sreality's 39,371 land rows
also stay NULL, because `area_m2` is NULL on all of them and a basis describes `area_m2`; that is
the concrete, permanent reason a land gate must keep OR-ing `category_main = 'pozemek'` and can
never trust `area_basis` alone.

**Snapshot impact is zero by three independent mechanisms** — the column is out of `_HASH_FIELDS`,
the script writes that column and nothing else, and both `listings` triggers are `UPDATE OF` clauses
naming other columns. It is DML, not DDL, so unlike an `alter table` on this table it cannot
head-block a writer; it still declines to start while a `rebuild_%` is active.

### Item 3 — dropping `public.browse_stats`

Destructive, and gated on operator approval. Two facts make the spelling of the drop non-negotiable:
the census matches a registered site to a scanned one by **exact string equality on `(path, arm)`**,
and `_SQL_DROP`'s kind alternation recognises only `materialized view | view | function | table`.
So a `drop routine`, a double-quoted identifier, or a 428-style `do $$ … execute format(…) … $$`
dynamic loop would all leave migration 083's `create` in the effective set — the function would be
gone while the census stayed green on a registration for an object that no longer exists. The drop
must be a plain, statement-level `drop function public.browse_stats(<full signature>)`, paired in
the same PR with deleting the `KIND_DEBT` entry from `toolkit.measures.REGISTERED_SITES`. The two
edits are strictly coupled: either alone reds the census.
