/* CollectionSaveToggle — the listing-detail "save to collection" control.
 *
 * Hermetic: mock the two reads (this property's memberships + the collection
 * list) and the two writes. What is worth pinning here is not the network call
 * (api/test_curation.py owns that) but the contract the operator sees: the
 * button says what it does, says it differently once the property IS saved,
 * opens a real named popup, and writes through the SHARED menu — the same one
 * the Browse card's bookmark glyph opens.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import CollectionSaveToggle from './CollectionSaveToggle';
import type { Collection } from '@/lib/types';
import * as api from '@/lib/api';
import * as queries from '@/lib/queries';
import { expectNoNestedInteractive } from '@/test/a11y';

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
  return { ...actual, fetchPropertyCollectionIds: vi.fn() };
});

const collection = (over: Partial<Collection>): Collection =>
  ({
    id: 1,
    name: 'Kolekce',
    description: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    listing_count: 0,
    monitoring_enabled: false,
    notify_channels: [],
    is_system: false,
    ...over,
  }) as Collection;

const COLLECTIONS = [
  collection({ id: 7, name: 'Sledované', monitoring_enabled: true }),
  collection({ id: 9, name: 'Praha 5' }),
];

function renderToggle() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <CollectionSaveToggle property_id={42} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('<CollectionSaveToggle>', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listCollections).mockResolvedValue({
      data: COLLECTIONS,
      total: 2,
    });
    vi.mocked(api.addPropertiesToCollection).mockResolvedValue({
      added: 1,
      skipped: 0,
    });
    vi.mocked(api.removePropertyFromCollection).mockResolvedValue({
      removed: true,
    });
  });

  it('offers the save verb when the property is in no collection', async () => {
    vi.mocked(queries.fetchPropertyCollectionIds).mockResolvedValue([]);
    renderToggle();

    const btn = await screen.findByRole('button', { name: 'Uložit do kolekce' });
    expect(btn).toHaveAttribute('aria-expanded', 'false');
    // A list of toggles is not a menu — same disclosure contract as the card.
    expect(btn).not.toHaveAttribute('aria-haspopup');
  });

  /* The visible label carries the state; an aria-label reading "Uložit do
     kolekce" over a button that says "V kolekci" would be the label-in-name
     mismatch the interactive-semantics program exists to catch. */
  it('says it is saved — and how many times — once the property is a member', async () => {
    vi.mocked(queries.fetchPropertyCollectionIds).mockResolvedValue([7]);
    renderToggle();
    expect(await screen.findByRole('button', { name: 'V kolekci' })).toBeInTheDocument();

    vi.mocked(queries.fetchPropertyCollectionIds).mockResolvedValue([7, 9]);
    renderToggle();
    expect(
      await screen.findByRole('button', { name: 'V kolekcích · 2' }),
    ).toBeInTheDocument();
  });

  it('opens the shared, named panel with monitored collections first', async () => {
    vi.mocked(queries.fetchPropertyCollectionIds).mockResolvedValue([]);
    renderToggle();

    const btn = await screen.findByRole('button', { name: 'Uložit do kolekce' });
    fireEvent.click(btn);

    const panel = await screen.findByRole('group', { name: 'Uložit do kolekce' });
    expect(btn).toHaveAttribute('aria-expanded', 'true');
    expect(btn).toHaveAttribute('aria-controls', panel.id);
    // Monitored first, then alphabetical — the order the card's panel uses.
    // findAll: the list is fetched when the panel opens, not before.
    const rows = await screen.findAllByRole('button', { name: /Sledované|Praha 5/ });
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveAccessibleName(/Sledované/);
    expect(rows[1]).toHaveAccessibleName(/Praha 5/);
    expectNoNestedInteractive(document.body);
  });

  it('adds the property to the collection that is clicked', async () => {
    vi.mocked(queries.fetchPropertyCollectionIds).mockResolvedValue([]);
    renderToggle();

    fireEvent.click(await screen.findByRole('button', { name: 'Uložit do kolekce' }));
    fireEvent.click(await screen.findByRole('button', { name: /Praha 5/ }));

    await waitFor(() =>
      expect(api.addPropertiesToCollection).toHaveBeenCalledWith(9, [42]),
    );
    expect(api.removePropertyFromCollection).not.toHaveBeenCalled();
  });

  /* Same click, opposite direction, when the row is already checked — the panel
     is a set of toggles, not an add-only list. */
  it('removes the property from a collection it is already in', async () => {
    vi.mocked(queries.fetchPropertyCollectionIds).mockResolvedValue([7]);
    renderToggle();

    fireEvent.click(await screen.findByRole('button', { name: 'V kolekci' }));
    fireEvent.click(await screen.findByRole('button', { name: /Sledované/ }));

    await waitFor(() =>
      expect(api.removePropertyFromCollection).toHaveBeenCalledWith(7, 42),
    );
    expect(api.addPropertiesToCollection).not.toHaveBeenCalled();
  });

  /* The panel used to answer a failed list and an empty one with the same
     "Create a collection →" — which tells an operator whose collections exist
     that they have none. Same distinction the broker vizitka draws. */
  it('says the list failed instead of offering to create a first collection', async () => {
    vi.mocked(queries.fetchPropertyCollectionIds).mockResolvedValue([]);
    vi.mocked(api.listCollections).mockRejectedValue(new Error('HTTP 500'));
    renderToggle();

    fireEvent.click(await screen.findByRole('button', { name: 'Uložit do kolekce' }));

    expect(await screen.findByText('Kolekce se nepodařilo načíst')).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /Create a collection/ })).toBeNull();
  });

  it('Escape closes the panel and hands focus back to the trigger', async () => {
    vi.mocked(queries.fetchPropertyCollectionIds).mockResolvedValue([]);
    renderToggle();

    const btn = await screen.findByRole('button', { name: 'Uložit do kolekce' });
    btn.focus();
    fireEvent.click(btn);
    const panel = await screen.findByRole('group', { name: 'Uložit do kolekce' });
    expect(panel.contains(document.activeElement)).toBe(true);

    fireEvent.keyDown(document.activeElement as HTMLElement, { key: 'Escape' });
    await waitFor(() =>
      expect(screen.queryByRole('group', { name: 'Uložit do kolekce' })).toBeNull(),
    );
    expect(document.activeElement).toBe(btn);
  });
});
