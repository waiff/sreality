/* The merged-adverts section: the property page's only list of adverts — one
 * row per advert, photos and the portal link collapsed, words on expand, the
 * asked-for advert's row open, and for an admin session each advert's origin
 * and a split letter per advert (or the letters a link brought): two letters in
 * use open the one split dialog over every advert shown (its own behaviour:
 * components/autodedup/SplitPanel.test.tsx). */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, useLocation } from 'react-router-dom';

import MergedAdvertsSection, { COLLAPSED_THUMBS } from './MergedAdvertsSection';
import * as api from '@/lib/api';
import * as auth from '@/lib/auth';
import * as brokers from '@/lib/brokers';
import { fmtCzk } from '@/lib/format';
import * as queries from '@/lib/queries';
import * as toast from '@/lib/toast';
import type { ImagePublic, ListingPublic, PropertySource } from '@/lib/types';

vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  fetchPropertyOrigins: vi.fn(),
  getSplitPreview: vi.fn(),
  splitProperty: vi.fn(),
}));
vi.mock('@/lib/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/auth')>()),
  useAuth: vi.fn(),
}));
vi.mock('@/lib/brokers', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/brokers')>()),
  fetchListingBrokersByIds: vi.fn(),
}));
vi.mock('@/lib/queries', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/queries')>()),
  fetchListingsForListingIds: vi.fn(),
  fetchImagesForListingIds: vi.fn(),
}));
vi.mock('@/lib/toast', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/toast')>()),
  pushToast: vi.fn(() => 1),
}));

const SOURCES: PropertySource[] = [
  {
    property_id: 42,
    id: 101,
    sreality_id: 555,
    source: 'sreality',
    source_url: 'https://www.sreality.cz/detail/prodej/byt/2+kk/praha/555',
    source_id_native: '555',
    is_active: true,
    price_czk: 5_000_000,
    first_seen_at: '2026-01-05T08:00:00Z',
    last_seen_at: '2026-09-20T08:00:00Z',
  },
  {
    property_id: 42,
    id: 202,
    sreality_id: -77,
    source: 'idnes',
    source_url: 'https://reality.idnes.cz/detail/prodej/byt/praha/abc123/',
    source_id_native: 'abc123',
    is_active: false,
    price_czk: 5_200_000,
    first_seen_at: '2025-11-02T08:00:00Z',
    last_seen_at: '2026-02-14T08:00:00Z',
  },
];

function listing(id: number, over: Partial<ListingPublic>): ListingPublic {
  return {
    id,
    category_main: 'byt',
    category_type: 'prodej',
    area_m2: 54,
    disposition: '2+kk',
    floor: 3,
    total_floors: 6,
    description: null,
    ...over,
  } as unknown as ListingPublic;
}

function images(listingId: number, n: number): ImagePublic[] {
  return Array.from({ length: n }, (_, i) => ({
    id: listingId * 100 + i,
    sreality_id: listingId,
    sequence: i,
    sreality_url: `https://cdn.example/${listingId}/${i}.jpg`,
    storage_path: null,
    clip_fine_tag: null,
    clip_logical_tag: null,
    clip_confidence: null,
    clip_render_score: null,
    phash: null,
  }));
}

/* Origins: an advert of the property's own (no merge brought it) and one a merge
 * brought from `from`. */
const ownAd = (listing_id: number): api.AdvertOrigin => ({
  listing_id,
  origin_property_id: null,
  merge_source: null,
  merged_at: null,
});
const mergedAd = (
  listing_id: number,
  from: number,
  over: Partial<api.AdvertOrigin> = {},
): api.AdvertOrigin => ({
  listing_id,
  origin_property_id: from,
  merge_source: 'operator',
  merged_at: '2026-09-21T09:00:00Z',
  ...over,
});
const originsOf = (...adverts: api.AdvertOrigin[]) => ({ property_id: 42, adverts });

/* 101 is the property's own advert (the header's); 202 came from #43 by the
 * operator's merge. */
function origins() {
  return originsOf(ownAd(101), mergedAd(202, 43));
}

/* The server's preview of a statement: A stays, every other letter goes back to #43. */
function previewOf(_id: number, letters: string): Promise<api.SplitPreview> {
  const groups = new Map<string, number[]>();
  for (const part of letters.split(',')) {
    const [id, letter] = part.split(':');
    groups.set(letter, [...(groups.get(letter) ?? []), Number(id)]);
  }
  return Promise.resolve({
    property_id: 42,
    letters: [...groups].sort().map(([letter, ids]) => ({
      letter,
      listing_ids: ids,
      lands: letter === 'A' ? 'kept' : 'origin',
      property_id: letter === 'A' ? 42 : 43,
      joins: 0,
      refused: null,
    })),
    curation: [],
    rulings: { different: 1, taken_back: 0, inside: [] },
    plan: `plan:${letters}`,
  });
}

