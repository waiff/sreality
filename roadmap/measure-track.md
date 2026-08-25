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
| W1 | Truth at the source: shared `derive_headline_area` across all 9 parsers, shared per-area price rail, per-basis floors | T3 | 423 | ✅ |
| W2 | Heal stored damage: mmreality area + unit-price masquerade backfills | T3 | — | 🟨 code + dry-run; **write pass is the operator's, still not run** |
| W3 | Property-grain coherence: numerator and denominator from the same child | T1+T3 | 424 | ✅ |
| W4 | **Keystone** — `measure_price_per_m2` + `measure_price_per_m2_basis` in SQL; 6 relations repointed | T1+T2 | 425 | ✅ |
| W5 | Python + API call sites onto the named measure (`toolkit/measures.py`) | T1+T2 | 426 | ✅ |
| W6 | Frontend: one formatter, basis on every surface, the map Kč/m² toggle | T1+T2 | — | ✅ |
| W7 | Chrome extension: read the server's measure, name the month | T2 | — | ✅ |
| W8 | **The permanent rail** — required-arg signatures + census CI gate + `FilterDef.basis` | T1 | — | ✅ |
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
### W5 as built — what changed, and three deviations

`toolkit/measures.py` is the Python face of the measure: `per_m2_sql(alias)` /
`per_m2_basis_sql(alias)` (both alias-required, so a unit-blind call cannot be written), the
four-token vocabulary plus `mixed` / `unknown`, the per-basis PRICE floors, the three Czech
unit strings, `ppm2_basis()` (a mirror of the SQL label for rows that never touched Postgres),
`spec_ppm2_basis()` (the same vocabulary read off a FILTER SPEC, where `None` means
UNCONSTRAINED), `cohort_basis()` and `require_scalable_basis()`. Nine consumers moved onto it. The five
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

### W5 review pass — five corrections

- **A cohort's unit is read off its ROWS, never off its filter pins.** `_filters_used` fed the
  filter spec to `ppm2_basis()`, a per-ROW mirror where `None` means "this row has no
  category" — but to a spec it means UNCONSTRAINED. `category_main=null, category_type='prodej'`
  is a legal request, and it stamped the whole envelope `sale_capital_czk_m2` while the cohort
  really held plots (Kč/m² of PLOT) beside flats (Kč/m² of FLOOR): the exact blanket unit this
  program exists to end, in the field the wave added to prevent it. `find_comparables` /
  `find_comparables_relaxed` now label the envelope AND every relaxation-trace snapshot with
  `cohort_basis()` over the rows that step returned, so a rung that widens into two bases says
  `mixed` at the rung that did it. The row-less path (the agent's opening message, written
  before any tool runs) uses `spec_ppm2_basis()`, which answers None rather than guess.
- **Agent mode is gated too.** `require_scalable_basis` lived only in `estimate_yield._scale`,
  which the agent never calls — and the SPA's default rent path IS agent mode. There the model
  does the multiplication and reports it through `record_estimate`, so an agent that widened a
  thin rental cohort onto `prodej` (`category_type` is in `_FCR_OVERRIDE_FIELDS`) could land
  `status='success'` with a purchase Kč/m² × area rendered as a monthly rent.
  `agent._require_cohort_scalable_into_rent` runs the same check on the server-derived cohort
  at terminator time, before `_finalise` persists anything; the run fails with the reason
  instead. A cohort with no measure-backed row is not gated — nothing produced a per-m² number
  there, so there is none to mislabel (the carve-out `_scale` already makes for an empty
  distribution). **This is a server-side gate, so it holds regardless of the two drifted
  `skills` rows below** — the prose fix was unreachable, the code one is not.
- **There is a SIXTH per-m² statement.** `api/portal_lookup._MARKET_SQL` gained three measure
  expressions and `l.area_basis`, and no gate could reach it: `sql_corpus.discover()` DOES find
  it, then `_is_format_template` skips it over its `{values}` slot, while
  `tests/test_measure_sql_prepare.py` hard-asserted the set was exactly five. A typo there is a
  42703 that ships green and 500s `POST /listings/lookup` — the Chrome extension's only
  market-data route. It is now the sixth entry, and the SKILL.md rule says a `*_SQL` constant is
  not evidence of coverage.
