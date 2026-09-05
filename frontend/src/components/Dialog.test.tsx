/* <Dialog> — the primitive, and the nested pair that broke every hand-rolled
 * version of it.
 *
 * Two things are proved here that no single-dialog test can see:
 *   - Escape closes ONE layer. With 12 listeners split across `window` and
 *     `document`, an inner dialog's Escape also reached the outer one's
 *     listener (an event dispatched at document bubbles to window), so opening
 *     a picker inside a modal and pressing Escape closed both.
 *   - the scroll lock is REF-COUNTED. Five copies each captured their own
 *     `previousOverflow`: the inner dialog captured 'hidden' from the outer
 *     one, so closing the inner released nothing and closing the outer then
 *     wrote 'hidden' back onto <body> permanently.
 *
 * The third block below covers what the second one CANNOT: it opens both
 * layers in one commit. "<Dialog> nested pair" clicks the inner one open, so
 * the two layers register in two separate commits and any ordering scheme —
 * including the effect-order one this file's first cut shipped — looks
 * correct. React flushes effects within a single commit child-before-parent,
 * so a pair that mounts together registers inner-first, and a stack ordered by
 * push order then hands "top" to the OUTER layer: Escape closes the wrong one
 * and the inner cannot be closed at all. Same for an outer dialog that
 * REMOUNTS (a key change) while an inner one is open.
 */
import { createPortal } from 'react-dom';
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';

import { expectDialogContract, expectNoNestedInteractive } from '@/test/a11y';
import Dialog, { DialogClose } from './Dialog';
import { MODAL_Z_BASE, openDialogLayerCount, topDialogPanel } from '@/lib/useDialog';

function Simple({ label = 'Details' }: { label?: string }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        open
      </button>
      <Dialog open={open} onClose={() => setOpen(false)} label={label}>
        <h2>Details</h2>
        <button type="button">first</button>
        <button type="button">second</button>
        <DialogClose onClick={() => setOpen(false)} />
      </Dialog>
    </>
  );
}

/* An outer dialog whose body opens a second one — the shape that double-closed. */
function Nested() {
  const [outer, setOuter] = useState(false);
  const [inner, setInner] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOuter(true)}>
        open outer
      </button>
      <Dialog open={outer} onClose={() => setOuter(false)} label="Outer">
        <button type="button" onClick={() => setInner(true)}>
          open inner
        </button>
        <Dialog open={inner} onClose={() => setInner(false)} label="Inner">
          <button type="button">inner control</button>
        </Dialog>
      </Dialog>
    </>
  );
}

describe('<Dialog>', () => {
  it('honours the whole dialog contract', () => {
    render(<Simple />);
    const trigger = screen.getByRole('button', { name: 'open' });
    expectDialogContract({ trigger, open: () => fireEvent.click(trigger) });
  });

  it('puts the role, aria-modal and the name on the PANEL, not the backdrop', () => {
    render(<Simple label="Details panel" />);
    fireEvent.click(screen.getByRole('button', { name: 'open' }));
    const panel = screen.getByRole('dialog', { name: 'Details panel' });
    // The backdrop is the panel's parent and must announce nothing. Six of the
    // twelve modals this replaces had it the other way round.
    const backdrop = panel.parentElement!;
    expect(backdrop.getAttribute('role')).toBe('presentation');
    expect(backdrop.hasAttribute('aria-modal')).toBe(false);
    expect(panel.getAttribute('aria-modal')).toBe('true');
  });

  it('takes its name from a visible heading via labelledBy', () => {
    function Titled() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>
            open
          </button>
          <Dialog open={open} onClose={() => setOpen(false)} labelledBy="dlg-title">
            <h2 id="dlg-title">Okolí nemovitosti</h2>
            <DialogClose onClick={() => setOpen(false)} />
          </Dialog>
        </>
      );
    }
    render(<Titled />);
    fireEvent.click(screen.getByRole('button', { name: 'open' }));
    // The type demands exactly one of label / labelledBy, so "unnamed" — the
    // state two of the thirteen shipped in — is not expressible.
    expect(screen.getByRole('dialog')).toHaveAccessibleName('Okolí nemovitosti');
  });

  it('renders in a portal on <body>, at the foot of the modal z-band', () => {
    const { container } = render(<Simple />);
    fireEvent.click(screen.getByRole('button', { name: 'open' }));
    const panel = screen.getByRole('dialog');
    expect(container.contains(panel)).toBe(false);
    expect(document.body.contains(panel)).toBe(true);
    /* THE Z LEDGER (lib/useDialog): 50 + rank for modal layers, 60 for
     * AnchoredPopover. A lone dialog is rank 0. It is an inline style and not
     * a `z-50` class precisely so the rank can move it. */
    expect(panel.parentElement!.style.zIndex).toBe(String(MODAL_Z_BASE));
    expect(panel.parentElement!.className).not.toContain('z-50');
  });

  it('closes on a backdrop press but not on a press inside the panel', () => {
    render(<Simple />);
    fireEvent.click(screen.getByRole('button', { name: 'open' }));
    const panel = screen.getByRole('dialog');
    fireEvent.mouseDown(panel);
    expect(screen.queryByRole('dialog')).toBeInTheDocument();
    fireEvent.mouseDown(panel.parentElement!);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('has no interactive element nested inside another', () => {
    render(<Simple />);
    fireEvent.click(screen.getByRole('button', { name: 'open' }));
    expectNoNestedInteractive(screen.getByRole('dialog'));
  });

  it('parks focus on the panel itself when it holds no control', () => {
    function Empty() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>
            open
          </button>
          <Dialog open={open} onClose={() => setOpen(false)} label="Empty">
            <p>nothing to operate</p>
          </Dialog>
        </>
      );
    }
    render(<Empty />);
    fireEvent.click(screen.getByRole('button', { name: 'open' }));
    expect(document.activeElement).toBe(screen.getByRole('dialog'));
  });

  it('leaves body overflow untouched when no dialog is open', () => {
    render(<Simple />);
    expect(document.body.style.overflow).toBe('');
    expect(openDialogLayerCount()).toBe(0);
  });
});

