/* The merged-adverts section: one row per advert, photos collapsed, words on
 * expand, and the two-step split behind its own switch and an admin session. */

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
import type { ImagePublic, ListingPublic, MergeGroup, PropertySource } from '@/lib/types';

vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  listPropertyMerges: vi.fn(),
  unmergeMergeGroup: vi.fn(),
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
  fetchPropertySources: vi.fn(async () => ({ property_id: 42, sources: [] })),
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

function group(over: Partial<MergeGroup> = {}): MergeGroup {
  return {
    merge_group_id: 'grp-1',
    merged_at: '2026-09-21T09:00:00Z',
    survivor_property_id: 42,
    retired_count: 1,
    listings_moved: 1,
    source: 'auto',
    reason: 'autodedup:test',
    fully_undone: false,
    ...over,
  };
}

function setup({
  sources = SOURCES,
  unmergeEnabled = false,
}: { sources?: PropertySource[]; unmergeEnabled?: boolean } = {}) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const view = render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <MergedAdvertsSection
          propertyId={42}
          currentListingId={101}
          sources={sources}
          unmergeEnabled={unmergeEnabled}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { qc, view };
}

function rowOf(portal: string): HTMLElement {
  return screen.getByText(portal).closest('li') as HTMLElement;
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
});

describe('<MergedAdvertsSection> rows', () => {
  it('renders nothing for a property of one advert', () => {
    const { view } = setup({ sources: SOURCES.slice(0, 1) });
    expect(view.container).toBeEmptyDOMElement();
    expect(queries.fetchListingsForListingIds).not.toHaveBeenCalled();
  });

  it('collapsed: portal, price, area + disposition, the seen span and the first photos — no description', async () => {
    setup();
    expect(screen.getByText('Sloučené inzeráty')).toBeInTheDocument();

    const sreality = rowOf('Sreality');
    expect(within(sreality).getByText('tento inzerát')).toBeInTheDocument();
    expect(within(sreality).getByText('5 000 000 Kč')).toBeInTheDocument();
    expect(await within(sreality).findByText('54 m² · 2+kk')).toBeInTheDocument();
    expect(within(sreality).getByText(/05\/01\/2026 –\s*dosud/)).toBeInTheDocument();

    const idnes = rowOf('iDNES Reality');
    expect(within(idnes).getByText('staženo')).toBeInTheDocument();
    expect(within(idnes).getByText(/02\/11\/2025 –\s*14\/02\/2026/)).toBeInTheDocument();

    // Eight photos: the strip shows the first six and counts the rest.
    const strip = await within(sreality).findByTestId('thumb-strip');
    await waitFor(() =>
      expect(within(strip).getAllByRole('presentation')).toHaveLength(COLLAPSED_THUMBS),
    );
    expect(within(strip).getByText('+2')).toBeInTheDocument();

    expect(screen.queryByText('Byt č. 14, orientace na jih.')).not.toBeInTheDocument();
    expect(brokers.fetchListingBroker).not.toHaveBeenCalled();
  });

  it('expanded: the description, every photo, the stored portal link and the broker', async () => {
    setup();
    const idnes = rowOf('iDNES Reality');
    const toggle = within(idnes).getAllByRole('button')[0];
    expect(toggle).toHaveAttribute('aria-expanded', 'false');

    fireEvent.click(toggle);

    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    // MemberText marks the unit tokens, so the text arrives in runs.
    await waitFor(() => expect(idnes.textContent).toContain('Byt č. 14, orientace na jih.'));
    const portalLink = within(idnes).getByRole('link', { name: /Na portálu iDNES Reality/ });
    // The row's own stored URL — never rebuilt from the category triple.
    expect(portalLink).toHaveAttribute('href', SOURCES[1].source_url);
    expect(within(idnes).getByRole('link', { name: 'Otevřít detail' })).toHaveAttribute(
      'href',
      '/listing/idnes/abc123',
    );
    expect(await within(idnes).findByText('Jana Nováková')).toBeInTheDocument();
    expect(brokers.fetchListingBroker).toHaveBeenCalledWith(202);
    // The carousel pages the whole album (2 photos → a counter).
    expect(within(idnes).getByText('1 / 2')).toBeInTheDocument();
  });

  it('the advert the page is open on is never linked to itself', async () => {
    setup();
    const sreality = rowOf('Sreality');
    fireEvent.click(within(sreality).getAllByRole('button')[0]);
    await within(sreality).findByRole('link', { name: /Na portálu Sreality/ });
    expect(within(sreality).queryByRole('link', { name: 'Otevřít detail' })).toBeNull();
  });
});

