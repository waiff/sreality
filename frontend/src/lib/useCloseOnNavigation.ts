/* Close an overlay when the page underneath it changes.
 *
 * The three modal providers mount in components/Shell.tsx ABOVE <Outlet>, so a
 * navigation started from INSIDE a modal swaps the route body and leaves the
 * modal — and its body scroll lock — sitting on top of a page the operator
 * never asked to see it over. Two of them papered over the visible half with a
 * per-link `onClick` that closed the modal before navigating, which only ever
 * covers the links someone remembered to wire: a <Link> in the modal's body, a
 * card in the embedded Browse experience, a programmatic navigate() from a
 * mutation, and the modal survived all of them.
 *
 * Fixing it at the provider makes navigation itself the trigger, so no future
 * link inside a modal has to know it is inside one.
 *
 * PATHNAME ONLY, the same rule useRouteFocus follows. Not because the modals'
 * own filters live in the query string — they do not: both pilots drive their
 * embedded <BrowseExperience> through useMemoryBrowseState, precisely so that
 * filtering inside a modal never rewrites the URL of the page underneath. The
 * rule is right for the plainer reason that a query-string edit is not a
 * change of the page under the overlay: the Browse page BEHIND the modal
 * writing its own `?price=` as the operator filters it, a `?tab=` or `?run=`
 * in-page state change, a preset load — none of those swapped the route body,
 * and dismissing an overlay on them would be a bug, not a courtesy. A pathname
 * change is what "the page underneath is gone" means. The first render is
 * skipped so mounting the provider never fires it.
 *
 * HONEST SCOPE. In today's App.tsx the route tree is wrapped in
 * `<ErrorBoundary key={location.pathname}>`, so a pathname change already
 * unmounts the Shell and with it these providers — the modal state is
 * destroyed and this hook never gets to run its effect (its host remounted;
 * `first` is true again). The right behaviour is therefore reached twice over
 * today. This hook is what keeps it reached if that key ever goes away, and it
 * is the rule the pilots' tests exercise directly. It is deliberately NOT
 * written against a module-level "last pathname" the way useRouteFocus is:
 * that hook has exactly one consumer, and a shared module variable across the
 * three providers would let whichever one ran its effect first swallow the
 * change for the other two.
 */
import { useEffect, useRef } from 'react';
import { useLocation } from 'react-router-dom';

export function useCloseOnNavigation(close: () => void): void {
  const { pathname } = useLocation();
  const closeRef = useRef(close);
  closeRef.current = close;
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    closeRef.current();
  }, [pathname]);
}
