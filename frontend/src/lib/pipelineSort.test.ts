import { describe, expect, it } from 'vitest';
import {
  DEFAULT_PIPELINE_SORT,
  PIPELINE_SORT_OPTIONS,
  sortPipelineCards,
  type PipelineSort,
} from './pipelineSort';
import type { PipelineBoardCard } from './types';

const card = (over: Partial<PipelineBoardCard> = {}): PipelineBoardCard => ({
  property_id: 1,
  stage_id: 1,
  board_position: 0,
  entered_stage_at: '2026-06-01T00:00:00Z',
  added_at: '2026-05-01T00:00:00Z',
  sreality_id: null,
  source: 'sreality',
  source_id_native: '1',
  listing_id: 1,
  category_main: 'byt',
  street: null,
  district: null,
  disposition: null,
  subtype: null,
  area_m2: null,
  price_czk: null,
  mf_gross_yield_pct: null,
  total_price_change_pct: null,
  price_change_count: null,
  obec_id: null,
  obec: null,
  locality: null,
  okres_id: null,
  region_id: null,
  place_search_text: null,
  okres: null,
  region: null,
  is_active: true,
  ...over,
});

const ids = (rows: PipelineBoardCard[]) => rows.map((r) => r.property_id);
const by = (rows: PipelineBoardCard[], s: PipelineSort) => ids(sortPipelineCards(rows, s));

describe('sort options', () => {
  it('defaults to the manual board order', () => {
    expect(DEFAULT_PIPELINE_SORT.field).toBe('board_position');
    expect(PIPELINE_SORT_OPTIONS[0].value).toBe('manual');
  });

  it('exposes unique URL tokens', () => {
    const values = PIPELINE_SORT_OPTIONS.map((o) => o.value);
    expect(new Set(values).size).toBe(values.length);
  });

  it('covers every requested key', () => {
    const fields = new Set(PIPELINE_SORT_OPTIONS.map((o) => o.field));
    for (const f of [
      'board_position',
      'added_at',
      'entered_stage_at',
      'price_czk',
      'total_price_change_pct',
      'city',
    ]) {
      expect(fields.has(f as never)).toBe(true);
    }
  });
});

describe('sorting', () => {
  it('orders by date added, newest first', () => {
    const rows = [
      card({ property_id: 1, added_at: '2026-05-01T00:00:00Z' }),
      card({ property_id: 2, added_at: '2026-07-01T00:00:00Z' }),
      card({ property_id: 3, added_at: '2026-06-01T00:00:00Z' }),
    ];
    expect(by(rows, { field: 'added_at', direction: 'desc' })).toEqual([2, 3, 1]);
  });

  it('"longest in stage" is entered_stage_at ascending', () => {
    const rows = [
      card({ property_id: 1, entered_stage_at: '2026-07-01T00:00:00Z' }),
      card({ property_id: 2, entered_stage_at: '2026-05-01T00:00:00Z' }),
    ];
    expect(by(rows, { field: 'entered_stage_at', direction: 'asc' })).toEqual([2, 1]);
  });

  it('orders by price with unpriced cards last in BOTH directions', () => {
    const rows = [
      card({ property_id: 1, price_czk: 5_000_000 }),
      card({ property_id: 2, price_czk: null }),
      card({ property_id: 3, price_czk: 12_000_000 }),
    ];
    expect(by(rows, { field: 'price_czk', direction: 'desc' })).toEqual([3, 1, 2]);
    expect(by(rows, { field: 'price_czk', direction: 'asc' })).toEqual([1, 3, 2]);
  });

  it('ranks the deepest price cut first when ascending', () => {
    const rows = [
      card({ property_id: 1, total_price_change_pct: -2.5 }),
      card({ property_id: 2, total_price_change_pct: 8 }),
      card({ property_id: 3, total_price_change_pct: -14 }),
      card({ property_id: 4, total_price_change_pct: 0 }),
    ];
    expect(by(rows, { field: 'total_price_change_pct', direction: 'asc' })).toEqual([3, 1, 4, 2]);
    expect(by(rows, { field: 'total_price_change_pct', direction: 'desc' })).toEqual([2, 4, 1, 3]);
  });

  it('sinks never-observed-moving cards below the movers in BOTH directions', () => {
    // NULL is "fewer than two priced snapshots", not "0% change" — the majority
    // of live cards. It must not outrank an observed 0%, and must not head the
    // biggest-risers list either.
    const rows = [
      card({ property_id: 1, total_price_change_pct: null }),
      card({ property_id: 2, total_price_change_pct: 0 }),
      card({ property_id: 3, total_price_change_pct: -6 }),
    ];
    expect(by(rows, { field: 'total_price_change_pct', direction: 'asc' })).toEqual([3, 2, 1]);
    expect(by(rows, { field: 'total_price_change_pct', direction: 'desc' })).toEqual([2, 3, 1]);
  });

  it('orders by city using Czech collation on the label the card shows', () => {
    // placePrimary prefers the free-text locality; 'Č' must sort after 'C'.
    const rows = [
      card({ property_id: 1, locality: 'Zlín', okres: 'Zlín-okres' }),
      card({ property_id: 2, locality: 'Česká Lípa', okres: 'Česká Lípa-okres' }),
      card({ property_id: 3, locality: 'Cheb', okres: 'Cheb-okres' }),
    ];
    expect(by(rows, { field: 'city', direction: 'asc' })).toEqual([2, 3, 1]);
  });

  it('falls back to the geo obec when the locality is merely the okres name', () => {
    // The Bazoš "Jihlava"-for-Telč case: the card shows Telč, so it sorts as Telč.
    const rows = [
      card({ property_id: 1, locality: 'Jihlava', okres: 'Jihlava', obec: 'Telč' }),
      card({ property_id: 2, locality: 'Slaný', okres: 'Kladno' }),
    ];
    expect(by(rows, { field: 'city', direction: 'asc' })).toEqual([2, 1]);
  });

  it('is stable across colliding board_positions — the live data condition', () => {
    // board_position is assigned max+1 in the ENTRY stage and never renumbered
    // on a stage move, so duplicates within a stage are normal. Without the
    // property_id tiebreak these would reshuffle between refetches.
    const rows = [
      card({ property_id: 9, board_position: 4 }),
      card({ property_id: 4, board_position: 4 }),
      card({ property_id: 7, board_position: 4 }),
    ];
    expect(by(rows, DEFAULT_PIPELINE_SORT)).toEqual([4, 7, 9]);
    expect(by([...rows].reverse(), DEFAULT_PIPELINE_SORT)).toEqual([4, 7, 9]);
  });
});
