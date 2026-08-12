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
