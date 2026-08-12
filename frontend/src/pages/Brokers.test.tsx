/* /brokers — the leaderboard page is agenda-gated, not admin-gated, so anything
 * it asks of an admin-only route must be gated client-side or every non-admin
 * page view spends two 403s (retry: 1, refetched on every remount) on a badge
 * that links somewhere they cannot go.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, waitFor } from '@testing-library/react';
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