const RESULT: api.SplitResult = {
  property_id: 42,
  call_id: 'c',
  letters: [
    { letter: 'A', listing_ids: [101], property_id: 42, lands: 'kept', joined: null },
    { letter: 'B', listing_ids: [202], property_id: 43, lands: 'origin', joined: null },
  ],
  curation: [],
  rulings: { different: 1, same: 0, taken_back: 0 },
};

function Where() {
  return <p data-testid="where">{useLocation().pathname}</p>;
}

function setup({
  sources = SOURCES,
  openAdvertId = null,
  canonicalListingId = 101,
  initialLetters,
}: {
  sources?: PropertySource[];
  openAdvertId?: number | null;
  canonicalListingId?: number;
  initialLetters?: api.SplitLetters;
} = {}) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const tree = (list: PropertySource[]) => (
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <MergedAdvertsSection
          propertyId={42}
          canonicalListingId={canonicalListingId}
          sources={list}
          openAdvertId={openAdvertId}
          initialLetters={initialLetters}
        />
        <Where />
      </MemoryRouter>
    </QueryClientProvider>
  );
  const view = render(tree(sources));
  /* The page re-read its advert list: same section, new list. */
  const rerender = (list: PropertySource[]) => view.rerender(tree(list));
  return { qc, view, rerender };
}

/* The advert's row — not a line of the split panel, which names portals too. */
function rowOf(portal: string): HTMLElement {
  return screen
    .getAllByText(portal)
    .map((el) => el.closest('li'))
    .find((li) => li && !li.closest('[role="group"]')) as HTMLElement;
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(auth.useAuth).mockReturnValue({ isAdmin: true } as ReturnType<typeof auth.useAuth>);
  vi.mocked(queries.fetchListingsForListingIds).mockResolvedValue(
    new Map([
      [101, listing(101, { description: 'Světlý byt ve 3. patře s balkonem.' })],
      [202, listing(202, { area_m2: 55, description: 'Byt č. 14, orientace na jih.' })],
    ]),
  );
  vi.mocked(queries.fetchImagesForListingIds).mockResolvedValue(
    new Map([
      [101, images(101, 8)],
      [202, images(202, 2)],
    ]),
  );
  vi.mocked(brokers.fetchListingBrokersByIds).mockResolvedValue(
    new Map([
      [202, {
        listing_id: 202,
        sreality_id: null,
        broker_id: 7,
        broker_display_name: 'Jana Nováková',
        broker_firm_label: 'RE/MAX Alfa',
      }],
    ]),
  );
  vi.mocked(api.fetchPropertyOrigins).mockResolvedValue(origins());
  vi.mocked(api.getSplitPreview).mockImplementation(previewOf);
  vi.mocked(api.splitProperty).mockResolvedValue(RESULT);
});

