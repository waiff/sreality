/* The one infinite-list hook, shared by every scrolled surface (Browse
 * cards + table on properties_public, and the Estimations + Watchdog API
 * feeds). It wraps React Query v5's useInfiniteQuery and adds the two
 * things every caller needs identically: (1) flattening pages into one row
 * array, and (2) de-duplicating by a stable row id so a row that re-sorts
 * across a page seam (under live mutation / a refetch) can never render
 * twice. The pageParam is an OPAQUE cursor — a keyset object for the
 * Supabase surfaces, a base64 string for the API feeds — so this hook is
 * backend-agnostic and there is exactly one mental model app-wide. */

import { useCallback, useMemo, useRef } from 'react';
import {
  useInfiniteQuery,
  keepPreviousData,
  type QueryKey,
} from '@tanstack/react-query';

export interface InfiniteListPage<TRow> {
  rows: TRow[];
  /* Cursor for the page AFTER this one. When omitted the pageSize
   * heuristic governs the stop (a short page ends the list). */
  nextCursor?: unknown;
}

/* Caller-facing poll decider: receives the flattened loaded rows so a feed
 * can keep polling while any row is still pending/running, across ALL loaded
 * pages (not just the first). Returns the interval ms, or false to stop. */
type RefetchInterval<TRow> =
  | number
  | false
  | ((rows: TRow[]) => number | false);

export interface UseInfiniteListOptions<
  TRow,
  TPage extends InfiniteListPage<TRow> = InfiniteListPage<TRow>,
> {
  queryKey: QueryKey;
  /* Receives the cursor for the page to load (null for the first page). */
  queryFn: (cursor: unknown | null) => Promise<TPage>;
  pageSize: number;
  getRowId: (row: TRow) => string | number;
  enabled?: boolean;
  staleTime?: number;
  /* Keep accumulated pages in cache long enough that "open a card → Back"
   * re-renders the whole scrolled list instantly (no skeleton collapse,
   * which is what lets native/explicit scroll restoration land correctly).
   * Defaults to React Query's 5 min if unset. */
  gcTime?: number;
  refetchInterval?: RefetchInterval<TRow>;
}

export interface InfiniteListResult<
  TRow,
  TPage extends InfiniteListPage<TRow> = InfiniteListPage<TRow>,
> {
  rows: TRow[];
  loadedCount: number;
  /* The first page object verbatim — lets a caller read page-level fields
   * the flattened rows drop (e.g. a `total` the API returns once). */
  firstPage: TPage | undefined;
  /* First page in flight with nothing yet to show (render a skeleton). */
  isLoading: boolean;
  isFetchingNextPage: boolean;
  hasNextPage: boolean;
  isError: boolean;
  error: Error | null;
  fetchNextPage: () => void;
  refetch: () => void;
}

export function useInfiniteList<
  TRow,
  TPage extends InfiniteListPage<TRow> = InfiniteListPage<TRow>,
>(
  opts: UseInfiniteListOptions<TRow, TPage>,
): InfiniteListResult<TRow, TPage> {
  const {
    queryKey,
    queryFn,
    pageSize,
    getRowId,
    enabled = true,
    staleTime,
    gcTime,
    refetchInterval,
  } = opts;

  /* Translate the rows-based poll decider into React Query's query-based
   * one, flattening every loaded page so a row that finishes far up the
   * feed still updates. */
  const rqRefetchInterval =
    typeof refetchInterval === 'function'
      ? (q: { state: { data?: { pages?: TPage[] } } }) => {
          const pages = q.state.data?.pages ?? [];
          return refetchInterval(pages.flatMap((p) => p.rows));
        }
      : refetchInterval;

  const query = useInfiniteQuery({
    queryKey,
    queryFn: ({ pageParam }) => queryFn((pageParam as unknown) ?? null),
    initialPageParam: null as unknown,
    getNextPageParam: (lastPage: TPage) =>
      lastPage.rows.length < pageSize
        ? undefined
        : (lastPage.nextCursor ?? undefined),
    enabled,
    staleTime,
    gcTime,
    refetchInterval: rqRefetchInterval as never,
    placeholderData: keepPreviousData,
  });

  /* getRowId via ref: callers pass an inline closure, so reading it through
   * a ref keeps the flatten/dedup memo keyed on the data alone. */
  const getRowIdRef = useRef(getRowId);
  getRowIdRef.current = getRowId;

  const rows = useMemo(() => {
    const pages = query.data?.pages ?? [];
    const seen = new Set<string | number>();
    const out: TRow[] = [];
    for (const page of pages) {
      for (const row of page.rows) {
        const id = getRowIdRef.current(row);
        if (seen.has(id)) continue;
        seen.add(id);
        out.push(row);
      }
    }
    return out;
  }, [query.data]);

  const fetchNextPage = useCallback(() => {
    if (query.hasNextPage && !query.isFetchingNextPage) {
      void query.fetchNextPage();
    }
  }, [query]);

  return {
    rows,
    loadedCount: rows.length,
    firstPage: query.data?.pages?.[0] as TPage | undefined,
    isLoading: enabled && query.isLoading,
    isFetchingNextPage: query.isFetchingNextPage,
    hasNextPage: query.hasNextPage,
    isError: query.isError,
    error: (query.error as Error) ?? null,
    fetchNextPage,
    refetch: () => void query.refetch(),
  };
}
