/* The board's row -> card projection, as a pure function.
 *
 * Lifted out of fetchPipelineBoard's queryFn so the mapping can be tested
 * without a network layer. W5 moved the pipeline/property join server-side
 * (pipeline_board_public, migration 417) — this now projects ONE already-
 * joined row per card instead of composing two separate arrays. The shape is
 * otherwise unchanged from the version that lived inline — only the cover URL
 * and the broker block are gone, because those are decorations and arrive
 * through lib/hydration instead. */

import type { PipelineBoardCard } from '@/lib/types';

/* One row of pipeline_board_public — the pipeline fields plus every property
 * display field the board can filter, sort, place or link on, already joined
 * server-side. Kept as one string so the select list and the projection below
 * can never drift apart. Column list mirrors the view's definition exactly. */
export const PIPELINE_BOARD_COLS =
  'property_id, stage_id, board_position, entered_stage_at, added_at, ' +
  'sreality_id, source, source_id_native, listing_id, category_main, ' +
  /* W3: `street` came out, `display_label` went in -- the board's place line is
   * the one server-composed label (migration 503), not a locality/obec/street
   * assembly the board did itself. W3 S4 (migration 506) took the rest of the
   * legacy place text with it: `district`, `place_search_text`, `locality`,
   * `okres` and `region` were the retired chip predicate's ILIKE inputs, and
   * once the predicate became four codes they were carried onto every card and
   * read by nothing. */
  'display_label, disposition, subtype, area_m2, price_czk, mf_gross_yield_pct, ' +
  'total_price_change_pct, price_change_count, obec_id, okres_id, region_id, ' +
  /* W3 S3: the fourth chip level. The board filters its cards in the browser
   * (matchesDistricts), so it needs the SAME four codes the server-side
   * predicate uses or a `cast_obce` chip would match on Browse and not here. */
  'cast_obce_id, ' +
  /* The town, and ONLY the town: the "Mesto A-Z" sort orders by it because the
   * label leads with the street when there is one (lib/pipelineSort). W4-a
   * re-sourced this column from `listing_location.obec_name` (migration 507) --
   * same name, same type, same sort; W4-c (migration 508) then dropped the
   * legacy `properties.obec` behind it. */
  'obec, is_active, ' +
  /* Migration 425 widened the view for exactly this: the board is deal-agnostic
   * by rule 22 (a card can be added from any cohort, and the pipeline scope is
   * `?pipeline=any`), so two cards in one column can be an 18 000 Kč/měs rent
   * and an 18 000 Kč sale. Without category_type they render the identical
   * string; without the measure + its label there is no per-m² figure at all. */
  'category_type, price_per_m2, price_per_m2_basis';

export interface PipelineBoardRow {
  property_id: number;
  stage_id: number;
  board_position: number;
  entered_stage_at: string;
  added_at: string;
  sreality_id: number | null;
  source: string | null;
  source_id_native: string | null;
  listing_id: number | null;
  category_main: string | null;
  display_label: string | null;
  disposition: string | null;
  subtype: string | null;
  area_m2: number | null;
  price_czk: number | null;
  mf_gross_yield_pct: number | null;
  // numeric arrives from PostgREST as a string on some paths — coerced below.
  total_price_change_pct: number | string | null;
  price_change_count: number | string | null;
  obec_id: number | null;
  okres_id: number | null;
  region_id: number | null;
  cast_obce_id: number | null;
  obec: string | null;
  is_active: boolean | null;
  category_type: string | null;
  price_per_m2: number | null;
  price_per_m2_basis: string | null;
}

export function composePipelineCards(
  rows: readonly PipelineBoardRow[],
): PipelineBoardCard[] {
  return rows.map((r) => ({
    property_id: r.property_id,
    stage_id: r.stage_id,
    board_position: r.board_position,
    entered_stage_at: r.entered_stage_at,
    added_at: r.added_at,
    sreality_id: r.sreality_id,
    source: r.source,
    source_id_native: r.source_id_native,
    listing_id: r.listing_id,
    category_main: r.category_main,
    display_label: r.display_label,
    disposition: r.disposition,
    subtype: r.subtype,
    area_m2: r.area_m2,
    price_czk: r.price_czk,
    mf_gross_yield_pct: r.mf_gross_yield_pct,
    total_price_change_pct:
      r.total_price_change_pct == null ? null : Number(r.total_price_change_pct),
    price_change_count:
      r.price_change_count == null ? null : Number(r.price_change_count),
    obec_id: r.obec_id,
    okres_id: r.okres_id,
    region_id: r.region_id,
    cast_obce_id: r.cast_obce_id,
    obec: r.obec,
    is_active: r.is_active ?? true,
    category_type: r.category_type,
    price_per_m2: r.price_per_m2,
    price_per_m2_basis: r.price_per_m2_basis,
  }));
}
