/* THE save-to-collection mark: a bookmark, filled when the property is in at
 * least one collection. One component so the Browse card's over-photo glyph and
 * the listing header's button render the same shape at the same sizes — the
 * collection analogue of <PipelineMark>, and deliberately NOT the funnel:
 * collections are m2m groupings (with optional monitoring), the pipeline is the
 * single-valued deal state (rule #22), and the two must never look alike.
 *
 * Purely presentational: colour is `currentColor`, so the surrounding control
 * owns the tint and its hover/disabled states.
 */

export default function CollectionMark({
  filled = false,
  className = 'h-3.5 w-3.5',
}: {
  filled?: boolean;
  className?: string;
}) {
  return (
    <svg
      viewBox="0 0 16 16"
      className={`${className} shrink-0`}
      fill={filled ? 'currentColor' : 'none'}
      stroke="currentColor"
      strokeWidth="1.3"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M4 2.5 H12 V13.5 L8 10.75 L4 13.5 Z" strokeLinecap="round" />
    </svg>
  );
}
