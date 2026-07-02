import type { DedupEngineRun } from './queries';

/* Health of the real-time dedup --dirty drain, derived from recent run rows. ONE source of
 * truth shared by the /dedup automation dashboard and the Health page so the two never disagree.
 *
 * The PRIMARY signal is dirty_cleared (migration 258): how many claimed properties each run
 * actually removed from dedup_dirty_properties. Depth alone lies — the 24h TTL prune deletes
 * stale rows whether or not any run processed them, so a falling depth can be pure eviction
 * over a livelocked drain (exactly the failure mode observed in 2026-07: 7 of 8 runs truncated
 * with cleared=0 while the depth "drained"). So: DRAINING means cleared>0 in the recent window
 * (real head-advance), and a truncated streak with zero cleared is a FAIL regardless of depth.
 * A flood (a portal launch enqueues most of the market) is still EXPECTED and self-limiting —
 * the claim is NEWEST-FIRST + TTL-evicted, so it shows as high depth while cleared stays
 * positive (warn, transient). Pre-258 rows lack cleared; the depth trend is the fallback. */

export type DirtyQueueStatus = 'ok' | 'warn' | 'fail' | 'idle';

// A depth below this is normal churn (steady-state inflow is well under the per-run bound).
export const DIRTY_QUEUE_WARN_DEPTH = 20_000;
// How many recent dirty runs to judge the trend over.
export const DIRTY_QUEUE_TREND_WINDOW = 4;

export interface DirtyQueueHealth {
  status: DirtyQueueStatus;
  depth: number | null; // latest dirty run's queue depth
  claimed: number | null; // latest dirty run's claimed slice
  draining: boolean | null; // real head-advance (cleared>0 in window)? null = not enough data
  /* sum of dirty_cleared over the trend window; null when no run in the window carries it
   * (pre-258 rows) — the depth-trend fallback then drives `draining`. */
  clearedInWindow: number | null;
  /* every run in the window truncated with zero cleared — the livelock signature. */
  livelocked: boolean;
  /* operator-facing one-liner explaining the status, ready for the banner. */
  reason: string | null;
  recentDepths: number[]; // oldest -> newest, for a sparkline
  lastDirtyAt: string | null;
}

/* `runs` is newest-first (as fetchDedupEngineRuns returns). */
export function assessDirtyQueue(runs: DedupEngineRun[]): DirtyQueueHealth {
  const dirty = runs.filter((r) => r.dirty_queue_depth != null);
  if (dirty.length === 0) {
    return {
      status: 'idle', depth: null, claimed: null, draining: null,
      clearedInWindow: null, livelocked: false, reason: null,
      recentDepths: [], lastDirtyAt: null,
    };
  }
  const latest = dirty[0];
  const depth = latest.dirty_queue_depth as number;
  const window = dirty.slice(0, DIRTY_QUEUE_TREND_WINDOW);
  const recentDepths = window.map((r) => r.dirty_queue_depth as number).reverse(); // oldest->newest

  const clearedVals = window.map((r) => r.dirty_cleared).filter((v): v is number => v != null);
  const clearedInWindow = clearedVals.length > 0 ? clearedVals.reduce((a, b) => a + b, 0) : null;

  // Draining = the drain genuinely advanced the head (cleared>0). Only when NO run in the
  // window carries cleared (pre-258 data) fall back to the old depth trend — which cannot
  // distinguish drainage from TTL eviction, but is better than nothing on old rows. A
  // single run is never judged (null): one budget-cut run must not flap the banner red.
  const depthFalling = depth < (window[window.length - 1].dirty_queue_depth as number);
  const draining =
    window.length >= 2
      ? clearedInWindow != null
        ? clearedInWindow > 0
        : depthFalling
      : null;

  // Livelock: every windowed run hit its budget AND none advanced the head. With the
  // per-group incremental clear a healthy-but-slow run still clears >0, so cleared==0
  // across a truncated streak means zero progress — fail even at low depth (those rows
  // will silently TTL-evict, not merge).
  const livelocked =
    window.length >= 2 &&
    clearedInWindow === 0 &&
    window.every((r) => r.dirty_truncated === 1);

  let status: DirtyQueueStatus = 'ok';
  let reason: string | null = null;
  if (livelocked) {
    status = 'fail';
    reason =
      'Every recent dirty run hit its budget with zero properties cleared — the drain is ' +
      'livelocked and the queue is only shrinking by TTL eviction. Check dedup_engine.yml runs.';
  } else if (depth >= DIRTY_QUEUE_WARN_DEPTH) {
    status = draining === false ? 'fail' : 'warn';
    // Only claim "draining" when it is PROVEN by cleared>0 — the depth-trend fallback
    // cannot distinguish drainage from TTL eviction, and a single run proves nothing.
    reason =
      draining === false
        ? 'Not draining across recent runs — the --dirty drain may be failing or out-paced; ' +
          'check the dedup_engine.yml runs.'
        : draining && clearedInWindow != null
          ? 'Draining through the bounded drain (a transient tagging flood).'
          : 'Deep queue; not enough recent run data to judge the drain — watch the next ' +
            'dedup_engine.yml dirty runs.';
  }
  return {
    status, depth, claimed: latest.dirty_claimed ?? null, draining,
    clearedInWindow, livelocked, reason, recentDepths, lastDirtyAt: latest.started_at,
  };
}
