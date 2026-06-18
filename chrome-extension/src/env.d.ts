/// <reference types="vite/client" />

/* Augment ImportMetaEnv with the Path 1 build-time vars (mirrors the
 * VITE_API_* keys in chrome-extension/.env.example). Both are required
 * at runtime; the runtime warns when either is empty. */
interface ImportMetaEnv {
  readonly VITE_API_BASE_URL: string;
  readonly VITE_API_TOKEN: string;
  /* SPA base URL for the "Otevřít v aplikaci" deep-link. Optional — when
   * empty the link simply doesn't render. */
  readonly VITE_APP_BASE_URL: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
