/* THE per-m² basis on the frontend: one vocabulary, one unit string, one place.
 *
 * NORTH STAR — one measure, one definition, one label. The NUMBER always comes
 * from the server (migration 425's `measure_price_per_m2`, published as
 * `price_per_m2` on all six read relations); nothing here re-derives price/area.
 * What lives here is the LABEL half: which basis a figure is on, and the Czech
 * unit that basis is spelled with.
 *
 * THE LABEL IS READ FROM THE SERVER TOO, WHEREVER THERE IS A COLUMN TO READ.
 * `measure_price_per_m2_basis(category_main, category_type)` is published as
 * `price_per_m2_basis` on ALL SIX read relations — listings_public,
 * properties_public, listing_feed_public, pipeline_board_public AND the two
 * derived Browse read models, `browse_list` + `properties_map_mv`. Migration
 * 425 § 9 refuses to commit unless the last two carry it (both rebuilds run
 * inside the migration, and a skipped rebuild raises), and the column was
 * re-verified present on production on 2026-08-25. So every render surface
 * selects the published token and maps it through `ppm2BasisFromToken`. An
 * earlier draft of this file mirrored the SQL in TS on the premise that the two
 * snapshots lacked the column; that premise was false, and a second definition
 * of the basis is exactly what this program exists to remove.
 *
 * `ppm2Basis` therefore survives for the ONE caller with no published column to
 * read: the estimation trace panel, whose rounds are a JSONB filter spec, not a
 * row of a relation. It is a line-for-line mirror of the SQL function and
 * `measure.test.ts` pins it against the SQL truth table — but a surface that
 * CAN read `price_per_m2_basis` must, so the two can never drift.
 *
 * Import direction: this module imports nothing from `filters.ts` (which imports
 * `format.ts`, which imports this) — `ppm2BasisOfCohort` takes the two fields it
 * needs structurally, so a `ListingFilters` still passes without a cycle.
 */

/* The render-side basis. 'mixed' is not a row state — a single listing always
 * has exactly one basis or none. It is what a COHORT resolves to when it spans
 * more than one, and it is the state that must never be given a blanket unit:
 * sale medians run ~91 535 Kč/m² against rent's ~319 Kč/m²/měs, a 300x category
 * error if the two share a label or an axis. */
export type Ppm2Basis = 'sale' | 'rent' | 'land' | 'mixed';

/* What a single row can resolve to. */
export type Ppm2RowBasis = Exclude<Ppm2Basis, 'mixed'>;

/* The exact strings `price_per_m2_basis` can hold (migration 425). NULL is the
 * fourth state and is spelled `null` here rather than given a token. */
export type Ppm2BasisToken =
  | 'sale_capital_czk_m2'
  | 'rent_monthly_czk_m2'
  | 'land_capital_czk_m2';

/* The four-token server vocabulary (migration 425). Kept here so the mapping
 * between what Postgres publishes and what this app renders is one table, not a
 * string comparison scattered across components. It is also what crosses the
 * wire to the annotations API: a service boundary carries the SQL vocabulary,
 * not this app's render-side shorthand. */
export const PPM2_BASIS_TOKEN: Record<Ppm2RowBasis, Ppm2BasisToken> = {
  sale: 'sale_capital_czk_m2',
  rent: 'rent_monthly_czk_m2',
  land: 'land_capital_czk_m2',
};

/* THE unit strings. Lifted verbatim from growthChoropleth's value labels — the
 * one place in this repo that already spelled the rent period correctly — so
 * the choropleth legend, a Browse pin, a table cell and a box-plot axis all say
 * the same thing. 'mixed' has NO unit on purpose: it is the absence of one. */
export const PPM2_UNIT: Record<Ppm2RowBasis, string> = {
  sale: 'Kč/m²',
  rent: 'Kč/m²/měs',
  land: 'Kč/m²',
};

/* The unit with the noun in front, for axis/legend captions that name the
 * quantity rather than suffixing a number. `sale` and `land` are both capital
 * Kč/m² but read differently: a land figure is per m² of PLOT (the Option-A
 * fork keeps area_m2 polymorphic), and saying so is the whole point. */
export const PPM2_VALUE_LABEL: Record<Ppm2RowBasis, string> = {
  sale: 'Cena Kč/m²',
  rent: 'Nájem Kč/m²/měs',
  land: 'Cena Kč/m² pozemku',
};

const CAPITAL_CATEGORY_TYPES: ReadonlyArray<string> = ['prodej', 'drazba', 'podil'];

/* Mirror of measure_price_per_m2_basis(category_main, category_type).
 *
 * Resolution is RENT-FIRST and the order matters: pozemek + pronajem is a
 * MONTHLY figure, and letting category_main win would file a rent under a
 * capital label. The capital list is an enumerated allowlist, not
 * "everything that is not pronajem", so an unknown future category_type
 * yields a visible gap instead of a silent guess — same as the SQL. */
export const ppm2Basis = (
  categoryMain: string | null | undefined,
  categoryType: string | null | undefined,
): Ppm2RowBasis | null => {
  if (categoryType === 'pronajem') return 'rent';
  if (categoryType == null || !CAPITAL_CATEGORY_TYPES.includes(categoryType)) return null;
  return categoryMain === 'pozemek' ? 'land' : 'sale';
};

