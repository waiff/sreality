/* The modal-dialog contract, as a hook.
 *
 * Thirteen hand-rolled modals shipped thirteen partial versions of this: 12
 * Escape listeners (8 on `window`, 4 on `document` — the split is exactly why
 * a nested pair double-closed, since a window listener also fires for an event
 * dispatched at document), 5 copies of the body scroll lock (each restoring
 * `overflow` independently, so an inner dialog's cleanup unlocked the page
 * while the outer one was still open), several with no initial focus at all
 * and three with focus-theft effects that re-ran on a prop change and yanked
 * focus back out of whatever the operator had tabbed to.
 *
 * This is a HOOK and not only a component because two dialogs own bespoke
 * chrome that <Dialog> cannot render — estimation/RunPanel is a side sheet
 * with its own layout, ImageLightbox is a full-bleed viewer — and they must
 * still get the same Escape, focus and scroll-lock behaviour. <Dialog>
 * (components/Dialog.tsx) is this hook plus the standard backdrop + panel.
 *
 * MOUNTING IS OPENING. Call this from a component that is mounted only while
 * the dialog is open (`{open && <Panel/>}`), never with an `open` flag: the
 * initial-focus and focus-restore effects are `[]` effects on purpose, and an
 * `open` dependency is precisely the re-run that caused the focus theft.
 *
 * ORDER IS A RENDER-PHASE FACT, NOT AN EFFECT-ORDER ONE. This is the part the
 * first cut of this file got wrong. Layers used to be ordered by the sequence
 * in which their effects pushed them, and React flushes effects within one
 * commit CHILD-BEFORE-PARENT — the exact inverse of z-order. A nested pair
 * that mounted in a single commit (a dialog whose body renders another one
 * open from its first render, or an outer dialog REMOUNTING while an inner is
 * open) therefore registered inner-first, `layers[last]` was the OUTER one,
 * Escape closed the wrong layer and the inner one could not be closed at all.
 * So each layer takes a monotonic number in the RENDER phase, where React does
 * go parent-before-child, and "top" is the MAX seq — never an array position.
 *
 * WHAT IT GUARANTEES
 *   - a MODULE-LEVEL layer stack with ONE window keydown listener. Only the
 *     top layer — max seq — answers Escape, so a nested pair closes one layer
 *     per press instead of both. `isTopLayer()` gates the backdrop click the
 *     same way.
 *   - a `zIndex` per layer, so paint order follows NESTING rather than the
 *     order the portals happened to be appended to <body> in.
 *   - Tab / Shift+Tab containment inside the panel (APG modal dialog): Tab
 *     from the last control wraps to the first, Shift+Tab from the first wraps
 *     to the last, and focus that has escaped the panel is pulled back.
 *   - initial focus to the first focusable control, or the panel itself when
 *     it has none (the panel therefore needs `tabIndex={-1}`).
 *   - focus RESTORE on unmount to whatever had focus at mount.
 *   - a REF-COUNTED body scroll lock: n open dialogs take one lock and the
 *     last close releases it, restoring the `overflow` value from before the
 *     FIRST lock rather than from before each one.
 *
 * The listener is on `window` deliberately. An event dispatched at `document`
 * still bubbles to `window`, but not the reverse, so `window` is the superset
 * of the two placements this replaces. AnchoredPopover keeps its own
 * `document` listener and stops propagation on Escape — a popover open inside
 * a dialog therefore closes the popover only, which is the ordering we want.
 *
 * HONEST LIMITS, so nobody over-trusts a green run:
 *   - jsdom has no layout, so "focusable" here is a selector, not a hit test.
 *     A control hidden by a CSS class counts as focusable in tests and does
 *     not in a browser. `aria-modal` is likewise an announcement, not
 *     enforcement — nothing here makes the rest of the page inert.
 *   - the rank below is published through an external store, and React
 *     subscribes to a store in a PASSIVE effect. For a pair that mounts in one
 *     commit there is therefore one painted frame before the inner layer's
 *     rank arrives, during which both backdrops sit at the base z-index and
 *     DOM order decides (which, for that case, already stacks them correctly —
 *     a portal is appended in tree order, parent first). Every later
 *     mount/unmount is corrected before paint, because the push below is a
 *     LAYOUT effect and the already-subscribed layers re-render synchronously.
 */
