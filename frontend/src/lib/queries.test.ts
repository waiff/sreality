/* Tests for the spatial-predicate helpers in queries.ts.
 *
 * The interesting logic is `effectiveBbox`: when the operator picks
 * centre+radius mode, the cohort filter sends the bbox of the
 * circumscribing square of the radius circle (haversine math, no
 * dependency on PostGIS). Viewport mode falls through to whatever
 * the map's panning has set as `bounds`. These are pure functions
 * we want pinned against accidental edits.
 */

import { describe, expect, it } from 'vitest';

import { DEFAULT_FILTERS } from './filters';
import { districtsFilterClause, effectiveBbox } from './queries';

describe('effectiveBbox', () => {
  it('returns null when both modes are empty', () => {
    expect(effectiveBbox(DEFAULT_FILTERS)).toBeNull();
  });

  it('returns the literal viewport bounds in viewport mode', () => {
    const bounds = { west: 14.3, south: 50.0, east: 14.5, north: 50.2 };
    const got = effectiveBbox({
      ...DEFAULT_FILTERS,
      bounds,
    });
    expect(got).toEqual(bounds);
  });

  it('ignores viewport bounds in centre+radius mode', () => {
    // Even with a viewport bbox set on the side, centre+radius wins.
    const got = effectiveBbox({
      ...DEFAULT_FILTERS,
      locationMode: 'center_radius',
      centerRadius: { lat: 50, lng: 14, radius_m: 1000 },
      bounds: { west: 14.3, south: 50.0, east: 14.5, north: 50.2 },
    });
    expect(got).not.toBeNull();
    // The viewport bbox would have been (14.3, 50.0, 14.5, 50.2); the
    // 1km centre+radius around (50, 14) sits a hair north + south of
    // lat 50 and is nowhere near the viewport rectangle.
    expect(got!.north).toBeLessThan(50.2);
    expect(got!.west).toBeLessThan(14.3);
  });

  it('returns null in centre+radius mode when no centre is set', () => {
    const got = effectiveBbox({
      ...DEFAULT_FILTERS,
      locationMode: 'center_radius',
      centerRadius: null,
    });
    expect(got).toBeNull();
  });

  it('produces a centre-symmetric bbox around the point', () => {
    const lat = 50;
    const lng = 14;
    const got = effectiveBbox({
      ...DEFAULT_FILTERS,
      locationMode: 'center_radius',
      centerRadius: { lat, lng, radius_m: 1000 },
    });
    expect(got).not.toBeNull();
    expect(got!.north - lat).toBeCloseTo(lat - got!.south, 6);
    expect(got!.east - lng).toBeCloseTo(lng - got!.west, 6);
  });

  it('produces a wider bbox at higher latitudes for the same radius', () => {
    // Longitude degrees shrink as |lat| → 90°; the bbox must compensate
    // so the circle still fits at the poles. Compare two centres with
    // the same radius and check the longitude span widens with lat.
    const near_equator = effectiveBbox({
      ...DEFAULT_FILTERS,
      locationMode: 'center_radius',
      centerRadius: { lat: 10, lng: 14, radius_m: 1000 },
    });
    const near_pole = effectiveBbox({
      ...DEFAULT_FILTERS,
      locationMode: 'center_radius',
      centerRadius: { lat: 70, lng: 14, radius_m: 1000 },
    });
    const equator_span = (near_equator!.east - near_equator!.west);
    const pole_span = (near_pole!.east - near_pole!.west);
    expect(pole_span).toBeGreaterThan(equator_span);
  });

  it('approximates roughly 1km ↔ 0.009 deg at typical Prague latitude', () => {
    // 1 deg latitude ≈ 111.32 km. A 1km radius circle around lat 50
    // should produce a bbox with dLat ≈ 1/111.32 ≈ 0.00899 deg
    // either side of the centre.
    const got = effectiveBbox({
      ...DEFAULT_FILTERS,
      locationMode: 'center_radius',
      centerRadius: { lat: 50, lng: 14, radius_m: 1000 },
    });
    expect(got).not.toBeNull();
    expect(got!.north - 50).toBeCloseTo(0.00899, 4);
  });
});

/* `districtsFilterClause` builds the PostgREST predicate for the location
 * chips — the frontend's copy of the chip contract kept in lockstep with
 * the watchdog matcher (`_build_match_clauses`) and browse_stats
 * (migration 182). Pinned here so a drive-by edit can't silently change
 * what a chip means on one surface only. */
describe('districtsFilterClause', () => {
  it('returns null with no chips', () => {
    expect(districtsFilterClause([])).toBeNull();
  });

  it('matches a resolved obec chip by stable admin id, never by name', () => {
    const got = districtsFilterClause([
      { name: 'Jihlava', context: null, level: 'obec', id: 586846 },
    ]);
    expect(got).toBe('and(or(obec_id.eq.586846))');
  });

  it('matches okres / kraj chips on their own id columns', () => {
    const got = districtsFilterClause([
      { name: 'okres Jihlava', context: null, level: 'okres', id: 3707 },
      { name: 'Kraj Vysočina', context: null, level: 'kraj', id: 108 },
    ]);
    expect(got).toBe('and(or(okres_id.eq.3707,region_id.eq.108))');
  });

  it('street pick = containing obec id AND place_search_text ILIKE', () => {
    // The bazos regression: the street lives in `street`, not `locality`,
    // so the text half must read place_search_text (street + locality).
    const got = districtsFilterClause([
      { name: 'Pezinská', context: 'Mladá Boleslav', level: 'locality', id: 535419 },
    ]);
    expect(got).toBe(
      'and(or(and(obec_id.eq.535419,place_search_text.ilike."*Pezinská*")))',
    );
  });

  it('legacy chip falls back to name ILIKE across the place columns', () => {
    const got = districtsFilterClause([
      { name: 'Edvarda Beneše', context: 'Plzeň' },
    ]);
    expect(got).toBe(
      'and(or(and(or(district.ilike."*Edvarda Beneše*",'
      + 'place_search_text.ilike."*Edvarda Beneše*",'
      + 'okres.ilike."*Edvarda Beneše*",region.ilike."*Edvarda Beneše*"),'
      + 'or(district.ilike."*Plzeň*",place_search_text.ilike."*Plzeň*",'
      + 'okres.ilike."*Plzeň*",region.ilike."*Plzeň*"))))',
    );
  });

  it('never references the bare locality column in any branch', () => {
    const got = districtsFilterClause([
      { name: 'Pezinská', context: null, level: 'locality', id: 535419 },
      { name: 'Brno', context: 'Jihomoravský kraj' },
      { name: 'Modřany', context: null, excluded: true },
    ]);
    expect(got).not.toBeNull();
    expect(got!).not.toMatch(/[(,]locality\.ilike/);
    expect(got!).toContain('place_search_text.ilike');
  });

  it('splits include and exclude chips into or(...) and not.or(...)', () => {
    const got = districtsFilterClause([
      { name: 'Jihlava', context: null, level: 'obec', id: 586846 },
      { name: 'Modřany', context: null, level: 'locality', id: 554782, excluded: true },
    ]);
    expect(got).toBe(
      'and(or(obec_id.eq.586846),'
      + 'not.or(and(obec_id.eq.554782,place_search_text.ilike."*Modřany*")))',
    );
  });

  it('escapes PostgREST breakout characters in chip names', () => {
    const got = districtsFilterClause([
      { name: 'Nové Město (u Brna), *', context: null },
    ]);
    expect(got).toContain('"*Nové Město \\(u Brna\\)\\, \\**"');
  });
});
