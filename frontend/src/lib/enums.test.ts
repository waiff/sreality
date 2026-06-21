import { describe, expect, it } from 'vitest';

import { categoryMainLabel } from './enums';

describe('categoryMainLabel', () => {
  it('maps known category_main slugs to Czech singular labels', () => {
    expect(categoryMainLabel('byt')).toBe('Byt');
    expect(categoryMainLabel('dum')).toBe('Dům');
    expect(categoryMainLabel('komercni')).toBe('Komerční prostor');
    expect(categoryMainLabel('pozemek')).toBe('Pozemek');
    expect(categoryMainLabel('ostatni')).toBe('Ostatní');
  });

  it('falls back to "Nemovitost" for unknown / null / empty', () => {
    expect(categoryMainLabel(null)).toBe('Nemovitost');
    expect(categoryMainLabel(undefined)).toBe('Nemovitost');
    expect(categoryMainLabel('')).toBe('Nemovitost');
    expect(categoryMainLabel('chata')).toBe('Nemovitost');
  });
});
