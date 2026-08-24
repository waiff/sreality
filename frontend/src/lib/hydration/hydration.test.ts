/* The hydration layer's two invariants.
 *
 * 1. Its cache keys must not collide with the pipeline's, because two live
 *    invalidations sweep by PREFIX: revalidatePipeline fires
 *    invalidateQueries(['pipeline','board']) after every card write, and the
 *    stage editor fires invalidateQueries(['pipeline']) wholesale. If a
 *    decoration key ever sat under either, every drag would refetch every
 *    thumbnail and every broker on the board — the split that makes the board
 *    fast would make it slower than the blocking chain it replaced. This is
 *    standing constraint 1 of the sprint, and it is cheap to violate by
 *    accident, so it is pinned here rather than trusted to review.
 *
 * 2. Keys are a property of the id SET, not of the array that arrived — so a
 *    re-sort or a re-render with the same cards is a cache hit.
 */

import { describe, expect, it } from 'vitest';

import { pipelineKeys } from '@/lib/queries';

import { hydrationKeys, idsKey, HYDRATION_NAMESPACE } from './keys';
import { makeHydration } from './CardHydration';

/* React Query's own prefix rule: a key is invalidated by `prefix` when every
 * element of `prefix` deep-equals the key's element at the same index. */
const matchesPrefix = (key: readonly unknown[], prefix: readonly unknown[]): boolean =>
  prefix.every((p, i) => JSON.stringify(p) === JSON.stringify(key[i]));

describe('hydration key namespace', () => {
  const decorationKeys = [
    hydrationKeys.covers([1, 2, 3]),
    hydrationKeys.brokers([1, 2, 3]),
  ];

  it('is disjoint from every pipeline invalidation prefix', () => {
    const sweeps: ReadonlyArray<readonly unknown[]> = [
      ['pipeline'],            // StageManager's wholesale invalidation
      pipelineKeys.board,      // revalidatePipeline, after every card write
      pipelineKeys.members,
      pipelineKeys.stages,
      pipelineKeys.card(42),
    ];
    for (const key of decorationKeys) {
      for (const sweep of sweeps) {
        expect(
          matchesPrefix(key, sweep),
          `${JSON.stringify(key)} must not be swept by ${JSON.stringify(sweep)}`,
        ).toBe(false);
      }
    }
  });

  it('roots every decoration under the hydration namespace', () => {
    for (const key of decorationKeys) expect(key[0]).toBe(HYDRATION_NAMESPACE);
    expect(HYDRATION_NAMESPACE).not.toBe('pipeline');
  });

  it('keys on the id SET, so order and duplicates do not re-key', () => {
    expect(hydrationKeys.covers([3, 1, 2])).toEqual(hydrationKeys.covers([1, 2, 3]));
    expect(hydrationKeys.covers([1, 1, 2])).toEqual(hydrationKeys.covers([1, 2]));
    expect(idsKey([10, 2])).toBe('2,10'); // numeric, not lexicographic
  });

  it('separates covers from brokers over the same cohort', () => {
    expect(hydrationKeys.covers([1])).not.toEqual(hydrationKeys.brokers([1]));
  });
});

describe('makeHydration lookup', () => {
  const covers = new Map([[7, 'https://img/7.jpg']]);
  const brokers = new Map([
    [7, { broker_id: 3, display_name: 'A', firm_label: null, email: null,
          phone: null, has_email: false, has_phone: false }],
  ]);

  it('resolves a decoration by listing id', () => {
    const h = makeHydration(covers, brokers);
    expect(h.coverFor(7)).toBe('https://img/7.jpg');
    expect(h.brokerFor(7)?.broker_id).toBe(3);
  });

  it('returns null for a missing entry and for a null listing id', () => {
    const h = makeHydration(covers, brokers);
    // A post-Gate-2 representative can have a null surrogate; it must degrade
    // to "no decoration", never throw and never key on undefined.
    expect(h.coverFor(null)).toBeNull();
    expect(h.coverFor(undefined)).toBeNull();
    expect(h.coverFor(999)).toBeNull();
    expect(h.brokerFor(null)).toBeNull();
    expect(h.brokerFor(999)).toBeNull();
  });

  it('carries the pending flags so a card can reserve space', () => {
    expect(makeHydration(covers, brokers, true, false).coversPending).toBe(true);
    expect(makeHydration(covers, brokers, false, true).brokersPending).toBe(true);
  });
});
