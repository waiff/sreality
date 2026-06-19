/* Inline SVG icons. This repo has no icon library by design (CLAUDE.md frontend
 * territory) — icons are inline SVG / Unicode glyphs that inherit currentColor.
 * Collect the reused ones here so they aren't re-drawn ad hoc per component. All
 * are aria-hidden (decorative); the surrounding control carries the label. */

type IconProps = { className?: string; strokeWidth?: number };

/* The app's "pipeline" mark — a horizontal filter / sliders glyph (three tracks
 * with knobs). `filled` = solid knobs (in pipeline / active / entry stage);
 * unfilled = ring knobs. The tracks are gapped around each knob so the state
 * reads crisply on any surface. */
export function FilterIcon({
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
      <line x1="3" y1="7" x2="12.7" y2="7" />
      <line x1="17.3" y1="7" x2="21" y2="7" />
      <line x1="3" y1="12" x2="6.7" y2="12" />
      <line x1="11.3" y1="12" x2="21" y2="12" />
      <line x1="3" y1="17" x2="13.7" y2="17" />
      <line x1="18.3" y1="17" x2="21" y2="17" />
      <circle cx="15" cy="7" r="2.4" fill={filled ? 'currentColor' : 'none'} />
      <circle cx="9" cy="12" r="2.4" fill={filled ? 'currentColor' : 'none'} />
      <circle cx="16" cy="17" r="2.4" fill={filled ? 'currentColor' : 'none'} />
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
