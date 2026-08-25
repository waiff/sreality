# Measure unification track

**North star: one measure, one definition, one label.** Every per-m² figure the platform
computes or renders — SQL, Python, SPA, Chrome extension — resolves from a single named
measure carrying its own numerator, denominator, unit and validity bounds. No consumer
re-derives the formula; no surface renders the number without its basis label.

Full charter, the 64-site consumer inventory, the architectural fork and the excluded list:
[docs/design/ppm2-measure-unification.md](../docs/design/ppm2-measure-unification.md).

Scope tests — a change is in scope if it passes at least one. **T1** collapses a duplicate
definition · **T2** makes a consumer basis-aware · **T3** makes the measure trustworthy at
the source. Passing none is scope creep, and the charter's §5 records what that excluded.

## Why this exists

`price ÷ area` is independently written into 64 code locations across four territories,
and none of them knows whether it is producing a sale price per m², a *monthly* rent per
m², or a land price per m² of plot. In the default mixed cohort (rule 22 — `category_type`
is nullable, the "Vše" pill) a rental card reading `319 Kč/m²` sits in the same list as a
sale card reading `91 535 Kč/m²`, with nothing distinguishing them. Two live input defects
feed it: mmreality stores the plot area for houses, and three portals store a per-m² unit
price in `price_czk`.

## The fork, decided

**Option A.** `area_m2` stays polymorphic; the MEASURE resolves its basis from
`(category_main, category_type)` at read time through one named function. `listings.area_basis`
ships as a **provenance stamp** — an observation of which physical area the column already
holds, never a constraint that changes its value.

Option B (PR #397's model — `area_m2` becomes one physical thing, land's plot relocates to
`estate_area`) was rejected: bazos writes no `estate_area` at all, so for land it is
deletion rather than relocation; `area_m2` is in `_HASH_FIELDS`, so nulling land area
appends ~24.8k snapshots for a non-event (rule 2); and `api/estimate_yield.py` would
silently switch land from per-m² to absolute-price comparison with no trace entry. The
north star constrains the *measure*, not the *column*. See charter §2.2.

**PR #397: supersede and close.** Salvage `scraper/area.py` and `tests/scraper/test_area.py`
by copy (land branch inverted); discard its migration, its land rule, and commit `f247ea68`
wholesale — that one deletes a different measure (`min/max_usable_area`), silently widening
every stored watchdog spec and saved preset, and breaking archived-run display (rule 12).

## Waves

| Wave | Purpose | Test | Migration | Status |
| --- | --- | --- | --- | --- |
| W1 | Truth at the source: shared `derive_headline_area` across all 9 parsers, shared per-area price rail, per-basis floors | T3 | 423 | ⬜ |
| W2 | Heal stored damage: mmreality area + unit-price masquerade backfills | T3 | — | ⬜ |
| W3 | Property-grain coherence: numerator and denominator from the same child | T1+T3 | 424 | ⬜ |
| W4 | **Keystone** — `measure_price_per_m2` + `measure_price_per_m2_basis` in SQL; 6 relations repointed | T1+T2 | 425 | ✅ |
| W5 | Python + API call sites onto the named measure (`toolkit/measures.py`) | T1+T2 | 426 | ⬜ |
| W6 | Frontend: one formatter, basis on every surface, the map Kč/m² toggle | T1+T2 | — | ⬜ |
| W7 | Chrome extension: read the server's measure, name the month | T2 | — | ⬜ |
| W8 | **The permanent rail** — required-arg signatures + census CI gate + `FilterDef.basis` | T1 | — | ⬜ |
| W9 | Plausibility gate: per-source drift detection the null-checks are blind to | T3 | 427 | ✅ |

Ordering: W1 ∥ W3 → W2; W3 → W4; W4 → {W5, W6, W9}; W5 → W7; everything → W8.

### W4 as built — four deviations every later wave must inherit

- **The capital `category_type` is an enumerated allowlist, not "everything that is not
  `pronajem`".** Live `category_type` has FOUR values, not the two the charter assumed:
  `drazba` (auction) and `podil` (co-ownership share) carry 10,595 active properties
  between them. Both are capital transactions and resolve to `sale_capital_czk_m2`;
  anything outside `('prodej','drazba','podil')` — including a NULL `category_type` —
  resolves to NULL basis and NULL measure, a visible gap rather than a silent guess.
  W5's `toolkit/measures.py` and W6's `frontend/src/lib/measure.ts` must use the same
  four-value vocabulary, not a two-value one.
- **Basis resolution is rent-first.** `pozemek` + `pronajem` (1,845 active properties) is a
  MONTHLY figure and is labelled `rent_monthly_czk_m2`; only a capital land listing gets
  `land_capital_czk_m2`. Resolving `category_main` first would put a rent under a capital
  label — the exact confusion this program exists to end.
- **`browse_stats(...)` was NOT dropped.** Dropping is destructive and this sprint carried
  no operator sign-off for a destructive step, so 425 records the intent reversibly with
  `COMMENT ON FUNCTION` (superseded, unreferenced, do not resurrect). **Follow-up needing
  operator approval:** `drop function public.browse_stats(<083 signature>)` plus a pg_dump.

