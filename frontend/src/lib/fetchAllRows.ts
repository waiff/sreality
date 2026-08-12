/* THE exhaustive PostgREST read: fetch a COMPLETE result set or throw.
 *
 * Some reads' entire meaning is "the whole set" — membership maps, prefilter
 * id-lists, choropleths, curated registries. For those, a partial answer is not
 * a smaller answer, it is a WRONG answer (a filter that quietly drops matches,
 * a map that quietly loses pins). This helper is the one place that contract is
 * implemented; every fetch-all site goes through it (an ESLint rule bans
 * `.range()` elsewhere so the pattern can't be reintroduced by copy-paste).
 *
 * Why it exists — the cap-drift incident (2026-08). PostgREST clamps every
 * response to the server-side `db-max-rows`; `.range(0, 99999)` asks for a
 * window, it does NOT lift the clamp. This project ran with the Supabase
 * default of 1,000 long enough to ship two real truncation bugs (the city-index
 * "~32 cities" popup, the rent-map choropleth), then the cap was LIFTED
 * out-of-band in the dashboard (the 50k-point map needs single big reads),
 * which silently stranded two generations of wrong comments: ".range bypasses
 * the cap" (never true — there was just no cap left to hit) and "hard-capped at
 * 1,000, paging is mandatory" (true when written, stale after the lift).
 * Migration 394 re-pins the cap in git (50,000 = MAP_CAP); this helper is
 * deliberately correct under ANY value — absent, lowered, raised — so the two
 * layers never have to move together.
 *
 * How that independence is achieved:
 * - Pages advance by rows RECEIVED and stop only on an EMPTY page. Never by
 *   "short page < requested size": if a future cap sat below the page size,
 *   every page would come back short-but-full and short-page detection would
 *   conclude "done" mid-set — the exact silent truncation this file exists to
 *   kill. The empty tail page costs one extra ~tens-of-ms request on reads
 *   cached for 30s+; correctness buys it.
 * - `orderBy` must cover `key`, a VERIFIED-unique column tuple (checked against
 *   live data per call site when introduced — see the PR audit). Offset paging
 *   without a total order duplicates/skips rows at page boundaries whenever two
 *   pages disagree about order within ties.
 * - Rows are deduped on the `key` tuple: a row INSERTED mid-pagination shifts
 *   later pages and can re-serve an already-seen row; the dedupe makes that
 *   harmless. (A concurrent DELETE can still cause a skip — acceptable at the
 *   operator/reference grain these reads serve, where writes are rare and the
 *   next 30s refetch reconciles; noted rather than solved.)
 * - `expectMax` is a LOUD ceiling: exceeding it throws FetchAllOverflowError
 *   (surfacing through the app's global query/mutation error toasts) instead of
 *   returning "some". Pick values that mean "something is structurally wrong",
 *   not "slightly more than today".
 */

export class FetchAllOverflowError extends Error {
  constructor(relation: string, expectMax: number) {
    super(
      `fetchAllRows(${relation}): more than ${expectMax} rows — refusing to return a partial set. `
      + 'Raise expectMax if this growth is legitimate, or move the read server-side.',
    );
    this.name = 'FetchAllOverflowError';
  }
}

export interface OrderSpec {
  column: string;
  ascending?: boolean;
}

/* The slice of a PostgREST builder this helper drives. Both `.from().select()`
 * and `.rpc()` builders satisfy it structurally. A FRESH builder is built per
 * page — PostgREST builders are single-use. */
export interface PageBuilder<Row> {
  order(column: string, opts?: { ascending?: boolean }): PageBuilder<Row>;
  range(
    from: number,
    to: number,
  ): PromiseLike<{ data: Row[] | null; error: { message: string } | null }>;
}

export interface FetchAllOptions<Row> {
  /* Display name for error messages (usually the relation / RPC name). */
  relation: string;
  build: () => PageBuilder<Row>;
  /* Applied in order; MUST make the sort total (i.e. include every `key`
   * column) — validated here, at call time, so a non-total order is a loud
   * programmer error instead of a rare page-boundary data bug. */
  orderBy: OrderSpec[];
  /* Verified-unique identity tuple; the dedupe + order-totality anchor. */
  key: readonly string[];
  /* Loud ceiling — throw rather than return more than this many rows. */
  expectMax: number;
  /* Rows requested per page. The server may return fewer under a lower
   * db-max-rows; the loop is correct either way. */
  pageSize?: number;
}

export async function fetchAllRows<Row extends object>({
  relation,
  build,
  orderBy,
  key,
  expectMax,
  pageSize = 1000,
}: FetchAllOptions<Row>): Promise<Row[]> {
  const ordered = new Set(orderBy.map((o) => o.column));
  for (const k of key) {
    if (!ordered.has(k)) {
      throw new Error(
        `fetchAllRows(${relation}): key column "${k}" missing from orderBy — the sort would not be total`,
      );
    }
  }

  const out: Row[] = [];
  const seen = new Set<string>();
  let offset = 0;

  for (;;) {
    let page = build();
    for (const o of orderBy) {
      page = page.order(o.column, { ascending: o.ascending ?? true });
    }
    const { data, error } = await page.range(offset, offset + pageSize - 1);
    if (error) throw error;
    const rows = data ?? [];
    if (rows.length === 0) return out;

    for (const row of rows) {
      const id = JSON.stringify(key.map((k) => (row as Record<string, unknown>)[k]));
      if (seen.has(id)) continue;
      seen.add(id);
      out.push(row);
    }
    if (out.length > expectMax) throw new FetchAllOverflowError(relation, expectMax);
    /* Advance by what the SERVER sent, not what we asked for — under a cap
     * smaller than pageSize this is what keeps the walk gap-free. */
    offset += rows.length;
  }
}
