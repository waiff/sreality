/* TiersSection — the admin tier editor (agenda matrix + per-account plan).
 *
 * Pins the two controls a placeholder or a neighbouring cell used to "name":
 * the add-tier key field, and the per-account plan <select>, which repeats once
 * per row — so its name has to carry the account it acts on, not a flat "Plan"
 * that would give every row the same name.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import TiersSection from './TiersSection';
import * as api from '@/lib/api';

vi.mock('@/lib/api', () => ({
  adminListPlans: vi.fn(),
  adminListEntitlements: vi.fn(),
  adminCreatePlan: vi.fn(),
  adminUpdatePlan: vi.fn(),
  adminDeletePlan: vi.fn(),
  adminSetEntitlement: vi.fn(),
}));

const PLANS: api.Plan[] = [
  { key: 'free', name: 'Free', position: 0, agendas: { browse: true }, is_default: true, updated_at: null },
  { key: 'pro', name: 'Pro', position: 1, agendas: { browse: true }, is_default: false, updated_at: null },
];

const ENTS: api.EntitlementRow[] = [
  {
    account_id: 'acc-1',
    email: 'hejtmanekp@gmail.com',
    plan: 'pro',
    status: 'active',
    current_period_end: null,
    is_explicit: true,
  },
  {
    account_id: 'acc-2',
    email: null,
    plan: 'free',
    status: 'active',
    current_period_end: null,
    is_explicit: false,
  },
];

const renderSection = () => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TiersSection />
    </QueryClientProvider>,
  );
};

describe('<TiersSection>', () => {
  beforeEach(() => {
    vi.mocked(api.adminListPlans).mockResolvedValue({ data: PLANS });
    vi.mocked(api.adminListEntitlements).mockResolvedValue({ data: ENTS });
  });

  it('names the add-tier field', async () => {
    renderSection();
    const field = await screen.findByRole('textbox', { name: 'New tier key' });
    expect(field).toHaveAccessibleName('New tier key');
    expect(screen.getByRole('button', { name: 'Add tier' })).toBeInTheDocument();
  });

  it('names each account plan select by the account it acts on', async () => {
    renderSection();
    await waitFor(() =>
      expect(
        screen.getByRole('combobox', { name: 'Tier for hejtmanekp@gmail.com' }),
      ).toHaveValue('pro'),
    );
    // The account with no email falls back to the id shown in the same row.
    expect(screen.getByRole('combobox', { name: 'Tier for acc-2' })).toHaveValue('free');
  });
});
