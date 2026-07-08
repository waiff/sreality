import type { QueryClient } from '@tanstack/react-query';

import type { DedupAuditRow } from '@/lib/api';
import type { DecisionFeedback, DedupCandidatesResponse } from '@/lib/types';

/* React-Query cache surgery for the operator's per-pair "this dedup decision was wrong"
 * flag. Two dedup query families carry the flag: the Decision-history feed (audit) and the
 * Needs-review queue (candidates). Both are PROPERTY-pair-keyed, so a flag written on one
 * canonical pair reflects on both — which is why the reflect/cancel/invalidate helpers
 * always act on BOTH families together.
 *
 * Why this exists: the flag write already returns the authoritative row, so reflecting it
 * into the cache SYNCHRONOUSLY (rather than waiting on the invalidation's background
 * refetch) is what makes the "Nesprávné" label appear the instant the operator saves. The
 * background refetch reflected unreliably on the Decision-history feed (a terminal-only
 * pair had to be re-saved before it showed), because that query has no poll to fall back
 * on — the optimistic patch removes the dependency on refetch timing entirely. */

const AUDIT_KEY = ['dedup', 'audit'] as const;
const CANDIDATES_KEY = ['dedup', 'candidates'] as const;

type AuditResponse = { data: DedupAuditRow[]; total: number; returned: number };

const canon = (a: number, b: number): [number, number] =>
  a <= b ? [a, b] : [b, a];

/* Patch a flag change into every cached audit + candidate view whose row is the given
 * PROPERTY pair. Matched by the CANONICAL (low, high) pair — the exact key the server
 * stores and joins on — so the left/right order a row happens to carry never matters.
 * `feedback = null` clears the flag (the un-flag path). Rows are replaced immutably and
 * only when they actually change, so React-Query's structural sharing stays intact. */
export function reflectDecisionFeedback(
  qc: QueryClient,
  leftPropertyId: number,
  rightPropertyId: number,
  feedback: DecisionFeedback | null,
): void {
  const [lo, hi] = canon(leftPropertyId, rightPropertyId);
  const isPair = (a: number | null | undefined, b: number | null | undefined) => {
    if (a == null || b == null) return false;
    const [x, y] = canon(a, b);
    return x === lo && y === hi;
  };

  qc.setQueriesData<AuditResponse>({ queryKey: AUDIT_KEY }, (old) => {
    if (!old?.data) return old;
    let changed = false;
    const data = old.data.map((r) => {
      if (!isPair(r.left_property_id, r.right_property_id)) return r;
      changed = true;
      return { ...r, feedback };
    });
    return changed ? { ...old, data } : old;
  });

  qc.setQueriesData<DedupCandidatesResponse>({ queryKey: CANDIDATES_KEY }, (old) => {
    if (!old?.data) return old;
    let changed = false;
    const data = old.data.map((c) => {
      if (!isPair(c.left_property?.property_id, c.right_property?.property_id)) return c;
      changed = true;
      return { ...c, feedback };
    });
    return changed ? { ...old, data } : old;
  });
}

/* Stop any in-flight (possibly pre-write) refetch of the two families before the
 * optimistic patch, so a stale response can't land afterwards and clobber it — the
 * canonical optimistic-update guard. The invalidate that follows starts a fresh fetch
 * that reconciles to the same committed value. */
export function cancelDedupFeedback(qc: QueryClient): Promise<void> {
  return Promise.all([
    qc.cancelQueries({ queryKey: AUDIT_KEY }),
    qc.cancelQueries({ queryKey: CANDIDATES_KEY }),
  ]).then(() => undefined);
}

/* Reconcile with the server after the optimistic patch — scoped to just the two
 * feedback-bearing families, so the ~8 other dedup queries (images / sources / summary /
 * …) that a flag never changes are left untouched. This is also what corrects the one
 * case the in-place patch can't: un-flagging a row while the audit feed is filtered to
 * "Jen nesprávná" leaves that row briefly visible-but-unflagged (its feedback is now
 * null, which the patch can't drop without knowing the active filter) — the refetch here
 * removes it. A blanket prune is deliberately avoided: in the default (all) view an
 * un-flagged row must STAY, so membership is the server's call, not the patch's. */
export function invalidateDedupFeedback(qc: QueryClient): void {
  qc.invalidateQueries({ queryKey: AUDIT_KEY });
  qc.invalidateQueries({ queryKey: CANDIDATES_KEY });
}
