import { defineConfig, loadEnv } from 'vite';
import { copyFileSync, readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
// Shared product brand — the SAME source the SPA + content script use, so the
// chrome://extensions display name stays linked to the one definition.
import { EXTENSION_NAME } from '../frontend/src/lib/brand';
// The portal registry is the single source of truth for "which hosts we run
// on" — content_scripts.matches is computed from it below instead of hand-
// duplicating the host list in manifest.json.
import { PORTALS } from './src/portals';

/* ONE ENTRY PER PASS — `npm run build` runs vite twice, content then background.
 *
 * A manifest-declared content script is a CLASSIC script: MV3 has no module mode
 * for `content_scripts[].js`, so content.js may not contain a static import.
 * Rollup hoists any module shared BETWEEN entries into a chunk, and `portals.ts`
 * is imported by content.ts AND background.ts (since #942, 2026-08-04), so the
 * old single two-entry build emitted `import ... from "./chunks/portals-*.js"` as
 * content.js line 1. Chrome refused the whole script — "Cannot use import
 * statement outside a module" — and the panel silently never mounted on any
 * portal page for six weeks. Nothing caught it: the build was green, the bundle
 * contained every feature, and only chrome://extensions showed the error.
 *
 * Separate passes keep each entry self-contained, and the classic-script guard
 * below fails the build if content.js ever regains a static import.
 *
 * Vite's default `dist/` layout would nest assets under hashed paths — Chrome's
 * manifest expects flat filenames (`content.js`, `background.js`) at the dist
 * root, so we override the output naming and copy manifest.json across as a
 * post-build step. */

/* The content script is IIFE (classic, self-contained); the service worker stays
 * ESM because manifest.json declares `background.type = "module"`. */
const ENTRIES = {
  content: { file: 'src/content.ts', format: 'iife' },
  background: { file: 'src/background.ts', format: 'es' },
} as const;

type EntryName = keyof typeof ENTRIES;
/* Chrome Web Store readiness: host_permissions narrows to just the two
 * origins the background worker actually fetches — the FastAPI service and
 * the Supabase GoTrue auth origin (the SW hits `${SUPABASE_URL}/auth/v1/token`
 * directly for PKCE exchange + refresh). Derived from origin, not the full
 * URL (host_permissions is origin + /*, no path). Falls back to an empty
 * host_permissions when a var is unset (e.g. a forked PR build with no
 * secrets) — least-privilege over silently wildcarding. */
function originPermission(raw: string | undefined): string | null {
  const trimmed = (raw ?? '').trim();
  if (trimmed === '') return null;
  const withScheme = /^https?:\/\//i.test(trimmed) ? trimmed : `https://${trimmed}`;
  try {
    return `${new URL(withScheme).origin}/*`;
  } catch {
    return null;
  }
}

export default defineConfig(({ mode }) => {
  // loadEnv reads chrome-extension/.env (and process.env) the same way Vite
  // populates import.meta.env for the bundled code — the config file itself
  // needs this explicit call since it runs in Node, not the bundle.
  const env = loadEnv(mode, __dirname, '');

  // `mode` selects the entry, so a bare `vite build` can't quietly emit half a
  // bundle: it fails here naming the command that does both passes.
  const entry = ENTRIES[mode as EntryName];
  if (entry == null) {
    throw new Error(
      `build one entry at a time: vite build --mode ${Object.keys(ENTRIES).join('|')} ` +
      `(got "${mode}"). Run \`npm run build\`, which runs both passes in order.`,
    );
  }

  return {
    build: {
      outDir: 'dist',
      // Only the first pass clears dist — the second would delete content.js.
      emptyOutDir: mode === 'content',
      sourcemap: true,
      rollupOptions: {
        input: { [mode]: resolve(__dirname, entry.file) },
        output: {
          entryFileNames: '[name].js',
          assetFileNames: 'assets/[name][extname]',
          format: entry.format,
          // One entry per pass, so nothing is left to split out into a chunk.
          inlineDynamicImports: true,
        },
      },
      target: 'es2022',
      minify: false,
    },
    plugins: [
      {
        name: 'copy-static',
        closeBundle() {
          // manifest.json is the template; its display `name` is stamped from the
          // shared brand, and host_permissions from the two live origins the
          // background worker fetches — both computed at build so chrome://extensions
          // tracks the one source and the store listing never ships a wildcard.
          const manifest = JSON.parse(
            readFileSync(resolve(__dirname, 'manifest.json'), 'utf8'),
          );
          manifest.name = EXTENSION_NAME;
          manifest.host_permissions = [
            originPermission(env.VITE_API_BASE_URL),
            originPermission(env.VITE_SUPABASE_URL),
          ].filter((o): o is string => o != null);
          manifest.content_scripts[0].matches = PORTALS.flatMap((p) =>
            p.hosts.map((h) => `https://${h}/*`),
          );
          /* Chrome only auto-updates when the offered version is STRICTLY greater
           * than the installed one, and the Web Store rejects re-uploading a
           * version that already exists — so a forgotten hand-bump silently
           * strands every installed copy on the old build (PR #942 shipped with
           * no bump). manifest.json's MAJOR.MINOR stays the hand-owned feature
           * declaration; CI stamps the patch from the run number, which is
           * monotonic and never resets. Local builds keep the committed version
           * so a dev reload doesn't churn it. */
          const runNumber = process.env.GITHUB_RUN_NUMBER;
          if (runNumber != null && runNumber !== '') {
            const [major, minor] = String(manifest.version).split('.');
            manifest.version = `${major}.${minor}.${runNumber}`;
          }
          writeFileSync(
            resolve(__dirname, 'dist', 'manifest.json'),
            JSON.stringify(manifest, null, 2) + '\n',
          );
          for (const name of ['icon-16.png', 'icon-48.png', 'icon-128.png']) {
            copyFileSync(
              resolve(__dirname, name),
              resolve(__dirname, 'dist', name),
            );
          }
        },
      },
      {
        /* THE RAIL. Compiles (never runs) the emitted bundle as a CLASSIC script,
         * which is exactly what Chrome does with a manifest-declared content
         * script — so a static import hoisted back into content.js fails the
         * build here, with the same SyntaxError chrome://extensions would show,
         * instead of shipping a bundle that loads nowhere. `new Function` only
         * parses: no DOM, no `chrome`, nothing executes. */
        name: 'assert-classic-content-script',
        closeBundle() {
          if (mode !== 'content') return;
          const built = resolve(__dirname, 'dist', 'content.js');
          try {
            new Function(readFileSync(built, 'utf8'));
          } catch (err) {
            throw new Error(
              'dist/content.js will not load as a classic script, so Chrome ' +
              'will refuse it and the panel will never mount. Something is ' +
              'shared with another entry and got hoisted into a chunk — keep ' +
              'one entry per build pass (see ENTRIES above). Parse error: ' +
              `${(err as Error).message}`,
            );
          }
        },
      },
    ],
  };
});
