/* The one shape of an optimistic cache write (the deal pipeline, dismissals).
 *
 * A patcher first HOLDS the queries it is about to change — cancels their
 * in-flight reads, which would otherwise land after the patch and undo it, and
 * snapshots them — then patches, and returns the snapshot's restore. It hands
 * back a rollback instead of taking an `onError` handler because React Query's
 * global MutationCache.onError (main.tsx), the app's only "the write failed"
 * feedback, stays silent for any mutation that defines its own `onError`.
 * Callers therefore roll back from `onSettled`, where the error is also in hand:
 *
 *   onMutate:  () => patch(qc, id),
 *   onSettled: (_d, err, _v, rollback) => { if (err) rollback?.(); revalidate(qc); },
 */

import type { QueryClient, QueryFilters } from '@tanstack/react-query';

export type Rollback = () => void;

/* Returned when a patcher had nothing to paint (its input has not loaded) — the
 * write still runs and the revalidation paints the result. A real closure
 * rather than `undefined` keeps every mutation's context one type. */
export const NO_ROLLBACK: Rollback = () => {};

export async function holdQueries(
  qc: QueryClient,
  filters: readonly QueryFilters[],
): Promise<Rollback> {
  await Promise.all(filters.map((f) => qc.cancelQueries(f)));
  const saved = filters.flatMap((f) => qc.getQueriesData(f));
  return () => {
    for (const [key, data] of saved) qc.setQueryData(key, data);
  };
}
