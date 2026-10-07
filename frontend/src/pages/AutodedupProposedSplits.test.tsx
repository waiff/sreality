/* The proposed-splits page (decision 9): the engine proposes, the operator
 * decides on the property page. Each card lists the engine's groups (photos,
 * reasons, rulings, the property's links) and links to the property page's
 * split dialog with the groups as letters (group n -> the n-th letter); the page
 * itself sends nothing. */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import AutodedupProposedSplits from './AutodedupProposedSplits';
import * as api from '@/lib/api';
import * as queries from '@/lib/queries';

vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  getProposedSplits: vi.fn(),
  splitProperty: vi.fn(),
}));
vi.mock('@/lib/queries', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/queries')>()),
  fetchListingsForListingIds: vi.fn(async () => new Map()),
  fetchImagesForListingIds: vi.fn(async () => new Map()),
}));

const advert = (listing_id: number, source: string, origin_property_id: number | null) => ({
  listing_id,
  source,
  is_active: true,
  origin_property_id,
});

const pair = (
  listing_lo: number,
  listing_hi: number,
  reason_source: api.ProposedSplit['splits'][number]['reason_source'] = 'pair',
  reason = 'reject: plocha 55 vs 72',
) => ({ listing_lo, listing_hi, reason_source, reason, ruling: null });

/* The operator's screenshot: sreality stays, bezrealitky is group 2, idnes group 3. */
const SCREENSHOT: api.ProposedSplit = {
  property_id: 13393,
  canonical_listing_id: 101,
  proposed: true,
  groups: [
    { cluster_key: 1, adverts: [advert(101, 'sreality', null)] },
    { cluster_key: 2, adverts: [advert(202, 'bezrealitky', 90211)] },
    { cluster_key: 3, adverts: [advert(303, 'idnes', 90312)] },
  ],
  unseen: [],
  splits: [pair(101, 202), pair(101, 303), pair(202, 303)],
  ruled: false,
};

const ITEMS: api.ProposedSplit[] = [
  SCREENSHOT,
  {
    /* Two adverts the engine calls one flat: they leave together, as one record. */
    property_id: 44,
    canonical_listing_id: 111,
    proposed: true,
    groups: [
      { cluster_key: 6, adverts: [advert(111, 'sreality', null)] },
      { cluster_key: 7, adverts: [advert(112, 'idnes', 45), advert(113, 'bazos', 45)] },
    ],
    unseen: [],
    splits: [pair(111, 112), pair(111, 113)],
    ruled: false,
  },
  {
    /* The canonical advert came by a merge: the property's own group stays. 463 is
     * not stated apart from it, so it is not ticked. */
    property_id: 46,
    canonical_listing_id: 461,
    proposed: true,
    groups: [
      { cluster_key: 8, adverts: [advert(461, 'sreality', 47)] },
      { cluster_key: null, adverts: [advert(462, 'bazos', null)] },
      { cluster_key: null, adverts: [advert(463, 'remax', 48)] },
    ],
    unseen: [],
    splits: [pair(461, 462, 'conflict', 'invariant: floor_spread')],
    ruled: false,
  },
  {
    property_id: 60,
    canonical_listing_id: 401,
    proposed: true,
    groups: [
      { cluster_key: 4, adverts: [advert(401, 'sreality', null)] },
      { cluster_key: 5, adverts: [advert(402, 'remax', 61)] },
    ],
    unseen: [advert(403, 'bazos', null)],
    splits: [
      {
        ...pair(401, 402, 'must_not_link', 'must_not_link (operator)'),
        ruling: {
          verdict: 'different',
          decided_by: 'operator',
          decided_at: '2026-09-24T10:00:00Z',
          note: null,
          reasons: [],
        },
      },
    ],
    ruled: true,
  },
  {
    /* 702 already sits on the property it came from: nothing would move it. */
    property_id: 70,
    canonical_listing_id: 701,
    proposed: true,
    groups: [
      { cluster_key: 9, adverts: [advert(701, 'sreality', null)] },
      { cluster_key: null, adverts: [advert(702, 'idnes', 70)] },
    ],
    unseen: [],
    splits: [pair(701, 702)],
    ruled: false,
  },
];

function setup(items: api.ProposedSplit[] = ITEMS, generation = 'g12') {
  vi.mocked(api.getProposedSplits).mockResolvedValue({
    store_ready: true,
    data: { generation, total: items.length, items, next_after: null },
  });
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const invalidate = vi.spyOn(qc, 'invalidateQueries');
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <AutodedupProposedSplits />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { invalidate };
}

