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
  applyRegistryUpdates,
  filtersEqualForPreset,
  filtersForPreset,
  filtersToWatchdogSpec,
  fromSearchParams,
  isDefault,
  type ListingFilters,
  type MapBounds,
  listingFiltersToRegistryView,
  toSearchParams,
  watchdogNameSuggestion,
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

  it('round-trips conditionMatch and drops unknown values', () => {
    const f: ListingFilters = {
      ...DEFAULT_FILTERS,
      conditionMatch: ['novostavba', 'po_rekonstrukci'],
    };
    const round = fromSearchParams(toSearchParams(f));
    expect(round.conditionMatch).toEqual(['novostavba', 'po_rekonstrukci']);
    // Unknown values in the URL must not leak into state — they
    // would otherwise reach Supabase and tighten the cohort to zero.
    const dirty = new URLSearchParams('condition=novostavba,bogus_value');
    expect(fromSearchParams(dirty).conditionMatch).toEqual(['novostavba']);
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

  it('parses a legacy ?districts=Praha URL into a single context-less chip', () => {
    const sp = new URLSearchParams({ districts: 'Praha' });
    const round = fromSearchParams(sp);
    expect(round.districts).toEqual([{ name: 'Praha', context: null }]);
  });

  it('round-trips a single chip without context (no districts_ctx in URL)', () => {
    const f = {
      ...DEFAULT_FILTERS,
      districts: [{ name: 'okres Jihlava', context: null }],
    };
    const sp = toSearchParams(f);
    expect(sp.get('districts')).toBe('okres%20Jihlava');
    expect(sp.has('districts_ctx')).toBe(false); // clean URL — no ctx needed
    expect(fromSearchParams(sp).districts).toEqual(f.districts);
  });

  it('round-trips a chip with parent-municipality context', () => {
    const f = {
      ...DEFAULT_FILTERS,
      districts: [{ name: 'Edvarda Beneše', context: 'Plzeň' }],
    };
    const sp = toSearchParams(f);
    expect(sp.get('districts')).toBe('Edvarda%20Bene%C5%A1e');
    expect(sp.get('districts_ctx')).toBe('Plze%C5%88');
    expect(fromSearchParams(sp).districts).toEqual(f.districts);
  });

  it('round-trips a mix of chips (some with context, some without)', () => {
    const f = {
      ...DEFAULT_FILTERS,
      districts: [
        { name: 'Praha', context: null },
        { name: 'Edvarda Beneše', context: 'Plzeň' },
      ],
    };
    const round = fromSearchParams(toSearchParams(f));
    expect(round.districts).toEqual(f.districts);
  });

  it('lifts two same-name chips with different contexts as two entries', () => {
    const f = {
      ...DEFAULT_FILTERS,
      districts: [
        { name: 'Edvarda Beneše', context: 'Plzeň' },
        { name: 'Edvarda Beneše', context: 'Olomouc' },
      ],
    };
    const round = fromSearchParams(toSearchParams(f));
    expect(round.districts).toEqual(f.districts);
  });

  it('round-trips an excluded chip (districts_excl=1)', () => {
    const f = {
      ...DEFAULT_FILTERS,
      districts: [{ name: 'Praha', context: null, excluded: true }],
    };
    const sp = toSearchParams(f);
    expect(sp.get('districts')).toBe('Praha');
    expect(sp.get('districts_excl')).toBe('1');
    expect(fromSearchParams(sp).districts).toEqual(f.districts);
  });

  it('omits districts_excl when no chip is excluded (clean URL)', () => {
    const f = {
      ...DEFAULT_FILTERS,
      districts: [{ name: 'Praha', context: null }],
    };
    expect(toSearchParams(f).has('districts_excl')).toBe(false);
  });

  it('treats a legacy URL with no districts_excl as all-include', () => {
    const round = fromSearchParams(
      new URLSearchParams({ districts: 'Praha,Brno' }),
    );
    expect(round.districts).toEqual([
      { name: 'Praha', context: null },
      { name: 'Brno', context: null },
    ]);
    expect(round.districts.every((d) => !d.excluded)).toBe(true);
  });

  it('round-trips a mix of include and exclude chips (parallel flags)', () => {
    const f = {
      ...DEFAULT_FILTERS,
      districts: [
        { name: 'Praha', context: null, excluded: true },
        { name: 'Brno', context: null },
      ],
    };
    const sp = toSearchParams(f);
    expect(sp.get('districts_excl')).toBe('1,0');
    expect(fromSearchParams(sp).districts).toEqual(f.districts);
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
    expect(view.condition_match).toBeNull();
  });

  it('passes a populated conditionMatch array through to the registry', () => {
    const view = listingFiltersToRegistryView({
      ...DEFAULT_FILTERS,
      conditionMatch: ['novostavba', 'velmi_dobry'],
    });
    expect(view.condition_match).toEqual(['novostavba', 'velmi_dobry']);
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

  it('accepts DistrictChip[] for districts and lifts legacy string[] callers', () => {
    let f = { ...DEFAULT_FILTERS };
    f = applyRegistryUpdate(f, 'districts', [
      { name: 'Edvarda Beneše', context: 'Plzeň' },
    ]);
    expect(f.districts).toEqual([{ name: 'Edvarda Beneše', context: 'Plzeň' }]);

    // Legacy callers (e.g. a registry-driven test fixture) that still
    // pass plain strings get lifted to context-null chips so the
    // shape stays consistent at the boundary.
    f = applyRegistryUpdate(DEFAULT_FILTERS, 'districts', ['okres Jihlava']);
    expect(f.districts).toEqual([
      { name: 'okres Jihlava', context: null },
    ]);

    f = applyRegistryUpdate(f, 'districts', null);
    expect(f.districts).toEqual([]);
  });

  it('normalises conditionMatch null ↔ array at the registry boundary', () => {
    let f: ListingFilters = {
      ...DEFAULT_FILTERS,
      conditionMatch: ['novostavba'],
    };
    f = applyRegistryUpdate(f, 'condition_match', null);
    expect(f.conditionMatch).toEqual([]);
    f = applyRegistryUpdate(f, 'condition_match', ['velmi_dobry', 'dobry']);
    expect(f.conditionMatch).toEqual(['velmi_dobry', 'dobry']);
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

describe('applyRegistryUpdates (batched)', () => {
  it('applies min and max in one transaction', () => {
    // Regression for the slider-frozen bug: when the FilterForm
    // emitted two onChange(id, value) calls in a row, a non-functional
    // setter saw both against the same stale state and the second
    // overwrote the first. The batched helper threads each update
    // through the prior result, so both sides land.
    const next = applyRegistryUpdates(DEFAULT_FILTERS, [
      { id: 'min_price_czk', value: 5_000 },
      { id: 'max_price_czk', value: 25_000 },
    ]);
    expect(next.priceMin).toBe(5_000);
    expect(next.priceMax).toBe(25_000);
  });

  it('preserves a value when the corresponding update has the same value', () => {
    const start: ListingFilters = {
      ...DEFAULT_FILTERS,
      priceMin: 5_000,
      priceMax: 25_000,
    };
    const next = applyRegistryUpdates(start, [
      { id: 'min_price_czk', value: 8_000 },
      { id: 'max_price_czk', value: 25_000 },
    ]);
    expect(next.priceMin).toBe(8_000);
    expect(next.priceMax).toBe(25_000);
  });

  it('returns the original filters for an empty batch', () => {
    expect(applyRegistryUpdates(DEFAULT_FILTERS, [])).toBe(DEFAULT_FILTERS);
  });
});

describe('filtersToWatchdogSpec', () => {
  it('maps category, dispositions and district chips', () => {
    const f: ListingFilters = {
      ...DEFAULT_FILTERS,
      categoryMain: 'byt',
      categoryType: 'prodej',
      dispositions: ['2+kk', '2+1'],
      districts: [{ name: 'Jihlava', context: null }],
    };
    const { spec } = filtersToWatchdogSpec(f);
    expect(spec.category_main).toBe('byt');
    expect(spec.category_type).toBe('prodej');
    expect(spec.dispositions).toEqual(['2+kk', '2+1']);
    expect(spec.districts).toEqual([{ name: 'Jihlava', context: null }]);
  });

  it('empty multi-selects become null (matcher "no constraint" sentinel)', () => {
    const { spec } = filtersToWatchdogSpec(DEFAULT_FILTERS);
    expect(spec.dispositions).toBeNull();
    expect(spec.districts).toBeNull();
    expect(spec.portals).toBeNull();
    expect(spec.condition_match).toBeNull();
    expect(spec.city_index_rules).toBeNull();
  });

  it('carries the advanced predicates the matcher honours', () => {
    const f: ListingFilters = {
      ...DEFAULT_FILTERS,
      priceMin: 1_000_000,
      priceMax: 5_000_000,
      mfGrossYieldPctMin: 4,
      maxPriceDropPctMin: 10,
      priceDropCountMin: 1,
      minCityPopulation: 50_000,
      maxCityPopulation: 200_000,
      cityIndexRules: [{ index_name: 'safety', op: '>=', value: 7 }],
      nearCityProximity: {
        index_rules: [{ index_name: 'safety', op: '>=', value: 8 }],
        population_min: 100_000,
        radius_km: 15,
      },
    };
    const { spec } = filtersToWatchdogSpec(f);
    expect(spec.min_price_czk).toBe(1_000_000);
    expect(spec.max_price_czk).toBe(5_000_000);
    expect(spec.min_mf_gross_yield_pct).toBe(4);
    expect(spec.max_price_drop_pct_min).toBe(10);
    expect(spec.price_drop_count_min).toBe(1);
    expect(spec.min_city_population).toBe(50_000);
    expect(spec.max_city_population).toBe(200_000);
    expect(spec.city_index_rules).toEqual([{ index_name: 'safety', op: '>=', value: 7 }]);
    expect(spec.near_city_proximity?.radius_km).toBe(15);
  });

  it('center+radius location maps to lat/lng/radius_m; viewport does not', () => {
    const cr = { lat: 50.08, lng: 14.42, radius_m: 1500 };
    const viewport = filtersToWatchdogSpec({
      ...DEFAULT_FILTERS,
      locationMode: 'viewport',
      centerRadius: cr,
    });
    expect(viewport.spec.lat).toBeNull();
    expect(viewport.spec.radius_m).toBeNull();

    const centered = filtersToWatchdogSpec({
      ...DEFAULT_FILTERS,
      locationMode: 'center_radius',
      centerRadius: cr,
    });
    expect(centered.spec.lat).toBe(50.08);
    expect(centered.spec.lng).toBe(14.42);
    expect(centered.spec.radius_m).toBe(1500);
  });

  it('tri-state amenities pivot to bool | null', () => {
    const { spec } = filtersToWatchdogSpec({
      ...DEFAULT_FILTERS,
      hasBalcony: 'yes',
      garage: 'no',
      cellar: 'any',
    });
    expect(spec.has_balcony).toBe(true);
    expect(spec.garage).toBe(false);
    expect(spec.cellar).toBeNull();
  });

  it('reports set-but-unmonitored Browse filters in `unsupported`', () => {
    const { unsupported } = filtersToWatchdogSpec({
      ...DEFAULT_FILTERS,
      status: 'active',
      lastSeenMaxDays: 7,
      tags: [3],
      buildingMaterial: ['cihla'],
    });
    expect(unsupported).toContain('listing status');
    expect(unsupported).toContain('last/first-seen date range');
    expect(unsupported).toContain('tags');
    expect(unsupported).toContain('building material');
  });

  it('a default filter set has nothing unsupported', () => {
    expect(filtersToWatchdogSpec(DEFAULT_FILTERS).unsupported).toEqual([]);
  });
});

describe('watchdogNameSuggestion', () => {
  it('builds category · dispositions · districts', () => {
    expect(
      watchdogNameSuggestion({
        ...DEFAULT_FILTERS,
        categoryMain: 'byt',
        categoryType: 'prodej',
        dispositions: ['2+kk', '2+1'],
        districts: [
          { name: 'Jihlava', context: null },
          { name: 'Havlíčkův Brod', context: null },
        ],
      }),
    ).toBe('byt prodej · 2+kk, 2+1 · Jihlava, Havlíčkův Brod');
  });

  it('falls back to just the category when nothing else is set', () => {
    expect(watchdogNameSuggestion(DEFAULT_FILTERS)).toBe('byt pronajem');
  });
});

describe('filter presets', () => {
  const BOUNDS: MapBounds = { west: 14.3, south: 50.0, east: 14.6, north: 50.2 };

  it('filtersForPreset drops bounds unless the map area is included', () => {
    const f: ListingFilters = { ...DEFAULT_FILTERS, priceMax: 6_000_000, bounds: BOUNDS };
    expect(filtersForPreset(f, false).bounds).toBeNull();
    expect(filtersForPreset(f, true).bounds).toEqual(BOUNDS);
    // Non-viewport filters survive either way.
    expect(filtersForPreset(f, false).priceMax).toBe(6_000_000);
  });

  it('matches identical filter sets', () => {
    const f: ListingFilters = { ...DEFAULT_FILTERS, priceMax: 5_000_000, dispositions: ['2+kk'] };
    expect(filtersEqualForPreset(f, { ...f })).toBe(true);
  });

  it('detects a changed filter as not matching', () => {
    const saved: ListingFilters = { ...DEFAULT_FILTERS, priceMax: 5_000_000 };
    const current: ListingFilters = { ...DEFAULT_FILTERS, priceMax: 4_000_000 };
    expect(filtersEqualForPreset(current, saved)).toBe(false);
  });

  it('ignores the map viewport when the preset stored none', () => {
    const saved: ListingFilters = { ...DEFAULT_FILTERS, priceMax: 5_000_000, bounds: null };
    const current: ListingFilters = { ...DEFAULT_FILTERS, priceMax: 5_000_000, bounds: BOUNDS };
    // Panning the map after loading a criteria-only preset must NOT mark it dirty.
    expect(filtersEqualForPreset(current, saved)).toBe(true);
  });

  it('honours the map viewport when the preset stored one', () => {
    const saved: ListingFilters = { ...DEFAULT_FILTERS, bounds: BOUNDS };
    const moved: MapBounds = { ...BOUNDS, north: 50.5 };
    const current: ListingFilters = { ...DEFAULT_FILTERS, bounds: moved };
    expect(filtersEqualForPreset(current, saved)).toBe(false);
    expect(filtersEqualForPreset({ ...DEFAULT_FILTERS, bounds: BOUNDS }, saved)).toBe(true);
  });

  it('treats a preset persisted without a newer field as equal to current defaults', () => {
    // Simulate an older spec missing a field added later.
    const saved = { ...DEFAULT_FILTERS } as Record<string, unknown>;
    delete saved.nearOverall15kmMin;
    expect(
      filtersEqualForPreset(DEFAULT_FILTERS, saved as unknown as ListingFilters),
    ).toBe(true);
  });
});
