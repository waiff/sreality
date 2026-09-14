/* A share-of-base bar: thin, rounded at the data end, on a recessive track.
 * `title` gives it the hover reading every mark on a chart owes the reader.
 *
 * IT LIVES ON ITS OWN (W16) because it is GEOMETRY, not a reported number. The
 * funnel next to it may not derive a count, a loss or a share — those come from
 * the store — and the rail that enforces that greps the component for
 * arithmetic. So the one division this mark needs sits here instead.
 *
 * Pass `pct` when the producer already measured the share (the funnel does);
 * pass `value` + `base` when the bar is a ratio the page is drawing for itself
 * (a histogram against its own tallest bar).
 */
export default function ProportionBar({
  value,
  base,
  pct,
  title,
}: {
  value?: number | null;
  base?: number;
  pct?: number | null;
  title?: string;
}) {
  const share =
    pct != null
      ? Math.min(100, pct)
      : value == null || base == null || base <= 0
        ? null
        : Math.min(100, (value / base) * 100);
  return (
    <div
      className="h-1.5 w-full rounded-full bg-[var(--color-inset)] overflow-hidden"
      title={title}
      aria-hidden
    >
      {share != null && (
        <div
          className="h-full rounded-full bg-[var(--color-copper)]"
          style={{ width: `${Math.max(share, share > 0 ? 0.5 : 0)}%` }}
        />
      )}
    </div>
  );
}
