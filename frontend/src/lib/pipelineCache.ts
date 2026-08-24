/* What a deal-pipeline write does to the client caches — one definition, used
 * by every surface that writes a card (rule #22).
 *
 * Two caches hold "where is this property in the pipeline": `members` (the map
 * every funnel — Browse cards/table AND the listing-detail toggle, since W3 —
 * renders from) and `board` (the kanban array, which also carries display
 * fields `members` doesn't: price, photo, place). A write changes the same
 * fact in both, and each surface used to patch only the one it could see: the
 * board's drag invalidated `board` alone, so after moving a card on the
 * kanban every Browse funnel kept painting the OLD stage badge until the
 * members query went stale on its own. (A THIRD cache, one entry per property
 * for the listing-detail toggle alone, existed until W3 — it was pure
 * duplication of what `members` already held for that property, and the two
 * had already drifted out of sync once on which columns they selected.)
 *
 * Each patcher returns a rollback closure instead of taking an `onError`
 * handler, because React Query's global MutationCache.onError (main.tsx) — the
 * app's only "the write failed" feedback — deliberately stays silent for any
 * mutation that defines its own `onError`. Callers therefore roll back from
 * `onSettled`, where the error is also in hand, and the toast still fires:
 *
 *   onMutate:  () => placeCard(qc, id, stage),
 *   onSettled: (_d, err, _v, rollback) => { if (err) rollback?.(); revalidate(qc); },
 */

import type { QueryClient } from '@tanstack/react-query';

import { pipelineKeys, type PipelineMembers } from '@/lib/queries';
import { invalidateBrowseQueries } from '@/lib/browseInvalidation';
import type { PipelineBoardCard, PipelineStage } from '@/lib/types';

export type PipelineRollback = () => void;

/* Returned when the optimistic patch was skipped (the stage list has not
 * loaded, so there is nothing to paint) — the write still runs and the
 * revalidation paints the result. A real closure rather than `undefined`
 * keeps every mutation's context one type. */
export const NO_ROLLBACK: PipelineRollback = () => {};

/* Snapshot the two caches and hand back the restore. */
function snapshot(qc: QueryClient): PipelineRollback {
  const members = qc.getQueryData<PipelineMembers>(pipelineKeys.members);
  const board = qc.getQueryData<PipelineBoardCard[]>(pipelineKeys.board);
  return () => {
    qc.setQueryData(pipelineKeys.members, members);
    qc.setQueryData(pipelineKeys.board, board);
  };
}

/* In-flight reads would otherwise land after the patch and undo it. */
async function quiesce(qc: QueryClient): Promise<void> {
  await Promise.all([
    qc.cancelQueries({ queryKey: pipelineKeys.members }),
    qc.cancelQueries({ queryKey: pipelineKeys.board }),
  ]);
}

/* Show the property as sitting at `stage` — used for both "bookmarked into the
 * entry stage" and "moved to another stage".
 *
 * The board array is patched in place only when it already holds the card: a
 * board entry carries the property's display fields (price, photo, place) that
 * a funnel click has no way to synthesise, so a NEW card reaches the board via
 * the revalidation instead of as a half-built row. */
export async function placeCard(
  qc: QueryClient,
  property_id: number,
  stage: PipelineStage,
): Promise<PipelineRollback> {
  await quiesce(qc);
  const rollback = snapshot(qc);

  qc.setQueryData<PipelineMembers>(pipelineKeys.members, (prev) => {
    if (!prev) return prev;
    const next = new Map(prev);
    next.set(property_id, {
      property_id,
      stage_id: stage.id,
      stage_label: stage.label,
      stage_color: stage.color,
      stage_code: stage.code ?? null,
      stage_position: stage.position,
      is_terminal: stage.is_terminal,
    });
    return next;
  });
  qc.setQueryData<PipelineBoardCard[]>(pipelineKeys.board, (prev) =>
    prev?.map((c) => (c.property_id === property_id ? { ...c, stage_id: stage.id } : c)),
  );

  return rollback;
}

/* Show the property as off the board. */
export async function dropCard(
  qc: QueryClient,
  property_id: number,
): Promise<PipelineRollback> {
  await quiesce(qc);
  const rollback = snapshot(qc);

  qc.setQueryData<PipelineMembers>(pipelineKeys.members, (prev) => {
    if (!prev) return prev;
    const next = new Map(prev);
    next.delete(property_id);
    return next;
  });
  qc.setQueryData<PipelineBoardCard[]>(pipelineKeys.board, (prev) =>
    prev?.filter((c) => c.property_id !== property_id),
  );

  return rollback;
}

/* Re-read the truth after any write, successful or not.
 *
 * `cohortScoped` is the caller's one knob: when Browse is scoped to the
 * pipeline, membership IS the cohort — un-bookmarking must drop the row from
 * the list — so the Browse read surfaces have to refetch too. With the scope
 * off, membership changes nothing about which properties match, and refetching
 * map + every loaded card page + count + stats on a funnel click is pure waste. */
export function revalidatePipeline(
  qc: QueryClient,
  { cohortScoped = false }: { cohortScoped?: boolean } = {},
): void {
  qc.invalidateQueries({ queryKey: pipelineKeys.members });
  qc.invalidateQueries({ queryKey: pipelineKeys.board });
  if (cohortScoped) invalidateBrowseQueries(qc);
}

/* The stage a write lands on, read from the shared stage list already in cache.
 * Returns null when the list has not loaded yet — the caller then skips the
 * optimistic patch and lets the revalidation paint the result. */
export function cachedStage(
  qc: QueryClient,
  match: number | 'entry',
): PipelineStage | null {
  const stages = qc.getQueryData<PipelineStage[]>(pipelineKeys.stages) ?? [];
  const found =
    match === 'entry'
      ? stages.find((s) => s.is_entry)
      : stages.find((s) => s.id === match);
  return found ?? null;
}
