/* Pure-function tests for the filter state plumbing.
 *
 * Covers:
 *   - URL round-trip through fromSearchParams / toSearchParams, including
 *     the new centre+radius + locationMode fields.
 *   - isDefault correctly returns false when any non-default field is set.
 *   - listingFiltersToRegistryView ↔ applyRegistryUpdate symmetry.
 *   - Tri-state amenities pivot 'any' / 'yes' / 'no' ↔ null / true / false
 *     at the adapter boundary.
 *
 * No jsdom needed — these are all pure transforms on the ListingFilters
 * shape. Render-time tests for FilterForm + filter-controls primitives
 * land in a follow-up batch once jsdom + @testing-library/react are
 * pulled in.
 */

import { describe, expect, it } from 'vitest';

import {
  DEFAULT_FILTERS,
  applyRegistryUpdate,
  fromSearchParams,
  isDefault,
  type ListingFilters,
  listingFiltersToRegistryView,
  toSearchParams,
} from './filters';

describe('URL round-trip', () => {
  it('preserves the empty default state across an empty URL', () => {
    const sp = toSearchParams(DEFAULT_FILTERS);
    expect(sp.toString()).toBe('');
    expect(fromSearchParams(new URLSearchParams())).toEqual(DEFAULT_FILTERS);
  });

  it('round-trips price + area + tri-state + dispositions', () => {
    const f: ListingFilters = {
      ...DEFAULT_FILTERS,
      priceMin: 12_000,
      priceMax: 28_000,
      areaMin: 40,
      areaMax: 80,
      dispositions: ['2+kk', '2+1'],
      hasBalcony: 'yes',
      terrace: 'no',
      furnished: 'castecne',
    };
    const round = fromSearchParams(toSearchParams(f));
    expect(round.priceMin).toBe(12_000);
    expect(round.priceMax).toBe(28_000);
    expect(round.areaMin).toBe(40);
    expect(round.areaMax).toBe(80);
    expect(round.dispositions).toEqual(['2+kk', '2+1']);
    expect(round.hasBalcony).toBe('yes');
    expect(round.terrace).toBe('no');
    expect(round.furnished).toBe('castecne');
  });

  it('round-trips the centre+radius mode and coordinates', () => {
    const f: ListingFilters = {
      ...DEFAULT_FILTERS,
      locationMode: 'center_radius',
      centerRadius: { lat: 50.0867, lng: 14.4205, radius_m: 1500 },
    };
    const round = fromSearchParams(toSearchParams(f));
    expect(round.locationMode).toBe('center_radius');
    expect(round.centerRadius).not.toBeNull();
    expect(round.centerRadius!.lat).toBeCloseTo(50.0867, 4);
    expect(round.centerRadius!.lng).toBeCloseTo(14.4205, 4);
    expect(round.centerRadius!.radius_m).toBe(1500);
  });

  it('round-trips the bounds box (viewport mode)', () => {
    const f = {
      ...DEFAULT_FILTERS,
      bounds: { west: 14.3, south: 50.0, east: 14.5, north: 50.2 },
    };
    const round = fromSearchParams(toSearchParams(f));
    expect(round.bounds).toEqual({
      west: 14.3, south: 50.0, east: 14.5, north: 50.2,
    });
    expect(round.locationMode).toBe('viewport');
  });

  it('omits locationMode and center from the URL on the default', () => {
    const sp = toSearchParams(DEFAULT_FILTERS);
    expect(sp.has('locmode')).toBe(false);
    expect(sp.has('center')).toBe(false);
  });
});

describe('isDefault', () => {
  it('returns true for the canonical default state', () => {
    expect(isDefault(DEFAULT_FILTERS)).toBe(true);
  });

  it('returns false when any single filter is non-default', () => {
    expect(isDefault({ ...DEFAULT_FILTERS, priceMin: 10_000 })).toBe(false);
    expect(isDefault({ ...DEFAULT_FILTERS, hasBalcony: 'yes' })).toBe(false);
    expect(
      isDefault({ ...DEFAULT_FILTERS, locationMode: 'center_radius' }),
    ).toBe(false);
    expect(
      isDefault({
        ...DEFAULT_FILTERS,
        centerRadius: { lat: 50, lng: 14, radius_m: 1000 },
      }),
    ).toBe(false);
  });
});

describe('listingFiltersToRegistryView', () => {
  it('translates tri-state amenities to bool | null', () => {
    const f: ListingFilters = {
      ...DEFAULT_FILTERS,
      hasBalcony: 'yes',
      hasLift: 'any',
      garage: 'no',
    };
    const view = listingFiltersToRegistryView(f);
    expect(view.has_balcony).toBe(true);
    expect(view.has_lift).toBeNull();
    expect(view.garage).toBe(false);
  });

  it('normalises empty multi-value arrays to null for the registry', () => {
    const view = listingFiltersToRegistryView(DEFAULT_FILTERS);
    expect(view.tags).toBeNull();
    expect(view.dispositions).toBeNull();
    expect(view.districts).toBeNull();
  });

  it('passes scalar fields through unchanged', () => {
    const f: ListingFilters = {
      ...DEFAULT_FILTERS,
      priceMin: 10_000,
      priceMax: null,
      categoryMain: 'dum',
      status: 'active',
    };
    const view = listingFiltersToRegistryView(f);
    expect(view.min_price_czk).toBe(10_000);
    expect(view.max_price_czk).toBeNull();
    expect(view.category_main).toBe('dum');
    expect(view.status).toBe('active');
  });
});

describe('applyRegistryUpdate', () => {
  it('pivots bool | null back to tri-state on tri-state amenities', () => {
    let f: ListingFilters = { ...DEFAULT_FILTERS };
    f = applyRegistryUpdate(f, 'has_balcony', true);
    expect(f.hasBalcony).toBe('yes');
    f = applyRegistryUpdate(f, 'has_balcony', false);
    expect(f.hasBalcony).toBe('no');
    f = applyRegistryUpdate(f, 'has_balcony', null);
    expect(f.hasBalcony).toBe('any');
  });

  it('normalises null to empty arrays for the multi-value fields', () => {
    let f: ListingFilters = { ...DEFAULT_FILTERS, tags: [1, 2, 3] };
    f = applyRegistryUpdate(f, 'tags', null);
    expect(f.tags).toEqual([]);
    f = applyRegistryUpdate(f, 'tags', [5, 7]);
    expect(f.tags).toEqual([5, 7]);
  });

  it('ignores unknown registry ids without mutating filters', () => {
    const before = { ...DEFAULT_FILTERS, priceMin: 10_000 };
    const after = applyRegistryUpdate(before, 'no_such_filter', 'x');
    expect(after).toBe(before); // identity preserved for unknown ids
  });

  it('round-trips through view → update for a scalar field', () => {
    const f0 = { ...DEFAULT_FILTERS, priceMin: 12_000 };
    const view = listingFiltersToRegistryView(f0);
    const f1 = applyRegistryUpdate(DEFAULT_FILTERS, 'min_price_czk', view.min_price_czk);
    expect(f1.priceMin).toBe(12_000);
  });
});
