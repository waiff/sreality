/* <CityPicker> — the modal contract, on the emptiest of the thirteen.
 *
 * Before the migration this dialog announced nothing at all: no role, no name,
 * no aria-modal, no Escape listener, no focus trap, no initial or restored
 * focus, no scroll lock. The only dismissal it had was an `onClick` on the
 * backdrop, undone one level in by a panel-wide `stopPropagation`. Datasets
 * mounts it from two forms, so every one of those held twice.
 *
 * The tree read is SEEDED into the query cache rather than mocked: the picker's
 * query carries `staleTime: Infinity`, so a primed cache means no fetch fires
 * and the whole dialog is there on the first render — every assertion below is
 * synchronous, with no act() gap for a late resolution to land in.
 */

import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { expectDialogContract } from '@/test/a11y';
import { priceStatsKeys, type ObecNode } from '@/lib/priceStats';
import CityPicker from './CityPicker';

/* One kraj with an okres and an obec under it, plus a kraj with an obec
 * hanging straight off it (Praha) — the two shapes buildTree distinguishes. */
const TREE: ObecNode[] = [
  { id: 1, level: 'kraj', name: 'Jihomoravský kraj', parent_id: null, population: null, sreality_id: null },
  { id: 2, level: 'okres', name: 'Brno-město', parent_id: 1, population: null, sreality_id: null },
  { id: 3, level: 'obec', name: 'Brno', parent_id: 2, population: 381_000, sreality_id: 5000 },
  { id: 4, level: 'kraj', name: 'Praha', parent_id: null, population: null, sreality_id: null },
  { id: 5, level: 'obec', name: 'Praha', parent_id: 4, population: 1_300_000, sreality_id: 1 },
];

function seededClient(): QueryClient {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(priceStatsKeys.obecTree, TREE);
  return qc;
}

type ApplySpy = (ids: number[], min: number | null, max: number | null) => void;

function Host({ onApply }: { onApply?: ApplySpy }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        open-picker
      </button>
      {open && (
        <CityPicker
          initialObecIds={[]}
          initialMin={null}
          initialMax={null}
          onClose={() => setOpen(false)}
          onApply={(ids, lo, hi) => {
            onApply?.(ids, lo, hi);
            setOpen(false);
          }}
        />
      )}
    </>
  );
}

function renderHost(onApply?: ApplySpy) {
  const view = render(
    <QueryClientProvider client={seededClient()}>
      <Host onApply={onApply} />
    </QueryClientProvider>,
  );
  return { ...view, trigger: screen.getByRole('button', { name: 'open-picker' }) };
}

describe('<CityPicker> dialog contract', () => {
  it('names itself, traps focus, closes one layer on Escape and gives focus back', () => {
    const { trigger } = renderHost();

    expectDialogContract({ trigger, open: () => fireEvent.click(trigger) });
  });

  it('is named by the heading the operator reads, not by a second copy of the words', () => {
    const { trigger } = renderHost();
    fireEvent.click(trigger);

    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAccessibleName('Select municipalities');
    /* The name is the <h2> itself: aria-labelledby, so it cannot drift from
     * what is on screen. */
    const heading = screen.getByRole('heading', { name: 'Select municipalities' });
    expect(dialog.getAttribute('aria-labelledby')).toBe(heading.id);
    expect(dialog).toHaveAttribute('aria-modal', 'true');
  });

  it('closes on a press that lands on the backdrop, and not on one inside the panel', () => {
    const { trigger } = renderHost();
    fireEvent.click(trigger);

    const dialog = screen.getByRole('dialog');
    const backdrop = dialog.parentElement as HTMLElement;
    /* Presentational: the dim is not the dialog. The old version put the
     * dismissal here as a plain onClick and needed a panel-wide
     * stopPropagation to keep a click in the panel from closing it. */
    expect(backdrop).toHaveAttribute('role', 'presentation');

    fireEvent.mouseDown(dialog);
    expect(screen.getByRole('dialog')).toBeInTheDocument();

    fireEvent.mouseDown(backdrop);
    expect(screen.queryByRole('dialog')).toBeNull();
  });
});

/* The create-dataset mount sits inside a <form>, and no button in the picker
 * sets a `type` — so each defaulted to `submit`. "Cancel" submitted the form
 * behind the modal. Portalled, the buttons have no form ancestor in the DOM. */
describe('<CityPicker> inside a form', () => {
  function FormHost({ onSubmit }: { onSubmit: () => void }) {
    const [open, setOpen] = useState(false);
    return (
      <form
        onSubmit={(e) => {
          e.preventDefault();
          onSubmit();
        }}
      >
        <button type="button" onClick={() => setOpen(true)}>
          open-picker
        </button>
        <button type="submit">create dataset</button>
        {open && (
          <CityPicker
            initialObecIds={[]}
            initialMin={null}
            initialMax={null}
            onClose={() => setOpen(false)}
            onApply={() => setOpen(false)}
          />
        )}
      </form>
    );
  }

  it('cancels instead of submitting the form it was mounted in', () => {
    const onSubmit = vi.fn();
    const { container } = render(
      <QueryClientProvider client={seededClient()}>
        <FormHost onSubmit={onSubmit} />
      </QueryClientProvider>,
    );
    const form = container.querySelector('form') as HTMLFormElement;

    fireEvent.click(screen.getByRole('button', { name: 'open-picker' }));
    expect(form.contains(screen.getByRole('dialog'))).toBe(false);

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(onSubmit).not.toHaveBeenCalled();

    /* Non-vacuity: this jsdom DOES submit a form from a click on a submit
     * button, so the assertion above is about the portal and not about a
     * gesture jsdom ignores. */
    fireEvent.click(screen.getByRole('button', { name: 'create dataset' }));
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });
});

/* The picker's own job, unchanged by the migration. */
describe('<CityPicker> selection', () => {
  it('selects every eligible obec, drops the ones a tightened bound excludes, and applies', () => {
    const onApply = vi.fn();
    const { trigger } = renderHost(onApply);
    fireEvent.click(trigger);

    fireEvent.click(screen.getByRole('button', { name: 'Select all in range' }));
    expect(screen.getByText('2 selected')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('min'), { target: { value: '500000' } });
    expect(screen.getByText('1 selected')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Use 1 municipalities' }));
    expect(onApply).toHaveBeenCalledWith([5], 500_000, null);
    expect(screen.queryByRole('dialog')).toBeNull();
  });
});
