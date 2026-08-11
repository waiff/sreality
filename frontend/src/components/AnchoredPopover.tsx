/* A floating panel anchored to a trigger, rendered in a portal on <body>.
 *
 * Why a portal rather than the `absolute` popovers this app already has
 * (TagEditPopover, the filter dropdowns): those live inside their own container
 * and only work because that container neither clips nor stacks. The pipeline
 * funnel does not have that luxury — on a Browse card it sits inside an
 * `overflow-hidden` wrapper AND inside the card's <Link>, and on the Table it
 * sits inside a horizontal scroller. An absolutely-positioned menu there is
 * clipped to the photo, and every click inside it navigates. Portalling to
 * <body> with `position: fixed` escapes both.
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
  /* Accessible name for the floating container. */
  ariaLabel?: string;
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

  return createPortal(
    <div
      ref={panelRef}
      aria-label={ariaLabel}
      /* The panel routinely opens over a <Link> (Browse card) or a clickable
       * row (Table); without this, every click inside it also navigates. */
      onClick={(e) => {
        e.preventDefault();
        e.stopPropagation();
      }}
      style={{
        top: pos?.top ?? 0,
        left: pos?.left ?? 0,
        visibility: pos ? undefined : 'hidden',
      }}
      className={[
        'fixed z-40 rounded-[var(--radius-md)] border border-[var(--color-rule-strong)]',
        'bg-[var(--color-paper-3)] shadow-[0_4px_16px_rgba(0,0,0,0.10)]',
        className,
      ].join(' ')}
    >
      {children}
    </div>,
    document.body,
  );
}
