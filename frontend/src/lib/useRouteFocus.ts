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
import { useEffect, useRef } from 'react';
import { useLocation } from 'react-router-dom';

export const MAIN_ID = 'main';

export function useRouteFocus(): void {
  const { pathname } = useLocation();
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    const main = document.getElementById(MAIN_ID);
    if (!main) return;
    // preventScroll: the page has just rendered at the top; jumping the
    // viewport again would fight the browser's own scroll restoration.
    main.focus({ preventScroll: true });
  }, [pathname]);
}
