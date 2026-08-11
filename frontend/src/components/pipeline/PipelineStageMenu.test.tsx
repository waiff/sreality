/* PipelineStageMenu — the shared "move or remove" menu behind every funnel.
 *
 * Hermetic: mock the stage read and the three writes. Pins the guarantees the
 * menu exists for — a stage click moves through the audited PATCH, removal is
 * unreachable without a second, explicit confirmation, and the panel lands in a
 * portal on <body> rather than inside its (clipped) anchor.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { createRef } from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import PipelineStageMenu from './PipelineStageMenu';
import type { PipelineStage } from '@/lib/types';
import * as api from '@/lib/api';
import * as queries from '@/lib/queries';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    addPipelineCard: vi.fn(),
    movePipelineCard: vi.fn(),
    removePipelineCard: vi.fn(),
  };
});

vi.mock('@/lib/queries', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/queries')>();
  return { ...actual, fetchPipelineStages: vi.fn() };
});

const STAGES: PipelineStage[] = [
  { id: 1, key: 'review', label: '1. For Review', position: 1, color: 'copper', is_terminal: false, is_entry: true, code: '1' },
  { id: 2, key: 'call', label: '2. For Call', position: 2, color: 'ochre', is_terminal: false, is_entry: false, code: '2' },
  /* Terminal, and coded "9" like the live board — never the ordinal (3). */
  { id: 3, key: 'lost', label: '9. Lost', position: 3, color: 'brick', is_terminal: true, is_entry: false, code: '9' },
];

function renderMenu(stageId = 1) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const anchor = createRef<HTMLButtonElement>();
  const onClose = vi.fn();
  const view = render(
    <QueryClientProvider client={qc}>
      <button ref={anchor} type="button">
        anchor
      </button>
      <PipelineStageMenu
        property_id={42}
        stageId={stageId}
        anchorRef={anchor}
        onClose={onClose}
      />
    </QueryClientProvider>,
  );
  return { ...view, onClose };
}

/* Writes go through an async onMutate (it cancels in-flight reads before
 * patching the caches), so the API call lands a microtask after the click —
 * every "did not write" assertion has to outlive that gap to mean anything. */
const flush = () => new Promise((r) => setTimeout(r, 0));

describe('<PipelineStageMenu>', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(queries.fetchPipelineStages).mockResolvedValue(STAGES);
    vi.mocked(api.movePipelineCard).mockResolvedValue({ property_id: 42, stage_id: 2, stage_key: 'call' });
    vi.mocked(api.removePipelineCard).mockResolvedValue({ removed: true });
  });

  it('moves the card when another stage is picked, and closes', async () => {
    const { onClose } = renderMenu();
    fireEvent.click(await screen.findByRole('menuitemradio', { name: /For Call/ }));
    await waitFor(() => expect(api.movePipelineCard).toHaveBeenCalledWith(42, 2));
    expect(onClose).toHaveBeenCalled();
  });

  it('marks the current stage and never moves to it', async () => {
    renderMenu(2);
    const current = await screen.findByRole('menuitemradio', { name: /For Call/ });
    expect(current).toHaveAttribute('aria-checked', 'true');
    fireEvent.click(current);
    await flush();
    expect(api.movePipelineCard).not.toHaveBeenCalled();
    /* Focusable, not `disabled`: the menu opens ON the current stage, and a
     * disabled button can't hold focus — the keyboard path would start dead. */
    expect(current).not.toBeDisabled();
    expect(document.activeElement).toBe(current);
  });

  it('badges each stage from its operator code, not its ordinal', async () => {
    renderMenu();
    // "9. Lost" is third in the list; its badge must read 9 (migration 377).
    const lost = await screen.findByRole('menuitemradio', { name: /Lost/ });
    expect(lost.textContent).toContain('9');
    expect(lost.textContent).not.toContain('3');
  });

  it('requires a second, explicit confirmation before removing', async () => {
    renderMenu();
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Odebrat z pipeline' }));
    // First click only opens the confirm — nothing has been written yet.
    await flush();
    expect(api.removePipelineCard).not.toHaveBeenCalled();
    expect(screen.getByText('Odebrat z pipeline?')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('menuitem', { name: 'Odebrat' }));
    await waitFor(() => expect(api.removePipelineCard).toHaveBeenCalledWith(42));
  });

  it('backs out of the confirm without writing', async () => {
    renderMenu();
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Odebrat z pipeline' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Zrušit' }));
    await flush();
    expect(api.removePipelineCard).not.toHaveBeenCalled();
    expect(screen.getByRole('menuitem', { name: 'Odebrat z pipeline' })).toBeInTheDocument();
  });

  it('renders in a portal on <body>, outside the anchor subtree', async () => {
    const { container } = renderMenu();
    const menu = await screen.findByRole('menu');
    expect(container.contains(menu)).toBe(false);
    expect(document.body.contains(menu)).toBe(true);
  });
});
