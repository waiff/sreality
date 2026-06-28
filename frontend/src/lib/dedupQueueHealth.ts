import type { DedupEngineRun } from './queries';

/* Health of the real-time dedup --dirty drain, derived from recent run rows. ONE source of
 * truth shared by the /dedup automation dashboard and the Health page so the two never disagree.
 *
 * The signal is dedup_dirty_properties depth over successive dirty runs (migration 255). A flood
 * (a new portal / retag campaign enqueues most of the market) is EXPECTED and self-limiting now
 * that the claim is FIFO-bounded — it shows as a high-but-FALLING depth (warn, transient). The
 * thing worth an alert is a depth that stays high and ISN'T draining (the drain is failing /
 * out-paced) — that's the 2-day-silent-backlog this metric exists to catch. */

export type DirtyQueueStatus = 'ok' | 'warn' | 'fail' | 'idle';

// A depth below this is normal churn (steady-state inflow is well under the per-run bound).
export const DIRTY_QUEUE_WARN_DEPTH = 20_000;
// How many recent dirty runs to judge the trend over.
export const DIRTY_QUEUE_TREND_WINDOW = 4;

export interface DirtyQueueHealth {
  status: DirtyQueueStatus;
  depth: number | null; // latest dirty run's queue depth
  claimed: number | null; // latest dirty run's claimed slice
  draining: boolean | null; // depth falling across the trend window? null = not enough runs
  recentDepths: number[]; // oldest -> newest, for a sparkline
  lastDirtyAt: string | null;
}

/* `runs` is newest-first (as fetchDedupEngineRuns returns). */
export function assessDirtyQueue(runs: DedupEngineRun[]): DirtyQueueHealth {
  const dirty = runs.filter((r) => r.dirty_queue_depth != null);
  if (dirty.length === 0) {
    return { status: 'idle', depth: null, claimed: null, draining: null, recentDepths: [], lastDirtyAt: null };
  }
  const latest = dirty[0];
  const depth = latest.dirty_queue_depth as number;
  const window = dirty.slice(0, DIRTY_QUEUE_TREND_WINDOW);
  const recentDepths = window.map((r) => r.dirty_queue_depth as number).reverse(); // oldest->newest
  // draining = the newest depth is below the oldest in the window (the backlog is shrinking).
  const draining = window.length >= 2 ? depth < (window[window.length - 1].dirty_queue_depth as number) : null;

  let status: DirtyQueueStatus = 'ok';
  if (depth >= DIRTY_QUEUE_WARN_DEPTH) {
    // High depth: amber while it's draining (a flood working through the bounded drain), red if
    // it's stuck / growing (draining false), amber if we can't yet tell (a single run).
    status = draining === false ? 'fail' : 'warn';
  }
  return { status, depth, claimed: latest.dirty_claimed ?? null, draining, recentDepths, lastDirtyAt: latest.started_at };
}
