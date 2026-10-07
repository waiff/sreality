/* One portal's "Newest first" (MS19) as the Browse page wires it: the header names the
 * order and each card shows its date on that portal beside its own first seen. Which
 * filters turn it on is queries.test.ts's (orderPortal); these pin the wiring from the
 * view to the header and to the cards. The reads are stubbed and the map is collapsed
 * (tests never render a live map). */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import BrowseExperience from './BrowseExperience';
import { useMemoryBrowseState } from '@/lib/browseState';
import { DEFAULT_FILTERS, type ListingFilters } from '@/lib/filters';
import { fmtShortDate } from '@/lib/format';
import * as queries from '@/lib/queries';
import { DEFAULT_SORT, type CardRow, type SortSpec } from '@/lib/queries';

vi.mock('@/lib/queries', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/queries')>()),
  fetchListingsForCards: vi.fn(),
  fetchBrowseCount: vi.fn(async () => ({ value: 1, precise: true })),
  fetchImagesForListingIds: vi.fn(async () => new Map()),
  fetchListingCovers: vi.fn(async () => new Map()),
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
const IDNES_DAY = '2026-03-05T10:00:00Z';
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
  all_sources: ['idnes', 'sreality'],
  active_sources: ['idnes', 'sreality'],
  newest_ad_at_idnes: IDNES_DAY,
} as unknown as CardRow;

function Host({ filters, sort }: { filters: ListingFilters; sort: SortSpec }) {
  const view = useMemoryBrowseState({ filters, sort, tab: 'map' });
  return (
    <BrowseExperience
      view={view}
      layout="modal"
      features={{ presetBar: false, watchdog: false, mergeMode: false, title: false, sidebar: false }}
    />
  );
}

const renderBrowse = (portals: string[], sort: SortSpec = DEFAULT_SORT) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>
        <Host filters={{ ...DEFAULT_FILTERS, portals }} sort={sort} />
      </MemoryRouter>
    </QueryClientProvider>,
  );

const lifespan = async () => (await screen.findByTitle(/^Aktivní/)).textContent;

beforeEach(() => {
  localStorage.setItem(MAP_COLLAPSED, '1');
  vi.mocked(queries.fetchListingsForCards).mockResolvedValue({ rows: [ROW], nextCursor: null });
});
afterEach(() => localStorage.removeItem(MAP_COLLAPSED));

describe('<BrowseExperience> one portal\'s "Newest first"', () => {
  it('names the order in the header and dates each card on that portal', async () => {
    renderBrowse(['idnes']);
    const chip = await screen.findByText('newest on iDNES Reality');
    expect(chip.getAttribute('title')).toMatch(
      /^Newest first follows iDNES Reality: each property is placed by when its newest iDNES Reality ad was first seen/,
    );
    expect(await lifespan()).toContain(`·iDNESod${fmtShortDate(IDNES_DAY)}`);
  });

  it('names "Oldest first" the same way', async () => {
    renderBrowse(['idnes'], { field: 'first_seen_at', direction: 'asc' });
    expect(await screen.findByText('oldest on iDNES Reality')).toBeInTheDocument();
  });

  it('keeps the property\'s own order and dates with two portals', async () => {
    renderBrowse(['idnes', 'sreality']);
    expect(await lifespan()).not.toContain('iDNES');
    expect(screen.queryByText(/^(newest|oldest) on /)).toBeNull();
  });
});
