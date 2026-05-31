import { describe, expect, it } from 'vitest';

import {
  AREA_DIFF_MAX_M2,
  PRICE_DRIFT_MAX,
  TIGHT_RADIUS_M,
  diffCandidate,
  type ListingDetailLite,
} from './dedupDiff';
import type { DedupCandidate, DedupPropertySide } from './types';

function side(p: Partial<DedupPropertySide>): DedupPropertySide {
  return {
    property_id: 1, status: 'active', sreality_id: 1, price_czk: 1_000_000,
    area_m2: 50, disposition: '2+kk', district: 'Praha', category_main: 'byt',
    category_type: 'prodej', source_count: 1, distinct_site_count: 1,
    first_seen_at: null, lat: null, lng: null, ...p,
  };
}

function candidate(p: Partial<DedupCandidate>): DedupCandidate {
  return {
    id: 1, tier: 'tier2', status: 'proposed', confidence: 0.6,
    markers_matched: { distance_m: 10 }, auto_merged: false, merge_group_id: null,
    created_at: '', reviewed_at: null,
    left_property: side({ property_id: 1 }),
    right_property: side({ property_id: 2 }),
    ...p,
  };
}

const row = (rows: ReturnType<typeof diffCandidate>, key: string) =>
  rows.find((r) => r.key === key)!;

describe('diffCandidate', () => {
  it('price within ±2% (matcher AUTO_PRICE_DRIFT_MAX) matches', () => {
    expect(PRICE_DRIFT_MAX).toBe(0.02);
    const c = candidate({
      left_property: side({ price_czk: 1_000_000 }),
      right_property: side({ price_czk: 1_020_000 }), // exactly 2%
    });
    expect(row(diffCandidate(c), 'price').verdict).toBe('match');
  });

  it('price beyond ±2% mismatches', () => {
    const c = candidate({
      left_property: side({ price_czk: 1_000_000 }),
      right_property: side({ price_czk: 1_050_000 }), // 5%
    });
    expect(row(diffCandidate(c), 'price').verdict).toBe('mismatch');
  });

  it('area within ±2 m² (matcher AUTO_AREA_DIFF_MAX_M2) matches', () => {
    expect(AREA_DIFF_MAX_M2).toBe(2.0);
    const c = candidate({
      left_property: side({ area_m2: 50 }),
      right_property: side({ area_m2: 52 }),
    });
    expect(row(diffCandidate(c), 'area').verdict).toBe('match');
  });

  it('area beyond ±2 m² mismatches', () => {
    const c = candidate({
      left_property: side({ area_m2: 50 }),
      right_property: side({ area_m2: 55 }),
    });
    expect(row(diffCandidate(c), 'area').verdict).toBe('mismatch');
  });

  it('loose-equivalent dispositions (2+kk ≈ 2+1) match', () => {
    const c = candidate({
      left_property: side({ disposition: '2+kk' }),
      right_property: side({ disposition: '2+1' }),
    });
    expect(row(diffCandidate(c), 'disposition').verdict).toBe('match');
  });

  it('non-equivalent dispositions (2+kk vs 3+kk) mismatch', () => {
    const c = candidate({
      left_property: side({ disposition: '2+kk' }),
      right_property: side({ disposition: '3+kk' }),
    });
    expect(row(diffCandidate(c), 'disposition').verdict).toBe('mismatch');
  });

  it('district is diacritics/case-insensitive', () => {
    const c = candidate({
      left_property: side({ district: 'Praha' }),
      right_property: side({ district: 'praha' }),
    });
    expect(row(diffCandidate(c), 'district').verdict).toBe('match');
  });

  it('street comes from the detail rows; null → unknown', () => {
    const c = candidate({});
    expect(row(diffCandidate(c), 'street').verdict).toBe('unknown');

    const left: ListingDetailLite = {
      sreality_id: 1, street: 'Hlavní', house_number: '12/3', floor: 2,
      disposition: '2+kk', district: 'Praha', price_czk: 1, area_m2: 1,
    };
    const right: ListingDetailLite = { ...left, sreality_id: 2 };
    const rows = diffCandidate(c, left, right);
    expect(row(rows, 'street').verdict).toBe('match');
    expect(row(rows, 'street').a).toBe('Hlavní 12/3');
    expect(row(rows, 'floor').verdict).toBe('match');
  });

  it('floor mismatch when detail floors differ', () => {
    const c = candidate({});
    const left: ListingDetailLite = {
      sreality_id: 1, street: null, house_number: null, floor: 2,
      disposition: null, district: null, price_czk: null, area_m2: null,
    };
    const right: ListingDetailLite = { ...left, sreality_id: 2, floor: 5 };
    expect(row(diffCandidate(c, left, right), 'floor').verdict).toBe('mismatch');
  });

  it('distance: tight (≤30 m) matches, far mismatches, tier1 labelled ≤20 m', () => {
    expect(TIGHT_RADIUS_M).toBe(30);
    expect(row(diffCandidate(candidate({ markers_matched: { distance_m: 10 } })), 'distance').verdict).toBe('match');
    expect(row(diffCandidate(candidate({ markers_matched: { distance_m: 90 } })), 'distance').verdict).toBe('mismatch');
    const t1 = candidate({ tier: 'tier1', markers_matched: {} });
    expect(row(diffCandidate(t1), 'distance').a).toBe('≤ 20 m apart');
  });

  it('returns the fixed row order', () => {
    expect(diffCandidate(candidate({})).map((r) => r.key)).toEqual([
      'price', 'area', 'disposition', 'street', 'floor', 'district', 'distance',
    ]);
  });
});
