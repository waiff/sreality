/* AUTODEDUP · the verdict write, shared by every review queue.
 *
 * An optimistic overlay keyed by a caller-chosen string, rolled back on failure.
 * THE LIST IS NEVER INVALIDATED — the review-grid lesson: a queue that reorders
 * under a correcting hand causes the mis-clicks it exists to catch.
 *
 * THE SPLIT RIDES THE SAME OVERLAY, because what it stores about a set of
 * adverts is one badge: `same` when the operator used one letter, otherwise the
 * weakest relation the split used. What it does NOT share is the failure
 * treatment — a rejected split must leave the operator's letters on screen to
 * correct, so the error is kept per card and shown in place, and a 409 arms the
 * button rather than retrying.
 *
 * TWO POSTERS, ONE HOOK. The proposed-cluster card posts `/verdict/split`; the
 * candidate card posts `/verdict/candidate-split`. Everything the operator sees
 * — the optimistic badge, the toast, the receipt, the 409 — is identical, so the
 * poster is a parameter rather than a second copy of this file.
 */

import { useCallback, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';

import {
  ApiError,
  postAutodedupSplitVerdict,
  postAutodedupVerdict,
  type AutodedupEnvelope,
  type AutodedupSplitInput,
  type AutodedupSplitResult,
  type AutodedupSplitUnit,
  type AutodedupVerdictInput,
  type AutodedupVerdictRow,
} from '@/lib/api';
import { pushToast } from '@/lib/toast';
import { clusterVerdictOf, type SplitError, type SplitReceipt } from './UnitSplit';

/* One provisional row so the badge flips on click; the server's own row
 * replaces it as soon as it lands. `id: 0` marks it as not-yet-stored. */
function optimisticVerdict(
  input: AutodedupVerdictInput,
  decidedBy: string,
): AutodedupVerdictRow {
  return {
    id: 0,
    kind: input.kind,
    cluster_key: input.cluster_key ?? null,
    listing_lo: input.listing_lo ?? null,
    listing_hi: input.listing_hi ?? null,
    verdict: input.verdict,
    note: input.note ?? null,
    decided_by: decidedBy,
    decided_at: new Date().toISOString(),
  };
}

/* What every split body has in common — enough to draw the optimistic badge and
 * the receipt, whichever endpoint it is bound for. */
export interface SplitLike {
  units: AutodedupSplitUnit[];
}

export function useVerdictOverlay<S extends SplitLike = AutodedupSplitInput>(
  postSplit: (body: S) => Promise<AutodedupEnvelope<AutodedupSplitResult>> =
    postAutodedupSplitVerdict as unknown as (
      body: S,
    ) => Promise<AutodedupEnvelope<AutodedupSplitResult>>,
) {
  const [overlay, setOverlay] = useState<Record<string, AutodedupVerdictRow>>({});
  /* The session counter is a SERVER count, and a verdict is what changes it —
   * so every successful write invalidates it and the strip re-reads. Without
   * this "37 / 100" would stand still through a whole session and the operator
   * would have no way to tell the sample was progressing. */
  const queryClient = useQueryClient();
  const countAgain = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['autodedup', 'validation-progress'] });
    queryClient.invalidateQueries({ queryKey: ['autodedup', 'agreement'] });
  }, [queryClient]);
  /* The IN-FLIGHT KEY, not a global boolean. One shared `isPending` would mark
   * every row in the queue busy while a single write lands, and disabling the
   * button that was just clicked blurs it — a keyboard operator loses their
   * place after every verdict. Nothing is disabled here: the optimistic overlay
   * already shows the press and onError already rolls it back. */
  const [inFlight, setInFlight] = useState<string | null>(null);
  const mutation = useMutation({
    mutationFn: (vars: { key: string; input: AutodedupVerdictInput }) =>
      postAutodedupVerdict(vars.input),
    onMutate: (vars) => {
      const previous = overlay[vars.key];
      setInFlight(vars.key);
      setOverlay((o) => ({ ...o, [vars.key]: optimisticVerdict(vars.input, 'ukládám…') }));
      return { previous };
    },
    onSuccess: (res, vars) => {
      if (res.data) setOverlay((o) => ({ ...o, [vars.key]: res.data as AutodedupVerdictRow }));
      const retracted = res.must_not_link_retracted ?? 0;
      pushToast(
        'ok',
        res.must_not_link
          ? 'Verdict recorded — this pair is now permanently un-linkable.'
          : retracted > 0
            /* A cluster confirmed as one property drops every veto inside it —
             * said out loud, because it is the permanent half of the click. */
            ? `Verdict recorded — ${retracted} pair(s) are linkable again.`
            : 'Verdict recorded.',
      );
    },
    onError: (err: Error, vars, ctx) => {
      setOverlay((o) => {
        const next = { ...o };
        if (ctx?.previous) next[vars.key] = ctx.previous;
        else delete next[vars.key];
        return next;
      });
      pushToast('err', `Verdict failed: ${err.message}`);
    },
    onSettled: () => {
      setInFlight(null);
      countAgain();
    },
  });
  const submit = useCallback(
    (key: string, input: AutodedupVerdictInput) => mutation.mutate({ key, input }),
    [mutation],
  );

  const [splitErrors, setSplitErrors] = useState<Record<string, SplitError>>({});
  /* The SUBMITTED assignment is kept beside the server's counts. A receipt read
   * off live state is not a receipt: change a select after saving and the line
   * would confirm, in the server's own numbers, a ruling that was never sent. */
  const [splitResults, setSplitResults] = useState<Record<string, SplitReceipt>>({});
  const splitMutation = useMutation({
    mutationFn: (vars: { key: string; input: S }) => postSplit(vars.input),
    onMutate: (vars) => {
      const previous = overlay[vars.key];
      setInFlight(vars.key);
      setSplitErrors((e) => {
        const next = { ...e };
        delete next[vars.key];
        return next;
      });
      setOverlay((o) => ({
        ...o,
        [vars.key]: optimisticVerdict(
          {
            kind: 'cluster',
            cluster_key: (vars.input as unknown as { cluster_key?: number }).cluster_key ?? null,
            verdict: clusterVerdictOf(vars.input),
          },
          'ukládám…',
        ),
      }));
      return { previous };
    },
    onSuccess: (res, vars) => {
      const stored = res.data?.cluster_verdict ?? null;
      if (stored) setOverlay((o) => ({ ...o, [vars.key]: stored }));
      if (res.data) {
        setSplitResults((r) => ({
          ...r,
          [vars.key]: { input: vars.input, result: res.data as AutodedupSplitResult },
        }));
      }
      const reversed = res.data?.reversed_pairs?.length ?? 0;
      pushToast(
        'ok',
        `Split recorded — ${res.data?.n_pairs_negative ?? 0} pair(s) permanently un-linkable`
          + (reversed > 0 ? `, ${reversed} earlier ruling(s) taken back.` : '.'),
      );
    },
    onError: (err: Error, vars, ctx) => {
      setOverlay((o) => {
        const next = { ...o };
        if (ctx?.previous) next[vars.key] = ctx.previous;
        else delete next[vars.key];
        return next;
      });
      /* 409 is not a failure: the server is asking whether the operator really
       * means to take back a veto they wrote earlier. The letters stay, the
       * reason is shown in place, and the Save button arms rather than retries. */
      setSplitErrors((e) => ({
        ...e,
        [vars.key]: {
          message: err.message,
          needsConfirm: err instanceof ApiError && err.status === 409,
        },
      }));
    },
    onSettled: () => {
      setInFlight(null);
      countAgain();
    },
  });
  const submitSplit = useCallback(
    (key: string, input: S) => splitMutation.mutate({ key, input }),
    [splitMutation],
  );

  return { overlay, submit, submitSplit, pendingKey: inFlight, splitErrors, splitResults };
}

export default useVerdictOverlay;
