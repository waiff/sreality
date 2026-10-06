/* The category review (E937): the property ids come from the link and are read
 * ten per request; each mixed property is a card of its sides (photos, facts and
 * each ad's own words, a contentless record as one muted line) with a letter per
 * ad, one per side until the operator moves one, and two decisions, each behind
 * a second click, both `POST /properties/{id}/split`: split by the letters (the
 * property page's `splitPlan`: every letter but the one that stays leaves as one
 * property) or keep as one property. The outcome stays in place with the
 * server's undo and the E52 re-send; a property no longer mixed, or confirmed,
 * is a done row. */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import AutodedupCategorySplits from './AutodedupCategorySplits';
import * as api from '@/lib/api';
import { fmtCzk } from '@/lib/format';
import * as queries from '@/lib/queries';
import type { ListingPublic } from '@/lib/types';

vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  getCategorySplits: vi.fn(),
  splitProperty: vi.fn(),
  undoSplit: vi.fn(),
}));
vi.mock('@/lib/queries', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/queries')>()),
  fetchListingsForListingIds: vi.fn(async () => new Map()),
  fetchImagesForListingIds: vi.fn(async () => new Map()),
}));

const ad = (
  listing_id: number,
  source: string,
  origin_property_id: number | null = null,
  detach_outcome = origin_property_id == null ? 'split_native' : 'detached',
  extra: Partial<api.CategorySplitAdvert> = {},
): api.CategorySplitAdvert => ({
  listing_id,
  source,
  is_active: true,
  origin_property_id,
  detach_outcome,
  splittable: detach_outcome === 'split_native' || detach_outcome === 'detached',
  empty: false,
  unknown: false,
  text: { title: `Inzerát ${listing_id}`, description: `Popis inzerátu ${listing_id}.` },
  ...extra,
});

const side = (
  category_type: string,
  category_main: string[],
  kept: boolean,
  adverts: api.CategorySplitAdvert[],
): api.CategorySplitSide => ({
  cluster_key: null,
  label: `${category_type} · ${category_main.join(' + ')}`,
  kept,
  category_type,
  category_main,
  adverts,
});

const clash = (lo: number, hi: number, reason: string) => ({
  listing_lo: lo,
  listing_hi: hi,
  reason_source: 'category' as const,
  reason,
  ruling: null,
});

const item = (
  property_id: number,
  groups: api.CategorySplitSide[],
  splits: api.CategorySplit['splits'],
  state: Partial<Pick<api.CategorySplit, 'mixed' | 'confirmed' | 'ruled'>> = {},
): api.CategorySplit => ({
  property_id,
  canonical_listing_id: groups[0].adverts[0].listing_id,
  groups,
  unseen: [],
  splits,
  ruled: false,
  mixed: true,
  confirmed: false,
  ...state,
});

/* 12664: six sale ads and three rental ads of one 30 m² flat, and a Bazoš index
 * sighting stored under the default byt/prodej that rides with the kept side. */
const SALE_RENT = item(
  12664,
  [
    side('prodej', ['byt'], true, [
      ad(1266401, 'sreality', null, 'split_native', {
        text: { title: 'Prodej bytu 1+kk, 30 m²', description: 'Světlý byt po rekonstrukci, [telefon].' },
      }),
      ...[1266402, 1266403, 1266404, 1266405, 1266406].map((id, i) => ad(id, 'idnes', 50002 + i)),
      ad(1266410, 'bazos', null, 'split_native', {
        empty: true,
        text: { title: 'Byt 1+kk', description: null },
      }),
    ]),
    side('pronajem', ['byt'], false, [
      ad(1266407, 'sreality', null, 'split_native', {
        text: { title: 'Pronájem bytu 1+kk, 30 m²', description: 'Pronájem od listopadu, 9 500 Kč měsíčně.' },
      }),
      ad(1266408, 'idnes', 50008),
      ad(1266409, 'bezrealitky', 50009),
    ]),
  ],
  [clash(1266401, 1266407, 'category_type: prodej vs pronajem')],
);

