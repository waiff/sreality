/* The no-reconstruction rail (docs/design/portal-listing-url.md).
 *
 * A listing's portal URL is `source_url` on the row, stored by the portal's own
 * parser. The SPA used to rebuild sreality's from display labels — wrong for 14
 * of 48 sub-category codes, and a green test pinned the 404. This census fails
 * the build if a sreality detail URL is ever assembled in app code again. The
 * allowlist is the one file that legitimately mentions the shape: the estimation
 * modal's <input placeholder> examples, which are text, not links. */
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';
import { describe, expect, it } from 'vitest';

import * as portals from './portals';

const ALLOWLIST = new Set(['components/NewEstimationModal.tsx']);

function sources(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) sources(p, out);
    else if (/\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name) && !name.endsWith('.generated.ts')) {
      out.push(p);
    }
  }
  return out;
}

describe('portal URL census', () => {
  it('no app code assembles a sreality detail URL', () => {
    const root = join(process.cwd(), 'src');
    const offenders: string[] = [];
    for (const file of sources(root)) {
      const rel = relative(root, file);
      if (ALLOWLIST.has(rel)) continue;
      if (readFileSync(file, 'utf8').includes('sreality.cz/detail')) offenders.push(rel);
    }
    expect(offenders).toEqual([]);
  });

  it('sees the one sanctioned mention (so a silent path miss cannot pass)', () => {
    const root = join(process.cwd(), 'src');
    const seen = sources(root)
      .map((f) => relative(root, f))
      .filter((rel) => ALLOWLIST.has(rel) && readFileSync(join(root, rel), 'utf8').includes('sreality.cz/detail'));
    expect(seen).toEqual([...ALLOWLIST]);
  });

  it('lib/portals exports labels only — no URL builder survives', () => {
    expect(Object.keys(portals).filter((k) => /url/i.test(k))).toEqual([]);
  });
});
