/* The board's row -> card projection, as a pure function.
 *
 * Lifted out of fetchPipelineBoard's queryFn so the mapping can be tested
 * without a network layer, and so the queryFn reads as what it now is: two
 * reads and a join. The shape is unchanged from the version that lived inline
 * — only the cover URL and the broker block are gone, because those are
 * decorations and arrive through lib/hydration instead. */

import type { PipelineBoardCard } from '@/lib/types';

/* The pipeline half of a card: one row of property_pipeline_public. */
export interface PipelineBoardRow {
  property_id: number;
  stage_id: number;
  board_position: number;
  entered_stage_at: string;
  added_at: string;
}

/* Everything the board can filter, sort, place or link on. Kept as one string
 * so the select list and the projection below can never drift apart. */
export const PIPELINE_PROPERTY_COLS =
  'property_id, sreality_id, source, source_id_native, listing_id, category_main, ' +
  'street, district, disposition, subtype, area_m2, price_czk, mf_gross_yield_pct, ' +
  'total_price_change_pct, price_change_count, obec_id, okres_id, region_id, ' +
  'place_search_text, obec, locality, okres, region, is_active';

export function composePipelineCards(
  rows: readonly PipelineBoardRow[],
  properties: ReadonlyArray<Record<string, unknown>>,
): PipelineBoardCard[] {
  const byId = new Map<number, Record<string, unknown>>(
    properties.map((p) => [p.property_id as number, p]),
  );

  return rows.map((r) => {
    const p = byId.get(r.property_id);
    return {
      property_id: r.property_id,
      stage_id: r.stage_id,
      board_position: r.board_position,
      entered_stage_at: r.entered_stage_at,
      added_at: r.added_at,
      sreality_id: (p?.sreality_id as number | null) ?? null,
      source: (p?.source as string | null) ?? null,
      source_id_native: (p?.source_id_native as string | null) ?? null,
      listing_id: (p?.listing_id as number | null) ?? null,
      category_main: (p?.category_main as string | null) ?? null,
      street: (p?.street as string | null) ?? null,
      district: (p?.district as string | null) ?? null,
      disposition: (p?.disposition as string | null) ?? null,
      subtype: (p?.subtype as string | null) ?? null,
      area_m2: (p?.area_m2 as number | null) ?? null,
      price_czk: (p?.price_czk as number | null) ?? null,
      mf_gross_yield_pct: (p?.mf_gross_yield_pct as number | null) ?? null,
      // numeric arrives from PostgREST as a string on some paths — coerce once,
      // here, so no consumer has to guess (the llm_cost_daily reader does the same).
      total_price_change_pct:
        p?.total_price_change_pct == null ? null : Number(p.total_price_change_pct),
      price_change_count:
        p?.price_change_count == null ? null : Number(p.price_change_count),
      obec_id: (p?.obec_id as number | null) ?? null,
      okres_id: (p?.okres_id as number | null) ?? null,
      region_id: (p?.region_id as number | null) ?? null,
      place_search_text: (p?.place_search_text as string | null) ?? null,
      obec: (p?.obec as string | null) ?? null,
      locality: (p?.locality as string | null) ?? null,
      okres: (p?.okres as string | null) ?? null,
      region: (p?.region as string | null) ?? null,
      is_active: (p?.is_active as boolean | null) ?? true,
    };
  });
}
