/* Tests for the spatial-predicate helpers in queries.ts.
 *
 * The interesting logic is `effectiveBbox`: when the operator picks
 * centre+radius mode, the cohort filter sends the bbox of the
 * circumscribing square of the radius circle (haversine math, no
 * dependency on PostGIS). Viewport mode falls through to whatever
 * the map's panning has set as `bounds`. These are pure functions
 * we want pinned against accidental edits.
 */

import { afterEach, describe, expect, it, vi } from 'vitest';

/* The broker allowlist is the one prefilter resolved over the JWT-gated API. */
vi.mock('./brokers', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./brokers')>()),
  fetchBrokerPropertyIds: vi.fn(async () => [1]),
}));

import { DEFAULT_FILTERS } from './filters';
import { ApiError } from './api';
import { supabase } from './supabase';
import {
  BROWSE_SELECT_COLUMNS,
  adScope,
  applyPortalRule,
  applyPrefilters,
  buildBrowseStatsArgs,
  fetchBrowseCount,
  fetchBrowseStats,
  fetchIsDismissed,
  fetchNoteCounts,
  fetchPropertySourcesByPropertyIds,
  fetchListingsForCards,
  fetchListingsForMap,
  fetchListingsForTable,
  districtsFilterClause,
  effectiveBbox,
  effectiveSort,
  matchesDistricts,
  orderPortal,
  parseSort,
  pipelineIdsForScope,
  priceNullTolerantOr,
  DEFAULT_SORT,
  type BrowsePrefilters,
  type DistrictMatchRow,
} from './queries';
import { fetchBrokerPropertyIds } from './brokers';
import type { DistrictChip, ListingFilters } from './filters';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

/* THE PostgREST stand-in for this file: every builder method chains (including
 * the `.retry()` / `.abortSignal()` pgRead sets, and the `.range()` fetchAllRows
 * drives), `.in()` records the ids it was handed, and awaiting the chain answers
 * with the rows registered for that relation, or with `error` as a 500.
 * `.rpc()` answers the same way and records its argument object. */
