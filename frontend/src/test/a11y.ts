/* Rendered-role assertions for interactive elements.
 *
 * The rail for the interactive-semantics program. Every property the program
 * promises — correct role, honest accessible name, keyboard operability, no
 * interactive nested inside another — is proved here against the RENDERED DOM
 * and never inferred from a class string. That distinction is the whole point:
 * the shipped route-registry program already documented (eslint.config.js)
 * that a lint selector cannot see across a component boundary, and every
 * defect this program fixes lives across one.
 *
 * WHY THIS IS HAND-WRITTEN AND NOT axe-core. Measured in this jsdom before
 * choosing (probe run and deleted, 2026-09-05): axe's `nested-interactive`
 * rule fires for a <button> or role="button" wrapping a control, but NOT for
 * an <a> wrapping one — ARIA's `link` role is not children-presentational, so
 * the rule's scope excludes it by design. The Browse card, the program's
 * highest-value defect, is an <a> wrapping eight controls. A dependency that
 * structurally cannot see the flagship case does not earn its place under the
 * project's no-new-dependency rule; the ten-line query below does see it. The
 * name-pollution half is already provable with jest-dom's
 * `toHaveAccessibleName`, which is installed and wired in vitest.setup.ts.
 *
 * HONEST LIMITS OF jsdom, so nobody over-trusts a green run:
 *   - no CSS is loaded, so :focus-visible, colour contrast and anything hidden
 *     purely by a Tailwind class are invisible here. A class-hidden control
 *     still counts as present. Focus VISIBILITY is a browser concern; focus
 *     MANAGEMENT (where document.activeElement goes) is what these assert.
 *   - keyboard events are dispatched, not typed; a handler that reads
 *     `event.key` is exercised, one that relies on native default actions is
 *     not.
 */
import { expect } from 'vitest';
import { fireEvent } from '@testing-library/react';

/* The focus trap's OWN definition of "focusable". Imported rather than
 * restated: an assertion written against a second list could cycle a different
 * set of controls than lib/useDialog does and still go green. INTERACTIVE_
 * SELECTOR below answers a different question — "is this thing interactive at
 * all", for the nesting and name checks — and the two are not interchangeable. */
import { focusablesIn, topDialogPanel } from '@/lib/useDialog';

/* Everything the HTML spec calls "interactive content", plus the ARIA roles
 * that make a generic element behave as one. `tabindex="-1"` is excluded on
 * purpose: it marks a programmatically-focusable but not tab-reachable
 * element, which is how roving groups and dialogs park their non-current
 * items. */
export const INTERACTIVE_SELECTOR = [
  'a[href]',
  'button',
  'input:not([type="hidden"])',
  'select',
  'textarea',
  'summary',
  '[tabindex]:not([tabindex="-1"])',
  '[role="button"]',
  '[role="link"]',
  '[role="checkbox"]',
  '[role="switch"]',
  '[role="tab"]',
  '[role="menuitem"]',
  '[role="menuitemradio"]',
  '[role="menuitemcheckbox"]',
  '[role="option"]',
  '[role="combobox"]',
  '[role="textbox"]',
  '[role="slider"]',
].join(',');

function describeEl(el: Element): string {
  const role = el.getAttribute('role');
  const id = el.id ? `#${el.id}` : '';
  const text = (el.textContent ?? '').trim().replace(/\s+/g, ' ').slice(0, 40);
  return `<${el.tagName.toLowerCase()}${id}${role ? ` role="${role}"` : ''}>${text ? ` "${text}"` : ''}`;
}

/* Find every interactive element that sits inside another interactive element.
 * Returns the offending pairs so the assertion message names them; an empty
 * array means the subtree is clean. Exported separately from the expect
 * wrapper so a test can also assert the NEGATIVE on a component that is still
 * broken, without lying about it. */
export function findNestedInteractive(root: ParentNode): Array<{ outer: Element; inner: Element }> {
  const out: Array<{ outer: Element; inner: Element }> = [];
  for (const inner of Array.from(root.querySelectorAll(INTERACTIVE_SELECTOR))) {
    const outer = inner.parentElement?.closest(INTERACTIVE_SELECTOR) ?? null;
    if (outer && outer !== inner && root.contains(outer)) out.push({ outer, inner });
  }
  return out;
}

/* Assert that no interactive element is a descendant of another one. This is
 * the query axe cannot run for <a> wrappers (see header). */
export function expectNoNestedInteractive(root: ParentNode): void {
  const pairs = findNestedInteractive(root);
  if (pairs.length === 0) return;
  // A plain Error so the offending pairs are IN the message, not only in a
  // diff a CI log may truncate.
  throw new Error(
    `interactive elements nested inside another interactive element:\n` +
      pairs.map((p) => `  ${describeEl(p.inner)} inside ${describeEl(p.outer)}`).join('\n'),
  );
}

/* Assert a roving group honours its arrow-key contract: with focus on the
 * first item, the given keys move focus to the expected sibling. The caller
 * supplies which keys the widget promises (a menu promises all four; a tablist
 * promises Left/Right; a listbox promises Up/Down), so the helper never
 * asserts a contract the role does not carry. */