import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type RefObject,
} from 'react';

/* THE Z LEDGER — the app's overlay order, in one place:
 *     50 + rank   modal layers (this file). A nested dialog paints over the
 *                 one it opened from, whatever order the portals landed in.
 *     60          AnchoredPopover. A transient panel opened FROM a dialog (a
 *                 pipeline stage menu on a card inside an Explore modal) has
 *                 to sit above it, so popovers stay above the whole 50..59
 *                 band rather than above one particular layer.
 * Ten simultaneous modal layers is not a shape this app has; if it ever grows
 * one, widen the band here rather than clamping the rank — a clamp would lie
 * about the order instead of running out of room. */
export const MODAL_Z_BASE = 50;

interface Layer {
  /* Render-phase order. Parent < child, always. */
  seq: number;
  close: () => void;
  panel: () => HTMLElement | null;
  /* Set the outermost fixed element's z-index directly (see applyRanks). */
  applyZ: (z: number) => void;
}

/* Module-level: the whole point. Two dialogs mounted from unrelated subtrees
 * still have to agree on which of them is on top. Membership only — the array
 * ORDER means nothing, `seq` does. */
const layers: Layer[] = [];
let nextSeq = 0;

/* Called from a useState initializer, i.e. in render. That is a module
 * mutation during render, and it is safe precisely because the value is
 * write-only and monotonic: a double-invoked render (StrictMode) burns a
 * number instead of corrupting one, and an abandoned render leaves a gap
 * nothing reads. What it buys is the ordering React's effect flush cannot
 * give — parent before child, in the same commit. */
function takeSeq(): number {
  nextSeq += 1;
  return nextSeq;
}

/* The stack as a tiny external store. No version counter: the only thing a
 * layer reads is its own rank, which is a number, so React's own === on the
 * snapshot decides who re-renders and a layer whose rank did not move does
 * not. */
const subscribers = new Set<() => void>();

function subscribe(fn: () => void): () => void {
  subscribers.add(fn);
  return () => {
    subscribers.delete(fn);
  };
}

/* Paint order must be right on the FIRST frame. The passive subscription
 * (useSyncExternalStore) re-renders a frame late, and React commits portal
 * placements child-first — so for a pair mounting in one commit the OUTER
 * backdrop is appended AFTER the inner and would paint over it at a z tie.
 * Every push/pop therefore re-applies each layer's z-index imperatively,
 * inside the layout effect, before the browser paints. */
function applyRanks(): void {
  const sorted = [...layers].sort((a, b) => a.seq - b.seq);
  sorted.forEach((l, i) => l.applyZ(MODAL_Z_BASE + i));
}

function emit(): void {
  applyRanks();
  for (const fn of Array.from(subscribers)) fn();
}

/* The frontmost layer: MAX seq, never `layers[layers.length - 1]`. */
function topLayer(): Layer | null {
  let top: Layer | null = null;
  for (const layer of layers) {
    if (top === null || layer.seq > top.seq) top = layer;
  }
  return top;
}

/* This layer's index among the open layers sorted by seq — the nesting depth.
 * Counting is enough; sorting an array to read one index is not. */
function rankOf(seq: number): number {
  let rank = 0;
  for (const layer of layers) {
    if (layer.seq < seq) rank += 1;
  }
  return rank;
}

