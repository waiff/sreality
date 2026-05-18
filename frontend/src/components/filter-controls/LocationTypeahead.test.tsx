/* Pure-function tests for `deriveContext` — the helper that walks
 * a Mapy.cz suggestion's `regionalStructure` to find the nearest
 * parent municipality. Component-level rendering tests live in
 * follow-ups; the deriveContext logic is what the new behaviour
 * hinges on (street-level pick → city-narrow chip) so it gets
 * direct coverage here.
 */

import { describe, expect, it } from 'vitest';

import { deriveContext } from './LocationTypeahead';
import type { MapySuggestion } from '@/lib/maps';

const suggestion = (overrides: Partial<MapySuggestion>): MapySuggestion => ({
  name: 'placeholder',
  label: 'placeholder',
  type: 'regional.street',
  regionalStructure: [],
  ...overrides,
});

describe('deriveContext', () => {
  it('returns the parent municipality for a street pick', () => {
    const s = suggestion({
      name: 'Edvarda Beneše',
      type: 'regional.street',
      regionalStructure: [
        { name: 'Edvarda Beneše', type: 'regional.street' },
        { name: 'Jižní Předměstí', type: 'regional.municipality_part' },
        { name: 'Plzeň', type: 'regional.municipality' },
        { name: 'okres Plzeň-město', type: 'regional.region.district' },
        { name: 'Plzeňský kraj', type: 'regional.region' },
      ],
    });
    expect(deriveContext(s)).toBe('Plzeň');
  });

  it('returns null for a municipality-level pick (no narrowing needed)', () => {
    expect(
      deriveContext(suggestion({ name: 'Praha', type: 'regional.municipality' })),
    ).toBeNull();
  });

  it('returns null for region- and country-level picks', () => {
    expect(
      deriveContext(suggestion({ name: 'Plzeňský kraj', type: 'regional.region' })),
    ).toBeNull();
    expect(
      deriveContext(suggestion({ name: 'Česká republika', type: 'regional.country' })),
    ).toBeNull();
  });

  it('returns null when the suggestion lacks a regional.municipality ancestor', () => {
    const orphan = suggestion({
      name: 'Restaurant XYZ',
      type: 'poi',
      regionalStructure: [
        { name: 'Plzeňský kraj', type: 'regional.region' },
      ],
    });
    expect(deriveContext(orphan)).toBeNull();
  });

  it('finds the nearest municipality when regionalStructure is unordered', () => {
    const s = suggestion({
      name: 'třída Edvarda Beneše 1',
      type: 'regional.address',
      regionalStructure: [
        { name: 'Hradec Králové - Třebeš', type: 'regional.municipality_part' },
        { name: 'Hradec Králové', type: 'regional.municipality' },
        { name: 'třída Edvarda Beneše', type: 'regional.street' },
      ],
    });
    expect(deriveContext(s)).toBe('Hradec Králové');
  });
});
