/* The one place property↔collection membership is written from (rule #18) —
 * the Browse cards, the listing-detail header, and the collection page's own
 * row removal.
 *
 * Add / remove were duplicated across those three surfaces, each with its own
 * idea of which caches to invalidate. They are one hook now: same audited API
 * calls, one cache policy (`lib/collectionCache.ts`), so "what a membership
 * write does to the client state" has one answer wherever it was clicked —
 * the collection analogue of usePipelineCard.
 *
 * Every write is optimistic; on failure the rollback fires from `onSettled`
 * (not `onError`, which would silence the app's global error toast — see
 * collectionCache's header) and the revalidation reconciles either way.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query';

import { addPropertiesToCollection, removePropertyFromCollection } from '@/lib/api';
import {
  revalidateCollections,
  setMembership,
  type CollectionRollback,
} from '@/lib/collectionCache';

export function useCollectionMembership(property_id: number) {
  const qc = useQueryClient();

  const settle = (
    _data: unknown,
    error: unknown,
    collection_id: number,
    rollback: CollectionRollback | undefined,
  ) => {
    if (error) rollback?.();
    revalidateCollections(qc, property_id, collection_id);
  };

  const add = useMutation({
    mutationFn: (collection_id: number) =>
      addPropertiesToCollection(collection_id, [property_id]),
    onMutate: (collection_id: number) =>
      setMembership(qc, property_id, collection_id, true),
    onSettled: settle,
  });

  const remove = useMutation({
    mutationFn: (collection_id: number) =>
      removePropertyFromCollection(collection_id, property_id),
    onMutate: (collection_id: number) =>
      setMembership(qc, property_id, collection_id, false),
    onSettled: settle,
  });

  return {
    add,
    remove,
    /* Membership renders as a checkbox everywhere it is shown; callers pass the
     * state they painted so they never have to decide which mutation it means. */
    toggle: (collection_id: number, member: boolean) =>
      (member ? remove : add).mutate(collection_id),
    pending: add.isPending || remove.isPending,
  };
}
