/* CurationBlock — accessible names for the three unlabelled curation inputs.
 *
 * All three were reachable only through a placeholder (or, for the note
 * editor, through nothing at all while autoFocus dropped the caret into it).
 * The list/membership reads are mocked so no network call fires.
 */

import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import type { Note, Tag } from '@/lib/types';

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  listCollections: vi.fn(),
  listTags: vi.fn(),
  listPropertyNotes: vi.fn(),
}));

vi.mock('@/lib/queries', async (orig) => ({
  ...(await orig<typeof import('@/lib/queries')>()),
  fetchPropertyCollectionIds: vi.fn(),
  fetchPropertyTagIds: vi.fn(),
}));

import { listCollections, listPropertyNotes, listTags } from '@/lib/api';
import { fetchPropertyCollectionIds, fetchPropertyTagIds } from '@/lib/queries';
import CurationBlock from './CurationBlock';

const TAG: Tag = {
  id: 3, name: 'k prohlídce', color: 'copper', created_at: '2026-05-01T00:00:00+00:00',
  listing_count: 2,
};

const NOTE: Note = {
  id: 11, property_id: 42, body: 'Sousedi jsou hlučni.', origin_listing_id: 900,
  created_at: '2026-05-02T00:00:00+00:00', updated_at: null,
};

function renderBlock() {
  vi.mocked(listCollections).mockResolvedValue({ data: [] } as never);
  vi.mocked(listTags).mockResolvedValue({ data: [TAG] } as never);
  vi.mocked(listPropertyNotes).mockResolvedValue({ data: [NOTE] } as never);
  vi.mocked(fetchPropertyCollectionIds).mockResolvedValue([]);
  vi.mocked(fetchPropertyTagIds).mockResolvedValue([]);

  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <CurationBlock property_id={42} sreality_id={900} listing_id={900} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('<CurationBlock> control names', () => {
  it('names the note composer by the visible Notes heading', async () => {
    renderBlock();
    expect(await screen.findByRole('textbox', { name: 'Notes' }))
      .toHaveAccessibleName('Notes');
  });

  it('names the tag find-or-create box', async () => {
    const user = userEvent.setup();
    renderBlock();
    await user.click(await screen.findByRole('button', { name: 'Add tag' }));
    expect(screen.getByRole('textbox', { name: 'Find or create a tag' }))
      .toBeInTheDocument();
  });

  it('names the note editor that autoFocus lands in', async () => {
    const user = userEvent.setup();
    renderBlock();
    await user.click(await screen.findByRole('button', { name: 'Edit note' }));
    const editor = screen.getByRole('textbox', { name: 'Edit note' });
    expect(editor).toHaveAccessibleName('Edit note');
    expect(document.activeElement).toBe(editor);
  });
});
