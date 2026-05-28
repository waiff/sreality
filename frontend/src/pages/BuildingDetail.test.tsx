/* BuildingDetail — B2 rollup + per-unit estimate rendering.
 *
 * Seeds the react-query cache with a finished building (status='success',
 * children + rollup totals) and asserts the page renders the building
 * totals, a per-unit strip per unit, and a "View estimate" link to each
 * child estimation. getBuilding is mocked so no network call fires.
 */

import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import type { BuildingRun } from '@/lib/types';

const building: BuildingRun = {
  id: 7,
  created_at: '2026-05-12T10:00:00+00:00',
  source: 'ui',
  status: 'success',
  input_url: 'https://www.sreality.cz/detail/dum/7',
  input_sreality_id: 999,
  input_spec: {
    lat: 50.08, lng: 14.42, area_m2: null, disposition: null,
    floor: null, exclude_ids: [],
  },
  source_kind: 'sreality',
  parse_confidence: 'high',
  parse_confidence_per_field: null,
  source_html: null,
  subject_summary: { fields: { locality: 'Praha 5', lat: 50.08, lng: 14.42 } } as never,
  units_proposal: null,
  units: [
    {
      unit_id: 'u1', label: 'flat 1', floor: '1', area_m2: 60,
      disposition: '2+kk', condition: 'dobry', is_potential: false,
      source: 'both', notes: null,
    },
    {
      unit_id: 'u2', label: 'flat 2', floor: '2', area_m2: 80,
      disposition: '3+kk', condition: 'dobry', is_potential: false,
      source: 'both', notes: null,
    },
  ],
  total_rent_p25_czk: 38_000,
  total_rent_p50_czk: 40_000,
  total_rent_p75_czk: 42_000,
  total_sale_p25_czk: 11_500_000,
  total_sale_p50_czk: 12_000_000,
  total_sale_p75_czk: 12_500_000,
  business_case: null,
  warnings: null,
  error_message: null,
  special_instructions: null,
  contextual_text: null,
  children: [
    {
      id: 101, created_at: '2026-05-12T10:01:00+00:00', status: 'success',
      estimate_kind: 'rent', building_unit_id: 'u1',
      estimated_monthly_rent_czk: 20_000, rent_p25_czk: 19_000, rent_p75_czk: 21_000,
      estimated_sale_price_czk: null, sale_p25_czk: null, sale_p75_czk: null,
      confidence: 'high', error_message: null,
    },
    {
      id: 102, created_at: '2026-05-12T10:02:00+00:00', status: 'success',
      estimate_kind: 'sale', building_unit_id: 'u1',
      estimated_monthly_rent_czk: null, rent_p25_czk: null, rent_p75_czk: null,
      estimated_sale_price_czk: 6_000_000, sale_p25_czk: 5_750_000, sale_p75_czk: 6_250_000,
      confidence: 'high', error_message: null,
    },
    {
      id: 103, created_at: '2026-05-12T10:03:00+00:00', status: 'success',
      estimate_kind: 'rent', building_unit_id: 'u2',
      estimated_monthly_rent_czk: 20_000, rent_p25_czk: 19_000, rent_p75_czk: 21_000,
      estimated_sale_price_czk: null, sale_p25_czk: null, sale_p75_czk: null,
      confidence: 'high', error_message: null,
    },
    {
      id: 104, created_at: '2026-05-12T10:04:00+00:00', status: 'success',
      estimate_kind: 'sale', building_unit_id: 'u2',
      estimated_monthly_rent_czk: null, rent_p25_czk: null, rent_p75_czk: null,
      estimated_sale_price_czk: 6_000_000, sale_p25_czk: 5_750_000, sale_p75_czk: 6_250_000,
      confidence: 'high', error_message: null,
    },
  ],
  attachments: [],
};

// The query cache is seeded below, so getBuilding never fires for the
// success row (fresh data, no refetch interval). The mock just guards
// against an accidental network call. Factory is hoisted — keep it free
// of outer-scope references.
vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  getBuilding: vi.fn(),
}));

import BuildingDetail from './BuildingDetail';

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(['building', 7], building);
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/building/7']}>
        <Routes>
          <Route path="building/:id" element={<BuildingDetail />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('<BuildingDetail> B2 rollup', () => {
  it('renders the building totals section with rent + sale strips', () => {
    renderPage();
    expect(screen.getByText('Building totals')).toBeInTheDocument();
    expect(screen.getByText('Total monthly rent (Kč)')).toBeInTheDocument();
    expect(screen.getByText('Total sale price (Kč)')).toBeInTheDocument();
  });

  it('renders one card per unit with both estimate kinds', () => {
    renderPage();
    expect(screen.getByText('u1')).toBeInTheDocument();
    expect(screen.getByText('u2')).toBeInTheDocument();
    // Each unit shows a rent + sale strip → 2 of each across 2 units.
    expect(screen.getAllByText('Monthly rent (Kč)')).toHaveLength(2);
    expect(screen.getAllByText('Sale price (Kč)')).toHaveLength(2);
  });

  it('links out to each child estimation detail page', () => {
    renderPage();
    const links = screen.getAllByRole('link', { name: /View estimate/ });
    expect(links).toHaveLength(4);
    const hrefs = links.map((l) => l.getAttribute('href')).sort();
    expect(hrefs).toEqual([
      '/estimation/101', '/estimation/102',
      '/estimation/103', '/estimation/104',
    ]);
  });
});
