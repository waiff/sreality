import { defineConfig, loadEnv } from 'vite';
import { copyFileSync, readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
// Shared product brand — the SAME source the SPA + content script use, so the
// chrome://extensions display name stays linked to the one definition.
import { EXTENSION_NAME } from '../frontend/src/lib/brand';

/* Two-entry build: the content script that mounts onto sreality.cz
 * detail pages, and the background service worker that owns every
 * fetch (so host_permissions covers the API origin and the call
 * doesn't fight sreality.cz's CORS).
 *
 * Vite's default `dist/` layout would nest assets under hashed paths
 * — Chrome's manifest expects flat filenames (`content.js`,
 * `background.js`) at the dist root, so we override the output
 * naming and copy manifest.json across as a post-build step. */
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

  return {
    build: {
      outDir: 'dist',
      emptyOutDir: true,
      sourcemap: true,
      rollupOptions: {
        input: {
          content: resolve(__dirname, 'src/content.ts'),
          background: resolve(__dirname, 'src/background.ts'),
        },
        output: {
          entryFileNames: '[name].js',
          chunkFileNames: 'chunks/[name]-[hash].js',
          assetFileNames: 'assets/[name][extname]',
          format: 'es',
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
    ],
  };
});
