import { QueryClient } from '@tanstack/react-query';
import { describe, expect, it } from 'vitest';

import { reflectDecisionFeedback } from './dedupFeedbackCache';
import type { DecisionFeedback, DedupCandidate, DedupPropertySide } from './types';

/* reflectDecisionFeedback is the optimistic-cache patch behind the "Nesprávné" flag: it
 * must flip the flag on both surfaces the instant a save returns, matched by the canonical
 * (low, high) property pair regardless of the order a cached row stores it in. */

const FLAG: DecisionFeedback = {
  is_incorrect: true,
  expected_outcome: 'should_dismiss',
  note: 'wrong',
  updated_at: null,
};

function auditRow(left: number | null, right: number | null) {
  return {
    audit_id: left != null && right != null ? left * 1000 + right : 0,
    run_at: '2026-07-08T00:00:00Z',
    left_sreality_id: null,
    right_sreality_id: null,
    left_property_id: left,
    right_property_id: right,
    category_main: 'byt',
    stage: 'visual',
    outcome: 'merged',
    source: 'engine' as const,
    merge_group_id: null,
    detail: null,
    undone: false,
    feedback: null as DecisionFeedback | null,
    audit_breakdown: [],
  };
}

function side(propertyId: number): DedupPropertySide {
  return { property_id: propertyId } as DedupPropertySide;
}

function candidate(id: number, left: number, right: number): DedupCandidate {
  return {
    id,
    tier: 'street_disposition',
    status: 'proposed',
    confidence: null,
    markers_matched: null,
    auto_merged: false,
    merge_group_id: null,
    created_at: '2026-07-08T00:00:00Z',
    reviewed_at: null,
    left_property: side(left),
    right_property: side(right),
    feedback: null,
  };
}

function client() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

describe('reflectDecisionFeedback', () => {
  it('flags the matching audit row and leaves others untouched', () => {
    const qc = client();
    qc.setQueryData(['dedup', 'audit', 'merged', '', '', '', null, '', null], {
      data: [auditRow(76470, 135912), auditRow(1, 2)],
      total: 2,
      returned: 2,
    });

    reflectDecisionFeedback(qc, 76470, 135912, FLAG);

    const rows = qc.getQueryData<{ data: ReturnType<typeof auditRow>[] }>([
      'dedup', 'audit', 'merged', '', '', '', null, '', null,
    ])!.data;
    expect(rows[0].feedback).toEqual(FLAG);
    expect(rows[1].feedback).toBeNull();
  });

  it('matches by canonical pair regardless of the stored left/right order', () => {
    const qc = client();
    // The audit row stores the pair in NON-canonical order (135912 > 76470), exactly as
    // dedup_pair_audit does — the patch must still find it.
    qc.setQueryData(['dedup', 'audit', 'x'], {
      data: [auditRow(135912, 76470)],
      total: 1,
      returned: 1,
    });

    reflectDecisionFeedback(qc, 76470, 135912, FLAG);

    const rows = qc.getQueryData<{ data: ReturnType<typeof auditRow>[] }>([
      'dedup', 'audit', 'x',
    ])!.data;
    expect(rows[0].feedback).toEqual(FLAG);
  });

  it('flags the matching candidate row (Needs-review surface) too', () => {
    const qc = client();
    qc.setQueryData(['dedup', 'candidates', { status: 'proposed' }], {
      data: [candidate(10, 135912, 76470), candidate(11, 3, 4)],
      total: 2,
      returned: 2,
    });

    reflectDecisionFeedback(qc, 76470, 135912, FLAG);

    const rows = qc.getQueryData<{ data: DedupCandidate[] }>([
      'dedup', 'candidates', { status: 'proposed' },
    ])!.data;
    expect(rows[0].feedback).toEqual(FLAG);
    expect(rows[1].feedback).toBeNull();
  });

  it('patches every cached view of the two families at once', () => {
    const qc = client();
    qc.setQueryData(['dedup', 'audit', 'a'], { data: [auditRow(5, 9)], total: 1, returned: 1 });
    qc.setQueryData(['dedup', 'audit', 'b'], { data: [auditRow(9, 5)], total: 1, returned: 1 });

    reflectDecisionFeedback(qc, 5, 9, FLAG);

    expect(qc.getQueryData<{ data: ReturnType<typeof auditRow>[] }>(['dedup', 'audit', 'a'])!.data[0].feedback).toEqual(FLAG);
    expect(qc.getQueryData<{ data: ReturnType<typeof auditRow>[] }>(['dedup', 'audit', 'b'])!.data[0].feedback).toEqual(FLAG);
  });

  it('clears the flag when passed null (the un-flag path)', () => {
    const qc = client();
    qc.setQueryData(['dedup', 'audit', 'a'], {
      data: [{ ...auditRow(5, 9), feedback: FLAG }],
      total: 1,
      returned: 1,
    });

    reflectDecisionFeedback(qc, 5, 9, null);

    expect(qc.getQueryData<{ data: ReturnType<typeof auditRow>[] }>(['dedup', 'audit', 'a'])!.data[0].feedback).toBeNull();
  });

  it('preserves object identity for unchanged rows (structural sharing)', () => {
    const qc = client();
    const untouched = auditRow(1, 2);
    qc.setQueryData(['dedup', 'audit', 'a'], {
      data: [auditRow(5, 9), untouched],
      total: 2,
      returned: 2,
    });

    reflectDecisionFeedback(qc, 5, 9, FLAG);

    const rows = qc.getQueryData<{ data: ReturnType<typeof auditRow>[] }>(['dedup', 'audit', 'a'])!.data;
    expect(rows[1]).toBe(untouched);
  });
});
