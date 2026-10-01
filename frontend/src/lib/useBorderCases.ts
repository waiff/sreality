import { useCallback, useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';

import { deleteBorderCase, setBorderCase } from '@/lib/api';
import { fetchBorderCasesByImageIds } from '@/lib/queries';
import { useOptimisticWrite } from '@/lib/useOptimisticWrite';

/* The "Border case" flag (migration 310) for a WHOLE grid of images — the one
 * read path, write path and stability policy behind every labeling surface
 * (NEW DEDUP Labeling's proposal grid and its tag-sample grid). It is a hook
 * rather than per-page state because the flag is image-grain: the same photo
 * can be two tiles at once (two models' proposals) and both must flip
 * together, and a per-surface copy of this logic is exactly how the labeling
 * surfaces drifted apart before.
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
 *    cancellation is needed for that (which is how a query-cache patch does it,
 *    and would here throw away every OTHER image in the same batch): settling an id
 *    — by reading it back OR by writing it — drops it out of `missing`, which
 *    changes the query key this hook observes, so the older read's result is
 *    never merged. `known` only ever grows, so a settled id can never re-enter.
 *
 * Writes go through lib/useOptimisticWrite (rule #22's write policy): the flag
 * paints on the click, rolls back from `onSettled` (never `onError`, which
 * would silence the global error toast), and pending is per image.
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

export function useBorderCases(imageIds: ReadonlyArray<number>): BorderCaseStore {
  const [resolved, setResolved] = useState<Resolved>(EMPTY);

  const missing = useMemo(
    () => [...new Set(imageIds)].filter((id) => !resolved.known.has(id)),
    [imageIds, resolved.known],
  );
  // The queryFn returns what it ASKED for alongside what came back: the response
  // carries flagged ids only, so on its own it can't say which ids are settled.
  const readQ = useQuery({
    queryKey: ['border-cases', missing.join(',')],
    queryFn: async ({ signal }) => ({
      requested: missing,
      flagged: await fetchBorderCasesByImageIds(missing, { signal }),
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

  /* ONE mutation instance for the whole grid; its observer only reflects the
   * most recent call, so per-image in-flight state is the hook's `pendingFor`. */
  const { mutate, pendingFor } = useOptimisticWrite({
    mutationKey: ['write', 'border-case'],
    mutationFn: ({ imageId, next }: Toggle): Promise<unknown> =>
      next ? setBorderCase(imageId) : deleteBorderCase(imageId),
    patch: ({ imageId, next }) => {
      apply(imageId, next);
      return () => apply(imageId, !next);
    },
    pendingKey: ({ imageId }) => imageId,
  });

  const toggle = useCallback(
    (imageId: number) => {
      if (pendingFor(imageId)) return;
      mutate({ imageId, next: !resolved.flagged.has(imageId) });
    },
    // `mutate` is referentially stable in React Query v5 and `pendingFor`
    // changes only with the in-flight set, so this callback (and the store
    // object below it) only changes when the state a tile renders does.
    [mutate, pendingFor, resolved.flagged],
  );

  return useMemo(
    () => ({
      has: (imageId: number) => resolved.flagged.has(imageId),
      isPending: pendingFor,
      toggle,
    }),
    [resolved.flagged, pendingFor, toggle],
  );
}
