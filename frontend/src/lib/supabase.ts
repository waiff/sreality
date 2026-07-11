import { createClient } from '@supabase/supabase-js';

const url = import.meta.env.VITE_SUPABASE_URL ?? '';
const key = import.meta.env.VITE_SUPABASE_ANON_KEY ?? '';

if (!url || !key) {
  console.warn(
    'Supabase env vars missing. Set VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY.',
  );
}

// Phase 1 auth: persist + auto-refresh the user session, and detect the session
// in the URL (OAuth callback + password-reset links). When no user is signed in
// the client falls back to the anon key exactly as before, so anonymous reads are
// unchanged — this is additive.
export const supabase = createClient(url, key, {
  auth: {
    persistSession: true,
    autoRefreshToken: true,
    detectSessionInUrl: true,
  },
  db: { schema: 'public' },
});

export const isSupabaseConfigured = (): boolean => Boolean(url && key);
