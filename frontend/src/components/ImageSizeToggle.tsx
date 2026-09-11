/* The small/large photo-size switch — shared by every grid that offers the
 * choice as a BOOLEAN (Browse's listing cards, the NEW DEDUP labeling review,
 * training-set and ranking grids). Presentation only: the caller owns the flag
 * (a persisted workspace preference, see `@/lib/persistedFlag`) and what
 * "large" does to its own grid. Those grids all express it the same way — one
 * `--*-min` custom property whose large value is exactly double the small one —
 * so "small" and "large" can never come to mean different things on different
 * pages.
 *
 * The pill chrome itself lives in `SizeToggle`, which also serves the
 * deal-pipeline board's three-step version. This wrapper exists because the
 * four boolean callers read better as `large={…} onChange={…}` than as a
 * two-member step list, and because the two glyphs are THIS switch's vocabulary
 * (many frames vs one big frame). */

import SizeToggle, { LargeImageGlyph, SmallImageGlyph } from '@/components/SizeToggle';

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
  return (
    <SizeToggle
      label={label}
      value={large ? 'lg' : 'sm'}
      onChange={(v) => onChange(v === 'lg')}
      steps={[
        { value: 'sm', label: 'Small', title: smallTitle, glyph: <SmallImageGlyph /> },
        { value: 'lg', label: 'Large', title: largeTitle, glyph: <LargeImageGlyph /> },
      ]}
    />
  );
}
