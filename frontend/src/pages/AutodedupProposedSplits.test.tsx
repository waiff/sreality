/* The proposed-splits page (decision 9): the engine proposes, the operator
 * splits — a checkbox per proposal, a two-step batch, one detach per advert
 * outside the canonical advert's group, progress, and an outcome per advert. */

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

const advert = (listing_id: number, source: string, origin_property_id: number | null) => ({
  listing_id,
  source,
  is_active: true,
  origin_property_id,
});

const ITEMS: api.ProposedSplit[] = [
  {
    property_id: 42,
    canonical_listing_id: 101,
    proposed: true,
    groups: [
      { cluster_key: 1, adverts: [advert(101, 'sreality', null)] },
      { cluster_key: 2, adverts: [advert(202, 'idnes', 43), advert(203, 'bazos', null)] },
    ],
    unseen: [],
    splits: [
      {
        listing_lo: 101,
        listing_hi: 202,
        reason_source: 'pair',
        reason: 'reject: plocha 55 vs 72',
        ruling: null,
      },
    ],
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
    splits: [
      { listing_lo: 301, listing_hi: 302, reason_source: 'none', reason: 'no stated fact', ruling: null },
    ],
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
        listing_lo: 401,
        listing_hi: 402,
        reason_source: 'must_not_link',
        reason: 'must_not_link (operator)',
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
    data: { generation: 'g12', total: 3, items: ITEMS, next_after: null },
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
    expect(await screen.findByText('generace g12 · 3 návrhů')).toBeInTheDocument();

    const p42 = card(42);
    expect(within(p42).getByText('Zůstává')).toBeInTheDocument();
    expect(within(p42).getByText('Oddělit · skupina 2')).toBeInTheDocument();
    expect(within(p42).getByText(/dvojice: reject: plocha 55 vs 72/)).toBeInTheDocument();
    // Photos side by side: one member card per advert.
    for (const id of ['#101', '#202', '#203']) expect(within(p42).getByText(id)).toBeInTheDocument();
    expect(within(p42).getByText('nepřišel sloučením — zůstane')).toBeInTheDocument();
    expect(within(p42).getByRole('link', { name: 'detail' })).toHaveAttribute('href', '/property/42');
    expect(queries.fetchListingsForListingIds).toHaveBeenCalledWith([101, 202, 203, 301, 302, 401, 402, 403]);

    // Nothing outside the first group came by a merge: nothing to split.
    expect(within(card(50)).getByRole('checkbox')).toBeDisabled();

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
  it('asks twice, detaches each advert outside the first group with the shared reason, and reports each', async () => {
    vi.mocked(api.detachListing).mockImplementation(async (propertyId, listingId) => {
      if (listingId === 402) throw new Error('HTTP 409');
      return {
        listing_id: listingId,
        detached: true,
        outcome: 'detached',
        survivor_property_id: propertyId,
        restored_property_id: 43,
        rulings_written: 1,
      };
    });
    const { invalidate } = setup();

    fireEvent.click(await screen.findByRole('checkbox', { name: 'Vybrat nemovitost #42' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Vybrat nemovitost #60' }));
    expect(screen.getByText(/Vybráno 2 · 2 inzeráty k/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Rozdělit vybrané' }));
    expect(screen.getByText('Oddělit 2 inzeráty z 2 nemovitostí?')).toBeInTheDocument();
    expect(api.detachListing).not.toHaveBeenCalled();

    fireEvent.change(screen.getByRole('textbox', { name: /Společný důvod/ }), {
      target: { value: '  jiné patro ' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Ano, rozdělit' }));

    await waitFor(() => expect(api.detachListing).toHaveBeenCalledTimes(2));
    expect(api.detachListing).toHaveBeenNthCalledWith(1, 42, 202, 'jiné patro');
    expect(api.detachListing).toHaveBeenNthCalledWith(2, 60, 402, 'jiné patro');

    const result = await screen.findByRole('region', { name: 'Výsledek rozdělení' });
    expect(within(result).getByText('Odděleno 1 z 2 inzeráty.')).toBeInTheDocument();
    expect(within(result).getByText(/inzerát #202: odděleno → nemovitost #43/)).toBeInTheDocument();
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
