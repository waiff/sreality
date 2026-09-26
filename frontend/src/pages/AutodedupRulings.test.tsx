/* The rulings page (E920): every ruling beside the engine's view, filters in the
 * URL, and corrections that are NEW rulings — Flip / Withdraw post
 * `POST /autodedup/verdict` with `supersedes` (a 409 says someone ruled since),
 * never a delete; the split / merge a disagreeing property needs is the existing
 * route, behind a second click. */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import AutodedupRulings, {
  RULING_DEFAULTS,
  corrections,
  sanitizeRulingFilters,
} from './AutodedupRulings';
import * as api from '@/lib/api';

vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  getAutodedupRulings: vi.fn(),
  postAutodedupVerdict: vi.fn(),
  detachListing: vi.fn(),
  mergePropertySet: vi.fn(),
}));
vi.mock('@/lib/queries', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/queries')>()),
  fetchListingsForListingIds: vi.fn(async () => new Map()),
  fetchImagesForListingIds: vi.fn(async () => new Map()),
}));

const AT = '2026-09-19T10:00:00+00:00';

const history = (id: number, verdict: api.AutodedupVerdictValue): api.AutodedupVerdictRow => ({
  id,
  kind: 'pair',
  listing_lo: 11,
  listing_hi: 12,
  verdict,
  note: null,
  decided_by: 'operator@example.com',
  decided_at: AT,
});

function pairRow(over: Partial<api.RulingPairRow> = {}): api.RulingPairRow {
  return {
    ruling_id: 7,
    ruling_kind: 'pair',
    listing_lo: 11,
    listing_hi: 12,
    verdict: 'same',
    status: 'standing',
    source: 'pair',
    merge_group_id: null,
    group_cluster_key: null,
    group_generation: null,
    note: 'stejný byt',
    reasons: [],
    decided_by: 'operator@example.com',
    decided_at: AT,
    n_rows: 1,
    must_not_link: null,
    property_lo: 100,
    property_hi: 200,
    together_now: false,
    adverts_on_property: null,
    obec_kod: 563510,
    obec_name: 'Jablonec nad Nisou',
    cast_obce_kod: null,
    cast_obce_name: null,
    street_lo: 'Mechová',
    cp_lo: '12',
    street_hi: null,
    cp_hi: null,
    zone: 'reject',
    score: 0.12,
    decision: 'auto_reject:attr_contradictions',
    guard_veto: null,
    engine_decided_at: AT,
    engine_group_lo: 900,
    engine_group_hi: null,
    seen_lo: true,
    seen_hi: true,
    engine_view: 'apart',
    agreement: 'disagrees',
    certificate: null,
    why_not_merged: 'auto-rejected on attr_contradictions',
    generation: 'rt',
    history: [history(7, 'same')],
    ...over,
  };
}

function groupRow(over: Partial<api.RulingGroupRow> = {}): api.RulingGroupRow {
  return {
    ruling_key: '31',
    ruling_id: 31,
    source: 'group',
    cluster_key: 501,
    generation: 'g4',
    merge_group_id: null,
    member_ids: [501, 502],
    set_recorded: true,
    verdict: 'same',
    status: 'standing',
    note: null,
    reasons: ['identical_photos'],
    decided_by: 'operator@example.com',
    decided_at: AT,
    n_rows: 1,
    n_members: 2,
    property_ids: [40],
    n_properties: 1,
    together_now: true,
    n_engine_groups: 1,
    n_grouped: 2,
    n_seen: 2,
    obec_kod: null,
    obec_name: null,
    cast_obce_kod: null,
    cast_obce_name: null,
    engine_view: 'together',
    agreement: 'agrees',
    history: [],
    ...over,
  };
}

function page<T>(items: T[], grain: 'pair' | 'group' = 'pair'): api.AutodedupEnvelope<api.RulingsPage<T>> {
  return {
    store_ready: true,
    data: {
      grain,
      generation: 'rt',
      items,
      next_after: null,
      total: items.length,
      facets: { source: { pair: 1 }, status: { standing: 1 }, verdict: { same: 1 }, engine: { disagrees: 1 } },
      towns: [{ grain: 'o', code: 563510, name: 'Jablonec nad Nisou', n: 3 }],
    },
  };
}

function setup(url = '/autodedup/rulings') {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[url]}>
        <AutodedupRulings />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const lastQuery = () => vi.mocked(api.getAutodedupRulings).mock.lastCall?.[0];

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.getAutodedupRulings).mockResolvedValue(page([pairRow()]) as never);
  vi.mocked(api.postAutodedupVerdict).mockResolvedValue({
    store_ready: true,
    data: null,
    must_not_link: false,
  });
});

