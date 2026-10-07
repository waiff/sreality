/* The category review (E937): the property ids come from the link and are read
 * ten per request; each mixed property is a card of its sides (photos, facts and
 * each ad's own words, a contentless record as one muted line) with a letter per
 * ad, one per side until the operator moves one, and "Rozdělit podle písmen", a
 * link to the property page's split dialog (MS18) with every ad lettered (the
 * riders with the letter that stays). The page itself sends nothing; a property
 * no longer mixed, or confirmed, is a done row. */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import AutodedupCategorySplits from './AutodedupCategorySplits';
import * as api from '@/lib/api';
import * as queries from '@/lib/queries';

vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  getCategorySplits: vi.fn(),
  splitProperty: vi.fn(),
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
  extra: Partial<api.CategorySplitAdvert> = {},
): api.CategorySplitAdvert => ({
  listing_id,
  source,
  is_active: true,
  origin_property_id,
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
      ad(1266401, 'sreality', null, {
        text: { title: 'Prodej bytu 1+kk, 30 m²', description: 'Světlý byt po rekonstrukci, [telefon].' },
      }),
      ...[1266402, 1266403, 1266404, 1266405, 1266406].map((id, i) => ad(id, 'idnes', 50002 + i)),
      ad(1266410, 'bazos', null, {
        empty: true,
        text: { title: 'Byt 1+kk', description: null },
      }),
    ]),
    side('pronajem', ['byt'], false, [
      ad(1266407, 'sreality', null, {
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
      ad(534885, 'realitymix', 53492, { unknown: true }),
    ]),
    side('prodej', ['byt'], false, [ad(534883, 'bazos', 53489)]),
  ],
  [clash(534881, 534883, 'category_main: dum vs byt')],
);

/* 914: nineteen rentals and one contentless record: one category, nothing to decide. */
const ONE_CATEGORY = item(
  914,
  [side('pronajem', ['byt'], true, [ad(91400, 'sreality'), ad(91419, 'bazos', null, { empty: true })])],
  [],
  { mixed: false },
);

/* 197654: two house sales and a share sale; one share ad already sits on its origin. */
const SHARE = item(
  197654,
  [
    side('prodej', ['dum'], true, [ad(1976541, 'sreality'), ad(1976542, 'idnes', 197001)]),
    side('podil', ['dum'], false, [ad(1976543, 'sreality', 197002), ad(1976544, 'idnes', 197654)]),
  ],
  [clash(1976541, 1976543, 'category_type: prodej vs podil')],
);

const ITEMS = [SALE_RENT, CONFIRMED, BLOCKED, ONE_CATEGORY, SHARE];
const LINK = '/autodedup/category-splits?properties=12664,9737,53488,914,197654,5';

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
const splitLink = (c: HTMLElement) => within(c).getByRole('link', { name: 'Rozdělit podle písmen' });
const letters = (c: HTMLElement) =>
  decodeURIComponent(new URL(splitLink(c).getAttribute('href')!, 'http://x').searchParams.get('letters')!);
const lettered = (entries: [number[], string][]) =>
  entries
    .flatMap(([ids, letter]) => ids.map((id) => [id, letter] as const))
    .sort((a, b) => a[0] - b[0])
    .map(([id, letter]) => `${id}:${letter}`)
    .join(',');

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

  it('says what rides along, and offers no keeping', async () => {
    setup();
    const mixed = await screen.findByTestId('category-53488');
    expect(within(mixed).getByRole('region', { name: 'Prodej · dům + komerční prostor' })).toBeInTheDocument();
    expect(
      within(mixed).getByText('kategorie neuvedena — bez písmena, zůstane se skupinou, která zůstává'),
    ).toBeInTheDocument();
    expect(within(mixed).queryByLabelText(LETTER(534885, 'RealityMix'))).toBeNull();
    // the ad of unknown category is counted, and lettered, with the group that stays
    expect(within(mixed).getByText('A zůstává (4) · B odejde (1)')).toBeInTheDocument();
    expect(letters(mixed)).toBe(lettered([[[534881, 534882, 534884, 534885], 'A'], [[534883], 'B']]));
    expect(screen.queryByRole('button', { name: /Ponechat|Potvrdit|Vrátit|Přesto/ })).toBeNull();
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

describe('<AutodedupCategorySplits> the split, on the property page', () => {
  it('untouched letters split by category: every ad lettered, the contentless record with A', async () => {
    setup();
    const shot = await screen.findByTestId('category-12664');
    // one letter per side; the contentless record takes none
    expect(within(shot).getByLabelText(LETTER(1266401, 'Sreality'))).toHaveValue('A');
    expect(within(shot).getByLabelText(LETTER(1266407, 'Sreality'))).toHaveValue('B');
    expect(within(shot).queryByLabelText(LETTER(1266410, 'Bazoš'))).toBeNull();
    expect(within(shot).getByText('A zůstává (7) · B odejde (3)')).toBeInTheDocument();
    expect(splitLink(shot).getAttribute('href')).toMatch(/^\/property\/12664\?letters=/);
    expect(letters(shot)).toBe(lettered([[SALE_ADS, 'A'], [RENT_ADS, 'B']]));
    expect(api.splitProperty).not.toHaveBeenCalled();
  });

  it('a side that bundles two flats: one rental ad to C is a third letter', async () => {
    setup();
    const shot = await screen.findByTestId('category-12664');
    fireEvent.change(within(shot).getByLabelText(LETTER(1266409, 'Bezrealitky')), {
      target: { value: 'C' },
    });
    expect(within(shot).getByText('A zůstává (7) · B odejde (2) · C odejde (1)')).toBeInTheDocument();
    expect(letters(shot)).toBe(
      lettered([[SALE_ADS, 'A'], [[1266407, 1266408], 'B'], [[1266409], 'C']]),
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
    expect(within(shot).queryByRole('link', { name: 'Rozdělit podle písmen' })).toBeNull();
    expect(
      within(shot).getByText('Všechny inzeráty mají stejné písmeno, není co rozdělit.'),
    ).toBeInTheDocument();
  });

  it('the riders go with the group that stays, also when the letters change which group that is', async () => {
    setup();
    const shot = await screen.findByTestId('category-12664');
    // the sale side's own ad joins the rentals: B now holds two own ads and stays
    fireEvent.change(within(shot).getByLabelText(LETTER(1266401, 'Sreality')), {
      target: { value: 'B' },
    });
    expect(within(shot).getByText('A odejde (5) · B zůstává (5)')).toBeInTheDocument();
    expect(letters(shot)).toBe(
      lettered([
        [[1266402, 1266403, 1266404, 1266405, 1266406], 'A'],
        [[1266401, 1266410, ...RENT_ADS], 'B'],
      ]),
    );
  });
});
