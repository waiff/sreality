import type { CSSProperties } from 'react';
import { TAG_COLORS, type TagColor } from '@/lib/types';

/* The one colour-swatch picker for the whole app: a soft-fill + solid-border
 * swatch per TAG_COLORS, a ring on the selected one. Returns a fragment (no
 * wrapper) so each caller supplies its own flex container + spacing — the
 * filter-preset save modal, the tag pickers, and the pipeline stage editor all
 * render literally the same control. `size` matches the two existing footprints
 * (md = the preset modal's h-6, sm = the popover/inline pickers' h-5);
 * `ringOffsetVar` is the surface the selection ring sits on (paper in a modal,
 * paper-3 in a popover) so the ring offset blends into its background. */
export default function TagColorPicker({
  value,
  onChange,
  showNull = false,
  size = 'sm',
  ringOffsetVar = 'var(--color-paper-3)',
}: {
  value: TagColor | null;
  onChange: (color: TagColor | null) => void;
  showNull?: boolean;
  size?: 'sm' | 'md';
  ringOffsetVar?: string;
}) {
  const dim = size === 'md' ? 'h-6 w-6' : 'h-5 w-5';
  const base = `${dim} shrink-0 rounded-full border transition-shadow`;
  const ring = (selected: boolean) => (selected ? 'ring-2 ring-offset-1' : '');

  return (
    <>
      {showNull && (
        <button
          type="button"
          onClick={() => onChange(null)}
          aria-pressed={value === null}
          aria-label="Bez barvy"
          title="Bez barvy"
          style={
            {
              background: 'var(--color-paper-2)',
              borderColor: 'var(--color-rule-strong)',
              ['--tw-ring-color' as string]: 'var(--color-ink-3)',
              ['--tw-ring-offset-color' as string]: ringOffsetVar,
            } as CSSProperties
          }
          className={`${base} ${ring(value === null)} flex items-center justify-center`}
        >
          <svg viewBox="0 0 24 24" className="h-3.5 w-3.5 text-[var(--color-ink-4)]" aria-hidden>
            <line x1="5" y1="19" x2="19" y2="5" stroke="currentColor" strokeWidth="2" />
          </svg>
        </button>
      )}
      {TAG_COLORS.map((c) => (
        <button
          key={c}
          type="button"
          onClick={() => onChange(c)}
          aria-pressed={value === c}
          aria-label={c}
          title={c}
          style={
            {
              background: `var(--color-tag-${c}-soft)`,
              borderColor: `var(--color-tag-${c})`,
              ['--tw-ring-color' as string]: `var(--color-tag-${c})`,
              ['--tw-ring-offset-color' as string]: ringOffsetVar,
            } as CSSProperties
          }
          className={`${base} ${ring(value === c)}`}
        />
      ))}
    </>
  );
}