/* Tab-reachable descendants, in DOM order. `[tabindex="-1"]` is excluded (that
 * is how the panel itself parks) and `disabled` / `hidden` / an aria-hidden
 * ancestor are filtered out.
 *
 * EXPORTED because src/test/a11y.ts's expectDialogContract asserts the trap
 * against it. Two lists would let the assertion and the implementation drift
 * apart and each stay green; there is one list, and it lives with the trap. */
export const FOCUSABLE_SELECTOR = [
  'a[href]',
  'area[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  'summary',
  'iframe',
  'audio[controls]',
  'video[controls]',
  '[contenteditable]:not([contenteditable="false"])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

export function focusablesIn(panel: HTMLElement): HTMLElement[] {
  return Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
    (el) => !el.hasAttribute('hidden') && !el.closest('[aria-hidden="true"]'),
  );
}

function onWindowKeyDown(e: KeyboardEvent): void {
  const top = topLayer();
  if (!top) return;
  /* A widget INSIDE the dialog (a combobox closing its listbox) claims the key
   * by calling preventDefault; the dialog must not also act on it. */
  if (e.defaultPrevented) return;

  if (e.key === 'Escape') {
    top.close();
    return;
  }
  if (e.key !== 'Tab') return;

  const panel = top.panel();
  if (!panel) return;
  const items = focusablesIn(panel);
  const active = document.activeElement;
  if (items.length === 0) {
    // Nothing to cycle through: keep focus on the panel rather than letting
    // Tab walk out into the page behind the modal.
    e.preventDefault();
    panel.focus();
    return;
  }
  const first = items[0];
  const last = items[items.length - 1];
  /* A transient companion — an AnchoredPopover opened from inside this
   * dialog — is portalled to <body> as a SIBLING of the panel, so
   * `panel.contains` is false while focus is legitimately inside it. It marks
   * itself; the trap treats it as part of the layer and leaves it alone. */
  const inCompanion = active instanceof Element && active.closest('[data-transient-layer]') !== null;
  const outside = !(active instanceof Node) || (!panel.contains(active) && !inCompanion);
  if (e.shiftKey ? outside || active === first : outside || active === last) {
    e.preventDefault();
    (e.shiftKey ? last : first).focus();
  }
}

/* ONE lock, however many dialogs. `previousOverflow` is captured when the
 * count goes 0 → 1 and restored when it returns to 0; the five copies this
 * replaces each captured their own, so an inner dialog closing restored the
 * outer dialog's `hidden` — and, once the outer closed too, wrote that stale
 * `hidden` back onto <body>. */
let lockCount = 0;
let previousOverflow = '';

function acquireScrollLock(): () => void {
  if (lockCount === 0) {
    previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
  }
  lockCount += 1;
  let released = false;
  return () => {
    if (released) return;
    released = true;
    lockCount -= 1;
    if (lockCount === 0) document.body.style.overflow = previousOverflow;
  };
}

export interface DialogOptions {
  /* Dismiss this layer. Read through a ref, so a handler recreated on every
   * render never re-registers the layer or re-runs the focus effect. */
  onClose: () => void;
  /* The element carrying role="dialog" — the focus trap's boundary. It must
   * have `tabIndex={-1}` so it can hold focus when it has no controls. */
  panelRef: RefObject<HTMLElement | null>;
  /* The outermost fixed element (the backdrop) whose z-index follows the
   * layer's rank; applied imperatively on every stack change so the first
   * painted frame is already correct. Optional for bespoke-chrome consumers. */
  zRef?: RefObject<HTMLElement | null>;
}

export interface DialogHandle {
  /* True only for the frontmost layer. The backdrop click is gated on this so
   * a click that lands on an outer backdrop while an inner dialog is open
   * cannot close the outer one out from under it. */
  isTopLayer: () => boolean;
  /* What this layer's backdrop must paint at: MODAL_Z_BASE + nesting depth.
   * Bespoke chrome (RunPanel, ImageLightbox) sets it the same way <Dialog>
   * does — `style={{ zIndex }}` on the outermost fixed element. */
  zIndex: number;
}

