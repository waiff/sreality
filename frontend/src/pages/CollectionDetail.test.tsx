/* CollectionDetail — the edit fields and the monitoring switch announce
 * themselves.
 *
 * Hermetic: mock getCollection. The three controls this pins were all nameless
 * — two inputs carrying only a placeholder, and a role="switch" whose only
 * child is a decorative knob, so a screen reader read it as "switch, off" with
 * nothing to say WHAT is off.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import CollectionDetail from './CollectionDetail';
import type { CollectionWithProperties } from '@/lib/types';
import * as api from '@/lib/api';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return { ...actual, getCollection: vi.fn() };
});

const DATA: CollectionWithProperties = {
  collection: {
    id: 1,
    name: 'Vinohrady watch',
    description: 'Flats worth a second look.',
    created_at: '2026-08-01T10:00:00Z',
    updated_at: '2026-08-20T10:00:00Z',
    listing_count: 0,
    monitoring_enabled: false,
    notify_channels: [],
    is_system: false,
  },
  properties: [],
};

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/collections/1']}>
        <Routes>
          <Route path="/collections/:id" element={<CollectionDetail />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('<CollectionDetail>', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getCollection).mockResolvedValue(DATA);
  });

  it('names both edit fields', async () => {
    renderPage();
    const name = await screen.findByRole('textbox', { name: 'Name' });
    expect(name).toHaveValue('Vinohrady watch');
    expect(screen.getByRole('textbox', { name: 'Description' })).toHaveValue(
      'Flats worth a second look.',
    );
  });

  it('names the monitoring switch from the visible caption above it', async () => {
    renderPage();
    const toggle = await screen.findByRole('switch', { name: 'Monitoring' });
    expect(toggle).toHaveAccessibleName('Monitoring');
    expect(toggle).toHaveAttribute('aria-checked', 'false');
  });
});
