/* Cache keys for card DECORATIONS — thumbnails, broker lines, anything a card
 * shows but is not the card.
 *
 * They live in their own top-level namespace on purpose, and this is the single
 * load-bearing detail of the whole hydration layer. Two invalidations sweep by
 * PREFIX today: `revalidatePipeline` fires `invalidateQueries(['pipeline',
 * 'board'])` after every card write (lib/pipelineCache), and the board's stage
 * editor fires `invalidateQueries(['pipeline'])` wholesale. Nesting the
 * decorations under either prefix would mean every drag of every card refetched
 * every thumbnail and every broker on the board — turning the split that makes
 * the board fast into something slower than the blocking chain it replaced.
 * `hydration.test.ts` asserts the disjointness so it cannot regress by accident.
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

export const hydrationKeys = {
  all: [HYDRATION_NAMESPACE] as const,
  covers: (ids: readonly number[]) =>
    [HYDRATION_NAMESPACE, 'covers', idsKey(ids)] as const,
  brokers: (ids: readonly number[]) =>
    [HYDRATION_NAMESPACE, 'brokers', idsKey(ids)] as const,
};
