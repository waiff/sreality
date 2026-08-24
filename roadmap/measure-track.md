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
| W4 | **Keystone** — `measure_price_per_m2` + `measure_price_per_m2_basis` in SQL; 6 relations repointed | T1+T2 | 425 | ⬜ |
| W5 | Python + API call sites onto the named measure (`toolkit/measures.py`) | T1+T2 | 426 | ⬜ |
| W6 | Frontend: one formatter, basis on every surface, the map Kč/m² toggle | T1+T2 | — | ⬜ |
| W7 | Chrome extension: read the server's measure, name the month | T2 | — | ⬜ |
| W8 | **The permanent rail** — required-arg signatures + census CI gate + `FilterDef.basis` | T1 | — | ⬜ |
| W9 | Plausibility gate: per-source drift detection the null-checks are blind to | T3 | 427 | ⬜ |

Ordering: W1 ∥ W3 → W2; W3 → W4; W4 → {W5, W6, W9}; W5 → W7; everything → W8.

## Next after this program

- `price_unit` normalisation into one typed enum across nine portals (genuinely T2, but it
  is in `_HASH_FIELDS` so write-time normalisation churns a snapshot per listing — a
  follow-up program, not a wave).
- Pin colouring by Kč/m² on the Browse map (needs per-basis ramps; deliberately deferred
  until the labels are on screen).
- Renaming the anon-exposed `price_stat_*` rate columns that are named like absolute
  prices (breaks the SPA's direct read — commented in W4, renamed never or much later).