describe('<Dialog> nested pair', () => {
  /* Each trigger is FOCUSED before it is clicked: a real click focuses the
   * button it hits, and jsdom's fireEvent.click does not. Skipping that would
   * make the focus-restore assertions prove nothing but document.body. */
  function openBoth() {
    render(<Nested />);
    const outerTrigger = screen.getByRole('button', { name: 'open outer' });
    outerTrigger.focus();
    fireEvent.click(outerTrigger);
    const innerTrigger = screen.getByRole('button', { name: 'open inner' });
    innerTrigger.focus();
    fireEvent.click(innerTrigger);
    expect(openDialogLayerCount()).toBe(2);
    expect(screen.getAllByRole('dialog')).toHaveLength(2);
  }

  it('closes ONE layer per Escape, innermost first, and holds the lock until the last', () => {
    openBoth();
    expect(document.body.style.overflow).toBe('hidden');

    fireEvent.keyDown(document.activeElement ?? document, { key: 'Escape' });
    // The inner one — and only it.
    expect(openDialogLayerCount()).toBe(1);
    expect(screen.getByRole('dialog', { name: 'Outer' })).toBeInTheDocument();
    expect(screen.queryByRole('dialog', { name: 'Inner' })).not.toBeInTheDocument();
    /* THE regression this exists for: one dialog is still open, so the lock is
     * still held. The five per-modal copies released it here. */
    expect(document.body.style.overflow).toBe('hidden');

    fireEvent.keyDown(document.activeElement ?? document, { key: 'Escape' });
    expect(openDialogLayerCount()).toBe(0);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    // ...and only now is it released, back to the value from before the FIRST
    // lock rather than to the inner dialog's captured 'hidden'.
    expect(document.body.style.overflow).toBe('');
  });

  it('returns focus down the stack: inner → its trigger, outer → the page trigger', () => {
    openBoth();
    const innerTrigger = screen.getByRole('button', { name: 'open inner' });
    fireEvent.keyDown(document.activeElement ?? document, { key: 'Escape' });
    expect(document.activeElement).toBe(innerTrigger);

    const outerTrigger = screen.getByRole('button', { name: 'open outer' });
    fireEvent.keyDown(document.activeElement ?? document, { key: 'Escape' });
    expect(document.activeElement).toBe(outerTrigger);
  });

  it('traps Tab in the INNER panel while it is on top', () => {
    openBoth();
    const inner = screen.getByRole('dialog', { name: 'Inner' });
    const only = screen.getByRole('button', { name: 'inner control' });
    only.focus();
    fireEvent.keyDown(only, { key: 'Tab' });
    expect(inner.contains(document.activeElement)).toBe(true);
    fireEvent.keyDown(document.activeElement!, { key: 'Tab', shiftKey: true });
    expect(inner.contains(document.activeElement)).toBe(true);
  });

  it('ignores a backdrop press on the layer that is not on top', () => {
    openBoth();
    const outerBackdrop = screen.getByRole('dialog', { name: 'Outer' }).parentElement!;
    fireEvent.mouseDown(outerBackdrop);
    expect(openDialogLayerCount()).toBe(2);
  });
});

/* Both layers open from the FIRST render, so they mount in ONE commit — the
 * case the click-driven fixture above cannot reach. */
function SameCommitPair() {
  const [outer, setOuter] = useState(true);
  const [inner, setInner] = useState(true);
  return (
    <Dialog open={outer} onClose={() => setOuter(false)} label="Outer">
      <button type="button">outer control</button>
      <Dialog open={inner} onClose={() => setInner(false)} label="Inner">
        <button type="button">inner control</button>
      </Dialog>
    </Dialog>
  );
}

/* The inner dialog's open state lives OUTSIDE the outer one, so re-keying the
 * outer remounts it — and its whole subtree, the inner dialog included — in a
 * single commit without the inner one being closed first. */
