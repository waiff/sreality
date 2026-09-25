/* The property page (decision 11): the header is the property as its Browse
 * card shows it, the merged-adverts section is the only advert list, and every
 * old advert address lands here with that advert's row open. Plus the freshness
 * affordance and the broker vizitka, both the canonical advert's.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';

import PropertyDetail, { AdvertRedirect, FreshnessBlock } from './PropertyDetail';
import * as api from '@/lib/api';
import * as auth from '@/lib/auth';
import * as brokers from '@/lib/brokers';
import * as queries from '@/lib/queries';
import { fmtCzk } from '@/lib/format';
import type { PropertySource } from '@/lib/types';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return { ...actual, verifyListingFreshness: vi.fn(), fetchPropertyOrigins: vi.fn() };
});

const verifyMock = vi.mocked(api.verifyListingFreshness);

function renderBlock(qc: QueryClient) {
  return render(
    <QueryClientProvider client={qc}>
      <FreshnessBlock sreality_id={123} checks={[]} />
    </QueryClientProvider>,
  );
}

function makeResult(
  outcome: api.FreshnessOutcome,
  whatChanged: string[] = [],
): api.VerifyFreshnessResult {
  return {
    data: {
      sreality_id: 123,
      outcome,
      verified: outcome !== 'cached',
      cached: outcome === 'cached',
      age_hours: 0,
      what_changed: whatChanged,
      snapshot_id: outcome === 'updated' ? 999 : null,
      current: null,
    },
    metadata: {
      tool: 'verify_listing_freshness',
      filters_used: { sreality_id: 123, max_age_hours: 0 },
      result_count: 1,
      queried_at: '2026-05-28T00:00:00Z',
      data_freshness: '2026-05-28T00:00:00Z',
    },
  };
}

describe('<FreshnessBlock> verify button', () => {
  beforeEach(() => {
    verifyMock.mockReset();
  });

  it('renders the verify button and empty-log copy', () => {
    renderBlock(new QueryClient());
    expect(
      screen.getByRole('button', { name: 'Ověřit aktuálnost' }),
    ).toBeInTheDocument();
    expect(
      screen.getByText('No on-demand freshness checks recorded.'),
    ).toBeInTheDocument();
  });

  it('calls the API, surfaces an "updated" outcome, and invalidates queries', async () => {
    let resolve!: (v: api.VerifyFreshnessResult) => void;
    verifyMock.mockReturnValue(
      new Promise<api.VerifyFreshnessResult>((r) => {
        resolve = r;
      }),
    );

    const qc = new QueryClient();
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries');
    renderBlock(qc);

    fireEvent.click(
      screen.getByRole('button', { name: 'Ověřit aktuálnost' }),
    );

    // The mutation runs async; the call + pending UI land after a tick.
    await waitFor(() => expect(verifyMock).toHaveBeenCalledWith(123));
    expect(
      screen.getByRole('button', { name: 'Ověřuji…' }),
    ).toBeDisabled();
    expect(
      screen.getByText('Re-fetching the listing from the source…'),
    ).toBeInTheDocument();

    resolve(makeResult('updated', ['price_czk']));

    await waitFor(() =>
      expect(
        screen.getByText(/Still listed — updated: price_czk\./),
      ).toBeInTheDocument(),
    );

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['freshness', 123] });
    // FreshnessBlock knows only the sreality_id, so the page's property and
    // snapshot reads are invalidated by their bare prefix.
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['property'] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['snapshots'] });
  });

  it('surfaces a "gone" outcome', async () => {
    verifyMock.mockResolvedValue(makeResult('gone'));
    renderBlock(new QueryClient());

    fireEvent.click(
      screen.getByRole('button', { name: 'Ověřit aktuálnost' }),
    );

    await waitFor(() =>
      expect(
        screen.getByText('No longer listed — marked inactive.'),
      ).toBeInTheDocument(),
    );
  });
});

/* -------------------------------------------------------------------------- */
/* The property page and the old addresses                                    */
/* -------------------------------------------------------------------------- */

vi.mock('@/lib/queries', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/queries')>();
  return {
    ...actual,
    fetchProperty: vi.fn(),
    fetchAdvertProperty: vi.fn(),
    fetchPropertySources: vi.fn(async () => []),
    fetchPropertyStatusEvents: vi.fn(async () => []),
    fetchSnapshotsForListings: vi.fn(async () => []),
    fetchFreshnessChecksByListing: vi.fn(async () => []),
    fetchImagesByListing: vi.fn(async () => []),
    fetchListingsForListingIds: vi.fn(async () => new Map()),
    fetchImagesForListingIds: vi.fn(async () => new Map()),
  };
});
vi.mock('@/lib/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/auth')>()),
  useAuth: vi.fn(() => ({ isAdmin: false })),
}));
/* Only the network wrapper is stubbed — contactState/prettyPhone stay REAL,
   because the vizitka's whole point is the three states they encode. */
