/* The small/large photo-size switch — one segmented control, shared by every
 * grid that offers the choice (Browse's listing cards, the NEW DEDUP labeling
 * review grid). Presentation only: the caller owns the flag (a persisted
 * workspace preference, see `@/lib/persistedFlag`) and what "large" does to
 * its own grid. Both grids express it the same way — one `--*-min` custom
 * property whose large value is exactly double the small one — so "small" and
 * "large" can never come to mean different things on different pages.
 *
 * Same segmented-control idiom as Browse's MapViewToggle, which it sits
 * beside there. */

interface Props {
  large: boolean;
  onChange: (large: boolean) => void;
  /* What this switch sizes, for the a11y group name — "Card image size",
   * "Review grid image size". Each surface has exactly one, so the label is
   * what tells a screen-reader user which grid they're about to reshape. */
  label: string;
  /* Tooltips: what the two ends of THIS grid's range actually do. */
  smallTitle?: string;
  largeTitle?: string;
}

export default function ImageSizeToggle({
  large,
  onChange,
  label,
  smallTitle = 'Smaller photos, more columns',
  largeTitle = 'Bigger photos, fewer columns',
}: Props) {
  const seg = (active: boolean) =>
    [
      'inline-flex items-center gap-1.5 px-2.5 py-1 text-[0.7rem] rounded-[var(--radius-xs)] transition-colors',
      active
        ? 'bg-[var(--color-copper)] text-white'
        : 'text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)]',
    ].join(' ');
  return (
    <div
      role="group"
      aria-label={label}
      className="inline-flex items-center gap-0.5 p-0.5 rounded-[var(--radius-sm)] bg-[var(--color-paper-2)] border border-[var(--color-rule)]"
    >
      <button
        type="button"
        onClick={() => onChange(false)}
        aria-pressed={!large}
        title={smallTitle}
        className={seg(!large)}
      >
        <SmallImageGlyph />
        Small
      </button>
      <button
        type="button"
        onClick={() => onChange(true)}
        aria-pressed={large}
        title={largeTitle}
        className={seg(large)}
      >
        <LargeImageGlyph />
        Large
      </button>
    </div>
  );
}

/* Many small photo frames — the "small" choice. */
function SmallImageGlyph() {
  return (
    <svg
      width="13"
      height="13"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.3"
      strokeLinejoin="round"
      aria-hidden
    >
      <rect x="1.5" y="2" width="3.5" height="2.8" rx="0.6" />
      <rect x="6.5" y="2" width="3.5" height="2.8" rx="0.6" />
      <rect x="11.5" y="2" width="3" height="2.8" rx="0.6" />
      <rect x="1.5" y="6.4" width="3.5" height="2.8" rx="0.6" />
      <rect x="6.5" y="6.4" width="3.5" height="2.8" rx="0.6" />
      <rect x="11.5" y="6.4" width="3" height="2.8" rx="0.6" />
      <rect x="1.5" y="10.8" width="3.5" height="2.8" rx="0.6" />
      <rect x="6.5" y="10.8" width="3.5" height="2.8" rx="0.6" />
      <rect x="11.5" y="10.8" width="3" height="2.8" rx="0.6" />
    </svg>
  );
}

/* One big photo frame — the "large" choice. */
function LargeImageGlyph() {
  return (
    <svg
      width="13"
      height="13"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.3"
      strokeLinejoin="round"
      aria-hidden
    >
      <rect x="1.5" y="2.5" width="13" height="8.5" rx="1.2" />
      <path d="M1.5 8.5 L5.5 5.5 L8.5 8 L11 6 L14.5 9" />
    </svg>
  );
}

