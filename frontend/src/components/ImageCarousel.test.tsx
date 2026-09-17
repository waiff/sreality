/* ImageCarousel — paging a strip of photos, and what it does with one that will
 * not load.
 *
 * The failure this pins is not cosmetic. A portal CDN refuses a cross-origin
 * request for a photo R2 has no copy of yet, and the handler used to answer that
 * by setting `visibility: hidden` on the <img> ELEMENT. React re-renders `src`
 * and leaves an imperatively-set style exactly where it was, so the next frame —
 * and every frame after it — rendered invisible under a counter still reading
 * "3 / 12". On the AUTODEDUP review card, where the operator is judging whether
 * two adverts are the same flat BY their photos, that turns one blocked frame
 * into a member with no evidence at all.
 */

import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import ImageCarousel from './ImageCarousel';

const frames = (...urls: string[]) =>
  urls.map((url) => ({ url, tag: null, confidence: null, renderScore: null }));

describe('<ImageCarousel>', () => {
  it('pages to the next frame after one of them failed to load', () => {
    render(<ImageCarousel images={frames('1.jpg', '2.jpg', '3.jpg')} />);
    fireEvent.error(document.querySelector('img')!);
    fireEvent.click(screen.getByRole('button', { name: 'Next photo' }));
    const img = document.querySelector('img')!;
    expect(img.getAttribute('src')).toBe('2.jpg');
    expect(img.style.visibility).toBe('');
  });

  it('remembers which frame was broken when the operator pages back to it', () => {
    render(<ImageCarousel images={frames('1.jpg', '2.jpg')} fallback={<span>nedostupné</span>} />);
    fireEvent.error(document.querySelector('img')!);
    expect(screen.getByText('nedostupné')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Next photo' }));
    expect(document.querySelector('img')!.getAttribute('src')).toBe('2.jpg');
    fireEvent.click(screen.getByRole('button', { name: 'Previous photo' }));
    expect(screen.getByText('nedostupné')).toBeInTheDocument();
  });

  it('shows the caller\'s labelled tile instead of a blank box', () => {
    render(<ImageCarousel images={frames('1.jpg')} fallback={<span>iDNES · foto nedostupné</span>} />);
    fireEvent.error(document.querySelector('img')!);
    expect(screen.getByText('iDNES · foto nedostupné')).toBeInTheDocument();
    expect(document.querySelector('img')).toBeNull();
  });

  it('keeps its own "no image" placeholder when the caller passes none', () => {
    render(<ImageCarousel images={[]} />);
    expect(screen.getByText('no image')).toBeInTheDocument();
  });
});
