/* One definition of how a deal-pipeline stage LOOKS, shared by every surface
 * that renders the funnel (rule #22): Browse cards, the Table, the listing
 * header, the kanban board and its stage editor.
 *
 * Before this module the accent had two answers — Pipeline.tsx fell back to
 * `--color-rule-strong` for an uncoloured stage while PipelineToggle.tsx fell
 * back to copper — so the same stage could render grey on the board and copper
 * on the listing page. Copper wins: it is THE deal-tracking accent (rule #22),
 * and an uncoloured stage should still read as "in pipeline". */

import type { PipelineStage, TagColor } from './types';

/* The minimum a caller needs to paint a stage: colour for the accent, code +
 * id for the badge. Satisfied structurally by PipelineStage and
 * PipelineMembership (via its stage_* fields, remapped by the caller). */
export interface StageLook {
  id: number;
  color: TagColor | null;
  code: string | null;
}

export interface StageAccent {
  /* Foreground / border colour. */
  fg: string;
  /* Tinted background for pills and active states. */
  soft: string;
}

export const stageAccent = (stage: Pick<StageLook, 'color'> | null): StageAccent =>
  stage?.color
    ? { fg: `var(--color-tag-${stage.color})`, soft: `var(--color-tag-${stage.color}-soft)` }
    : { fg: 'var(--color-copper)', soft: 'var(--color-copper-soft)' };

/* The badge the funnel renders.
 *
 * `code` (migration 377) is the operator's own short label for the stage — the
 * live board numbers its stages 1,2,3,4 and then 9,9,9 for the three closed
 * ones, so the number is NOT the ordinal and must never be derived from
 * `position`. When a stage has no code we fall back to its 1-based ordinal
 * among the live stages, ordered as the board is; that keeps a freshly created
 * stage badged sensibly without writing a guessed value into the database.
 *
 * `stages` is the ordered live stage list (pipeline_stages_public, already
 * ordered by position). A stage that isn't in it — archived under a card that
 * still points at it, or the list not yet loaded — falls back to no badge
 * rather than to a wrong number. */
export const stageBadge = (
  stage: StageLook,
  stages: ReadonlyArray<PipelineStage> = [],
): string | null => {
  if (stage.code) return stage.code;
  const idx = stages.findIndex((s) => s.id === stage.id);
  return idx < 0 ? null : String(idx + 1);
};