const stubReads = (rows: Record<string, object[]> = {}, error: unknown = null) => {
  const inIds: number[][] = [];
  const rpcArgs: Array<Record<string, unknown>> = [];
  const calls: Array<[string, unknown[]]> = [];
  const chain = (relation: string): unknown => {
    const data = rows[relation] ?? [];
    const answer = () =>
      Promise.resolve(
        error
          ? { data: null, error, count: null, status: 500, statusText: '' }
          : { data, error: null, count: data.length, status: 200, statusText: 'OK' },
      );
    const page: unknown = new Proxy(() => {}, {
      get: (_t, prop) => {
        if (prop === 'then') return (resolve: (v: unknown) => void) => answer().then(resolve);
        if (prop === 'in') {
          return (_column: string, ids: number[]) => {
            inIds.push(ids);
            return page;
          };
        }
        return (...args: unknown[]) => {
          calls.push([String(prop), args]);
          return page;
        };
      },
    });
    return page;
  };
  return {
    from: vi.spyOn(supabase, 'from').mockImplementation(((rel: string) => chain(rel)) as never),
    rpc: vi.spyOn(supabase, 'rpc').mockImplementation(((name: string, args: unknown) => {
      rpcArgs.push(args as Record<string, unknown>);
      return chain(name);
    }) as never),
    inIds,
    rpcArgs,
    calls,
  };
};

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
describe('applyPrefilters (prefilter id-spaces)', () => {
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
    obecIds: null,
    propertyIds: null,
    empty: false,
  };

  /* W3 S4 deleted the legacy city-quality listing-id allowlist together with
   * the `?cityQualityLegacy=1` hatch that was its only producer; city-quality
   * has resolved to an OBEC allowlist since W5 (migration 436). What remains is
   * that every surviving grain lands on its own column, in order. */
  it('leaves each prefilter grain on its own column', () => {
    const { q, calls } = record();
    applyPrefilters(q, {
      ...base,
      obecIds: [500],
      propertyIds: [7],
    });
    expect(calls).toEqual([
      { col: 'obec_id', vals: [500] },
      { col: 'property_id', vals: [7] },
    ]);
    expect(calls.some((c) => c.col === 'sreality_id')).toBe(false);
  });

  it('applies no id filter when every prefilter is inactive (null)', () => {
    const { q, calls } = record();
    applyPrefilters(q, base);
    expect(calls).toEqual([]);
  });

  it('applies an EMPTY allowlist — "scope on, nothing matched" is not "no constraint"', () => {
    const { q, calls } = record();
    applyPrefilters(q, { ...base, propertyIds: [] });
    expect(calls).toEqual([{ col: 'property_id', vals: [] }]);
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

/* The chip predicate itself moved to `lib/districtCodes.ts` and is pinned
 * against the CROSS-LANGUAGE table in `districtCodes.test.ts` (the same
 * `tests/fixtures/district_chip_plan.json` the API and the two RPC bodies are
 * tested against). What stays here is the queries.ts contract: the re-exports
 * exist, and Browse applies the clause to its read. */
describe('districtsFilterClause, re-exported by queries.ts', () => {
  it('returns null with no chips', () => {
    expect(districtsFilterClause([])).toBeNull();
  });

  it('compiles a chip to one code equality at its level', () => {
    expect(districtsFilterClause([
      { name: 'Jihlava', context: null, level: 'obec', id: 586846 },
    ])).toBe('and(or(obec_id.in.(586846)))');
  });

  it('compiles the quarter level Browse gained in W3', () => {
    expect(districtsFilterClause([
      { name: 'Žižkov', context: 'Praha', level: 'cast_obce', id: 490067 },
    ])).toBe('and(or(cast_obce_id.in.(490067)))');
  });

  it('never emits a name match for a chip that carries no code', () => {
    const got = districtsFilterClause([{ name: 'Brno', context: null }]);
    expect(got).toBe('and(or(obec_id.in.(-1)))');
    expect(got).not.toContain('ilike');
  });
});

describe('matchesDistricts, re-exported by queries.ts', () => {
  const mkRow = (o: Partial<DistrictMatchRow>): DistrictMatchRow => ({
    obec_id: null, okres_id: null, region_id: null, cast_obce_id: null, ...o,
  });

  it('matches any row when there are no chips', () => {
    expect(matchesDistricts(mkRow({ obec_id: 1 }), [])).toBe(true);
  });

  it('matches on the code at the chip level, never on a name', () => {
    const chip: DistrictChip = { name: 'Jihlava', context: null, level: 'obec', id: 586846 };
    expect(matchesDistricts(mkRow({ obec_id: 586846 }), [chip])).toBe(true);
    expect(matchesDistricts(mkRow({ obec_id: 999 }), [chip])).toBe(false);
  });

  it('splits include and exclude: included AND not excluded', () => {
    const inc: DistrictChip = { name: 'Jihlava', context: null, level: 'obec', id: 586846 };
    const exc: DistrictChip = {
      name: 'Modřany', context: null, level: 'cast_obce', id: 490017, excluded: true,
    };
    expect(matchesDistricts(mkRow({ obec_id: 586846 }), [inc, exc])).toBe(true);
    expect(
      matchesDistricts(mkRow({ obec_id: 586846, cast_obce_id: 490017 }), [inc, exc]),
    ).toBe(false);
    expect(matchesDistricts(mkRow({ obec_id: 1 }), [exc])).toBe(true);
  });
});

/* ---------------------------------------------------------------------- */
/* THE portal rule (MS19), against the table tests/test_portal_rule.py     */
/* runs through portal_status_matches, the RPCs and the Watchdog. Pure      */
/* semantics: every recorded predicate is evaluated against the case row.   */
/* ---------------------------------------------------------------------- */

type RuleRow = Record<string, unknown>;
interface RuleCase {
  name: string;
  is_active: boolean;
  all_sources: string[];
  active_sources: string[];
  portals: string[];
  status: 'any' | 'active' | 'inactive';
  expected: boolean;
}
interface BrokerCase {
  name: string;
  ads: Array<{ broker: string; source: string; is_active: boolean }>;
  broker: string;
  portals: string[];
  status: 'any' | 'active' | 'inactive';
  expected: boolean;
}

/* Records what applyPortalRule asks PostgREST for, as predicates over a row. */
const PORTAL_RULE = JSON.parse(
  readFileSync(join(process.cwd(), '..', 'tests', 'fixtures', 'portal_rule.json'), 'utf-8'),
) as { cases: RuleCase[]; broker_cases: BrokerCase[] };

class RuleRecorder {
  preds: Array<(row: RuleRow) => boolean> = [];
  ops: string[] = [];
  eq(c: string, v: unknown) {
    this.ops.push(`${c}.eq`);
    this.preds.push((row) => row[c] === v);
    return this;
  }
  overlaps(c: string, v: readonly string[]) {
    this.ops.push(`${c}.ov`);
    this.preds.push((row) => (row[c] as string[]).some((x) => v.includes(x)));
    return this;
  }
  not(c: string, op: string, v: unknown) {
    this.ops.push(`${c}.not.${op}`);
    if (op === 'ov') {
      const vals = String(v).replace(/^\{|\}$/g, '').split(',');
      this.preds.push((row) => !(row[c] as string[]).some((x) => vals.includes(x)));
    } else if (op === 'is' && v === null) {
      this.preds.push((row) => row[c] != null);
    } else {
      throw new Error(`unexpected not.${op}`);
    }
    return this;
  }
}

/* A browse_list row as the rollup stores it: listed <=> dated, per portal. */
const ruleRow = (c: Pick<RuleCase, 'is_active' | 'all_sources' | 'active_sources'>): RuleRow => {
  const row: RuleRow = { ...c };
  for (const p of ['sreality', 'bazos', 'idnes', 'maxima', 'ceskereality', 'bezrealitky',
    'mmreality', 'remax', 'realitymix']) {
    row[`newest_ad_at_${p}`] = c.all_sources.includes(p) ? '2026-09-01T00:00:00+00:00' : null;
  }
  return row;
};

const ruleMatches = (f: ListingFilters, row: RuleRow): boolean =>
  applyPortalRule(new RuleRecorder(), f).preds.every((p) => p(row));

describe('the portal rule (MS19)', () => {
  const filtersFor = (c: { portals: string[]; status: RuleCase['status'] }): ListingFilters => ({
    ...DEFAULT_FILTERS, portals: c.portals, status: c.status,
  });

  for (const c of PORTAL_RULE.cases) {
    it(c.name, () => {
      expect(ruleMatches(filtersFor(c), ruleRow(c))).toBe(c.expected);
    });
  }

  /* The broker lookup answers the same rule over ONE broker's ads (portal and broker =
   * one ad): the same rendering over the aggregate of the broker's ads agrees with it. */
  for (const c of PORTAL_RULE.broker_cases) {
    it(`broker: ${c.name}`, () => {
      const own = c.ads.filter((ad) => ad.broker === c.broker);
      const sources = (ads: typeof own) => [...new Set(ads.map((ad) => ad.source))].sort();
      const row = ruleRow({
        is_active: own.some((ad) => ad.is_active),
        all_sources: sources(own),
        active_sources: sources(own.filter((ad) => ad.is_active)),
      });
      expect(ruleMatches(filtersFor(c), row)).toBe(c.expected);
    });
  }

  it('steps aside under a broker: the server judged portal and status on its ads', () => {
    const f = { ...DEFAULT_FILTERS, brokerId: 527, portals: ['idnes', 'remax'], status: 'inactive' as const };
    expect(adScope(f)).toEqual({ portals: [], status: 'any' });
    expect(applyPortalRule(new RuleRecorder(), f).ops).toEqual([]);
  });

  it('keeps the one-portal conjunct under a broker, so the order stays indexable', () => {
    const f = { ...DEFAULT_FILTERS, brokerId: 527, portals: ['idnes'], status: 'active' as const };
    expect(applyPortalRule(new RuleRecorder(), f).ops).toEqual(['newest_ad_at_idnes.not.is']);
  });

  it('adds the conjunct for exactly one portal, and it narrows nothing', () => {
    const one = applyPortalRule(new RuleRecorder(), { ...DEFAULT_FILTERS, portals: ['maxima'] });
    expect(one.ops).toEqual(['all_sources.ov', 'newest_ad_at_maxima.not.is']);
    const two = applyPortalRule(new RuleRecorder(), { ...DEFAULT_FILTERS, portals: ['maxima', 'remax'] });
    expect(two.ops).toEqual(['all_sources.ov']);
    expect(applyPortalRule(new RuleRecorder(), DEFAULT_FILTERS).ops).toEqual([]);
  });
});

/* One portal's "Newest first" (MS19, Q49 b). */
describe('the one-portal order', () => {
  const withPortals = (portals: string[]) => ({ ...DEFAULT_FILTERS, portals });

  it('orders "newest/oldest first" by the portal\'s newest ad, both directions', () => {
    expect(orderPortal(withPortals(['idnes']), DEFAULT_SORT)).toBe('idnes');
    expect(effectiveSort(withPortals(['idnes']), { field: 'first_seen_at', direction: 'desc' }))
      .toEqual({ field: 'newest_ad_at_idnes', direction: 'desc' });
    expect(effectiveSort(withPortals(['maxima']), { field: 'first_seen_at', direction: 'asc' }))
      .toEqual({ field: 'newest_ad_at_maxima', direction: 'asc' });
  });

  it('falls back to the property\'s first seen with no portal or several', () => {
    for (const f of [DEFAULT_FILTERS, withPortals(['idnes', 'remax'])]) {
      expect(orderPortal(f, DEFAULT_SORT)).toBeNull();
      expect(effectiveSort(f, DEFAULT_SORT)).toEqual(DEFAULT_SORT);
    }
  });

  it('passes every other sort straight through', () => {
    for (const field of ['price_czk', 'price_per_m2', 'area_m2', 'last_seen_at',
                         'display_label', 'mf_gross_yield_pct'] as const) {
      expect(orderPortal(withPortals(['idnes']), { field, direction: 'desc' })).toBeNull();
      expect(effectiveSort(withPortals(['idnes']), { field, direction: 'desc' }))
        .toEqual({ field, direction: 'desc' });
    }
  });

  it('is not changed by a broker filter', () => {
    expect(effectiveSort({ ...withPortals(['idnes']), brokerId: 527 }, DEFAULT_SORT))
      .toEqual({ field: 'newest_ad_at_idnes', direction: 'desc' });
    expect(effectiveSort({ ...DEFAULT_FILTERS, brokerId: 527 }, DEFAULT_SORT)).toEqual(DEFAULT_SORT);
  });

  it('never reaches a URL: no ?sort= can pin it', () => {
    expect(parseSort('-newest_ad_at_idnes')).toEqual(DEFAULT_SORT);
  });

  it('pages the cards on the portal\'s column, with the rule and the conjunct on the chain', async () => {
    const s = stubReads();
    await fetchListingsForCards(withPortals(['idnes']), DEFAULT_SORT, null);
    const select = s.calls.find(([m]) => m === 'select')![1][0] as string;
    expect(select.split(',')).toEqual(expect.arrayContaining(
      ['newest_ad_at_idnes', 'property_id', 'all_sources', 'active_sources']));
    expect(s.calls).toContainEqual(['overlaps', ['all_sources', ['idnes']]]);
    expect(s.calls).toContainEqual(['not', ['newest_ad_at_idnes', 'is', null]]);
    expect(s.calls.filter(([m]) => m === 'order').map(([, a]) => a)).toEqual([
      ['newest_ad_at_idnes', { ascending: false, nullsFirst: undefined }],
      ['property_id', { ascending: false }],
    ]);
    vi.restoreAllMocks();
  });

  it('counts with the same chain the list pages with', async () => {
    const s = stubReads();
    await fetchBrowseCount({ ...withPortals(['idnes']), status: 'active' });
    expect(s.calls).toContainEqual(['overlaps', ['active_sources', ['idnes']]]);
    expect(s.calls).toContainEqual(['not', ['newest_ad_at_idnes', 'is', null]]);
    expect(s.calls.some(([m, a]) => m === 'eq' && a[0] === 'is_active')).toBe(false);
    vi.restoreAllMocks();
  });
});

/* The measure travels with its PUBLISHED LABEL on every Browse lane, or the
 * number arrives unlabelable. All six migration-425 relations publish
 * `price_per_m2_basis` — including `browse_list` and `properties_map_mv`, whose
 * rebuilds run inside the migration and whose column presence § 9 asserts
 * before it commits — so no surface re-derives the basis in TypeScript.
 * `category_main` / `category_type` ride along for what the basis token does
 * not say: the denominator (plot vs floor area) and the monthly period on the
 * absolute price. */
describe('Browse select-lists carry the measure with its published basis', () => {
  const cols = (list: string): string[] => list.split(',');

  it('selects the measure, its published basis, and both category columns', () => {
    for (const [lane, list] of Object.entries(BROWSE_SELECT_COLUMNS)) {
      const c = cols(list);
      expect(c, `${lane}: price_per_m2`).toContain('price_per_m2');
      expect(c, `${lane}: price_per_m2_basis`).toContain('price_per_m2_basis');
      expect(c, `${lane}: category_main`).toContain('category_main');
      expect(c, `${lane}: category_type`).toContain('category_type');
    }
  });
});

/* The "~NaN" Browse header (2026-09-25). The count asks the dismissal-aware
 * FUNCTION for an exact total, and when that misses its budget falls back to the
 * planner's estimate. PostgREST plans a count only for a table or view: over a
 * function call its Content-Range total is `*`, which the client parses to NaN.
 * Driven through the real client against a stubbed PostgREST, so the parse that
 * produced the NaN is the one under test. */
/* A refused answer as it arrives on the wire. Every count read is `head: true`,
 * which supabase-js sends as an HTTP HEAD, and a HEAD response carries no body,
 * so a count's SQLSTATE never reaches the client: only its status does. */
const failed = (status: number, body: object) => (init: RequestInit) =>
  ({
    ok: false,
    status,
    statusText: '',
    headers: new Headers(),
    text: async () => (init.method === 'HEAD' ? '' : JSON.stringify(body)),
  }) as unknown as Response;
/* An exact count that never answers: it settles only when its signal aborts,
 * on our own budget or the caller's say. */
const STALLS = (init: RequestInit) =>
  new Promise<Response>((_resolve, reject) => {
    init.signal!.addEventListener('abort', () => reject(init.signal!.reason));
  });

describe('the Browse cohort total is always a number', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  /* Production as observed: the exact count misses its 2.5 s client budget;
   * a planned count carries a total only when it reads a relation. Every
   * deadline runs 100x faster here, so the real budget path is exercised
   * without a 2.5 s wait per case, and `budgets` records the unscaled values. */
  const stubPostgrest = (
    relationEstimate: string,
    exact: (init: RequestInit) => Response | Promise<Response> = STALLS,
  ) => {
    const realTimeout = AbortSignal.timeout.bind(AbortSignal);
    const budgets = vi
      .spyOn(AbortSignal, 'timeout')
      .mockImplementation((ms: number) => realTimeout(ms / 100));
    const planned: string[] = [];
    const fetch = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
      const path = new URL(String(input)).pathname;
      const prefer = new Headers(init.headers).get('Prefer') ?? '';
      if (prefer.includes('count=exact')) return exact(init);
      if (prefer.includes('count=planned')) planned.push(path);
      const total = path.includes('/rpc/') ? '*' : relationEstimate;
      return {
        ok: true,
        status: 200,
        statusText: 'OK',
        headers: new Headers({ 'Content-Range': `*/${total}` }),
        text: async () => '',
      } as unknown as Response;
    });
    vi.stubGlobal('fetch', fetch);
    return { planned, budgets, fetch };
  };

  it('falls back to the estimate of the plain relation when the exact count misses its budget', async () => {
    const { planned, budgets, fetch } = stubPostgrest('72');
    const total = await fetchBrowseCount(DEFAULT_FILTERS);
    expect(Number.isFinite(total.value)).toBe(true);
    expect(total).toEqual({ value: 72, precise: false });
    expect(planned).toEqual(['/rest/v1/browse_list']);
    expect(budgets).toHaveBeenCalledWith(2500);
    expect(fetch.mock.calls[0][1]?.method).toBe('HEAD');
  });

  it('estimates a one-portal cohort from the plain list, with the rule and its conjunct', async () => {
    const { planned, fetch } = stubPostgrest('15');
    const total = await fetchBrowseCount({ ...DEFAULT_FILTERS, portals: ['bazos'] });
    expect(total).toEqual({ value: 15, precise: false });
    expect(planned).toEqual(['/rest/v1/browse_list']);
    const url = new URL(String(fetch.mock.calls.at(-1)![0]));
    expect(url.searchParams.get('all_sources')).toBe('ov.{bazos}');
    expect(url.searchParams.get('newest_ad_at_bazos')).toBe('not.is.null');
  });

  it('fails the count, which the header shows as an error, when there is no number at all', async () => {
    stubPostgrest('*');
    await expect(fetchBrowseCount(DEFAULT_FILTERS)).rejects.toThrow(/count unavailable/);
  });

  /* A caller abort is a cancellation, not "too slow": no estimate is asked for. */
  it('cancels, never estimating, when the caller aborts during the exact count', async () => {
    const { planned, budgets, fetch } = stubPostgrest('72');
    budgets.mockRestore();
    const ctrl = new AbortController();
    const p = fetchBrowseCount(DEFAULT_FILTERS, { signal: ctrl.signal });
    await vi.waitFor(() => expect(fetch).toHaveBeenCalled());
    ctrl.abort();
    const err = await p.catch((e: unknown) => e);
    expect(err).not.toBeInstanceOf(ApiError);
    expect((err as Error).name).toBe('AbortError');
    expect(planned).toEqual([]);
  });

  /* Only "too slow" earns the estimate. A refused or malformed exact count is a
   * real failure; answering it with a different relation's estimate hid it. */
  it('does not answer a failed exact count with an estimate', async () => {
    const { planned } = stubPostgrest(
      '72',
      failed(400, { code: 'PGRST100', message: 'failed to parse filter' }),
    );
    await expect(fetchBrowseCount(DEFAULT_FILTERS)).rejects.toMatchObject({
      name: 'ApiError',
      status: 400,
      kind: 'http',
    });
    expect(planned).toEqual([]);
  });
});

