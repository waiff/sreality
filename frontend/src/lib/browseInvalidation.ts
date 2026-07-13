/* The single source of truth for "which Browse read surfaces must refresh after
 * a property-identity-changing mutation (merge / unmerge / link)".
 *
 * Every Browse surface reads the browse_list read model (cards, table, the
 * header/tab count, stats) or the map matview, so a merge done anywhere — Browse
 * merge-mode OR the /dedup review queue — must invalidate all of them. Before
 * this helper the key list was hand-typed per call site and drifted: the header
 * count key ('browse-count') was missing from every list, and the /dedup page
 * invalidated only its own keys, so a merge approved there left Browse stale.
 * Import and call this instead of re-typing the list. */

import type { QueryClient } from '@tanstack/react-query';

/* Partial query-key prefixes — invalidateQueries matches every query whose key
 * STARTS WITH one of these, regardless of the filters/sort suffix each carries
 * (e.g. ['cards', filters, sort], ['browse-count', filters]). */
export const BROWSE_QUERY_KEYS = [
  'cards',
  'map',
  'table',
  'stats',
  'browse-count',
] as const;

/** Invalidate every Browse read surface. Call in a mutation's onSuccess after any
 * merge / unmerge / asset-link so cards, table, map, stats, and the header count
 * all refetch the post-mutation state. */
export function invalidateBrowseQueries(queryClient: QueryClient): void {
  for (const key of BROWSE_QUERY_KEYS) {
    queryClient.invalidateQueries({ queryKey: [key] });
  }
}
