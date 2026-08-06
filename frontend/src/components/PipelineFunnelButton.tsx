/* The compact pipeline control used wherever a property appears in a LIST — the
 * Browse cards and the Table rows. Click = bookmark into the entry stage /
 * un-bookmark, the same audited API path every other surface uses; the mark
 * shows the current stage's badge in that stage's colour once the property is
 * on the board.
 *
 * Membership and the stage list are two shared queries (pipelineKeys.members /
 * .stages) that React Query dedupes across every button on screen — a grid of
 * 60 cards issues two reads, not 120.
 */

import { useQuery } from '@tanstack/react-query';

import PipelineMark from '@/components/PipelineMark';
import { fetchPipelineMembers, fetchPipelineStages, pipelineKeys } from '@/lib/queries';
import { stageAccent, stageBadge } from '@/lib/pipelineStage';
import { usePipelineCard } from '@/lib/usePipelineCard';

export interface PipelineFunnelButtonProps {
  property_id: number;
  /* True when the surrounding cohort is filtered by pipeline membership — a
   * write then changes which rows match, so Browse must refetch. */
  cohortScoped?: boolean;
  /* Cards float this over the photo (translucent chrome); the table renders it
   * inline on the row background. */
  variant?: 'overlay' | 'inline';
  /* Stop the click from triggering an enclosing Link / row navigation. */
  stopPropagation?: boolean;
}

export default function PipelineFunnelButton({
  property_id,
  cohortScoped = false,
  variant = 'overlay',
  stopPropagation = true,
}: PipelineFunnelButtonProps) {
  const membersQ = useQuery({
    queryKey: pipelineKeys.members,
    queryFn: fetchPipelineMembers,
    staleTime: 30_000,
  });
  const stagesQ = useQuery({
    queryKey: pipelineKeys.stages,
    queryFn: fetchPipelineStages,
    staleTime: 60_000,
  });

  const member = membersQ.data?.get(property_id) ?? null;
  const inPipeline = member != null;
  const look = member
    ? { id: member.stage_id, color: member.stage_color, code: member.stage_code }
    : null;
  const badge = look ? stageBadge(look, stagesQ.data ?? []) : null;
  const accent = stageAccent(look);

  const { add, remove, pending } = usePipelineCard(property_id, { cohortScoped });

  const label = inPipeline
    ? `V pipeline (${member.stage_label}) — odebrat`
    : 'Přidat do pipeline';

  return (
    <button
      type="button"
      onClick={(e) => {
        if (stopPropagation) {
          e.preventDefault();
          e.stopPropagation();
        }
        if (pending) return;
        (inPipeline ? remove : add).mutate();
      }}
      disabled={pending}
      aria-pressed={inPipeline}
      aria-label={label}
      title={label}
      style={inPipeline ? { color: accent.fg, borderColor: accent.fg } : undefined}
      className={[
        'flex h-6 items-center justify-center rounded-[var(--radius-xs)] border transition-colors disabled:opacity-60',
        badge ? 'gap-0.5 px-1' : 'w-6',
        variant === 'overlay' ? 'backdrop-blur' : '',
        inPipeline
          ? variant === 'overlay'
            ? 'bg-[var(--color-paper-3)]/90'
            : 'bg-transparent'
          : variant === 'overlay'
            ? 'bg-[var(--color-paper-3)]/85 border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-copper)] hover:border-[var(--color-copper)]'
            : 'border-transparent text-[var(--color-ink-4)] hover:text-[var(--color-copper)] hover:border-[var(--color-copper)]',
      ].join(' ')}
    >
      <PipelineMark filled={inPipeline} badge={badge} />
    </button>
  );
}
