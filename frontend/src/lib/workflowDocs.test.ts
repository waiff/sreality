/* fetchWorkflowDocs — the guard that matters is the content-type one.
 *
 * Caddy's SPA fallback answers ANY unmatched path with index.html and HTTP 200,
 * so a wrong URL or an undeployed file does not 404 — it returns HTML. Checking
 * only `res.ok` would hand that to `res.json()` and surface "Unexpected token
 * '<'" instead of something a reader can act on. */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { fetchWorkflowDocs } from './workflowDocs';

function respond(body: unknown, init: { status?: number; type?: string } = {}) {
  const { status = 200, type = 'application/json' } = init;
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (h: string) => (h.toLowerCase() === 'content-type' ? type : null) },
    json: async () => body,
  } as unknown as Response;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('fetchWorkflowDocs', () => {
  it('returns the workflows array', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(respond({ workflows: [{ filename: 'a.yml' }] })),
    );
    await expect(fetchWorkflowDocs()).resolves.toEqual([{ filename: 'a.yml' }]);
  });

  it('rejects the SPA fallback instead of parsing HTML as JSON', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(respond('<html></html>', { type: 'text/html' })),
    );
    await expect(fetchWorkflowDocs()).rejects.toThrow(/not deployed/);
  });

  it('rejects a non-OK response', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(respond(null, { status: 500 })));
    await expect(fetchWorkflowDocs()).rejects.toThrow(/HTTP 500/);
  });

  it('rejects a payload without a workflows array', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(respond({ nope: true })));
    await expect(fetchWorkflowDocs()).rejects.toThrow(/malformed/);
  });
});
