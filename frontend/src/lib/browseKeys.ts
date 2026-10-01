/* The Browse read surfaces' query keys — one table of roots, every reader and
 * every sweep derived from it, so a new surface cannot be read under a key the
 * merge / dismissal / pipeline sweeps never reach.
 *
 * Slot 0 is the surface, slot 1 the filters: useDismissal's hiding predicate
 * reads `queryKey[1]`. No namespace prefix — the roots ARE the namespace, and
 * none of them may equal lib/hydration's (hydration.test.ts pins that). */

import type { ListingFilters } from '@/lib/filters';
import type { SortSpec } from '@/lib/queries';

const root = {
  cards: 'cards',
  table: 'table',
  map: 'map',
  stats: 'stats',
  count: 'browse-count',
  noPriceCount: 'no-price-count',
} as const;

export const browseKeys = {
  cards: (f: ListingFilters, s: SortSpec) => [root.cards, f, s] as const,
  table: (f: ListingFilters, s: SortSpec) => [root.table, f, s] as const,
  map: (f: ListingFilters) => [root.map, f] as const,
  stats: (f: ListingFilters) => [root.stats, f] as const,
  count: (f: ListingFilters) => [root.count, f] as const,
  noPriceCount: (f: ListingFilters) => [root.noPriceCount, f] as const,
  /** The paged lists: patched in place by a dismissal, never refetched for one. */
  lists: [[root.cards], [root.table]] as const,
  /** Every other read of the browse read model. */
  aggregates: [[root.map], [root.stats], [root.count], [root.noPriceCount]] as const,
  /** Every Browse read surface: what a merge / split / link / restore sweeps. */
  all: Object.values(root).map((r) => [r] as const),
};
