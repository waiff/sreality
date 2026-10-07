import { describe, it, expect } from 'vitest';
import {
  applyKeyset,
  formatKeysetValue,
  nextCursorFrom,
  withKeysetColumns,
  type KeysetBuilder,
  type KeysetCursor,
} from './keyset';
import type { SortSpec } from './queries';

/* A recording stand-in for the supabase-js query builder: every method
 * appends a typed entry and returns `this`, so a test can assert the exact
 * ORDER BY / OR / IS / LT chain keyset emits. */
type Call =
  | { m: 'order'; column: string; ascending: boolean; nullsFirst?: boolean }
  | { m: 'or'; filters: string }
  | { m: 'is'; column: string; value: null }
  | { m: 'lt'; column: string; value: number }
  | { m: 'gt'; column: string; value: number };

class Recorder implements KeysetBuilder {
  calls: Call[] = [];
  order(column: string, opts: { ascending: boolean; nullsFirst?: boolean }) {
    this.calls.push({ m: 'order', column, ...opts });
    return this;
  }
  or(filters: string) {
    this.calls.push({ m: 'or', filters });
    return this;
  }
  is(column: string, value: null) {
    this.calls.push({ m: 'is', column, value });
    return this;
  }
  lt(column: string, value: number) {
    this.calls.push({ m: 'lt', column, value });
    return this;
  }
  gt(column: string, value: number) {
    this.calls.push({ m: 'gt', column, value });
    return this;
  }
}

const sort = (field: string, direction: 'asc' | 'desc'): SortSpec =>
  ({ field, direction }) as SortSpec;

describe('formatKeysetValue', () => {
  it('renders numbers bare', () => {
    expect(formatKeysetValue(4250000)).toBe('4250000');
  });
  it('renders booleans bare', () => {
    expect(formatKeysetValue(true)).toBe('true');
    expect(formatKeysetValue(false)).toBe('false');
  });
  it('double-quotes strings (ISO timestamps survive the colons/dots)', () => {
    expect(formatKeysetValue('2026-06-19T10:00:00.123456+00:00')).toBe(
      '"2026-06-19T10:00:00.123456+00:00"',
    );
  });
  it('escapes embedded quotes and backslashes, protecting reserved chars', () => {
    // A district label with a comma/paren is shielded by the surrounding
    // quotes; only " and \ need escaping.
    expect(formatKeysetValue('Praha 5 - Smíchov')).toBe('"Praha 5 - Smíchov"');
    expect(formatKeysetValue('a"b\\c')).toBe('"a\\"b\\\\c"');
  });
});

describe('applyKeyset — ORDER BY', () => {
  it('appends property_id as the tiebreaker in the DESC direction for a desc sort', () => {
    const r = new Recorder();
    applyKeyset(r, sort('last_seen_at', 'desc'), null);
    expect(r.calls).toEqual([
      // last_seen_at is NOT NULL — no nulls clause, so DESC gets the btree
      // default (NULLS FIRST) that a backward index scan produces.
      { m: 'order', column: 'last_seen_at', ascending: false },
      { m: 'order', column: 'property_id', ascending: false },
    ]);
  });

  it('orders BOTH the sort column and property_id ascending for an asc sort', () => {
    const r = new Recorder();
    applyKeyset(r, sort('price_czk', 'asc'), null);
    expect(r.calls).toEqual([
      { m: 'order', column: 'price_czk', ascending: true, nullsFirst: false },
      { m: 'order', column: 'property_id', ascending: true },
    ]);
  });
});

/* The index-matching contract (the fix for the market-wide Browse timeout): a
 * DESC sort on a NOT-NULL keyset column must NOT emit `nullslast`, or the
 * planner can't use the `(col, id)` btree and falls back to a full-cohort scan
 * + sort (8.9s cold vs ~ms indexed). Nullable columns keep `nullslast` because
 * the two-phase cursor requires it. These assertions are the frontend guard;
 * the DB-plan guard lives in tests/test_browse_read_path_guardrail.py. */
describe('applyKeyset — nulls placement matches the serving btree', () => {
  const nullsFirstOf = (calls: Call[], column: string) => {
    const c = calls.find((x) => x.m === 'order' && x.column === column);
    return c && c.m === 'order' ? c.nullsFirst : 'MISSING';
  };

  it.each(['last_seen_at', 'first_seen_at'])(
    'NOT-NULL column %s omits the nulls clause (both directions)',
    (field) => {
      for (const dir of ['desc', 'asc'] as const) {
        const r = new Recorder();
        applyKeyset(r, sort(field, dir), null);
        // undefined => postgrest-js appends no `.nullsfirst`/`.nullslast`.
        expect(nullsFirstOf(r.calls, field)).toBeUndefined();
      }
    },
  );

  it.each(['price_czk', 'area_m2', 'district'])(
    'NULLABLE column %s keeps NULLS LAST (two-phase cursor needs it)',
    (field) => {
      const r = new Recorder();
      applyKeyset(r, sort(field, 'desc'), null);
      expect(nullsFirstOf(r.calls, field)).toBe(false);
    },
  );
});

