/* The one place a property is dismissed or restored from (migration 536) — the
 * Browse cards, the Table rows and the listing header all share this hook.
 *
 * Dismissing is optimistic and list-aware: the property leaves every cached
 * Browse list that hides dismissed properties at once, and those lists are NOT
 * refetched — a triage run of twenty clicks must not re-read every loaded page
 * twenty times. Counts, Stats and the map re-read. Restoring re-reads the lists
 * too, because a hidden row has to come back, and so does a FAILED dismissal:
 * its rollback restores the lists as they were when it was clicked, which
 * would otherwise resurrect a neighbour dismissed successfully meanwhile.
 * Both writes go through lib/useOptimisticWrite (rollback from `onSettled`,
 * never `onError`).
 */

import { useQuery, type InfiniteData, type Query } from '@tanstack/react-query';

import { dismissProperty, undismissProperty } from '@/lib/api';
import { browseKeys } from '@/lib/browseKeys';
import type { ListingFilters } from '@/lib/filters';
import { dismissalKeys, fetchIsDismissed } from '@/lib/queries';
import { dismissToast, pushToast } from '@/lib/toast';
import { cachePatch, useOptimisticWrite } from '@/lib/useOptimisticWrite';

type ListPage = { rows: ReadonlyArray<{ property_id: number }> };

/* The two paged Browse lists, keyed [name, filters, sort] — those whose filters
 * hide dismissed properties. */
const hidingLists = browseKeys.lists.map((queryKey) => ({
  queryKey,
  predicate: (q: Query) => !(q.queryKey[1] as ListingFilters | undefined)?.showDismissed,
}));

const dropRow = (property_id: number) => (data: InfiniteData<ListPage> | undefined) =>
  data && {
    ...data,
    pages: data.pages.map((p) => ({
      ...p,
      rows: p.rows.filter((r) => r.property_id !== property_id),
    })),
  };

/* One undo offer on screen at a time: an action toast stays until closed (so
 * its button stays reachable), and a triage run would otherwise stack them. */
let undoToast: number | null = null;

export function useDismissal(property_id: number) {
  const state = useQuery({
    queryKey: dismissalKeys.state(property_id),
    queryFn: () => fetchIsDismissed(property_id),
    staleTime: 60_000,
  });

  const restore = useOptimisticWrite({
    mutationKey: ['write', 'dismissal', 'restore'],
    mutationFn: () => undismissProperty(property_id),
    patch: () => [cachePatch({ queryKey: dismissalKeys.state(property_id), exact: true }, () => false)],
    revalidate: [dismissalKeys.state(property_id), dismissalKeys.count, ...browseKeys.all],
  });

  const dismiss = useOptimisticWrite({
    mutationKey: ['write', 'dismissal', 'dismiss'],
    mutationFn: () => dismissProperty(property_id),
    patch: () => [
      cachePatch({ queryKey: dismissalKeys.state(property_id), exact: true }, () => true),
      ...hidingLists.map((filters) => cachePatch(filters, dropRow(property_id))),
    ],
    revalidate: (_vars, failed) => [
      dismissalKeys.state(property_id),
      dismissalKeys.count,
      ...(failed ? browseKeys.all : browseKeys.aggregates),
    ],
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
