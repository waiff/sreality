import { useCallback, useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';

import { deleteBorderCase, setBorderCase } from '@/lib/api';
import { fetchBorderCasesByImageIds } from '@/lib/queries';

/* The "Border case" flag (migration 310) for a WHOLE grid of images — the one
 * read path, write path and stability policy behind every labeling surface
 * (/clip-audit's feed + label browser, NEW DEDUP Labeling's review grid). It is
 * a hook rather than per-page state because the flag is image-grain: the same
 * photo can be two tiles at once (two models' proposals) and both must flip
 * together, and a per-page copy of this logic is exactly how /clip-audit and the
 * Labeling page drifted apart before (see TrainControl's own history).
 *
 * Three properties are load-bearing:
 *
 * 1. **Ids ACCUMULATE; only never-seen ones are ever requested.** Keying the
 *    read on the grid's CURRENT id list would make every review — which changes
 *    that list — a new cache entry, blanking every button in the grid back to
 *    "unflagged" until the refetch lands. Same reason the Labeling page keeps
 *    its photos in a page-level id->image map.
 * 2. **A toggle patches this store, never invalidates.** A refetch would
 *    re-render the whole grid the operator is working through tile by tile.
 * 3. **A read that lands after a click cannot resurrect what it replaced.** No
 *    cancellation is needed for that (which is how `pipelineCache` does it, and
 *    would here throw away every OTHER image in the same batch): settling an id
 *    — by reading it back OR by writing it — drops it out of `missing`, which
 *    changes the query key this hook observes, so the older read's result is
 *    never merged. `known` only ever grows, so a settled id can never re-enter.
 *
 * Writes are optimistic and roll back from `onSettled`, never `onError` — the
 * app's global MutationCache.onError (main.tsx) is the only "the write failed"
 * feedback, and it deliberately stays silent for a mutation that defines its
 * own onError (rule #22's cache policy, same idiom as `pipelineCache`).
 */
export type BorderCaseStore = {
  /** Is this image flagged? False for an id whose state hasn't loaded yet. */
  has: (imageId: number) => boolean;
  /** Is a flag/unflag write in flight for this image? */
  isPending: (imageId: number) => boolean;
  toggle: (imageId: number) => void;
};

// `known` is every id whose flag state is settled (read back, or just written);
// `flagged` is the subset that carries the flag. The two are separate because a
// read returns the flagged ids only — without `known`, every unflagged image
// would be re-requested on every render.
type Resolved = { known: ReadonlySet<number>; flagged: ReadonlySet<number> };

const EMPTY: Resolved = { known: new Set(), flagged: new Set() };

type Toggle = { imageId: number; next: boolean };
type Rollback = () => void;

export function useBorderCases(imageIds: ReadonlyArray<number>): BorderCaseStore {
  const [resolved, setResolved] = useState<Resolved>(EMPTY);
  const [pending, setPending] = useState<ReadonlySet<number>>(new Set());

  const missing = useMemo(
    () => [...new Set(imageIds)].filter((id) => !resolved.known.has(id)),
    [imageIds, resolved.known],
  );
  // The queryFn returns what it ASKED for alongside what came back: the response
  // carries flagged ids only, so on its own it can't say which ids are settled.
  const readQ = useQuery({
    queryKey: ['border-cases', missing.join(',')],
    queryFn: async () => ({
      requested: missing,
      flagged: await fetchBorderCasesByImageIds(missing),
    }),
    enabled: missing.length > 0,
  });

  useEffect(() => {
    const page = readQ.data;
    if (!page) return;
    setResolved((prev) => {
      const known = new Set(prev.known);
      const flagged = new Set(prev.flagged);
      let grew = false;
      for (const id of page.requested) {
        if (known.has(id)) continue; // already settled — keep `prev` untouched
        known.add(id);
        if (page.flagged.has(id)) flagged.add(id);
        grew = true;
      }
      return grew ? { known, flagged } : prev;
    });
  }, [readQ.data]);

  const apply = useCallback((imageId: number, next: boolean) => {
    setResolved((prev) => {
      const known = new Set(prev.known).add(imageId);
      const flagged = new Set(prev.flagged);
      if (next) flagged.add(imageId);
      else flagged.delete(imageId);
      return { known, flagged };
    });
  }, []);

  const release = useCallback((imageId: number) => {
    setPending((prev) => {
      if (!prev.has(imageId)) return prev;
      const rest = new Set(prev);
      rest.delete(imageId);
      return rest;
    });
  }, []);

  /* ONE mutation instance for the whole grid: its observer only ever reflects
   * the most recent call, so per-image in-flight state is tracked here instead
   * (the same reason the Labeling page keeps its own pendingRowKeys). */
  const write = useMutation<unknown, Error, Toggle, Rollback>({
    mutationFn: ({ imageId, next }: Toggle) =>
      next ? setBorderCase(imageId) : deleteBorderCase(imageId),
    onMutate: ({ imageId, next }) => {
      setPending((prev) => new Set(prev).add(imageId));
      apply(imageId, next);
      return () => apply(imageId, !next);
    },
    onSettled: (_data, error, { imageId }, rollback) => {
      if (error) rollback?.();
      release(imageId);
    },
  });

  const toggle = useCallback(
    (imageId: number) => {
      if (pending.has(imageId)) return;
      write.mutate({ imageId, next: !resolved.flagged.has(imageId) });
    },
    [pending, resolved.flagged, write],
  );

  return useMemo(
    () => ({
      has: (imageId: number) => resolved.flagged.has(imageId),
      isPending: (imageId: number) => pending.has(imageId),
      toggle,
    }),
    [resolved.flagged, pending, toggle],
  );
}
