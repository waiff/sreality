import type { BorderCaseStore } from '@/lib/useBorderCases';

/* "Border case" — the one-click quarantine flag (migration 310) shared by every
 * labeling surface. It is NOT a taxonomy label and not a review verdict: it
 * records that the image is unclear even to a human, and it is independent of
 * whether the image also carries a training label or a confirmed/dismissed
 * proposal. A flag has no value of its own to edit, so there is no picker and no
 * separate remove link — clicking again unflags.
 *
 * Presentational only; the state and the writes live in `useBorderCases`, so the
 * two grids that render this can never drift apart on what a click means. */
export default function BorderCaseButton({
  imageId,
  store,
  className = '',
}: {
  imageId: number;
  store: BorderCaseStore;
  className?: string;
}) {
  const flagged = store.has(imageId);
  const pending = store.isPending(imageId);
  return (
    <button
      type="button"
      onClick={() => store.toggle(imageId)}
      disabled={pending}
      aria-pressed={flagged}
      title={
        flagged
          ? 'Border case — click to clear'
          : "Unclear even to a human — park it as a border case (doesn't label it)"
      }
      className={[
        'shrink-0 px-2 py-1 text-[0.72rem] rounded-[var(--radius-xs)] border transition-colors disabled:opacity-50',
        'border-[var(--color-brick)] text-[var(--color-brick)]',
        flagged ? 'bg-[var(--color-brick-soft)]' : 'hover:bg-[var(--color-brick-soft)]',
        className,
      ].join(' ')}
    >
      {pending ? '…' : flagged ? '✓ Border case' : 'Border case'}
    </button>
  );
}
