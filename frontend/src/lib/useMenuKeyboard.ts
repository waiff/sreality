/* Keyboard behaviour for the app's popover menus: open focused on the checked
 * item, then rove with Up/Down/Home/End.
 *
 * WAI-ARIA's menu pattern for a radio/checkbox group — the operator's next move
 * is almost always one step from where they are, and a menu that can only be
 * driven by mouse is a dead end once it is open. Shared by the pipeline stage
 * menu and the collection picker so the two behave identically; both are opened
 * from the same rows of controls, and a menu whose arrow keys work in one and
 * not the other is worse than neither.
 *
 * Items are found by `role^="menuitem"` inside `listRef`, so a menu adds items
 * without telling this hook about them.
 */

import { useEffect, type KeyboardEvent, type RefObject } from 'react';

const ROVE_KEYS = ['ArrowDown', 'ArrowUp', 'Home', 'End'];

export function useMenuKeyboard(
  listRef: RefObject<HTMLElement | null>,
  /* Re-focus when these change — the item list arriving, or the menu swapping
   * into a confirm step. Focus is skipped entirely while `active` is false. */
  { active = true, deps = [] as unknown[] } = {},
) {
  useEffect(() => {
    if (!active) return;
    const el = listRef.current?.querySelector<HTMLElement>('[aria-checked="true"]');
    (el ?? listRef.current?.querySelector<HTMLElement>('[role^="menuitem"]'))?.focus();
    // The caller's deps say when the item list is ready / has changed shape.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, listRef, ...deps]);

  return (e: KeyboardEvent) => {
    if (!ROVE_KEYS.includes(e.key)) return;
    const items = [
      ...(listRef.current?.querySelectorAll<HTMLElement>(
        '[role^="menuitem"]:not([disabled])',
      ) ?? []),
    ];
    if (items.length === 0) return;
    e.preventDefault();
    const at = items.indexOf(document.activeElement as HTMLElement);
    const next =
      e.key === 'Home'
        ? 0
        : e.key === 'End'
          ? items.length - 1
          : e.key === 'ArrowDown'
            ? (at + 1) % items.length
            : (at - 1 + items.length) % items.length;
    items[next]?.focus();
  };
}
