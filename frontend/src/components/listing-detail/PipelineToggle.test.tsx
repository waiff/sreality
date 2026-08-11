/* PipelineToggle — the listing-detail deal-pipeline control.
 *
 * Hermetic: mock the two reads (card + stages) and the three writes (add / move
 * / remove). Pins the three behaviours: add when out of pipeline, change stage
 * through the SHARED stage menu (which replaced this surface's own <select>),
 * and remove only behind that menu's confirm. The real write is verified by
 * api/test_pipeline.py; here we only assert the right wrapper is called.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import PipelineToggle from './PipelineToggle';
import type { PipelineCard, PipelineStage } from '@/lib/types';
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
  return {
    ...actual,
    fetchPropertyPipeline: vi.fn(),
    fetchPipelineStages: vi.fn(),
  };
});

const STAGES: PipelineStage[] = [
  { id: 1, key: 'interested', label: 'Zájem', position: 1, color: 'copper', is_terminal: false, is_entry: true, code: '1' },
  /* No `code` — exercises stageBadge's ordinal fallback (migration 377). */
  { id: 3, key: 'offer', label: 'Nabídka', position: 3, color: 'teal', is_terminal: false, is_entry: false, code: null },
];

const CARD: PipelineCard = {
  property_id: 42,
  stage_id: 1,
  stage_key: 'interested',
  stage_label: 'Zájem',
  stage_code: '1',
  stage_color: 'copper',
  is_terminal: false,
  stage_position: 1,
};

const flush = () => new Promise((r) => setTimeout(r, 0));

function renderToggle() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <PipelineToggle property_id={42} />
    </QueryClientProvider>,
  );
}

describe('<PipelineToggle>', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(queries.fetchPipelineStages).mockResolvedValue(STAGES);
    vi.mocked(api.addPipelineCard).mockResolvedValue({ property_id: 42, stage_key: 'interested', added: true });
    vi.mocked(api.movePipelineCard).mockResolvedValue({ property_id: 42, stage_id: 3, stage_key: 'offer' });
    vi.mocked(api.removePipelineCard).mockResolvedValue({ removed: true });
  });

  it('adds to the pipeline when the property is not in it', async () => {
    vi.mocked(queries.fetchPropertyPipeline).mockResolvedValue(null);
    renderToggle();
    const add = await screen.findByTitle('Přidat do pipeline');
    fireEvent.click(add);
    await waitFor(() => expect(api.addPipelineCard).toHaveBeenCalledWith(42));
  });

  it('changes the stage through the shared menu', async () => {
    vi.mocked(queries.fetchPropertyPipeline).mockResolvedValue(CARD);
    renderToggle();
    fireEvent.click(await screen.findByRole('button', { name: /V pipeline/ }));
    fireEvent.click(await screen.findByRole('menuitemradio', { name: /Nabídka/ }));
    await waitFor(() => expect(api.movePipelineCard).toHaveBeenCalledWith(42, 3));
  });

  it('removes only after the menu confirm', async () => {
    vi.mocked(queries.fetchPropertyPipeline).mockResolvedValue(CARD);
    renderToggle();
    fireEvent.click(await screen.findByRole('button', { name: /V pipeline/ }));
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Odebrat z pipeline' }));
    await flush();
    expect(api.removePipelineCard).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('menuitem', { name: 'Odebrat' }));
    await waitFor(() => expect(api.removePipelineCard).toHaveBeenCalledWith(42));
  });
});
