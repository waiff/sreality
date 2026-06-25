import { imageTagLabel } from '@/lib/imageTags';

/* The CLIP image-tag pill shown in the bottom-left corner of a listing photo.
 * Self-guards: renders nothing when the image has no tag yet (most images during
 * the CLIP backfill ramp). The caller owns POSITIONING (passes the absolute
 * corner classes) so one badge serves the shared carousel, the gallery, and the
 * comparables modal; this component owns only the pill look + the confidence
 * tooltip. Mirrors the carousel's "n / total" counter treatment. */

interface Props {
  tag: string | null | undefined;
  /* CLIP softmax confidence 0..1; shown as a percentage in the hover tooltip. */
  confidence?: number | null;
  /* Positioning + sizing classes from the caller (e.g. the absolute corner). */
  className?: string;
}

export default function ImageTagBadge({ tag, confidence, className = '' }: Props) {
  const label = imageTagLabel(tag);
  if (!label) return null;
  const title =
    confidence != null ? `CLIP ${Math.round(confidence * 100)} %` : 'CLIP';
  return (
    <span
      title={title}
      className={[
        'pointer-events-none px-1.5 py-0.5 text-[0.6rem] tracking-[0.08em] uppercase',
        'rounded-[var(--radius-xs)] bg-[var(--color-paper-3)]/85 border border-[var(--color-rule)]',
        'text-[var(--color-ink-2)] backdrop-blur-sm',
        className,
      ].join(' ')}
    >
      {label}
    </span>
  );
}
