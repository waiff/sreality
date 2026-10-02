/* The one place a deal-pipeline card is written from (rule #22) — the Browse
 * funnels, the listing header, the stage menu they share, AND the kanban's
 * drag and trash.
 *
 * Add / remove / move were duplicated across those surfaces, each with its own
 * idea of which caches to invalidate (the board forgot the members set, the
 * cards forgot the board). They are one hook now: same audited API calls, one
 * cache patch (`lib/pipelineCache.ts`), one write policy
 * (`lib/useOptimisticWrite.ts`), so "what a pipeline write does to the client
 * state" has one answer whether the operator clicked a funnel or dragged a card.
 * The property id travels with each call, so one hook instance serves a whole
 * board.
 *
 * Every write is optimistic. A funnel click has to repaint before the round
 * trip to Frankfurt or the menu closes onto a stale badge; on failure the
 * rollback fires from `onSettled` (not `onError`, which would silence the app's
 * global error toast) and the revalidation reconciles with the server either
 * way. `pending(id)` is per property and spans hook instances: the funnel stays
 * busy while the menu it opened is still writing.
 */

import { addPipelineCard, movePipelineCard, removePipelineCard } from '@/lib/api';
import {
  cachedStage,
  dropCard,
  pipelineRevalidation,
  placeCard,
} from '@/lib/pipelineCache';
import { useOptimisticWrite } from '@/lib/useOptimisticWrite';

export interface UsePipelineCardOptions {
  /* True when the caller's cohort is itself filtered by pipeline membership. */
  cohortScoped?: boolean;
}

/* What planMove (pages/Pipeline) resolves a drag into. */
export interface PipelineMove {
  propertyId: number;
  stageId: number;
}

export function usePipelineCard({ cohortScoped = false }: UsePipelineCardOptions = {}) {
  const revalidate = pipelineRevalidation(cohortScoped);

  const add = useOptimisticWrite({
    mutationKey: ['write', 'pipeline', 'add'],
    mutationFn: (property_id: number) => addPipelineCard(property_id),
    // The entry stage IS the bookmark (rule #22). Unknown until the stage list
    // has loaded — then nothing is painted and the funnel fills when the
    // revalidation lands.
    patch: (property_id, qc) => {
      const entry = cachedStage(qc, 'entry');
      return entry ? placeCard(property_id, entry) : [];
    },
    revalidate,
    pendingKey: (property_id) => property_id,
  });

  const remove = useOptimisticWrite({
    mutationKey: ['write', 'pipeline', 'remove'],
    mutationFn: (property_id: number) => removePipelineCard(property_id),
    patch: (property_id) => dropCard(property_id),
    revalidate,
    pendingKey: (property_id) => property_id,
  });

  const move = useOptimisticWrite({
    mutationKey: ['write', 'pipeline', 'move'],
    mutationFn: ({ propertyId, stageId }: PipelineMove) => movePipelineCard(propertyId, stageId),
    patch: ({ propertyId, stageId }, qc) => {
      const stage = cachedStage(qc, stageId);
      return stage ? placeCard(propertyId, stage) : [];
    },
    revalidate,
    pendingKey: ({ propertyId }) => propertyId,
  });

  return {
    add,
    remove,
    move,
    pending: (property_id: number) =>
      add.pendingFor(property_id) || remove.pendingFor(property_id) || move.pendingFor(property_id),
  };
}
