/* THE per-m² basis on the frontend: one vocabulary, one unit string, one place.
 *
 * NORTH STAR — one measure, one definition, one label. The NUMBER always comes
 * from the server (migration 425's `measure_price_per_m2`, published as
 * `price_per_m2` on all six read relations); nothing here re-derives price/area.
 * What lives here is the LABEL half: which basis a figure is on, and the Czech
 * unit that basis is spelled with.
 *
 * WHY THE BASIS IS RESOLVED HERE AND NOT ONLY READ FROM THE SERVER COLUMN.
 * `measure_price_per_m2_basis(category_main, category_type)` is published as
 * `price_per_m2_basis` on listings_public / properties_public /
 * listing_feed_public / pipeline_board_public — but NOT on the two derived read
 * models the Browse map and list actually read. `browse_list` and
 * `properties_map_mv` are `select * from browse_projection` snapshots, and they
 * only gain the column when their rebuild runs (migration 425 § 8; verified
 * absent on production 2026-08-25 while the views already carry it). Selecting a
 * column a snapshot has not picked up yet is a hard PostgREST 400, so the SPA
 * would be coupled to a rebuild it does not control. `ppm2Basis` is therefore a
 * line-for-line mirror of that SQL function over the SAME two inputs, which both
 * snapshots DO carry. `measure.test.ts` pins the mirror against the SQL truth
 * table, and `ppm2BasisFromToken` maps the published column onto the same union
 * so the two paths can never mean different things.
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

/* The four-token server vocabulary (migration 425). Kept here so the mapping
 * between what Postgres publishes and what this app renders is one table, not a
 * string comparison scattered across components. */
export const PPM2_BASIS_TOKEN: Record<Ppm2RowBasis, string> = {
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

/* What an `area_m2` on this row MEANS. The Option-A fork kept the column
 * polymorphic — floor area for byt/dum/komerční, PLOT area for pozemek — so the
 * denominator carries a basis of its own, and a surface with room to say which
 * one should say it. */
export type AreaKind = 'usable' | 'plot';

export const areaKindOf = (categoryMain: string | null | undefined): AreaKind =>
  categoryMain === 'pozemek' ? 'plot' : 'usable';
