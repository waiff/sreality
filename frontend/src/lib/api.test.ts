/* Query-param serialization + the auth mode in lib/api's request(). Imported
 * dynamically because BASE_URL and TOKEN are read from import.meta.env at
 * module-evaluation time. Every /brokers call passes `jwt: true` — those routes
 * are verify_jwt-gated and 401 on the static token. */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { supabase } from './supabase';

async function loadApi() {
  vi.stubEnv('VITE_API_BASE_URL', 'https://api.test.invalid');
  vi.stubEnv('VITE_API_TOKEN', 'STATIC-BUNDLE-TOKEN');
  return import('./api');
}

function captureFetch(): { urls: string[]; headers: Record<string, string>[] } {
  const urls: string[] = [];
  const headers: Record<string, string>[] = [];
  vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
    urls.push(String(input));
    headers.push((init?.headers ?? {}) as Record<string, string>);
    return { ok: true, status: 200, statusText: 'OK', text: async () => '{}' } as Response;
  });
  return { urls, headers };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe('apiGet query params', () => {
  it('serializes an array as repeated params, not a comma join', async () => {
    const { urls } = captureFetch();
    const { apiGet } = await loadApi();
    await apiGet('/brokers', { ids: [1, 2, 3] }, undefined, true);
    expect(new URL(urls[0]).search).toBe('?ids=1&ids=2&ids=3');
  });

  it('omits an empty array and nullish values entirely', async () => {
    const { urls } = captureFetch();
    const { apiGet } = await loadApi();
    await apiGet('/brokers', { ids: [], q: undefined, limit: 5 }, undefined, true);
    expect(new URL(urls[0]).search).toBe('?limit=5');
  });

  it('drops an empty-string value instead of emitting a meaningless param', async () => {
    /* THE REGRESSION. `[null].join(',')` is '' in JS, which used to be sent as
     * `?listing_ids=`. The API read the empty value as "no filter" and answered
     * with the entire estimation_runs table. An empty string is not a filter. */
    const { urls } = captureFetch();
    const { apiGet } = await loadApi();
    await apiGet('/estimations', { listing_ids: '', limit: 5 }, undefined, true);
    expect(new URL(urls[0]).search).toBe('?limit=5');
  });

  it('still sets a scalar once', async () => {
    const { urls } = captureFetch();
    const { apiGet } = await loadApi();
    await apiGet('/brokers/search', { q: 'alfa', limit: 12 }, undefined, true);
    expect(new URL(urls[0]).search).toBe('?q=alfa&limit=12');
  });
});

describe('apiGet auth mode', () => {
  it('sends the caller session JWT when jwt: true — what /brokers/* requires', async () => {
    vi.spyOn(supabase.auth, 'getSession').mockResolvedValue({
      data: { session: { access_token: 'USER-JWT' } },
      error: null,
    } as unknown as Awaited<ReturnType<typeof supabase.auth.getSession>>);
    const { headers } = captureFetch();
    const { apiGet } = await loadApi();
    await apiGet('/brokers/leaderboard', { limit: 5 }, undefined, true);
    expect(headers[0].Authorization).toBe('Bearer USER-JWT');
  });

  it('falls back to the static bundle token when jwt is omitted — a 401 on /brokers/*', async () => {
    const { headers } = captureFetch();
    const { apiGet } = await loadApi();
    await apiGet('/brokers/leaderboard', { limit: 5 });
    expect(headers[0].Authorization).toBe('Bearer STATIC-BUNDLE-TOKEN');
  });
});

describe('estimation subject identity', () => {
  it('fetches property-grain runs by SURROGATE listing ids, not sreality ids', async () => {
    /* listings.sreality_id is NULL for every non-sreality listing (migration
     * 311's sign check), so keying this fetch on it silently dropped those
     * subjects — and an all-null id array collapsed to an unfiltered request. */
    const { urls } = captureFetch();
    const { fetchEstimationsForListings } = await import('./queries');
    await fetchEstimationsForListings([501, 502]);
    const search = new URL(urls[0]).search;
    expect(search).toContain('listing_ids=501%2C502');
    expect(search).not.toContain('sreality_ids');
  });

  it('sends the caller JWT so account scoping resolves the operator, not SYSTEM', async () => {
    vi.spyOn(supabase.auth, 'getSession').mockResolvedValue({
      data: { session: { access_token: 'USER-JWT' } },
      error: null,
    } as unknown as Awaited<ReturnType<typeof supabase.auth.getSession>>);
    const { headers } = captureFetch();
    const { fetchEstimationsForListings } = await import('./queries');
    await fetchEstimationsForListings([501]);
    expect(headers[0].Authorization).toBe('Bearer USER-JWT');
  });
});
