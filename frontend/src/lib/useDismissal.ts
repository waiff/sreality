/* The one place a property is dismissed or restored from (migration 536) — the
 * Browse cards, the Table rows and the listing header all share this hook.
 *
 * Dismissing is optimistic and list-aware: the property leaves every cached
 * Browse list that hides dismissed properties at once, and those lists are NOT
 * refetched — a triage run of twenty clicks must not re-read every loaded page
 * twenty times. Counts, Stats and the map re-read. Restoring re-reads the lists
 * too, because a hidden row has to come back. Rollback follows
 * lib/optimisticCache's shape (from `onSettled`, never `onError`).
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type InfiniteData,
  type Query,
  type QueryClient,
} from '@tanstack/react-query';

import { dismissProperty, undismissProperty } from '@/lib/api';
import { invalidateBrowseQueries } from '@/lib/browseInvalidation';
import type { ListingFilters } from '@/lib/filters';
import { holdQueries, type Rollback } from '@/lib/optimisticCache';
import { dismissalKeys, fetchIsDismissed } from '@/lib/queries';
import { dismissToast, pushToast } from '@/lib/toast';

/* The two paged Browse lists, keyed [name, filters, sort]. */
const LIST_NAMES = ['cards', 'table'] as const;

type ListPage = { rows: ReadonlyArray<{ property_id: number }> };

const hidingLists = LIST_NAMES.map((name) => ({
  queryKey: [name],
  predicate: (q: Query) => !(q.queryKey[1] as ListingFilters | undefined)?.showDismissed,
}));

async function markDismissed(qc: QueryClient, property_id: number): Promise<Rollback> {
  const rollback = await holdQueries(qc, [
    { queryKey: dismissalKeys.state(property_id) },
    ...hidingLists,
  ]);
  qc.setQueryData(dismissalKeys.state(property_id), true);
  for (const filter of hidingLists) {
    qc.setQueriesData<InfiniteData<ListPage>>(filter, (data) =>
      data && {
        ...data,
        pages: data.pages.map((p) => ({
          ...p,
          rows: p.rows.filter((r) => r.property_id !== property_id),
        })),
      },
    );
  }
  return rollback;
}

async function markRestored(qc: QueryClient, property_id: number): Promise<Rollback> {
  const rollback = await holdQueries(qc, [{ queryKey: dismissalKeys.state(property_id) }]);
  qc.setQueryData(dismissalKeys.state(property_id), false);
  return rollback;
}

function revalidate(qc: QueryClient, property_id: number, restored: boolean): void {
  qc.invalidateQueries({ queryKey: dismissalKeys.state(property_id) });
  qc.invalidateQueries({ queryKey: dismissalKeys.count });
  if (restored) {
    invalidateBrowseQueries(qc);
    return;
  }
  for (const name of ['browse-count', 'stats', 'map']) {
    qc.invalidateQueries({ queryKey: [name] });
  }
}

/* One undo offer on screen at a time: an action toast stays until closed (so
 * its button stays reachable), and a triage run would otherwise stack them. */
let undoToast: number | null = null;

export function useDismissal(property_id: number) {
  const qc = useQueryClient();
  const state = useQuery({
    queryKey: dismissalKeys.state(property_id),
    queryFn: () => fetchIsDismissed(property_id),
    staleTime: 60_000,
  });

  const settle = (restored: boolean) =>
    (_data: unknown, error: unknown, _vars: void, rollback: Rollback | undefined) => {
      if (error) rollback?.();
      revalidate(qc, property_id, restored);
    };

  const restore = useMutation({
    mutationFn: () => undismissProperty(property_id),
    onMutate: () => markRestored(qc, property_id),
    onSettled: settle(true),
  });

  const dismiss = useMutation({
    mutationFn: () => dismissProperty(property_id),
    onMutate: () => markDismissed(qc, property_id),
    onSettled: settle(false),
    onSuccess: () => {
      if (undoToast != null) dismissToast(undoToast);
      const id = pushToast('info', 'Nemovitost skryta', 0, {
        label: 'Vrátit',
        onClick: () => {
          dismissToast(id);
          restore.mutate();
        },
      });
      undoToast = id;
    },
  });

  return {
    dismissed: state.data ?? null,
    dismiss,
    restore,
    pending: dismiss.isPending || restore.isPending,
  };
}
