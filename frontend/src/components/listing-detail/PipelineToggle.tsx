/* The deal-pipeline bookmark for ONE property, sized for the listing-detail
 * header action bar (next to "New estimation").
 *
 * "Bookmark / interested" == the entry stage (rule #22): presence of a card ==
 * the property is in the pipeline. Out of pipeline → a copper ☆ "Přidat do
 * pipeline" (the app's one accent, marking THE deal-tracking verb); in pipeline
 * → a ★ pill tinted with the current stage's colour, showing the stage label.
 * Clicking toggles add/remove — the same contract as the Browse-card ★, so the
 * star means the same thing on every surface. Reads the single card (for the
 * stage label); the Browse cards read a shared members-set instead, so a toggle
 * here invalidates both keys to keep the two surfaces in sync.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { addPipelineCard, removePipelineCard } from '@/lib/api';
import { fetchPropertyPipeline, pipelineKeys } from '@/lib/queries';
import { FunnelIcon } from '@/components/icons';

export default function PipelineToggle({ property_id }: { property_id: number }) {
  const qc = useQueryClient();

  const cardQ = useQuery({
    queryKey: pipelineKeys.card(property_id),
    queryFn: () => fetchPropertyPipeline(property_id),
    staleTime: 30_000,
  });
  const card = cardQ.data ?? null;
  const inPipeline = card != null;

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: pipelineKeys.card(property_id) });
    // Keep the Browse-card ★ (shared members-set) in sync with this toggle.
    qc.invalidateQueries({ queryKey: pipelineKeys.members });
  };
  const add = useMutation({
    mutationFn: () => addPipelineCard(property_id),
    onSuccess: invalidate,
  });
  const remove = useMutation({
    mutationFn: () => removePipelineCard(property_id),
    onSuccess: invalidate,
  });
  const pending = add.isPending || remove.isPending;

  const fg = card?.stage_color
    ? `var(--color-tag-${card.stage_color})`
    : 'var(--color-copper)';
  const bg = card?.stage_color
    ? `var(--color-tag-${card.stage_color}-soft)`
    : 'var(--color-copper-soft)';

  return (
    <button
      type="button"
      onClick={() => (inPipeline ? remove.mutate() : add.mutate())}
      disabled={pending || cardQ.isLoading}
      aria-pressed={inPipeline}
      title={inPipeline ? 'Odebrat z pipeline' : 'Přidat do pipeline'}
      className={[
        'inline-flex items-center gap-1.5 px-3 py-1.5 text-[0.8rem] rounded-[var(--radius-sm)] border transition-colors disabled:opacity-60',
        inPipeline
          ? ''
          : 'bg-[var(--color-paper-2)] border-[var(--color-rule)] text-[var(--color-ink-2)] hover:border-[var(--color-rule-strong)] hover:text-[var(--color-ink)]',
      ].join(' ')}
      style={inPipeline ? { background: bg, color: fg, borderColor: fg } : undefined}
    >
      <span
        aria-hidden
        className="inline-flex leading-none"
        style={inPipeline ? undefined : { color: 'var(--color-copper)' }}
      >
        <FunnelIcon filled={inPipeline} className="h-4 w-4" />
      </span>
      <span>{inPipeline ? (card?.stage_label ?? 'V pipeline') : 'Přidat do pipeline'}</span>
    </button>
  );
}
