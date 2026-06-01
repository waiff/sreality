import { describe, expect, it } from 'vitest';

import { portalListingUrl } from './portals';

describe('portalListingUrl', () => {
  it('prefers a stored source_url for any portal', () => {
    expect(
      portalListingUrl('idnes', 'https://reality.idnes.cz/detail/prodej/dum/x/abc/', -57891),
    ).toBe('https://reality.idnes.cz/detail/prodej/dum/x/abc/');
  });

  it('rebuilds the sreality URL from the native id when source_url is missing', () => {
    // sreality's scraper never stores source_url; the public page resolves by id
    expect(portalListingUrl('sreality', null, 2349675340)).toBe(
      'https://www.sreality.cz/detail/x/x/x/2349675340',
    );
    expect(portalListingUrl('sreality', null, '2349675340')).toBe(
      'https://www.sreality.cz/detail/x/x/x/2349675340',
    );
  });

  it('returns null (→ in-app fallback) for a non-sreality portal with no source_url', () => {
    expect(portalListingUrl('bazos', null, -40997)).toBeNull();
  });

  it('returns null for sreality with no usable id', () => {
    expect(portalListingUrl('sreality', null, null)).toBeNull();
    expect(portalListingUrl('sreality', null, '')).toBeNull();
  });
});
