/* TaxonomyManageModal — the "Modify labels" dialog for the NEW DEDUP taxonomy.
 *
 * Props-only, so the harness is the component itself. Pins the two fields that
 * a placeholder used to "name": the add field (a visible "New label" caption)
 * and the inline rename field, which is anonymous exactly when it autofocuses
 * because it REPLACES the label text it would otherwise be named by. The
 * rename field reuses TagDefinitionList's wording so one query names the
 * affordance on both surfaces.
 *
 * Since W6b it also runs on <Dialog>, so the shared modal contract
 * (src/test/a11y.ts) is asserted against it — and, separately, that the add
 * field still owns initial focus. The dialog primitive parks focus on the first
 * focusable control, which here is the close glyph; this dialog is opened in
 * order to type, so the component moves it. A regression there is invisible to
 * the contract, which only asks that focus land somewhere inside.
 */

import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import { expectDialogContract } from '@/test/a11y';

import TaxonomyManageModal from './TaxonomyManageModal';
import type { TaxonomyManageModalProps } from './TaxonomyManageModal';
import type { NewDedupTag } from '@/lib/api';

const tag = (over: Partial<NewDedupTag> = {}): NewDedupTag => ({
  id: 1,
  label: 'interier - kuchyne',
  priority: false,
  ready_for_training: false,
  positive_count: 4,
  negative_count: 2,
  excluded_count: 0,
  ...over,
} as NewDedupTag);

const props = (over: Partial<TaxonomyManageModalProps> = {}): TaxonomyManageModalProps => ({
  labels: [tag()],
  onClose: vi.fn(),
  newLabelText: '',
  onNewLabelTextChange: vi.fn(),
  onAdd: vi.fn(),
  addPending: false,
  onRename: vi.fn(),
  renamePending: false,
  onRemove: vi.fn(),
  removePending: false,
  onSetFlags: vi.fn(),
  flagsPending: false,
  ...over,
});

/* Mounting IS opening (lib/useDialog): the host renders the modal only while
 * open, and the trigger is where focus has to come back to. */
function Host() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        open the label manager
      </button>
      {open && <TaxonomyManageModal {...props({ onClose: () => setOpen(false) })} />}
    </>
  );
}

describe('<TaxonomyManageModal>', () => {
  it('names the add field "New label" and keeps the Add button its own name', () => {
    render(<TaxonomyManageModal {...props()} />);
    expect(screen.getByRole('textbox', { name: 'New label' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add label' })).toBeInTheDocument();
  });

  it('still routes typing in the named add field to onNewLabelTextChange', () => {
    const onNewLabelTextChange = vi.fn();
    render(<TaxonomyManageModal {...props({ onNewLabelTextChange })} />);
    fireEvent.change(screen.getByRole('textbox', { name: 'New label' }), {
      target: { value: 'interier - koupelna' },
    });
    expect(onNewLabelTextChange).toHaveBeenCalledWith('interier - koupelna');
  });

  it('Escape in the rename field reverts the draft and leaves the dialog open', () => {
    // The field claims the key (preventDefault), so lib/useDialog does not
    // also close the modal — before, one Escape did both.
    const onClose = vi.fn();
    render(<TaxonomyManageModal {...props({ onClose })} />);
    fireEvent.click(screen.getByRole('button', { name: 'rename' }));
    const field = screen.getByRole('textbox', { name: 'New tag label' });
    fireEvent.change(field, { target: { value: 'something else' } });
    fireEvent.keyDown(field, { key: 'Escape' });
    expect(screen.queryByRole('textbox', { name: 'New tag label' })).not.toBeInTheDocument();
    expect(screen.getByText('interier - kuchyne')).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('names the inline rename field, which replaces the row label it would be named by', () => {
    render(<TaxonomyManageModal {...props()} />);
    fireEvent.click(screen.getByRole('button', { name: 'rename' }));
    const field = screen.getByRole('textbox', { name: 'New tag label' });
    expect(field).toHaveAccessibleName('New tag label');
    expect(field).toHaveValue('interier - kuchyne');
  });

  it('honours the shared modal-dialog contract', () => {
    render(<Host />);
    const trigger = screen.getByRole('button', { name: 'open the label manager' });
    expectDialogContract({ open: () => fireEvent.click(trigger), trigger });
  });

  it('opens with focus in the add field, not on the close glyph', () => {
    render(<Host />);
    fireEvent.click(screen.getByRole('button', { name: 'open the label manager' }));
    expect(screen.getByRole('textbox', { name: 'New label' })).toHaveFocus();
  });

  it('closes from the one shared close glyph', () => {
    render(<Host />);
    fireEvent.click(screen.getByRole('button', { name: 'open the label manager' }));
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
});
