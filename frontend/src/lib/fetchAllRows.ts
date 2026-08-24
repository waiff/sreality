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
 *
 * EXACT-COUNT FAST PATH (W2b). When the builder requests `count: 'exact'`
 * (PostgREST's `Prefer: count=exact`), page 1's response carries the TRUE row
 * total regardless of any cap. Two things follow, both keyed off whether page
 * 1 came back FULL (`rows.length === pageSize`, i.e. nothing clamped it):
 *   - short first page (`rows.length < pageSize`): either the count IS that
 *     length (done — return now, no terminator needed) or the server clamped
 *     below `pageSize` (the cap-drift case) and count-based page math would
 *     be wrong for the SAME reason a `pageSize + 1` probe is wrong — a lower
 *     cap invalidates any assumption about how many rows a future full-size
 *     window returns. That case (and any call site not requesting a count at
 *     all) falls through to the sequential empty-page walk unchanged.
 *   - full first page with count known: every further page uses the same
 *     request shape that just proved uncapped, so pages 2..ceil(count /
 *     pageSize) are correct issued together — one wave, not a chain — with no
 *     terminating request, because the exact total already says when to stop.
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
  ): PromiseLike<{
    data: Row[] | null;
    error: { message: string } | null;
    /* Present only when the builder requested `count: 'exact'`; drives the
     * fast path above. Absent (or null) callers get the plain sequential walk. */
    count?: number | null;
  }>;
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

  const orderedBuild = (): PageBuilder<Row> => {
    let page = build();
    for (const o of orderBy) {
      page = page.order(o.column, { ascending: o.ascending ?? true });
    }
    return page;
  };

  const absorb = (rows: readonly Row[]): void => {
    for (const row of rows) {
      const id = JSON.stringify(key.map((k) => (row as Record<string, unknown>)[k]));
      if (seen.has(id)) continue;
      seen.add(id);
      out.push(row);
    }
    if (out.length > expectMax) throw new FetchAllOverflowError(relation, expectMax);
  };

  const first = await orderedBuild().range(0, pageSize - 1);
  if (first.error) throw first.error;
  const firstRows = first.data ?? [];
  if (firstRows.length === 0) return out;

  const count = first.count ?? null;
  if (count != null && count > expectMax) {
    throw new FetchAllOverflowError(relation, expectMax);
  }
  absorb(firstRows);

  if (count != null) {
    if (firstRows.length === count) {
      /* Exact-count termination: page 1 already holds everything, so the
       * terminating empty-page request the fallback below would otherwise
       * spend is skipped entirely. */
      return out;
    }
    if (firstRows.length === pageSize) {
      /* Page 1 came back full — nothing clamped it — so every remaining
       * full-size window is safe to request up front, together. */
      const totalPages = Math.ceil(count / pageSize);
      const rest = await Promise.all(
        Array.from({ length: totalPages - 1 }, (_, i) => i + 1).map((i) =>
          orderedBuild().range(i * pageSize, (i + 1) * pageSize - 1),
        ),
      );
      for (const page of rest) {
        if (page.error) throw page.error;
        absorb(page.data ?? []);
      }
      return out;
    }
    /* Short first page but count says there's more: db-max-rows clamped
     * below pageSize (the cap-drift case). Count-based page math would be
     * wrong here for the same reason a pageSize+1 probe would be — a lower
     * cap invalidates any assumption about a future window's size. Fall
     * through to the sequential walk, which is correct under any cap. */
  }

  /* Sequential empty-page walk — the fallback for a builder that didn't
   * request an exact count, or a count that came back below db-max-rows. */
  let offset = firstRows.length;
  for (;;) {
    const { data, error } = await orderedBuild().range(offset, offset + pageSize - 1);
    if (error) throw error;
    const rows = data ?? [];
    if (rows.length === 0) return out;
    absorb(rows);
    /* Advance by what the SERVER sent, not what we asked for — under a cap
     * smaller than pageSize this is what keeps the walk gap-free. */
    offset += rows.length;
  }
}
