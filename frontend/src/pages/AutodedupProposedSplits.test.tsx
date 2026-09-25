/* The proposed-splits page (decision 9): the engine proposes, the operator
 * splits — a checkbox per proposal, a two-step batch, one detach per advert
 * that may leave (alone in its group, splittable — merged in, or the property's
 * own for a new record — stated apart from the group that stays), progress, and
 * an outcome per advert. */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import AutodedupProposedSplits from './AutodedupProposedSplits';
import * as api from '@/lib/api';
import * as queries from '@/lib/queries';

vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  getProposedSplits: vi.fn(),
  detachListing: vi.fn(),
}));
vi.mock('@/lib/queries', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/queries')>()),
  fetchListingsForListingIds: vi.fn(async () => new Map()),
  fetchImagesForListingIds: vi.fn(async () => new Map()),
}));

const advert = (
  listing_id: number,
  source: string,
  origin_property_id: number | null,
  splittable = true,
) => ({
  listing_id,
  source,
  is_active: true,
  origin_property_id,
  splittable,
});

const pair = (
  listing_lo: number,
  listing_hi: number,
  reason_source: api.ProposedSplit['splits'][number]['reason_source'] = 'pair',
  reason = 'reject: plocha 55 vs 72',
) => ({ listing_lo, listing_hi, reason_source, reason, ruling: null });

const ITEMS: api.ProposedSplit[] = [
  {
    property_id: 42,
    canonical_listing_id: 101,
    proposed: true,
    groups: [
      { cluster_key: 1, adverts: [advert(101, 'sreality', null)] },
      { cluster_key: 2, adverts: [advert(202, 'idnes', 43)] },
    ],
    unseen: [],
    splits: [pair(101, 202)],
    ruled: false,
  },
  {
    /* Two adverts the engine calls one flat: detaching them one by one would
     * rule them different from each other. */
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
    /* The canonical advert came by a merge: the property's own advert stays. */
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
    property_id: 50,
    canonical_listing_id: 301,
    proposed: true,
    groups: [
      { cluster_key: 3, adverts: [advert(301, 'sreality', null)] },
      { cluster_key: null, adverts: [advert(302, 'idnes', null)] },
    ],
    unseen: [],
    splits: [pair(301, 302, 'must_not_link', 'must_not_link (operator)')],
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
    /* 702 already sits on the property it came from: its detach would move nothing. */
    property_id: 70,
    canonical_listing_id: 701,
    proposed: true,
    groups: [
      { cluster_key: 9, adverts: [advert(701, 'sreality', null)] },
      { cluster_key: null, adverts: [advert(702, 'idnes', 70, false)] },
    ],
    unseen: [],
    splits: [pair(701, 702)],
    ruled: false,
  },
];

function setup() {
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

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.getProposedSplits).mockResolvedValue({
    store_ready: true,
    data: { generation: 'g12', total: 6, items: ITEMS, next_after: null },
  });
  vi.mocked(api.detachListing).mockImplementation(async (propertyId, listingId) => ({
    listing_id: listingId,
    detached: true,
    outcome: 'detached',
    survivor_property_id: propertyId,
    restored_property_id: propertyId + 1,
    rulings_written: 1,
  }));
});

