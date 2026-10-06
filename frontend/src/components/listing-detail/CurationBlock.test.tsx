/* CurationBlock — accessible names for the three unlabelled curation inputs.
 *
 * All three were reachable only through a placeholder (or, for the note
 * editor, through nothing at all while autoFocus dropped the caret into it).
 * The list/membership reads are mocked so no network call fires.
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import type { Note, Tag } from '@/lib/types';

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  listCollections: vi.fn(),
  listTags: vi.fn(),
  listPropertyNotes: vi.fn(),
  createPropertyNote: vi.fn(),
  deletePropertyNote: vi.fn(),
}));

vi.mock('@/lib/queries', async (orig) => ({
  ...(await orig<typeof import('@/lib/queries')>()),
  fetchPropertyCollectionMemberSet: vi.fn(),
  fetchPropertyTagIds: vi.fn(),
}));

import { createPropertyNote, deletePropertyNote, listCollections, listPropertyNotes, listTags } from '@/lib/api';
import { curationKeys, fetchPropertyCollectionMemberSet, fetchPropertyTagIds } from '@/lib/queries';
import CurationBlock from './CurationBlock';

const TAG: Tag = {
  id: 3, name: 'k prohlídce', color: 'copper', created_at: '2026-05-01T00:00:00+00:00',
  listing_count: 2,
};

const NOTE: Note = {
  id: 11, property_id: 42, body: 'Sousedi jsou hlučni.', origin_listing_id: 900,
  created_at: '2026-05-02T00:00:00+00:00', updated_at: null,
};

function mockReads() {
  vi.mocked(listCollections).mockResolvedValue({ data: [] } as never);
  vi.mocked(listTags).mockResolvedValue({ data: [TAG] } as never);
  vi.mocked(listPropertyNotes).mockResolvedValue({ data: [NOTE] } as never);
  vi.mocked(fetchPropertyCollectionMemberSet).mockResolvedValue(new Map());
  vi.mocked(fetchPropertyTagIds).mockResolvedValue([]);
}

function renderBlock() {
  mockReads();
  return renderWithReads();
}

/* Renders over whatever the mocks hold now, so a test can fail one read first. */
function renderWithReads() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(qc, 'invalidateQueries');
  const view = render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <CurationBlock property_id={42} sreality_id={900} listing_id={900} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...view, invalidate };
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

/* The tag-edit popover on each row of this dropdown is portalled to <body>
 * now (it used to be an `absolute` panel clipped by the listbox's
 * `overflow-y-auto`). This dropdown dismisses itself with a document
 * mousedown + `ref.contains`, which a portalled child fails — so the first
 * press inside the edit panel closed the dropdown and unmounted the panel
 * mid-edit. The guard is the `[data-transient-layer]` marker AnchoredPopover
 * sets, the same one lib/useDialog's focus trap reads. */
describe('<CurationBlock> the add-tag dropdown and its portalled edit popover', () => {
  it('stays open while the operator presses inside the edit popover', async () => {
    const user = userEvent.setup();
    renderBlock();

    await user.click(await screen.findByRole('button', { name: 'Add tag' }));
    await user.click(screen.getByRole('button', { name: `Edit tag ${TAG.name}` }));

    const field = screen.getByRole('textbox', { name: 'Edit tag' });
    fireEvent.mouseDown(field);

    expect(screen.getByRole('textbox', { name: 'Find or create a tag' })).toBeInTheDocument();
    expect(field).toBeInTheDocument();
  });

  it('still closes on a press that is genuinely outside', async () => {
    const user = userEvent.setup();
    renderBlock();

    await user.click(await screen.findByRole('button', { name: 'Add tag' }));
    fireEvent.mouseDown(document.body);

    expect(screen.queryByRole('textbox', { name: 'Find or create a tag' })).toBeNull();
  });
});

/* MS16: every Browse row's note mark counts the account's notes, so a note
 * added or deleted here re-reads the counts. */
describe('<CurationBlock> note counts', () => {
  it('re-reads the note marks after a note is added and after one is deleted', async () => {
    const user = userEvent.setup();
    mockReads();
    vi.mocked(createPropertyNote).mockResolvedValue(NOTE as never);
    vi.mocked(deletePropertyNote).mockResolvedValue({ deleted: true } as never);
    const { invalidate } = renderWithReads();
    const counts = () =>
      invalidate.mock.calls.filter(([f]) => f?.queryKey === curationKeys.noteCounts).length;

    await user.type(await screen.findByRole('textbox', { name: 'Notes' }), 'Volat v pondělí.');
    await user.click(screen.getByRole('button', { name: 'Save note' }));
    await waitFor(() => expect(counts()).toBe(1));

    await user.click(screen.getByRole('button', { name: 'Delete note' }));
    await user.click(screen.getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(counts()).toBe(2));
  });
});

/* MS16: a failed read says so and reads again; it is never drawn as "no notes",
 * "no tags" or a row of unticked collections. */
describe('<CurationBlock> failed reads', () => {
  const banner = (subject: string) =>
    screen.findByText(subject, { selector: 'strong' }).then((s) => s.parentElement as HTMLElement);

  it('says the notes failed, with no "(0)", and reads them again', async () => {
    mockReads();
    vi.mocked(listPropertyNotes).mockRejectedValueOnce(new Error('HTTP 500'));
    renderWithReads();

    const notes = await banner('Poznámky');
    expect(screen.getByText('(—)')).toBeInTheDocument();
    expect(screen.queryByText('(0)')).toBeNull();
    fireEvent.click(within(notes).getByRole('button', { name: 'Zkusit znovu' }));
    expect(await screen.findByText('Sousedi jsou hlučni.')).toBeInTheDocument();
    expect(screen.getByText('(1)')).toBeInTheDocument();
  });

  it('says the tags failed instead of offering an empty picker, and reads them again', async () => {
    mockReads();
    vi.mocked(fetchPropertyTagIds).mockRejectedValueOnce(new Error('HTTP 500'));
    renderWithReads();

    const tags = await banner('Štítky');
    expect(screen.queryByRole('button', { name: 'Add tag' })).toBeNull();
    fireEvent.click(within(tags).getByRole('button', { name: 'Zkusit znovu' }));
    expect(await screen.findByRole('button', { name: 'Add tag' })).toBeInTheDocument();
  });

  it('says the collection memberships failed instead of unticking every toggle', async () => {
    mockReads();
    vi.mocked(listCollections).mockResolvedValue({ data: [{ id: 7, name: 'Šortlist' }] } as never);
    vi.mocked(fetchPropertyCollectionMemberSet).mockRejectedValueOnce(new Error('HTTP 500'));
    renderWithReads();

    const collections = await banner('Kolekce');
    expect(screen.queryByRole('button', { name: /Šortlist/ })).toBeNull();
    vi.mocked(fetchPropertyCollectionMemberSet).mockResolvedValue(new Map([[42, [7]]]));
    fireEvent.click(within(collections).getByRole('button', { name: 'Zkusit znovu' }));
    expect(await screen.findByRole('button', { name: /Šortlist/ })).toHaveAttribute('aria-pressed', 'true');
  });
});
