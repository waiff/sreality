import { describe, expect, it } from 'vitest';

import {
  type BakeoffRow,
  costPerCall,
  distinctCategories,
  distinctModels,
  filterPairs,
  groupPairs,
  isReviewRun,
  summarize,
  verdictGloss,
} from './bakeoff';

function row(p: Partial<BakeoffRow>): BakeoffRow {
  return {
    id: 1,
    run_label: 'rl',
    set_name: 'gs',
    check_type: 'precision',
    lane: 'site_plan',
    model: 'qwen',
    sreality_id_a: 1,
    sreality_id_b: 2,
    room_type: 'site_plan',
    is_same: false,
    label_source: 'engine_site_plan_verdict',
    category_main: 'pozemek',
    expected_verdict: null,
    danger_verdict: 'same_unit',
    candidate_verdict: 'different_unit',
    is_correct: true,
    is_dangerous: false,
    cost_usd: 0.01,
    created_at: '2026-07-13T16:00:00Z',
    ...p,
  };
}

describe('summarize', () => {
  it('splits recall vs precision per (model, lane) and computes pct', () => {
    const rows = [
      row({ model: 'qwen', lane: 'site_plan', check_type: 'precision', is_correct: true }),
      row({ model: 'qwen', lane: 'site_plan', check_type: 'precision', is_correct: false, sreality_id_a: 3 }),
      row({ model: 'qwen', lane: 'site_plan', check_type: 'recall', is_correct: true, sreality_id_a: 4 }),
    ];
    const q = summarize(rows).get('qwen')!;
    expect(q.lanes.site_plan.precision.n).toBe(2);
    expect(q.lanes.site_plan.precision.correct).toBe(1);
    expect(q.lanes.site_plan.precision.pct).toBe(0.5);
    expect(q.lanes.site_plan.recall.n).toBe(1);
    expect(q.lanes.site_plan.recall.pct).toBe(1);
  });

  it('leaves an unevaluated cell at n=0 / pct=null', () => {
    const s = summarize([row({ model: 'sonnet', lane: 'compare', check_type: 'recall' })]).get('sonnet')!;
    expect(s.lanes.floor_plan.precision.n).toBe(0);
    expect(s.lanes.floor_plan.precision.pct).toBeNull();
  });

  it('accumulates per-model cost and reports $/call', () => {
    const rows = [
      row({ model: 'qwen', cost_usd: 0.02, check_type: 'recall' }),
      row({ model: 'qwen', cost_usd: 0.04, check_type: 'recall', sreality_id_a: 7 }),
      row({ model: 'qwen', cost_usd: null, check_type: 'recall', sreality_id_a: 8 }),
    ];
    const q = summarize(rows).get('qwen')!;
    expect(q.totalCostUsd).toBeCloseTo(0.06);
    expect(q.callCount).toBe(2); // the null-cost row doesn't count toward $/call
    expect(costPerCall(q)).toBeCloseTo(0.03);
  });

  it('tallies would-merge votes for review rows (no correctness)', () => {
    const rows = [
      row({ model: 'qwen', lane: 'site_plan', check_type: 'review', is_same: null, is_correct: null, is_dangerous: true }),
      row({ model: 'qwen', lane: 'site_plan', check_type: 'review', is_same: null, is_correct: null, is_dangerous: false, sreality_id_a: 3 }),
    ];
    const q = summarize(rows).get('qwen')!;
    expect(q.lanes.site_plan.review.n).toBe(2);
    expect(q.lanes.site_plan.review.mergeVotes).toBe(1);
    expect(q.lanes.site_plan.review.pct).toBe(0.5);
    // review rows don't inflate recall/precision
    expect(q.lanes.site_plan.precision.n).toBe(0);
  });
});

describe('isReviewRun', () => {
  it('true only when every row is a review row', () => {
    expect(isReviewRun([row({ check_type: 'review' })])).toBe(true);
    expect(isReviewRun([row({ check_type: 'review' }), row({ check_type: 'precision', sreality_id_a: 3 })])).toBe(false);
    expect(isReviewRun([])).toBe(false);
  });
});

