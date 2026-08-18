/* /brokers — the leaderboard page is agenda-gated, not admin-gated, so anything
 * it asks of an admin-only route must be gated client-side or every non-admin
 * page view spends two 403s (retry: 1, refetched on every remount) on a badge
 * that links somewhere they cannot go.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import Brokers from './Brokers';
import * as api from '@/lib/api';
import * as auth from '@/lib/auth';
import * as brokers from '@/lib/brokers';

vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  listBrokerMergeCandidates: vi.fn(),
}));
vi.mock('@/lib/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/auth')>()),
  useAuth: vi.fn(),
}));
vi.mock('@/lib/brokers', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/brokers')>()),
  fetchBrokerLeaderboard: vi.fn(),
  searchBrokerFirms: vi.fn(),
}));
vi.mock('@/lib/supabase', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/supabase')>()),
  isSupabaseConfigured: () => true,
}));

function asUser(isAdmin: boolean): void {
  vi.mocked(auth.useAuth).mockReturnValue({
    isAdmin,
    session: null,
    user: null,
    loading: false,
    agendas: { brokers: true },
  } as unknown as ReturnType<typeof auth.useAuth>);
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/brokers']}>
        <Brokers />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(brokers.fetchBrokerLeaderboard).mockResolvedValue([]);
  vi.mocked(brokers.searchBrokerFirms).mockResolvedValue([]);
  vi.mocked(api.listBrokerMergeCandidates).mockResolvedValue({
    data: [],
    count: 0,
    reason_counts: { name_firm: 3 },
  } as unknown as Awaited<ReturnType<typeof api.listBrokerMergeCandidates>>);
});

describe('<Brokers> merge-candidates badge', () => {
  it('never asks the admin-only route for a non-admin viewer', async () => {
    asUser(false);
    renderPage();
    await waitFor(() => expect(brokers.fetchBrokerLeaderboard).toHaveBeenCalled());
    expect(api.listBrokerMergeCandidates).not.toHaveBeenCalled();
  });

  it('still sizes the badge for an admin', async () => {
    asUser(true);
    const { findByText } = renderPage();
    await waitFor(() => expect(api.listBrokerMergeCandidates).toHaveBeenCalledWith(1));
    expect(await findByText(/Sloučit duplicity \(3\)/)).toBeInTheDocument();
  });
});

describe('<Brokers> company filter', () => {
  it('renders the top companies as toggleable buttons, filters the leaderboard by firmIds, and untoggling clears it', async () => {
    asUser(false);
    // mmreality.cz is a franchise domain with a NULL display_name (migration
    // 190's _FIRM_DISPLAY_NAMES excludes franchises), so the button must fall
    // back to canonical_domain. Two options confirm toggling one leaves the
    // other's state alone. broker_count stays under 1000 so the expected
    // button text doesn't depend on cs-CZ's thousands-separator character.
    vi.mocked(brokers.searchBrokerFirms).mockResolvedValue([
      { firm_id: 3, canonical_domain: 'mmreality.cz', display_name: null,
        is_franchise: true, broker_count: 42 },
      { firm_id: 7, canonical_domain: 're-max.cz', display_name: null,
        is_franchise: true, broker_count: 30 },
    ]);
    const { getAllByRole } = renderPage();
    await waitFor(() => expect(brokers.fetchBrokerLeaderboard).toHaveBeenCalled());

    // Selected by plain textContent, not the accessible-name matcher: the
    // label and the "(count)" span are separate DOM nodes, and dom-testing-
    // library's name computation collapses them in a way a role/name query
    // doesn't reliably match, even though the rendered text is unambiguous.
    // Retried via waitFor — Typ/Nabídka's static buttons exist from the
    // first render, so a one-shot query resolves before the async company
    // buttons (behind searchBrokerFirms) have rendered at all.
    let mmreality: HTMLElement | undefined;
    let remax: HTMLElement | undefined;
    await waitFor(() => {
      const buttons = getAllByRole('button');
      mmreality = buttons.find((b) => b.textContent?.includes('mmreality.cz'));
      remax = buttons.find((b) => b.textContent?.includes('re-max.cz'));
      expect(mmreality).toBeDefined();
      expect(remax).toBeDefined();
    });
    if (!mmreality || !remax) throw new Error('company buttons not found');
    expect(mmreality.textContent).toContain('42');
    expect(remax.textContent).toContain('30');
    expect(mmreality).toHaveAttribute('aria-pressed', 'false');

    fireEvent.click(mmreality);
    expect(mmreality).toHaveAttribute('aria-pressed', 'true');
    expect(remax).toHaveAttribute('aria-pressed', 'false');
    await waitFor(() =>
      expect(brokers.fetchBrokerLeaderboard).toHaveBeenLastCalledWith(
        expect.objectContaining({ firmIds: [3] }),
      ),
    );

    // Back to firmIds: [] is the SAME query key as the initial mount, so
    // react-query serves it from cache — assert the button's own toggled
    // state rather than a fresh fetchBrokerLeaderboard call, which caching
    // correctly skips.
    fireEvent.click(mmreality);
    expect(mmreality).toHaveAttribute('aria-pressed', 'false');
  });
});
