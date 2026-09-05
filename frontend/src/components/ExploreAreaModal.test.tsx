/* "Go to Browse" is a destination, so it is a real anchor built from the shared
 * gesture rules rather than a hand-rolled guard. The guard this replaces tested
 * `e.button === 1` for middle-click (which fires `auxclick`, never `click`, so
 * the arm was dead) and omitted `altKey`, swallowing alt-click — "download this
 * link" on Chrome and Firefox — into an SPA navigation.
 *
 * The link no longer carries an onClick at all: closing on navigation is the
 * PROVIDER's job (lib/useCloseOnNavigation.ts), so the modifier-click cases
 * below now pass for a structural reason — a modifier click does not navigate,
 * so the pathname never changes — instead of because one link remembered to
 * check. The dialog contract itself comes from src/test/a11y.ts. */
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { expectDialogContract } from '@/test/a11y';
import { ExploreAreaProvider, useExploreAreaModal } from './ExploreAreaModal';

vi.mock('@/lib/queries', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/queries')>()),
}));

function Opener() {
  const { open } = useExploreAreaModal();
  return (
    <button
      type="button"
      onClick={() =>
        open({
          lat: 50.08,
          lng: 14.42,
          categoryMain: 'byt',
          categoryType: 'prodej',
          disposition: null,
          label: 'Okoli nemovitosti',
        })
      }
    >
      open-modal
    </button>
  );
}

function renderHost() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/listing/1']}>
        <ExploreAreaProvider>
          <Opener />
        </ExploreAreaProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function setup() {
  const view = renderHost();
  fireEvent.click(screen.getByRole('button', { name: 'open-modal' }));
  return view;
}

describe('<ExploreAreaModal> "Go to Browse"', () => {
  it('is a real link to Browse, not a button with a navigate handler', () => {
    setup();
    const link = screen.getByRole('link', { name: /Go to Browse/ });
    expect(link.getAttribute('href')).toMatch(/^\/browse(\?|$)/);
  });

  it('closes the modal on a plain click', () => {
    setup();
    const link = screen.getByRole('link', { name: /Go to Browse/ });
    fireEvent.click(link);
    expect(screen.queryByRole('link', { name: /Go to Browse/ })).toBeNull();
  });

  it('leaves the modal open on a modifier click, so the new tab does not also close it', () => {
    setup();
    const link = screen.getByRole('link', { name: /Go to Browse/ });
    fireEvent.click(link, { ctrlKey: true });
    expect(screen.getByRole('link', { name: /Go to Browse/ })).toBeInTheDocument();
  });

  it('leaves the modal open on an alt click — the gesture the old guard swallowed', () => {
    setup();
    const link = screen.getByRole('link', { name: /Go to Browse/ });
    fireEvent.click(link, { altKey: true });
    expect(screen.getByRole('link', { name: /Go to Browse/ })).toBeInTheDocument();
  });
});

describe('<ExploreAreaModal> dialog contract', () => {
  it('names itself, traps focus, closes one layer on Escape and gives focus back', () => {
    renderHost();
    const trigger = screen.getByRole('button', { name: 'open-modal' });
    expectDialogContract({ trigger, open: () => fireEvent.click(trigger) });
  });

  it('leaves no scroll lock behind when an in-modal link navigates away', () => {
    renderHost();
    const trigger = screen.getByRole('button', { name: 'open-modal' });
    trigger.focus();
    fireEvent.click(trigger);
    expect(document.body.style.overflow).toBe('hidden');

    /* The defect: the provider mounts above <Outlet>, so before
     * useCloseOnNavigation this left the modal — and this lock — over a
     * different page. The link itself has no close handler any more. */
    fireEvent.click(screen.getByRole('link', { name: /Go to Browse/ }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(document.body.style.overflow).toBe('');
  });
});