describe('applyKeyset — DESC non-null cursor', () => {
  it('NOT NULL column (last_seen_at): no is.null disjunct (keeps the index)', () => {
    const r = new Recorder();
    const cur: KeysetCursor = { value: '2026-06-19T10:00:00+00:00', id: 9931 };
    applyKeyset(r, sort('last_seen_at', 'desc'), cur);
    const orCall = r.calls.find((c) => c.m === 'or');
    expect(orCall).toEqual({
      m: 'or',
      filters:
        'last_seen_at.lt."2026-06-19T10:00:00+00:00",'
        + 'and(last_seen_at.eq."2026-06-19T10:00:00+00:00",property_id.lt.9931)',
    });
  });

  it('NULLABLE column (price_czk): appends is.null so the tail stays reachable', () => {
    const r = new Recorder();
    applyKeyset(r, sort('price_czk', 'desc'), { value: 4250000, id: 12 });
    expect((r.calls.find((c) => c.m === 'or') as { filters: string }).filters).toBe(
      'price_czk.lt.4250000,and(price_czk.eq.4250000,property_id.lt.12),price_czk.is.null',
    );
  });

  it('NOT NULL column (first_seen_at): no is.null disjunct', () => {
    const r = new Recorder();
    applyKeyset(r, sort('first_seen_at', 'asc'), { value: '2026-01-01T00:00:00+00:00', id: 5 });
    expect((r.calls.find((c) => c.m === 'or') as { filters: string }).filters).toBe(
      'first_seen_at.gt."2026-01-01T00:00:00+00:00",and(first_seen_at.eq."2026-01-01T00:00:00+00:00",property_id.gt.5)',
    );
  });
});

describe('applyKeyset — ASC non-null cursor', () => {
  it('flips BOTH the boundary and the tiebreaker comparison to `gt`, keeps the null disjunct', () => {
    const r = new Recorder();
    applyKeyset(r, sort('price_czk', 'asc'), { value: 999, id: 7 });
    expect((r.calls.find((c) => c.m === 'or') as { filters: string }).filters).toBe(
      'price_czk.gt.999,and(price_czk.eq.999,property_id.gt.7),price_czk.is.null',
    );
  });
});

describe('applyKeyset — NULLS-LAST tail (cursor value null)', () => {
  it('pages the null block by `field IS NULL AND property_id < id`, no OR', () => {
    const r = new Recorder();
    applyKeyset(r, sort('area_m2', 'desc'), { value: null, id: 4040 });
    expect(r.calls).toEqual([
      { m: 'order', column: 'area_m2', ascending: false, nullsFirst: false },
      { m: 'order', column: 'property_id', ascending: false },
      { m: 'is', column: 'area_m2', value: null },
      { m: 'lt', column: 'property_id', value: 4040 },
    ]);
    expect(r.calls.some((c) => c.m === 'or')).toBe(false);
  });

  it('null-tail pages the tiebreaker in the sort direction (`gt` for ASC)', () => {
    const r = new Recorder();
    applyKeyset(r, sort('area_m2', 'asc'), { value: null, id: 4040 });
    expect(r.calls).toEqual([
      { m: 'order', column: 'area_m2', ascending: true, nullsFirst: false },
      { m: 'order', column: 'property_id', ascending: true },
      { m: 'is', column: 'area_m2', value: null },
      { m: 'gt', column: 'property_id', value: 4040 },
    ]);
  });
});

describe('nextCursorFrom', () => {
  it('returns null for an empty page', () => {
    expect(nextCursorFrom([], sort('last_seen_at', 'desc'))).toBeNull();
  });

  it('reads the sort column + property_id off the last row', () => {
    const rows = [
      { property_id: 1, last_seen_at: 'a' },
      { property_id: 2, last_seen_at: 'b' },
    ];
    expect(nextCursorFrom(rows, sort('last_seen_at', 'desc'))).toEqual({
      value: 'b',
      id: 2,
    });
  });

  it('carries a NULL boundary value as value:null (enters the tail next page)', () => {
    const rows = [{ property_id: 88, area_m2: null }];
    expect(nextCursorFrom(rows, sort('area_m2', 'desc'))).toEqual({
      value: null,
      id: 88,
    });
  });

  it('coerces property_id to a number', () => {
    const rows = [{ property_id: '503', price_czk: 1 }];
    expect(nextCursorFrom(rows, sort('price_czk', 'desc'))?.id).toBe(503);
  });
});

