/* Vitest setup — runs before any test file loads.
 *
 * Stubs the two Vite env vars that `lib/supabase.ts` reads at module-
 * evaluation time. Without these the Supabase client throws on
 * import, which means any test that transitively imports queries.ts
 * fails to even collect. Tests don't talk to Supabase; the stub is
 * purely so the import graph evaluates.
 */

import { vi } from 'vitest';

vi.stubEnv('VITE_SUPABASE_URL', 'https://test.invalid.supabase.co');
vi.stubEnv('VITE_SUPABASE_ANON_KEY', 'test-key-not-used');
