/* collectionCache — the optimistic patch behind every membership write.
 *
 * The bug class this guards: a patch that seeds a cache which had not loaded.
 * The shared members map answers "is ANY property filed", so writing one
 * property's membership into an absent map would leave every other property on
 * the screen looking unfiled until the refetch landed.
 */

import { describe, expect, it } from 'vitest';
import { QueryClient } from '@tanstack/react-query';

import { curationKeys } from './queries';
import { revalidateCollections, setMembership } from './collectionCache';

const sharedKey = curationKeys.propertyCollectionMembers;
const singleKey = curationKeys.propertyCollections(42);

const newClient = () => new QueryClient({ defaultOptions: { queries: { retry: false } } });

describe('setMembership', () => {
  it('adds the collection to both cache shapes', async () => {
    const qc = newClient();
    qc.setQueryData(sharedKey, new Map([[42, [7]]]));
    qc.setQueryData(singleKey, [7]);

    await setMembership(qc, 42, 9, true);

    expect(qc.getQueryData<Map<number, number[]>>(sharedKey)?.get(42)).toEqual([7, 9]);
    expect(qc.getQueryData<number[]>(singleKey)).toEqual([7, 9]);
  });

  it('removes the collection from both cache shapes', async () => {
    const qc = newClient();
    qc.setQueryData(sharedKey, new Map([[42, [7, 9]]]));
    qc.setQueryData(singleKey, [7, 9]);

    await setMembership(qc, 42, 9, false);

    expect(qc.getQueryData<Map<number, number[]>>(sharedKey)?.get(42)).toEqual([7]);
    expect(qc.getQueryData<number[]>(singleKey)).toEqual([7]);
  });

  it('leaves an unloaded cache alone rather than seeding it', async () => {
    const qc = newClient();
    await setMembership(qc, 42, 9, true);
    expect(qc.getQueryData(sharedKey)).toBeUndefined();
    expect(qc.getQueryData(singleKey)).toBeUndefined();
  });

  it('is idempotent — a repeated add does not duplicate the id', async () => {
    const qc = newClient();
    qc.setQueryData(singleKey, [9]);
    await setMembership(qc, 42, 9, true);
    expect(qc.getQueryData<number[]>(singleKey)).toEqual([9]);
  });

  it('rolls back to the exact prior state', async () => {
    const qc = newClient();
    qc.setQueryData(sharedKey, new Map([[42, [7]]]));
    qc.setQueryData(singleKey, [7]);

    const rollback = await setMembership(qc, 42, 9, true);
    rollback();

    expect(qc.getQueryData<Map<number, number[]>>(sharedKey)?.get(42)).toEqual([7]);
    expect(qc.getQueryData<number[]>(singleKey)).toEqual([7]);
  });

  it('does not mutate the cached map or array in place', async () => {
    const qc = newClient();
    const ids = [7];
    const map = new Map([[42, ids]]);
    qc.setQueryData(sharedKey, map);

    await setMembership(qc, 42, 9, true);

    expect(ids).toEqual([7]);
    expect(map.get(42)).toEqual([7]);
  });
});

describe('revalidateCollections', () => {
  it('invalidates every read surface a membership write can change', () => {
    const qc = newClient();
    const seen: unknown[] = [];
    qc.invalidateQueries = ((opts: { queryKey: unknown }) => {
      seen.push(opts.queryKey);
      return Promise.resolve();
    }) as typeof qc.invalidateQueries;

    revalidateCollections(qc, 42, 9);

    expect(seen).toEqual([
      sharedKey,
      singleKey,
      curationKeys.collections,
      curationKeys.collection(9),
    ]);
  });
});
