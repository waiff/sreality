import type { QueryKey } from '@tanstack/react-query';
import { describe, expect, it, vi } from 'vitest';

import { invalidateBrowseQueries } from './browseInvalidation';
import { browseKeys } from './browseKeys';
import { DEFAULT_FILTERS } from './filters';
import { DEFAULT_SORT } from './queries';

const roots = (keys: readonly QueryKey[]) => keys.map((k) => k[0]);

describe('browse invalidation contract', () => {
  it('includes the header cohort count so it never lags after a merge', () => {
    // Regression guard: 'browse-count' was historically absent from the
    // hand-typed invalidation lists, so the header/tab total stayed stale after
    // every merge (finding #1, docs/design/browse-merge-consistency.md).
    expect(roots(browseKeys.all)).toContain('browse-count');
  });

  it('covers exactly the six Browse read surfaces', () => {
    expect([...roots(browseKeys.all)].sort()).toEqual([
      'browse-count',
      'cards',
      'map',
      'no-price-count',
      'stats',
      'table',
    ]);
  });

  it('invalidates each key once as a prefix match', () => {
    const invalidateQueries = vi.fn();
    invalidateBrowseQueries({ invalidateQueries } as never);
    expect(invalidateQueries).toHaveBeenCalledTimes(browseKeys.all.length);
    for (const queryKey of browseKeys.all) {
      expect(invalidateQueries).toHaveBeenCalledWith({ queryKey });
    }
  });
});

/* The readers take their keys from the same table the sweep is built from, so a
 * new surface cannot be read under a key no merge reaches. */
describe('browse key factories', () => {
  const f = DEFAULT_FILTERS;
  const readers: QueryKey[] = [
    browseKeys.cards(f, DEFAULT_SORT),
    browseKeys.table(f, DEFAULT_SORT),
    browseKeys.map(f),
    browseKeys.stats(f),
    browseKeys.count(f),
    browseKeys.noPriceCount(f),
  ];

  it('roots every reader in the sweep', () => {
    const swept = roots(browseKeys.all);
    for (const key of readers) expect(swept).toContain(key[0]);
    expect(new Set(readers.map((k) => k[0])).size).toBe(browseKeys.all.length);
  });

  it('splits the sweep into the paged lists and the aggregates, nothing else', () => {
    expect([...roots(browseKeys.lists), ...roots(browseKeys.aggregates)].sort()).toEqual(
      [...roots(browseKeys.all)].sort(),
    );
  });

  it('keeps the filters in slot 1 of the paged lists, where the dismissal predicate reads them', () => {
    expect(browseKeys.cards(f, DEFAULT_SORT)[1]).toBe(f);
    expect(browseKeys.table(f, DEFAULT_SORT)[1]).toBe(f);
  });
});