- **The client half of the basis handoff landed here, not in W6.** `_build_payload` now declares
  the unit UNKNOWN when no basis is supplied, so shipping the server half alone would have
  stripped the correct `Kč/m²` from every single-basis annotation for the whole W5→W6 window.
  `BrowseExperience` passes `BrowseStats.ppm2_basis` — the basis `browse_stats_properties`
  already resolves from the cohort's own rows (migration 425), not a second derivation off the
  filters — through `fetchRegionDispositionAnnotations`, and it is part of the React Query key.
  **Consequence, now rather than at W6: a mixed cohort's Stats annotations disappear** —
  verified on production, the DEFAULT Browse cohort reads `mixed` (rule 22), a `deal=prodej`
  one reads `sale_capital_czk_m2`. So the server's `metadata.notes` is now RENDERED in the
  annotations' place ("cohort mixes sale and rental listings…"): the paragraphs vanishing with
  no explanation would read as a bug rather than as the refusal it is.
- **Three behaviour changes shipped with no test.** The `'mixed'` refusal, the measure-gated
  neighborhoods cohort, and the `_tool_summary` key fixes could each be reverted with the suite
  still green. Each is now pinned (and each pin was mutation-checked against its own revert),
  alongside a new `tests/toolkit/test_measures.py` covering the full
  (category_main × category_type) matrix against migration 425's CASE, the `cohort_basis` /
  `require_scalable_basis` edges, and the floors read out of the migration file itself.

**Operator action still outstanding — the two live `skills` rows.** The charter asked for a
`PUT /admin/skills/{name}` alongside the SKILL.md edits. Both live rows have DRIFTED from their
files and in opposite directions: `rental_estimator_v1` is at 4,741 characters against a 5,931-
character file body (version 1, `seed` — git moved on without a re-import), and
`rental_estimator_full_v1` is at 12,426 against 12,080 (version 8, `settings_ui` — seven
operator revisions). Pushing either file wholesale would clobber real edits, so W5 changed the
git canon only. The per-m² sentences are identical in both copies, so the live rows can be
patched surgically from Settings; the diff is in the PR body.
### W2 open items

The two backfills are written, tested and measured; the **write pass is the operator's**
(`--write`, one portal at a time). W2 also found three live holes W1 believed it had closed —
all now fixed with a failing-first test, all of which the heal depended on, because a
quarantine the next drain cycle undoes is not a heal:

- **ceskereality's JSON-LD offer bypassed the per-area rail.** `parse_detail` trusted
  `offers.price` first, and the portal puts the RATE there verbatim (`"price":100` beside
  `100 Kč za m²/měsíc`). The rail only ever guarded the fallback path. The "Cena" spec cell
  now VETOES the offer rather than merely standing in for it when absent.
- **realitymix brackets its marker** (`45 Kč / (za m²)`), and the anchored test could not
  open a bracket, so it walked past every one of ~1,880 confirmed rows.
