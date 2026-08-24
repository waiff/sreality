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

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['freshness', 123] });
    // listingQ's real key is ['listing', legacyId, resolvedListingId] and
    // snapshotsQ's is ['snapshots', snapshotListingIds] (an array of surrogate
    // ids) — FreshnessBlock only knows sreality_id, which matches neither
    // shape, so both invalidate their bare prefix instead of guessing it.
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['listing'] });
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
/* Only the network wrapper is stubbed — contactState/prettyPhone stay REAL,
   because the vizitka's whole point is the three states they encode. Since W6
   there is one wrapper to stub, not two: the contact arrives on the attribution
   row itself (migration 419). */
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

const RESOLVER_LISTING = {
  id: 105053,
  sreality_id: -11876,
  first_seen_at: '2026-01-01T00:00:00Z',
  last_seen_at: '2026-01-02T00:00:00Z',
  is_active: true,
  source: 'idnes',
  // W9b (migration 420): the listing's own identity, on the listing row.
  source_id_native: '6a147cfde222cf687509e018',
  property_id: 774,
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
    // The page canonicalizes itself once the listing lands (see the W9b case
    // below), and the canonical route it lands on is id-keyed.
    vi.mocked(queries.fetchListingById).mockResolvedValue(RESOLVER_LISTING);

    renderAt('/listing/-11876');

    await waitFor(() =>
      expect(queries.fetchListingBySreality).toHaveBeenCalledWith(-11876),
    );
    // The point of the legacy route: the URL IS the sreality_id, so it is never
    // worth a natural-key resolve. (fetchListingById is a different claim — the
    // canonical URL this redirects to is id-keyed by construction.)
    expect(queries.fetchListingIdByNaturalKey).not.toHaveBeenCalled();

    // Once loaded, the surrogate id (not sreality_id) keys the child loaders;
    // freshness stays sreality_id-keyed (listing_freshness_checks has no
    // listing_id column at all).
    //
    // W9b: on the legacy route this read cannot start until the listing lands,
    // so the listing's own property_id is ALWAYS in hand by then — it is handed
    // over, and fetchPropertySources skips the resolve hop that would otherwise
    // re-read this same row from property_sources_public to learn one column of
    // it. The second argument is the assertion.
    await waitFor(() =>
      expect(queries.fetchPropertySources).toHaveBeenCalledWith(105053, 774),
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

    // W9b's known-property_id fast path deliberately does NOT reach here: on the
    // canonical route this read fires alongside the listing (W9a), so there is
    // no listing row to take a property_id from yet and it resolves one itself.
    // Gating it on the listing would trade one hop for a whole waterfall level.
    await waitFor(() =>
      expect(queries.fetchPropertySources).toHaveBeenCalledWith(105053, undefined),
    );
    expect(queries.fetchImagesByListing).toHaveBeenCalledWith(105053);
    expect(queries.fetchFreshnessChecksByListing).toHaveBeenCalledWith(-11876);
  });

  it('W9a: fires property-sources off the resolved id, not the loaded listing — parallel with the listing fetch, not after it', async () => {
    vi.mocked(queries.fetchListingIdByNaturalKey).mockResolvedValue(105053);
    // fetchListingById deliberately never resolves in this test — if
    // property-sources still waited on listingQ.data, it would never fire.
    vi.mocked(queries.fetchListingById).mockReturnValue(new Promise(() => {}));

    renderAt('/listing/idnes/6a147cfde222cf687509e018');

    await waitFor(() =>
      expect(queries.fetchListingIdByNaturalKey).toHaveBeenCalledWith(
        'idnes',
        '6a147cfde222cf687509e018',
      ),
    );
    // W9b must not have quietly undone this: the known-property_id fast path is
    // an ARGUMENT, never a gate. Here the listing never resolves, so the second
    // argument is `undefined` and the read does its own resolve hop — exactly as
    // before — rather than waiting for a property_id that will never arrive.
    await waitFor(() =>
      expect(queries.fetchPropertySources).toHaveBeenCalledWith(105053, undefined),
    );
    // The listing fetch was issued (id known) but is still pending — proving
    // property-sources didn't wait for it to settle.
    expect(queries.fetchListingById).toHaveBeenCalledWith(105053);
  });

  /* W9b: the legacy URL canonicalizes off the LISTING ROW alone.
     source_id_native is on listings_public now (migration 420), so the synthetic
     negative id leaves the URL bar as soon as the listing lands. Before, the
     redirect searched the sibling source list for the row matching this
     listing's surrogate id — i.e. it waited on the whole multi-portal read to
     learn one string belonging to the row it was already holding.
     property-sources is made to hang forever here: if the redirect still needed
     it, the URL would never change. */
  it('W9b: canonicalizes the legacy URL from the listing row, without the sources read', async () => {
    vi.mocked(queries.fetchListingBySreality).mockResolvedValue(RESOLVER_LISTING);
    vi.mocked(queries.fetchListingById).mockResolvedValue(RESOLVER_LISTING);
    vi.mocked(queries.fetchPropertySources).mockReturnValue(
      new Promise(() => {}) as ReturnType<typeof queries.fetchPropertySources>,
    );

    renderAt('/listing/-11876');

    // Landing on the canonical route is what proves it: that route is keyed on
    // the surrogate id, and only the redirect (seeding state.listingId) gets us
    // there without a natural-key resolve.
    await waitFor(() =>
      expect(queries.fetchListingById).toHaveBeenCalledWith(105053),
    );
    expect(queries.fetchListingIdByNaturalKey).not.toHaveBeenCalled();
  });

  /* The pre-attach window (rule #19: a freshly scraped row lands property_id
     NULL) is the one case the fast path must NOT claim to know the answer for —
     property_sources_public filters `property_id is not null`, so a NULL is
     "ask", not "there is none". */
  it('W9b: falls back to resolving when the listing has no property_id yet', async () => {
    vi.mocked(queries.fetchListingBySreality).mockResolvedValue({
      ...RESOLVER_LISTING,
      property_id: null,
    } as unknown as ListingPublic);
    vi.mocked(queries.fetchListingById).mockResolvedValue({
      ...RESOLVER_LISTING,
      property_id: null,
    } as unknown as ListingPublic);

    renderAt('/listing/-11876');

    await waitFor(() =>
      expect(queries.fetchPropertySources).toHaveBeenCalledWith(105053, null),
    );
  });
});

