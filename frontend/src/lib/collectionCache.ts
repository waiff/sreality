/* The single source of truth for "which caches a collection write must refresh"
 * — the curation analogue of lib/browseInvalidation, and it exists for the same
 * reason its header records: the key list was hand-typed per call site and
 * drifted. Every writer but the menu had lost the shared member map — the
 * CurationBlock row, the collection page's row-remove, both collection DELETEs,
 * and the merge, which invalidated no curation key at all — so a membership
 * change left every Browse card glyph painting the old state until the query
 * went stale on its own.
 * Import and call this instead of re-typing the list. */

import type { QueryClient } from '@tanstack/react-query';

import { curationKeys } from '@/lib/queries';
import { invalidateBrowseQueries } from '@/lib/browseInvalidation';

/** Re-read collections after ANY collection write — one idiom per call site,
 * metadata included. The member map is in the list because a property-grain
 * add/remove, a collection DELETE (migration 202 cascades its memberships away)
 * and a merge (operator_state re-points collection_properties onto the
 * survivor) each change it. Pass `collection_id` when the write names one
 * collection, so its own page refetches too.
 *
 * `cohortScoped` is revalidatePipeline's knob again: when Browse is scoped to
 * collections, membership IS the cohort, so the Browse reads refetch too. Only
 * a caller that knows the current filters can say so; everyone else leaves it
 * false (refetching every Browse surface on a save is otherwise waste). */
export function revalidateCollections(
  qc: QueryClient,
  {
    collection_id,
    cohortScoped = false,
  }: { collection_id?: number; cohortScoped?: boolean } = {},
): void {
  qc.invalidateQueries({ queryKey: curationKeys.propertyCollectionMembers });
  qc.invalidateQueries({ queryKey: curationKeys.collections });
  if (collection_id != null) {
    qc.invalidateQueries({ queryKey: curationKeys.collection(collection_id) });
  }
  if (cohortScoped) invalidateBrowseQueries(qc);
}
