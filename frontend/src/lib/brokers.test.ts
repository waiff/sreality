/* The repointed broker read layer (lib/brokers.ts).
 *
 * These functions moved off supabase-js onto the identity-gated /brokers API on
 * 2026-08-12. Three things are worth pinning, because each has a silent-failure
 * mode: (1) EVERY call must carry the caller's real session JWT — the routes
 * reject the static bundle token, and a missed `jwt: true` reads as "no broker"
 * rather than as an error; (2) a 404 (unknown broker) is an ANSWER, everything
 * else must propagate; (3) contact PII arrives as
 * has_email/has_phone flags for a non-admin, and `contactState` must not collapse
 * that into "no contact exists".
 *
 * The module is imported dynamically because lib/api reads BASE_URL and TOKEN
 * from import.meta.env at module-evaluation time.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { supabase } from './supabase';

async function loadBrokers() {
  vi.stubEnv('VITE_API_BASE_URL', 'https://api.test.invalid');
  vi.stubEnv('VITE_API_TOKEN', 'STATIC-BUNDLE-TOKEN');
  return import('./brokers');
}

interface Call {
  url: string;
  init: RequestInit | undefined;
}

function stubFetch(
  responder: (url: string) => { status?: number; body?: unknown },
): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, init });
    const { status = 200, body = { data: [] } } = responder(url);
    return {
      ok: status >= 200 && status < 300,
      status,
      statusText: 'OK',
      text: async () => JSON.stringify(body),
    } as Response;
  });
  return calls;
}

const authHeader = (c: Call): string | undefined =>
  (c.init?.headers as Record<string, string> | undefined)?.Authorization;

beforeEach(() => {
  vi.spyOn(supabase.auth, 'getSession').mockResolvedValue({
    data: { session: { access_token: 'USER-JWT' } },
    error: null,
  } as unknown as Awaited<ReturnType<typeof supabase.auth.getSession>>);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe('contactState', () => {
  it('reports the real value for an admin session', async () => {
    const { contactState } = await loadBrokers();
    expect(contactState('+420777123456', undefined)).toEqual({
      state: 'value',
      value: '+420777123456',
    });
  });

  it('reports `masked` when only the has_* flag arrived', async () => {
    const { contactState } = await loadBrokers();
    expect(contactState(undefined, true)).toEqual({ state: 'masked' });
  });

  it('reports `none` when the flag says no contact is on file', async () => {
    const { contactState } = await loadBrokers();
    expect(contactState(undefined, false)).toEqual({ state: 'none' });
    expect(contactState(null, undefined)).toEqual({ state: 'none' });
  });

  /* The regression that matters: a masked row must never look like an empty one. */
  it('separates masked from none', async () => {
    const { contactState } = await loadBrokers();
    expect(contactState(undefined, true)).not.toEqual(contactState(undefined, false));
  });
});

describe('auth mode', () => {
  it('sends the session JWT on every repointed read, never the bundle token', async () => {
    const calls = stubFetch(() => ({ body: { data: [] } }));
    const b = await loadBrokers();
    await b.fetchBrokerLeaderboard({
      regionIds: [],
      okresIds: [],
      obecIds: [],
      categoryMain: null,
      categoryType: null,
      metric: 'active_property_count',
    });
    await b.searchBrokersByName('novak');
    await b.searchBrokerFirms('mmreality');
    await b.fetchListingBrokersByIds([1]);
    await b.fetchBrokerListings(7);
    expect(calls).toHaveLength(5);
    for (const c of calls) expect(authHeader(c)).toBe('Bearer USER-JWT');
  });
});

