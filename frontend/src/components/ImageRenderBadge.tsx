/* The CLIP render-vs-photo pill (migration 239), shown bottom-RIGHT of a listing
 * photo (the room tag owns bottom-left). Lets the operator eyeball the render
 * detector: >= the engine's exclusion threshold reads "Render NN" (oxidised-brick
 * accent — these images are dropped from the byt merge signal), otherwise "Foto NN"
 * (neutral). Shows the score itself so a wrong call is obvious. Self-guards: renders
 * nothing until the image is CLIP-scored. Caller owns positioning. */

// Mirrors toolkit.dedup_engine.RENDER_SCORE_EXCLUDE_MIN (0.65) — keep in sync.
const RENDER_THRESHOLD = 0.65;

interface Props {
  /* CLIP render_score 0..1 (images_public.clip_render_score); null until scored. */
  renderScore?: number | null;
  /* Positioning + sizing classes from the caller (e.g. the absolute corner). */
  className?: string;
}

export default function ImageRenderBadge({ renderScore, className = '' }: Props) {
  if (renderScore == null) return null;
  const isRender = renderScore >= RENDER_THRESHOLD;
  const pct = Math.round(renderScore * 100);
  return (
    <span
      title={`CLIP render skóre ${pct} %`}
      className={[
        'pointer-events-none px-1.5 py-0.5 text-[0.6rem] tracking-[0.08em] uppercase',
        'rounded-[var(--radius-xs)] border backdrop-blur-sm',
        isRender
          ? 'bg-[var(--color-brick)]/15 border-[var(--color-brick)]/40 text-[var(--color-brick)]'
          : 'bg-[var(--color-paper-3)]/85 border-[var(--color-rule)] text-[var(--color-ink-3)]',
        className,
      ].join(' ')}
    >
      {isRender ? `Render ${pct}` : `Foto ${pct}`}
    </span>
  );
}
