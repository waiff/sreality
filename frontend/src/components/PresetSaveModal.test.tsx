/* PresetSaveModal — the name + colour dialog behind "Save preset".
 *
 * The modal-dialog contract (src/test/a11y.ts, over lib/useDialog): named,
 * focus trapped and wrapping both ways, Escape closing exactly ONE layer,
 * focus handed back to the trigger, body scroll released. Before the migration
 * this dialog announced itself with a hand-written aria-label, kept its own
 * `window` Escape listener, and made every click inside the panel call
 * stopPropagation to stop the BACKDROP's own onClick from closing it.
 *
 * The one behaviour the primitive does not supply, and this file therefore
 * pins separately: the name is pre-SELECTED on open, so an operator renaming a
 * preset can type straight over the old name.
 */
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';

import { expectDialogContract } from '@/test/a11y';
import PresetSaveModal, { type PresetSaveModalProps } from './PresetSaveModal';

function Host({ onSubmit = vi.fn() }: { onSubmit?: PresetSaveModalProps['onSubmit'] }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Save preset
      </button>
      {/* Mounting IS opening — the shape lib/useDialog's contract requires. */}
      {open ? (
        <PresetSaveModal
          title="Update preset"
          initialName="2+kk Praha pod 6M"
          initialColor={null}
          submitLabel="Update preset"
          showMapAreaToggle
          initialIncludeMapArea={false}
          busy={false}
          error={null}
          onSubmit={onSubmit}
          onClose={() => setOpen(false)}
        />
      ) : null}
    </>
  );
}

describe('<PresetSaveModal> dialog contract', () => {
  it('names itself, traps focus, closes one layer on Escape and gives focus back', () => {
    render(<Host />);
    const trigger = screen.getByRole('button', { name: 'Save preset' });
    expectDialogContract({ trigger, open: () => fireEvent.click(trigger) });
  });

  it('takes its name from the visible heading rather than a second string', () => {
    render(<Host />);
    fireEvent.click(screen.getByRole('button', { name: 'Save preset' }));
    expect(screen.getByRole('dialog')).toHaveAccessibleName('Update preset');
  });

  it('opens with the current name focused AND selected, so it can be typed over', () => {
    render(<Host />);
    fireEvent.click(screen.getByRole('button', { name: 'Save preset' }));
    const input = screen.getByRole('textbox', { name: 'Preset name' }) as HTMLInputElement;
    expect(document.activeElement).toBe(input);
    expect(input.selectionStart).toBe(0);
    expect(input.selectionEnd).toBe(input.value.length);
  });

  it('does not close on a click that lands inside the panel', () => {
    render(<Host />);
    fireEvent.click(screen.getByRole('button', { name: 'Save preset' }));
    /* The `onClick={(e) => e.stopPropagation()}` this replaces existed only to
     * defuse the backdrop's own onClick. <Dialog> tests the mousedown target
     * instead, so an in-panel click is a non-event without the panel having to
     * swallow it. */
    fireEvent.mouseDown(screen.getByRole('textbox', { name: 'Preset name' }));
    fireEvent.click(screen.getByRole('textbox', { name: 'Preset name' }));
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });
});
