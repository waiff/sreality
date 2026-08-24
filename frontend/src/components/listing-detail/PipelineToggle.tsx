/* The deal-pipeline control for ONE property, sized for the listing-detail
 * header action bar (next to "New estimation").
 *
 * "Bookmark / interested" == the entry stage (rule #22): presence of a card ==
 * the property is in the pipeline. Out of pipeline → a copper funnel "Přidat do
 * pipeline" (the app's one accent, marking THE deal-tracking verb). In pipeline
 * → a pill tinted with the current stage's colour that opens the shared
 * `<PipelineStageMenu>`: change stage, or remove behind the confirm.
 *
 * That menu is the same component the Browse funnels open. It replaced a native
 * <select> plus a bare ✕ here — three surfaces had grown three different
 * answers to "the property is in the pipeline, now what", and only one of them
 * (the kanban) asked before dropping a card. Writes still go through the shared
 * `usePipelineCard` hook, so every surface issues the same audited PATCH and
 * gets the same cache policy.
 */

import { useCallback, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { fetchPipelineMembers, fetchPipelineStages, pipelineKeys } from '@/lib/queries';
import PipelineMark from '@/components/PipelineMark';
import PipelineStageMenu from '@/components/pipeline/PipelineStageMenu';
import { stageAccent, stageBadge } from '@/lib/pipelineStage';
import { usePipelineCard } from '@/lib/usePipelineCard';

export default function PipelineToggle({ property_id }: { property_id: number }) {
  // W3: the shared members map (also what every Browse/Table funnel reads),
  // not a separate per-property fetch — one query answers "is this property
  // in the pipeline, at which stage" for every surface.
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
  const card = membersQ.data?.get(property_id) ?? null;
  const stages = stagesQ.data ?? [];
  const inPipeline = card != null;

  // Writes — and the cache policy every other surface depends on — live in the
  // shared hook. A listing page is never itself a pipeline-scoped cohort, so it
  // has no reason to invalidate the Browse read surfaces.
  const { add, pending } = usePipelineCard(property_id);
  const pillRef = useRef<HTMLButtonElement>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const closeMenu = useCallback(() => setMenuOpen(false), []);

  if (membersQ.isLoading) {
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
        <PipelineMark filled={false} iconClassName="h-4 w-4" />
        <span>Přidat do pipeline</span>
      </button>
    );
  }

  /* Accent + badge come from the shared stage helpers, so this pill, the Browse
   * card funnels and the board all render one stage the same way. */
  const look = { id: card.stage_id, color: card.stage_color, code: card.stage_code };
  const { fg, soft: bg } = stageAccent(look);
  const badge = stageBadge(look, stages);

  return (
    <>
      <button
        ref={pillRef}
        type="button"
        onClick={() => setMenuOpen((v) => !v)}
        disabled={pending}
        aria-haspopup="menu"
        aria-expanded={menuOpen}
        aria-label={`V pipeline (${card.stage_label}) — změnit fázi`}
        title="Změnit fázi nebo odebrat z pipeline"
        className="inline-flex items-center gap-1.5 rounded-[var(--radius-sm)] border py-1.5 pl-2.5 pr-2 text-[0.8rem] transition-opacity disabled:opacity-60"
        style={{ background: bg, color: fg, borderColor: fg }}
      >
        <PipelineMark filled badge={badge} iconClassName="h-4 w-4" badgeClassName="text-[0.7rem]" />
        <span className="font-medium">{card.stage_label}</span>
        <span className="text-[0.6rem] leading-none opacity-70" aria-hidden>
          ▾
        </span>
      </button>
      {menuOpen && (
        <PipelineStageMenu
          property_id={property_id}
          stageId={card.stage_id}
          anchorRef={pillRef}
          onClose={closeMenu}
        />
      )}
    </>
  );
}
