/* The Browse page turns the cards' ad-count read on: BrowseExperience hands the
 * hydration provider the cards' property ids (`renders.adCounts`), and a card of
 * two ads or more draws "N inzeráty". ListingCards.test.tsx pins the badge under
 * its own provider, so without this file, dropping that one prop would delete the
 * badge from Browse with every test green. The reads are stubbed and the map is
 * collapsed (tests never render a live map). */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import BrowseExperience from './BrowseExperience';
import { useMemoryBrowseState } from '@/lib/browseState';
import { DEFAULT_FILTERS } from '@/lib/filters';
import * as queries from '@/lib/queries';
import type { CardRow } from '@/lib/queries';

vi.mock('@/lib/queries', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/queries')>()),
  fetchListingsForCards: vi.fn(),
  fetchBrowseCount: vi.fn(async () => ({ value: 1, precise: true })),
  fetchImagesForListingIds: vi.fn(async () => new Map()),
  fetchListingCovers: vi.fn(async () => new Map()),
  fetchPropertySourcesByPropertyIds: vi.fn(),
  fetchPropertyCollectionMemberSet: vi.fn(async () => new Map()),
  fetchPipelineMembers: vi.fn(async () => new Map()),
  fetchPipelineStages: vi.fn(async () => []),
  fetchIsDismissed: vi.fn(async () => false),
  fetchNoteCounts: vi.fn(async () => new Map()),
}));
vi.mock('@/lib/brokers', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/brokers')>()),
  fetchListingBrokersByIds: vi.fn(async () => new Map()),
}));

const MAP_COLLAPSED = 'sreality.browse.mapCollapsed';
const ROW = {
  property_id: 42,
  listing_id: 111,
  sreality_id: 900,
  price_czk: 5_400_000,
  area_m2: 62,
  display_label: 'Sadová 12, Praha',
  disposition: '2+kk',
  first_seen_at: '2026-01-01T00:00:00Z',
  last_seen_at: '2026-01-02T00:00:00Z',
  is_active: true,
  tom_days: 3,
  category_main: 'byt',
  category_type: 'prodej',
  source: 'sreality',
  source_id_native: '900',
  mf_gross_yield_pct: null,
  total_price_change_pct: null,
  price_change_count: null,
  all_sources: ['sreality'],
  active_sources: ['sreality'],
} as unknown as CardRow;

function Host() {
  const view = useMemoryBrowseState({ filters: DEFAULT_FILTERS, tab: 'map' });
  return (
    <BrowseExperience
      view={view}
      layout="modal"
      features={{ presetBar: false, watchdog: false, mergeMode: false, title: false, sidebar: false }}
    />
  );
}

beforeEach(() => {
  localStorage.setItem(MAP_COLLAPSED, '1');
  vi.mocked(queries.fetchListingsForCards).mockResolvedValue({ rows: [ROW], nextCursor: null });
  /* Property 42's three ads, as the read answers them: one row per ad. */
  vi.mocked(queries.fetchPropertySourcesByPropertyIds).mockResolvedValue(
    new Map([[42, [1, 2, 3].map((id) => ({ id, property_id: 42 }))]]) as never,
  );
});
afterEach(() => localStorage.removeItem(MAP_COLLAPSED));

describe('<BrowseExperience> the cards\' ad counts', () => {
  it('reads them for the grid\'s properties and draws "3 inzeráty"', async () => {
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter>
          <Host />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    const badge = await screen.findByTitle(/^Nemovitost spojuje 3 inzeráty/);
    expect(badge).toHaveTextContent(/^3\s*inzeráty$/);
    expect(queries.fetchPropertySourcesByPropertyIds).toHaveBeenCalledWith([42], expect.anything());
  });
});