describe('fetchBrokerLeaderboard', () => {
  it('sends each geo level as repeated params and omits the empty ones', async () => {
    const calls = stubFetch(() => ({ body: { data: [] } }));
    const { fetchBrokerLeaderboard } = await loadBrokers();
    await fetchBrokerLeaderboard({
      regionIds: [19, 27],
      okresIds: [],
      obecIds: [554782],
      categoryMain: 'byt',
      categoryType: 'prodej',
      metric: 'listing_count',
      limit: 50,
      firmIds: [8, 41],
    });
    const url = new URL(calls[0].url);
    expect(url.pathname).toBe('/brokers/leaderboard');
    expect(url.searchParams.getAll('region_ids')).toEqual(['19', '27']);
    expect(url.searchParams.getAll('okres_ids')).toEqual([]);
    expect(url.searchParams.getAll('obec_ids')).toEqual(['554782']);
    expect(url.searchParams.get('metric')).toBe('listing_count');
    expect(url.searchParams.get('limit')).toBe('50');
    expect(url.searchParams.getAll('firm_ids')).toEqual(['8', '41']);
  });

  it('omits firm_ids when no company filter is set', async () => {
    const calls = stubFetch(() => ({ body: { data: [] } }));
    const { fetchBrokerLeaderboard } = await loadBrokers();
    await fetchBrokerLeaderboard({
      regionIds: [], okresIds: [], obecIds: [],
      categoryMain: null, categoryType: null, metric: 'listing_count',
    });
    expect(new URL(calls[0].url).searchParams.getAll('firm_ids')).toEqual([]);
  });

  it('sends min_price_czk and include_unpriced when a value filter is set', async () => {
    const calls = stubFetch(() => ({ body: { data: [] } }));
    const { fetchBrokerLeaderboard } = await loadBrokers();
    await fetchBrokerLeaderboard({
      regionIds: [], okresIds: [], obecIds: [],
      categoryMain: 'byt', categoryType: 'prodej', metric: 'active_property_count',
      minPriceCzk: 5_000_000, includeUnpriced: true,
    });
    const url = new URL(calls[0].url);
    expect(url.searchParams.get('min_price_czk')).toBe('5000000');
    expect(url.searchParams.get('include_unpriced')).toBe('true');
  });

  it('sends each selected subtype as a repeated param, with the unknown-subtype flag', async () => {
    const calls = stubFetch(() => ({ body: { data: [] } }));
    const { fetchBrokerLeaderboard } = await loadBrokers();
    await fetchBrokerLeaderboard({
      regionIds: [], okresIds: [], obecIds: [],
      categoryMain: 'komercni', categoryType: null, metric: 'active_property_count',
      subtypes: ['kancelar', 'sklad'], includeUnknownSubtype: true,
    });
    const url = new URL(calls[0].url);
    expect(url.searchParams.getAll('subtypes')).toEqual(['kancelar', 'sklad']);
    expect(url.searchParams.get('include_unknown_subtype')).toBe('true');
  });

  it('omits subtypes entirely when none are selected', async () => {
    const calls = stubFetch(() => ({ body: { data: [] } }));
    const { fetchBrokerLeaderboard } = await loadBrokers();
    await fetchBrokerLeaderboard({
      regionIds: [], okresIds: [], obecIds: [],
      categoryMain: null, categoryType: null, metric: 'listing_count',
    });
    const url = new URL(calls[0].url);
    expect(url.searchParams.getAll('subtypes')).toEqual([]);
    expect(url.searchParams.get('include_unknown_subtype')).toBe('false');
  });

  // null/undefined must be OMITTED, not sent as the literal string "null" — the
  // API route's `int | None` default only applies when the param is absent.
  it('omits min_price_czk when unset and defaults include_unpriced to false', async () => {
    const calls = stubFetch(() => ({ body: { data: [] } }));
    const { fetchBrokerLeaderboard } = await loadBrokers();
    await fetchBrokerLeaderboard({
      regionIds: [], okresIds: [], obecIds: [],
      categoryMain: null, categoryType: null, metric: 'listing_count',
    });
    const url = new URL(calls[0].url);
    expect(url.searchParams.has('min_price_czk')).toBe(false);
    expect(url.searchParams.get('include_unpriced')).toBe('false');
  });

  it('keeps the masked flags on the returned rows', async () => {
    stubFetch(() => ({
      body: {
        data: [{ broker_id: 1, display_name: 'A', has_email: true, has_phone: false }],
        metadata: { pii_masked: true },
      },
    }));
    const { fetchBrokerLeaderboard, contactState } = await loadBrokers();
    const rows = await fetchBrokerLeaderboard({
      regionIds: [], okresIds: [], obecIds: [],
      categoryMain: null, categoryType: null, metric: 'listing_count',
    });
    expect(contactState(rows[0].primary_email, rows[0].has_email)).toEqual({
      state: 'masked',
    });
    expect(contactState(rows[0].primary_phone, rows[0].has_phone)).toEqual({
      state: 'none',
    });
  });
});

describe('searchBrokersByName', () => {
  it('short-circuits below 2 chars without a round-trip', async () => {
    const calls = stubFetch(() => ({ body: { data: [] } }));
    const { searchBrokersByName } = await loadBrokers();
    expect(await searchBrokersByName(' a ')).toEqual([]);
    expect(calls).toHaveLength(0);
  });

  it('sends the trimmed term', async () => {
    const calls = stubFetch(() => ({ body: { data: [] } }));
    const { searchBrokersByName } = await loadBrokers();
    await searchBrokersByName('  novak  ');
    expect(new URL(calls[0].url).searchParams.get('q')).toBe('novak');
  });
});

