import { defineConfig } from 'vite';
import { copyFileSync } from 'node:fs';
import { resolve } from 'node:path';

/* Two-entry build: the content script that mounts onto sreality.cz
 * detail pages, and the background service worker that owns every
 * fetch (so host_permissions covers the API origin and the call
 * doesn't fight sreality.cz's CORS).
 *
 * Vite's default `dist/` layout would nest assets under hashed paths
 * — Chrome's manifest expects flat filenames (`content.js`,
 * `background.js`) at the dist root, so we override the output
 * naming and copy manifest.json across as a post-build step. */
export default defineConfig({
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
        for (const name of [
          'manifest.json',
          'icon-16.png', 'icon-48.png', 'icon-128.png',
        ]) {
          copyFileSync(
            resolve(__dirname, name),
            resolve(__dirname, 'dist', name),
          );
        }
      },
    },
  ],
});
