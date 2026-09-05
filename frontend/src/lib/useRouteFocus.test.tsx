/* Where focus goes on a route change. jsdom tracks document.activeElement and
 * fires real focus events, so this needs no browser. */
import { describe, expect, it } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { Link, MemoryRouter, Route, Routes } from 'react-router-dom';
import { MAIN_ID, useRouteFocus } from './useRouteFocus';

function Shell() {
  useRouteFocus();
  return (
    <>
      <nav>
        {/* eslint-disable-next-line no-restricted-syntax -- synthetic routes for a routing-hook harness, not app destinations */}
        <Link to="/a">to a</Link>
        {/* eslint-disable-next-line no-restricted-syntax -- synthetic routes for a routing-hook harness, not app destinations */}
        <Link to="/b">to b</Link>
        {/* eslint-disable-next-line no-restricted-syntax -- synthetic routes for a routing-hook harness, not app destinations */}
        <Link to="/a?tab=2">a with query</Link>
      </nav>
      <main id={MAIN_ID} tabIndex={-1} data-testid="main">
        <Routes>
          <Route path="/a" element={<button type="button">on a</button>} />
          <Route path="/b" element={<button type="button">on b</button>} />
        </Routes>
      </main>
    </>
  );
}

function setup(initial = '/a') {
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <Shell />
    </MemoryRouter>,
  );
}

describe('useRouteFocus', () => {
  it('does NOT move focus on the first render, so a deep link is not stolen', () => {
    setup();
    expect(document.activeElement).not.toBe(screen.getByTestId('main'));
  });

  it('moves focus to <main> after a link navigation', () => {
    setup();
    const link = screen.getByRole('link', { name: 'to b' });
    link.focus();
    expect(document.activeElement).toBe(link);
    act(() => {
      fireEvent.click(link);
    });
    expect(screen.getByRole('button', { name: 'on b' })).toBeInTheDocument();
    expect(document.activeElement).toBe(screen.getByTestId('main'));
  });

  it('leaves focus alone on a query-string change — that is in-page state, not a navigation', () => {
    setup();
    // First a real navigation so the hook is past its first-render guard.
    act(() => {
      fireEvent.click(screen.getByRole('link', { name: 'to b' }));
    });
    const back = screen.getByRole('link', { name: 'to a' });
    act(() => {
      fireEvent.click(back);
    });
    expect(document.activeElement).toBe(screen.getByTestId('main'));

    const query = screen.getByRole('link', { name: 'a with query' });
    query.focus();
    act(() => {
      fireEvent.click(query);
    });
    // Same pathname (/a), only the search changed: focus stays where it was.
    expect(document.activeElement).toBe(query);
  });
});
