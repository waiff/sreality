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
  applyPrefilters,
  districtsFilterClause,
  effectiveBbox,
  effectiveSort,
  isPortalMirror,
  keysetTiebreak,
  matchesDistricts,
  parseSort,
  pipelineCardBroker,
  pipelineIdsForScope,
  portalMirrorSource,
  priceNullTolerantOr,
  DEFAULT_SORT,
  type BrowsePrefilters,
  type DistrictMatchRow,
} from './queries';
import type { BrokerPublic, ListingBroker } from './brokers';
import type { DistrictChip } from './filters';

describe('priceNullTolerantOr', () => {
  it('AND-groups both bounds with the NULL disjunct', () => {
    expect(priceNullTolerantOr(1_000_000, 5_000_000)).toBe(
      'and(price_czk.gte.1000000,price_czk.lte.5000000),price_czk.is.null',
    );
  });
  it('keeps a single bound un-grouped', () => {
    expect(priceNullTolerantOr(1_000_000, null)).toBe(
      'price_czk.gte.1000000,price_czk.is.null',
    );
    expect(priceNullTolerantOr(null, 5_000_000)).toBe(
      'price_czk.lte.5000000,price_czk.is.null',
    );
  });
});

/* Gate-2: the city-quality allowlist must be AND'd onto the cohort via the
 * surrogate `listing_id`, NOT `sreality_id`. A post-Gate-2 non-sreality repr has
 * a NULL sreality_id (IN never matches NULL → the listing silently vanishes from
 * Map/Table/Cards/Count while browse_stats still counts it), and — worse — the
 * id-spaces overlap by ~435, so a sreality_id passed into an `IN listing_id`
 * predicate would match a DIFFERENT listing. Pin the filter column here. */
describe('applyPrefilters (city-quality id-space)', () => {
  const record = () => {
    const calls: Array<{ col: string; vals: readonly unknown[] }> = [];
    const q = {
      in(col: string, vals: readonly unknown[]) {
        calls.push({ col, vals });
        return q;
      },
    };
    return { q, calls };
  };
  const base: BrowsePrefilters = {
    listingIds: null,
    obecIds: null,
    propertyIds: null,
    empty: false,
  };

  it('filters the city-quality allowlist on listing_id, never sreality_id', () => {
    const { q, calls } = record();
    applyPrefilters(q, { ...base, listingIds: [10, 20, 30] });
    expect(calls).toContainEqual({ col: 'listing_id', vals: [10, 20, 30] });
    expect(calls.some((c) => c.col === 'sreality_id')).toBe(false);
  });

  it('leaves the other prefilter grains on their own columns', () => {
    const { q, calls } = record();
    applyPrefilters(q, {
      ...base,
      listingIds: [1],
      obecIds: [500],
      propertyIds: [7],
    });
    expect(calls).toEqual([
      { col: 'listing_id', vals: [1] },
      { col: 'obec_id', vals: [500] },
      { col: 'property_id', vals: [7] },
    ]);
  });

  it('applies no id filter when city-quality is inactive (null)', () => {
    const { q, calls } = record();
    applyPrefilters(q, base);
    expect(calls).toEqual([]);
  });
});

/* The deal-pipeline scope resolves to a property-id allowlist that is AND'd
 * onto the cohort like tags / with-estimates. The distinction that matters:
 * "scope off" (null → no constraint) vs "scope on, nothing matched" ([] → the
 * caller short-circuits to zero results). Collapsing those would show the whole
 * market to an operator who asked for an empty pipeline. */
describe('pipelineIdsForScope', () => {
  const members = new Map([
    [1, { property_id: 1, stage_id: 11 }],
    [2, { property_id: 2, stage_id: 12 }],
    [3, { property_id: 3, stage_id: 12 }],
  ] as const) as unknown as Parameters<typeof pipelineIdsForScope>[0];

  it('is null (no constraint) when the scope is off', () => {
    expect(pipelineIdsForScope(members, null)).toBeNull();
  });

  it('takes every card when no stage is picked', () => {
    expect(pipelineIdsForScope(members, { stage_ids: [] })).toEqual([1, 2, 3]);
  });

  it('narrows to the picked stages', () => {
    expect(pipelineIdsForScope(members, { stage_ids: [12] })).toEqual([2, 3]);
  });

  it('returns [] — not null — when the scope matches nothing', () => {
    expect(pipelineIdsForScope(members, { stage_ids: [999] })).toEqual([]);
    expect(pipelineIdsForScope(new Map(), { stage_ids: [] })).toEqual([]);
  });
});

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

/* ---------------------------------------------------------------------- */
/* Portal-mirror mode selection (docs/design/portal-order-fidelity.md).    */
/*                                                                        */
/* These pin the THREE things that have to move together, because getting */
/* any one of them wrong is silent rather than loud: the relation read,    */
/* the keyset tiebreaker, and the sort key. A property_id tiebreaker on    */
/* the listing-grain feed does not error — it just drops rows at page      */
/* boundaries, which looks like ordinary infinite-scroll behaviour.        */
/* ---------------------------------------------------------------------- */