/* 9737: two flat ads and two commercial ads, every pair across ruled "stejné". */
const CONFIRMED = item(
  9737,
  [
    side('prodej', ['komercni'], true, [ad(973701, 'sreality'), ad(973702, 'sreality')]),
    side('prodej', ['byt'], false, [ad(973703, 'idnes', 9738), ad(973704, 'idnes', 9738)]),
  ],
  [clash(973701, 973703, 'category_main: komercni vs byt')],
  { confirmed: true, ruled: true },
);

/* 53488: house and commercial are one side; the flat ad cannot leave, so the
 * split is not offered; an ad of unknown category rides with the kept side. */
const BLOCKED = item(
  53488,
  [
    side('prodej', ['dum', 'komercni'], true, [
      ad(534881, 'sreality'),
      ad(534882, 'idnes', 53490),
      ad(534884, 'remax', 53491),
      ad(534885, 'realitymix', 53492, 'detached', { unknown: true }),
    ]),
    side('prodej', ['byt'], false, [ad(534883, 'bazos', 53489, 'origin_moved_on')]),
  ],
  [clash(534881, 534883, 'category_main: dum vs byt')],
);

/* 914: nineteen rentals and one contentless record: one category, nothing to decide. */
const ONE_CATEGORY = item(
  914,
  [side('pronajem', ['byt'], true, [ad(91400, 'sreality'), ad(91419, 'bazos', null, 'split_native', { empty: true })])],
  [],
  { mixed: false },
);

/* 197654: two house sales and a share sale; one share ad already sits on its origin. */
const SHARE = item(
  197654,
  [
    side('prodej', ['dum'], true, [ad(1976541, 'sreality'), ad(1976542, 'idnes', 197001)]),
    side('podil', ['dum'], false, [ad(1976543, 'sreality', 197002), ad(1976544, 'idnes', 197654, 'on_origin')]),
  ],
  [clash(1976541, 1976543, 'category_type: prodej vs podil')],
);

const ITEMS = [SALE_RENT, CONFIRMED, BLOCKED, ONE_CATEGORY, SHARE];
const LINK = '/autodedup/category-splits?properties=12664,9737,53488,914,197654,5';

function result(
  propertyId: number,
  units: api.SplitUnit[],
  undo: api.SplitUndoBody | null = null,
): api.SplitResult {
  return {
    call_id: 'c-7',
    property_id: propertyId,
    record_kept_by: 'A',
    units,
    moved: units.reduce((n, u) => n + u.moved.length, 0),
    rulings: { written: 1, same: 0, different: 1, must_not_link_written: 1, must_not_link_retracted: 0 },
    reversed_pairs: [],
    undo,
  };
}

const UNDO: api.SplitUndoBody = {
  call_id: 'c-7',
  placements: { '1266407': 12664, '1266408': 12664, '1266409': 12664 },
  rulings: [{ listing_lo: 1266401, listing_hi: 1266407, verdict: null, note: null, reasons: [] }],
};

const SPLIT_DONE = result(
  12664,
  [
    {
      unit: 'A',
      role: 'kept',
      listing_ids: [1266401, 1266402, 1266403, 1266404, 1266405, 1266406, 1266410],
      property_id: 12664,
      moved: [],
      merge_group_id: null,
    },
    {
      unit: 'B',
      role: 'separated',
      listing_ids: [1266407, 1266408, 1266409],
      property_id: 90001,
      moved: [
        { listing_id: 1266407, outcome: 'split_native', from: 12664, to: 90001 },
        { listing_id: 1266408, outcome: 'detached', from: 12664, to: 50008 },
        { listing_id: 1266409, outcome: 'detached', from: 12664, to: 50009 },
      ],
      merge_group_id: 'g-join',
    },
  ],
  UNDO,
);

