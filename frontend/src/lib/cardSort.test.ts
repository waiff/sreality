import { describe, expect, it } from 'vitest';
import { compareCs } from './collator';
import {
  byNumber,
  makeSorter,
  parseSortParam,
  sortParamOf,
  timeKey,
  type SortOption,
} from './cardSort';

interface Row {
  id: number;
  price: number | null;
  city: string | null;
  added: string | null;
}

type Field = 'price' | 'city' | 'added';

const OPTIONS: ReadonlyArray<SortOption<Field>> = [
  { value: '-price', label: 'Cena sestupně', field: 'price', direction: 'desc' },
  { value: 'price', label: 'Cena vzestupně', field: 'price', direction: 'asc' },
  { value: 'city', label: 'Město', field: 'city', direction: 'asc' },
  { value: '-added', label: 'Nejnovější', field: 'added', direction: 'desc' },
];

const sorter = makeSorter<Row, Field>(
  {
    price: (r) => r.price,
    city: (r) => r.city,
    added: (r) => timeKey(r.added),
  },
  byNumber((r) => r.id),
);

const ids = (rows: Row[]) => rows.map((r) => r.id);

describe('sort param round-trip', () => {
  it('serializes direction as a leading minus', () => {
    expect(sortParamOf({ field: 'price', direction: 'desc' })).toBe('-price');
    expect(sortParamOf({ field: 'price', direction: 'asc' })).toBe('price');
  });

  it('parses a known token', () => {
    expect(parseSortParam('-price', OPTIONS, { field: 'city', direction: 'asc' }))
      .toEqual({ field: 'price', direction: 'desc' });
  });

  it('falls back for unknown, malformed, and absent tokens', () => {
    const fb = { field: 'city', direction: 'asc' } as const;
    expect(parseSortParam('nonsense', OPTIONS, fb)).toEqual(fb);
    expect(parseSortParam('-nonsense', OPTIONS, fb)).toEqual(fb);
    expect(parseSortParam(null, OPTIONS, fb)).toEqual(fb);
    expect(parseSortParam('', OPTIONS, fb)).toEqual(fb);
  });
});

describe('null handling', () => {
  const rows: Row[] = [
    { id: 1, price: 500, city: null, added: null },
    { id: 2, price: null, city: 'Brno', added: '2026-01-01T00:00:00Z' },
    { id: 3, price: 100, city: 'Aš', added: '2026-02-01T00:00:00Z' },
  ];

  it('puts nulls last ASCENDING', () => {
    expect(ids(sorter(rows, { field: 'price', direction: 'asc' }))).toEqual([3, 1, 2]);
  });

  it('puts nulls last DESCENDING too — a card with no price never tops "most expensive"', () => {
    expect(ids(sorter(rows, { field: 'price', direction: 'desc' }))).toEqual([1, 3, 2]);
  });

  it('applies the same rule to string and time keys', () => {
    expect(ids(sorter(rows, { field: 'city', direction: 'asc' }))).toEqual([3, 2, 1]);
    expect(ids(sorter(rows, { field: 'added', direction: 'desc' }))).toEqual([3, 2, 1]);
  });
});

describe('stability', () => {
  it('breaks ties deterministically instead of preserving fetch order', () => {
    const a: Row[] = [
      { id: 9, price: 100, city: 'X', added: null },
      { id: 4, price: 100, city: 'X', added: null },
      { id: 7, price: 100, city: 'X', added: null },
    ];
    // Same keys throughout: order must come from the tiebreak, and must not
    // depend on the order the rows arrived in.
    expect(ids(sorter(a, { field: 'price', direction: 'desc' }))).toEqual([4, 7, 9]);
    expect(ids(sorter([...a].reverse(), { field: 'price', direction: 'desc' })))
      .toEqual([4, 7, 9]);
  });

  it('does not mutate the input array', () => {
    const rows: Row[] = [
      { id: 2, price: 2, city: null, added: null },
      { id: 1, price: 1, city: null, added: null },
    ];
    const before = ids(rows);
    sorter(rows, { field: 'price', direction: 'asc' });
    expect(ids(rows)).toEqual(before);
  });
});

describe('Czech collation', () => {
  it('sorts Č straight after C, not after Z', () => {
    const sorted = ['Zlín', 'Česká Lípa', 'Cheb', 'Brno'].sort(compareCs);
    expect(sorted).toEqual(['Brno', 'Česká Lípa', 'Cheb', 'Zlín']);
  });

  it('orders embedded numbers naturally', () => {
    expect(['Praha 10', 'Praha 2', 'Praha 1'].sort(compareCs)).toEqual([
      'Praha 1',
      'Praha 2',
      'Praha 10',
    ]);
  });

  it('sorts blank-ish values last', () => {
    expect(['Brno', null, '  ', 'Aš'].sort(compareCs)).toEqual(['Aš', 'Brno', null, '  ']);
  });
});

describe('timeKey', () => {
  it('parses ISO to epoch ms and nulls anything unusable', () => {
    expect(timeKey('2026-01-01T00:00:00Z')).toBe(Date.parse('2026-01-01T00:00:00Z'));
    expect(timeKey(null)).toBeNull();
    expect(timeKey('not a date')).toBeNull();
  });
});
