import { describe, it, expect } from 'vitest';
import {
  BUILDING_GRANULARITY_RANK,
  MAX_DRAWN_CIRCLE_RADIUS_M,
  drawnUncertaintyRadiusM,
  formatUncertaintyRadius,
  isPinExact,
  pinPrecisionLabel,
  uncertaintyCircleRadiusM,
  uncertaintyPixelsAtZoom0,
  type PinPrecision,
} from './uncertaintyCircle';

/* The ranks are migration 380's seed. They are spelled out here rather than
 * imported so a change to the lookup table has to be made twice, deliberately —
 * the circle rule is a product decision about WHICH rung stops being fuzzy, not
 * a mirror of whatever the table happens to say. */
const RANK = {
  obec: 40,
  cast_obce_or_quarter: 50,
  street: 60,
  street_segment: 70,
  parcel: 80,
  building: 90,
  address_point: 100,
} as const;

describe('uncertaintyCircleRadiusM', () => {
  it('draws the circle below building level', () => {
    expect(uncertaintyCircleRadiusM({ granularity_rank: RANK.obec, uncertainty_radius_m: 1500 }))
      .toBe(1500);
    expect(uncertaintyCircleRadiusM({ granularity_rank: RANK.street, uncertainty_radius_m: 120 }))
      .toBe(120);
    expect(uncertaintyCircleRadiusM({ granularity_rank: RANK.parcel, uncertainty_radius_m: 25 }))
      .toBe(25);
  });

  it('draws no circle at or above building level -- the pin IS the building', () => {
    expect(uncertaintyCircleRadiusM({ granularity_rank: RANK.building, uncertainty_radius_m: 15 }))
      .toBeNull();
    expect(
      uncertaintyCircleRadiusM({ granularity_rank: RANK.address_point, uncertainty_radius_m: 5 }),
    ).toBeNull();
  });

  it('pins the boundary to the building rung', () => {
    expect(BUILDING_GRANULARITY_RANK).toBe(RANK.building);
    expect(
      uncertaintyCircleRadiusM({
        granularity_rank: BUILDING_GRANULARITY_RANK - 1,
        uncertainty_radius_m: 10,
      }),
    ).toBe(10);
  });

  it('draws no circle for an unresolved pin', () => {
    // No listing_location row: both columns arrive NULL through the projection.
    expect(uncertaintyCircleRadiusM({ granularity_rank: null, uncertainty_radius_m: null }))
      .toBeNull();
    // A rank with no radius is not a circle of unknown size -- it is no circle.
    expect(uncertaintyCircleRadiusM({ granularity_rank: RANK.obec, uncertainty_radius_m: null }))
      .toBeNull();
    expect(uncertaintyCircleRadiusM({ granularity_rank: null, uncertainty_radius_m: 900 }))
      .toBeNull();
  });

  it('treats a zero or negative radius as nothing to draw', () => {
    expect(uncertaintyCircleRadiusM({ granularity_rank: RANK.obec, uncertainty_radius_m: 0 }))
      .toBeNull();
    expect(uncertaintyCircleRadiusM({ granularity_rank: RANK.obec, uncertainty_radius_m: -5 }))
      .toBeNull();
  });

  it('accepts the numeric-as-string shape PostgREST sends for `numeric`', () => {
    expect(
      uncertaintyCircleRadiusM({
        granularity_rank: RANK.cast_obce_or_quarter,
        uncertainty_radius_m: '750' as unknown as number,
      }),
    ).toBe(750);
  });
});

describe('drawnUncertaintyRadiusM', () => {
  it('draws the true radius when it is inside the display cap', () => {
    expect(drawnUncertaintyRadiusM({ granularity_rank: RANK.street, uncertainty_radius_m: 120 }))
      .toBe(120);
    expect(
      drawnUncertaintyRadiusM({
        granularity_rank: RANK.cast_obce_or_quarter,
        uncertainty_radius_m: MAX_DRAWN_CIRCLE_RADIUS_M,
      }),
    ).toBe(MAX_DRAWN_CIRCLE_RADIUS_M);
  });

  it('clamps a coarse rung instead of washing the map out', () => {
    // okres ~25 km, kraj ~60 km, unknown ~250 km: drawn true, one pin covers the
    // viewport and clips into a moving arc on pan.
    expect(drawnUncertaintyRadiusM({ granularity_rank: 30, uncertainty_radius_m: 25_000 }))
      .toBe(MAX_DRAWN_CIRCLE_RADIUS_M);
    expect(drawnUncertaintyRadiusM({ granularity_rank: 20, uncertainty_radius_m: 60_000 }))
      .toBe(MAX_DRAWN_CIRCLE_RADIUS_M);
    expect(drawnUncertaintyRadiusM({ granularity_rank: 0, uncertainty_radius_m: 250_000 }))
      .toBe(MAX_DRAWN_CIRCLE_RADIUS_M);
  });

  it('clamps the DRAWING only -- the rule still reports the true radius', () => {
    const pin = { granularity_rank: RANK.obec, uncertainty_radius_m: 25_000 };
    expect(uncertaintyCircleRadiusM(pin)).toBe(25_000);
    expect(drawnUncertaintyRadiusM(pin)).toBe(MAX_DRAWN_CIRCLE_RADIUS_M);
  });

  it('still draws nothing where the rule says nothing', () => {
    expect(drawnUncertaintyRadiusM({ granularity_rank: RANK.building, uncertainty_radius_m: 15 }))
      .toBeNull();
    expect(drawnUncertaintyRadiusM({ granularity_rank: null, uncertainty_radius_m: null }))
      .toBeNull();
  });
});

