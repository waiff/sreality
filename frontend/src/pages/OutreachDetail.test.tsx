/* OutreachDetail — the letter's two editing controls must be named.
 *
 * The subject input was named only by a placeholder (which vanishes the moment
 * the operator types) and the 7-row body textarea by nothing at all. Both now
 * sit in a <Field as="control">, so the caption names them for good.
 */

import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import type { OutreachCampaign, OutreachMessage } from '@/lib/api';

const campaign: OutreachCampaign = {
  id: 1,
  name: 'Jarní oslovení',
  goal: null,
  guidance: null,
  status: 'active',
  target: {} as OutreachCampaign['target'],
  created_at: '2026-05-12T10:00:00+00:00',
  updated_at: '2026-05-12T10:00:00+00:00',
};

const message: OutreachMessage = {
  id: 10,
  campaign_id: 1,
  broker_id: 5,
  broker_name: 'Jan Novák',
  firm_name: null,
  channel: 'email',
  to_email: 'jan@example.com',
  to_phone: null,
  subject: 'Spolupráce',
  body: 'Dobrý den…',
  status: 'draft',
  model: null,
  cost_usd: null,
  generated_at: '2026-05-12T10:00:00+00:00',
  approved_at: null,
  sent_at: null,
  sent_via: null,
  notes: null,
};

// The cache is seeded below; the mocks only guard against a network call.
// Factory is hoisted — keep it free of outer-scope references.
vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  getOutreachCampaign: vi.fn(),
  listOutreachMessages: vi.fn(),
  previewOutreachTargets: vi.fn(),
  listOutreachSuppressions: vi.fn().mockResolvedValue({ suppressions: [] }),
}));

import OutreachDetail from './OutreachDetail';

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(['outreach-campaign', 1], campaign);
  qc.setQueryData(['outreach-messages', 1], { messages: [message] });
  qc.setQueryData(['outreach-targets', 1], { targets: [], count: 0 });
  qc.setQueryData(['outreach-suppressions'], { suppressions: [] });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/outreach/1']}>
        <Routes>
          <Route path="outreach/:id" element={<OutreachDetail />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('<OutreachDetail> letter fields', () => {
  it('names the subject input and the body textarea by their captions', () => {
    renderPage();
    const subject = screen.getByRole('textbox', { name: 'Předmět' });
    const body = screen.getByRole('textbox', { name: 'Text zprávy' });
    expect(subject).toHaveValue('Spolupráce');
    expect(body).toHaveAccessibleName('Text zprávy');
  });
});
