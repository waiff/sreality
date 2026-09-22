/* CollectionDetail — the edit fields and the monitoring switch announce
 * themselves.
 *
 * Hermetic: mock getCollection. The three controls this pins were all nameless
 * — two inputs carrying only a placeholder, and a role="switch" whose only
 * child is a decorative knob, so a screen reader read it as "switch, off" with
 * nothing to say WHAT is off.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import CollectionDetail from './CollectionDetail';
import type { CollectionPropertyRow, CollectionWithProperties } from '@/lib/types';
import * as api from '@/lib/api';
import { curationKeys } from '@/lib/queries';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    getCollection: vi.fn(),
    removePropertyFromCollection: vi.fn(),
  };
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

const MEMBER: CollectionPropertyRow = {
  property_id: 42,
  sreality_id: 900,
  source: 'sreality',
  display_label: 'Praha 5',
  disposition: '2+kk',
  subtype: null,
  area_m2: 55,
  price_czk: 8_900_000,
  last_seen_at: '2026-08-20T10:00:00Z',
  is_active: true,
  added_at: '2026-08-01T10:00:00Z',
};

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const invalidate = vi.spyOn(qc, 'invalidateQueries');
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/collections/1']}>
        <Routes>
          <Route path="/collections/:id" element={<CollectionDetail />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { invalidate };
}

describe('<CollectionDetail>', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getCollection).mockResolvedValue(DATA);
    vi.mocked(api.removePropertyFromCollection).mockResolvedValue({
      removed: true,
    });
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

  /* Removing a member here changes the SAME fact the Browse card glyph and the
     listing header paint, so it has to revalidate the shared member map — this
     row used to hand-type a key list that omitted it. */
  it('removes a member through the API and revalidates the shared member map', async () => {
    vi.mocked(api.getCollection).mockResolvedValue({
      ...DATA,
      properties: [MEMBER],
    });
    const { invalidate } = renderPage();

    fireEvent.click(
      await screen.findByRole('button', { name: 'Remove listing from collection' }),
    );

    await waitFor(() =>
      expect(api.removePropertyFromCollection).toHaveBeenCalledWith(1, 42),
    );
    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({
        queryKey: curationKeys.propertyCollectionMembers,
      }),
    );
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: curationKeys.collection(1),
    });
  });
});
