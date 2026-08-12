/* fetchAllRows — the complete-or-throw exhaustive read.
 *
 * The fake builder serves a fixed row set through real range() windows, with a
 * configurable server-side clamp — the situations that matter are exactly the
 * ones a live Supabase makes hard to reproduce: a cap SMALLER than the page
 * size (the failure that motivated empty-page termination), a result set that
 * is an exact multiple of the page size, and a concurrent insert shifting page
 * boundaries mid-walk.
 */

import { describe, expect, it } from 'vitest';

import {
  FetchAllOverflowError,
  fetchAllRows,
  type OrderSpec,
  type PageBuilder,
} from './fetchAllRows';

interface Row extends Record<string, unknown> {
  id: number;
  v: string;
}

const makeRows = (n: number): Row[] =>
  Array.from({ length: n }, (_, i) => ({ id: i + 1, v: `r${i + 1}` }));

/* A server: rows live here; each build() hands out a fresh single-use builder
 * that records the orders applied and serves range() windows off the CURRENT
 * array (so tests can mutate it between pages), clamped like db-max-rows. */
function fakeServer(rows: Row[], { clamp = Infinity } = {}) {
  const orderCalls: OrderSpec[][] = [];
  const rangeCalls: Array<[number, number]> = [];
  const build = (): PageBuilder<Row> => {
    const applied: OrderSpec[] = [];
    orderCalls.push(applied);
    const builder: PageBuilder<Row> = {
      order(column, opts) {
        applied.push({ column, ascending: opts?.ascending });
        return builder;
      },
      range(from, to) {
        rangeCalls.push([from, to]);
        const asked = to - from + 1;
        const data = rows.slice(from, from + Math.min(asked, clamp));
        return Promise.resolve({ data, error: null });
      },
    };
    return builder;
  };
  return { build, orderCalls, rangeCalls };
}

const opts = { relation: 'test', orderBy: [{ column: 'id' }], key: ['id'] as const };

describe('fetchAllRows', () => {
  it('assembles multiple pages in order and applies the declared sort to every page', async () => {
    const srv = fakeServer(makeRows(2500));
    const out = await fetchAllRows<Row>({ ...opts, build: srv.build, expectMax: 10_000 });
    expect(out).toHaveLength(2500);
    expect(out[0].id).toBe(1);
    expect(out[2499].id).toBe(2500);
    // 2 full pages, a 500-row tail (offset then advances by rows RECEIVED,
    // so the empty terminator probes from 2500), each freshly ordered.
    expect(srv.rangeCalls).toEqual([[0, 999], [1000, 1999], [2000, 2999], [2500, 3499]]);
    expect(srv.orderCalls.every((c) => c.length === 1 && c[0].column === 'id')).toBe(true);
  });

  it('terminates on the empty page when the set is an exact page multiple', async () => {
    const srv = fakeServer(makeRows(2000));
    const out = await fetchAllRows<Row>({ ...opts, build: srv.build, expectMax: 10_000 });
    expect(out).toHaveLength(2000);
    expect(srv.rangeCalls).toHaveLength(3); // 2 full + 1 empty
  });

  it('stays complete when the server clamps below the page size (the cap-drift case)', async () => {
    // db-max-rows=700 vs pageSize=1000: every page comes back short-but-full.
    // Short-page termination would stop at 700 rows and call it complete.
    const srv = fakeServer(makeRows(2400), { clamp: 700 });
    const out = await fetchAllRows<Row>({ ...opts, build: srv.build, expectMax: 10_000 });
    expect(out).toHaveLength(2400);
    expect(out.map((r) => r.id)).toEqual(makeRows(2400).map((r) => r.id));
  });

  it('dedupes a row re-served after a concurrent insert shifts page boundaries', async () => {
    const rows = makeRows(1500);
    const srv = fakeServer(rows);
    let intercepted = false;
    const build = () => {
      // After page 1 is served, a new row lands at the FRONT of the order —
      // page 2's window now re-serves the old row 1000.
      if (!intercepted && srv.rangeCalls.length === 1) {
        intercepted = true;
        rows.unshift({ id: 0, v: 'r0' });
      }
      return srv.build();
    };
    const out = await fetchAllRows<Row>({ ...opts, build, expectMax: 10_000 });
    const ids = out.map((r) => r.id);
    expect(new Set(ids).size).toBe(ids.length); // no duplicates survived
    expect(ids).toContain(1500); // and the tail was still reached
  });

  it('throws FetchAllOverflowError instead of returning a partial set', async () => {
    const srv = fakeServer(makeRows(3000));
    await expect(
      fetchAllRows<Row>({ ...opts, build: srv.build, expectMax: 2500 }),
    ).rejects.toBeInstanceOf(FetchAllOverflowError);
  });

  it('rejects an orderBy that does not cover the key (non-total sort)', async () => {
    const srv = fakeServer(makeRows(5));
    await expect(
      fetchAllRows<Row>({
        relation: 'test',
        build: srv.build,
        orderBy: [{ column: 'v' }],
        key: ['id'],
        expectMax: 100,
      }),
    ).rejects.toThrow(/missing from orderBy/);
  });

  it('propagates a page error verbatim', async () => {
    const build = (): PageBuilder<Row> => {
      const b: PageBuilder<Row> = {
        order: () => b,
        range: () => Promise.resolve({ data: null, error: { message: 'boom' } }),
      };
      return b;
    };
    await expect(
      fetchAllRows<Row>({ ...opts, build, expectMax: 100 }),
    ).rejects.toEqual({ message: 'boom' });
  });
});
