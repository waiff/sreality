/* Single source of truth for the product brand, shared across BOTH browser
 * surfaces — the SPA and the Chrome extension.
 *
 * The SPA imports it natively. The Chrome extension imports THIS SAME FILE
 * (`chrome-extension/src/content.ts` and its `vite.config.ts`) rather than
 * hand-mirroring the name: this module has zero dependencies and no DOM/React,
 * so it is safe to pull into either build, and both builds always have the
 * whole repo checked out. Change the name here and it flows to:
 *   - the SPA header wordmark (components/Shell.tsx, derived from APP_NAME)
 *   - every SPA browser-tab title (lib/pageTitle.tsx)
 *   - the extension panel wordmark (chrome-extension/src/content.ts)
 *   - the extension's chrome://extensions name (manifest, built from
 *     EXTENSION_NAME by chrome-extension/vite.config.ts)
 *
 * Location note: it lives under `frontend/` because the SPA's production Docker
 * build context is `frontend/` only — a repo-root shared file would build in CI
 * but fail the Railway image. The extension reaches in via a relative import,
 * which is fine since the extension is always built from the full repo.
 */

export const APP_NAME = 'Limen Reality';

/** Abbreviation used as the browser-tab title prefix ("LR: …"). */
export const APP_SHORT = 'LR';

/** chrome://extensions display name — brand + what the panel does. */
export const EXTENSION_NAME = `${APP_NAME} — výnos & pipeline`;
