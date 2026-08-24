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
  'street, district, disposition, subtype, area_m2, price_czk, mf_gross_yield_pct, ' +
  'total_price_change_pct, price_change_count, obec_id, okres_id, region_id, ' +
  'place_search_text, obec, locality, okres, region, is_active';

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
  street: string | null;
  district: string | null;
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
  place_search_text: string | null;
  obec: string | null;
  locality: string | null;
  okres: string | null;
  region: string | null;
  is_active: boolean | null;
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
    street: r.street,
    district: r.district,
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
    place_search_text: r.place_search_text,
    obec: r.obec,
    locality: r.locality,
    okres: r.okres,
    region: r.region,
    is_active: r.is_active ?? true,
  }));
}
