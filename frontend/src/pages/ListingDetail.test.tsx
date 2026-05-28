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

import { FreshnessBlock } from './ListingDetail';
import * as api from '@/lib/api';

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
      ['listing', 123],
    ]) {
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: key });
    }
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