describe('searchBrokerFirms', () => {
  it('omits q rather than short-circuiting on an empty query, unlike broker name search', async () => {
    // Companies are browsable before typing (top firms by broker_count), so an
    // empty query is a real request, not a no-op the way it is for broker names.
    const calls = stubFetch(() => ({ body: { data: [] } }));
    const { searchBrokerFirms } = await loadBrokers();
    await searchBrokerFirms('  ');
    expect(calls).toHaveLength(1);
    expect(new URL(calls[0].url).searchParams.has('q')).toBe(false);
  });

  it('sends the trimmed term', async () => {
    const calls = stubFetch(() => ({ body: { data: [] } }));
    const { searchBrokerFirms } = await loadBrokers();
    await searchBrokerFirms('  mmreality  ');
    expect(new URL(calls[0].url).searchParams.get('q')).toBe('mmreality');
  });

  it('returns the firm options from the envelope', async () => {
    stubFetch(() => ({
      body: {
        data: [{ firm_id: 3, canonical_domain: 'mmreality.cz', display_name: null,
                 is_franchise: true, broker_count: 1021 }],
      },
    }));
    const { searchBrokerFirms } = await loadBrokers();
    const rows = await searchBrokerFirms('mmreality');
    expect(rows).toEqual([{ firm_id: 3, canonical_domain: 'mmreality.cz', display_name: null,
                            is_franchise: true, broker_count: 1021 }]);
  });
});

/* MS7: a property's brokers are a list over its ads, and nothing stores it. */
describe('propertyBrokers', () => {
  const broker = (listing_id: number, broker_id: number) => ({
    sreality_id: null,
    listing_id,
    broker_id,
    broker_display_name: `Makléř ${broker_id}`,
    broker_firm_label: null,
  });
  const ad = (id: number, is_active = true) => ({ id, is_active });

  const ids = (out: { brokers: { broker_id: number }[] }) => out.brokers.map((b) => b.broker_id);
  const byListing = new Map([[1, broker(1, 10)], [2, broker(2, 10)], [3, broker(3, 30)]]);

  it('lists the active ads’ brokers once each, the canonical ad’s first, skipping the unattributed', async () => {
    const { propertyBrokers } = await loadBrokers();
    const out = propertyBrokers([ad(1), ad(2), ad(3), ad(4)], byListing, 3);
    expect(ids(out)).toEqual([30, 10]);
    expect(out.fromInactive).toBe(false);
    // An inactive ad's broker is hidden while another ad is active.
    expect(ids(propertyBrokers([ad(1), ad(3, false)], byListing, 1))).toEqual([10]);
  });

  it('falls back to every ad’s broker, marked, when no ad is active', async () => {
    const { propertyBrokers } = await loadBrokers();
    const out = propertyBrokers([ad(1, false), ad(3, false)], byListing, 3);
    expect(ids(out)).toEqual([30, 10]);
    expect(out.fromInactive).toBe(true);
  });
});

