/* The property page's reads: one read each, keyed on what the URL carries.
 *
 * The page is keyed on the PROPERTY, so its advert list is one read by
 * property_id — the listing → property resolve hop the advert page needed is
 * gone. An old advert address resolves its property in one read too, from
 * whichever identity it carries. */

import { beforeEach, describe, expect, it, vi } from 'vitest';

const h = vi.hoisted(() => ({
  tables: {} as Record<string, Array<Record<string, unknown>>>,
  reads: [] as Array<{ relation: string; select: string; eq: Array<[string, unknown]> }>,
}));

vi.mock('./supabase', () => {
  const builder = (relation: string) => {
    const call = { relation, select: '', eq: [] as Array<[string, unknown]> };
    h.reads.push(call);
    const rows = () => h.tables[relation] ?? [];
    const b: Record<string, unknown> = {
      select: (cols: string) => {
        call.select = cols;
        return b;
      },
      order: () => b,
      eq: (col: string, val: unknown) => {
        call.eq.push([col, val]);
        return b;
      },
      maybeSingle: () => Promise.resolve({ data: rows()[0] ?? null, error: null }),
      then: (resolve: (r: unknown) => unknown) => resolve({ data: rows(), error: null }),
    };
    return b;
  };
  return { supabase: { from: (relation: string) => builder(relation) } };
});

import { fetchAdvertProperty, fetchProperty, fetchPropertySources } from './queries';

beforeEach(() => {
  h.reads.length = 0;
  h.tables = {
    property_sources_public: [
      { id: 105053, property_id: 774, source: 'idnes', source_id_native: 'abc' },
      { id: 105054, property_id: 774, source: 'sreality', source_id_native: '99' },
    ],
    listings_public: [{ id: 105054, property_id: 774 }],
    properties_public: [
      { id: 105054, property_id: 774, price_czk: 5_000_000, total_price_change_pct: '-4.5' },
    ],
  };
});

describe('fetchPropertySources', () => {
  it('is one read, scoped by the property', async () => {
    const out = await fetchPropertySources(774);
    expect(h.reads).toHaveLength(1);
    expect(h.reads[0].eq).toEqual([['property_id', 774]]);
    expect(out).toHaveLength(2);
  });
});

describe('fetchAdvertProperty', () => {
  it('resolves a natural key in one read', async () => {
    expect(await fetchAdvertProperty({ source: 'sreality', nativeId: '99' })).toEqual({
      id: 105054,
      property_id: 774,
    });
    expect(h.reads).toHaveLength(1);
    expect(h.reads[0].eq).toEqual([
      ['source', 'sreality'],
      ['source_id_native', '99'],
    ]);
  });

  it('resolves a legacy sreality id in one read', async () => {
    await fetchAdvertProperty({ srealityId: -11876 });
    expect(h.reads[0].eq).toEqual([['sreality_id', -11876]]);
  });
});

describe('fetchProperty', () => {
  it('reads the property row with the canonical advert as its id', async () => {
    const p = await fetchProperty(774);
    expect(h.reads[0].relation).toBe('properties_public');
    expect(h.reads[0].select.startsWith('id:listing_id,property_id,')).toBe(true);
    expect(h.reads[0].eq).toEqual([['property_id', 774]]);
    expect(p?.id).toBe(105054);
    // PostgREST hands numeric over as a string.
    expect(p?.total_price_change_pct).toBe(-4.5);
    expect(p?.source_url).toBeNull();
  });

  it('answers null for an id that is not an active property', async () => {
    h.tables.properties_public = [];
    expect(await fetchProperty(1)).toBeNull();
  });
});
