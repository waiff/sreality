/* The one modal dialog. Backdrop + panel + close glyph, over lib/useDialog.
 *
 * Replaces thirteen hand-rolled modals. Six of the twelve that announced a
 * role put `role="dialog"` and `aria-modal` on the viewport-sized BACKDROP, so
 * the "dialog" a screen reader found was the dimmed overlay and the actual
 * panel was an unnamed div inside it. Here the split is fixed by construction:
 * the backdrop is `role="presentation"` and the PANEL carries the role, the
 * `aria-modal`, the name and `tabIndex={-1}`.
 *
 * PORTALLED TO <body>, deliberately. Three reasons, in order:
 *
 *   1. `position: fixed` is not enough. A transformed, filtered or
 *      `will-change` ancestor becomes the containing block for a fixed
 *      descendant, and any ancestor stacking context caps the panel's
 *      z-index — so an in-place modal's correctness depends on every ancestor
 *      of every call site, forever. Two of the thirteen (estimation/RunPanel,
 *      ImageLightbox) already open from deep inside page bodies.
 *   2. It is the only way the z-index MEANS anything. The backdrop's z-index
 *      is `MODAL_Z_BASE + rank` (lib/useDialog owns that ledger: 50..59 for
 *      modal layers, 60 for AnchoredPopover). A modal that stayed in the tree
 *      would lose to a portalled popover whenever the modal's own subtree sat
 *      in a lower-priority stacking context, and a nested pair could not order
 *      itself at all. Portalled, every overlay is a sibling in the root
 *      stacking context and the numbers decide.
 *   3. The suite already copes, and the blast radius was measured before
 *      choosing: `screen` queries `document.body`, so every getByRole /
 *      findByRole keeps working; 12 test files destructure `render(...)
 *      .container`, and none of them queries a dialog. The precedent is
 *      components/pipeline/PipelineStageMenu.test.tsx, which asserts the
 *      portal explicitly (`container.contains(menu) === false`) rather than
 *      quietly depending on placement.
 *
 * A dialog is ALWAYS named — the type below demands exactly one of `label` /
 * `labelledBy`, because two of the thirteen shipped unnamed.
 */
import { useRef, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { useDialog } from '@/lib/useDialog';

interface DialogBaseProps {
  open: boolean;
  onClose: () => void;
  children: ReactNode;
  /* Panel chrome — size and layout. Positioning, colour and the role live
   * here; callers set width/height the way AnchoredPopover's callers do. */
  className?: string;
}

/* Exactly one name, never zero. `label` is a literal string; `labelledBy` is
 * the id of the visible heading, which is better whenever there is one. */
type DialogName =
  | { label: string; labelledBy?: never }
  | { labelledBy: string; label?: never };

export type DialogProps = DialogBaseProps & DialogName;

const PANEL_BASE =
  'rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper)] shadow-2xl overflow-hidden';

export default function Dialog({ open, ...rest }: DialogProps) {
  /* Mounting IS opening — see lib/useDialog's header. The layer, the focus
   * effects and the scroll lock are all scoped to <DialogLayer>'s lifetime,
   * so an `open` flag never becomes an effect dependency. */
  if (!open) return null;
  return <DialogLayer {...rest} />;
}

function DialogLayer({
  onClose,
  children,
  className = '',
  label,
  labelledBy,
}: Omit<DialogBaseProps, 'open'> & Partial<Record<'label' | 'labelledBy', string>>) {
  const panelRef = useRef<HTMLDivElement>(null);
  const backdropRef = useRef<HTMLDivElement>(null);
  const { isTopLayer, zIndex } = useDialog({ onClose, panelRef, zRef: backdropRef });

  return createPortal(
    <div
      /* Presentational: it is the dim, not the dialog. Six of the twelve
       * modals this replaces announced themselves here instead. */
      ref={backdropRef}
      role="presentation"
      className="fixed inset-0 flex items-center justify-center p-3 sm:p-4 bg-[var(--color-ink)]/40 backdrop-blur-[2px]"
      /* NOT a `z-50` class: the layer's rank decides, so a dialog opened from
       * inside another paints over it whatever order the portals were appended
       * in. See the ledger in lib/useDialog. */
      style={{ zIndex }}
      onMouseDown={(e) => {
        // mousedown, not click: a drag that starts inside the panel and ends
        // on the backdrop must not count as a dismissal. Only a press that
        // both starts and lands on the backdrop itself closes, and only for
        // the frontmost layer.
        if (e.target === e.currentTarget && isTopLayer()) onClose();
      }}
    >
      <div
        ref={panelRef}
        // eslint-disable-next-line no-restricted-syntax -- this IS the primitive the ban points at.
        role="dialog"
        aria-modal="true"
        aria-label={label}
        aria-labelledby={labelledBy}
        /* Focusable but not tab-reachable: where useDialog parks initial focus
         * when the panel has no controls of its own. */
        tabIndex={-1}
        className={[PANEL_BASE, className].filter(Boolean).join(' ')}
      >
        {children}
      </div>
    </div>,
    document.body,
  );
}

/* THE close glyph. Six drifted definitions of this X existed across the
 * thirteen modals — different stroke widths, three of them unnamed. */
export function DialogClose({
  onClick,
  label = 'Close',
  className = '',
}: {
  onClick: () => void;
  /* Override only when "Close" would be ambiguous beside another close. */
  label?: string;
  className?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      className={[
        'shrink-0 px-2 py-1 text-[var(--color-ink-3)] hover:text-[var(--color-ink)] transition-colors',
        className,
      ]
        .filter(Boolean)
        .join(' ')}
    >
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden>
        <line x1="3.5" y1="3.5" x2="12.5" y2="12.5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
        <line x1="12.5" y1="3.5" x2="3.5" y2="12.5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      </svg>
    </button>
  );
}
