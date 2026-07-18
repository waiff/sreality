/* TrainControl — the shared Train + Border case CTA (/phash-audit, /clip-audit).
 *
 * Hermetic: mock the four writes. Pins: Train submits the default (CLIP fine_tag)
 * value; Border case is a plain toggle independent of the label, in both directions.
 * The real writes are verified by tests/test_image_annotations.py; here we only
 * assert the right wrapper is called with the right args.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import TrainControl from './TrainControl';
import type { ImagePublic } from '@/lib/types';
import * as api from '@/lib/api';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    setTrainingExample: vi.fn(),
    deleteTrainingExample: vi.fn(),
    setBorderCase: vi.fn(),
    deleteBorderCase: vi.fn(),
  };
});

const IMAGE: ImagePublic = {
  id: 42,
  sreality_id: 123,
  sequence: null,
  sreality_url: 'https://x/a.jpg',
  storage_path: null,
  clip_fine_tag: 'hallway',
  clip_logical_tag: 'hallway',
  clip_confidence: 0.9,
  clip_render_score: null,
  phash: null,
};

function renderControl(borderCase: boolean, example?: api.TrainingExample) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <TrainControl
        image={IMAGE}
        example={example}
        borderCase={borderCase}
        labelOptions={[{ value: 'hallway', label: 'chodba' }]}
        queryKeyPrefix="phash-audit"
      />
    </QueryClientProvider>,
  );
}

describe('<TrainControl> border case', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.setTrainingExample).mockResolvedValue({
      data: { image_id: 42, label: 'hallway', updated_at: '2026-07-18T00:00:00Z' },
    });
    vi.mocked(api.setBorderCase).mockResolvedValue({
      data: { image_id: 42, created_at: '2026-07-18T00:00:00Z' },
    });
    vi.mocked(api.deleteBorderCase).mockResolvedValue({ data: { deleted: true } });
  });

  it('flags an unflagged image on click', async () => {
    renderControl(false);
    fireEvent.click(screen.getByText('Border case'));
    await waitFor(() => expect(api.setBorderCase).toHaveBeenCalledWith(42));
    expect(api.deleteBorderCase).not.toHaveBeenCalled();
  });

  it('unflags an already-flagged image on click (shows the checkmark state first)', async () => {
    renderControl(true);
    expect(screen.getByText('✓ Border case')).toBeInTheDocument();
    fireEvent.click(screen.getByText('✓ Border case'));
    await waitFor(() => expect(api.deleteBorderCase).toHaveBeenCalledWith(42));
    expect(api.setBorderCase).not.toHaveBeenCalled();
  });

  it('is independent of the Train label — clicking Train never touches border-case state', async () => {
    renderControl(false);
    fireEvent.click(screen.getByText('Train'));
    await waitFor(() =>
      expect(api.setTrainingExample).toHaveBeenCalledWith({ image_id: 42, label: 'hallway' }),
    );
    expect(api.setBorderCase).not.toHaveBeenCalled();
    expect(api.deleteBorderCase).not.toHaveBeenCalled();
  });
});