export function expectRovingGroup(
  group: HTMLElement,
  opts: {
    /* Every role the widget's arrow keys traverse. A menu mixes menuitem and
     * menuitemradio; pass both, or Home/End will be asserted against the wrong
     * "last" item. */
    itemRole: string | string[];
    next: 'ArrowDown' | 'ArrowRight';
    prev: 'ArrowUp' | 'ArrowLeft';
    homeEnd?: boolean;
  },
): void {
  const roles = Array.isArray(opts.itemRole) ? opts.itemRole : [opts.itemRole];
  const selector = roles.map((r) => `[role="${r}"]`).join(',');
  const items = Array.from(group.querySelectorAll<HTMLElement>(selector)).filter(
    (el) => !el.hasAttribute('disabled') && el.getAttribute('aria-disabled') !== 'true',
  );
  expect(items.length, `expected at least two ${selector} items`).toBeGreaterThan(1);

  items[0].focus();
  expect(document.activeElement, 'focus should be settable on the first item').toBe(items[0]);

  fireEvent.keyDown(items[0], { key: opts.next });
  expect(document.activeElement, `${opts.next} should move focus to the second item`).toBe(items[1]);

  fireEvent.keyDown(items[1], { key: opts.prev });
  expect(document.activeElement, `${opts.prev} should move focus back to the first item`).toBe(items[0]);

  if (opts.homeEnd) {
    fireEvent.keyDown(items[0], { key: 'End' });
    expect(document.activeElement, 'End should move focus to the last item').toBe(items[items.length - 1]);
    fireEvent.keyDown(items[items.length - 1], { key: 'Home' });
    expect(document.activeElement, 'Home should move focus to the first item').toBe(items[0]);
  }
}

/* Assert the whole modal-dialog contract against a rendered trigger + dialog:
 * focus goes INSIDE on open, Tab wraps at both ends, Escape closes exactly ONE
 * layer, focus comes back to the trigger, and the body scroll lock is
 * released. `open` is whatever gesture opens it (usually a click on the
 * trigger); `trigger` is where focus must return.
 *
 * WHAT jsdom CAN see here: document.activeElement (so every focus claim is
 * real), the presence and count of role="dialog" nodes, the `key` on a
 * dispatched keydown, and document.body.style.overflow.
 *
 * WHAT IT CANNOT, so nobody over-trusts a green run:
 *   - no layout and no CSS, so a control hidden by a Tailwind class still
 *     counts as focusable and still takes a turn in the Tab cycle. The trap's
 *     ORDER is proved; its visual truthfulness is not.
 *   - Tab does not natively move focus in jsdom. `fireEvent.keyDown(…, { key:
 *     'Tab' })` exercises the dialog's own handler — which IS the code under
 *     test — but the assertions below therefore start focus on a known control
 *     rather than assuming a Tab walked there, and they cannot prove that the
 *     browser's native sequencing between the wrap points is also correct.
 *   - `aria-modal` is an announcement, not enforcement: nothing here proves
 *     the page behind the dialog is inert, because in a browser it is not
 *     either (only <dialog>.showModal() gives that).
 *   - the scroll lock is read off document.body.style.overflow only. A lock
 *     implemented on <html>, or with `position: fixed`, would read as absent. */
export function expectDialogContract(opts: {
  /* Opens the dialog. Runs inside the helper so the BEFORE state (focus,
   * overflow, layer count) is captured first. */
  open: () => void;
  /* The control that opened it — focus must come back here on close. */
  trigger: HTMLElement;
  /* Where to look for the dialog. Defaults to document.body, which is right
   * for a portalled dialog and harmless for one rendered in place. */
  within?: ParentNode;
}): void {
  const root = opts.within ?? document.body;
  const dialogs = () => Array.from(root.querySelectorAll<HTMLElement>('[role="dialog"]'));

  const overflowBefore = document.body.style.overflow;
  const layersBefore = dialogs().length;
  opts.trigger.focus();

  opts.open();

  const openDialogs = dialogs();
  expect(openDialogs.length, 'opening should add exactly one role="dialog"').toBe(
    layersBefore + 1,
  );
  /* The newest layer is the one on top — and the one that must answer. */
  // The stack says who is on top; DOM order is exactly the heuristic the
  // ordering bug hid behind.
  const dialog = topDialogPanel() ?? openDialogs[openDialogs.length - 1];
  expect(
    dialog.getAttribute('aria-modal'),
    'a modal dialog must say so — and on the PANEL, not the backdrop',
  ).toBe('true');
  expect(dialog, 'a dialog is always named').toHaveAccessibleName();

  expect(
    dialog.contains(document.activeElement),
    `focus should land inside the dialog on open, not stay on ${describeEl(opts.trigger)}`,
  ).toBe(true);
  expect(document.body.style.overflow, 'an open dialog locks body scroll').toBe('hidden');

  /* Tab containment, against the trap's own FOCUSABLE_SELECTOR so the two can
   * never disagree. `tabindex="-1"` is excluded there because it is how the
   * panel itself parks; the filter here drops what is present but not
   * operable, mirroring lib/useDialog's focusablesIn plus aria-disabled. */
  // The trap's OWN definition of focusable, not a second list that could
  // drift and stay green.
  const items = focusablesIn(dialog);
  expect(items.length, 'a dialog with no focusable control cannot be operated').toBeGreaterThan(0);
  const first = items[0];
  const last = items[items.length - 1];

  last.focus();
  fireEvent.keyDown(last, { key: 'Tab' });
  expect(document.activeElement, 'Tab from the last control wraps to the first').toBe(first);

  first.focus();
  fireEvent.keyDown(first, { key: 'Tab', shiftKey: true });
  expect(document.activeElement, 'Shift+Tab from the first control wraps to the last').toBe(last);

  /* Escape closes exactly one layer — dispatched from inside the dialog, the
   * way a real key press reaches it. */
  fireEvent.keyDown(document.activeElement ?? document, { key: 'Escape' });
  expect(dialogs().length, 'Escape should close exactly one layer').toBe(layersBefore);

  expect(
    document.activeElement,
    `focus should return to ${describeEl(opts.trigger)} after close`,
  ).toBe(opts.trigger);
  expect(document.body.style.overflow, 'closing releases the body scroll lock').toBe(
    overflowBefore,
  );
}