describe('groupPairs', () => {
  it('groups by (a,b) and indexes rows by model|lane', () => {
    const rows = [
      row({ model: 'qwen', lane: 'site_plan', candidate_verdict: 'same_unit', is_dangerous: true }),
      row({ model: 'sonnet', lane: 'site_plan', candidate_verdict: 'different_unit' }),
    ];
    const groups = groupPairs(rows);
    expect(groups).toHaveLength(1);
    const g = groups[0];
    expect(g.byModelLane.get('qwen|site_plan')!.candidate_verdict).toBe('same_unit');
    expect(g.byModelLane.get('sonnet|site_plan')!.candidate_verdict).toBe('different_unit');
    expect(g.anyDangerous).toBe(true);
    expect(g.hasDisagreement).toBe(true); // qwen vs sonnet differ on site_plan
  });

  it('anyDangerous ignores a merge verdict on a SAME-property pair (compare recall reproducing High)', () => {
    // is_same=true, candidate High == danger verdict, but this is a CORRECT recall reproduction,
    // not a false merge — must not flag the pair dangerous.
    const rows = [
      row({
        check_type: 'recall',
        lane: 'compare',
        is_same: true,
        candidate_verdict: 'High',
        danger_verdict: 'High',
        is_dangerous: true,
        is_correct: true,
      }),
    ];
    expect(groupPairs(rows)[0].anyDangerous).toBe(false);
  });

  it('anyDangerous flags a merge verdict on a DIFFERENT pair', () => {
    const rows = [
      row({ is_same: false, candidate_verdict: 'same_unit', is_dangerous: true, is_correct: false }),
    ];
    expect(groupPairs(rows)[0].anyDangerous).toBe(true);
  });

  it('marks no disagreement when all models agree on every lane', () => {
    const rows = [
      row({ model: 'qwen', lane: 'site_plan', candidate_verdict: 'different_unit' }),
      row({ model: 'sonnet', lane: 'site_plan', candidate_verdict: 'different_unit' }),
    ];
    expect(groupPairs(rows)[0].hasDisagreement).toBe(false);
  });

  it('backfills pair metadata from a precision row when a recall row left it null', () => {
    const rows = [
      row({ check_type: 'recall', category_main: null, label_source: null, is_same: null, lane: 'compare' }),
      row({ check_type: 'precision', category_main: 'dum', label_source: 'operator_dismissal', is_same: false }),
    ];
    const g = groupPairs(rows)[0];
    expect(g.category_main).toBe('dum');
    expect(g.label_source).toBe('operator_dismissal');
    expect(g.is_same).toBe(false);
  });
});

describe('filterPairs', () => {
  const base = groupPairs([
    row({ sreality_id_a: 1, category_main: 'pozemek', lane: 'site_plan', is_dangerous: true, model: 'qwen', candidate_verdict: 'same_unit' }),
    row({ sreality_id_a: 1, category_main: 'pozemek', lane: 'site_plan', model: 'sonnet', candidate_verdict: 'different_unit' }),
    row({ sreality_id_a: 9, category_main: 'byt', lane: 'compare', is_dangerous: false, model: 'qwen', candidate_verdict: 'Low' }),
    row({ sreality_id_a: 9, category_main: 'byt', lane: 'compare', model: 'sonnet', candidate_verdict: 'Low' }),
  ]);

  it('dangerousOnly keeps only pairs where a model emitted the danger verdict', () => {
    const out = filterPairs(base, { lane: 'all', checkType: 'all', category: 'all', disagreementsOnly: false, dangerousOnly: true });
    expect(out.map((p) => p.a)).toEqual([1]);
  });

  it('disagreementsOnly keeps only pairs where models differ', () => {
    const out = filterPairs(base, { lane: 'all', checkType: 'all', category: 'all', disagreementsOnly: true, dangerousOnly: false });
    expect(out.map((p) => p.a)).toEqual([1]); // pair 9: both said Low
  });

  it('category + lane filters narrow correctly', () => {
    const out = filterPairs(base, { lane: 'compare', checkType: 'all', category: 'byt', disagreementsOnly: false, dangerousOnly: false });
    expect(out.map((p) => p.a)).toEqual([9]);
  });
});

describe('distinct helpers', () => {
  it('lists sorted models and categories', () => {
    const rows = [row({ model: 'sonnet' }), row({ model: 'qwen', sreality_id_a: 5, category_main: 'byt' })];
    expect(distinctModels(rows)).toEqual(['qwen', 'sonnet']);
    expect(distinctCategories(groupPairs(rows))).toEqual(['byt', 'pozemek']);
  });
});

describe('verdictGloss', () => {
  it('maps known verdicts to plain language and falls back to the raw value', () => {
    expect(verdictGloss('same_unit')).toContain('MERGE');
    expect(verdictGloss('different_unit')).toContain('keeps apart');
    expect(verdictGloss('High')).toContain('same room');
    expect(verdictGloss('mystery_verdict')).toBe('mystery_verdict');
  });
});
