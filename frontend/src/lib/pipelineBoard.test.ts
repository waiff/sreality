/* fetchPipelineBoard's read budget.
 *
 * This file used to pin broker-enrichment ISOLATION: the two /brokers reads ran
 * inside the board's queryFn, so a failure there could take the whole board
 * down (the 2026-07-20 incident), and the fix was a hand-written `.catch`
 * swallow that the tests then held in place.
 *
 * The hydration sprint removed the reason for that test rather than the test's
 * subject: brokers and cover images are no longer part of this queryFn at all.
 * They are independent queries in lib/hydration, so isolation is now structural
 * — a failed broker read cannot touch the board because it is not on the
 * board's promise — and the swallow is gone with it (a failure is a real error
 * again, visible to React Query and the global toast, instead of being
 * converted into "this listing has no broker" forever).
 *
 * W5 moved the pipeline/property join server-side (pipeline_board_public,
 * migration 417), so what is pinned here now is ONE relation read touching NO
 * enrichment source — down from the two-relation client-side join W1/W2b left
 * in place, itself down from six serialized cross-origin round trips before a
 * single column could paint.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

const h = vi.hoisted(() => ({
  tables: {} as Record<string, Array<Record<string, unknown>>>,
  reads: [] as string[],
}));

vi.mock('./supabase', () => {
  const builder = (relation: string) => {
    h.reads.push(relation);
    const rows = () => h.tables[relation] ?? [];
    const b: Record<string, unknown> = {
      select: () => b,
      order: () => b,
      in: () => b,
      eq: () => b,
      limit: () => b,
      range: (from: number, to: number) =>
        Promise.resolve({ data: rows().slice(from, to + 1), error: null }),
      then: (resolve: (r: unknown) => unknown) =>
        resolve({ data: rows(), error: null }),
    };
    return b;
  };
  return { supabase: { from: (relation: string) => builder(relation) } };
});

/* Mocked so that ANY call is a test failure, not a network attempt. */
vi.mock('./brokers', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./brokers')>()),
  fetchListingBrokersByIds: vi.fn(),
  fetchBrokersByIds: vi.fn(),
}));

import * as brokers from './brokers';
import { fetchPipelineBoard } from './queries';

beforeEach(() => {
  h.reads = [];
  vi.clearAllMocks();
  h.tables.pipeline_board_public = [
    {
      property_id: 42,
      stage_id: 1,
      board_position: 0,
      entered_stage_at: '2026-06-01T00:00:00Z',
      added_at: '2026-05-20T00:00:00Z',
      sreality_id: 900,
      listing_id: 7,
      source: 'sreality',
      source_id_native: '900',
      category_main: 'byt',
      price_czk: 5_000_000,
      area_m2: 62,
      is_active: true,
      obec: 'Brno',
      total_price_change_pct: '-3.5',
      price_change_count: '2',
    },
  ];
  h.tables.images_public = [];
});

describe('fetchPipelineBoard read budget', () => {
  it('reads exactly one relation: pipeline_board_public', async () => {
    await fetchPipelineBoard();
    /* fetchAllRows pays one extra terminating page (it stops only on an empty
       page) unless the count-exact fast path applies — deduplicate to
       relations so this test pins the SHAPE (which sources are touched) and
       not the pagination detail. */
    expect([...new Set(h.reads)]).toEqual(['pipeline_board_public']);
  });

  it('never reads images or brokers — those are decorations', async () => {
    await fetchPipelineBoard();
    expect(h.reads).not.toContain('images_public');
    expect(brokers.fetchListingBrokersByIds).not.toHaveBeenCalled();
    expect(brokers.fetchBrokersByIds).not.toHaveBeenCalled();
  });

  it('projects the structural fields a card renders and sorts on', async () => {
    const board = await fetchPipelineBoard();
    expect(board).toHaveLength(1);
    expect(board[0]).toMatchObject({
      property_id: 42,
      stage_id: 1,
      listing_id: 7,
      source: 'sreality',
      price_czk: 5_000_000,
      obec: 'Brno',
      is_active: true,
    });
    // numerics arrive from PostgREST as strings on some paths — coerced once.
    expect(board[0].total_price_change_pct).toBe(-3.5);
    expect(board[0].price_change_count).toBe(2);
  });

  it('returns an empty board when the pipeline is empty', async () => {
    h.tables.pipeline_board_public = [];
    const board = await fetchPipelineBoard();
    expect(board).toEqual([]);
  });
});
