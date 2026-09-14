/* The waterfall reader (migration 523).
 *
 * Two properties, and each is a way the page could quietly mislead the operator:
 *   - the steps come back in the store's order, with their sub-rows nested under
 *     the parent they actually name;
 *   - nothing here recomputes a number. The fixture below is production's shape
 *     (2026-09-14), so the arithmetic the store guarantees — `lost` = the previous
 *     chain step's count minus this one's, and every split partitioning its parent
 *     — is asserted on the payload rather than re-derived from it.
 */

import { describe, expect, it } from 'vitest';

import {
  groupWaterfall,
  waterfallRefreshedAt,
  type WaterfallRow,
} from './locationWaterfall';

const AT = '2026-09-14T19:25:00Z';

const row = (
  step_no: number,
  sub_no: number,
  step_key: string,
  kind: WaterfallRow['kind'],
  parent_key: string | null,
  n: number,
  lost: number | null,
): WaterfallRow => ({
  step_key,
  step_no,
  sub_no,
  kind,
  parent_key,
  label_cs: step_key,
  n,
  lost,
  share_pct: Math.round((n / 841428) * 100_000) / 1000,
  refreshed_at: AT,
});

/* Production, 2026-09-14. Deliberately NOT in step order — the store publishes an
 * order and the reader must impose it, not inherit whatever PostgREST returned. */
const ROWS: WaterfallRow[] = [
  row(6, 1, 'hidden_unresolved', 'split', 'hidden', 12054, null),
  row(1, 0, 'all_listings', 'chain', null, 841428, 0),
  row(5, 2, 'located_foreign', 'split', 'served_located', 45582, null),
  row(2, 0, 'not_served', 'deduction', 'all_listings', 87756, null),
  row(2, 1, 'not_served_no_verdict', 'split', 'not_served', 28162, null),
  row(2, 2, 'not_served_verdict_no_location', 'split', 'not_served', 41396, null),
  row(2, 3, 'not_served_located', 'split', 'not_served', 18198, null),
  row(3, 0, 'served', 'chain', null, 753672, 87756),
  row(4, 0, 'served_with_verdict', 'chain', null, 753658, 14),
  row(5, 0, 'served_located', 'chain', null, 741604, 12054),
  row(5, 1, 'located_town', 'split', 'served_located', 695130, null),
  row(5, 3, 'located_no_town', 'split', 'served_located', 892, null),
  row(6, 0, 'hidden', 'deduction', 'served', 12068, null),
  row(6, 2, 'hidden_pending', 'split', 'hidden', 14, null),
];

describe('groupWaterfall', () => {
  it('returns the six steps in the store order', () => {
    expect(groupWaterfall(ROWS).map((s) => s.row.step_key)).toEqual([
      'all_listings',
      'not_served',
      'served',
      'served_with_verdict',
      'served_located',
      'hidden',
    ]);
  });

  it('nests every sub-row under the step it names', () => {
    const byKey = new Map(groupWaterfall(ROWS).map((s) => [s.row.step_key, s]));
    expect(byKey.get('not_served')!.splits.map((r) => r.step_key)).toEqual([
      'not_served_no_verdict',
      'not_served_verdict_no_location',
      'not_served_located',
    ]);
    expect(byKey.get('served_located')!.splits.map((r) => r.step_key)).toEqual([
      'located_town',
      'located_foreign',
      'located_no_town',
    ]);
    expect(byKey.get('hidden')!.splits.map((r) => r.n)).toEqual([12054, 14]);
    /* A chain step with no split gets an empty list, never undefined. */
    expect(byKey.get('served')!.splits).toEqual([]);
  });

  it('carries the arithmetic the store wrote: lost = previous n − n', () => {
    const chain = groupWaterfall(ROWS)
      .map((s) => s.row)
      .filter((r) => r.kind === 'chain');
    expect(chain[0].lost).toBe(0);
    chain.slice(1).forEach((r, i) => {
      expect(r.lost).toBe(chain[i].n - r.n);
    });
  });

  it('every split partitions its parent exactly', () => {
    for (const { row: step, splits } of groupWaterfall(ROWS)) {
      if (splits.length === 0) continue;
      expect(splits.reduce((a, r) => a + r.n, 0)).toBe(step.n);
    }
  });

  it('the hidden set is the remainder of the served set', () => {
    const by = new Map(ROWS.map((r) => [r.step_key, r.n]));
    expect(by.get('hidden')).toBe(by.get('served')! - by.get('served_located')!);
    /* And it is a share of the DATABASE, not of itself — 1.4 %, not 100 %. */
    const hidden = ROWS.find((r) => r.step_key === 'hidden')!;
    expect(hidden.share_pct).toBeCloseTo(1.435, 2);
  });

  it('reports one refresh time for the whole chain', () => {
    expect(waterfallRefreshedAt(ROWS)).toBe(AT);
    expect(waterfallRefreshedAt([])).toBeNull();
  });
});
