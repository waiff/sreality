/* LocationQuality — interactive-semantics rail.
 *
 * Every control on this page is a bare input/select with only a placeholder or
 * nothing at all to identify it. What is pinned here is the ACCESSIBLE NAME of
 * each one, computed against the rendered DOM: the inspector query box, the
 * correction form it opens, and the page's source scope.
 *
 * Hermetic: every `/location/*` wrapper is mocked. The one panel that needs a
 * large fixture (source overview) is deliberately failed — its error banner is
 * a real render path and none of the named controls live in it.
 *
 * W2-b deleted the frozen-labelled-sample section with its two tables, so the
 * six cell names this file used to pin went with it.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import LocationQuality from './LocationQuality';
import * as lq from '../lib/locationQuality';
import type { Inspector } from '../lib/locationQuality';

vi.mock('../lib/locationQuality', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/locationQuality')>();
  return {
    ...actual,
    fetchCorpusSummary: vi.fn(),
    fetchSourceOverview: vi.fn(),
    fetchInspector: vi.fn(),
    fetchInspectorByNative: vi.fn(),
    submitCorrection: vi.fn(),
  };
});

const inspector: Inspector = {
  listing_id: 42,
  projection: { street_name: 'Krátká', house_number_cp: '3', obec_name: 'Brno' },
  claims: [],
};

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <LocationQuality />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(lq.fetchCorpusSummary).mockResolvedValue({ data: { grain: 'listing', sources: [] } });
  vi.mocked(lq.fetchSourceOverview).mockRejectedValue(new Error('overview unavailable'));
  vi.mocked(lq.fetchInspector).mockResolvedValue({ data: inspector });
});

describe('LocationQuality accessible names', () => {
  it('names the page scope select after its visible caption', async () => {
    renderPage();
    expect(screen.getByRole('combobox', { name: 'Source' })).toHaveValue('bezrealitky');
  });

  it('names the inspector query box, and the correction form it opens', async () => {
    renderPage();
    const query = await screen.findByRole('textbox', { name: 'Listing or native id' });
    fireEvent.change(query, { target: { value: '42' } });
    fireEvent.click(screen.getByRole('button', { name: 'Inspect' }));

    const correct = await screen.findByRole('button', { name: 'Correct' });
    const form = within(correct.closest('form') as HTMLElement);
    await waitFor(() => {
      expect(form.getByRole('combobox', { name: 'Claim type' })).toBeInTheDocument();
    });
    expect(form.getByRole('textbox', { name: 'Corrected value' })).toBeInTheDocument();
    expect(form.getByRole('textbox', { name: 'Note' })).toBeInTheDocument();
  });
});