const KEPT_DONE = result(
  12664,
  [
    {
      unit: 'A',
      role: 'kept',
      listing_ids: [1266401, 1266402, 1266403, 1266404, 1266405, 1266406, 1266410, 1266407, 1266408, 1266409],
      property_id: 12664,
      moved: [],
      merge_group_id: null,
    },
  ],
  { call_id: 'c-8', placements: {}, rulings: [] },
);

function setup(url = LINK, items: api.CategorySplit[] = ITEMS) {
  vi.mocked(api.getCategorySplits).mockImplementation(async (ids) => ({
    store_ready: true,
    data: {
      items: items.filter((i) => ids.includes(i.property_id)),
      missing: ids.filter((id) => !items.some((i) => i.property_id === id)),
    },
  }));
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const invalidate = vi.spyOn(qc, 'invalidateQueries');
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[url]}>
        <AutodedupCategorySplits />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { invalidate };
}

const card = (pid: number) => screen.getByTestId(`category-${pid}`);
const SALE_ADS = [1266401, 1266402, 1266403, 1266404, 1266405, 1266406, 1266410];
const RENT_ADS = [1266407, 1266408, 1266409];
/* An ad's letter, as the property page names it. */
const LETTER = (id: number, portal: string) => `Nemovitost inzerátu ${portal} #${id}`;
const planLines = (confirm: HTMLElement) =>
  within(confirm).getAllByRole('listitem').map((li) => li.textContent);

/* 12664's facts: one 30 m² flat for sale; two ads of one rental flat at 10 000 Kč
 * a month and a third, another flat, at 9 500 (the operator's case of 2026-10-05). */
const FACTS = new Map(
  (
    [
      [1266401, 'sreality', 2_990_000, 'prodej'],
      [1266402, 'idnes', 2_990_000, 'prodej'],
      [1266403, 'idnes', 2_990_000, 'prodej'],
      [1266404, 'idnes', 2_990_000, 'prodej'],
      [1266405, 'idnes', 2_990_000, 'prodej'],
      [1266406, 'idnes', 2_990_000, 'prodej'],
      [1266407, 'sreality', 10_000, 'pronajem'],
      [1266408, 'idnes', 10_000, 'pronajem'],
      [1266409, 'bezrealitky', 9_500, 'pronajem'],
    ] as const
  ).map(([id, source, price_czk, category_type]) => [
    id,
    {
      id,
      source,
      price_czk,
      category_type,
      category_main: 'byt',
      is_active: true,
    } as unknown as ListingPublic,
  ]),
);
const SALE_LINE = [
  `Sreality ${fmtCzk(2_990_000)}`,
  ...Array.from({ length: 5 }, () => `iDNES Reality ${fmtCzk(2_990_000)}`),
  'Bazoš cena neuvedena',
].join(', ');
const RENT_LINE = [
  `Sreality ${fmtCzk(10_000)} / měs`,
  `iDNES Reality ${fmtCzk(10_000)} / měs`,
  `Bezrealitky ${fmtCzk(9_500)} / měs`,
].join(', ');

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(queries.fetchListingsForListingIds).mockResolvedValue(new Map());
});

