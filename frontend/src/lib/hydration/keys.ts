/* Cache keys for card DECORATIONS — thumbnails, broker lines, anything a card
 * shows but is not the card.
 *
 * They live in their own top-level namespace on purpose, and this is the single
 * load-bearing detail of the whole hydration layer. Invalidations sweep by
 * PREFIX: every pipeline card write re-reads `PIPELINE_REVALIDATE` (members and
 * board, lib/pipelineCache), and the board's stage editor re-reads stages and
 * board. Nesting the decorations under any of those prefixes would mean every
 * drag of every card refetched every thumbnail and every broker on the board —
 * turning the split that makes the board fast into something slower than the
 * blocking chain it replaced. `hydration.test.ts` asserts the disjointness from
 * every write sweep (pipeline, Browse, autodedup, dismissals) so it cannot
 * regress by accident.
 *
 * Keys are cohort-shaped, not per-id: one query for the whole visible id set,
 * so N cards cost one request, not N. `idsKey` makes that set order-independent
 * and duplicate-free, so re-sorting a board or re-rendering with the same cards
 * in a different order is a cache HIT rather than a new key. */

export const HYDRATION_NAMESPACE = 'hydration' as const;

/* Sorted + de-duplicated so the key is a property of the SET, not of the array
 * that happened to arrive. Numeric sort (not lexicographic) keeps it readable
 * in devtools. */
export function idsKey(ids: readonly number[]): string {
  return [...new Set(ids)].sort((a, b) => a - b).join(',');
}

/* A board card's property and the canonical ad that heads its broker list. */
export interface BrokerSubject {
  property_id: number;
  listing_id: number | null;
}

export const hydrationKeys = {
  all: [HYDRATION_NAMESPACE] as const,
  covers: (ids: readonly number[]) =>
    [HYDRATION_NAMESPACE, 'covers', idsKey(ids)] as const,
  brokers: (ids: readonly number[]) =>
    [HYDRATION_NAMESPACE, 'brokers', idsKey(ids)] as const,
  /* PROPERTY-grain (MS7), never under `brokers`: the two id spaces overlap. The
   * canonical ad heads the list, so it is in the key too. */
  propertyBrokers: (subjects: readonly BrokerSubject[]) =>
    [
      HYDRATION_NAMESPACE,
      'property-brokers',
      subjects.map((s) => `${s.property_id}:${s.listing_id ?? ''}`).sort().join(','),
    ] as const,
  /* Several photos per listing — the Browse card carousel and the comparables
   * modal, as distinct from `covers` (the board's ONE thumbnail, W4). `perId` is
   * part of the key on purpose: it is a client-side retention cap applied to the
   * SAME server read, so a cohort capped at 6 and the same cohort capped at 50
   * hold different data and must not collide. Leaving it out would let whichever
   * surface asked first silently truncate the other's carousel. */
  photos: (ids: readonly number[], perId: number) =>
    [HYDRATION_NAMESPACE, 'photos', idsKey(ids), perId] as const,
  /* PROPERTY-grain like propertyBrokers: how many ads each card's property
   * holds. A merge or a split changes the count, so `adCountsAll` is the one
   * hydration root lib/mergedAdverts' refreshAfterSplit re-reads; every other
   * decoration stays cached through it. */
  adCounts: (propertyIds: readonly number[]) =>
    [HYDRATION_NAMESPACE, 'ad-counts', idsKey(propertyIds)] as const,
  adCountsAll: [HYDRATION_NAMESPACE, 'ad-counts'] as const,
};
