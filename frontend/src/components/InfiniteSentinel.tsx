import { useEffect, useRef, type RefObject } from 'react';
import Spinner from '@/components/Spinner';

/* The scroll-trigger + status footer for every infinite list. Render it as
 * the LAST element inside the scroll container. It watches itself with an
 * IntersectionObserver and calls `onReach` as it nears the bottom (a
 * generous rootMargin pre-loads the next page so scrolling never stalls).
 *
 * The scroll root is parameterized because the surfaces differ: the Browse
 * cards live in an independently-scrolling fixed-height column (pass that
 * element's ref), while the table / API feeds page-scroll the window (omit
 * rootRef → viewport). This is the one knob that lets a single component
 * serve both layouts without a per-surface fork. */
export interface InfiniteSentinelProps {
  onReach: () => void;
  hasNextPage: boolean;
  isFetchingNextPage: boolean;
  loadedCount: number;
  total: number | null;
  /* Scroll container the observer watches; omit/undefined → viewport. */
  rootRef?: RefObject<Element | null>;
  /* How early to pre-load, in px before the sentinel enters the root. */
  rootMargin?: string;
  loadingLabel?: string;
}

export default function InfiniteSentinel({
  onReach,
  hasNextPage,
  isFetchingNextPage,
  loadedCount,
  total,
  rootRef,
  rootMargin = '700px',
  loadingLabel = 'Loading…',
}: InfiniteSentinelProps) {
  const ref = useRef<HTMLDivElement>(null);
  const onReachRef = useRef(onReach);
  onReachRef.current = onReach;
  const intersectingRef = useRef(false);

  useEffect(() => {
    const el = ref.current;
    if (!el || !hasNextPage) return;
    const root = rootRef?.current ?? null;
    const obs = new IntersectionObserver(
      (entries) => {
        intersectingRef.current = entries[0].isIntersecting;
        if (entries[0].isIntersecting) onReachRef.current();
      },
      { root, rootMargin },
    );
    obs.observe(el);
    return () => obs.disconnect();
  }, [hasNextPage, rootRef, rootMargin]);

  /* If a freshly-loaded page was too short to push the sentinel back out of
   * view, the observer won't fire again on its own — re-trigger so the list
   * keeps filling until the viewport is covered or the list is exhausted. */
  useEffect(() => {
    if (!isFetchingNextPage && hasNextPage && intersectingRef.current) {
      onReachRef.current();
    }
  }, [isFetchingNextPage, hasNextPage]);

  const atEnd = !hasNextPage && loadedCount > 0;

  return (
    <div
      ref={ref}
      aria-live="polite"
      className="py-5 flex items-center justify-center text-[0.75rem] text-[var(--color-ink-3)] tabular-nums"
    >
      {isFetchingNextPage ? (
        <span className="inline-flex items-center gap-2">
          <Spinner />
          {loadingLabel}
        </span>
      ) : atEnd ? (
        <span className="text-[var(--color-ink-4)]">
          {total != null
            ? `End — ${total.toLocaleString('cs-CZ')} total`
            : 'End of results'}
        </span>
      ) : (
        /* Reserve a little height so the observer has a stable target even
         * between fetches. */
        <span className="block h-1" aria-hidden />
      )}
    </div>
  );
}
