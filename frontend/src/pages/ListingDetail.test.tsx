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
vi.mock('@/lib/brokers', () => ({ fetchListingBroker: vi.fn(async () => null) }));
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
