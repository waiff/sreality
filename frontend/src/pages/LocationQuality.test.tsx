/* LocationQuality — interactive-semantics rail.
 *
 * Every control on this page is a bare input/select with only a placeholder,
 * a column header, or nothing at all to identify it. What is pinned here is
 * the ACCESSIBLE NAME of each one, computed against the rendered DOM: the six
 * frozen-sample cells take their name from their column header, and the
 * inspector / correction / source-scope controls from their own caption.
 *
 * Hermetic: every `/location/*` wrapper is mocked. The two panels that need a
 * large fixture (source overview, W1v gate) are deliberately failed — their
 * error banner is a real render path and none of the named controls live in
 * them.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import LocationQuality from './LocationQuality';
import * as lq from '../lib/locationQuality';
import type { Inspector, SampleMember, SampleScore, SampleStatus } from '../lib/locationQuality';

vi.mock('../lib/locationQuality', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/locationQuality')>();
  return {
    ...actual,
    fetchCorpusSummary: vi.fn(),
    fetchSourceOverview: vi.fn(),
    fetchW1vGate: vi.fn(),
    fetchSample: vi.fn(),
    fetchSampleScore: vi.fn(),
    fetchInspector: vi.fn(),
    fetchInspectorByNative: vi.fn(),
    submitCorrection: vi.fn(),
  };
});

const member: SampleMember = {
  listing_id: 42,
  source_id_native: 'BR-42',
  position: 1,
  label_street: null,
  label_street_nd: false,
  label_house_number: null,
  label_house_number_nd: false,
  label_obec: null,
  label_obec_nd: false,
  label_okres: null,
  label_okres_nd: false,
  label_precision_class: null,
  label_precision_nd: false,
  label_note: null,
  labelled_at: null,
  is_active: true,
  source_url: null,
};

const sample: SampleStatus = {
  sample: {
    id: 1, source: 'bezrealitky', drawn_at: new Date().toISOString(),
    method: 'random', n: 1, note: null, members: 1, labelled: 0,
  },
  members: [member],
};

const emptyBlock = { determinable: 0, new: { asserted: 0, matches: 0, precision_pct: null, yield_pct: null }, floor_pct: 95 };
const score: SampleScore = {
  source: 'bezrealitky', grain: 'listing', labelled: 0,
  street: emptyBlock, obec: emptyBlock, okres: emptyBlock, precision_class: emptyBlock,
};

const inspector: Inspector = {
  listing_id: 42,
  projection: { display_label: 'Krátká 3, Brno' },
  claims: [],
  candidates: [],
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
  vi.mocked(lq.fetchW1vGate).mockRejectedValue(new Error('gate unavailable'));
  vi.mocked(lq.fetchSample).mockResolvedValue({ data: sample });
  vi.mocked(lq.fetchSampleScore).mockResolvedValue({ data: score });
  vi.mocked(lq.fetchInspector).mockResolvedValue({ data: inspector });
});

describe('LocationQuality accessible names', () => {
  it('names the page scope select after its visible caption', async () => {
    renderPage();
    expect(screen.getByRole('combobox', { name: 'Source' })).toHaveValue('bezrealitky');
  });

  it('names every frozen-sample cell after its column header', async () => {
    renderPage();
    await screen.findByRole('textbox', { name: 'Street' });
    expect(screen.getByRole('textbox', { name: 'No.' })).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Obec' })).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Okres' })).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Note' })).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Precision' })).toBeInTheDocument();
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