/* Migration 537. Every Browse cohort read starts from ONE source: the
 * relation's dismissal-aware twin by default (the exclusion happens
 * server-side, under the caller's RLS — a dismissed set never rides in the
 * URL), the plain relation only when the operator reveals dismissed ones. */
describe('dismissed properties are hidden at the source', () => {
  afterEach(() => vi.restoreAllMocks());

  const revealed = { ...DEFAULT_FILTERS, showDismissed: true };
  const onePortal = { ...DEFAULT_FILTERS, portals: ['bazos'] };

  it('reads the visible twins by default, for cards, table and count', async () => {
    const s = stubReads();
    await fetchListingsForCards(DEFAULT_FILTERS, DEFAULT_SORT, null);
    await fetchListingsForTable(DEFAULT_FILTERS, DEFAULT_SORT, null);
    await fetchBrowseCount(DEFAULT_FILTERS);
    expect(s.rpc.mock.calls.map((c) => c[0])).toEqual([
      'browse_list_visible', 'browse_list_visible', 'browse_list_visible',
    ]);
    expect(s.rpc.mock.calls[0].slice(1)).toEqual([{}, { get: true }]);
    expect(s.rpc.mock.calls[2].slice(1)).toEqual([{}, { get: true, count: 'exact', head: true }]);
    expect(s.from).not.toHaveBeenCalled();
  });

  it('reads the plain relation only when dismissed properties are revealed', async () => {
    const s = stubReads();
    await fetchListingsForCards(revealed, DEFAULT_SORT, null);
    await fetchBrowseCount(revealed);
    expect(s.from.mock.calls.map((c) => c[0])).toEqual(['browse_list', 'browse_list']);
    expect(s.rpc).not.toHaveBeenCalled();
  });

  it('reads the same twin under one portal: there is no second relation', async () => {
    const s = stubReads();
    await fetchListingsForCards(onePortal, DEFAULT_SORT, null);
    expect(s.rpc.mock.calls.map((c) => c[0])).toEqual(['browse_list_visible']);
    await fetchListingsForCards({ ...onePortal, showDismissed: true }, DEFAULT_SORT, null);
    expect(s.from.mock.calls.map((c) => c[0])).toEqual(['browse_list']);
  });

  it('tells the map cells and reads the map pins through the same twin', async () => {
    const s = stubReads();
    await fetchListingsForMap(DEFAULT_FILTERS);
    const [cells, pins] = s.rpc.mock.calls;
    expect(cells[0]).toBe('browse_map_cells');
    expect((cells[1] as Record<string, unknown>).hide_dismissed).toBe(true);
    expect(pins[0]).toBe('properties_map_visible');
  });

  it('puts the flag on the one Stats/map argument object', () => {
    const resolved = { obec_ids_filter: null, property_ids_filter: null };
    expect(buildBrowseStatsArgs(DEFAULT_FILTERS, resolved).hide_dismissed).toBe(true);
    expect(buildBrowseStatsArgs(revealed, resolved).hide_dismissed).toBe(false);
  });
});