/* -------------------------------------------------------------------------- */
/* Broker vizitka (C2) — the header chip's tri-state fetch behaviour, plus the */
/* per-field 3-state contact rendering the chip never had                     */
/* -------------------------------------------------------------------------- */

/* One row now: /brokers/by-listing carries identity AND contact (migration 419).
   Which contact half arrives (primary_* vs has_*) is still a property of the
   CALLER — admin vs not — so each test picks one. broker_id (7) and listing_id
   (105053) are deliberately different values; that difference is what pins the
   link below to the BROKER dossier rather than a listing-id-shaped route. */
const attribution = (
  contact: Partial<brokers.BrokerContactFields> = {},
): brokers.ListingBroker => ({
  sreality_id: -11876,
  listing_id: 105053,
  broker_id: 7,
  broker_display_name: 'Jan Novák',
  broker_firm_label: 'RE/MAX Alfa',
  ...contact,
});

describe('<BrokerVizitka>', () => {
  beforeEach(() => {
    vi.mocked(queries.fetchListingBySreality).mockReset();
    vi.mocked(queries.fetchListingBySreality).mockResolvedValue(RESOLVER_LISTING);
    // Since W9b the legacy URL canonicalizes off the listing row alone, so this
    // page redirects to /listing/{source}/{native} the moment the listing lands
    // — as it does in production. The id-keyed loader has to answer there, or
    // these tests would be asserting against an unmounted page.
    vi.mocked(queries.fetchListingById).mockReset();
    vi.mocked(queries.fetchListingById).mockResolvedValue(RESOLVER_LISTING);
    vi.mocked(brokers.fetchListingBroker).mockReset();
    vi.mocked(brokers.fetchListingBroker).mockResolvedValue(attribution());
  });

  function renderListing() {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/listing/-11876']}>
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
    expect(brokers.fetchListingBroker).toHaveBeenCalledWith(105053);
    expect(brokers.fetchListingBroker).toHaveBeenCalledTimes(1);
    // ATTRIBUTION deliberately gives broker_id (7) and listing_id (105053)
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
