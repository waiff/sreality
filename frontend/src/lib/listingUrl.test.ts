import { describe, expect, it } from 'vitest';
import { advertPath, listingPath, propertyPath } from './listingUrl';

describe('propertyPath (the one detail page)', () => {
  it('builds the stable property address', () => {
    expect(propertyPath(42)).toBe('/property/42');
  });
  it('opens one advert’s row when asked', () => {
    expect(propertyPath(42, 105053)).toBe('/property/42?advert=105053');
  });
});

describe('listingPath (legacy advert alias)', () => {
  it('builds /listing/{id} for a real sreality id', () => {
    expect(listingPath(4294963276)).toBe('/listing/4294963276');
  });
  it('accepts the negative synthetic id (the alias resolves it)', () => {
    expect(listingPath(-284913)).toBe('/listing/-284913');
  });
});

describe('advertPath (natural key first)', () => {
  it('prefers the self-describing /listing/{source}/{native} url', () => {
    expect(
      advertPath({ source: 'idnes', source_id_native: '6a625d608a2b370d4a071f4c', sreality_id: -399151 }),
    ).toBe('/listing/idnes/6a625d608a2b370d4a071f4c');
  });
  it('encodes a native id that would otherwise break the path', () => {
    expect(advertPath({ source: 'mmreality', source_id_native: 'a/b c' })).toBe(
      '/listing/mmreality/a%2Fb%20c',
    );
  });
  it('falls back to the legacy id when the native id is missing', () => {
    expect(advertPath({ source: 'idnes', source_id_native: null, sreality_id: -284913 })).toBe(
      '/listing/-284913',
    );
  });
  it('returns null rather than fabricating a destination', () => {
    expect(advertPath({ source: 'idnes', source_id_native: null, sreality_id: null })).toBeNull();
  });
});
