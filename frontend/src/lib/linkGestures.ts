/* Browser-gesture rules for links the router does not own.
 *
 * `<Link>` gets all of this right, and every in-app destination should be one.
 * Two places cannot use it: MapLibre owns its popup DOM and renders anchors from
 * a raw HTML string, and a modal that must run a side effect before navigating
 * needs to know whether the click was a plain one. Both used to hand-roll the
 * rules, and both got them wrong the same way — testing `e.button === 1` for
 * middle-click (which fires `auxclick`, never `click`, so the arm was dead code)
 * and omitting `altKey` (alt/option-click is "download this link" on Chrome and
 * Firefox, and swallowing it into an SPA navigation steals the gesture).
 *
 * The predicates are structurally typed rather than taking a real MouseEvent, so
 * they unit-test without constructing DOM events or mounting a map. */
import { useEffect } from 'react';
import { useNavigate } from 'react-router-dom';

/* The modifier set react-router's own `isModifiedEvent` uses, plus the button
 * test it applies separately. A click matching this is the one gesture that
 * means "take me there in this tab". */
export function isPlainLeftClick(e: {
  button?: number;
  metaKey?: boolean;
  ctrlKey?: boolean;
  shiftKey?: boolean;
  altKey?: boolean;
  defaultPrevented?: boolean;
}): boolean {
  return (
    !e.defaultPrevented &&
    (e.button ?? 0) === 0 &&
    !e.metaKey &&
    !e.ctrlKey &&
    !e.shiftKey &&
    !e.altKey
  );
}

/* The in-app path a click should be routed to, or null to leave it to the
 * browser. Null covers: a modified or non-primary click (the user asked for a
 * tab/window/download), no anchor ancestor, an explicit download, a target other
 * than _self, an absolute or protocol-relative URL (an external destination —
 * the map's own attribution links are exactly this), and a click something else
 * already handled. */
export function spaNavHrefForClick(
  e: {
    button?: number;
    metaKey?: boolean;
    ctrlKey?: boolean;
    shiftKey?: boolean;
    altKey?: boolean;
    defaultPrevented?: boolean;
    target?: unknown;
  },
): string | null {
  if (!isPlainLeftClick(e)) return null;

  const el = e.target as { closest?: (s: string) => unknown } | null;
  if (!el || typeof el.closest !== 'function') return null;
  const anchor = el.closest('a') as {
    getAttribute?: (n: string) => string | null;
    hasAttribute?: (n: string) => boolean;
  } | null;
  if (!anchor || typeof anchor.getAttribute !== 'function') return null;

  if (anchor.hasAttribute?.('download')) return null;

  const target = anchor.getAttribute('target');
  if (target && target !== '_self') return null;

  const href = anchor.getAttribute('href');
  // Same-origin app paths only. `//host/x` is protocol-relative — an EXTERNAL
  // destination that merely starts with a slash — so it must not be captured.
  if (!href || !href.startsWith('/') || href.startsWith('//')) return null;

  return href;
}

/* Route in-app clicks from a subtree React does not render — a MapLibre popup,
 * or any other library that writes its own HTML. Delegated on the container, so
 * markup added to that subtree later is covered by construction rather than by
 * remembering to wire it up.
 *
 * Attach this in its OWN effect. Folding it into a map-initialisation effect
 * would add `navigate` to that effect's dependency list and tear the map down
 * on every navigation. */
export function useSpaLinkDelegation(
  ref: { current: HTMLElement | null },
): void {
  const navigate = useNavigate();
  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    const onClick = (e: MouseEvent) => {
      const href = spaNavHrefForClick(e);
      if (href == null) return;
      e.preventDefault();
      navigate(href);
    };
    node.addEventListener('click', onClick);
    return () => node.removeEventListener('click', onClick);
  }, [ref, navigate]);
}
