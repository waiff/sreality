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
import {
  districtsFilterClause,
  effectiveBbox,
  matchesDistricts,
  type DistrictMatchRow,
} from './queries';
import type { DistrictChip } from './filters';

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

/* `matchesDistricts` is the in-memory predicate the pipeline board uses. Pinned
 * against the SAME chip fixtures as `districtsFilterClause` above so the two
 * implementations of the location-chip contract can never silently diverge. */
describe('matchesDistricts', () => {
  const mkRow = (o: Partial<DistrictMatchRow>): DistrictMatchRow => ({
    obec_id: null, okres_id: null, region_id: null,
    district: null, place_search_text: null, okres: null, region: null,
    ...o,
  });

  it('matches any row when there are no chips', () => {
    expect(matchesDistricts(mkRow({ obec_id: 1 }), [])).toBe(true);
  });

  it('matches a resolved obec chip by stable admin id, never by name', () => {
    const chip: DistrictChip = { name: 'Jihlava', context: null, level: 'obec', id: 586846 };
    expect(matchesDistricts(mkRow({ obec_id: 586846 }), [chip])).toBe(true);
    // Same name in the text but a different id → no match (id, not name).
    expect(matchesDistricts(mkRow({ obec_id: 999, district: 'Jihlava' }), [chip])).toBe(false);
  });

  it('matches okres / kraj chips on their own id columns', () => {
    const chips: DistrictChip[] = [
      { name: 'okres Jihlava', context: null, level: 'okres', id: 3707 },
      { name: 'Kraj Vysočina', context: null, level: 'kraj', id: 108 },
    ];
    expect(matchesDistricts(mkRow({ okres_id: 3707 }), chips)).toBe(true);
    expect(matchesDistricts(mkRow({ region_id: 108 }), chips)).toBe(true);
    expect(matchesDistricts(mkRow({ okres_id: 1, region_id: 2 }), chips)).toBe(false);
  });

  it('street pick = containing obec id AND place_search_text substring', () => {
    const chip: DistrictChip = { name: 'Pezinská', context: 'Mladá Boleslav', level: 'locality', id: 535419 };
    expect(matchesDistricts(mkRow({ obec_id: 535419, place_search_text: 'Pezinská 12, Mladá Boleslav' }), [chip])).toBe(true);
    // Right obec, wrong street text → no match.
    expect(matchesDistricts(mkRow({ obec_id: 535419, place_search_text: 'Hlavní 1' }), [chip])).toBe(false);
    // Right street text, wrong obec → no match.
    expect(matchesDistricts(mkRow({ obec_id: 1, place_search_text: 'Pezinská 12' }), [chip])).toBe(false);
  });

  it('legacy chip falls back to name substring AND context across place columns', () => {
    const chip: DistrictChip = { name: 'Edvarda Beneše', context: 'Plzeň' };
    expect(matchesDistricts(mkRow({ place_search_text: 'Edvarda Beneše 3', okres: 'Plzeň-město', region: 'Plzeňský kraj' }), [chip])).toBe(true);
    // Name matches but the context (Plzeň) appears in no place column → no match.
    expect(matchesDistricts(mkRow({ place_search_text: 'Edvarda Beneše 3', region: 'Jihomoravský kraj' }), [chip])).toBe(false);
  });

  it('name fallback is case-insensitive (mirrors ILIKE "*…*")', () => {
    expect(matchesDistricts(mkRow({ district: 'Edvarda BENEŠE' }), [
      { name: 'beneše', context: null },
    ])).toBe(true);
  });

  it('splits include and exclude: included AND not excluded', () => {
    const inc: DistrictChip = { name: 'Jihlava', context: null, level: 'obec', id: 586846 };
    const exc: DistrictChip = { name: 'Modřany', context: null, level: 'locality', id: 554782, excluded: true };
    expect(matchesDistricts(mkRow({ obec_id: 586846 }), [inc, exc])).toBe(true);
    // A Modřany row is excluded (and isn't an include either).
    expect(matchesDistricts(mkRow({ obec_id: 554782, place_search_text: 'Modřany' }), [inc, exc])).toBe(false);
    // Exclude-only: a non-Modřany row passes.
    expect(matchesDistricts(mkRow({ obec_id: 1 }), [exc])).toBe(true);
  });
});
