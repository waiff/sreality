/* The portalled anchored panel's keyboard contract.
 *
 * Found live, not in jsdom: the panel renders `visibility: hidden` until it is
 * measured, and a mount-only focus ran before that measurement — Chromium
 * refuses to focus a hidden element, jsdom does not care. These pin the
 * ordering (focus only once positioned) and the restore. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import { createRef, useState } from 'react';
import AnchoredPopover from './AnchoredPopover';

function Harness({ label }: { label?: string }) {
  const anchor = createRef<HTMLButtonElement>();
  const [open, setOpen] = useState(true);
  return (
    <>
      <button ref={anchor} type="button" onClick={() => setOpen((v) => !v)}>
        anchor
      </button>
      {open && (
        <AnchoredPopover anchorRef={anchor} onClose={() => setOpen(false)} ariaLabel={label} id="pop">
          <button type="button">first</button>
          <button type="button">second</button>
        </AnchoredPopover>
      )}
    </>
  );
}

beforeEach(() => {
  // place() measures the anchor and the panel; give jsdom real-looking boxes
  // so `pos` is set and the panel leaves its hidden-until-measured state.
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
    x: 100, y: 100, top: 100, left: 100, right: 140, bottom: 124, width: 40, height: 24, toJSON: () => ({}),
  } as DOMRect);
});
afterEach(() => vi.restoreAllMocks());

describe('<AnchoredPopover>', () => {
  it('moves focus to its first control only once it is positioned and visible', () => {
    render(<Harness label="Menu" />);
    const first = screen.getByRole('button', { name: 'first' });
    const panel = document.getElementById('pop')!;
    expect(document.activeElement).toBe(first);
    expect(panel.style.visibility).not.toBe('hidden');
  });

  it('is a named group with the id the trigger points at, marked as a transient layer', () => {
    render(<Harness label="Menu" />);
    const panel = screen.getByRole('group', { name: 'Menu' });
    expect(panel.id).toBe('pop');
    expect(panel).toHaveAttribute('data-transient-layer');
  });

  it('has no group role when it has no name', () => {
    render(<Harness />);
    expect(screen.queryByRole('group')).toBeNull();
  });

  it('hands focus back to the anchor when it closes', () => {
    render(<Harness label="Menu" />);
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'first' }));
    // Escape is the panel's own dismissal path.
    act(() => {
      document.activeElement!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    });
    expect(screen.queryByRole('group')).toBeNull();
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'anchor' }));
  });
});
