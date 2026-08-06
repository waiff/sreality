/* The one place a deal-pipeline card is written from (rule #22).
 *
 * Add / remove / move were duplicated across the Browse cards, the listing
 * header and the kanban board — three copies of the same three mutations, each
 * with its own idea of which caches to invalidate (the board forgot the members
 * set, the cards forgot the board). They are one hook now: same audited API
 * calls, one invalidation policy.
 *
 * `cohortScoped` is the one caller-supplied knob. When the Browse pipeline
 * scope is ON, membership IS the cohort — un-bookmarking a property must drop
 * it from the list — so the Browse read surfaces have to be invalidated too.
 * When the scope is off, membership changes nothing about which properties
 * match, and refetching the whole cohort (map + every loaded card page + count
 * + stats) on a bookmark click would be pure waste.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query';

import { addPipelineCard, movePipelineCard, removePipelineCard } from '@/lib/api';
import { invalidateBrowseQueries } from '@/lib/browseInvalidation';
import { pipelineKeys } from '@/lib/queries';

export interface UsePipelineCardOptions {
  /* True when the caller's cohort is itself filtered by pipeline membership. */
  cohortScoped?: boolean;
  /* Extra work after any successful write (e.g. the board's optimistic reset). */
  onWritten?: () => void;
}

export function usePipelineCard(
  property_id: number,
  { cohortScoped = false, onWritten }: UsePipelineCardOptions = {},
) {
  const qc = useQueryClient();

  const syncSurfaces = () => {
    qc.invalidateQueries({ queryKey: pipelineKeys.card(property_id) });
    qc.invalidateQueries({ queryKey: pipelineKeys.members });
    qc.invalidateQueries({ queryKey: pipelineKeys.board });
    if (cohortScoped) invalidateBrowseQueries(qc);
    onWritten?.();
  };

  const add = useMutation({
    mutationFn: () => addPipelineCard(property_id),
    onSuccess: syncSurfaces,
  });
  const remove = useMutation({
    mutationFn: () => removePipelineCard(property_id),
    onSuccess: syncSurfaces,
  });
  const move = useMutation({
    mutationFn: (stageId: number) => movePipelineCard(property_id, stageId),
    onSuccess: syncSurfaces,
  });

  return {
    add,
    remove,
    move,
    pending: add.isPending || remove.isPending || move.isPending,
  };
}