- **Two consumers were repointed onto the measure in W4, not W5.** The Watchdog matcher
  (`api/notifications.py._build_match_clauses`) hand-typed `price_czk / NULLIF(area_m2, 0)`
  against `properties_public` — the very view 425 re-emits — so leaving it would have broken
  architectural rule #16 (Watchdog and Browse share one definition of "matches") the moment
  the floors landed: a saved `max_price_per_m2` would still fire on the ~2,092
  komercni/pronajem 0.54 Kč/m² rows Browse now excludes. It reads `l.price_per_m2` now. The
  Browse table and cards did the same division client-side
  (`fmtPricePerM2(price_czk, area_m2)`) while sorting and filtering on the server column, so
  ~2,900 rows displayed a figure the sort and the filter said did not exist; both cells now
  read the selected `price_per_m2` through `fmtMeasuredPricePerM2`. **Neither change needs
  migration 425 applied first** — `properties_public.price_per_m2` and
  `browse_list.price_per_m2` both exist today; 425 only changes what they contain.
  **Still deferred to W5**, and knowingly so: the estimation path re-derives the quotient
  against `listings` (which publishes no measure column), at
  `toolkit/comparables.py:427,432` (the `_shared_filter_where` bounds) and `:627` (the
  `price_per_m2` projected into every comparable dict), `toolkit/transit_axis.py:313`, and
  `toolkit/neighborhoods.py:103,106,109`. Those sites must move to
  `measure_price_per_m2(l.price_czk::numeric, l.area_m2::numeric, l.category_main,
  l.category_type)`, which is the first code that will REQUIRE 425 to be applied before it
  merges — and it changes estimation output (comparables below their basis floor lose their
  per-m² figure), so it wants its own gate. Until then the estimation trace's per-m² for a
  listing disagrees with `listings_public.price_per_m2` for that same listing, by rounding
  and by the floor.

Migration 425 also repaired pre-existing prod/repo drift discovered while fetching the live
bodies: `properties.all_sources` / `active_sources` (text[]) existed on production and were
projected by the live `browse_projection`, with no migration file on any branch that reached
`main` (migration 375's header flagged and deliberately deferred it). 425 adds both
idempotently. Because `CREATE OR REPLACE VIEW` may append an output column but may never
REPOSITION one, and the drift pair sits AHEAD of `home_city_id` on production while a fresh
CI replay of migration 375 has neither, 425 guards the re-emission: on the narrow 375 shape
only (i.e. on the replay) it drops `properties_map_mv` and `browse_projection` first, so the
statement becomes a plain create at the right shape. On production the guard is inert.
Nothing reads or writes the two columns.

### W9 as built — what the operator will see, and what it says about the data

Migration 427 adds `measure_plausibility_by_source` (per source × category_main × category_type,
active listings: the medians, the share the basis floor NULLs, the share where `area_m2` diverges
from `usable_area` by >10%, and the coverage denominators) and `scripts/verify_pipeline.py` gains
four checks over it — `ppm2_median_shift`, `ppm2_basis_floor_share`, `area_vs_usable_divergence`,
`ppm2_measure_coverage` — on the 6-hourly lane,
sharing one read per run. They exist because `data_quality_by_source` tests 29 fields for
`IS NOT NULL` only, and both defects this program fixed produce 100% non-NULL values.

Measured against production the day before W2's backfill, both checks indict the real defect and
stay silent on the healthy portals in the same cell: `area_vs_usable_divergence` reads **99.7%** on
mmreality dum/prodej (`area_m2` 905.0 vs `usable_area` 130.0 on 3 588 rows) against **0.0%** on
every other portal, and `ppm2_basis_floor_share` reads **20.0% / 19.0% / 15.7% / 11.3% / 6.7%** on
ceskereality, realitymix ×2, remax and bazos against **0.5%** on idnes and sreality. So **/health
shows two red tiles from the moment this deploys**, and that is the correct reading — they go green
when W1's parser rail and W2's backfill have worked through the stock, which is exactly the signal
the operator has never had. The five deviations from the charter's plan (why the divergence test
is not `IS DISTINCT FROM`, why the floor share is a level and not a jump, why there is no
cross-portal peer arm, why a fourth coverage check was needed, why each median is gated on its own
support) are recorded in the design doc's W9 section and migration 427's header.

**A third tile is AMBER on day one, and it is also correct.** `ppm2_measure_coverage` reports that
sreality's four `pozemek` cells (20 484 + 5 825 + 459 + 406 active rows) and bezrealitky's
(1 643) have no per-m² measure at all: land plot size lives in `estate_area`, which
`measure_price_per_m2` does not read, so `area_m2` is NULL on 27 174 of 27 174 sreality land rows.
That is a **standing, sanctioned gap** (the charter's "a visible gap, never a guess"), not a
regression — which is why the stock arm can only amber, while the same share among a week's new
arrivals fails. Teaching the measure to read `estate_area` for `pozemek` is the obvious follow-up
and is deliberately NOT part of W9: this wave makes the hole visible, it does not fill it.

## Next after this program

- `price_unit` normalisation into one typed enum across nine portals (genuinely T2, but it
  is in `_HASH_FIELDS` so write-time normalisation churns a snapshot per listing — a
  follow-up program, not a wave).
- Pin colouring by Kč/m² on the Browse map (needs per-basis ramps; deliberately deferred
  until the labels are on screen).
- Renaming the anon-exposed `price_stat_*` rate columns that are named like absolute
  prices (breaks the SPA's direct read — commented in W4, renamed never or much later).
