import { describe, expect, it, vi } from 'vitest';
import { revalidateCollections } from './collectionCache';
import { BROWSE_QUERY_KEYS } from './browseInvalidation';
import { curationKeys } from './queries';

describe('collection revalidation contract', () => {
  it('invalidates exactly the two global keys when no collection is named', () => {
    /* The shared member map is pinned first on purpose: every membership writer
     * but the save menu had hand-typed a key list that omitted it, which is the
     * drift this helper exists to end (docs/architecture.md rule 18). */
    const invalidateQueries = vi.fn();
    revalidateCollections({ invalidateQueries } as never);
    expect(invalidateQueries.mock.calls.map(([arg]) => arg.queryKey)).toEqual([
      curationKeys.propertyCollectionMembers,
      curationKeys.collections,
    ]);
  });

  it("adds the named collection's own page when the write touches one", () => {
    const invalidateQueries = vi.fn();
    revalidateCollections({ invalidateQueries } as never, { collection_id: 7 });
    expect(invalidateQueries.mock.calls.map(([arg]) => arg.queryKey)).toEqual([
      curationKeys.propertyCollectionMembers,
      curationKeys.collections,
      curationKeys.collection(7),
    ]);
  });

  /* When Browse is scoped to collections, membership IS the cohort: a removal
   * has to drop the row from the list, not just un-fill a glyph. */
  it('re-reads the Browse surfaces only when membership is the cohort', () => {
    const off = vi.fn();
    revalidateCollections({ invalidateQueries: off } as never, { collection_id: 7 });
    expect(off).toHaveBeenCalledTimes(3);

    const on = vi.fn();
    revalidateCollections({ invalidateQueries: on } as never, { cohortScoped: true });
    expect(on.mock.calls.map(([arg]) => arg.queryKey)).toEqual([
      curationKeys.propertyCollectionMembers,
      curationKeys.collections,
      ...BROWSE_QUERY_KEYS.map((k) => [k]),
    ]);
  });
});
