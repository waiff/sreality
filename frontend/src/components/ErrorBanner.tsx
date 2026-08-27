/* One failed-write/failed-read banner for both tag-annotation pages. Extracted
 * byte-identical from the two copies it replaces — a duplicated banner that
 * drifts is how the same failure starts reading differently on two screens. */
export default function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="mt-6 p-3 rounded-[var(--radius-sm)] border border-[var(--color-brick)]/30 bg-[var(--color-brick-soft)] text-sm text-[var(--color-brick)]">
      <strong className="font-medium">Failed:</strong> {message}
    </div>
  );
}
