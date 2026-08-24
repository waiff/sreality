/* fetchPropertySources' read budget (W9b, migration 420).
 *
 * The function used to open with a resolve hop: `select property_id from
 * property_sources_public where id = <this listing>`. That view is a thin view
 * over `listings` itself (`where property_id is not null`), so the hop re-read
 * the very heap tuple listings_public had already returned — one PostgREST round
 * trip later, to learn one of its own columns. Now listings_public carries
 * property_id, so a caller holding the listing row hands it over and the hop
 * disappears.
 *
 * What is pinned here is the SHAPE, in both directions: one read when the answer
 * is known, two when it is not. The second half matters as much as the first —
 * on the canonical route this function fires in parallel with the listing read
 * (W9a) and has no property_id to be handed, and gating it on one would trade a
 * single hop for a whole waterfall level.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

const h = vi.hoisted(() => ({
  tables: {} as Record<string, Array<Record<string, unknown>>>,
  reads: [] as Array<{ relation: string; eq: Array<[string, unknown]> }>,
}));

vi.mock('./supabase', () => {
  const builder = (relation: string) => {
    const call = { relation, eq: [] as Array<[string, unknown]> };
    h.reads.push(call);
    const rows = () => h.tables[relation] ?? [];
    const b: Record<string, unknown> = {
      select: () => b,
      order: () => b,
      in: () => b,
      eq: (col: string, val: unknown) => {
        call.eq.push([col, val]);
        return b;
      },
      /* The resolve hop ends in .maybeSingle(); the sibling list is a bare
         awaited builder. Both have to be answerable from the same stub or the
         "which read happened" assertion below would be measuring the stub. */
      maybeSingle: () =>
        Promise.resolve({ data: rows()[0] ?? null, error: null }),
      then: (resolve: (r: unknown) => unknown) =>
        resolve({ data: rows(), error: null }),
    };
    return b;
  };
  return { supabase: { from: (relation: string) => builder(relation) } };
});

import { fetchPropertySources } from './queries';

beforeEach(() => {
  h.reads.length = 0;
  h.tables = {
    property_sources_public: [
      { id: 105053, property_id: 774, source: 'idnes', source_id_native: 'abc' },
      { id: 105054, property_id: 774, source: 'sreality', source_id_native: '99' },
    ],
  };
});

describe('fetchPropertySources', () => {
  it('skips the resolve hop when the caller already holds the property_id', async () => {
    const out = await fetchPropertySources(105053, 774);

    expect(h.reads).toHaveLength(1);
    // The one read left is the sibling list, scoped by property_id — never the
    // by-listing-id lookup the hop used to do.
    expect(h.reads[0].eq).toEqual([['property_id', 774]]);
    expect(out.property_id).toBe(774);
    expect(out.sources).toHaveLength(2);
  });

  it('resolves it itself when the caller does not know it', async () => {
    const out = await fetchPropertySources(105053);

    expect(h.reads).toHaveLength(2);
    expect(h.reads[0].eq).toEqual([['id', 105053]]);
    expect(h.reads[1].eq).toEqual([['property_id', 774]]);
    expect(out.property_id).toBe(774);
  });

  /* A NULL property_id is the ~5-min pre-attach window after a scrape (rule
     #19), NOT "this listing has no property" — so it must mean "ask", or a page
     loaded during that window would cache an empty sources list built from a
     stale row rather than re-reading. Only a NUMBER takes the fast path. */
  it('treats a null property_id as unknown, not as an answer', async () => {
    await fetchPropertySources(105053, null);

    expect(h.reads).toHaveLength(2);
    expect(h.reads[0].eq).toEqual([['id', 105053]]);
  });

  it('returns an empty list without a second read when nothing resolves', async () => {
    h.tables.property_sources_public = [];

    const out = await fetchPropertySources(105053);

    expect(h.reads).toHaveLength(1);
    expect(out).toEqual({ property_id: null, sources: [] });
  });
});