describe('<MergedAdvertsSection> Rozdělit', () => {
  it('is absent while its switch is off, whoever is signed in', () => {
    setup({ unmergeEnabled: false });
    expect(screen.queryByRole('button', { name: /Rozdělit/ })).toBeNull();
    expect(auth.useAuth).not.toHaveBeenCalled();
  });

  it('is absent for a session that is not an admin', () => {
    vi.mocked(auth.useAuth).mockReturnValue({ isAdmin: false } as ReturnType<typeof auth.useAuth>);
    setup({ unmergeEnabled: true });
    expect(screen.queryByRole('button', { name: /Rozdělit/ })).toBeNull();
  });

  it('asks twice, then undoes the one merge that joined the pair and refreshes the page and Browse', async () => {
    vi.mocked(api.listPropertyMerges).mockResolvedValue({ data: [group()], total: 1 });
    vi.mocked(api.unmergeMergeGroup).mockResolvedValue({
      data: {
        merge_group_id: 'grp-1',
        survivor_id: 42,
        retired_ids: [43],
        listings_moved_back: 1,
        conflicts: [],
      },
    });
    const { qc } = setup({ unmergeEnabled: true });
    const invalidate = vi.spyOn(qc, 'invalidateQueries');

    // Step one reads the ledger — never on page load.
    expect(api.listPropertyMerges).not.toHaveBeenCalled();
    fireEvent.click(within(rowOf('iDNES Reality')).getByRole('button', { name: /Rozdělit/ }));
    expect(await screen.findByText('Oddělit tento inzerát?')).toBeInTheDocument();
    expect(api.listPropertyMerges).toHaveBeenCalledWith({ limit: 200, offset: 0 });
    expect(api.unmergeMergeGroup).not.toHaveBeenCalled();

    // Step two is the write.
    fireEvent.click(screen.getByRole('button', { name: 'Ano, oddělit' }));
    await waitFor(() => expect(api.unmergeMergeGroup).toHaveBeenCalledWith('grp-1'));
    await waitFor(() =>
      expect(toast.pushToast).toHaveBeenCalledWith(
        'ok',
        'Rozděleno — 1 inzerát zpět v původní nemovitosti.',
      ),
    );
    // Read-your-writes: the page's sources re-resolved from the listing alone,
    // and every Browse surface refetches.
    await waitFor(() => expect(queries.fetchPropertySources).toHaveBeenCalledWith(101));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['listing'] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['cards'] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['browse-count'] });
  });

  it('Zrušit steps back without writing', async () => {
    vi.mocked(api.listPropertyMerges).mockResolvedValue({ data: [group()], total: 1 });
    setup({ unmergeEnabled: true });
    fireEvent.click(within(rowOf('Sreality')).getByRole('button', { name: /Rozdělit/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Zrušit' }));
    expect(screen.queryByText('Oddělit tento inzerát?')).toBeNull();
    expect(within(rowOf('Sreality')).getByRole('button', { name: /Rozdělit/ })).toBeInTheDocument();
    expect(api.unmergeMergeGroup).not.toHaveBeenCalled();
  });

  it('offers no write when the ledger cannot say which merge brought the advert', async () => {
    vi.mocked(api.listPropertyMerges).mockResolvedValue({
      data: [group({ merge_group_id: 'a' }), group({ merge_group_id: 'b' })],
      total: 2,
    });
    const three = [
      ...SOURCES,
      { ...SOURCES[1], id: 303, source: 'bazos', source_id_native: 'z9', source_url: null },
    ];
    vi.mocked(queries.fetchListingsForListingIds).mockResolvedValue(new Map());
    setup({ sources: three, unmergeEnabled: true });

    fireEvent.click(within(rowOf('Bazoš')).getByRole('button', { name: /Rozdělit/ }));
    expect(await screen.findByText(/nejde poznat, které/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Ano/ })).toBeNull();
    expect(screen.getByRole('button', { name: 'Zavřít' })).toBeInTheDocument();
  });

  it('names the whole group when one merge made the property and a row cannot leave alone', async () => {
    vi.mocked(api.listPropertyMerges).mockResolvedValue({
      data: [group({ listings_moved: 2, retired_count: 2, source: 'operator' })],
      total: 1,
    });
    const three = [
      ...SOURCES,
      { ...SOURCES[1], id: 303, source: 'bazos', source_id_native: 'z9', source_url: null },
    ];
    setup({ sources: three, unmergeEnabled: true });

    fireEvent.click(within(rowOf('Bazoš')).getByRole('button', { name: /Rozdělit/ }));
    expect(await screen.findByText('Jeden inzerát samostatně oddělit nejde.')).toBeInTheDocument();
    expect(screen.getByText(/rozpadne zpět na 3 nemovitosti/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Ano, vrátit celé sloučení' })).toBeInTheDocument();
  });
});