describe('<MergedAdvertsSection> rows', () => {
  it('lists a singleton property’s one advert too — it is the only advert list', () => {
    setup({ sources: SOURCES.slice(0, 1) });
    expect(screen.getByText('Inzerát')).toBeInTheDocument();
    expect(within(rowOf('Sreality')).getByRole('link', { name: 'Na portálu Sreality' })).toHaveAttribute(
      'href',
      SOURCES[0].source_url,
    );
  });

  it('collapsed: portal, price, area + disposition, the seen span, the first photos and the link out — no description', async () => {
    setup();
    expect(screen.getByText('Sloučené inzeráty')).toBeInTheDocument();

    const sreality = rowOf('Sreality');
    /* The primary advert (the header's) is named on its row, and only there. */
    expect(within(sreality).getByText('hlavní inzerát')).toHaveAttribute(
      'title',
      'Záhlaví nemovitosti ukazuje fotky a údaje tohoto inzerátu.',
    );
    expect(screen.getAllByText('hlavní inzerát')).toHaveLength(1);
    expect(within(sreality).getByText('5 000 000 Kč')).toBeInTheDocument();
    expect(await within(sreality).findByText('54 m² · 2+kk')).toBeInTheDocument();
    expect(within(sreality).getByText(/05\/01\/2026 –\s*dosud/)).toBeInTheDocument();

    const idnes = rowOf('iDNES Reality');
    expect(within(idnes).getByText('staženo')).toBeInTheDocument();
    expect(within(idnes).getByText(/02\/11\/2025 –\s*14\/02\/2026/)).toBeInTheDocument();
    // The row's own stored URL — never rebuilt from the category triple.
    expect(within(idnes).getByRole('link', { name: 'Na portálu iDNES Reality' })).toHaveAttribute(
      'href',
      SOURCES[1].source_url,
    );

    // Eight photos: the strip shows the first six and counts the rest.
    const strip = await within(sreality).findByTestId('thumb-strip');
    await waitFor(() =>
      expect(within(strip).getAllByRole('presentation')).toHaveLength(COLLAPSED_THUMBS),
    );
    expect(within(strip).getByText('+2')).toBeInTheDocument();

    expect(screen.queryByText('Byt č. 14, orientace na jih.')).not.toBeInTheDocument();
    // Every advert's broker in ONE batched read for the section, not one per row.
    await waitFor(() => expect(brokers.fetchListingBrokersByIds).toHaveBeenCalledTimes(1));
    expect(brokers.fetchListingBrokersByIds).toHaveBeenCalledWith([101, 202]);
  });

  it('expanded: the description, every photo and the broker — no link to another detail page', async () => {
    setup();
    const idnes = rowOf('iDNES Reality');
    const toggle = within(idnes).getAllByRole('button')[0];
    expect(toggle).toHaveAttribute('aria-expanded', 'false');

    fireEvent.click(toggle);

    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    // MemberText marks the unit tokens, so the text arrives in runs.
    await waitFor(() => expect(idnes.textContent).toContain('Byt č. 14, orientace na jih.'));
    expect(within(idnes).queryByRole('link', { name: 'Otevřít detail' })).toBeNull();
    expect(await within(idnes).findByText('Jana Nováková')).toBeInTheDocument();
    // An advert with no attributed broker says so on its own row.
    const sreality = rowOf('Sreality');
    fireEvent.click(within(sreality).getAllByRole('button')[0]);
    expect(await within(sreality).findByText('Makléř: nepřiřazen')).toBeInTheDocument();
    // The carousel pages the whole album (2 photos → a counter).
    expect(within(idnes).getByText('1 / 2')).toBeInTheDocument();
    // Where it came from, as information.
    expect(
      await within(idnes).findByText('nemovitost #43 · ruční sloučení ze dne 21/09/2026'),
    ).toBeInTheDocument();
  });

  it('says a failed broker read failed, with a retry — never "nepřiřazen"', async () => {
    vi.mocked(brokers.fetchListingBrokersByIds).mockRejectedValueOnce(new Error('HTTP 500'));
    setup({ openAdvertId: 202 });
    const idnes = rowOf('iDNES Reality');
    expect(await within(idnes).findByText('Makléře se nepodařilo načíst')).toBeInTheDocument();
    expect(within(idnes).queryByText('Makléř: nepřiřazen')).toBeNull();
    fireEvent.click(within(idnes).getByRole('button', { name: 'Zkusit znovu' }));
    expect(await within(idnes).findByText('Jana Nováková')).toBeInTheDocument();
  });

  it('opens the row an old advert address asked for, and only that one', async () => {
    setup({ openAdvertId: 202 });
    expect(within(rowOf('iDNES Reality')).getAllByRole('button')[0]).toHaveAttribute(
      'aria-expanded',
      'true',
    );
    const sreality = rowOf('Sreality');
    expect(within(sreality).getAllByRole('button')[0]).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(within(sreality).getAllByRole('button')[0]);
    expect(
      await within(sreality).findByText('tato nemovitost (nepřišel sloučením)'),
    ).toBeInTheDocument();
  });
});

/* Two flats, two adverts each: 101 (the header's, the property's own) and 202 are
 * one; 303 and 404, which merges brought, are the other. */
const FOUR: PropertySource[] = [
  ...SOURCES,
  { ...SOURCES[1], id: 303, sreality_id: -78, source: 'bazos', source_id_native: 'z9', source_url: null },
  {
    ...SOURCES[1],
    id: 404,
    sreality_id: -79,
    source: 'ceskereality',
    source_id_native: 'c4',
    source_url: 'https://www.ceskereality.cz/prodej/byty/c4.html',
  },
];
const FIVE: PropertySource[] = [
  ...FOUR,
  { ...SOURCES[1], id: 505, sreality_id: -80, source: 'bezrealitky', source_id_native: 'b5', source_url: null },
];
const FOUR_ORIGINS = originsOf(
  ownAd(101),
  mergedAd(202, 43),
  mergedAd(303, 44, { merge_source: 'autodedup' }),
  mergedAd(404, 45),
);