vi.mock('@/lib/brokers', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/brokers')>()),
  fetchListingBroker: vi.fn(async () => null),
}));
vi.mock('@/components/NewEstimationModal', () => ({
  useNewEstimationModal: () => ({ open: vi.fn() }),
}));
vi.mock('@/components/ExploreAreaModal', () => ({
  useExploreAreaModal: () => ({ open: vi.fn() }),
}));

/* The property as properties_public answers it: its canonical advert is 105054
   (sreality), and every advert field is that advert's. */
const PROPERTY = {
  id: 105054,
  property_id: 774,
  sreality_id: 999,
  first_seen_at: '2025-11-01T00:00:00Z',
  last_seen_at: '2026-01-02T00:00:00Z',
  is_active: true,
  source: 'sreality',
  source_id_native: '999',
  source_url: null,
  category_main: 'byt',
  category_type: 'prodej',
  price_czk: 5_000_000,
  area_m2: 54,
  disposition: '2+kk',
  display_label: 'Kolbenova, Praha 9',
  tom_days: 62,
  price_change_count: 2,
  total_price_change_pct: -4.5,
} as unknown as queries.PropertyPublic;

const SOURCES: PropertySource[] = [
  {
    property_id: 774,
    id: 105053,
    sreality_id: -11876,
    source: 'idnes',
    source_url: 'https://reality.idnes.cz/detail/x/',
    source_id_native: '6a147cfde222cf687509e018',
    is_active: true,
    price_czk: 5_200_000,
    first_seen_at: '2025-11-01T00:00:00Z',
    last_seen_at: '2026-01-02T00:00:00Z',
  },
  {
    property_id: 774,
    id: 105054,
    sreality_id: 999,
    source: 'sreality',
    source_url: 'https://www.sreality.cz/detail/y',
    source_id_native: '999',
    is_active: true,
    price_czk: 5_000_000,
    first_seen_at: '2025-12-01T00:00:00Z',
    last_seen_at: '2026-01-02T00:00:00Z',
  },
];

function Where() {
  const l = useLocation();
  return <p data-testid="where">{l.pathname + l.search + l.hash}</p>;
}