/* Rule #16: Stats and the map must describe the cohort the list describes. The
 * five Size-group controls (plot, usable, garden, parking count) narrowed the
 * list and were silently dropped on the way to the two aggregate RPCs, whose
 * parameters have existed since migrations 133/439. */
describe('buildBrowseStatsArgs sends the size bounds', () => {
  const resolved = { obec_ids_filter: null, property_ids_filter: null };

  it('passes every Size-group bound through', () => {
    const args = buildBrowseStatsArgs({
      ...DEFAULT_FILTERS,
      estateAreaMin: 400, estateAreaMax: 1200,
      usableAreaMin: 60, usableAreaMax: 90,
      gardenAreaMin: 100, gardenAreaMax: 300,
      parkingLotsMin: 2,
    }, resolved);
    expect(args.estate_area_min_filter).toBe(400);
    expect(args.estate_area_max_filter).toBe(1200);
    expect(args.usable_area_min_filter).toBe(60);
    expect(args.usable_area_max_filter).toBe(90);
    expect(args.garden_area_min_filter).toBe(100);
    expect(args.garden_area_max_filter).toBe(300);
    expect(args.parking_lots_min_filter).toBe(2);
  });

  it('sends null when unset, never undefined (the RPC default is the same)', () => {
    const args = buildBrowseStatsArgs(DEFAULT_FILTERS, resolved);
    for (const k of [
      'estate_area_min_filter', 'estate_area_max_filter',
      'usable_area_min_filter', 'usable_area_max_filter',
      'garden_area_min_filter', 'garden_area_max_filter',
      'parking_lots_min_filter',
    ]) {
      expect(args[k]).toBeNull();
    }
  });
});