const LABEL: Record<number, string> = {
  101: 'Nemovitost inzerátu Sreality #101',
  202: 'Nemovitost inzerátu iDNES Reality #202',
  303: 'Nemovitost inzerátu Bazoš #303',
  404: 'Nemovitost inzerátu Českéreality #404',
  505: 'Nemovitost inzerátu Bezrealitky #505',
};
/* A plan line names an advert as its row does: portal and price. */
const AD: Record<number, string> = {
  101: `Sreality ${fmtCzk(5_000_000)}`,
  202: `iDNES Reality ${fmtCzk(5_200_000)}`,
  303: `Bazoš ${fmtCzk(5_200_000)}`,
  404: `Českéreality ${fmtCzk(5_200_000)}`,
  505: `Bezrealitky ${fmtCzk(5_200_000)}`,
};

/* Gives each named advert its letter, once the origins have been read. */
async function assign(letters: Record<number, string>) {
  for (const [id, letter] of Object.entries(letters)) {
    fireEvent.change(await screen.findByLabelText(LABEL[Number(id)]), { target: { value: letter } });
  }
}

const panel = () => screen.getByRole('group', { name: 'Rozdělení nemovitosti' });
const noPanel = () => expect(screen.queryByRole('group', { name: 'Rozdělení nemovitosti' })).toBeNull();
const planLines = async () => {
  await within(panel()).findByText(/zůstává v nemovitosti/);
  return within(panel())
    .getAllByRole('listitem')
    .map((li) => li.textContent)
    .filter((t) => /^[A-Z] — /.test(t ?? ''));
};
const splitNow = () => fireEvent.click(within(panel()).getByRole('button', { name: 'Rozdělit nemovitost' }));

