/* The deal-pipeline control for ONE property, sized for the listing-detail
 * header action bar (next to "New estimation").
 *
 * "Bookmark / interested" == the entry stage (rule #22): presence of a card ==
 * the property is in the pipeline. Out of pipeline → a copper funnel "Přidat do
 * pipeline" (the app's one accent, marking THE deal-tracking verb). In pipeline →
 * a pill tinted with the current stage's colour that lets the operator CHANGE the
 * stage (a native <select>, the app's single-choice control — the kanban moves by
 * drag, but a record page has no board to drag onto) and remove the property.
 *
 * Stage change goes through the SAME `movePipelineCard` PATCH the kanban uses, so
 * it stamps `entered_stage_at` + logs a `moved` event to `property_pipeline_events`
 * — one audited write path for every surface. Membership reads differ per surface
 * (this reads the single card; Browse cards read a shared members-set), so writes
 * here invalidate the other surfaces' keys to keep them in sync.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { addPipelineCard, movePipelineCard, removePipelineCard } from '@/lib/api';
import { fetchPipelineStages, fetchPropertyPipeline, pipelineKeys } from '@/lib/queries';
import { FunnelIcon } from '@/components/icons';
import type { PipelineCard } from '@/lib/types';

export default function PipelineToggle({ property_id }: { property_id: number }) {
  const qc = useQueryClient();

  const cardQ = useQuery({
    queryKey: pipelineKeys.card(property_id),
    queryFn: () => fetchPropertyPipeline(property_id),
    staleTime: 30_000,
  });
  const stagesQ = useQuery({
    queryKey: pipelineKeys.stages,
    queryFn: fetchPipelineStages,
    staleTime: 60_000,
  });
  const card = cardQ.data ?? null;
  const stages = stagesQ.data ?? [];
  const inPipeline = card != null;

  // The Browse-card funnels (shared members-set) + the kanban board read pipeline
  // state elsewhere; keep them in sync after any write here.
  const syncSurfaces = () => {
    qc.invalidateQueries({ queryKey: pipelineKeys.card(property_id) });
    qc.invalidateQueries({ queryKey: pipelineKeys.members });
    qc.invalidateQueries({ queryKey: pipelineKeys.board });
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
    // Optimistic: recolour the pill + reselect instantly, like the kanban board.
    onMutate: async (stageId): Promise<{ prev: PipelineCard | null | undefined }> => {
      await qc.cancelQueries({ queryKey: pipelineKeys.card(property_id) });
      const prev = qc.getQueryData<PipelineCard | null>(pipelineKeys.card(property_id));
      const s = stages.find((st) => st.id === stageId);
      if (prev && s) {
        qc.setQueryData<PipelineCard | null>(pipelineKeys.card(property_id), {
          ...prev,
          stage_id: s.id,
          stage_key: s.key,
          stage_label: s.label,
          stage_color: s.color,
          is_terminal: s.is_terminal,
          stage_position: s.position,
        });
      }
      return { prev };
    },
    onError: (_e, _v, ctx) => {
      if (ctx) qc.setQueryData(pipelineKeys.card(property_id), ctx.prev ?? null);
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: pipelineKeys.card(property_id) });
      qc.invalidateQueries({ queryKey: pipelineKeys.board });
    },
  });

  const pending = add.isPending || remove.isPending || move.isPending;

  if (cardQ.isLoading) {
    return (
      <span
        className="inline-flex h-[1.9rem] w-32 animate-pulse rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)]"
        aria-hidden
      />
    );
  }

  if (!inPipeline) {
    return (
      <button
        type="button"
        onClick={() => add.mutate()}
        disabled={pending}
        title="Přidat do pipeline"
        className="inline-flex items-center gap-1.5 rounded-[var(--radius-sm)] border border-[var(--color-copper)] bg-[var(--color-paper-2)] px-3 py-1.5 text-[0.8rem] text-[var(--color-copper)] transition-colors hover:bg-[var(--color-copper-soft)] disabled:opacity-60"
      >
        <FunnelIcon filled={false} className="h-4 w-4 shrink-0" />
        <span>Přidat do pipeline</span>
      </button>
    );
  }

  const fg = card.stage_color
    ? `var(--color-tag-${card.stage_color})`
    : 'var(--color-copper)';
  const bg = card.stage_color
    ? `var(--color-tag-${card.stage_color}-soft)`
    : 'var(--color-copper-soft)';

  return (
    <div
      className="inline-flex items-center gap-1 rounded-[var(--radius-sm)] border py-0.5 pl-2.5 pr-1 text-[0.8rem]"
      style={{ background: bg, color: fg, borderColor: fg, opacity: pending ? 0.6 : undefined }}
    >
      <FunnelIcon filled className="h-4 w-4 shrink-0" />
      <select
        value={card.stage_id}
        onChange={(e) => move.mutate(Number(e.target.value))}
        disabled={pending}
        aria-label="Fáze v pipeline"
        title="Změnit fázi"
        className="cursor-pointer border-0 bg-transparent py-0.5 pr-1 font-medium focus:outline-none disabled:cursor-default"
        style={{ color: fg }}
      >
        {stages.map((s) => (
          <option key={s.id} value={s.id} style={{ color: 'var(--color-ink)' }}>
            {s.label}
          </option>
        ))}
      </select>
      <button
        type="button"
        onClick={() => remove.mutate()}
        disabled={pending}
        aria-label="Odebrat z pipeline"
        title="Odebrat z pipeline"
        className="shrink-0 rounded-[var(--radius-xs)] px-1 leading-none hover:text-[var(--color-brick)] focus-visible:outline focus-visible:outline-1 focus-visible:outline-offset-1 focus-visible:outline-current"
      >
        ✕
      </button>
    </div>
  );
}
