import { describe, expect, it } from 'vitest';
import {
  bandForValue,
  CITY_INDEX_HEX,
  CITY_INDEX_STOPS,
  legendGradient,
  mapRampStops,
  normalizeIndexValue,
} from './cityIndexScale';
import { CARD_INDEX_ABBR, CARD_INDEX_SLUGS, PINNED_SLUGS } from './cityIndexes';
import type { CityIndexDefinition } from './queries';

const def = (over: Partial<CityIndexDefinition> = {}): CityIndexDefinition => ({
  index_name: 'celkove_hodnoceni',
  label_cs: 'Celkové hodnocení',
  label_en: null,
  category: 'overall',
  scale_min: 0,
  scale_max: 10,
  higher_is_better: true,
  sort_order: 0,
  description: null,
  ...over,
});

describe('normalizeIndexValue', () => {
  it('maps the definition domain onto 0..1', () => {
    expect(normalizeIndexValue(0, def())).toBe(0);
    expect(normalizeIndexValue(5, def())).toBe(0.5);
    expect(normalizeIndexValue(10, def())).toBe(1);
  });

  it('uses scale_min/scale_max, not a hardcoded 0..10', () => {
    // The old map ramp interpolated against literal 0/5/10 while the legend
    // printed these bounds — this is the regression guard for that mismatch.
    const d = def({ scale_min: 100, scale_max: 200 });
    expect(normalizeIndexValue(150, d)).toBe(0.5);
    expect(normalizeIndexValue(100, d)).toBe(0);
    expect(normalizeIndexValue(200, d)).toBe(1);
  });

  it('inverts when higher_is_better is false', () => {
    const d = def({ higher_is_better: false });
    expect(normalizeIndexValue(0, d)).toBe(1);
    expect(normalizeIndexValue(10, d)).toBe(0);
  });

  it('clamps out-of-domain readings instead of extrapolating colour', () => {
    expect(normalizeIndexValue(-4, def())).toBe(0);
    expect(normalizeIndexValue(99, def())).toBe(1);
  });

  it('falls back to 0..10 on a degenerate domain rather than dividing by zero', () => {
    expect(normalizeIndexValue(5, def({ scale_min: 7, scale_max: 7 }))).toBe(0.5);
  });

  it('returns null for absent or non-finite readings', () => {
    expect(normalizeIndexValue(null, def())).toBeNull();
    expect(normalizeIndexValue(undefined, def())).toBeNull();
    expect(normalizeIndexValue(NaN, def())).toBeNull();
  });
});

describe('bandForValue', () => {
  it('snaps to low / mid / high', () => {
    expect(bandForValue(1, def())).toBe('low');
    expect(bandForValue(5, def())).toBe('mid');
    expect(bandForValue(9, def())).toBe('high');
  });

  it('keeps "no reading" distinct from mid-scale', () => {
    expect(bandForValue(null, def())).toBeNull();
    expect(bandForValue(5, def())).toBe('mid');
  });

  it('follows higher_is_better', () => {
    const d = def({ higher_is_better: false });
    expect(bandForValue(1, d)).toBe('high');
    expect(bandForValue(9, d)).toBe('low');
  });
});

describe('one ramp, two renderings', () => {
  it('the map stop list and the legend gradient use the same colours in the same order', () => {
    const stops = mapRampStops('light');
    expect(stops).toEqual([0, '#a04b3d', 0.5, '#b0b1b7', 1, '#5e7a4a']);
    const grad = legendGradient('light');
    for (const hex of CITY_INDEX_HEX.light) expect(grad).toContain(hex);
  });

  it('has one colour per stop in both themes', () => {
    expect(CITY_INDEX_HEX.light).toHaveLength(CITY_INDEX_STOPS.length);
    expect(CITY_INDEX_HEX.dark).toHaveLength(CITY_INDEX_STOPS.length);
  });
});

describe('card index slugs', () => {
  it('are the leading PINNED_SLUGS, so the two lists cannot drift', () => {
    expect(CARD_INDEX_SLUGS).toEqual(PINNED_SLUGS.slice(0, CARD_INDEX_SLUGS.length));
  });

  it('each have an abbreviation', () => {
    for (const slug of CARD_INDEX_SLUGS) {
      expect(CARD_INDEX_ABBR[slug]).toMatch(/^[A-ZČŘŠŽ]{2}$/);
    }
  });
});