describe('<AutodedupProposedSplits> the list', () => {
  it('shows each proposal as the engine groups it, with the reason and any ruling', async () => {
    setup();
    expect(await screen.findByText('generace g12 · 6 návrhů')).toBeInTheDocument();

    const p42 = card(42);
    expect(within(p42).getByText('Zůstává')).toBeInTheDocument();
    expect(within(p42).getByText('Oddělit · skupina 2')).toBeInTheDocument();
    expect(within(p42).getByText(/dvojice: reject: plocha 55 vs 72/)).toBeInTheDocument();
    // Photos side by side: one member card per advert.
    for (const id of ['#101', '#202']) expect(within(p42).getByText(id)).toBeInTheDocument();
    expect(within(p42).getByRole('checkbox')).toBeEnabled();
    expect(within(p42).getByRole('link', { name: 'detail' })).toHaveAttribute('href', '/property/42');
    expect(queries.fetchListingsForListingIds).toHaveBeenCalledWith([
      101, 202, 111, 112, 113, 461, 462, 463, 301, 302, 401, 402, 403, 701, 702,
    ]);

    // A group of two cannot leave one advert at a time.
    const p44 = card(44);
    expect(within(p44).getByRole('checkbox')).toBeDisabled();
    expect(within(p44).getAllByText('skupinu nelze oddělit po jednom — zůstane')).toHaveLength(2);
    expect(within(p44).getByText('Skupina 2')).toBeInTheDocument();

    // The canonical advert came by a merge: the property's own group stays, it leaves;
    // an advert the engine never stated apart from the staying group stays too.
    const p46 = card(46);
    expect(within(p46).getByText('Oddělit · skupina 1')).toBeInTheDocument();
    expect(within(p46).getByText('Zůstává')).toBeInTheDocument();
    expect(within(p46).getByText('Skupina 3')).toBeInTheDocument();
    expect(within(p46).getByText('engine ho od zůstávající skupiny neodlišil — zůstane')).toBeInTheDocument();
    expect(within(p46).getByRole('checkbox')).toBeEnabled();

    // Nothing came by a merge: the property's own advert leaves for a new record.
    expect(within(card(50)).getByRole('checkbox')).toBeEnabled();
    expect(within(card(50)).getByText('Oddělit · skupina 2')).toBeInTheDocument();

    // A detach that would move nothing is no split.
    expect(within(card(70)).getByRole('checkbox')).toBeDisabled();
    expect(within(card(70)).getByText('oddělení by ho nepřesunulo — zůstane')).toBeInTheDocument();
    expect(within(card(70)).getByText(/nelze rozdělit/)).toBeInTheDocument();

    const p60 = card(60);
    expect(within(p60).getByText('rozhodnuto')).toBeInTheDocument();
    expect(within(p60).getByText(/rozhodnutí: Různé/)).toBeInTheDocument();
    expect(within(p60).getByText(/Engine neviděl \(zůstávají\): #403 \(bazos\)/)).toBeInTheDocument();
  });

  it('says so when the store is not migrated', async () => {
    vi.mocked(api.getProposedSplits).mockResolvedValue({ store_ready: false, data: null });
    setup();
    expect(await screen.findByText(/Úložiště programu v této databázi zatím není/)).toBeInTheDocument();
  });
});

describe('<AutodedupProposedSplits> Rozdělit vybrané', () => {
  it('asks twice, detaches each advert that may leave with the shared reason, and reports each', async () => {
    vi.mocked(api.detachListing).mockImplementation(async (propertyId, listingId) => {
      if (listingId === 402) throw new Error('HTTP 409');
      const native = listingId === 302;
      return {
        listing_id: listingId,
        detached: true,
        outcome: native ? 'split_native' : 'detached',
        survivor_property_id: propertyId,
        restored_property_id: native ? 9001 : 43,
        rulings_written: 1,
      };
    });
    const { invalidate } = setup();

    fireEvent.click(await screen.findByRole('checkbox', { name: 'Vybrat nemovitost #42' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Vybrat nemovitost #46' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Vybrat nemovitost #50' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Vybrat nemovitost #60' }));
    expect(screen.getByText(/Vybráno 4 · 4 inzeráty k/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Rozdělit vybrané' }));
    expect(screen.getByText('Oddělit 4 inzeráty z 4 nemovitostí?')).toBeInTheDocument();
    expect(api.detachListing).not.toHaveBeenCalled();

    fireEvent.change(screen.getByRole('textbox', { name: /Společný důvod/ }), {
      target: { value: '  jiné patro ' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Ano, rozdělit' }));

    await waitFor(() => expect(api.detachListing).toHaveBeenCalledTimes(4));
    expect(api.detachListing).toHaveBeenNthCalledWith(1, 42, 202, 'jiné patro');
    expect(api.detachListing).toHaveBeenNthCalledWith(2, 46, 461, 'jiné patro');
    expect(api.detachListing).toHaveBeenNthCalledWith(3, 50, 302, 'jiné patro');
    expect(api.detachListing).toHaveBeenNthCalledWith(4, 60, 402, 'jiné patro');

    const result = await screen.findByRole('region', { name: 'Výsledek rozdělení' });
    expect(within(result).getByText('Odděleno 3 z 4 inzeráty.')).toBeInTheDocument();
    expect(within(result).getByText(/inzerát #202: odděleno → nemovitost #43/)).toBeInTheDocument();
    expect(within(result).getByText(/inzerát #302: odděleno → nová nemovitost #9001/)).toBeInTheDocument();
    expect(within(result).getByText(/inzerát #402: chyba: HTTP 409/)).toBeInTheDocument();
    // Read-your-writes: the proposals and every surface re-read.
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['autodedup', 'proposed-splits'] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['property'] });
  });

  it('Zrušit steps back without writing', async () => {
    setup();
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Vybrat nemovitost #42' }));
    fireEvent.click(screen.getByRole('button', { name: 'Rozdělit vybrané' }));
    fireEvent.click(screen.getByRole('button', { name: 'Zrušit' }));
    expect(screen.queryByText(/Oddělit 1 inzerát/)).toBeNull();
    expect(api.detachListing).not.toHaveBeenCalled();
  });

  it('offers no batch until something splittable is selected', async () => {
    setup();
    expect(await screen.findByRole('button', { name: 'Rozdělit vybrané' })).toBeDisabled();
  });
});
