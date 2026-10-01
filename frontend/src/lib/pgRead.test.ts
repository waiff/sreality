/* pgRead — the one PostgREST read.
 *
 * Driven through the REAL supabase-js client against a stubbed global fetch, so
 * what is pinned is what postgrest-js actually hands the seam (its non-throw
 * envelope, its status-0 abort/network shape, its client-side maybeSingle), not
 * a hand-written builder's idea of it. Global fetch is the one seam both
 * adapters cross; lib/api.test.ts drives the other one the same way. */

import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError, isTransientApiError } from './api';
import { pgRead } from './pgRead';
import { supabase } from './supabase';

const reply = (status: number, body?: unknown, headers: Record<string, string> = {}) =>
  ({
    ok: status < 300,
    status,
    statusText: '',
    headers: new Headers(headers),
    text: async () => (body === undefined ? '' : JSON.stringify(body)),
  }) as unknown as Response;

const stubFetch = (impl: (url: URL, init: RequestInit) => Promise<Response>) => {
  const f = vi.fn((i: RequestInfo | URL, init?: RequestInit) => impl(new URL(String(i)), init ?? {}));
  vi.stubGlobal('fetch', f);
  return f;
};

/* A server that never answers: the request settles only when its signal aborts. */
const hang = (_: URL, init: RequestInit) =>
  new Promise<Response>((_resolve, reject) => {
    init.signal!.addEventListener('abort', () => reject(init.signal!.reason));
  });

const pgError = (status: number, code: string, message: string) => () =>
  Promise.resolve(reply(status, { code, message, details: null, hint: null }));

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('pgRead — answers', () => {
  it('returns the rows, and hands the request a signal', async () => {
    const f = stubFetch(async () => reply(200, [{ id: 1 }]));
    const out = await pgRead<{ id: number }[]>(supabase.from('t').select('id'));
    expect(out).toEqual({ data: [{ id: 1 }], count: null });
    expect(f).toHaveBeenCalledTimes(1);
    expect(f.mock.calls[0][1]?.signal).toBeDefined();
  });

  it('reads a head count off Content-Range', async () => {
    stubFetch(async () => reply(200, undefined, { 'Content-Range': '*/42' }));
    const out = await pgRead(supabase.from('t').select('*', { count: 'exact', head: true }));
    expect(out).toEqual({ data: null, count: 42 });
  });

  it('keeps maybeSingle: none is null, one is the row, two is an error', async () => {
    stubFetch(async () => reply(200, []));
    expect((await pgRead(supabase.from('t').select('id').maybeSingle())).data).toBeNull();

    stubFetch(async () => reply(200, [{ id: 7 }]));
    expect((await pgRead(supabase.from('t').select('id').maybeSingle())).data).toEqual({ id: 7 });

    stubFetch(async () => reply(200, [{ id: 7 }, { id: 8 }]));
    await expect(pgRead(supabase.from('t').select('id').maybeSingle())).rejects.toMatchObject({
      name: 'ApiError',
      kind: 'http',
      status: 406,
      body: { code: 'PGRST116' },
    });
  });
});

