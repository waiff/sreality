/* The repointed broker read layer (lib/brokers.ts).
 *
 * These functions moved off supabase-js onto the identity-gated /brokers API on
 * 2026-08-12. Three things are worth pinning, because each has a silent-failure
 * mode: (1) EVERY call must carry the caller's real session JWT — the routes
 * reject the static bundle token, and a missed `jwt: true` reads as "no broker"
 * rather than as an error; (2) a 404 (unattributed listing / unknown broker) is
 * an ANSWER, everything else must propagate; (3) contact PII arrives as
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
    await b.fetchListingBroker(1);
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
    expect(calls).toHaveLength(6);
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

describe('fetchListingBroker', () => {
  it('keys on the surrogate listing_id, not sreality_id', async () => {
    const calls = stubFetch(() => ({
      body: { data: { listing_id: 55, broker_id: 7 } },
    }));
    const { fetchListingBroker } = await loadBrokers();
    const row = await fetchListingBroker(55);
    const url = new URL(calls[0].url);
    expect(url.pathname).toBe('/brokers/by-listing');
    expect(url.searchParams.get('listing_id')).toBe('55');
    expect(url.searchParams.has('sreality_id')).toBe(false);
    expect(row?.broker_id).toBe(7);
  });

  it('treats 404 as "unattributed", not as a failure', async () => {
    stubFetch(() => ({ status: 404, body: { detail: 'listing has no attributed broker' } }));
    const { fetchListingBroker } = await loadBrokers();
    await expect(fetchListingBroker(55)).resolves.toBeNull();
  });

  it('propagates a real fault instead of degrading to null', async () => {
    stubFetch(() => ({ status: 500, body: { detail: 'boom' } }));
    const { fetchListingBroker } = await loadBrokers();
    await expect(fetchListingBroker(55)).rejects.toThrow('boom');
  });

  /* A 404 that did NOT come from the route saying "nothing attributed" —
     Railway's edge on an unrouted host, a stale VITE_API_BASE_URL, a renamed
     path — must not read as "this listing has no broker", or the whole corpus
     goes silently dark again exactly as it did under PostgREST. */
  it('propagates a routing 404 instead of swallowing it as "unattributed"', async () => {
    stubFetch(() => ({ status: 404, body: { detail: 'Not Found' } }));
    const { fetchListingBroker } = await loadBrokers();
    await expect(fetchListingBroker(55)).rejects.toThrow('Not Found');
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
