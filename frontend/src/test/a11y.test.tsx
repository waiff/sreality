/* The helpers must be able to FAIL, or a green run proves nothing. Each one is
 * shown here catching the defect it exists for and passing the correct shape. */
import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/react';
import { useRef, type KeyboardEvent } from 'react';
import { expectNoNestedInteractive, expectRovingGroup, findNestedInteractive } from './a11y';

describe('expectNoNestedInteractive', () => {
  it('catches a button inside an anchor — the Browse-card shape axe cannot see', () => {
    const { container } = render(
      <a href="#outer">
        listing <button type="button">save</button>
      </a>,
    );
    const pairs = findNestedInteractive(container);
    expect(pairs).toHaveLength(1);
    expect(pairs[0].outer.tagName).toBe('A');
    expect(pairs[0].inner.tagName).toBe('BUTTON');
    expect(() => expectNoNestedInteractive(container)).toThrow(/inside <a>/);
  });

  it('catches an anchor inside an anchor', () => {
    const { container } = render(
      <a href="#outer">
        <a href="#inner">inner</a>
      </a>,
    );
    expect(findNestedInteractive(container)).toHaveLength(1);
  });

  it('catches a control inside a role="button" div', () => {
    const { container } = render(
      <div role="button" tabIndex={0}>
        card <button type="button">save</button>
      </div>,
    );
    expect(findNestedInteractive(container)).toHaveLength(1);
  });

  it('passes the sibling-controls shape', () => {
    const { container } = render(
      <div>
        <h3>
          <a href="#outer">listing</a>
        </h3>
        <button type="button">save</button>
      </div>,
    );
    expectNoNestedInteractive(container);
  });

  it('does not count a tabindex=-1 element as an interactive wrapper', () => {
    // A dialog or roving-group container parks itself at -1 to be focusable
    // programmatically; that is not "an interactive element wrapping others".
    const { container } = render(
      <div tabIndex={-1}>
        <button type="button">ok</button>
      </div>,
    );
    expectNoNestedInteractive(container);
  });

  it('names the accessible-name pollution the nesting causes', () => {
    const { getByRole } = render(
      <a href="#outer">
        <span>Byt 2+kk</span>
        <button type="button">Uložit do kolekce</button>
      </a>,
    );
    // The card link no longer names a listing; it names every control inside.
    expect(getByRole('link')).toHaveAccessibleName('Byt 2+kk Uložit do kolekce');
  });
});

/* A minimal roving menu: all items tabindex -1, arrow keys move focus. */
function Roving({ broken = false }: { broken?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  const onKeyDown = (e: KeyboardEvent) => {
    if (broken) return;
    const items = Array.from(ref.current!.querySelectorAll<HTMLElement>('[role="menuitem"]'));
    const i = items.indexOf(document.activeElement as HTMLElement);
    const next =
      e.key === 'Home' ? 0
      : e.key === 'End' ? items.length - 1
      : e.key === 'ArrowDown' ? (i + 1) % items.length
      : e.key === 'ArrowUp' ? (i - 1 + items.length) % items.length
      : -1;
    if (next >= 0) items[next].focus();
  };
  return (
    <div ref={ref} role="menu" aria-label="m" onKeyDown={onKeyDown}>
      <button role="menuitem" tabIndex={-1}>one</button>
      <button role="menuitem" tabIndex={-1}>two</button>
      <button role="menuitem" tabIndex={-1}>three</button>
    </div>
  );
}

describe('expectRovingGroup', () => {
  it('passes a menu that honours ArrowDown/ArrowUp/Home/End', () => {
    const { getByRole } = render(<Roving />);
    expectRovingGroup(getByRole('menu'), {
      itemRole: 'menuitem', next: 'ArrowDown', prev: 'ArrowUp', homeEnd: true,
    });
  });

  it('fails a menu that announces the role but ignores the keys', () => {
    const { getByRole } = render(<Roving broken />);
    expect(() =>
      expectRovingGroup(getByRole('menu'), { itemRole: 'menuitem', next: 'ArrowDown', prev: 'ArrowUp' }),
    ).toThrow(/ArrowDown should move focus/);
  });
});