/* The published `price_per_m2_basis` column / the `ppm2_basis` key the stats
 * RPCs return, mapped onto the render-side union. Anything unrecognised — an
 * older deploy that does not send the key, a NULL basis, a token this build has
 * never heard of — resolves to null, i.e. "no label", never to a guess. */
export const ppm2BasisFromToken = (
  token: string | null | undefined,
): Ppm2Basis | null => {
  if (token === 'mixed') return 'mixed';
  if (token === PPM2_BASIS_TOKEN.sale) return 'sale';
  if (token === PPM2_BASIS_TOKEN.rent) return 'rent';
  if (token === PPM2_BASIS_TOKEN.land) return 'land';
  return null;
};

/* The basis a COHORT can be labelled with, from its filter spec alone.
 *
 * Rule 22: `categoryType` is nullable and NULL means "no constraint" — the
 * Browse "Vše" pill and the Pipeline view both produce it — so the default
 * cohort is genuinely sale AND rent at once and resolves to 'mixed'. An
 * unrecognised categoryType is undecidable, not mixed, and yields null.
 *
 * Land is only separable when the cohort is EXACTLY pozemek; an empty
 * categoryMain means "every category", which includes pozemek, so it spans both
 * a floor-area and a plot-area denominator — the same 'mixed' the SQL aggregates
 * report via `count(distinct price_per_m2_basis) > 1`. */
export const ppm2BasisOfCohort = (
  f: {
    categoryMain: ReadonlyArray<string>;
    categoryType: string | null;
  },
): Ppm2Basis | null => {
  if (f.categoryType === 'pronajem') return 'rent';
  if (f.categoryType == null) return 'mixed';
  if (!CAPITAL_CATEGORY_TYPES.includes(f.categoryType)) return null;
  const kinds = f.categoryMain;
  if (kinds.length === 0) return 'mixed';
  if (kinds.every((k) => k === 'pozemek')) return 'land';
  if (kinds.some((k) => k === 'pozemek')) return 'mixed';
  return 'sale';
};

/* WHY A COHORT HAS MORE THAN ONE BASIS — there are exactly two causes, and they
 * need different instructions.
 *
 * The server's test is `count(distinct measure_price_per_m2_basis(...)) > 1`,
 * which is true for {sale, rent} AND for {sale, land}. Telling an operator who
 * has already picked "Prodej" to "choose one deal type" is unactionable: their
 * mix is the DENOMINATOR (a pozemek's m² is plot area, a byt's is floor area),
 * not the deal. The deal cause takes priority when both are present — fixing
 * the deal type is the first step either way. */
export type MixedBasisCause = 'deal' | 'denominator';

export const mixedBasisCause = (
  f: {
    categoryMain: ReadonlyArray<string>;
    categoryType: string | null;
  },
): MixedBasisCause => (f.categoryType === null ? 'deal' : 'denominator');

/* The instruction that actually clears the mix, one sentence per cause, shared
 * so the box-plot refusal and the withheld percentile card cannot disagree. */
export const MIXED_BASIS_HINT: Record<MixedBasisCause, string> = {
  deal: 'Smíšený základ (prodej + pronájem) — zvolte jeden typ nabídky',
  denominator:
    'Smíšený základ (podlahová plocha + plocha pozemku) — vylučte pozemky',
};

/* THE PERIOD OF AN ABSOLUTE PRICE, from the cohort's deal type alone.
 *
 * Deliberately NOT `ppm2BasisOfCohort`: that function's 'mixed' also covers a
 * sale+land cohort, which is a per-m² problem only — a plot's asking price and
 * a flat's asking price are both plain capital Kč and pool perfectly well. What
 * an absolute price cannot survive is mixing a MONTHLY rent with a capital sum,
 * and that is decided by `category_type` and nothing else.
 *
 * It is also not derived from the measure's basis: `ppm2_basis` is computed
 * only over rows that HAVE a measure, so a rent cohort whose rows are all under
 * the 1 000 Kč rent floor or carry a NULL area publishes a NULL basis while its
 * prices are still monthly. The period is a property of the cohort spec. */
export type PricePeriod = 'capital' | 'monthly' | 'mixed';

export const PRICE_PERIOD_UNIT: Record<Exclude<PricePeriod, 'mixed'>, string> = {
  capital: 'Kč',
  monthly: 'Kč / měs',
};

export const pricePeriodOfCohort = (
  categoryType: string | null | undefined,
): PricePeriod | null => {
  if (categoryType === 'pronajem') return 'monthly';
  if (categoryType == null) return 'mixed';
  if (!CAPITAL_CATEGORY_TYPES.includes(categoryType)) return null;
  return 'capital';
};

/* What an `area_m2` on this row MEANS. The Option-A fork kept the column
 * polymorphic — floor area for byt/dum/komerční, PLOT area for pozemek — so the
 * denominator carries a basis of its own, and a surface with room to say which
 * one should say it. */
export type AreaKind = 'usable' | 'plot';

export const areaKindOf = (categoryMain: string | null | undefined): AreaKind =>
  categoryMain === 'pozemek' ? 'plot' : 'usable';
