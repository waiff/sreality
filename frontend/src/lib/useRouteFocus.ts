/* Where focus GOES when the route changes.
 *
 * An SPA navigation leaves keyboard focus on whatever was clicked in the OLD
 * page — a link that no longer exists — so a screen reader announces nothing
 * and the next Tab lands somewhere arbitrary. Native page loads reset focus to
 * the document; this restores that contract for client-side routing by moving
 * focus to the <main> landmark, which then also anchors the next Tab at the
 * top of the new content.
 *
 * Sits beside useScrollRestoration, which solved the same problem for scroll
 * position; focus position had not been considered.
 *
 * Keyed on PATHNAME only. Query-string edits (Browse filters, ?run=, ?tab=)
 * are in-page state changes, not navigations — hijacking focus on every filter
 * click would be worse than the defect. The first render is skipped so a deep
 * link is not stolen from whatever the browser focused on load. */
import { useEffect } from 'react';
import { useLocation } from 'react-router-dom';

export const MAIN_ID = 'main';

/* MODULE-level, not a ref: App.tsx wraps the whole route tree in
 * <ErrorBoundary key={location.pathname}> so a crashed page recovers on the
 * next navigation — which means the Shell, and this hook's host, REMOUNT on
 * every pathname change. An instance-level "skip the first render" guard
 * therefore skipped every navigation in production (measured: <main> was a
 * new element 65 ms after each nav click, and focus ended on <body>). The
 * last pathname this module focused survives the remount; a full page load
 * resets it, which is exactly the "do not steal a deep link" case. */
let lastPathname: string | null = null;

/* Test seam: module state would otherwise leak between cases. */
export function resetRouteFocus(): void {
  lastPathname = null;
}

export function useRouteFocus(): void {
  const { pathname } = useLocation();
  useEffect(() => {
    if (lastPathname === null) {
      // First render after a full page load — leave the browser's focus alone.
      lastPathname = pathname;
      return;
    }
    if (pathname === lastPathname) return; // same-path remount, nothing moved
    lastPathname = pathname;
    const main = document.getElementById(MAIN_ID);
    if (!main) return;
    // preventScroll: the page has just rendered at the top; jumping the
    // viewport again would fight the browser's own scroll restoration.
    main.focus({ preventScroll: true });
  }, [pathname]);
}
