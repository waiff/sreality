import { describe, it, expect } from 'vitest';
import {
  BUILDING_GRANULARITY_RANK,
  uncertaintyCircleRadiusM,
  uncertaintyPixelsAtZoom0,
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
