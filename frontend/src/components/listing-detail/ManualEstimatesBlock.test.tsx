/* Manual rental estimates are shared reference data: every session sees them,
 * only an admin session gets the add / edit / delete controls. */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import ManualEstimatesBlock from './ManualEstimatesBlock';
import * as api from '@/lib/api';
import * as auth from '@/lib/auth';
import type { ManualRentalEstimate } from '@/lib/types';

vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  listManualEstimates: vi.fn(),
}));
vi.mock('@/lib/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/auth')>()),
  useAuth: vi.fn(),
}));

const ESTIMATE: ManualRentalEstimate = {
  id: 1,
  sreality_id: 555,
  listing_id: 101,
  rent_czk: 30_000,
  author: 'op',
  source_kind: 'broker',
  notes: 'from a broker quote',
  created_at: '2026-09-01T08:00:00Z',
  updated_at: '2026-09-01T08:00:00Z',
};

function renderBlock(isAdmin: boolean) {
  vi.mocked(auth.useAuth).mockReturnValue({ isAdmin } as ReturnType<typeof auth.useAuth>);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ManualEstimatesBlock sreality_id={555} />
    </QueryClientProvider>,
  );
}

describe('ManualEstimatesBlock', () => {
  beforeEach(() => {
    vi.mocked(api.listManualEstimates).mockResolvedValue({ data: [ESTIMATE] });
  });

  it('shows the estimates without write controls to a non-admin', async () => {
    renderBlock(false);
    expect(await screen.findByText('from a broker quote')).toBeTruthy();
    expect(screen.queryByText('+ Add estimate')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Delete' })).toBeNull();
  });

  it('gives an admin the add, edit and delete controls', async () => {
    renderBlock(true);
    expect(await screen.findByText('from a broker quote')).toBeTruthy();
    expect(screen.getByText('+ Add estimate')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Edit' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Delete' })).toBeTruthy();
  });
});
