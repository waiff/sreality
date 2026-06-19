/* Restore the scroll position of an INNER scroll container across unmount
 * (e.g. open a card → Back → land where you were). Browsers natively
 * restore WINDOW scroll on history navigation, but not the scrollTop of a
 * nested overflow element — which is exactly what the Browse cards column
 * is. So this is needed only for element-scrolled surfaces; the window-
 * scrolled ones (table / API feeds) rely on native restoration plus a warm
 * React Query cache (pages still rendered on Back → full height → native
 * restore lands correctly).
 *
 * Positions are held in a module-level map (session-scoped, per cohort
 * key). A genuinely new cohort (filters/sort changed → new key) has no
 * saved position and resets to the top. */

import { useEffect, type RefObject } from 'react';

const positions = new Map<string, number>();

export function useScrollRestoration(
  ref: RefObject<HTMLElement | null>,
  key: string,
  ready: boolean,
): void {
  /* Restore (or reset to top for a fresh cohort) once the content is
   * present, so scrollHeight exists to scroll into. rAF lets the grid
   * settle a frame before we set scrollTop. */
  useEffect(() => {
    const el = ref.current;
    if (!el || !ready) return;
    const saved = positions.get(key) ?? 0;
    const raf = requestAnimationFrame(() => {
      el.scrollTop = saved;
    });
    return () => cancelAnimationFrame(raf);
  }, [ref, key, ready]);

  /* Persist scrollTop continuously (rAF-throttled) and on unmount. */
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    let raf = 0;
    const onScroll = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => positions.set(key, el.scrollTop));
    };
    el.addEventListener('scroll', onScroll, { passive: true });
    return () => {
      el.removeEventListener('scroll', onScroll);
      cancelAnimationFrame(raf);
      positions.set(key, el.scrollTop);
    };
  }, [ref, key]);
}
