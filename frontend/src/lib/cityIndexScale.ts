/* THE city-quality index colour scale — one definition, every surface.
 *
 * WHY THIS EXISTS
 * The map used to carry its own ramp (`#c0392b → #f1c40f → #2ecc71`, red →
 * yellow → green) hardcoded to absolute 0/5/10 stops. Three problems, all
 * fixed here:
 *
 *   1. RAINBOW. Three hues with a hue (yellow) at the midpoint is the textbook
 *      diverging anti-pattern, and red↔green is precisely the deuteranopia /
 *      protanopia confusion pair. The scale below is a proper diverging ramp —
 *      two hues around a NEUTRAL midpoint — drawn from the app's own semantic
 *      tokens. Validated: worst adjacent CVD ΔE 21.9 (light) / 16.9 (dark),
 *      both comfortably over the ≥8 bar.
 *   2. THE LEGEND LIED. The fill interpolated against literal 0/5/10 while the
 *      legend printed the definition's `scale_min`/`scale_max` underneath. Any
 *      index not on a 0–10 scale rendered with a legend that disagreed with the
 *      paint. `normalize()` below drives BOTH off the definition, so they can't
 *      diverge again. (Dormant today — every seeded definition is 0–10 — which
 *      is exactly why it would have bitten silently.)
 *   3. `higher_is_better` WAS IGNORED. It is stored per definition and was read
 *      by nothing, so an index where low is good (an unemployment or pollution
 *      index) would have painted backwards. Currently every seeded definition
 *      is `true`, so this is prevention, not a live fix.
 *
 * ONE RAMP, TWO RENDERINGS. The map interpolates continuously across the three
 * stops (it has the space). A 16px card cell does not — low-alpha continuous
 * tints measured ΔE 3.5–4.9 between adjacent steps, i.e. indistinguishable
 * even with full colour vision — so cards snap to the nearest of the three
 * bands and print the value for precision. Same stops, same semantics.
 */

import type { CityIndexDefinition } from './queries';

/** Scale positions, expressed as a fraction of the definition's domain. */
export const CITY_INDEX_STOPS: readonly number[] = [0, 0.5, 1];

/** Resolved hexes per theme. Light = --color-brick / --color-ink-4 /
 *  --color-sage; dark = their dark-mode counterparts. MapLibre paint
 *  expressions need literal hex at layer-build time, which is why these are
 *  duplicated out of CSS rather than read from custom properties. */
export const CITY_INDEX_HEX = {
  light: ['#a04b3d', '#b0b1b7', '#5e7a4a'] as const,
  dark: ['#c47767', '#50535b', '#97b27e'] as const,
} as const;

/** The three bands, low → high. `mid` is deliberately near-neutral: it is the
 *  midpoint of a diverging scale, not a third category. */
export type IndexBand = 'low' | 'mid' | 'high';

export const INDEX_BANDS: readonly IndexBand[] = ['low', 'mid', 'high'];

/** CSS custom properties per band — the DOM side of the same scale, so a card
 *  chip follows the theme without a re-render. */
export const INDEX_BAND_VAR: Record<IndexBand, string> = {
  low: 'var(--color-brick)',
  mid: 'var(--color-ink-4)',
  high: 'var(--color-sage)',
};

/* A definition whose domain is degenerate (scale_max <= scale_min) would make
 * normalize() divide by zero. Seeded data is always 0–10, but the columns are
 * operator-writable, so clamp rather than emit NaN. */
const domainOf = (def: Pick<CityIndexDefinition, 'scale_min' | 'scale_max'>) => {
  const min = Number(def.scale_min);
  const max = Number(def.scale_max);
  if (!Number.isFinite(min) || !Number.isFinite(max) || max <= min) {
    return { min: 0, max: 10 };
  }
  return { min, max };
};

/** Map a raw index value onto 0..1 where 1 is ALWAYS "good", honouring the
 *  definition's domain and its `higher_is_better` flag. Returns null when the
 *  value is absent or non-finite — callers must render "no reading" distinctly
 *  from a mid-scale reading, never as a mid-band colour. */
export function normalizeIndexValue(
  value: number | null | undefined,
  def: Pick<CityIndexDefinition, 'scale_min' | 'scale_max' | 'higher_is_better'>,
): number | null {
  if (value == null || !Number.isFinite(value)) return null;
  const { min, max } = domainOf(def);
  const t = Math.min(1, Math.max(0, (value - min) / (max - min)));
  return def.higher_is_better === false ? 1 - t : t;
}

/** Snap a normalized 0..1 reading to its band. Thirds: the scale has three
 *  stops, so the band boundaries sit halfway between adjacent stops. */
export function bandForNormalized(t: number | null): IndexBand | null {
  if (t == null) return null;
  if (t < 0.25) return 'low';
  if (t < 0.75) return 'mid';
  return 'high';
}

/** Convenience: raw value + definition → band, or null for "no reading". */
export function bandForValue(
  value: number | null | undefined,
  def: Pick<CityIndexDefinition, 'scale_min' | 'scale_max' | 'higher_is_better'>,
): IndexBand | null {
  return bandForNormalized(normalizeIndexValue(value, def));
}

/** The MapLibre `interpolate` stop list for a NORMALIZED (0..1) input, as a
 *  flat [stop, color, stop, color, …] array. Feeding normalized values rather
 *  than raw ones is what keeps the paint and the legend on one domain. */
export const mapRampStops = (mode: 'light' | 'dark' = 'light'): (number | string)[] =>
  CITY_INDEX_STOPS.flatMap((stop, i) => [stop, CITY_INDEX_HEX[mode][i]]);

/** CSS gradient for a legend, in the same order and the same colours. */
export const legendGradient = (mode: 'light' | 'dark' = 'light'): string =>
  `linear-gradient(to right, ${CITY_INDEX_HEX[mode]
    .map((c, i) => `${c} ${CITY_INDEX_STOPS[i] * 100}%`)
    .join(', ')})`;
