/* First-visit JS budget, measured the way a browser pays it.
 *
 * The budget this replaces summed `dist/assets/index-*.js` alone and asserted that was
 * "what loads before any lazy import", noting that maplibre and recharts "only count when
 * their route is opened". That was not true, and nothing noticed for as long as it was
 * wrong: both were `modulepreload`ed from index.html on EVERY route, because React had
 * been folded into the chunk named `recharts` by `manualChunks` and the entry statically
 * imported it. The budget read ~239 kB gzip and stayed green while a first visit actually
 * cost ~608 kB. A number that only measures one file cannot see a second file arriving.
 *
 * So this measures the real set: the entry module plus everything index.html tells the
 * browser to preload, plus anything those statically import, transitively. That is exactly
 * what lands before a single lazy route is opened.
 *
 * It also asserts the two heavyweights are absent from that set by CONTENT, not by
 * filename — Rollup names an auto-split chunk after whichever module it hoisted, so the
 * maplibre chunk has shipped as `maplibre-*.js` and as `basemap-*.js` under different
 * configs, and a filename check would have silently stopped matching.
 */

import { gzipSync } from 'node:zlib';
import { readFileSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';

const DIST = join(dirname(new URL(import.meta.url).pathname), '..', 'dist');
const HTML = join(DIST, 'index.html');

/* Gzipped bytes a first visit must not exceed. Set from the measured value with headroom,
 * not aspirationally — a budget nobody can hold gets raised instead of respected. */
const BUDGET_GZIP = 330_000;

/* Marker strings that identify the heavy libraries wherever they end up. */
const FORBIDDEN = [
  { name: 'maplibre-gl', marker: 'MapLibre GL JS' },
  { name: 'recharts', marker: 'recharts-scale' },
];

if (!existsSync(HTML)) {
  console.error(`bundle-budget: no build found at ${HTML} — run \`npm run build\` first.`);
  process.exit(1);
}

const html = readFileSync(HTML, 'utf8');

/* The entry <script type="module"> and every modulepreload: the browser fetches all of
 * them before the app renders. */
const firstVisit = new Set();
for (const re of [
  /<script[^>]+type="module"[^>]+src="([^"]+)"/g,
  /<link[^>]+rel="modulepreload"[^>]+href="([^"]+)"/g,
]) {
  for (const m of html.matchAll(re)) firstVisit.add(m[1].replace(/^\//, ''));
}

if (firstVisit.size === 0) {
  console.error('bundle-budget: parsed no entry script from index.html — check the format.');
  process.exit(1);
}

/* Follow static imports transitively; a statically imported chunk is fetched even without
 * a preload link for it. Dynamic `import("./x.js")` is deliberately NOT followed. */
const seen = new Set();
const queue = [...firstVisit];
while (queue.length) {
  const rel = queue.pop();
  if (seen.has(rel)) continue;
  seen.add(rel);
  const path = join(DIST, rel);
  if (!existsSync(path)) continue;
  const src = readFileSync(path, 'utf8');
  for (const m of src.matchAll(/from"\.\/([A-Za-z0-9_.-]+\.js)"/g)) {
    queue.push(`assets/${m[1]}`);
  }
}

let total = 0;
const rows = [];
for (const rel of [...seen].sort()) {
  const path = join(DIST, rel);
  if (!existsSync(path)) continue;
  const bytes = readFileSync(path);
  const gz = gzipSync(bytes).length;
  total += gz;
  rows.push([rel, gz]);
}

console.log('First-visit JS (entry + preloads + their static imports):');
for (const [rel, gz] of rows) {
  console.log(`  ${rel.padEnd(46)} ${String(gz).padStart(9)} bytes gzip`);
}
console.log(`  ${'TOTAL'.padEnd(46)} ${String(total).padStart(9)} bytes gzip`);

let failed = false;

for (const { name, marker } of FORBIDDEN) {
  const hits = rows
    .map(([rel]) => rel)
    .filter((rel) => readFileSync(join(DIST, rel), 'utf8').includes(marker));
  if (hits.length) {
    console.error(
      `::error::${name} is on the first-visit path (${hits.join(', ')}). It must load only ` +
        `when a component that renders one is mounted. A static import — often via a barrel ` +
        `re-export, or a manualChunks entry that swallows React — puts it back here.`,
    );
    failed = true;
  }
}

if (total > BUDGET_GZIP) {
  console.error(
    `::error::first-visit JS is ${total} bytes gzip, over the ${BUDGET_GZIP} budget.`,
  );
  failed = true;
}

process.exit(failed ? 1 : 0);
