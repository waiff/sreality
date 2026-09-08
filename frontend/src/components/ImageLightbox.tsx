import React, { useCallback, useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import type { ImagePublic } from '@/lib/types';
import { imageSrc } from '@/lib/imageUrl';
import ImageTagBadge from '@/components/ImageTagBadge';
import ImageRenderBadge from '@/components/ImageRenderBadge';
import { DialogClose } from '@/components/Dialog';
import { useDialog } from '@/lib/useDialog';

/* The full-screen photo modal — arrow-key nav, the tag/render badges on the
 * enlarged photo. Extracted from listing-detail/Gallery (the property-detail image
 * expand) so the CLIP/pHash audit pages open the SAME modal instead of a second one-off —
 * it already operates on ImagePublic[], the exact shape images_public rows already are.
 *
 * BESPOKE CHROME over lib/useDialog, not <Dialog>: there is no card here. The
 * viewer is full-bleed — the photo centred in the dim, its counter, close and
 * arrows pinned to the VIEWPORT's edges — so <Dialog>'s backdrop+panel pair
 * would put the arrows at the photo's edges instead. So the element carrying
 * the role is viewport-sized too — which is NOT the backdrop mistake <Dialog>'s
 * header describes: the dim behind it is a separate presentation layer, and
 * everything reachable (counter, close, arrows, photo) sits INSIDE the element
 * that announces itself. The hook still owns
 * Escape layering, the focus trap, initial/restored focus, the z rank and the
 * ref-counted scroll lock; only the chrome is local.
 *
 * Portalled to <body> for the same reason <Dialog> is: this opens from deep
 * inside three different page bodies (the listing gallery, the labeling review
 * grids, the tag-contents gallery), and a `position: fixed` overlay left in the
 * tree inherits any ancestor's stacking context — which would cap its z-index
 * below the layer opened FROM it and make the rank meaningless.
 *
 * WHAT LEFT: its own document keydown listener (Escape), its own body
 * scroll-lock copy, its own close-glyph, and the four `stopPropagation`s that
 * existed only to keep a click on the chrome from reaching the dim's
 * `onClick={onClose}` — the dismissal is a target check now, so nothing needs
 * undoing. */

interface Props {
  images: ImagePublic[];
  startIndex: number;
  onClose: () => void;
  /* Extra classes on the enlarged <img> (Gallery's inactive-listing desaturation). */
  dim?: string;
  /* The tag badge for the photo at that position, replacing what the image row's
   * own CLIP call says. For grids whose tiles show a DIFFERENT tag (the labeling
   * page's proposed tag) so the enlarged photo can never contradict the tile it
   * was opened from. */
  tagAt?: (index: number) => { tag: string | null; confidence: number | null };
  /* An optional control bar under the photo, for the position on show. The
   * training-set review opens this viewer to DECIDE, not only to look, and
   * making the operator close it to reach the marks turns one judgement into
   * three clicks. Callers that only display photos pass nothing and the
   * viewer is unchanged. */
  actionsAt?: (index: number) => React.ReactNode;
  /* Single-key shortcuts for the photo on show, keyed by `e.key` lowercased —
   * with the space bar spelled 'space', because ' ' as an object key is too
   * easy to misread. They live HERE rather than in the calling page so they
   * inherit the one rule the arrow keys already follow: only the frontmost
   * layer answers. A page-level listener would fire under a dialog opened over
   * this one. */
  shortcuts?: Record<string, (index: number) => void>;
}

export default function ImageLightbox({
  images,
  startIndex,
  onClose,
  dim = '',
  tagAt,
  actionsAt,
  shortcuts,
}: Props) {
  const [index, setIndex] = useState(startIndex);
  const [errored, setErrored] = useState(false);
  const total = images.length;
  const counterId = useId();
  const dimRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const { isTopLayer, zIndex } = useDialog({ onClose, panelRef, zRef: dimRef });

  // The grid behind can shrink while the modal is open (a reviewed tile leaving
  // its tab), so the stored index is clamped rather than trusted — an
  // out-of-range one renders nothing while the dialog still holds the page's
  // scroll lock.
  const i = total > 0 ? Math.min(index, total - 1) : 0;

  const prev = useCallback(() => {
    setErrored(false);
    setIndex((i - 1 + total) % total);
  }, [i, total]);
  const next = useCallback(() => {
    setErrored(false);
    setIndex((i + 1) % total);
  }, [i, total]);

  /* Read through a ref so the arrow-key effect below never has to list the
   * handle — which is a fresh object every render — among its dependencies. */
  const isTopRef = useRef(isTopLayer);
  isTopRef.current = isTopLayer;
  /* Same reason as isTopRef: read through refs so the listener never has to
   * list a fresh-every-render object among its dependencies. */
  const shortcutsRef = useRef(shortcuts);
  shortcutsRef.current = shortcuts;
  const indexRef = useRef(0);

  /* THE ARROW KEYS, AND NOTHING ELSE. This effect used to also call
   * `closeBtnRef.current?.focus()` while depending on [onClose, prev, next] —
   * and prev/next take a new identity on every step — so each arrow press
   * re-ran the whole effect and slammed focus back onto Close, out of whatever
   * the operator had tabbed to. Initial focus belongs to useDialog now and is
   * mount-only by contract; what is left here only adds and removes a
   * listener, so re-registering as the position moves costs nothing.
   *
   * `window`, matching useDialog's own listener: an event dispatched at
   * `document` still bubbles up to it, so this is the superset of the two
   * placements. Only the frontmost layer answers, the same rule Escape
   * follows — an arrow press must not walk the gallery underneath a dialog
   * opened over it. */
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.defaultPrevented) return;
      if (!isTopRef.current()) return;
      if (e.key === 'ArrowLeft') { prev(); return; }
      if (e.key === 'ArrowRight') { next(); return; }
      /* A shortcut is a bare keypress: a modifier means the browser's own
       * command (⌘A, ctrl-D), and a field with focus means the operator is
       * typing a note, not deciding. */
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const el = e.target as HTMLElement | null;
      if (el && (el.isContentEditable
                 || ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName))) return;
      const run = shortcutsRef.current?.[e.key === ' ' ? 'space' : e.key.toLowerCase()];
      if (run) {
        /* preventDefault is load-bearing for SPACE, not a nicety. A focused
         * button activates on space, so after one click on a mark the next
         * space would fire that mark AND this shortcut. Preventing the keydown
         * suppresses the activation, so the space bar means one thing wherever
         * focus happens to sit. (It also stops the page scrolling behind, and
         * backspace navigating back, for any caller still mapping those.) */
        e.preventDefault();
        run(indexRef.current);
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [prev, next]);

  indexRef.current = i;
  const current = images[i];
  if (!current) return null;
  const badge = tagAt?.(i) ?? {
    tag: current.clip_fine_tag,
    confidence: current.clip_confidence,
  };

  return createPortal(
    <div
      /* Presentational: it is the dim, not the dialog. */
      ref={dimRef}
      role="presentation"
      className="fixed inset-0"
      /* NOT a `z-50` class: the layer's rank decides, so a dialog opened from
       * inside this one paints over it. See the ledger in lib/useDialog. */
      style={{ zIndex, background: 'rgba(20, 22, 27, 0.92)' }}
    >
      <div
        ref={panelRef}
        /* The viewer IS the dialog: full-bleed by design, with the photo and
         * every control inside it. The role sits here and not on the dim
         * above, which is the split six of the modals this program replaces
         * had backwards. */
        // eslint-disable-next-line no-restricted-syntax -- bespoke chrome over lib/useDialog (see the header); the layering, trap, Escape and scroll lock all come from the hook.
        role="dialog"
        aria-modal="true"
        /* Named by the visible counter — the rule ComparableModal's eyebrow
         * follows too: never a literal that could drift from the words on
         * screen. */
        aria-labelledby={counterId}
        /* Focusable but not tab-reachable: where useDialog parks focus if the
         * viewer ever renders without a control. */
        tabIndex={-1}
        onMouseDown={(e) => {
          // mousedown, not click: a drag that starts on the photo and ends on
          // the surrounding dark must not count as a dismissal. Only a press
          // that both starts and lands on the empty surface closes, and only
          // for the frontmost layer.
          if (e.target === e.currentTarget && isTopLayer()) onClose();
        }}
        className="absolute inset-0 flex items-center justify-center"
      >
        <div
          id={counterId}
          className="absolute top-3 left-1/2 -translate-x-1/2 px-2.5 py-1 text-[0.72rem] tracking-[0.18em] uppercase text-[var(--color-ink-4)] font-mono tabular-nums"
        >
          {i + 1} / {total}
        </div>

        <DialogClose
          onClick={onClose}
          tone="onDark"
          className="absolute top-3 right-3 w-9 h-9 flex items-center justify-center focus-visible:border focus-visible:border-[var(--color-copper)] rounded-[var(--radius-sm)]"
        />

        {total > 1 && (
          <>
            <button
              type="button"
              onClick={prev}
              aria-label="Previous photo"
              className="absolute left-2 md:left-6 top-1/2 -translate-y-1/2 w-11 h-11 flex items-center justify-center rounded-full text-[var(--color-ink-4)] hover:text-[var(--color-paper)] hover:bg-[var(--color-paper)]/10 focus-visible:border focus-visible:border-[var(--color-copper)]"
            >
              <ArrowGlyph dir="left" />
            </button>
            <button
              type="button"
              onClick={next}
              aria-label="Next photo"
              className="absolute right-2 md:right-6 top-1/2 -translate-y-1/2 w-11 h-11 flex items-center justify-center rounded-full text-[var(--color-ink-4)] hover:text-[var(--color-paper)] hover:bg-[var(--color-paper)]/10 focus-visible:border focus-visible:border-[var(--color-copper)]"
            >
              <ArrowGlyph dir="right" />
            </button>
          </>
        )}

        <div className="relative max-w-[92vw] max-h-[88vh] flex flex-col items-center justify-center gap-3">
        <div className="relative min-h-0 flex items-center justify-center">
          {errored ? (
            <div
              className="px-12 py-10 border border-[var(--color-rule-strong)] text-[var(--color-ink-4)] tracking-[0.14em] uppercase text-sm"
            >
              Image unavailable
            </div>
          ) : (
            <>
              <img
                key={current.id}
                src={imageSrc(current)}
                alt=""
                onError={() => setErrored(true)}
                className={[
                  // The bar is a SIBLING, not an overlay: it takes its height
                  // out of the photo's rather than sitting on top of it, so a
                  // control never covers the part being judged.
                  'max-w-[92vw] object-contain',
                  actionsAt ? 'max-h-[74vh]' : 'max-h-[88vh]',
                  'border border-[var(--color-copper)]/40',
                  dim,
                ].join(' ')}
              />
              <ImageTagBadge
                tag={badge.tag}
                confidence={badge.confidence}
                className="absolute bottom-2 left-2 text-[0.7rem]"
              />
              <ImageRenderBadge
                renderScore={current.clip_render_score}
                className="absolute bottom-2 right-2 text-[0.7rem]"
              />
            </>
          )}
        </div>
        {actionsAt && (
          <div data-testid="lightbox-actions" className="w-full max-w-[46rem] shrink-0">
            {actionsAt(i)}
          </div>
        )}
        </div>
      </div>
    </div>,
    document.body,
  );
}

function ArrowGlyph({ dir }: { dir: 'left' | 'right' }) {
  if (dir === 'left') {
    return (
      <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden>
        <polyline points="9,2 3,7 9,12" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  }
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden>
      <polyline points="5,2 11,7 5,12" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