describe('portal-mirror mode selection', () => {
  const withPortals = (portals: string[]) => ({ ...DEFAULT_FILTERS, portals });

  it('engages on exactly one portal', () => {
    expect(isPortalMirror(withPortals(['bazos']))).toBe(true);
    expect(portalMirrorSource(withPortals(['bazos']))).toBe('bazos');
  });

  it('stays off for no portal filter — the deduped market view is the default', () => {
    expect(isPortalMirror(DEFAULT_FILTERS)).toBe(false);
    expect(portalMirrorSource(DEFAULT_FILTERS)).toBeNull();
  });

  it('stays off for two or more portals — that is what dedup is for', () => {
    expect(isPortalMirror(withPortals(['bazos', 'sreality']))).toBe(false);
    expect(portalMirrorSource(withPortals(['bazos', 'sreality']))).toBeNull();
  });

  it('anchors the cursor on listing_id in mirror mode, property_id otherwise', () => {
    expect(keysetTiebreak(withPortals(['idnes']))).toBe('listing_id');
    expect(keysetTiebreak(DEFAULT_FILTERS)).toBe('property_id');
    expect(keysetTiebreak(withPortals(['idnes', 'remax']))).toBe('property_id');
  });

  it('remaps only "newest/oldest first" onto the portal order key', () => {
    const mirror = withPortals(['bazos']);
    expect(effectiveSort(mirror, { field: 'first_seen_at', direction: 'desc' })).toEqual({
      field: 'portal_sort_key',
      direction: 'desc',
    });
    /* Direction is preserved: "oldest first" mirrors the portal's oldest. */
    expect(effectiveSort(mirror, { field: 'first_seen_at', direction: 'asc' })).toEqual({
      field: 'portal_sort_key',
      direction: 'asc',
    });
  });

  it('passes every other sort field straight through — they all exist on the feed', () => {
    const mirror = withPortals(['bazos']);
    for (const field of ['price_czk', 'price_per_m2', 'area_m2', 'last_seen_at',
                         'district', 'mf_gross_yield_pct'] as const) {
      expect(effectiveSort(mirror, { field, direction: 'desc' })).toEqual({
        field, direction: 'desc',
      });
    }
  });

  it('never remaps the sort when the mirror is off', () => {
    expect(effectiveSort(DEFAULT_FILTERS, { field: 'first_seen_at', direction: 'desc' }))
      .toEqual({ field: 'first_seen_at', direction: 'desc' });
    expect(effectiveSort(withPortals(['a', 'b']), { field: 'first_seen_at', direction: 'desc' }))
      .toEqual({ field: 'first_seen_at', direction: 'desc' });
  });

  it('keeps portal_sort_key out of the user-selectable sorts, so no URL can pin it', () => {
    /* It is derived from the filter state, never round-tripped through ?sort=. */
    expect(parseSort('-portal_sort_key')).toEqual(DEFAULT_SORT);
  });
});

/* Pipeline board broker hydration.
 *
 * Both broker reads moved onto the identity-gated /brokers API (2026-08-12). The
 * board used to swallow a PostgREST 42501 as an EXPECTED masked state and show no
 * broker at all; the API instead answers 200 with contact columns replaced by
 * has_email / has_phone. The card must therefore be able to say "a contact exists,
 * you just can't see it" — which only works if the flags survive the projection. */
describe('pipelineCardBroker', () => {
  const lb: ListingBroker = {
    sreality_id: null,
    listing_id: 10,
    broker_id: 7,
    broker_display_name: 'Jan Novák',
    broker_firm_label: 'RE/MAX',
  };
  const contact = (over: Partial<BrokerPublic>): BrokerPublic =>
    ({ broker_id: 7, ...over }) as BrokerPublic;

  it('is null when the listing has no resolved broker', () => {
    expect(pipelineCardBroker(undefined, undefined)).toBeNull();
    expect(pipelineCardBroker(undefined, contact({ primary_email: 'a@b.cz' }))).toBeNull();
  });

  it('keeps an admin session real values and derives both flags from them', () => {
    expect(
      pipelineCardBroker(lb, contact({ primary_email: 'jan@remax.cz', primary_phone: null })),
    ).toEqual({
      broker_id: 7,
      display_name: 'Jan Novák',
      firm_label: 'RE/MAX',
      email: 'jan@remax.cz',
      phone: null,
      has_email: true,
      has_phone: false,
    });
  });

  it('carries a non-admin masked row through as flags with no values', () => {
    const b = pipelineCardBroker(lb, contact({ has_email: true, has_phone: false }));
    expect(b).toMatchObject({ email: null, phone: null, has_email: true, has_phone: false });
  });

  /* A failed broker read degrades the CARD, never the board — but "no contact"
     must not be claimed for a broker we simply failed to hydrate. */
  it('reports no contact when the contact read produced nothing', () => {
    expect(pipelineCardBroker(lb, undefined)).toMatchObject({
      display_name: 'Jan Novák',
      has_email: false,
      has_phone: false,
    });
  });
});
