import { describe, expect, it, vi } from 'vitest';
import { BROWSE_QUERY_KEYS, invalidateBrowseQueries } from './browseInvalidation';

describe('browse invalidation contract', () => {
  it('includes the header cohort count so it never lags after a merge', () => {
    // Regression guard: 'browse-count' was historically absent from the
    // hand-typed invalidation lists, so the header/tab total stayed stale after
    // every merge (finding #1, docs/design/browse-merge-consistency.md).
    expect(BROWSE_QUERY_KEYS).toContain('browse-count');
  });

  it('covers exactly the five Browse read surfaces', () => {
    expect([...BROWSE_QUERY_KEYS].sort()).toEqual([
      'browse-count',
      'cards',
      'map',
      'stats',
      'table',
    ]);
  });

  it('invalidates each key once as a prefix match', () => {
    const invalidateQueries = vi.fn();
    invalidateBrowseQueries({ invalidateQueries } as never);
    expect(invalidateQueries).toHaveBeenCalledTimes(BROWSE_QUERY_KEYS.length);
    for (const key of BROWSE_QUERY_KEYS) {
      expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: [key] });
    }
  });
});
