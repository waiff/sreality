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
import { effectiveBbox } from './queries';

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
