import { describe, expect, it } from 'vitest';
import {
  assessDirtyQueue,
  DIRTY_QUEUE_WARN_DEPTH,
  DIRTY_QUEUE_STARVE_AGE_SECONDS,
} from './dedupQueueHealth';
import type { DedupEngineRun } from './queries';

// Minimal run row — only the fields assessDirtyQueue reads matter; the rest are filler.
function run(
  depth: number | null,
  opts: {
    cleared?: number | null;
    truncated?: number | null;
    startedAt?: string;
    ageP95?: number | null;
  } = {},
): DedupEngineRun {
  const {
    cleared = null, truncated = null, startedAt = '2026-06-28T00:00:00Z', ageP95 = null,
  } = opts;
  return {
    id: 1, started_at: startedAt, ended_at: startedAt, eligible: 0, flagged_location: 0,
    flagged_disposition: 0, pairs_considered: 0, rejected: 0, auto_address: 0, auto_phash: 0,
    auto_visual: 0, queued: 0, vision_calls: 0, auto_dismissed: 0,
    floor_plan_deferred: 0, clip_deferred: 0,
    dirty_queue_depth: depth, dirty_claimed: depth == null ? null : 3000,
    dirty_cleared: depth == null ? null : cleared,
    dirty_truncated: depth == null ? null : truncated,
    run_kind: depth == null ? 'full' : 'dirty', truncated: truncated ?? 0,
    skipped_unresolved: null, skipped_oversized: null, oversized_groups: null,
    vision_errors: null, truncated_cause: null, scan_groups_total: null,
    scan_groups_scanned: null, dirty_age_p95_seconds: ageP95, dirty_pruned: null, runner: null,
  };
}

