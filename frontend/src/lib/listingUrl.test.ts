import { describe, expect, it } from 'vitest';
import {
  listingCanonicalPath,
  listingPath,
  listingRowPath,
  propertyListingPath,
} from './listingUrl';

describe('listingPath (legacy/resolver form)', () => {
  it('builds /listing/{id} for a real sreality id', () => {
    expect(listingPath(4294963276)).toBe('/listing/4294963276');
  });
  it('accepts the negative synthetic id (the resolver route handles it)', () => {
    expect(listingPath(-284913)).toBe('/listing/-284913');
  });
});

describe('propertyListingPath', () => {
  it('builds the ?property= entry', () => {
    expect(propertyListingPath(42)).toBe('/listing?property=42');
  });
});

describe('listingRowPath (Gate-2 null-safe Browse row link)', () => {
  it('uses the fast legacy path when the row has a sreality_id', () => {
    expect(listingRowPath({ sreality_id: 4294963276, property_id: 42 })).toBe(
      '/listing/4294963276',
    );
  });
  it('still accepts the negative synthetic id (pre-Gate-2 non-sreality rows)', () => {
    expect(listingRowPath({ sreality_id: -284913, property_id: 42 })).toBe(
      '/listing/-284913',
    );
  });
  it('routes a NULL sreality_id to the property route, never /listing/null', () => {
    // Post-Gate-2 a new non-sreality repr has sreality_id = NULL; listingPath(null)
    // would build "/listing/null" (and the id-spaces overlap, so the surrogate must
    // NOT be routed through the legacy sreality route). The property route is the
    // null-safe fallback ListingDetail resolves canonically.
    const path = listingRowPath({ sreality_id: null, property_id: 42 });
    expect(path).toBe('/listing?property=42');
    expect(path).not.toContain('null');
  });
});

describe('listingCanonicalPath (natural-key form)', () => {
  it('builds a self-describing /listing/{source}/{native} url', () => {
    expect(listingCanonicalPath('bazos', '218865547')).toBe('/listing/bazos/218865547');
  });
  it('never emits a negative synthetic id — the native id is the portal key', () => {
    const path = listingCanonicalPath('idnes', 'abc-123');
    expect(path).toBe('/listing/idnes/abc-123');
    expect(path).not.toContain('-284913');
  });
  it('encodes a native id that would otherwise break the path', () => {
    expect(listingCanonicalPath('mmreality', 'a/b c')).toBe('/listing/mmreality/a%2Fb%20c');
  });
});
