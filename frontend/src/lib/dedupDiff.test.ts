import { describe, expect, it } from 'vitest';

import {
  AREA_DIFF_MAX_M2,
  PRICE_DRIFT_MAX,
  TIGHT_RADIUS_M,
  clusterCandidates,
  diffCandidate,
  diffCluster,
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
      category_type: 'prodej', category_main: 'byt', category_sub_cb: 4,
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
      category_type: null, category_main: null, category_sub_cb: null,
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

describe('clusterCandidates', () => {
  it('merges transitive pairs (A-B, B-C) into one 3-member cluster', () => {
    const cands = [
      candidate({ id: 10, left_property: side({ property_id: 1 }), right_property: side({ property_id: 2 }) }),
      candidate({ id: 11, left_property: side({ property_id: 2 }), right_property: side({ property_id: 3 }) }),
    ];
    const clusters = clusterCandidates(cands);
    expect(clusters).toHaveLength(1);
    expect(clusters[0].members.map((m) => m.property_id)).toEqual([1, 2, 3]);
    expect(clusters[0].candidateIds).toEqual([10, 11]);
  });

  it('collapses the redundant fan-out (idnes X paired with two sreality rows) into one cluster', () => {
    // mirrors the screenshot: #129114 vs #32402 and #129114 vs #54628
    const cands = [
      candidate({ id: 1, left_property: side({ property_id: 32402 }), right_property: side({ property_id: 129114 }) }),
      candidate({ id: 2, left_property: side({ property_id: 54628 }), right_property: side({ property_id: 129114 }) }),
    ];
    const clusters = clusterCandidates(cands);
    expect(clusters).toHaveLength(1);
    expect(clusters[0].members).toHaveLength(3);
    expect(clusters[0].candidateIds).toEqual([1, 2]);
  });

  it('keeps unrelated pairs as separate clusters', () => {
    const cands = [
      candidate({ id: 1, left_property: side({ property_id: 1 }), right_property: side({ property_id: 2 }) }),
      candidate({ id: 2, left_property: side({ property_id: 8 }), right_property: side({ property_id: 9 }) }),
    ];
    const clusters = clusterCandidates(cands);
    expect(clusters).toHaveLength(2);
  });

  it('picks up the engine visual verdict from an edge', () => {
    const cands = [
      candidate({ id: 1, markers_matched: { verdict: 'Medium', rationale: 'similar kitchen', room_type: 'kitchen' } }),
    ];
    expect(clusterCandidates(cands)[0].visual).toEqual({
      verdict: 'Medium', rationale: 'similar kitchen', room: 'kitchen',
    });
  });
});

describe('diffCluster', () => {
  const noDetail = () => null;

  it('all-agree → match; one outlier → mismatch (N-way)', () => {
    const members = [
      side({ property_id: 1, area_m2: 100 }),
      side({ property_id: 2, area_m2: 100 }),
      side({ property_id: 3, area_m2: 100 }),
    ];
    const r = diffCluster(members, noDetail);
    expect(r.find((x) => x.key === 'area')!.verdict).toBe('match');
    expect(r.find((x) => x.key === 'area')!.values).toHaveLength(3);

    const outlier = diffCluster(
      [side({ property_id: 1, area_m2: 100 }), side({ property_id: 2, area_m2: 100 }), side({ property_id: 3, area_m2: 200 })],
      noDetail,
    );
    expect(outlier.find((x) => x.key === 'area')!.verdict).toBe('mismatch');
  });

  it('fewer than two known values → unknown', () => {
    const members = [side({ property_id: 1, district: 'Praha' }), side({ property_id: 2, district: null })];
    expect(diffCluster(members, noDetail).find((x) => x.key === 'district')!.verdict).toBe('unknown');
  });

  it('loose disposition equivalence agrees across the cluster', () => {
    const members = [
      side({ property_id: 1, disposition: '2+kk' }),
      side({ property_id: 2, disposition: '2+1' }),
    ];
    expect(diffCluster(members, noDetail).find((x) => x.key === 'disposition')!.verdict).toBe('match');
  });
});
