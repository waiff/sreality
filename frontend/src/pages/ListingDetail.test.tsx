/* FreshnessBlock — the "Ověřit aktuálnost" (verify freshness) affordance.
 *
 * The true end-to-end path (API write → listing_freshness_checks row →
 * anon read refresh) needs production secrets, so it can't run here.
 * These cases pin the button's client behaviour: it calls the bearer-
 * gated wrapper, shows a pending state, surfaces the outcome, and
 * invalidates the listing / snapshot / freshness queries so the timeline
 * and log refetch.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import ListingDetail, { FreshnessBlock } from './ListingDetail';
import * as api from '@/lib/api';
import * as brokers from '@/lib/brokers';
import * as queries from '@/lib/queries';
import type { ListingPublic } from '@/lib/types';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return { ...actual, verifyListingFreshness: vi.fn() };
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

    for (const key of [
      ['freshness', 123],
      ['snapshots', 123],
    ]) {
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: key });
    }
    // listingQ's real key is ['listing', legacyId, natKeyId] (R2 Phase C
    // resolver-chain cutover) — FreshnessBlock only knows sreality_id, so it
    // invalidates the bare 'listing' prefix instead of guessing the shape.
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['listing'] });
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
/* Resolver chain — R2 Phase C cutover (legacy sreality_id route vs canonical  */
/* natural-key route, both converging on the same surrogate-id-keyed loaders) */
/* -------------------------------------------------------------------------- */

vi.mock('@/lib/queries', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/queries')>();
  return {
    ...actual,
    fetchListingBySreality: vi.fn(),
    fetchListingById: vi.fn(),
    fetchListingIdByNaturalKey: vi.fn(),
    fetchPropertyReprNaturalKey: vi.fn(async () => null),
    fetchPropertySources: vi.fn(async () => ({ property_id: null, sources: [] })),
    fetchPropertyMf: vi.fn(async () => null),
    fetchSnapshotsForListings: vi.fn(async () => []),
    fetchFreshnessChecksByListing: vi.fn(async () => []),
    fetchImagesByListing: vi.fn(async () => []),
  };
});
/* Only the two network wrappers are stubbed — contactState/prettyPhone stay REAL,
   because the vizitka's whole point is the three states they encode. */
vi.mock('@/lib/brokers', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/brokers')>()),
  fetchListingBroker: vi.fn(async () => null),
  fetchBrokersByIds: vi.fn(async () => new Map()),
}));
vi.mock('@/components/NewEstimationModal', () => ({
  useNewEstimationModal: () => ({ open: vi.fn() }),
}));
vi.mock('@/components/ExploreAreaModal', () => ({
  useExploreAreaModal: () => ({ open: vi.fn() }),
}));

const RESOLVER_LISTING = {
  id: 105053,
  sreality_id: -11876,
  first_seen_at: '2026-01-01T00:00:00Z',
  last_seen_at: '2026-01-02T00:00:00Z',
  is_active: true,
  source: 'idnes',
  category_main: 'byt',
  category_type: 'prodej',
  price_czk: 5_000_000,
  disposition: '2+kk',
  tom_days: 3,
} as unknown as ListingPublic;

describe('<ListingDetail> resolver chain', () => {
  beforeEach(() => {
    vi.mocked(queries.fetchListingBySreality).mockReset();
    vi.mocked(queries.fetchListingById).mockReset();
    vi.mocked(queries.fetchListingIdByNaturalKey).mockReset();
    vi.mocked(queries.fetchPropertySources).mockClear();
    vi.mocked(queries.fetchImagesByListing).mockClear();
    vi.mocked(queries.fetchFreshnessChecksByListing).mockClear();
  });

  function renderAt(path: string) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path="listing/:sreality_id" element={<ListingDetail />} />
            <Route
              path="listing/:source/:nativeId"
              element={<ListingDetail />}
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  it('legacy /listing/{sreality_id} fetches by sreality_id in ONE round trip, never resolves a natural key', async () => {
    vi.mocked(queries.fetchListingBySreality).mockResolvedValue(RESOLVER_LISTING);

    renderAt('/listing/-11876');

    await waitFor(() =>
      expect(queries.fetchListingBySreality).toHaveBeenCalledWith(-11876),
    );
    expect(queries.fetchListingIdByNaturalKey).not.toHaveBeenCalled();
    expect(queries.fetchListingById).not.toHaveBeenCalled();

    // Once loaded, the surrogate id (not sreality_id) keys the child loaders;
    // freshness stays sreality_id-keyed (listing_freshness_checks has no
    // listing_id column at all).
    await waitFor(() =>
      expect(queries.fetchPropertySources).toHaveBeenCalledWith(105053),
    );
    expect(queries.fetchImagesByListing).toHaveBeenCalledWith(105053);
    expect(queries.fetchFreshnessChecksByListing).toHaveBeenCalledWith(-11876);
  });

  it('canonical /listing/{source}/{native} resolves the surrogate id first, then fetches by id', async () => {
    vi.mocked(queries.fetchListingIdByNaturalKey).mockResolvedValue(105053);
    vi.mocked(queries.fetchListingById).mockResolvedValue(RESOLVER_LISTING);

    renderAt('/listing/idnes/6a147cfde222cf687509e018');

    await waitFor(() =>
      expect(queries.fetchListingIdByNaturalKey).toHaveBeenCalledWith(
        'idnes',
        '6a147cfde222cf687509e018',
      ),
    );
    await waitFor(() =>
      expect(queries.fetchListingById).toHaveBeenCalledWith(105053),
    );
    expect(queries.fetchListingBySreality).not.toHaveBeenCalled();

    await waitFor(() =>
      expect(queries.fetchPropertySources).toHaveBeenCalledWith(105053),
    );
    expect(queries.fetchImagesByListing).toHaveBeenCalledWith(105053);
    expect(queries.fetchFreshnessChecksByListing).toHaveBeenCalledWith(-11876);
  });
});