describe('<AutodedupRulings> the list', () => {
  it('shows the ruling, where the adverts are now and what the engine does', async () => {
    setup();
    const card = await screen.findByTestId('ruling-11-12');
    expect(within(card).getByText('Stejné')).toBeInTheDocument();
    expect(within(card).getByText('platí')).toBeInTheDocument();
    expect(within(card).getByText('k páru')).toBeInTheDocument();
    expect(within(card).getByText('neshoda')).toBeInTheDocument();
    expect(within(card).getByText('dvě různé nemovitosti')).toBeInTheDocument();
    expect(within(card).getByText(/odděleně · pár: reject 0\.12/)).toBeInTheDocument();
    expect(within(card).getByText(/Mechová čp\. 12/)).toBeInTheDocument();
    expect(within(card).getByRole('link', { name: 'nemovitost #100' })).toHaveAttribute(
      'href',
      '/property/100?advert=11',
    );
    expect(within(card).getByRole('link', { name: 'důkazy páru' })).toHaveAttribute(
      'href',
      '/autodedup/pair/11/12?generation=rt',
    );
    fireEvent.click(within(card).getByRole('button', { name: 'Historie (1)' }));
    expect(within(card).getByText(/Stejné · operator@example\.com/)).toBeInTheDocument();
  });

  it('asks the server for "Neshody" first-class and counts it', async () => {
    setup();
    const chip = await screen.findByRole('button', { name: 'Neshody (1)' });
    fireEvent.click(chip);
    await waitFor(() => expect(lastQuery()).toMatchObject({ engine: 'disagrees', grain: 'pair' }));
    expect(lastQuery()?.after).toBeNull();
  });

  it('reads a filter from the link another page built, and drops it on ✕', async () => {
    setup('/autodedup/rulings?property=100&verdict=bogus');
    await screen.findByTestId('ruling-11-12');
    expect(lastQuery()).toMatchObject({ property: 100, verdict: null });
    fireEvent.click(screen.getByRole('button', { name: 'Zrušit filtr nemovitost #100' }));
    await waitFor(() => expect(lastQuery()).toMatchObject({ property: null }));
  });
});