describe('uncertaintyPixelsAtZoom0', () => {
  it('scales so that doubling per zoom level lands on the true metre radius', () => {
    // 1 000 m at Prague's latitude, read back at zoom 14: metres per pixel there
    // is 156543.034 * cos(lat) / 2^14, so the circle must be that many pixels.
    const lat = 50.0755;
    const px0 = uncertaintyPixelsAtZoom0(1000, lat);
    const mPerPxAt14 = (156_543.033_928 * Math.cos((lat * Math.PI) / 180)) / 2 ** 14;
    expect(px0 * 2 ** 14).toBeCloseTo(1000 / mPerPxAt14, 6);
  });

  it('is bigger further north -- a metre is fewer Mercator pixels at the equator', () => {
    expect(uncertaintyPixelsAtZoom0(1000, 51)).toBeGreaterThan(
      uncertaintyPixelsAtZoom0(1000, 48),
    );
  });
});

describe('isPinExact', () => {
  it('is solid exactly where the circle rule draws no circle for a known rung', () => {
    expect(isPinExact({ granularity_rank: RANK.address_point, uncertainty_radius_m: 10 })).toBe(true);
    expect(isPinExact({ granularity_rank: RANK.building, uncertainty_radius_m: 15 })).toBe(true);
    expect(isPinExact({ granularity_rank: RANK.parcel, uncertainty_radius_m: 25 })).toBe(false);
    expect(isPinExact({ granularity_rank: RANK.obec, uncertainty_radius_m: 1000 })).toBe(false);
  });

  it('never draws an unresolved pin solid -- nobody claimed "exactly here"', () => {
    expect(isPinExact({ granularity_rank: null, uncertainty_radius_m: null })).toBe(false);
    // Through the map's vector-tile round trip a NULL property arrives absent.
    expect(isPinExact({} as PinPrecision)).toBe(false);
  });
});

describe('formatUncertaintyRadius', () => {
  it('prints tens of metres below a kilometre', () => {
    expect(formatUncertaintyRadius(300)).toBe('±300 m');
    expect(formatUncertaintyRadius(611.55245496)).toBe('±610 m');
    expect(formatUncertaintyRadius(3)).toBe('±10 m');
  });

  it('switches to kilometres with a Czech decimal comma', () => {
    expect(formatUncertaintyRadius(1000)).toBe('±1 km');
    expect(formatUncertaintyRadius(996)).toBe('±1 km');
    expect(formatUncertaintyRadius(4333.78982623)).toBe('±4,3 km');
    expect(formatUncertaintyRadius(250_000)).toBe('±250 km');
  });
});

describe('pinPrecisionLabel', () => {
  it('names the rung and the radius of an approximate pin', () => {
    expect(pinPrecisionLabel({ granularity_rank: RANK.street, uncertainty_radius_m: 300 }))
      .toBe('Přibližná poloha: ulice, ±300 m');
    expect(pinPrecisionLabel({ granularity_rank: RANK.obec, uncertainty_radius_m: 1000 }))
      .toBe('Přibližná poloha: obec, ±1 km');
    expect(
      pinPrecisionLabel({
        granularity_rank: RANK.cast_obce_or_quarter,
        uncertainty_radius_m: '750' as unknown as number,
      }),
    ).toBe('Přibližná poloha: část obce, ±750 m');
  });

  it('prints the TRUE radius where the drawn circle is capped', () => {
    expect(pinPrecisionLabel({ granularity_rank: 30, uncertainty_radius_m: 25_000 }))
      .toBe('Přibližná poloha: okres, ±25 km');
  });

  it('says exact for the building rungs, without a radius', () => {
    expect(pinPrecisionLabel({ granularity_rank: RANK.address_point, uncertainty_radius_m: 10 }))
      .toBe('Přesná poloha (adresní bod)');
    expect(pinPrecisionLabel({ granularity_rank: RANK.building, uncertainty_radius_m: 15 }))
      .toBe('Přesná poloha (budova)');
  });

  it('reads a rung inserted between two seeds as the coarser neighbour', () => {
    expect(pinPrecisionLabel({ granularity_rank: 45, uncertainty_radius_m: 900 }))
      .toBe('Přibližná poloha: obec, ±900 m');
  });

  it('drops the figure it does not have, and says so when there is no rung', () => {
    expect(pinPrecisionLabel({ granularity_rank: RANK.street, uncertainty_radius_m: null }))
      .toBe('Přibližná poloha: ulice');
    expect(pinPrecisionLabel({ granularity_rank: null, uncertainty_radius_m: null }))
      .toBe('Přesnost polohy neznámá');
  });
});
