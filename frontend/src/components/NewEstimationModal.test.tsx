/* NewEstimationModal — the "new estimation / new building" popup.
 *
 * Two things pinned here. (1) The dialog and its fields are wired by useId(),
 * not by hard-coded DOM ids, so two mounted providers cannot cross-wire a
 * <label> to the other modal's control — the test renders two and asserts the
 * ids are distinct. (2) The building attachments file input is named by the
 * caption that sits above it; that caption was a sibling <label> with no
 * htmlFor, so it named nothing.
 */

import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import { NewEstimationProvider, useNewEstimationModal } from './NewEstimationModal';

function Opener() {
  const { open } = useNewEstimationModal();
  return (
    <button type="button" onClick={() => open()}>
      Open estimation
    </button>
  );
}

const renderModals = (count = 1) => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const providers = Array.from({ length: count }, (_, i) => (
    <NewEstimationProvider key={i}>
      <Opener />
    </NewEstimationProvider>
  ));
  const utils = render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{providers}</MemoryRouter>
    </QueryClientProvider>,
  );
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
