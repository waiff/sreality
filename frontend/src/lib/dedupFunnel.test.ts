import { describe, expect, it } from 'vitest';
import {
  assembleFunnel,
  DEDUP_CALLED_FOR_STEP,
  isDedupCalledFor,
  pivotCostMatrix,
  summarizeCapture,
  type DedupCostByCategoryRow,
  type DedupEngineFlowRow,
  type DedupQueueRow,
  type DedupResolutionRow,
} from './dedupFunnel';

const res = (over: Partial<DedupResolutionRow>): DedupResolutionRow => ({
  source: 'engine', stage: 'phash', outcome: 'merged',
  category_main: 'byt', category_type: 'prodej',
  pairs_7d: 1, pairs_30d: 2, properties_7d: 2, properties_30d: 4,
  listings_7d: 2, listings_30d: 4,
  ...over,
});

const cost = (over: Partial<DedupCostByCategoryRow>): DedupCostByCategoryRow => ({
  called_for: 'compare_listings_visually',
  category_main: 'byt', category_type: 'prodej',
  calls_7d: 5, calls_30d: 10, cost_7d: 1, cost_30d: 2,
  listings_7d: 3, listings_30d: 6,
  ...over,
});

const FLOW = {
  eligible_market: 90000, flagged_location_market: 0, flagged_disposition_market: 0,
  runs_7d: 10, runs_30d: 40,
  pairs_considered_7d: 100, pairs_considered_30d: 400,
  rejected_7d: 20, rejected_30d: 80,
  queued_7d: 5, queued_30d: 15,
  clip_cosine_calls_7d: 50, clip_cosine_calls_30d: 200,
  routed_haiku_7d: 30, routed_haiku_30d: 120,
  routed_sonnet_7d: 10, routed_sonnet_30d: 40,
  floor_plan_deferred_7d: 1, floor_plan_deferred_30d: 4,
  clip_deferred_7d: 2, clip_deferred_30d: 8,
  skipped_unresolved_7d: 0, skipped_unresolved_30d: 0,
  vision_calls_7d: 40, vision_calls_30d: 160,
  vision_errors_7d: 3, vision_errors_30d: 12,
} as DedupEngineFlowRow;

describe('assembleFunnel', () => {
  const resolutions = [
    res({ stage: 'phash', outcome: 'merged', pairs_30d: 100 }),
    res({ stage: 'phash', outcome: 'dismissed', pairs_30d: 10, category_main: 'dum' }),
    res({ stage: 'visual', outcome: 'merged', pairs_30d: 7 }),
    res({ source: 'operator', stage: 'address', outcome: 'merged', pairs_30d: 3 }),
  ];
  const queue: DedupQueueRow[] = [
    { tier: 'street_disposition', category_main: 'byt', category_type: 'pronajem', pairs: 12 },
  ];
  const costs = [cost({ cost_30d: 20 }), cost({ called_for: 'classify_listing_images', cost_30d: 5 })];

  it('routes engine resolutions to their stage step and operator rows to the operator step', () => {
    const steps = assembleFunnel(resolutions, FLOW, queue, costs, 30);
    const byId = Object.fromEntries(steps.map((s) => [s.def.id, s]));
    expect(byId.phash.merged).toBe(100);
    expect(byId.phash.dismissed).toBe(10);
    expect(byId.visual.merged).toBe(7);
    // operator-sourced address row lands on the OPERATOR step, not address
    expect(byId.operator.merged).toBe(3);
    expect(byId.address.merged).toBe(0);
  });

  it('sums paid-lane cost into the owning step (visual = compare + classify)', () => {
    const steps = assembleFunnel(resolutions, FLOW, queue, costs, 30);
    const visual = steps.find((s) => s.def.id === 'visual')!;
    expect(visual.cost).toBe(25);
    expect(visual.calls).toBe(20);
  });

  it('exposes flow counters as evaluations and the open queue as an extra', () => {
    const steps = assembleFunnel(resolutions, FLOW, queue, costs, 30);
    const byId = Object.fromEntries(steps.map((s) => [s.def.id, s]));
    expect(byId.considered.evaluations).toBe(400);
    expect(byId.rejected.evaluations).toBe(80);
    expect(byId.clip.evaluations).toBe(200);
    expect(byId.queue.evaluations).toBe(15);
    expect(byId.queue.extras.find((e) => e.label === 'open now')?.value).toBe(12);
  });

  it('respects the window switch', () => {
    const steps = assembleFunnel(resolutions, FLOW, queue, costs, 7);
    const byId = Object.fromEntries(steps.map((s) => [s.def.id, s]));
    expect(byId.phash.merged).toBe(1); // pairs_7d default
    expect(byId.considered.evaluations).toBe(100);
  });
});

describe('summarizeCapture', () => {
  it('splits resolved pairs by step kind and totals paid cost', () => {
    const steps = assembleFunnel(
      [
        res({ stage: 'phash', outcome: 'merged', pairs_30d: 50 }),
        res({ stage: 'visual', outcome: 'merged', pairs_30d: 5 }),
        res({ source: 'operator', stage: 'operator', outcome: 'dismissed', pairs_30d: 2 }),
      ],
      FLOW,
      [],
      [cost({ cost_30d: 9 })],
      30,
    );
    const c = summarizeCapture(steps);
    expect(c.freeResolved).toBe(50);
    expect(c.paidResolved).toBe(5);
    expect(c.manualResolved).toBe(2);
    expect(c.paidCost).toBe(9);
  });
});

describe('pivotCostMatrix', () => {
  it('buckets categories, folds unknown types into ostatni, and totals', () => {
    const { matrix, total } = pivotCostMatrix(
      [
        cost({ category_main: 'byt', category_type: 'prodej', cost_30d: 10, calls_30d: 4 }),
        cost({ category_main: 'chata', category_type: 'drazba', cost_30d: 2, calls_30d: 1 }),
      ],
      30,
    );
    expect(matrix.byt.prodej.cost).toBe(10);
    expect(matrix.ostatni.ostatni.cost).toBe(2);
    expect(total.cost).toBe(12);
    expect(total.calls).toBe(5);
  });
});

describe('registry', () => {
  it('maps all four dedup called_for tags to paid steps with anchors', () => {
    for (const cf of [
      'compare_listings_visually',
      'classify_listing_images',
      'compare_listing_floor_plans',
      'compare_listing_site_plans',
    ]) {
      expect(isDedupCalledFor(cf)).toBe(true);
      expect(DEDUP_CALLED_FOR_STEP[cf].kind).toBe('paid');
      expect(DEDUP_CALLED_FOR_STEP[cf].anchor).toMatch(/^funnel-/);
    }
    expect(isDedupCalledFor('score_listing_condition')).toBe(false);
  });
});
