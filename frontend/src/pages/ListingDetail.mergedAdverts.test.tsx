/* ListingDetail with the merged-adverts SECTION switched on (the unmerge switch
 * left off) — the wiring the shipped-dark test in ListingDetail.test cannot
 * reach: the section appears for a property of two adverts, keyed on the
 * property the sources read resolved, and the history block stops listing the
 * same adverts a second time. A separate file because the switch is a module
 * constant and vi.mock is per file. */

import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import ListingDetail from './ListingDetail';
import * as queries from '@/lib/queries';
import type { ListingPublic, PropertySource } from '@/lib/types';

vi.mock('@/lib/mergedAdverts', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/mergedAdverts')>()),
  MERGED_ADVERTS_SECTION_ENABLED: true,
  MERGED_ADVERTS_UNMERGE_ENABLED: false,
}));

const LISTING = {
  id: 105053,
  sreality_id: -11876,
  first_seen_at: '2026-01-01T00:00:00Z',
  last_seen_at: '2026-01-02T00:00:00Z',
  is_active: true,
  source: 'idnes',
  source_id_native: '6a147cfde222cf687509e018',
  property_id: 774,
  category_main: 'byt',
  category_type: 'prodej',
  price_czk: 5_000_000,
  area_m2: 54,
  disposition: '2+kk',
  tom_days: 3,
} as unknown as ListingPublic;

const SOURCES: PropertySource[] = [
  {
    property_id: 774,
    id: 105053,
    sreality_id: -11876,
    source: 'idnes',
    source_url: 'https://reality.idnes.cz/detail/x/',
    source_id_native: '6a147cfde222cf687509e018',
    is_active: true,
    price_czk: 5_000_000,
    first_seen_at: '2026-01-01T00:00:00Z',
    last_seen_at: '2026-01-02T00:00:00Z',
  },
  {
    property_id: 774,
    id: 205,
    sreality_id: 999,
    source: 'sreality',
    source_url: 'https://www.sreality.cz/detail/y',
    source_id_native: '999',
    is_active: true,
    price_czk: 5_100_000,
    first_seen_at: '2026-01-01T00:00:00Z',
    last_seen_at: '2026-01-02T00:00:00Z',
  },
];

vi.mock('@/lib/queries', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/queries')>();
  return {
    ...actual,
    fetchListingById: vi.fn(async () => LISTING),
    fetchListingIdByNaturalKey: vi.fn(async () => 105053),
    fetchPropertyReprNaturalKey: vi.fn(async () => null),
    fetchPropertySources: vi.fn(async () => ({ property_id: 774, sources: SOURCES })),
    fetchPropertyMf: vi.fn(async () => null),
    fetchPropertyStatusEvents: vi.fn(async () => []),
    fetchSnapshotsForListings: vi.fn(async () => []),
    fetchFreshnessChecksByListing: vi.fn(async () => []),
    fetchImagesByListing: vi.fn(async () => []),
    fetchListingsForListingIds: vi.fn(async () => new Map([[205, { ...LISTING, id: 205 }]])),
    fetchImagesForListingIds: vi.fn(async () => new Map()),
  };
});
vi.mock('@/lib/brokers', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/brokers')>()),
  fetchListingBroker: vi.fn(async () => null),
}));
vi.mock('@/components/NewEstimationModal', () => ({
  useNewEstimationModal: () => ({ open: vi.fn() }),
}));
vi.mock('@/components/ExploreAreaModal', () => ({
  useExploreAreaModal: () => ({ open: vi.fn() }),
}));

describe('<ListingDetail> with the merged-adverts section on', () => {
  it('shows one row per advert and drops the history block’s duplicate list', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/listing/idnes/6a147cfde222cf687509e018']}>
          <Routes>
            <Route path="listing/:source/:nativeId" element={<ListingDetail />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByText('Sloučené inzeráty')).toBeInTheDocument();
    expect(screen.getByText('tento inzerát')).toBeInTheDocument();
    expect(queries.fetchListingsForListingIds).toHaveBeenCalledWith([105053, 205]);
    // The history block's per-advert list ("this listing") is the duplicate.
    expect(screen.queryByText('this listing')).not.toBeInTheDocument();
    // No write affordance while the unmerge switch is off.
    expect(screen.queryByRole('button', { name: /Rozdělit/ })).not.toBeInTheDocument();
  });
});
