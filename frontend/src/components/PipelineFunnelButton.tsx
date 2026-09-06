/* The compact pipeline control used wherever a property appears in a LIST — the
 * Browse cards and the Table rows.
 *
 * Out of the pipeline, a click bookmarks into the entry stage (rule #22): one
 * cheap, reversible keystroke in the middle of triage. Already in it, a click
 * opens the shared `<PipelineStageMenu>` — the same menu the listing header
 * opens — to move the card or take it off the board behind a confirm. It used
 * to REMOVE on that second click, unconfirmed and undoable, which made a stray
 * click in a 60-card grid a silent data loss, and left no way to advance a deal
 * without opening the listing page.
 *
 * Membership and the stage list are two shared queries (pipelineKeys.members /
 * .stages) that React Query dedupes across every button on screen — a grid of
 * 60 cards issues two reads, not 120.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';

import PipelineMark from '@/components/PipelineMark';
import PipelineStageMenu from '@/components/pipeline/PipelineStageMenu';
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
}

export default function PipelineFunnelButton({
  property_id,
  cohortScoped = false,
  variant = 'overlay',
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

  const { add, pending } = usePipelineCard(property_id, { cohortScoped });
  const btnRef = useRef<HTMLButtonElement>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  /* Stable so the popover's positioning effect doesn't re-subscribe each render. */
  const closeMenu = useCallback(() => setMenuOpen(false), []);

  /* Drop the open flag when the property leaves the pipeline from somewhere else
   * (the kanban trash, the listing header). The menu unmounts either way — but a
   * flag left true would spring it open by itself the moment the property was
   * bookmarked again. */
  useEffect(() => {
    if (!inPipeline) setMenuOpen(false);
  }, [inPipeline]);

  const label = inPipeline
    ? `V pipeline (${member.stage_label}) — změnit fázi`
    : 'Přidat do pipeline';

  return (
    <>
      <button
        ref={btnRef}
        type="button"
        /* No preventDefault/stopPropagation: the funnel used to sit INSIDE the
         * Browse card's detail <Link>, and swallowing the event was the only
         * thing keeping a bookmark click from navigating. It is a sibling of
         * that link now (ListingCards), and the Table renders it in its own
         * cell, so the click reaches nothing else either way. */
        onClick={() => {
          if (pending) return;
          if (inPipeline) setMenuOpen((v) => !v);
          else add.mutate();
        }}
        disabled={pending}
        aria-pressed={inPipeline}
        aria-haspopup={inPipeline ? 'menu' : undefined}
        aria-expanded={inPipeline ? menuOpen : undefined}
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
      {menuOpen && member && (
        <PipelineStageMenu
          property_id={property_id}
          stageId={member.stage_id}
          cohortScoped={cohortScoped}
          anchorRef={btnRef}
          onClose={closeMenu}
        />
      )}
    </>
  );
}
