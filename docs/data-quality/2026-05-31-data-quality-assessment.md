# Data-Quality & Completeness Assessment — 2026-05-31

**Method.** Live SELECTs against production (Supabase project `erlvtprrmrylhznfyaih`) cross-referenced
against a code map of every parser, the filter/watchdog WHERE-clause builders, and the derived-attribute
pipelines. All percentages are over **active** listings (`is_active = true`) unless noted, because that is
the set filters and watchdogs actually query.

**Caveat — the dataset is ~1 month old.** sreality history starts 2026-05-01 (when the project was
created); idnes/bazos/bezrealitky/maxima only started 2026-05-28→30. Time-series signals (price-drop
counts, time-on-market, delisting) are therefore shallow and not yet meaningful. This assessment is about
*completeness and integrity of the current state*, not longitudinal behaviour.

---

## 0. Platform snapshot

| Source | total | active | active & seen ≤7d | history since |
|---|--:|--:|--:|---|
| **sreality** | 94,006 | 71,317 | 71,291 | 2026-05-01 |
| **idnes** | 33,696 | 32,629 | 32,629 | 2026-05-29 |
| **bazos** | 8,110 | 7,863 | 7,863 | 2026-05-28 |
| **bezrealitky** | 3,624 | 3,423 | 3,423 | 2026-05-29 |
| **maxima** | 254 | 254 | 254 | 2026-05-30 |
| **TOTAL** | **139,690** | **115,486** | 115,460 | |

The platform has quietly grown from 2 sources to **5**. idnes is now the second-largest source (29% of
active listings). The idnes/bezrealitky/maxima bulk crawlers are **on `main`** (`scraper/idnes_*`,
`bezrealitky_*`, `maxima_*` + their workflows) — production is reproducible from `main`. They share the
unified portal framework (`BasePortalClient` + `db.ingest_scraped_listing()` + `listing_detail_queue`).

**Source characteristics that drive everything below:**

| | sreality | idnes | bazos | bezrealitky | maxima |
|---|---|---|---|---|---|
| transport | JSON v1 API | HTML scrape | HTML scrape | GraphQL API | HTML scrape |
| typed attributes | rich | medium | **none** | rich | medium |
| coordinates | API, precise | page-map (64%) | Maps-embed, **coarse** | API, precise | page-map (83%) |
| admin geo IDs | yes | no | no | no | no |

---

## 1. Do the scrapers parse what filters & watchdogs need?  (Areas 1 & 4)

**Headline: yes for the core economic fields, with sharp source-specific holes in the secondary fields.**

The single most important methodological point: **raw per-field NULL rates are misleading because most
secondary fields are category-conditional.** sreality `disposition` looks like 42.6% populated overall — but
that is because houses, land and commercial don't *have* a `X+kk` disposition. Conditioned on apartments it
is 98.6%. Every "scary" sreality number below dissolved the same way once conditioned. **sreality is, in
fact, near-complete for apartments.**

### 1a. Apartments (`category_main='byt'`, active) — the segment that matters most

| field | sreality | idnes | bazos | bezrealitky | maxima |
|---|--:|--:|--:|--:|--:|
| n (active byt) | 30,817 | 30,544 | 7,863 | 2,335 | 101 |
| price_czk | 97.2 | 97.5 | 90.0 | 100.0 | 97.0 |
| area_m2 | 100.0 | 100.0 | 86.8 | 100.0 | 100.0 |
| disposition | 98.6 | 99.4 | 90.7 | 99.4 | 100.0 |
| floor | 99.5 | 94.6 | **0.0** | 99.1 | 82.2 |
| condition (raw) | 100.0 | 98.6 | **0.0** | 97.3 | 100.0 |
| **has_balcony** | 100.0 | **6.4** | **0.0** | 50.0 | 26.7 |
| **has_lift** | 89.0 | **34.6** | **0.0** | 100.0 | 57.4 |
| geom (coords) | 100.0 | **63.9** | 89.9 | 100.0 | 87.1 |

### 1b. Source verdicts