function renderAt(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="property/:propertyId" element={<PropertyDetail />} />
          <Route path="listing" element={<AdvertRedirect />} />
          <Route path="listing/:source/:nativeId" element={<AdvertRedirect />} />
          <Route path="listing/:sreality_id" element={<AdvertRedirect />} />
        </Routes>
        <Where />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const where = () => screen.getByTestId('where').textContent;
const rowToggle = (portal: string) =>
  within(screen.getAllByText(portal)[0].closest('li') as HTMLElement).getAllByRole('button')[0];

beforeEach(() => {
  vi.mocked(auth.useAuth).mockReturnValue({ isAdmin: false } as ReturnType<typeof auth.useAuth>);
  vi.mocked(queries.fetchProperty).mockReset();
  vi.mocked(queries.fetchProperty).mockResolvedValue(PROPERTY);
  vi.mocked(queries.fetchAdvertProperty).mockReset();
  vi.mocked(queries.fetchPropertySources).mockResolvedValue(SOURCES);
  vi.mocked(queries.fetchImagesByListing).mockClear();
  vi.mocked(queries.fetchSnapshotsForListings).mockClear();
});

describe('<PropertyDetail> one property, one voice', () => {
  it('heads the page with the property as its Browse card shows it — the canonical advert', async () => {
    renderAt('/property/774');

    const h1 = await screen.findByRole('heading', { level: 1 });
    // The canonical advert's price, never the other portal's 5.2M.
    expect(h1.textContent).toContain(fmtCzk(5_000_000));
    expect(screen.getByText('Kolbenova, Praha 9')).toBeInTheDocument();
    expect(queries.fetchProperty).toHaveBeenCalledWith(774);
    // Photos and the price history are the canonical advert's own.
    await waitFor(() => expect(queries.fetchImagesByListing).toHaveBeenCalledWith(105054));
    expect(queries.fetchSnapshotsForListings).toHaveBeenCalledWith([105054]);
    // The price moves are the property's, as Browse filters on them.
    expect(screen.getByText('Price changes').nextSibling).toHaveTextContent('2');
    expect(screen.getByText('Days on market').nextSibling).toHaveTextContent('62');
  });

  it('lists the adverts once, in the merged-adverts section, with no duplicate lists', async () => {
    renderAt('/property/774');

    expect(await screen.findByText('Sloučené inzeráty')).toBeInTheDocument();
    expect(within(screen.getByText('Sreality').closest('li') as HTMLElement).getByText('v záhlaví'))
      .toBeInTheDocument();
    // The chips row, the "current active listing" jump, the history block's URL
    // list and the price-mismatch note are gone: one list, one price.
    expect(screen.queryByText('View the current active listing')).toBeNull();
    expect(screen.queryByText('this listing')).toBeNull();
    expect(screen.queryByText(/Pozn\.:/)).toBeNull();
    expect(screen.getAllByRole('link', { name: /Na portálu/ })).toHaveLength(2);
    // Nothing asked for, nothing opened.
    expect(rowToggle('iDNES Reality')).toHaveAttribute('aria-expanded', 'false');
  });

  it('opens the asked-for advert’s row, but not the one the header already is', async () => {
    renderAt('/property/774?advert=105053');
    await screen.findByText('Sloučené inzeráty');
    expect(rowToggle('iDNES Reality')).toHaveAttribute('aria-expanded', 'true');
    expect(rowToggle('Sreality')).toHaveAttribute('aria-expanded', 'false');
  });

  it('follows a merged-away property to its survivor (admin)', async () => {
    vi.mocked(auth.useAuth).mockReturnValue({ isAdmin: true } as ReturnType<typeof auth.useAuth>);
    vi.mocked(queries.fetchProperty).mockImplementation(async (id) => (id === 774 ? PROPERTY : null));
    vi.mocked(api.fetchPropertyOrigins).mockResolvedValue({ property_id: 774, adverts: [] });

    renderAt('/property/500?advert=105053');

    await waitFor(() => expect(where()).toBe('/property/774?advert=105053'));
    expect(api.fetchPropertyOrigins).toHaveBeenCalledWith(500);
  });
});

describe('<AdvertRedirect> old advert addresses', () => {
  it('lands the natural-key address on the property page with that advert’s row open', async () => {
    vi.mocked(queries.fetchAdvertProperty).mockResolvedValue({ id: 105053, property_id: 774 });

    renderAt('/listing/idnes/6a147cfde222cf687509e018');

    await waitFor(() => expect(where()).toBe('/property/774?advert=105053'));
    expect(queries.fetchAdvertProperty).toHaveBeenCalledWith({
      source: 'idnes',
      nativeId: '6a147cfde222cf687509e018',
    });
    await screen.findByText('Sloučené inzeráty');
    expect(rowToggle('iDNES Reality')).toHaveAttribute('aria-expanded', 'true');
  });

  it('keeps ?run= and the hash through the legacy numeric address', async () => {
    vi.mocked(queries.fetchAdvertProperty).mockResolvedValue({ id: 105053, property_id: 774 });

    renderAt('/listing/-11876?run=9#estimations');

    await waitFor(() => expect(where()).toBe('/property/774?run=9&advert=105053#estimations'));
    expect(queries.fetchAdvertProperty).toHaveBeenCalledWith({ srealityId: -11876 });
  });

  it('sends the old ?property= form straight to the property, with no read', async () => {
    renderAt('/listing?property=774');

    await waitFor(() => expect(where()).toBe('/property/774'));
    expect(queries.fetchAdvertProperty).not.toHaveBeenCalled();
  });

  it('says not found for an advert nobody has', async () => {
    vi.mocked(queries.fetchAdvertProperty).mockResolvedValue(null);
    renderAt('/listing/bazos/nope');
    expect(await screen.findByText('Not found')).toBeInTheDocument();
  });
});

/* -------------------------------------------------------------------------- */
/* Broker vizitka (C2) — the header chip's tri-state fetch behaviour, plus the */
/* per-field 3-state contact rendering the chip never had                     */
/* -------------------------------------------------------------------------- */

/* One row now: /brokers/by-listing carries identity AND contact (migration 419).
   Which contact half arrives (primary_* vs has_*) is still a property of the
   CALLER — admin vs not — so each test picks one. broker_id (7) and listing_id
   (105054) are deliberately different values; that difference is what pins the
   link below to the BROKER dossier rather than a listing-id-shaped route. */
const attribution = (
  contact: Partial<brokers.BrokerContactFields> = {},
): brokers.ListingBroker => ({
  sreality_id: 999,
  listing_id: 105054,
  broker_id: 7,
  broker_display_name: 'Jan Novák',
  broker_firm_label: 'RE/MAX Alfa',
  ...contact,
});

describe('<BrokerVizitka>', () => {
  beforeEach(() => {
    vi.mocked(brokers.fetchListingBroker).mockReset();
    vi.mocked(brokers.fetchListingBroker).mockResolvedValue(attribution());
  });

  const renderListing = () => renderAt('/property/774');

  function withContact(contact: Partial<brokers.BrokerContactFields>) {
    vi.mocked(brokers.fetchListingBroker).mockResolvedValue(attribution(contact));
  }

  it('shows the real contact for an admin session, keyed on the attributed broker', async () => {
    withContact({ primary_phone: '420777123456', primary_email: 'jan@remax.cz' });

    renderListing();

    expect(await screen.findByText('+420 777 123 456')).toBeInTheDocument();
    expect(screen.getByText('jan@remax.cz')).toBeInTheDocument();
    expect(screen.getByText('Jan Novák')).toBeInTheDocument();
    expect(screen.getByText('RE/MAX Alfa')).toBeInTheDocument();
    // W6: identity AND contact come from /brokers/by-listing. One call is the
    // assertion — a second broker read reappearing here is the regression.
    // The canonical advert's broker; the other adverts' are read on their rows.
    expect(brokers.fetchListingBroker).toHaveBeenCalledWith(105054);
    expect(brokers.fetchListingBroker).toHaveBeenCalledTimes(1);
    // ATTRIBUTION deliberately gives broker_id (7) and listing_id (105054)
    // different values — pins the link to the BROKER dossier, not a
    // listing-id-shaped route that would 404 on every click.
    expect(screen.getByRole('link', { name: /Jan Novák/ })).toHaveAttribute(
      'href',
      '/brokers/7',
    );
  });

  /* W6 deleted the two states that only existed because contact arrived on a
     SECOND, later request: "Načítám kontakt…" (identity painted, contact still in
     flight) and "Kontakt není k dispozici" (that read succeeded but held no row
     for this broker). Neither is reachable now — holding the broker row IS
     holding the answer — so the card paints complete in one pass. This pins the
     absence: if a chained contact read ever comes back, so will the reflow. */
  it('paints the contact in the same pass as the identity, with no interim state', async () => {
    withContact({ primary_phone: '420777123456', primary_email: 'jan@remax.cz' });

    renderListing();

    expect(await screen.findByText('Jan Novák')).toBeInTheDocument();
    expect(screen.getByText('+420 777 123 456')).toBeInTheDocument();
    expect(screen.queryByText('Načítám kontakt…')).toBeNull();
    expect(screen.queryByText('Kontakt není k dispozici')).toBeNull();
    expect(screen.queryByText('Kontakt se nepodařilo načíst')).toBeNull();
  });

  /* A non-admin gets has_* instead of the values; an em-dash there would claim
     the broker is unreachable. Same three states as /brokers/:id, same copy. */
  it('says a masked contact is on file rather than showing the empty dash', async () => {
    withContact({ has_phone: true, has_email: true });

    renderListing();

    expect(
      await screen.findByText(/telefon · kontakt na vyžádání/),
    ).toBeInTheDocument();
    expect(screen.getByText(/e-mail · kontakt na vyžádání/)).toBeInTheDocument();
  });

  it('keeps the plain dash when the broker genuinely has no contact', async () => {
    withContact({ has_phone: false, has_email: false });

    renderListing();

    expect(await screen.findByText(/telefon —/)).toBeInTheDocument();
    expect(screen.getByText(/e-mail —/)).toBeInTheDocument();
    expect(screen.queryByText(/na vyžádání/)).not.toBeInTheDocument();
  });

  it('renders nothing at all for a genuinely unattributed listing', async () => {
    vi.mocked(brokers.fetchListingBroker).mockResolvedValue(null);

    renderListing();

    await waitFor(() => expect(brokers.fetchListingBroker).toHaveBeenCalled());
    expect(screen.queryByText('Makléř')).toBeNull();
    expect(screen.queryByText('Makléře se nepodařilo načíst')).toBeNull();
  });

  /* fetchListingBroker returns null ONLY for the two 404 bodies that mean "nothing
     is attributed here"; every other error rethrows. Rendering both as an absent
     card asserted "no broker" for every outage — the dark state that hid the
     PostgREST revocation on this surface for a month. */
  it('says so when the attribution read fails instead of looking unattributed', async () => {
    vi.mocked(brokers.fetchListingBroker).mockRejectedValue(
      new api.ApiError('Invalid token', 401, null),
    );

    renderListing();

    expect(
      await screen.findByText('Makléře se nepodařilo načíst'),
    ).toBeInTheDocument();
  });

  /* The distinction that used to need its own read: a broker we could not fetch
     must never render as a broker with no reachable channel. With one read there
     is no half-loaded card left to get this wrong — a failure takes the whole
     block to the error line, and an em-dash is only ever drawn from a row we
     actually hold. */
  it('never draws an empty channel for a broker it failed to read', async () => {
    vi.mocked(brokers.fetchListingBroker).mockRejectedValue(new Error('HTTP 500'));

    renderListing();

    expect(
      await screen.findByText('Makléře se nepodařilo načíst'),
    ).toBeInTheDocument();
    expect(screen.queryByText(/telefon —/)).not.toBeInTheDocument();
    expect(screen.queryByText(/e-mail —/)).not.toBeInTheDocument();
  });
});
