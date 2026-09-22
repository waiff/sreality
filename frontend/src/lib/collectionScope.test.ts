import { describe, expect, it } from 'vitest';

import { propertyIdsInCollections } from './collectionScope';

/* The case that must never blur: "nothing selected" (no constraint) and
 * "selected, nothing matched" (zero rows) are different answers. */
describe('collection scope', () => {
  const members = new Map([
    [1, [10, 11]],
    [2, [11]],
    [3, [12]],
  ]);

  it('is no constraint when nothing is selected', () => {
    expect(propertyIdsInCollections(members, [])).toBeNull();
  });

  it('takes a property that is in ANY selected collection (OR)', () => {
    expect(propertyIdsInCollections(members, [11, 12])).toEqual([1, 2, 3]);
    expect(propertyIdsInCollections(members, [10])).toEqual([1]);
  });

  it('resolves a selection nothing is in to ZERO properties, not to null', () => {
    expect(propertyIdsInCollections(members, [99])).toEqual([]);
  });
});
