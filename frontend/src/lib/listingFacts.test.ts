import { describe, expect, it } from 'vitest';
import { buildAmenities, buildFacts } from './listingFacts';
import type { ListingPublic } from './types';

/* Minimal ListingPublic with every fact/amenity null — the shared facts module
 * must degrade gracefully on a sparse row (bazos/idnes), never throw or show a
 * null as "absent". */
const base = (over: Partial<ListingPublic>): ListingPublic =>
  ({
    estate_area: null,
    garden_area: null,
    building_type: null,
    condition: null,
    energy_rating: null,
    ownership: null,
    furnished: null,
    has_balcony: null,
    terrace: null,
    has_lift: null,
    cellar: null,
    garage: null,
    has_parking: null,
    parking_lots: null,
    ...over,
  }) as ListingPublic;

describe('buildFacts', () => {
  it('returns [] when every fact is null', () => {
    expect(buildFacts(base({}))).toEqual([]);
  });

  it('includes only present facts, in order, capitalised', () => {
    const facts = buildFacts(base({ building_type: 'panel', condition: 'velmi_dobry' }));
    expect(facts.map((f) => f.label)).toEqual(['Building', 'Condition']);
    expect(facts[0].value).toBe('Panel');
  });
});

describe('buildAmenities', () => {
  it('drops amenities whose presence is unknown (null != absent)', () => {
    expect(buildAmenities(base({}))).toEqual([]);
  });

  it('keeps known amenities (true OR false) and notes the parking-spaces count', () => {
    const ams = buildAmenities(
      base({ has_balcony: true, terrace: false, has_parking: true, parking_lots: 2 }),
    );
    const byLabel = Object.fromEntries(ams.map((a) => [a.label, a]));
    expect(Object.keys(byLabel).sort()).toEqual(['Balcony', 'Parking', 'Terrace']);
    expect(byLabel.Balcony.present).toBe(true);
    expect(byLabel.Terrace.present).toBe(false);
    expect(byLabel.Parking.note).toBeTruthy();
  });

  it('omits the parking note when there are no recorded spaces', () => {
    const ams = buildAmenities(base({ has_parking: true, parking_lots: 0 }));
    expect(ams.find((a) => a.label === 'Parking')?.note).toBeNull();
  });
});
