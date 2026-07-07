import { describe, expect, it } from 'vitest';
import {
  buildDailySeries,
  colorTokenFor,
  computeKpis,
  summarizeByFeature,
  summarizeByModel,
  type LlmCostDailyRow,
} from './llmCosts';

const NOW = new Date('2026-07-07T12:00:00Z');

const row = (over: Partial<LlmCostDailyRow>): LlmCostDailyRow => ({
  day: '2026-07-07',
  called_for: 'compare_listings_visually',
  provider: 'anthropic',
  model: 'claude-sonnet-4-5',
  calls: 10,
  error_calls: 0,
  cost_usd: 1,
  input_tokens: 1000,
  output_tokens: 100,
  cache_read_tokens: 0,
  cache_write_tokens: 0,
  ...over,
});

describe('computeKpis', () => {
  it('buckets today / 7d / prev-7d / 30d and projects a month from the 7d avg', () => {
    const rows = [
      row({ day: '2026-07-07', cost_usd: 10 }), // today, in 7d
      row({ day: '2026-07-03', cost_usd: 4 }),  // in 7d
      row({ day: '2026-06-27', cost_usd: 7 }),  // prev 7d, in 30d
      row({ day: '2026-06-08', cost_usd: 100 }), // in 30d only
      row({ day: '2026-05-01', cost_usd: 999 }), // outside every window
    ];
    const k = computeKpis(rows, NOW);
    expect(k.today).toBe(10);
    expect(k.last7).toBe(14);
    expect(k.prev7).toBe(7);
    expect(k.last30).toBe(121);
    expect(k.projectedMonth).toBeCloseTo((14 / 7) * 30);
  });

  it('sums calls and errors over the 7-day window only', () => {
    const rows = [
      row({ day: '2026-07-06', calls: 100, error_calls: 5 }),
      row({ day: '2026-06-20', calls: 900, error_calls: 90 }),
    ];
    const k = computeKpis(rows, NOW);
    expect(k.calls7).toBe(100);
    expect(k.errors7).toBe(5);
  });
});

describe('buildDailySeries', () => {
  it('zero-fills missing days and folds beyond-top features into other', () => {
    const rows = [
      row({ day: '2026-07-06', called_for: 'a', cost_usd: 50 }),
      row({ day: '2026-07-06', called_for: 'b', cost_usd: 40 }),
      row({ day: '2026-07-06', called_for: 'c', cost_usd: 3 }),
    ];
    const s = buildDailySeries(rows, NOW, 5, 2);
    expect(s.data).toHaveLength(5); // continuous window, gaps zero-filled
    expect(s.features).toEqual(['a', 'b', 'other']);
    const day6 = s.data.find((d) => d.day === '2026-07-06')!;
    expect(day6.a).toBe(50);
    expect(day6.other).toBe(3);
    const day5 = s.data.find((d) => d.day === '2026-07-05')!;
    expect(day5.a).toBe(0);
  });

  it('keeps canonical features in fixed stack order regardless of rank', () => {
    const rows = [
      row({ day: '2026-07-06', called_for: 'classify_listing_images', cost_usd: 99 }),
      row({ day: '2026-07-06', called_for: 'compare_listings_visually', cost_usd: 1 }),
    ];
    const s = buildDailySeries(rows, NOW, 3, 6);
    expect(s.features).toEqual(['compare_listings_visually', 'classify_listing_images']);
  });
});

describe('colorTokenFor', () => {
  it('is entity-stable for known features and deterministic for unknown ones', () => {
    expect(colorTokenFor('compare_listings_visually')).toBe('--color-tag-ochre');
    expect(colorTokenFor('other')).toBe('--color-ink-3');
    expect(colorTokenFor('brand_new_feature')).toBe(colorTokenFor('brand_new_feature'));
  });
});

describe('summarizeByFeature / summarizeByModel', () => {
  it('computes shares, per-call averages, and model lists over 30d', () => {
    const rows = [
      row({ day: '2026-07-06', called_for: 'x', calls: 10, cost_usd: 30, model: 'm1' }),
      row({ day: '2026-06-20', called_for: 'x', calls: 5, cost_usd: 10, model: 'm2' }),
      row({ day: '2026-07-06', called_for: 'y', calls: 4, cost_usd: 60, model: 'm1' }),
    ];
    const feats = summarizeByFeature(rows, NOW);
    expect(feats[0].feature).toBe('y');
    const x = feats.find((f) => f.feature === 'x')!;
    expect(x.cost30).toBe(40);
    expect(x.cost7).toBe(30);
    expect(x.avgPerCall7).toBe(3);
    expect(x.models).toEqual(['m1', 'm2']);
    expect(x.share30).toBeCloseTo(0.4);

    const models = summarizeByModel(rows, NOW);
    expect(models[0].model).toBe('m1');
    expect(models[0].cost30).toBe(90);
    expect(models[0].share30).toBeCloseTo(0.9);
  });
});
