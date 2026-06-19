/* Pipeline kanban — drag-and-drop move resolution + board interaction.
 *
 * The real DnD gesture (pointer drag across columns) can't be faithfully
 * simulated in jsdom, so the bug-prone part — resolving a drag-end into a
 * stage move — is extracted into the pure `planMove` and unit-tested directly.
 * A render smoke test then pins the board's columns and the trash → confirm →
 * remove flow (stage moves are drag-only; the select fallback was removed).
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import Pipeline, { planMove } from './Pipeline';
import type { PipelineBoardCard, PipelineStage } from '@/lib/types';
import * as api from '@/lib/api';
import * as queries from '@/lib/queries';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    movePipelineCard: vi.fn(),
    removePipelineCard: vi.fn(),
  };
});

vi.mock('@/lib/queries', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/queries')>();
  return {
    ...actual,
    fetchPipelineStages: vi.fn(),
    fetchPipelineBoard: vi.fn(),
  };
});

const CARDS: PipelineBoardCard[] = [
  {
    property_id: 42,
    stage_id: 1,
    board_position: 0,
    entered_stage_at: '2026-06-01T00:00:00Z',
    sreality_id: 111,
    street: 'Sadová',
    district: 'Praha',
    disposition: '2+kk',
    area_m2: 55,
    price_czk: 5_000_000,
    mf_gross_yield_pct: 4.3,
    image_url: null,
  },
];

describe('planMove', () => {
  it('resolves a cross-column drop into a stage move', () => {
    expect(planMove('card:42', 'stage:3', CARDS)).toEqual({
      propertyId: 42,
      stageId: 3,
    });
  });

  it('is a no-op for a same-column drop', () => {
    expect(planMove('card:42', 'stage:1', CARDS)).toBeNull();
  });

  it('is a no-op when dropped outside any column', () => {
    expect(planMove('card:42', null, CARDS)).toBeNull();
  });

  it('is a no-op when over is not a stage droppable', () => {
    expect(planMove('card:42', 'card:99', CARDS)).toBeNull();
  });

  it('is a no-op for an unknown card', () => {
    expect(planMove('card:999', 'stage:3', CARDS)).toBeNull();
  });
});

const STAGES: PipelineStage[] = [
  { id: 1, key: 'interested', label: 'Zájem', position: 1, color: 'copper', is_terminal: false, is_entry: true },
  { id: 3, key: 'offer', label: 'Nabídka', position: 3, color: 'teal', is_terminal: false, is_entry: false },
];

function renderBoard() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Pipeline />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('<Pipeline> board', () => {
  beforeEach(() => {
    vi.mocked(queries.fetchPipelineStages).mockResolvedValue(STAGES);
    vi.mocked(queries.fetchPipelineBoard).mockResolvedValue(CARDS);
    vi.mocked(api.movePipelineCard).mockResolvedValue({
      property_id: 42,
      stage_id: 3,
      stage_key: 'offer',
    });
    vi.mocked(api.removePipelineCard).mockResolvedValue({ removed: true });
  });

  it('renders draggable cards with a drag handle + enriched content', async () => {
    renderBoard();
    // One card → one grip handle; proves the column + draggable card mounted.
    expect(
      await screen.findByLabelText('Přetáhnout kartu do jiné fáze'),
    ).toBeInTheDocument();
    // Both stage columns render their header label.
    expect(screen.getByText('Zájem')).toBeInTheDocument();
    expect(screen.getByText('Nabídka')).toBeInTheDocument();
    // Enriched card content: street + MF yield.
    expect(screen.getByText('Sadová, Praha')).toBeInTheDocument();
    expect(screen.getByText(/MF\s*4,3\s*%/)).toBeInTheDocument();
  });

  it('trash → confirm removes the card via removePipelineCard', async () => {
    renderBoard();
    const trash = await screen.findByLabelText('Odebrat z pipeline');
    fireEvent.click(trash);
    // Inline two-step confirm appears; nothing removed until confirmed.
    expect(api.removePipelineCard).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText('Odebrat'));
    await waitFor(() =>
      expect(api.removePipelineCard).toHaveBeenCalledWith(42),
    );
  });
});
