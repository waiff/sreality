/* THE PostgREST read: every supabase-js read in the SPA is awaited here.
 *
 * The SPA has two adapters to two servers. lib/api.ts `send()` owns the FastAPI
 * half (one deadline, one abort rule, one error class); this is the same seam
 * for the supabase-js half. Before it, 51 call sites each did
 * `const { data, error } = await …; if (error) throw error` — no deadline (a
 * stalled read spun forever), no React Query signal (an unmounted query kept
 * its request running), a PostgrestError the retry rule could not classify
 * (main.tsx retried no supabase read, not even a network blip), and
 * postgrest-js's own hidden GET retry stacked under all of it.
 *
 * What this owns:
 * - ONE deadline per request (PG_DEADLINE_MS, or `deadlineMs` for a read that
 *   has a cheaper fallback), combined with the caller's signal.
 * - ONE abort rule: a caller abort rethrows `signal.reason`, so it stays a
 *   cancellation and never becomes an error banner.
 * - ONE error class: every failure is an ApiError with the wire status, so
 *   `isTransientApiError` (lib/api.ts) is the one retry rule for both adapters.
 * - ONE retry rule: the library's own retry is switched off per builder. It
 *   re-sent a GET/HEAD up to 3× (1 s / 2 s / 4 s, honouring Retry-After) on a
 *   503, a 520 or a network error; main.tsx now sends ONE retry after 1 s for
 *   the same cases, as it does for the FastAPI half. Fewer attempts on a
 *   longer outage is the accepted price of one rule the app can see.
 *
 * Tolerant reads (a hint, the agenda gate) handle the rejection at their call
 * site; there is no result mode here. */
import type { PostgrestSingleResponse } from '@supabase/supabase-js';
import { ApiError } from './api';

/* `authenticated` reads are cancelled server-side at 8 s (statement_timeout,
 * migrations 425/413/503; lock_timeout is 8 s too) and PostgREST answers its own
 * pool wait. Past 20 s nothing server-side is still producing the answer — the
 * transport stalled — so the read fails here, once. It also covers streaming
 * the largest body (the 50k-row map, ~22 MB). Same rule as REQUEST_DEADLINE_MS
 * in lib/api.ts, sized for the other server. */
export const PG_DEADLINE_MS = 20_000;

/* Busy, not wrong: classifying these 503 keeps isTransientApiError the ONE
 * rule. The FastAPI adapter (api/main.py `_DB_BUSY_ERRORS`) also answers 57014
 * as a retried 503; here it deliberately stays `kind: 'timeout'`, never
 * retried (as before this seam), because the Browse list banners key on its
 * verbatim message to say "too slow", not "busy". Do not add it.
 *
 * A HEAD read (every `head: true` count) gets its error with no body, so no
 * SQLSTATE reaches this file: a busy or cancelled count is a plain 500. */
const DB_BUSY = new Set(['55P03', '40P01']);

/* Every PostgREST builder: .from()…, .rpc()…, and .maybeSingle()/.single(). */
export interface PgBuilder extends PromiseLike<PostgrestSingleResponse<unknown>> {
  retry(enabled: boolean): unknown;
}

export interface PgReadOptions {
  signal?: AbortSignal;
  deadlineMs?: number;
}

export interface PgResult<T> {
  data: T;
  count: number | null;
}

export async function pgRead<T = unknown>(
  builder: PgBuilder,
  { signal, deadlineMs = PG_DEADLINE_MS }: PgReadOptions = {},
): Promise<PgResult<T>> {
  if (signal?.aborted) throw signal.reason;
  const deadline = AbortSignal.timeout(deadlineMs);
  const stop = signal ? AbortSignal.any([signal, deadline]) : deadline;
  builder.retry(false);
  /* maybeSingle()/single() return `this` at runtime (postgrest-js); only their
   * declared type drops abortSignal. */
  (builder as PgBuilder & { abortSignal(s: AbortSignal): unknown }).abortSignal(stop);
  /* The race bounds the read even where no signal reaches: supabase-js awaits
   * auth.getSession() before every fetch. It resolves rather than rejects, so a
   * deadline firing after success is never an unhandled rejection. */
  const res = await Promise.race([
    builder,
    new Promise<null>((resolve) => {
      stop.addEventListener('abort', () => resolve(null), { once: true });
    }),
  ]);
  if (res !== null && res.error == null) {
    return { data: res.data as T, count: res.count ?? null };
  }
  /* On the non-throw path postgrest-js turns every abort into a resolved
   * `status: 0` envelope, so abort / deadline / network are told apart by our
   * own two signals, never by the message text. */
  if (res === null || (res.status === 0 && stop.aborted)) {
    if (signal?.aborted) throw signal.reason;
    /* Deliberately not "statement timeout": that is the server's fact, and the
     * Browse banners match it to say something different. */
    throw new ApiError(`Databáze neodpověděla do ${deadlineMs / 1000} s`, 0, null, 'timeout');
  }
  const { error, status, statusText } = res;
  if (status === 0) throw new ApiError(error.message, 0, error, 'network');
  const message = error.message || statusText || `HTTP ${status}`;
  /* The server's statement_timeout. The message stays verbatim — the Browse
   * banners read it. */
  if (error.code === '57014') throw new ApiError(message, status, error, 'timeout');
  throw new ApiError(message, DB_BUSY.has(error.code) ? 503 : status, error, 'http');
}