describe('batched hydration', () => {
  it('POSTs the listing ids and keys the result map on listing_id', async () => {
    const calls = stubFetch(() => ({
      body: {
        data: [
          { listing_id: 10, broker_id: 1, sreality_id: null },
          { listing_id: 11, broker_id: 2, sreality_id: 999 },
        ],
      },
    }));
    const { fetchListingBrokersByIds } = await loadBrokers();
    const map = await fetchListingBrokersByIds([10, 11]);
    expect(calls[0].init?.method).toBe('POST');
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({ listing_ids: [10, 11] });
    expect(map.get(10)?.broker_id).toBe(1);
    expect(map.get(11)?.broker_id).toBe(2);
  });

  it('short-circuits the batch read on an empty id list', async () => {
    const calls = stubFetch(() => ({ body: { data: [] } }));
    const { fetchListingBrokersByIds } = await loadBrokers();
    expect((await fetchListingBrokersByIds([])).size).toBe(0);
    expect(calls).toHaveLength(0);
  });

  /* Both routes cap their input at toolkit.brokers.MAX_BATCH (1000) with a 422,
     not a truncated 200 — one oversized call would drop EVERY card's broker, not
     just the overflow. The old supabase-js `.in()` had no such cap, so this cliff
     arrived with the repoint. */
  it('chunks the POST below the 1000-id route cap and covers every id', async () => {
    const calls = stubFetch(() => ({ body: { data: [] } }));
    const { fetchListingBrokersByIds } = await loadBrokers();
    const ids = Array.from({ length: 2_500 }, (_, i) => i + 1);
    await fetchListingBrokersByIds(ids);
    const bodies = calls.map(
      (c) => (JSON.parse(String(c.init?.body)) as { listing_ids: number[] }).listing_ids,
    );
    expect(bodies).toHaveLength(3);
    for (const b of bodies) expect(b.length).toBeLessThanOrEqual(1000);
    expect(bodies.flat()).toEqual(ids);
  });

  /* W6: the contact pair rides on the attribution row (migration 419), so the
     whole broker line — name, firm, both channels — comes out of THIS one call.
     The deleted GET twin is what used to carry primary_email / primary_phone; if a
     later change drops them from the POST projection, this is what says so. */
  it('carries the contact pair on the same row as the identity', async () => {
    const calls = stubFetch(() => ({
      body: {
        data: [
          {
            listing_id: 10,
            broker_id: 1,
            sreality_id: null,
            broker_display_name: 'Jan Novák',
            broker_firm_label: 'RE/MAX',
            primary_email: 'jan@remax.cz',
            primary_phone: '+420777123456',
          },
        ],
      },
    }));
    const { fetchListingBrokersByIds } = await loadBrokers();
    const map = await fetchListingBrokersByIds([10]);
    expect(calls).toHaveLength(1);
    expect(map.get(10)).toMatchObject({
      broker_display_name: 'Jan Novák',
      primary_email: 'jan@remax.cz',
      primary_phone: '+420777123456',
    });
  });

  /* A genuinely empty batch (none of the requested ids matched, e.g. filtered by
     status) is a real, successful `data: []` — must resolve to an empty map, not
     throw. Distinguishes this from the malformed-response case right below. */
  it('resolves an empty map for a successful empty batch', async () => {
    stubFetch(() => ({ body: { data: [] } }));
    const { fetchListingBrokersByIds } = await loadBrokers();
    await expect(fetchListingBrokersByIds([3])).resolves.toEqual(new Map());
  });

  /* An SPA-fallback HTML page (or a proxy page) answers 200 with no envelope.
     Silently returning an empty map here reads, one layer up, as "the broker read
     succeeded and found nothing" — indistinguishable from the case above — when
     what actually happened is the read never reached the API at all. The guard
     moved here from the deleted fetchBrokersByIds; with one read left it now
     covers the ENTIRE broker line rather than half of it. */
  it('throws on a 200 that carries no envelope', async () => {
    stubFetch(() => ({ body: '<!doctype html><title>app</title>' }));
    const { fetchListingBrokersByIds } = await loadBrokers();
    await expect(fetchListingBrokersByIds([3])).rejects.toThrow(/malformed/);
  });
});

describe('fetchBrokerDossier', () => {
  const dossierBody = (extra: Record<string, unknown>, metadata?: unknown) => ({
    data: {
      broker: { broker_id: 7, display_name: 'Jan' },
      memberships: [],
      region_shares: [{ geo_id: 19, name: 'Praha', active_property_count: 4 }],
      ...extra,
    },
    ...(metadata === undefined ? {} : { metadata }),
  });

  it('returns identity, firms and region shares from ONE call', async () => {
    const calls = stubFetch(() => ({
      body: dossierBody({ contacts: [] }, { pii_masked: false }),
    }));
    const { fetchBrokerDossier } = await loadBrokers();
    const d = await fetchBrokerDossier(7);
    expect(calls).toHaveLength(1);
    expect(new URL(calls[0].url).pathname).toBe('/brokers/7');
    // The region name is joined server-side — the page no longer needs the
    // geo-options query it used to gate the shares query on.
    expect(d?.region_shares[0].name).toBe('Praha');
    expect(d?.pii_masked).toBe(false);
  });

  it('carries pii_masked through for a non-admin (contacts key absent)', async () => {
    stubFetch(() => ({ body: dossierBody({}, { pii_masked: true }) }));
    const { fetchBrokerDossier } = await loadBrokers();
    const d = await fetchBrokerDossier(7);
    expect(d?.pii_masked).toBe(true);
    expect(d?.contacts).toBeUndefined();
  });

  it('fails closed to masked when metadata is missing', async () => {
    stubFetch(() => ({ body: dossierBody({}) }));
    const { fetchBrokerDossier } = await loadBrokers();
    expect((await fetchBrokerDossier(7))?.pii_masked).toBe(true);
  });

  it('returns null for an unknown broker (404) and rethrows anything else', async () => {
    stubFetch(() => ({ status: 404, body: { detail: 'broker not found' } }));
    const { fetchBrokerDossier } = await loadBrokers();
    await expect(fetchBrokerDossier(7)).resolves.toBeNull();

    vi.unstubAllGlobals();
    stubFetch(() => ({ status: 403, body: { detail: 'forbidden' } }));
    const again = await loadBrokers();
    await expect(again.fetchBrokerDossier(7)).rejects.toThrow('forbidden');
  });

  /* The whole-corpus outage shape: an unroutable API host (dead Railway service,
     stale VITE_API_BASE_URL) 404s every path. Reading that as "no such broker"
     would render "Makléř nenalezen." for every id with no error anywhere. */
  it('rethrows a 404 that did not come from the broker route itself', async () => {
    stubFetch(() => ({ status: 404, body: { detail: 'Not Found' } }));
    const { fetchBrokerDossier } = await loadBrokers();
    await expect(fetchBrokerDossier(7)).rejects.toThrow('Not Found');
  });

  /* An SPA-fallback HTML page answers 200 with no envelope; spreading it would
     yield a broker-less dossier that also renders as "Makléř nenalezen.". */
  it('throws on a 200 that carries no envelope', async () => {
    stubFetch(() => ({ body: '<!doctype html><title>app</title>' }));
    const { fetchBrokerDossier } = await loadBrokers();
    await expect(fetchBrokerDossier(7)).rejects.toThrow(/malformed/);
  });
});

