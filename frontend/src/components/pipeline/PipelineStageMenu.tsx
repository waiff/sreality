/* THE deal-pipeline card menu (rule #22): move this property to another stage,
 * or take it off the board.
 *
 * One menu for every surface that carries the funnel — the Browse cards, the
 * Browse table rows and the listing-detail header all open this exact component
 * from their own `<PipelineMark>`. Before it, each surface had its own answer
 * to "the property is already in the pipeline, now what": Browse REMOVED it on
 * a single unconfirmed click (a stray click in a 60-card grid silently dropped
 * a deal, and there was no way to advance one without leaving the page), while
 * the listing header offered a native <select> plus an unconfirmed ✕.
 *
 * Removal is the only destructive action in the pipeline and it has no undo:
 * `remove_card` DELETEs the row, so the card's `added_at` / `entered_stage_at`
 * / board position are gone and re-adding stamps a fresh `added_at` — quietly
 * resetting "in pipeline since" and every time-in-stage figure the board sorts
 * on. (The stage-transition trail survives in `property_pipeline_events`, and
 * operator notes are property-grain — rule #18 — so those are never at risk.)
 * Hence the two-step confirm, matching the kanban trash, and the nudge toward a
 * terminal stage: closing a deal into "Passed / Bought / Lost" keeps the record.
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';

import AnchoredPopover from '@/components/AnchoredPopover';
import { fetchPipelineStages, pipelineKeys } from '@/lib/queries';
import { stageAccent, stageBadge } from '@/lib/pipelineStage';
import { usePipelineCard } from '@/lib/usePipelineCard';
import type { PipelineStage } from '@/lib/types';

export interface PipelineStageMenuProps {
  property_id: number;
  /* The stage the card sits at now — checked in the list, and never a move target. */
  stageId: number;
  /* True when the surrounding cohort is filtered by pipeline membership, so a
   * write changes which rows match and Browse has to refetch. */
  cohortScoped?: boolean;
  /* The control the menu hangs off (the funnel button / the header pill). */
  anchorRef: React.RefObject<HTMLElement | null>;
  onClose: () => void;
}

