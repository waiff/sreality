import { describe, expect, it } from 'vitest';
import { bboxAround, fromSearchParams, DEFAULT_FILTERS } from './filters';
import {
  DEFAULT_OVERLAY,
  browseFiltersForArea,
  browseUrlFromState,
  type ExploreAreaSeed,
} from './browseState';
import { DEFAULT_SORT } from './queries';

const TREBIC: Pick<ExploreAreaSeed, 'lat' | 'lng'> = { lat: 49.2147, lng: 15.8816 };

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
    expect(f.categoryMain).toBe('byt');
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
    expect(f.categoryMain).toBe('dum');
    expect(f.dispositions).toEqual([]);
  });

  it('falls back to the default category for non-UI categories (pozemek)', () => {
    const f = browseFiltersForArea({
      ...TREBIC,
      categoryMain: 'pozemek',
      categoryType: 'prodej',
      disposition: null,
    });
    expect(f.categoryMain).toBe(DEFAULT_FILTERS.categoryMain);
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
    expect(parsed.categoryMain).toBe('byt');
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
