/* Inline SVG icons. This repo has no icon library by design (CLAUDE.md frontend
 * territory) — icons are inline SVG / Unicode glyphs that inherit currentColor.
 * Collect the reused ones here so they aren't re-drawn ad hoc per component. All
 * are aria-hidden (decorative); the surrounding control carries the label. */

type IconProps = { className?: string; strokeWidth?: number };

/* The app's "pipeline" mark — a bookmark ribbon (the entry stage is literally
 * "bookmark / interested", rule #22). `filled` = solid ribbon (in pipeline /
 * entry stage); unfilled = outline. Used on the listing-detail header toggle,
 * Browse cards, and the stage-manager entry indicator so the "into the pipeline"
 * concept reads as one icon everywhere. */
export function BookmarkIcon({
  filled = false,
  className = 'h-4 w-4',
  strokeWidth = 1.75,
}: IconProps & { filled?: boolean }) {
  return (
    <svg
      viewBox="0 0 24 24"
      className={className}
      aria-hidden
      fill={filled ? 'currentColor' : 'none'}
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z" />
    </svg>
  );
}

export function TrashIcon({ className = 'h-4 w-4', strokeWidth = 1.6 }: IconProps) {
  return (
    <svg
      viewBox="0 0 24 24"
      className={className}
      aria-hidden
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M3 6h18" />
      <path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2" />
      <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
      <line x1="10" y1="11" x2="10" y2="17" />
      <line x1="14" y1="11" x2="14" y2="17" />
    </svg>
  );
}

export function InfoIcon({ className = 'h-3.5 w-3.5', strokeWidth = 1.75 }: IconProps) {
  return (
    <svg
      viewBox="0 0 24 24"
      className={className}
      aria-hidden
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <circle cx="12" cy="12" r="9" />
      <line x1="12" y1="11" x2="12" y2="16" />
      <circle cx="12" cy="8" r="0.6" fill="currentColor" stroke="none" />
    </svg>
  );
}
