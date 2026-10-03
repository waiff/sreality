/* The single source of truth for "which Browse read surfaces must refresh after
 * a property-identity-changing mutation (merge / unmerge / link)".
 *
 * Every Browse surface reads the browse_list read model (cards, table, the
 * header/tab count, the no-price count, stats) or the map matview, so a merge
 * done anywhere must invalidate all of them. Before this helper the key list
 * was hand-typed per call site and drifted: the header count key
 * ('browse-count') was missing from every list, and the (since-removed) dedup
 * review page invalidated only its own keys, so a merge approved there left
 * Browse stale. The list itself is `browseKeys.all` (lib/browseKeys), the same
 * table every reader takes its key from.
 * Import and call this instead of re-typing the list. */

import type { QueryClient } from '@tanstack/react-query';

import { browseKeys } from '@/lib/browseKeys';

/** Invalidate every Browse read surface, by prefix. Call after any merge /
 * unmerge / asset-link (or a write whose cohort IS the Browse filter) settles,
 * so cards, table, map, stats and both counts refetch the post-write state. */
export function invalidateBrowseQueries(queryClient: QueryClient): void {
  for (const queryKey of browseKeys.all) {
    void queryClient.invalidateQueries({ queryKey });
  }
}
