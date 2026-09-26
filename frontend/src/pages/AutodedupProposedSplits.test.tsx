/* The proposed-splits page (decision 9): the engine proposes, the operator
 * states the partition — a tick per advert and per group (the ticked adverts of
 * one group leave together, different groups apart, the rest is confirmed one
 * property; nothing ticked = confirm as one), ticks that start at the proposal,
 * a card checkbox for the batch, one `POST /properties/{id}/split` per card
 * behind a two-step confirm, and an outcome per card with a link to each unit's
 * property, the server's undo and the E52 re-send. */

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
  splitProperty: vi.fn(),
  undoSplit: vi.fn(),
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
  detach_outcome = origin_property_id == null ? 'split_native' : 'detached',
) => ({
  listing_id,
  source,
  is_active: true,
  origin_property_id,
  detach_outcome,
  splittable: detach_outcome === 'split_native' || detach_outcome === 'detached',
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
      { cluster_key: null, adverts: [advert(702, 'idnes', 70, 'on_origin')] },
    ],
    unseen: [],
    splits: [pair(701, 702)],
    ruled: false,
  },
];

function result(propertyId: number, units: api.SplitUnit[], undo: api.SplitUndoBody | null = null): api.SplitResult {
  return {
    call_id: 'c-1',
    property_id: propertyId,
    record_kept_by: 'A',
    units,
    moved: units.reduce((n, u) => n + u.moved.length, 0),
    rulings: { written: 3, same: 1, different: 2, must_not_link_written: 2, must_not_link_retracted: 1 },
    reversed_pairs: [],
    undo,
  };
}

const UNDO: api.SplitUndoBody = {
  call_id: 'c-1',
  placements: { '101': 13393, '202': 90211, '303': 13393 },
  rulings: [{ listing_lo: 101, listing_hi: 202, verdict: null, note: null, reasons: [] }],
};

const SCREENSHOT_DONE = result(
  13393,
  [
    { unit: 'A', role: 'kept', listing_ids: [101, 303], property_id: 13393, moved: [], merge_group_id: null },
    {
      unit: 'B',
      role: 'separated',
      listing_ids: [202],
      property_id: 90211,
      moved: [{ listing_id: 202, outcome: 'detached', from: 13393, to: 90211 }],
      merge_group_id: null,
    },
  ],
  UNDO,
);

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
const tick = (pid: number, lid: number) =>
  within(card(pid)).getByRole('checkbox', { name: `Oddělit inzerát #${lid}` }) as HTMLInputElement;

async function run(...pids: number[]) {
  await screen.findByTestId(`proposal-${pids[0]}`);
  for (const pid of pids) fireEvent.click(screen.getByRole('checkbox', { name: `Vybrat nemovitost #${pid}` }));
  fireEvent.click(screen.getByRole('button', { name: 'Provést vybrané' }));
  fireEvent.click(screen.getByRole('button', { name: 'Ano, provést' }));
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.splitProperty).mockImplementation(async (propertyId) => result(propertyId, []));
});

