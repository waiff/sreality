/* Every `role="dialog"` outside the primitive must be bespoke chrome over
 * lib/useDialog. The per-line lint exemption says so in prose; this proves
 * the import, so a future hand-rolled modal wearing the same comment fails
 * here instead of shipping. Companion to eslint.config.js's dialog ban. */
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';
import { describe, expect, it } from 'vitest';

function sources(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) sources(p, out);
    else if (name.endsWith('.tsx') && !name.endsWith('.test.tsx')) out.push(p);
  }
  return out;
}

// The ban forces every JSX `role="dialog"` behind a per-line exemption, so
// the exemption line followed by the attribute IS the site (prose mentions
// of the attribute in comments do not match).
const BESPOKE_SITE = /eslint-disable-next-line no-restricted-syntax[^\n]*\n\s*role="dialog"/;

describe('role="dialog" census', () => {
  it('every bespoke dialog site runs on lib/useDialog', () => {
    const root = join(process.cwd(), 'src');
    const offenders: string[] = [];
    for (const file of sources(root)) {
      const rel = relative(root, file);
      if (rel === 'components/Dialog.tsx') continue;
      const src = readFileSync(file, 'utf8');
      if (!BESPOKE_SITE.test(src)) continue;
      if (!src.includes("from '@/lib/useDialog'")) offenders.push(rel);
    }
    expect(offenders).toEqual([]);
  });

  it('sees the two sanctioned bespoke sites (so a silent regex miss cannot pass)', () => {
    const root = join(process.cwd(), 'src');
    const sites = sources(root)
      .map((f) => relative(root, f))
      .filter((rel) => rel !== 'components/Dialog.tsx' && BESPOKE_SITE.test(readFileSync(join(root, rel), 'utf8')))
      .sort();
    expect(sites).toEqual(['components/ImageLightbox.tsx', 'components/estimation/RunPanel.tsx']);
  });
});
