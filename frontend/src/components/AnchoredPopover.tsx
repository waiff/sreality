/* A floating panel anchored to a trigger, rendered in a portal on <body>.
 *
 * Why a portal rather than the `absolute` popovers this app already has
 * (TagEditPopover, the filter dropdowns): those live inside their own container
 * and only work because that container neither clips nor stacks. The controls
 * on a Browse card do not have that luxury — the pipeline funnel and the
 * collection trigger sit inside the photo's `overflow-hidden` frame, and on the
 * Table the funnel sits inside a horizontal scroller. An absolutely-positioned
 * menu there is clipped to the photo. Portalling to <body> with
 * `position: fixed` escapes both.
 *
 * Deliberately minimal, and deliberately not a dependency (rule 7): anchor rect
 * → fixed coordinates, flip up when the panel would fall off the bottom, clamp
 * to the viewport, reposition on scroll/resize, close when the anchor scrolls
 * out of sight. No arrow, no virtual elements, no middleware stack.
 *
 * Dismissal is the caller's `onClose`: outside pointerdown, Escape, or the
 * anchor leaving the viewport. Pointerdown ON the anchor is ignored so the
 * trigger's own click can toggle the panel shut instead of it closing and
 * reopening in the same gesture.
 */

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
  type RefObject,
} from 'react';
import { createPortal } from 'react-dom';

export interface AnchoredPopoverProps {
  /* The element the panel hangs off — usually the button that opened it. */
  anchorRef: RefObject<HTMLElement | null>;
  onClose: () => void;
  children: ReactNode;
  /* Accessible name for the floating container. With it the panel is a named
   * role="group" (a container of controls); without it, a plain container. */
  ariaLabel?: string;
  /* DOM id, so the trigger's aria-controls can point at the panel itself. */
  id?: string;
  /* Panel chrome. Callers set width here; positioning is owned by this file. */
  className?: string;
  /* Gap between anchor and panel, px. */
  offset?: number;
}

/* Keep the panel this far from every viewport edge. */
const MARGIN = 8;

