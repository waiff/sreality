/* The merged-adverts section: the property page's only list of adverts — one
 * row per advert, photos and the portal link collapsed, words on expand, the
 * asked-for advert's row open, and for an admin session each advert's origin
 * and a split letter per advert: the letters state the whole partition as ONE
 * split statement over every advert shown — every letter group but the one
 * keeping the record (most own adverts, else the header's) one `separate` unit,
 * `keep_together: false`. */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, useLocation } from 'react-router-dom';

import MergedAdvertsSection, { COLLAPSED_THUMBS } from './MergedAdvertsSection';
import * as api from '@/lib/api';
import * as auth from '@/lib/auth';
import * as brokers from '@/lib/brokers';
import { fmtCzk } from '@/lib/format';
import * as queries from '@/lib/queries';
import { stateStays } from '@/lib/mergedAdverts';
import * as toast from '@/lib/toast';
import type { ImagePublic, ListingPublic, PropertySource } from '@/lib/types';

vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  fetchPropertyOrigins: vi.fn(),
  splitProperty: vi.fn(),
}));
vi.mock('@/lib/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/auth')>()),
  useAuth: vi.fn(),
}));
vi.mock('@/lib/brokers', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/brokers')>()),
  fetchListingBroker: vi.fn(),
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
const ownAd = (listing_id: number, detach_outcome = 'last_native'): api.AdvertOrigin => ({
  listing_id,
  origin_property_id: null,
  merge_source: null,
  merged_at: null,
  detach_outcome,
  splittable: detach_outcome === 'split_native',
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
  detach_outcome: 'detached',
  splittable: true,
  ...over,
});
const originsOf = (...adverts: api.AdvertOrigin[]) => ({ property_id: 42, adverts });

/* 101 is the property's own advert (the header's, grouped at ingest with another
 * own advert); 202 came from #43 by the operator's merge. */
function origins() {
  return originsOf(ownAd(101, 'split_native'), mergedAd(202, 43));
}

/* The split route's answer, unit by unit (nothing moved: `outcome` null). */
const kept = (ids: number[]): api.SplitUnit => ({
  unit: 'A',
  role: 'kept',
  listing_ids: ids,
  property_id: 42,
  moved: [],
  merge_group_id: null,
});
const left = (
  unit: string,
  ids: number[],
  to: number,
  outcome: string | null,
  mergeGroupId: string | null = null,
): api.SplitUnit => ({
  unit,
  role: 'separated',
  listing_ids: ids,
  property_id: to,
  moved: outcome ? ids.map((listing_id) => ({ listing_id, outcome, from: 42, to })) : [],
  merge_group_id: mergeGroupId,
});
function result(...units: api.SplitUnit[]): api.SplitResult {
  const moved = units.reduce((n, u) => n + u.moved.length, 0);
  return {
    call_id: '5b0c0000-0000-4000-8000-000000000000',
    property_id: 42,
    record_kept_by: 'A',
    units,
    moved,
    rulings: { written: moved ? 1 : 0, same: 0, different: moved ? 1 : 0, must_not_link_written: 0, must_not_link_retracted: 0 },
    reversed_pairs: [],
    undo: null,
  };
}
const refusal = (code: string, ids: unknown[], message: string) =>
  new api.ApiError(message, 409, { detail: { code, message, ids } });

function Where() {
  return <p data-testid="where">{useLocation().pathname}</p>;
}

function setup({
  sources = SOURCES,
  openAdvertId = null,
}: { sources?: PropertySource[]; openAdvertId?: number | null } = {}) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const tree = (list: PropertySource[]) => (
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <MergedAdvertsSection
          propertyId={42}
          canonicalListingId={101}
          sources={list}
          openAdvertId={openAdvertId}
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
  vi.mocked(brokers.fetchListingBroker).mockResolvedValue({
    listing_id: 202,
    sreality_id: null,
    broker_id: 7,
    broker_display_name: 'Jana Nováková',
    broker_firm_label: 'RE/MAX Alfa',
  } as unknown as Awaited<ReturnType<typeof brokers.fetchListingBroker>>);
  vi.mocked(api.fetchPropertyOrigins).mockResolvedValue(origins());
  vi.mocked(api.splitProperty).mockResolvedValue(result(kept([101]), left('B', [202], 43, 'detached')));
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
    expect(within(sreality).getByText('v záhlaví')).toBeInTheDocument();
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
    expect(brokers.fetchListingBroker).not.toHaveBeenCalled();
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
    expect(brokers.fetchListingBroker).toHaveBeenCalledWith(202);
    // The carousel pages the whole album (2 photos → a counter).
    expect(within(idnes).getByText('1 / 2')).toBeInTheDocument();
    // Where it came from, as information.
    expect(
      await within(idnes).findByText('nemovitost #43 · ruční sloučení ze dne 21/09/2026'),
    ).toBeInTheDocument();
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
const STALE =
  'Nemovitost se mezitím změnila — načteno znovu, nic se nezapsalo. Zkontrolujte písmena a rozdělte znovu.';

/* Gives each named advert its letter, once the origins have been read. */
async function assign(letters: Record<number, string>) {
  for (const [id, letter] of Object.entries(letters)) {
    fireEvent.change(await screen.findByLabelText(LABEL[Number(id)]), { target: { value: letter } });
  }
}

const panel = () => screen.getByRole('group', { name: 'Rozdělení nemovitosti' });
const noPanel = () => expect(screen.queryByRole('group', { name: 'Rozdělení nemovitosti' })).toBeNull();
const planLines = () => within(panel()).getAllByRole('listitem').map((li) => li.textContent);
const reasonBox = () => within(panel()).getByRole('textbox', { name: 'Důvod rozdělení (nepovinné)' });
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
    vi.mocked(api.fetchPropertyOrigins).mockResolvedValue(originsOf(ownAd(101, 'not_merged')));
    setup({ sources: SOURCES.slice(0, 1) });
    fireEvent.click(within(rowOf('Sreality')).getAllByRole('button')[0]);
    expect(await screen.findByText('tato nemovitost (nepřišel sloučením)')).toBeInTheDocument();
    expect(screen.queryByRole('combobox')).toBeNull();
    expect(screen.queryByText('vlastní inzerát')).toBeNull();
    expect(screen.queryByText(/Nelze oddělit/)).toBeNull();
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

  it('starts every advert at A and opens the panel only once a second letter is in use', async () => {
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

    fireEvent.change(idnes, { target: { value: 'B' } });
    expect(planLines()).toEqual([
      `A — zůstává v nemovitosti #42: ${AD[101]}`,
      `B — odejde jako jedna nemovitost: ${AD[202]}`,
    ]);
    expect(panel().textContent).toContain(
      'každá dvojice inzerátů napříč písmeny se uloží jako „různé“ a dostane trvalý zákaz spojení; inzeráty se stejným písmenem zůstanou spolu jako jedna nemovitost.',
    );
    expect(panel().textContent).toContain(stateStays(42));
    expect(api.splitProperty).not.toHaveBeenCalled();

    fireEvent.change(idnes, { target: { value: 'A' } });
    noPanel();
  });

  it('four adverts, two flats: the twins leave together as ONE unit, the own advert’s pair stays', async () => {
    vi.mocked(api.fetchPropertyOrigins).mockResolvedValue(FOUR_ORIGINS);
    setup({ sources: FOUR });
    await assign({ 303: 'B', 404: 'B' });
    expect(within(screen.getByLabelText(LABEL[303])).getAllByRole('option')).toHaveLength(4);
    expect(planLines()).toEqual([
      `A — zůstává v nemovitosti #42: ${AD[101]}, ${AD[202]}`,
      `B — odejde jako jedna nemovitost: ${AD[303]}, ${AD[404]}`,
    ]);
    splitNow();
    await waitFor(() =>
      expect(api.splitProperty).toHaveBeenCalledWith(42, {
        adverts: [101, 202, 303, 404],
        separate: [[303, 404]],
        keep_together: false,
      }),
    );
  });

  it('three letters: one unit per leaving letter, in letter order, ids ascending inside', async () => {
    vi.mocked(api.fetchPropertyOrigins).mockResolvedValue(FOUR_ORIGINS);
    // The page's order is not the ids' order.
    setup({ sources: [FOUR[0], FOUR[3], FOUR[1], FOUR[2]] });
    await assign({ 404: 'C', 202: 'B', 303: 'C' });
    expect(planLines()).toEqual([
      `A — zůstává v nemovitosti #42: ${AD[101]}`,
      `B — odejde jako jedna nemovitost: ${AD[202]}`,
      `C — odejde jako jedna nemovitost: ${AD[303]}, ${AD[404]}`,
    ]);
    splitNow();
    await waitFor(() =>
      expect(api.splitProperty).toHaveBeenCalledWith(42, {
        adverts: [101, 404, 202, 303],
        separate: [[202], [303, 404]],
        keep_together: false,
      }),
    );
  });

  it('the letters do not decide who stays: the own advert under B keeps the property, A leaves', async () => {
    vi.mocked(api.fetchPropertyOrigins).mockResolvedValue(FOUR_ORIGINS);
    setup({ sources: FOUR });
    await assign({ 101: 'B', 202: 'B' });
    expect(planLines()).toEqual([
      `A — odejde jako jedna nemovitost: ${AD[303]}, ${AD[404]}`,
      `B — zůstává v nemovitosti #42: ${AD[101]}, ${AD[202]}`,
    ]);
    splitNow();
    await waitFor(() =>
      expect(api.splitProperty).toHaveBeenCalledWith(42, {
        adverts: [101, 202, 303, 404],
        separate: [[303, 404]],
        keep_together: false,
      }),
    );
  });

  it('no own advert at all: the header advert’s group keeps the property', async () => {
    vi.mocked(api.fetchPropertyOrigins).mockResolvedValue(
      originsOf(mergedAd(101, 41), mergedAd(202, 43)),
    );
    setup();
    await assign({ 101: 'B' });
    expect(screen.queryByText('vlastní inzerát')).toBeNull();
    expect(planLines()).toEqual([
      `A — odejde jako jedna nemovitost: ${AD[202]}`,
      `B — zůstává v nemovitosti #42: ${AD[101]}`,
    ]);
    splitNow();
    await waitFor(() =>
      expect(api.splitProperty).toHaveBeenCalledWith(42, {
        adverts: [101, 202],
        separate: [[202]],
        keep_together: false,
      }),
    );
  });

  it('own adverts under two letters: the group holding more of them stays', async () => {
    vi.mocked(api.fetchPropertyOrigins).mockResolvedValue(
      originsOf(
        ownAd(101, 'split_native'),
        mergedAd(202, 43),
        ownAd(303, 'split_native'),
        ownAd(404, 'split_native'),
      ),
    );
    setup({ sources: FOUR });
    await assign({ 303: 'B', 404: 'B' });
    expect(planLines()).toEqual([
      `A — odejde jako jedna nemovitost: ${AD[101]}, ${AD[202]}`,
      `B — zůstává v nemovitosti #42: ${AD[303]}, ${AD[404]}`,
    ]);
    expect(within(rowOf('Bazoš')).getByText('vlastní inzerát')).toHaveAttribute(
      'title',
      'Nepřišel sloučením — při rozdělení zůstanou v této nemovitosti inzeráty písmena, které má nejvíc vlastních inzerátů.',
    );
    splitNow();
    await waitFor(() =>
      expect(api.splitProperty).toHaveBeenCalledWith(42, {
        adverts: [101, 202, 303, 404],
        separate: [[101, 202]],
        keep_together: false,
      }),
    );
  });

  it('own adverts tied across letters: the earliest of those letters stays', async () => {
    vi.mocked(api.fetchPropertyOrigins).mockResolvedValue(
      originsOf(ownAd(101, 'split_native'), mergedAd(202, 43), ownAd(303, 'split_native'), mergedAd(404, 45)),
    );
    setup({ sources: FOUR });
    await assign({ 101: 'B', 303: 'C' });
    expect(planLines()).toEqual([
      `A — odejde jako jedna nemovitost: ${AD[202]}, ${AD[404]}`,
      `B — zůstává v nemovitosti #42: ${AD[101]}`,
      `C — odejde jako jedna nemovitost: ${AD[303]}`,
    ]);
    splitNow();
    await waitFor(() =>
      expect(api.splitProperty).toHaveBeenCalledWith(42, {
        adverts: [101, 202, 303, 404],
        separate: [[202, 404], [303]],
        keep_together: false,
      }),
    );
  });

  it('the reason travels trimmed', async () => {
    setup();
    await assign({ 202: 'B' });
    fireEvent.change(reasonBox(), { target: { value: '  jiné patro ' } });
    splitNow();
    await waitFor(() =>
      expect(api.splitProperty).toHaveBeenCalledWith(42, {
        adverts: [101, 202],
        separate: [[202]],
        keep_together: false,
        reason: 'jiné patro',
      }),
    );
  });

  it('a blank reason is not sent', async () => {
    setup();
    await assign({ 202: 'B' });
    fireEvent.change(reasonBox(), { target: { value: '   ' } });
    splitNow();
    await waitFor(() => expect(api.splitProperty).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.splitProperty).mock.calls[0][1]).not.toHaveProperty('reason');
  });

  it('in flight: the button says so and keeps focus, and a second click sends nothing', async () => {
    let finish: (r: api.SplitResult) => void = () => {};
    vi.mocked(api.splitProperty).mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    setup();
    await assign({ 202: 'B' });
    splitNow();
    const busy = await within(panel()).findByRole('button', { name: 'Probíhá…' });
    expect(busy).toHaveAttribute('aria-busy', 'true');
    expect(busy).not.toBeDisabled();
    expect(screen.getByLabelText(LABEL[202])).toBeDisabled();
    fireEvent.click(busy);
    expect(api.splitProperty).toHaveBeenCalledTimes(1);
    await act(async () => finish(result(kept([101]), left('B', [202], 43, 'detached'))));
    await waitFor(noPanel);
  });

  it('a split that would take back an earlier „různé“ asks first, then re-sends the same statement with confirm_retract', async () => {
    vi.mocked(api.fetchPropertyOrigins).mockResolvedValue(FOUR_ORIGINS);
    vi.mocked(api.splitProperty)
      .mockRejectedValueOnce(
        refusal(
          'reverses_rulings',
          [[303, 404]],
          'this split takes back your earlier ruling on 1 pair(s) (303-404) and drops their permanent must-not-link — re-send with confirm_retract to go ahead',
        ),
      )
      .mockResolvedValueOnce(result(kept([101, 202]), left('B', [303, 404], 44, 'detached', 'g-join')));
    setup({ sources: FOUR });
    await assign({ 303: 'B', 404: 'B' });
    fireEvent.change(reasonBox(), { target: { value: 'dva byty' } });
    splitNow();
    expect(await within(panel()).findByRole('alert')).toHaveTextContent(
      'Nic se nezapsalo: tím byste vzali zpět své dřívější rozhodnutí „různé“ u #303 × #404 a zrušili jejich trvalý zákaz spojení.',
    );
    const statement = {
      adverts: [101, 202, 303, 404],
      separate: [[303, 404]],
      keep_together: false,
      reason: 'dva byty',
    };
    expect(api.splitProperty).toHaveBeenLastCalledWith(42, statement);

    fireEvent.click(within(panel()).getByRole('button', { name: 'Přesto rozdělit' }));
    await waitFor(() =>
      expect(api.splitProperty).toHaveBeenLastCalledWith(42, { ...statement, confirm_retract: true }),
    );
    expect(api.splitProperty).toHaveBeenCalledTimes(2);
  });

  it('a letter moved after that refusal is a new statement: no confirm_retract until it is refused again', async () => {
    vi.mocked(api.fetchPropertyOrigins).mockResolvedValue(FOUR_ORIGINS);
    vi.mocked(api.splitProperty).mockRejectedValueOnce(
      refusal('reverses_rulings', [[303, 404]], 'this split takes back your earlier ruling'),
    );
    setup({ sources: FOUR });
    await assign({ 303: 'B', 404: 'B' });
    splitNow();
    expect(await within(panel()).findByRole('button', { name: 'Přesto rozdělit' })).toBeInTheDocument();
    await assign({ 202: 'C' });
    expect(within(panel()).queryByRole('alert')).toBeNull();
    splitNow();
    await waitFor(() =>
      expect(api.splitProperty).toHaveBeenLastCalledWith(42, {
        adverts: [101, 202, 303, 404],
        separate: [[303, 404], [202]],
        keep_together: false,
      }),
    );
  });

  it('a refusal holds only for the statement it refused: once the origins say another group stays, no confirm_retract', async () => {
    vi.mocked(api.fetchPropertyOrigins).mockResolvedValue(FOUR_ORIGINS);
    vi.mocked(api.splitProperty).mockRejectedValueOnce(
      refusal('reverses_rulings', [[303, 404]], 'this split takes back your earlier ruling'),
    );
    const { qc } = setup({ sources: FOUR });
    await assign({ 303: 'B', 404: 'B' });
    splitNow();
    expect(await within(panel()).findByRole('button', { name: 'Přesto rozdělit' })).toBeInTheDocument();

    // A re-read finds 303 and 404 the property's own: B stays now, A would leave.
    vi.mocked(api.fetchPropertyOrigins).mockResolvedValue(
      originsOf(
        ownAd(101, 'split_native'),
        mergedAd(202, 43),
        ownAd(303, 'split_native'),
        ownAd(404, 'split_native'),
      ),
    );
    await act(() => qc.invalidateQueries({ queryKey: ['merged-adverts', 'origins', 42] }));
    await waitFor(() =>
      expect(planLines()).toEqual([
        `A — odejde jako jedna nemovitost: ${AD[101]}, ${AD[202]}`,
        `B — zůstává v nemovitosti #42: ${AD[303]}, ${AD[404]}`,
      ]),
    );
    expect(within(panel()).queryByRole('button', { name: 'Přesto rozdělit' })).toBeNull();
    splitNow();
    await waitFor(() =>
      expect(api.splitProperty).toHaveBeenLastCalledWith(42, {
        adverts: [101, 202, 303, 404],
        separate: [[101, 202]],
        keep_together: false,
      }),
    );
  });

  it('a property that changed since the page read it: the panel says so, the page re-reads, the letters start again', async () => {
    vi.mocked(api.splitProperty).mockRejectedValue(
      refusal('stale', [505], 'property 42 holds adverts the statement did not name: 505'),
    );
    const { qc, rerender } = setup();
    const invalidate = vi.spyOn(qc, 'invalidateQueries');
    await assign({ 202: 'B' });
    splitNow();
    expect(await within(panel()).findByRole('alert')).toHaveTextContent(STALE);
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['property'] }));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['property-sources'] });
    // Nothing looks done: the panel stays, and no toast speaks of a split.
    expect(within(panel()).getByRole('button', { name: 'Rozdělit nemovitost' })).toBeInTheDocument();
    expect(toast.pushToast).not.toHaveBeenCalled();

    // The re-read brings the newcomer: every letter is back at A, the refusal still said.
    rerender([...SOURCES, FIVE[4]]);
    expect(await screen.findByLabelText(LABEL[505])).toHaveValue('A');
    expect(screen.getByLabelText(LABEL[202])).toHaveValue('A');
    noPanel();
    expect(screen.getByRole('alert')).toHaveTextContent(STALE);
    await assign({ 202: 'B' });
    expect(within(panel()).queryByRole('alert')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('an advert that cannot leave: the panel says which and why, and the origins are re-read', async () => {
    vi.mocked(api.fetchPropertyOrigins).mockResolvedValue(FOUR_ORIGINS);
    vi.mocked(api.splitProperty).mockRejectedValue(
      refusal(
        'cannot_move',
        [
          { listing_id: 303, outcome: 'origin_moved_on' },
          { listing_id: 404, outcome: 'shared_origin' },
        ],
        'an advert cannot leave the property',
      ),
    );
    setup({ sources: FOUR });
    await assign({ 303: 'B', 404: 'C' });
    splitNow();
    const alert = await within(panel()).findByRole('alert');
    expect(alert).toHaveTextContent('Nic se nezapsalo — tyto inzeráty nemohou odejít:');
    expect(within(alert).getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      'Bazoš #303: nemovitost, ze které přišel, byla mezitím sloučena jinam; nejdřív rozdělte tam',
      'Českéreality #404: přišel ze stejné nemovitosti jako inzerát s jiným písmenem a vrátily by se do ní spolu',
    ]);
    await waitFor(() => expect(api.fetchPropertyOrigins).toHaveBeenCalledTimes(2));
    expect(within(panel()).getByRole('button', { name: 'Rozdělit nemovitost' })).toBeInTheDocument();
  });

  it('any other refusal is said as the server said it', async () => {
    vi.mocked(api.splitProperty).mockRejectedValue(
      refusal('busy', [42], 'the property is being changed right now; try again'),
    );
    setup();
    await assign({ 202: 'B' });
    splitNow();
    expect(await within(panel()).findByRole('alert')).toHaveTextContent(
      'Chyba: the property is being changed right now; try again',
    );
  });

  it('done: a toast per unit that left — a new record, a joined pair, a return — a re-read, every letter back at A', async () => {
    vi.mocked(api.fetchPropertyOrigins).mockResolvedValue(
      originsOf(
        ownAd(101, 'split_native'),
        ownAd(202, 'split_native'),
        mergedAd(303, 44),
        mergedAd(404, 45),
        mergedAd(505, 46),
      ),
    );
    vi.mocked(api.splitProperty).mockResolvedValue(
      result(
        kept([101]),
        left('B', [202], 9001, 'split_native'),
        left('C', [303, 404], 44, 'detached', 'g-join'),
        left('D', [505], 46, 'detached'),
      ),
    );
    const { qc } = setup({ sources: FIVE });
    const invalidate = vi.spyOn(qc, 'invalidateQueries');
    await assign({ 202: 'B', 303: 'C', 404: 'C', 505: 'D' });
    // One own advert under A, one under B: the earlier letter keeps the property.
    expect(planLines()[0]).toBe(`A — zůstává v nemovitosti #42: ${AD[101]}`);
    splitNow();
    await waitFor(() =>
      expect(api.splitProperty).toHaveBeenCalledWith(42, {
        adverts: [101, 202, 303, 404, 505],
        separate: [[202], [303, 404], [505]],
        keep_together: false,
      }),
    );
    await waitFor(() => expect(toast.pushToast).toHaveBeenCalledTimes(3));
    expect(
      vi.mocked(toast.pushToast).mock.calls.map(([kind, text, ttl, action]) => [kind, text, ttl, action?.label]),
    ).toEqual([
      ['ok', 'Odděleno — 1 inzerát: nová nemovitost #9001.', 0, 'Otevřít #9001'],
      ['ok', 'Odděleno — 2 inzeráty: sloučeno do #44.', 0, 'Otevřít #44'],
      ['ok', 'Odděleno — 1 inzerát: vráceno do #46.', 0, 'Otevřít #46'],
    ]);
    // Read-your-writes: the property and its advert list, and every Browse surface.
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['property'] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['property-sources'] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['cards'] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['browse-count'] });
    for (const id of [101, 202, 303, 404, 505]) expect(screen.getByLabelText(LABEL[id])).toHaveValue('A');
    noPanel();
    // A toast's link opens where its unit landed.
    act(() => vi.mocked(toast.pushToast).mock.calls[0][3]!.onClick());
    expect(screen.getByTestId('where')).toHaveTextContent('/property/9001');
  });

  it('a split that moved nothing (a re-send) says so, and still re-reads', async () => {
    vi.mocked(api.splitProperty).mockResolvedValue(result(kept([101]), left('B', [202], 43, null)));
    const { qc } = setup();
    const invalidate = vi.spyOn(qc, 'invalidateQueries');
    await assign({ 202: 'B' });
    splitNow();
    await waitFor(() =>
      expect(toast.pushToast).toHaveBeenCalledWith('info', 'Nic se nepřesunulo — inzeráty už jsou odděleny.'),
    );
    expect(toast.pushToast).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['property'] }));
  });

  it('Zrušit puts every letter back at A and clears the reason and the refusal', async () => {
    vi.mocked(api.splitProperty).mockRejectedValue(
      refusal('busy', [42], 'the property is being changed right now; try again'),
    );
    setup();
    await assign({ 202: 'B' });
    fireEvent.change(reasonBox(), { target: { value: 'jiné patro' } });
    splitNow();
    expect(await within(panel()).findByRole('alert')).toBeInTheDocument();

    fireEvent.click(within(panel()).getByRole('button', { name: 'Zrušit' }));
    noPanel();
    expect(screen.getByLabelText(LABEL[202])).toHaveValue('A');
    expect(screen.queryByRole('alert')).toBeNull();
    await assign({ 202: 'B' });
    expect(reasonBox()).toHaveValue('');
    expect(within(panel()).queryByRole('alert')).toBeNull();
    expect(api.splitProperty).toHaveBeenCalledTimes(1);
  });

  it('says why an advert cannot leave — but not of the last own advert, whose letter keeps it here', async () => {
    vi.mocked(api.fetchPropertyOrigins).mockResolvedValue(
      originsOf(
        ownAd(101, 'last_native'),
        mergedAd(202, 43, { detach_outcome: 'origin_moved_on', splittable: false }),
      ),
    );
    setup();
    const moved = rowOf('iDNES Reality');
    expect(
      await within(moved).findByText(/Nelze oddělit: nemovitost, ze které přišel/),
    ).toBeInTheDocument();
    expect(within(moved).getByRole('link', { name: 'kam odešla #43' })).toHaveAttribute(
      'href',
      '/property/43',
    );
    const own = rowOf('Sreality');
    expect(within(own).queryByText(/Nelze oddělit/)).toBeNull();
    expect(within(own).getByText('vlastní inzerát')).toBeInTheDocument();
    // Both still carry a letter: an advert that cannot leave can still stay with the record.
    expect(screen.getByLabelText(LABEL[101])).toBeInTheDocument();
    expect(screen.getByLabelText(LABEL[202])).toBeInTheDocument();
  });
});
