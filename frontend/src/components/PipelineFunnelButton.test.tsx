/* PipelineFunnelButton — the funnel on Browse cards and Table rows.
 *
 * The regression this file exists for: clicking the funnel of a property that
 * is ALREADY in the pipeline used to remove it outright — one unconfirmed,
 * undoable click on a 60-card grid. It must now open the stage menu instead,
 * and removal must be unreachable without the menu's confirm.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import PipelineFunnelButton from './PipelineFunnelButton';
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
  return { ...actual, fetchPipelineMembers: vi.fn(), fetchPipelineStages: vi.fn() };
});

const STAGES: PipelineStage[] = [
  { id: 1, key: 'review', label: '1. For Review', position: 1, color: 'copper', is_terminal: false, is_entry: true, code: '1' },
  { id: 2, key: 'call', label: '2. For Call', position: 2, color: 'ochre', is_terminal: false, is_entry: false, code: '2' },
];

const MEMBER = {
  property_id: 42,
  stage_id: 1,
  stage_label: '1. For Review',
  stage_color: 'copper' as const,
  stage_code: '1',
  stage_position: 1,
  is_terminal: false,
};

const flush = () => new Promise((r) => setTimeout(r, 0));

function renderButton() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <PipelineFunnelButton property_id={42} />
    </QueryClientProvider>,
  );
}

describe('<PipelineFunnelButton>', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(queries.fetchPipelineStages).mockResolvedValue(STAGES);
    vi.mocked(api.addPipelineCard).mockResolvedValue({ property_id: 42, stage_key: 'review', added: true });
    vi.mocked(api.movePipelineCard).mockResolvedValue({ property_id: 42, stage_id: 2, stage_key: 'call' });
    vi.mocked(api.removePipelineCard).mockResolvedValue({ removed: true });
  });

  it('bookmarks into the entry stage in one click when out of the pipeline', async () => {
    vi.mocked(queries.fetchPipelineMembers).mockResolvedValue(new Map());
    renderButton();
    fireEvent.click(await screen.findByRole('button', { name: 'Přidat do pipeline' }));
    await waitFor(() => expect(api.addPipelineCard).toHaveBeenCalledWith(42));
  });

  it('opens the stage menu instead of removing when already in the pipeline', async () => {
    vi.mocked(queries.fetchPipelineMembers).mockResolvedValue(new Map([[42, MEMBER]]));
    renderButton();
    const funnel = await screen.findByRole('button', { name: /V pipeline/ });
    fireEvent.click(funnel);
    await flush();

    expect(api.removePipelineCard).not.toHaveBeenCalled();
    expect(await screen.findByRole('menu', { name: 'Fáze v pipeline' })).toBeInTheDocument();
    expect(funnel).toHaveAttribute('aria-expanded', 'true');
  });

  it('moves the card from the menu the funnel opened', async () => {
    vi.mocked(queries.fetchPipelineMembers).mockResolvedValue(new Map([[42, MEMBER]]));
    renderButton();
    fireEvent.click(await screen.findByRole('button', { name: /V pipeline/ }));
    fireEvent.click(await screen.findByRole('menuitemradio', { name: /For Call/ }));
    await waitFor(() => expect(api.movePipelineCard).toHaveBeenCalledWith(42, 2));
    // The menu closes behind the write; the funnel is the tab stop again.
    await waitFor(() => expect(screen.queryByRole('menu')).not.toBeInTheDocument());
  });

  it('closes the menu on a second funnel click without writing', async () => {
    vi.mocked(queries.fetchPipelineMembers).mockResolvedValue(new Map([[42, MEMBER]]));
    renderButton();
    const funnel = await screen.findByRole('button', { name: /V pipeline/ });
    fireEvent.click(funnel);
    expect(await screen.findByRole('menu')).toBeInTheDocument();
    fireEvent.click(funnel);
    await flush();
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(api.removePipelineCard).not.toHaveBeenCalled();
    expect(api.movePipelineCard).not.toHaveBeenCalled();
  });
});