/* Browse's broker prefilter (MS19): the server judges portal and status over the
 * broker's own ads, and list, count, map and Stats share ONE lookup per view. The
 * memo lives in the module, so every test below uses its own broker id. */
describe('fetchBrokerPropertyIds', () => {
  it("asks for the broker's properties under the portal rule, on the session JWT", async () => {
    const calls = stubFetch(() => ({ body: { data: [5, 6], metadata: { capped: false } } }));
    const b = await loadBrokers();
    expect(await b.fetchBrokerPropertyIds(901, 'inactive', ['remax', 'idnes'])).toEqual([5, 6]);
    expect(calls).toHaveLength(1);
    const url = new URL(calls[0].url);
    expect(url.pathname).toBe('/brokers/901/property-ids');
    expect(url.searchParams.get('status')).toBe('inactive');
    expect(url.searchParams.getAll('portal')).toEqual(['remax', 'idnes']);
    expect(authHeader(calls[0])).toBe('Bearer USER-JWT');
  });

  it('shares one lookup per view, whatever order the portals come in', async () => {
    const calls = stubFetch(() => ({ body: { data: [1] } }));
    const b = await loadBrokers();
    const reads = await Promise.all([
      b.fetchBrokerPropertyIds(902, 'any', ['idnes', 'remax']),
      b.fetchBrokerPropertyIds(902, 'any', ['remax', 'idnes']),
      b.fetchBrokerPropertyIds(902, 'any', ['idnes', 'remax']),
    ]);
    expect(reads).toEqual([[1], [1], [1]]);
    expect(calls).toHaveLength(1);
    await b.fetchBrokerPropertyIds(902, 'active', ['idnes', 'remax']);
    await b.fetchBrokerPropertyIds(902, 'any', ['idnes']);
    expect(calls).toHaveLength(3);
  });

  it('asks again once the minute is up', async () => {
    const now = vi.spyOn(Date, 'now').mockReturnValue(1_000_000);
    const calls = stubFetch(() => ({ body: { data: [] } }));
    const b = await loadBrokers();
    await b.fetchBrokerPropertyIds(903, 'any', []);
    now.mockReturnValue(1_059_000);
    await b.fetchBrokerPropertyIds(903, 'any', []);
    expect(calls).toHaveLength(1);
    now.mockReturnValue(1_061_000);
    await b.fetchBrokerPropertyIds(903, 'any', []);
    expect(calls).toHaveLength(2);
  });

  it('forgets a failed lookup, so the next read retries', async () => {
    let fail = true;
    const calls = stubFetch(() => (fail
      ? { status: 422, body: { detail: 'unknown portal(s): x' } }
      : { body: { data: [3] } }));
    const b = await loadBrokers();
    await expect(b.fetchBrokerPropertyIds(904, 'any', [])).rejects.toThrow();
    const failed = calls.length;
    fail = false;
    expect(await b.fetchBrokerPropertyIds(904, 'any', [])).toEqual([3]);
    expect(calls.length).toBe(failed + 1);
  });

  it('warns when the server capped the allowlist', async () => {
    stubFetch(() => ({ body: { data: [1], metadata: { capped: true } } }));
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const b = await loadBrokers();
    await b.fetchBrokerPropertyIds(905, 'any', []);
    expect(warn).toHaveBeenCalledWith(expect.stringContaining('broker 905'));
  });
});
