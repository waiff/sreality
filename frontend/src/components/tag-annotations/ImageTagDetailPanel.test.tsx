/* ImageTagDetailPanel — the shared "every tag on ONE image" dialog, opened from
 * both NEW DEDUP pages.
 *
 * Its behaviour is covered where it is used (pages/NewDedupLabeling.test.tsx and
 * pages/NewDedupTaxonomy.test.tsx drive the tri-state writes, the batch actions
 * and the pinned subject block through the real pages). What those cannot state
 * is the dialog contract itself, so this harness renders it over a trigger and
 * asserts the whole of it: focus in, Tab wrap both ways, Escape closing exactly
 * one layer, focus back on the trigger, body scroll lock released.
 *
 * The tag rows are seeded straight into the query cache rather than awaited
 * from the mocked fetch, because the contract helper is synchronous — a panel
 * still on its "Loading…" frame would prove the Tab cycle against its single
 * close glyph and call that a trap.
 */

import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { expectDialogContract } from '@/test/a11y';
import { newDedupImageTagsKey } from '@/lib/newDedupKeys';

import ImageTagDetailPanel from './ImageTagDetailPanel';
import type { NewDedupImageTag } from '@/lib/api';

vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  listNewDedupImageTags: vi.fn(),
  setNewDedupTagAnnotation: vi.fn(),
  bulkSetNewDedupImageTags: vi.fn(),
}));

const IMAGE_ID = 101;

const ROWS: NewDedupImageTag[] = [
  {
    id: 1,
    label: 'interier - kuchyne',
    family: 'interier',
    state: 'untouched',
    updated_at: null,
    source: null,
    excluded_reason: null,
  },
  {
    id: 2,
    label: 'exterier - fasada',
    family: 'exterier',
    state: 'positive',
    updated_at: 't',
    source: 'human',
    excluded_reason: null,
  },
];

/* Mounting IS opening (lib/useDialog): the host renders the panel only while
 * open, and the trigger is where focus has to come back to. */
function Host({ subjectTagId }: { subjectTagId?: number | null } = {}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        all tags
      </button>
      {open && (
        <ImageTagDetailPanel
          imageId={IMAGE_ID}
          onClose={() => setOpen(false)}
          subjectTagId={subjectTagId}
        />
      )}
    </>
  );
}

function renderHost(subjectTagId?: number | null) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  qc.setQueryData(newDedupImageTagsKey(IMAGE_ID), { data: ROWS });
  return render(
    <QueryClientProvider client={qc}>
      <Host subjectTagId={subjectTagId} />
    </QueryClientProvider>,
  );
}

describe('<ImageTagDetailPanel>', () => {
  it('honours the shared modal-dialog contract', () => {
    renderHost();
    const trigger = screen.getByRole('button', { name: 'all tags' });
    expectDialogContract({ open: () => fireEvent.click(trigger), trigger });
  });

  it('names the dialog on the panel, not on the backdrop', () => {
    renderHost();
    fireEvent.click(screen.getByRole('button', { name: 'all tags' }));

    const panel = screen.getByRole('dialog', { name: 'All tags on this image' });
    expect(panel).toHaveAttribute('aria-modal', 'true');
    expect(panel).toHaveTextContent('Image 101 — all tags');
    // The dim above it announces nothing.
    expect(panel.parentElement).toHaveAttribute('role', 'presentation');
  });

  it('closes from the one shared close glyph', () => {
    renderHost();
    fireEvent.click(screen.getByRole('button', { name: 'all tags' }));
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('holds the contract with the subject block pinned above the list too', () => {
    renderHost(2);
    const trigger = screen.getByRole('button', { name: 'all tags' });
    expectDialogContract({ open: () => fireEvent.click(trigger), trigger });
  });
});
