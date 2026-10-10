/* One portal's "Newest first" (MS19) as the Browse page wires it: the header names the
 * order and each card shows its date on that portal beside its own first seen. Which
 * filters turn it on is queries.test.ts's (orderPortal); these pin the wiring from the
 * view to the header and to the cards. Also the card's "N inzeráty" badge, from the same
 * read, and the Cover pick (#1726) beside them. The reads are stubbed and the map is
 * collapsed (tests never render a live map). */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import BrowseExperience from './BrowseExperience';
import { useMemoryBrowseState } from '@/lib/browseState';
import { DEFAULT_FILTERS, type ListingFilters } from '@/lib/filters';
import { fmtShortDate } from '@/lib/format';
import * as queries from '@/lib/queries';
import { DEFAULT_SORT, type CardRow, type SortSpec } from '@/lib/queries';
import type { ImagePublic } from '@/lib/types';

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
  source_count: 2,
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

/* The badge reads the property's ad count off the cards read (`source_count`,
 * migration 590): the page wires no second read for it. */
describe('<BrowseExperience> the cards\' ad counts', () => {
  it('draws "3 inzeráty" from the cards read', async () => {
    vi.mocked(queries.fetchListingsForCards).mockResolvedValue({
      rows: [{ ...ROW, source_count: 3 } as CardRow],
      nextCursor: null,
    });
    renderBrowse([]);

    const badge = await screen.findByTitle(/^Nemovitost spojuje 3 inzeráty/);
    expect(badge).toHaveTextContent(/^3\s*inzeráty$/);
  });
});

/* The Cover dropdown beside Sort (#1726) as the page wires it: the pick is this
 * browser's own (`sreality.browse.cardCoverTag`) and opens each card on that photo,
 * with the same card's portal badge, ad count and portal date still on it. */
describe('<BrowseExperience> the cards\' cover photo', () => {
  const COVER_KEY = 'sreality.browse.cardCoverTag';
  const tagged = (id: number, clip_fine_tag: string) =>
    ({ id, sreality_url: `https://img/${id}.jpg`, storage_path: null, clip_fine_tag,
       clip_confidence: 0.9, tag_head_scores: null }) as unknown as ImagePublic;
  const counterIs = (want: string) => (_t: string, el: Element | null) =>
    el != null && el.children.length === 0 && el.textContent?.trim() === want;

  beforeEach(() => {
    vi.mocked(queries.fetchImagesForListingIds).mockResolvedValue(
      new Map([[111, [tagged(1, 'exterior_facade'), tagged(2, 'hallway'), tagged(3, 'kitchen'), tagged(4, 'bedroom')]]]),
    );
  });
  afterEach(() => {
    localStorage.removeItem(COVER_KEY);
    vi.mocked(queries.fetchImagesForListingIds).mockResolvedValue(new Map());
  });

  it('opens the card on the picked photo and keeps the pick for this browser', async () => {
    renderBrowse(['idnes']);
    expect(await screen.findByText(counterIs('1 / 4'))).toBeInTheDocument();

    fireEvent.change(screen.getByRole('combobox', { name: /Cover/ }), { target: { value: 'kitchen' } });

    expect(await screen.findByText(counterIs('3 / 4'))).toBeInTheDocument();
    expect(localStorage.getItem(COVER_KEY)).toBe('kitchen');
    const card = screen.getByTitle(/^Portály/).closest('li')!;
    expect(within(card).getByText(counterIs('3 / 4'))).toBeInTheDocument();
    expect(within(card).getByTitle(/^Portály/).textContent).toBe('portáliDNES Reality · Sreality');
    expect(within(card).getByTitle(/^Nemovitost spojuje 2 inzeráty/)).toHaveTextContent(/^2\s*inzeráty$/);
    expect(within(card).getByTitle(/^Aktivní/).textContent).toContain(`·iDNESod${fmtShortDate(IDNES_DAY)}`);
  });

  it('opens on the stored pick when the page loads', async () => {
    localStorage.setItem(COVER_KEY, 'kitchen');
    renderBrowse([]);
    expect(await screen.findByText(counterIs('3 / 4'))).toBeInTheDocument();
  });
});
