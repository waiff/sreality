/* The merged-adverts section: the property page's only list of adverts — one
 * row per advert, photos and the portal link collapsed, words on expand, the
 * asked-for advert's row open, and for an admin session each advert's origin
 * and the exact two-step per-advert split, any property size, any merge origin. */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import MergedAdvertsSection, { COLLAPSED_THUMBS } from './MergedAdvertsSection';
import * as api from '@/lib/api';
import * as auth from '@/lib/auth';
import * as brokers from '@/lib/brokers';
import * as queries from '@/lib/queries';
import * as toast from '@/lib/toast';
import type { ImagePublic, ListingPublic, PropertySource } from '@/lib/types';

vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  fetchPropertyOrigins: vi.fn(),
  detachListing: vi.fn(),
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

/* 101 is the property's own advert; 202 came from #43 by the operator's merge. */
function origins(extra: api.AdvertOrigin[] = []) {
  return {
    property_id: 42,
    adverts: [
      { listing_id: 101, origin_property_id: null, merge_source: null, merged_at: null },
      {
        listing_id: 202,
        origin_property_id: 43,
        merge_source: 'operator',
        merged_at: '2026-09-21T09:00:00Z',
      },
      ...extra,
    ],
  };
}

function setup({
  sources = SOURCES,
  openAdvertId = null,
}: { sources?: PropertySource[]; openAdvertId?: number | null } = {}) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const view = render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <MergedAdvertsSection
          propertyId={42}
          canonicalListingId={101}
          sources={sources}
          openAdvertId={openAdvertId}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { qc, view };
}

function rowOf(portal: string): HTMLElement {
  return screen.getAllByText(portal)[0].closest('li') as HTMLElement;
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
  vi.mocked(api.detachListing).mockResolvedValue({
    listing_id: 202,
    detached: true,
    outcome: 'detached',
    survivor_property_id: 42,
    restored_property_id: 43,
    rulings_written: 1,
  });
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

describe('<MergedAdvertsSection> Rozdělit', () => {
  it('is absent for a session that is not an admin, which never reads the ledger', () => {
    vi.mocked(auth.useAuth).mockReturnValue({ isAdmin: false } as ReturnType<typeof auth.useAuth>);
    setup();
    expect(screen.queryByRole('button', { name: /Rozdělit/ })).toBeNull();
    expect(api.fetchPropertyOrigins).not.toHaveBeenCalled();
  });

  it('is offered only on an advert with an origin — never on the property’s own', async () => {
    setup();
    expect(
      await within(rowOf('iDNES Reality')).findByRole('button', { name: /Rozdělit/ }),
    ).toBeInTheDocument();
    expect(api.fetchPropertyOrigins).toHaveBeenCalledWith(42);
    expect(within(rowOf('Sreality')).queryByRole('button', { name: /Rozdělit/ })).toBeNull();
  });

  it('asks twice, then detaches that one advert with the typed reason and refreshes', async () => {
    const { qc } = setup();
    const invalidate = vi.spyOn(qc, 'invalidateQueries');

    fireEvent.click(
      await within(rowOf('iDNES Reality')).findByRole('button', { name: /Rozdělit/ }),
    );
    expect(screen.getByText('Oddělit tento inzerát?')).toBeInTheDocument();
    expect(
      screen.getByText(/Vrátí se do nemovitosti #43, odkud ho přivedlo ruční sloučení ze dne 21\/09\/2026/),
    ).toBeInTheDocument();
    expect(api.detachListing).not.toHaveBeenCalled();

    // Step two is the write, with the optional reason trimmed.
    fireEvent.change(screen.getByRole('textbox', { name: /Důvod/ }), {
      target: { value: '  jiné patro ' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Ano, oddělit' }));
    await waitFor(() => expect(api.detachListing).toHaveBeenCalledWith(42, 202, 'jiné patro'));
    await waitFor(() =>
      expect(toast.pushToast).toHaveBeenCalledWith(
        'ok',
        'Odděleno — inzerát je zpět v nemovitosti #43.',
      ),
    );
    // Read-your-writes: the property and its advert list, and every Browse surface.
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['property'] }));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['property-sources'] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['cards'] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['browse-count'] });
  });

  it('Zrušit steps back without writing', async () => {
    setup();
    fireEvent.click(
      await within(rowOf('iDNES Reality')).findByRole('button', { name: /Rozdělit/ }),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Zrušit' }));
    expect(screen.queryByText('Oddělit tento inzerát?')).toBeNull();
    expect(
      within(rowOf('iDNES Reality')).getByRole('button', { name: /Rozdělit/ }),
    ).toBeInTheDocument();
    expect(api.detachListing).not.toHaveBeenCalled();
  });

  it('splits any row of a bigger property, an AUTODEDUP merge like any other; no reason sends none', async () => {
    vi.mocked(api.fetchPropertyOrigins).mockResolvedValue(
      origins([
        {
          listing_id: 303,
          origin_property_id: 44,
          merge_source: 'autodedup',
          merged_at: '2026-09-22T09:00:00Z',
        },
      ]),
    );
    const three = [
      ...SOURCES,
      { ...SOURCES[1], id: 303, source: 'bazos', source_id_native: 'z9', source_url: null },
    ];
    setup({ sources: three });

    fireEvent.click(await within(rowOf('Bazoš')).findByRole('button', { name: /Rozdělit/ }));
    expect(
      screen.getByText(/nemovitosti #44, odkud ho přivedlo automatické \(AUTODEDUP\) sloučení/),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Ano, oddělit' }));
    await waitFor(() => expect(api.detachListing).toHaveBeenCalledWith(42, 303, undefined));
  });

  it('a detach that moved nothing says why, and still refreshes', async () => {
    vi.mocked(api.detachListing).mockResolvedValue({
      listing_id: 202,
      detached: false,
      outcome: 'not_on_property',
      survivor_property_id: 42,
      restored_property_id: 43,
      rulings_written: 0,
    });
    const { qc } = setup();
    const invalidate = vi.spyOn(qc, 'invalidateQueries');
    fireEvent.click(
      await within(rowOf('iDNES Reality')).findByRole('button', { name: /Rozdělit/ }),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Ano, oddělit' }));
    await waitFor(() =>
      expect(toast.pushToast).toHaveBeenCalledWith(
        'info',
        'Nic se nepřesunulo — inzerát už v této nemovitosti není.',
      ),
    );
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['property'] }));
  });
});
