/* imageSrc — URL resolution for listing photos.
 *
 * Focused on the CDN fallback (no R2 copy yet, storage_path null), which must
 * normalise sreality's render-transform onto the URL: a BARE sdn.cz URL 401s,
 * and a stored LEGACY chain would serve the 4:3 crop instead of the master.
 */

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

import { imageSrc } from './imageUrl';

const OPS = 'res,1800,1800,1|shr,,20|jpg,80';

// The SAME vectors tests/test_image_transform_parity.py runs against the Python
// normaliser: the two copies of this logic are compared on BEHAVIOUR, not just on
// the template string, so a drift in either one reds a suite.
interface Probe {
  why: string;
  input: string;
  expected: string;
}
const contract = JSON.parse(
  readFileSync(resolve(process.cwd(), '../tests/fixtures/sreality_transform_probes.json'), 'utf8'),
) as { ops: string; probes: Probe[] };

describe('imageSrc — CDN fallback (storage_path null)', () => {
  it('appends the 1800px whole-frame transform to a bare sdn.cz URL', () => {
    expect(
      imageSrc({ sreality_url: 'https://d18-a.sdn.cz/d_18/x/c6cb.jpeg', storage_path: null }),
    ).toBe(`https://d18-a.sdn.cz/d_18/x/c6cb.jpeg?fl=${OPS}`);
  });

  it('normalises a legacy 749 crop chain onto the 1800 template', () => {
    expect(
      imageSrc({
        sreality_url: 'https://d18-a.sdn.cz/d_18/x/c6cb.jpeg?fl=res,749,562,3|shr,,20|jpg,90',
        storage_path: null,
      }),
    ).toBe(`https://d18-a.sdn.cz/d_18/x/c6cb.jpeg?fl=${OPS}`);
  });

  it('preserves a rot prefix chain in front of the transform', () => {
    expect(
      imageSrc({
        sreality_url: 'https://d18-a.sdn.cz/d_18/x/sw6Lvw.mpo?fl=rot,180,0|',
        storage_path: null,
      }),
    ).toBe(`https://d18-a.sdn.cz/d_18/x/sw6Lvw.mpo?fl=rot,180,0|${OPS}`);
  });

  it('is idempotent — a URL already on the 1800 template is unchanged', () => {
    const u = `https://d18-a.sdn.cz/d_18/x/c6cb.jpeg?fl=${OPS}`;
    expect(imageSrc({ sreality_url: u, storage_path: null })).toBe(u);
  });

  it('leaves non-sreality URLs (bazos/idnes/bezrealitky) untouched', () => {
    const u = 'https://www.bazos.cz/img/1t/835/218425835.jpg';
    expect(imageSrc({ sreality_url: u, storage_path: null })).toBe(u);
  });
});

describe('imageSrc — the shared scraper/SPA probe contract', () => {
  it('pins the same template the Python side downloads through', () => {
    expect(contract.ops).toBe(OPS);
    expect(contract.probes.length).toBeGreaterThan(0);
  });

  it.each(contract.probes)('$why', ({ input, expected }) => {
    expect(imageSrc({ sreality_url: input, storage_path: null })).toBe(expected);
  });
});
