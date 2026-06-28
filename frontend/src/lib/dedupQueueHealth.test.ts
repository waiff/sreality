import { describe, expect, it } from 'vitest';
import { assessDirtyQueue, DIRTY_QUEUE_WARN_DEPTH } from './dedupQueueHealth';
import type { DedupEngineRun } from './queries';

// Minimal run row — only the fields assessDirtyQueue reads matter; the rest are filler.
function run(depth: number | null, startedAt = '2026-06-28T00:00:00Z'): DedupEngineRun {
  return {
    id: 1, started_at: startedAt, ended_at: startedAt, eligible: 0, flagged_location: 0,
    flagged_disposition: 0, pairs_considered: 0, rejected: 0, auto_address: 0, auto_phash: 0,
    auto_visual: 0, queued: 0, vision_calls: 0, cost_usd: 0, auto_dismissed: 0,
    floor_plan_deferred: 0, clip_deferred: 0, dirty_queue_depth: depth, dirty_claimed: depth == null ? null : 10000,
  };
}

describe('assessDirtyQueue', () => {
  it('idle when no dirty runs', () => {
    expect(assessDirtyQueue([run(null), run(null)]).status).toBe('idle');
  });

  it('ok when depth is below the warn threshold', () => {
    expect(assessDirtyQueue([run(500), run(800)]).status).toBe('ok');
  });

  it('warn (not fail) when high but DRAINING — newest below oldest in window', () => {
    // newest-first: 40k, 60k, 90k, 120k -> falling, so transient flood
    const h = assessDirtyQueue([run(40_000), run(60_000), run(90_000), run(120_000)]);
    expect(h.status).toBe('warn');
    expect(h.draining).toBe(true);
  });

  it('fail when high and NOT draining (stuck / growing)', () => {
    // newest-first: 130k, 125k, 120k -> latest is ABOVE the oldest in window => not draining
    const h = assessDirtyQueue([run(130_000), run(125_000), run(120_000)]);
    expect(h.status).toBe('fail');
    expect(h.draining).toBe(false);
  });

  it('warn when high with only one dirty run (trend unknown, do not fail prematurely)', () => {
    const h = assessDirtyQueue([run(DIRTY_QUEUE_WARN_DEPTH + 1)]);
    expect(h.status).toBe('warn');
    expect(h.draining).toBeNull();
  });

  it('ignores non-dirty (full-scan) rows when finding the latest depth', () => {
    const h = assessDirtyQueue([run(null), run(800)]);
    expect(h.depth).toBe(800);
    expect(h.status).toBe('ok');
  });
});