- **The shared anchor knew only Kč, and could not survive a decimal amount.** Both portals
  also quote in EUR — ceskereality stages `16 EUR za m²/měsíc` verbatim, realitymix renders
  `12,00 € / (za m²/měsíc)` and its amount scanner (an integer run, like every portal's)
  stops at the comma, so the slice reaching the rail began `,00 € …`. These rows were not
  merely missed, they were affirmatively decided `keep — no per-area marker`. The anchor now
  accepts `eur`/`€` and absorbs a leading decimal fraction, which adds **336 ceskereality**
  (1,015 → **1,351**, 1,336 active) and **~281 realitymix** (260 active) confirmed rows. A
  EUR *total* (`6 500 EUR za měsíc`, 46 ceskereality rows) is a currency bug, not a unit
  masquerade, and is still read as a total — the anchor's negatives are unchanged.

Still open after the write pass:

- **bazos is unrecoverable by design.** Across all 26,592 priced active bazos rows, ZERO
  carry a per-area marker anywhere in structured state — the cell is a bare `170 Kč` and
  the m² basis lives only in the prose description. The confirmed damage (Karlín offices at
  `432 Kč` on 635 m²) is real and is left untouched. Reaching it needs a description-level
  reader, which is W9's plausibility-gate ground, not a backfill's.
- A whole-fleet `area_basis` backfill. W2 stamps mmreality's 11,218 rows because it is
  re-deriving them anyway; the other eight portals are still NULL.

### W8 as built — the rail, and the one site the sweep had missed

**All nine waves are shipped.** W8 installs the mechanism that makes a 65th unit-blind site
fail CI rather than merely be absent today. Three interlocking parts, none sufficient alone:

- **(a) Required-argument signatures.** `toolkit.measures.per_m2_sql(alias)` /
  `per_m2_basis_sql(alias)` have no zero-arg fallback (pinned by a `TypeError` test), and
  `fmtMeasuredPricePerM2(value, basis)` makes the deleted `fmtPricePerM2(price, area)` a
  TypeScript error. That second half is now PINNED: `format.test.ts` carries two
  `@ts-expect-error` cases, so if the basis ever becomes optional — or accepts a number again —
  the directive itself becomes a TS2578 compile error under CI's already-blocking
  `npx tsc --noEmit`. Verified by making one call valid and watching the build go red.
- **(b) The census.** `tests/test_measure_registry_census.py` + `toolkit.measures.REGISTERED_SITES`
  — offline, no DB, no new dependency, ~1.1 s inside the existing `pytest -q`. It scans
  `scraper/ toolkit/ api/ scripts/ frontend/src/ chrome-extension/src/` **and `migrations/`** —
  both the EFFECTIVE (highest-numbered, undropped) definition of each database object AND,
  unconditionally, every statement that is not one of the five tracked `create` forms. **Three
  arms**, each of which the other two would miss something without:
  - `division` — a price-ish expression over an area-ish one. Both operands are resolved by a
    bracket-balanced walk outward from the operator, so `sum(l.price_czk)::numeric /
    nullif(sum(l.area_m2), 0)`, `r["price_czk"] / r["area_m2"]`, `coalesce(price_czk, 0) /
    area_m2` and `price // area_m2` all land — not only bare identifiers. `ruian_*` and
    `area_km2`/`area_ha` are exempt on the DENOMINATOR (polygon area is a name collision).
  - `unit` — a per-m² unit literal. It is the arm that catches
    `price_stats_metrics.gross_yield_pct`'s `12.0 * rent_per_m2_month / sale_per_m2`, which
    names no area at all.
  - `vocab` — every file that reads `PPM2_UNIT` / `PPM2_UNIT_CS` / `PPM2_VALUE_LABEL` /
    `PPM2_BASIS_TOKEN`, one hit per FILE. The other two are spelling filters, and this rule
    teaches developers to IMPORT the label rather than spell it — so a site that labels
    correctly and computes the number itself spells no unit and names no price identifier, and
    walks through both. Consuming the vocabulary is therefore a census event too.

  Comments are stripped, string literals and docstrings are not — a comment is prose about the
  code, a string is something the program can emit; a prose match is registered as
  `kind="prose"`, never reworded away. The registry declares **43 site-arms / 102 occurrences**,
  plus three MEASURES (`ppm2`, `fond_per_m2`, `gross_yield_pct`) each carrying its own
  numerator, denominator, unit and validity bounds. Two VALUE-comparing tests sit beside the
  three counting arms, because the census counts occurrences and is otherwise blind to what they
  say: the SPA's `PPM2_UNIT` and the extension's copied monthly suffix are pinned against
  `PPM2_UNIT_CS` basis-for-basis.

  Proven red on every shape: subscripted and aggregate divisions, a `generated always as`
  column, a `comment on column` mislabel, and the "labels correctly, computes wrongly"
  combination that walked through the two-arm draft. Also proven NOT red on a legitimate future
  `create or replace view listings_public` that calls the measure — the earlier draft pinned
  that view to migration `425_` by filename and would have reddened the nineteenth replacement
  of the most-churned object in the schema, blaming the wrong migration.

  **The census names its own blind spots** in the module docstring and in
  `docs/architecture.md` § rule 23, because a rail that oversells itself is worse than no rail:
  both value arms are closed-vocabulary spelling filters (`price_czk / sqm` passes), a division
  routed through a helper has no operator to walk out from, and the SQL half is a census of
  `migrations/` **on disk, not of the database** — dynamic DDL inside plpgsql and the
  `property_sources_mv` drift are unregisterable and unseen.
- **(c) `FilterDef.basis`** beside the existing `unit` (27 uses, previously **zero** test
  coverage), set to `depends_on_category` on `min/max_price_per_m2`, serialised into
  `/admin/filter-schema` and `filterRegistry.generated.ts`. Three new tests:
  `test_every_pg_backed_numeric_filter_declares_a_unit` (silence is no longer legal — a numeric
  filter carries a `unit` or is recorded in the new `UNITLESS_NUMERIC_FILTERS`, which also fails
  on stale ids), `test_per_m2_filters_declare_a_basis` (and the converse: an absolute must NOT
  claim one), and a payload test so the field cannot stop at the dataclass boundary. The unit
  test guards **all 47** numeric filters, not the 32 column-backed ones: the first draft scoped
  it with `and f.pg_column`, and the two filters that restriction hid (`floor_band`,
  `price_change_count_min`) were exactly the two with no declaration — the scope was
  load-bearing for green rather than principled, and it would have exempted the case that
  matters most, a derived per-m² bound served from an RPC rather than a column.

**Two illegitimate sites, found here and fixed here.** `RunPanel.tsx`'s "Fond oprav + SVJ"
field carried `suffix="Kč/m²"` — the CAPITAL label on a MONTHLY charge, off by a factor of
twelve, and the same field in the Chrome extension already rendered `Kč/m²/měs` via W7's
`CZK_PER_M2_MONTH`. It was site #27 of the charter's inventory and W6 missed it. It now reads
`PPM2_UNIT.rent` from the shared map.

And **the SPA's land basis was a byte-for-byte copy of its sale basis** — `PPM2_UNIT.land`
spelled the bare floor-area unit while `toolkit.measures.PPM2_UNIT_CS`, the SPA's own
`PPM2_VALUE_LABEL.land` and `fmtArea(n, 'plot')` all said *pozemku*. One measure with two
labels, in the two modules the registry calls twins. The census could not see it — both
spellings are legal strings, and it counts occurrences rather than comparing them. `PPM2_UNIT`
and `PPM2_UNIT_CS` are now compared VALUE by VALUE by the rail, the extension's copied suffix
alongside them (that territory has no test job at all, so this is the only thing between it and
a silent 12x mislabel), and `format.test.ts` asserts all three bases render pairwise
differently rather than only rent-vs-sale — which is how the drift stood.

Also here: **CLAUDE.md rule #23** (the sprint's own conclusion, as a hard rule) — landable only
after reclaiming 13 lines from rules #15 and #22, whose full prose already lived in
`docs/architecture.md`; CLAUDE.md is 295/300. `docs/architecture.md` gains a § rule 23 with the
measure's definition, why the basis is never read from `price_unit`, and why a rail rather than
a rule.

## What remains OPEN after W1–W9 — three items, none of them quietly dropped

1. **The W2 backfill WRITE passes have not been run** (`--write` is the operator's, one portal
   at a time): `scripts/backfill_mmreality_areas.py` and
   `scripts/backfill_unit_price_masquerade.py`. Live 2026-08-25: mmreality dum/prodej still
   reads a median **5 701 Kč/m² on a 905.0 m² median area** across 3 601 active rows, against
   **48 325 Kč/m²** for the same cell on every other portal. Nothing downstream can fix this —
   the measure is faithfully dividing a real price by a plot area stored in a floor-area column.
2. **`area_basis` is a young stamp, and NOTHING carries the plot token yet.** Migration 423 ships
   no backfill; the column fills as rows are detail-drained. Live 2026-08-25: **24.1% of 701 704
   listings** populated (up from ~14.6% at W7) — `usable` 146 687, `unknown` 22 560, `floor` **1**,
   `total` **0**, `plot` **0**. So any gate written on `area_basis` ALONE is inert today
   and will read "not land" for every plot in the database. **Gate on `category_main = 'pozemek'`
   as well**, exactly as `measure_price_per_m2` itself does.

   **DECIDED: a script calling `derive_headline_area`, not a forward migration**
   (`scripts/backfill_area_basis.py` + `backfill_area_basis.yml`). The stamp is a claim about a
   value already stored, so the backfill must PROVE which arm won, never infer it. The script
   feeds the one measure the stored columns prove was the winner to
   `scraper.area.derive_headline_area` and writes back what it returns — so the precedence and
   the five-token vocabulary keep exactly one definition. A SQL migration would have restated
   that logic as a second one, and could not have batched: a single `UPDATE` over ~242k rows of
   an 11 GB table does not fit the 120 s `statement_timeout`, and a `do $$ loop $$` would hold
   one transaction across the whole sweep.

   **Three proofs, ~241 900 of the 459 896 stampable rows (52.6%), taking the table to ~58.6%:**
   `plot` where `category_main='pozemek'` and there is an area — **71 353 rows, and the token
   goes off zero** — because the land arm stamps `plot` on whatever measure the page carried and
   that value IS `area_m2`, so no portal input is needed; `unknown` for bazos's 61 041 non-land
   rows, whose parser passes `fallback` and nothing else; `usable` on the ~109 500 rows where
   `area_m2 = usable_area` on the **six** portals that store that column un-collapsed.

   **DECLINED, ~218 000 rows, deliberately.** idnes and ceskereality collapse
   `užitná ?? podlahová ?? plocha` into `usable_area` before storing it, so an exact match there
   proves only "one of three labels won" — stamping `usable` would fabricate provenance on ~183k
   rows. Recoverable later by re-parsing `portal_raw_pages` (100% detail coverage on all seven
   HTML portals), which is a re-parse project, not a stamp. **sreality land (39 371 rows) stays
   NULL and that is correct**: `area_m2 IS NULL` on every one of them while the parcel sits in
   `estate_area`, and a basis describes `area_m2`. Moving it would be a value change on a hashed
   column — a different project. This is the concrete reason the `category_main` half of the gate
   can never be dropped.

   **Snapshot impact: zero, by three independent mechanisms.** `area_basis` is in
   `_LISTING_FIELDS` and not in `_HASH_FIELDS`; the script writes that column and nothing else;
   and both triggers on `listings` are `UPDATE OF geom` / `UPDATE OF geom, obec_id, category_main,
   category_type`, so neither fires. It is DML, not DDL — no ACCESS EXCLUSIVE, so unlike an
   `alter table` here it cannot head-block a writer — and it still refuses to start while a
   `rebuild_%` is active. It also skips `dirty_properties`: the singleton rollup does not mirror
   `area_basis` onto `properties` at all, so all 686 291 property rows stay NULL until a rollup
   change ships.

   It additionally corrects **8 rows that carry a basis `derive_headline_area` cannot produce**
   (7 sreality `pozemek` stamped `usable` with no area at all), which is why the selection reaches
   past `area_basis IS NULL`.
3. **`drop function public.browse_stats(<083 signature>)` still needs operator approval + a
   pg_dump.** Verified still present on production 2026-08-25. It is an orphan — zero callers in
   `api/ toolkit/ frontend/src/ scripts/`, and no function or view in the database references it
   either — superseded by `browse_stats_properties`, and it holds eleven unfloored, basis-blind
   per-m² expressions, the largest single cluster left in the schema.

   **It was not inert, though, and the census had registered it as if it were.** Live check:
   `has_function_privilege('authenticated', 'public.browse_stats', 'EXECUTE')` was **true**
   (`anon` false). The SPA runs as `authenticated` once a Supabase Auth user JWT is in hand, so
   the function was reachable as `POST /rest/v1/rpc/browse_stats` by any logged-in session —
   i.e. the platform could still be asked for exactly the numbers rule #23 says it no longer
   produces. Registering a *reachable* re-derivation as inert debt is the one thing this census
   must not do. **`migrations/428_revoke_browse_stats_execute.sql`** revokes that grant — a
   privilege change, additive and autonomous under the database gate, no pg_dump, and a single
   `grant` reverses it. **NOT YET APPLIED: the operator applies it via the Supabase MCP.** The
   `drop` itself is still destructive and still needs approval; the debt entry now says both
   things. Delete the registry entry when the function goes and the census will REQUIRE it gone.

## Next after this program

- `price_unit` normalisation into one typed enum across nine portals (genuinely T2, but it
  is in `_HASH_FIELDS` so write-time normalisation churns a snapshot per listing — a
  follow-up program, not a wave).
- Pin colouring by Kč/m² on the Browse map (needs per-basis ramps; deliberately deferred
  until the labels are on screen).
- Renaming the anon-exposed `price_stat_*` rate columns that are named like absolute
  prices (breaks the SPA's direct read — commented in W4, renamed never or much later).
