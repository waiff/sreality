/// <reference types="vite/client" />

/* Augment ImportMetaEnv with the Path 1 build-time vars (mirrors the
 * VITE_* keys in chrome-extension/.env.example). Required at runtime; the
 * runtime warns when a required one is empty. */
interface ImportMetaEnv {
  readonly VITE_API_BASE_URL: string;
  /* The extension's OWN Supabase session (Wave 1) — mirrors the SPA's
   * frontend/src/lib/supabase.ts env var names. Used for the hand-rolled
   * PKCE flow against GoTrue; no supabase-js in this vanilla-TS bundle. */
  readonly VITE_SUPABASE_URL: string;
  readonly VITE_SUPABASE_ANON_KEY: string;
  /* SPA base URL for the "Otevřít v aplikaci" deep-link. Optional — when
   * empty the link simply doesn't render. */
  readonly VITE_APP_BASE_URL: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