describe('pgRead — failures are ApiErrors the one retry rule can read', () => {
  it("classifies the server's statement timeout as a timeout and keeps its message verbatim", async () => {
    stubFetch(pgError(500, '57014', 'canceling statement due to statement timeout'));
    const err = await pgRead(supabase.from('t').select('id')).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({
      kind: 'timeout',
      status: 500,
      message: 'canceling statement due to statement timeout',
    });
    expect(isTransientApiError(err)).toBe(false);
  });

  it.each(['55P03', '40P01'])('answers a busy database (%s) as a transient 503', async (code) => {
    stubFetch(pgError(500, code, 'busy'));
    const err = await pgRead(supabase.from('t').select('id')).catch((e: unknown) => e);
    expect(err).toMatchObject({ name: 'ApiError', kind: 'http', status: 503 });
    expect(isTransientApiError(err)).toBe(true);
  });

  it('throws a revoked grant, never resolving it to empty data', async () => {
    stubFetch(pgError(403, '42501', 'permission denied for view t'));
    await expect(pgRead(supabase.from('t').select('id'))).rejects.toMatchObject({
      name: 'ApiError',
      kind: 'http',
      status: 403,
      message: 'permission denied for view t',
    });
  });

  it("switches the library's own retry off, so main.tsx's rule is the only one", async () => {
    const f = stubFetch(pgError(503, 'PGRST002', 'Could not query the database for the schema cache'));
    const err = await pgRead(supabase.from('t').select('id')).catch((e: unknown) => e);
    expect(isTransientApiError(err)).toBe(true);
    expect(f).toHaveBeenCalledTimes(1);
  });

  /* Supabase's REST endpoint sits behind Cloudflare; postgrest-js retried its
   * 520 on its own, so with that retry off the one rule has to take it over.
   * The body is Cloudflare's HTML, so there is no PostgREST code to read. */
  it("treats Cloudflare's 520 as transient, sent once", async () => {
    const f = stubFetch(async () =>
      ({
        ok: false,
        status: 520,
        statusText: '',
        headers: new Headers(),
        text: async () => '<html><body>error code: 520</body></html>',
      }) as unknown as Response,
    );
    const err = await pgRead(supabase.from('t').select('id')).catch((e: unknown) => e);
    expect(err).toMatchObject({ name: 'ApiError', kind: 'http', status: 520 });
    expect(isTransientApiError(err)).toBe(true);
    expect(f).toHaveBeenCalledTimes(1);
  });

  it('classifies a failed fetch as a transient network error, sent once', async () => {
    const f = stubFetch(() => Promise.reject(new TypeError('Failed to fetch')));
    const err = await pgRead(supabase.from('t').select('id')).catch((e: unknown) => e);
    expect(err).toMatchObject({ name: 'ApiError', kind: 'network', status: 0 });
    expect(isTransientApiError(err)).toBe(true);
    expect(f).toHaveBeenCalledTimes(1);
  });
});

describe('pgRead — deadline and abort', () => {
  it('fails a stalled read at its deadline, not phrased as a statement timeout', async () => {
    stubFetch(hang);
    const err = await pgRead(supabase.from('t').select('id'), { deadlineMs: 20 }).catch(
      (e: unknown) => e,
    );
    expect(err).toMatchObject({ name: 'ApiError', kind: 'timeout', status: 0 });
    expect((err as Error).message).not.toMatch(/statement timeout|57014/i);
    expect(isTransientApiError(err)).toBe(false);
  });

  it('bounds the token acquisition that runs before fetch, too', async () => {
    vi.spyOn(supabase.auth, 'getSession').mockReturnValue(new Promise(() => {}));
    const f = stubFetch(async () => reply(200, []));
    await expect(
      pgRead(supabase.from('t').select('id'), { deadlineMs: 20 }),
    ).rejects.toMatchObject({ name: 'ApiError', kind: 'timeout' });
    expect(f).not.toHaveBeenCalled();
  });

  it('rethrows a caller abort as a cancellation, not an ApiError', async () => {
    const f = stubFetch(hang);
    const ctrl = new AbortController();
    const p = pgRead(supabase.from('t').select('id'), { signal: ctrl.signal });
    await vi.waitFor(() => expect(f).toHaveBeenCalled());
    ctrl.abort();
    const err = await p.catch((e: unknown) => e);
    expect(err).not.toBeInstanceOf(ApiError);
    expect((err as Error).name).toBe('AbortError');
  });

  it("combines the caller's signal with its own deadline on the request", async () => {
    const f = stubFetch(hang);
    const ctrl = new AbortController();
    const p = pgRead(supabase.from('t').select('id'), { signal: ctrl.signal }).catch(() => {});
    await vi.waitFor(() => expect(f).toHaveBeenCalled());
    const sent = f.mock.calls[0][1]!.signal!;
    expect(sent).not.toBe(ctrl.signal);
    expect(sent.aborted).toBe(false);
    ctrl.abort();
    expect(sent.aborted).toBe(true);
    await p;
  });

  it('never sends a read whose caller already gave up', async () => {
    const f = stubFetch(async () => reply(200, []));
    const ctrl = new AbortController();
    ctrl.abort();
    await expect(
      pgRead(supabase.from('t').select('id'), { signal: ctrl.signal }),
    ).rejects.toMatchObject({ name: 'AbortError' });
    expect(f).not.toHaveBeenCalled();
  });
});
