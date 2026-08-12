/* CollectionSaveButton — the one save-to-collection control (Browse cards +
 * the listing-detail action bar).
 *
 * Hermetic: mock the collection index, both membership reads and the two
 * writes. Pins what the shared component has to get right — the two membership
 * SOURCES (a record page must not download every membership; a list surface
 * must not issue one read per card), that a click toggles the right way in each
 * direction, and that the menu survives a tick so filing into several
 * collections is one pass. The real writes are covered by api/test_curation.py.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import CollectionSaveButton from './CollectionSaveButton';
import type { Collection } from '@/lib/types';
import * as api from '@/lib/api';
import * as queries from '@/lib/queries';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    listCollections: vi.fn(),
    addPropertiesToCollection: vi.fn(),
    removePropertyFromCollection: vi.fn(),
  };
});

vi.mock('@/lib/queries', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/queries')>();
  return {
    ...actual,
    fetchPropertyCollectionIds: vi.fn(),
    fetchPropertyCollectionMemberSet: vi.fn(),
  };
});

const collection = (id: number, name: string, monitoring = false): Collection => ({
  id,
  name,
  description: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  listing_count: 0,
  monitoring_enabled: monitoring,
  notify_channels: [],
  is_system: false,
});

const COLLECTIONS = [collection(1, 'Byty Praha'), collection(2, 'Sledované', true)];

function renderButton(
  props: Partial<React.ComponentProps<typeof CollectionSaveButton>> = {},
) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <CollectionSaveButton property_id={42} {...props} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/* The record-page shape, where membership is read for one property. */
const header = { variant: 'header', source: 'single' } as const;

describe('<CollectionSaveButton>', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listCollections).mockResolvedValue({ data: COLLECTIONS, total: 2 });
    vi.mocked(api.addPropertiesToCollection).mockResolvedValue({ added: 1, skipped: 0 });
    vi.mocked(api.removePropertyFromCollection).mockResolvedValue({ removed: true });
    vi.mocked(queries.fetchPropertyCollectionIds).mockResolvedValue([]);
    vi.mocked(queries.fetchPropertyCollectionMemberSet).mockResolvedValue(new Map());
  });

  it('reads membership from the shared map on list surfaces', async () => {
    vi.mocked(queries.fetchPropertyCollectionMemberSet).mockResolvedValue(
      new Map([[42, [1]]]),
    );
    renderButton();
    await screen.findByTitle('V kolekci');
    expect(queries.fetchPropertyCollectionIds).not.toHaveBeenCalled();
  });

  it('reads only this property on a record page, and labels the header variant', async () => {
    vi.mocked(queries.fetchPropertyCollectionIds).mockResolvedValue([1, 2]);
    renderButton(header);
    // Two memberships → plural label plus the count.
    expect(await screen.findByText('V kolekcích')).toBeInTheDocument();
    expect(screen.getByText('2')).toBeInTheDocument();
    expect(queries.fetchPropertyCollectionMemberSet).not.toHaveBeenCalled();
  });

  it('adds the property to a collection it is not in', async () => {
    renderButton(header);
    fireEvent.click(await screen.findByTitle('Uložit do kolekce'));
    fireEvent.click(await screen.findByRole('menuitemcheckbox', { name: /Byty Praha/ }));
    await waitFor(() =>
      expect(api.addPropertiesToCollection).toHaveBeenCalledWith(1, [42]),
    );
    expect(api.removePropertyFromCollection).not.toHaveBeenCalled();
  });

  it('removes the property from a collection it is already in', async () => {
    vi.mocked(queries.fetchPropertyCollectionIds).mockResolvedValue([1]);
    renderButton(header);
    fireEvent.click(await screen.findByTitle('V kolekci'));
    const item = await screen.findByRole('menuitemcheckbox', { name: /Byty Praha/ });
    expect(item).toHaveAttribute('aria-checked', 'true');
    fireEvent.click(item);
    await waitFor(() =>
      expect(api.removePropertyFromCollection).toHaveBeenCalledWith(1, 42),
    );
    expect(api.addPropertiesToCollection).not.toHaveBeenCalled();
  });

  it('keeps the menu open after a tick — the picker is multi-select', async () => {
    renderButton(header);
    fireEvent.click(await screen.findByTitle('Uložit do kolekce'));
    fireEvent.click(await screen.findByRole('menuitemcheckbox', { name: /Byty Praha/ }));
    await waitFor(() => expect(api.addPropertiesToCollection).toHaveBeenCalled());
    fireEvent.click(screen.getByRole('menuitemcheckbox', { name: /Sledované/ }));
    await waitFor(() =>
      expect(api.addPropertiesToCollection).toHaveBeenCalledWith(2, [42]),
    );
  });

  it('does not fetch the collection index until the menu opens', async () => {
    renderButton();
    await screen.findByTitle('Uložit do kolekce');
    expect(api.listCollections).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTitle('Uložit do kolekce'));
    await waitFor(() => expect(api.listCollections).toHaveBeenCalled());
  });
});