const card = (pid: number) => screen.getByTestId(`proposal-${pid}`);
const splitLink = (pid: number) =>
  within(card(pid)).getByRole('link', { name: 'Rozdělit na stránce nemovitosti' });
const letters = (pid: number) =>
  decodeURIComponent(new URL(splitLink(pid).getAttribute('href')!, 'http://x').searchParams.get('letters')!);

beforeEach(() => {
  vi.clearAllMocks();
});

describe('<AutodedupProposedSplits> the cards', () => {
  it('shows each group with its photos, reasons and rulings, and links out to the split', async () => {
    setup();
    expect(await screen.findByText('generace g12 · 5 návrhů')).toBeInTheDocument();

    const shot = card(13393);
    expect(within(shot).getAllByText(/dvojice: reject: plocha 55 vs 72/)).toHaveLength(3);
    for (const id of ['#101', '#202', '#303']) expect(within(shot).getByText(id)).toBeInTheDocument();
    expect(within(shot).getByRole('link', { name: 'detail' })).toHaveAttribute('href', '/property/13393');
    // the operator's rulings on this property's adverts (E920), as the property page links them
    expect(within(shot).getByRole('link', { name: 'Rozhodnutí o těchto inzerátech' })).toHaveAttribute(
      'href',
      '/autodedup/rulings?property=13393',
    );
    expect(within(shot).getByText('Skupina 1 · A')).toBeInTheDocument();
    expect(within(shot).getByText('Skupina 2 · B')).toBeInTheDocument();
    expect(queries.fetchListingsForListingIds).toHaveBeenCalledWith(
      [101, 202, 303, 111, 112, 113, 461, 462, 463, 401, 402, 403, 701, 702],
      expect.anything(),
    );

    // an advert the engine never saw is a group of its own, with its photos
    const p60 = card(60);
    expect(within(p60).getByText('Skupina 3 · engine neviděl · C')).toBeInTheDocument();
    expect(within(p60).getByText('#403')).toBeInTheDocument();
    expect(within(p60).getByText('rozhodnuto')).toBeInTheDocument();
    expect(within(p60).getByText(/rozhodnutí: Různé/)).toBeInTheDocument();
    // nothing here ticks, batches, confirms or undoes
    expect(screen.queryByRole('checkbox')).toBeNull();
    expect(screen.queryByRole('button', { name: /Provést|Vrátit|Přesto/ })).toBeNull();
  });

  it('links each card to the property page with the engine’s groups as letters', async () => {
    setup();
    await screen.findByTestId('proposal-13393');
    expect(splitLink(13393).getAttribute('href')).toMatch(/^\/property\/13393\?letters=/);
    expect(letters(13393)).toBe('101:A,202:B,303:C');
    // a group of two goes as one letter
    expect(letters(44)).toBe('111:A,112:B,113:B');
    // the engine's group order, whatever the property's own adverts are
    expect(letters(46)).toBe('461:A,462:B,463:C');
    // an advert the engine never saw: a letter after the groups
    expect(letters(60)).toBe('401:A,402:B,403:C');
    expect(api.splitProperty).not.toHaveBeenCalled();
  });

  it('says so when the store is not migrated', async () => {
    vi.mocked(api.getProposedSplits).mockResolvedValue({ store_ready: false, data: null });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <AutodedupProposedSplits />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(await screen.findByText(/Úložiště programu v této databázi zatím není/)).toBeInTheDocument();
  });

  it('says when the live stream never compared the adverts', async () => {
    setup(
      [
        {
          property_id: 80,
          canonical_listing_id: 801,
          proposed: true,
          groups: [
            { cluster_key: 11, adverts: [advert(801, 'sreality', null)] },
            { cluster_key: null, adverts: [advert(802, 'idnes', 81)] },
          ],
          unseen: [],
          splits: [pair(801, 802, 'not_compared', 'not compared')],
          ruled: false,
        },
      ],
      'rt',
    );
    const p80 = await screen.findByTestId('proposal-80');
    expect(within(p80).getByText(/neporovnáno: not compared/)).toBeInTheDocument();
    expect(within(p80).getByText('engine tyto inzeráty neporovnal — rozdělení nenavrhuje')).toBeInTheDocument();
  });
});