/* -------------------------------------------------------------------------- */
/* Broker vizitka (C2) — the header chip's tri-state fetch behaviour, plus the */
/* per-field 3-state contact rendering the chip never had                     */
/* -------------------------------------------------------------------------- */

const ATTRIBUTION: brokers.ListingBroker = {
  sreality_id: -11876,
  listing_id: 105053,
  broker_id: 7,
  broker_display_name: 'Jan Novák',
  broker_firm_label: 'RE/MAX Alfa',
};

// The /brokers batch row. Which contact half arrives (primary_* vs has_*) is a
// property of the CALLER — admin vs not — so each test picks one.
function brokerRow(contact: Partial<brokers.BrokerPublic>): brokers.BrokerPublic {
  return {
    broker_id: 7,
    display_name: 'Jan Novák',
    firm_name: 'RE/MAX Alfa',
    ...contact,
  } as brokers.BrokerPublic;
}

describe('<BrokerVizitka>', () => {
  beforeEach(() => {
    vi.mocked(queries.fetchListingBySreality).mockReset();
    vi.mocked(queries.fetchListingBySreality).mockResolvedValue(RESOLVER_LISTING);
    vi.mocked(brokers.fetchListingBroker).mockReset();
    vi.mocked(brokers.fetchListingBroker).mockResolvedValue(ATTRIBUTION);
    vi.mocked(brokers.fetchBrokersByIds).mockReset();
    vi.mocked(brokers.fetchBrokersByIds).mockResolvedValue(new Map());
  });

  function renderListing() {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/listing/-11876']}>
          <Routes>
            <Route path="listing/:sreality_id" element={<ListingDetail />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  function withContact(contact: Partial<brokers.BrokerPublic>) {
    vi.mocked(brokers.fetchBrokersByIds).mockResolvedValue(
      new Map([[7, brokerRow(contact)]]),
    );
  }

  it('shows the real contact for an admin session, keyed on the attributed broker', async () => {
    withContact({ primary_phone: '420777123456', primary_email: 'jan@remax.cz' });

    renderListing();

    expect(await screen.findByText('+420 777 123 456')).toBeInTheDocument();
    expect(screen.getByText('jan@remax.cz')).toBeInTheDocument();
    expect(screen.getByText('Jan Novák')).toBeInTheDocument();
    expect(screen.getByText('RE/MAX Alfa')).toBeInTheDocument();
    // Identity comes from /brokers/by-listing, contact from the /brokers batch —
    // the by-listing route carries no contact fields to render.
    expect(brokers.fetchListingBroker).toHaveBeenCalledWith(105053);
    expect(brokers.fetchBrokersByIds).toHaveBeenCalledWith([7]);
    // ATTRIBUTION deliberately gives broker_id (7) and listing_id (105053)
    // different values — pins the link to the BROKER dossier, not a
    // listing-id-shaped route that would 404 on every click.
    expect(screen.getByRole('link', { name: /Jan Novák/ })).toHaveAttribute(
      'href',
      '/brokers/7',
    );
  });

  it('shows a loading placeholder while the contact read is in flight, never the error line', async () => {
    vi.mocked(brokers.fetchBrokersByIds).mockReturnValue(new Promise(() => {}));

    renderListing();

    expect(await screen.findByText('Jan Novák')).toBeInTheDocument();
    expect(screen.getByText('Načítám kontakt…')).toBeInTheDocument();
    expect(screen.queryByText('Kontakt se nepodařilo načíst')).toBeNull();
  });

  /* The beforeEach default: fetchBrokersByIds resolves (succeeds) with an empty
     Map — the broker just isn't in this batch (filtered by status, merged away
     mid-request). A SUCCESSFUL empty result must not read as a failed one. */
  it('shows a neutral message, not the error line, when the contact batch succeeds with no row for this broker', async () => {
    renderListing();

    expect(await screen.findByText('Jan Novák')).toBeInTheDocument();
    expect(screen.getByText('Kontakt není k dispozici')).toBeInTheDocument();
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
    // No attribution, no broker_id — the contact call must not fire at all.
    expect(brokers.fetchBrokersByIds).not.toHaveBeenCalled();
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

  /* The same distinction one level up: a failed contact read must not render as
     a broker with no reachable channel. The identity still shows. */
  it('separates a failed contact read from a broker with no contact', async () => {
    vi.mocked(brokers.fetchBrokersByIds).mockRejectedValue(new Error('HTTP 500'));

    renderListing();

    expect(
      await screen.findByText('Kontakt se nepodařilo načíst'),
    ).toBeInTheDocument();
    expect(screen.getByText('Jan Novák')).toBeInTheDocument();
    expect(screen.queryByText(/telefon —/)).not.toBeInTheDocument();
  });
});
