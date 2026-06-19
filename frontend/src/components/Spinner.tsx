/* Shared inline spinner. `currentColor` + Tailwind's built-in animate-spin,
 * so the caller controls colour and size via the surrounding text styles.
 * Extracted from ListingCards so every loading affordance (cards, table,
 * the infinite-scroll sentinel, API feeds) reads the same. */
export default function Spinner({
  size = 11,
  className = '',
}: {
  size?: number;
  className?: string;
}) {
  return (
    <svg
      className={`animate-spin ${className}`.trim()}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden
    >
      <circle
        cx="12" cy="12" r="9"
        stroke="currentColor" strokeWidth="3" strokeOpacity="0.25"
      />
      <path
        d="M21 12a9 9 0 0 0-9-9"
        stroke="currentColor" strokeWidth="3" strokeLinecap="round"
      />
    </svg>
  );
}
