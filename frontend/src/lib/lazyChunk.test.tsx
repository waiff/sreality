/* lazyChunk — the code-splitting chokepoint.
 *
 * The guarantee under test is a negative one: when a chunk fails to load, the
 * user must NOT see an error. The page reloads while React keeps showing the
 * Suspense fallback. Everything else here exists to bound that behaviour — one
 * reload for many concurrent failures, a rate limit instead of a one-shot flag,
 * offline handled as its own case, and storage failures favouring recovery.
 *
 * Two control tests pin the upstream semantics the whole design rests on. Note
 * that vitest runs React's DEVELOPMENT build, which reports an undefined module
 * as "Cannot use 'in' operator…" where the production build says "Cannot read
 * properties of undefined (reading 'default')" — the operator saw the latter.
 * The assertion is on the mechanism (an error reaches the boundary, mentioning
 * `default`), not on the production wording.
 */

import { Suspense } from 'react';
import { act, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import ErrorBoundary from '@/components/ErrorBoundary';
import {
  __resetChunkLoaderForTests,
  lazyChunk,
  loadChunk,
  readChunkEvents,
  StaleBuildError,
} from './lazyChunk';

function Hello() {
  return <p>chunk loaded</p>;
}

const reload = vi.fn();

beforeEach(() => {
  __resetChunkLoaderForTests();
  reload.mockClear();
  window.sessionStorage.clear();
  window.localStorage.clear();
  Object.defineProperty(window, 'location', {
    configurable: true,
    value: { ...window.location, reload, pathname: '/pipeline' },
  });
  Object.defineProperty(window.navigator, 'onLine', {
    configurable: true,
    value: true,
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

/* Renders a lazy component inside a boundary + Suspense and lets pending
 * microtasks flush, so the assertions see the settled tree. */
async function renderLazy(load: () => Promise<{ default: typeof Hello }>) {
  const Lazy = lazyChunk(load);
  render(
    <ErrorBoundary fallback={<p>BOUNDARY</p>}>
      <Suspense fallback={<p>SKELETON</p>}>
        <Lazy />
      </Suspense>
    </ErrorBoundary>,
  );
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe('lazyChunk', () => {
  it('renders the component when the chunk loads', async () => {
    await renderLazy(() => Promise.resolve({ default: Hello }));
    expect(screen.getByText('chunk loaded')).toBeInTheDocument();
    expect(reload).not.toHaveBeenCalled();
  });

  it('holds the Suspense fallback and reloads when the chunk 404s', async () => {
    /* The whole point: no error is shown while the browser navigates. */
    await renderLazy(() => Promise.reject(new Error('Failed to fetch dynamically imported module')));
    expect(screen.getByText('SKELETON')).toBeInTheDocument();
    expect(screen.queryByText('BOUNDARY')).toBeNull();
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it('reloads once for many concurrent chunk failures', async () => {
    /* Listing Detail fires six lazy loads at once; after a deploy all six
     * reject. Without the in-flight flag each would call reload(). */
    const fail = () => loadChunk(() => Promise.reject(new Error('404')));
    await act(async () => {
      void Promise.all([fail(), fail(), fail(), fail(), fail(), fail()]);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it('surfaces a StaleBuildError instead of reloading again within the interval', async () => {
    window.sessionStorage.setItem('limen:chunkReloadAt', String(Date.now()));
    await expect(loadChunk(() => Promise.reject(new Error('404')))).rejects.toBeInstanceOf(
      StaleBuildError,
    );
    expect(reload).not.toHaveBeenCalled();
  });

  it('reloads again once the rate-limit interval has passed', async () => {
    window.sessionStorage.setItem(
      'limen:chunkReloadAt',
      String(Date.now() - 61_000),
    );
    await act(async () => {
      void loadChunk(() => Promise.reject(new Error('404')));
      await Promise.resolve();
    });
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it('does not reload a working app into the offline page', async () => {
    Object.defineProperty(window.navigator, 'onLine', {
      configurable: true,
      value: false,
    });
    const err = await loadChunk(() => Promise.reject(new Error('net down'))).catch(
      (e: unknown) => e,
    );
    expect(err).toBeInstanceOf(StaleBuildError);
    expect((err as StaleBuildError).reason).toBe('offline');
    expect(reload).not.toHaveBeenCalled();
  });

  it('still reloads when sessionStorage throws (Safari lockdown)', async () => {
    /* The old handler read storage first and outside a try, so a throw disabled
     * recovery entirely. Storage failures must favour the reload. */
    vi.spyOn(window.sessionStorage, 'getItem').mockImplementation(() => {
      throw new Error('storage disabled');
    });
    vi.spyOn(window.sessionStorage, 'setItem').mockImplementation(() => {
      throw new Error('storage disabled');
    });
    await act(async () => {
      void loadChunk(() => Promise.reject(new Error('404')));
      await Promise.resolve();
    });
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it('renders the user-facing copy, not a raw TypeError, for a StaleBuildError', async () => {
    window.sessionStorage.setItem('limen:chunkReloadAt', String(Date.now()));
    const Lazy = lazyChunk(() => Promise.reject(new Error('404')));
    render(
      <ErrorBoundary>
        <Suspense fallback={<p>SKELETON</p>}>
          <Lazy />
        </Suspense>
      </ErrorBoundary>,
    );
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByText(/newer version of the app was released/i)).toBeInTheDocument();
    expect(screen.queryByText(/TypeError/)).toBeNull();
  });

  it('records chunk events for the operator, capped', async () => {
    window.sessionStorage.setItem('limen:chunkReloadAt', String(Date.now()));
    for (let i = 0; i < 25; i++) {
      await loadChunk(() => Promise.reject(new Error('404'))).catch(() => {});
    }
    const events = readChunkEvents();
    expect(events.length).toBe(20);
    expect(events.at(-1)).toMatchObject({ kind: 'blocked', path: '/pipeline' });
  });
});

describe('control: the upstream semantics this design relies on', () => {
  it('React.lazy throws about `default` when a module resolves to undefined', async () => {
    /* This is what Vite's preventDefault path produced and what the deleted
     * main.tsx handler turned every stale chunk into. Dev-build wording differs
     * from production; assert on the mechanism. */
    const Lazy = lazyChunk(
      () => Promise.resolve(undefined) as unknown as Promise<{ default: typeof Hello }>,
    );
    render(
      <ErrorBoundary fallback={<p>BOUNDARY</p>}>
        <Suspense fallback={<p>SKELETON</p>}>
          <Lazy />
        </Suspense>
      </ErrorBoundary>,
    );
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByText('BOUNDARY')).toBeInTheDocument();
  });

  it('a never-settling payload keeps React suspended indefinitely', async () => {
    const Lazy = lazyChunk(() => new Promise<{ default: typeof Hello }>(() => {}));
    render(
      <ErrorBoundary fallback={<p>BOUNDARY</p>}>
        <Suspense fallback={<p>SKELETON</p>}>
          <Lazy />
        </Suspense>
      </ErrorBoundary>,
    );
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });
    expect(screen.getByText('SKELETON')).toBeInTheDocument();
    expect(screen.queryByText('BOUNDARY')).toBeNull();
  });
});