describe('<AutodedupCategorySplits> the list', () => {
  it('shows each side with its photos, facts and words, the contentless record as a line', async () => {
    setup();
    const shot = await screen.findByTestId('category-12664');
    expect(within(shot).getByRole('heading', { name: 'Nemovitost #12664' })).toBeInTheDocument();
    expect(within(shot).getByText('10 inzerátů · 2 strany')).toBeInTheDocument();
    expect(within(shot).getByRole('link', { name: 'detail' })).toHaveAttribute('href', '/property/12664');
    expect(within(shot).getByRole('link', { name: 'Rozhodnutí o těchto inzerátech' })).toHaveAttribute(
      'href',
      '/autodedup/rulings?property=12664',
    );

    const sale = within(shot).getByRole('region', { name: 'Prodej · byt' });
    const rent = within(shot).getByRole('region', { name: 'Pronájem · byt' });
    expect(within(sale).getByText('Prodej · byt · 6 inzerátů')).toBeInTheDocument();
    expect(within(rent).getByText('Pronájem · byt · 3 inzeráty')).toBeInTheDocument();
    // every ad a card with its number, and its own words under it
    for (const id of [1266401, 1266406]) expect(within(sale).getByText(`#${id}`)).toBeInTheDocument();
    for (const id of RENT_ADS) expect(within(rent).getByText(`#${id}`)).toBeInTheDocument();
    expect(within(sale).getByRole('heading', { name: 'Prodej bytu 1+kk, 30 m²' })).toBeInTheDocument();
    expect(within(sale).getByText('Světlý byt po rekonstrukci, [telefon].')).toBeInTheDocument();
    expect(within(rent).getByRole('heading', { name: 'Pronájem bytu 1+kk, 30 m²' })).toBeInTheDocument();
    // the contentless record is one muted line, not a card
    expect(
      within(sale).getByText('#1266410 · Bazoš · prázdný záznam, bez ceny, plochy a textu, zůstane'),
    ).toBeInTheDocument();
    expect(within(sale).queryByText('#1266410')).toBeNull();
    expect(within(shot).getByText(/kategorie: Prodej · byt × Pronájem · byt/)).toBeInTheDocument();

    // the open cards' ads are hydrated in one read, done rows and contentless records not
    expect(queries.fetchListingsForListingIds).toHaveBeenCalledWith(
      [1266401, 1266402, 1266403, 1266404, 1266405, 1266406, 1266407, 1266408, 1266409,
        534881, 534882, 534884, 534885, 534883, 1976541, 1976542, 1976543, 1976544],
      expect.anything(),
    );
  });

  it('says why a split is not offered and what rides along', async () => {
    setup();
    const blocked = await screen.findByTestId('category-53488');
    expect(within(blocked).getByRole('region', { name: 'Prodej · dům + komerční prostor' })).toBeInTheDocument();
    expect(
      within(blocked).getByText('kategorie neuvedena — bez písmena, zůstane se skupinou, která zůstává'),
    ).toBeInTheDocument();
    expect(within(blocked).queryByLabelText(LETTER(534885, 'RealityMix'))).toBeNull();
    expect(
      within(blocked).getByText(
        'Nelze oddělit: nemovitost, ze které přišel, byla mezitím sloučena jinam; nejdřív rozdělte tam.',
      ),
    ).toBeInTheDocument();
    // the ad of unknown category is counted with the group that stays
    expect(within(blocked).getByText('A zůstává (4) · B odejde (1)')).toBeInTheDocument();
    expect(within(blocked).getByRole('button', { name: 'Rozdělit podle písmen' })).toBeDisabled();
    expect(
      within(blocked).getByText(
        'Rozdělit nelze: #534883 nejde oddělit (nemovitost, ze které přišel, byla mezitím sloučena jinam; nejdřív rozdělte tam).',
      ),
    ).toBeInTheDocument();
    expect(within(blocked).getByRole('button', { name: 'Ponechat jako jednu nemovitost' })).toBeEnabled();
  });

  it('is the progress view: counts, done rows and the ids without a card', async () => {
    setup();
    await screen.findByTestId('category-12664');
    expect(
      screen.getByText(
        'Nemovitostí v odkazu: 6 · stránka 1 / 1 (po 10) · na této stránce: k rozhodnutí 3 · ' +
          'rozděleno 1 · ponecháno 1',
      ),
    ).toBeInTheDocument();
    const kept = card(9737);
    expect(within(kept).getByText('ponecháno jako jedna nemovitost')).toBeInTheDocument();
    expect(within(kept).getByRole('link', { name: 'detail' })).toHaveAttribute('href', '/property/9737');
    expect(within(kept).queryByRole('button')).toBeNull();
    expect(within(card(914)).getByText('jen jedna kategorie (Pronájem · byt), není co dělit')).toBeInTheDocument();
    expect(within(card(914)).queryByRole('region')).toBeNull();
    expect(
      screen.getByText('Bez karty (nejsou aktivní nemovitostí se dvěma a více inzeráty): #5'),
    ).toBeInTheDocument();
  });

  it('reads the ids from the link, in order and once; without ids it says where it opens from', async () => {
    setup('/autodedup/category-splits?properties=12664, 9737,abc,12664,');
    await screen.findByTestId('category-12664');
    expect(api.getCategorySplits).toHaveBeenCalledWith([12664, 9737]);

    cleanup();
    vi.clearAllMocks();
    setup('/autodedup/category-splits');
    expect(await screen.findByText(/Tato stránka se otevírá z odkazu, který nemovitosti vyjmenuje/)).toBeInTheDocument();
    expect(api.getCategorySplits).not.toHaveBeenCalled();
  });

  it('pages the link ten properties per request', async () => {
    const ids = Array.from({ length: 23 }, (_, i) => 100 + i);
    setup(`/autodedup/category-splits?properties=${ids.join(',')}`, []);
    expect(await screen.findByText(/Nemovitostí v odkazu: 23 · stránka 1 \/ 3 \(po 10\)/)).toBeInTheDocument();
    expect(api.getCategorySplits).toHaveBeenLastCalledWith(ids.slice(0, 10));
    expect(screen.getByRole('button', { name: '← Předchozí' })).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: 'Další →' }));
    await waitFor(() => expect(api.getCategorySplits).toHaveBeenLastCalledWith(ids.slice(10, 20)));
    expect(await screen.findByText(/stránka 2 \/ 3/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Další →' }));
    await waitFor(() => expect(api.getCategorySplits).toHaveBeenLastCalledWith(ids.slice(20)));
    expect(screen.getByRole('button', { name: 'Další →' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '← Předchozí' }));
    expect(await screen.findByText(/stránka 2 \/ 3/)).toBeInTheDocument();
    expect(api.getCategorySplits).toHaveBeenCalledTimes(3);
  });
});

