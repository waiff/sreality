/* The estimations chapter shows the PROPERTY's MF result, by shape, and nothing
 * else: no run's frozen reference rent stands in for a missing one, and there
 * is no English placeholder of its own. */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import type { ReferenceRent } from '@/lib/mfReference';
import type { EstimationRun } from '@/lib/types';

vi.mock('@/lib/queries', async (orig) => ({
  ...(await orig<typeof import('@/lib/queries')>()),
  fetchEstimationsForListings: vi.fn(),
}));
vi.mock('@/components/estimation/RunPanel', () => ({
  RunBody: () => <div data-testid="run-body" />,
  ConfidencePill: () => null,
  RunStatusChip: () => null,
}));
vi.mock('@/components/NewEstimationModal', () => ({
  useNewEstimationModal: () => ({ open: () => {} }),
}));

import { fetchEstimationsForListings, type PropertyPublic } from '@/lib/queries';
import EstimationsBlock from './EstimationsBlock';

const fetchRuns = vi.mocked(fetchEstimationsForListings);

const MF_LABEL = 'Odhad nájmu · cenová mapa MF';

const FROZEN_REF: ReferenceRent = {
  territory: { ruian_code: 698903, level: 'ku', name: 'Moravské Budějovice', kraj: null },
  vk: 3,
  is_novostavba: false,
  source_revision: 1,
  base_per_m2: 184,
  adjustments: [],
  total_per_m2: 184,
  area_m2: 75,
  monthly_rent_czk: 13_800,
};

const run = {
  id: 77,
  status: 'success',
  mode: 'comparables',
  estimate_kind: 'rent',
  estimated_monthly_rent_czk: 21_000,
  estimated_sale_price_czk: null,
  gross_yield_pct: null,
  confidence: 'medium',
  created_at: '2026-09-01T10:00:00Z',
  reference_rent: FROZEN_REF,
} as unknown as EstimationRun;

const property = (mf: Partial<PropertyPublic>): PropertyPublic =>
  ({
    id: 11,
    property_id: 5,
    category_main: 'byt',
    category_type: 'prodej',
    mf_gross_yield_pct: null,
    mf_reference_rent: null,
    ...mf,
  }) as unknown as PropertyPublic;

function renderBlock(listing: PropertyPublic) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <EstimationsBlock listing={listing} listingIds={[listing.id]} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('EstimationsBlock MF card', () => {
  beforeEach(() => fetchRuns.mockReset());

  it('never borrows a run frozen reference rent for a property without MF', async () => {
    fetchRuns.mockResolvedValue({ data: [run], total: 1, limit: 50, offset: 0, next_cursor: null });
    const { container } = renderBlock(property({}));
    await waitFor(() => expect(screen.getByTestId('run-body')).toBeInTheDocument());
    expect(container).not.toHaveTextContent(MF_LABEL);
    expect(container).not.toHaveTextContent('13 800');
    expect(container).not.toHaveTextContent('No MF reference');
  });

  it('renders nothing at all with no MF result and no runs', async () => {
    fetchRuns.mockResolvedValue({ data: [], total: 0, limit: 50, offset: 0, next_cursor: null });
    const { container } = renderBlock(property({}));
    await waitFor(() => expect(fetchRuns).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it('shows the property value with the property yield', async () => {
    fetchRuns.mockResolvedValue({ data: [], total: 0, limit: 50, offset: 0, next_cursor: null });
    const { container } = renderBlock(
      property({ mf_reference_rent: FROZEN_REF, mf_gross_yield_pct: 4.6 }),
    );
    await waitFor(() => expect(container).toHaveTextContent('13 800 Kč/měs'));
    expect(container).toHaveTextContent('hrubý výnos 4,60 %');
  });

  it('shows the property reason as its note, in place of any placeholder', async () => {
    const note = '(a reason note, from SQL)';
    fetchRuns.mockResolvedValue({ data: [], total: 0, limit: 50, offset: 0, next_cursor: null });
    const { container } = renderBlock(
      property({
        mf_reference_rent: { status: 'location_unknown', note } as unknown as ReferenceRent,
      }),
    );
    await waitFor(() => expect(container).toHaveTextContent(note));
    expect(container).toHaveTextContent(MF_LABEL);
  });
});