export default function AnchoredPopover({
  anchorRef,
  onClose,
  id,
  children,
  ariaLabel,
  className = '',
  offset = 4,
}: AnchoredPopoverProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  /* null until measured — the panel renders hidden for one frame so its own
   * height can be read before deciding whether it opens up or down. */
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);

  const place = useCallback(() => {
    const anchor = anchorRef.current;
    const panel = panelRef.current;
    if (!anchor || !panel) return;
    const a = anchor.getBoundingClientRect();
    const p = panel.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;

    // Anchor gone from view (the list scrolled past it) — a menu floating over
    // an off-screen row acts on a property the operator can no longer see.
    if (a.bottom < 0 || a.top > vh || a.right < 0 || a.left > vw) {
      onClose();
      return;
    }

    const below = a.bottom + offset;
    const above = a.top - offset - p.height;
    const fitsBelow = below + p.height + MARGIN <= vh;
    const top = fitsBelow || above < MARGIN ? below : above;
    const maxLeft = Math.max(MARGIN, vw - p.width - MARGIN);
    const left = Math.min(Math.max(a.left, MARGIN), maxLeft);
    setPos({ top, left });
  }, [anchorRef, offset, onClose]);

  useLayoutEffect(place, [place]);

  /* The panel's own height changes while it is open (the stage menu swaps its
   * remove row for a taller confirm). A panel that flipped ABOVE its anchor was
   * positioned for the old height, so growing it would push it down over the
   * anchor — re-place instead. Guarded for jsdom, which has no ResizeObserver. */
  useEffect(() => {
    const panel = panelRef.current;
    if (!panel || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(place);
    ro.observe(panel);
    return () => ro.disconnect();
  }, [place]);

  useEffect(() => {
    const onDocPointerDown = (e: PointerEvent) => {
      const target = e.target as Node;
      if (panelRef.current?.contains(target)) return;
      if (anchorRef.current?.contains(target)) return;
      onClose();
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      e.stopPropagation();
      // Escape is a deliberate dismissal, so focus goes back where it came
      // from; an outside click does not steal it back from whatever was hit.
      anchorRef.current?.focus();
      onClose();
    };
    /* capture: a scroll inside ANY ancestor (the Table's scroller, the page)
     * moves the anchor, and scroll events do not bubble. */
    document.addEventListener('pointerdown', onDocPointerDown, true);
    document.addEventListener('keydown', onKeyDown);
    window.addEventListener('scroll', place, true);
    window.addEventListener('resize', place);
    return () => {
      document.removeEventListener('pointerdown', onDocPointerDown, true);
      document.removeEventListener('keydown', onKeyDown);
      window.removeEventListener('scroll', place, true);
      window.removeEventListener('resize', place);
    };
  }, [anchorRef, onClose, place]);

  /* Keyboard entry and exit. The portal places the panel at the END of
   * <body>, so from the trigger, Tab would walk the entire rest of the page
   * before reaching it. Move focus INTO the panel on mount (first control, else
   * the panel itself) and back to the anchor on unmount — the APG disclosure
   * contract. Mount-only on purpose: re-running on prop change is the
   * focus-theft bug. */
  /* Restore on unmount: capture the anchor at mount, hand focus back on close. */
  useEffect(() => {
    const anchor = anchorRef.current;
    return () => {
      anchor?.focus({ preventScroll: true });
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- mount-only by contract
  }, []);

  /* Focus IN, keyed on `pos`, not on mount. The panel renders
   * `visibility: hidden` until place() has measured it, and place() runs in a
   * LAYOUT effect: React flushes the first commit's passive effects BEFORE
   * processing that layout-effect state update, so a mount-only focus ran
   * while the panel was still hidden — a no-op in Chromium (which refuses to
   * focus a hidden element) and invisible in jsdom (which does not enforce
   * visibility; the unit test passed while production did not). Once-only via
   * the ref: repositioning on scroll/resize must not re-steal focus. */
  const focusedIn = useRef(false);
  useEffect(() => {
    if (!pos || focusedIn.current) return;
    focusedIn.current = true;
    const panel = panelRef.current;
    const first = panel?.querySelector<HTMLElement>(
      'a[href],button:not([disabled]),input:not([disabled]):not([type="hidden"]),select,textarea,[tabindex]:not([tabindex="-1"])',
    );
    (first ?? panel)?.focus({ preventScroll: true });
  }, [pos]);

  return createPortal(
    <div
      ref={panelRef}
      id={id}
      // Read by lib/useDialog's focus trap: focus in here counts as inside the
      // dialog that opened this panel, not as an escape to be yanked back.
      data-transient-layer=""

      role={ariaLabel ? 'group' : undefined}
      tabIndex={-1}
      aria-label={ariaLabel}
      /* NO panel-wide click suppression. It used to preventDefault every click
       * in here, because React routes a portal's synthetic events up the REACT
       * tree — so a click in the panel reached the Browse card's wrapping
       * <Link> and navigated. That wrapper is gone (ListingCards), the Table's
       * <tr> has never had an onClick, and the listing header's pill is not
       * inside anything clickable; the three callers are the whole population.
       * Keeping it would break the one link the panels legitimately contain —
       * the collection popover's "Create a collection →". */
      style={{
        top: pos?.top ?? 0,
        left: pos?.left ?? 0,
        visibility: pos ? undefined : 'hidden',
      }}
      className={[
        // z-60: every modal in the app is z-50 and a popover opened FROM a modal
        // (Explore modals render Browse cards) must sit above it. A transient
        // panel is always on top of whatever opened it.
        'fixed z-[60] rounded-[var(--radius-md)] border border-[var(--color-rule-strong)]',
        'bg-[var(--color-paper-3)] shadow-[0_4px_16px_rgba(0,0,0,0.10)]',
        className,
      ].join(' ')}
    >
      {children}
    </div>,
    document.body,
  );
}
