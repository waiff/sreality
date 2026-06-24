import { describe, expect, it } from 'vitest';
import { bboxAround, fromSearchParams, DEFAULT_FILTERS } from './filters';
import {
  DEFAULT_OVERLAY,
  browseFiltersForArea,
  browseUrlFromState,
  type ExploreAreaSeed,
  type ExploreOrigin,
} from './browseState';
import { DEFAULT_SORT } from './queries';
import type { ListingPublic } from './types';

const TREBIC: Pick<ExploreAreaSeed, 'lat' | 'lng'> = { lat: 49.2147, lng: 15.8816 };

/* A throwaway origin — the decoupling tests only care that it is IGNORED by the
 * cohort builders, so its contents are irrelevant. */
const SOME_ORIGIN: ExploreOrigin = {
  listing: { sreality_id: 42, is_active: true } as ListingPublic,
  images: [],
};

describe('bboxAround', () => {
  it('produces a well-ordered box ~km across centred on the point', () => {
    const b = bboxAround(TREBIC.lat, TREBIC.lng, 5);
    expect(b.west).toBeLessThan(b.east);
    expect(b.south).toBeLessThan(b.north);
    // centre is (lat, lng)
    expect((b.north + b.south) / 2).toBeCloseTo(TREBIC.lat, 6);
    expect((b.east + b.west) / 2).toBeCloseTo(TREBIC.lng, 6);
    // N–S span ≈ 5 km (1° lat ≈ 111.32 km)
    expect((b.north - b.south) * 111.32).toBeCloseTo(5, 1);
  });
});

describe('browseFiltersForArea', () => {
  it('seeds category + disposition + viewport bounds from a byt listing', () => {
    const f = browseFiltersForArea({
      ...TREBIC,
      categoryMain: 'byt',
      categoryType: 'prodej',
      disposition: '2+1',
    });
    expect(f.categoryMain).toEqual(['byt']);
    expect(f.categoryType).toBe('prodej');
    expect(f.dispositions).toEqual(['2+1']);
    expect(f.locationMode).toBe('viewport');
    expect(f.bounds).not.toBeNull();
    expect(f.bounds!.west).toBeLessThan(f.bounds!.east);
  });

  it('drops the disposition filter when the listing has no disposition', () => {
    const f = browseFiltersForArea({
      ...TREBIC,
      categoryMain: 'dum',
      categoryType: 'prodej',
      disposition: null,
    });
    expect(f.categoryMain).toEqual(['dum']);
    expect(f.dispositions).toEqual([]);
  });

  it('seeds pozemek now that Browse supports all five categories', () => {
    const f = browseFiltersForArea({
      ...TREBIC,
      categoryMain: 'pozemek',
      categoryType: 'prodej',
      disposition: null,
    });
    expect(f.categoryMain).toEqual(['pozemek']);
  });

  it('falls back to the default category for an unknown category', () => {
    const f = browseFiltersForArea({
      ...TREBIC,
      categoryMain: 'spaceship',
      categoryType: 'prodej',
      disposition: null,
    });
    expect(f.categoryMain).toEqual(DEFAULT_FILTERS.categoryMain);
  });

  it('IGNORES origin — the anchor never leaks into the cohort filters', () => {
    const seed: ExploreAreaSeed = {
      ...TREBIC,
      categoryMain: 'byt',
      categoryType: 'prodej',
      disposition: '2+1',
    };
    const withoutOrigin = browseFiltersForArea(seed);
    const withOrigin = browseFiltersForArea({ ...seed, origin: SOME_ORIGIN });
    // The cohort is computed purely from the seed fields; origin is display-only
    // (anchor pin + top panel), so the two filter sets must be identical.
    expect(withOrigin).toEqual(withoutOrigin);
  });
});

describe('browseUrlFromState ↔ fromSearchParams round-trip', () => {
  it('carries category + disposition + bounds to the Browse URL', () => {
    const filters = browseFiltersForArea({
      ...TREBIC,
      categoryMain: 'byt',
      categoryType: 'prodej',
      disposition: '3+kk',
    });
    const url = browseUrlFromState({
      filters,
      sort: DEFAULT_SORT,
      tab: 'map',
      overlay: DEFAULT_OVERLAY,
    });
    const parsed = fromSearchParams(new URLSearchParams(url.split('?')[1]));
    expect(parsed.categoryMain).toEqual(['byt']);
    expect(parsed.categoryType).toBe('prodej');
    expect(parsed.dispositions).toEqual(['3+kk']);
    // bbox serialises at 5-decimal precision
    expect(parsed.bounds).not.toBeNull();
    expect(parsed.bounds!.west).toBeCloseTo(filters.bounds!.west, 4);
    expect(parsed.bounds!.north).toBeCloseTo(filters.bounds!.north, 4);
  });

  it('encodes a non-default overlay (rent map + VK) into the URL', () => {
    const url = browseUrlFromState({
      filters: DEFAULT_FILTERS,
      sort: DEFAULT_SORT,
      tab: 'map',
      overlay: { ...DEFAULT_OVERLAY, showRentMap: true, rentVk: 3 },
    });
    expect(url).toContain('rentmap=1');
    expect(url).toContain('rentvk=3');
  });
});
