/* TrainControl — the /clip-audit Train CTA.
 *
 * Hermetic: mock the two training writes. Pins that Train submits the default
 * (CLIP fine_tag) value and that it never touches the border-case flag beside
 * it — the two are independent facts about one image. The flag's own behavior
 * lives in BorderCaseButton.test.tsx (the button) and useBorderCases.test.tsx
 * (the reads/writes); the real endpoints are covered by
 * tests/test_image_annotations.py.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import TrainControl from './TrainControl';
import type { ImagePublic } from '@/lib/types';
import type { BorderCaseStore } from '@/lib/useBorderCases';
import * as api from '@/lib/api';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    setTrainingExample: vi.fn(),
    deleteTrainingExample: vi.fn(),
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

function stubStore(flagged: boolean): BorderCaseStore {
  return { has: () => flagged, isPending: () => false, toggle: vi.fn() };
}

function renderControl(store: BorderCaseStore, example?: api.TrainingExample) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <TrainControl
        image={IMAGE}
        example={example}
        borderCases={store}
        labelOptions={[{ value: 'hallway', label: 'chodba' }]}
        queryKeyPrefix="clip-audit"
      />
    </QueryClientProvider>,
  );
}

describe('<TrainControl>', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.setTrainingExample).mockResolvedValue({
      data: { image_id: 42, label: 'hallway', updated_at: '2026-07-18T00:00:00Z' },
    });
  });

  it('trains under the CLIP fine tag by default', async () => {
    renderControl(stubStore(false));
    fireEvent.click(screen.getByText('Train'));
    await waitFor(() =>
      expect(api.setTrainingExample).toHaveBeenCalledWith({ image_id: 42, label: 'hallway' }),
    );
  });

  it('renders the border-case flag beside Train, in whichever state the store reports', () => {
    renderControl(stubStore(true));
    expect(screen.getByText('✓ Border case')).toBeInTheDocument();
  });

  it('is independent of the flag — clicking Train never toggles it', async () => {
    const store = stubStore(false);
    renderControl(store);
    fireEvent.click(screen.getByText('Train'));
    await waitFor(() => expect(api.setTrainingExample).toHaveBeenCalled());
    expect(store.toggle).not.toHaveBeenCalled();
  });

  it('is independent the other way — the flag click never writes a training example', () => {
    const store = stubStore(false);
    renderControl(store);
    fireEvent.click(screen.getByText('Border case'));
    expect(store.toggle).toHaveBeenCalledWith(42);
    expect(api.setTrainingExample).not.toHaveBeenCalled();
  });
});
