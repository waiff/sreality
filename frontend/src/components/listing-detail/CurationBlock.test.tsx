/* CurationBlock — accessible names for the three unlabelled curation inputs.
 *
 * All three were reachable only through a placeholder (or, for the note
 * editor, through nothing at all while autoFocus dropped the caret into it).
 * The list/membership reads are mocked so no network call fires.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
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
  uploadNoteAttachment: vi.fn(),
  fetchNoteAttachmentBlob: vi.fn(),
  deleteNoteAttachment: vi.fn(),
}));

vi.mock('@/lib/queries', async (orig) => ({
  ...(await orig<typeof import('@/lib/queries')>()),
  fetchPropertyCollectionMemberSet: vi.fn(),
  fetchPropertyTagIds: vi.fn(),
}));

import {
  createPropertyNote,
  deleteNoteAttachment,
  deletePropertyNote,
  fetchNoteAttachmentBlob,
  listCollections,
  listPropertyNotes,
  listTags,
  uploadNoteAttachment,
} from '@/lib/api';
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

/* Files on notes (migration 592): dropped or picked into the composer they wait for the note;
 * dropped onto a saved note they go onto that note; a refused file is named, never sent. */
describe('<CurationBlock> note attachments', () => {
  const pdf = () => new File(['%PDF'], 'plan.pdf', { type: 'application/pdf' });
  const dropOn = (el: HTMLElement, files: File[]) => {
    const dataTransfer = { files, types: ['Files'], dropEffect: 'none' };
    fireEvent.dragEnter(el, { dataTransfer });
    fireEvent.dragOver(el, { dataTransfer });
    fireEvent.drop(el, { dataTransfer });
  };
  const savedNote = () => screen.findByRole('group', { name: /^Note from/ });

  beforeEach(() => {
    vi.clearAllMocks();
    URL.createObjectURL = vi.fn(() => 'blob:x');
    URL.revokeObjectURL = vi.fn();
  });

  it('stages a file dropped on the Notes section and uploads it once the note is saved', async () => {
    const user = userEvent.setup();
    mockReads();
    vi.mocked(createPropertyNote).mockResolvedValue({ ...NOTE, id: 12 } as never);
    vi.mocked(uploadNoteAttachment).mockResolvedValue({} as never);
    renderWithReads();
    const box = await screen.findByRole('textbox', { name: 'Notes' });

    const file = pdf();
    dropOn(box, [file]);
    expect(within(screen.getByRole('list', { name: 'Files to attach' })).getByText('plan.pdf'))
      .toBeInTheDocument();
    expect(uploadNoteAttachment).not.toHaveBeenCalled();

    await user.type(box, 'Půdorys od makléře.');
    await user.click(screen.getByRole('button', { name: 'Save note' }));
    await waitFor(() => expect(uploadNoteAttachment).toHaveBeenCalledWith(42, 12, file));
    expect(createPropertyNote).toHaveBeenCalledWith(42, 'Půdorys od makléře.', 900, 900);
    await waitFor(() => expect(screen.queryByRole('list', { name: 'Files to attach' })).toBeNull());
  });

  it('saves a files-only note under its file names', async () => {
    const user = userEvent.setup();
    mockReads();
    vi.mocked(createPropertyNote).mockResolvedValue({ ...NOTE, id: 12 } as never);
    vi.mocked(uploadNoteAttachment).mockResolvedValue({} as never);
    renderWithReads();
    await screen.findByRole('textbox', { name: 'Notes' });

    await user.upload(screen.getAllByTestId('attach-input')[0], pdf());
    await user.click(screen.getByRole('button', { name: 'Save note' }));
    await waitFor(() => expect(createPropertyNote).toHaveBeenCalledWith(
      42, 'Attached: plan.pdf', 900, 900));
  });

  it('puts a file dropped on a saved note onto that note, not into the composer', async () => {
    mockReads();
    vi.mocked(uploadNoteAttachment).mockResolvedValue({} as never);
    const { invalidate } = renderWithReads();
    const file = pdf();

    dropOn(await savedNote(), [file]);
    await waitFor(() => expect(uploadNoteAttachment).toHaveBeenCalledWith(42, NOTE.id, file));
    expect(createPropertyNote).not.toHaveBeenCalled();
    expect(screen.queryByRole('list', { name: 'Files to attach' })).toBeNull();
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({
      queryKey: curationKeys.propertyNotes(42) }));
  });

  it('names a refused file and never sends it', async () => {
    mockReads();
    renderWithReads();
    dropOn(await screen.findByRole('textbox', { name: 'Notes' }),
      [new File(['<svg/>'], 'drawing.svg', { type: 'image/svg+xml' })]);
    expect(await screen.findByText('drawing.svg: this file type cannot be attached'))
      .toBeInTheDocument();
    expect(screen.queryByRole('list', { name: 'Files to attach' })).toBeNull();
    expect(uploadNoteAttachment).not.toHaveBeenCalled();
  });

  it('shows a saved note’s image as a thumbnail, downloads the rest, and asks before removing',
    async () => {
      const user = userEvent.setup();
      mockReads();
      const at = '2026-05-02T00:00:00+00:00';
      vi.mocked(listPropertyNotes).mockResolvedValue({ data: [{ ...NOTE, attachments: [
        { id: 1, note_id: 11, filename: 'foto.png', mime_type: 'image/png', byte_size: 2048,
          created_at: at },
        { id: 2, note_id: 11, filename: 'plan.pdf', mime_type: 'application/pdf',
          byte_size: 4096, created_at: at },
      ] }] } as never);
      vi.mocked(fetchNoteAttachmentBlob).mockResolvedValue(new Blob(['x']));
      vi.mocked(deleteNoteAttachment).mockResolvedValue({ deleted: true } as never);
      const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
      renderWithReads();

      const files = await screen.findByRole('list', { name: 'Attachments' });
      expect(await within(files).findByRole('img', { name: 'foto.png' })).toBeInTheDocument();
      expect(fetchNoteAttachmentBlob).toHaveBeenCalledWith(42, 11, 1);

      await user.click(within(files).getByRole('button', { name: 'Download plan.pdf' }));
      await waitFor(() => expect(fetchNoteAttachmentBlob).toHaveBeenCalledWith(42, 11, 2));
      await waitFor(() => expect(click).toHaveBeenCalled());

      await user.click(within(files).getByRole('button', { name: 'Remove plan.pdf' }));
      expect(deleteNoteAttachment).not.toHaveBeenCalled();
      expect(screen.getByText('Remove plan.pdf?')).toBeInTheDocument();
      await user.click(screen.getByRole('button', { name: 'Remove' }));
      await waitFor(() => expect(deleteNoteAttachment).toHaveBeenCalledWith(42, 11, 2));
      click.mockRestore();
    });
});