describe('assessDirtyQueue', () => {
  it('idle when no dirty runs', () => {
    expect(assessDirtyQueue([run(null), run(null)]).status).toBe('idle');
  });

  it('ok when depth is below the warn threshold and runs are clearing', () => {
    const h = assessDirtyQueue([run(500, { cleared: 300, truncated: 0 }), run(800, { cleared: 500, truncated: 0 })]);
    expect(h.status).toBe('ok');
    expect(h.draining).toBe(true);
    expect(h.clearedInWindow).toBe(800);
    expect(h.livelocked).toBe(false);
  });

  it('DRAINING means cleared>0 — a falling depth with zero cleared is NOT draining (TTL eviction)', () => {
    // The 2026-07 production failure mode: depth 26k -> 17k purely via the 24h TTL prune
    // while every run truncated with cleared=0. The old depth-trend heuristic called this
    // "draining"; it must read as a livelocked FAIL even below the warn depth.
    const h = assessDirtyQueue([
      run(15_000, { cleared: 0, truncated: 1 }),
      run(18_000, { cleared: 0, truncated: 1 }),
      run(22_000, { cleared: 0, truncated: 1 }),
      run(26_000, { cleared: 0, truncated: 1 }),
    ]);
    expect(h.draining).toBe(false);
    expect(h.livelocked).toBe(true);
    expect(h.status).toBe('fail');
    expect(h.reason).toMatch(/livelocked/);
  });

  it('truncated runs that still clear are healthy (per-group incremental clear)', () => {
    // Post-#670: a budget-cut run clears the groups it finished. Truncation with
    // real head-advance is a working (if slow) drain, not a livelock.
    const h = assessDirtyQueue([
      run(9_000, { cleared: 1200, truncated: 1 }),
      run(10_000, { cleared: 900, truncated: 1 }),
    ]);
    expect(h.livelocked).toBe(false);
    expect(h.draining).toBe(true);
    expect(h.status).toBe('ok');
  });

  it('warn when high but genuinely draining (cleared>0)', () => {
    const h = assessDirtyQueue([
      run(40_000, { cleared: 3000, truncated: 1 }),
      run(60_000, { cleared: 3000, truncated: 1 }),
      run(90_000, { cleared: 3000, truncated: 1 }),
    ]);
    expect(h.status).toBe('warn');
    expect(h.draining).toBe(true);
  });

  it('fail when high and not draining (cleared==0 but not a full truncated streak)', () => {
    // Mixed window (one run completed but cleared nothing => not livelock-shaped),
    // depth high and no head-advance => still a depth-based fail.
    const h = assessDirtyQueue([
      run(130_000, { cleared: 0, truncated: 0 }),
      run(125_000, { cleared: 0, truncated: 1 }),
      run(120_000, { cleared: 0, truncated: 1 }),
    ]);
    expect(h.status).toBe('fail');
    expect(h.draining).toBe(false);
  });

  it('warn when high with only one dirty run (trend unknown, do not fail prematurely)', () => {
    const h = assessDirtyQueue([run(DIRTY_QUEUE_WARN_DEPTH + 1, { cleared: 0, truncated: 1 })]);
    expect(h.status).toBe('warn');
    expect(h.livelocked).toBe(false); // one run is not a streak
    expect(h.draining).toBeNull(); // a single run is never judged...
    expect(h.reason).toMatch(/not enough recent run data/); // ...and the banner must not claim draining
  });

  it('falls back to the depth trend on pre-258 rows lacking dirty_cleared', () => {
    // Legacy rows: cleared is null everywhere -> clearedInWindow null -> depth trend drives.
    const h = assessDirtyQueue([
      run(40_000, { cleared: null, truncated: null }),
      run(60_000, { cleared: null, truncated: null }),
    ]);
    expect(h.clearedInWindow).toBeNull();
    expect(h.draining).toBe(true); // 40k < 60k
    expect(h.status).toBe('warn');
    expect(h.livelocked).toBe(false);
    // depth-trend "draining" cannot be distinguished from TTL eviction, so the
    // operator-facing reason stays neutral rather than claiming drainage.
    expect(h.reason).toMatch(/not enough recent run data/);
  });

  it('ignores non-dirty (full-scan) rows when finding the latest depth', () => {
    const h = assessDirtyQueue([run(null), run(800, { cleared: 100, truncated: 0 })]);
    expect(h.depth).toBe(800);
    expect(h.status).toBe('ok');
  });

  it('STARVING: p95 wait past 24h is a fail even at low depth with runs clearing', () => {
    // Migration 271: the real-time "merge within minutes" lane. A day-old p95 means the old
    // tail isn't served — a fail regardless of a low, draining depth.
    const h = assessDirtyQueue([
      run(500, { cleared: 300, truncated: 0, ageP95: DIRTY_QUEUE_STARVE_AGE_SECONDS + 3_600 }),
      run(600, { cleared: 300, truncated: 0, ageP95: 80_000 }),
    ]);
    expect(h.agePctl95Seconds).toBe(DIRTY_QUEUE_STARVE_AGE_SECONDS + 3_600);
    expect(h.starving).toBe(true);
    expect(h.status).toBe('fail');
    expect(h.reason).toMatch(/starving/);
  });

  it('does NOT flag starving when the p95 wait is under 24h', () => {
    const h = assessDirtyQueue([
      run(500, { cleared: 300, truncated: 0, ageP95: 3_600 }),
      run(600, { cleared: 300, truncated: 0, ageP95: 3_000 }),
    ]);
    expect(h.starving).toBe(false);
    expect(h.status).toBe('ok');
  });

  it('starving is null-safe on pre-271 rows lacking the age gauge', () => {
    const h = assessDirtyQueue([run(500, { cleared: 300 }), run(600, { cleared: 200 })]);
    expect(h.agePctl95Seconds).toBeNull();
    expect(h.starving).toBe(false);
    expect(h.status).toBe('ok');
  });

  it('livelock takes precedence over starving in the reason', () => {
    // Both conditions true -> livelock is the root cause and owns the message.
    const h = assessDirtyQueue([
      run(15_000, { cleared: 0, truncated: 1, ageP95: 200_000 }),
      run(18_000, { cleared: 0, truncated: 1, ageP95: 200_000 }),
    ]);
    expect(h.livelocked).toBe(true);
    expect(h.starving).toBe(true);
    expect(h.status).toBe('fail');
    expect(h.reason).toMatch(/livelocked/);
  });
});
