/* "Go to Browse" is a destination, so it is a real anchor built from the shared
 * gesture rules rather than a hand-rolled guard. The guard this replaces tested
 * `e.button === 1` for middle-click (which fires `auxclick`, never `click`, so
 * the arm was dead) and omitted `altKey`, swallowing alt-click — "download this
 * link" on Chrome and Firefox — into an SPA navigation. */
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

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

function setup() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/listing/1']}>
        <ExploreAreaProvider>
          <Opener />
        </ExploreAreaProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
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