- **sreality** — gold standard. Core + secondary fields all ≥89% for apartments. The only genuine source
  sparsity is `furnished` (43% of rentals) and `ownership` (varies by category), which sreality simply
  doesn't require sellers to fill — confirmed by raw_json inspection (the `ownership` object is *absent*,
  not zeroed, for the 110 null sale-apartments).
- **idnes** — strong core (price/area/disposition/floor/condition/ownership 95–99%) but **amenity extraction
  is badly broken**: apartment `has_balcony` 6.4%, `has_lift` 34.6%, `terrace` 8.0%, `garage`/`parking_lots`
  0%. Every idnes listing *has* a parsed detail page (`portal_raw_pages`: 33,696 parsed, 0 errors), so this
  is a **parser-coverage gap, not a fetch backlog** — the icon/`<dd>` detection in `idnes_parser.py` is not
  matching the live markup for these fields.
- **bazos** — structurally minimal *by design*: only price, area (86.8%), disposition, coords, description.
  Everything else (floor, amenities, condition, building_type, energy, furnished, ownership, locality,
  district) is **0%**. Any filter touching those fields silently excludes all 7,863 bazos listings.
- **bezrealitky** — second-best after sreality (GraphQL gives clean typed fields). Solid across the board.
- **maxima** — tiny (254), decent core, weaker amenities/geo. Low analytic weight.

### 1c. Enum hygiene (silently breaks multi-select filters)

- **sreality leaks listing *status* into attribute fields.** Reserved/sold listings carry
  `building_type ∈ {"rezervováno" (492), "prodáno" (47)}` and `condition = "rezervovano" (492)` — the same
  rows. sreality's API overlays the status label onto the `building_condition`/`building_type` params and
  `parser.py` stores it verbatim. **This corrupts the `condition` + `building_type` filters for those rows
  and feeds garbage into condition scoring.** (Bug — see §5.)
- **diacritic inconsistency**: wood construction is `"dřevostavba"` on sreality/bezrealitky but `"drevo"` on
  idnes/maxima — one concept, two stored values, so `building_type_match` fragments across sources. sreality
  also stores unmapped types *with* diacritics (`"modulární"`), violating the no-diacritics schema convention.
- **idnes ownership pollution**: `"jine"` (88), `"s.r.o."` (57), `"podilove"` (3) — 148 rows outside the
  canonical `{osobni, druzstevni, statni}` the `ownership` filter offers.
- `disposition` and `furnished` are clean across all sources.

### 1d. Filter-coverage gaps (a filter that binds a column populated for only one source silently drops the rest)

| filter(s) | column | coverage |
|---|---|---|
| `districts`, `locality_district_id`, `locality_region_id` | `district`, `locality_*_id` | **sreality only** — 48% of active listings (idnes/bazos/bezrealitky/maxima) can never match |
| `has_balcony`, `has_lift`, `terrace`, `garage`, `min_parking_lots` | amenity bools | reliable only on sreality/bezrealitky; idnes mostly null; bazos/maxima null |
| `building_condition_level_min`, `apartment_condition_level_min` | condition levels | **14% of active listings** (see §7) |
| `energy_rating_match` | `energy_rating` | populated but **semantically polluted** (see §4) |
| `min_estate_area`/`garden_area`, street/number | various | sparse or 0% (see §2/§4) |

---

## 2. Geospatial data quality  (Area 2)

### 2a. Street-level address: **0% everywhere, all sources**

`listings.street`, `house_number`, `zip`, `street_id` columns **exist** but are **0.0% populated for every
source, including sreality.** The structured-address capability is entirely unrealized: the parser does not
emit these fields and no backfill from `raw_json->'locality'` has run. sreality *detail* records demonstrably
carry `raw.locality.{street, housenumber, zip, street_id}` (per the dedup design doc), so this is recoverable
for sreality detail rows; bazos/idnes/maxima/bezrealitky do not carry street+number at all. **No exact-address
features (precise geo, address-based dedup rung, street display) are possible today.**

### 2b. Coordinates (`geom`)

