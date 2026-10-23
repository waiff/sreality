/* "Explore area" under the header map is the twin of the neighbourhood tile's
 * "Prozkoumat okolí" (operator 2026-10-23): one opener over one scope, so the two
 * open the explore window on the same sample, lens and dashboard. */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import type { PropertyPublic } from '@/lib/queries';
import type { SavedDashboard } from '@/lib/types';

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  isApiConfigured: () => true,
}));
vi.mock('@/lib/stats', async (orig) => ({
  ...(await orig<typeof import('@/lib/stats')>()),
  listStatsDashboards: vi.fn(),
  fetchStatsPivot: vi.fn(),
}));
const openExplore = vi.hoisted(() => vi.fn());
vi.mock('@/components/ExploreAreaModal', () => ({
  useExploreAreaModal: () => ({ open: openExplore }),
}));

import { fetchStatsPivot, listStatsDashboards } from '@/lib/stats';
import { DEFAULT_SCOPE, neighbourhoodFilters } from '@/lib/neighbourhood';
import { pivotOf } from '@/components/stats/testPivot';
import ExploreAreaButton from './ExploreAreaButton';
import MarketTile from './MarketTile';

const TEMPLATE: SavedDashboard = {
  id: 'd-tpl',
  name: 'Trh v okolí',
  charts: [],
  filter_spec: null,
  position: 0,
  color: null,
  created_at: '2026-10-23T08:00:00+00:00',
  updated_at: '2026-10-23T08:00:00+00:00',
  owner_email: null,
  shared_with: [],
};

/* Each test its own property: the building and condition picks are held per property. */
const property = (property_id: number, over: Partial<PropertyPublic> = {}): PropertyPublic =>
  ({
    id: 105054,
    property_id,
    lat: 50.1,
    lng: 14.45,
    category_main: 'byt',
    category_type: 'prodej',
    disposition: '2+kk',
    ownership: 'osobni',
    display_label: 'Kolbenova, Praha 9',
    is_active: true,
    ...over,
  }) as unknown as PropertyPublic;

function renderPage(p: PropertyPublic, withTile = false) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ExploreAreaButton property={p} images={[]} />
        {withTile && <MarketTile property={p} images={[]} />}
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  openExplore.mockClear();
  vi.mocked(fetchStatsPivot).mockReset().mockResolvedValue(pivotOf({ spec: { rows: [], cols: [], measure: 'ppm2' } }));
  vi.mocked(listStatsDashboards).mockReset().mockResolvedValue({ data: [TEMPLATE], total: 1 });
});

describe('<ExploreAreaButton>', () => {
  it('opens the explore window over the neighbourhood, Stats tab, lens and dashboard', async () => {
    const p = property(801);
    renderPage(p);
    fireEvent.click(screen.getByRole('button', { name: 'Explore area' }));
    const { filters } = neighbourhoodFilters(p, DEFAULT_SCOPE)!;
    await waitFor(() =>
      expect(openExplore).toHaveBeenCalledWith(
        expect.objectContaining({ filters, lat: 50.1, lng: 14.45, tab: 'stats', market: 'sold', dashboard: 'd-tpl' }),
      ),
    );
  });

  it('opens what Prozkoumat okolí opens, whatever the tile is set to', async () => {
    const p = property(802);
    renderPage(p, true);
    await screen.findByRole('button', { name: /Prozkoumat okolí/ });
    fireEvent.click(within(screen.getByRole('group', { name: 'Okruh' })).getByRole('button', { name: '1 km' }));
    fireEvent.click(within(screen.getByRole('group', { name: 'Vzorek' })).getByRole('button', { name: 'Vše' }));
    fireEvent.click(screen.getByRole('button', { name: 'Cihla' }));

    fireEvent.click(screen.getByRole('button', { name: /Prozkoumat okolí/ }));
    await waitFor(() => expect(openExplore).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: 'Explore area' }));
    await waitFor(() => expect(openExplore).toHaveBeenCalledTimes(2));

    const [fromTile, fromMap] = openExplore.mock.calls.map(([payload]) => payload);
    expect(fromMap).toEqual(fromTile);
    expect(fromMap).toMatchObject({
      market: 'all',
      filters: { buildingMaterial: ['cihla'], centerRadius: { radius_m: 1000 } },
    });
  });

  it('still opens, without a dashboard, when the dashboard list cannot be read', async () => {
    vi.mocked(listStatsDashboards).mockRejectedValue(new Error('offline'));
    renderPage(property(803));
    fireEvent.click(screen.getByRole('button', { name: 'Explore area' }));
    await waitFor(() => expect(openExplore).toHaveBeenCalledWith(expect.objectContaining({ dashboard: null })));
  });

  it('is not there when the property has no point', () => {
    renderPage(property(804, { lat: null } as unknown as Partial<PropertyPublic>));
    expect(screen.queryByRole('button', { name: 'Explore area' })).toBeNull();
  });
});
