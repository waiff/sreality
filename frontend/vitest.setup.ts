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

afterEach(() => {
  cleanup();
});