describe('withKeysetColumns', () => {
  it('adds property_id and the sort column without duplicating existing ones', () => {
    const out = withKeysetColumns('sreality_id,price_czk,area_m2', sort('price_czk', 'desc'));
    const cols = out.split(',');
    expect(cols).toContain('property_id');
    expect(cols).toContain('price_czk');
    expect(cols.filter((c) => c === 'price_czk')).toHaveLength(1);
  });

  it('appends a computed sort column not in the base SELECT (price_per_m2)', () => {
    const out = withKeysetColumns('sreality_id,price_czk', sort('price_per_m2', 'asc'));
    expect(out.split(',')).toContain('price_per_m2');
  });
});

/* ---------------------------------------------------------------------- */
/* One portal's order column (MS19, migration 590): NOT NULL on every row   */
/* it pages (applyPortalRule's conjunct), so single-phase and btree-default */
/* null placement, which the DESC partial index serves both ways.           */
/* ---------------------------------------------------------------------- */

describe("applyKeyset on one portal's order column", () => {
  it('emits the btree-default null placement in both directions', () => {
    for (const direction of ['desc', 'asc'] as const) {
      const r = applyKeyset(new Recorder(), sort('newest_ad_at_idnes', direction), null);
      expect(r.calls).toEqual([
        { m: 'order', column: 'newest_ad_at_idnes', ascending: direction === 'asc', nullsFirst: undefined },
        { m: 'order', column: 'property_id', ascending: direction === 'asc' },
      ]);
    }
  });

  it('pages single-phase: no `is.null` disjunct to defeat the index', () => {
    const r = applyKeyset(
      new Recorder(),
      sort('newest_ad_at_maxima', 'desc'),
      { value: '2026-10-01T08:00:00+00:00', id: 900 },
    );
    const or = r.calls.find((c) => c.m === 'or') as { m: 'or'; filters: string };
    expect(or.filters).toBe(
      'newest_ad_at_maxima.lt."2026-10-01T08:00:00+00:00",'
      + 'and(newest_ad_at_maxima.eq."2026-10-01T08:00:00+00:00",property_id.lt.900)',
    );
  });

  it('selects the order column so the cursor can be derived', () => {
    const cols = withKeysetColumns('listing_id,price_czk', sort('newest_ad_at_remax', 'asc')).split(',');
    expect(cols).toEqual(['listing_id', 'price_czk', 'property_id', 'newest_ad_at_remax']);
  });
});

/* ---------------------------------------------------------------------- */
/* A caller-supplied tiebreaker: the listing-grain pin audit                */
/* (lib/pinAudit.ts), whose unique key is listing_id.                       */
/* ---------------------------------------------------------------------- */

describe('applyKeyset with a custom tiebreak', () => {
  it('orders by the tiebreak column it was given, not property_id', () => {
    const r = applyKeyset(new Recorder(), sort('first_seen_at', 'desc'), null, 'listing_id');
    expect(r.calls).toEqual([
      { m: 'order', column: 'first_seen_at', ascending: false, nullsFirst: undefined },
      { m: 'order', column: 'listing_id', ascending: false },
    ]);
  });

  it('carries the tiebreak into the NULLS-LAST tail phase too', () => {
    const r = applyKeyset(
      new Recorder(),
      sort('price_per_m2', 'desc'),
      { value: null, id: 41 },
      'listing_id',
    );
    expect(r.calls).toContainEqual({ m: 'lt', column: 'listing_id', value: 41 });
    expect(r.calls.some((c) => 'column' in c && c.column === 'property_id')).toBe(false);
  });

  it('pages ASC with `>` on the tiebreak, same as the default lane', () => {
    const r = applyKeyset(
      new Recorder(),
      sort('first_seen_at', 'asc'),
      { value: 'abc', id: 7 },
      'listing_id',
    );
    const or = r.calls.find((c) => c.m === 'or') as { m: 'or'; filters: string };
    expect(or.filters).toBe('first_seen_at.gt."abc",and(first_seen_at.eq."abc",listing_id.gt.7)');
  });

  it('still defaults to property_id when no tiebreak is passed', () => {
    const r = applyKeyset(new Recorder(), sort('first_seen_at', 'desc'), null);
    expect(r.calls[1]).toEqual({ m: 'order', column: 'property_id', ascending: false });
  });
});

describe('nextCursorFrom / withKeysetColumns with a custom tiebreak', () => {
  it('reads the cursor id off the given tiebreak column', () => {
    const rows = [
      { listing_id: 11, property_id: 999, first_seen_at: 'k1' },
      { listing_id: 12, property_id: 999, first_seen_at: 'k2' },
    ];
    expect(nextCursorFrom(rows, sort('first_seen_at', 'desc'), 'listing_id')).toEqual({
      value: 'k2',
      id: 12,
    });
  });

  it('selects the tiebreak + sort column so the cursor can be derived', () => {
    const cols = withKeysetColumns(
      'listing_id,price_czk',
      sort('area_m2', 'desc'),
      'listing_id',
    ).split(',');
    expect(cols).toContain('area_m2');
    expect(cols.filter((c) => c === 'listing_id')).toHaveLength(1);
    expect(cols).not.toContain('property_id');
  });
});
