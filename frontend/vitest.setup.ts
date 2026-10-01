/* Vitest setup — runs before any test file loads.
 *
 * Two responsibilities:
 *   1. Stub the two Vite env vars that `lib/supabase.ts` reads at
 *      module-evaluation time. Without these the Supabase client
 *      throws on import, which means any test that transitively
 *      imports queries.ts fails to even collect. Tests don't talk
 *      to Supabase; the stub is purely so the import graph evaluates.
 *   2. Wire @testing-library/jest-dom matchers + unmount any rendered
 *      trees between tests so RTL queries don't leak state across
 *      cases.
 */

import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, vi } from 'vitest';

vi.stubEnv('VITE_SUPABASE_URL', 'https://test.invalid.supabase.co');
vi.stubEnv('VITE_SUPABASE_ANON_KEY', 'test-key-not-used');

// maplibre-gl evaluates `URL.createObjectURL(new Blob(...))` at module
// load to register its web-worker, which jsdom doesn't implement.
// Tests don't render a live map; the stub just lets the module's
// top-level code finish so test files that transitively import maplibre
// (through the filter-controls barrel) can be collected.
if (!('createObjectURL' in URL)) {
  (URL as unknown as { createObjectURL: () => string }).createObjectURL = () =>
    'blob:stub';
}

// jsdom implements no media queries, so `useTokenColors` (which watches
// prefers-color-scheme to re-read the CSS variables on a theme flip) throws on
// mount. Every chart component reads its palette that way; the stub reports
// light mode and a listener that never fires.
if (typeof window.matchMedia !== 'function') {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }),
  });
}

// jsdom 25 has AbortSignal.timeout but not AbortSignal.any, which every
// browser the SPA targets ships and both read adapters use to combine React
// Query's signal with their own deadline (lib/api.ts send(), lib/pgRead.ts).
if (typeof (AbortSignal as { any?: unknown }).any !== 'function') {
  (AbortSignal as unknown as { any: (signals: AbortSignal[]) => AbortSignal }).any = (
    signals,
  ) => {
    const ctrl = new AbortController();
    for (const s of signals) {
      if (s.aborted) {
        ctrl.abort(s.reason);
        break;
      }
      s.addEventListener('abort', () => ctrl.abort(s.reason), { once: true });
    }
    return ctrl.signal;
  };
}

afterEach(() => {
  cleanup();
});
