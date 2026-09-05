/* /outreach — campaign rows are destinations, so they are anchors.
 *
 * Two defects were fixed together here: the row offered no right-click "open in
 * new tab" (it was a <button> calling navigate()), and CampaignRow's root is a
 * <div>, which a <button> may not legally contain (phrasing content only). An
 * <a> is transparent content, so the anchor fixes both. */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import Outreach from './Outreach';
import * as api from '@/lib/api';
import { ROUTES } from '@/lib/routes';

vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  listOutreachCampaigns: vi.fn(),
  createOutreachCampaign: vi.fn(),
}));

function campaign(id: number, name: string) {
  return {
    id,
    name,
    goal: 'test goal',
    status: 'draft',
    created_at: '2026-09-01T00:00:00Z',
    draft_count: 1,
    approved_count: 0,
    sent_count: 0,
  } as unknown as api.OutreachCampaign;
}

function payload(...cs: ReturnType<typeof campaign>[]) {
  return { campaigns: cs } as unknown as Awaited<
    ReturnType<typeof api.listOutreachCampaigns>
  >;
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/outreach']}>
        <Outreach />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(api.listOutreachCampaigns).mockResolvedValue(payload());
});

describe('<Outreach> campaign rows are links', () => {
  it('renders each campaign as an anchor to its detail route', async () => {
    vi.mocked(api.listOutreachCampaigns).mockResolvedValue(
      payload(campaign(3, 'Jarni kampan'), campaign(9, 'Podzimni kampan')),
    );
    renderPage();

    const first = await screen.findByRole('link', { name: /Jarni kampan/ });
    expect(first).toHaveAttribute('href', ROUTES.outreachDetail.build({ id: 3 }));
    expect(screen.getByRole('link', { name: /Podzimni kampan/ })).toHaveAttribute(
      'href',
      ROUTES.outreachDetail.build({ id: 9 }),
    );
  });

  it('exposes no row-shaped button once the list has rendered', async () => {
    vi.mocked(api.listOutreachCampaigns).mockResolvedValue(payload(campaign(3, 'Jarni kampan')));
    renderPage();
    await screen.findByRole('link', { name: /Jarni kampan/ });
    expect(screen.queryByRole('button', { name: /Jarni kampan/ })).toBeNull();
  });
});
