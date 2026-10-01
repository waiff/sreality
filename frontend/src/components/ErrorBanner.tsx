/* One failed-write/failed-read banner. Extracted byte-identical from the two
 * tag-annotation copies it replaced — a duplicated banner that drifts is how
 * the same failure starts reading differently on two screens. `onRetry` adds
 * the refetch affordance a failed READ deserves (a stuck page must offer a way
 * forward, not just a verdict); writes keep their own retry semantics. */
export default function ErrorBanner({
  message,
  title = 'Failed:',
  onRetry,
}: {
  message: string;
  title?: string;
  onRetry?: () => void;
}) {
  return (
    <div className="mt-6 p-3 rounded-[var(--radius-sm)] border border-[var(--color-brick)]/30 bg-[var(--color-brick-soft)] text-sm text-[var(--color-brick)]">
      <strong className="font-medium">{title}</strong> {message}
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="ml-3 font-medium underline underline-offset-2 hover:no-underline"
        >
          Zkusit znovu
        </button>
      )}
    </div>
  );
}
