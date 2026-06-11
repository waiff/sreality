/* imageSrc — URL resolution for listing photos.
 *
 * Focused on the CDN fallback (no R2 copy yet, storage_path null), which must
 * append sreality's render-transform: a BARE sdn.cz URL 401s, so without this
 * every not-yet-downloaded sreality photo renders UNAVAILABLE.
 */

import { describe, expect, it } from 'vitest';

import { imageSrc } from './imageUrl';

describe('imageSrc — CDN fallback (storage_path null)', () => {
  it('appends the render-transform to a bare sdn.cz URL', () => {
    expect(
      imageSrc({ sreality_url: 'https://d18-a.sdn.cz/d_18/x/c6cb.jpeg', storage_path: null }),
    ).toBe('https://d18-a.sdn.cz/d_18/x/c6cb.jpeg?fl=res,749,562,3|shr,,20|jpg,90');
  });

  it('leaves an already-transformed sreality URL unchanged', () => {
    const u = 'https://d18-a.sdn.cz/d_18/x/c6cb.jpeg?fl=res,749,562,3|shr,,20|jpg,90';
    expect(imageSrc({ sreality_url: u, storage_path: null })).toBe(u);
  });

  it('completes a prefix transform chain, preserving the rot op', () => {
    expect(
      imageSrc({
        sreality_url: 'https://d18-a.sdn.cz/d_18/x/sw6Lvw.mpo?fl=rot,180,0|',
        storage_path: null,
      }),
    ).toBe('https://d18-a.sdn.cz/d_18/x/sw6Lvw.mpo?fl=rot,180,0|res,749,562,3|shr,,20|jpg,90');
  });

  it('leaves non-sreality URLs (bazos/idnes/bezrealitky) untouched', () => {
    const u = 'https://www.bazos.cz/img/1t/835/218425835.jpg';
    expect(imageSrc({ sreality_url: u, storage_path: null })).toBe(u);
  });
});