| source | active w/ geom | % | distinct coords | **% distinct** | outside CZ |
|---|--:|--:|--:|--:|--:|
| sreality | 71,316 | 100.0 | 48,846 | 68.5 | 0 |
| idnes | 21,424 | **65.7** | 16,864 | 78.7 | 0 |
| bazos | 7,068 | 89.9 | 1,012 | **14.3** | 0 |
| bezrealitky | 3,423 | 100.0 | 3,227 | 94.3 | 0 |
| maxima | 210 | 82.7 | 209 | 99.5 | 0 |

- **idnes is missing coordinates for 11,205 active listings (34%)** — and **every single one has a
  `locality` string** (`no_geom_no_locality = 0`). The Mapy.cz geocoding fallback that *should* resolve those
  is not firing in the idnes bulk path. ~11.2k listings are a `geocode(locality)` call away from being
  mappable. This is the single highest-leverage geo fix.
- **bazos coordinates are coarse**: only **14.3% distinct** (1,012 unique points for 7,068 listings) — the
  Google-Maps-embed coordinate is a town/area centroid reused across many listings. This is *why* bazos won't
  dedup (§3): the Tier-1 matcher needs 20 m precision, but a shared centroid clusters dozens of distinct flats
  on one point.
- **No coordinate is outside the CZ bounding box for any source** — the bbox guard works; there is no garbage-
  coordinate problem. sreality's 68.5%-distinct reflects legitimate street/area-level rounding for privacy.

### 2c. Admin geo (district + locality IDs): **sreality only** (district 99.6%, locality_*_id 100%; all four
other sources 0%). `locality` text is present for everyone except **bazos (0%)**, which stores coordinates
but no place name at all.

---

## 3. Property grouping / cross-source dedup  (Area 3)

```
139,690 listings  →  127,088 properties     (every listing has a property_id; none orphaned)
  114,623 singletons   |   12,465 multi-listing   |   max 3 children
```

**All 12,465 multi-listing properties are also multi-*source*** (multi_listing == multi_source) — confirming
the design invariant that same-source listings are never merged (two same-address flats on one portal are
legitimately distinct units).

**Cross-source groupings actually formed:**

| sources merged | properties |
|---|--:|
| idnes + sreality | 11,741 |
| bezrealitky + idnes | 313 |
| bezrealitky + sreality | 270 |
| bezrealitky + idnes + sreality | 134 |
| maxima + sreality | 4 |
| **bazos + (anything)** | **2** |

**Findings:**
- Cross-source dedup works well for the **precise-coordinate sources** (sreality ↔ idnes ↔ bezrealitky).
- **bazos is effectively NOT deduped**: 7,863 active listings, but it appears in only **2** cross-source
  properties total. Root cause is mechanical (§2b): no locality + coarse centroid coords starve the Tier-1
  geo+price+area matcher. bazos currently adds inventory but almost no cross-portal grouping value.
- **The Tier-2 review queue is unconsumed**: `property_identity_candidates` holds **3,350 rows, 100% in
  `proposed`** — zero `auto_merged`, `rejected`, or `confirmed`. The Tier-2 sweep is *generating* candidates
  but nothing is resolving them (no auto-merge firing, no operator review). The 12,465 merges above all came
  from **Tier-1 insert-time** matching. (This is exactly the gap the open `/dedup` UI plan targets.)
- **The image-identity dedup rung is starved**: only **1.5% of images have a pHash** (34,831 / 2.32M). The
  pHash job is far behind, so the photo-based corroborator can't help bazos or anything else.

---

## 4. Key-attribute completeness  (Area 4)

Core economic fields (active):

| field | sreality | idnes | bazos | bezrealitky | maxima | note |
|---|--:|--:|--:|--:|--:|---|
| price_czk | 89.1 | 97.1 | 90.0 | 100.0 | 94.9 | sreality nulls are legit "cena na vyžádání" (commercial-rent 30.5%, houses) — **0.7% on rental apartments** |
| area_m2 | 100.0 | 100.0 | 86.8 | 91.2 | 100.0 | bazos 13% missing (regex from free text) |
| disposition | (98.6 byt) | (99.4 byt) | (90.7 byt) | (99.4 byt) | (100 byt) | excellent once category-conditioned |
| category_main/type | 100.0 | 100.0 | 100.0 | 100.0 | 100.0 | universally present |

