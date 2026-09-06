/* NewEstimationModal — the "new estimation / new building" popup.
 *
 * Four things pinned here. (1) The dialog and its fields are wired by useId(),
 * not by hard-coded DOM ids, so two mounted providers cannot cross-wire a
 * <label> to the other modal's control — the test renders two and asserts the
 * ids are distinct. (2) The building attachments file input is named by the
 * caption that sits above it; that caption was a sibling <label> with no
 * htmlFor, so it named nothing. (3) The shared modal-dialog contract
 * (src/test/a11y.ts, over lib/useDialog). (4) The two behaviours the migration
 * to <Dialog> had to carry across by hand: initial focus lands on the URL
 * field rather than on the header's close glyph — which is what the primitive
 * would pick, being the first focusable control — and NO dismissal gesture
 * fires while a submit is in flight.
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Link, MemoryRouter } from 'react-router-dom';

import { expectDialogContract } from '@/test/a11y';
import { ROUTES } from '@/lib/routes';
import { NewEstimationProvider, useNewEstimationModal } from './NewEstimationModal';

/* Only previewListingUrl is replaced, and only so the in-flight case has an
 * honest way to STAY in flight: the modal reads `pending` off the mutation, so
 * a promise that never settles is the state under test. */
const previewListingUrl = vi.hoisted(() => vi.fn());
vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  previewListingUrl,
}));

function Opener() {
  const { open } = useNewEstimationModal();
  return (
    <>
      <button type="button" onClick={() => open()}>
        Open estimation
      </button>
      <button type="button" onClick={() => open({ url: 'https://www.sreality.cz/detail/prodej/byt/1' })}>
        Open prefilled
      </button>
    </>
  );
}

function renderHost(count = 1) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const providers = Array.from({ length: count }, (_, i) => (
    <NewEstimationProvider key={i}>
      <Opener />
      {/* Below the provider on purpose: this is a navigation started from a
        * page the provider sits ABOVE, which is the shape useCloseOnNavigation
        * exists for. */}
      <Link to={ROUTES.browse.build()}>go elsewhere</Link>
    </NewEstimationProvider>
  ));
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/estimations']}>{providers}</MemoryRouter>
    </QueryClientProvider>,
  );
}

const renderModals = (count = 1) => {
  const utils = renderHost(count);
  screen.getAllByRole('button', { name: 'Open estimation' }).forEach((b) => fireEvent.click(b));
  return utils;
};

describe('<NewEstimationModal>', () => {
  it('names the URL field and the dialog', () => {
    renderModals();
    expect(screen.getByRole('textbox', { name: 'Listing URL' })).toBeInTheDocument();
    expect(screen.getByRole('dialog')).toHaveAccessibleName('Where is the listing?');
  });

  it('names the operator-context textareas', () => {
    renderModals();
    expect(screen.getByRole('textbox', { name: 'Special instructions' })).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Property context' })).toBeInTheDocument();
  });

  it('names the building attachments picker by its visible caption', () => {
    renderModals();
    fireEvent.click(screen.getByRole('radio', { name: /Building/ }));
    expect(
      screen.getByLabelText('Attachments — floor plans, photos, drawings'),
    ).toHaveAttribute('type', 'file');
  });

  it('gives two mounted modals distinct ids, so neither steals the other label', () => {
    renderModals(2);
    const [a, b] = screen.getAllByRole('textbox', { name: 'Listing URL' });
    expect(a.id).not.toBe('');
    expect(a.id).not.toBe(b.id);
    const dialogs = screen.getAllByRole('dialog');
    expect(dialogs[0].getAttribute('aria-labelledby')).not.toBe(
      dialogs[1].getAttribute('aria-labelledby'),
    );
  });
});

describe('<NewEstimationModal> dialog contract', () => {
  it('names itself, traps focus, closes one layer on Escape and gives focus back', () => {
    renderHost();
    const trigger = screen.getByRole('button', { name: 'Open estimation' });
    expectDialogContract({ trigger, open: () => fireEvent.click(trigger) });
  });

  it('puts initial focus on the URL field, not on the close glyph in front of it', () => {
    renderModals();
    expect(document.activeElement).toBe(
      screen.getByRole('textbox', { name: 'Listing URL' }),
    );
  });

  it('opens on the primary action when the URL is prefilled and its field is not rendered', () => {
    renderHost();
    fireEvent.click(screen.getByRole('button', { name: 'Open prefilled' }));
    expect(screen.queryByRole('textbox', { name: 'Listing URL' })).not.toBeInTheDocument();
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Estimate' }));
  });

  it('leaves no scroll lock behind when the page underneath navigates away', () => {
    renderHost();
    const trigger = screen.getByRole('button', { name: 'Open estimation' });
    trigger.focus();
    fireEvent.click(trigger);
    expect(document.body.style.overflow).toBe('hidden');

    /* The defect useCloseOnNavigation fixes: the provider mounts above
     * <Outlet>, so without it this left the modal — and this lock — sitting
     * over a page the operator never asked to see it over. */
    fireEvent.click(screen.getByRole('link', { name: 'go elsewhere' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(document.body.style.overflow).toBe('');
  });
});

describe('<NewEstimationModal> in-flight guard', () => {
  /* The pre-migration modal gated each dismissal gesture separately (a
   * `!pending` arm in its own Escape listener, another in the backdrop
   * mousedown, `disabled` on the close button). The gating now lives in ONE
   * guarded onClose, so all three are asserted against that one place. */
  async function submitAndHang() {
    previewListingUrl.mockReturnValue(new Promise(() => {}));
    renderModals();
    fireEvent.change(screen.getByRole('textbox', { name: 'Listing URL' }), {
      target: { value: 'https://www.sreality.cz/detail/pronajem/byt/1' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Estimate' }));
    /* react-query settles `isPending` a microtask later, so waiting on the
     * button's in-flight label is what makes this test about the guard rather
     * than about a race it would otherwise win by accident. */
    await screen.findByText('Scraping…');
    return screen.getByRole('dialog');
  }

  it('ignores Escape while a submit is in flight', async () => {
    const dialog = await submitAndHang();
    fireEvent.keyDown(document.activeElement ?? document, { key: 'Escape' });
    expect(dialog).toBeInTheDocument();
    expect(document.body.style.overflow).toBe('hidden');
  });

  it('ignores the close glyph while a submit is in flight', async () => {
    const dialog = await submitAndHang();
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(dialog).toBeInTheDocument();
  });
});
