/* A placeholder block for content that is still loading.
 *
 * Suspense boundaries around code-split sections used `fallback={null}`, which
 * means the section is simply absent until its chunk arrives — the page paints
 * short, then jumps when the block appears. That reads as breakage, and it
 * became load-bearing with `lazyChunk`: a stale chunk after a deploy now holds
 * its fallback for the ~0.3-2s the recovery reload takes, so the fallback is
 * exactly what the user looks at. `null` would show them a page with a hole in
 * it and no explanation.
 *
 * Deliberately plain: a token-styled surface with reserved height, no shimmer
 * animation. It reads as "this is coming" without competing for attention. */
export default function Skeleton({
  height,
  className = '',
}: {
  /* Reserve the block's approximate final height so the page doesn't jump. */
  height: number;
  className?: string;
}) {
  return (
    <div
      aria-hidden
      style={{ height }}
      className={`rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] ${className}`}
    />
  );
}
