/* <TagEditPopover> — the one member of the dialog set that was never a dialog.
 *
 * It announced `role="dialog"` with no `aria-modal`, no focus trap and no
 * scroll lock: the announcement of a modal with none of the behaviour. It is a
 * disclosure hung off a pencil trigger, so it is the shared <AnchoredPopover>
 * now. What is asserted here is exactly that split — a named `group`, NO modal
 * layer taken, no scroll lock, and the disclosure contract (focus in on open,
 * back to the trigger on Escape) that its own document listeners used to
 * half-do.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { openDialogLayerCount } from '@/lib/useDialog';
import type { Tag } from '@/lib/types';

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  updateTag: vi.fn(),
  deleteTag: vi.fn(),
}));

import { updateTag } from '@/lib/api';
import TagEditPopover from './TagEditPopover';

const TAG: Tag = {
  id: 3,
  name: 'k prohlídce',
  color: 'copper',
  created_at: '2026-05-01T00:00:00+00:00',
  listing_count: 2,
};

function renderPopover() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const view = render(
    <QueryClientProvider client={qc}>
      <TagEditPopover tag={TAG} otherNames={['jiný štítek']} />
    </QueryClientProvider>,
  );
  return { ...view, trigger: screen.getByRole('button', { name: 'Edit tag k prohlídce' }) };
}

const panelName = 'Edit tag k prohlídce';

describe('<TagEditPopover> is a disclosure, not a modal', () => {
  beforeEach(() => vi.clearAllMocks());

  it('takes no dialog layer, no scroll lock, and announces a named group', () => {
    const { trigger } = renderPopover();
    const overflowBefore = document.body.style.overflow;

    fireEvent.click(trigger);

    expect(screen.queryByRole('dialog')).toBeNull();
    /* The stack itself, not what happens to be rendered: a popover that took a
     * modal layer would answer Escape ahead of the dialog it opened from. */
    expect(openDialogLayerCount()).toBe(0);
    expect(document.body.style.overflow).toBe(overflowBefore);
    expect(screen.getByRole('group', { name: panelName })).toBeInTheDocument();
  });

  it('says what it controls, and toggles shut on a second press of the pencil', () => {
    const { trigger } = renderPopover();
    expect(trigger).toHaveAttribute('aria-expanded', 'false');

    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    const panel = screen.getByRole('group', { name: panelName });
    expect(trigger.getAttribute('aria-controls')).toBe(panel.id);

    /* A pointerdown ON the anchor is ignored by AnchoredPopover so the
     * trigger's own click can close it, instead of the panel dismissing and
     * reopening in the same gesture. */
    fireEvent.pointerDown(trigger);
    fireEvent.click(trigger);
    expect(screen.queryByRole('group', { name: panelName })).toBeNull();
  });

  it('escapes the row that clipped it — the panel is portalled to <body>', () => {
    const { container, trigger } = renderPopover();
    fireEvent.click(trigger);

    const panel = screen.getByRole('group', { name: panelName });
    expect(container.contains(panel)).toBe(false);
    expect(document.body.contains(panel)).toBe(true);
  });
});

describe('<TagEditPopover> keyboard entry and exit', () => {
  beforeEach(() => vi.clearAllMocks());

  it('moves focus into the name field on open and back to the pencil on Escape', () => {
    const { trigger } = renderPopover();
    trigger.focus();

    fireEvent.click(trigger);
    const field = screen.getByRole('textbox', { name: 'Edit tag' });
    expect(document.activeElement).toBe(field);

    fireEvent.keyDown(field, { key: 'Escape' });
    expect(screen.queryByRole('group', { name: panelName })).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it('dismisses on an outside pointerdown and hands focus back', () => {
    const { trigger } = renderPopover();
    trigger.focus();
    fireEvent.click(trigger);

    fireEvent.pointerDown(document.body);
    expect(screen.queryByRole('group', { name: panelName })).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });
});

/* The form the popover carries, unchanged by the migration: unwrapping its
 * root <div> must not cost the rename its write. */
describe('<TagEditPopover> renaming', () => {
  beforeEach(() => vi.clearAllMocks());

  it('PATCHes only what changed', async () => {
    vi.mocked(updateTag).mockResolvedValue({ ...TAG, name: 'k jednání' } as never);
    const { trigger } = renderPopover();
    fireEvent.click(trigger);

    fireEvent.change(screen.getByRole('textbox', { name: 'Edit tag' }), {
      target: { value: 'k jednání' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() =>
      expect(updateTag).toHaveBeenCalledWith(3, { name: 'k jednání', color: undefined }),
    );
  });

  it('refuses a name another tag already carries', () => {
    renderPopover();
    fireEvent.click(screen.getByRole('button', { name: 'Edit tag k prohlídce' }));

    fireEvent.change(screen.getByRole('textbox', { name: 'Edit tag' }), {
      target: { value: 'jiný štítek' },
    });
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    expect(updateTag).not.toHaveBeenCalled();
  });
});
