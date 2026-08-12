/* Query-param serialization in lib/api's request(). Imported dynamically because
 * BASE_URL is read from import.meta.env at module-evaluation time. */

import { afterEach, describe, expect, it, vi } from 'vitest';

async function loadApi() {
  vi.stubEnv('VITE_API_BASE_URL', 'https://api.test.invalid');
  return import('./api');
}

function captureFetch(): string[] {
  const urls: string[] = [];
  vi.stubGlobal('fetch', async (input: RequestInfo | URL) => {
    urls.push(String(input));
    return { ok: true, status: 200, statusText: 'OK', text: async () => '{}' } as Response;
  });
  return urls;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe('apiGet query params', () => {
  it('serializes an array as repeated params, not a comma join', async () => {
    const urls = captureFetch();
    const { apiGet } = await loadApi();
    await apiGet('/brokers', { ids: [1, 2, 3] });
    expect(new URL(urls[0]).search).toBe('?ids=1&ids=2&ids=3');
  });

  it('omits an empty array and nullish values entirely', async () => {
    const urls = captureFetch();
    const { apiGet } = await loadApi();
    await apiGet('/brokers', { ids: [], q: undefined, limit: 5 });
    expect(new URL(urls[0]).search).toBe('?limit=5');
  });

  it('still sets a scalar once', async () => {
    const urls = captureFetch();
    const { apiGet } = await loadApi();
    await apiGet('/brokers/search', { q: 'alfa', limit: 12 });
    expect(new URL(urls[0]).search).toBe('?q=alfa&limit=12');
  });
});