/* A page of cards asks per property; the view is read once per batch. */
describe('fetchIsDismissed batches per task', () => {
  afterEach(() => vi.restoreAllMocks());

  it('answers every call made in one task with one read', async () => {
    const { inIds } = stubReads({ property_dismissals_public: [{ property_id: 2 }] });
    const answers = await Promise.all([1, 2, 3, 2].map((id) => fetchIsDismissed(id)));
    expect(answers).toEqual([false, true, false, true]);
    expect(inIds).toEqual([[1, 2, 3]]);
    expect(supabase.from).toHaveBeenCalledWith('property_dismissals_public');
  });

  it('never sends more than 200 ids in one URL', async () => {
    const { inIds } = stubReads();
    await Promise.all(Array.from({ length: 450 }, (_, i) => fetchIsDismissed(i + 1)));
    expect(inIds.map((c) => c.length)).toEqual([200, 200, 50]);
  });

  it('fails every waiter of a failed read', async () => {
    stubReads({}, new Error('boom'));
    const outcomes = await Promise.allSettled([fetchIsDismissed(1), fetchIsDismissed(2)]);
    expect(outcomes.map((o) => o.status)).toEqual(['rejected', 'rejected']);
  });
});

/* One whole-set read answers every Browse row's note mark (MS16), and one batch
 * every board card's ads (MS7) — never one request per row or card. */
