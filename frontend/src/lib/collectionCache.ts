/* What a collection-membership write does to the client caches — one
 * definition, used by every surface that files a property (rule #18).
 *
 * Two caches hold "is this property in collection X", in different shapes:
 * `propertyCollectionMembers` (the all-properties map every Browse control
 * renders from) and `propertyCollections(id)` (one property's own list, what a
 * record page reads). Two more are derived and left to the refetch:
 * `collections` carries a server-computed `listing_count`, and
 * `collection(id)` carries member rows with display fields (price, place,
 * status) a checkbox click has no way to synthesise — patching those from the
 * client would be inventing data.
 *
 * Before this, each surface hand-typed its own invalidation list and every one
 * of them forgot a different cache: the Browse cards forgot the collection
 * page, the listing page and the collection page both forgot the shared map —
 * so a save on one screen left the others showing the old membership. Same
 * failure mode `pipelineCache` and `browseInvalidation` exist to prevent.
 *
 * Rollbacks are returned as a closure rather than handled in `onError`, because
 * React Query's global MutationCache.onError (main.tsx) — the app's only "the
 * write failed" feedback — deliberately stays silent for any mutation that
 * defines its own `onError`. Callers roll back from `onSettled` instead.
 */

import type { QueryClient } from '@tanstack/react-query';

import { curationKeys } from '@/lib/queries';

export type CollectionRollback = () => void;

type MemberMap = Map<number, number[]>;

/* Optimistically show the property as in / out of one collection.
 *
 * The optimism is what makes the picker usable: it is a multi-select, and
 * without it every tick waits on a round trip to Frankfurt while the whole list
 * sits disabled, so filing into three collections costs three stalls. */
export async function setMembership(
  qc: QueryClient,
  property_id: number,
  collection_id: number,
  member: boolean,
): Promise<CollectionRollback> {
  const sharedKey = curationKeys.propertyCollectionMembers;
  const singleKey = curationKeys.propertyCollections(property_id);

  // In-flight reads would otherwise land after the patch and undo it.
  await Promise.all([
    qc.cancelQueries({ queryKey: sharedKey }),
    qc.cancelQueries({ queryKey: singleKey }),
  ]);

  const prevShared = qc.getQueryData<MemberMap>(sharedKey);
  const prevSingle = qc.getQueryData<number[]>(singleKey);
  const rollback = () => {
    qc.setQueryData(sharedKey, prevShared);
    qc.setQueryData(singleKey, prevSingle);
  };

  const nextIds = (ids: number[]) =>
    member
      ? ids.includes(collection_id)
        ? ids
        : [...ids, collection_id]
      : ids.filter((id) => id !== collection_id);

  /* Only patch a cache that is actually loaded — seeding one from a single
   * property's write would leave every OTHER property looking unfiled. */
  qc.setQueryData<MemberMap>(sharedKey, (prev) => {
    if (!prev) return prev;
    const next = new Map(prev);
    next.set(property_id, nextIds(prev.get(property_id) ?? []));
    return next;
  });
  qc.setQueryData<number[]>(singleKey, (prev) =>
    prev ? nextIds(prev) : prev,
  );

  return rollback;
}

/* Re-read the truth after any write, successful or not. */
export function revalidateCollections(
  qc: QueryClient,
  property_id: number,
  collection_id: number,
): void {
  qc.invalidateQueries({ queryKey: curationKeys.propertyCollectionMembers });
  qc.invalidateQueries({ queryKey: curationKeys.propertyCollections(property_id) });
  qc.invalidateQueries({ queryKey: curationKeys.collections });
  qc.invalidateQueries({ queryKey: curationKeys.collection(collection_id) });
}
