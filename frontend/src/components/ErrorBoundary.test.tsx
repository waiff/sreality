/* ErrorBoundary — the SPA's render-error net.
 *
 * Behaviour pinned:
 *   - renders children unchanged when nothing throws
 *   - a throwing child is contained: a custom `fallback` replaces only the
 *     boundary's subtree (this is what keeps a recharts crash from
 *     white-screening the whole Listing Detail page)
 *   - with no `fallback`, the default notice renders and surfaces the message
 */

import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

import ErrorBoundary from './ErrorBoundary';

function Boom(): never {
  throw new Error('kaboom');
}

describe('<ErrorBoundary>', () => {
  it('renders children when nothing throws', () => {
    render(
      <ErrorBoundary>
        <p>healthy</p>
      </ErrorBoundary>,
    );
    expect(screen.getByText('healthy')).toBeInTheDocument();
  });

  it('renders the custom fallback in place of a crashed subtree', () => {
    // React logs the caught error to console.error; silence it for a clean run.
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    render(
      <ErrorBoundary fallback={<span>chart unavailable</span>}>
        <Boom />
      </ErrorBoundary>,
    );
    expect(screen.getByText('chart unavailable')).toBeInTheDocument();
    spy.mockRestore();
  });

  it('renders the default notice with the error message when no fallback is given', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    );
    expect(screen.getByText('This page hit an error')).toBeInTheDocument();
    expect(screen.getByText('kaboom')).toBeInTheDocument();
    spy.mockRestore();
  });
});
