import { describe, it, expect } from 'vitest';
import { placePrimary } from './placeLabel';

describe('placePrimary', () => {
  it('falls back to the geo town when a Bazoš locality is just the okres', () => {
    // The reported case: a Telč flat whose seller typed "Jihlava" (the okres).
    expect(
      placePrimary({ locality: 'Jihlava', district: 'okres Jihlava', okres: 'Jihlava', obec: 'Telč', street: null }),
    ).toBe('Telč');
  });

  it('prefixes the parsed street when present', () => {
    expect(
      placePrimary({ locality: 'Jihlava', okres: 'Jihlava', obec: 'Telč', street: 'Radkovská' }),
    ).toBe('Radkovská, Telč');
  });

  it('keeps a rich sreality locality verbatim (never coarsens to obec)', () => {
    expect(
      placePrimary({ locality: 'Edvarda Beneše, Plzeň', okres: 'Plzeň-město', obec: 'Plzeň', street: null }),
    ).toBe('Edvarda Beneše, Plzeň');
  });

  it('uses the geo town when locality is missing entirely', () => {
    expect(placePrimary({ locality: null, district: 'okres Brno-město', okres: 'Brno-město', obec: 'Brno' })).toBe('Brno');
  });

  it('last-resorts to district, then null', () => {
    expect(placePrimary({ district: 'okres X' })).toBe('okres X');
    expect(placePrimary({})).toBeNull();
  });

  it('treats whitespace-only fields as empty', () => {
    expect(placePrimary({ locality: '   ', obec: 'Telč' })).toBe('Telč');
  });
});