describe('<AutodedupProposedSplits> the cards', () => {
  it('shows each group with a tick per advert and per group, starting at the proposal', async () => {
    setup();
    expect(await screen.findByText('generace g12 · 5 návrhů')).toBeInTheDocument();

    const shot = card(13393);
    expect(within(shot).getAllByText(/dvojice: reject: plocha 55 vs 72/)).toHaveLength(3);
    for (const id of ['#101', '#202', '#303']) expect(within(shot).getByText(id)).toBeInTheDocument();
    expect(within(shot).getByRole('link', { name: 'detail' })).toHaveAttribute('href', '/property/13393');
    // the proposal: both apart groups ticked, the kept group not
    expect([tick(13393, 101).checked, tick(13393, 202).checked, tick(13393, 303).checked]).toEqual([
      false,
      true,
      true,
    ]);
    expect(
      within(shot).getByText(
        'Plán: oddělit #202 (bezrealitky) · oddělit #303 (idnes) · zbytek (#101) potvrdit jako jednu nemovitost',
      ),
    ).toBeInTheDocument();
    expect(within(shot).getByText('Skupina 1 · zůstává')).toBeInTheDocument();
    expect(within(shot).getByText('Skupina 2 · oddělit')).toBeInTheDocument();
    expect(queries.fetchListingsForListingIds).toHaveBeenCalledWith([
      101, 202, 303, 111, 112, 113, 461, 462, 463, 401, 402, 403, 701, 702,
    ]);

    // a proposed group of two now leaves whole
    expect([tick(44, 112).checked, tick(44, 113).checked]).toEqual([true, true]);
    expect(within(card(44)).getByRole('checkbox', { name: 'Oddělit skupinu 2' })).toBeChecked();

    // the canonical advert came by a merge: it leaves; 463 is not stated apart and stays
    expect([tick(46, 461).checked, tick(46, 462).checked, tick(46, 463).checked]).toEqual([
      true,
      false,
      false,
    ]);

    // an advert the engine never saw is a group of its own, with its photos, never pre-ticked
    const p60 = card(60);
    expect(within(p60).getByText('Skupina 3 · engine neviděl · zůstává')).toBeInTheDocument();
    expect(within(p60).getByText('#403')).toBeInTheDocument();
    expect(tick(60, 403).checked).toBe(false);
    expect(within(p60).getByText('rozhodnuto')).toBeInTheDocument();
    expect(within(p60).getByText(/rozhodnutí: Různé/)).toBeInTheDocument();

    // an advert nothing would move has no tick, and says why; the card still selects
    expect(within(card(70)).queryByRole('checkbox', { name: 'Oddělit inzerát #702' })).toBeNull();
    expect(
      within(card(70)).getByText('inzerát už je v nemovitosti, ze které přišel — zůstane'),
    ).toBeInTheDocument();
    expect(within(card(70)).getByText('Plán: potvrdit jako jednu nemovitost')).toBeInTheDocument();
    expect(within(card(70)).getByRole('checkbox', { name: 'Vybrat nemovitost #70' })).toBeEnabled();
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

  it('never pre-ticks on a pair the live stream never compared', async () => {
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
    expect(tick(80, 802).checked).toBe(false);
    expect(within(p80).getByText('Plán: potvrdit jako jednu nemovitost')).toBeInTheDocument();
  });
});

describe('<AutodedupProposedSplits> the statement per card', () => {
  it('the screenshot: idnes unticked — bezrealitky leaves, sreality + idnes are one', async () => {
    const { invalidate } = setup();
    vi.mocked(api.splitProperty).mockResolvedValue(SCREENSHOT_DONE);
    await screen.findByTestId('proposal-13393');
    fireEvent.click(tick(13393, 303));
    expect(
      within(card(13393)).getByText(
        'Plán: oddělit #202 (bezrealitky) · zbytek (#101, #303) potvrdit jako jednu nemovitost',
      ),
    ).toBeInTheDocument();
    await run(13393);
    await waitFor(() => expect(api.splitProperty).toHaveBeenCalledTimes(1));
    expect(api.splitProperty).toHaveBeenCalledWith(13393, {
      adverts: [101, 202, 303],
      separate: [[202]],
      keep_together: true,
    });

    // the outcome links the property each unit sits on now
    const outcome = await screen.findByTestId('outcome-13393');
    expect(within(outcome).getByRole('link', { name: 'zůstává #13393' })).toHaveAttribute('href', '/property/13393');
    expect(within(outcome).getByRole('link', { name: 'vráceno do #90211' })).toHaveAttribute(
      'href',
      '/property/90211',
    );
    expect(within(outcome).getByText(/odděleno: #202 \(bezrealitky\)/)).toBeInTheDocument();
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['autodedup', 'proposed-splits'] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['property'] });

    // Vrátit posts the server's undo body verbatim
    vi.mocked(api.undoSplit).mockResolvedValue({
      call_id: 'c-1',
      undone: true,
      property_id: 13393,
      merge_group_id: 'g-u',
      rulings: { restored: 3 },
    });
    fireEvent.click(within(outcome).getByRole('button', { name: 'Vrátit' }));
    await waitFor(() => expect(api.undoSplit).toHaveBeenCalledWith(13393, UNDO));
    expect(await within(outcome).findByText(/Vráceno — inzeráty jsou znovu jedna nemovitost/)).toBeInTheDocument();
    expect(within(outcome).getByRole('link', { name: '#13393' })).toHaveAttribute('href', '/property/13393');
  });

  it('a group tick separates the whole group; unticking it keeps it', async () => {
    setup();
    await screen.findByTestId('proposal-46');
    fireEvent.click(within(card(46)).getByRole('checkbox', { name: 'Oddělit skupinu 1' }));
    fireEvent.click(within(card(46)).getByRole('checkbox', { name: 'Oddělit skupinu 3' }));
    expect([tick(46, 461).checked, tick(46, 463).checked]).toEqual([false, true]);
    fireEvent.click(within(card(44)).getByRole('checkbox', { name: 'Oddělit skupinu 2' }));
    expect([tick(44, 112).checked, tick(44, 113).checked]).toEqual([false, false]);
    fireEvent.click(within(card(44)).getByRole('checkbox', { name: 'Oddělit skupinu 2' }));
    await run(44, 46);
    await waitFor(() => expect(api.splitProperty).toHaveBeenCalledTimes(2));
    expect(api.splitProperty).toHaveBeenNthCalledWith(1, 44, {
      adverts: [111, 112, 113],
      separate: [[112, 113]],
      keep_together: true,
    });
    expect(api.splitProperty).toHaveBeenNthCalledWith(2, 46, {
      adverts: [461, 462, 463],
      separate: [[463]],
      keep_together: true,
    });
  });

  it('a card with nothing ticked confirms it as one property', async () => {
    setup();
    await screen.findByTestId('proposal-60');
    fireEvent.click(tick(60, 402));
    expect(screen.getByText(/Vybráno 0 · oddělit 0 inzerátů · potvrdit 0 jako jednu/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Vybrat nemovitost #60' }));
    expect(screen.getByText(/Vybráno 1 · oddělit 0 inzerátů · potvrdit 1 jako jednu/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Provést vybrané' }));
    fireEvent.click(screen.getByRole('button', { name: 'Ano, provést' }));
    await waitFor(() =>
      expect(api.splitProperty).toHaveBeenCalledWith(60, {
        adverts: [401, 402, 403],
        separate: [],
        keep_together: true,
      }),
    );
  });

  it('refuses to arm a card whose adverts are all ticked', async () => {
    setup();
    await screen.findByTestId('proposal-13393');
    fireEvent.click(tick(13393, 101));
    expect(within(card(13393)).getByText('Nelze: jedna skupina musí zůstat.')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Vybrat nemovitost #13393' }));
    expect(screen.getByRole('button', { name: 'Provést vybrané' })).toBeDisabled();
    expect(screen.getByText('U #13393 musí jedna skupina zůstat.')).toBeInTheDocument();
  });
});

describe('<AutodedupProposedSplits> Provést vybrané', () => {
  it('asks twice, sends one statement per card with the shared reason, and reports each', async () => {
    vi.mocked(api.splitProperty).mockImplementation(async (propertyId) => {
      if (propertyId === 60) {
        throw new api.ApiError('property 60 holds adverts the statement did not name: 404', 409, {
          detail: { code: 'stale', message: 'property 60 holds adverts the statement did not name: 404', ids: [404] },
        });
      }
      if (propertyId === 70) throw new api.ApiError('HTTP 500', 500, null);
      return result(propertyId, []);
    });
    setup();
    await screen.findByTestId('proposal-44');
    for (const pid of [44, 60, 70]) {
      fireEvent.click(screen.getByRole('checkbox', { name: `Vybrat nemovitost #${pid}` }));
    }
    expect(screen.getByText(/Vybráno 3 · oddělit 3 inzeráty · potvrdit 1 jako jednu/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Provést vybrané' }));
    const confirm = screen.getByRole('group', { name: 'Potvrdit provedení' });
    expect(within(confirm).getByText('Provést 3 návrhy?')).toBeInTheDocument();
    expect(within(confirm).getByText(/#70: Plán: potvrdit jako jednu nemovitost/)).toBeInTheDocument();
    expect(api.splitProperty).not.toHaveBeenCalled();

    fireEvent.change(screen.getByRole('textbox', { name: /Společný důvod/ }), {
      target: { value: '  jiné patro ' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Ano, provést' }));
    await waitFor(() => expect(api.splitProperty).toHaveBeenCalledTimes(3));
    expect(api.splitProperty).toHaveBeenNthCalledWith(1, 44, {
      adverts: [111, 112, 113],
      separate: [[112, 113]],
      keep_together: true,
      reason: 'jiné patro',
    });
    expect(api.splitProperty).toHaveBeenNthCalledWith(2, 60, {
      adverts: [401, 402, 403],
      separate: [[402]],
      keep_together: true,
      reason: 'jiné patro',
    });

    const panel = await screen.findByRole('region', { name: 'Výsledek rozdělení' });
    expect(within(panel).getByText('Provedeno 1 z 3.')).toBeInTheDocument();
    expect(within(screen.getByTestId('outcome-60')).getByText(/Karta se mezitím změnila/)).toBeInTheDocument();
    expect(within(screen.getByTestId('outcome-70')).getByText('Chyba: HTTP 500')).toBeInTheDocument();
    expect(within(screen.getByTestId('outcome-44')).getByText('Beze změny — už platí.')).toBeInTheDocument();
  });

  it('a card that would take back my own "různé" lists the pairs and re-sends with confirm_retract', async () => {
    vi.mocked(api.splitProperty).mockImplementation(async (_pid, statement) => {
      if (!statement.confirm_retract) {
        throw new api.ApiError('takes back', 409, {
          detail: { code: 'reverses_rulings', message: 'takes back', ids: [[101, 303]] },
        });
      }
      return SCREENSHOT_DONE;
    });
    setup();
    await screen.findByTestId('proposal-13393');
    fireEvent.click(tick(13393, 303));
    await run(13393);
    const outcome = await screen.findByTestId('outcome-13393');
    expect(within(outcome).getByText(/rozhodnutí „různé“ u #101 × #303/)).toBeInTheDocument();
    fireEvent.click(within(outcome).getByRole('button', { name: 'Přesto uložit' }));
    await waitFor(() =>
      expect(api.splitProperty).toHaveBeenLastCalledWith(13393, {
        adverts: [101, 202, 303],
        separate: [[202]],
        keep_together: true,
        confirm_retract: true,
      }),
    );
    expect(await within(outcome).findByRole('link', { name: 'vráceno do #90211' })).toBeInTheDocument();
  });

  it('Zrušit steps back without writing', async () => {
    setup();
    await screen.findByTestId('proposal-13393');
    fireEvent.click(screen.getByRole('checkbox', { name: 'Vybrat nemovitost #13393' }));
    fireEvent.click(screen.getByRole('button', { name: 'Provést vybrané' }));
    fireEvent.click(screen.getByRole('button', { name: 'Zrušit' }));
    expect(screen.queryByRole('group', { name: 'Potvrdit provedení' })).toBeNull();
    expect(api.splitProperty).not.toHaveBeenCalled();
  });

  it('offers no batch until a card is selected', async () => {
    setup();
    expect(await screen.findByRole('button', { name: 'Provést vybrané' })).toBeDisabled();
  });
});