describe('note counts and board ads, one read each', () => {
  afterEach(() => vi.restoreAllMocks());

  it('counts the caller’s notes per property', async () => {
    stubReads({ property_notes_public: [1, 2, 3].map((id) => ({ id, property_id: id < 3 ? 42 : 7 })) });
    expect([...(await fetchNoteCounts())]).toEqual([[42, 2], [7, 1]]);
    expect(supabase.from).toHaveBeenCalledTimes(1);
  });

  it('groups the cards’ ads by property', async () => {
    const { inIds } = stubReads({
      property_sources_public: [{ id: 1, property_id: 42 }, { id: 2, property_id: 43 }, { id: 3, property_id: 42 }],
    });
    const byProperty = await fetchPropertySourcesByPropertyIds([42, 43]);
    expect(inIds).toEqual([[42, 43]]);
    expect(byProperty.get(42)?.map((a) => a.id)).toEqual([1, 3]);
  });
});
/* Rule #16 again, on the one Browse fetcher that used to name its prefilters by
 * hand. Every property-grain filter now resolves ONCE, in resolveBrowsePrefilters,
 * and reaches list, map and Stats through the same `property_ids_filter` seam
 * (migration 378) — so a new curated-set filter cannot narrow the list and leave
 * the panel above it counting the whole market. */