**`energy_rating` is populated but semantically polluted.** It is the legal default `"G"` for *"no PENB
certificate provided"*, so the field conflates genuinely-G buildings with "unknown":

- idnes: **64% of all energy ratings are "G"** (20,782 / ~32k).
- sreality: "G" is also the modal value (34,287), plus 11,668 null.

Filtering or analyzing on `energy_rating` will badly over-count G and treat "no certificate" as "worst
rating." Treat "G" as low-information until the no-certificate case can be distinguished.

**Area fields** behave as expected: `estate_area`/`garden_area` are ~null for apartments (correct — they're
house/land fields), well-populated for `dum`/`pozemek`. `usable_area` mirrors `area_m2` on most sources.

---

## 5. Root causes of data-quality degradation  (Area 5)

1. **Per-portal parser coverage is uneven and HTML-fragile.** The biggest *real* gaps (idnes amenities 6–35%,
   idnes coords 66%, maxima amenities) are HTML-extraction misses, not pipeline failures — confirmed by a
   clean `portal_raw_pages` (every detail page parsed, 0 parse_errors). HTML portals (idnes, maxima) degrade
   silently when the live markup doesn't match the parser's icon/label selectors; API portals (sreality,
   bezrealitky) don't.
2. **Geocoding fallback isn't wired into the idnes bulk path.** 11,205 idnes listings have a geocodable
   locality but no coords — a capability that exists (`scraper/geocoding.py`) but isn't invoked here.
3. **Source-specific fields are simply absent at the source** (bazos has no structured attributes; sreality
   sellers skip furnished/ownership). This is irreducible — the fix is to *model NULL honestly per source*,
   not to "fill" it.
4. **Derived-attribute jobs lag far behind ingest.** Condition scoring covers 14%; pHash 1.5%; property-stats
   rollups stale for 7%. Ingest scaled to 5 portals faster than the enrichment jobs' throughput/scope.
5. **Enrichment jobs are sreality-scoped.** Condition scoring runs almost exclusively on sreality (22% vs
   ≤0.6% for idnes/bazos/maxima) even though idnes has 98% raw condition text — the scorer wasn't extended to
   the new portals (a multi-portal-modularity gap).
6. **Source quirks ingested without guards.** sreality overlaying `"rezervováno"`/`"prodáno"` onto
   `building_type`/`condition` (§1c) is a status value leaking into an attribute field because the parser maps
   unknown labels through instead of rejecting non-construction/non-condition strings.
7. **Cross-source value normalization is incomplete.** Diacritic and enum variants (`dřevostavba` vs `drevo`;
   idnes `s.r.o.`/`jine`) aren't canonicalized to a shared vocabulary, so the same real-world value reads as
   different values across sources.

---

## 6. Shortcuts, estimations & simplifications in the pipelines  (Area 6)

These are deliberate trade-offs — listed so they're visible, not necessarily wrong.

| # | shortcut | where | risk / effect |
|---|---|---|---|
| 1 | **Condition scoring is 100% text-only** (`avg n_images = 0` across all 19,389 rows) — the vision path is never exercised | `condition_scores.yml` | apartment-condition confidence avg **0.59**; it's a text re-encoding of the `condition` enum + markers, not a visual assessment |
| 2 | **`energy_rating = "G"` as the no-certificate default** | source convention | inflates G; field is low-information (§4) |
| 3 | **Tier-1 dedup tolerances**: geo ≤20 m, price ±2%, area ±1 m², same-source excluded | `scraper/db.py` | misses coarse-coordinate sources entirely (bazos) |
| 4 | **Tier-2 generation/merge thresholds** (150 m / ±8% / ±10 m²; auto-merge 30 m + corroborator) | `scripts/dedup_sweep.py` | output currently unconsumed (3,350 proposed, §3) |
| 5 | **Coordinates accepted as-is from portal embeds**; bazos centroid coords treated as precise | parsers | starves dedup; coarse map pins |
| 6 | **Unknown enum codes pass through** (building_type/condition/ownership stripped & stored raw) | per-source parsers | enum pollution (§1c) |
| 7 | **Content hash strips volatile fields** (`stats`, `note`, `is_topped`, and `lat`/`lon`/`sreality_id`) | `scraper/hashing.py` | geocoding drift never triggers a new snapshot |
| 8 | **Per-run detail-refetch caps** + queue draining | scrape config | transient lag; acceptable |
| 9 | **`is_active` inferred from index absence** gated on ≥90% walk completeness | `scraper/main.py` | safe rail, but a truncated walk defers delisting |
| 10 | **Marker dictionary / summaries on-demand only** | toolkit | tiny footprints (388 summaries, 0 image-comparisons) — fine, but not coverage |