function RemountablePair({ outerKey }: { outerKey: string }) {
  const [inner, setInner] = useState(true);
  return (
    <Dialog key={outerKey} open onClose={() => {}} label="Outer">
      <button type="button">outer control</button>
      <Dialog open={inner} onClose={() => setInner(false)} label="Inner">
        <button type="button">inner control</button>
      </Dialog>
    </Dialog>
  );
}

const backdropZ = (name: string): number =>
  Number(screen.getByRole('dialog', { name }).parentElement!.style.zIndex);

describe('<Dialog> layers that register in ONE commit', () => {
  it('ranks a same-commit nested pair by RENDER order, not effect order', () => {
    render(<SameCommitPair />);
    expect(openDialogLayerCount()).toBe(2);

    /* The claim the DOM cannot make for us: both dialogs are on screen either
     * way, so the stack itself has to say which one Escape will reach. Ordered
     * by push, this is the OUTER panel — effects flush child-before-parent. */
    expect(topDialogPanel()).toBe(screen.getByRole('dialog', { name: 'Inner' }));
    // initial focus lands in the INNER layer, not the outer — passive effects
    // flush child-first too, so without the top-only guard the outer wins.
    expect(screen.getByRole('dialog', { name: 'Inner' }).contains(document.activeElement)).toBe(true);
    // and the first painted frame already has the inner above the outer
    expect(Number(screen.getByRole('dialog', { name: 'Inner' }).parentElement!.style.zIndex))
      .toBeGreaterThan(Number(screen.getByRole('dialog', { name: 'Outer' }).parentElement!.style.zIndex));

    /* Paint order follows nesting for the same reason, and through the same
     * seq: rank 0 → 50, rank 1 → 51, both under AnchoredPopover's 60. */
    expect(backdropZ('Outer')).toBe(MODAL_Z_BASE);
    expect(backdropZ('Inner')).toBe(MODAL_Z_BASE + 1);
    expect(backdropZ('Outer')).toBeLessThan(backdropZ('Inner'));
    expect(document.body.style.overflow).toBe('hidden');

    fireEvent.keyDown(window, { key: 'Escape' });
    // ONE layer, the inner one. With the stack ordered by push this closed the
    // outer — taking the inner down with it, since it renders inside it.
    expect(openDialogLayerCount()).toBe(1);
    expect(screen.queryByRole('dialog', { name: 'Inner' })).not.toBeInTheDocument();
    expect(screen.getByRole('dialog', { name: 'Outer' })).toBeInTheDocument();
    expect(topDialogPanel()).toBe(screen.getByRole('dialog', { name: 'Outer' }));
    // One dialog still open ⇒ the ref-counted lock is still held.
    expect(document.body.style.overflow).toBe('hidden');

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(openDialogLayerCount()).toBe(0);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(document.body.style.overflow).toBe('');
  });

  it('keeps the inner layer on top when the OUTER one remounts under it', () => {
    const { rerender } = render(<RemountablePair outerKey="a" />);
    expect(openDialogLayerCount()).toBe(2);

    rerender(<RemountablePair outerKey="b" />);
    // Both layers were re-pushed in one commit; the old pair was popped, not
    // stranded.
    expect(openDialogLayerCount()).toBe(2);
    expect(topDialogPanel()).toBe(screen.getByRole('dialog', { name: 'Inner' }));
    expect(backdropZ('Outer')).toBeLessThan(backdropZ('Inner'));

    /* The bug this rail exists for: the remounted outer became "top", so
     * Escape ran its no-op onClose and the inner dialog could not be closed at
     * all. A remount takes a FRESH seq — higher than the layer it replaces,
     * still lower than the child rendered after it. */
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByRole('dialog', { name: 'Inner' })).not.toBeInTheDocument();
    expect(screen.getByRole('dialog', { name: 'Outer' })).toBeInTheDocument();
    expect(openDialogLayerCount()).toBe(1);
    expect(document.body.style.overflow).toBe('hidden');
  });
});

/* A popover opened from inside a dialog is portalled to <body> as a SIBLING of
 * the panel. The trap must treat focus inside it as inside the layer, or every
 * Tab inside the popover is yanked back into the dialog. */
describe('<Dialog> with a transient companion', () => {
  function WithCompanion() {
    return (
      <Dialog open onClose={() => {}} label="Host">
        <button type="button">host control</button>
        {createPortal(
          <div data-transient-layer="">
            <button type="button">companion a</button>
            <button type="button">companion b</button>
          </div>,
          document.body,
        )}
      </Dialog>
    );
  }
  it('does not yank focus out of a companion panel on Tab', () => {
    render(<WithCompanion />);
    const a = screen.getByRole('button', { name: 'companion a' });
    a.focus();
    expect(document.activeElement).toBe(a);
    fireEvent.keyDown(a, { key: 'Tab' });
    // The trap left it alone: focus is still inside the companion (jsdom
    // does not move focus natively on Tab, so "still on a" is the signal).
    expect(document.activeElement).toBe(a);
  });
});
