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
| W5 | Python + API call sites onto the named measure (`toolkit/measures.py`) | T1+T2 | 426 | ✅ |
| W6 | Frontend: one formatter, basis on every surface, the map Kč/m² toggle | T1+T2 | — | ⬜ |
| W7 | Chrome extension: read the server's measure, name the month | T2 | — | ⬜ |
| W8 | **The permanent rail** — required-arg signatures + census CI gate + `FilterDef.basis` | T1 | — | ⬜ |
| W9 | Plausibility gate: per-source drift detection the null-checks are blind to | T3 | 427 | ⬜ |

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

### W5 as built — what changed, and three deviations

`toolkit/measures.py` is the Python face of the measure: `per_m2_sql(alias)` /
`per_m2_basis_sql(alias)` (both alias-required, so a unit-blind call cannot be written), the
four-token vocabulary plus `mixed` / `unknown`, the per-basis PRICE floors, the three Czech
unit strings, `ppm2_basis()` (a mirror of the SQL label for rows that never touched Postgres),
`cohort_basis()` and `require_scalable_basis()`. Nine consumers moved onto it. The five
estimation-path statements W4 deferred (`comparables.py` ×3, `transit_axis.py`,
`neighborhoods.py`) now call `measure_price_per_m2(...)`, which **changes estimation output**:
a comparable below its basis floor loses its per-m² figure and drops out of the cohort's
percentiles. Live count on `listings` today: **2,843 of 312,224** active priced-and-sized rows
(0.91%), of which 2,295 are `komercni`/`pronajem` under the 1,000 Kč rent floor and 188 have no
resolvable basis at all.

- **The Watchdog and comparables clauses are NOT byte-identical, and cannot be.** The charter
  asked for a byte-identity test between `notifications._build_match_clauses` and
  `comparables._shared_filter_where`. They run against different relations: the matcher reads
  `properties_public`, which PUBLISHES `price_per_m2` as the measure, while
  `_shared_filter_where`'s only three FROM clauses (`comparables.py`, `velocity.py`,
  `transit_axis.py`) are the `listings` TABLE, which has no such column — `l.price_per_m2`
  there is a 42703 at PREPARE. The invariant that IS enforceable is "neither derives the
  formula, both resolve to `measure_price_per_m2`", and that is what
  `tests/api/test_watchdog_browse_one_measure.py` pins, including a source-text guard against
  a fifth hand-typed copy.
- **`_scale` needs the estimate_kind as well as the basis.** The charter specified
  `_scale(..., *, basis)`. A basis alone cannot detect the error worth detecting: `median ×
  area` is the same multiplication for a monthly Kč/m² and a capital Kč/m², so the check is
  whether the basis agrees with what the product will be CALLED. `_scale` takes both, and
  `POST /estimate_yield` maps the resulting `MeasureBasisError` to 422 (the two run-backed
  callers already record it as a failed run). The `price_czk`-percentile branch is not gated.
- **`describe_neighborhood`'s agent summary read two keys that do not exist.**
  `agent.py`'s `_summarise_tool_result` asked for `active_listings` and a cohort-wide
  `median_price_per_m2`; the tool publishes `active_listing_count` and a per-DISPOSITION block,
  so the agent has always been handed two `None`s. It could not be made basis-aware without
  being made correct first, so it now reports the count that exists and the per-disposition
  medians each with its own basis.

Also in W5: `neighborhoods`' per-disposition stats are gated on the MEASURE rather than on
price+area, so `n`, `median_price_czk` and `median_area_m2` describe the same rows the
percentiles do; `portal_lookup` serves `price_per_m2`, `price_per_m2_basis`, `area_basis` and a
new `mf_reference_rent_per_m2_czk` computed at the grain of its own numerator (a CASE, not a
coalesce of two ratios — the extension divides a property-grain rent by a listing-grain area
today, which is wrong for every merged group); `analyze_distribution` /
`find_distribution_outliers` / `cluster_comparables` carry `basis` in their envelopes and
degrade to `'unknown'` for the caller-supplied rows `POST /tools/analyze_distribution` accepts;
`summarize_region_dispositions` takes `ppm2_basis`, states the unit in the payload, and REFUSES
a `'mixed'` cohort outright (no LLM call, no cache write). Migration 426 adds
`estimation_cohort_entries.price_per_m2_basis` (nullable; historical rows stay NULL =
"basis unknown, pre-426", never backfilled — rules 8 and 12) and supersedes migration 104's
region-annotator prompt, guarded on `updated_by = 'seed'` (verified still `seed` on production).

**Operator action still outstanding — the two live `skills` rows.** The charter asked for a
`PUT /admin/skills/{name}` alongside the SKILL.md edits. Both live rows have DRIFTED from their
files and in opposite directions: `rental_estimator_v1` is at 4,741 characters against a 5,931-
character file body (version 1, `seed` — git moved on without a re-import), and
`rental_estimator_full_v1` is at 12,426 against 12,080 (version 8, `settings_ui` — seven
operator revisions). Pushing either file wholesale would clobber real edits, so W5 changed the
git canon only. The per-m² sentences are identical in both copies, so the live rows can be
patched surgically from Settings; the diff is in the PR body.

## Next after this program

- `price_unit` normalisation into one typed enum across nine portals (genuinely T2, but it
  is in `_HASH_FIELDS` so write-time normalisation churns a snapshot per listing — a
  follow-up program, not a wave).
- Pin colouring by Kč/m² on the Browse map (needs per-basis ramps; deliberately deferred
  until the labels are on screen).
- Renaming the anon-exposed `price_stat_*` rate columns that are named like absolute
  prices (breaks the SPA's direct read — commented in W4, renamed never or much later).
