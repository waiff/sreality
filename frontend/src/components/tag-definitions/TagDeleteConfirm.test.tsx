/* TagDeleteConfirm — the destructive confirm for deleting a tag.
 *
 * Props-only, so the harness is the component plus a trigger to open it from
 * (mounting IS opening — lib/useDialog). What is pinned here is the part of the
 * dialog contract this one dialog cannot inherit: APG asks a destructive
 * confirm to open on its LEAST destructive control, and <Dialog> parks initial
 * focus on the FIRST focusable one — which, whenever the acknowledgement gate
 * is present, is the checkbox and not Cancel. Both shapes are asserted, because
 * the gate's presence depends on the tag's human_count.
 *
 * Also pinned: `pending` still guards the close path. Escape and the backdrop
 * used to be two handlers with their own copy of that guard; they are one now,
 * and a delete already in flight must still not be dismissible.
 */

import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import { expectDialogContract } from '@/test/a11y';

import TagDeleteConfirm from './TagDeleteConfirm';
import type { NewDedupTag } from '@/lib/api';

const tag = (over: Partial<NewDedupTag> = {}): NewDedupTag =>
  ({
    id: 1,
    label: 'interier - kuchyne',
    family: 'interier',
    active: true,
    priority: false,
    ready_for_training: false,
    positive_count: 12,
    negative_count: 8,
    excluded_count: 5,
    human_count: 25,
    machine_count: 0,
    backfill_count: 1300,
    ...over,
  }) as NewDedupTag;

function Host({
  t = tag(),
  pending = false,
  onConfirm = () => {},
}: {
  t?: NewDedupTag;
  pending?: boolean;
  onConfirm?: () => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        open the delete confirm
      </button>
      {open && (
        <TagDeleteConfirm
          tag={t}
          definitionVersion={2}
          savedVersionCount={3}
          hasUnsavedDraft={false}
          onCancel={() => setOpen(false)}
          onConfirm={onConfirm}
          pending={pending}
          error={null}
        />
      )}
    </>
  );
}

const openIt = () => {
  const trigger = screen.getByRole('button', { name: 'open the delete confirm' });
  fireEvent.click(trigger);
  return trigger;
};

describe('<TagDeleteConfirm>', () => {
  it('honours the shared modal-dialog contract', () => {
    render(<Host />);
    const trigger = screen.getByRole('button', { name: 'open the delete confirm' });
    expectDialogContract({ open: () => fireEvent.click(trigger), trigger });
  });

  it('opens with focus on Cancel, never on the destructive action', () => {
    render(<Host />);
    openIt();
    expect(screen.getByRole('button', { name: 'Cancel' })).toHaveFocus();
    expect(screen.getByRole('button', { name: 'Delete tag' })).not.toHaveFocus();
  });

  it('opens on Cancel even with the acknowledgement gate in front of it', () => {
    // The gate is the FIRST focusable control in the panel, so this is the
    // shape where the primitive's own initial focus would land elsewhere.
    render(<Host t={tag({ human_count: 25 })} />);
    openIt();
    expect(screen.getByRole('checkbox')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Cancel' })).toHaveFocus();
  });

  it('opens on Cancel for a tag with no human decisions and so no gate', () => {
    render(<Host t={tag({ human_count: 0 })} />);
    openIt();
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Cancel' })).toHaveFocus();
  });

  it('does not dismiss a delete that is already in flight', () => {
    const onConfirm = vi.fn();
    render(<Host pending onConfirm={onConfirm} />);
    openIt();

    fireEvent.keyDown(document.body, { key: 'Escape' });
    expect(screen.getByRole('dialog', { name: 'Delete tag' })).toBeInTheDocument();

    // The backdrop is the panel's parent (a portalled sibling of nothing else).
    const backdrop = screen.getByRole('dialog', { name: 'Delete tag' }).parentElement!;
    fireEvent.mouseDown(backdrop);
    expect(screen.getByRole('dialog', { name: 'Delete tag' })).toBeInTheDocument();
    expect(onConfirm).not.toHaveBeenCalled();
  });
});