describe('Browse Stats resolves through the one prefilter path', () => {
  afterEach(() => vi.restoreAllMocks());

  const MEMBERS = [
    { property_id: 1, collection_id: 10 },
    { property_id: 2, collection_id: 11 },
    { property_id: 3, collection_id: 12 },
  ];

  it('sends the collection allowlist as property_ids_filter, OR across the selection', async () => {
    const { rpcArgs } = stubReads({ collection_properties_public: MEMBERS });
    await fetchBrowseStats({ ...DEFAULT_FILTERS, collections: [10, 12] });
    expect(rpcArgs[0].property_ids_filter).toEqual([1, 3]);
  });

  it('sends [] — not null — for a selection nothing is in', async () => {
    const { rpcArgs } = stubReads({ collection_properties_public: MEMBERS });
    await fetchBrowseStats({ ...DEFAULT_FILTERS, collections: [99] });
    expect(rpcArgs[0].property_ids_filter).toEqual([]);
  });

  it('keeps the tag AND contract, resolved off the membership rows', async () => {
    const { rpcArgs } = stubReads({
      property_tags_public: [
        { property_id: 1, tag_id: 5 },
        { property_id: 1, tag_id: 6 },
        { property_id: 2, tag_id: 5 },
      ],
    });
    await fetchBrowseStats({ ...DEFAULT_FILTERS, tags: [5, 6] });
    expect(rpcArgs[0].property_ids_filter).toEqual([1]);
    /* property_tags_public, not the properties_with_tags RPC, whose body
     * truncates at 5000 rows under a comment claiming exhaustiveness. */
    expect(supabase.from).toHaveBeenCalledWith('property_tags_public');
  });

  it('intersects the curated sets: a property must satisfy both', async () => {
    const { rpcArgs } = stubReads({
      collection_properties_public: MEMBERS,
      property_tags_public: [
        { property_id: 2, tag_id: 5 },
        { property_id: 3, tag_id: 5 },
      ],
    });
    await fetchBrowseStats({ ...DEFAULT_FILTERS, collections: [10, 12], tags: [5] });
    expect(rpcArgs[0].property_ids_filter).toEqual([3]);
  });

  it('sends the RPC own tag / estimate predicates OFF — one resolution path', async () => {
    const { rpcArgs } = stubReads({ collection_properties_public: MEMBERS });
    await fetchBrowseStats({ ...DEFAULT_FILTERS, tags: [5], withEstimates: true });
    expect(rpcArgs[0].tag_ids).toBeNull();
    expect(rpcArgs[0].with_estimates).toBe(false);
  });

  it('carries the broker in property_ids_filter; the server judged its portal and status', async () => {
    const { rpcArgs } = stubReads();
    await fetchBrowseStats({
      ...DEFAULT_FILTERS, brokerId: 527, portals: ['idnes'], status: 'active',
    });
    expect(fetchBrokerPropertyIds).toHaveBeenCalledWith(527, 'active', ['idnes']);
    expect(rpcArgs[0].property_ids_filter).toEqual([1]);
    expect(rpcArgs[0].portal_filter).toBeNull();
    expect(rpcArgs[0].active_only_filter).toBe(false);
  });

  it('sends the portal rule\'s two inputs without a broker', () => {
    const args = buildBrowseStatsArgs(
      { ...DEFAULT_FILTERS, portals: ['idnes'], status: 'inactive' },
      { obec_ids_filter: null, property_ids_filter: null },
    );
    expect(args.portal_filter).toEqual(['idnes']);
    expect([args.active_only_filter, args.inactive_only_filter]).toEqual([false, true]);
  });

  it('never names the retired listing_ids_filter on the map, and carries the broker', async () => {
    const { rpcArgs } = stubReads();
    await fetchListingsForMap({ ...DEFAULT_FILTERS, brokerId: 527 });
    expect(rpcArgs[0]).not.toHaveProperty('listing_ids_filter');
    expect(rpcArgs[0].property_ids_filter).toEqual([1]);
    expect(rpcArgs[0].point_budget).toBe(2000);
  });
});
