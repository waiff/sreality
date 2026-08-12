/* fetchPipelineBoard's broker-enrichment isolation.
 *
 * Lives in its own file because it needs `./supabase` and `./brokers` mocked,
 * and queries.test.ts's other cases are pure functions that must keep running
 * against the real modules.
 *
 * What is pinned: the two /brokers reads are an ENRICHMENT. A failure there must
 * degrade the broker block of a card and nothing else — stages, cards, images
 * and the board itself still render (the 2026-07-20 incident, where a broker
 * read took the whole board down). The 2026-08-12 repoint removed the
 * `brokerMaskExpected` branch that swallowed PostgREST's 42501 silently, so the
 * second half matters just as much: every failure is now logged, because a
 * board that shows "no broker" forever with no console signal is how the dark
 * state went unnoticed for a month in the first place.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const h = vi.hoisted(() => ({
  tables: {} as Record<string, Array<Record<string, unknown>>>,
}));

vi.mock('./supabase', () => {
  const builder = (relation: string) => {
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

vi.mock('./brokers', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./brokers')>()),
  fetchListingBrokersByIds: vi.fn(),
  fetchBrokersByIds: vi.fn(),
}));

import * as brokers from './brokers';
import { fetchPipelineBoard } from './queries';

beforeEach(() => {
  h.tables.property_pipeline_public = [
    {
      property_id: 42,
      stage_id: 1,
      board_position: 0,
      entered_stage_at: '2026-06-01T00:00:00Z',
      added_at: '2026-05-20T00:00:00Z',
    },
  ];
  h.tables.properties_public = [
    { property_id: 42, listing_id: 111, sreality_id: 111, street: 'Sadová', is_active: true },
  ];
  h.tables.images_public = [];
  vi.mocked(brokers.fetchListingBrokersByIds).mockResolvedValue(
    new Map([
      [
        111,
        {
          sreality_id: 111,
          listing_id: 111,
          broker_id: 7,
          broker_display_name: 'Jan Novák',
          broker_firm_label: 'RE/MAX',
        },
      ],
    ]),
  );
  vi.mocked(brokers.fetchBrokersByIds).mockResolvedValue(new Map());
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('fetchPipelineBoard broker enrichment', () => {
  it('hydrates the card broker when both reads succeed', async () => {
    vi.mocked(brokers.fetchBrokersByIds).mockResolvedValue(
      new Map([[7, { broker_id: 7, has_email: true } as brokers.BrokerPublic]]),
    );
    const board = await fetchPipelineBoard();
    expect(board).toHaveLength(1);
    expect(board[0].broker).toMatchObject({
      broker_id: 7,
      display_name: 'Jan Novák',
      email: null,
      has_email: true,
      has_phone: false,
    });
  });

  /* The 42501 that used to be "expected" can no longer happen (the API answers
     200 + masked columns), so a permission-shaped error is now an ordinary
     fault: still isolated, but no longer silent. */
  it('keeps the board and logs when the listing→broker read fails', async () => {
    vi.mocked(brokers.fetchListingBrokersByIds).mockRejectedValue(
      Object.assign(new Error('permission denied'), { code: '42501' }),
    );
    const board = await fetchPipelineBoard();
    expect(board).toHaveLength(1);
    expect(board[0].street).toBe('Sadová');
    expect(board[0].broker).toBeNull();
    expect(console.error).toHaveBeenCalled();
  });

  it('keeps the board and logs when the broker→contact read fails', async () => {
    vi.mocked(brokers.fetchBrokersByIds).mockRejectedValue(new Error('HTTP 500'));
    const board = await fetchPipelineBoard();
    expect(board).toHaveLength(1);
    // The name still comes from the first read; only the contact is missing.
    expect(board[0].broker).toMatchObject({
      display_name: 'Jan Novák',
      has_email: false,
      has_phone: false,
    });
    expect(console.error).toHaveBeenCalled();
  });
});