---

## 7. Quality & completeness of calculated attributes  (Area 7)

| derived attribute | coverage (active) | quality signal | verdict |
|---|---|---|---|
| **building_condition_level** | 16,398 / 115,486 = **14.2%** | avg confidence 0.65 | low coverage |
| **apartment_condition_level** | 14.2% | avg confidence **0.59**; 2,489 rows (13%) < 0.5 | low coverage + low confidence |
| condition scoring — modality | — | **100% text-only**, 0 images ever used | not the designed two-axis vision score |
| condition scoring — by source | sreality 22%, bezrealitky 14%, idnes 0.6%, bazos 0.5%, maxima 0% | — | sreality-scoped; 88,296 active rows have raw condition text but no score |
| **property rollup stats** | 9,140 properties (7.2%) stale >2d or null | — | recompute job lagging |
| **pHash** | 34,831 / 2.32M images = **1.5%** | — | dedup image rung starved |
| listing_summaries | 388 | on-demand | working as intended |
| listing_image_comparisons | 0 | on-demand (≈$0.05/pair) | never run in prod |
| listing_marker_extractions | 1,869 (last 2026-05-17) | on-demand | stale, small |
| estimation_runs | 56 (last 2026-05-28) | operator tool | low usage, expected |

Condition level distribution (active, building axis): L1 50 · L2 881 · L3 5,717 · L4 5,978 · L5 3,772 —
concentrated in the middle/upper range, very few "critical" ratings. Total LLM spend on condition scoring to
date: **$252**.

---

## Prioritized findings

**P0 — silently wrong / corrupting:**
- sreality status (`rezervováno`/`prodáno`) leaking into `building_type` + `condition` (~540 rows) — parser
  guard + cleanup. Also poisons condition scoring input.
- `energy_rating = "G"` conflation — at minimum document; ideally distinguish "no certificate" from real G.

**P1 — large unrealized coverage (capabilities that exist but aren't running):**
- idnes geocoding fallback off → 11,205 listings (34% of idnes) mappable with one geocode call.
- Condition scoring not extended to idnes/bezrealitky (have raw condition text) → ~+34k scoreable rows.
- pHash backlog (1.5%) → blocks image-based dedup, esp. for bazos.
- Tier-2 dedup queue unconsumed (3,350 proposed) → no auto-merge / no review.

**P2 — structural gaps & hygiene:**
- street/house_number/zip 0% — parse sreality structured address + backfill (recoverable for sreality detail).
- district/locality_id sreality-only → district filters exclude 48% of listings; consider deriving
  district from coords for the other sources.
- idnes amenity parser coverage (balcony 6%, lift 35%) — fix selectors.
- Cross-source value normalization (`dřevostavba`/`drevo`, idnes ownership enums).
- bazos coarse coords / no locality → low dedup value; decide bazos's role.

---

## Appendix — reproduction

All figures from live SELECTs on `erlvtprrmrylhznfyaih` on 2026-05-31. The per-source completeness matrix,
category-conditioned matrix, geo, grouping, and derived-attribute queries are reproducible; a candidate
monitoring view (`data_quality_by_source`) would re-emit the §1/§2 matrices on demand for drift tracking.
