/* The one place a deal-pipeline card is written from for a SINGLE property
 * (rule #22) — the Browse funnels, the listing header, and the stage menu they
 * share.
 *
 * Add / remove / move were duplicated across those surfaces, each with its own
 * idea of which caches to invalidate (the board forgot the members set, the
 * cards forgot the board). They are one hook now: same audited API calls, one
 * cache policy — `lib/pipelineCache.ts`, which the kanban's own bulk mutations
 * share, so "what a pipeline write does to the client state" has one answer
 * whether the operator clicked a funnel or dragged a card.
 *
 * Every write is optimistic. A funnel click has to repaint before the round
 * trip to Frankfurt or the menu closes onto a stale badge; on failure the
 * rollback fires from `onSettled` (not `onError`, which would silence the app's
 * global error toast — see pipelineCache's header) and the revalidation
 * reconciles with the server either way.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query';

import { addPipelineCard, movePipelineCard, removePipelineCard } from '@/lib/api';
import {
  cachedStage,
  dropCard,
  NO_ROLLBACK,
  placeCard,
  revalidatePipeline,
  type PipelineRollback,
} from '@/lib/pipelineCache';

export interface UsePipelineCardOptions {
  /* True when the caller's cohort is itself filtered by pipeline membership. */
  cohortScoped?: boolean;
}

export function usePipelineCard(
  property_id: number,
  { cohortScoped = false }: UsePipelineCardOptions = {},
) {
  const qc = useQueryClient();

  /* Same tail for all three: undo the optimistic patch if the write failed,
   * then re-read the truth. Typed loosely because each mutation's variables
   * differ; only the rollback context matters here. */
  const settle = <V,>(
    _data: unknown,
    error: unknown,
    _vars: V,
    rollback: PipelineRollback | undefined,
  ) => {
    if (error) rollback?.();
    revalidatePipeline(qc, property_id, { cohortScoped });
  };

  const add = useMutation({
    mutationFn: () => addPipelineCard(property_id),
    onMutate: () => {
      // The entry stage IS the bookmark (rule #22). Unknown until the stage
      // list has loaded — then the patch is skipped and the funnel fills when
      // the revalidation lands.
      const entry = cachedStage(qc, 'entry');
      return entry ? placeCard(qc, property_id, entry) : NO_ROLLBACK;
    },
    onSettled: settle,
  });

  const remove = useMutation({
    mutationFn: () => removePipelineCard(property_id),
    onMutate: () => dropCard(qc, property_id),
    onSettled: settle,
  });

  const move = useMutation({
    mutationFn: (stageId: number) => movePipelineCard(property_id, stageId),
    onMutate: (stageId: number) => {
      const stage = cachedStage(qc, stageId);
      return stage ? placeCard(qc, property_id, stage) : NO_ROLLBACK;
    },
    onSettled: settle,
  });

  return {
    add,
    remove,
    move,
    pending: add.isPending || remove.isPending || move.isPending,
  };
}