export default function PipelineStageMenu({
  property_id,
  stageId,
  cohortScoped = false,
  anchorRef,
  onClose,
}: PipelineStageMenuProps) {
  const stagesQ = useQuery({
    queryKey: pipelineKeys.stages,
    queryFn: fetchPipelineStages,
    staleTime: 60_000,
  });
  const stages = useMemo(() => stagesQ.data ?? [], [stagesQ.data]);
  const { move, remove, pending } = usePipelineCard(property_id, { cohortScoped });
  const [confirming, setConfirming] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);

  /* Open on the current stage, the WAI-ARIA menu default for a radio group —
   * the operator's next move is almost always one step from where they are. */
  useEffect(() => {
    if (confirming || stages.length === 0) return;
    const el = listRef.current?.querySelector<HTMLElement>('[aria-checked="true"]');
    (el ?? listRef.current?.querySelector<HTMLElement>('[role^="menuitem"]'))?.focus();
  }, [confirming, stages.length]);

  /* Roving focus across the menu items (Up/Down/Home/End), so the menu is
   * operable without a mouse once it is open. */
  const onKeyDown = (e: React.KeyboardEvent) => {
    const keys = ['ArrowDown', 'ArrowUp', 'Home', 'End'];
    if (!keys.includes(e.key)) return;
    const items = [
      ...(listRef.current?.querySelectorAll<HTMLElement>(
        '[role^="menuitem"]:not([disabled])',
      ) ?? []),
    ];
    if (items.length === 0) return;
    e.preventDefault();
    const at = items.indexOf(document.activeElement as HTMLElement);
    const next =
      e.key === 'Home'
        ? 0
        : e.key === 'End'
          ? items.length - 1
          : e.key === 'ArrowDown'
            ? (at + 1) % items.length
            : (at - 1 + items.length) % items.length;
    items[next]?.focus();
  };

  const moveTo = (stage: PipelineStage) => {
    if (stage.id === stageId || pending) return;
    move.mutate(stage.id);
    onClose();
  };

  /* Index of the first closed stage, so the terminal block reads as the
   * outcomes it is rather than as three more steps. -1 when the operator's
   * board has none (or only terminal stages) and the divider is skipped. */
  const firstTerminal = stages.findIndex((s) => s.is_terminal);
  const hasTerminalSplit = firstTerminal > 0;

  return (
    <AnchoredPopover
      anchorRef={anchorRef}
      onClose={onClose}
      ariaLabel="Fáze v pipeline"
      className="w-[15.5rem] py-1"
    >
      <div ref={listRef} role="menu" aria-label="Fáze v pipeline" onKeyDown={onKeyDown}>
        <p className="px-2.5 pb-1 pt-1 text-[0.62rem] uppercase tracking-[0.18em] text-[var(--color-ink-4)]">
          Přesunout do fáze
        </p>

        {stagesQ.isLoading && (
          <p className="px-2.5 py-1.5 text-[0.75rem] text-[var(--color-ink-4)]">Načítám…</p>
        )}

        {stages.map((stage, i) => {
          const current = stage.id === stageId;
          const accent = stageAccent(stage);
          const badge = stageBadge(stage, stages);
          return (
            <div key={stage.id}>
              {hasTerminalSplit && i === firstTerminal && (
                <p className="mt-1 border-t border-[var(--color-rule-soft)] px-2.5 pb-1 pt-1.5 text-[0.62rem] uppercase tracking-[0.18em] text-[var(--color-ink-4)]">
                  Uzavřené
                </p>
              )}
              <button
                type="button"
                role="menuitemradio"
                aria-checked={current}
                tabIndex={-1}
                /* NOT disabled when it is the current stage — a disabled button
                 * can't hold focus, and this is the item the menu opens on.
                 * `moveTo` no-ops instead. */
                disabled={pending}
                onClick={() => moveTo(stage)}
                className={[
                  'flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-[0.8rem] transition-colors',
                  'disabled:cursor-default disabled:opacity-50',
                  current
                    ? 'cursor-default font-medium text-[var(--color-ink)]'
                    : 'text-[var(--color-ink-2)] hover:bg-[var(--color-paper-2)]',
                ].join(' ')}
                style={current ? { background: accent.soft } : undefined}
              >
                <span
                  aria-hidden
                  className="inline-flex h-4 min-w-4 shrink-0 items-center justify-center rounded-[var(--radius-xs)] border px-1 font-mono text-[0.62rem] tabular-nums"
                  style={{ color: accent.fg, borderColor: accent.fg }}
                >
                  {badge ?? '·'}
                </span>
                <span className="min-w-0 flex-1 truncate">{stage.label}</span>
                {current && (
                  <span
                    className="shrink-0 text-[0.7rem]"
                    style={{ color: accent.fg }}
                    aria-hidden
                  >
                    ✓
                  </span>
                )}
              </button>
            </div>
          );
        })}

        <div className="mt-1 border-t border-[var(--color-rule-soft)] pt-1">
          {confirming ? (
            <div className="px-2.5 py-1.5">
              <p className="text-[0.75rem] font-medium text-[var(--color-ink)]">
                Odebrat z pipeline?
              </p>
              <p className="mt-1 text-[0.7rem] leading-snug text-[var(--color-ink-3)]">
                Karta zmizí z nástěnky a ztratí „v pipeline od“ i „ve fázi od“ — po
                opětovném přidání se počítají znovu.
                {hasTerminalSplit
                  ? ' Uzavřený obchod raději přesuňte do některé z uzavřených fází.'
                  : ''}
              </p>
              <div className="mt-2 flex items-center gap-1.5">
                <button
                  type="button"
                  role="menuitem"
                  tabIndex={-1}
                  autoFocus
                  disabled={pending}
                  onClick={() => {
                    remove.mutate();
                    onClose();
                  }}
                  className="rounded-[var(--radius-sm)] border border-[var(--color-brick)] px-2 py-0.5 text-[0.72rem] text-[var(--color-brick)] transition-colors hover:bg-[var(--color-brick)]/10 disabled:opacity-50"
                >
                  Odebrat
                </button>
                <button
                  type="button"
                  role="menuitem"
                  tabIndex={-1}
                  onClick={() => setConfirming(false)}
                  className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] px-2 py-0.5 text-[0.72rem] text-[var(--color-ink-2)] transition-colors hover:border-[var(--color-rule-strong)] hover:bg-[var(--color-rule-soft)]"
                >
                  Zrušit
                </button>
              </div>
            </div>
          ) : (
            <button
              type="button"
              role="menuitem"
              tabIndex={-1}
              disabled={pending}
              onClick={() => setConfirming(true)}
              className="flex w-full items-center px-2.5 py-1.5 text-left text-[0.78rem] text-[var(--color-ink-3)] transition-colors hover:bg-[var(--color-brick-soft)] hover:text-[var(--color-brick)] disabled:opacity-50"
            >
              Odebrat z pipeline
            </button>
          )}
        </div>
      </div>
    </AnchoredPopover>
  );
}
