import { describe, expect, it } from 'vitest';

import { portalListingUrl, srealityListingUrl } from './portals';

describe('portalListingUrl', () => {
  it('prefers a stored source_url for any portal', () => {
    expect(
      portalListingUrl('idnes', 'https://reality.idnes.cz/detail/prodej/dum/x/abc/', -57891),
    ).toBe('https://reality.idnes.cz/detail/prodej/dum/x/abc/');
  });

  it('rebuilds the sreality URL from the category triple when source_url is missing', () => {
    // sreality stores no source_url; its detail page 404s on a placeholder
    // slug, so we need the real type/main/sub (locality "x" → 301 to canonical).
    expect(
      portalListingUrl('sreality', null, 3604022092, {
        categoryType: 'prodej',
        categoryMain: 'komercni',
        categorySubCb: 38, // Činžovní dům
      }),
    ).toBe('https://www.sreality.cz/detail/prodej/komercni/cinzovni-dum/x/3604022092');
  });

  it('returns null for sreality when the category triple is missing (→ in-app fallback)', () => {
    // Without the sub-category we cannot build a resolvable URL — a broken
    // /detail/x/x/x/{id} 404s, so prefer the in-app view.
    expect(portalListingUrl('sreality', null, 2349675340)).toBeNull();
    expect(
      portalListingUrl('sreality', null, 2349675340, {
        categoryType: 'prodej',
        categoryMain: 'byt',
        categorySubCb: null,
      }),
    ).toBeNull();
  });

  it('returns null (→ in-app fallback) for a non-sreality portal with no source_url', () => {
    expect(portalListingUrl('bazos', null, -40997)).toBeNull();
  });

  it('returns null for sreality with no usable id', () => {
    expect(
      portalListingUrl('sreality', null, null, {
        categoryType: 'prodej',
        categoryMain: 'byt',
        categorySubCb: 5,
      }),
    ).toBeNull();
    expect(
      portalListingUrl('sreality', null, '', {
        categoryType: 'prodej',
        categoryMain: 'byt',
        categorySubCb: 5,
      }),
    ).toBeNull();
  });
});

describe('srealityListingUrl', () => {
  it('keeps the "+" in disposition sub-categories', () => {
    expect(
      srealityListingUrl(481398860, {
        categoryType: 'prodej',
        categoryMain: 'byt',
        categorySubCb: 5, // 2+1
      }),
    ).toBe('https://www.sreality.cz/detail/prodej/byt/2+1/x/481398860');
  });

  it('slugifies a named sub-category (diacritics stripped, spaces → hyphens)', () => {
    expect(
      srealityListingUrl(123, {
        categoryType: 'prodej',
        categoryMain: 'dum',
        categorySubCb: 37, // Rodinný dům
      }),
    ).toBe('https://www.sreality.cz/detail/prodej/dum/rodinny-dum/x/123');
  });

  it('returns null when type, main, or sub is absent', () => {
    expect(srealityListingUrl(1, { categoryMain: 'byt', categorySubCb: 5 })).toBeNull();
    expect(srealityListingUrl(1, { categoryType: 'prodej', categorySubCb: 5 })).toBeNull();
    expect(
      srealityListingUrl(1, { categoryType: 'prodej', categoryMain: 'byt' }),
    ).toBeNull();
    expect(
      srealityListingUrl(1, { categoryType: 'prodej', categoryMain: 'byt', categorySubCb: 99999 }),
    ).toBeNull(); // unmapped cb → no slug → null
  });
});
