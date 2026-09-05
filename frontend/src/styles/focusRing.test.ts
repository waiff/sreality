/* The global :focus-visible ring is now the ONLY keyboard-focus indicator on
 * ~65 controls that used to opt out of it. jsdom loads no CSS, so this guards
 * the rule at the source: if it is deleted or narrowed, every one of those
 * controls goes keyboard-invisible at once and nothing else would notice. */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

// vitest runs from frontend/; import.meta.url is not a file: URL under vite.
const css = readFileSync(join(process.cwd(), 'src/styles/globals.css'), 'utf8');

describe('globals.css focus ring', () => {
  it('declares a global :focus-visible outline in the focus token', () => {
    const rule = css.match(/:focus-visible\s*\{([^}]*)\}/);
    expect(rule, 'the global :focus-visible rule is gone').not.toBeNull();
    expect(rule![1]).toMatch(/outline:\s*2px solid var\(--color-focus\)/);
    expect(rule![1]).toMatch(/outline-offset/);
  });

  it('defines --color-focus in the base theme', () => {
    expect(css).toMatch(/--color-focus:\s*rgba?\(/);
  });
});
