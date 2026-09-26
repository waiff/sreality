/* THE MF "Cenová mapa nájemného" reference-rent result — its type, and the ONE
 * rule for what a surface shows of it.
 *
 * One jsonb, wherever it is stored or served: `properties_public.mf_reference_rent`
 * (the listing page; the extension lookup serves the same column) and
 * `estimation_runs.reference_rent` (frozen per run, rule 12). Every surface
 * decides what to render by the SHAPE of that one value — never by its status
 * code and never with a sentence of its own: the reason texts are written once,
 * in SQL, and arrive here as `note`. Surfaces differ only in how much of the
 * shape they have room for.
 *
 * Zero dependencies, like brand.ts, so the Chrome extension imports THIS file
 * instead of keeping a second copy of the rule. Keep it that way: no `@/`
 * aliases, no React, no DOM.
 *
 * Every `*_per_m2` figure is CZK per m² per MONTH — this is a rent map. */

export interface ReferenceRentAdjustment {
  attribute: string;
  czk_per_m2: number;
}

/* The ministry's published range, for a flat whose location is known only to
 * town level in a town priced per katastr. `per_m2_*` is the published range
 * of the reference flat's rate across the town's katastry (the range twin of
 * `base_per_m2`); `rent_*_czk` add this flat's `adjustments` before × area.
 * The yields exist only for a sale flat with a usable price. */
export interface ReferenceRentRange {
  per_m2_min: number;
  per_m2_max: number;
  rent_min_czk: number;
  rent_max_czk: number;
  yield_min_pct?: number | null;
  yield_max_pct?: number | null;
}

/* The breakdown keys below `territory` describe a VALUE; a range or a reason
 * carries them only in part. Read the result through `mfShape`, which is what
 * decides which of them a surface may use. `status`/`note`/`range` are absent
 * on every result stored before the read-time measure, which is a value. */
export interface ReferenceRent {
  territory: {
    ruian_code: number;
    level: 'ku' | 'obec';
    name: string;
    kraj: string | null;
  };
  vk: number;
  is_novostavba: boolean;
  source_revision: number;
  source_date?: string | null;
  base_per_m2: number;
  adjustments: ReferenceRentAdjustment[];
  adjustments_sum_per_m2?: number;
  total_per_m2: number;
  area_m2: number;
  monthly_rent_czk: number;
  status?: string | null;
  note?: string | null;
  range?: ReferenceRentRange | null;
}

export type MfShape =
  | { kind: 'value'; ref: ReferenceRent }
  | { kind: 'range'; ref: ReferenceRent; range: ReferenceRentRange; note: string | null }
  | { kind: 'note'; note: string }
  | { kind: 'none' };

/* value → the breakdown; range → the published range (+ its note, the (i));
 * note → the reason alone; none → render nothing. */
export function mfShape(ref: ReferenceRent | null | undefined): MfShape {
  if (ref == null) return { kind: 'none' };
  if (ref.monthly_rent_czk != null) return { kind: 'value', ref };
  if (ref.range != null) {
    return { kind: 'range', ref, range: ref.range, note: ref.note ?? null };
  }
  if (ref.note != null && ref.note !== '') return { kind: 'note', note: ref.note };
  return { kind: 'none' };
}
