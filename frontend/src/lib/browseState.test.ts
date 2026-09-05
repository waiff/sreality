import { describe, expect, it } from 'vitest';
import { bboxAround, fromSearchParams, DEFAULT_FILTERS } from './filters';
import {
  DEFAULT_OVERLAY,
  browseFiltersForArea,
  browseFiltersForBroker,
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

/* Unlike browseFiltersForArea, an unset ("Vše") category must resolve to NO
 * constraint — not Browse's narrow default (['byt'] / 'pronajem'). A broker who
 * mostly sells houses would otherwise open to an empty or wrong map. */
describe('browseFiltersForBroker', () => {
  it('seeds only the broker id for Vše/Vše — no category constraint, no viewport', () => {
    const f = browseFiltersForBroker({ brokerId: 527, categoryMain: null, categoryType: null });
    expect(f.brokerId).toBe(527);
    expect(f.categoryMain).toEqual([]);
    expect(f.categoryType).toBeNull();
    expect(f.bounds).toBeNull();
    expect(f.status).toBe('any');
  });

  it('carries a picked Typ/Nabídka through', () => {
    const f = browseFiltersForBroker({ brokerId: 527, categoryMain: 'dum', categoryType: 'prodej' });
    expect(f.categoryMain).toEqual(['dum']);
    expect(f.categoryType).toBe('prodej');
  });

  it("drops an unknown category to NO constraint, never to Browse's default", () => {
    const f = browseFiltersForBroker({ brokerId: 527, categoryMain: 'spaceship', categoryType: 'lease' });
    expect(f.categoryMain).toEqual([]);
    expect(f.categoryType).toBeNull();
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

/* The map's Kč/m² toggle is a VIEW knob on MapOverlayState: URL-serialised so
 * the view is shareable, and deliberately outside preset identity so flipping it
 * can never mark a saved preset dirty. */
describe('priceMetric on the map overlay', () => {
  const state = (priceMetric: 'total' | 'per_m2') => ({
    filters: DEFAULT_FILTERS,
    sort: DEFAULT_SORT,
    tab: 'map' as const,
    overlay: { ...DEFAULT_OVERLAY, priceMetric },
  });

  it('defaults to the total price', () => {
    expect(DEFAULT_OVERLAY.priceMetric).toBe('total');
  });

  it('serialises per_m2 into the shareable URL under `pm`', () => {
    expect(browseUrlFromState(state('per_m2'))).toContain('pm=per_m2');
  });

  /* The default must not appear in the URL — a default-state map has to
   * serialise to the plain /browse link, like every other overlay knob. */
  it('leaves the URL untouched at the default', () => {
    expect(browseUrlFromState(state('total'))).not.toContain('pm=');
  });

  /* `pm` is a view knob, NOT a cohort filter: it must not reach the filter
   * spec, or a shared Kč/m² map would look like a different search. */
  it('is invisible to the filter parser', () => {
    const withPm = fromSearchParams(new URLSearchParams('pm=per_m2'));
    const without = fromSearchParams(new URLSearchParams(''));
    expect(withPm).toEqual(without);
  });
});
