/* Inline SVG icons. This repo has no icon library by design (CLAUDE.md frontend
 * territory) — icons are inline SVG / Unicode glyphs that inherit currentColor.
 * Collect the reused ones here so they aren't re-drawn ad hoc per component. All
 * are aria-hidden (decorative); the surrounding control carries the label. */

type IconProps = { className?: string; strokeWidth?: number };

/* The app's "pipeline" mark — a funnel with three arrows feeding into it (the
 * deal-funnel metaphor). `filled` = solid funnel body (in pipeline / active /
 * entry stage); unfilled = outline. The arrows stay outline in both states so
 * the fill cleanly signals membership. */
export function FunnelIcon({
  filled = false,
  className = 'h-4 w-4',
  strokeWidth = 1.75,
}: IconProps & { filled?: boolean }) {
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
      {/* three arrows feeding the funnel */}
      <line x1="7.5" y1="2" x2="7.5" y2="5.5" />
      <polyline points="6.2,4 7.5,5.7 8.8,4" />
      <line x1="12" y1="2" x2="12" y2="5.5" />
      <polyline points="10.7,4 12,5.7 13.3,4" />
      <line x1="16.5" y1="2" x2="16.5" y2="5.5" />
      <polyline points="15.2,4 16.5,5.7 17.8,4" />
      {/* funnel: wide rim → taper → short spout */}
      <path
        d="M4 8 H20 L13.5 15 V21 H10.5 V15 Z"
        fill={filled ? 'currentColor' : 'none'}
      />
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
