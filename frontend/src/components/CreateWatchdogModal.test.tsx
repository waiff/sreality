/* CreateWatchdogModal — the "save these Browse filters as a watchdog" prompt.
 *
 * The modal-dialog contract (src/test/a11y.ts, over lib/useDialog): named,
 * focus trapped and wrapping both ways, Escape closing exactly ONE layer,
 * focus handed back to the trigger, body scroll released. Before the migration
 * this dialog kept its own `window` Escape listener and defused the backdrop's
 * onClick with a stopPropagation on the panel.
 *
 * The name is pinned separately because it CHANGED: the old aria-label said
 * "Create watchdog", which is the submit button's words, not the heading's. A
 * dialog that announces something other than what the operator is reading is
 * exactly the drift `labelledBy` removes.
 */
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useState } from 'react';

import { expectDialogContract } from '@/test/a11y';
import { DEFAULT_FILTERS, filtersToWatchdogSpec } from '@/lib/filters';
import CreateWatchdogModal from './CreateWatchdogModal';

/* The real producer, not a hand-built literal: Browse only ever hands this
 * modal a spec that came out of filtersToWatchdogSpec. */
const { spec: SPEC } = filtersToWatchdogSpec(DEFAULT_FILTERS);

function Host({ unsupported = [] }: { unsupported?: string[] }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Watch these filters
      </button>
      {/* Mounting IS opening — the shape lib/useDialog's contract requires. */}
      {open ? (
        <CreateWatchdogModal
          spec={SPEC}
          unsupported={unsupported}
          suggestedName="2+kk Praha pod 6M"
          onClose={() => setOpen(false)}
        />
      ) : null}
    </>
  );
}

function renderHost(unsupported?: string[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Host unsupported={unsupported} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('<CreateWatchdogModal> dialog contract', () => {
  it('names itself, traps focus, closes one layer on Escape and gives focus back', () => {
    renderHost();
    const trigger = screen.getByRole('button', { name: 'Watch these filters' });
    expectDialogContract({ trigger, open: () => fireEvent.click(trigger) });
  });

  it('announces the heading the operator is reading, not the submit button words', () => {
    renderHost();
    fireEvent.click(screen.getByRole('button', { name: 'Watch these filters' }));
    expect(screen.getByRole('dialog')).toHaveAccessibleName(
      'Save these filters as a watchdog',
    );
  });

  it('opens with the suggested name focused AND selected, so it can be typed over', () => {
    renderHost();
    fireEvent.click(screen.getByRole('button', { name: 'Watch these filters' }));
    const input = screen.getByRole('textbox', { name: 'Watchdog name' }) as HTMLInputElement;
    expect(document.activeElement).toBe(input);
    expect(input.selectionStart).toBe(0);
    expect(input.selectionEnd).toBe(input.value.length);
  });

  it('does not close on a click that lands inside the panel', () => {
    renderHost();
    fireEvent.click(screen.getByRole('button', { name: 'Watch these filters' }));
    /* The `onClick={(e) => e.stopPropagation()}` this replaces existed only to
     * defuse the backdrop's own onClick. <Dialog> tests the mousedown target
     * instead. */
    fireEvent.mouseDown(screen.getByRole('textbox', { name: 'Watchdog name' }));
    fireEvent.click(screen.getByRole('textbox', { name: 'Watchdog name' }));
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('still surfaces the unwatchable-filters heads-up inside the panel', () => {
    renderHost(['Datum přidání']);
    fireEvent.click(screen.getByRole('button', { name: 'Watch these filters' }));
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveTextContent('Datum přidání');
  });
});