describe('<MergedAdvertsSection> the split letters', () => {
  it('offers no letter to a session that is not an admin, which never reads the ledger', async () => {
    vi.mocked(auth.useAuth).mockReturnValue({ isAdmin: false } as ReturnType<typeof auth.useAuth>);
    setup();
    expect(await screen.findByText('54 m² · 2+kk')).toBeInTheDocument();
    expect(screen.queryByRole('combobox')).toBeNull();
    expect(screen.queryByText('vlastní inzerát')).toBeNull();
    expect(api.fetchPropertyOrigins).not.toHaveBeenCalled();
  });

  it('offers no letter on a property of one advert', async () => {
    vi.mocked(api.fetchPropertyOrigins).mockResolvedValue(originsOf(ownAd(101)));
    setup({ sources: SOURCES.slice(0, 1) });
    fireEvent.click(within(rowOf('Sreality')).getAllByRole('button')[0]);
    expect(await screen.findByText('tato nemovitost (nepřišel sloučením)')).toBeInTheDocument();
    expect(screen.queryByRole('combobox')).toBeNull();
    expect(screen.queryByText('vlastní inzerát')).toBeNull();
  });

  it('offers no letter while the origins are being read', async () => {
    vi.mocked(api.fetchPropertyOrigins).mockReturnValue(new Promise(() => {}));
    setup();
    expect(await screen.findByText('54 m² · 2+kk')).toBeInTheDocument();
    expect(api.fetchPropertyOrigins).toHaveBeenCalledWith(42);
    expect(screen.queryByRole('combobox')).toBeNull();
  });

  it('offers no letter when the origins could not be read', async () => {
    vi.mocked(api.fetchPropertyOrigins).mockRejectedValue(new Error('ledger down'));
    setup();
    fireEvent.click(within(rowOf('Sreality')).getAllByRole('button')[0]);
    expect(await screen.findByText('nepodařilo se načíst')).toBeInTheDocument();
    expect(screen.queryByRole('combobox')).toBeNull();
  });

  it('starts every advert at A and opens the dialog only once a second letter is in use', async () => {
    setup();
    const idnes = await screen.findByLabelText(LABEL[202]);
    expect(idnes).toHaveValue('A');
    expect(screen.getByLabelText(LABEL[101])).toHaveValue('A');
    // As many letters as adverts.
    expect(within(idnes).getAllByRole('option').map((o) => o.textContent)).toEqual(['A', 'B']);
    expect(
      screen.getByText(/stejné písmeno = jedna nemovitost, různá písmena = různé nemovitosti/),
    ).toBeInTheDocument();
    // The property's own advert says so; one a merge brought does not.
    expect(within(rowOf('Sreality')).getByText('vlastní inzerát')).toHaveAttribute(
      'title',
      'Nepřišel sloučením — inzeráty s jeho písmenem při rozdělení zůstanou v této nemovitosti.',
    );
    expect(within(rowOf('iDNES Reality')).queryByText('vlastní inzerát')).toBeNull();
    noPanel();
    expect(api.getSplitPreview).not.toHaveBeenCalled();

    fireEvent.change(idnes, { target: { value: 'B' } });
    expect(await planLines()).toEqual([
      `A — zůstává v nemovitosti #42: ${AD[101]}`,
      `B — vrátí se do nemovitosti #43: ${AD[202]}`,
    ]);
    expect(api.getSplitPreview).toHaveBeenLastCalledWith(42, '101:A,202:B');
    expect(api.splitProperty).not.toHaveBeenCalled();

    fireEvent.change(idnes, { target: { value: 'A' } });
    noPanel();
  });

  it('the dialog reads every advert shown, each with its letter, and the click sends them all', async () => {
    vi.mocked(api.fetchPropertyOrigins).mockResolvedValue(FOUR_ORIGINS);
    // The page's order is not the ids' order.
    setup({ sources: [FOUR[0], FOUR[3], FOUR[1], FOUR[2]] });
    await assign({ 404: 'C', 202: 'B', 303: 'C' });
    expect(within(screen.getByLabelText(LABEL[303])).getAllByRole('option')).toHaveLength(4);
    expect(await planLines()).toEqual([
      `A — zůstává v nemovitosti #42: ${AD[101]}`,
      `B — vrátí se do nemovitosti #43: ${AD[202]}`,
      `C — vrátí se do nemovitosti #43: ${AD[303]}, ${AD[404]}`,
    ]);
    expect(api.getSplitPreview).toHaveBeenLastCalledWith(42, '101:A,202:B,303:C,404:C');
    splitNow();
    await waitFor(() =>
      expect(api.splitProperty).toHaveBeenCalledWith(42, {
        letters: { 101: 'A', 202: 'B', 303: 'C', 404: 'C' },
        expect: 'plan:101:A,202:B,303:C,404:C',
      }),
    );
  });

  it('opens on the letters a review page’s link brought, the adverts shown only', async () => {
    vi.mocked(api.fetchPropertyOrigins).mockResolvedValue(FOUR_ORIGINS);
    setup({ sources: FOUR, initialLetters: { 303: 'B', 404: 'B', 999: 'C' } });
    expect(await screen.findByLabelText(LABEL[303])).toHaveValue('B');
    expect(screen.getByLabelText(LABEL[101])).toHaveValue('A');
    await waitFor(() => expect(api.getSplitPreview).toHaveBeenCalledWith(42, '101:A,202:A,303:B,404:B'));
    expect(panel()).toBeInTheDocument();
  });

  it('done: every letter back at A and the dialog closed; the receipt is the dialog’s', async () => {
    setup();
    await assign({ 202: 'B' });
    await planLines();
    splitNow();
    await waitFor(noPanel);
    expect(screen.getByLabelText(LABEL[202])).toHaveValue('A');
    expect(toast.pushToast).toHaveBeenCalledTimes(1);
  });

  it('a list that changed (a newcomer, a split elsewhere) starts again at A', async () => {
    const { rerender } = setup();
    await assign({ 202: 'B' });
    await planLines();
    rerender([...SOURCES, FIVE[4]]);
    expect(await screen.findByLabelText(LABEL[505])).toHaveValue('A');
    expect(screen.getByLabelText(LABEL[202])).toHaveValue('A');
    noPanel();
  });

  it('Zrušit puts every letter back at A', async () => {
    setup();
    await assign({ 202: 'B' });
    await planLines();
    fireEvent.click(within(panel()).getByRole('button', { name: 'Zrušit' }));
    noPanel();
    expect(screen.getByLabelText(LABEL[202])).toHaveValue('A');
    expect(api.splitProperty).not.toHaveBeenCalled();
  });

  it('marks own adverts: under two letters, the group holding more of them stays', async () => {
    vi.mocked(api.fetchPropertyOrigins).mockResolvedValue(
      originsOf(ownAd(101), mergedAd(202, 43), ownAd(303), ownAd(404)),
    );
    setup({ sources: FOUR });
    expect(await within(rowOf('Bazoš')).findByText('vlastní inzerát')).toHaveAttribute(
      'title',
      'Nepřišel sloučením — při rozdělení zůstanou v této nemovitosti inzeráty písmena, které má nejvíc vlastních inzerátů.',
    );
  });
});
