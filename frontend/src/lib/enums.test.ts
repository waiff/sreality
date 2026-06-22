import { describe, expect, it } from 'vitest';

import {
  categoryMainLabel,
  categoryMainLabelPlural,
  categoryTypeLabel,
  listingKindLabel,
  listingKindParts,
  listingTypeLabel,
  subtypeLabel,
  SUBTYPE_LABELS_BY_MAIN,
} from './enums';

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

describe('subtypeLabel + SUBTYPE_LABELS_BY_MAIN (derived from the filter registry)', () => {
  it('maps canonical subtype slugs to their Czech labels', () => {
    expect(subtypeLabel('ubytovani')).toBe('Ubytování');
    expect(subtypeLabel('cinzovni_dum')).toBe('Činžovní dům');
    expect(subtypeLabel('rodinny_dum')).toBe('Rodinný dům');
  });

  it('returns the raw slug when unknown (never blank)', () => {
    expect(subtypeLabel('not_a_subtype')).toBe('not_a_subtype');
  });

  it('groups slugs under the right category_main, sourced from the registry', () => {
    const komercniSlugs = SUBTYPE_LABELS_BY_MAIN.komercni.map((o) => o.slug);
    const dumSlugs = SUBTYPE_LABELS_BY_MAIN.dum.map((o) => o.slug);
    expect(komercniSlugs).toContain('kancelar');
    expect(komercniSlugs).toContain('ubytovani');
    expect(dumSlugs).toContain('rodinny_dum');
    // the partition is disjoint — a slug never appears in both groups
    expect(komercniSlugs).not.toContain('rodinny_dum');
    expect(dumSlugs).not.toContain('kancelar');
  });
});

describe('listingKind — disposition / subtype are complementary faces of one descriptor', () => {
  it('apartments use disposition (subtype NULL)', () => {
    const apt = { disposition: '2+kk', subtype: null };
    expect(listingKindLabel(apt)).toBe('2+kk');
    expect(listingKindParts(apt)).toEqual(['2+kk']);
    expect(listingTypeLabel({ ...apt, category_main: 'byt' })).toBe('Byt');
  });

  it('commercial uses the subtype label (disposition NULL → no more bare dash)', () => {
    const comm = { disposition: null, subtype: 'ubytovani' };
    expect(listingKindLabel(comm)).toBe('Ubytování');
    expect(listingKindParts(comm)).toEqual(['Ubytování']);
    expect(listingTypeLabel({ ...comm, category_main: 'komercni' })).toBe('Ubytování');
  });

  it('subtype-less commercial falls back to the generic category word', () => {
    expect(listingTypeLabel({ disposition: null, subtype: null, category_main: 'komercni' }))
      .toBe('Komerční prostor');
  });

  it('a house listed by room count keeps both, subtype first', () => {
    const house = { disposition: '5+kk', subtype: 'rodinny_dum' };
    expect(listingKindParts(house)).toEqual(['Rodinný dům', '5+kk']);
    expect(listingKindLabel(house)).toBe('Rodinný dům');
  });

  it('land has neither → null / empty', () => {
    const land = { disposition: null, subtype: null };
    expect(listingKindLabel(land)).toBeNull();
    expect(listingKindParts(land)).toEqual([]);
  });
});

describe('categoryMainLabelPlural + categoryTypeLabel (registry-derived, shared by Health)', () => {
  it('plural category headings', () => {
    expect(categoryMainLabelPlural('byt')).toBe('Byty');
    expect(categoryMainLabelPlural('dum')).toBe('Domy');
    expect(categoryMainLabelPlural('komercni')).toBe('Komerční');
  });

  it('deal-type words', () => {
    expect(categoryTypeLabel('prodej')).toBe('Prodej');
    expect(categoryTypeLabel('pronajem')).toBe('Pronájem');
  });
});
