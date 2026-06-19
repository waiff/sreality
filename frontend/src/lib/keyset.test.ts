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
      { m: 'order', column: 'last_seen_at', ascending: false, nullsFirst: false },
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
