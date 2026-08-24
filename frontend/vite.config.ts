/// <reference types="vitest" />
import { defineConfig } from 'vite';
import { fileURLToPath } from 'node:url';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    target: 'es2022',
    /* NO `manualChunks`, deliberately — it was the cause, not the cure.
     *
     * It used to name two chunks, `maplibre: ['maplibre-gl']` and `recharts: ['recharts']`.
     * The object form claims a listed module AND every dependency of it that nothing else
     * has claimed, and recharts was react/react-dom/scheduler's only *statically* reachable
     * importer — so React got folded into the chunk named `recharts`. From then on every
     * chunk that touched React (31 of them) and the entry itself carried
     * `import{...}from"./recharts-*.js"`, Vite emitted a `modulepreload` for it, and no
     * amount of lazy-importing the chart components could help: recharts was on the
     * critical path of every route because React was inside it.
     *
     * With all three chart consumers and all five map consumers behind `lazyChunk`,
     * Rollup's automatic splitting gets this exactly right on its own: the entry ends up
     * with ZERO static chunk imports, index.html emits ZERO modulepreload links, and both
     * libraries become single shared async chunks pulled only by the components that draw
     * something. Re-adding a hand-written chunk map is what would break it again.
     *
     * The 500 kB chunk-size warning below is expected and accepted: the entry is the whole
     * app shell, and splitting it further only helps a cache that every deploy invalidates
     * anyway (a one-character change rotates every hashed chunk — see lib/lazyChunk.ts). */
  },
  test: {
    // jsdom for component tests (RangeInputs, MultiselectChips, …).
    // Pure-function tests still run here — jsdom adds a few ms of
    // setup but otherwise behaves like node for non-DOM code.
    environment: 'jsdom',
    setupFiles: ['./vitest.setup.ts'],
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
  },
});
