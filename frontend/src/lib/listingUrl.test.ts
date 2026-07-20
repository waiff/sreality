import { describe, expect, it } from 'vitest';
import { listingCanonicalPath, listingPath, propertyListingPath } from './listingUrl';

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