describe('<AutodedupCategorySplits> the decisions', () => {
  it('untouched letters split by category: asks twice, sends the other side, offers the undo', async () => {
    const { invalidate } = setup();
    vi.mocked(queries.fetchListingsForListingIds).mockResolvedValue(FACTS);
    vi.mocked(api.splitProperty).mockResolvedValue(SPLIT_DONE);
    const shot = await screen.findByTestId('category-12664');
    // one letter per side; the contentless record takes none
    expect(within(shot).getByLabelText(LETTER(1266401, 'Sreality'))).toHaveValue('A');
    expect(within(shot).getByLabelText(LETTER(1266407, 'Sreality'))).toHaveValue('B');
    expect(within(shot).queryByLabelText(LETTER(1266410, 'Bazoš'))).toBeNull();
    expect(within(shot).getByText('A zůstává (7) · B odejde (3)')).toBeInTheDocument();

    fireEvent.click(within(shot).getByRole('button', { name: 'Rozdělit podle písmen' }));
    const confirm = within(shot).getByRole('group', { name: 'Potvrdit rozdělení' });
    await waitFor(() =>
      expect(planLines(confirm)).toEqual([
        `A — zůstává v nemovitosti #12664: ${SALE_LINE}`,
        `B — odejde jako jedna nemovitost: ${RENT_LINE}`,
      ]),
    );
    expect(confirm.textContent).toContain('každá dvojice inzerátů napříč písmeny se uloží jako „různé“');
    // what the confirm shows is what is sent: the letters wait
    expect(within(shot).getByLabelText(LETTER(1266409, 'Bezrealitky'))).toBeDisabled();
    expect(api.splitProperty).not.toHaveBeenCalled();
    fireEvent.click(within(confirm).getByRole('button', { name: 'Zrušit' }));
    expect(within(shot).queryByRole('group', { name: 'Potvrdit rozdělení' })).toBeNull();
    expect(api.splitProperty).not.toHaveBeenCalled();

    fireEvent.click(within(shot).getByRole('button', { name: 'Rozdělit podle písmen' }));
    fireEvent.click(within(shot).getByRole('button', { name: 'Potvrdit' }));
    await waitFor(() =>
      expect(api.splitProperty).toHaveBeenCalledWith(12664, {
        adverts: [...SALE_ADS, ...RENT_ADS],
        separate: [RENT_ADS],
        keep_together: false,
      }),
    );

    // the outcome in place: where each unit sits now, and the server's undo
    expect(await within(shot).findByRole('link', { name: 'sloučeno do #90001' })).toHaveAttribute(
      'href',
      '/property/90001',
    );
    expect(within(shot).getByRole('link', { name: 'zůstává #12664' })).toBeInTheDocument();
    expect(within(shot).getByText(/odděleno: #1266407 \(sreality\), #1266408 \(idnes\)/)).toBeInTheDocument();
    expect(within(shot).queryByRole('button', { name: 'Rozdělit podle písmen' })).toBeNull();
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['autodedup', 'category-splits'] });

    vi.mocked(api.undoSplit).mockResolvedValue({
      call_id: 'c-7',
      undone: true,
      property_id: 12664,
      merge_group_id: 'g-u',
      rulings: { restored: 1 },
    });
    fireEvent.click(within(shot).getByRole('button', { name: 'Vrátit' }));
    await waitFor(() => expect(api.undoSplit).toHaveBeenCalledWith(12664, UNDO));
    expect(await within(card(12664)).findByText(/Vráceno — inzeráty jsou znovu jedna nemovitost/)).toBeInTheDocument();
    // back to a card the operator can decide again
    expect(within(card(12664)).getByRole('button', { name: 'Rozdělit podle písmen' })).toBeEnabled();
  });

  it('a leaving group holding an ad that cannot move is not split; its letter can keep it', async () => {
    setup();
    vi.mocked(api.splitProperty).mockResolvedValue(result(197654, []));
    const share = await screen.findByTestId('category-197654');
    expect(within(share).getByRole('button', { name: 'Rozdělit podle písmen' })).toBeDisabled();
    expect(
      within(share).getByText(
        'Rozdělit nelze: #1976544 nejde oddělit (inzerát už je v nemovitosti, ze které přišel).',
      ),
    ).toBeInTheDocument();
    expect(within(share).getByRole('button', { name: 'Ponechat jako jednu nemovitost' })).toBeEnabled();
    expect(api.splitProperty).not.toHaveBeenCalled();

    // the operator keeps the stuck ad with the group that stays: the rest of its side leaves
    fireEvent.change(within(share).getByLabelText(LETTER(1976544, 'iDNES Reality')), {
      target: { value: 'A' },
    });
    expect(within(share).getByText('A zůstává (3) · B odejde (1)')).toBeInTheDocument();
    fireEvent.click(within(share).getByRole('button', { name: 'Rozdělit podle písmen' }));
    fireEvent.click(within(share).getByRole('button', { name: 'Potvrdit' }));
    await waitFor(() =>
      expect(api.splitProperty).toHaveBeenCalledWith(197654, {
        adverts: [1976541, 1976542, 1976543, 1976544],
        separate: [[1976543]],
        keep_together: false,
      }),
    );
  });

  it('a side that bundles two flats: one rental ad to C sends three units', async () => {
    setup();
    vi.mocked(queries.fetchListingsForListingIds).mockResolvedValue(FACTS);
    vi.mocked(api.splitProperty).mockResolvedValue(result(12664, []));
    const shot = await screen.findByTestId('category-12664');
    fireEvent.change(within(shot).getByLabelText(LETTER(1266409, 'Bezrealitky')), {
      target: { value: 'C' },
    });
    expect(within(shot).getByText('A zůstává (7) · B odejde (2) · C odejde (1)')).toBeInTheDocument();

    fireEvent.click(within(shot).getByRole('button', { name: 'Rozdělit podle písmen' }));
    const confirm = within(shot).getByRole('group', { name: 'Potvrdit rozdělení' });
    await waitFor(() =>
      expect(planLines(confirm)).toEqual([
        `A — zůstává v nemovitosti #12664: ${SALE_LINE}`,
        `B — odejde jako jedna nemovitost: Sreality ${fmtCzk(10_000)} / měs, iDNES Reality ${fmtCzk(10_000)} / měs`,
        `C — odejde jako jedna nemovitost: Bezrealitky ${fmtCzk(9_500)} / měs`,
      ]),
    );
    fireEvent.click(within(confirm).getByRole('button', { name: 'Potvrdit' }));
    await waitFor(() =>
      expect(api.splitProperty).toHaveBeenCalledWith(12664, {
        adverts: [...SALE_ADS, ...RENT_ADS],
        separate: [[1266407, 1266408], [1266409]],
        keep_together: false,
      }),
    );
  });

  it('one letter for every ad leaves nothing to split, and says so', async () => {
    setup();
    const shot = await screen.findByTestId('category-12664');
    const rentals = [
      [1266407, 'Sreality'],
      [1266408, 'iDNES Reality'],
      [1266409, 'Bezrealitky'],
    ] as const;
    for (const [id, portal] of rentals) {
      fireEvent.change(within(shot).getByLabelText(LETTER(id, portal)), { target: { value: 'A' } });
    }
    expect(within(shot).getByText('A zůstává (10)')).toBeInTheDocument();
    expect(within(shot).getByRole('button', { name: 'Rozdělit podle písmen' })).toBeDisabled();
    expect(
      within(shot).getByText('Všechny inzeráty mají stejné písmeno, není co rozdělit.'),
    ).toBeInTheDocument();
    expect(within(shot).getByRole('button', { name: 'Ponechat jako jednu nemovitost' })).toBeEnabled();
  });

  it('the riders go with the group that stays, also when the letters change which group that is', async () => {
    setup();
    vi.mocked(queries.fetchListingsForListingIds).mockResolvedValue(FACTS);
    vi.mocked(api.splitProperty).mockResolvedValue(result(12664, []));
    const shot = await screen.findByTestId('category-12664');
    // the sale side's own ad joins the rentals: B now holds two own ads and stays
    fireEvent.change(within(shot).getByLabelText(LETTER(1266401, 'Sreality')), {
      target: { value: 'B' },
    });
    expect(within(shot).getByText('A odejde (5) · B zůstává (5)')).toBeInTheDocument();
    fireEvent.click(within(shot).getByRole('button', { name: 'Rozdělit podle písmen' }));
    const confirm = within(shot).getByRole('group', { name: 'Potvrdit rozdělení' });
    await waitFor(() =>
      expect(planLines(confirm)[1]).toMatch(/^B — zůstává v nemovitosti #12664: .*Bazoš cena neuvedena$/),
    );
    fireEvent.click(within(confirm).getByRole('button', { name: 'Potvrdit' }));
    await waitFor(() =>
      expect(api.splitProperty).toHaveBeenCalledWith(12664, {
        adverts: [...SALE_ADS, ...RENT_ADS],
        separate: [[1266402, 1266403, 1266404, 1266405, 1266406]],
        keep_together: false,
      }),
    );
  });

  it('Ponechat jako jednu nemovitost asks twice and keeps every ad as one property', async () => {
    setup();
    vi.mocked(api.splitProperty).mockResolvedValue(KEPT_DONE);
    const shot = await screen.findByTestId('category-12664');
    fireEvent.click(within(shot).getByRole('button', { name: 'Ponechat jako jednu nemovitost' }));
    const confirm = within(shot).getByRole('group', { name: 'Potvrdit ponechání' });
    expect(
      within(confirm).getByText(
        'Plán: potvrdit jako jednu nemovitost — všechny inzeráty zůstanou na #12664 a mezi stranami ' +
          'se zapíše „stejné“',
      ),
    ).toBeInTheDocument();
    expect(api.splitProperty).not.toHaveBeenCalled();
    fireEvent.click(within(confirm).getByRole('button', { name: 'Potvrdit' }));
    await waitFor(() =>
      expect(api.splitProperty).toHaveBeenCalledWith(12664, {
        adverts: [...SALE_ADS, ...RENT_ADS],
        separate: [],
        keep_together: true,
      }),
    );
    expect(await within(shot).findByText('Ponecháno jako jedna nemovitost.')).toBeInTheDocument();
    expect(within(shot).getByRole('button', { name: 'Vrátit' })).toBeEnabled();
  });

  it('a keep that would take back an earlier "různé" says so and re-sends with confirm_retract', async () => {
    vi.mocked(api.splitProperty).mockImplementation(async (_pid, statement) => {
      if (!statement.confirm_retract) {
        throw new api.ApiError('takes back', 409, {
          detail: { code: 'reverses_rulings', message: 'takes back', ids: [[1266401, 1266407]] },
        });
      }
      return KEPT_DONE;
    });
    setup();
    const shot = await screen.findByTestId('category-12664');
    fireEvent.click(within(shot).getByRole('button', { name: 'Ponechat jako jednu nemovitost' }));
    fireEvent.click(within(shot).getByRole('button', { name: 'Potvrdit' }));
    expect(await within(shot).findByText(/rozhodnutí „různé“ u #1266401 × #1266407/)).toBeInTheDocument();
    fireEvent.click(within(shot).getByRole('button', { name: 'Přesto uložit' }));
    await waitFor(() =>
      expect(api.splitProperty).toHaveBeenLastCalledWith(12664, {
        adverts: [...SALE_ADS, ...RENT_ADS],
        separate: [],
        keep_together: true,
        confirm_retract: true,
      }),
    );
    expect(await within(shot).findByText('Ponecháno jako jedna nemovitost.')).toBeInTheDocument();
  });

  it('disables the card while it runs; a stale card is read again, another refusal printed', async () => {
    let answer: (r: api.SplitResult) => void = () => {};
    vi.mocked(api.splitProperty).mockImplementationOnce(
      () => new Promise<api.SplitResult>((resolve) => (answer = resolve)),
    );
    setup();
    const shot = await screen.findByTestId('category-12664');
    fireEvent.click(within(shot).getByRole('button', { name: 'Ponechat jako jednu nemovitost' }));
    fireEvent.click(within(shot).getByRole('button', { name: 'Potvrdit' }));
    expect(await within(shot).findByRole('button', { name: 'Provádím…' })).toBeDisabled();
    expect(within(shot).getByRole('button', { name: 'Zrušit' })).toBeDisabled();
    answer(KEPT_DONE);
    expect(await within(shot).findByText('Ponecháno jako jedna nemovitost.')).toBeInTheDocument();

    vi.mocked(api.splitProperty).mockRejectedValueOnce(
      new api.ApiError('property 197654 holds adverts the statement did not name: 1976545', 409, {
        detail: { code: 'stale', message: 'property 197654 holds adverts…', ids: [1976545] },
      }),
    );
    const share = card(197654);
    const reads = vi.mocked(api.getCategorySplits).mock.calls.length;
    fireEvent.click(within(share).getByRole('button', { name: 'Ponechat jako jednu nemovitost' }));
    fireEvent.click(within(share).getByRole('button', { name: 'Potvrdit' }));
    expect(
      await within(share).findByText('Karta se mezitím změnila — načteno znovu, nic se nezapsalo.'),
    ).toBeInTheDocument();
    await waitFor(() => expect(vi.mocked(api.getCategorySplits).mock.calls.length).toBeGreaterThan(reads));

    vi.mocked(api.splitProperty).mockRejectedValueOnce(new api.ApiError('HTTP 500', 500, null));
    fireEvent.click(within(card(197654)).getByRole('button', { name: 'Ponechat jako jednu nemovitost' }));
    fireEvent.click(within(card(197654)).getByRole('button', { name: 'Potvrdit' }));
    expect(await within(card(197654)).findByText('Chyba: HTTP 500')).toBeInTheDocument();
  });
});