describe('<AutodedupRulings> corrections are new rulings', () => {
  it('withdraws with a note, superseding the ruling it showed', async () => {
    setup();
    const card = await screen.findByTestId('ruling-11-12');
    fireEvent.click(within(card).getByRole('button', { name: 'Odvolat' }));
    expect(within(card).getByText(/Nic se nemaže/)).toBeInTheDocument();
    fireEvent.change(within(card).getByLabelText('Poznámka k opravě (nepovinné)'), {
      target: { value: 'nejsem si jistý' },
    });
    fireEvent.click(within(card).getByRole('button', { name: 'Ano, zapsat' }));
    await waitFor(() =>
      expect(api.postAutodedupVerdict).toHaveBeenCalledWith({
        kind: 'pair',
        verdict: 'unsure',
        note: 'nejsem si jistý',
        listing_lo: 11,
        listing_hi: 12,
        supersedes: 7,
      }),
    );
  });

  it('flips a negative to same', async () => {
    vi.mocked(api.getAutodedupRulings).mockResolvedValue(
      page([pairRow({ verdict: 'same_building_different_unit', agreement: 'agrees' })]) as never,
    );
    setup();
    const card = await screen.findByTestId('ruling-11-12');
    expect(within(card).getByText('Různé')).toBeInTheDocument();
    fireEvent.click(within(card).getByRole('button', { name: 'Otočit na Stejné' }));
    fireEvent.click(within(card).getByRole('button', { name: 'Ano, zapsat' }));
    await waitFor(() =>
      expect(api.postAutodedupVerdict).toHaveBeenCalledWith(
        expect.objectContaining({ verdict: 'same', supersedes: 7, note: null }),
      ),
    );
  });

  it('says so when the ruling was ruled again since the page loaded (409)', async () => {
    vi.mocked(api.postAutodedupVerdict).mockRejectedValue(
      new api.ApiError('ruled again', 409, { detail: 'ruled again' }),
    );
    setup();
    const card = await screen.findByTestId('ruling-11-12');
    fireEvent.click(within(card).getByRole('button', { name: 'Otočit na Různé' }));
    fireEvent.click(within(card).getByRole('button', { name: 'Ano, zapsat' }));
    expect(await within(card).findByRole('alert')).toHaveTextContent(
      'Mezitím bylo o tomto rozhodnuto znovu',
    );
  });

  it('an implied pair is ruled at pair grain, superseding nothing', async () => {
    vi.mocked(api.getAutodedupRulings).mockResolvedValue(
      page([
        pairRow({
          ruling_id: 31,
          ruling_kind: 'cluster',
          source: 'implied',
          group_cluster_key: 501,
          group_generation: 'g4',
        }),
      ]) as never,
    );
    setup();
    const card = await screen.findByTestId('ruling-11-12');
    expect(within(card).getByText('ze skupiny (implikováno)')).toBeInTheDocument();
    expect(within(card).getByText(/Plyne z potvrzené skupiny 501/)).toBeInTheDocument();
    fireEvent.click(within(card).getByRole('button', { name: 'Otočit na Různé' }));
    fireEvent.click(within(card).getByRole('button', { name: 'Ano, zapsat' }));
    await waitFor(() =>
      expect(api.postAutodedupVerdict).toHaveBeenCalledWith(
        expect.objectContaining({ kind: 'pair', verdict: 'different', supersedes: null }),
      ),
    );
  });

  it('offers the split a negative on one property needs, naming how many adverts stay', async () => {
    vi.mocked(api.getAutodedupRulings).mockResolvedValue(
      page([
        pairRow({
          verdict: 'different',
          together_now: true,
          property_hi: 100,
          adverts_on_property: 3,
          engine_view: 'together',
        }),
      ]) as never,
    );
    vi.mocked(api.detachListing).mockResolvedValue({
      listing_id: 12,
      detached: true,
      outcome: 'detached',
      survivor_property_id: 100,
      restored_property_id: 300,
      rulings_written: 2,
    });
    setup();
    const card = await screen.findByTestId('ruling-11-12');
    fireEvent.click(within(card).getByRole('button', { name: 'Rozdělit: oddělit #12' }));
    expect(within(card).getByText(/Nemovitost má 3 inzeráty/)).toBeInTheDocument();
    fireEvent.change(within(card).getByLabelText('Důvod rozdělení (nepovinné)'), {
      target: { value: 'jiné patro' },
    });
    fireEvent.click(within(card).getByRole('button', { name: 'Ano, rozdělit' }));
    await waitFor(() => expect(api.detachListing).toHaveBeenCalledWith(100, 12, 'jiné patro'));
    expect(await within(card).findByText(/oddělen → nemovitost #300/)).toBeInTheDocument();
    expect(api.postAutodedupVerdict).not.toHaveBeenCalled();
  });

  it('offers the merge a same on two properties needs', async () => {
    vi.mocked(api.mergePropertySet).mockResolvedValue({
      merge_group_id: 'g',
      survivor_id: 100,
      retired_ids: [200],
      listings_moved: 1,
      pairs_ruled_same: 1,
    });
    setup();
    const card = await screen.findByTestId('ruling-11-12');
    fireEvent.click(within(card).getByRole('button', { name: 'Sloučit #100 a #200' }));
    fireEvent.click(within(card).getByRole('button', { name: 'Ano, sloučit' }));
    await waitFor(() => expect(api.mergePropertySet).toHaveBeenCalledWith([100, 200]));
  });
});

describe('<AutodedupRulings> group grain', () => {
  it('withdraws a group ruling by its id, and opens a Browse merge as its pairs', async () => {
    vi.mocked(api.getAutodedupRulings).mockResolvedValue(
      page(
        [
          groupRow(),
          groupRow({
            ruling_key: '6c9f0d8e-1b2a-4c3d-9e8f-001122334455',
            ruling_id: null,
            source: 'browse_merge',
            cluster_key: null,
            generation: null,
            merge_group_id: '6c9f0d8e-1b2a-4c3d-9e8f-001122334455',
          }),
        ],
        'group',
      ) as never,
    );
    setup('/autodedup/rulings?grain=group');
    const group = await screen.findByTestId('ruling-group-31');
    expect(lastQuery()).toMatchObject({ grain: 'group' });
    expect(within(group).getByText(/skupina 501 · g4/)).toBeInTheDocument();
    fireEvent.click(within(group).getByRole('button', { name: 'Odvolat' }));
    fireEvent.click(within(group).getByRole('button', { name: 'Ano, zapsat' }));
    await waitFor(() =>
      expect(api.postAutodedupVerdict).toHaveBeenCalledWith({
        kind: 'cluster',
        verdict: 'unsure',
        note: null,
        supersedes: 31,
      }),
    );
    const merge = screen.getByTestId('ruling-group-6c9f0d8e-1b2a-4c3d-9e8f-001122334455');
    expect(within(merge).queryByRole('button', { name: 'Odvolat' })).toBeNull();
    fireEvent.click(within(merge).getByRole('button', { name: 'Zobrazit páry tohoto sloučení' }));
    await waitFor(() =>
      expect(lastQuery()).toMatchObject({
        grain: 'pair',
        merge_group: '6c9f0d8e-1b2a-4c3d-9e8f-001122334455',
      }),
    );
  });
});

describe('corrections', () => {
  it('a standing word flips or is withdrawn; a withdrawn one is said again', () => {
    expect(corrections('same', 'standing', 'pair').map((c) => c.verdict)).toEqual([
      'different',
      'unsure',
    ]);
    expect(corrections('same_project_different_unit', 'standing', 'pair').map((c) => c.verdict)).toEqual([
      'same',
      'unsure',
    ]);
    expect(corrections('unsure', 'withdrawn', 'group').map((c) => c.verdict)).toEqual([
      'same',
      'different',
    ]);
  });
});

describe('sanitizeRulingFilters', () => {
  it('drops a value outside its vocabulary rather than sending it', () => {
    expect(
      sanitizeRulingFilters({
        ...RULING_DEFAULTS,
        grain: 'cluster',
        source: 'group',
        town: 'Jablonec',
        listing: '12x',
        merge_group: 'nope',
        engine: 'disagrees',
      }),
    ).toEqual({ ...RULING_DEFAULTS, engine: 'disagrees' });
  });
});