export function useDialog({ onClose, panelRef, zRef }: DialogOptions): DialogHandle {
  /* Taken in RENDER, where parent runs before child. A useState initializer
   * runs exactly once per mount, so a REMOUNT takes a fresh, higher number —
   * which is right: the remounted dialog is a new layer, and re-keying an
   * outer dialog while an inner one is open must not make the outer the top. */
  const [seq] = useState(takeSeq);

  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  const layerRef = useRef<Layer | null>(null);

  const rank = useSyncExternalStore(subscribe, () => rankOf(seq));

  /* Layer registration + scroll lock. One effect, because the unmount ORDER
   * matters: pop the stack and release the lock together in the cleanup, so
   * the moment an inner dialog is gone the outer one is top again and still
   * holds the lock.
   *
   * A LAYOUT effect, so that a layer opening or closing over already-mounted
   * ones re-ranks them before the browser paints. Push order is still
   * child-first within a commit and that is fine — membership is not ordering;
   * `seq` is. */
  useLayoutEffect(() => {
    const layer: Layer = {
      seq,
      close: () => closeRef.current(),
      panel: () => panelRef.current,
      applyZ: (z) => {
        if (zRef?.current) zRef.current.style.zIndex = String(z);
      },
    };
    layerRef.current = layer;
    layers.push(layer);
    if (layers.length === 1) window.addEventListener('keydown', onWindowKeyDown);
    const releaseLock = acquireScrollLock();
    emit();
    return () => {
      /* Pop by IDENTITY. Two layers are never interchangeable and an index
       * captured at push time would be stale the moment anything below this
       * layer closed first. */
      const i = layers.indexOf(layer);
      if (i >= 0) layers.splice(i, 1);
      if (layers.length === 0) window.removeEventListener('keydown', onWindowKeyDown);
      layerRef.current = null;
      releaseLock();
      emit();
    };
  }, [panelRef, seq, zRef]);

  /* Initial focus + restore, in their OWN [] effect. NEVER add a dependency
   * here: re-running this is the focus-theft bug — the dialog yanks focus back
   * to its first control every time a prop changes, mid-typing. */
  useEffect(() => {
    const restoreTo = document.activeElement;
    const panel = panelRef.current;
    /* Only the TOP layer takes initial focus. Passive effects flush
     * child-before-parent within a commit, so in a same-commit nested pair
     * the OUTER dialog's effect runs last and would steal focus from the
     * inner — the same inversion the render-phase `seq` fixes for ordering,
     * one effect phase over. By the time this runs both layers are pushed
     * (layout effects already ran), so topLayer() is the truth. */
    if (panel && topLayer() === layerRef.current) {
      const first = focusablesIn(panel)[0];
      (first ?? panel).focus();
    }
    return () => {
      /* Not `instanceof HTMLElement`: an <svg> control is an SVGElement and
       * focusable all the same, and a trigger inside a same-origin iframe
       * belongs to another realm where that check is false for everything.
       * Element + a real `focus` is the honest test. */
      if (restoreTo instanceof Element && restoreTo.isConnected && 'focus' in restoreTo) {
        (restoreTo as HTMLElement).focus();
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- mount-only by contract; see above.
  }, []);

  return {
    isTopLayer: () => layerRef.current !== null && topLayer() === layerRef.current,
    zIndex: MODAL_Z_BASE + rank,
  };
}

/* Test-only: how many layers are open. Exported so a test can prove Escape
 * pops exactly one, instead of inferring it from whatever happens to still be
 * rendered. */
export function openDialogLayerCount(): number {
  return layers.length;
}

/* Test-only: the panel of the layer that Escape and the backdrop click will
 * act on. The stack-ordering bug was invisible to every assertion made against
 * what was RENDERED — both dialogs were still on screen — so the tests ask the
 * stack itself who is on top. */
export function topDialogPanel(): HTMLElement | null {
  return topLayer()?.panel() ?? null;
}
