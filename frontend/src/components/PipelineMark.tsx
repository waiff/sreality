/* THE deal-pipeline mark (rule #22): the funnel, plus the stage badge when the
 * property is on the board. One component so the Browse card, the Table row,
 * the listing header and the kanban all render the same glyph at the same
 * sizes — the extension reproduces this shape by hand (vanilla TS, no React),
 * and its inline SVG is kept in step with icons.tsx:FunnelIcon.
 *
 * Purely presentational: colour comes from `currentColor`, so the surrounding
 * control owns the tint (stageAccent) and hover/disabled states. The badge is a
 * sibling numeral rather than an overlay on the glyph — at the 14px icon size
 * the cards use, a digit inside the funnel is unreadable, and a two-character
 * code ("10", "2b") does not fit at all.
 */

import { FunnelIcon } from '@/components/icons';

export interface PipelineMarkProps {
  /* The stage badge, already resolved (lib/pipelineStage.ts:stageBadge). Null
   * renders the bare funnel — either the property is not in the pipeline, or
   * its stage has no code and no ordinal could be resolved. */
  badge?: string | null;
  /* Solid funnel body = the property is on the board. */
  filled?: boolean;
  iconClassName?: string;
  badgeClassName?: string;
}

export default function PipelineMark({
  badge = null,
  filled = false,
  iconClassName = 'h-3.5 w-3.5',
  badgeClassName = 'text-[0.6rem]',
}: PipelineMarkProps) {
  return (
    <span className="inline-flex items-center gap-[0.15rem] leading-none">
      <FunnelIcon filled={filled} className={`${iconClassName} shrink-0`} />
      {badge ? (
        <span
          className={`font-mono tabular-nums font-medium ${badgeClassName}`}
          aria-hidden
        >
          {badge}
        </span>
      ) : null}
    </span>
  );
}
